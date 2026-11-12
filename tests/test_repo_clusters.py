import pytest

from inventory.collector.units import metrics_from_cluster, parse_cpu
from inventory.db.repo import (
    mark_cluster_failed,
    mark_cluster_ok,
    upsert_cluster,
    write_metrics,
)


@pytest.mark.live
def test_local_cluster_and_metrics(db, dev_clusters, dev_node_usage):
    local = next(c for c in dev_clusters if c["id"] == "local")
    upsert_cluster(db, "dev", local, run_id=1)
    m = metrics_from_cluster(local, dev_node_usage)
    write_metrics(db, "dev", "local", 1, "2026-11-12", m, retention_days=365)
    row = db.execute("SELECT * FROM clusters WHERE cluster_id='local'").fetchone()
    assert row["provider"] == "rke2" and row["node_count"] == local["nodeCount"]
    assert m.cpu_req_m == parse_cpu(local["requested"]["cpu"])
    assert m.cpu_used_m is not None and m.cpu_used_m > 0
    write_metrics(db, "dev", "local", 2, "2026-11-12", m, retention_days=365)
    assert db.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0] == 1
    latest = db.execute("SELECT run_id FROM cluster_metrics_latest").fetchone()
    assert latest["run_id"] == 2


@pytest.mark.live
def test_retention_and_marks(db, local_cluster, dev_node_usage):
    upsert_cluster(db, "dev", local_cluster, run_id=1)
    m = metrics_from_cluster(local_cluster, None)
    assert m.cpu_used_m is None and m.mem_used_b is None
    write_metrics(db, "dev", "local", 1, "2025-01-01", m, retention_days=365)
    write_metrics(db, "dev", "local", 2, "2026-11-12", m, retention_days=365)
    days = [r[0] for r in db.execute("SELECT day FROM cluster_metrics")]
    assert days == ["2026-11-12"]

    mark_cluster_failed(db, "dev", "local", "boom")
    assert db.execute("SELECT last_error FROM clusters").fetchone()[0] == "boom"
    mark_cluster_ok(db, "dev", "local", 2, "2026-11-12T02:00:00Z")
    row = db.execute("SELECT * FROM clusters").fetchone()
    assert row["last_error"] is None and row["last_ok_run"] == 2
    assert row["last_ok_at"] == "2026-11-12T02:00:00Z"
