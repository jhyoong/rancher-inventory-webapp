"""Migration planning views computed at page time from the stored inventory."""

import json
import sqlite3

from inventory.collector.annotations import matches
from inventory.collector.rules import Rules

SERVICE_SIGNALS = ("pinned-ip", "local-traffic-policy", "source-ranges", "shared-ip", "vendor-annotations")

UNIDENTIFIED = "unidentified"
SEVERAL_LB = "several-lb-implementations"


def lb_implementation(lb_class: str | None, annotation_keys: list[str], rules: Rules) -> str:
    for name, impl in rules.network["lb_implementations"].items():
        if lb_class and lb_class in impl["classes"]:
            return name
        if any(matches(k, d) for k in annotation_keys for d in impl["annotation_domains"]):
            return name
    return UNIDENTIFIED


def service_signals(svc: dict, rules: Rules) -> list[str]:
    net = rules.network
    keys = svc.get("annotation_keys") or []
    found = {
        "pinned-ip": bool(svc.get("load_balancer_ip")) or any(k in net["pinned_ip_annotations"] for k in keys),
        "local-traffic-policy": svc.get("type") in ("LoadBalancer", "NodePort")
        and svc.get("external_traffic_policy") == "Local",
        "source-ranges": (svc.get("source_ranges") or 0) > 0,
        "shared-ip": any(k in net["shared_ip_annotations"] for k in keys),
        "vendor-annotations": any(matches(k, d) for k in keys for d in net["vendor_annotation_domains"]),
    }
    return [s for s in SERVICE_SIGNALS if found[s]]


def _scope(site: str | None, cluster_id: str | None, prefix: str) -> tuple[str, list]:
    parts, args = [], []
    if site:
        parts.append(f"{prefix}.site = ?")
        args.append(site)
    if cluster_id:
        parts.append(f"{prefix}.cluster_id = ?")
        args.append(cluster_id)
    return "".join(f" AND {p}" for p in parts), args


def lb_services(
    conn: sqlite3.Connection, rules: Rules, site: str | None = None, cluster_id: str | None = None
) -> list[dict]:
    where, args = _scope(site, cluster_id, "r")
    rows = conn.execute(
        f"""SELECT r.id, r.site, r.cluster_id, c.name AS cluster, r.namespace, r.name,
                   json_extract(r.data, '$.spec.type') AS type,
                   json_extract(r.data, '$.lb_class') AS lb_class,
                   json_extract(r.data, '$.annotation_keys') AS annotation_keys,
                   json_extract(r.data, '$.external_traffic_policy') AS external_traffic_policy,
                   json_extract(r.data, '$.spec.loadBalancerIP') AS load_balancer_ip,
                   json_extract(r.data, '$.source_ranges') AS source_ranges
            FROM resources r
            LEFT JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
            WHERE r.kind = 'services' AND json_extract(r.data, '$.spec.type') = 'LoadBalancer'{where}
            ORDER BY r.site, c.name, r.namespace, r.name""",
        args,
    ).fetchall()
    out = []
    for r in rows:
        svc = dict(r)
        svc["annotation_keys"] = json.loads(svc["annotation_keys"] or "[]")
        addresses = {
            a[0] for a in conn.execute("SELECT address FROM addresses WHERE resource_id = ? AND role = 'lb'", (r["id"],))
        }
        if svc["load_balancer_ip"]:
            addresses.add(svc["load_balancer_ip"])
        out.append(
            {
                "site": r["site"], "cluster_id": r["cluster_id"], "cluster": r["cluster"],
                "namespace": r["namespace"], "name": r["name"],
                "addresses": sorted(addresses),
                "implementation": lb_implementation(svc["lb_class"], svc["annotation_keys"], rules),
                "signals": service_signals(svc, rules),
            }
        )  # fmt: skip
    return out


def floating_ips(
    conn: sqlite3.Connection, rules: Rules, site: str | None = None, cluster_id: str | None = None
) -> dict[tuple[str, str], dict]:
    """Per cluster: addresses that need a floating IP after migration, by source."""
    where, args = _scope(site, cluster_id, "c")
    r_where, r_args = _scope(site, cluster_id, "r")
    result = {}
    for c in conn.execute(
        f"SELECT site, cluster_id, api_endpoint FROM clusters c WHERE 1=1{where} ORDER BY site, cluster_id", args
    ):
        result[(c["site"], c["cluster_id"])] = {"control_plane": c["api_endpoint"], "_sources": {}}
    for r in conn.execute(
        f"""SELECT r.site, r.cluster_id, a.role, a.address
            FROM addresses a JOIN resources r ON r.id = a.resource_id
            WHERE a.role IN ('lb', 'ingress-lb', 'external-ip', 'node-external'){r_where}""",
        r_args,
    ):
        entry = result.get((r["site"], r["cluster_id"]))
        if entry is not None:
            entry["_sources"].setdefault(r["role"], set()).add(r["address"])

    services: dict[tuple[str, str], list[dict]] = {}
    for s in lb_services(conn, rules, site, cluster_id):
        services.setdefault((s["site"], s["cluster_id"]), []).append(s)

    for key, entry in result.items():
        src = entry.pop("_sources")
        # status.loadBalancer.ingress (stored as lb addresses) plus pinned spec.loadBalancerIP
        lb = src.get("lb", set()) | {a for s in services.get(key, []) for a in s["addresses"]}
        ingress = src.get("ingress-lb", set()) - lb
        external = src.get("external-ip", set())
        node = src.get("node-external", set())
        addresses = sorted(lb | ingress | external | node)
        svcs = services.get(key, [])
        implementations = sorted({s["implementation"] for s in svcs})
        special = {sig for s in svcs for sig in s["signals"]}
        if len([i for i in implementations if i != UNIDENTIFIED]) > 1:
            special.add(SEVERAL_LB)
        entry.update(
            lb=len(lb), external_ip=len(external), ingress_lb=len(ingress), node_external=len(node),
            addresses=addresses,
            total=len(addresses) + (1 if entry["control_plane"] else 0),
            implementations=implementations,
            special=sorted(special),
        )  # fmt: skip
    return result
