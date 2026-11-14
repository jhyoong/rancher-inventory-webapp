"""Migration planning views computed at page time from the stored inventory."""

import json
import sqlite3
from fnmatch import fnmatchcase
from urllib.parse import quote

from inventory.collector.annotations import matches
from inventory.collector.classify import is_system_namespace
from inventory.collector.rollup import COMPLEXITY_FLAGS
from inventory.collector.rules import Rules
from inventory.collector.units import parse_bytes, parse_cpu
from inventory.web import queries, tracking
from inventory.web.queries import FLAG_COLUMNS, inventory_rows

SERVICE_SIGNALS = ("pinned-ip", "local-traffic-policy", "source-ranges", "shared-ip", "vendor-annotations")

DATABASE_KINDS = ("deployments", "statefulsets", "daemonsets", "cronjobs", "jobs")

UNIDENTIFIED = "unidentified"
NOT_COLLECTED = "not collected"
SEVERAL_LB = "several-lb-implementations"

CLASS_ORDER = ("empty", "stateless", "stateful", "stateful-helm", "stateful-crd", "stateful-helm-crd", NOT_COLLECTED)
TOP_N = 10


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
        # each source excludes addresses already counted, so the sources add up to the address count
        ingress = src.get("ingress-lb", set()) - lb
        external = src.get("external-ip", set()) - lb - ingress
        node = src.get("node-external", set()) - lb - ingress - external
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

# Watch out flag -> id of the cluster page section with the rows behind it
FLAG_ANCHORS = {
    "node-pinned-pv": "pvs", "database": "databases", "external-ip": "external-networking",
    "nodeport": "nodeport-services", "host-network": "namespaces", "custom-webhooks": "webhooks",
    "special-network-config": "lb-services", "not-collected": "summary",
    "k8s<1.32": "summary", "stateful": "databases", "rwx": "pvcs", "nginx-snippets": "ingresses",
    "many-crds": "crd-groups", "deprecated-apis": "summary", "privileged/hostpath": "namespaces",
    "gpu": "nodes", "operators": "crd-groups", "intercept-all-webhooks": "webhooks",
}  # fmt: skip

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


def watch_out(
    conn: sqlite3.Connection, rules: Rules, site: str | None = None, cluster_id: str | None = None
) -> dict[tuple[str, str], list[str]]:
    """Per cluster: WATCH_OUT_FLAGS that apply, in that order, then the stored complexity flags."""
    floating = floating_ips(conn, rules, site, cluster_id)
    return _watch_out(conn, rules, site, floating, databases(conn, rules, site, cluster_id), cluster_id)


def _watch_out(
    conn: sqlite3.Connection, rules: Rules, site: str | None, floating: dict, dbs: dict, cluster_id: str | None = None
) -> dict[tuple[str, str], list[str]]:
    where, args = _scope(site, cluster_id, "c")
    r_where, r_args = _scope(site, cluster_id, "r")
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
        # LB (with pinned loadBalancerIP), Service externalIPs and ingress LB addresses; node ExternalIP
        # addresses and the control plane endpoint count as floating IPs but do not set the flag
        "external-ip": {k for k, f in floating.items() if f["lb"] or f["external_ip"] or f["ingress_lb"]},
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
    status: str | None = None,
    wave: str | None = None,
) -> list[dict]:
    """Inventory rows plus pools, OS, usage, PVs, floating IPs, databases, Watch out flags and
    migration tracking (status, wave, note) per cluster."""
    if flag and flag not in ALL_FLAGS:
        raise ValueError(f"unknown flag: {flag}")
    if status and status not in tracking.STATUSES:
        raise ValueError(f"unknown status: {status}")
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
    tracked = tracking.current(conn, site)
    untracked = {"status": tracking.STATUSES[0], "wave": "", "note": ""}
    for row in rows:
        t = tracked.get((row["site"], row["cluster_id"]), untracked)
        row.update(status=t["status"], wave=t["wave"], note=t["note"])
    if status:
        rows = [r for r in rows if r["status"] == status]
    if wave:
        rows = [r for r in rows if r["wave"] == wave]
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
        count_bytes[1] += _quantity(parse_bytes, v["size"]) or 0  # an unparsable size counts as unknown

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
            # no cluster_profile row: the cluster never had a successful collection
            collected=row["class"] is not None,
        )  # fmt: skip
    return rows


