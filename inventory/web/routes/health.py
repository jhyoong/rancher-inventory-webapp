import sqlite3

from fastapi import APIRouter, Request

from inventory.db.connection import connect
from inventory.web import queries
from inventory.web.deps import get_settings, optional_user

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """ok: the DB opens and the last good run is newer than STALE_AFTER_HOURS."""
    settings = get_settings(request)
    try:
        conn = connect(settings.db_path, readonly=True)
    except sqlite3.Error:
        return {"ok": False}
    try:
        as_of = queries.data_as_of(conn)
        ok = not queries.is_stale(as_of, settings)
        if optional_user(request, conn) is None:
            return {"ok": ok}
        return {
            "ok": ok,
            "data_as_of": as_of.isoformat() if as_of else None,
            "active_run": queries.active_run(conn),
        }
    except sqlite3.Error:
        return {"ok": False}
    finally:
        conn.close()
