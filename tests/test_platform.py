import asyncio
import json

import pytest

from inventory.collector.platform import (
    collect_api_versions,
    derive_apiservice,
    derive_binding,
    derive_clusterrole,
    derive_webhook,
    in_use_api_version,
    is_platform_role,
    is_rancher_generated,
)
from inventory.collector.rules import load_rules

pytestmark = pytest.mark.live

REVIEWED_CUSTOM = {"local-path-provisioner-role"}  # kubectl-applied on dev; reviewed with the user 2026-11-12


@pytest.fixture
def rules(settings):
    return load_rules(settings)


def test_platform_kinds_listed(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    for k in ("mutatingwebhookconfigurations", "validatingwebhookconfigurations", "apiservices",
              "clusterroles", "clusterrolebindings", "rolebindings"):
        row = conn.execute("SELECT status FROM run_kinds WHERE kind=? AND cluster_id='local'", [k]).fetchone()
        assert row is not None and row[0] == "ok", k


def test_no_rancher_role_counted_as_custom(dev_lists, rules):
    custom = [r["metadata"]["name"] for r in dev_lists["clusterroles"]
              if not is_rancher_generated(r, rules) and not is_platform_role(r, rules)]
    assert custom and all(n.startswith("kagent") or n in REVIEWED_CUSTOM for n in custom), custom


def test_rancher_markers(dev_lists, rules):
    by_name = {r["metadata"]["name"]: r for r in dev_lists["clusterroles"]}
    for name in ("cattle-globalrole-admin", "inventory-reader", "cluster-owner", "local-clusterowner",
                 "p-kbfdk-namespaces-edit", "u-b4qkhsnliz-view", "fleetworkspace-admin"):
        if name in by_name:
            assert is_rancher_generated(by_name[name], rules), name
    for name in ("kagent-writer-role", "cert-manager-view", "system:node"):
        assert not is_rancher_generated(by_name[name], rules), name


def test_rancher_webhook_found(dev_lists):
    [wh] = [w for w in dev_lists["validatingwebhookconfigurations"] if w["metadata"]["name"] == "rancher.cattle.io"]
    d = derive_webhook(wh)
    assert d["webhooks"] == len(wh["webhooks"])
    assert d["service_namespaces"] == ["cattle-system"]
    assert set(d["failure_policies"]) <= {"Fail", "Ignore"}
    assert isinstance(d["intercepts_all"], bool) and d["url_based"] is False
    for w in dev_lists["mutatingwebhookconfigurations"] + dev_lists["validatingwebhookconfigurations"]:
        derive_webhook(w)


def test_apiservices_aggregated_only(collected_db, dev_lists, open_db):
    aggregated = [a for a in dev_lists["apiservices"] if (a.get("spec") or {}).get("service")]
    assert aggregated and len(aggregated) < len(dev_lists["apiservices"])
    for a in dev_lists["apiservices"]:
        d = derive_apiservice(a)
        assert (d is None) == (not (a.get("spec") or {}).get("service"))
    conn = open_db(collected_db.db_path)
    stored = {r[0] for r in conn.execute("SELECT name FROM resources WHERE kind='apiservices' AND cluster_id='local'")}
    assert stored == {a["metadata"]["name"] for a in aggregated}
    [m] = [a for a in aggregated if a["metadata"]["name"] == "v1beta1.metrics.k8s.io"]
    assert derive_apiservice(m) == {"service_namespace": "kube-system", "available": True}


def test_clusterrole_and_binding(dev_lists):
    for r in dev_lists["clusterroles"]:
        d = derive_clusterrole(r)
        assert d["rule_count"] == len(r.get("rules") or [])
        assert d["aggregated"] == bool(r.get("aggregationRule"))
    for b in dev_lists["clusterrolebindings"] + dev_lists["rolebindings"]:
        d = derive_binding(b)
        assert d["role"] == [b["roleRef"]["kind"], b["roleRef"]["name"]]
        assert d["subjects"] == [[s["kind"], s["name"]] for s in b.get("subjects") or []]


def test_rbac_flags_stored(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    rows = {r[0]: json.loads(r[1]) for r in conn.execute(
        "SELECT name, data FROM resources WHERE kind='clusterroles' AND cluster_id='local'")}
    assert rows["cattle-globalrole-admin"]["rancher_generated"] is True
    assert rows["kagent-writer-role"]["rancher_generated"] is False
    assert rows["kagent-writer-role"]["platform"] is False
    assert rows["cert-manager-view"]["platform"] is True


def test_last_applied_dropped(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    n = conn.execute("SELECT count(*) FROM resource_annotations WHERE key='kubectl.kubernetes.io/last-applied-configuration'").fetchone()[0]
    assert n == 0
    def has_value(node) -> bool:  # a mapping holding the annotation; key names in annotation_keys lists are fine
        if isinstance(node, dict):
            return "kubectl.kubernetes.io/last-applied-configuration" in node or any(map(has_value, node.values()))
        return isinstance(node, list) and any(map(has_value, node))

    assert not any(has_value(json.loads(r[0])) for r in conn.execute("SELECT data FROM resources"))


def test_in_use_from_last_applied(collected_db, dev_lists, open_db):
    expected = set()
    for items in dev_lists.values():
        for o in items:
            gv = in_use_api_version(o)
            if gv:
                expected.add(gv)
    assert expected  # local-path-provisioner was kubectl-applied
    conn = open_db(collected_db.db_path)
    stored = {(r[0], r[1]) for r in conn.execute(
        "SELECT group_version, kind FROM api_versions WHERE cluster_id='local' AND source='in_use'")}
    assert stored == expected


def test_api_versions_served(collected_db, live_loop, live_client, open_db):
    conn = open_db(collected_db.db_path)
    served = {r[0] for r in conn.execute(
        "SELECT group_version FROM api_versions WHERE cluster_id='local' AND source='served'")}
    assert {"v1", "apps/v1", "metrics.k8s.io/v1beta1"} <= served
    assert served == set(live_loop.run_until_complete(collect_api_versions(live_client, "local")))


def test_removed_cluster_drops_api_versions(collected_db, tmp_path, open_db):
    """Covered by the runner removal path: api_versions rows of a gone cluster are deleted."""
    import shutil

    from inventory.collector.runner import _remove_unlisted

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    conn = open_db(tmp_path / "inventory.db")
    assert conn.execute("SELECT count(*) FROM api_versions WHERE cluster_id='local'").fetchone()[0] > 0
    _remove_unlisted(conn, "dev", set())
    assert conn.execute("SELECT count(*) FROM api_versions WHERE cluster_id='local'").fetchone()[0] == 0
