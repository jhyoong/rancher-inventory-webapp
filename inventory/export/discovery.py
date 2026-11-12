"""Anonymised discovery export (spec 7): the per-cluster CSV files of k8s-discovery.sh 1.0.0.

Tables are built from the database with clear names (like the script's "pre" step),
the summary is computed from them, then every table is anonymised with the script's
column rules and written as CSV. No Rancher calls are made.
"""

import csv
import io
import json
import re
import shutil
import sqlite3
import tarfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from inventory import __version__
from inventory.collector.classify import is_system_namespace
from inventory.collector.rules import Rules, load_rules
from inventory.collector.runner import Scope
from inventory.collector.units import parse_bytes, parse_cpu
from inventory.db.repo import transaction
from inventory.export.hashing import hash_name, salt_id
from inventory.settings import ConfigError, Settings

# ---- the script's anonymisation rules (anon_spec, PUBPFX, PUBREG) ----

ANON_SPEC: dict[str, dict[str, str]] = {
    "cluster": {"cluster_id": "h:cluster", "context": "h:ctx"},
    "summary": {"cluster_id": "h:cluster", "context": "h:ctx"},
    "api_versions_served": {"groupversion": "s:gv"},
    "nodes": {"node": "h:node", "taints": "keys", "label_keys": "keys"},
    "namespaces": {"namespace": "ns", "label_keys": "keys"},
    "workloads": {
        "namespace": "ns", "name": "h:wl", "owner_kind": "s:own", "helm_chart": "s:chart", "managed_by": "s:mb",
        "node_selector_keys": "keys", "toleration_keys": "keys", "priority_class": "s:pc", "pvc_templates": "s:sc",
    },
    "images": {"registry": "reg", "project": "h:proj", "repository": "s:img"},
    "pvcs": {"namespace": "ns", "pvc": "h:pvc", "storage_class": "s:sc"},
    "pvs": {"pv": "h:pv", "storage_class": "s:sc", "claim_namespace": "ns"},
    "storageclasses": {"storage_class": "s:sc", "parameter_keys": "keys"},
    "csidrivers": {},
    "services": {"namespace": "ns", "service": "h:svc", "external_name": "h:host", "annotation_keys": "keys"},
    "ingresses": {"namespace": "ns", "ingress": "h:ing", "domains": "h+:dom", "class": "s:ic", "annotation_keys": "keys"},
    "ingressclasses": {"ingress_class": "s:ic"},
    "ingress_nginx_config_keys": {},
    "networkpolicies": {"namespace": "ns"},
    "crds": {"crd": "s:crd", "group": "s:grp", "managed_by": "s:mb", "helm_release_namespace": "ns", "helm_release": "h:rel"},
    "webhooks": {"webhook_config": "s:wh", "service_namespaces": "nsl", "rules": "s:rule", "helm_release": "h:rel"},
    "apiservices": {"apiservice": "s:apis", "service_namespace": "ns"},
    "pdbs": {"namespace": "ns", "pdb": "h:pdb"},
    "hpa": {"namespace": "ns"},
    "priorityclasses": {"priority_class": "s:pc"},
    "clusterroles": {"clusterrole": "s:cr"},
    "bindings": {"namespace": "ns", "binding": "s:bind", "role": "s:cr", "groups": "h+:grp"},
    "secret_types": {},
    "helm_releases": {"namespace": "ns", "release": "h:rel", "chart": "s:chart"},
    "deprecated_apis_requested": {},
    "api_versions_in_use": {},
    "errors": {},
}  # fmt: skip

COLUMNS: dict[str, list[str]] = {
    "cluster": ["cluster_id", "context", "kubernetes_version", "collected_utc", "tool_version"],
    "api_versions_served": ["groupversion"],
    "nodes": "node roles ready unschedulable kubelet os_image kernel runtime arch cpu_capacity_m mem_capacity_gib "
             "pods_capacity ephemeral_gib gpu cpu_allocatable_m mem_allocatable_gib taints label_keys provider age_days "
             "cpu_used_pct mem_used_pct pods_running cpu_req_pct mem_req_pct".split(),
    "namespaces": "namespace system psa_enforce psa_warn label_keys quota quota_cpu quota_mem quota_storage limitrange "
                  "pods pods_running standalone_pods restarts cpu_req_cores cpu_lim_cores mem_req_gib mem_lim_gib gpu "
                  "cpu_used_cores mem_used_gib deployments statefulsets daemonsets cronjobs jobs pvcs pvc_requested_gib "
                  "pvc_used_gib rwx_pvcs svc_loadbalancer svc_nodeport ingresses networkpolicies age_days".split(),
    "workloads": "namespace kind name replicas ready owner_kind helm_chart managed_by containers init_containers "
                 "cpu_req_m_per_pod mem_req_mib_per_pod cpu_lim_m_per_pod mem_lim_mib_per_pod gpu_per_pod "
                 "cpu_req_m_total mem_req_gib_total containers_no_requests containers_no_limits containers_no_probes "
                 "privileged host_network host_pid host_ports cap_add pvc_volumes hostpath_volumes nfs_volumes "
                 "emptydir_volumes csi_inline_volumes node_selector_keys affinity topology_spread toleration_keys "
                 "priority_class custom_service_account strategy pvc_templates pvc_template_gib_total schedule "
                 "suspended age_days".split(),
    "images": "registry registry_public project repository tag pinned_by_digest pods_using workloads_using".split(),
    "pvcs": "namespace pvc storage_class access_modes volume_mode requested_gib capacity_gib phase used_gib "
            "source_type used_by mounted data_source age_days".split(),
    "pvs": "pv capacity_gib access_modes storage_class volume_mode reclaim phase source_type claim_namespace "
           "node_affinity_pinned".split(),
    "storageclasses": "storage_class provisioner reclaim binding_mode allow_expansion default parameter_keys".split(),
    "csidrivers": ["csi_driver", "attach_required", "modes"],
    "services": "namespace service type headless has_selector ports node_ports external_traffic_policy lb_class "
                "external_ips_count external_name annotation_keys ip_families session_affinity".split(),
    "ingresses": "namespace ingress class hosts wildcard_hosts domains tls_hosts tls_secrets paths path_types "
                 "backend_services default_backend nginx_annotations nginx_categories nginx_snippets "
                 "annotation_keys".split(),
    "ingressclasses": ["ingress_class", "controller", "default"],
    "ingress_nginx_config_keys": ["configmap", "keys"],
    "networkpolicies": ["namespace", "policies", "ingress_type", "egress_type", "namespace_wide", "ipblock_peers"],
    "crds": "crd group kind scope versions stored_versions conversion instances namespaces_with_instances in_use "
            "managed_by helm_release_namespace helm_release age_days".split(),
    "webhooks": "type webhook_config webhooks failure_policies service_namespaces url_based rules intercepts_all "
                "timeouts namespace_selector helm_release".split(),
    "apiservices": ["apiservice", "service_namespace", "available"],
    "pdbs": ["namespace", "pdb", "min_available", "max_unavailable", "expected_pods", "disruptions_allowed",
             "blocks_drain_now"],
    "hpa": ["namespace", "target_kind", "min", "max", "metrics"],
    "priorityclasses": ["priority_class", "value", "global_default"],
    "clusterroles": ["clusterrole", "system", "aggregated", "rules", "helm_managed"],
    "bindings": "binding_kind namespace binding role_kind role system subject_users subject_groups "
                "subject_serviceaccounts groups".split(),
    "secret_types": ["secret_type", "count"],
    "helm_releases": "namespace release revision chart chart_version app_version status last_deployed objects "
                     "kinds".split(),
    "deprecated_apis_requested": ["group", "version", "resource", "subresource", "removed_release"],
    "api_versions_in_use": ["api_version", "kind", "source", "objects"],
    "errors": ["step", "error"],
    "summary": "cluster_id context k8s_version nodes control_plane_nodes worker_nodes worker_vcpu worker_mem_gib gpus "
               "os_images namespaces_total namespaces_app pods_total pods_app app_cpu_req_cores app_mem_req_gib "
               "app_cpu_used_cores app_mem_used_gib deployments statefulsets daemonsets_app cronjobs jobs pvcs "
               "pvc_requested_gib pvc_used_gib pvc_rwx pvc_block_mode pvc_unmounted pv_source_types volumesnapshots "
               "svc_loadbalancer svc_nodeport svc_externalname svc_without_selector ingresses ingress_hosts "
               "ingress_with_nginx_annotations ingress_with_snippets networkpolicies crds_total crds_in_use "
               "cr_instances webhook_configs webhooks_non_system webhooks_intercept_all helm_releases "
               "images_distinct images_latest_tag private_registries deprecated_api_hits privileged_workloads "
               "hostpath_workloads hostnetwork_workloads operator_owned_workloads workloads_without_requests "
               "pdbs_blocking_drain custom_clusterroles non_system_cluster_admin_bindings serviceaccounts "
               "node_custom_label_keys node_taints tanzu_clusters_seen collection_errors complexity_flags".split(),
}  # fmt: skip

