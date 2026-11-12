"""Platform kinds (spec 6.11): webhooks, APIServices, RBAC, the Rancher-generated filter and API versions."""

import json
from fnmatch import fnmatchcase

from inventory.collector.rules import Rules

LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _meta(obj: dict) -> dict:
    return obj.get("metadata") or {}


def _owner_groups(obj: dict) -> set[str]:
    return {
        str(ref.get("apiVersion") or "").split("/")[0]
        for ref in _meta(obj).get("ownerReferences") or []
        if isinstance(ref, dict)
    }


def _has_prefix(keys, prefixes: list[str]) -> bool:
    return any(k.startswith(p) for k in keys for p in prefixes)


def is_rancher_generated(obj: dict, rules: Rules) -> bool:
    """True for objects Rancher creates: matched by label or annotation prefix, owner API group or name."""
    rg = rules.rancher_generated
    meta = _meta(obj)
    return (
        _has_prefix((meta.get("labels") or {}).keys(), rg.get("label_prefixes", []))
        or _has_prefix((meta.get("annotations") or {}).keys(), rg.get("annotation_prefixes", []))
        or bool(_owner_groups(obj) & set(rg.get("owner_api_groups", [])))
        or any(fnmatchcase(meta.get("name") or "", p) for p in rg.get("name_patterns", []))
    )


def is_platform_role(obj: dict, rules: Rules) -> bool:
    """True for Kubernetes system roles and roles of platform components (rules.yaml platform_roles)."""
    from inventory.collector.classify import is_system_namespace

    pr = rules.platform_roles
    meta = _meta(obj)
    labels = meta.get("labels") or {}
    name = meta.get("name") or ""
    helm_ns = (meta.get("annotations") or {}).get("meta.helm.sh/release-namespace")
    return (
        any(fnmatchcase(name, p) for p in pr.get("names", []))
        or any(k in labels for k in pr.get("label_keys", []))
        or _has_prefix(labels.keys(), pr.get("label_prefixes", []))
        or bool(_owner_groups(obj) & set(pr.get("owner_api_groups", [])))
        or bool(pr.get("helm_in_system_namespace") and helm_ns and is_system_namespace(helm_ns, rules))
    )


def derive_webhook(obj: dict) -> dict:
    hooks = [w for w in obj.get("webhooks") or [] if isinstance(w, dict)]
    rules: list[str] = []
    intercepts_all = False
    for w in hooks:
        for r in w.get("rules") or []:
            groups, resources = r.get("apiGroups") or [], r.get("resources") or []
            rules.append(f"{' '.join(groups)}:{' '.join(resources)}")  # as the script prints them
            intercepts_all |= "*" in groups and "*" in resources
    cfg = [w.get("clientConfig") or {} for w in hooks]
    return {
        "webhooks": len(hooks),
        "failure_policies": [w.get("failurePolicy") for w in hooks],
        "service_namespaces": sorted({(c.get("service") or {}).get("namespace") for c in cfg if c.get("service")}),
        "url_based": any(c.get("url") for c in cfg),
        "rules": rules,
        "intercepts_all": intercepts_all,
        "timeouts": [w.get("timeoutSeconds") for w in hooks],
        "has_namespace_selector": any(w.get("namespaceSelector") for w in hooks),
        "helm_release": (_meta(obj).get("annotations") or {}).get("meta.helm.sh/release-name"),
    }


def derive_apiservice(obj: dict) -> dict | None:
    """None for a local (non-aggregated) APIService."""
    service = (obj.get("spec") or {}).get("service")
    if not service:
        return None
    conds = (obj.get("status") or {}).get("conditions") or []
    available = next((c.get("status") == "True" for c in conds if c.get("type") == "Available"), False)
    return {"service_namespace": service.get("namespace"), "available": available}


def derive_clusterrole(obj: dict) -> dict:
    return {
        "rule_count": len(obj.get("rules") or []),
        "aggregated": bool(obj.get("aggregationRule")),
        "helm_managed": bool((_meta(obj).get("annotations") or {}).get("meta.helm.sh/release-name")),
    }


def derive_binding(obj: dict) -> dict:
    ref = obj.get("roleRef") or {}
    return {
        "role": [ref.get("kind"), ref.get("name")],
        "subjects": [[s.get("kind"), s.get("name")] for s in obj.get("subjects") or [] if isinstance(s, dict)],
    }


def in_use_api_version(obj: dict) -> tuple[str, str] | None:
    """apiVersion and kind from the last-applied-configuration annotation; nothing else is read."""
    text = (_meta(obj).get("annotations") or {}).get(LAST_APPLIED)
    if not text:
        return None
    try:
        applied = json.loads(text)
    except ValueError:
        return None
    if not isinstance(applied, dict) or not applied.get("apiVersion") or not applied.get("kind"):
        return None
    return str(applied["apiVersion"]), str(applied["kind"])


async def collect_api_versions(client, cluster_id: str) -> list[str]:
    """Group versions served, from the /api and /apis discovery endpoints."""
    core = await client.get_json(f"/k8s/clusters/{cluster_id}/api")
    apis = await client.get_json(f"/k8s/clusters/{cluster_id}/apis")
    out = set(core.get("versions") or [])
    for g in apis.get("groups") or []:
        for v in g.get("versions") or []:
            if v.get("groupVersion"):
                out.add(v["groupVersion"])
    return sorted(out)
