from datetime import datetime, timezone
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
