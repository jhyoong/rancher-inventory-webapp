import pytest

from inventory.collector.rules import load_rules
from inventory.db.connection import connect
from inventory.web import queries

pytestmark = pytest.mark.live


def test_inventory_has_local(admin_client_collected):
    r = admin_client_collected.get("/inventory")
    assert r.status_code == 200 and "local" in r.text and "stateful-helm-crd" in r.text and "last ok" in r.text


def test_inventory_filters(admin_client_collected):
    assert "stateful-helm-crd" in admin_client_collected.get("/inventory?flag=hostpath&cls=stateful-helm-crd").text
    assert "No clusters match" in admin_client_collected.get("/inventory?cls=empty").text
    assert "No clusters match" in admin_client_collected.get("/inventory?label=nope=1").text
    assert admin_client_collected.get("/inventory?flag=bogus").status_code == 400


def test_network_search_node_ip(admin_client_collected, dev_lists):
    ip = next(a["address"] for a in dev_lists["nodes"][0]["status"]["addresses"] if a["type"] == "InternalIP")
    assert "local" in admin_client_collected.get(f"/network?q={ip}").text


def test_network_prefix(collected_db):
    conn = connect(collected_db.db_path, readonly=True)
    try:
        assert len(queries.search_addresses(conn, "192.168.50.")) >= 1
        assert queries.search_addresses(conn, "%") == []
        assert queries.duplicate_addresses(conn) == []
        assert len(queries.load_balancers(conn)) >= 1
    finally:
        conn.close()


def test_helm_page_lists_rancher_chart(admin_client_collected):
    r = admin_client_collected.get("/helm")
    assert "rancher-webhook" in r.text and "cert-manager" in r.text
    assert "deployed" in admin_client_collected.get("/helm?chart=cert-manager").text


def test_helm_matches_catalog(collected_db, dev_lists):
    conn = connect(collected_db.db_path, readonly=True)
    try:
        assert sum(c["releases"] for c in queries.helm_charts(conn)) == len(dev_lists["helmreleases"])
    finally:
        conn.close()


def test_crds_custom_toggle(admin_client_collected):
    assert "management.cattle.io" in admin_client_collected.get("/crds").text
    assert "management.cattle.io" not in admin_client_collected.get("/crds?custom_only=1").text


def test_annotations_page(collected_db, admin_client_collected):
    conn = connect(collected_db.db_path, readonly=True)
    try:
        keys = queries.annotation_keys(conn)
        assert keys
        assert queries.annotation_objects(conn, keys[0]["key"])
    finally:
        conn.close()
    assert admin_client_collected.get("/annotations").status_code == 200


def test_cluster_page_profile(admin_client_collected):
    r = admin_client_collected.get("/clusters/dev/local")
    assert "Namespaces" in r.text and "Helm releases" in r.text and "stateful-helm-crd" in r.text


def test_home_class_card(admin_client_collected):
    assert "Clusters per class" in admin_client_collected.get("/").text