PUBLIC_KEY_PREFIXES = (
    "kubernetes.io k8s.io helm.sh cert-manager.io vmware.com cattle.io argoproj.io fluxcd.io traefik.io containo.us "
    "prometheus.io coreos.com istio.io linkerd.io cilium.io projectcalico.org antrea.io nvidia.com velero.io "
    "nutanix.com d2iq.io mesosphere.com bitnami.com elastic.co kyverno.io gatekeeper.sh openpolicyagent.org "
    "opentelemetry.io grafana.com metallb.io kube-vip.io rancher.io longhorn.io strimzi.io mongodb.com percona.com "
    "zalando.org cnpg.io min.io keda.sh knative.dev tekton.dev kubeflow.org dapr.io"
).split()
PUBLIC_REGISTRIES = (
    "docker.io index.docker.io quay.io gcr.io k8s.gcr.io registry.k8s.io ghcr.io mcr.microsoft.com public.ecr.aws "
    "docker.elastic.co nvcr.io registry.gitlab.com projects.registry.vmware.com registry.access.redhat.com "
    "registry.redhat.io registry.suse.com"
).split()
NODE_LABEL_SYSTEM = re.compile(r"^(kubernetes\.io|beta\.kubernetes\.io|node\.kubernetes\.io|topology\.kubernetes\.io)/")
BUILTIN_OWNERS = ("ReplicaSet", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicationController")
KIND_LABELS = {
    "deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet", "jobs": "Job",
    "cronjobs": "CronJob", "replicationcontrollers": "ReplicationController",
}  # fmt: skip

_URL = re.compile(r"https?://[^\",\s]+")
_IPV4 = re.compile(r"(^|[^0-9.])((?:[0-9]{1,3}\.){3}[0-9]{1,3})(?=[^0-9.]|$)")
_IPV6 = re.compile(r"([0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}")
SELF_CHECK_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass
class ExportOptions:
    scope: Scope = field(default_factory=Scope)
    strict: bool = False


@dataclass
class SelfCheckHit:
    file: str
    column: str
    value_sample: str  # masked


class ExportBlocked(Exception):
    def __init__(self, hits: list[SelfCheckHit]):
        super().__init__(f"self-check found {len(hits)} problem(s)")
        self.hits = hits


# ---- small formatting helpers, as the script prints them ----


def _gib(b: float) -> str:
    return f"{b / 1073741824:.2f}"


def _b(v) -> str:
    """Boolean as the script's awk prints it."""
    return "1" if v else "0"


def _qty(parse, v) -> int:
    try:
        return parse(v) if v not in (None, "") else 0
    except (ArithmeticError, ValueError, TypeError):
        return 0


def _age(created: str | None, today: date) -> str:
    if not created:
        return ""
    try:
        return str((today - date.fromisoformat(created[:10])).days)
    except ValueError:
        return ""


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _words(v) -> list[str]:
    return [x for x in (v or []) if x not in (None, "")]


# ---- loading one cluster ----


@dataclass
class Obj:
    kind: str
    namespace: str
    name: str
    data: dict
    labels: dict
    annotations: dict
    images: list[str]
    created_at: str | None


def _load(conn: sqlite3.Connection, site: str, cluster_id: str) -> dict[str, list[Obj]]:
    labels: dict[int, dict] = defaultdict(dict)
    for rid, k, v in conn.execute(
        "SELECT l.resource_id, l.key, l.value FROM resource_labels l JOIN resources r ON r.id = l.resource_id"
        " WHERE r.site=? AND r.cluster_id=?",
        (site, cluster_id),
    ):
        labels[rid][k] = v
    anns: dict[int, dict] = defaultdict(dict)
    for rid, k, v in conn.execute(
        "SELECT a.resource_id, a.key, a.value FROM resource_annotations a JOIN resources r ON r.id = a.resource_id"
        " WHERE r.site=? AND r.cluster_id=?",
        (site, cluster_id),
    ):
        anns[rid][k] = v
    images: dict[int, list] = defaultdict(list)
    for rid, img in conn.execute(
        "SELECT i.resource_id, i.image FROM resource_images i JOIN resources r ON r.id = i.resource_id"
        " WHERE r.site=? AND r.cluster_id=?",
        (site, cluster_id),
    ):
        images[rid].append(img)
    out: dict[str, list[Obj]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, kind, namespace, name, data, created_at FROM resources WHERE site=? AND cluster_id=?"
        " ORDER BY kind, namespace, name",
        (site, cluster_id),
    ):
        out[r["kind"]].append(
            Obj(r["kind"], r["namespace"], r["name"], json.loads(r["data"]), labels[r["id"]], anns[r["id"]],
                images[r["id"]], r["created_at"])
        )  # fmt: skip
    return out


def _optional(conn, site: str, cluster_id: str, collector: str) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT key, value FROM optional_results WHERE site=? AND cluster_id=? AND collector=?",
            (site, cluster_id, collector),
        )
    }


