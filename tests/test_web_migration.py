import csv
import io
import json
import shutil

import pytest

from inventory.collector.rules import load_rules
from inventory.web.migration import floating_ips, watch_out

pytestmark = pytest.mark.live

LOCAL_LINK = 'href="/clusters/dev/local"'
CARD = 'class="card cluster"'


def _stored_flags(conn) -> list[str]:
    sql = "SELECT complexity_flags FROM cluster_profile WHERE site='dev' AND cluster_id='local'"
    return json.loads(conn.execute(sql).fetchone()[0])


def test_watch_out_dev(collected_db, open_db, settings):
    conn = open_db(collected_db.db_path, readonly=True)
    flags = watch_out(conn, load_rules(settings))[("dev", "local")]
    for f in ("node-pinned-pv", "database", "external-ip"):
        assert f in flags, f
    assert "not-collected" not in flags
    stored = _stored_flags(conn)
    assert flags[len(flags) - len(stored) :] == stored


def test_not_collected_flag(collected_db, tmp_path, open_db, settings):
    copy = tmp_path / "copy.db"
    shutil.copy(collected_db.db_path, copy)
    conn = open_db(copy)
    conn.execute(
        "UPDATE run_kinds SET status='failed' WHERE rowid = (SELECT rowid FROM run_kinds"
        " WHERE site='dev' AND cluster_id='local'"
        " AND run_id = (SELECT max(run_id) FROM run_kinds WHERE site='dev' AND cluster_id='local') LIMIT 1)"
    )
    assert "not-collected" in watch_out(conn, load_rules(settings))[("dev", "local")]


def test_external_ip_ignores_control_plane(collected_db, tmp_path, open_db, settings):
    copy = tmp_path / "copy.db"
    shutil.copy(collected_db.db_path, copy)
    conn = open_db(copy)
    conn.execute("UPDATE clusters SET api_endpoint = '192.168.50.200' WHERE site='dev' AND cluster_id='local'")
    conn.execute(
        "DELETE FROM addresses WHERE role IN ('lb', 'ingress-lb', 'external-ip', 'node-external')"
        " AND resource_id IN (SELECT id FROM resources WHERE site='dev' AND cluster_id='local')"
    )
    rules = load_rules(settings)
    assert floating_ips(conn, rules)[("dev", "local")]["total"] == 1
    assert "external-ip" not in watch_out(conn, rules)[("dev", "local")]


def test_clusters_page_table_and_cards(admin_client_collected, dev_lists):
    os_image = dev_lists["nodes"][0]["status"]["nodeInfo"]["osImage"]
    get = admin_client_collected.get
    table = get("/clusters").text
    cards = get("/clusters?view=cards").text
    for text in (table, cards):
        assert LOCAL_LINK in text and os_image in text and "node-pinned-pv" in text
    assert "<th>Watch out</th>" in table and CARD not in table
    assert CARD in cards and "<th>Watch out</th>" not in cards
    bogus = get("/clusters?view=bogus").text
    assert "<th>Watch out</th>" in bogus and CARD not in bogus


def test_flag_filter_old_and_new(admin_client_collected, collected_db, open_db, dev_lists):
    conn = open_db(collected_db.db_path, readonly=True)
    first = _stored_flags(conn)[0]
    get = admin_client_collected.get
    for flag in ("hostpath", "k8s<1.32", first, "database"):
        assert get("/clusters", params={"flag": flag}).status_code == 200, flag
    for flag in ("hostpath", first, "database"):
        assert LOCAL_LINK in get("/clusters", params={"flag": flag}).text, flag
    # dev has a NodePort service in the user namespace kagent (kagent-ui), so nodeport matches local
    nodeport = {s["metadata"]["namespace"] for s in dev_lists["services"] if s["spec"].get("type") == "NodePort"}
    assert ("kagent" in nodeport) == (LOCAL_LINK in get("/clusters?flag=nodeport").text)
    assert "No clusters match." in get("/clusters?flag=not-collected").text
    assert get("/clusters?flag=bogus").status_code == 400
    assert get("/query?flag=stateful").status_code == 200


def test_label_filter_node_label(admin_client_collected, dev_lists):
    node = dev_lists["nodes"][0]["metadata"]["name"]
    get = admin_client_collected.get
    text = get(f"/clusters?label=kubernetes.io/hostname%3D{node}").text
    assert LOCAL_LINK in text and f"node {node}" in text and "<th>Matched</th>" in text
    assert "No clusters match." in get("/clusters?label=nope%3D1").text


def test_inventory_redirect(admin_client_collected):
    r = admin_client_collected.get("/inventory?cls=x", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/clusters?cls=x"


def test_clusters_csv(admin_client_collected):
    get = admin_client_collected.get
    rows = list(csv.DictReader(io.StringIO(get("/clusters?format=csv").text)))
    local = next(r for r in rows if r["cluster_id"] == "local")
    assert "node-pinned-pv" in local["flags"].split("; ")
    assert int(local["floating_total"]) >= 1 and "floating_lb" in local
    # the old Inventory CSV link still returns the cluster table
    old = get("/inventory?format=csv&table=inventory")
    assert old.status_code == 200 and "node-pinned-pv" in old.text
