from datetime import datetime, timezone
import pytest
from inventory.web import custom_columns as cc

T0 = "2026-11-01T00:00:00+00:00"


def _cluster(db, site, cid, name, removed=False):
    if removed:
        db.execute("INSERT INTO removed_clusters (site, cluster_id, name, removed_at) VALUES (?,?,?,?)",
                   (site, cid, name, T0))
    else:
        db.execute("INSERT INTO clusters (site, cluster_id, name) VALUES (?,?,?)", (site, cid, name))


def _value(db, site, cid, col, value):
    db.execute("INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at) "
               "VALUES (?,?,?,?,1,?)", (site, cid, col, value, T0))


def _get(db, site, cid, col):
    r = db.execute("SELECT value FROM custom_values WHERE site=? AND cluster_id=? AND column_id=?",
                   (site, cid, col)).fetchone()
    return r["value"] if r else None


def test_create_and_list_in_position_order(db):
    a = cc.create_column(db, "Tier", "choice", ["critical", "standard", "dev"])
    b = cc.create_column(db, "Owner team", "text", [])
    assert [(c.id, c.name, c.type, c.choices) for c in cc.list_columns(db)] == [
        (a, "Tier", "choice", ["critical", "standard", "dev"]), (b, "Owner team", "text", [])]
    assert cc.get_column(db, a).name == "Tier"
    assert cc.get_column(db, 999) is None


@pytest.mark.parametrize("name,type_,choices", [
    ("", "text", []), ("x" * 41, "text", []), ("Tier", "number", []),
    ("C", "choice", []), ("C", "choice", ["a", "A"]), ("C", "choice", ["none"]),
    ("C", "choice", ["x" * 101]), ("C", "text", ["a"])])
def test_create_rejects(db, name, type_, choices):
    with pytest.raises(ValueError):
        cc.create_column(db, name, type_, choices)


def test_name_unique_ignoring_case(db):
    cc.create_column(db, "Tier", "text", [])
    with pytest.raises(ValueError):
        cc.create_column(db, "tier", "text", [])


def test_move_and_rename(db):
    a = cc.create_column(db, "A", "text", [])
    b = cc.create_column(db, "B", "text", [])
    cc.move_column(db, b, "up")
    assert [c.id for c in cc.list_columns(db)] == [b, a]
    cc.move_column(db, b, "up")  # already first: no-op
    assert [c.id for c in cc.list_columns(db)] == [b, a]
    cc.move_column(db, b, "down")
    assert [c.id for c in cc.list_columns(db)] == [a, b]
    cc.rename_column(db, a, "Alpha")
    assert cc.get_column(db, a).name == "Alpha"
    with pytest.raises(ValueError):
        cc.rename_column(db, a, "b")


def test_set_choices_add_reorder_remove(db):
    c = cc.create_column(db, "Tier", "choice", ["a", "b"])
    cc.set_choices(db, c, ["b", "a", "c"])
    assert cc.get_column(db, c).choices == ["b", "a", "c"]
    cc.set_choices(db, c, ["b", "c"])
    assert cc.get_column(db, c).choices == ["b", "c"]
    with pytest.raises(ValueError):
        cc.set_choices(db, c, [])


def test_remove_used_choice_blocked_with_removed_cluster(db):
    c = cc.create_column(db, "Tier", "choice", ["prod", "dev"])
    _cluster(db, "s1", "c-1", "one")
    _cluster(db, "s1", "c-2", "gone", removed=True)
    _value(db, "s1", "c-2", c, "dev")
    with pytest.raises(cc.InUse) as e:
        cc.set_choices(db, c, ["prod"])
    assert e.value.value == "dev" and e.value.live == 0
    assert e.value.removed == [{"site": "s1", "cluster_id": "c-2", "name": "gone"}]
    assert cc.get_column(db, c).choices == ["prod", "dev"]
    _value(db, "s1", "c-1", c, "dev")
    with pytest.raises(cc.InUse) as e:
        cc.set_choices(db, c, ["prod"])
    assert e.value.live == 1


def test_usage(db):
    c = cc.create_column(db, "Tier", "choice", ["prod", "dev"])
    _cluster(db, "s2", "c-1", "one")
    _cluster(db, "s1", "c-2", "zeta", removed=True)
    _cluster(db, "s1", "c-3", "alpha", removed=True)
    _value(db, "s2", "c-1", c, "prod")
    _value(db, "s1", "c-2", c, "dev")
    _value(db, "s1", "c-3", c, "dev")
    assert cc.usage(db, c)["live"] == 1
    assert [r["name"] for r in cc.usage(db, c)["removed"]] == ["alpha", "zeta"]
    u = cc.usage(db, c, "prod")
    assert u["live"] == 1 and u["removed"] == []


