"""Edit migration tracking: one cluster from its page, or many from the cluster list."""

import logging
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from inventory.web import tracking
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


@router.post("/migration/bulk", dependencies=[Depends(check_csrf)])
def save_bulk(
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
    if not status and new_wave is None:
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
            changed += tracking.save(conn, user.id, site, cluster_id, name, status=status or None, wave=new_wave)
    except BaseException as e:
        conn.execute("ROLLBACK")
        if isinstance(e, ValueError):
            raise HTTPException(400, str(e)) from None
        raise
    conn.execute("COMMIT")
    log.info("tracking bulk edit of %d clusters (%d changed) by user=%r", len(targets), changed, user.username)
    return RedirectResponse(safe_next(next), 303)
