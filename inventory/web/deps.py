"""Request dependencies: DB connections, current user, admin check, CSRF and page rendering."""

import hmac
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from inventory.db.connection import connect
from inventory.settings import Settings
from inventory.web.auth import User, csrf_token, session_user
from inventory.web.queries import header_context

COOKIE = "session"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _dt(value: datetime | str | None) -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
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
    if request.headers.get("HX-Request") or request.url.path.startswith("/api/"):
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
    **ctx,
):
    settings = get_settings(request)
    token = request.cookies.get(COOKIE)
    ctx.update(
        user=user,
        csrf=csrf_token(settings.session_secret, token) if user and token else "",
        header=header_context(conn, settings),
        settings=settings,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
