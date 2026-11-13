import json
import shutil

import pytest

from inventory.collector.netpolicy import derive_service
from inventory.collector.rules import load_rules
from inventory.collector.transform import trim
from inventory.web.migration import floating_ips, lb_implementation, service_signals

ADDRESS_ROLES = ("lb", "ingress-lb", "external-ip", "node-external")


@pytest.mark.live
def test_source_ranges_dev(dev_lists):
    for s in dev_lists["services"]:
        assert derive_service(s)["source_ranges"] == len((s.get("spec") or {}).get("loadBalancerSourceRanges") or [])


@pytest.mark.live
def test_floating_ips_dev(collected_db, open_db, settings):
    conn = open_db(collected_db.db_path)
    fi = floating_ips(conn, load_rules(settings))[("dev", "local")]
    stored = {
        r[0]
        for r in conn.execute(
            "SELECT a.address FROM addresses a JOIN resources r ON r.id = a.resource_id"
            " WHERE r.site='dev' AND r.cluster_id='local' AND a.role IN (?, ?, ?, ?)",
            ADDRESS_ROLES,
        )
    }
    assert stored <= set(fi["addresses"])
    assert fi["control_plane"] is None
    assert fi["total"] == len(fi["addresses"])
    assert "192.168.50.210" in fi["addresses"]


@pytest.mark.live
def test_same_ip_counted_once(collected_db, tmp_path, open_db, settings):
    copy = tmp_path / "copy.db"
    shutil.copy(collected_db.db_path, copy)
    conn = open_db(copy)
    rules = load_rules(settings)
    before = floating_ips(conn, rules)[("dev", "local")]
    assert before["ingress_lb"] > 0
    service_id = conn.execute("SELECT id FROM resources WHERE kind='services' AND cluster_id='local'").fetchone()[0]
    for (address,) in conn.execute(
        "SELECT DISTINCT a.address FROM addresses a JOIN resources r ON r.id = a.resource_id"
        " WHERE r.cluster_id='local' AND a.role='ingress-lb'"
    ).fetchall():
        conn.execute("INSERT OR IGNORE INTO addresses (resource_id, role, address) VALUES (?, 'lb', ?)", (service_id, address))
    conn.commit()
    after = floating_ips(conn, rules)[("dev", "local")]
    assert after["total"] == before["total"]
    assert after["ingress_lb"] == 0


@pytest.mark.real_fixtures
def test_real_lb_services(real_fixture, kinds, settings, capsys):
    rules = load_rules(settings)
    domains = [d for impl in rules.network["lb_implementations"].values() for d in impl["annotation_domains"]]
    from inventory.collector.annotations import matches

    items = real_fixture("services-lb.json")
    items = items.get("items", items) if isinstance(items, dict) else items
    for obj in items:
        if (obj.get("spec") or {}).get("type") != "LoadBalancer":
            continue
        t = trim(kinds["services"], obj, rules)
        impl = lb_implementation(t.data["lb_class"], t.data["annotation_keys"], rules)
        print(obj["metadata"]["name"], impl)
        if t.data["lb_class"] or any(matches(k, d) for k in t.data["annotation_keys"] for d in domains):
            assert impl != "unidentified"


@pytest.mark.live
def test_service_signals_from_real_copy(dev_lists, settings):
    svc = json.loads(json.dumps(dev_lists["services"][0]))
    svc["spec"]["type"] = "LoadBalancer"
    svc["spec"]["externalTrafficPolicy"] = "Local"
    svc["spec"]["loadBalancerIP"] = svc["spec"]["clusterIP"]
    svc.setdefault("metadata", {}).setdefault("annotations", {})["metallb.universe.tf/allow-shared-ip"] = "x"
    derived = derive_service(svc)
    signals = service_signals(
        {
            "type": svc["spec"]["type"],
            "lb_class": derived["lb_class"],
            "annotation_keys": derived["annotation_keys"],
            "external_traffic_policy": derived["external_traffic_policy"],
            "load_balancer_ip": svc["spec"]["loadBalancerIP"],
            "source_ranges": derived["source_ranges"],
        },
        load_rules(settings),
    )
    assert signals == ["pinned-ip", "local-traffic-policy", "shared-ip"]


@pytest.mark.live
def test_pinned_load_balancer_ip_counted(collected_db, tmp_path, open_db, settings):
    copy = tmp_path / "copy.db"
    shutil.copy(collected_db.db_path, copy)
    conn = open_db(copy)
    rules = load_rules(settings)
    before = floating_ips(conn, rules)[("dev", "local")]
    # dev has no LoadBalancer service: turn a real service into one on the copy
    service_id = conn.execute("SELECT id FROM resources WHERE kind='services' AND cluster_id='local' ORDER BY id").fetchone()[0]
    conn.execute("UPDATE resources SET data = json_set(data, '$.spec.type', 'LoadBalancer') WHERE id = ?", (service_id,))

    def pin(address):
        conn.execute("UPDATE resources SET data = json_set(data, '$.spec.loadBalancerIP', ?) WHERE id = ?", (address, service_id))
        conn.commit()
        return floating_ips(conn, rules)[("dev", "local")]

    new = pin("10.255.255.254")
    assert new["lb"] == before["lb"] + 1
    assert new["total"] == before["total"] + 1
    assert "10.255.255.254" in new["addresses"]

    existing = conn.execute(
        "SELECT a.address FROM addresses a JOIN resources r ON r.id = a.resource_id"
        " WHERE r.cluster_id='local' AND a.role='ingress-lb'"
    ).fetchone()[0]
    assert pin(existing)["total"] == before["total"]
