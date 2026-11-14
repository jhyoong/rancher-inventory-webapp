"""Request dependencies: DB connections, current user, admin check, CSRF and page rendering."""

import hmac
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from inventory.db.connection import connect
from inventory.settings import Settings
from inventory.web.auth import User, csrf_token, session_user
from inventory.web import tracking
from inventory.web.migration import FLAG_ANCHORS
from inventory.web.queries import header_context

COOKIE = "session"
log = logging.getLogger("inventory.web")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _dt(value: datetime | str | None) -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _cores(m: int | None) -> str:
    return "-" if m is None else f"{m / 1000:.1f}"


def _gib(b: int | None) -> str:
    return "-" if b is None else f"{b / 2**30:.1f}"


def _secs(s: float | None) -> str:
    return "-" if s is None else f"{s:.1f} s"


def _size(b: int | None) -> str:
    if b is None:
        return "-"
    value = float(b)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


templates.env.filters.update(dt=_dt, cores=_cores, gib=_gib, secs=_secs, size=_size)
templates.env.globals["flag_anchors"] = FLAG_ANCHORS
templates.env.globals.update(statuses=tracking.STATUSES, wave_max=tracking.WAVE_MAX, note_max=tracking.NOTE_MAX)
templates.env.globals["csv_url"] = lambda request, table: str(
    request.url.include_query_params(format="csv", table=table)
)


def set_session_cookie(resp, token: str, settings: Settings) -> None:
    resp.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def flag_params(request: Request, allowed) -> list[str]:
    """Names from `allowed` that are set (non-empty) in the query string."""
    return [f for f in allowed if request.query_params.get(f)]


def wants_csv(request: Request) -> bool:
    return request.query_params.get("format") == "csv"


def csv_download(request: Request, user: User, page: str, tables: dict[str, list], columns: dict | None = None):
    """CSV of one page table: ?table=<name>, default the first. Each download is logged."""
    from inventory.export.csv import csv_response

    name = request.query_params.get("table") or next(iter(tables))
    if name not in tables:
        raise HTTPException(400, f"unknown table: {name}")
    log.info("csv download user=%r path=%s table=%s", user.username, request.url.path, name)
    return csv_response(tables[name] or [], f"{page}-{name}.csv", (columns or {}).get(name))


def legacy_redirect(request: Request, target: str, csv_table: str) -> RedirectResponse:
    """307 from a retired page to its new place, keeping the query string.

    A bare CSV link (format=csv without table) gets table=csv_table, so the old CSV URL returns the same table.
    """
    query = request.url.query
    if wants_csv(request) and "table" not in request.query_params:
        query += ("&" if query else "") + f"table={csv_table}"
    return RedirectResponse(target + (f"?{query}" if query else ""), status_code=307)


def cap_rows(tables: dict[str, list | None], limit: int) -> set[str]:
    """Cut each list fetched with limit + 1 rows back to limit; return the names that had more."""
    capped = {name for name, rows in tables.items() if rows is not None and len(rows) > limit}
    for name in capped:
        del tables[name][limit:]
    return capped


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def read_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(get_settings(request).db_path, readonly=True)
    try:
        yield conn
    finally:
        conn.close()


def write_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(get_settings(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


def optional_user(request: Request, conn: sqlite3.Connection = Depends(read_db)) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    return session_user(conn, token, datetime.now(timezone.utc))


def current_user(request: Request, user: User | None = Depends(optional_user)) -> User:
    if user is not None:
        return user
    if request.headers.get("HX-Request"):
        # HTMX follows HX-Redirect with a full page load instead of swapping an error in.
        raise HTTPException(401, "login required", headers={"HX-Redirect": "/login"})
    if request.url.path.startswith("/api/"):
        raise HTTPException(401, "login required")
    raise HTTPException(303, headers={"Location": "/login"})


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


async def check_csrf(request: Request, user: User = Depends(current_user)) -> None:
    form = await request.form()
    sent = form.get("csrf")
    token = request.cookies.get(COOKIE, "")
    expected = csrf_token(get_settings(request).session_secret, token)
    if not isinstance(sent, str) or not hmac.compare_digest(sent, expected):
        raise HTTPException(403, "bad or missing CSRF token")


def render(
    request: Request,
    conn: sqlite3.Connection,
    user: User | None,
    name: str,
    status_code: int = 200,
    session_token: str | None = None,
    **ctx,
):
    """session_token overrides the request cookie, for a response that sets a new session."""
    settings = get_settings(request)
    token = session_token or request.cookies.get(COOKIE)
    ctx.update(
        user=user,
        csrf=csrf_token(settings.session_secret, token) if user and token else "",
        header=header_context(conn, settings),
        settings=settings,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
