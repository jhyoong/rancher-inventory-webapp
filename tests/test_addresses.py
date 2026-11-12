import pytest

from inventory.collector.addresses import Address, extract_addresses
from inventory.collector.transform import trim


def test_service_roles_literal():
    svc = {
        "spec": {
            "type": "LoadBalancer",
            "clusterIPs": ["10.43.0.5", "None"],
            "externalIPs": ["1.2.3.4"],
            "ports": [{"port": 443, "protocol": "TCP"}, {"port": 80}],
        },
        "status": {"loadBalancer": {"ingress": [{"ip": "5.6.7.8"}, {"hostname": "lb.example.com"}, {"ip": "5.6.7.8"}]}},
    }  # fmt: skip
    assert extract_addresses("services", svc) == [
        Address("cluster-ip", "10.43.0.5"),
        Address("external-ip", "1.2.3.4"),
        Address("lb", "5.6.7.8", "443/TCP,80/TCP"),
        Address("lb", "lb.example.com", "443/TCP,80/TCP"),
    ]
    assert extract_addresses("services", {"spec": {"clusterIPs": ["None"]}}) == []


def test_ingress_roles_literal():
    ing = {
        "spec": {"rules": [{"host": "a.example.com"}, {}], "tls": [{"hosts": ["a.example.com", "b.example.com"]}]},
        "status": {"loadBalancer": {"ingress": [{"ip": "9.9.9.9"}]}},
    }  # fmt: skip
    assert extract_addresses("ingresses", ing) == [
        Address("ingress-lb", "9.9.9.9"),
        Address("ingress-host", "a.example.com"),
        Address("ingress-host", "b.example.com"),
    ]




@pytest.mark.live
def test_node_addresses(dev_lists, kinds):
    for o in dev_lists["nodes"]:
        internal = next(a["address"] for a in o["status"]["addresses"] if a["type"] == "InternalIP")
        got = trim(kinds["nodes"], o).addresses
        assert Address("node-internal", internal) in got


@pytest.mark.live
def test_service_cluster_ips(dev_lists, kinds):
    for o in dev_lists["services"]:
        roles = [a.role for a in trim(kinds["services"], o).addresses if a.role == "cluster-ip"]
        headless = (o["spec"].get("clusterIP") or "") == "None"
        assert (roles == []) if headless else (len(roles) >= 1)


@pytest.mark.live
def test_ingress_nginx_lb(collected_db):
    from inventory.db.connection import connect

    conn = connect(collected_db.db_path)
    try:
        rows = conn.execute("SELECT role FROM addresses WHERE address = '192.168.50.210'").fetchall()
    finally:
        conn.close()
    assert rows, "no address row for 192.168.50.210"
