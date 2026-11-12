from dataclasses import dataclass


@dataclass(frozen=True)
class Address:
    role: str
    address: str
    ports: str | None = None


def _lb_ingress(obj: dict) -> list[str]:
    items = (((obj.get("status") or {}).get("loadBalancer") or {}).get("ingress")) or []
    return [i.get("ip") or i.get("hostname") for i in items if isinstance(i, dict) and (i.get("ip") or i.get("hostname"))]


def _ports(spec: dict) -> str | None:
    parts = [f"{p['port']}/{p.get('protocol') or 'TCP'}" for p in spec.get("ports") or [] if isinstance(p, dict) and p.get("port")]
    return ",".join(parts) or None


def extract_addresses(kind: str, obj: dict) -> list[Address]:
    """IPs and DNS names of a node, service or ingress (spec 6.4), deduplicated."""
    spec = obj.get("spec") or {}
    found: list[Address] = []
    if kind == "nodes":
        roles = {"InternalIP": "node-internal", "ExternalIP": "node-external"}
        for a in (obj.get("status") or {}).get("addresses") or []:
            role = roles.get((a or {}).get("type"))
            if role and a.get("address"):
                found.append(Address(role, a["address"]))
    elif kind == "services":
        ips = spec.get("clusterIPs") or ([spec["clusterIP"]] if spec.get("clusterIP") else [])
        found += [Address("cluster-ip", ip) for ip in ips if ip and ip != "None"]
        found += [Address("external-ip", ip) for ip in spec.get("externalIPs") or [] if ip]
        if spec.get("type") == "LoadBalancer":
            ports = _ports(spec)
            found += [Address("lb", a, ports) for a in _lb_ingress(obj)]
    elif kind == "ingresses":
        found += [Address("ingress-lb", a) for a in _lb_ingress(obj)]
        hosts = [r.get("host") for r in spec.get("rules") or [] if isinstance(r, dict)]
        for t in spec.get("tls") or []:
            if isinstance(t, dict):
                hosts += t.get("hosts") or []
        found += [Address("ingress-host", h) for h in hosts if h]
    unique: dict[tuple[str, str], Address] = {}
    for a in found:
        unique.setdefault((a.role, a.address), a)
    return list(unique.values())
