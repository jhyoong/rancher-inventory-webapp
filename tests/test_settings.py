import pytest

from inventory.settings import ConfigError, load_sites, mask_token, site_token


def test_dev_site_loads(settings):
    sites = load_sites(settings)
    assert [s.name for s in sites] == ["dev"]
    assert sites[0].url == "https://rancher.local"
    assert sites[0].tls_verify is False


def test_site_token_missing(monkeypatch, dev_site):
    monkeypatch.delenv("RANCHER_DEV_TOKEN")
    with pytest.raises(ConfigError, match="RANCHER_DEV_TOKEN"):
        site_token(dev_site)


def test_duplicate_site_names_rejected(tmp_path, monkeypatch, settings):
    sites_file = tmp_path / "sites.yaml"
    sites_file.write_text(
        "sites:\n"
        "  - {name: a, url: 'https://a.example/', token_env: RANCHER_A_TOKEN}\n"
        "  - {name: a, url: 'https://b.example', token_env: RANCHER_B_TOKEN}\n"
    )
    monkeypatch.setenv("SITES_FILE", str(sites_file))
    with pytest.raises(ConfigError, match="duplicate"):
        load_sites(type(settings)())


def test_mask_token():
    assert mask_token("token-k6lm8:secretvalue") == "token-k6lm8:****"


@pytest.mark.parametrize("value", ["2am", "24:00", "02:60", "2:00", "02:00:00", ""])
def test_collect_daily_at_rejected(value, monkeypatch):
    from inventory.settings import Settings

    monkeypatch.setenv("COLLECT_DAILY_AT", value)
    with pytest.raises(ValueError, match="must be HH:MM"):
        Settings()


@pytest.mark.parametrize("value", ["00:00", "02:00", "23:59"])
def test_collect_daily_at_accepted(value, monkeypatch):
    from inventory.settings import Settings

    monkeypatch.setenv("COLLECT_DAILY_AT", value)
    assert Settings().collect_daily_at == value


def test_cli_reports_bad_daily_at_plainly(monkeypatch, capsys):
    from inventory.cli import main

    monkeypatch.setenv("COLLECT_DAILY_AT", "2am")
    assert main(["check-config"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("config error: COLLECT_DAILY_AT: must be HH:MM") and "Traceback" not in err


def test_site_url_trailing_slash_stripped(tmp_path, monkeypatch, settings):
    f = tmp_path / "sites.yaml"
    f.write_text("sites:\n  - {name: a, url: 'https://a.example//', token_env: RANCHER_A_TOKEN}\n")
    monkeypatch.setenv("SITES_FILE", str(f))
    assert load_sites(type(settings)())[0].url == "https://a.example"


def test_mask_token_without_colon():
    assert mask_token("plainsecret") == "****"


def test_tests_run_from_another_directory(tmp_path):
    """conftest finds .env and the dev config by absolute path."""
    import subprocess
    import sys

    from tests.conftest import ROOT

    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(ROOT / "tests/test_settings.py::test_dev_site_loads"),
         str(ROOT / "tests/test_settings.py::test_site_token_missing")],
        cwd=tmp_path, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