def test_rename_choice_updates_values_and_logs(db):
    c = cc.create_column(db, "Tier", "choice", ["production", "dev"])
    _cluster(db, "s1", "c-1", "one")
    _value(db, "s1", "c-1", c, "production")
    assert cc.rename_choice(db, 1, c, "production", "prod") == 1
    assert cc.get_column(db, c).choices == ["prod", "dev"]
    assert _get(db, "s1", "c-1", c) == "prod"
    rows = db.execute("SELECT * FROM custom_values_log").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert (r["site"], r["cluster_id"], r["name"], r["column_name"], r["value"], r["updated_by"]) == (
        "s1", "c-1", "one", "Tier", "prod", 1)
    with pytest.raises(ValueError):
        cc.rename_choice(db, 1, c, "prod", "DEV")
    with pytest.raises(ValueError):
        cc.rename_choice(db, 1, c, "nope", "x")


def test_change_type_text_to_choice(db):
    c = cc.create_column(db, "Owner", "text", [])
    _cluster(db, "s1", "c-1", "one")
    _cluster(db, "s1", "c-2", "two")
    _value(db, "s1", "c-1", c, "Critical")
    _value(db, "s1", "c-2", c, "weird")
    with pytest.raises(ValueError, match="weird"):
        cc.change_type(db, 1, c, "choice", ["critical"])
    assert cc.get_column(db, c).type == "text"
    assert _get(db, "s1", "c-1", c) == "Critical"
    assert db.execute("SELECT COUNT(*) FROM custom_values_log").fetchone()[0] == 0
    db.execute("DELETE FROM custom_values WHERE cluster_id='c-2'")
    cc.change_type(db, 1, c, "choice", ["critical"])
    col = cc.get_column(db, c)
    assert (col.type, col.choices) == ("choice", ["critical"])
    assert _get(db, "s1", "c-1", c) == "critical"
    log = db.execute("SELECT * FROM custom_values_log").fetchall()
    assert [(r["cluster_id"], r["value"], r["updated_by"]) for r in log] == [("c-1", "critical", 1)]


def test_change_type_choice_to_text_keeps_values(db):
    c = cc.create_column(db, "Tier", "choice", ["a"])
    _cluster(db, "s1", "c-1", "one")
    _value(db, "s1", "c-1", c, "a")
    cc.change_type(db, 1, c, "text", [])
    col = cc.get_column(db, c)
    assert (col.type, col.choices) == ("text", [])
    assert _get(db, "s1", "c-1", c) == "a"


def test_delete_column_removes_values_and_log(db):
    a = cc.create_column(db, "A", "choice", ["x"])
    b = cc.create_column(db, "B", "text", [])
    _cluster(db, "s1", "c-1", "one")
    _value(db, "s1", "c-1", a, "x")
    _value(db, "s1", "c-1", b, "y")
    cc.rename_choice(db, 1, a, "x", "z")
    cc.delete_column(db, a)
    assert cc.get_column(db, a) is None
    assert [r["column_id"] for r in db.execute("SELECT column_id FROM custom_values")] == [b]
    assert db.execute("SELECT COUNT(*) FROM custom_values_log").fetchone()[0] == 0


def _cols(db):
    t = cc.create_column(db, "Tier", "choice", ["critical", "standard", "dev"])
    o = cc.create_column(db, "Owner", "text", [])
    return t, o


def test_save_set_keep_clear_and_log(db):
    t, o = _cols(db)
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: "Critical", o: "payments"}) == 2
    assert cc.values(db)[("dev", "c1")] == {t: "critical", o: "payments"}
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: None, o: ""}) == 1
    assert cc.values(db)[("dev", "c1")] == {t: "critical"}
    assert [h["value"] for h in cc.history(db, "dev", "c1")] == ["", "payments", "critical"]
    h = cc.history(db, "dev", "c1")[0]
    assert set(h) >= {"updated_at", "updated_by_name", "column_name", "value"}


def test_values_filtered_by_site(db):
    t, o = _cols(db)
    cc.save_values(db, 1, "a", "c1", "c1", {o: "x"})
    cc.save_values(db, 1, "b", "c2", "c2", {o: "y"})
    assert list(cc.values(db, "a")) == [("a", "c1")]
    assert set(cc.values(db)) == {("a", "c1"), ("b", "c2")}


def test_unchanged_writes_nothing(db):
    t, o = _cols(db)
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: "dev", o: "x"}) == 2
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: "DEV", o: " x "}) == 0
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: "", o: ""}) == 2
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: "", o: ""}) == 0
    assert len(cc.history(db, "dev", "c1")) == 4


