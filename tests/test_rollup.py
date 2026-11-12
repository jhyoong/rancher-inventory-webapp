import csv
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import pytest

from inventory.collector.units import parse_bytes, parse_cpu

pytestmark = pytest.mark.live

SCRIPT_OUT = Path("/tmp/k8sd-local")
# our column -> script namespaces.csv column (filled from the script's header)
NAMESPACE_COLUMN_MAP = {
    "pods": "pods", "pods_running": "pods_running", "pods_standalone": "standalone_pods", "restarts": "restarts",
    "gpu": "gpu", "deployments": "deployments", "statefulsets": "statefulsets", "daemonsets": "daemonsets",
    "cronjobs": "cronjobs", "jobs": "jobs", "pvc_count": "pvcs", "pvc_rwx": "rwx_pvcs",
    "lb_services": "svc_loadbalancer", "nodeport_services": "svc_nodeport", "ingresses": "ingresses",
    "netpols": "networkpolicies", "has_quota": "quota", "has_limitrange": "limitrange",
}  # fmt: skip


def _owner(o):
    return ((o["metadata"].get("ownerReferences") or [{}])[0]).get("kind")


def _sum(containers, section, key, parse):
    return sum(parse(v) for c in containers if (v := ((c.get("resources") or {}).get(section) or {}).get(key)))


def _expected(dev_lists):
    """The script's namespace roll-up computed from the raw lists."""
    e = defaultdict(lambda: defaultdict(int))
    for p in dev_lists["pods"]:
        n, st = p["metadata"]["namespace"], p.get("status") or {}
        e[n]["pods"] += 1
        e[n]["pods_running"] += st.get("phase") == "Running"
        e[n]["pods_standalone"] += _owner(p) is None
        e[n]["restarts"] += sum(c.get("restartCount", 0) for c in st.get("containerStatuses") or [])
        if st.get("phase") in ("Running", "Pending"):
            cs = p["spec"]["containers"]
            e[n]["cpu_req_m"] += _sum(cs, "requests", "cpu", parse_cpu)
            e[n]["cpu_lim_m"] += _sum(cs, "limits", "cpu", parse_cpu)
            e[n]["mem_req_b"] += _sum(cs, "requests", "memory", parse_bytes)
            e[n]["mem_lim_b"] += _sum(cs, "limits", "memory", parse_bytes)
    for kind in ("deployments", "statefulsets", "daemonsets", "cronjobs", "jobs"):
        for o in dev_lists[kind]:
            if kind == "jobs" and _owner(o) == "CronJob":
                continue
            e[o["metadata"]["namespace"]][kind] += 1
    for o in dev_lists["persistentvolumeclaims"]:
        n = o["metadata"]["namespace"]
        e[n]["pvc_count"] += 1
        e[n]["pvc_rwx"] += "ReadWriteMany" in (o["spec"].get("accessModes") or [])
    for o in dev_lists["services"]:
        n, t = o["metadata"]["namespace"], o["spec"].get("type")
        e[n]["lb_services"] += t == "LoadBalancer"
        e[n]["nodeport_services"] += t == "NodePort"
    for kind, col in (("ingresses", "ingresses"), ("networkpolicies", "netpols")):
        for o in dev_lists[kind]:
            e[o["metadata"]["namespace"]][col] += 1
    for kind, col in (("resourcequotas", "has_quota"), ("limitranges", "has_limitrange")):
        for o in dev_lists[kind]:
            e[o["metadata"]["namespace"]][col] = 1
    return e


ROLLUP_COLS = (
    "pods", "pods_running", "pods_standalone", "restarts", "cpu_req_m", "cpu_lim_m", "mem_req_b", "mem_lim_b",
    "gpu", "deployments", "statefulsets", "daemonsets", "cronjobs", "jobs", "pvc_count", "pvc_rwx",
    "lb_services", "nodeport_services", "ingresses", "netpols", "has_quota", "has_limitrange",
)  # fmt: skip


