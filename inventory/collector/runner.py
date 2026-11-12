"""Runs: create, execute with progress, cancel, retry and interruption."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone

from inventory.collector.cluster import collect_cluster
from inventory.collector.kinds import KindSpec, load_kinds
from inventory.collector.rules import Rules, load_rules
from inventory.db.connection import connect
from inventory.db.repo import mark_cluster_failed, transaction
from inventory.rancher.client import RancherClient
from inventory.settings import ConfigError, Settings, Site, load_sites, site_token

log = logging.getLogger(__name__)

KEEP_RUNS = 30  # run_clusters and run_kinds rows are kept for this many runs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Scope:
    site: str | None = None
    cluster_id: str | None = None
    clusters: list[tuple[str, str]] | None = None

    def to_json(self) -> str:
        data: dict = {}
        if self.site is not None:
            data["site"] = self.site
        if self.cluster_id is not None:
            data["cluster_id"] = self.cluster_id
        if self.clusters is not None:
            data["clusters"] = [list(pair) for pair in self.clusters]
        return json.dumps(data)

    @classmethod
    def from_json(cls, text: str | None) -> "Scope":
        data = json.loads(text or "{}")
        clusters = data.get("clusters")
        return cls(
            site=data.get("site"),
            cluster_id=data.get("cluster_id"),
            clusters=[(s, c) for s, c in clusters] if clusters is not None else None,
        )

    def includes_site(self, site: str) -> bool:
        if self.site is not None and site != self.site:
            return False
        if self.clusters is not None and site not in {s for s, _ in self.clusters}:
            return False
        return True

    def includes(self, site: str, cluster_id: str) -> bool:
        if not self.includes_site(site):
            return False
        if self.cluster_id is not None and cluster_id != self.cluster_id:
            return False
        if self.clusters is not None and (site, cluster_id) not in self.clusters:
            return False
        return True


class RunInProgress(Exception):
    def __init__(self, run_id: int):
        super().__init__(f"run {run_id} is in progress")
        self.run_id = run_id


def create_run(conn: sqlite3.Connection, trigger: str, triggered_by: str | None, scope: Scope) -> int:
    if scope.clusters is not None and not scope.clusters:
        raise ValueError("scope has no clusters")
    with transaction(conn):
        row = conn.execute("SELECT id FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            raise RunInProgress(row[0])
        cur = conn.execute(
            "INSERT INTO runs (started_at, status, trigger, triggered_by, scope) VALUES (?, 'running', ?, ?, ?)",
            (_now(), trigger, triggered_by, scope.to_json()),
        )
        return cur.lastrowid


def request_cancel(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=? AND status='running'", (run_id,))


def retry_scope(conn: sqlite3.Connection, run_id: int) -> Scope:
    rows = conn.execute(
        "SELECT site, cluster_id FROM run_clusters WHERE run_id=? AND status='failed' ORDER BY site, cluster_id",
        (run_id,),
    ).fetchall()
    return Scope(clusters=[(r[0], r[1]) for r in rows])


def mark_interrupted(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        ids = [r[0] for r in conn.execute("SELECT id FROM runs WHERE status='running'").fetchall()]
        for rid in ids:
            conn.execute(
                "UPDATE run_clusters SET status='skipped' WHERE run_id=? AND status IN ('pending', 'running')",
                (rid,),
            )
        conn.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (_now(),))
    return len(ids)


def last_good_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Newest full-scope run that ended ok or partial. Scoped runs do not count."""
    return conn.execute(
        "SELECT * FROM runs WHERE status IN ('ok', 'partial') AND scope=? ORDER BY id DESC LIMIT 1",
        (Scope().to_json(),),
    ).fetchone()


def _scrub(text: str, token: str | None) -> str:
    if token and token in text:
        text = text.replace(token, "****")
    return text


def _final_status(cancelled: bool, stopped: bool, sites_total: int, site_errors: list[str], statuses: list[str]) -> str:
    if cancelled:
        return "cancelled"
    if stopped:
        return "interrupted"
    ok = statuses.count("ok")
    failed = statuses.count("failed")
    if sites_total and len(site_errors) == sites_total:
        return "failed"
    if not site_errors and failed == 0:
        return "ok"
    if ok == 0 and failed > 0:
        return "failed"
    return "partial"


async def execute_run(
    settings: Settings,
    run_id: int,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Execute a run created by create_run and return its final status.

    should_stop is for process shutdown: clusters in progress stop after their
    current kind, the rest are skipped and the run ends as 'interrupted'.
    """
    conn = connect(settings.db_path)
    try:
        try:
            return await _execute(settings, conn, run_id, should_stop or (lambda: False))
        except asyncio.CancelledError:
            log.warning("run %s was cancelled", run_id)
            _finish_safely(conn, run_id, "interrupted", "run task was cancelled")
            raise
        except Exception as e:
            log.exception("run %s failed", run_id)
            _finish_safely(conn, run_id, "failed", f"{type(e).__name__}: {e}")
            return "failed"
    finally:
        conn.close()


def _finish_safely(conn: sqlite3.Connection, run_id: int, status: str, error: str) -> None:
    """Best effort final update on an error path, so the run does not stay 'running'."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        _finish(conn, run_id, status, error)
    except Exception:
        log.exception("could not record final status %s for run %s", status, run_id)


def _finish(conn: sqlite3.Connection, run_id: int, status: str, error: str | None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE run_clusters SET status='skipped' WHERE run_id=? AND status IN ('pending', 'running')",
            (run_id,),
        )
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
            (status, _now(), error, run_id),
        )
        for table in ("run_clusters", "run_kinds"):
            conn.execute(
                f"DELETE FROM {table} WHERE run_id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
                (KEEP_RUNS,),
            )


