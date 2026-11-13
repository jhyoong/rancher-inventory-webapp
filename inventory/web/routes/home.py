import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import migration, queries
from inventory.web.auth import User
from inventory.collector.optional import active
from inventory.collector.rules import load_rules
from inventory.web.deps import current_user, get_settings, read_db, render
from inventory.web.routes.clusters import VIEWS

router = APIRouter()


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params
    site = q.get("site", "").strip()
    view = q.get("view") if q.get("view") in VIEWS else "table"
    rules = load_rules(get_settings(request))
    return render(
        request, conn, user, "home.html", overview=queries.overview(conn),
        site=site, view=view, data=migration.site_overview(conn, rules, site=site or None),
        site_names=queries.site_names(conn), optins=active(rules),
    )  # fmt: skip
