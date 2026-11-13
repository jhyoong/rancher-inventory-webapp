"""Derived network, policy and scheduling fields (spec 6.10). No IPs, CIDRs or hostnames are added."""


def _ann(obj: dict) -> dict:
    return (obj.get("metadata") or {}).get("annotations") or {}


def derive_service(obj: dict) -> dict:
    spec = obj.get("spec") or {}
    return {
        "headless": spec.get("clusterIP") == "None",
        "has_selector": bool(spec.get("selector")),
        "node_ports": sum(1 for p in spec.get("ports") or [] if isinstance(p, dict) and p.get("nodePort")),
        "external_traffic_policy": spec.get("externalTrafficPolicy"),
        "lb_class": spec.get("loadBalancerClass"),
        "source_ranges": len(spec.get("loadBalancerSourceRanges") or []),
        "ip_families": list(spec.get("ipFamilies") or []),
        "session_affinity": spec.get("sessionAffinity"),
        "annotation_keys": sorted(_ann(obj)),
    }


def nginx_category(key: str) -> str:
    """Category of an nginx annotation key, as in k8s-discovery.sh."""
    k = key.lower()
    rules = (
        ("snippet", ("snippet",)),
        ("client-cert", ("auth-tls",)),
        ("auth", ("auth-",)),
        ("rewrite/regex", ("rewrite-target", "use-regex", "app-root", "x-forwarded-prefix")),
        ("canary", ("canary",)),
        ("rate-limit", ("limit-",)),
        ("affinity", ("affinity", "session-cookie")),
        ("cors", ("cors",)),
        ("backend-protocol", ("backend-protocol", "grpc", "secure-backends", "proxy-ssl")),
        ("ssl-redirect", ("ssl-redirect", "force-ssl", "ssl-passthrough")),
        ("ip-allow/deny", ("source-range",)),
        ("timeouts/buffers/body", ("proxy-",)),
        ("upstream/lb", ("upstream-", "load-balance", "service-upstream")),
        ("headers/redirect", ("custom-headers", "configuration", "server-alias", "redirect")),
        ("mirror", ("mirror",)),
    )
    for name, needles in rules:
        if any(n in k for n in needles):
            return name
    return "other"


def derive_ingress(obj: dict) -> dict:
    spec = obj.get("spec") or {}
    ann = _ann(obj)
    nginx = [k for k in ann if "nginx" in k]
    cats: dict[str, int] = {}
    for k in nginx:
        c = nginx_category(k)
        cats[c] = cats.get(c, 0) + 1
    return {
        "class": spec.get("ingressClassName") or ann.get("kubernetes.io/ingress.class"),
        "default_backend": bool(spec.get("defaultBackend")),
        "nginx_annotations": len(nginx),
        "nginx_categories": dict(sorted(cats.items())),
        "has_snippet": "snippet" in cats,
        "annotation_keys": sorted(ann),
    }


def derive_ingressclass(obj: dict) -> dict:
    return {"default": _ann(obj).get("ingressclass.kubernetes.io/is-default-class") == "true"}


def derive_pdb(obj: dict) -> dict:
    st = obj.get("status") or {}
    allowed, expected = st.get("disruptionsAllowed"), st.get("expectedPods") or 0
    return {"blocks_drain": allowed == 0 and expected > 0}


def derive_hpa(obj: dict) -> dict:
    metrics = (obj.get("spec") or {}).get("metrics") or []
    return {"metric_types": [m.get("type") for m in metrics if isinstance(m, dict)]}


def derive_resourcequota(obj: dict) -> dict:
    hard = (obj.get("spec") or {}).get("hard") or {}
    return {
        "quota_cpu": hard.get("requests.cpu") or hard.get("cpu"),
        "quota_mem": hard.get("requests.memory") or hard.get("memory"),
        "quota_storage": hard.get("requests.storage"),
    }


def derive_netpol(obj: dict) -> dict:
    spec = obj.get("spec") or {}
    peers = 0
    for rule in spec.get("ingress") or []:
        peers += sum(1 for p in (rule or {}).get("from") or [] if (p or {}).get("ipBlock"))
    for rule in spec.get("egress") or []:
        peers += sum(1 for p in (rule or {}).get("to") or [] if (p or {}).get("ipBlock"))
    return {
        "policy_types": list(spec.get("policyTypes") or []),
        "namespace_wide": not (spec.get("podSelector") or {}),
        "ipblock_peers": peers,
    }
