import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import cap_rows, csv_download, current_user, flag_params, read_db, render, wants_csv

router = APIRouter()


@router.get("/storage")
def storage(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    flags = flag_params(request, queries.STORAGE_FLAGS)
    source = request.query_params.get("source", "").strip()
    storage_class = request.query_params.get("storage_class", "").strip()
    tables = {
        "pvcs": queries.pvcs(conn, flags, source, storage_class, limit=queries.PAGE_LIMIT + 1),
        "pvs": queries.pvs(conn),
        "snapshots": queries.snapshot_counts(conn),
    }
    capped = cap_rows(tables, queries.PAGE_LIMIT)
    if wants_csv(request):
        return csv_download(request, user, "storage", tables)
    return render(
        request, conn, user, "storage.html", **tables, options=queries.storage_options(conn),
        flags=flags, source=source, storage_class=storage_class,
        all_flags=list(queries.STORAGE_FLAGS), max_rows=queries.PAGE_LIMIT, capped=capped,
    )  # fmt: skip
