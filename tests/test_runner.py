import asyncio
from datetime import datetime

import pytest

from inventory.collector import runner
from inventory.collector.runner import (
    RunInProgress,
    Scope,
    create_run,
    execute_run,
    last_good_run,
    mark_interrupted,
    request_cancel,
    retry_scope,
)
from inventory.collector.thread import next_start
from inventory.db.connection import connect


def test_second_run_refused(db):
    rid = create_run(db, "manual", "a", Scope())
    with pytest.raises(RunInProgress) as e:
        create_run(db, "schedule", None, Scope())
    assert e.value.run_id == rid


def test_mark_interrupted(db):
    rid = create_run(db, "manual", "a", Scope())
    db.execute(
        "INSERT INTO run_clusters (run_id, site, cluster_id, status) VALUES (?, 'dev', 'c1', 'ok'), (?, 'dev', 'c2', 'running')",
        [rid, rid],
    )
    assert mark_interrupted(db) == 1
    assert db.execute("SELECT status FROM runs WHERE id=?", [rid]).fetchone()[0] == "interrupted"
    rows = dict(db.execute("SELECT cluster_id, status FROM run_clusters WHERE run_id=?", [rid]).fetchall())
    assert rows == {"c1": "ok", "c2": "skipped"}
    # a new run can start now
    assert create_run(db, "manual", "a", Scope()) > rid


def test_scope_json_roundtrip():
    for s in (Scope(), Scope(site="dev"), Scope(site="dev", cluster_id="local"), Scope(clusters=[("dev", "local")])):
        assert Scope.from_json(s.to_json()) == s


def test_retry_scope_and_last_good_run(db):
    assert last_good_run(db) is None
    rid = create_run(db, "manual", "a", Scope())
    db.execute(
        "INSERT INTO run_clusters (run_id, site, cluster_id, status) VALUES (?, 'dev', 'c1', 'ok'), (?, 'dev', 'c2', 'failed')",
        [rid, rid],
    )
    db.execute("UPDATE runs SET status='partial' WHERE id=?", [rid])
    assert retry_scope(db, rid) == Scope(clusters=[("dev", "c2")])
    assert last_good_run(db)["id"] == rid
    rid2 = create_run(db, "manual", "a", Scope())
    request_cancel(db, rid2)
    assert db.execute("SELECT cancel_requested FROM runs WHERE id=?", [rid2]).fetchone()[0] == 1


def test_next_daily_start():
    assert next_start(datetime(2026, 11, 12, 3, 0), "02:00") == datetime(2026, 11, 13, 2, 0)
    assert next_start(datetime(2026, 11, 12, 1, 0), "02:00") == datetime(2026, 11, 12, 2, 0)


