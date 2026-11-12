import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/check")
def check_page(
    request: Request,
    preset: int | None = None,
    conn: sqlite3.Connection = Depends(read_db),
    user: User = Depends(current_user),
):
    selected = rows = None
    if preset is not None:
        selected = queries.get_preset(conn, preset)
        if selected is None:
            raise HTTPException(404, "preset not found")
        rows = queries.check_preset(conn, preset)
    return render(
        request, conn, user, "check.html", presets=queries.list_presets(conn), selected=selected, rows=rows
    )
