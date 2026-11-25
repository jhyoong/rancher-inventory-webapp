import csv
import io
import json

from inventory.db.connection import connect

T0 = "2026-11-01T00:00:00+00:00"


def _seed(web_settings):
    conn = connect(web_settings.db_path)
    try:
        for cid, status, at in (("c1", "ok", T0), ("c2", None, None), ("c3", "failed", T0), ("c4", "failed", None),
                                ("c5", "ok", T0)):
            conn.execute("INSERT INTO clusters (site, cluster_id, name, members_status, members_at) VALUES ('dev',?,?,?,?)",
                         (cid, "n-" + cid, status, at))

        def member(cid, kind, subject, name, username, disabled, grants, ord_):
            conn.execute("INSERT INTO cluster_members VALUES ('dev',?,?,?,?,?,?,?,?)",
                         (cid, kind, subject, name, username, disabled, json.dumps(grants), ord_))

        own = [{"role": "Cluster Owner", "template": "cluster-owner", "scope": "cluster"}]
        member("c1", "user", "u-1", "Alice Smith", "atan", 0, own, 0)
        member("c1", "user", "u-2", "Bob Gone", "bobg", 1, [{"role": "Project Member", "template": "project-member", "scope": "project Default"}], 1)
        member("c1", "group", "g-1", "Platform Team", None, 0, [{"role": "Cluster Member", "template": "cluster-member", "scope": "cluster"}], 2)
        member("c3", "user", "u-3", "Carol Old", "carol", 0, own, 0)
    finally:
        conn.close()


def test_members_column_and_sort_text(admin_client, web_settings):
    _seed(web_settings)
    body = admin_client.get("/clusters").text
    assert "<th>Members</th>" in body
    assert "1 user, 1 group" in body  # disabled Bob not counted
    assert "0 users, 0 groups" in body  # c5 ok but empty
    assert "not collected" in body
    cards = admin_client.get("/clusters?view=cards").text
    assert "<dt>Members</dt><dd>1 user, 1 group</dd>" in cards


def test_stale_cell(admin_client, web_settings):
    _seed(web_settings)
    body = admin_client.get("/clusters").text
    assert 'title="last collected 2026-11-01 00:00 UTC"' in body
    assert "1 user, 0 groups (stale)" in body
    # failed with no members_at reads as not collected
    from inventory.web.migration import members_cell
    assert members_cell({"members_status": "failed", "members_at": None, "member_users": None, "member_groups": None}) == "not collected"
    assert members_cell({"members_status": "forbidden", "members_at": None, "member_users": None, "member_groups": None}) == "not collected"


def test_member_filter_matches_name_or_username(admin_client, web_settings):
    _seed(web_settings)
    for q in ("ALICE", "atan", "platform", "bobg"):
        body = admin_client.get(f"/clusters?member={q}").text
        assert "n-c1" in body and "n-c3" not in body and "n-c5" not in body, q
    assert "No clusters match" in admin_client.get("/clusters?member=nobody").text


def test_cluster_page_lists_members_owner_first(admin_client, web_settings):
    _seed(web_settings)
    body = admin_client.get("/clusters/dev/c1").text
    assert '<h2 id="members">Members</h2>' in body
    assert body.index("Alice Smith") < body.index("Bob Gone") < body.index("Platform Team")
    assert "disabled" in body and "Cluster Owner" in body and "project Default" in body
    assert "Rancher global admins can also access this cluster." in body


def test_cluster_page_states(admin_client, web_settings):
    _seed(web_settings)
    assert "Allow reading cluster members" in admin_client.get("/clusters/dev/c2").text
    stale = admin_client.get("/clusters/dev/c3").text
    assert "Carol Old" in stale and "2026-11-01" in stale and "failed" in stale
    assert "No members besides global admins." in admin_client.get("/clusters/dev/c5").text


def test_csv_counts_no_names(admin_client, web_settings):
    _seed(web_settings)
    text = admin_client.get("/clusters?format=csv").text
    for name in ("Alice", "atan", "Platform", "Bob", "Carol"):
        assert name not in text
    rows = {r["cluster_id"]: r for r in csv.DictReader(io.StringIO(text))}
    assert (rows["c1"]["member_users"], rows["c1"]["member_groups"], rows["c1"]["members_status"]) == ("1", "1", "ok")
    assert rows["c2"]["members_status"] == "not collected" and rows["c2"]["member_users"] == "not collected"
    assert rows["c3"]["members_status"] == "stale" and rows["c3"]["member_users"] == "1"
    assert rows["c4"]["members_status"] == "not collected"


def test_ok_forbidden_failed_cell_is_not_collected(admin_client, web_settings):
    from inventory.collector.members import Member, MembersResult
    from inventory.db.repo import write_members

    conn = connect(web_settings.db_path)
    try:
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','z1','n-z1')")
        m = Member("user", "u-1", "ann", "ann", False, [])
        write_members(conn, "dev", "z1", MembersResult("ok", {"z1": [m]}), T0)
        write_members(conn, "dev", "z1", MembersResult("forbidden"), T0)
        write_members(conn, "dev", "z1", MembersResult("failed", {}, "x"), T0)
    finally:
        conn.close()
    body = admin_client.get("/clusters").text
    assert "0 users, 0 groups" not in body
    assert "not collected" in body
    page = admin_client.get("/clusters/dev/z1").text
    assert "No members besides global admins." not in page


def test_member_filter_casefolds_non_ascii(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        conn.execute("INSERT INTO clusters (site, cluster_id, name, members_status, members_at) VALUES ('dev','k1','n-k1','ok',?)", (T0,))
        conn.execute("INSERT INTO clusters (site, cluster_id, name, members_status, members_at) VALUES ('dev','k2','n-k2','ok',?)", (T0,))
        conn.execute("INSERT INTO cluster_members VALUES ('dev','k1','user','u-1',?,?,0,'[]',0)", ("ÖSTERBERG", "oster"))
        conn.execute("INSERT INTO cluster_members VALUES ('dev','k2','user','u-2','Plain','plain',0,'[]',0)")
    finally:
        conn.close()
    body = admin_client.get("/clusters?member=österberg").text
    assert "n-k1" in body and "n-k2" not in body
    body = admin_client.get("/clusters?member=50%25").text
    assert "No clusters match" in body


def test_cluster_page_failed_without_date_has_distinct_message(admin_client, web_settings):
    _seed(web_settings)
    page = admin_client.get("/clusters/dev/c4").text
    assert "The member read failed and no earlier list exists." in page
    assert "Allow reading cluster members" not in page
    assert "Allow reading cluster members" in admin_client.get("/clusters/dev/c2").text


def test_stale_tooltip_uses_dt_filter(admin_client, web_settings):
    _seed(web_settings)
    body = admin_client.get("/clusters").text
    assert 'title="last collected 2026-11-01T00:00:00+00:00"' not in body
    assert 'title="last collected 2026-11-01' in body
