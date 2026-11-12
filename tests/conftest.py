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
