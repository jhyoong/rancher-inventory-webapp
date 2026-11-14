import csv
import io
import json
import re
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest

from inventory.collector.provisioning import parse_provisioning
from inventory.collector.rules import load_rules
from inventory.web import queries
from inventory.web.migration import FLAG_ANCHORS, assign_nodes, floating_ips, node_groups, site_overview, watch_out

pytestmark = pytest.mark.live

LOCAL_LINK = 'href="/clusters/dev/local"'
CARD = 'class="card cluster"'


@contextmanager
def _client_on_copy(collected_db, tmp_path, open_db, change):
    """Admin client on a copy of collected_db after change(conn) ran on the copy."""
    from fastapi.testclient import TestClient

    from inventory.web.app import create_app
    from tests.conftest import TEST_PASSWORD, _login, _web_settings

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = _web_settings(tmp_path)
    conn = open_db(s.db_path)
    change(conn)
    conn.commit()
    with TestClient(create_app(s, None)) as c:
        yield _login(c, s, "admin", TEST_PASSWORD)


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
    # the old Inventory CSV link still returns the old inventory table (see test_old_inventory_csv_columns)
    old = get("/inventory?format=csv&table=inventory")
    assert old.status_code == 200 and "complexity" in old.text.splitlines()[0].split(",")


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
    cap = charts["capacity"]
    assert cap["clusters"] == len(o["clusters"])
    assert cap["cpu"]["alloc"] == t["cpu_alloc_m"]
    assert cap["cpu"]["alloc_n"] == sum(1 for r in o["clusters"] if r["cpu_alloc_m"] is not None)
    assert sum(c["count"] for c in charts["by_class"]) == len(o["clusters"])
    assert all(c["count"] > 0 for c in charts["by_class"] + charts["by_flag"])
    counts = [c["count"] for c in charts["by_flag"]]
    assert counts == sorted(counts, reverse=True)
    assert all(f["href"].startswith("/clusters?flag=") for f in charts["by_flag"])
    assert len(charts["top_cpu"]) <= 10
    biggest = min((r for r in o["clusters"] if r["cpu_req_m"] is not None), key=lambda r: (-r["cpu_req_m"], r["name"]))
    assert charts["top_cpu"][0]["href"] == f"/clusters/{biggest['site']}/{biggest['cluster_id']}"
    assert sum(charts["floating"].values()) == t["floating_ips"]
    assert [s["site"] for s in charts["site_class"]] == ["dev"]
    dev_hrefs = [c["href"] for c in site_overview(conn, rules, site="dev")["charts"]["by_class"] if c["href"]]
    assert dev_hrefs and all(h.startswith("/clusters?cls=") and h.endswith("&site=dev") for h in dev_hrefs)
    assert site_overview(conn, rules, site="other")["totals"]["clusters"] == 0


