"""Migration planning views computed at page time from the stored inventory."""

import json
import sqlite3
from fnmatch import fnmatchcase

from inventory.collector.annotations import matches
from inventory.collector.classify import is_system_namespace
from inventory.collector.rollup import COMPLEXITY_FLAGS
from inventory.collector.rules import Rules
from inventory.collector.units import parse_bytes
from inventory.web.queries import FLAG_COLUMNS, inventory_rows

SERVICE_SIGNALS = ("pinned-ip", "local-traffic-policy", "source-ranges", "shared-ip", "vendor-annotations")

DATABASE_KINDS = ("deployments", "statefulsets", "daemonsets", "cronjobs", "jobs")

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


def image_repo_name(image: str) -> str:
    """Last path segment of the repository, without tag or digest."""
    repo = image.split("@", 1)[0]
    last = repo.rsplit("/", 1)[-1]
    return last.split(":", 1)[0]


def image_engine(image: str, rules: Rules) -> str | None:
    name = image_repo_name(image)
    db = rules.databases
    if any(fnmatchcase(name, p) for p in db["exclude"]):
        return None
    for engine, patterns in db["images"].items():
        if any(fnmatchcase(name, p) for p in patterns):
            return engine
    return None


def databases(
    conn: sqlite3.Connection, rules: Rules, site: str | None = None, cluster_id: str | None = None
) -> dict[tuple[str, str], list[dict]]:
    """Per cluster: databases found in user-namespace workload images and operator CRD groups."""
    where, args = _scope(site, cluster_id, "c")
    r_where, r_args = _scope(site, cluster_id, "r")
    result = {
        (c["site"], c["cluster_id"]): []
        for c in conn.execute(f"SELECT site, cluster_id FROM clusters c WHERE 1=1{where} ORDER BY site, cluster_id", args)
    }
    kinds = ", ".join("?" for _ in DATABASE_KINDS)
    seen = set()
    for r in conn.execute(
        f"""SELECT r.site, r.cluster_id, r.namespace, r.kind, r.name, i.image
            FROM resources r
            JOIN resource_images i ON i.resource_id = r.id
            JOIN namespace_profile n ON n.site = r.site AND n.cluster_id = r.cluster_id AND n.namespace = r.namespace
            WHERE r.kind IN ({kinds}) AND n.is_system = 0{r_where}
            ORDER BY r.site, r.cluster_id, r.namespace, r.kind, r.name, i.image""",
        [*DATABASE_KINDS, *r_args],
    ):
        hits = result.get((r["site"], r["cluster_id"]))
        engine = image_engine(r["image"], rules)
        key = (r["site"], r["cluster_id"], engine, r["namespace"], r["kind"], r["name"])
        if hits is None or engine is None or key in seen:
            continue
        seen.add(key)
        hits.append(
            {"engine": engine, "namespace": r["namespace"], "kind": r["kind"], "name": r["name"], "image": r["image"]}
        )

    group_engine = {g: e for e, groups in rules.databases["operator_crd_groups"].items() for g in groups}
    for r in conn.execute(
        f"""SELECT DISTINCT r.site, r.cluster_id, json_extract(r.data, '$.spec.group') AS grp
            FROM resources r WHERE r.kind = 'customresourcedefinitions'{r_where}
            ORDER BY r.site, r.cluster_id, grp""",
        r_args,
    ):
        hits = result.get((r["site"], r["cluster_id"]))
        if hits is not None and r["grp"] in group_engine:
            hits.append({"engine": group_engine[r["grp"]], "crd_group": r["grp"]})
    return result


# ---- Watch out flags and cluster rows ----

WATCH_OUT_FLAGS = (
    "node-pinned-pv", "database", "external-ip", "nodeport", "host-network",
    "custom-webhooks", "special-network-config", "not-collected",
)  # fmt: skip

ALL_FLAGS: tuple[str, ...] = tuple(dict.fromkeys((*FLAG_COLUMNS, *COMPLEXITY_FLAGS, *WATCH_OUT_FLAGS)))

WORKLOAD_KINDS = (*DATABASE_KINDS, "replicationcontrollers")
WEBHOOK_KINDS = ("mutatingwebhookconfigurations", "validatingwebhookconfigurations")
FLOATING_SOURCES = ("lb", "external_ip", "ingress_lb", "node_external", "control_plane")

_USER_NS = (
    "JOIN namespace_profile n ON n.site = r.site AND n.cluster_id = r.cluster_id"
    " AND n.namespace = r.namespace AND n.is_system = 0"
)


def _clusters_with(conn: sqlite3.Connection, sql: str, args: list) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in conn.execute(sql, args)}


def _custom_webhook(data: dict, rules: Rules) -> bool:
    if data.get("rancher_generated"):
        return False
    namespaces = data.get("service_namespaces") or []
    return bool(data.get("url_based")) or any(ns and not is_system_namespace(ns, rules) for ns in namespaces)


