import pytest

from inventory.collector.classify import (
    Flags,
    build_profiles,
    class_label,
    is_system_crd_group,
    is_system_namespace,
)
from inventory.collector.rules import load_rules
from inventory.db.connection import connect


@pytest.fixture
def rules(settings):
    return load_rules(settings)


@pytest.mark.parametrize("flags,label", [
    (Flags(False, False, False, False, False), "empty"),
    (Flags(True, False, False, False, False), "stateless"),
    (Flags(True, True, False, False, False), "stateful"),
    (Flags(True, True, True, False, False), "stateful-helm"),
    (Flags(True, True, False, True, False), "stateful-crd"),
    (Flags(True, True, True, True, False), "stateful-helm-crd"),
])
def test_class_label(flags, label):
    assert class_label(flags) == label


@pytest.mark.parametrize("ns,system", [
    ("kube-system", True), ("cattle-fleet-system", True), ("local", True),
    ("cert-manager", True), ("kagent", False), ("default", False),
])
def test_system_namespace(ns, system, rules):
    assert is_system_namespace(ns, rules) is system


@pytest.mark.parametrize("group,system", [
    ("fleet.cattle.io", True), ("networking.k8s.io", True), ("cilium.io", True),
    ("kagent.dev", False),
])
def test_system_crd_group(group, system, rules):
    assert is_system_crd_group(group, rules) is system


@pytest.mark.live
def test_local_profile(collected_db, rules, dev_lists):
    conn = connect(collected_db.db_path)
    try:
        f = build_profiles(conn, "dev", "local", 1, rules)
        user_ns_workloads = [
            d for d in dev_lists["deployments"] + dev_lists["statefulsets"] + dev_lists["daemonsets"]
            if not is_system_namespace(d["metadata"]["namespace"], rules)
        ]
        assert f.has_workloads == bool(user_ns_workloads)
        row = conn.execute("SELECT * FROM cluster_profile WHERE cluster_id='local'").fetchone()
        assert row["class"] == class_label(f)
        assert row["network"] is None
        assert row["helm_releases"] == sum(
            1 for h in dev_lists["helmreleases"] if h["spec"]["info"]["status"] == "deployed"
        )
        custom = {
            c["spec"]["group"] for c in dev_lists["customresourcedefinitions"]
            if not is_system_crd_group(c["spec"]["group"], rules)
        }
        assert f.custom_crds == bool(custom)
        assert row["custom_crds"] == int(bool(custom))
        # idempotent: a second build leaves one row per namespace
        build_profiles(conn, "dev", "local", 1, rules)
        n = conn.execute("SELECT count(*) FROM namespace_profile WHERE cluster_id='local'").fetchone()[0]
        names = {x["metadata"]["name"] for x in dev_lists["namespaces"]}
        assert n >= len(names)
        sys_rows = conn.execute(
            "SELECT namespace FROM namespace_profile WHERE cluster_id='local' AND is_system=0"
        ).fetchall()
        assert all(not is_system_namespace(r["namespace"], rules) for r in sys_rows)
    finally:
        conn.close()
