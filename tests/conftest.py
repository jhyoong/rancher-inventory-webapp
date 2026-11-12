from pathlib import Path

import pytest

from inventory.settings import Settings, load_env_file, load_sites

load_env_file(Path(".env"))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SITES_FILE", "dev/config/sites.yaml")
    monkeypatch.setenv("DEFAULTS_DIR", "config/defaults")
    monkeypatch.setenv("COLLECTOR_ENABLED", "false")


@pytest.fixture
def settings(env):
    return Settings()


@pytest.fixture
def dev_site(settings):
    return load_sites(settings)[0]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "inventory.db"


@pytest.fixture
def db(db_path):
    from inventory.db.connection import connect
    from inventory.db.migrate import migrate

    conn = connect(db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def dev_live():
    """Live data from the dev Rancher, read once with one client."""
    import asyncio

    from inventory.rancher.client import RancherClient
    from inventory.settings import site_token

    settings = Settings(
        sites_file=Path("dev/config/sites.yaml"),
        defaults_dir=Path("config/defaults"),
        collector_enabled=False,
    )
    site = load_sites(settings)[0]

    async def go():
        async with RancherClient(site, site_token(site), settings.request_timeout_s) as client:
            clusters = await client.list_clusters()
            usage = await client.list_kind("local", "/apis/metrics.k8s.io/v1beta1/nodes")
            return clusters, usage

    clusters, usage = asyncio.run(go())
    return {"clusters": clusters, "node_usage": usage}


@pytest.fixture(scope="session")
def dev_clusters(dev_live):
    return dev_live["clusters"]


@pytest.fixture(scope="session")
def dev_node_usage(dev_live):
    return dev_live["node_usage"]


@pytest.fixture(scope="session")
def local_cluster(dev_clusters):
    return next(c for c in dev_clusters if c["id"] == "local")


@pytest.fixture(scope="session")
def live_loop():
    """One event loop shared by the live collection tests and their client."""
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def live_client(live_loop):
    """One RancherClient bound to live_loop (one connection setup for the session)."""
    from inventory.rancher.client import RancherClient
    from inventory.settings import site_token

    settings = Settings(
        sites_file=Path("dev/config/sites.yaml"),
        defaults_dir=Path("config/defaults"),
        collector_enabled=False,
    )
    site = load_sites(settings)[0]
    client = RancherClient(site, site_token(site), settings.request_timeout_s)
    yield client
    live_loop.run_until_complete(client.aclose())


@pytest.fixture
def kinds(settings):
    from inventory.collector.kinds import load_kinds

    return load_kinds(settings)
