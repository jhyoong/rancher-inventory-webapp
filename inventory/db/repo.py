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


def upsert_resources(conn: sqlite3.Connection, site: str, cluster_id: str, run_id: int, items: list) -> int:
    """Write one kind's objects. Unchanged hash only bumps last_seen_run.

    Returns the number of rows rewritten (new or changed). The caller owns the transaction.
    """
    rewritten = 0
    for t in items:
        row = conn.execute(
            "SELECT id, hash FROM resources"
            " WHERE site=? AND cluster_id=? AND kind=? AND namespace=? AND name=?",
            (site, cluster_id, t.kind, t.namespace, t.name),
        ).fetchone()
        if row is not None and row[1] == t.hash:
            conn.execute("UPDATE resources SET last_seen_run=? WHERE id=?", (run_id, row[0]))
            continue
        data = json.dumps(t.data, sort_keys=True)
        if row is None:
            rid = conn.execute(
                "INSERT INTO resources (site, cluster_id, kind, namespace, name, data, hash,"
                " created_at, last_seen_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (site, cluster_id, t.kind, t.namespace, t.name, data, t.hash, t.created_at, run_id),
            ).lastrowid
        else:
            rid = row[0]
            conn.execute(
                "UPDATE resources SET data=?, hash=?, created_at=?, last_seen_run=? WHERE id=?",
                (data, t.hash, t.created_at, run_id, rid),
            )
            for table in ("resource_labels", "resource_annotations", "resource_images", "addresses"):
                conn.execute(f"DELETE FROM {table} WHERE resource_id=?", (rid,))
        conn.executemany(
            "INSERT INTO resource_labels (resource_id, key, value) VALUES (?, ?, ?)",
            [(rid, k, v) for k, v in t.labels.items()],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO resource_annotations (resource_id, key, value, custom)"
            " VALUES (?, ?, ?, ?)",
            [(rid, k, v, int(c)) for k, v, c in t.annotations],
        )
        conn.executemany(
            "INSERT INTO resource_images (resource_id, image) VALUES (?, ?)",
            [(rid, i) for i in t.images],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO addresses (resource_id, role, address, ports) VALUES (?, ?, ?, ?)",
            [(rid, a.role, a.address, a.ports) for a in t.addresses],
        )
        rewritten += 1
    return rewritten


def delete_stale(conn: sqlite3.Connection, site: str, cluster_id: str, kind: str, run_id: int) -> int:
    """Delete rows of one fully listed kind that this run did not see."""
    cur = conn.execute(
        "DELETE FROM resources WHERE site=? AND cluster_id=? AND kind=? AND last_seen_run<?",
        (site, cluster_id, kind, run_id),
    )
    return cur.rowcount


def write_run_kind(
    conn: sqlite3.Connection, run_id: int, site: str, cluster_id: str, kind: str,
    status: str, objects: int | None, error: str | None,
) -> None:  # fmt: skip
    conn.execute(
        "INSERT OR REPLACE INTO run_kinds (run_id, site, cluster_id, kind, status, objects, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, site, cluster_id, kind, status, objects, error),
    )
