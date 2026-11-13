import asyncio
import copy

import pytest

from inventory.checkconfig import check_config, format_report
from inventory.collector.provisioning import ProvisioningResult, parse_provisioning, pools_status, read_provisioning

FIXTURE = "provisioning-clusters.json"


def _items(real_fixture):
    return real_fixture(FIXTURE)["items"]


@pytest.mark.real_fixtures
def test_parse_real_pools(real_fixture):
    seen = 0
    for item in _items(real_fixture):
        machine_pools = (item["spec"].get("rkeConfig") or {}).get("machinePools")
        if not machine_pools:
            continue
        key, p = parse_provisioning(item)
        assert key == item["status"]["clusterName"]
        assert [m.name for m in p.pools] == [mp["name"] for mp in machine_pools]
        for m, mp in zip(p.pools, machine_pools):
            assert m.quantity == mp.get("quantity")
            assert m.labels == (mp.get("labels") or {})
            assert m.machine_config_kind == mp["machineConfigRef"]["kind"]
            assert all("=" not in t for t in m.taints)
        seen += 1
    assert seen


@pytest.mark.real_fixtures
def test_parse_without_cluster_name(real_fixture):
    item = copy.deepcopy(_items(real_fixture)[0])
    item["status"].pop("clusterName", None)
    assert parse_provisioning(item) is None


def test_pools_status():
    assert pools_status(None, "c") == "not_collected"
    assert pools_status(ProvisioningResult("forbidden", {}), "c") == "not_collected"
    assert pools_status(ProvisioningResult("ok", {}), "c") == "none"


@pytest.mark.live
def test_read_dev(live_loop, live_client):
    result = live_loop.run_until_complete(read_provisioning(live_client))
    if result.status == "forbidden":
        pytest.skip("P1 not applied: provisioning clusters forbidden (HTTP 403)")
    assert result.status == "ok"
    assert "local" in result.clusters
    assert pools_status(result, "local") == "none"


@pytest.mark.live
def test_read_forbidden_does_not_raise(live_loop, live_client):
    result = live_loop.run_until_complete(read_provisioning(live_client))
    if result.status == "ok":
        pytest.skip("P1 applied; forbidden path not reachable")
    assert result.status == "forbidden"
    assert result.clusters == {}
    assert pools_status(result, "local") == "not_collected"


@pytest.mark.live
def test_check_config_reports_provisioning(settings):
    checks = asyncio.run(check_config(settings))
    text = format_report(checks)
    if checks[0].provisioning == 403:
        assert "provisioning clusters (optional): HTTP 403, machine pools and CNI from Rancher not available" in text
    else:
        assert checks[0].provisioning == 200
        assert "provisioning clusters (optional): readable" in text


@pytest.mark.live
def test_read_skips_malformed_item(live_loop, live_client, caplog):
    class WithMalformedItem:
        """The live client, with a copy of the first real item whose machine pools are malformed added."""

        async def list_kind(self, cluster, path):
            items = await live_client.list_kind(cluster, path)
            bad = copy.deepcopy(items[0])
            bad["status"]["clusterName"] = "malformed-copy"
            bad["spec"]["rkeConfig"] = {"machinePools": ["not a pool"]}
            return [bad, *items]

    result = live_loop.run_until_complete(read_provisioning(WithMalformedItem()))
    if result.status == "forbidden":
        pytest.skip("P1 not applied: provisioning clusters forbidden (HTTP 403)")
    assert result.status == "ok"
    assert "malformed-copy" not in result.clusters and "local" in result.clusters
    assert "skipped malformed provisioning cluster" in caplog.text
