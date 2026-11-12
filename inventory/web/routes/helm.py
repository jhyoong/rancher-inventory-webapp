import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/helm")
def helm(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    chart = request.query_params.get("chart", "").strip()
    releases = queries.helm_releases(conn, chart) if chart else None
    return render(request, conn, user, "helm.html", charts=queries.helm_charts(conn), chart=chart, releases=releases)
