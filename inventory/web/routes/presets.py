import json
import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import check_csrf, current_user, read_db, render, write_db

log = logging.getLogger("inventory.web")
router = APIRouter()


@router.get("/presets")
def presets_page(
    request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)
):
    rows = [{**dict(p), "filters": json.loads(p["filters"])} for p in queries.list_presets(conn)]
    return render(request, conn, user, "presets.html", presets=rows)


@router.post("/presets", dependencies=[Depends(check_csrf)])
def save_preset(
    name: str = Form(""),
    filters: str = Form("{}"),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(current_user),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        data = json.loads(filters)
        if not isinstance(data, dict):
            raise ValueError("filters must be an object")
        data = {k: str(v) for k, v in data.items() if str(v).strip()}
        queries._where(data)  # validates keys
    except ValueError as e:
        raise HTTPException(400, f"bad filters: {e}") from None
    if not data:
        raise HTTPException(400, "filters are empty")
    try:
        conn.execute(
            "INSERT INTO presets (name, filters, created_by, created_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(data, sort_keys=True), user.id, datetime.now(timezone.utc).isoformat()),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "a preset with this name already exists") from None
    log.info("preset %r saved by user=%s", name, user.username)
    return RedirectResponse("/presets", status_code=303)


@router.post("/presets/{preset_id}/delete", dependencies=[Depends(check_csrf)])
def delete_preset(
    preset_id: int, conn: sqlite3.Connection = Depends(write_db), user: User = Depends(current_user)
):
    p = queries.get_preset(conn, preset_id)
    if p is None:
        raise HTTPException(404, "preset not found")
    if user.role != "admin" and p["created_by"] != user.id:
        raise HTTPException(403, "only the creator or an admin can delete a preset")
    conn.execute("DELETE FROM presets WHERE id=?", (preset_id,))
    log.info("preset %s deleted by user=%s", preset_id, user.username)
    return RedirectResponse("/presets", status_code=303)