async def _execute(settings: Settings, conn: sqlite3.Connection, run_id: int, should_stop: Callable[[], bool]) -> str:
    row = conn.execute("SELECT scope FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"run {run_id} not found")
    scope = Scope.from_json(row[0])
    kinds = load_kinds(settings)
    rules = load_rules(settings)
    sites = [s for s in load_sites(settings) if scope.includes_site(s.name)]

    def cancelled() -> bool:
        return bool(conn.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()[0])

    site_errors: list[str] = []
    statuses: list[str] = []
    async with AsyncExitStack() as stack:
        clients: dict[str, tuple[RancherClient, str]] = {}
        for site in sites:
            try:
                token = site_token(site)
            except ConfigError as e:
                site_errors.append(f"{site.name}: {e}")
                continue
            client = await stack.enter_async_context(RancherClient(site, token, settings.request_timeout_s))
            clients[site.name] = (client, token)

        async def list_site(site: Site) -> list[dict] | None:
            client, token = clients[site.name]
            try:
                return await client.list_clusters()
            except Exception as e:
                site_errors.append(f"{site.name}: " + _scrub(str(e) or type(e).__name__, token))
                return None

        listed_sites = [s for s in sites if s.name in clients]
        listed = await asyncio.gather(*(list_site(s) for s in listed_sites))
        work: list[tuple[Site, dict]] = []
        for site, clusters in zip(listed_sites, listed):
            for cluster in clusters or []:
                if scope.includes(site.name, cluster["id"]):
                    work.append((site, cluster))

        with transaction(conn):
            conn.executemany(
                "INSERT INTO run_clusters (run_id, site, cluster_id, status, kinds_total, kinds_done) "
                "VALUES (?, ?, ?, 'pending', ?, 0)",
                [(run_id, s.name, c["id"], len(kinds)) for s, c in work],
            )
            conn.execute("UPDATE runs SET clusters_total=? WHERE id=?", (len(work), run_id))

        sems = {name: asyncio.Semaphore(settings.concurrency_per_site) for name in clients}

        async def one(site: Site, cluster: dict) -> str:
            async with sems[site.name]:
                client, token = clients[site.name]
                return await _run_cluster(
                    conn, run_id, site.name, cluster, client, token, kinds, rules, settings,
                    cancelled, should_stop,
                )  # fmt: skip

        statuses = await asyncio.gather(*(one(s, c) for s, c in work))

    status = _final_status(cancelled(), should_stop(), len(sites), site_errors, list(statuses))
    _finish(conn, run_id, status, "; ".join(site_errors) or None)
    return status


async def _run_cluster(
    conn: sqlite3.Connection,
    run_id: int,
    site: str,
    cluster: dict,
    client: RancherClient,
    token: str,
    kinds: dict[str, KindSpec],
    rules: Rules,
    settings: Settings,
    cancelled: Callable[[], bool],
    should_stop: Callable[[], bool],
) -> str:
    cid = cluster["id"]
    key = (run_id, site, cid)
    if cancelled() or should_stop():
        conn.execute(
            "UPDATE run_clusters SET status='skipped' WHERE run_id=? AND site=? AND cluster_id=?", key
        )
        return "skipped"
    conn.execute(
        "UPDATE run_clusters SET status='running', started_at=? WHERE run_id=? AND site=? AND cluster_id=?",
        (_now(), *key),
    )

    def on_progress(done: int, total: int) -> None:
        conn.execute(
            "UPDATE run_clusters SET kinds_done=?, kinds_total=? WHERE run_id=? AND site=? AND cluster_id=?",
            (done, total, *key),
        )

    try:
        r = await collect_cluster(
            client, conn, site, cluster, run_id, kinds, rules, on_progress, should_stop,
            retention_days=settings.metrics_retention_days,
        )  # fmt: skip
        status, total, done, objects, error = r.status, r.kinds_total, r.kinds_done, r.objects, r.error
    except Exception as e:
        log.exception("cluster %s/%s failed", site, cid)
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        error = _scrub(f"{type(e).__name__}: {e}", token)
        status, objects = "failed", None
        cur = conn.execute(
            "SELECT kinds_total, kinds_done FROM run_clusters WHERE run_id=? AND site=? AND cluster_id=?", key
        ).fetchone()
        total, done = cur[0], cur[1]
        try:
            mark_cluster_failed(conn, site, cid, error)
        except Exception:
            log.exception("could not record failure for %s/%s", site, cid)
    conn.execute(
        "UPDATE run_clusters SET status=?, kinds_total=?, kinds_done=?, objects=?, finished_at=?, error=? "
        "WHERE run_id=? AND site=? AND cluster_id=?",
        (status, total, done, objects, _now(), error, *key),
    )
    return status
