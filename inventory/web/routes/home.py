import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return render(request, conn, user, "home.html", overview=queries.overview(conn))
