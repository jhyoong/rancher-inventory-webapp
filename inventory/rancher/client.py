import ssl
from urllib.parse import parse_qsl

import httpx

from inventory.settings import Site


class RancherError(Exception):
    def __init__(self, message: str, status: int | None, url: str):
        super().__init__(message)
        self.status = status
        self.url = url


class RancherClient:
    def __init__(self, site: Site, token: str, timeout_s: float):
        if not site.tls_verify:
            verify: bool | ssl.SSLContext = False
        elif site.ca_file:
            verify = ssl.create_default_context(cafile=site.ca_file)
        else:
            verify = True
        self._base = site.url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            verify=verify,
            timeout=timeout_s,
        )

    async def __aenter__(self) -> "RancherClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise RancherError(
                f"request failed: {type(e).__name__} for {url}", None, url
            ) from None
        if resp.status_code != 200:
            raise RancherError(f"HTTP {resp.status_code} for {url}", resp.status_code, url)
        try:
            return resp.json()
        except ValueError:
            raise RancherError(f"invalid JSON from {url}", resp.status_code, url) from None

    async def whoami(self) -> str:
        url = f"{self._base}/v3/users"
        data = await self.get_json("/v3/users", {"me": "true"})
        try:
            return data["data"][0]["username"]
        except (KeyError, IndexError, TypeError):
            raise RancherError(f"unexpected response from {url}", 200, url) from None

    async def list_clusters(self, page_size: int | None = None) -> list[dict]:
        """Every cluster, following pagination.next. Any page error raises."""
        path: str | None = "/v3/clusters"
        params: dict | None = {"limit": page_size} if page_size else None
        clusters: list[dict] = []
        seen: set[str] = set()
        while path:
            url = f"{self._base}{path}"
            data = await self.get_json(path, params)
            page = data.get("data") if isinstance(data, dict) else None
            if not isinstance(page, list) or not all(isinstance(c, dict) and "id" in c for c in page):
                raise RancherError(f"unexpected response from {url}", 200, url)
            clusters.extend(page)
            nxt = (data.get("pagination") or {}).get("next")
            if not nxt:
                break
            if not nxt.startswith(self._base + "/"):
                raise RancherError(f"pagination link to another host from {url}", 200, url)
            path, params = nxt[len(self._base):], None
            if path in seen:
                raise RancherError(f"pagination loop at {url}", 200, url)
            seen.add(path)
        return clusters

    async def list_kind(self, cluster_id: str, path: str, page_size: int = 500) -> list[dict]:
        base, _, query = f"/k8s/clusters/{cluster_id}{path}".partition("?")
        url = base
        # httpx replaces a query string when params are given, so merge them here.
        extra = dict(parse_qsl(query, keep_blank_values=True))
        items: list[dict] = []
        token = ""
        seen: set[str] = set()
        while True:
            params: dict = {**extra, "limit": page_size}
            if token:
                params["continue"] = token
            data = await self.get_json(url, params)
            page = data.get("items") if isinstance(data, dict) else None
            if not isinstance(page, list):
                # null items is a valid empty list; anything else is not a list response
                if isinstance(data, dict) and "items" in data and page is None:
                    page = []
                else:
                    raise RancherError(f"no items list in response from {self._base}{url}", 200, f"{self._base}{url}")
            items.extend(page)
            token = (data.get("metadata") or {}).get("continue") or ""
            if not token:
                return items
            if token in seen:
                raise RancherError(f"continue token repeated for {self._base}{url}", 200, f"{self._base}{url}")
            seen.add(token)
