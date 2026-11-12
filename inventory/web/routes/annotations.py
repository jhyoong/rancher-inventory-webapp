import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, read_db, render, wants_csv

router = APIRouter()

ANNOTATION_LIMIT = 5000  # rows in the objects list, like the /query cap


@router.get("/annotations")
def annotations(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    key = request.query_params.get("key", "").strip()
    objects = queries.annotation_objects(conn, key, limit=ANNOTATION_LIMIT) if key else None
    keys = queries.annotation_keys(conn)
    if wants_csv(request):
        tables = {"keys": keys} | ({"objects": objects} if objects is not None else {})
        return csv_download(request, user, "annotations", tables)
    return render(
        request, conn, user, "annotations.html", keys=keys, key=key, objects=objects, max_rows=ANNOTATION_LIMIT
    )
