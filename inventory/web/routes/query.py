import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.db.connection import QueryTimeout, run_readonly_query
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, read_db, render, wants_csv

router = APIRouter()
MAX_ROWS = 5000
FORM_FIELDS = (
    "site", "cluster", "kind", "namespace", "name", "label", "image",
    "annotation", "service_type", "address", "helm_chart", "crd_group", "class", "flag",
)  # fmt: skip
PLACEHOLDERS = {
    "label": "key or key=value", "annotation": "key or key=value", "address": "IP, prefix or hostname",
    "helm_chart": "chart or chart@version", "service_type": "LoadBalancer", "class": "stateful-helm",
    "flag": "hostpath",
}  # fmt: skip


def run_filters(conn: sqlite3.Connection, filters: dict[str, str]) -> tuple[list[dict], bool]:
    """Rows for the filters, at most MAX_ROWS, and whether more rows exist. Raises ValueError, QueryTimeout."""
    sql, params = queries.build_query(filters)
    rows = run_readonly_query(conn, sql, params, timeout_s=10.0, max_rows=MAX_ROWS + 1)
    return [dict(r) for r in rows[:MAX_ROWS]], len(rows) > MAX_ROWS


@router.get("/query")
def query_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    filters = {k: request.query_params.get(k, "").strip() for k in FORM_FIELDS}
    filters = {k: v for k, v in filters.items() if v}
    ctx = dict(
        filters=filters, fields=FORM_FIELDS, placeholders=PLACEHOLDERS, rows=None, error=None, capped=False,
        max_rows=MAX_ROWS,
    )  # fmt: skip
    status = 200
    if filters:
        try:
            rows, capped = run_filters(conn, filters)
            ctx.update(rows=rows, capped=capped)
        except QueryTimeout:
            ctx["error"] = "The query took longer than 10 seconds. Add more filters and try again."
        except ValueError as e:
            ctx["error"] = str(e)
            status = 400
    if wants_csv(request) and status == 200:
        return csv_download(request, user, "query", {"results": ctx["rows"] or []})
    return render(request, conn, user, "query.html", status_code=status, **ctx)
