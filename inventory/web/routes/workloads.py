import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import cap_rows, csv_download, current_user, flag_params, read_db, render, wants_csv

router = APIRouter()


@router.get("/workloads")
def workloads(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    flags = flag_params(request, queries.WORKLOAD_FILTERS)
    name = request.query_params.get("name", "").strip()
    rows = queries.workloads(conn, flags, name, limit=queries.PAGE_LIMIT + 1)
    sort = request.query_params.get("sort", "").strip()
    try:
        namespaces = queries.namespace_rollup(conn, sort, limit=queries.PAGE_LIMIT + 1)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    capped = cap_rows({"workloads": rows, "namespaces": namespaces}, queries.PAGE_LIMIT)
    if wants_csv(request):
        return csv_download(request, user, "workloads", {"workloads": rows, "namespaces": namespaces})
    return render(
        request, conn, user, "workloads.html", rows=rows, flags=flags, name_filter=name, namespaces=namespaces,
        sort=sort, sorts=queries.NAMESPACE_SORTS, all_flags=list(queries.WORKLOAD_FILTERS),
        max_rows=queries.PAGE_LIMIT, capped=capped,
    )
