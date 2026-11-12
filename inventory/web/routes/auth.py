import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from inventory.web import auth
from inventory.web.deps import COOKIE, check_csrf, get_settings, optional_user, read_db, render, write_db

log = logging.getLogger("inventory.web")
router = APIRouter()


@router.get("/login")
def login_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, conn, None, "login.html", error=None)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    conn: sqlite3.Connection = Depends(write_db),
):
    settings = get_settings(request)
    try:
        token = auth.login(conn, username, password, settings.session_hours, datetime.now(timezone.utc))
    except auth.LoginLocked:
        log.warning("login refused, account locked: user=%s", username)
        return render(request, conn, None, "login.html", 429, error="Too many failed logins, try again later")
    if token is None:
        log.warning("login failed: user=%s", username)
        return render(request, conn, None, "login.html", 401, error="Wrong username or password")
    log.info("login: user=%s", username)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return resp


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, conn: sqlite3.Connection = Depends(write_db)):
    auth.logout(conn, request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
