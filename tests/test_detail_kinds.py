import json

import pytest

from inventory.collector.netpolicy import derive_ingress, derive_netpol, derive_pdb, derive_service
from inventory.collector.storage import derive_node_provider, derive_pv, derive_pvc, derive_storageclass
from inventory.collector.transform import trim

pytestmark = pytest.mark.live

NEW_KINDS = (
    "csidrivers", "volumesnapshots", "ingressclasses", "priorityclasses", "poddisruptionbudgets",
    "horizontalpodautoscalers", "resourcequotas", "limitranges", "networkpolicies", "serviceaccounts", "roles",
)  # fmt: skip


def _data(conn, kind):
    return [(r[0], r[1], json.loads(r[2])) for r in conn.execute(
        "SELECT namespace, name, data FROM resources WHERE kind=? AND cluster_id='local'", [kind])]


def test_volumesnapshots_ok_or_not_installed(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    s = conn.execute("SELECT status FROM run_kinds WHERE kind='volumesnapshots' AND cluster_id='local'").fetchone()[0]
    assert s in ("ok", "not_found")


def test_every_new_kind_listed(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    for k in NEW_KINDS:
        row = conn.execute("SELECT status FROM run_kinds WHERE kind=? AND cluster_id='local'", [k]).fetchone()
        assert row is not None and row[0] == "ok", k


def test_cluster_uid_is_kube_system_uid(collected_db, dev_lists, open_db):
    conn = open_db(collected_db.db_path)
    [ks] = [n for n in dev_lists["namespaces"] if n["metadata"]["name"] == "kube-system"]
    uid = conn.execute("SELECT cluster_uid FROM cluster_profile WHERE cluster_id='local'").fetchone()[0]
    assert uid == ks["metadata"]["uid"]


def test_storageclass_no_values(collected_db, dev_lists, open_db):
    conn = open_db(collected_db.db_path)
    rows = _data(conn, "storageclasses")
    assert rows
    for _, _, d in rows:
        assert "parameters" not in d
    for sc in dev_lists["storageclasses"]:
        d = derive_storageclass(sc)
        assert d["parameter_keys"] == sorted(sc.get("parameters") or {})
        ann = (sc["metadata"].get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class")
        assert d["default"] == (ann == "true")


def test_ingress_snippet_flag(dev_lists):
    assert dev_lists["ingresses"]
    for ing in dev_lists["ingresses"]:
        d = derive_ingress(ing)
        assert isinstance(d["has_snippet"], bool)
        keys = (ing["metadata"].get("annotations") or {}).keys()
        assert d["nginx_annotations"] == sum(1 for k in keys if "nginx" in k)
        assert d["has_snippet"] == any("snippet" in k.lower() for k in keys if "nginx" in k)


def test_pv_source_and_pinned(dev_lists):
    assert dev_lists["persistentvolumes"]
    for pv in dev_lists["persistentvolumes"]:
        d = derive_pv(pv)
        spec = pv["spec"]
        if "csi" in spec:
            assert d["source"] == "csi:" + spec["csi"]["driver"]
        assert d["pinned"] == bool(spec.get("nodeAffinity"))
        assert d["access_modes"] == spec.get("accessModes", [])


def test_pvc_mounted_from_pod_claims(dev_lists):
    from inventory.collector.podspec import derive_pod

    used_by: dict = {}
    for pod in dev_lists["pods"]:
        dp = derive_pod(pod)
        for claim in dp["pvc_claims"]:
            used_by.setdefault((pod["metadata"]["namespace"], claim), set()).add(dp["owner_kind"] or "Pod")
    assert dev_lists["persistentvolumeclaims"]
    for pvc in dev_lists["persistentvolumeclaims"]:
        key = (pvc["metadata"]["namespace"], pvc["metadata"]["name"])
        d = derive_pvc(pvc, set(used_by), {k: sorted(v) for k, v in used_by.items()})
        assert d["mounted"] == (key in used_by)
        assert d["used_by"] == sorted(used_by.get(key, []))


def test_pvc_mounted_stored(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    rows = _data(conn, "persistentvolumeclaims")
    assert rows and any(d["mounted"] for _, _, d in rows)


def test_pdb_blocks_drain(dev_lists):
    for pdb in dev_lists["poddisruptionbudgets"]:
        st = pdb.get("status") or {}
        assert derive_pdb(pdb)["blocks_drain"] == (st.get("disruptionsAllowed") == 0 and st.get("expectedPods", 0) > 0)


def test_netpol_no_cidrs_stored(collected_db, dev_lists, open_db):
    for np in dev_lists["networkpolicies"]:
        d = derive_netpol(np)
        assert d["namespace_wide"] == (not (np["spec"].get("podSelector") or {}))
        assert set(d) >= {"policy_types", "namespace_wide", "ipblock_peers"}
    conn = open_db(collected_db.db_path)
    for _, _, d in _data(conn, "networkpolicies"):
        assert "cidr" not in json.dumps(d)


def test_service_fields(dev_lists):
    for svc in dev_lists["services"]:
        d = derive_service(svc)
        assert d["has_selector"] == bool(svc["spec"].get("selector"))
        assert d["headless"] == (svc["spec"].get("clusterIP") == "None")


def test_node_provider(dev_lists, kinds):
    assert derive_node_provider("rke2://node-1") == "rke2"
    assert derive_node_provider("") == "none"
    for node in dev_lists["nodes"]:
        d = trim(kinds["nodes"], node).data
        assert d["provider"] == derive_node_provider(node["spec"].get("providerID") or "")
        assert "providerID" not in json.dumps(d)