def _chart_inputs(rows: list[dict], site: str | None) -> dict:
    """Overview chart data summarized across the given cluster rows."""
    suffix = f"&site={quote(site)}" if site else ""

    def block(prefix: str, unit: str) -> dict:
        out = {}
        for key in ("alloc", "req", "used"):
            values = [r[f"{prefix}_{key}_{unit}"] for r in rows if r[f"{prefix}_{key}_{unit}"] is not None]
            out[key], out[f"{key}_n"] = (sum(values) if values else None), len(values)
        return out

    class_counts: dict[str, int] = {}
    site_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        label = r["class"] or NOT_COLLECTED
        class_counts[label] = class_counts.get(label, 0) + 1
        per_site = site_counts.setdefault(r["site"], {})
        per_site[label] = per_site.get(label, 0) + 1
    by_class = [
        {"label": label, "count": class_counts[label],
         "href": None if label == NOT_COLLECTED else f"/clusters?cls={quote(label)}{suffix}"}
        for label in CLASS_ORDER if class_counts.get(label)
    ]  # fmt: skip

    flag_counts = {f: sum(1 for r in rows if f in r["flags"]) for f in ALL_FLAGS}
    by_flag = sorted(
        ({"label": f, "count": n, "href": f"/clusters?flag={quote(f)}{suffix}"} for f, n in flag_counts.items() if n),
        key=lambda x: (-x["count"], x["label"]),
    )

    def top(column: str) -> tuple[list[dict], float | None]:
        have = sorted((r for r in rows if r[column] is not None), key=lambda r: (-r[column], r["name"]))
        items = [
            {"label": r["name"], "value": r[column], "href": f"/clusters/{r['site']}/{r['cluster_id']}"}
            for r in have[:TOP_N]
        ]
        total = sum(r[column] for r in have)
        return items, (round(100 * sum(i["value"] for i in items) / total, 1) if total else None)

    top_cpu, top_cpu_share = top("cpu_req_m")
    top_mem, top_mem_share = top("mem_req_b")
    return {
        "capacity": {"clusters": len(rows), "cpu": block("cpu", "m"), "mem": block("mem", "b")},
        "by_class": by_class,
        "by_flag": by_flag,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "top_cpu_share": top_cpu_share,
        "top_mem_share": top_mem_share,
        "site_class": [
            {"site": s, "counts": {label: site_counts[s][label] for label in CLASS_ORDER if label in site_counts[s]}}
            for s in sorted(site_counts)
        ],
        "floating": {
            **{k: sum(r["floating"][k] for r in rows) for k in ("lb", "external_ip", "ingress_lb", "node_external")},
            "control_plane": sum(1 for r in rows if r["floating"]["control_plane"]),
        },
    }


