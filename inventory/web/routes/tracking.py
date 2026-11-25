"""Edit migration tracking: one cluster from its page, or many from the cluster list."""

import logging
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from inventory.web import custom_columns, tracking
from inventory.web.auth import User
from inventory.web.deps import check_csrf, current_user, write_db

log = logging.getLogger("inventory.web")
router = APIRouter()


def cluster_name(conn: sqlite3.Connection, site: str, cluster_id: str) -> str | None:
    """Name from clusters, else from removed_clusters; None when the cluster is in neither."""
    for table in ("clusters", "removed_clusters"):
        row = conn.execute(f"SELECT name FROM {table} WHERE site=? AND cluster_id=?", (site, cluster_id)).fetchone()
        if row is not None:
            return row["name"]
    return None


def safe_next(target: str) -> str:
    """A path on this app, else "/". Rejects //host and /\\host, which browsers treat as another host."""
    if target.startswith("/") and not target.startswith("//") and not target.startswith("/\\"):
        return target
    return "/"


@router.post("/clusters/{site}/{cluster_id}/migration", dependencies=[Depends(check_csrf)])
def save_one(
    site: str,
    cluster_id: str,
    status: str = Form(""),
    wave: str = Form(""),
    note: str = Form(""),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(current_user),
):
    name = cluster_name(conn, site, cluster_id)
    if name is None:
        raise HTTPException(404, "cluster not found")
    try:
        changed = tracking.save(conn, user.id, site, cluster_id, name, status=status or None, wave=wave, note=note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if changed:
        log.info("tracking %s/%s saved by user=%r", site, cluster_id, user.username)
    return RedirectResponse(f"/clusters/{quote(site, safe='')}/{quote(cluster_id, safe='')}#migration", 303)


@router.post("/clusters/{site}/{cluster_id}/custom", dependencies=[Depends(check_csrf)])
async def save_custom(
    request: Request,
    site: str,
    cluster_id: str,
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(current_user),
):
    name = cluster_name(conn, site, cluster_id)
    if name is None:
        raise HTTPException(404, "cluster not found")
    form = await request.form()
    known = {c.id for c in custom_columns.list_columns(conn)}
    changes: dict[int, str | None] = {}
    for key, value in form.items():
        cid = custom_columns.column_id_from_key(key)
        if cid in known and isinstance(value, str):
            changes[cid] = value
    try:
        changed = custom_columns.save_values(conn, user.id, site, cluster_id, name, changes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if changed:
        log.info("custom fields %s/%s saved by user=%r (%d changed)", site, cluster_id, user.username, changed)
    return RedirectResponse(f"/clusters/{quote(site, safe='')}/{quote(cluster_id, safe='')}#custom", 303)


@router.post("/migration/bulk", dependencies=[Depends(check_csrf)])
async def save_bulk(
    request: Request,
    cluster: list[str] = Form([]),
    status: str = Form(""),
    wave: str = Form(""),
    clear_wave: str = Form(""),
    next: str = Form("/"),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(current_user),
):
    # validate everything before the first write
    if not cluster:
        raise HTTPException(400, "no clusters selected")
    if status and status not in tracking.STATUSES:
        raise HTTPException(400, f"unknown status: {status!r}")
    wave = wave.strip()
    if clear_wave and wave:
        raise HTTPException(400, "choose either a wave or Clear wave, not both")
    if len(wave) > tracking.WAVE_MAX:
        raise HTTPException(400, f"wave is longer than {tracking.WAVE_MAX} characters")
    new_wave = "" if clear_wave else (wave or None)
    form = await request.form()
    custom: dict[int, str | None] = {}
    for col in custom_columns.list_columns(conn):
        raw = form.get(f"cf{col.id}")
        raw = raw.strip() if isinstance(raw, str) else ""
        if col.type == "choice":
            custom[col.id] = "" if raw.lower() == "none" else (raw or None)
        else:
            clear = bool(form.get(f"cf{col.id}_clear"))
            if clear and raw:
                raise HTTPException(400, f"choose either a value or Clear for {col.name!r}, not both")
            custom[col.id] = "" if clear else (raw or None)
        if custom[col.id]:
            try:
                custom[col.id] = custom_columns.normalize(col, custom[col.id])
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
    custom = {cid: v for cid, v in custom.items() if v is not None}
    if not status and new_wave is None and not custom:
        raise HTTPException(400, "nothing to change")
    targets = []
    for value in dict.fromkeys(cluster):
        site, sep, cluster_id = value.rpartition("/")
        name = cluster_name(conn, site, cluster_id) if sep and site and cluster_id else None
        if name is None:
            raise HTTPException(400, f"unknown cluster: {value!r}")
        targets.append((site, cluster_id, name))

    changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for site, cluster_id, name in targets:
            if status or new_wave is not None:
                changed += tracking.save(conn, user.id, site, cluster_id, name, status=status or None, wave=new_wave)
            if custom:
                changed += custom_columns.save_values(conn, user.id, site, cluster_id, name, custom)
    except BaseException as e:
        conn.execute("ROLLBACK")
        if isinstance(e, ValueError):
            raise HTTPException(400, str(e)) from None
        raise
    conn.execute("COMMIT")
    log.info("tracking bulk edit of %d clusters (%d changed) by user=%r", len(targets), changed, user.username)
    return RedirectResponse(safe_next(next), 303)
