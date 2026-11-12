"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from inventory.collector.thread import CollectorThread
from inventory.db.connection import connect
from inventory.db.migrate import migrate
from inventory.settings import Settings
from inventory.web.auth import ensure_first_admin
from inventory.web.routes import admin, auth, clusters, health, home, runs

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
    app.state.settings = settings
    app.state.collector = collector
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for module in (auth, home, clusters, runs, admin, health):
        app.include_router(module.router)
    return app
