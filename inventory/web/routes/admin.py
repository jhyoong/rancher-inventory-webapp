import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request

from inventory.web import auth
from inventory.web.auth import User
from inventory.web.deps import (
    check_csrf,
    get_settings,
    read_db,
    render,
    require_admin,
    set_session_cookie,
    write_db,
)

log = logging.getLogger("inventory.web")
router = APIRouter()


def _page(request: Request, conn: sqlite3.Connection, user: User, message: str | None = None, error: str | None = None):
    return render(
        request, conn, user, "admin_users.html", users=auth.list_users(conn), message=message, error=error
    )


def _own_reset(request: Request, conn: sqlite3.Connection, user: User, password: str):
    """The reset ended every session of this admin; start a fresh one for this browser."""
    settings = get_settings(request)
    token = auth.login(conn, user.username, password, settings.session_hours, datetime.now(timezone.utc))
    log.info("admin user=%r: reset own password", user.username)
    resp = render(
        request, conn, user, "admin_users.html", session_token=token, users=auth.list_users(conn),
        message="Your password was changed. You have a new session.", error=None,
    )
    set_session_cookie(resp, token, settings)
    return resp


@router.get("/admin/users")
def users_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(require_admin)):
    return _page(request, conn, user)


@router.post("/admin/users", dependencies=[Depends(check_csrf)])
def users_action(
    request: Request,
    action: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("viewer"),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(require_admin),
):
    try:
        if action == "add":
            auth.create_user(conn, username.strip(), password, role)
            message = f"Added {username}"
        elif action == "reset":
            auth.set_password(conn, username, password)
            message = f"Password reset for {username}"
            if auth.normalize_username(username) == user.username:
                return _own_reset(request, conn, user, password)
        elif action in ("disable", "enable"):
            if action == "disable" and username == user.username:
                raise ValueError("you cannot disable your own account")
            auth.set_disabled(conn, username, action == "disable")
            message = f"{username} {action}d"
        else:
            raise ValueError("unknown action")
    except ValueError as e:
        return _page(request, conn, user, error=str(e))
    log.info("admin user=%r: %s %r", user.username, action, username)
    return _page(request, conn, user, message=message)
