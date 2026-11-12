import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, read_db, render, wants_csv

router = APIRouter()


@router.get("/check")
def check_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    selected = rows = error = None
    raw = request.query_params.get("preset", "").strip()
    if raw:
        if not raw.isdigit():
            error = f"{raw!r} is not a preset id. Pick a preset from the list."
        else:
            selected = queries.get_preset(conn, int(raw))
            if selected is None:
                raise HTTPException(404, "preset not found")
            try:
                rows = queries.check_preset(conn, selected["id"])
            except ValueError as e:  # corrupt JSON or a filter this version does not know
                error = f"invalid preset {selected['name']!r}: {e}"
    if wants_csv(request):
        return csv_download(request, user, "check", {"results": rows or []},
                            columns={"results": ["site", "cluster_id", "name", "result", "reason"]})
    return render(
        request, conn, user, "check.html", presets=queries.list_presets(conn), selected=selected, rows=rows,
        error=error,
    )
