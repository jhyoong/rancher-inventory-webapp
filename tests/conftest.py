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