def watch_out(conn: sqlite3.Connection, rules: Rules, site: str | None = None) -> dict[tuple[str, str], list[str]]:
    """Per cluster: WATCH_OUT_FLAGS that apply, in that order, then the stored complexity flags."""
    return _watch_out(conn, rules, site, floating_ips(conn, rules, site), databases(conn, rules, site))


def _watch_out(
    conn: sqlite3.Connection, rules: Rules, site: str | None, floating: dict, dbs: dict
) -> dict[tuple[str, str], list[str]]:
    where, args = _scope(site, None, "c")
    r_where, r_args = _scope(site, None, "r")
    stored = {
        (c["site"], c["cluster_id"]): json.loads(c["complexity_flags"] or "[]")
        for c in conn.execute(
            f"""SELECT c.site, c.cluster_id, p.complexity_flags FROM clusters c
                LEFT JOIN cluster_profile p ON p.site = c.site AND p.cluster_id = c.cluster_id
                WHERE 1=1{where} ORDER BY c.site, c.cluster_id""",
            args,
        )
    }
    base = "SELECT DISTINCT r.site, r.cluster_id FROM resources r"
    workloads = ", ".join("?" for _ in WORKLOAD_KINDS)
    webhooks = {
        (r["site"], r["cluster_id"])
        for r in conn.execute(
            f"SELECT r.site, r.cluster_id, r.data FROM resources r WHERE r.kind IN (?, ?){r_where}",
            [*WEBHOOK_KINDS, *r_args],
        )
        if _custom_webhook(json.loads(r["data"]), rules)
    }
    found = {
        "node-pinned-pv": _clusters_with(
            conn,
            f"{base} WHERE r.kind = 'persistentvolumes'"
            f" AND (json_extract(r.data, '$.local') = 1 OR json_extract(r.data, '$.pinned') = 1){r_where}",
            r_args,
        ),
        "database": {k for k, hits in dbs.items() if hits},
        # LB, externalIPs, ingress LB and node external addresses; the control plane endpoint does not count
        "external-ip": {k for k, f in floating.items() if f["addresses"]},
        "nodeport": _clusters_with(
            conn,
            f"{base} {_USER_NS} WHERE r.kind = 'services'"
            f" AND json_extract(r.data, '$.spec.type') = 'NodePort'{r_where}",
            r_args,
        ),
        "host-network": _clusters_with(
            conn,
            f"{base} {_USER_NS} WHERE r.kind IN ({workloads})"
            f" AND (json_extract(r.data, '$.host_network') = 1 OR json_extract(r.data, '$.host_ports') > 0){r_where}",
            [*WORKLOAD_KINDS, *r_args],
        ),
        "custom-webhooks": webhooks,
        "special-network-config": {k for k, f in floating.items() if f["special"]},
        # only a base kind that errored in the cluster's latest run; forbidden and not_found do not count
        "not-collected": _clusters_with(
            conn,
            f"""SELECT DISTINCT r.site, r.cluster_id FROM run_kinds r WHERE r.status = 'failed'
                AND r.run_id = (SELECT max(k.run_id) FROM run_kinds k
                                WHERE k.site = r.site AND k.cluster_id = r.cluster_id){r_where}""",
            r_args,
        ),
    }  # fmt: skip
    return {key: [f for f in WATCH_OUT_FLAGS if key in found[f]] + flags for key, flags in stored.items()}


def label_matches(conn: sqlite3.Connection, label: str) -> dict[tuple[str, str], list[str]]:
    """Clusters whose cluster, node or machine pool labels match `key` or `key=value`, with what matched."""
    key, sep, value = label.partition("=")

    def cond(prefix: str) -> tuple[str, list]:
        if sep:
            return f"{prefix}.key = ? AND {prefix}.value = ?", [key, value]
        return f"{prefix}.key = ?", [key]

    # malformed labels JSON counts as no labels
    sources = [
        (
            "SELECT c.site, c.cluster_id, NULL FROM clusters c,"
            " json_each(CASE WHEN json_valid(c.labels) THEN c.labels ELSE '{}' END) j WHERE {cond}"
            " ORDER BY c.site, c.cluster_id",
            "j", lambda name: "cluster",
        ),
        (
            "SELECT r.site, r.cluster_id, r.name FROM resources r JOIN resource_labels l ON l.resource_id = r.id"
            " WHERE r.kind = 'nodes' AND {cond} ORDER BY r.site, r.cluster_id, r.name",
            "l", lambda name: f"node {name}",
        ),
        (
            "SELECT p.site, p.cluster_id, p.name FROM machine_pools p,"
            " json_each(CASE WHEN json_valid(p.labels) THEN p.labels ELSE '{}' END) j WHERE {cond}"
            " ORDER BY p.site, p.cluster_id, p.name",
            "j", lambda name: f"pool {name}",
        ),
    ]  # fmt: skip
    out: dict[tuple[str, str], list[str]] = {}
    for sql, prefix, what in sources:
        where, args = cond(prefix)
        for r in conn.execute(sql.replace("{cond}", where), args):
            out.setdefault((r[0], r[1]), []).append(what(r[2]))
    return out


