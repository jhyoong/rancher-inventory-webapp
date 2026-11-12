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
