import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/clusters")
def clusters(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return render(request, conn, user, "clusters.html", clusters=queries.clusters(conn))


@router.get("/clusters/{site}/{cluster_id}")
def cluster(
    request: Request,
    site: str,
    cluster_id: str,
    conn: sqlite3.Connection = Depends(read_db),
    user: User = Depends(current_user),
):
    row = queries.cluster(conn, site, cluster_id)
    if row is None:
        raise HTTPException(404, "cluster not found")
    metrics = queries.cluster_metrics(conn, site, cluster_id)
    return render(request, conn, user, "cluster.html", c=row, metrics=metrics)