# ---- building the clear tables ----


def build_tables(conn, site: str, cluster_id: str, rules: Rules, today: date) -> dict[str, list[dict]]:
    o = _load(conn, site, cluster_id)
    c = conn.execute("SELECT * FROM clusters WHERE site=? AND cluster_id=?", (site, cluster_id)).fetchone()
    prof = conn.execute("SELECT * FROM cluster_profile WHERE site=? AND cluster_id=?", (site, cluster_id)).fetchone()
    sys_ns = lambda n: is_system_namespace(n, rules)  # noqa: E731
    t: dict[str, list[dict]] = {}

    uid = (prof["cluster_uid"] if prof else None) or cluster_id
    t["cluster"] = [{
        "cluster_id": uid, "context": c["name"], "kubernetes_version": c["k8s_version"] or "",
        "collected_utc": c["last_ok_at"] or "", "tool_version": f"rancher-inventory {__version__}",
    }]  # fmt: skip
    t["api_versions_served"] = [
        {"groupversion": r[0]}
        for r in conn.execute(
            "SELECT group_version FROM api_versions WHERE site=? AND cluster_id=? AND source='served'"
            " ORDER BY group_version",
            (site, cluster_id),
        )
    ]

    # pods per node and per namespace
    pods = o["pods"]
    by_node: dict[str, list[Obj]] = defaultdict(list)
    for p in pods:
        if p.data.get("status", {}).get("phase") in ("Running", "Pending") and p.data.get("spec", {}).get("nodeName"):
            by_node[p.data["spec"]["nodeName"]].append(p)

    t["nodes"] = []
    for n in o["nodes"]:
        d = n.data
        cap, alloc = d.get("status", {}).get("capacity", {}), d.get("status", {}).get("allocatable", {})
        info = d.get("status", {}).get("nodeInfo", {})
        ac, am = _qty(parse_cpu, alloc.get("cpu")), _qty(parse_bytes, alloc.get("memory"))
        np_ = by_node.get(n.name, [])
        creq = sum(p.data.get("cpu_req_m") or 0 for p in np_)
        mreq = sum(p.data.get("mem_req_b") or 0 for p in np_)
        gpu = sum(_qty(lambda v: int(parse_cpu(v) / 1000), cap.get(k)) for k in ("nvidia.com/gpu", "amd.com/gpu"))
        t["nodes"].append({
            "node": n.name, "roles": ";".join(d.get("roles") or []) or "worker", "ready": d.get("ready") or "",
            "unschedulable": _b(d.get("spec", {}).get("unschedulable") is True),
            "kubelet": info.get("kubeletVersion", ""), "os_image": info.get("osImage", ""),
            "kernel": info.get("kernelVersion", ""), "runtime": info.get("containerRuntimeVersion", ""),
            "arch": info.get("architecture", ""), "cpu_capacity_m": _qty(parse_cpu, cap.get("cpu")),
            "mem_capacity_gib": _gib(_qty(parse_bytes, cap.get("memory"))), "pods_capacity": cap.get("pods", ""),
            "ephemeral_gib": _gib(_qty(parse_bytes, cap.get("ephemeral-storage"))), "gpu": gpu,
            "cpu_allocatable_m": ac, "mem_allocatable_gib": _gib(am), "taints": ";".join(d.get("taints") or []),
            "label_keys": ";".join(k for k in sorted(n.labels) if not k.startswith("node-role.kubernetes.io/")
                                   and k != "kubernetes.io/hostname"),
            "provider": "" if d.get("provider") == "none" else d.get("provider", ""),
            "age_days": _age(n.created_at, today), "cpu_used_pct": "", "mem_used_pct": "",
            "pods_running": len(np_), "cpu_req_pct": f"{100 * creq / ac:.1f}" if ac else "",
            "mem_req_pct": f"{100 * mreq / am:.1f}" if am else "",
        })  # fmt: skip

    # workloads
    t["workloads"] = []
    for kind, label in KIND_LABELS.items():
        for w in o[kind]:
            d = w.data
            owner = f"{d.get('owner_api_version') or ''}/{d.get('owner_kind') or ''}"
            if owner == "/" or (d.get("owner_kind") in BUILTIN_OWNERS):
                owner = ""
            spec, st = d.get("spec", {}), d.get("status", {})
            if kind == "daemonsets":
                rep, ready = st.get("desiredNumberScheduled"), st.get("numberReady")
            elif kind == "jobs":
                rep, ready = spec.get("parallelism"), st.get("active")
            elif kind == "cronjobs":
                rep, ready = 1, None
            else:
                rep, ready = spec.get("replicas"), st.get("readyReplicas")
            if rep is None:
                rep = 0 if kind == "daemonsets" else 1
            tpl = d.get("volume_claim_templates") or []
            tpl_bytes = sum(_qty(parse_bytes, x.get("size")) for x in tpl)
            t["workloads"].append({
                "namespace": w.namespace, "kind": label, "name": w.name, "replicas": rep, "ready": int(ready or 0),
                "owner_kind": owner, "helm_chart": w.labels.get("helm.sh/chart", ""),
                "managed_by": w.labels.get("app.kubernetes.io/managed-by", ""),
                "containers": d.get("containers", 0), "init_containers": d.get("init_containers", 0),
                "cpu_req_m_per_pod": d.get("cpu_req_m", 0), "mem_req_mib_per_pod": int((d.get("mem_req_b") or 0) / 1048576),
                "cpu_lim_m_per_pod": d.get("cpu_lim_m", 0), "mem_lim_mib_per_pod": int((d.get("mem_lim_b") or 0) / 1048576),
                "gpu_per_pod": d.get("gpu", 0), "cpu_req_m_total": (d.get("cpu_req_m") or 0) * rep,
                "mem_req_gib_total": _gib((d.get("mem_req_b") or 0) * rep),
                "containers_no_requests": d.get("no_requests", 0), "containers_no_limits": d.get("no_limits", 0),
                "containers_no_probes": d.get("no_probes", 0), "privileged": d.get("privileged", 0),
                "host_network": _b(d.get("host_network")), "host_pid": _b(d.get("host_pid")),
                "host_ports": d.get("host_ports", 0), "cap_add": " ".join(d.get("added_caps") or []),
                "pvc_volumes": len(d.get("pvc_claims") or []), "hostpath_volumes": d.get("hostpath", 0),
                "nfs_volumes": d.get("nfs", 0), "emptydir_volumes": d.get("emptydir", 0),
                "csi_inline_volumes": len(d.get("csi_drivers") or []),
                "node_selector_keys": ";".join(d.get("node_selector_keys") or []), "affinity": _b(d.get("affinity")),
                "topology_spread": _b(d.get("topology_spread")),
                "toleration_keys": " ".join(d.get("toleration_keys") or []),
                "priority_class": d.get("priority_class") or "",
                "custom_service_account": _b(d.get("service_account")), "strategy": d.get("update_strategy") or "",
                "pvc_templates": ";".join(f"{x.get('size') or ''}/{x.get('storage_class') or ''}/" for x in tpl),
                "pvc_template_gib_total": _gib(tpl_bytes * rep), "schedule": d.get("schedule") or "",
                "suspended": _str(d.get("suspended")) if kind == "cronjobs" else "",
                "age_days": _age(w.created_at, today),
            })  # fmt: skip

    # images: pods use them, workloads declare them
    pod_imgs: Counter[str] = Counter(i for p in pods for i in p.images)
    wl_imgs: Counter[str] = Counter(i for kind in KIND_LABELS for w in o[kind] for i in w.images)
    t["images"] = []
    for ref in sorted(set(pod_imgs) | set(wl_imgs), key=lambda r: (-pod_imgs[r], -wl_imgs[r], r)):
        t["images"].append({**_split_image(ref), "pods_using": pod_imgs[ref], "workloads_using": wl_imgs[ref]})

    # storage
    pv_by_name = {p.name: p for p in o["persistentvolumes"]}
    usage = _optional(conn, site, cluster_id, "pvc_usage")
    t["pvcs"] = []
    for p in o["persistentvolumeclaims"]:
        d = p.data
        pv = pv_by_name.get(d.get("volume_name") or "")
        used = usage.get(f"{p.namespace}/{p.name}")
        t["pvcs"].append({
            "namespace": p.namespace, "pvc": p.name, "storage_class": d.get("spec", {}).get("storageClassName") or "",
            "access_modes": " ".join(d.get("access_modes") or []), "volume_mode": d.get("volume_mode") or "Filesystem",
            "requested_gib": _gib(_qty(parse_bytes, d.get("spec", {}).get("resources", {}).get("requests", {}).get("storage"))),
            "capacity_gib": _gib(_qty(parse_bytes, d.get("status", {}).get("capacity", {}).get("storage"))),
            "phase": d.get("status", {}).get("phase") or "", "used_gib": _gib(int(used)) if used else "",
            "source_type": pv.data.get("source", "") if pv else "", "used_by": ";".join(d.get("used_by") or []),
            "mounted": _b(d.get("mounted")), "data_source": d.get("data_source_kind") or "",
            "age_days": _age(p.created_at, today),
        })  # fmt: skip
    t["pvs"] = [{
        "pv": p.name, "capacity_gib": _gib(_qty(parse_bytes, p.data.get("spec", {}).get("capacity", {}).get("storage"))),
        "access_modes": " ".join(p.data.get("access_modes") or []),
        "storage_class": p.data.get("spec", {}).get("storageClassName") or "",
        "volume_mode": p.data.get("volume_mode") or "Filesystem", "reclaim": p.data.get("reclaim") or "",
        "phase": p.data.get("status", {}).get("phase") or "", "source_type": p.data.get("source") or "other",
        "claim_namespace": p.data.get("spec", {}).get("claimRef", {}).get("namespace") or "",
        "node_affinity_pinned": _b(p.data.get("pinned")),
    } for p in o["persistentvolumes"]]  # fmt: skip
    t["storageclasses"] = [{
        "storage_class": s.name, "provisioner": s.data.get("provisioner", ""), "reclaim": s.data.get("reclaimPolicy", ""),
        "binding_mode": s.data.get("volumeBindingMode", ""), "allow_expansion": _b(s.data.get("allow_expansion")),
        "default": _b(s.data.get("default")), "parameter_keys": ";".join(s.data.get("parameter_keys") or []),
    } for s in o["storageclasses"]]  # fmt: skip
    t["csidrivers"] = [{
        "csi_driver": s.name, "attach_required": _str(s.data.get("attach_required")),
        "modes": " ".join(s.data.get("modes") or []),
    } for s in o["csidrivers"]]  # fmt: skip

    # network
    t["services"] = []
    for s in o["services"]:
        d, spec = s.data, s.data.get("spec", {})
        ports = "".join(f"{p.get('port')}/{p.get('protocol') or 'TCP'};" for p in spec.get("ports") or [])
        t["services"].append({
            "namespace": s.namespace, "service": s.name, "type": spec.get("type") or "ClusterIP",
            "headless": _b(d.get("headless")), "has_selector": _b(d.get("has_selector")), "ports": ports,
            "node_ports": d.get("node_ports", 0), "external_traffic_policy": d.get("external_traffic_policy") or "",
            "lb_class": d.get("lb_class") or "", "external_ips_count": len(spec.get("externalIPs") or []),
            "external_name": spec.get("externalName") or "", "annotation_keys": ";".join(d.get("annotation_keys") or []),
            "ip_families": " ".join(d.get("ip_families") or []), "session_affinity": d.get("session_affinity") or "",
        })  # fmt: skip
    t["ingresses"] = [_ingress_row(i) for i in o["ingresses"]]
    t["ingressclasses"] = [{
        "ingress_class": i.name, "controller": i.data.get("spec", {}).get("controller", ""),
        "default": _b(i.data.get("default")),
    } for i in o["ingressclasses"]]  # fmt: skip
    nginx = _optional(conn, site, cluster_id, "nginx_config_keys")
    t["ingress_nginx_config_keys"] = [{"configmap": k, "keys": v} for k, v in sorted(nginx.items())]
    np_rows: dict[str, dict] = {}
    for p in o["networkpolicies"]:
        r = np_rows.setdefault(p.namespace, {"namespace": p.namespace, "policies": 0, "ingress_type": 0,
                                              "egress_type": 0, "namespace_wide": 0, "ipblock_peers": 0})
        types = p.data.get("policy_types") or []
        r["policies"] += 1
        r["ingress_type"] += "Ingress" in types or not types
        r["egress_type"] += "Egress" in types
        r["namespace_wide"] += bool(p.data.get("namespace_wide"))
        r["ipblock_peers"] += p.data.get("ipblock_peers") or 0
    t["networkpolicies"] = list(np_rows.values())

    # extensions
    cr = _optional(conn, site, cluster_id, "cr_counts")
    t["crds"] = []
    for d_ in o["customresourcedefinitions"]:
        d, spec = d_.data, d_.data.get("spec", {})
        versions = "".join(
            f"{v.get('name')}(served={_str(v.get('served'))},storage={_str(v.get('storage'))},"
            f"deprecated={_str(v.get('deprecated'))});"
            for v in spec.get("versions") or []
        )
        count = cr.get(d_.name)
        t["crds"].append({
            "crd": d_.name, "group": spec.get("group", ""), "kind": spec.get("names", {}).get("kind", ""),
            "scope": spec.get("scope", ""), "versions": versions,
            "stored_versions": " ".join(d.get("status", {}).get("storedVersions") or []),
            "conversion": spec.get("conversion", {}).get("strategy") or "None",
            "instances": count or "", "namespaces_with_instances": "", "in_use": _b(count and int(count) > 0),
            "managed_by": d_.labels.get("app.kubernetes.io/managed-by", ""),
            "helm_release_namespace": d_.annotations.get("meta.helm.sh/release-namespace", ""),
            "helm_release": d_.annotations.get("meta.helm.sh/release-name", ""), "age_days": _age(d_.created_at, today),
        })  # fmt: skip
    t["webhooks"] = []
    for kind, typ in (("mutatingwebhookconfigurations", "mutating"), ("validatingwebhookconfigurations", "validating")):
        for w in o[kind]:
            d = w.data
            t["webhooks"].append({
                "type": typ, "webhook_config": w.name, "webhooks": d.get("webhooks", 0),
                "failure_policies": " ".join(_words(d.get("failure_policies"))),
                "service_namespaces": " ".join(d.get("service_namespaces") or []), "url_based": _b(d.get("url_based")),
                "rules": "".join(f"{r};" for r in d.get("rules") or []), "intercepts_all": _b(d.get("intercepts_all")),
                "timeouts": " ".join(str(x) for x in _words(d.get("timeouts"))),
                "namespace_selector": _b(d.get("has_namespace_selector")), "helm_release": d.get("helm_release") or "",
            })  # fmt: skip
    t["apiservices"] = [{
        "apiservice": a.name, "service_namespace": a.data.get("service_namespace") or "",
        "available": "True" if a.data.get("available") else "False",
    } for a in o["apiservices"]]  # fmt: skip
    t["pdbs"] = [{
        "namespace": p.namespace, "pdb": p.name, "min_available": _str(p.data.get("spec", {}).get("minAvailable")),
        "max_unavailable": _str(p.data.get("spec", {}).get("maxUnavailable")),
        "expected_pods": _str(p.data.get("status", {}).get("expectedPods")),
        "disruptions_allowed": _str(p.data.get("status", {}).get("disruptionsAllowed")),
        "blocks_drain_now": _b(p.data.get("blocks_drain")),
    } for p in o["poddisruptionbudgets"]]  # fmt: skip
    t["hpa"] = [{
        "namespace": h.namespace, "target_kind": h.data.get("spec", {}).get("scaleTargetRef", {}).get("kind", ""),
        "min": _str(h.data.get("spec", {}).get("minReplicas")), "max": _str(h.data.get("spec", {}).get("maxReplicas")),
        "metrics": " ".join(_words(h.data.get("metric_types"))),
    } for h in o["horizontalpodautoscalers"]]  # fmt: skip
    t["priorityclasses"] = [{
        "priority_class": p.name, "value": _str(p.data.get("value")), "global_default": _str(p.data.get("globalDefault")),
    } for p in o["priorityclasses"]]  # fmt: skip

    # RBAC, with the script's system rules for the CSV rows
    t["clusterroles"] = []
    for r in o["clusterroles"]:
        system = r.name.startswith("system:") or r.labels.get("kubernetes.io/bootstrapping") == "rbac-defaults" \
            or r.name in ("cluster-admin", "admin", "edit", "view")
        if not system:
            t["clusterroles"].append({
                "clusterrole": r.name, "system": "0", "aggregated": _b(r.data.get("aggregated")),
                "rules": r.data.get("rule_count", 0), "helm_managed": _b(r.data.get("helm_managed")),
            })  # fmt: skip
    t["bindings"] = []
    from inventory.web.queries import is_system_binding

    for kind, label in (("clusterrolebindings", "ClusterRoleBinding"), ("rolebindings", "RoleBinding")):
        for b in o[kind]:
            role_kind, role = (b.data.get("role") or [None, None])
            subjects = b.data.get("subjects") or []
            if is_system_binding(b.name, b.namespace, role or "", subjects):
                continue
            groups = [n for k, n in subjects if k == "Group" and not (n or "").startswith("system:")]
            t["bindings"].append({
                "binding_kind": label, "namespace": b.namespace, "binding": b.name, "role_kind": role_kind or "",
                "role": role or "", "system": "0", "subject_users": sum(1 for k, _ in subjects if k == "User"),
                "subject_groups": sum(1 for k, _ in subjects if k == "Group"),
                "subject_serviceaccounts": sum(1 for k, _ in subjects if k == "ServiceAccount"),
                "groups": ";".join(groups),
            })  # fmt: skip
    t["secret_types"] = [
        {"secret_type": k, "count": v} for k, v in sorted(_optional(conn, site, cluster_id, "secret_types").items())
    ]

    # Helm from Rancher catalog apps
    t["helm_releases"] = []
    for h in o["helmreleases"]:
        spec = h.data.get("spec", {})
        res = spec.get("resources") or []
        kinds: list[str] = []
        for r in res:
            if r.get("kind") and r["kind"] not in kinds:
                kinds.append(r["kind"])
        meta = spec.get("chart", {}).get("metadata", {})
        t["helm_releases"].append({
            "namespace": h.namespace, "release": spec.get("name") or h.name, "revision": _str(spec.get("version")),
            "chart": meta.get("name", ""), "chart_version": meta.get("version", ""),
            "app_version": meta.get("appVersion", ""), "status": spec.get("info", {}).get("status", ""),
            "last_deployed": (spec.get("info", {}).get("lastDeployed") or "")[:10], "objects": len(res),
            "kinds": ";".join(kinds),
        })  # fmt: skip
    dep = _optional(conn, site, cluster_id, "deprecated_api_metrics")
    t["deprecated_apis_requested"] = []
    for key in sorted(dep):
        parts = (key.split("|") + [""] * 5)[:5]
        t["deprecated_apis_requested"].append(dict(zip(COLUMNS["deprecated_apis_requested"], parts)))
    in_use: Counter[tuple[str, str, str]] = Counter()
    for h in o["helmreleases"]:
        for r in h.data.get("spec", {}).get("resources") or []:
            if r.get("apiVersion") and r.get("kind"):
                in_use[(r["apiVersion"], r["kind"], "helm-manifest")] += 1
    for gv, kind, n in conn.execute(
        "SELECT group_version, kind, objects FROM api_versions WHERE site=? AND cluster_id=? AND source='in_use'",
        (site, cluster_id),
    ):
        in_use[(gv, kind, "last-applied")] += n or 0
    t["api_versions_in_use"] = [
        {"api_version": gv, "kind": k, "source": s, "objects": n} for (gv, k, s), n in sorted(in_use.items())
    ]
    t["errors"] = [
        {"step": r[0], "error": r[1] or r[2]}
        for r in conn.execute(
            "SELECT kind, error, status FROM run_kinds WHERE site=? AND cluster_id=? AND status != 'ok'"
            " AND run_id = (SELECT max(run_id) FROM run_kinds WHERE site=? AND cluster_id=?) ORDER BY kind",
            (site, cluster_id, site, cluster_id),
        )
    ]

    t["namespaces"] = _namespace_rows(conn, site, cluster_id, o, t, today, sys_ns)
    t["summary"] = [_summary(t, o, c, prof, uid, sys_ns)]
    return t