def _copy_local_as_gone(conn, table):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    select = ", ".join("'c-gone'" if c == "cluster_id" else c for c in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {select} FROM {table} WHERE cluster_id='local'")


def test_site_overview_two_clusters(collected_db, tmp_path, open_db, settings):
    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    conn = open_db(tmp_path / "inventory.db")
    before = site_overview(conn, load_rules(settings))["clusters"]
    for table in ("clusters", "cluster_profile", "cluster_metrics_latest"):
        _copy_local_as_gone(conn, table)
    conn.execute("UPDATE cluster_metrics_latest SET mem_req_b = NULL WHERE cluster_id='c-gone'")
    conn.commit()
    o = site_overview(conn, load_rules(settings))
    ch = o["charts"]
    assert ch["capacity"]["clusters"] == len(before) + 1
    assert ch["capacity"]["mem"]["req_n"] == sum(1 for r in before if r["mem_req_b"] is not None)
    assert all(t["href"] != "/clusters/dev/c-gone" for t in ch["top_mem"])
    assert ch["top_cpu_share"] == 100.0  # at most 10 clusters, so the top 10 hold everything
    assert sum(c["count"] for c in ch["by_class"]) == len(before) + 1


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
    at = [re.search(rf"<h2[^>]*>{h}", text).start() for h in headings]
    assert at == sorted(at)
    assert "kagent-postgresql" in text and "not visible to this app" in text
    nodes = get("/clusters/dev/local?format=csv&table=nodes")
    assert nodes.status_code == 200
    assert next(csv.reader(io.StringIO(nodes.text)))[0] == "name"


def test_cluster_csv_tables(admin_client_collected, dev_lists):
    get = admin_client_collected.get

    def table(query: str) -> list[dict]:
        return list(csv.DictReader(io.StringIO(get(f"/clusters/dev/local?format=csv{query}").text)))

    # services keeps its old meaning: every service of the cluster
    assert {r["name"] for r in table("&table=services")} == {s["metadata"]["name"] for s in dev_lists["services"]}
    assert "kagent-ui" in {r["name"] for r in table("&table=nodeport_services")}
    # a bare CSV link returns namespaces, as before the migration sheet
    bare = get("/clusters/dev/local?format=csv").text.splitlines()[0]
    assert bare == get("/clusters/dev/local?format=csv&table=namespaces").text.splitlines()[0]
    assert "namespace" in bare.split(",")


@pytest.mark.parametrize(
    "old, new",
    [
        ("/helm?chart=cert-manager", "/platform?chart=cert-manager"),
        ("/crds?custom_only=1", "/platform?custom_only=1"),
        ("/annotations?key=k", "/platform?key=k"),
        ("/namespaces?sort=cpu_req_m", "/workloads?sort=cpu_req_m"),
        ("/helm?format=csv", "/platform?format=csv&table=charts"),
    ],
)
def test_legacy_redirects(admin_client_collected, old, new):
    r = admin_client_collected.get(old, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == new
    assert admin_client_collected.get(old).status_code == 200


def test_legacy_helm_csv_is_charts(admin_client_collected):
    rows = list(csv.reader(io.StringIO(admin_client_collected.get("/helm?format=csv").text)))
    assert rows[0] == ["chart", "releases", "clusters", "versions"]


def test_nav_links(admin_client_collected):
    text = admin_client_collected.get("/").text
    nav = text[text.index("<nav>") : text.index("</nav>")]
    for href in ("/", "/clusters", "/network", "/storage", "/workloads", "/platform",
                 "/query", "/presets", "/check", "/runs", "/export/discovery", "/admin/users"):  # fmt: skip
        assert f'href="{href}"' in nav, href
    assert "Tools" in nav
    for href in ("/helm", "/crds", "/annotations", "/namespaces", "/inventory"):
        assert f'href="{href}"' not in text, href


def test_tables_js_served(admin_client_collected):
    assert admin_client_collected.get("/static/tables.js").status_code == 200
    assert '<script src="/static/tables.js"' in admin_client_collected.get("/").text


def test_watch_out_flags_link_to_rows(admin_client_collected):
    get = admin_client_collected.get
    anchor = FLAG_ANCHORS["node-pinned-pv"]
    link = f'href="/clusters/dev/local#{anchor}">node-pinned-pv</a>'
    for page in ("/clusters", "/clusters?view=cards", "/clusters/dev/local"):
        assert link in get(page).text, page
    page = get("/clusters/dev/local").text
    assert f'id="{anchor}"' in page
    assert 'class="badge class"' not in page


def test_cluster_page_has_every_flag_anchor(admin_client_collected):
    page = admin_client_collected.get("/clusters/dev/local").text
    for flag, anchor in FLAG_ANCHORS.items():
        assert f'id="{anchor}"' in page, flag


def test_never_collected_shows_not_collected(collected_db, tmp_path, open_db):
    def drop_profile(conn):
        conn.execute("DELETE FROM cluster_profile WHERE site='dev' AND cluster_id='local'")

    with _client_on_copy(collected_db, tmp_path, open_db, drop_profile) as c:
        table = c.get("/clusters").text
        row = table[table.index(LOCAL_LINK) : table.index("</tr>", table.index(LOCAL_LINK))]
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        # cells after the cluster cell: Kubernetes, Nodes, Pools, OS, vCPU, Memory, PVs, Floating IPs, LB, Watch out
        assert cells[-4:] == ["not collected"] * 4, cells
        cards = c.get("/clusters?view=cards").text
        for label in ("PVs", "Floating IPs", "LB", "Watch out"):
            assert f"<dt>{label}</dt><dd>not collected</dd>" in cards, label
        rows = list(csv.DictReader(io.StringIO(c.get("/clusters?format=csv").text)))
        local = next(r for r in rows if r["cluster_id"] == "local")
        for k in ("pv_count", "pv_bytes", "floating_total", "floating_lb", "lb_implementations", "flags"):
            assert local[k] == "not collected", k


def test_unparsable_pv_size(collected_db, tmp_path, open_db, settings):
    def bad_size(conn):
        conn.execute(
            "UPDATE resources SET data = json_set(data, '$.spec.capacity.storage', 'not-a-size')"
            " WHERE id = (SELECT min(id) FROM resources WHERE kind='persistentvolumes' AND cluster_id='local')"
        )

    with _client_on_copy(collected_db, tmp_path, open_db, bad_size) as c:
        for page in ("/", "/clusters", "/clusters?flag=database", "/clusters?format=csv"):
            assert c.get(page).status_code == 200, page
    conn = open_db(tmp_path / "inventory.db", readonly=True)
    before = site_overview(open_db(collected_db.db_path, readonly=True), load_rules(settings))["totals"]
    after = site_overview(conn, load_rules(settings))["totals"]
    assert after["pv_count"] == before["pv_count"] and after["pv_bytes"] < before["pv_bytes"]


def test_node_external_only_is_not_external_ip(collected_db, tmp_path, open_db, settings):
    copy = tmp_path / "copy.db"
    shutil.copy(collected_db.db_path, copy)
    conn = open_db(copy)
    local = "resource_id IN (SELECT id FROM resources WHERE site='dev' AND cluster_id='local')"
    conn.execute(f"DELETE FROM addresses WHERE role IN ('lb', 'ingress-lb', 'external-ip') AND {local}")
    # keep one node address as node-external; dev nodes may report no ExternalIP, then a node InternalIP is used
    keep = conn.execute(
        f"SELECT resource_id, role, address FROM addresses WHERE role IN ('node-external', 'node-internal') AND {local}"
        " ORDER BY role = 'node-internal', resource_id, address LIMIT 1"
    ).fetchone()
    conn.execute(f"DELETE FROM addresses WHERE role = 'node-external' AND {local}")
    conn.execute(
        "INSERT OR REPLACE INTO addresses (resource_id, role, address) VALUES (?, 'node-external', ?)",
        (keep["resource_id"], keep["address"]),
    )
    # pinned loadBalancerIP values also feed the lb source; dev has no LoadBalancer services
    assert conn.execute("SELECT count(*) FROM resources WHERE kind='services' AND cluster_id='local'"
                        " AND json_extract(data, '$.spec.type') = 'LoadBalancer'").fetchone()[0] == 0  # fmt: skip
    conn.commit()
    rules = load_rules(settings)
    f = floating_ips(conn, rules)[("dev", "local")]
    assert f["node_external"] == 1 and f["total"] == 1 + (1 if f["control_plane"] else 0)
    assert "external-ip" not in watch_out(conn, rules)[("dev", "local")]


def test_floating_sources_add_up(collected_db, open_db, settings):
    conn = open_db(collected_db.db_path, readonly=True)
    for f in floating_ips(conn, load_rules(settings)).values():
        sources = f["lb"] + f["external_ip"] + f["ingress_lb"] + f["node_external"]
        assert sources == len(f["addresses"]) == f["total"] - (1 if f["control_plane"] else 0)


def test_watch_out_one_cluster(collected_db, open_db, settings):
    conn = open_db(collected_db.db_path, readonly=True)
    rules = load_rules(settings)
    one = watch_out(conn, rules, "dev", "local")
    assert list(one) == [("dev", "local")]
    assert one[("dev", "local")] == watch_out(conn, rules)[("dev", "local")]


def test_old_inventory_csv_columns(admin_client_collected, collected_db, open_db):
    conn = open_db(collected_db.db_path, readonly=True)
    expected = list(queries.inventory_rows(conn)[0])
    text = admin_client_collected.get("/clusters?format=csv&table=inventory").text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == expected
    local = dict(zip(rows[0], next(r for r in rows[1:] if r[rows[0].index("cluster_id")] == "local")))
    assert local["complexity"] == json.dumps(_stored_flags(conn))
    assert local["crd_group_list"].startswith("[")
    old = admin_client_collected.get("/inventory?format=csv")
    assert old.status_code == 200 and old.text.splitlines()[0] == text.splitlines()[0]
