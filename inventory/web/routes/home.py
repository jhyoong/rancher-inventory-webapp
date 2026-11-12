import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.collector.optional import active
from inventory.collector.rules import load_rules
from inventory.web.deps import current_user, get_settings, read_db, render

router = APIRouter()


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return render(
        request, conn, user, "home.html", overview=queries.overview(conn),
        classes=queries.clusters_per_class(conn), site_names=queries.site_names(conn),
        optins=active(load_rules(get_settings(request))),
    )
