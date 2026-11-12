import json
import shutil

import pytest

from inventory.collector.rules import load_rules
from inventory.db.connection import connect
from inventory.web import queries

pytestmark = pytest.mark.live


def test_inventory_has_local(admin_client_collected):
    r = admin_client_collected.get("/inventory")
    assert r.status_code == 200 and "local" in r.text and "stateful-helm-crd" in r.text and "last ok" in r.text


LOCAL_LINK = 'href="/clusters/dev/local"'


def test_inventory_filters(admin_client_collected):
    get = admin_client_collected.get
    assert LOCAL_LINK in get("/inventory?flag=hostpath&cls=stateful-helm-crd").text
    assert LOCAL_LINK in get("/inventory?site=dev").text
    assert LOCAL_LINK not in get("/inventory?cls=empty").text
    assert LOCAL_LINK not in get("/inventory?site=other").text
    assert LOCAL_LINK not in get("/inventory?label=nope=1").text
    assert get("/inventory?flag=bogus").status_code == 400


def test_inventory_label_filter(collected_db, admin_client_collected, open_db):
    conn = open_db(collected_db.db_path, readonly=True)
    try:
        labels = json.loads(conn.execute("SELECT labels FROM clusters WHERE cluster_id='local'").fetchone()[0])
    finally:
        conn.close()
    assert "provider.cattle.io" in labels
    value = labels["provider.cattle.io"]
    get = admin_client_collected.get
    assert LOCAL_LINK in get("/inventory?label=provider.cattle.io").text
    assert LOCAL_LINK in get(f"/inventory?label=provider.cattle.io%3D{value}").text
    assert LOCAL_LINK not in get(f"/inventory?label=provider.cattle.io%3D{value}-x").text


def test_network_search_node_ip(admin_client_collected, dev_lists):
    ip = next(a["address"] for a in dev_lists["nodes"][0]["status"]["addresses"] if a["type"] == "InternalIP")
    assert "local" in admin_client_collected.get(f"/network?q={ip}").text


def test_network_prefix(collected_db, open_db):
    conn = open_db(collected_db.db_path, readonly=True)
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


def test_helm_matches_catalog(collected_db, dev_lists, open_db):
    conn = open_db(collected_db.db_path, readonly=True)
    try:
        assert sum(c["releases"] for c in queries.helm_charts(conn)) == len(dev_lists["helmreleases"])
    finally:
        conn.close()


def test_crds_custom_toggle(admin_client_collected):
    assert "management.cattle.io" in admin_client_collected.get("/crds").text
    assert "management.cattle.io" not in admin_client_collected.get("/crds?custom_only=1").text


def test_annotations_page(collected_db, admin_client_collected, open_db):
    conn = open_db(collected_db.db_path, readonly=True)
    try:
        keys = queries.annotation_keys(conn)
        assert keys
        assert queries.annotation_objects(conn, keys[0]["key"])
    finally:
        conn.close()
    assert admin_client_collected.get("/annotations").status_code == 200


def test_cluster_page_profile(admin_client_collected, dev_lists):
    r = admin_client_collected.get("/clusters/dev/local")
    assert "Namespaces" in r.text and "Helm releases" in r.text and "stateful-helm-crd" in r.text
    assert "<td>kagent</td>" in r.text  # a real user namespace
    release = dev_lists["helmreleases"][0]["metadata"]["name"]
    assert f"<td>{release}</td>" in r.text


def test_home_class_card(admin_client_collected):
    text = admin_client_collected.get("/").text
    assert "Clusters per class" in text
    assert '<div class="num">1</div><a href="/inventory?cls=stateful-helm-crd">stateful-helm-crd</a>' in text