@pytest.mark.parametrize("v", ["none", "NONE", "bogus", "x" * 101])
def test_save_rejects_and_writes_nothing(db, v):
    t, o = _cols(db)
    with pytest.raises(ValueError):
        cc.save_values(db, 1, "dev", "c1", "c1", {o: "fine", t: v})
    assert cc.values(db) == {} and cc.history(db, "dev", "c1") == []
    if v != "bogus":
        with pytest.raises(ValueError):
            cc.save_values(db, 1, "dev", "c1", "c1", {t: "dev", o: v})
        assert cc.values(db) == {} and cc.history(db, "dev", "c1") == []


def test_unknown_column_ignored(db):
    _cols(db)
    assert cc.save_values(db, 1, "dev", "c1", "c1", {999: "x"}) == 0
    assert cc.values(db) == {}


def test_log_uses_name_and_column_name_and_user(db):
    t, o = _cols(db)
    cc.save_values(db, 1, "dev", "c1", "My cluster", {o: "x"})
    r = db.execute("SELECT name, column_name FROM custom_values_log").fetchone()
    assert (r["name"], r["column_name"]) == ("My cluster", "Owner")
    assert "updated_by_name" in cc.history(db, "dev", "c1")[0]


def test_values_survive_cluster_removal(db):
    from inventory.collector import runner
    from inventory.db import repo
    t, o = _cols(db)
    run_id = _seed_run(db)
    repo.upsert_cluster(db, "dev", {"id": "c1", "name": "c1"}, run_id)
    cc.save_values(db, 1, "dev", "c1", "c1", {o: "x"})
    runner._remove_unlisted(db, "dev", set())
    assert cc.values(db)[("dev", "c1")] == {o: "x"}
    assert len(cc.history(db, "dev", "c1")) == 1
    repo.upsert_cluster(db, "dev", {"id": "c1", "name": "c1"}, run_id)
    assert cc.values(db)[("dev", "c1")] == {o: "x"}


def _seed_run(db):
    cols = [r["name"] for r in db.execute("PRAGMA table_info(runs)") if r["notnull"] and r["dflt_value"] is None and not r["pk"]]
    vals = {c: ("ok" if c == "status" else T0 if c.endswith("_at") else "all" if c in ("scope", "trigger") else 0) for c in cols}
    cur = db.execute(f"INSERT INTO runs ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
    return cur.lastrowid


def test_merged_history_orders_by_time():
    tr = [{"updated_at": "2026-11-02", "note": "a"}, {"updated_at": "2026-11-01", "note": "b"}]
    cu = [{"updated_at": "2026-11-03", "value": "x"}, {"updated_at": "2026-11-02", "value": "y"}]
    out = cc.merged_history(tr, cu)
    assert [(i["kind"], i["updated_at"]) for i in out] == [
        ("custom", "2026-11-03"), ("tracking", "2026-11-02"), ("custom", "2026-11-02"), ("tracking", "2026-11-01")]
    assert "kind" not in tr[0] and "kind" not in cu[0]


def test_sort_key():
    col = cc.Column(1, "Tier", "choice", ["critical", "standard", "dev"], 1)
    assert cc.sort_key(col, "dev") == "2"
    assert cc.sort_key(col, None) is None
    assert cc.sort_key(cc.Column(2, "O", "text", [], 2), "abc") == "abc"


def test_parse_filters_notices():
    cols = [cc.Column(1, "Tier", "choice", ["dev", "prod"], 1)]
    got = cc.parse_filters(cols, {"cf1": "dev", "cf99": "x", "cf1x": "y", "cf2": ""})
    assert got == ({1: "dev"}, ["Ignored filter cf99: the column no longer exists"])
    got = cc.parse_filters(cols, {"cf1": "bogus"})
    assert got == ({}, ['Ignored filter Tier: "bogus" is not a choice'])
    assert cc.parse_filters(cols, {"cf1": "none"}) == ({1: "none"}, [])


def test_matches():
    tier = cc.Column(1, "Tier", "choice", ["dev"], 1)
    owner = cc.Column(2, "Owner", "text", [], 2)
    by_id = {1: tier, 2: owner}
    assert cc.matches(by_id, {1: "none"}, {2: "x"})
    assert not cc.matches(by_id, {1: "none"}, {1: "dev"})
    assert cc.matches(by_id, {1: "dev"}, {1: "dev"})
    assert not cc.matches(by_id, {1: "dev"}, {})
    assert cc.matches(by_id, {2: "payments"}, {2: "Payments"})
    assert cc.matches(by_id, {1: "dev", 2: "a"}, {1: "dev", 2: "A"})
    assert not cc.matches(by_id, {1: "dev", 2: "b"}, {1: "dev", 2: "A"})
