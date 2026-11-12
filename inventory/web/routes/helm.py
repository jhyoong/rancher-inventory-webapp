import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, read_db, render, wants_csv

router = APIRouter()


@router.get("/helm")
def helm(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    chart = request.query_params.get("chart", "").strip()
    releases = queries.helm_releases(conn, chart) if chart else None
    charts = queries.helm_charts(conn)
    if wants_csv(request):
        tables = {"charts": charts} | ({"releases": releases} if releases is not None else {})
        return csv_download(request, user, "helm", tables)
    return render(request, conn, user, "helm.html", charts=charts, chart=chart, releases=releases)