@pytest.fixture
def failed_clients(collected_db, tmp_path):
    from fastapi.testclient import TestClient

    from inventory.web.app import create_app
    from inventory.web.auth import create_user
    from tests.conftest import TEST_PASSWORD, _login, _web_settings

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    settings = _web_settings(tmp_path)
    assert settings.db_path.parent == tmp_path
    conn = connect(settings.db_path)
    try:
        create_user(conn, "viewer", "viewer-password-1", "viewer")
        conn.execute(
            "INSERT INTO runs (id, started_at, finished_at, status, trigger, scope)"
            " VALUES (999, '2999-01-01T00:00:00+00:00', '2999-01-01T00:01:00+00:00', 'partial', 'manual', '{}')"
        )
        conn.execute(
            "INSERT INTO run_clusters (run_id, site, cluster_id, status, error)"
            " VALUES (999, 'dev', 'local', 'failed', 'boom-unreachable-xyz')"
        )
    finally:
        conn.close()
    with TestClient(create_app(settings, None)) as a, TestClient(create_app(settings, None)) as v:
        yield _login(a, settings, "admin", TEST_PASSWORD), _login(v, settings, "viewer", "viewer-password-1")


@pytest.mark.parametrize("path", ["/inventory", "/clusters/dev/local"])
def test_failed_marker(failed_clients, path):
    admin, viewer = failed_clients
    a, v = admin.get(path).text, viewer.get(path).text
    assert "last collection failed" in a and "last collection failed" in v
    assert "boom-unreachable-xyz" in a
    assert "boom-unreachable-xyz" not in v


def test_network_lists_are_capped(admin_client_collected, monkeypatch, open_db):
    """Each /network list stops at NETWORK_LIMIT rows and says so."""
    conn = open_db(admin_client_collected.app.state.settings.db_path, readonly=True)
    try:
        assert len(queries.node_ips(conn)) == 3
        assert len(queries.node_ips(conn, limit=1)) == 1
        assert len(queries.search_addresses(conn, "1", limit=2)) == 2
    finally:
        conn.close()
    note = "Showing the first 1 rows."
    assert note not in admin_client_collected.get("/network").text
    monkeypatch.setattr(queries, "NETWORK_LIMIT", 1)
    assert note in admin_client_collected.get("/network").text


def test_refresh_site_button(admin_client_collected):
    for path in ("/", "/clusters"):
        text = admin_client_collected.get(path).text
        assert 'name="site" value="dev"' in text and "Refresh this site" in text, path


def test_failed_marker_survives_running_row(failed_clients, collected_db, open_db):
    admin, _ = failed_clients
    conn = open_db(admin.app.state.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (id, started_at, status, trigger, scope)"
            " VALUES (1000, '2999-01-02T00:00:00+00:00', 'running', 'manual', '{}')"
        )
        conn.execute("INSERT INTO run_clusters (run_id, site, cluster_id, status) VALUES (1000, 'dev', 'local', 'running')")
    finally:
        conn.close()
    for path in ("/inventory", "/clusters/dev/local"):
        assert "last collection failed" in admin.get(path).text, path


def test_daemonset_ready_fields(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    try:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT data FROM resources WHERE kind='daemonsets'")]
    finally:
        conn.close()
    assert rows
    for d in rows:
        assert "numberReady" in d["status"] and "desiredNumberScheduled" in d["status"]
        assert "readyReplicas" not in d.get("status", {}) and "replicas" not in d.get("spec", {})


def test_bad_label_json_is_empty(collected_db, tmp_path, open_db):
    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    conn = open_db(tmp_path / "inventory.db")
    try:
        conn.execute("UPDATE clusters SET labels='{not json' WHERE cluster_id='local'")
        assert queries.inventory_rows(conn, label="provider.cattle.io") == []
    finally:
        conn.close()


def test_annotation_objects_cap_note(admin_client_collected, monkeypatch, open_db):
    conn = open_db(admin_client_collected.app.state.settings.db_path, readonly=True)
    try:
        key = queries.annotation_keys(conn)[0]["key"]
    finally:
        conn.close()
    from inventory.web.routes import annotations as ann_route

    monkeypatch.setattr(ann_route, "ANNOTATION_LIMIT", 1, raising=False)
    from urllib.parse import quote

    assert "Showing the first 1 rows." in admin_client_collected.get(f"/annotations?key={quote(key)}").text