def site_overview(conn: sqlite3.Connection, rules: Rules, site: str | None = None, scope: str = "remaining") -> dict:
    """Totals and chart data for the overview page, for one site or all sites.

    scope "remaining" builds the charts only from clusters still left to move (status in
    tracking.REMAINING); "all" uses every cluster. Totals and the cluster list always use all.
    """
    if scope not in ("remaining", "all"):
        raise ValueError(f"unknown scope: {scope}")
    rows = cluster_rows(conn, rules, site=site)
    chart_rows = rows if scope == "all" else [r for r in rows if r["status"] in tracking.REMAINING]
    keep = {(r["site"], r["cluster_id"]) for r in chart_rows}
    r_where, r_args = _scope(site, None, "r")
    os_all: dict[tuple[str, str], dict[str, int]] = {}
    for r in conn.execute(
        f"""SELECT r.site, r.cluster_id,
                   COALESCE(json_extract(r.data, '$.status.nodeInfo.osImage'), 'not collected') AS os,
                   COUNT(*) AS nodes
            FROM resources r WHERE r.kind = 'nodes'{r_where} GROUP BY r.site, r.cluster_id, os""",
        r_args,
    ):
        os_all.setdefault((r["site"], r["cluster_id"]), {})[r["os"]] = r["nodes"]

    def os_sum(keys) -> list[dict]:
        sums: dict[str, int] = {}
        for k in keys:
            for os_name, n in os_all.get(k, {}).items():
                sums[os_name] = sums.get(os_name, 0) + n
        return [{"os": o, "nodes": n} for o, n in sorted(sums.items(), key=lambda x: (-x[1], x[0]))]

    pv_all: dict[tuple[str, str], dict[str, dict]] = {}
    for v in conn.execute(
        f"""SELECT r.site, r.cluster_id,
                   COALESCE(json_extract(r.data, '$.spec.storageClassName'), 'none') AS storage_class,
                   json_extract(r.data, '$.spec.capacity.storage') AS size
            FROM resources r WHERE r.kind = 'persistentvolumes'{r_where}""",
        r_args,
    ):
        per = pv_all.setdefault((v["site"], v["cluster_id"]), {})
        c = per.setdefault(v["storage_class"], {"storage_class": v["storage_class"], "pv_count": 0, "bytes": 0})
        c["pv_count"] += 1
        c["bytes"] += _quantity(parse_bytes, v["size"]) or 0  # an unparsable size counts as unknown
    pv_by_class: dict[str, dict] = {}
    for k in keep:
        for name, c in pv_all.get(k, {}).items():
            t = pv_by_class.setdefault(name, {"storage_class": name, "pv_count": 0, "bytes": 0})
            t["pv_count"] += c["pv_count"]
            t["bytes"] += c["bytes"]
    return {
        "totals": {
            "clusters": len(rows),
            "nodes": sum(n for per in os_all.values() for n in per.values()),
            "cpu_alloc_m": sum(r["cpu_alloc_m"] or 0 for r in rows),
            "mem_alloc_b": sum(r["mem_alloc_b"] or 0 for r in rows),
            "pv_count": sum(r["pv_count"] for r in rows),
            "pv_bytes": sum(r["pv_bytes"] for r in rows),
            "floating_ips": sum(r["floating"]["total"] for r in rows),
            "special_network_clusters": sum(1 for r in rows if "special-network-config" in r["flags"]),
        },
        "charts": {
            "scope": scope,
            "os_images": os_sum(keep),
            "pv_by_class": sorted(pv_by_class.values(), key=lambda c: (-c["pv_count"], c["storage_class"])),
            **_chart_inputs(chart_rows, site),
        },
        "clusters": rows,
    }  # fmt: skip


def flat_row(row: dict) -> dict:
    """One CSV row: lists joined with "; ", floating IPs as a total plus one column per source.

    A cluster without a successful collection gets "not collected" for PVs, floating IPs, LB and flags.
    """
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
    if not row.get("collected", True):
        for k in out:
            if k in ("pv_count", "pv_bytes", "lb_implementations", "flags") or k.startswith("floating_"):
                out[k] = NOT_COLLECTED
    return out


# ---- cluster page ----

BUILTIN_LABEL_PREFIXES = ("kubernetes.io/", "beta.kubernetes.io/", "node-role.kubernetes.io/", "node.kubernetes.io/")


def _quantity(parse, value) -> int | None:
    try:
        return parse(value) if value else None
    except (ArithmeticError, ValueError):
        return None