def _split_image(ref: str) -> dict:
    r, digest = ref, False
    if "@" in r:
        digest, r = True, r.split("@", 1)[0]
    last = r.rsplit("/", 1)[-1]
    tag = ""
    if ":" in last:
        tag = last.split(":", 1)[1]
        r = r[: len(r) - len(tag) - 1]
    if not tag and not digest:
        tag = "latest(implicit)"
    parts = r.split("/")
    reg, start = "docker.io", 0
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        reg, start = parts[0], 1
    path = "/".join(parts[start:])
    public = reg.lower() in PUBLIC_REGISTRIES
    if public:
        project, repo = "", path
    else:
        project, repo = "/".join(parts[start:-1]), parts[-1]
    return {"registry": reg, "registry_public": _b(public), "project": project, "repository": repo, "tag": tag,
            "pinned_by_digest": _b(digest)}  # fmt: skip


def _ingress_row(i: Obj) -> dict:
    d, spec = i.data, i.data.get("spec", {})
    hosts = [r.get("host") for r in spec.get("rules") or [] if r.get("host")]
    domains: list[str] = []
    for h in hosts:
        parts = h.split(".")
        dom = ".".join(parts[-2:]) if len(parts) >= 2 else h
        if dom not in domains:
            domains.append(dom)
    paths = [p for r in spec.get("rules") or [] for p in (r.get("http") or {}).get("paths") or []]
    backends = {((p.get("backend") or {}).get("service") or {}).get("name") for p in paths} - {None}
    types = Counter(p.get("pathType") for p in paths if p.get("pathType"))
    return {
        "namespace": i.namespace, "ingress": i.name, "class": d.get("class") or "", "hosts": len(hosts),
        "wildcard_hosts": sum(1 for h in hosts if h.startswith("*")), "domains": ";".join(domains),
        "tls_hosts": sum(len(t.get("hosts") or []) for t in spec.get("tls") or []),
        "tls_secrets": sum(1 for t in spec.get("tls") or [] if t.get("secretName")), "paths": len(paths),
        "path_types": ";".join(f"{k}={v}" for k, v in types.items()), "backend_services": len(backends),
        "default_backend": _b(d.get("default_backend")), "nginx_annotations": d.get("nginx_annotations", 0),
        "nginx_categories": ";".join(f"{k}={v}" for k, v in (d.get("nginx_categories") or {}).items()),
        "nginx_snippets": _b(d.get("has_snippet")), "annotation_keys": ";".join(d.get("annotation_keys") or []),
    }  # fmt: skip


