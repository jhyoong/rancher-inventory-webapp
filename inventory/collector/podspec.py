"""Derived fields from pod specs (spec 6.9). Env, commands, args, paths and servers are never kept."""

from inventory.collector.units import parse_bytes, parse_cpu

GPU_KEYS = ("nvidia.com/gpu", "amd.com/gpu")


def _quantity(parse, value) -> int:
    try:
        return parse(value)
    except (ArithmeticError, ValueError, TypeError):
        return 0


def _sum(containers: list, section: str, key: str, parse) -> int:
    total = 0
    for c in containers:
        value = (((c or {}).get("resources") or {}).get(section) or {}).get(key)
        if value:
            total += _quantity(parse, value)
    return total


def _gpu(containers: list) -> int:
    return sum(_sum(containers, "limits", k, lambda v: int(_quantity(parse_cpu, v) / 1000)) for k in GPU_KEYS)


def _claims(volumes: list) -> list[str]:
    return sorted({
        v["persistentVolumeClaim"]["claimName"]
        for v in volumes
        if isinstance(v, dict) and (v.get("persistentVolumeClaim") or {}).get("claimName")
    })  # fmt: skip


def pod_template_spec(kind: str, obj: dict) -> dict:
    """The pod spec inside a workload object."""
    spec = obj.get("spec") or {}
    if kind == "cronjobs":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def derive_podspec(pod_spec: dict) -> dict:
    cs = [c for c in pod_spec.get("containers") or [] if isinstance(c, dict)]
    volumes = [v for v in pod_spec.get("volumes") or [] if isinstance(v, dict)]
    caps: set[str] = set()
    privileged = host_ports = 0
    for c in cs:
        sc = c.get("securityContext") or {}
        if sc.get("privileged") is True:
            privileged += 1
        caps.update(str(x) for x in ((sc.get("capabilities") or {}).get("add") or []))
        host_ports += sum(1 for p in c.get("ports") or [] if isinstance(p, dict) and p.get("hostPort"))
    sa = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
    return {
        "containers": len(cs),
        "init_containers": len(pod_spec.get("initContainers") or []),
        "cpu_req_m": _sum(cs, "requests", "cpu", parse_cpu),
        "cpu_lim_m": _sum(cs, "limits", "cpu", parse_cpu),
        "mem_req_b": _sum(cs, "requests", "memory", parse_bytes),
        "mem_lim_b": _sum(cs, "limits", "memory", parse_bytes),
        "gpu": _gpu(cs),
        "no_requests": sum(1 for c in cs if not (c.get("resources") or {}).get("requests")),
        "no_limits": sum(1 for c in cs if not (c.get("resources") or {}).get("limits")),
        "no_probes": sum(1 for c in cs if not (c.get("readinessProbe") or c.get("livenessProbe"))),
        "privileged": privileged,  # number of privileged containers, as the script counts them
        "host_network": bool(pod_spec.get("hostNetwork")),
        "host_pid": bool(pod_spec.get("hostPID")),
        "host_ports": host_ports,
        "added_caps": sorted(caps),
        "pvc_claims": _claims(volumes),
        "hostpath": sum(1 for v in volumes if "hostPath" in v),
        "nfs": sum(1 for v in volumes if "nfs" in v),
        "emptydir": sum(1 for v in volumes if "emptyDir" in v),
        "csi_drivers": sorted({(v.get("csi") or {}).get("driver") for v in volumes if (v.get("csi") or {}).get("driver")}),
        "node_selector_keys": sorted(pod_spec.get("nodeSelector") or {}),
        "affinity": bool(pod_spec.get("affinity")),
        "topology_spread": bool(pod_spec.get("topologySpreadConstraints")),
        "toleration_keys": sorted({t["key"] for t in pod_spec.get("tolerations") or [] if isinstance(t, dict) and t.get("key")}),
        "priority_class": pod_spec.get("priorityClassName") or None,
        "service_account": sa if sa and sa != "default" else None,
    }


def _owner(obj: dict) -> dict:
    refs = (obj.get("metadata") or {}).get("ownerReferences") or []
    return refs[0] if refs and isinstance(refs[0], dict) else {}


def derive_workload(kind: str, obj: dict) -> dict:
    """Pod spec fields plus owner, update strategy and, for cronjobs, schedule and suspend."""
    spec = obj.get("spec") or {}
    owner = _owner(obj)
    out = derive_podspec(pod_template_spec(kind, obj))
    out["owner_api_version"] = owner.get("apiVersion")
    out["owner_kind"] = owner.get("kind")
    out["update_strategy"] = (spec.get("strategy") or spec.get("updateStrategy") or {}).get("type")
    if kind == "cronjobs":
        out["schedule"] = spec.get("schedule")
        out["suspended"] = bool(spec.get("suspend"))
    return out


def derive_pod(pod: dict) -> dict:
    spec = pod.get("spec") or {}
    cs = [c for c in spec.get("containers") or [] if isinstance(c, dict)]
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return {
        "qos": (pod.get("status") or {}).get("qosClass"),
        "owner_kind": _owner(pod).get("kind"),
        "restarts": sum(int(s.get("restartCount") or 0) for s in statuses if isinstance(s, dict)),
        "cpu_req_m": _sum(cs, "requests", "cpu", parse_cpu),
        "mem_req_b": _sum(cs, "requests", "memory", parse_bytes),
        "cpu_lim_m": _sum(cs, "limits", "cpu", parse_cpu),
        "mem_lim_b": _sum(cs, "limits", "memory", parse_bytes),
        "gpu": _gpu(cs),
        "pvc_claims": _claims([v for v in spec.get("volumes") or [] if isinstance(v, dict)]),
    }


def skip_object(kind: str, obj: dict) -> bool:
    """Jobs owned by a CronJob are skipped, as in k8s-discovery.sh."""
    return kind == "jobs" and _owner(obj).get("kind") == "CronJob"
