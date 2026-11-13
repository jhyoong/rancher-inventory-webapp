"""Read queries shared by the web pages."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from inventory.collector.rollup import COMPLEXITY_FLAGS
from inventory.collector.runner import last_good_run
from inventory.settings import Settings


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def data_as_of(conn: sqlite3.Connection) -> datetime | None:
    row = last_good_run(conn)
    if row is None:
        return None
    return _parse(row["finished_at"] or row["started_at"])


def is_stale(as_of: datetime | None, settings: Settings) -> bool:
    if as_of is None:
        return True
    return datetime.now(timezone.utc) - as_of > timedelta(hours=settings.stale_after_hours)


def active_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, clusters_total FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    counts = conn.execute(
        "SELECT SUM(status IN ('ok', 'failed', 'skipped')), SUM(status='failed')"
        " FROM run_clusters WHERE run_id=?",
        (row["id"],),
    ).fetchone()
    return {"id": row["id"], "done": counts[0] or 0, "total": row["clusters_total"], "failed": counts[1] or 0}


TOKEN_WARN_DAYS = 30


def token_warnings(settings: Settings) -> list[str]:
    """One line per site whose token expires within TOKEN_WARN_DAYS days (or has expired)."""
    from inventory.settings import ConfigError, load_sites

    try:
        sites = load_sites(settings)
    except (ConfigError, OSError, ValueError):
        return []
    today = datetime.now(timezone.utc).date()
    out = []
    for s in sites:
        if s.token_expires is None:
            continue
        days = (s.token_expires - today).days
        if days < 0:
            out.append(f"Token for {s.name} expired {-days} days ago")
        elif days <= TOKEN_WARN_DAYS:
            out.append(f"Token for {s.name} expires in {days} days")
    return out


def header_context(conn: sqlite3.Connection, settings: Settings) -> dict:
    as_of = data_as_of(conn)
    return {
        "data_as_of": as_of, "stale": is_stale(as_of, settings), "active_run": active_run(conn),
        "token_warnings": token_warnings(settings),
    }  # fmt: skip


_CLUSTER_SQL = """
SELECT c.site, c.cluster_id, c.name, c.state, c.provider, c.k8s_version, c.node_count,
       c.last_ok_at, c.last_error,
       m.cpu_alloc_m, m.cpu_req_m, m.cpu_used_m, m.mem_alloc_b, m.mem_req_b, m.mem_used_b
