import json

from inventory.collector import runner
from inventory.collector.members import Member, MembersResult
from inventory.db.repo import write_members


def _seed(db, cid="c1"):
    db.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev', ?, ?)", [cid, cid])


def _m(kind, subject, name, disabled=False):
    return Member(kind, subject, name, None if kind == "group" else name, disabled,
                  [{"role": "Owner", "template": "cluster-owner", "scope": "cluster"}])  # fmt: skip


def _rows(db, cid="c1"):
    return db.execute(
        "SELECT * FROM cluster_members WHERE site='dev' AND cluster_id=? ORDER BY ord", [cid]
    ).fetchall()


def _state(db, cid="c1"):
    r = db.execute("SELECT members_status, members_at FROM clusters WHERE cluster_id=?", [cid]).fetchone()
    return r[0], r[1]


def test_ok_replaces_rows_and_sets_status(db):
    _seed(db)
    write_members(db, "dev", "c1", MembersResult("ok", {"c1": [_m("user", "u-1", "ann"), _m("group", "g-1", "ops")]}), "t1")
    write_members(db, "dev", "c1", MembersResult("ok", {"c1": [_m("group", "g-2", "dev"), _m("user", "u-2", "bob", True)]}), "t2")
    rows = _rows(db)
    assert [(r["subject"], r["ord"], r["disabled"]) for r in rows] == [("g-2", 0, 0), ("u-2", 1, 1)]
    assert rows[1]["username"] == "bob" and rows[0]["username"] is None
    assert json.loads(rows[0]["grants"])[0]["role"] == "Owner"
    assert _state(db) == ("ok", "t2")


def test_forbidden_clears_rows(db):
    _seed(db)
    write_members(db, "dev", "c1", MembersResult("ok", {"c1": [_m("user", "u-1", "ann")]}), "t1")
    write_members(db, "dev", "c1", MembersResult("forbidden"), "t2")
    assert _rows(db) == []
    assert _state(db)[0] == "forbidden"


def test_failed_keeps_rows_and_date(db):
    _seed(db)
    write_members(db, "dev", "c1", MembersResult("ok", {"c1": [_m("user", "u-1", "ann")]}), "t1")
    write_members(db, "dev", "c1", MembersResult("failed", {}, "boom"), "t2")
    assert len(_rows(db)) == 1
    assert _state(db) == ("failed", "t1")


def test_no_cluster_row_writes_nothing(db):
    write_members(db, "dev", "ghost", MembersResult("ok", {"ghost": [_m("user", "u-1", "ann")]}), "t1")
    assert _rows(db, "ghost") == []


def test_remove_unlisted_deletes_members(db):
    _seed(db)
    _seed(db, "c2")
    write_members(db, "dev", "c1", MembersResult("ok", {"c1": [_m("user", "u-1", "ann")]}), "t1")
    write_members(db, "dev", "c2", MembersResult("ok", {"c2": [_m("user", "u-1", "ann")]}), "t1")
    runner._remove_unlisted(db, "dev", {"c2"})
    assert _rows(db, "c1") == [] and len(_rows(db, "c2")) == 1


def test_ok_forbidden_failed_leaves_no_date_and_no_rows(db):
    _seed(db)
    write_members(db, "dev", "c1", MembersResult("ok", {"c1": [_m("user", "u-1", "ann")]}), "t1")
    write_members(db, "dev", "c1", MembersResult("forbidden"), "t2")
    write_members(db, "dev", "c1", MembersResult("failed", {}, "boom"), "t3")
    assert _rows(db) == []
    assert _state(db) == ("failed", None)
