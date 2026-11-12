import logging
import sqlite3

from fastapi import APIRouter, Depends, Form, Request

from inventory.web import auth
from inventory.web.auth import User
from inventory.web.deps import check_csrf, read_db, render, require_admin, write_db

log = logging.getLogger("inventory.web")
router = APIRouter()


def _page(request: Request, conn: sqlite3.Connection, user: User, message: str | None = None, error: str | None = None):
    return render(
        request, conn, user, "admin_users.html", users=auth.list_users(conn), message=message, error=error
    )


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
        elif action in ("disable", "enable"):
            if action == "disable" and username == user.username:
                raise ValueError("you cannot disable your own account")
            auth.set_disabled(conn, username, action == "disable")
            message = f"{username} {action}d"
        else:
            raise ValueError("unknown action")
    except ValueError as e:
        return _page(request, conn, user, error=str(e))
    log.info("admin user=%s: %s %s", user.username, action, username)
    return _page(request, conn, user, message=message)