FROM clusters c
LEFT JOIN cluster_metrics_latest m ON m.site = c.site AND m.cluster_id = c.cluster_id
"""


def clusters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(_CLUSTER_SQL + " ORDER BY c.site, c.name").fetchall()


def cluster(conn: sqlite3.Connection, site: str, cluster_id: str) -> sqlite3.Row | None:
    return conn.execute(_CLUSTER_SQL + " WHERE c.site=? AND c.cluster_id=?", (site, cluster_id)).fetchone()


def cluster_metrics(conn: sqlite3.Connection, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT day, nodes, pods, cpu_alloc_m, cpu_req_m, cpu_used_m, mem_alloc_b, mem_req_b, mem_used_b"
        " FROM cluster_metrics WHERE site=? AND cluster_id=? ORDER BY day",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


def site_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT site FROM clusters ORDER BY site")]


def overview(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(DISTINCT site), COUNT(*), COALESCE(SUM(node_count), 0) FROM clusters"
    ).fetchone()
    last = conn.execute(
        "SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    failed = []
    if last is not None:
        failed = conn.execute(
            "SELECT rc.site, rc.cluster_id, rc.error, c.name FROM run_clusters rc"
            " LEFT JOIN clusters c ON c.site = rc.site AND c.cluster_id = rc.cluster_id"
            " WHERE rc.run_id=? AND rc.status='failed' ORDER BY rc.site, rc.cluster_id",
            (last["id"],),
        ).fetchall()
    return {"sites": row[0], "clusters": row[1], "nodes": row[2], "last_run": last, "failed": failed}


def runs(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def run_clusters(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT rc.*, c.name FROM run_clusters rc"
        " LEFT JOIN clusters c ON c.site = rc.site AND c.cluster_id = rc.cluster_id"
        " WHERE rc.run_id=? ORDER BY rc.status != 'failed', rc.site, rc.cluster_id",
        (run_id,),
    ).fetchall()
    return [dict(r, duration=duration(r["started_at"], r["finished_at"])) for r in rows]


def site_totals(rows: list[dict]) -> list[dict]:
    totals: dict[str, dict] = {}
    for r in rows:
        t = totals.setdefault(r["site"], {"site": r["site"], "clusters": 0, "ok": 0, "failed": 0, "objects": 0})
        t["clusters"] += 1
        t["objects"] += r["objects"] or 0
        if r["status"] in ("ok", "failed"):
            t[r["status"]] += 1
    return list(totals.values())


def duration(started_at: str | None, finished_at: str | None) -> float | None:
    start, end = _parse(started_at), _parse(finished_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


# ---- query page, presets and check ----

ALLOWED_FILTERS = {
    "site", "cluster", "kind", "namespace", "name", "label", "image",
    "annotation", "service_type", "address", "helm_chart", "crd_group", "class", "flag",
}  # fmt: skip


def validate_filters(filters: dict) -> None:
    """Raise ValueError for an unknown filter key or flag value."""
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    _where({str(k): str(v) for k, v in filters.items()})


def _key_value(value: str, prefix: str, params: dict) -> str:
    """Condition on <alias>.key and optional <alias>.value for "key" or "key=value"."""
    key, sep, val = value.partition("=")
    params[f"{prefix}_key"] = key
    cond = f"{prefix[0]}.key = :{prefix}_key"
    if sep:
        params[f"{prefix}_value"] = val
        cond += f" AND {prefix[0]}.value = :{prefix}_value"
    return cond


def _where(filters: dict[str, str]) -> tuple[str, dict]:
    """WHERE clause over resources r and clusters c. Values are only named parameters."""
    bad = set(filters) - ALLOWED_FILTERS
    if bad:
        raise ValueError(f"unknown filter: {', '.join(sorted(bad))}")
    parts: list[str] = []
    params: dict = {}
    for key, value in filters.items():
        if value is None or str(value).strip() == "":
            continue
        value = str(value).strip()
        if key == "site":
            parts.append("r.site = :site")
        elif key == "cluster":
            parts.append("(r.cluster_id = :cluster OR c.name = :cluster)")
        elif key == "kind":
            parts.append("r.kind = :kind")
        elif key == "namespace":
            parts.append("r.namespace = :namespace")
        elif key == "name":
            parts.append("r.name LIKE '%' || :name || '%' ESCAPE '\\'")
            value = _like_escape(value)
        elif key == "image":
            parts.append(
                "EXISTS (SELECT 1 FROM resource_images i WHERE i.resource_id = r.id"
                " AND i.image LIKE '%' || :image || '%' ESCAPE '\\')"
            )
            value = _like_escape(value)
        elif key == "label":
            cond = _key_value(value, "label", params)
            parts.append(f"EXISTS (SELECT 1 FROM resource_labels l WHERE l.resource_id = r.id AND {cond})")
            continue
        elif key == "annotation":
            cond = _key_value(value, "annotation", params)
            parts.append(f"EXISTS (SELECT 1 FROM resource_annotations a WHERE a.resource_id = r.id AND {cond})")
            continue
        elif key == "service_type":
            parts.append("r.kind = 'services' AND json_extract(r.data, '$.spec.type') = :service_type")
        elif key == "address":
            parts.append(
                "EXISTS (SELECT 1 FROM addresses ad WHERE ad.resource_id = r.id"
                " AND (ad.address = :address OR ad.address LIKE :address_prefix ESCAPE '\\'))"
            )
            params["address_prefix"] = _like_escape(value) + "%"
        elif key == "helm_chart":
            chart, sep, version = value.partition("@")
            parts.append(f"r.kind = 'helmreleases' AND {_HELM_CHART} = :helm_chart")
            params["helm_chart"] = chart
            if sep:
                parts.append(f"{_HELM_VERSION} = :helm_version")
                params["helm_version"] = version
            continue
        elif key == "crd_group":
            parts.append("r.kind = 'customresourcedefinitions' AND json_extract(r.data, '$.spec.group') = :crd_group")
        elif key == "class":
            parts.append(
                "EXISTS (SELECT 1 FROM cluster_profile p WHERE p.site = r.site AND p.cluster_id = r.cluster_id"
                " AND p.class = :class)"
            )
        elif key == "flag":
            if value in FLAG_COLUMNS:
                cond = f"{FLAG_COLUMNS[value]} = 1"  # column comes from the fixed map
            elif value in COMPLEXITY_FLAGS:
                cond = "EXISTS (SELECT 1 FROM json_each(coalesce(p.complexity_flags, '[]')) WHERE value = :flag)"
            else:
                raise ValueError(f"unknown flag: {value}")
            parts.append(
                f"EXISTS (SELECT 1 FROM cluster_profile p WHERE p.site = r.site AND p.cluster_id = r.cluster_id AND {cond})"
            )
        params[key] = value
    return (" AND ".join(parts) if parts else "1"), params


_FROM = " FROM resources r LEFT JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id"


def build_query(filters: dict[str, str]) -> tuple[str, dict]:
    where, params = _where(filters)
    sql = (
        "SELECT r.site, r.cluster_id, c.name AS cluster_name, r.kind, r.namespace, r.name"
        + _FROM
        + f" WHERE {where} ORDER BY r.site, r.cluster_id, r.kind, r.namespace, r.name"
    )
    return sql, params


def list_presets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id, p.name, p.filters, p.created_at, p.created_by, u.username AS creator"
        " FROM presets p LEFT JOIN users u ON u.id = p.created_by ORDER BY p.name"
    ).fetchall()


def get_preset(conn: sqlite3.Connection, preset_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM presets WHERE id=?", (preset_id,)).fetchone()


def check_preset(conn: sqlite3.Connection, preset_id: int) -> list[dict]:
    """One row per cluster: present, absent or unknown."""

    preset = get_preset(conn, preset_id)
    if preset is None:
        raise ValueError("preset not found")
    filters = json.loads(preset["filters"])
    validate_filters(filters)
    where, params = _where(filters)
    found = {
        (r[0], r[1])
        for r in conn.execute("SELECT DISTINCT r.site, r.cluster_id" + _FROM + f" WHERE {where}", params)
    }
    kind = (filters.get("kind") or "").strip()
    out = []
    for c in conn.execute("SELECT site, cluster_id, name, last_ok_run FROM clusters ORDER BY site, name"):
        site, cid = c["site"], c["cluster_id"]
        latest = conn.execute(
            "SELECT status FROM run_clusters WHERE site=? AND cluster_id=? ORDER BY run_id DESC LIMIT 1",
            (site, cid),
        ).fetchone()
        reason = None
        if c["last_ok_run"] is None:
            reason = "never collected"
        elif latest is not None and latest["status"] == "failed":
            reason = "latest run failed"
        elif kind:
            k = conn.execute(
                "SELECT status FROM run_kinds WHERE run_id=? AND site=? AND cluster_id=? AND kind=?",
                (c["last_ok_run"], site, cid, kind),
            ).fetchone()
            if k is None or k["status"] != "ok":
                reason = f"kind {kind} was not collected"
        if reason:
            result = "unknown"
        else:
            result = "present" if (site, cid) in found else "absent"
        out.append(
            {"site": site, "cluster_id": cid, "name": c["name"], "result": result, "reason": reason}
        )
    return out


# ---- inventory views ----

FLAG_COLUMNS = {"stateful": "p.stateful", "helm": "p.helm", "custom_crds": "p.custom_crds", "hostpath": "p.hostpath"}

_INVENTORY_SQL = """
SELECT c.site, c.cluster_id, c.name, c.k8s_version, c.node_count, c.last_ok_at, c.labels,
       p.class, p.stateful, p.helm, p.custom_crds, p.hostpath,
       p.statefulsets, p.pvc_bound, p.pvc_bytes, p.helm_releases, p.crd_groups,
       p.lb_services, p.ingresses, p.network, p.updated_run, p.complexity_flags,
       (SELECT rc.status FROM run_clusters rc WHERE rc.site = c.site AND rc.cluster_id = c.cluster_id
        AND rc.status IN ('ok', 'failed') ORDER BY rc.run_id DESC LIMIT 1) AS latest_status,
       (SELECT rc.error FROM run_clusters rc WHERE rc.site = c.site AND rc.cluster_id = c.cluster_id
        AND rc.status IN ('ok', 'failed') ORDER BY rc.run_id DESC LIMIT 1) AS latest_error
