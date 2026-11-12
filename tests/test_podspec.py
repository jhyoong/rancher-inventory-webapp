import json
import re

import pytest

from inventory.collector.podspec import derive_pod, derive_podspec, pod_template_spec, skip_object
from inventory.collector.transform import trim
from inventory.collector.units import parse_bytes, parse_cpu

pytestmark = pytest.mark.live

PODSPEC_KINDS = ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs", "replicationcontrollers")


def test_podspec_kinds_marked(kinds):
    for k in PODSPEC_KINDS:
        assert kinds[k].podspec, k
    assert not kinds["pods"].podspec


def test_new_workload_kinds_collected(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    for k in ("jobs", "cronjobs", "replicationcontrollers"):
        row = conn.execute("SELECT status FROM run_kinds WHERE kind=? AND cluster_id='local'", [k]).fetchone()
        assert row is not None and row[0] == "ok", k


def test_no_env_or_commands_stored(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    kinds = ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs", "replicationcontrollers", "pods")
    rows = conn.execute(f"SELECT data FROM resources WHERE kind IN {kinds}").fetchall()
    assert rows
    for (data,) in rows:
        assert not re.search(r'"(env|envFrom|command|args|path|server)"', data), data[:200]


def test_ingress_nginx_daemonset_host_ports(dev_lists):
    [ds] = [d for d in dev_lists["daemonsets"] if d["metadata"]["name"] == "rke2-ingress-nginx-controller"]
    assert derive_podspec(pod_template_spec("daemonsets", ds))["host_ports"] >= 2


def test_cronjob_jobs_skipped(collected_db, dev_lists, open_db):
    conn = open_db(collected_db.db_path)
    stored = {r[0] for r in conn.execute("SELECT name FROM resources WHERE kind='jobs' AND cluster_id='local'")}
    owned = {j["metadata"]["name"] for j in dev_lists["jobs"]
             if ((j["metadata"].get("ownerReferences") or [{}])[0]).get("kind") == "CronJob"}
    expected = {j["metadata"]["name"] for j in dev_lists["jobs"]} - owned
    assert stored == expected
    assert all(skip_object("jobs", j) for j in dev_lists["jobs"] if j["metadata"]["name"] in owned)
    for (data,) in conn.execute("SELECT data FROM resources WHERE kind='jobs'"):
        assert json.loads(data).get("owner_kind") != "CronJob"


def test_pod_usage_per_namespace(collected_db, open_db):
    conn = open_db(collected_db.db_path)
    row = conn.execute(
        "SELECT cpu_used_m, mem_used_b FROM namespace_profile WHERE cluster_id='local' AND namespace='kube-system'"
    ).fetchone()
    assert row["cpu_used_m"] > 0 and row["mem_used_b"] > 0
    for (data,) in conn.execute("SELECT data FROM resources WHERE kind='pods'"):
        assert "usage" not in json.loads(data)


def _sum(containers, section, key, parse):
    total = 0
    for c in containers:
        v = ((c.get("resources") or {}).get(section) or {}).get(key)
        if v:
            total += parse(v)
    return total


def test_resource_sums(dev_lists):
    checked = 0
    for kind in ("deployments", "daemonsets", "statefulsets"):
        for obj in dev_lists[kind]:
            spec = pod_template_spec(kind, obj)
            cs = spec.get("containers") or []
            d = derive_podspec(spec)
            assert d["cpu_req_m"] == _sum(cs, "requests", "cpu", parse_cpu)
            assert d["mem_req_b"] == _sum(cs, "requests", "memory", parse_bytes)
            assert d["cpu_lim_m"] == _sum(cs, "limits", "cpu", parse_cpu)
            assert d["mem_lim_b"] == _sum(cs, "limits", "memory", parse_bytes)
            assert d["containers"] == len(cs)
            assert d["init_containers"] == len(spec.get("initContainers") or [])
            assert d["no_requests"] == sum(1 for c in cs if not (c.get("resources") or {}).get("requests"))
            assert d["no_probes"] == sum(1 for c in cs if not (c.get("readinessProbe") or c.get("livenessProbe")))
            checked += 1
    assert checked > 10


def test_podspec_security_and_volumes(dev_lists):
    found = {"host_network": False, "hostpath": False, "privileged": False}
    for kind in ("deployments", "daemonsets", "statefulsets"):
        for obj in dev_lists[kind]:
            spec = pod_template_spec(kind, obj)
            d = derive_podspec(spec)
            vols = spec.get("volumes") or []
            assert d["host_network"] == bool(spec.get("hostNetwork"))
            assert d["hostpath"] == sum(1 for v in vols if "hostPath" in v)
            assert d["emptydir"] == sum(1 for v in vols if "emptyDir" in v)
            assert d["pvc_claims"] == sorted({v["persistentVolumeClaim"]["claimName"] for v in vols
                                              if "persistentVolumeClaim" in v})
            assert d["service_account"] in (None, spec.get("serviceAccountName"))
            assert d["node_selector_keys"] == sorted(spec.get("nodeSelector") or {})
            found["host_network"] |= d["host_network"]
            found["hostpath"] |= d["hostpath"] > 0
            found["privileged"] |= d["privileged"] > 0
    assert all(found.values()), found  # dev has each (rke2 system daemonsets)


def test_workload_owner_and_strategy(dev_lists, kinds):
    for obj in dev_lists["deployments"]:
        t = trim(kinds["deployments"], obj)
        assert t.data["update_strategy"] == (obj["spec"].get("strategy") or {}).get("type")
        ref = (obj["metadata"].get("ownerReferences") or [None])[0]
        assert t.data["owner_kind"] == (ref or {}).get("kind")
    for obj in dev_lists["cronjobs"]:
        t = trim(kinds["cronjobs"], obj)
        assert t.data["schedule"] == obj["spec"]["schedule"]
        assert t.data["suspended"] == bool(obj["spec"].get("suspend"))


def test_derive_pod(dev_lists):
    for pod in dev_lists["pods"]:
        d = derive_pod(pod)
        st = pod.get("status") or {}
        assert d["qos"] == st.get("qosClass")
        assert d["restarts"] == sum(c.get("restartCount", 0) for c in st.get("containerStatuses") or [])
        assert d["owner_kind"] == ((pod["metadata"].get("ownerReferences") or [{}])[0]).get("kind")
        assert d["cpu_req_m"] == _sum(pod["spec"]["containers"], "requests", "cpu", parse_cpu)
        assert d["mem_lim_b"] == _sum(pod["spec"]["containers"], "limits", "memory", parse_bytes)
