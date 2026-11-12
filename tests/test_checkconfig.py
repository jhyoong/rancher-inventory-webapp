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
