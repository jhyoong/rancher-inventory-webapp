from inventory.db.connection import connect
from inventory.web import custom_columns as cc

T0 = "2026-11-01T00:00:00+00:00"


def _post(c, **data):
    return c.post("/admin/columns", data={"csrf": c.csrf, **data})


def _cols(web_settings):
    conn = connect(web_settings.db_path)
    try:
        return cc.list_columns(conn)
    finally:
        conn.close()


def test_viewer_cannot_manage_columns(viewer_client):
    assert viewer_client.get("/admin/columns").status_code == 403
    r = viewer_client.post("/admin/columns", data={"csrf": viewer_client.csrf, "action": "add",
                                                   "name": "Tier", "type": "text"})
    assert r.status_code == 403


def test_add_choice_column_and_list(admin_client, web_settings):
    r = _post(admin_client, action="add", name="Tier", type="choice", choices="critical\nstandard\n\ndev")
    assert r.status_code == 200  # redirect followed
    assert [c.choices for c in _cols(web_settings)] == [["critical", "standard", "dev"]]
    body = admin_client.get("/admin/columns").text
    assert "<td>critical, standard, dev</td>" in body
    assert 'href="/admin/columns"' in body


def test_add_redirects_303(admin_client):
    r = admin_client.post("/admin/columns", data={"csrf": admin_client.csrf, "action": "add", "name": "Owner",
                                                  "type": "text"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/columns"


def test_add_rejects_none_choice(admin_client, web_settings):
    r = _post(admin_client, action="add", name="Tier", type="choice", choices="None\nx")
    assert r.status_code == 400
    assert "reserved" in r.text
    assert _cols(web_settings) == []


def test_remove_used_choice_shows_links(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        col = cc.create_column(conn, "Tier", "choice", ["dev", "prod"])
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c-live','live')")
        conn.execute("INSERT INTO removed_clusters (site, cluster_id, name, removed_at) VALUES ('dev','c-gone','gone',?)", (T0,))
        for cid in ("c-live", "c-gone"):
            conn.execute("INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at)"
                         " VALUES ('dev',?,?,'dev',1,?)", (cid, col, T0))
    finally:
        conn.close()
    r = _post(admin_client, action="choices", column_id=str(col), choices="prod")
    assert r.status_code == 400
    assert f'/clusters?cf{col}=dev' in r.text
    assert "/clusters/dev/c-gone" in r.text
    assert [c.choices for c in _cols(web_settings)] == [["dev", "prod"]]


def test_rename_move_and_type(admin_client, web_settings):
    _post(admin_client, action="add", name="A", type="text")
    _post(admin_client, action="add", name="B", type="text")
    a, b = [c.id for c in _cols(web_settings)]
    _post(admin_client, action="move", column_id=str(b), direction="up")
    assert [c.id for c in _cols(web_settings)] == [b, a]
    _post(admin_client, action="rename", column_id=str(a), name="Alpha")
    _post(admin_client, action="type", column_id=str(a), type="choice", choices="x\ny")
    col = [c for c in _cols(web_settings) if c.id == a][0]
    assert (col.name, col.type, col.choices) == ("Alpha", "choice", ["x", "y"])
    assert _post(admin_client, action="rename_choice", column_id=str(a), old="x", new="z").status_code == 200
    assert [c for c in _cols(web_settings) if c.id == a][0].choices == ["z", "y"]


def test_delete_needs_confirmation_page(admin_client, web_settings):
    _post(admin_client, action="add", name="Tier", type="text")
    col = _cols(web_settings)[0].id
    conn = connect(web_settings.db_path)
    try:
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c-1','one')")
        conn.execute("INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at)"
                     " VALUES ('dev','c-1',?,'v',1,?)", (col, T0))
    finally:
        conn.close()
    r = admin_client.get(f"/admin/columns/{col}/delete")
    assert r.status_code == 200 and "1 cluster" in r.text
    assert len(_cols(web_settings)) == 1
    r = admin_client.post(f"/admin/columns/{col}/delete", data={"csrf": admin_client.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert _cols(web_settings) == []


def test_post_without_csrf_rejected(admin_client, web_settings):
    r = admin_client.post("/admin/columns", data={"action": "add", "name": "Tier", "type": "text"})
    assert r.status_code == 403
    assert _cols(web_settings) == []


def _seed_cluster(web_settings, removed=False):
    conn = connect(web_settings.db_path)
    try:
        tier = cc.create_column(conn, "Tier", "choice", ["critical", "standard"])
        owner = cc.create_column(conn, "Owner", "text", [])
        if removed:
            conn.execute("INSERT INTO removed_clusters (site, cluster_id, name, removed_at) VALUES ('dev','c-gone','gone',?)", (T0,))
        else:
            conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
    finally:
        conn.close()
    return tier, owner


def _stored(web_settings):
    conn = connect(web_settings.db_path)
    try:
        return cc.values(conn)
    finally:
        conn.close()


def test_viewer_sets_values_on_cluster_page(viewer_client, web_settings):
    tier, owner = _seed_cluster(web_settings)
    r = viewer_client.post("/clusters/dev/c1/custom", data={"csrf": viewer_client.csrf, f"cf{tier}": "critical",
                           f"cf{owner}": "", "cf99999": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clusters/dev/c1#custom"
    assert _stored(web_settings) == {("dev", "c1"): {tier: "critical"}}
    body = viewer_client.get("/clusters/dev/c1").text
    assert '<h2 id="custom">Custom fields</h2>' in body
    assert '<option value="critical" selected>' in body
    assert "Tier: critical" in body


def test_removed_cluster_page_edits_values(admin_client, web_settings):
    tier, owner = _seed_cluster(web_settings, removed=True)
    r = admin_client.post("/clusters/dev/c-gone/custom", data={"csrf": admin_client.csrf, f"cf{owner}": "alice"})
    assert r.status_code == 200
    assert _stored(web_settings) == {("dev", "c-gone"): {owner: "alice"}}
    body = admin_client.get("/clusters/dev/c-gone").text
    assert 'id="custom"' in body and 'value="alice"' in body and "Owner: alice" in body


def test_cluster_post_rejects_none_text(admin_client, web_settings):
    tier, owner = _seed_cluster(web_settings)
    r = admin_client.post("/clusters/dev/c1/custom", data={"csrf": admin_client.csrf, f"cf{owner}": "None"})
    assert r.status_code == 400
    assert _stored(web_settings) == {}


def test_unknown_cluster_404(admin_client, web_settings):
    tier, owner = _seed_cluster(web_settings)
    r = admin_client.post("/clusters/dev/nope/custom", data={"csrf": admin_client.csrf, f"cf{owner}": "a"})
    assert r.status_code == 404


def test_no_custom_section_without_columns(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
    conn.close()
    assert 'id="custom"' not in admin_client.get("/clusters/dev/c1").text


def _seed_two(web_settings):
    tier, owner = _seed_cluster(web_settings)
    conn = connect(web_settings.db_path)
    try:
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c2','two')")
    finally:
        conn.close()
    return tier, owner


def _bulk(c, **data):
    return c.post("/migration/bulk", data={"csrf": c.csrf, "cluster": ["dev/c1", "dev/c2"], **data},
                  follow_redirects=False)


def _count(web_settings, sql):
    conn = connect(web_settings.db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_column_id_from_key():
    assert cc.column_id_from_key("cf12") == 12
    for bad in ("cf", "cf\u00b2", "cf\u0663", "cf1_clear", "xcf1", "cf-1", "cf1\n"):
        assert cc.column_id_from_key(bad) is None


def test_cluster_custom_ignores_non_ascii_digit_key(admin_client, web_settings):
    _seed_cluster(web_settings)
    r = admin_client.post("/clusters/dev/c1/custom", data={"csrf": admin_client.csrf, "cf\u00b2": "x"},
                          follow_redirects=False)
    assert r.status_code == 303


def test_bulk_custom_only(admin_client, web_settings):
    tier, owner = _seed_two(web_settings)
    r = _bulk(admin_client, **{f"cf{tier}": "standard", f"cf{owner}": "", "cf99999": "x", "cf\u00b2": "x"})
    assert r.status_code == 303
    assert _stored(web_settings) == {("dev", "c1"): {tier: "standard"}, ("dev", "c2"): {tier: "standard"}}
    assert _count(web_settings, "SELECT COUNT(*) FROM migration_tracking") == 0


def test_bulk_clear_choice_and_text(admin_client, web_settings):
    tier, owner = _seed_two(web_settings)
    conn = connect(web_settings.db_path)
    try:
        for cid in ("c1", "c2"):
            cc.save_values(conn, 1, "dev", cid, cid, {tier: "critical", owner: "alice"})
    finally:
        conn.close()
    r = _bulk(admin_client, **{f"cf{tier}": "none", f"cf{owner}_clear": "1"})
    assert r.status_code == 303
    assert _stored(web_settings) == {}
    assert _count(web_settings, "SELECT COUNT(*) FROM custom_values_log WHERE value=''") == 4


def test_bulk_text_value_and_clear_rejected(admin_client, web_settings):
    tier, owner = _seed_two(web_settings)
    r = _bulk(admin_client, **{f"cf{owner}": "bob", f"cf{owner}_clear": "1"})
    assert r.status_code == 400
    assert _stored(web_settings) == {}


def test_bulk_nothing_to_change(admin_client, web_settings):
    tier, owner = _seed_two(web_settings)
    r = _bulk(admin_client, **{f"cf{tier}": "", f"cf{owner}": ""})
    assert r.status_code == 400 and "nothing to change" in r.text


def test_bulk_rejects_invalid_choice_atomically(admin_client, web_settings):
    tier, owner = _seed_two(web_settings)
    before = _count(web_settings, "SELECT COUNT(*) FROM migration_tracking")
    r = _bulk(admin_client, status="planned", **{f"cf{tier}": "bogus"})
    assert r.status_code == 400
    assert _stored(web_settings) == {}
    assert _count(web_settings, "SELECT COUNT(*) FROM migration_tracking") == before


def test_bulk_failure_mid_transaction_rolls_back_tracking(admin_client, web_settings, monkeypatch):
    tier, owner = _seed_two(web_settings)
    real = cc.save_values
    calls = []

    def boom(conn, *a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("boom")
        return real(conn, *a, **k)

    monkeypatch.setattr(cc, "save_values", boom)
    before = _count(web_settings, "SELECT COUNT(*) FROM migration_tracking")
    r = _bulk(admin_client, status="planned", **{f"cf{tier}": "standard"})
    assert r.status_code == 400
    assert _stored(web_settings) == {}
    assert _count(web_settings, "SELECT COUNT(*) FROM migration_tracking") == before


def test_bulk_bar_shows_controls_on_home(admin_client, web_settings):
    tier, owner = _seed_two(web_settings)
    body = admin_client.get("/").text
    assert f'name="cf{tier}"' in body and f'name="cf{owner}_clear"' in body
    assert 'value="none">clear</option>' in body


def test_removed_table_has_no_custom_controls(admin_client, web_settings):
    tier, owner = _seed_cluster(web_settings, removed=True)
    body = admin_client.get("/").text
    assert 'id="removed"' in body and "c-gone" in body
    removed = body[body.index('id="removed"'):]
    assert 'name="cf' not in removed


def _seed_tier(web_settings):
    """Tier choices critical/standard/dev; c1 = dev, c2 unset. Returns the Tier column id."""
    conn = connect(web_settings.db_path)
    try:
        tier = cc.create_column(conn, "Tier", "choice", ["critical", "standard", "dev"])
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c2','two')")
        conn.execute(
            "INSERT INTO custom_values (site, cluster_id, column_id, value, updated_at) VALUES ('dev','c1',?,'dev',?)",
            (tier, T0),
        )
    finally:
        conn.close()
    return tier


def test_table_shows_custom_columns_with_sort_keys(admin_client, web_settings):
    _seed_tier(web_settings)
    body = admin_client.get("/clusters").text
    assert "<th>Tier</th>" in body
    assert 'data-sort="2">dev</td>' in body
    assert 'data-sort-last="1">none<' in body


def test_overview_links_value_to_filter(admin_client, web_settings):
    tier = _seed_tier(web_settings)
    assert f'href="/clusters?cf{tier}=dev"' in admin_client.get("/").text
    assert f'href="/clusters?cf{tier}=dev"' not in admin_client.get("/clusters").text


def test_text_value_sorts_by_its_text(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        owner = cc.create_column(conn, "Owner", "text", [])
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
        conn.execute(
            "INSERT INTO custom_values (site, cluster_id, column_id, value, updated_at) VALUES ('dev','c1',?,'Team A&B',?)",
            (owner, T0),
        )
    finally:
        conn.close()
    assert 'data-sort="Team A&amp;B"><a href="/clusters?cf%d=Team%%20A%%26B">Team A&amp;B</a>' % owner in admin_client.get("/").text


def test_csv_has_custom_columns(admin_client, web_settings):
    import csv
    import io

    _seed_tier(web_settings)
    rows = list(csv.DictReader(io.StringIO(admin_client.get("/clusters?format=csv").text)))
    assert {r["name"]: r["custom_Tier"] for r in rows} == {"one": "dev", "two": "none"}
    assert "custom" not in rows[0]


def _seed_filter(web_settings):
    tier = _seed_tier(web_settings)
    conn = connect(web_settings.db_path)
    try:
        owner = cc.create_column(conn, "Owner", "text", [])
        conn.execute(
            "INSERT INTO custom_values (site, cluster_id, column_id, value, updated_at) VALUES ('dev','c1',?,'Payments',?)",
            (owner, T0),
        )
    finally:
        conn.close()
    return tier, owner


def _names(client, qs):
    import csv
    import io

    r = client.get("/clusters?format=csv&" + qs)
    assert r.status_code == 200
    return sorted(x["name"] for x in csv.DictReader(io.StringIO(r.text)))


def test_filter_choice_none_and_text_case(admin_client, web_settings):
    tier, owner = _seed_filter(web_settings)
    assert _names(admin_client, f"cf{tier}=dev") == ["one"]
    assert _names(admin_client, f"cf{tier}=none") == ["two"]
    assert _names(admin_client, f"cf{owner}=payments") == ["one"]
    assert _names(admin_client, f"cf{owner}=%20payments%20") == ["one"]
    assert _names(admin_client, f"cf{owner}=") == ["one", "two"]


def test_filter_combines_with_status(admin_client, web_settings):
    tier, _ = _seed_filter(web_settings)
    assert _names(admin_client, f"cf{tier}=dev&status=not%20started") == ["one"]
    assert _names(admin_client, f"cf{tier}=dev&status=planned") == []
    assert _names(admin_client, f"cf{tier}=none&status=not%20started") == ["two"]
    assert admin_client.get(f"/clusters?cf{tier}=dev&status=bogus").status_code == 400


def test_filter_form_controls_and_selected(admin_client, web_settings):
    tier, owner = _seed_filter(web_settings)
    body = admin_client.get(f"/clusters?cf{tier}=none&cf{owner}=abc").text
    assert f'name="cf{tier}"' in body and f'name="cf{owner}"' in body
    assert '<option value="none" selected>none</option>' in body
    assert 'placeholder="exact value"' in body and 'value="abc"' in body


def test_stale_bookmark_loads_with_notice(admin_client, web_settings):
    tier, _ = _seed_filter(web_settings)
    r = admin_client.get(f"/clusters?cf999=x&cf{tier}=bogus")
    assert r.status_code == 200
    assert "Ignored filter cf999: the column no longer exists" in r.text
    assert "Ignored filter Tier: &#34;bogus&#34; is not a choice" in r.text
    assert 'class="warn"' in r.text
    assert "one" in r.text and "two" in r.text


def test_choice_to_text_from_admin_form_with_prefilled_choices(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        col = cc.create_column(conn, "Tier", "choice", ["dev", "prod"])
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
        conn.execute("INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at)"
                     " VALUES ('dev','c1',?,'dev',1,?)", (col, T0))
    finally:
        conn.close()
    r = admin_client.post("/admin/columns", data={"csrf": admin_client.csrf, "action": "type",
                                                  "column_id": str(col), "type": "text",
                                                  "choices": "dev\nprod"}, follow_redirects=False)
    assert r.status_code == 303
    c = _cols(web_settings)[0]
    assert (c.type, c.choices) == ("text", [])
    assert _stored(web_settings) == {("dev", "c1"): {col: "dev"}}


def test_add_text_column_ignores_choices_text(admin_client, web_settings):
    r = admin_client.post("/admin/columns", data={"csrf": admin_client.csrf, "action": "add", "name": "Owner",
                                                  "type": "text", "choices": "x"}, follow_redirects=False)
    assert r.status_code == 303
    assert _cols(web_settings)[0].choices == []


def test_text_to_choice_blocked_lists_removed_cluster(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        col = cc.create_column(conn, "Owner", "text", [])
        conn.execute("INSERT INTO removed_clusters (site, cluster_id, name, removed_at) VALUES ('dev','c-gone','gone',?)", (T0,))
        conn.execute("INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at)"
                     " VALUES ('dev','c-gone',?,'weird',1,?)", (col, T0))
    finally:
        conn.close()
    r = _post(admin_client, action="type", column_id=str(col), type="choice", choices="ok")
    assert r.status_code == 400
    assert "/clusters/dev/c-gone" in r.text
    assert f"/clusters?cf{col}=weird" in r.text
    assert _cols(web_settings)[0].type == "text"


def test_choice_with_double_space_is_submitted_exactly(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        col = cc.create_column(conn, "Tier", "choice", ["a  b"])
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
    finally:
        conn.close()
    assert 'value="a  b"' in admin_client.get("/").text
    assert 'value="a  b"' in admin_client.get("/admin/columns").text


def test_in_use_message_points_to_rename_choice(admin_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        col = cc.create_column(conn, "Tier", "choice", ["dev", "prod"])
        conn.execute("INSERT INTO clusters (site, cluster_id, name) VALUES ('dev','c1','one')")
        conn.execute("INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at)"
                     " VALUES ('dev','c1',?,'dev',1,?)", (col, T0))
    finally:
        conn.close()
    r = _post(admin_client, action="choices", column_id=str(col), choices="prod")
    assert "Rename choice" in r.text


def test_viewer_cannot_delete_column(viewer_client, web_settings):
    conn = connect(web_settings.db_path)
    try:
        col = cc.create_column(conn, "Tier", "text", [])
    finally:
        conn.close()
    assert viewer_client.get(f"/admin/columns/{col}/delete").status_code == 403
    r = viewer_client.post(f"/admin/columns/{col}/delete", data={"csrf": viewer_client.csrf})
    assert r.status_code == 403
    assert len(_cols(web_settings)) == 1