def assign_nodes(nodes: list[dict], pools: list[dict], prov_name: str | None) -> list[dict]:
    """Group nodes by machine pool (Rancher names machines <prov_name>-<pool>-...), the rest by role set."""
    groups = [
        {"kind": "pool", "name": p["name"], "quantity": p["quantity"], "roles": p["roles"], "labels": p["labels"],
         "taints": p["taints"], "machine_config_kind": p["machine_config_kind"], "nodes": []}
        for p in pools
    ]  # fmt: skip
    # longest prefix first, so pool "worker" does not take nodes of pool "worker-gpu"
    by_prefix = sorted(((f"{prov_name}-{g['name']}-", g) for g in groups if prov_name), key=lambda x: -len(x[0]))
    by_role: dict[str, dict] = {}
    for n in nodes:
        group = next((g for prefix, g in by_prefix if n["name"].startswith(prefix)), None)
        if group is None:
            name = "/".join(n["roles"]) or "no role"
            group = by_role.setdefault(
                name,
                {"kind": "role", "name": name, "quantity": 0, "roles": n["roles"], "labels": {}, "taints": [],
                 "machine_config_kind": None, "nodes": []},
            )  # fmt: skip
            group["quantity"] += 1
            group["taints"] = sorted({*group["taints"], *n.get("taints", [])})
        group["nodes"].append(n)
    return groups + [by_role[k] for k in sorted(by_role)]


def node_groups(conn: sqlite3.Connection, site: str, cluster_id: str) -> dict:
    """Nodes of one cluster grouped by machine pool or role, with the cluster's pools_status."""
    c = conn.execute("SELECT pools_status, prov_name FROM clusters WHERE site = ? AND cluster_id = ?", (site, cluster_id))
    cluster = c.fetchone()
    pools = [
        {"name": p["name"], "quantity": p["quantity"], "roles": json.loads(p["roles"] or "[]"),
         "labels": json.loads(p["labels"] or "{}"), "taints": json.loads(p["taints"] or "[]"),
         "machine_config_kind": p["machine_config_kind"]}
        for p in conn.execute(
            "SELECT * FROM machine_pools WHERE site = ? AND cluster_id = ? ORDER BY name", (site, cluster_id)
        )
    ]  # fmt: skip
    labels: dict[int, dict] = {}
    for r in conn.execute(
        """SELECT l.resource_id, l.key, l.value FROM resource_labels l JOIN resources r ON r.id = l.resource_id
           WHERE r.site = ? AND r.cluster_id = ? AND r.kind = 'nodes' ORDER BY l.key""",
        (site, cluster_id),
    ):
        labels.setdefault(r["resource_id"], {})[r["key"]] = r["value"]
    nodes = []
    for r in conn.execute(
        "SELECT id, name, data FROM resources WHERE site = ? AND cluster_id = ? AND kind = 'nodes' ORDER BY name",
        (site, cluster_id),
    ):
        data = json.loads(r["data"])
        status = data.get("status") or {}
        info = status.get("nodeInfo") or {}
        capacity = status.get("capacity") or {}
        node_labels = labels.get(r["id"], {})
        nodes.append(
            {
                "name": r["name"],
                "internal_ip": next(
                    (a.get("address") for a in status.get("addresses") or [] if a.get("type") == "InternalIP"), None
                ),
                "os": info.get("osImage"), "kernel": info.get("kernelVersion"),
                "runtime": info.get("containerRuntimeVersion"), "arch": info.get("architecture"),
                "ready": data.get("ready"),
                "cpu_m": _quantity(parse_cpu, capacity.get("cpu")),
                "mem_b": _quantity(parse_bytes, capacity.get("memory")),
                "roles": data.get("roles") or [], "taints": data.get("taints") or [],
                "labels_builtin": {k: v for k, v in node_labels.items() if k.startswith(BUILTIN_LABEL_PREFIXES)},
                "labels_other": {k: v for k, v in node_labels.items() if not k.startswith(BUILTIN_LABEL_PREFIXES)},
            }
        )  # fmt: skip
    groups = assign_nodes(nodes, pools, cluster["prov_name"] if cluster else None)
    for g in groups:
        g["os"] = sorted({n["os"] for n in g["nodes"] if n["os"]})
        known = [n for n in g["nodes"] if n["cpu_m"] is not None and n["mem_b"] is not None]
        complete = g["nodes"] and len(known) == len(g["nodes"])
        g["cpu_m"] = sum(n["cpu_m"] for n in known) if complete else None
        g["mem_b"] = sum(n["mem_b"] for n in known) if complete else None
    return {"pools_status": cluster["pools_status"] if cluster else None, "groups": groups}


