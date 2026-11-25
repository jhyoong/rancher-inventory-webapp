import logging
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from inventory.web import custom_columns as cc
from inventory.web.auth import User
from inventory.web.deps import check_csrf, read_db, render, require_admin, write_db

log = logging.getLogger("inventory.web")
router = APIRouter()


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _page(request: Request, conn: sqlite3.Connection, user: User, error=None, in_use=None, unmatched=None, status_code: int = 200):
    rows = []
    for col in cc.list_columns(conn):
        u = cc.usage(conn, col.id)
        rows.append({"col": col, "live": u["live"], "removed": len(u["removed"])})
    return render(request, conn, user, "admin_columns.html", status_code=status_code,
                  rows=rows, error=error, in_use=in_use, unmatched=unmatched)


@router.get("/admin/columns")
def columns_page(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(require_admin)):
    return _page(request, conn, user)


@router.post("/admin/columns", dependencies=[Depends(check_csrf)])
def columns_action(
    request: Request,
    action: str = Form(""),
    column_id: int = Form(0),
    name: str = Form(""),
    type: str = Form(""),
    choices: str = Form(""),
    direction: str = Form(""),
    old: str = Form(""),
    new: str = Form(""),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(require_admin),
):
    try:
        if action == "add":
            cc.create_column(conn, name, type, [] if type == "text" else _lines(choices))
        elif action == "rename":
            cc.rename_column(conn, column_id, name)
        elif action == "move":
            cc.move_column(conn, column_id, direction)
        elif action == "choices":
            cc.set_choices(conn, column_id, _lines(choices))
        elif action == "rename_choice":
            cc.rename_choice(conn, user.id, column_id, old, new)
        elif action == "type":
            cc.change_type(conn, user.id, column_id, type, [] if type == "text" else _lines(choices))
        else:
            raise ValueError("unknown action")
    except cc.Unmatched as e:
        return _page(request, conn, user, error=str(e), unmatched={"column_id": column_id, "values_": e.items},
                     status_code=400)
    except cc.InUse as e:
        return _page(request, conn, user, error=str(e), in_use={"column_id": column_id, "exc": e}, status_code=400)
    except ValueError as e:
        return _page(request, conn, user, error=str(e), status_code=400)
    log.info("admin user=%r: columns %s column=%s", user.username, action, column_id)
    return RedirectResponse("/admin/columns", status_code=303)


@router.get("/admin/columns/{column_id}/delete")
def delete_page(
    column_id: int, request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(require_admin)
):
    col = cc.get_column(conn, column_id)
    if col is None:
        return RedirectResponse("/admin/columns", status_code=303)
    u = cc.usage(conn, column_id)
    return render(request, conn, user, "admin_column_delete.html", col=col, live=u["live"], removed=len(u["removed"]))


@router.post("/admin/columns/{column_id}/delete", dependencies=[Depends(check_csrf)])
def delete_action(
    column_id: int,
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(require_admin),
):
    cc.delete_column(conn, column_id)
    log.info("admin user=%r: deleted column %s", user.username, column_id)
    return RedirectResponse("/admin/columns", status_code=303)
