import asyncio

import pytest

from inventory.rancher.client import RancherClient, RancherError
from inventory.settings import site_token

pytestmark = pytest.mark.live


def _client(dev_site, settings):
    return RancherClient(dev_site, site_token(dev_site), settings.request_timeout_s)


def _run(dev_site, settings, fn):
    async def go():
        async with _client(dev_site, settings) as c:
            return await fn(c)

    return asyncio.run(go())


def _error(dev_site, settings, path):
    async def go():
        async with _client(dev_site, settings) as c:
            await c.list_kind("local", path)

    with pytest.raises(RancherError) as exc:
        asyncio.run(go())
    return exc.value


def test_whoami(dev_site, settings):
    assert _run(dev_site, settings, lambda c: c.whoami()) == "inventory-reader"


def test_list_clusters_has_local(dev_site, settings):
    clusters = _run(dev_site, settings, lambda c: c.list_clusters())
    assert "local" in {c["id"] for c in clusters}


def test_pagination_returns_all(dev_site, settings):
    async def go(c):
        small = await c.list_kind("local", "/api/v1/pods", page_size=5)
        big = await c.list_kind("local", "/api/v1/pods", page_size=500)
        return small, big

    small, big = _run(dev_site, settings, go)
    names = lambda items: sorted(
        (i["metadata"]["namespace"], i["metadata"]["name"]) for i in items
    )
    assert len(big) > 5
    assert names(small) == names(big)


def test_secrets_forbidden(dev_site, settings):
    assert _error(dev_site, settings, "/api/v1/secrets").status == 403


def test_unknown_group_404(dev_site, settings):
    err = _error(dev_site, settings, "/apis/example.invalid/v1")
    assert err.status == 404


def test_token_not_in_error(dev_site, settings):
    err = _error(dev_site, settings, "/api/v1/secrets")
    assert site_token(dev_site) not in str(err)


@pytest.mark.live
def test_list_kind_without_items_is_an_error(live_loop, live_client):
    """A 200 JSON body with no items list must not look like an empty kind."""
    with pytest.raises(RancherError, match="no items list"):
        live_loop.run_until_complete(live_client.list_kind("local", "/version"))


def test_list_clusters_reads_all_pages(dev_site, settings):
    async def go(c):
        return await c.list_clusters(page_size=1), await c.list_clusters()

    small, big = _run(dev_site, settings, go)
    assert sorted(c["id"] for c in small) == sorted(c["id"] for c in big)


def test_bad_continue_token_is_error(live_loop, live_client):
    with pytest.raises(RancherError) as exc:
        live_loop.run_until_complete(
            live_client.get_json("/k8s/clusters/local/api/v1/pods", params={"limit": 1, "continue": "bogus"})
        )
    assert 400 <= exc.value.status < 500


def test_unexpected_body_is_rancher_error(dev_site, settings, monkeypatch):
    """whoami and list_clusters get a real body of the wrong shape (the /v3 root)."""

    async def go(c):
        real = c.get_json
        monkeypatch.setattr(c, "get_json", lambda path, params=None: real("/v3/settings/server-version"))
        errors = []
        for fn in (c.whoami, c.list_clusters):
            try:
                await fn()
            except RancherError as e:
                errors.append(e)
        return errors

    assert len(_run(dev_site, settings, go)) == 2
