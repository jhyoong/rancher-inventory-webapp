import asyncio
import os

import pytest

from inventory.checkconfig import check_config, format_report
from inventory.cli import main


@pytest.mark.live
def test_check_config_dev(settings):
    [c] = asyncio.run(check_config(settings))
    assert c.reachable and c.user == "inventory-reader"
    assert c.tls == "skipped"
    assert c.clusters >= 1 and c.sample_cluster == "local"
    assert c.kinds["nodes"] == 200


@pytest.mark.live
def test_report_masks_token(settings):
    text = format_report(asyncio.run(check_config(settings)))
    assert os.environ["RANCHER_DEV_TOKEN"].split(":")[1] not in text
    assert "TLS verification is off" in text


@pytest.mark.live
def test_cli_check_config_exit_code_bad_token(monkeypatch):
    monkeypatch.setenv("RANCHER_DEV_TOKEN", "token-bad00:nope")
    assert main(["check-config"]) == 1


def _sites_file(tmp_path, monkeypatch, url, tls_verify):
    f = tmp_path / "sites.yaml"
    f.write_text(
        "sites:\n"
        "  - name: t\n"
        f"    url: {url}\n"
        "    token_env: RANCHER_DEV_TOKEN\n"
        f"    tls_verify: {str(tls_verify).lower()}\n"
    )
    monkeypatch.setenv("SITES_FILE", str(f))


@pytest.mark.live
def test_tls_verify_fails_on_placeholder_cert(settings, tmp_path, monkeypatch):
    _sites_file(tmp_path, monkeypatch, "https://rancher.local", True)
    [c] = asyncio.run(check_config(type(settings)()))
    assert c.tls.startswith("error")
    assert not c.reachable


def test_unreachable_url_tls_unknown(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("RANCHER_DEV_TOKEN", "token-x:y")
    _sites_file(tmp_path, monkeypatch, "https://127.0.0.1:1", True)
    [c] = asyncio.run(check_config(type(settings)()))
    assert c.tls == "unknown"
    assert not c.reachable
    assert main(["check-config"]) == 1


def test_cli_check_config_settings_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SITES_FILE", str(tmp_path / "missing.yaml"))
    assert main(["check-config"]) == 1
    assert "sites file not found" in capsys.readouterr().err
