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
from inventory.web import tracking


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
def test_full_run_dev(settings, db, db_path, open_db, dev_clusters):
    rid = create_run(open_db(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(settings, rid)) == "ok"
    rows = open_db(db_path).execute("SELECT * FROM run_clusters WHERE run_id=?", [rid]).fetchall()
    assert {r["cluster_id"] for r in rows} == {c["id"] for c in dev_clusters} and "local" in {c["id"] for c in dev_clusters}
    for row in rows:
        assert row["status"] == "ok" and row["kinds_done"] == row["kinds_total"], row["cluster_id"]
    run = open_db(db_path).execute("SELECT * FROM runs WHERE id=?", [rid]).fetchone()
    assert run["status"] == "ok" and run["clusters_total"] == len(dev_clusters) and run["finished_at"]


@pytest.mark.live
def test_cancel_before_start(settings, db, db_path, open_db):
    rid = create_run(open_db(db_path), "manual", "t", Scope())
    request_cancel(open_db(db_path), rid)
    assert asyncio.run(execute_run(settings, rid)) == "cancelled"
    row = open_db(db_path).execute("SELECT status FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["status"] == "skipped"


@pytest.mark.live
def test_bad_token_site_fails(settings, db, db_path, monkeypatch, open_db):
    monkeypatch.setenv("RANCHER_DEV_TOKEN", "token-bad00:x")
    rid = create_run(open_db(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(settings, rid)) == "failed"
    run = open_db(db_path).execute("SELECT * FROM runs WHERE id=?", [rid]).fetchone()
    assert "401" in run["error"]
    assert "token-bad00" not in run["error"]


@pytest.mark.live
def test_unexpected_cluster_error_is_contained(settings, db, db_path, monkeypatch, open_db):
    """A non-RancherError from collect_cluster fails that cluster, not the run loop."""

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "collect_cluster", broken)
    rid = create_run(open_db(db_path), "manual", "t", Scope(site="dev", cluster_id="local"))
    assert asyncio.run(execute_run(settings, rid)) == "failed"
    row = open_db(db_path).execute("SELECT * FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["status"] == "failed" and "boom" in row["error"] and row["finished_at"]
    err = open_db(db_path).execute("SELECT last_error FROM clusters WHERE cluster_id='local'").fetchone()
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
def test_cancelled_task_ends_interrupted(settings, db, db_path, open_db):
    rid = create_run(open_db(db_path), "manual", "t", Scope())

    async def go():
        task = asyncio.create_task(execute_run(settings, rid))
        await _wait_for_running_cluster(open_db(db_path), rid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    run = open_db(db_path).execute("SELECT status, finished_at FROM runs WHERE id=?", [rid]).fetchone()
    assert run["status"] == "interrupted" and run["finished_at"]
    assert create_run(open_db(db_path), "manual", "t", Scope()) > rid


@pytest.mark.live
def test_bad_ca_file_site_does_not_stop_other_sites(settings, db, db_path, tmp_path, monkeypatch, open_db, dev_clusters):
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
    rid = create_run(open_db(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(s, rid)) == "partial"
    conn = open_db(db_path)
    rows = conn.execute("SELECT * FROM run_clusters WHERE run_id=? AND site='dev'", [rid]).fetchall()
    assert {r["cluster_id"] for r in rows} == {c["id"] for c in dev_clusters}
    assert all(r["status"] == "ok" for r in rows)
    run = conn.execute("SELECT * FROM runs WHERE id=?", [rid]).fetchone()
    assert "dev-badca" in run["error"]


def _copy_local_as_gone(conn, table):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    select = ", ".join("'c-gone'" if c == "cluster_id" else c for c in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {select} FROM {table} WHERE cluster_id='local'")


@pytest.mark.live
def test_full_run_removes_clusters_gone_from_rancher(collected_db, tmp_path, open_db):
    """Rows of a cluster that Rancher no longer lists are removed by a full run.

    The removed cluster is simulated by copying the real collected rows of `local`
    under the id 'c-gone'. Daily metrics history and run history are kept.
    """
    import shutil

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = collected_db.model_copy(update={"data_dir": tmp_path})
    conn = open_db(s.db_path)
    conn.execute("BEGIN")
    _copy_local_as_gone(conn, "clusters")
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
        _copy_local_as_gone(conn, table)
    conn.execute(
        "INSERT INTO machine_pools (site, cluster_id, name) SELECT site, 'c-gone', 'pool-a' FROM clusters"
        " WHERE cluster_id='local'"
    )
    conn.execute("COMMIT")
    assert conn.execute("SELECT count(*) FROM resource_labels l JOIN resources r ON r.id=l.resource_id "
                        "WHERE r.cluster_id='c-gone'").fetchone()[0] > 0
    tracking.save(conn, 1, "dev", "c-gone", "gone", status="migrated")

    rid = create_run(conn, "manual", "t", Scope())
    assert asyncio.run(execute_run(s, rid)) == "ok"

    def count(table, cid):
        return conn.execute(f"SELECT count(*) FROM {table} WHERE cluster_id=?", [cid]).fetchone()[0]

    for table in ("clusters", "resources", "namespace_profile", "cluster_profile", "cluster_metrics_latest"):
        assert count(table, "c-gone") == 0, table
        assert count(table, "local") > 0, table
    assert count("machine_pools", "c-gone") == 0
    assert conn.execute("SELECT count(*) FROM resource_labels l LEFT JOIN resources r ON r.id=l.resource_id "
                        "WHERE r.id IS NULL").fetchone()[0] == 0
    assert count("cluster_metrics", "c-gone") > 0
    assert count("run_clusters", "c-gone") > 0
    gone = conn.execute("SELECT name, removed_at FROM removed_clusters WHERE cluster_id='c-gone'").fetchone()
    assert gone is not None and gone["removed_at"]
    assert conn.execute("SELECT count(*) FROM removed_clusters WHERE cluster_id='local'").fetchone()[0] == 0
    assert tracking.get(conn, "dev", "c-gone")["status"] == "migrated"
    assert len(tracking.history(conn, "dev", "c-gone")) == 1


@pytest.mark.live
def test_relisted_cluster_leaves_removed_list(collected_db, tmp_path, open_db):
    import shutil

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = collected_db.model_copy(update={"data_dir": tmp_path})
    conn = open_db(s.db_path)
    conn.execute("INSERT INTO removed_clusters VALUES ('dev', 'local', 'local', '2026-11-10T00:00:00+00:00')")
    tracking.save(conn, 1, "dev", "local", "local", status="planned")
    rid = create_run(conn, "manual", "t", Scope())
    assert asyncio.run(execute_run(s, rid)) == "ok"
    assert conn.execute("SELECT count(*) FROM removed_clusters").fetchone()[0] == 0
    assert tracking.get(conn, "dev", "local")["status"] == "planned"


@pytest.mark.live
def test_scoped_run_keeps_unlisted_clusters(collected_db, tmp_path, open_db):
    import shutil

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = collected_db.model_copy(update={"data_dir": tmp_path})
    conn = open_db(s.db_path)
    _copy_local_as_gone(conn, "clusters")
    rid = create_run(conn, "manual", "t", Scope(site="dev", cluster_id="local"))
    assert asyncio.run(execute_run(s, rid)) == "ok"
    assert conn.execute("SELECT count(*) FROM clusters WHERE cluster_id='c-gone'").fetchone()[0] == 1


@pytest.mark.live
def test_metrics_error_does_not_fail_cluster(settings, db, db_path, monkeypatch, open_db):
    from inventory.collector import cluster as cluster_mod

    def broken(*args, **kwargs):
        raise ValueError("bad quantity")

    monkeypatch.setattr(cluster_mod, "metrics_from_cluster", broken)
    rid = create_run(open_db(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(settings, rid)) == "ok"
    row = open_db(db_path).execute("SELECT status FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["status"] == "ok"


def test_schedule_survives_errors(settings, monkeypatch):
    """An error in one schedule iteration is logged and the loop keeps going."""
    from inventory.collector import thread as thread_mod
    from inventory.collector.thread import CollectorThread

    t = CollectorThread(settings)
    calls = []
    monkeypatch.setattr(t, "_is_stale", lambda: False)
    monkeypatch.setattr(thread_mod, "RETRY_SLEEP_S", 0.01)

    def bad_next_start(now, at):
        calls.append(1)
        if len(calls) >= 3:
            t._stop_flag.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(thread_mod, "next_start", bad_next_start)

    async def go():
        await asyncio.wait_for(t._schedule(), timeout=5)

    asyncio.run(go())
    assert len(calls) == 3


@pytest.mark.live
def test_removal_skipped_when_listing_fails(collected_db, tmp_path, monkeypatch, open_db):
    """A site whose cluster list cannot be read keeps its stored clusters after a full run."""
    import shutil

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = collected_db.model_copy(update={"data_dir": tmp_path})
    monkeypatch.setenv("RANCHER_DEV_TOKEN", "token-bad00:x")
    conn = open_db(s.db_path)
    before = conn.execute("SELECT count(*) FROM resources WHERE cluster_id='local'").fetchone()[0]
    rid = create_run(conn, "manual", "t", Scope())
    assert asyncio.run(execute_run(s, rid)) == "failed"
    assert conn.execute("SELECT count(*) FROM clusters WHERE cluster_id='local'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM resources WHERE cluster_id='local'").fetchone()[0] == before


def test_request_run_without_loop_leaves_no_running_row(settings, db):
    from inventory.collector.thread import CollectorThread

    t = CollectorThread(settings)  # never started: its loop is not running
    with pytest.raises(RuntimeError):
        t.request_run("manual", "a", Scope())
    assert db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0] == 0


def test_scheduled_start_retried_until_it_succeeds(settings, monkeypatch):
    """A scheduled start that raises is retried; today's run is not skipped."""
    from datetime import timedelta

    from inventory.collector import thread as thread_mod
    from inventory.collector.thread import CollectorThread

    t = CollectorThread(settings)
    monkeypatch.setattr(t, "_is_stale", lambda: False)
    monkeypatch.setattr(thread_mod, "RETRY_SLEEP_S", 0.01)
    targets = []

    def soon(now, at):  # first target is now; later ones are tomorrow
        targets.append(now)
        return now if len(targets) == 1 else now + timedelta(days=1)

    monkeypatch.setattr(thread_mod, "next_start", soon)
    calls = []

    def flaky(trigger, by, scope):
        calls.append(trigger)
        if len(calls) < 3:
            raise OSError("database is locked")
        t._stop_flag.set()
        return 1

    monkeypatch.setattr(t, "request_run", flaky)

    async def go():
        await asyncio.wait_for(t._schedule(), timeout=5)

    asyncio.run(go())
    assert calls == ["schedule"] * 3


@pytest.mark.parametrize("cancelled,stopped,sites,errors,statuses,expected", [
    (False, False, 1, [], ["ok", "ok"], "ok"),
    (False, False, 1, [], ["ok", "failed"], "partial"),
    (False, False, 1, [], ["failed", "failed"], "failed"),
    (False, False, 1, [], ["ok", "skipped"], "ok"),
    (False, False, 1, [], [], "ok"),
    (True, False, 1, [], ["ok"], "cancelled"),
    (True, True, 1, [], ["ok"], "cancelled"),
    (False, True, 1, [], ["ok", "skipped"], "interrupted"),
    (False, False, 2, ["a: x", "b: y"], [], "failed"),
    (False, False, 2, ["a: x"], ["ok"], "partial"),
    (False, False, 2, ["a: x"], ["failed"], "failed"),
    (False, False, 2, ["a: x"], [], "partial"),
])
def test_final_status(cancelled, stopped, sites, errors, statuses, expected):
    assert runner._final_status(cancelled, stopped, sites, errors, statuses) == expected


async def _wait_for_running_cluster(conn, rid, timeout=60.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if conn.execute("SELECT 1 FROM run_clusters WHERE run_id=? AND status='running'", [rid]).fetchone():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no cluster reached running")


@pytest.mark.live
def test_cancel_during_run(settings, db, db_path, open_db):
    """Cancel requested while a cluster is collecting: it finishes, the run ends cancelled."""
    conn = open_db(db_path)
    rid = create_run(conn, "manual", "t", Scope())

    async def go():
        task = asyncio.create_task(execute_run(settings, rid))
        await _wait_for_running_cluster(conn, rid)
        request_cancel(conn, rid)
        return await task

    assert asyncio.run(go()) == "cancelled"
    statuses = [r[0] for r in conn.execute("SELECT status FROM run_clusters WHERE run_id=?", [rid])]
    assert statuses and "running" not in statuses and "pending" not in statuses


@pytest.mark.live
@pytest.mark.slow
def test_scheduler_fires(collected_db, tmp_path, open_db):
    """COLLECT_DAILY_AT one minute ahead starts a scheduled run (data is fresh, so no startup run)."""
    import shutil
    import time
    from datetime import timedelta

    from inventory.collector.thread import CollectorThread

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    at = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M")
    s = collected_db.model_copy(update={"data_dir": tmp_path, "collector_enabled": True, "collect_daily_at": at})
    conn = open_db(s.db_path)
    t = CollectorThread(s)
    t.start()
    try:
        deadline = time.monotonic() + 150
        row = None
        while time.monotonic() < deadline and row is None:
            row = conn.execute("SELECT * FROM runs WHERE trigger='schedule'").fetchone()
            time.sleep(1)
    finally:
        t.stop(timeout=120)
    assert row is not None and row["triggered_by"] is None
