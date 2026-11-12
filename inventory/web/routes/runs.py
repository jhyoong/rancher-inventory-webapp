import logging
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from inventory.collector.runner import RunInProgress, Scope, request_cancel, retry_scope
from inventory.web import queries
from inventory.web.auth import User
from inventory.web.deps import check_csrf, current_user, read_db, render, require_admin, write_db

log = logging.getLogger("inventory.web")
router = APIRouter()


@router.get("/runs")
def runs(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return render(request, conn, user, "runs.html", runs=queries.runs(conn))


@router.get("/runs/progress")
def progress(request: Request, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)):
    return render(request, conn, user, "_progress.html")


@router.get("/runs/{run_id}")
def run_page(
    request: Request, run_id: int, conn: sqlite3.Connection = Depends(read_db), user: User = Depends(current_user)
):
    run = queries.run(conn, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    rows = queries.run_clusters(conn, run_id)
    return render(
        request,
        conn,
        user,
        "run.html",
        run=run,
        rows=rows,
        totals=queries.site_totals(rows),
        duration=queries.duration(run["started_at"], run["finished_at"]),
        any_failed=any(r["status"] == "failed" for r in rows),
    )


def _start(request: Request, conn: sqlite3.Connection, user: User, scope: Scope):
    """Start a run through the collector thread; answer for a form post or an HTMX button."""
    active = queries.active_run(conn)
    if active is not None:
        raise HTTPException(409, f"run {active['id']} is in progress")
    collector = request.app.state.collector
    if collector is None:
        raise HTTPException(503, "collector is not running")
    try:
        run_id = collector.request_run("manual", user.username, scope)
    except RunInProgress as e:
        raise HTTPException(409, f"run {e.run_id} is in progress") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError:  # collector is stopping
        raise HTTPException(503, "collector is not running") from None
    log.info("run %s started by user=%s scope=%s", run_id, user.username, scope.to_json())
    if request.headers.get("HX-Request"):
        return render(request, conn, user, "_progress.html")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.post("/runs", dependencies=[Depends(check_csrf)])
def start_run(
    request: Request,
    site: str = Form(""),
    cluster_id: str = Form(""),
    conn: sqlite3.Connection = Depends(write_db),
    user: User = Depends(require_admin),
):
    if cluster_id and not site:
        raise HTTPException(400, "cluster_id needs site")
    return _start(request, conn, user, Scope(site=site or None, cluster_id=cluster_id or None))


@router.post("/runs/{run_id}/retry", dependencies=[Depends(check_csrf)])
def retry_run(
    request: Request, run_id: int, conn: sqlite3.Connection = Depends(write_db), user: User = Depends(require_admin)
):
    if queries.run(conn, run_id) is None:
        raise HTTPException(404, "run not found")
    return _start(request, conn, user, retry_scope(conn, run_id))


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(check_csrf)])
def cancel_run(
    request: Request, run_id: int, conn: sqlite3.Connection = Depends(write_db), user: User = Depends(require_admin)
):
    run = queries.run(conn, run_id)
    if run is None or run["status"] != "running":
        return render(request, conn, user, "runs.html", runs=queries.runs(conn), message=f"Run {run_id} is not running.")
    request_cancel(conn, run_id)
    log.info("cancel of run %s requested by user=%s", run_id, user.username)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)
