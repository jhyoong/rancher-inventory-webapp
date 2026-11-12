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
        data = await self.get_json("/v3/users", {"me": "true"})
        return data["data"][0]["username"]

    async def list_clusters(self) -> list[dict]:
        return (await self.get_json("/v3/clusters"))["data"]

    async def list_kind(self, cluster_id: str, path: str, page_size: int = 500) -> list[dict]:
        base, _, query = f"/k8s/clusters/{cluster_id}{path}".partition("?")
        url = base
        # httpx replaces a query string when params are given, so merge them here.
        extra = dict(parse_qsl(query, keep_blank_values=True))
        items: list[dict] = []
        token = ""
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
