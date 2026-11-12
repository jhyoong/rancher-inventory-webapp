import sqlite3

from fastapi import APIRouter, Depends, Request

from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import current_user, read_db, render

router = APIRouter()


@router.get("/network")
def network(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    q = request.query_params.get("q", "").strip()
    return render(
        request,
        conn,
        user,
        "network.html",
        q=q,
        results=queries.search_addresses(conn, q),
        load_balancers=queries.load_balancers(conn),
        ingress_hosts=queries.ingress_hosts(conn),
        node_ips=queries.node_ips(conn),
        services=queries.nodeport_and_external_services(conn),
        duplicates=queries.duplicate_addresses(conn),
    )