def test_rollup_matches_raw_lists(collected_db, dev_lists, open_db):
    conn = open_db(collected_db.db_path)
    expected = _expected(dev_lists)
    rows = {r["namespace"]: r for r in conn.execute("SELECT * FROM namespace_profile WHERE cluster_id='local'")}
    assert set(rows) == {n["metadata"]["name"] for n in dev_lists["namespaces"]}
    for n, row in rows.items():
        for col in ROLLUP_COLS:
            exp = expected[n][col]
            got = row[col]
            if col in ("pods", "pods_running", "restarts") and got != exp:
                # pods can come and go between the session run and the dev_lists read
                assert abs(got - exp) <= 2, (n, col, got, exp)
            else:
                assert got == exp, (n, col, got, exp)


def test_namespace_totals_match_script(collected_db, open_db):
    if not (SCRIPT_OUT / "namespaces.csv").is_file():
        pytest.skip(f"no k8s-discovery.sh output in {SCRIPT_OUT}; run the script with --clear-names first")
    conn = open_db(collected_db.db_path)
    with open(SCRIPT_OUT / "namespaces.csv") as f:
        ref = list(csv.DictReader(f))
    for row in ref:
        ours = conn.execute("SELECT * FROM namespace_profile WHERE namespace=?", [row["namespace"]]).fetchone()
        for col, ref_col in NAMESPACE_COLUMN_MAP.items():
            assert ours[col] == int(row[ref_col]), (row["namespace"], col)
        for col, ref_col, unit in (("cpu_used_m", "cpu_used_cores", 1000), ("mem_used_b", "mem_used_gib", 2**30)):
            if row[ref_col]:
                ref_value = float(row[ref_col]) * unit
                assert abs((ours[col] or 0) - ref_value) <= 0.2 * max(ref_value, 1), (row["namespace"], col)


def _table_rows(html: str) -> int:
    return html.count('<tr class="row">')


@pytest.mark.parametrize("path,filters", [
    ("/workloads", ["privileged", "host_network", "hostpath", "no_requests", "no_limits", "no_probes",
                    "operator_owned", "gpu"]),
    ("/storage", ["rwx", "block", "unmounted"]),
])
def test_page_filters_are_subsets(admin_client_collected, path, filters):
    r = admin_client_collected.get(path)
    assert r.status_code == 200 and "local" in r.text
    total = _table_rows(r.text)
    assert total > 0
    for f in filters:
        r = admin_client_collected.get(f"{path}?{f}=1")
        assert r.status_code == 200, f
        assert _table_rows(r.text) <= total, f
    assert _table_rows(admin_client_collected.get(f"{path}?hostpath=1").text if path == "/workloads"
                       else admin_client_collected.get(f"{path}?unmounted=1").text) < total


def test_storage_value_filters(admin_client_collected, collected_db, open_db):
    conn = open_db(collected_db.db_path)
    sc = conn.execute("SELECT json_extract(data, '$.spec.storageClassName') FROM resources "
                      "WHERE kind='persistentvolumeclaims' LIMIT 1").fetchone()[0]
    src = conn.execute("SELECT json_extract(data, '$.source') FROM resources "
                       "WHERE kind='persistentvolumes' LIMIT 1").fetchone()[0]
    total = _table_rows(admin_client_collected.get("/storage").text)
    for q in ({"storage_class": sc}, {"source": src}, {"storage_class": "no-such-class"}):
        r = admin_client_collected.get("/storage?" + urlencode(q))
        assert r.status_code == 200 and _table_rows(r.text) <= total, q
    assert _table_rows(admin_client_collected.get("/storage?storage_class=no-such-class").text) == 0


def test_daemonset_ready_shown(admin_client_collected, dev_lists):
    [ds] = [d for d in dev_lists["daemonsets"] if d["metadata"]["name"] == "rke2-ingress-nginx-controller"]
    st = ds["status"]
    text = admin_client_collected.get("/workloads?name=rke2-ingress-nginx-controller").text
    assert f"{st['numberReady']} / {st['desiredNumberScheduled']}" in text


@pytest.mark.parametrize("sort", ["cpu_req_m", "cpu_used_m", "mem_req_b", "mem_used_b", "restarts"])
def test_namespaces_page_sorts(admin_client_collected, sort):
    r = admin_client_collected.get(f"/namespaces?sort={sort}")
    assert r.status_code == 200 and "kube-system" in r.text and "local" in r.text


def test_namespaces_page_rejects_unknown_sort(admin_client_collected):
    assert admin_client_collected.get("/namespaces?sort=name;drop").status_code == 400
