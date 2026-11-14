"""Manual migration tracking: status, wave and note per cluster, with a change log."""

import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

STATUSES = ("not started", "planned", "in progress", "migrated", "decommissioned", "won't migrate")
REMAINING = STATUSES[:3]
DONE = ("migrated", "decommissioned")
FINISHED = (*DONE, "won't migrate")
WAVE_MAX = 100
NOTE_MAX = 2000
NO_WAVE = "no wave"

_DEFAULT = {"status": STATUSES[0], "wave": "", "note": "", "updated_by_name": None, "updated_at": None}

_SELECT = """
    SELECT t.site, t.cluster_id, t.status, t.wave, t.note, t.updated_at, u.username AS updated_by_name
    FROM migration_tracking t LEFT JOIN users u ON u.id = t.updated_by
"""


def _shape(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in ("status", "wave", "note", "updated_by_name", "updated_at")}


def get(conn: sqlite3.Connection, site: str, cluster_id: str) -> dict:
    row = conn.execute(_SELECT + " WHERE t.site=? AND t.cluster_id=?", (site, cluster_id)).fetchone()
    return _shape(row) if row else dict(_DEFAULT)


def current(conn: sqlite3.Connection, site: str | None = None) -> dict[tuple[str, str], dict]:
    sql, params = _SELECT, ()
    if site is not None:
        sql, params = sql + " WHERE t.site=?", (site,)
    return {(r["site"], r["cluster_id"]): _shape(r) for r in conn.execute(sql, params)}


def history(conn: sqlite3.Connection, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT l.id, l.name, l.status, l.wave, l.note, l.updated_at, u.username AS updated_by_name
           FROM migration_tracking_log l LEFT JOIN users u ON u.id = l.updated_by
           WHERE l.site=? AND l.cluster_id=? ORDER BY l.updated_at DESC, l.id DESC""",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


def save(
    conn: sqlite3.Connection,
    user_id: int,
    site: str,
    cluster_id: str,
    name: str,
    status: str | None = None,
    wave: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Save the new state. None keeps the current value, "" clears wave or note.

    Returns False and writes nothing when the result equals the current state.
    """
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    wave = wave.strip() if wave is not None else None
    note = note.strip() if note is not None else None
    if wave is not None and len(wave) > WAVE_MAX:
        raise ValueError(f"wave is longer than {WAVE_MAX} characters")
    if note is not None and len(note) > NOTE_MAX:
        raise ValueError(f"note is longer than {NOTE_MAX} characters")

    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        cur = get(conn, site, cluster_id)
        new = {
            "status": cur["status"] if status is None else status,
            "wave": cur["wave"] if wave is None else wave,
            "note": cur["note"] if note is None else note,
        }
        exists = conn.execute(
            "SELECT 1 FROM migration_tracking WHERE site=? AND cluster_id=?", (site, cluster_id)
        ).fetchone()
        if exists and all(new[k] == cur[k] for k in new):
            changed = False
        elif not exists and new == {"status": _DEFAULT["status"], "wave": "", "note": ""}:
            changed = False
        else:
            changed = True
            ts = (now or datetime.now(timezone.utc)).isoformat()
            vals = (site, cluster_id, name, new["status"], new["wave"], new["note"], user_id, ts)
            conn.execute(
                """INSERT INTO migration_tracking
                   (site, cluster_id, name, status, wave, note, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(site, cluster_id) DO UPDATE SET name=excluded.name,
                   status=excluded.status, wave=excluded.wave, note=excluded.note,
                   updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                vals,
            )
            conn.execute(
                """INSERT INTO migration_tracking_log
                   (site, cluster_id, name, status, wave, note, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                vals,
            )
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    if own_tx:
        conn.execute("COMMIT")
    return changed


def removed(conn: sqlite3.Connection, site: str | None = None) -> list[dict]:
    """Clusters that left Rancher, newest removal first, with their tracking state."""
    sql, params = "SELECT site, cluster_id, name, removed_at FROM removed_clusters", ()
    if site is not None:
        sql, params = sql + " WHERE site=?", (site,)
    rows = conn.execute(sql + " ORDER BY removed_at DESC, site, cluster_id", params).fetchall()
    tracked = current(conn, site)
    out = []
    for r in rows:
        t = tracked.get((r["site"], r["cluster_id"]), _DEFAULT)
        out.append({
            "site": r["site"], "cluster_id": r["cluster_id"], "name": r["name"],
            "removed_at": r["removed_at"], "status": t["status"], "wave": t["wave"],
            "note": t["note"], "done": t["status"] in FINISHED,
        })
    return out


def progress(conn: sqlite3.Connection, site: str | None = None) -> dict:
    """Counts by status, site and wave.

    Clusters are the current ones plus those removed from Rancher.
    """
    where, params = ("WHERE site=?", (site,)) if site is not None else ("", ())
    keys = {
        (r["site"], r["cluster_id"])
        for table in ("clusters", "removed_clusters")
        for r in conn.execute(f"SELECT site, cluster_id FROM {table} {where}", params)
    }
    tracked = current(conn, site)
    zero = {s: 0 for s in STATUSES}
    by_site: dict[str, dict] = {}
    by_wave: dict[str, dict] = {}
    totals = dict(zero)
    for key in keys:
        t = tracked.get(key, _DEFAULT)
        totals[t["status"]] += 1
        by_site.setdefault(key[0], dict(zero))[t["status"]] += 1
        by_wave.setdefault(t["wave"] or NO_WAVE, dict(zero))[t["status"]] += 1
    total = len(keys)
    suffix = f"&site={quote(site)}" if site is not None else ""
    waves = sorted(w for w in by_wave if w != NO_WAVE) + ([NO_WAVE] if NO_WAVE in by_wave else [])
    return {
        "total": total,
        "by_status": [
            {"status": s, "count": totals[s], "pct": round(100 * totals[s] / total, 1) if total else 0,
             "href": f"/clusters?status={quote(s)}{suffix}"}
            for s in STATUSES
        ],
        "by_site": [{"site": k, "counts": by_site[k]} for k in sorted(by_site)],
        "by_wave": [{"wave": w, "counts": by_wave[w]} for w in waves],
    }
