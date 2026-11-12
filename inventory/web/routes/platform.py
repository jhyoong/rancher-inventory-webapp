import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/platform")
def platform(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return render(
        request, conn, user, "platform.html",
        webhooks=queries.webhooks(conn), apiservices=queries.apiservices(conn),
        roles=queries.custom_clusterroles(conn), admin_bindings=queries.cluster_admin_bindings(conn),
        api_versions=queries.api_version_spread(conn), max_rows=queries.PAGE_LIMIT,
    )  # fmt: skip
