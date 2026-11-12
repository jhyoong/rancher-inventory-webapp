"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from inventory.collector.thread import CollectorThread
from inventory.db.connection import connect
from inventory.db.migrate import migrate
from inventory.settings import Settings
from inventory.web.auth import ensure_first_admin
from inventory.web.routes import (
    admin,
    annotations,
    auth,
    check,
    clusters,
    crds,
    health,
    helm,
    home,
    inventory,
    namespaces,
    network,
    platform,
    presets,
    query,
    runs,
    storage,
    workloads,
)

log = logging.getLogger("inventory.web")


def _prepare_db(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        if ensure_first_admin(conn, settings):
            log.info("created first admin user=%s", settings.admin_user)
    finally:
        conn.close()


ERROR_TITLES = {
    400: "Bad request",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    500: "Server error",
    503: "Service unavailable",
}


def _wants_html(request: Request) -> bool:
    path = request.url.path
    return not (path == "/healthz" or path.startswith("/api/") or request.headers.get("HX-Request"))


def _error_page(request: Request, status: int, detail: str):
    from inventory.web.deps import templates

    ctx = {"user": None, "title": ERROR_TITLES[status], "detail": detail}
    return templates.TemplateResponse(request, "error.html", ctx, status_code=status)


async def _http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code in ERROR_TITLES and _wants_html(request):
        return _error_page(request, exc.status_code, str(exc.detail))
    return await http_exception_handler(request, exc)


async def _server_error(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse

    log.error("unhandled error on %s", request.url.path, exc_info=exc)
    if _wants_html(request):
        return _error_page(request, 500, "The server hit an error. The log has the details.")
    return JSONResponse({"detail": "server error"}, status_code=500)


def create_app(settings: Settings, collector: CollectorThread | None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _prepare_db(settings)
        try:
            yield
        finally:
            if collector is not None:
                log.info("stopping collector")
                collector.stop(timeout=60)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _server_error)

    @app.middleware("http")
    async def frame_options(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.state.settings = settings
    app.state.collector = collector
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for module in (
        auth, home, clusters, inventory, network, helm, crds, annotations, workloads, storage, namespaces, platform,
        runs, query, presets, check, admin, health,
    ):
        app.include_router(module.router)
    return app
