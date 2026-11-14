"""Manual migration tracking: status, wave and note per cluster, with a change log."""

import sqlite3
from datetime import datetime, timezone

STATUSES = ("not started", "planned", "in progress", "migrated", "decommissioned", "won't migrate")
REMAINING = STATUSES[:3]
DONE = ("migrated", "decommissioned")
FINISHED = (*DONE, "won't migrate")
WAVE_MAX = 100
NOTE_MAX = 2000

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
