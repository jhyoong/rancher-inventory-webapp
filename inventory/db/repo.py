import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

from inventory.collector.units import ClusterMetrics


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Open a transaction unless the caller already owns one."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def upsert_cluster(conn: sqlite3.Connection, site: str, cluster: dict, run_id: int) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO clusters (site, cluster_id, name, state, provider, k8s_version,
                                     labels, node_count, last_seen_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (site, cluster_id) DO UPDATE SET
                 name=excluded.name, state=excluded.state, provider=excluded.provider,
                 k8s_version=excluded.k8s_version, labels=excluded.labels,
                 node_count=excluded.node_count, last_seen_run=excluded.last_seen_run""",
            (
                site,
                cluster["id"],
                cluster.get("name") or cluster["id"],
                cluster.get("state"),
                cluster.get("provider"),
                (cluster.get("version") or {}).get("gitVersion"),
                json.dumps(cluster.get("labels") or {}, sort_keys=True),
                cluster.get("nodeCount"),
                run_id,
            ),
        )


_METRIC_COLS = (
    "nodes, pods, cpu_alloc_m, cpu_req_m, cpu_used_m, mem_alloc_b, mem_req_b, mem_used_b"
)


def write_metrics(
    conn: sqlite3.Connection,
    site: str,
    cluster_id: str,
    run_id: int,
    day: str,
    m: ClusterMetrics,
    *,
    retention_days: int,
) -> None:
    values = (
        m.nodes, m.pods, m.cpu_alloc_m, m.cpu_req_m, m.cpu_used_m,
        m.mem_alloc_b, m.mem_req_b, m.mem_used_b,
    )  # fmt: skip
    cutoff = (date.fromisoformat(day) - timedelta(days=retention_days)).isoformat()
    with transaction(conn):
        conn.execute(
            f"INSERT OR REPLACE INTO cluster_metrics (site, cluster_id, day, run_id, {_METRIC_COLS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (site, cluster_id, day, run_id, *values),
        )
        conn.execute(
            f"INSERT OR REPLACE INTO cluster_metrics_latest (site, cluster_id, run_id, {_METRIC_COLS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (site, cluster_id, run_id, *values),
        )
        conn.execute(
            "DELETE FROM cluster_metrics WHERE day < ?",
            (cutoff,),
        )


def mark_cluster_ok(conn: sqlite3.Connection, site: str, cluster_id: str, run_id: int, now: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clusters SET last_ok_run=?, last_ok_at=?, last_error=NULL"
            " WHERE site=? AND cluster_id=?",
            (run_id, now, site, cluster_id),
        )


def mark_cluster_failed(conn: sqlite3.Connection, site: str, cluster_id: str, error: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clusters SET last_error=? WHERE site=? AND cluster_id=?",
            (error, site, cluster_id),
        )
