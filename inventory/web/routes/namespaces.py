import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/namespaces")
def namespaces(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    sort = request.query_params.get("sort", "").strip()
    try:
        rows = queries.namespace_rollup(conn, sort)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return render(
        request, conn, user, "namespaces.html", rows=rows, sort=sort,
        sorts=queries.NAMESPACE_SORTS, max_rows=queries.PAGE_LIMIT,
    )
