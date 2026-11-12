import json

import pytest

from inventory.collector.rollup import COMPLEXITY_FLAGS, complexity_flags, k8s_minor
from inventory.collector.rules import load_rules
from inventory.web import queries


@pytest.fixture
def rules(settings):
    return load_rules(settings)


def test_version_flag_from_k8s_version():
    assert k8s_minor("v1.34.3+rke2r1") == 34
    assert k8s_minor("v1.31.0") == 31
    assert k8s_minor("") is None and k8s_minor(None) is None and k8s_minor("garbage") is None


def test_rules_complexity_settings(rules):
    assert rules.complexity == {"min_k8s_minor": 32, "many_crds": 30}


@pytest.mark.live
def test_local_flags(collected_db, rules, dev_lists, open_db):
    conn = open_db(collected_db.db_path)
    flags = complexity_flags(conn, "dev", "local", rules)
    stored = json.loads(conn.execute("SELECT complexity_flags FROM cluster_profile WHERE cluster_id='local'").fetchone()[0])
    assert stored == flags
    assert set(flags) <= set(COMPLEXITY_FLAGS)
    assert "k8s<1.32" not in flags  # dev local runs 1.34
    assert "many-crds" not in flags and "deprecated-apis" not in flags  # opt-in collectors are off
    # facts checked against the raw lists
    rwx = any("ReadWriteMany" in (p["spec"].get("accessModes") or []) for p in dev_lists["persistentvolumeclaims"])
    assert ("rwx" in flags) == rwx
    gpu = any(any(k in (n["status"].get("capacity") or {}) for k in ("nvidia.com/gpu", "amd.com/gpu"))
              for n in dev_lists["nodes"])
    assert ("gpu" in flags) == gpu
    assert "stateful" in flags  # dev local is classed stateful


@pytest.mark.live
def test_platform_page(admin_client_collected):
    r = admin_client_collected.get("/platform")
    assert r.status_code == 200
    for text in ("rancher.cattle.io", "v1beta1.metrics.k8s.io", "kagent-writer-role", "apps/v1"):
        assert text in r.text, text
    assert "cattle-globalrole-admin" not in r.text  # Rancher-generated roles are not listed as custom


@pytest.mark.live
def test_platform_cluster_admin_bindings(collected_db, dev_lists, open_db):
    conn = open_db(collected_db.db_path)
    names = {r["name"] for r in queries.cluster_admin_bindings(conn)}
    for b in dev_lists["clusterrolebindings"]:
        n = b["metadata"]["name"]
        if b["roleRef"]["name"] != "cluster-admin" or n.startswith("system:"):
            assert n not in names
    assert all(not n.startswith(("grb-", "crb-", "globaladmin-", "cattle-")) for n in names)


@pytest.mark.live
def test_inventory_flag_badges_and_filter(admin_client_collected, collected_db, open_db):
    conn = open_db(collected_db.db_path)
    flags = json.loads(conn.execute("SELECT complexity_flags FROM cluster_profile WHERE cluster_id='local'").fetchone()[0])
    text = admin_client_collected.get("/inventory").text
    for f in flags:
        assert f'<span class="badge complexity">{f}</span>' in text.replace("&lt;", "<"), f
    assert "local" in admin_client_collected.get("/inventory?flag=" + flags[0]).text
    absent = next(f for f in COMPLEXITY_FLAGS if f not in flags)
    assert "No clusters match." in admin_client_collected.get("/inventory", params={"flag": absent}).text
