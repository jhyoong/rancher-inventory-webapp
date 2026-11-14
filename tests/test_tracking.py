import csv
import io
import json
import re
import shutil
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
def test_progress_by_wave(tracked_conn):
    n = _count(tracked_conn)
    d = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    tracking.save(tracked_conn, 1, "dev", "local", "local", status="migrated", wave="wave 1", now=d)
    p = tracking.progress(tracked_conn)
    zero = {s: 0 for s in tracking.STATUSES}
    assert p["by_wave"][0] == {"wave": "wave 1", "counts": {**zero, "migrated": 1}}
    if n > 1:
        assert p["by_wave"][1] == {"wave": "no wave", "counts": {**zero, "not started": n - 1}}
    assert len(p["by_wave"]) == (2 if n > 1 else 1)


# ---- web: edit tracking ----

@pytest.fixture
def tracked_client(tracked_settings):
    from fastapi.testclient import TestClient

    from tests.conftest import TEST_PASSWORD, _login
    from inventory.web.app import create_app

    with TestClient(create_app(tracked_settings, None)) as c:
        yield _login(c, tracked_settings, "admin", TEST_PASSWORD)


@pytest.fixture
def tracked_viewer(tracked_settings):
    from fastapi.testclient import TestClient

    from tests.conftest import _login
    from inventory.db.connection import connect
    from inventory.web.app import create_app
    from inventory.web.auth import create_user

    with TestClient(create_app(tracked_settings, None)) as c:
        conn = connect(tracked_settings.db_path)
        try:
            create_user(conn, "viewer", "viewer-password-1", "viewer")
        finally:
            conn.close()
        yield _login(c, tracked_settings, "viewer", "viewer-password-1")


def _csv_rows(client, query=""):
    text = client.get(f"/clusters?format=csv&table=clusters{query}").text
    return list(csv.DictReader(io.StringIO(text)))


def _local(rows):
    return next(r for r in rows if r["cluster_id"] == "local")


