import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.db.connection import QueryTimeout, run_readonly_query
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()
MAX_ROWS = 5000
FORM_FIELDS = ("site", "cluster", "kind", "namespace", "name", "label", "image")


@router.get("/query")
def query_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    filters = {k: request.query_params.get(k, "").strip() for k in FORM_FIELDS}
    filters = {k: v for k, v in filters.items() if v}
    ctx = dict(filters=filters, fields=FORM_FIELDS, rows=None, error=None, capped=False, max_rows=MAX_ROWS)
    status = 200
    if filters:
        try:
            sql, params = queries.build_query(filters)
            rows = run_readonly_query(conn, sql, params, timeout_s=10.0, max_rows=MAX_ROWS)
            ctx.update(rows=rows, capped=len(rows) >= MAX_ROWS)
        except QueryTimeout:
            ctx["error"] = "The query took longer than 10 seconds. Add more filters and try again."
        except ValueError as e:
            ctx["error"] = str(e)
            status = 400
    return render(request, conn, user, "query.html", status_code=status, **ctx)
