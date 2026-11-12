import hashlib
import json
from dataclasses import astuple, dataclass, field

from inventory.collector.addresses import Address
from inventory.collector.kinds import KindSpec
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


def trim(kind: KindSpec, obj: dict, rules: Rules | None = None) -> Trimmed:
    """Reduce a Kubernetes object to the fields we store.

    `rules` is accepted for later tasks; annotations and addresses stay empty here.
    """
    meta = obj.get("metadata") or {}
    data = {}
    for path in kind.keep:
        value = _get_path(obj, path)
        if value is not _MISSING:
            data[path] = value
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
    payload = [t.data, t.labels, t.annotations, t.images, [astuple(a) for a in t.addresses]]
    t.hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return t
