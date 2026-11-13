import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from inventory.collector.rules import load_rules
from inventory.web import migration, queries
from inventory.web.auth import User
from inventory.web.deps import csv_download, current_user, get_settings, read_db, render, wants_csv

router = APIRouter()

VIEWS = ("table", "cards")


@router.get("/clusters")
def clusters(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params
    f = {k: q.get(k, "").strip() for k in ("site", "cls", "flag", "label")}
    view = q.get("view") if q.get("view") in VIEWS else "table"
    rules = load_rules(get_settings(request))
    try:
        rows = migration.cluster_rows(conn, rules, **{k: v or None for k, v in f.items()})
    except ValueError as e:
        raise HTTPException(400, str(e))
    if wants_csv(request):
        flat = [migration.flat_row(r) for r in rows]
        # "inventory" is the table name of the old /inventory CSV link
        return csv_download(request, user, "clusters", {"clusters": flat, "inventory": flat})
    options = {**queries.inventory_filter_options(conn), "flags": list(migration.ALL_FLAGS)}
    return render(
        request, conn, user, "clusters.html", rows=rows, f=f, view=view, options=options,
        site_names=queries.site_names(conn),
    )  # fmt: skip


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
    sheet = migration.cluster_sheet(conn, rules, site, cluster_id)
    # CSV and page tables in page order
    tables = {
        **{k: sheet["tables"][k] for k in ("nodes", "pools", "lb_services", "services")},
        "ingresses": queries.cluster_ingresses(conn, site, cluster_id),
        **{k: sheet["tables"][k] for k in ("pvs", "pvcs", "databases", "statefulsets")},
        "helm": queries.cluster_helm(conn, site, cluster_id),
        "crd_groups": queries.cluster_crd_groups(conn, site, cluster_id, rules),
        "webhooks": sheet["tables"]["webhooks"],
        "namespaces": queries.namespace_profiles(conn, site, cluster_id),
        "annotations": sheet["tables"]["annotations"],
        "metrics": metrics,
        "cluster_labels": sheet["tables"]["cluster_labels"],
    }
    if wants_csv(request):
        return csv_download(request, user, f"cluster-{site}-{cluster_id}", tables)
    return render(
        request, conn, user, "cluster.html", c=row, p=queries.cluster_inventory(conn, site, cluster_id),
        flags=sheet["flags"], floating=sheet["floating"], pools_status=sheet["pools_status"], groups=sheet["groups"],
        **tables,
    )  # fmt: skip
