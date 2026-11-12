from pathlib import Path

import pytest

from inventory.settings import Settings, load_env_file, load_sites

ROOT = Path(__file__).resolve().parent.parent
load_env_file(ROOT / ".env")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SITES_FILE", str(ROOT / "dev/config/sites.yaml"))
    monkeypatch.setenv("DEFAULTS_DIR", str(ROOT / "config/defaults"))
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
        sites_file=ROOT / "dev/config/sites.yaml",
        defaults_dir=ROOT / "config/defaults",
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
        sites_file=ROOT / "dev/config/sites.yaml",
        defaults_dir=ROOT / "config/defaults",
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


# ---- web app fixtures ----

TEST_PASSWORD = "test-admin-password"


@pytest.fixture
def test_password():
    return TEST_PASSWORD


def _web_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        sites_file=ROOT / "dev/config/sites.yaml",
        defaults_dir=ROOT / "config/defaults",
        collector_enabled=False,
        cookie_secure=False,  # TestClient talks plain http
        admin_user="admin",
        admin_password=TEST_PASSWORD,
    )


def _login(client, settings: Settings, username: str, password: str):
    from inventory.web.auth import csrf_token

    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.csrf = csrf_token(settings.session_secret, client.cookies["session"])
    return client


@pytest.fixture
def web_settings(tmp_path):
    return _web_settings(tmp_path)


@pytest.fixture
def web_app(web_settings):
    from inventory.web.app import create_app

    return create_app(web_settings, None)


@pytest.fixture
def client(web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as c:
        yield c


@pytest.fixture
def admin_client(client, web_settings):
    return _login(client, web_settings, "admin", TEST_PASSWORD)


@pytest.fixture
def viewer_client(web_app, web_settings):
    from fastapi.testclient import TestClient

    from inventory.db.connection import connect
    from inventory.web.auth import create_user

    with TestClient(web_app) as c:
        conn = connect(web_settings.db_path)
        try:
            create_user(conn, "viewer", "viewer-password-1", "viewer")
        finally:
            conn.close()
        yield _login(c, web_settings, "viewer", "viewer-password-1")


@pytest.fixture(scope="session")
def collected_db(tmp_path_factory):
    """Settings for a session tmp DB after one real full run against dev."""
    import asyncio

    from inventory.collector.runner import Scope, create_run, execute_run
    from inventory.db.connection import connect
    from inventory.db.migrate import migrate

    settings = _web_settings(tmp_path_factory.mktemp("collected"))
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        run_id = create_run(conn, "manual", "test", Scope())
    finally:
        conn.close()
    assert asyncio.run(execute_run(settings, run_id)) == "ok"
    return settings


@pytest.fixture
def admin_client_collected(collected_db):
    from fastapi.testclient import TestClient

    from inventory.web.app import create_app

    with TestClient(create_app(collected_db, None)) as c:
        yield _login(c, collected_db, "admin", TEST_PASSWORD)


@pytest.fixture(scope="session")
def dev_lists(live_loop, live_client):
    """Kind name -> raw items from the dev `local` cluster, for every kind in kinds.yaml."""
    from inventory.collector.kinds import load_kinds

    settings = Settings(
        sites_file=ROOT / "dev/config/sites.yaml",
        defaults_dir=ROOT / "config/defaults",
        collector_enabled=False,
    )
    kinds = load_kinds(settings)

    async def go():
        return {k: await live_client.list_kind("local", s.path) for k, s in kinds.items()}

    return live_loop.run_until_complete(go())


@pytest.fixture
def open_db():
    """connect() for tests; every connection is closed after the test."""
    from inventory.db.connection import connect

    opened = []

    def _open(path, readonly: bool = False):
        conn = connect(path, readonly=readonly)
        opened.append(conn)
        return conn

    yield _open
    for conn in opened:
        conn.close()