def _namespace_rows(conn, site, cluster_id, o, t, today, sys_ns) -> list[dict]:
    prof = {r["namespace"]: r for r in conn.execute(
        "SELECT * FROM namespace_profile WHERE site=? AND cluster_id=?", (site, cluster_id))}  # fmt: skip
    quotas = {q.namespace: q.data for q in o["resourcequotas"]}
    pvc_req: Counter[str] = Counter()
    pvc_used: Counter[str] = Counter()
    for p in t["pvcs"]:
        pvc_req[p["namespace"]] += float(p["requested_gib"])
        pvc_used[p["namespace"]] += float(p["used_gib"] or 0)
    rows = []
    for n in o["namespaces"]:
        p = prof.get(n.name)
        g = (lambda k: (p[k] if p is not None and p[k] is not None else 0))  # noqa: E731
        q = quotas.get(n.name, {})
        used = p is not None and p["cpu_used_m"] is not None
        rows.append({
            "namespace": n.name, "system": _b(sys_ns(n.name)),
            "psa_enforce": n.labels.get("pod-security.kubernetes.io/enforce", ""),
            "psa_warn": n.labels.get("pod-security.kubernetes.io/warn", ""),
            "label_keys": ";".join(k for k in sorted(n.labels) if k != "kubernetes.io/metadata.name"),
            "quota": _b(g("has_quota")), "quota_cpu": q.get("quota_cpu") or "", "quota_mem": q.get("quota_mem") or "",
            "quota_storage": q.get("quota_storage") or "", "limitrange": _b(g("has_limitrange")),
            "pods": g("pods"), "pods_running": g("pods_running"), "standalone_pods": g("pods_standalone"),
            "restarts": g("restarts"), "cpu_req_cores": f"{g('cpu_req_m') / 1000:.2f}",
            "cpu_lim_cores": f"{g('cpu_lim_m') / 1000:.2f}", "mem_req_gib": _gib(g("mem_req_b")),
            "mem_lim_gib": _gib(g("mem_lim_b")), "gpu": g("gpu"),
            "cpu_used_cores": f"{g('cpu_used_m') / 1000:.2f}" if used else "",
            "mem_used_gib": _gib(g("mem_used_b")) if used else "",
            "deployments": g("deployments"), "statefulsets": g("statefulsets"), "daemonsets": g("daemonsets"),
            "cronjobs": g("cronjobs"), "jobs": g("jobs"), "pvcs": g("pvc_count"),
            "pvc_requested_gib": f"{pvc_req[n.name]:.2f}", "pvc_used_gib": f"{pvc_used[n.name]:.2f}",
            "rwx_pvcs": g("pvc_rwx"), "svc_loadbalancer": g("lb_services"), "svc_nodeport": g("nodeport_services"),
            "ingresses": g("ingresses"), "networkpolicies": g("netpols"), "age_days": _age(n.created_at, today),
        })  # fmt: skip
    return rows


