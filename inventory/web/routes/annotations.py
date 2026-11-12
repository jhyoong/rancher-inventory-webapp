import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/annotations")
def annotations(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    key = request.query_params.get("key", "").strip()
    objects = queries.annotation_objects(conn, key) if key else None
    return render(
        request, conn, user, "annotations.html", keys=queries.annotation_keys(conn), key=key, objects=objects
    )
