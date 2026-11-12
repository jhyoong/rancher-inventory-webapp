import hashlib
import json
from dataclasses import astuple, dataclass, field

from inventory.collector.addresses import Address, extract_addresses
from inventory.collector.annotations import filter_annotations
from inventory.collector.kinds import KindSpec
from inventory.collector.netpolicy import (
    derive_hpa,
    derive_ingress,
    derive_ingressclass,
    derive_netpol,
    derive_pdb,
    derive_resourcequota,
    derive_service,
)
from inventory.collector.platform import (
    derive_apiservice,
    derive_binding,
    derive_clusterrole,
    derive_webhook,
    in_use_api_version,
    is_platform_role,
    is_rancher_generated,
)
from inventory.collector.podspec import derive_pod, derive_workload
from inventory.collector.podspec import skip_object as skip_job
from inventory.collector.storage import (
    derive_csidriver,
    derive_node_provider,
    derive_pv,
    derive_pvc,
    derive_storageclass,
)
from inventory.collector.rules import Rules


@dataclass
class Trimmed:
    kind: str
    namespace: str
    name: str
    created_at: str | None
    data: dict
    labels: dict[str, str]
    annotations: list[tuple[str, str, bool]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)
    in_use: tuple[str, str] | None = None  # apiVersion and kind from last-applied-configuration
    hash: str = ""


_MISSING = object()


