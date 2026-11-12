"""JSON API (spec 7). Needs a session like every page; errors are JSON."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.db.connection import QueryTimeout
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db
from inventory.web.routes.export import query_filters
from inventory.web.routes.query import run_filters

router = APIRouter()


@router.get("/api/clusters")
def api_clusters(conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return [dict(r) for r in queries.clusters(conn)]


@router.get("/api/resources")
def api_resources(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    filters = query_filters(request)
    try:
        rows, truncated = run_filters(conn, filters)
    except QueryTimeout as e:
        raise HTTPException(503, str(e)) from None
    return {"rows": rows, "truncated": truncated}
