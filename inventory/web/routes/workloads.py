import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, flag_params, read_db, render, wants_csv

router = APIRouter()


@router.get("/workloads")
def workloads(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    flags = flag_params(request, queries.WORKLOAD_FILTERS)
    name = request.query_params.get("name", "").strip()
    rows = queries.workloads(conn, flags, name)
    if wants_csv(request):
        return csv_download(request, user, "workloads", {"workloads": rows})
    return render(
        request, conn, user, "workloads.html", rows=rows, flags=flags, name_filter=name,
        all_flags=list(queries.WORKLOAD_FILTERS), max_rows=queries.PAGE_LIMIT,
    )