def _get_path(obj: dict, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _images_from_pod_spec(spec) -> list[str]:
    if not isinstance(spec, dict):
        return []
    out = []
    for key in ("containers", "initContainers"):
        for c in spec.get(key) or []:
            if isinstance(c, dict) and c.get("image"):
                out.append(c["image"])
    return out


def _images(obj: dict) -> list[str]:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return []
    found = _images_from_pod_spec(spec)
    found += _images_from_pod_spec((spec.get("template") or {}).get("spec"))
    job_tpl = (spec.get("jobTemplate") or {}).get("spec") or {}
    found += _images_from_pod_spec((job_tpl.get("template") or {}).get("spec"))
    return sorted(set(found))


def _set_path(data: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _derive_node(obj: dict, data: dict) -> None:
    prefix = "node-role.kubernetes.io/"
    labels = (obj.get("metadata") or {}).get("labels") or {}
    data["roles"] = sorted(k[len(prefix):] for k in labels if k.startswith(prefix) and k[len(prefix):])
    data["provider"] = derive_node_provider((obj.get("spec") or {}).get("providerID") or "")
    conds = (obj.get("status") or {}).get("conditions") or []
    data["ready"] = next((c.get("status") for c in conds if isinstance(c, dict) and c.get("type") == "Ready"), None)
    data["taints"] = [
        f"{t.get('key')}:{t.get('effect')}" for t in (obj.get("spec") or {}).get("taints") or [] if isinstance(t, dict)
    ]


def _derive_statefulset(obj: dict, data: dict) -> None:
    out = []
    for t in (obj.get("spec") or {}).get("volumeClaimTemplates") or []:
        spec = t.get("spec") or {}
        out.append(
            {
                "name": (t.get("metadata") or {}).get("name"),
                "storage_class": spec.get("storageClassName"),
                "size": ((spec.get("resources") or {}).get("requests") or {}).get("storage"),
            }
        )
    data["volume_claim_templates"] = out


def _derive_pod(obj: dict, data: dict) -> None:
    data.update(derive_pod(obj))


def _drop_path(data: dict, path: str) -> None:
    *parents, last = path.split(".")
    cur = data
    for part in parents:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return
    cur.pop(last, None)


def _derive_crd(obj: dict, data: dict) -> None:
    versions = (obj.get("spec") or {}).get("versions")
    _drop_path(data, "spec.versions")
    if isinstance(versions, list):
        wanted = ("name", "served", "storage", "deprecated")
        _set_path(
            data,
            "spec.versions",
            [{k: v[k] for k in wanted if k in v} for v in versions if isinstance(v, dict)],
        )


def _derive_helmrelease(obj: dict, data: dict) -> None:
    resources = (obj.get("spec") or {}).get("resources")
    _drop_path(data, "spec.resources")
    if isinstance(resources, list):
        _set_path(
            data,
            "spec.resources",
            [{"apiVersion": r.get("apiVersion"), "kind": r.get("kind")} for r in resources if isinstance(r, dict)],
        )


_DERIVE = {
    "nodes": _derive_node,
    "statefulsets": _derive_statefulset,
    "pods": _derive_pod,
    "customresourcedefinitions": _derive_crd,
    "helmreleases": _derive_helmrelease,
}


# kind -> function(obj) -> dict of derived fields merged into data
_DERIVED = {
    "persistentvolumes": derive_pv,
    "storageclasses": derive_storageclass,
    "csidrivers": derive_csidriver,
    "services": derive_service,
    "ingresses": derive_ingress,
    "ingressclasses": derive_ingressclass,
    "poddisruptionbudgets": derive_pdb,
    "horizontalpodautoscalers": derive_hpa,
    "resourcequotas": derive_resourcequota,
    "networkpolicies": derive_netpol,
}


_RBAC_FLAGGED = ("clusterroles", "clusterrolebindings", "rolebindings",
                 "mutatingwebhookconfigurations", "validatingwebhookconfigurations")  # fmt: skip
_DERIVED.update({
    "mutatingwebhookconfigurations": derive_webhook,
    "validatingwebhookconfigurations": derive_webhook,
    "apiservices": lambda obj: derive_apiservice(obj) or {},
    "clusterroles": derive_clusterrole,
    "clusterrolebindings": derive_binding,
    "rolebindings": derive_binding,
})


def skip_object(kind: str, obj: dict) -> bool:
    """Objects that are not stored: CronJob-owned jobs and local (non-aggregated) APIServices."""
    if kind == "apiservices":
        return derive_apiservice(obj) is None
    return skip_job(kind, obj)


@dataclass
class ClusterContext:
    """Facts from kinds listed earlier in the same cluster run. None means not known."""

    pvc_users: dict[tuple[str, str], list[str]] | None = None


def trim(kind: KindSpec, obj: dict, rules: Rules | None = None, ctx: ClusterContext | None = None) -> Trimmed:
    """Reduce a Kubernetes object to the fields we store.

    Annotations need `rules` and a kind with `annotations: true`. Addresses are
    filled for kinds with `addresses: true` and for nodes. PVC `mounted` and
    `used_by` need `ctx.pvc_users` from the pods of the same cluster.
    """
    meta = obj.get("metadata") or {}
    data = {}
    if not kind.metadata_only:
        for path in kind.keep:
            value = _get_path(obj, path)
            if value is not _MISSING:
                _set_path(data, path, value)
        if kind.podspec:
            data.update(derive_workload(kind.name, obj))
        derive = _DERIVE.get(kind.name)
        if derive:
            derive(obj, data)
        if kind.name in _DERIVED:
            data.update(_DERIVED[kind.name](obj))
        if kind.name in _RBAC_FLAGGED and rules is not None:
            data["rancher_generated"] = is_rancher_generated(obj, rules)
        if kind.name == "clusterroles" and rules is not None:
            data["platform"] = is_platform_role(obj, rules)
        if kind.name == "persistentvolumeclaims":
            users = ctx.pvc_users if ctx else None
            data.update(derive_pvc(obj, set(users) if users is not None else None, users))
    labels = {str(k): str(v) for k, v in (meta.get("labels") or {}).items()}
    t = Trimmed(
        kind=kind.name,
        namespace=meta.get("namespace") or "",
        name=meta.get("name") or "",
        created_at=meta.get("creationTimestamp"),
        data=data,
        labels=labels,
        images=_images(obj),
        in_use=in_use_api_version(obj),
    )
    if kind.annotations and rules is not None:
        t.annotations = filter_annotations(meta.get("annotations") or {}, rules)
    if kind.addresses or kind.name == "nodes":
        t.addresses = extract_addresses(kind.name, obj)
    payload = [t.data, t.labels, t.annotations, t.images, [astuple(a) for a in t.addresses]]
    t.hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return t
