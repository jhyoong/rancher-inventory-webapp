import sqlite3
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from inventory.web import migration, queries, tracking
from inventory.web.auth import User
from inventory.collector.optional import active
from inventory.collector.rules import load_rules
from inventory.web.deps import current_user, get_settings, read_db, render
from inventory.web.routes.clusters import VIEWS

router = APIRouter()


def _day(value: str | None) -> str:
    """The date part of an ISO timestamp, or "not available"."""
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except (TypeError, ValueError):
        return "not available"


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params
    site = q.get("site", "").strip()
    view = q.get("view") if q.get("view") in VIEWS else "table"
    rules = load_rules(get_settings(request))
    scope = q.get("scope") if q.get("scope") in ("remaining", "all") else "remaining"
    data = migration.site_overview(conn, rules, site=site or None, scope=scope)
    progress = tracking.progress(conn, site or None)
    removed = tracking.removed(conn, site or None)
    params = ([("site", site)] if site else []) + ([("view", q["view"])] if q.get("view") in VIEWS else [])
    keep = ([("site", site)] if site else []) + ([("view", view)] if view != "table" else [])
    scope_links = [
        {"label": label, "href": "/?" + urlencode(keep + [("scope", key)]), "active": key == scope}
        for key, label in (("remaining", "Remaining clusters"), ("all", "All clusters"))
    ]
    removed_next = "/" + ("?" + urlencode(params) if params else "") + "#removed"
    for r in removed:
        r["removed_day"] = _day(r["removed_at"])
    return render(
        request, conn, user, "home.html", overview=queries.overview(conn),
        site=site, view=view, scope_links=scope_links, data=data, progress=progress, removed=removed, removed_next=removed_next, chart_data={**data["charts"], "progress": progress},
        site_names=queries.site_names(conn), optins=active(rules),
    )  # fmt: skip
