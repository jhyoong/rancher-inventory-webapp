import hashlib
import json
from dataclasses import astuple, dataclass, field

from inventory.collector.addresses import Address, extract_addresses
from inventory.collector.annotations import filter_annotations
from inventory.collector.kinds import KindSpec
from inventory.collector.rules import Rules
from inventory.collector.units import parse_bytes, parse_cpu


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


def _pod_spec(obj: dict) -> dict:
    return ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}


def _derive_node(obj: dict, data: dict) -> None:
    prefix = "node-role.kubernetes.io/"
    labels = (obj.get("metadata") or {}).get("labels") or {}
    data["roles"] = sorted(k[len(prefix):] for k in labels if k.startswith(prefix) and k[len(prefix):])


def _derive_workload(obj: dict, data: dict) -> None:
    volumes = _pod_spec(obj).get("volumes") or []
    claims = []
    for v in volumes:
        name = ((v or {}).get("persistentVolumeClaim") or {}).get("claimName")
        if name:
            claims.append(name)
    data["pvc_claims"] = sorted(set(claims))
    data["hostpath"] = any("hostPath" in (v or {}) for v in volumes)


def _derive_statefulset(obj: dict, data: dict) -> None:
    _derive_workload(obj, data)
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


def _derive_pv(obj: dict, data: dict) -> None:
    spec = obj.get("spec") or {}
    data["local"] = bool(spec.get("local") or spec.get("hostPath"))


def _quantity(parse, value) -> int | None:
    """Parse a quantity, or None when it cannot be read."""
    try:
        return parse(value)
    except (ArithmeticError, ValueError, TypeError):
        return None


def _derive_pod(obj: dict, data: dict) -> None:
    cpu = mem = 0
    for c in (obj.get("spec") or {}).get("containers") or []:
        req = ((c or {}).get("resources") or {}).get("requests") or {}
        if req.get("cpu"):
            cpu += _quantity(parse_cpu, req["cpu"]) or 0
        if req.get("memory"):
            mem += _quantity(parse_bytes, req["memory"]) or 0
    data["cpu_req_m"] = cpu
    data["mem_req_b"] = mem


def _derive_crd(obj: dict, data: dict) -> None:
    versions = (obj.get("spec") or {}).get("versions")
    if isinstance(versions, list):
        wanted = ("name", "served", "storage", "deprecated")
        _set_path(
            data,
            "spec.versions",
            [{k: v[k] for k in wanted if k in v} for v in versions if isinstance(v, dict)],
        )


def _derive_helmrelease(obj: dict, data: dict) -> None:
    resources = (obj.get("spec") or {}).get("resources")
    if isinstance(resources, list):
        _set_path(
            data,
            "spec.resources",
            [{"apiVersion": r.get("apiVersion"), "kind": r.get("kind")} for r in resources if isinstance(r, dict)],
        )


_DERIVE = {
    "nodes": _derive_node,
    "deployments": _derive_workload,
    "daemonsets": _derive_workload,
    "statefulsets": _derive_statefulset,
    "persistentvolumes": _derive_pv,
    "pods": _derive_pod,
    "customresourcedefinitions": _derive_crd,
    "helmreleases": _derive_helmrelease,
}


def trim(kind: KindSpec, obj: dict, rules: Rules | None = None) -> Trimmed:
    """Reduce a Kubernetes object to the fields we store.

    Annotations need `rules` and a kind with `annotations: true`. Addresses are
    filled for kinds with `addresses: true` and for nodes.
    """
    meta = obj.get("metadata") or {}
    data = {}
    if not kind.metadata_only:
        for path in kind.keep:
            value = _get_path(obj, path)
            if value is not _MISSING:
                _set_path(data, path, value)
        derive = _DERIVE.get(kind.name)
        if derive:
            derive(obj, data)
    labels = {str(k): str(v) for k, v in (meta.get("labels") or {}).items()}
    t = Trimmed(
        kind=kind.name,
        namespace=meta.get("namespace") or "",
        name=meta.get("name") or "",
        created_at=meta.get("creationTimestamp"),
        data=data,
        labels=labels,
        images=_images(obj),
    )
    if kind.annotations and rules is not None:
        t.annotations = filter_annotations(meta.get("annotations") or {}, rules)
    if kind.addresses or kind.name == "nodes":
        t.addresses = extract_addresses(kind.name, obj)
    payload = [t.data, t.labels, t.annotations, t.images, [astuple(a) for a in t.addresses]]
    t.hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return t