@pytest.mark.live
def test_full_run_dev(settings, db, db_path):
    rid = create_run(connect(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(settings, rid)) == "ok"
    row = connect(db_path).execute("SELECT * FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["cluster_id"] == "local" and row["status"] == "ok"
    assert row["kinds_done"] == row["kinds_total"]
    run = connect(db_path).execute("SELECT * FROM runs WHERE id=?", [rid]).fetchone()
    assert run["status"] == "ok" and run["clusters_total"] == 1 and run["finished_at"]


@pytest.mark.live
def test_cancel_before_start(settings, db, db_path):
    rid = create_run(connect(db_path), "manual", "t", Scope())
    request_cancel(connect(db_path), rid)
    assert asyncio.run(execute_run(settings, rid)) == "cancelled"
    row = connect(db_path).execute("SELECT status FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["status"] == "skipped"


@pytest.mark.live
def test_bad_token_site_fails(settings, db, db_path, monkeypatch):
    monkeypatch.setenv("RANCHER_DEV_TOKEN", "token-bad00:x")
    rid = create_run(connect(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(settings, rid)) == "failed"
    run = connect(db_path).execute("SELECT * FROM runs WHERE id=?", [rid]).fetchone()
    assert "401" in run["error"]
    assert "token-bad00" not in run["error"]


@pytest.mark.live
def test_unexpected_cluster_error_is_contained(settings, db, db_path, monkeypatch):
    """A non-RancherError from collect_cluster fails that cluster, not the run loop."""

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "collect_cluster", broken)
    rid = create_run(connect(db_path), "manual", "t", Scope(site="dev", cluster_id="local"))
    assert asyncio.run(execute_run(settings, rid)) == "failed"
    row = connect(db_path).execute("SELECT * FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["status"] == "failed" and "boom" in row["error"] and row["finished_at"]
    err = connect(db_path).execute("SELECT last_error FROM clusters WHERE cluster_id='local'").fetchone()
    assert err is None or "boom" in err[0]


def test_collector_thread_refuses_second_run(settings, db):
    from inventory.collector.thread import CollectorThread

    stale = create_run(db, "manual", "a", Scope())
    t = CollectorThread(settings)  # COLLECTOR_ENABLED=false in tests: no schedule
    t.start()
    try:
        # start() marks leftover runs interrupted even with the collector off
        assert db.execute("SELECT status FROM runs WHERE id=?", [stale]).fetchone()[0] == "interrupted"
        rid = create_run(db, "manual", "a", Scope())
        with pytest.raises(RunInProgress) as e:
            t.request_run("manual", "b", Scope())
        assert e.value.run_id == rid
    finally:
        t.stop(timeout=5)
    assert not t.is_alive()


def test_last_good_run_ignores_scoped_runs(db):
    full = create_run(db, "manual", "a", Scope())
    db.execute("UPDATE runs SET status='ok' WHERE id=?", [full])
    scoped = create_run(db, "manual", "a", Scope(site="dev", cluster_id="local"))
    db.execute("UPDATE runs SET status='ok' WHERE id=?", [scoped])
    assert last_good_run(db)["id"] == full


def test_empty_scope_refused(db):
    rid = create_run(db, "manual", "a", Scope())
    db.execute("UPDATE runs SET status='ok' WHERE id=?", [rid])
    with pytest.raises(ValueError):
        create_run(db, "manual", "a", retry_scope(db, rid))
    assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_finish_prunes_old_run_rows(db):
    ids = []
    for _ in range(31):
        rid = create_run(db, "manual", "a", Scope())
        db.execute("INSERT INTO run_clusters (run_id, site, cluster_id, status) VALUES (?, 'dev', 'c1', 'ok')", [rid])
        db.execute("INSERT INTO run_kinds (run_id, site, cluster_id, kind, status) VALUES (?, 'dev', 'c1', 'nodes', 'ok')", [rid])
        runner._finish(db, rid, "ok", None)
        ids.append(rid)
    for table in ("run_clusters", "run_kinds"):
        kept = [r[0] for r in db.execute(f"SELECT run_id FROM {table} ORDER BY run_id")]
        assert kept == ids[1:]


@pytest.mark.live
def test_cancelled_task_ends_interrupted(settings, db, db_path):
    rid = create_run(connect(db_path), "manual", "t", Scope())

    async def go():
        task = asyncio.create_task(execute_run(settings, rid))
        await asyncio.sleep(0.5)  # inside the cluster listing (first connection takes ~5 s here)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    run = connect(db_path).execute("SELECT status, finished_at FROM runs WHERE id=?", [rid]).fetchone()
    assert run["status"] == "interrupted" and run["finished_at"]
    assert create_run(connect(db_path), "manual", "t", Scope()) > rid


@pytest.mark.live
def test_bad_ca_file_site_does_not_stop_other_sites(settings, db, db_path, tmp_path, monkeypatch):
    """A site whose CA file is missing is a site error; the other sites still collect."""
    import os

    monkeypatch.setenv("RANCHER_DEVBAD_TOKEN", os.environ["RANCHER_DEV_TOKEN"])
    sites = tmp_path / "sites.yaml"
    sites.write_text(
        "sites:\n"
        "  - name: dev\n    url: https://rancher.local\n    token_env: RANCHER_DEV_TOKEN\n    tls_verify: false\n"
        "  - name: dev-badca\n    url: https://rancher.local\n    token_env: RANCHER_DEVBAD_TOKEN\n"
        f"    ca_file: {tmp_path / 'missing-ca.pem'}\n"
    )
    s = settings.model_copy(update={"sites_file": sites, "defaults_dir": settings.defaults_dir.resolve()})
    rid = create_run(connect(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(s, rid)) == "partial"
    conn = connect(db_path)
    row = conn.execute("SELECT * FROM run_clusters WHERE run_id=? AND site='dev'", [rid]).fetchone()
    assert row["cluster_id"] == "local" and row["status"] == "ok"
    run = conn.execute("SELECT * FROM runs WHERE id=?", [rid]).fetchone()
    assert "dev-badca" in run["error"]


@pytest.mark.live
def test_full_run_removes_clusters_gone_from_rancher(collected_db, tmp_path):
    """Rows of a cluster that Rancher no longer lists are removed by a full run.

    The removed cluster is simulated by copying the real collected rows of `local`
    under the id 'c-gone'. Daily metrics history and run history are kept.
    """
    import shutil

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = collected_db.model_copy(update={"data_dir": tmp_path})
    conn = connect(s.db_path)
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO clusters SELECT site, 'c-gone', name, state, provider, k8s_version, labels, node_count, "
        "last_ok_run, last_ok_at, last_error, last_seen_run FROM clusters WHERE cluster_id='local'"
    )
    conn.execute(
        "INSERT INTO resources (site, cluster_id, kind, namespace, name, data, hash, created_at, last_seen_run) "
        "SELECT site, 'c-gone', kind, namespace, name, data, hash, created_at, last_seen_run "
        "FROM resources WHERE cluster_id='local'"
    )
    conn.execute(
        "INSERT INTO resource_labels SELECT g.id, l.key, l.value FROM resource_labels l "
        "JOIN resources r ON r.id=l.resource_id "
        "JOIN resources g ON g.cluster_id='c-gone' AND g.site=r.site AND g.kind=r.kind "
        "AND g.namespace=r.namespace AND g.name=r.name WHERE r.cluster_id='local'"
    )
    for table in ("namespace_profile", "cluster_profile", "cluster_metrics_latest", "cluster_metrics", "run_clusters"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        select = ", ".join("'c-gone'" if c == "cluster_id" else c for c in cols)
        conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {select} FROM {table} WHERE cluster_id='local'")
    conn.execute("COMMIT")
    assert conn.execute("SELECT count(*) FROM resource_labels l JOIN resources r ON r.id=l.resource_id "
                        "WHERE r.cluster_id='c-gone'").fetchone()[0] > 0

    rid = create_run(conn, "manual", "t", Scope())
    assert asyncio.run(execute_run(s, rid)) == "ok"

    def count(table, cid):
        return conn.execute(f"SELECT count(*) FROM {table} WHERE cluster_id=?", [cid]).fetchone()[0]

    for table in ("clusters", "resources", "namespace_profile", "cluster_profile", "cluster_metrics_latest"):
        assert count(table, "c-gone") == 0, table
        assert count(table, "local") > 0, table
    assert conn.execute("SELECT count(*) FROM resource_labels l LEFT JOIN resources r ON r.id=l.resource_id "
                        "WHERE r.id IS NULL").fetchone()[0] == 0
    assert count("cluster_metrics", "c-gone") > 0
    assert count("run_clusters", "c-gone") > 0


@pytest.mark.live
def test_scoped_run_keeps_unlisted_clusters(collected_db, tmp_path):
    import shutil

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = collected_db.model_copy(update={"data_dir": tmp_path})
    conn = connect(s.db_path)
    conn.execute(
        "INSERT INTO clusters SELECT site, 'c-gone', name, state, provider, k8s_version, labels, node_count, "
        "last_ok_run, last_ok_at, last_error, last_seen_run FROM clusters WHERE cluster_id='local'"
    )
    rid = create_run(conn, "manual", "t", Scope(site="dev", cluster_id="local"))
    assert asyncio.run(execute_run(s, rid)) == "ok"
    assert conn.execute("SELECT count(*) FROM clusters WHERE cluster_id='c-gone'").fetchone()[0] == 1
