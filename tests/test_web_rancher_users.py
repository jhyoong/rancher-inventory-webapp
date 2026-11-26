import json
from urllib.parse import quote

from inventory.db.connection import connect

T0 = "2026-11-01T00:00:00+00:00"
ANN = "activedirectory_user://CN=Ann Lee,OU=Staff,DC=corp"
OWN = [{"role": "Cluster Owner", "template": "cluster-owner", "scope": "cluster"}]
PROJ = [{"role": "Project Member", "template": "project-member", "scope": "project: Web"}]


def _seed(web_settings):
    conn = connect(web_settings.db_path)
    try:
        for site, cid, status, at in (("east", "e1", "ok", T0), ("east", "e2", "failed", T0), ("east", "e3", None, None),
                                      ("west", "w1", "ok", T0)):
            conn.execute("INSERT INTO clusters (site, cluster_id, name, members_status, members_at) VALUES (?,?,?,?,?)",
                         (site, cid, "n-" + cid, status, at))

        def member(site, cid, kind, subject, name, username, disabled, grants, principal, ord_=0):
            conn.execute(
                "INSERT INTO cluster_members (site, cluster_id, kind, subject, display_name, username, disabled, grants,"
                " ord, principal) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (site, cid, kind, subject, name, username, disabled, json.dumps(grants), ord_, principal))

        # Ann: same AD principal, different Rancher ids per site; e2 is stale; e3 never collected (not counted)
        member("east", "e1", "user", "u-a1", "Ann Lee", "ann", 0, OWN, ANN)
        member("east", "e2", "user", "u-a1", "Ann Lee", "ann", 0, PROJ, ANN)
        member("east", "e3", "user", "u-a1", "Ann Lee", "ann", 0, OWN, ANN)
        member("west", "w1", "user", "u-zz", "Ann L.", "ann", 1, PROJ, ANN)
        # local accounts with the same id on two sites stay separate
        member("east", "e1", "user", "u-loc", "Local Larry", "larry", 0, OWN, None, 1)
        member("west", "w1", "user", "u-loc", "Local Larry", "larry", 0, OWN, None, 1)
        member("east", "e1", "group", "activedirectory_group://CN=Ops,DC=corp", "Ops", None, 0, OWN, None, 2)
    finally:
        conn.close()


def test_list_matches_by_principal(viewer_client, web_settings):
    _seed(web_settings)
    body = viewer_client.get("/rancher-users").text
    assert 'href="/rancher-users"' in body  # nav tab
    assert "matched across sites by AD principal" in body
    assert body.count("Local Larry") == 2  # one row per site
    assert "Ops" not in body.split("<table>")[1]  # groups excluded
    row = body.split("Ann Lee</a>")[1].split("</tr>")[0]
    assert "also: Ann L." in row and "<td>3</td>" in row and "east, west" in row and "on some sites" in row
    assert body.index("Ann Lee") < body.index("Local Larry")  # most clusters first


def test_list_filters(viewer_client, web_settings):
    _seed(web_settings)
    assert "Local Larry" not in viewer_client.get("/rancher-users?q=staff").text
    assert "Ann Lee" in viewer_client.get("/rancher-users?q=OU%3DSTAFF").text
    west = viewer_client.get("/rancher-users?site=west").text
    assert "Ann Lee" in west and west.count("Local Larry") == 1
    assert "No Rancher users match." in viewer_client.get("/rancher-users?q=nobody").text


def test_user_page_by_principal(viewer_client, web_settings):
    _seed(web_settings)
    body = viewer_client.get("/rancher-users/user?p=" + quote(ANN, safe="")).text
    assert "<h1>Ann Lee</h1>" in body
    for cid in ("e1", "e2", "w1"):
        assert f'href="/clusters/{"west" if cid == "w1" else "east"}/{cid}#members"' in body
    assert "n-e3" not in body
    assert "(stale)" in body.split("n-e2")[1].split("</td>")[0]
    assert "Cluster Owner" in body and "project: Web" in body


def test_user_page_link_round_trip(viewer_client, web_settings):
    _seed(web_settings)
    body = viewer_client.get("/rancher-users").text
    href = body.split("Ann Lee</a>")[0].rsplit('href="', 1)[1].split('"')[0].replace("&amp;", "&")
    assert "<h1>Ann Lee</h1>" in viewer_client.get(href).text
    local = viewer_client.get("/rancher-users/user?site=west&id=u-loc").text
    assert "local / unmatched" in local and "n-w1" in local and "n-e1" not in local


def test_user_page_not_found(viewer_client, web_settings):
    _seed(web_settings)
    assert viewer_client.get("/rancher-users/user?p=nobody").status_code == 404
    assert viewer_client.get("/rancher-users/user").status_code == 404


def test_empty_state(viewer_client):
    assert "No cluster members collected yet." in viewer_client.get("/rancher-users").text
