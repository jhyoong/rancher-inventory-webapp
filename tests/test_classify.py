import json

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
    ("kagent.dev", False), ("crd.projectcalico.org", True), ("metallb.io", True),
    ("addons.metallb.io", True), ("notmetallb.io", False),
])
def test_system_crd_group(group, system, rules):
    assert is_system_crd_group(group, rules) is system


def _bytes(q):
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
    for u, m in units.items():
        if q.endswith(u):
            return int(q[: -len(u)]) * m
    return int(q)


def _expected(dev_lists, rules):
    """Flags and per-namespace counts from raw Kubernetes items, in plain Python."""
    from fnmatch import fnmatchcase

    def user(ns):
        return not any(fnmatchcase(ns, p) for p in rules.system_namespaces)

    def meta(o):
        return o["metadata"]["namespace"]

    ns = {}

    def row(n):
        return ns.setdefault(n, dict(workloads=0, statefulsets=0, pvc_bound=0, pvc_bytes=0,
                                     hostpath_workloads=0, helm_releases=0, lb_services=0, ingresses=0))

    helm_labeled_stateful = set()
    for kind in ("deployments", "statefulsets", "daemonsets"):
        for o in dev_lists[kind]:
            r = row(meta(o))
            r["workloads"] += 1
            if kind == "statefulsets":
                r["statefulsets"] += 1
            vols = o["spec"]["template"]["spec"].get("volumes") or []
            if any("hostPath" in v for v in vols):
                r["hostpath_workloads"] += 1
            has_pvc = any("persistentVolumeClaim" in v for v in vols)
            labels = o["metadata"].get("labels") or {}
            if (kind == "statefulsets" or has_pvc) and labels.get("app.kubernetes.io/managed-by") == "Helm":
                helm_labeled_stateful.add(meta(o))
    for o in dev_lists["persistentvolumeclaims"]:
        if o["status"].get("phase") == "Bound":
            r = row(meta(o))
            r["pvc_bound"] += 1
            r["pvc_bytes"] += _bytes(
                o["status"].get("capacity", {}).get("storage") or o["spec"]["resources"]["requests"]["storage"]
            )
    for o in dev_lists["helmreleases"]:
        if o["spec"]["info"]["status"] == "deployed":
            row(meta(o))["helm_releases"] += 1
    for o in dev_lists["services"]:
        if o["spec"].get("type") == "LoadBalancer":
            row(meta(o))["lb_services"] += 1
    for o in dev_lists["ingresses"]:
        row(meta(o))["ingresses"] += 1

    users = {n: r for n, r in ns.items() if user(n)}
    stateful_ns = {n for n, r in users.items() if r["statefulsets"] or r["pvc_bound"]}
    def system_group(g):
        return any(g == p or g.endswith("." + p) or fnmatchcase(g, p) for p in rules.system_crd_groups)

    groups = sorted({c["spec"]["group"] for c in dev_lists["customresourcedefinitions"]
                     if not system_group(c["spec"]["group"])})
    flags = Flags(
        has_workloads=any(r["workloads"] for r in users.values()),
        stateful=bool(stateful_ns),
        helm=any(users[n]["helm_releases"] for n in stateful_ns) or bool(helm_labeled_stateful & stateful_ns),
        custom_crds=bool(groups),
        hostpath=any(r["hostpath_workloads"] for r in users.values())
        or any(pv["spec"].get("local") or pv["spec"].get("hostPath") for pv in dev_lists["persistentvolumes"]),
    )
    return flags, users, groups, ns


@pytest.mark.live
def test_local_profile(collected_db, rules, dev_lists, open_db):
    flags, users, groups, all_ns = _expected(dev_lists, rules)
    conn = open_db(collected_db.db_path)
    try:
        def snapshot():
            cp = dict(conn.execute("SELECT * FROM cluster_profile WHERE cluster_id='local'").fetchone())
            nps = [dict(r) for r in conn.execute(
                "SELECT * FROM namespace_profile WHERE cluster_id='local' ORDER BY namespace")]
            return cp, nps

        got = build_profiles(conn, "dev", "local", 1, rules)
        assert got == flags
        first = snapshot()
        cp, nps = first
        assert cp["class"] == class_label(flags)
        assert (cp["stateful"], cp["helm"], cp["custom_crds"], cp["hostpath"]) == (
            int(flags.stateful), int(flags.helm), int(flags.custom_crds), int(flags.hostpath))
        for key in ("statefulsets", "pvc_bound", "pvc_bytes", "helm_releases"):
            assert cp[key] == sum(r[key] for r in users.values()), key
        assert cp["lb_services"] == sum(r["lb_services"] for r in all_ns.values())
        assert cp["ingresses"] == sum(r["ingresses"] for r in all_ns.values())
        assert json.loads(cp["crd_groups"]) == groups
        assert cp["network"] is None and cp["updated_run"] == 1

        by_ns = {r["namespace"]: r for r in nps}
        names = [r["namespace"] for r in nps]
        assert sorted(names) == sorted(m["metadata"]["name"] for m in dev_lists["namespaces"])
        for n, exp in users.items():
            assert by_ns[n]["is_system"] == 0
            for key, value in exp.items():
                assert by_ns[n][key] == value, (n, key)
        assert all(r["is_system"] == 0 for r in nps if r["namespace"] in users)
        assert all(r["is_system"] == 1 for r in nps if r["namespace"] not in users and is_system_namespace(r["namespace"], rules))

        # a second build replaces rows instead of duplicating them
        build_profiles(conn, "dev", "local", 1, rules)
        assert snapshot() == first
        assert conn.execute("SELECT count(*) FROM cluster_profile").fetchone()[0] == 1
    finally:
        conn.close()
