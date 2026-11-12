import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/network")
def network(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params.get("q", "").strip()
    limit = queries.NETWORK_LIMIT
    return render(
        request,
        conn,
        user,
        "network.html",
        q=q,
        max_rows=limit,
        results=queries.search_addresses(conn, q, limit),
        load_balancers=queries.load_balancers(conn, limit),
        ingress_hosts=queries.ingress_hosts(conn, limit),
        node_ips=queries.node_ips(conn, limit),
        services=queries.nodeport_and_external_services(conn, limit),
        duplicates=queries.duplicate_addresses(conn, limit),
    )