def cluster_rows(
    conn: sqlite3.Connection,
    rules: Rules,
    site: str | None = None,
    cls: str | None = None,
    flag: str | None = None,
    label: str | None = None,
) -> list[dict]:
    """Inventory rows plus pools, OS, usage, PVs, floating IPs, databases and Watch out flags per cluster."""
    if flag and flag not in ALL_FLAGS:
        raise ValueError(f"unknown flag: {flag}")
    stored_flag = flag if flag in FLAG_COLUMNS or flag in COMPLEXITY_FLAGS else None
    rows = inventory_rows(conn, site=site, cls=cls, flag=stored_flag)
    floating = floating_ips(conn, rules, site)
    dbs = databases(conn, rules, site)
    flags = _watch_out(conn, rules, site, floating, dbs)
    if flag and not stored_flag:
        rows = [r for r in rows if flag in flags.get((r["site"], r["cluster_id"]), [])]
    hits = label_matches(conn, label) if label else {}
    if label:
        rows = [r for r in rows if (r["site"], r["cluster_id"]) in hits]
    if not rows:
        return []

    where, args = _scope(site, None, "c")
    r_where, r_args = _scope(site, None, "r")
    p_where, p_args = _scope(site, None, "p")
    extra = {
        (c["site"], c["cluster_id"]): dict(c)
        for c in conn.execute(
            f"""SELECT c.site, c.cluster_id, c.state, c.provider, c.last_error, c.pools_status,
                       m.cpu_alloc_m, m.cpu_req_m, m.cpu_used_m, m.mem_alloc_b, m.mem_req_b, m.mem_used_b
                FROM clusters c
                LEFT JOIN cluster_metrics_latest m ON m.site = c.site AND m.cluster_id = c.cluster_id
                WHERE 1=1{where}""",
            args,
        )
    }
    pools: dict[tuple[str, str], list[str]] = {}
    for p in conn.execute(
        f"SELECT p.site, p.cluster_id, p.name FROM machine_pools p WHERE 1=1{p_where} ORDER BY p.name", p_args
    ):
        pools.setdefault((p["site"], p["cluster_id"]), []).append(p["name"])
    os_images: dict[tuple[str, str], list[str]] = {}
    for n in conn.execute(
        f"""SELECT DISTINCT r.site, r.cluster_id, json_extract(r.data, '$.status.nodeInfo.osImage') AS os
            FROM resources r WHERE r.kind = 'nodes'
            AND json_extract(r.data, '$.status.nodeInfo.osImage') IS NOT NULL{r_where} ORDER BY os""",
        r_args,
    ):
        os_images.setdefault((n["site"], n["cluster_id"]), []).append(n["os"])
    pvs: dict[tuple[str, str], list[int]] = {}
    for v in conn.execute(
        f"""SELECT r.site, r.cluster_id, json_extract(r.data, '$.spec.capacity.storage') AS size
            FROM resources r WHERE r.kind = 'persistentvolumes'{r_where}""",
        r_args,
    ):
        count_bytes = pvs.setdefault((v["site"], v["cluster_id"]), [0, 0])
        count_bytes[0] += 1
        count_bytes[1] += parse_bytes(v["size"]) if v["size"] else 0

    for row in rows:
        key = (row["site"], row["cluster_id"])
        e = extra[key]
        pv_count, pv_bytes = pvs.get(key, [0, 0])
        row.update(
            state=e["state"], provider=e["provider"], last_error=e["last_error"],
            pools_status=e["pools_status"],
            pools=pools.get(key, []),
            os_images=os_images.get(key) or ["not collected"],
            cpu_alloc_m=e["cpu_alloc_m"], cpu_req_m=e["cpu_req_m"], cpu_used_m=e["cpu_used_m"],
            mem_alloc_b=e["mem_alloc_b"], mem_req_b=e["mem_req_b"], mem_used_b=e["mem_used_b"],
            pv_count=pv_count, pv_bytes=pv_bytes,
            floating=floating[key],
            databases=sorted({h["engine"] for h in dbs.get(key, [])}),
            flags=flags.get(key, []),
            label_hits=hits.get(key, []),
        )  # fmt: skip
    return rows


def flat_row(row: dict) -> dict:
    """One CSV row: lists joined with "; ", floating IPs as a total plus one column per source."""
    out = {}
    for k, v in row.items():
        if k == "floating":
            out["floating_total"] = v["total"]
            for s in FLOATING_SOURCES:
                out[f"floating_{s}"] = (1 if v[s] else 0) if s == "control_plane" else v[s]
            out["lb_implementations"] = "; ".join(v["implementations"])
        elif isinstance(v, list):
            out[k] = "; ".join(str(x) for x in v)
        else:
            out[k] = v
    return out
