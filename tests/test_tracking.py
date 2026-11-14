import shutil
from datetime import date, datetime, timezone
import pytest
from inventory.web import tracking

def test_default_is_not_started(db):
    assert tracking.get(db, "dev", "local")["status"] == "not started"

def test_save_writes_row_and_log(db):
    assert tracking.save(db, 1, "dev", "local", "local", status="planned", wave="wave 1")
    t = tracking.get(db, "dev", "local")
    assert (t["status"], t["wave"], t["note"]) == ("planned", "wave 1", "")
    assert len(tracking.history(db, "dev", "local")) == 1

def test_none_keeps_and_empty_clears(db):
    tracking.save(db, 1, "dev", "local", "local", status="planned", wave="wave 1", note="to nkp-a")
    tracking.save(db, 1, "dev", "local", "local", status="in progress")
    assert tracking.get(db, "dev", "local")["wave"] == "wave 1"
    tracking.save(db, 1, "dev", "local", "local", wave="")
    assert tracking.get(db, "dev", "local")["wave"] == ""
    assert tracking.get(db, "dev", "local")["note"] == "to nkp-a"

def test_unchanged_save_writes_nothing(db):
    tracking.save(db, 1, "dev", "local", "local", status="planned")
    assert tracking.save(db, 1, "dev", "local", "local", status="planned") is False
    assert len(tracking.history(db, "dev", "local")) == 1

def test_history_newest_first(db):
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    tracking.save(db, 1, "dev", "local", "local", status="planned", now=t0)
    tracking.save(db, 1, "dev", "local", "local", status="migrated", now=t0.replace(day=2))
    assert [h["status"] for h in tracking.history(db, "dev", "local")] == ["migrated", "planned"]

@pytest.mark.parametrize("kw", [{"status": "done"}, {"wave": "x" * 101}, {"note": "x" * 2001}])
def test_save_rejects_bad_input(db, kw):
    with pytest.raises(ValueError):
        tracking.save(db, 1, "dev", "local", "local", **kw)
    assert tracking.history(db, "dev", "local") == []


@pytest.fixture
def tracked_settings(collected_db, tmp_path):
    """Settings pointing at a copy of the real collected dev DB."""
    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    return collected_db.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def tracked_conn(tracked_settings, open_db):
    return open_db(tracked_settings.db_path)


def _count(conn, where="1=1", params=()):
    return conn.execute(f"SELECT count(*) FROM clusters WHERE {where}", params).fetchone()[0]


@pytest.mark.live
def test_removed_without_tracking_counts_as_not_started(tracked_conn):
    n = _count(tracked_conn)
    tracked_conn.execute("INSERT INTO removed_clusters VALUES ('dev', 'c-gone', 'gone', '2026-11-10T00:00:00+00:00')")
    p = tracking.progress(tracked_conn)
    assert p["total"] == n + 1
    assert p["by_status"][0] == {
        "status": "not started", "count": n + 1, "pct": 100.0, "href": "/clusters?status=not%20started"}
    assert [s["status"] for s in p["by_status"]] == list(tracking.STATUSES)
    r = tracking.removed(tracked_conn)
    assert [(x["cluster_id"], x["status"], x["done"]) for x in r] == [("c-gone", "not started", False)]


@pytest.mark.live
def test_progress_scoped_by_site(tracked_conn):
    tracked_conn.execute("INSERT INTO removed_clusters VALUES ('other', 'c-x', 'x', '2026-11-10T00:00:00+00:00')")
    assert tracking.progress(tracked_conn, site="dev")["total"] == _count(tracked_conn, "site='dev'")
    assert tracking.removed(tracked_conn, site="dev") == []
    assert [s["site"] for s in tracking.progress(tracked_conn)["by_site"]] == ["dev", "other"]
    assert tracking.progress(tracked_conn, site="dev")["by_status"][0]["href"].endswith("&site=dev")


@pytest.mark.live
def test_progress_waves_and_over_time(tracked_conn):
    n = _count(tracked_conn)
    d = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    tracking.save(tracked_conn, 1, "dev", "local", "local", status="migrated", wave="wave 1", now=d)
    p = tracking.progress(tracked_conn, today=date(2026, 11, 3))
    zero = {s: 0 for s in tracking.STATUSES}
    assert p["by_wave"][0] == {"wave": "wave 1", "counts": {**zero, "migrated": 1}}
    if n > 1:
        assert p["by_wave"][1] == {"wave": "no wave", "counts": {**zero, "not started": n - 1}}
    assert len(p["by_wave"]) == (2 if n > 1 else 1)
    assert p["over_time"] == [{"day": "2026-11-01", "done": 1}, {"day": "2026-11-02", "done": 1},
                              {"day": "2026-11-03", "done": 1}]


@pytest.mark.live
def test_over_time_drops_when_status_moves_back(tracked_conn):
    t0 = datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc)
    tracking.save(tracked_conn, 1, "dev", "local", "local", status="migrated", now=t0)
    tracking.save(tracked_conn, 1, "dev", "local", "local", status="in progress", now=t0.replace(day=2))
    p = tracking.progress(tracked_conn, today=date(2026, 11, 2))
    assert p["over_time"] == [{"day": "2026-11-01", "done": 1}, {"day": "2026-11-02", "done": 0}]
    assert tracking.progress(tracked_conn, site="nope", today=date(2026, 11, 2))["over_time"] == []
