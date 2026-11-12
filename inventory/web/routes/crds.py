import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.collector.rules import load_rules
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, get_settings, read_db, render, wants_csv

router = APIRouter()


@router.get("/crds")
def crds(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    custom_only = request.query_params.get("custom_only") == "1"
    rules = load_rules(get_settings(request))
    groups = queries.crd_groups(conn, custom_only, rules)
    if wants_csv(request):
        return csv_download(request, user, "crds", {"groups": groups})
    return render(request, conn, user, "crds.html", groups=groups, custom_only=custom_only)
