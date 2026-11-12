import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.collector.rules import load_rules
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, get_settings, read_db, render

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
    rules = load_rules(get_settings(request))
    return render(
        request,
        conn,
        user,
        "cluster.html",
        c=row,
        metrics=metrics,
        p=queries.cluster_inventory(conn, site, cluster_id),
        namespaces=queries.namespace_profiles(conn, site, cluster_id),
        services=queries.cluster_services(conn, site, cluster_id),
        ingresses=queries.cluster_ingresses(conn, site, cluster_id),
        helm=queries.cluster_helm(conn, site, cluster_id),
        crd_groups=queries.cluster_crd_groups(conn, site, cluster_id, rules),
    )