FROM clusters c
LEFT JOIN cluster_profile p ON p.site = c.site AND p.cluster_id = c.cluster_id
"""


def _decode_profile(d: dict) -> dict:

    groups = json.loads(d["crd_groups"]) if d.get("crd_groups") else []
    net = json.loads(d["network"]) if d.get("network") else {}
    d["complexity"] = json.loads(d["complexity_flags"]) if d.get("complexity_flags") else []
    d["crd_group_list"] = groups
    d["crd_group_count"] = len(groups)
    d["cni"] = net.get("cni") or ""
    d["cidrs"] = ", ".join(v for v in (net.get("cluster_cidr"), net.get("service_cidr")) if v)
    if not d["cidrs"]:
        pods = net.get("pod_cidrs") or []
        d["cidrs"] = "pod ranges from nodes: " + ", ".join(pods) if pods else "not available"
    d["failed"] = d.get("latest_status") == "failed"
    return d


def inventory_rows(conn, site=None, cls=None, flag=None, label=None) -> list[dict]:
    parts: list[str] = []
    params: dict = {}
    if site:
        parts.append("c.site = :site")
        params["site"] = site
    if cls:
        parts.append("p.class = :cls")
        params["cls"] = cls
    if flag:
        if flag in FLAG_COLUMNS:
            parts.append(f"{FLAG_COLUMNS[flag]} = 1")  # column comes from the fixed map above
        elif flag in COMPLEXITY_FLAGS:
            parts.append("EXISTS (SELECT 1 FROM json_each(coalesce(p.complexity_flags, '[]')) WHERE value = :flag)")
            params["flag"] = flag
        else:
            raise ValueError(f"unknown flag: {flag}")
    if label:
        lkey, sep, lvalue = label.partition("=")
        cond = "key = :label_key"
        params["label_key"] = lkey
        if sep:
            cond += " AND value = :label_value"
            params["label_value"] = lvalue
        # malformed labels JSON counts as no labels
        parts.append(
            "EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.labels) THEN c.labels ELSE '{}' END)"
            f" WHERE {cond})"
        )
    where = " WHERE " + " AND ".join(parts) if parts else ""
    rows = conn.execute(_INVENTORY_SQL + where + " ORDER BY c.site, c.name", params).fetchall()
    return [_decode_profile(dict(r)) for r in rows]


def inventory_filter_options(conn) -> dict:
    return {
        "sites": [r[0] for r in conn.execute("SELECT DISTINCT site FROM clusters ORDER BY site")],
        "classes": [r[0] for r in conn.execute("SELECT DISTINCT class FROM cluster_profile ORDER BY class")],
        "flags": list(FLAG_COLUMNS) + [f for f in COMPLEXITY_FLAGS if f not in FLAG_COLUMNS],
    }


def clusters_per_class(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT p.class, COUNT(*) AS clusters FROM cluster_profile p"
        " JOIN clusters c ON c.site = p.site AND c.cluster_id = p.cluster_id"
        " GROUP BY p.class ORDER BY p.class"
    ).fetchall()
    return [dict(r) for r in rows]


def cluster_inventory(conn, site: str, cluster_id: str) -> dict | None:
    row = conn.execute(
        _INVENTORY_SQL + " WHERE c.site = ? AND c.cluster_id = ?", (site, cluster_id)
    ).fetchone()
    return _decode_profile(dict(row)) if row else None


def namespace_profiles(conn, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM namespace_profile WHERE site=? AND cluster_id=? ORDER BY is_system, namespace",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- network ----


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


NETWORK_LIMIT = 5000  # rows per /network list, like the /query cap


def search_addresses(conn, q: str, limit: int = NETWORK_LIMIT) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    rows = conn.execute(
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.kind, r.namespace, r.name,
                  a.role, a.address, a.ports
           FROM addresses a
           JOIN resources r ON r.id = a.resource_id
           JOIN clusters  c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE a.address = :q OR a.address LIKE :like ESCAPE '\\'
           ORDER BY r.site, c.name, r.kind, r.namespace, r.name, a.address
           LIMIT :limit""",
        {"q": q, "like": _like_escape(q) + "%", "limit": limit},
    ).fetchall()
    return [dict(r) for r in rows]