def _summary(t, o, c, prof, uid, sys_ns) -> dict:
    nodes = t["nodes"]
    workers = [n for n in nodes if not re.search(r"control-plane|master", n["roles"])]
    app_ns = [n for n in t["namespaces"] if n["system"] == "0"]
    app_wl = [w for w in t["workloads"] if not sys_ns(w["namespace"])]
    kinds = Counter(w["kind"] for w in app_wl)
    pvcs = t["pvcs"]
    pvc_used = [float(p["used_gib"]) for p in pvcs if p["used_gib"] != ""]
    svcs = t["services"]
    crds_in_use = [r for r in t["crds"] if r["in_use"] == "1"]
    metrics = any(n["cpu_used_cores"] != "" for n in app_ns)
    label_keys = {k for n in nodes for k in n["label_keys"].split(";") if k and not NODE_LABEL_SYSTEM.match(k)}
    taints = {x for n in nodes for x in n["taints"].split(";") if x}
    flags = json.loads(prof["complexity_flags"]) if prof is not None and prof["complexity_flags"] else []
    custom_roles = sum(1 for r in o["clusterroles"]
                       if r.data.get("rancher_generated") is False and r.data.get("platform") is False)  # fmt: skip
    ca = sum(1 for b in t["bindings"] if b["binding_kind"] == "ClusterRoleBinding" and b["role"] == "cluster-admin"
             and not any(x.name == b["binding"] and x.data.get("rancher_generated") for x in o["clusterrolebindings"]))
    return {
        "cluster_id": uid, "context": c["name"], "k8s_version": c["k8s_version"] or "", "nodes": len(nodes),
        "control_plane_nodes": len(nodes) - len(workers), "worker_nodes": len(workers),
        "worker_vcpu": f"{sum(n['cpu_capacity_m'] for n in workers) / 1000:.1f}",
        "worker_mem_gib": f"{sum(float(n['mem_capacity_gib']) for n in workers):.1f}",
        "gpus": sum(n["gpu"] for n in nodes), "os_images": ";".join(sorted({n["os_image"] for n in nodes})),
        "namespaces_total": len(t["namespaces"]), "namespaces_app": len(app_ns),
        "pods_total": sum(n["pods"] for n in t["namespaces"]), "pods_app": sum(n["pods"] for n in app_ns),
        "app_cpu_req_cores": f"{sum(float(n['cpu_req_cores']) for n in app_ns):.1f}",
        "app_mem_req_gib": f"{sum(float(n['mem_req_gib']) for n in app_ns):.1f}",
        "app_cpu_used_cores": f"{sum(float(n['cpu_used_cores'] or 0) for n in app_ns):.1f}" if metrics else "",
        "app_mem_used_gib": f"{sum(float(n['mem_used_gib'] or 0) for n in app_ns):.1f}" if metrics else "",
        "deployments": kinds["Deployment"], "statefulsets": kinds["StatefulSet"], "daemonsets_app": kinds["DaemonSet"],
        "cronjobs": kinds["CronJob"], "jobs": kinds["Job"], "pvcs": len(pvcs),
        "pvc_requested_gib": f"{sum(float(p['requested_gib']) for p in pvcs):.1f}",
        "pvc_used_gib": f"{sum(pvc_used):.1f}" if pvc_used else "",
        "pvc_rwx": sum(1 for p in pvcs if "ReadWriteMany" in p["access_modes"]),
        "pvc_block_mode": sum(1 for p in pvcs if p["volume_mode"] == "Block"),
        "pvc_unmounted": sum(1 for p in pvcs if p["mounted"] == "0"),
        "pv_source_types": ";".join(sorted({p["source_type"] for p in t["pvs"]})),
        "volumesnapshots": len(o["volumesnapshots"]),
        "svc_loadbalancer": sum(1 for s in svcs if s["type"] == "LoadBalancer"),
        "svc_nodeport": sum(1 for s in svcs if s["type"] == "NodePort"),
        "svc_externalname": sum(1 for s in svcs if s["type"] == "ExternalName"),
        "svc_without_selector": sum(1 for s in svcs if s["has_selector"] == "0" and s["type"] != "ExternalName"),
        "ingresses": len(t["ingresses"]), "ingress_hosts": sum(i["hosts"] for i in t["ingresses"]),
        "ingress_with_nginx_annotations": sum(1 for i in t["ingresses"] if i["nginx_annotations"]),
        "ingress_with_snippets": sum(1 for i in t["ingresses"] if i["nginx_snippets"] == "1"),
        "networkpolicies": sum(n["networkpolicies"] for n in t["namespaces"]), "crds_total": len(t["crds"]),
        "crds_in_use": len(crds_in_use), "cr_instances": sum(int(r["instances"] or 0) for r in crds_in_use),
        "webhook_configs": len(t["webhooks"]),
        "webhooks_non_system": sum(1 for w in t["webhooks"] if w["url_based"] == "1" or any(
            not sys_ns(n) for n in w["service_namespaces"].split())),
        "webhooks_intercept_all": sum(1 for w in t["webhooks"] if w["intercepts_all"] == "1"),
        "helm_releases": len(t["helm_releases"]), "images_distinct": len(t["images"]),
        "images_latest_tag": sum(1 for i in t["images"] if i["tag"] in ("latest", "latest(implicit)")),
        "private_registries": len({i["registry"] for i in t["images"] if i["registry_public"] == "0"}),
        "deprecated_api_hits": len(t["deprecated_apis_requested"]),
        "privileged_workloads": sum(1 for w in app_wl if w["privileged"]),
        "hostpath_workloads": sum(1 for w in app_wl if w["hostpath_volumes"]),
        "hostnetwork_workloads": sum(1 for w in app_wl if w["host_network"] == "1"),
        "operator_owned_workloads": sum(1 for w in app_wl if w["owner_kind"]),
        "workloads_without_requests": sum(1 for w in app_wl if w["containers_no_requests"]),
        "pdbs_blocking_drain": sum(1 for p in t["pdbs"] if p["blocks_drain_now"] == "1"),
        "custom_clusterroles": custom_roles, "non_system_cluster_admin_bindings": ca,
        "serviceaccounts": len(o["serviceaccounts"]), "node_custom_label_keys": len(label_keys),
        "node_taints": len(taints), "tanzu_clusters_seen": 0, "collection_errors": len(t["errors"]),
        "complexity_flags": ";".join(flags),
    }  # fmt: skip


