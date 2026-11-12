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