_ADDR_SQL = """
SELECT r.site, c.name AS cluster, r.cluster_id, r.kind, r.namespace, r.name,
       a.role, a.address, a.ports{extra}
FROM addresses a
JOIN resources r ON r.id = a.resource_id
JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
WHERE {where}
ORDER BY r.site, c.name, a.address, r.namespace, r.name
LIMIT ?
"""


def _addr_rows(conn, where: str, limit: int, extra: str = "") -> list[dict]:
    sql = _ADDR_SQL.format(where=where, extra=extra)
    return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]


def load_balancers(conn, limit: int = NETWORK_LIMIT) -> list[dict]:
    return _addr_rows(
        conn,
        "a.role IN ('lb', 'ingress-lb')",
        limit,
        extra=""", (SELECT group_concat(an.key || '=' || an.value, char(10))
                    FROM resource_annotations an WHERE an.resource_id = r.id) AS annotations""",
    )


def ingress_hosts(conn, limit: int = NETWORK_LIMIT) -> list[dict]:
    return _addr_rows(conn, "a.role = 'ingress-host'", limit)


def node_ips(conn, limit: int = NETWORK_LIMIT) -> list[dict]:
    return _addr_rows(conn, "a.role IN ('node-internal', 'node-external')", limit)


def nodeport_and_external_services(conn, limit: int = NETWORK_LIMIT) -> list[dict]:
    rows = conn.execute(
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.namespace, r.name,
                  json_extract(r.data, '$.spec.type') AS type,
                  json_extract(r.data, '$.spec.externalIPs') AS external_ips,
                  (SELECT group_concat(a.address, ', ') FROM addresses a
                   WHERE a.resource_id = r.id AND a.role = 'external-ip') AS external_addresses,
                  (SELECT group_concat(a.ports, ', ') FROM addresses a
                   WHERE a.resource_id = r.id AND a.role = 'external-ip') AS ports
           FROM resources r
           JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE r.kind = 'services'
             AND (json_extract(r.data, '$.spec.type') = 'NodePort'
                  OR EXISTS (SELECT 1 FROM addresses a WHERE a.resource_id = r.id AND a.role = 'external-ip'))
           ORDER BY r.site, c.name, r.namespace, r.name
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def duplicate_addresses(conn, limit: int = NETWORK_LIMIT) -> list[dict]:
    """An lb, ingress-lb or ingress-host address found in more than one cluster, any role mix."""
    rows = conn.execute(
        """SELECT lower(a.address) AS address, group_concat(DISTINCT a.role) AS role,
                  COUNT(DISTINCT r.site || '/' || r.cluster_id) AS clusters,
                  group_concat(DISTINCT r.site || '/' || c.name) AS where_found
           FROM addresses a
           JOIN resources r ON r.id = a.resource_id
           JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE a.role IN ('lb', 'ingress-lb', 'ingress-host')
           GROUP BY lower(a.address)
           HAVING clusters > 1
           ORDER BY address
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- helm, crds, annotations ----

_HELM_CHART = "coalesce(json_extract(r.data, '$.spec.chart.metadata.name'), json_extract(r.data, '$.spec.name'), r.name)"
_HELM_VERSION = "coalesce(json_extract(r.data, '$.spec.chart.metadata.version'), '')"


def helm_charts(conn) -> list[dict]:
    """One row per chart: release and cluster counts, with the clusters per chart version."""
    rows = conn.execute(
        f"""SELECT {_HELM_CHART} AS chart, {_HELM_VERSION} AS version,
                   COUNT(*) AS releases, COUNT(DISTINCT r.site || '/' || r.cluster_id) AS clusters
            FROM resources r WHERE r.kind = 'helmreleases'
            GROUP BY chart, version ORDER BY chart, version"""
    ).fetchall()
    totals = conn.execute(
        f"""SELECT {_HELM_CHART} AS chart, COUNT(*) AS releases,
                   COUNT(DISTINCT r.site || '/' || r.cluster_id) AS clusters
            FROM resources r WHERE r.kind = 'helmreleases' GROUP BY chart ORDER BY chart"""
    ).fetchall()
    out = {t["chart"]: {"chart": t["chart"], "releases": t["releases"], "clusters": t["clusters"], "versions": []} for t in totals}
    for r in rows:
        out[r["chart"]]["versions"].append(
            {"version": r["version"], "clusters": r["clusters"], "releases": r["releases"]}
        )
    return list(out.values())


def helm_releases(conn, chart: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT r.site, c.name AS cluster, r.cluster_id, r.namespace, r.name,
                   {_HELM_VERSION} AS version,
                   json_extract(r.data, '$.spec.chart.metadata.appVersion') AS app_version,
                   json_extract(r.data, '$.spec.info.status') AS status,
                   json_extract(r.data, '$.spec.info.lastDeployed') AS last_deployed
            FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
            WHERE r.kind = 'helmreleases' AND {_HELM_CHART} = ?
            ORDER BY r.site, c.name, r.namespace, r.name""",
        (chart,),
    ).fetchall()
    return [dict(r) for r in rows]


def crd_groups(conn, custom_only: bool, rules) -> list[dict]:
    from inventory.collector.classify import is_system_crd_group

    rows = conn.execute(
        """SELECT json_extract(r.data, '$.spec.group') AS grp, COUNT(*) AS crds,
                  COUNT(DISTINCT r.site || '/' || r.cluster_id) AS clusters
           FROM resources r WHERE r.kind = 'customresourcedefinitions' AND grp IS NOT NULL
           GROUP BY grp ORDER BY clusters DESC, grp"""
    ).fetchall()
    out = [
        {"group": r["grp"], "crds": r["crds"], "clusters": r["clusters"], "custom": not is_system_crd_group(r["grp"], rules)}
        for r in rows
    ]
    return [g for g in out if g["custom"]] if custom_only else out


def annotation_keys(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT a.key, COUNT(*) AS objects, COUNT(DISTINCT r.site || '/' || r.cluster_id) AS clusters
           FROM resource_annotations a JOIN resources r ON r.id = a.resource_id
           WHERE a.custom = 1 GROUP BY a.key ORDER BY clusters DESC, a.key"""
    ).fetchall()
    return [dict(r) for r in rows]


def annotation_objects(conn, key: str, limit: int = 5000) -> list[dict]:
    rows = conn.execute(
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.kind, r.namespace, r.name, a.value
           FROM resource_annotations a
           JOIN resources r ON r.id = a.resource_id
           JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE a.key = ? AND a.custom = 1
           ORDER BY r.site, c.name, r.kind, r.namespace, r.name LIMIT ?""",
        (key, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- cluster page extras ----


def cluster_services(conn, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT r.namespace, r.name, json_extract(r.data, '$.spec.type') AS type,
                  (SELECT group_concat(a.address, ', ') FROM addresses a
                   WHERE a.resource_id = r.id AND a.role IN ('lb', 'external-ip')) AS addresses
           FROM resources r WHERE r.site=? AND r.cluster_id=? AND r.kind='services'
           ORDER BY r.namespace, r.name""",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


def cluster_ingresses(conn, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT r.namespace, r.name,
                  (SELECT group_concat(a.address, ', ') FROM addresses a
                   WHERE a.resource_id = r.id AND a.role = 'ingress-host') AS hosts,
                  (SELECT group_concat(a.address, ', ') FROM addresses a
                   WHERE a.resource_id = r.id AND a.role = 'ingress-lb') AS lbs
           FROM resources r WHERE r.site=? AND r.cluster_id=? AND r.kind='ingresses'
           ORDER BY r.namespace, r.name""",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


def cluster_helm(conn, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT r.namespace, r.name, {_HELM_CHART} AS chart, {_HELM_VERSION} AS version,
                   json_extract(r.data, '$.spec.info.status') AS status
            FROM resources r WHERE r.site=? AND r.cluster_id=? AND r.kind='helmreleases'
            ORDER BY r.namespace, r.name""",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


def cluster_crd_groups(conn, site: str, cluster_id: str, rules) -> list[dict]:
    from inventory.collector.classify import is_system_crd_group

    rows = conn.execute(
        """SELECT json_extract(r.data, '$.spec.group') AS grp, COUNT(*) AS crds
           FROM resources r WHERE r.site=? AND r.cluster_id=? AND r.kind='customresourcedefinitions'
           GROUP BY grp ORDER BY grp""",
        (site, cluster_id),
    ).fetchall()
    return [
        {"group": r["grp"], "crds": r["crds"]}
        for r in rows
        if r["grp"] and not is_system_crd_group(r["grp"], rules)
    ]


# ---- workloads, storage and namespaces pages (spec 7, discovery fields) ----

PAGE_LIMIT = 5000  # rows per discovery page list, like the /query cap

_BUILTIN_OWNERS = "('ReplicaSet', 'Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob', 'ReplicationController')"

WORKLOAD_FILTERS = {
    "privileged": "json_extract(r.data, '$.privileged') > 0",
    "host_network": "json_extract(r.data, '$.host_network') = 1",
    "hostpath": "json_extract(r.data, '$.hostpath') > 0",
    "no_requests": "json_extract(r.data, '$.no_requests') > 0",
    "no_limits": "json_extract(r.data, '$.no_limits') > 0",
    "no_probes": "json_extract(r.data, '$.no_probes') > 0",
    "operator_owned": f"json_extract(r.data, '$.owner_kind') NOT IN {_BUILTIN_OWNERS}",
    "gpu": "json_extract(r.data, '$.gpu') > 0",
}

_READY = """CASE r.kind
    WHEN 'daemonsets' THEN json_extract(r.data, '$.status.numberReady')
    WHEN 'jobs' THEN json_extract(r.data, '$.status.active')
    WHEN 'cronjobs' THEN NULL
    ELSE json_extract(r.data, '$.status.readyReplicas') END"""
_DESIRED = """CASE r.kind
    WHEN 'daemonsets' THEN json_extract(r.data, '$.status.desiredNumberScheduled')
    WHEN 'jobs' THEN json_extract(r.data, '$.spec.parallelism')
    WHEN 'cronjobs' THEN NULL
    ELSE json_extract(r.data, '$.spec.replicas') END"""


def workloads(conn, flags: list[str], name: str = "", limit: int = PAGE_LIMIT) -> list[dict]:
    """Workloads of every podspec kind. flags are keys of WORKLOAD_FILTERS, all must hold."""
    bad = set(flags) - set(WORKLOAD_FILTERS)
    if bad:
        raise ValueError(f"unknown filter: {', '.join(sorted(bad))}")
    parts = ["r.kind IN ('deployments', 'statefulsets', 'daemonsets', 'jobs', 'cronjobs', 'replicationcontrollers')"]
    parts += [WORKLOAD_FILTERS[f] for f in flags]  # SQL comes from the fixed map above
    params: dict = {"limit": limit}
    if name:
        parts.append("r.name LIKE '%' || :name || '%' ESCAPE '\\'")
        params["name"] = _like_escape(name)
    rows = conn.execute(
        f"""SELECT r.site, c.name AS cluster, r.cluster_id, r.kind, r.namespace, r.name,
                   {_READY} AS ready, {_DESIRED} AS desired,
                   json_extract(r.data, '$.containers') AS containers,
                   json_extract(r.data, '$.cpu_req_m') AS cpu_req_m, json_extract(r.data, '$.mem_req_b') AS mem_req_b,
                   json_extract(r.data, '$.cpu_lim_m') AS cpu_lim_m, json_extract(r.data, '$.mem_lim_b') AS mem_lim_b,
                   json_extract(r.data, '$.gpu') AS gpu, json_extract(r.data, '$.privileged') AS privileged,
                   json_extract(r.data, '$.host_network') AS host_network,
                   json_extract(r.data, '$.hostpath') AS hostpath,
                   json_extract(r.data, '$.no_requests') AS no_requests,
                   json_extract(r.data, '$.no_limits') AS no_limits,
                   json_extract(r.data, '$.no_probes') AS no_probes,
                   json_extract(r.data, '$.owner_kind') AS owner_kind,
                   json_extract(r.data, '$.owner_api_version') AS owner_api_version
            FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
            WHERE {" AND ".join(parts)}
            ORDER BY r.site, c.name, r.namespace, r.kind, r.name LIMIT :limit""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


STORAGE_FLAGS = {
    "rwx": "EXISTS (SELECT 1 FROM json_each(r.data, '$.access_modes') WHERE value = 'ReadWriteMany')",
    "block": "json_extract(r.data, '$.volume_mode') = 'Block'",
    "unmounted": "json_extract(r.data, '$.mounted') = 0",
}


def pvcs(conn, flags: list[str], source: str = "", storage_class: str = "", limit: int = PAGE_LIMIT) -> list[dict]:
    bad = set(flags) - set(STORAGE_FLAGS)
    if bad:
        raise ValueError(f"unknown filter: {', '.join(sorted(bad))}")
    parts = ["r.kind = 'persistentvolumeclaims'"] + [STORAGE_FLAGS[f] for f in flags]
    params: dict = {"limit": limit}
    if source:
        parts.append("json_extract(pv.data, '$.source') = :source")
        params["source"] = source
    if storage_class:
        parts.append("json_extract(r.data, '$.spec.storageClassName') = :storage_class")
        params["storage_class"] = storage_class
    rows = conn.execute(
        f"""SELECT r.site, c.name AS cluster, r.cluster_id, r.namespace, r.name,
                   json_extract(r.data, '$.spec.storageClassName') AS storage_class,
                   (SELECT group_concat(value, ',') FROM json_each(r.data, '$.access_modes')) AS access_modes,
                   json_extract(r.data, '$.volume_mode') AS volume_mode,
                   json_extract(r.data, '$.spec.resources.requests.storage') AS requested,
                   json_extract(r.data, '$.status.capacity.storage') AS capacity,
                   json_extract(r.data, '$.status.phase') AS phase,
                   json_extract(r.data, '$.mounted') AS mounted,
                   (SELECT group_concat(value, ',') FROM json_each(r.data, '$.used_by')) AS used_by,
                   json_extract(pv.data, '$.source') AS source
            FROM resources r
            JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
            LEFT JOIN resources pv ON pv.site = r.site AND pv.cluster_id = r.cluster_id
                 AND pv.kind = 'persistentvolumes' AND pv.namespace = ''
                 AND pv.name = json_extract(r.data, '$.volume_name')
            WHERE {" AND ".join(parts)}
            ORDER BY r.site, c.name, r.namespace, r.name LIMIT :limit""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def pvs(conn, limit: int = PAGE_LIMIT) -> list[dict]:
    rows = conn.execute(
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.name,
                  json_extract(r.data, '$.spec.capacity.storage') AS capacity,
                  json_extract(r.data, '$.spec.storageClassName') AS storage_class,
                  json_extract(r.data, '$.source') AS source, json_extract(r.data, '$.pinned') AS pinned,
                  json_extract(r.data, '$.reclaim') AS reclaim, json_extract(r.data, '$.status.phase') AS phase,
                  json_extract(r.data, '$.spec.claimRef.namespace') AS claim_namespace,
                  json_extract(r.data, '$.spec.claimRef.name') AS claim_name
           FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE r.kind = 'persistentvolumes' ORDER BY r.site, c.name, r.name LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def storage_options(conn) -> dict:
    return {
        "sources": [r[0] for r in conn.execute(
            "SELECT DISTINCT json_extract(data, '$.source') AS s FROM resources"
            " WHERE kind='persistentvolumes' AND s IS NOT NULL ORDER BY s")],
        "storage_classes": [r[0] for r in conn.execute(
            "SELECT DISTINCT json_extract(data, '$.spec.storageClassName') AS s FROM resources"
            " WHERE kind='persistentvolumeclaims' AND s IS NOT NULL ORDER BY s")],
    }  # fmt: skip


def snapshot_counts(conn) -> list[dict]:
    """Per cluster: volume snapshot count, or None when snapshots are not installed or not listed."""
    rows = conn.execute(
        """SELECT c.site, c.name AS cluster, c.cluster_id,
                  (SELECT rk.status FROM run_kinds rk WHERE rk.site = c.site AND rk.cluster_id = c.cluster_id
                   AND rk.kind = 'volumesnapshots' ORDER BY rk.run_id DESC LIMIT 1) AS status,
                  (SELECT count(*) FROM resources r WHERE r.site = c.site AND r.cluster_id = c.cluster_id
                   AND r.kind = 'volumesnapshots') AS snapshots
           FROM clusters c ORDER BY c.site, c.name"""
    ).fetchall()
    return [dict(r) for r in rows]


NAMESPACE_SORTS = ("cpu_req_m", "cpu_used_m", "mem_req_b", "mem_used_b", "restarts")


def namespace_rollup(conn, sort: str = "", limit: int = PAGE_LIMIT) -> list[dict]:
    if sort and sort not in NAMESPACE_SORTS:
        raise ValueError(f"unknown sort: {sort}")
    order = f"coalesce(p.{sort}, -1) DESC, " if sort else ""  # column comes from the fixed list above
    rows = conn.execute(
        f"""SELECT p.*, c.name AS cluster FROM namespace_profile p
            JOIN clusters c ON c.site = p.site AND c.cluster_id = p.cluster_id
            ORDER BY {order}p.site, c.name, p.namespace LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- platform page (spec 6.11) ----


def _json_rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def webhooks(conn, limit: int = PAGE_LIMIT) -> list[dict]:
    rows = _json_rows(
        conn,
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.kind, r.name, r.data
           FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE r.kind IN ('mutatingwebhookconfigurations', 'validatingwebhookconfigurations')
           ORDER BY r.site, c.name, r.kind, r.name LIMIT ?""",
        (limit,),
    )
    for r in rows:
        r.update(json.loads(r.pop("data")))
        r["type"] = "mutating" if r["kind"].startswith("mutating") else "validating"
    return rows


def apiservices(conn, limit: int = PAGE_LIMIT) -> list[dict]:
    return _json_rows(
        conn,
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.name,
                  json_extract(r.data, '$.service_namespace') AS service_namespace,
                  json_extract(r.data, '$.available') AS available
           FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE r.kind = 'apiservices' ORDER BY r.site, c.name, r.name LIMIT ?""",
        (limit,),
    )


def custom_clusterroles(conn, limit: int = PAGE_LIMIT) -> list[dict]:
    """ClusterRoles that are neither Rancher-generated nor Kubernetes or platform roles."""
    return _json_rows(
        conn,
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.name,
                  json_extract(r.data, '$.rule_count') AS rule_count,
                  json_extract(r.data, '$.aggregated') AS aggregated,
                  json_extract(r.data, '$.helm_managed') AS helm_managed
           FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE r.kind = 'clusterroles' AND json_extract(r.data, '$.rancher_generated') = 0
             AND json_extract(r.data, '$.platform') = 0
           ORDER BY r.site, c.name, r.name LIMIT ?""",
        (limit,),
    )


def is_system_binding(name: str, namespace: str, role: str, subjects: list) -> bool:
    """The script's rule for system bindings."""
    return (
        name.startswith("system:")
        or role.startswith("system:")
        or (name == "cluster-admin" and subjects == [["Group", "system:masters"]])
        or namespace.startswith("kube-")
    )


def cluster_admin_bindings(conn) -> list[dict]:
    """ClusterRoleBindings to cluster-admin that are not system and not Rancher-generated."""
    rows = _json_rows(
        conn,
        """SELECT r.site, c.name AS cluster, r.cluster_id, r.namespace, r.name, r.data
           FROM resources r JOIN clusters c ON c.site = r.site AND c.cluster_id = r.cluster_id
           WHERE r.kind = 'clusterrolebindings' AND json_extract(r.data, '$.role[1]') = 'cluster-admin'
             AND json_extract(r.data, '$.rancher_generated') = 0
           ORDER BY r.site, c.name, r.name""",
    )
    out = []
    for r in rows:
        d = json.loads(r.pop("data"))
        if is_system_binding(r["name"], r["namespace"], d["role"][1] or "", d["subjects"]):
            continue
        r["subjects"] = ", ".join(f"{k}={n}" for k, n in d["subjects"])
        out.append(r)
    return out


def api_version_spread(conn) -> list[dict]:
    """Group versions served and in use, with the number of clusters for each."""
    return _json_rows(
        conn,
        """SELECT a.group_version, a.source, group_concat(DISTINCT a.kind) AS kinds,
                  COUNT(DISTINCT a.site || '/' || a.cluster_id) AS clusters
           FROM api_versions a JOIN clusters c ON c.site = a.site AND c.cluster_id = a.cluster_id
           GROUP BY a.group_version, a.source ORDER BY a.source DESC, a.group_version""",
    )
