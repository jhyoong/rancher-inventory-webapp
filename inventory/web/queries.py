"""Read queries shared by the web pages."""

import sqlite3
from datetime import datetime, timedelta, timezone

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


def header_context(conn: sqlite3.Connection, settings: Settings) -> dict:
    as_of = data_as_of(conn)
    return {"data_as_of": as_of, "stale": is_stale(as_of, settings), "active_run": active_run(conn)}


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

ALLOWED_FILTERS = {"site", "cluster", "kind", "namespace", "name", "label", "image"}


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
            parts.append("r.name LIKE '%' || :name || '%'")
        elif key == "image":
            parts.append(
                "EXISTS (SELECT 1 FROM resource_images i WHERE i.resource_id = r.id"
                " AND i.image LIKE '%' || :image || '%')"
            )
        elif key == "label":
            lkey, sep, lvalue = value.partition("=")
            params["label_key"] = lkey
            cond = "l.resource_id = r.id AND l.key = :label_key"
            if sep:
                params["label_value"] = lvalue
                cond += " AND l.value = :label_value"
            parts.append(f"EXISTS (SELECT 1 FROM resource_labels l WHERE {cond})")
            continue
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
    import json

    preset = get_preset(conn, preset_id)
    if preset is None:
        raise ValueError("preset not found")
    filters = json.loads(preset["filters"])
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
