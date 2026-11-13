import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.collector.rules import load_rules
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import cap_rows, csv_download, current_user, get_settings, read_db, render, wants_csv

router = APIRouter()

ANNOTATION_LIMIT = 5000  # rows in the objects list, like the /query cap


@router.get("/platform")
def platform(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    chart = request.query_params.get("chart", "").strip()
    key = request.query_params.get("key", "").strip()
    custom_only = request.query_params.get("custom_only") == "1"
    tables = {
        "webhooks": queries.webhooks(conn),
        "apiservices": queries.apiservices(conn),
        "roles": queries.custom_clusterroles(conn),
        "admin_bindings": queries.cluster_admin_bindings(conn),
        "api_versions": queries.api_version_spread(conn),
        "charts": queries.helm_charts(conn),
    }
    if chart:
        tables["releases"] = queries.helm_releases(conn, chart)
    tables["groups"] = queries.crd_groups(conn, custom_only, load_rules(get_settings(request)))
    tables["keys"] = queries.annotation_keys(conn)
    if key:
        tables["objects"] = queries.annotation_objects(conn, key, limit=ANNOTATION_LIMIT + 1)
    capped = cap_rows({"objects": tables.get("objects")}, ANNOTATION_LIMIT)
    if wants_csv(request):
        return csv_download(request, user, "platform", tables)
    return render(
        request, conn, user, "platform.html", **{"releases": None, "objects": None} | tables, chart=chart, key=key,
        custom_only=custom_only, max_rows=ANNOTATION_LIMIT, capped=capped,
    )
