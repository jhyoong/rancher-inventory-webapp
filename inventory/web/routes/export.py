"""CSV exports of clusters and resources, and the anonymised discovery export (spec 7)."""

import sqlite3
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from inventory.collector.runner import Scope
from inventory.db.connection import QueryTimeout
from inventory.export.csv import csv_response
from inventory.export.discovery import ExportBlocked, ExportOptions, build_export, name_map_csv
from inventory.settings import ConfigError
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import check_csrf, current_user, get_settings, log, read_db, render, require_admin, write_db
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


def _export_page(request, conn, user, status_code=200, **ctx):
    sites = queries.site_names(conn)
    clusters = [dict(c) for c in queries.clusters(conn)]
    return render(
        request, conn, user, "export.html", status_code=status_code, sites=sites, clusters=clusters,
        salt_set=bool(get_settings(request).export_salt), hits=None, error=None, **ctx,
    )  # fmt: skip


@router.get("/export/discovery")
def discovery_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(require_admin)):
    return _export_page(request, conn, user)


@router.post("/export/discovery", dependencies=[Depends(check_csrf)])
def discovery_download(
    request: Request,
    site: str = Form(""),
    clusters: list[str] = Form([]),
    strict: str = Form(""),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(require_admin),
):
    settings = get_settings(request)
    if not settings.export_salt:
        raise HTTPException(400, "EXPORT_SALT is not set. Add it to .env and restart; keep the same salt for every export.")
    pairs = [tuple(c.split("/", 1)) for c in clusters if "/" in c] or None
    opts = ExportOptions(Scope(site=site or None, clusters=pairs), strict=bool(strict))
    tmp = Path(tempfile.mkdtemp(prefix="export-", dir=settings.data_dir))
    try:
        path = build_export(conn, settings, opts, tmp)
    except ExportBlocked as e:
        _rmtree(tmp)
        log.warning("discovery export blocked by self-check: user=%r hits=%d", user.username, len(e.hits))
        return _export_page(request, conn, user, status_code=422, hits=e.hits)
    except (ConfigError, ValueError) as e:
        _rmtree(tmp)
        raise HTTPException(400, str(e)) from None
    log.info("discovery export by user=%r scope=%s strict=%s", user.username, opts.scope.to_json(), opts.strict)
    return FileResponse(path, media_type="application/gzip", filename=path.name, background=BackgroundTask(_rmtree, tmp))


@router.get("/export/discovery/name-map.csv")
def name_map(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(require_admin)):
    settings = get_settings(request)
    if not settings.export_salt:
        raise HTTPException(400, "EXPORT_SALT is not set")
    log.info("name map download user=%r", user.username)
    return Response(
        name_map_csv(conn, settings), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="export-name-map.csv"'},
    )


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
