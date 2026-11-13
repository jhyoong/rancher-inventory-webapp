import csv
import io
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from inventory.collector.provisioning import parse_provisioning
from inventory.collector.rules import load_rules
from inventory.web.migration import assign_nodes, floating_ips, node_groups, site_overview, watch_out

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


def test_site_overview_dev(collected_db, open_db, settings, dev_lists):
    conn = open_db(collected_db.db_path, readonly=True)
    rules = load_rules(settings)
    o = site_overview(conn, rules)
    t, charts = o["totals"], o["charts"]
    assert t["nodes"] == len(dev_lists["nodes"])
    assert t["pv_count"] == len(dev_lists["persistentvolumes"])
    assert sum(x["nodes"] for x in charts["os_images"]) == t["nodes"]
    assert sum(x["pv_count"] for x in charts["pv_by_class"]) == t["pv_count"]
    assert t["floating_ips"] == floating_ips(conn, rules)[("dev", "local")]["total"]
    assert [c["cluster"] for c in charts["floating"]] == [r["name"] for r in o["clusters"]]
    assert site_overview(conn, rules, site="other")["totals"]["clusters"] == 0


def test_home_page(admin_client_collected):
    text = admin_client_collected.get("/").text
    for part in ('id="overview-data"', "/static/overview.js", "Floating IP", "Opt-in collectors:", "/clusters/dev/local"):
        assert part in text, part
    assert "Clusters per class" not in text
    assert admin_client_collected.get("/?site=dev").status_code == 200
    other = admin_client_collected.get("/?site=other")
    assert other.status_code == 200
    assert "No clusters match." in other.text
    assert CARD in admin_client_collected.get("/?view=cards").text


def test_node_groups_dev(collected_db, open_db, dev_lists):
    conn = open_db(collected_db.db_path, readonly=True)
    stored = conn.execute("SELECT pools_status FROM clusters WHERE site='dev' AND cluster_id='local'").fetchone()[0]
    result = node_groups(conn, "dev", "local")
    # not_collected while the provisioning GlobalRole is not applied on dev, none once it is
    assert result["pools_status"] == stored and stored in ("not_collected", "none")
    assert result["groups"] and all(g["kind"] == "role" for g in result["groups"])
    rows = [n for g in result["groups"] for n in g["nodes"]]
    nodes = {n["name"]: n for n in rows}
    assert len(rows) == len(nodes) and set(nodes) == {n["metadata"]["name"] for n in dev_lists["nodes"]}
    for live in dev_lists["nodes"]:
        row = nodes[live["metadata"]["name"]]
        assert row["os"] == live["status"]["nodeInfo"]["osImage"]
        assert "kubernetes.io/hostname" in row["labels_builtin"]
        assert "kubernetes.io/hostname" not in row["labels_other"]


@pytest.mark.real_fixtures
def test_assign_nodes_real(real_fixture):
    prov = real_fixture("provisioning-clusters.json")["items"]
    items = real_fixture("nodes.json")
    items = items.get("items", items) if isinstance(items, dict) else items
    name_file = Path(__file__).parent / "fixtures/real/nodes-cluster.txt"
    if not name_file.exists():
        pytest.skip("P2 fixtures missing: nodes-cluster.txt")
    cluster_name = name_file.read_text().strip()
    item = next(i for i in prov if (i.get("status") or {}).get("clusterName") == cluster_name)
    _, p = parse_provisioning(item)
    pools = [asdict(m) for m in p.pools]
    nodes = [{"name": n["metadata"]["name"], "roles": []} for n in items]
    groups = assign_nodes(nodes, pools, p.prov_name)
    assert all(g["kind"] == "pool" for g in groups if g["nodes"])
    for g in groups:
        assert len(g["nodes"]) == g["quantity"], g["name"]


def test_cluster_page_sections(admin_client_collected):
    get = admin_client_collected.get
    text = get("/clusters/dev/local").text
    headings = ("Nodes", "External networking", "Storage", "Databases", "Platform dependencies",
                "Namespaces", "Custom annotations", "Usage per day", "Cluster labels")  # fmt: skip
    at = [text.index(f"<h2>{h}") for h in headings]
    assert at == sorted(at)
    assert "kagent-postgresql" in text and "not visible to this app" in text
    nodes = get("/clusters/dev/local?format=csv&table=nodes")
    assert nodes.status_code == 200
    assert next(csv.reader(io.StringIO(nodes.text)))[0] == "name"