# ---- anonymisation (the script's ANON_AWK, in Python) ----


class Anonymiser:
    def __init__(self, salt: str, sensitive: list[str], strict: bool, rules: Rules):
        self.salt = salt
        self.sensitive = [s.lower() for s in sensitive if s]
        self.strict = strict
        self.rules = rules
        self.used: dict[tuple[str, str], str] = {}

    def is_sensitive(self, v: str) -> bool:
        lv = v.lower()
        return any(s in lv for s in self.sensitive)

    def want(self, type_: str, v: str) -> str:
        if v == "":
            return v
        h = hash_name(self.salt, type_, v)
        self.used[(type_, v)] = h
        return h

    def _public_prefix(self, p: str) -> bool:
        lp = p.lower()
        return any(lp == x or lp.endswith("." + x) for x in PUBLIC_KEY_PREFIXES)

    def tx(self, t: str, v: str) -> str:
        if v == "":
            return v
        if t == "ns":
            if is_system_namespace(v, self.rules) and not self.is_sensitive(v):
                return v
            return self.want("ns", v)
        if t == "nsl":
            return ";".join(self.tx("ns", x) for x in v.split(" ") if x)
        if t == "reg":
            if v.lower() in PUBLIC_REGISTRIES and not self.is_sensitive(v):
                return v
            return self.want("reg", v)
        if t.startswith("h+:"):
            return ";".join(self.want(t[3:], x) for x in v.split(";"))
        if t.startswith("h:"):
            return self.want(t[2:], v)
        if t.startswith("s:"):
            return self.want(t[2:], v) if self.strict or self.is_sensitive(v) else v
        if t == "keys":
            out = []
            for r in v.split(";"):
                if "/" in r:
                    p, rest = r.split("/", 1)
                    if not self._public_prefix(p) or self.is_sensitive(p):
                        r = self.want("pfx", p) + "/" + rest
                if self.is_sensitive(r):
                    r = self.want("key", r)
                out.append(r)
            return ";".join(out)
        return v

    def row(self, spec: dict[str, str], row: dict) -> dict:
        out = {}
        for col, value in row.items():
            v = _str(value)
            if col in spec:
                v = self.tx(spec[col], v)
            if self.is_sensitive(v):
                v = self.want("str", v)
            out[col] = _scrub(v)
        return out