@pytest.mark.live
def test_cluster_page_saves_tracking(tracked_client):
    r = tracked_client.post("/clusters/dev/local/migration", data={"status": "planned", "wave": "wave 1", "note": "to nkp-a", "csrf": tracked_client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#migration")
    html = tracked_client.get("/clusters/dev/local").text
    assert 'id="migration"' in html and "to nkp-a" in html and "wave 1" in html
    assert "Last changed by admin" in html


@pytest.mark.live
def test_cluster_page_tracking_errors(tracked_client):
    data = {"csrf": tracked_client.csrf}
    assert tracked_client.post("/clusters/dev/c-never/migration", data={**data, "status": "planned"}).status_code == 404
    assert tracked_client.post("/clusters/dev/local/migration", data={**data, "status": "done"}).status_code == 400
    assert tracked_client.post("/clusters/dev/local/migration", data={**data, "wave": "x" * 101}).status_code == 400
    assert tracked_client.post("/clusters/dev/local/migration", data={"status": "planned"}).status_code == 403
    assert "Not changed yet" in tracked_client.get("/clusters/dev/local").text


@pytest.mark.live
def test_viewer_can_edit_tracking(tracked_viewer):
    r = tracked_viewer.post("/clusters/dev/local/migration", data={"status": "planned", "csrf": tracked_viewer.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert _local(_csv_rows(tracked_viewer))["status"] == "planned"


@pytest.mark.live
def test_bulk_rejects_empty_and_bad_status(tracked_client, tracked_conn):
    base = {"csrf": tracked_client.csrf, "next": "/clusters"}
    assert tracked_client.post("/migration/bulk", data={**base, "status": "planned"}).status_code == 400
    assert tracked_client.post("/migration/bulk", data={**base, "cluster": "dev/local", "status": "done"}).status_code == 400
    assert tracked_client.post("/migration/bulk", data={**base, "cluster": "dev/local"}).status_code == 400
    assert tracked_client.post("/migration/bulk", data={**base, "cluster": ["dev/local", "dev/nope"], "status": "planned"}).status_code == 400
    assert tracked_client.post("/migration/bulk", data={**base, "cluster": "dev/local", "wave": "w", "clear_wave": "1"}).status_code == 400
    assert tracked_client.post("/migration/bulk", data={**base, "cluster": "dev/local", "wave": "x" * 101}).status_code == 400
    assert tracked_client.post("/migration/bulk", data={**base, "cluster": "nosite", "status": "planned"}).status_code == 400
    rows = _csv_rows(tracked_client)
    assert rows and "status" in rows[0]
    assert all(r["status"] == "not started" for r in rows)
    assert tracking.current(tracked_conn) == {}


@pytest.mark.live
def test_bulk_next_must_be_local(tracked_client):
    data = {"csrf": tracked_client.csrf, "cluster": "dev/local", "status": "planned"}
    for bad in ("//evil.example", "http://evil.example/", "/\\evil.example", ""):
        r = tracked_client.post("/migration/bulk", data={**data, "next": bad}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


@pytest.mark.live
def test_bulk_sets_status_and_clears_wave(tracked_client):
    tracked_client.post("/clusters/dev/local/migration", data={"status": "planned", "wave": "wave 1", "note": "n", "csrf": tracked_client.csrf})
    r = tracked_client.post("/migration/bulk", data={"csrf": tracked_client.csrf, "cluster": "dev/local", "status": "in progress", "clear_wave": "1", "next": "/clusters?status=in%20progress"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clusters?status=in%20progress"
    row = _local(_csv_rows(tracked_client))
    assert (row["status"], row["wave"], row["note"]) == ("in progress", "", "n")


@pytest.mark.live
def test_bulk_sets_wave_on_all(tracked_client, tracked_conn):
    ids = [r["cluster_id"] for r in tracked_conn.execute("SELECT cluster_id FROM clusters WHERE site='dev'")]
    r = tracked_client.post("/migration/bulk", data={"csrf": tracked_client.csrf, "cluster": [f"dev/{i}" for i in ids], "wave": "wave 2", "next": "/clusters"}, follow_redirects=False)
    assert r.status_code == 303
    assert {r["cluster_id"] for r in _csv_rows(tracked_client, "&wave=wave%202")} == set(ids)
    assert all(t["status"] == "not started" for t in tracking.current(tracked_conn).values())


@pytest.mark.live
def test_clusters_status_filter(tracked_client):
    assert "No clusters match." in tracked_client.get("/clusters?status=migrated").text
    assert "No clusters match." not in tracked_client.get("/clusters?status=not%20started").text
    assert tracked_client.get("/clusters?status=bogus").status_code == 400
    html = tracked_client.get("/clusters").text
    assert 'action="/migration/bulk"' in html and "data-select-all" in html and 'name="wave"' in html
    assert 'value="dev/local"' in html
    assert 'name="next" value="/clusters?site=dev&amp;view=table"' in tracked_client.get("/clusters?site=dev&view=table").text
    cards = tracked_client.get("/clusters?view=cards").text
    assert "<dt>Status</dt><dd>not started</dd>" in cards and 'name="cluster"' not in cards
    assert 'name="cluster"' in tracked_client.get("/").text


@pytest.mark.live
def test_removed_cluster_page(tracked_client, tracked_conn):
    tracked_conn.execute("INSERT INTO removed_clusters VALUES ('dev', 'c-gone', 'gone', '2026-11-10T00:00:00+00:00')")
    tracked_conn.commit()
    html = tracked_client.get("/clusters/dev/c-gone").text
    assert "Removed from Rancher" in html and 'id="migration"' in html
    assert tracked_client.get("/clusters/dev/c-never").status_code == 404
    r = tracked_client.post("/clusters/dev/c-gone/migration", data={"status": "decommissioned", "csrf": tracked_client.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert tracking.history(tracked_conn, "dev", "c-gone")[0]["name"] == "gone"


@pytest.mark.live
def test_overview_progress_and_removed(tracked_client, tracked_conn):
    tracked_conn.execute("INSERT INTO removed_clusters VALUES ('dev', 'c-gone', 'gone', '2026-11-10T00:00:00+00:00')")
    tracked_conn.commit()
    html = tracked_client.get("/").text
    assert html.index('id="progress-chart"') < html.index('id="capacity-chart"')
    assert 'id="removed"' in html and "not marked done" in html
    assert "2026-11-10" in html.split('id="removed"')[1]
    assert 'value="/#removed"' in html.split('id="removed"')[1]
    data = json.loads(re.search(r'id="overview-data">(.*?)</script>', html, re.S).group(1))
    assert data["progress"]["total"] == _count(tracked_conn) + 1
    other = tracked_client.get("/?site=other")
    assert other.status_code == 200
    assert 'id="removed"' in other.text
    assert "gone" not in other.text.split('id="removed"')[1]
    assert 'value="/?site=dev#removed"' in tracked_client.get("/?site=dev").text.split('id="removed"')[1]


# ---- overview scope ----

@pytest.mark.live
def test_charts_leave_out_finished_clusters(tracked_conn, tracked_settings):
    from inventory.collector.rules import load_rules
    from inventory.web.migration import site_overview

    rules = load_rules(tracked_settings)
    clusters = site_overview(tracked_conn, rules, scope="all")["clusters"]
    n = len(clusters)
    local = next(c for c in clusters if c["cluster_id"] == "local")
    tracking.save(tracked_conn, 1, local["site"], "local", local["name"], status="migrated")
    if n > 1:
        assert site_overview(tracked_conn, rules)["charts"]["capacity"]["clusters"] == n - 1
    for c in clusters:
        tracking.save(tracked_conn, 1, c["site"], c["cluster_id"], c["name"], status="migrated")
    rem = site_overview(tracked_conn, rules)
    assert rem["charts"]["capacity"]["clusters"] == 0
    assert rem["charts"]["os_images"] == [] and rem["charts"]["by_class"] == []
    assert rem["charts"]["pv_by_class"] == []
    assert rem["charts"]["scope"] == "remaining"
    assert rem["totals"]["clusters"] == n
    every = site_overview(tracked_conn, rules, scope="all")
    assert every["charts"]["capacity"]["clusters"] == n
    assert sum(x["nodes"] for x in every["charts"]["os_images"]) == every["totals"]["nodes"]
    with pytest.raises(ValueError):
        site_overview(tracked_conn, rules, scope="bogus")


@pytest.mark.live
def test_scope_links(tracked_client):
    html = tracked_client.get("/?scope=all&site=dev").text
    assert 'aria-current="page">All clusters' in html
    assert "scope=remaining&amp;site=dev" in html or "site=dev&amp;scope=remaining" in html
    assert 'aria-current="page">Remaining clusters' in tracked_client.get("/?scope=bogus").text


@pytest.mark.live
def test_site_picker_and_removed_form_keep_scope(tracked_client, tracked_conn):
    tracked_conn.execute("INSERT INTO removed_clusters VALUES ('dev', 'c-gone', 'gone', '2026-11-10T00:00:00+00:00')")
    tracked_conn.commit()
    html = tracked_client.get("/?scope=all&site=dev").text
    assert '<input type="hidden" name="scope" value="all">' in html
    assert 'value="/?site=dev&amp;scope=all#removed"' in html.split('id="removed"')[1]
    plain = tracked_client.get("/").text
    assert 'name="scope"' not in plain
    assert 'value="/#removed"' in plain.split('id="removed"')[1]


@pytest.mark.live
def test_removed_date_same_on_both_pages(tracked_client, tracked_conn):
    tracked_conn.execute("INSERT INTO removed_clusters VALUES ('dev', 'c-gone', 'gone', '2026-11-10T23:30:00+00:00')")
    tracked_conn.commit()
    assert "<td>2026-11-10</td>" in tracked_client.get("/").text
    assert "Removed from Rancher on 2026-11-10." in tracked_client.get("/clusters/dev/c-gone").text
