import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import rancher_users
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/rancher-users")
def rancher_users_list(
    request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)
):
    f = {k: request.query_params.get(k, "").strip() for k in ("q", "site")}
    return render(
        request, conn, user, "rancher_users.html", f=f, sites=rancher_users.sites(conn),
        users=rancher_users.users(conn, f["q"], f["site"]),
    )  # fmt: skip


@router.get("/rancher-users/user")
def rancher_user(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params
    found = rancher_users.user(conn, q.get("p", ""), q.get("site", ""), q.get("id", ""))
    if found is None:
        raise HTTPException(404, "Rancher user not found")
    return render(request, conn, user, "rancher_user.html", u=found)