def _scrub(v: str) -> str:
    """URLs and IP addresses replaced, as the script's sed step does."""
    v = _URL.sub("<url>", v)
    v = _IPV4.sub(lambda m: m.group(1) + "<ip>", v)
    return _IPV6.sub("<ip6>", v)


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") for c in columns])


README = """rancher-inventory {version} - discovery export for one cluster
Same files and columns as k8s-discovery.sh 1.0.0, built from the inventory database.

NOT included: IP addresses, node/host names, Secret or ConfigMap values, env vars,
commands, annotation or label VALUES, Ingress hostnames, NFS servers/paths, private
registry hostnames. Names are salted SHA-256 hashes; system namespaces stay readable.
{sensitive}
Review before sharing: images.csv (repository name + tag readable unless strict),
helm_releases.csv (chart names readable unless strict), crds.csv (API groups).
The name map is NOT in this bundle.
"""


def _clusters_in_scope(conn, scope: Scope) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT site, cluster_id FROM clusters WHERE last_ok_run IS NOT NULL ORDER BY site, name"
    ).fetchall()
    return [(r[0], r[1]) for r in rows if scope.includes(r[0], r[1])]


def build_export(conn, settings: Settings, opts: ExportOptions, out_dir: Path) -> Path:
    """Write the bundle into out_dir and return the tar.gz path. Raises ExportBlocked on a self-check hit."""
    if not settings.export_salt:
        raise ConfigError("EXPORT_SALT is not set; set it in .env and keep it (the same salt gives the same hashes)")
    rules = load_rules(settings)
    today = datetime.now(timezone.utc).date()
    anon = Anonymiser(settings.export_salt, settings.sensitive_strings, opts.strict, rules)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stage = Path(out_dir) / f"discovery-{stamp}"
    stage.mkdir(parents=True, exist_ok=False)
    fleet: list[dict] = []
    clusters = _clusters_in_scope(conn, opts.scope)
    if not clusters:
        raise ValueError("no collected clusters in scope")
    for site, cid in clusters:
        tables = build_tables(conn, site, cid, rules, today)
        anon_tables = {name: [anon.row(ANON_SPEC[name], r) for r in rows] for name, rows in tables.items()}
        folder = stage / (anon_tables["summary"][0]["cluster_id"] or "cluster-unknown")
        folder.mkdir()
        for name, rows in anon_tables.items():
            _write_csv(folder / f"{name}.csv", COLUMNS[name], rows)
        sens = f"{len(anon.sensitive)} sensitive string(s) were hashed wherever they appeared.\n" if anon.sensitive else ""
        (folder / "README.txt").write_text(README.format(version=__version__, sensitive=sens))
        fleet.extend(anon_tables["summary"])
    _write_csv(stage / "fleet_summary.csv", COLUMNS["summary"], fleet)

    hits = self_check(stage, settings.sensitive_strings, real_names(conn, settings))
    if hits:
        shutil.rmtree(stage)
        raise ExportBlocked(hits)
    sid = salt_id(settings.export_salt)
    with transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO export_name_map (type, value, hashed, salt_id) VALUES (?, ?, ?, ?)",
            [(t, v, h, sid) for (t, v), h in sorted(anon.used.items())],
        )
    tgz = Path(out_dir) / f"{stage.name}.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(stage, arcname=".")
    shutil.rmtree(stage)
    return tgz


# ---- self-check ----


def _hashed_columns(base: str) -> set[str]:
    spec = ANON_SPEC.get(base, {})
    return {c for c, t in spec.items() if t.startswith(("h:", "h+:")) or t in ("ns", "nsl")}


def _mask(v: str) -> str:
    return v[:2] + "*" * max(len(v) - 2, 1) if len(v) > 2 else "*" * len(v)


def self_check(bundle_dir: Path, sensitive: list[str], known_names: set[str]) -> list[SelfCheckHit]:
    """IPv4-like strings and sensitive strings anywhere; real names in the columns that must be hashed."""
    sens = [s.lower() for s in sensitive if s]
    known = {n.lower() for n in known_names if n}
    hits: list[SelfCheckHit] = []
    for path in sorted(Path(bundle_dir).rglob("*.csv")):
        rel = path.relative_to(bundle_dir).as_posix()
        name_cols = _hashed_columns(path.stem)
        with path.open(newline="") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            for row in reader:
                for col, value in zip(header, row):
                    lv = value.lower()
                    tokens = {x for x in re.split(r"[;,\s]+", lv) if x}
                    if (
                        SELF_CHECK_IPV4.search(value)
                        or any(s in lv for s in sens)
                        or (col in name_cols and tokens & known)
                    ):
                        hits.append(SelfCheckHit(rel, col, _mask(value)))
    return hits


def real_names(conn, settings: Settings) -> set[str]:
    """Real cluster, node, namespace and workload names, except readable system namespaces."""
    rules = load_rules(settings)
    names = {r[0] for r in conn.execute("SELECT name FROM clusters")}
    names |= {r[0] for r in conn.execute(
        "SELECT name FROM resources WHERE kind IN ('nodes', 'namespaces', 'deployments', 'statefulsets',"
        " 'daemonsets', 'jobs', 'cronjobs', 'replicationcontrollers')")}  # fmt: skip
    return {n for n in names if n and not is_system_namespace(n, rules)}


def name_map_csv(conn, settings: Settings) -> str:
    """The hash-to-name map of the current salt, for admins only. Never part of a bundle."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["hashed", "type", "value"])
    for r in conn.execute(
        "SELECT hashed, type, value FROM export_name_map WHERE salt_id=? ORDER BY type, value",
        (salt_id(settings.export_salt),),
    ):
        w.writerow(list(r))
    return buf.getvalue()
