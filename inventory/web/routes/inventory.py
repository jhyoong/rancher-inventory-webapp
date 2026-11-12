import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, read_db, render, wants_csv

router = APIRouter()


@router.get("/inventory")
def inventory(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params
    f = {k: q.get(k, "").strip() for k in ("site", "cls", "flag", "label")}
    try:
        rows = queries.inventory_rows(conn, **{k: v or None for k, v in f.items()})
    except ValueError as e:
        raise HTTPException(400, str(e))
    if wants_csv(request):
        return csv_download(request, user, "inventory", {"inventory": rows})
    return render(
        request, conn, user, "inventory.html", rows=rows, f=f, options=queries.inventory_filter_options(conn)
    )
