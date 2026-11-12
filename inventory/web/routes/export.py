"""CSV exports of clusters and resources (spec 7). The discovery export is added in Task 8."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.db.connection import QueryTimeout
from inventory.export.csv import csv_response
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, log, read_db
from inventory.web.routes.query import FORM_FIELDS, run_filters

router = APIRouter()


def query_filters(request: Request) -> dict[str, str]:
    """Query string filters; a key outside the allowed filters is a 400."""
    filters = {k: v.strip() for k, v in request.query_params.items() if v.strip()}
    try:
        queries.validate_filters(filters)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {k: v for k, v in filters.items() if k in FORM_FIELDS}


@router.get("/export/clusters.csv")
def clusters_csv(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    log.info("csv download user=%r path=%s", user.username, request.url.path)
    return csv_response(queries.clusters(conn), "clusters.csv")


@router.get("/export/resources.csv")
def resources_csv(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    filters = query_filters(request)
    try:
        rows, _ = run_filters(conn, filters)
    except QueryTimeout as e:
        raise HTTPException(503, str(e)) from None
    log.info("csv download user=%r path=%s filters=%r", user.username, request.url.path, filters)
    return csv_response(rows, "resources.csv", ["site", "cluster_id", "cluster_name", "kind", "namespace", "name"])