def _node_pinned_pv(pv: dict) -> bool:
    return bool(pv["pinned"]) or pv["source"] in ("local", "hostPath")


def cluster_sheet(conn: sqlite3.Connection, rules: Rules, site: str, cluster_id: str) -> dict:
    """Everything the cluster page shows beyond the cluster row, one entry per page table."""
    key = (site, cluster_id)
    floating = floating_ips(conn, rules, site, cluster_id)
    dbs = databases(conn, rules, site, cluster_id)
    flags = _watch_out(conn, rules, site, floating, dbs, cluster_id).get(key, [])
    nodes = node_groups(conn, site, cluster_id)
    pvs = queries.pvs(conn, site=site, cluster_id=cluster_id)
    for pv in pvs:
        pv["node_pinned"] = _node_pinned_pv(pv)
    pinned_claims = {(pv["claim_namespace"], pv["claim_name"]) for pv in pvs if pv["node_pinned"]}
    pvcs = queries.pvcs(conn, [], site=site, cluster_id=cluster_id)
    for pvc in pvcs:
        pvc["node_pinned"] = (pvc["namespace"], pvc["name"]) in pinned_claims
    services = queries.cluster_services(conn, site, cluster_id)
    nodeport_services = [
        s for s in services if s["type"] == "NodePort" or (s["type"] != "LoadBalancer" and s["addresses"])
    ]  # fmt: skip
    webhooks = queries.webhooks(conn, site=site, cluster_id=cluster_id)
    for w in webhooks:
        w["custom"] = _custom_webhook(w, rules)
    statefulsets = [
        {"namespace": r["namespace"], "name": r["name"], "replicas": r["replicas"],
         "volume_claim_templates": json.loads(r["vcts"] or "[]")}
        for r in conn.execute(
            """SELECT r.namespace, r.name, json_extract(r.data, '$.spec.replicas') AS replicas,
                      json_extract(r.data, '$.volume_claim_templates') AS vcts
               FROM resources r WHERE r.site = ? AND r.cluster_id = ? AND r.kind = 'statefulsets'
               ORDER BY r.namespace, r.name""",
            (site, cluster_id),
        )
    ]  # fmt: skip
    stored = conn.execute("SELECT labels FROM clusters WHERE site = ? AND cluster_id = ?", key).fetchone()
    try:
        labels = json.loads(stored["labels"] or "{}") if stored else {}
    except ValueError:  # malformed labels JSON counts as no labels
        labels = {}
    groups = nodes["groups"]
    return {
        "flags": flags,
        "floating": floating.get(key),
        "pools_status": nodes["pools_status"],
        "groups": groups,
        "tables": {
            "nodes": [{**n, "group": g["name"]} for g in groups for n in g["nodes"]],
            "pools": [{k: v for k, v in g.items() if k != "nodes"} | {"node_count": len(g["nodes"])} for g in groups],
            "lb_services": lb_services(conn, rules, site, cluster_id),
            "services": services,
            "nodeport_services": nodeport_services,
            "pvs": pvs,
            "pvcs": pvcs,
            "databases": [{"engine": h["engine"], "namespace": h.get("namespace"), "kind": h.get("kind"),
                           "name": h.get("name"), "image": h.get("image"), "crd_group": h.get("crd_group")}
                          for h in dbs.get(key, [])],
            "statefulsets": statefulsets,
            "webhooks": webhooks,
            "annotations": queries.cluster_annotations(conn, site, cluster_id),
            "cluster_labels": [{"key": k, "value": v} for k, v in sorted(labels.items())],
        },
    }  # fmt: skip
