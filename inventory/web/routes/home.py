import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from inventory.web import custom_columns, migration, queries, tracking
from inventory.web.auth import User
from inventory.collector.optional import active
from inventory.collector.rules import load_rules
from inventory.web.deps import current_user, get_settings, read_db, render

router = APIRouter()


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params
    site = q.get("site", "").strip()
    rules = load_rules(get_settings(request))
    scope = q.get("scope") if q.get("scope") in ("remaining", "all") else "remaining"
    data = migration.site_overview(conn, rules, site=site or None, scope=scope)
    progress = tracking.progress(conn, site or None)
    removed = tracking.removed(conn, site or None)
    keep = ([("site", site)] if site else [])
    scope_links = [
        {"label": label, "href": "/?" + urlencode(keep + [("scope", key)]), "active": key == scope}
        for key, label in (("remaining", "Remaining clusters"), ("all", "All clusters"))
    ]
    kept = keep + ([("scope", "all")] if scope == "all" else [])
    removed_next = "/" + ("?" + urlencode(kept) if kept else "") + "#removed"
    return render(
        request, conn, user, "home.html", overview=queries.overview(conn),
        site=site, scope=scope, scope_links=scope_links, data=data, progress=progress, removed=removed, removed_next=removed_next, custom_columns=custom_columns.list_columns(conn), chart_data={**data["charts"], "progress": progress},
        site_names=queries.site_names(conn), optins=active(rules),
    )  # fmt: skip
