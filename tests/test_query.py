import json
import shutil
import sqlite3
from fastapi.testclient import TestClient

import pytest

from inventory.db.connection import connect, run_readonly_query
from inventory.web import queries
from inventory.web.app import create_app
from inventory.web.queries import build_query, check_preset


def test_unknown_filter_rejected():
    with pytest.raises(ValueError):
        build_query({"data": "x"})


def test_values_are_parameters():
    sql, params = build_query({"name": "x'; DROP TABLE users; --"})
    assert "DROP" not in sql and params


def test_label_filter_forms():
    sql, params = build_query({"label": "app=web"})
    assert params["label_key"] == "app" and params["label_value"] == "web"
    _, params = build_query({"label": "app"})
    assert "label_value" not in params


def test_query_page_empty_form_only(admin_client):
    r = admin_client.get("/query")
    assert r.status_code == 200 and "<table" not in r.text
    assert admin_client.get("/query?kind=pods").status_code == 200


def test_pages_need_login(client):
    for path in ("/query", "/presets", "/check"):
        assert client.get(path, follow_redirects=False).status_code == 303


def test_preset_permissions(web_app, web_settings, viewer_client, open_db):
    r = viewer_client.post(
        "/presets",
        data={"name": "p1", "filters": json.dumps({"kind": "pods"}), "csrf": viewer_client.csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert viewer_client.post("/presets", data={"name": "p1", "filters": '{"kind":"pods"}', "csrf": viewer_client.csrf}).status_code == 400
    assert viewer_client.post("/presets", data={"name": "p2", "filters": '{"data":"x"}', "csrf": viewer_client.csrf}).status_code == 400
    assert viewer_client.post("/presets", data={"name": "p3", "filters": '{"kind":"pods"}'}).status_code == 403
    assert "p1" in viewer_client.get("/presets").text
    conn = open_db(web_settings.db_path)
    pid = conn.execute("SELECT id FROM presets WHERE name='p1'").fetchone()["id"]
    # a second viewer cannot delete it
    from inventory.web.auth import create_user

    create_user(conn, "other", "other-password-1", "viewer")
    conn.close()
    other = TestClient(web_app)
    other.post("/login", data={"username": "other", "password": "other-password-1"})
    from inventory.web.auth import csrf_token

    tok = csrf_token(web_settings.session_secret, other.cookies["session"])
    assert other.post(f"/presets/{pid}/delete", data={"csrf": tok}).status_code == 403
    assert viewer_client.post(f"/presets/{pid}/delete", data={"csrf": viewer_client.csrf}, follow_redirects=False).status_code == 303
    assert "p1" not in viewer_client.get("/presets").text


@pytest.mark.live
def test_query_finds_coredns(collected_db, open_db):
    conn = open_db(collected_db.db_path, readonly=True)
    try:
        sql, p = build_query({"kind": "deployments", "name": "coredns"})
        assert any(r["cluster_id"] == "local" for r in run_readonly_query(conn, sql, p))
        sql, p = build_query({"cluster": "local", "kind": "namespaces", "namespace": ""})
        assert run_readonly_query(conn, sql, p)
    finally:
        conn.close()


@pytest.mark.live
def test_query_page_live(admin_client_collected):
    r = admin_client_collected.get("/query?kind=deployments&name=coredns")
    assert r.status_code == 200 and "coredns" in r.text and "Save preset" in r.text


@pytest.mark.live
def test_check_present_and_unknown(collected_db, tmp_path):
    copy = tmp_path / "copy.db"
    shutil.copy(collected_db.db_path, copy)
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        def add(name, filters):
            return conn.execute(
                "INSERT INTO presets (name, filters, created_at) VALUES (?, ?, 'now')",
                (name, json.dumps(filters)),
            ).lastrowid

        def local(rows):
            return next(r for r in rows if r["cluster_id"] == "local")["result"]

        present = add("a", {"kind": "deployments", "name": "coredns"})
        absent = add("b", {"kind": "deployments", "name": "no-such-deployment-xyz"})
        assert local(check_preset(conn, present)) == "present"
        assert local(check_preset(conn, absent)) == "absent"
        conn.execute("UPDATE run_clusters SET status='failed' WHERE cluster_id='local'"
                     " AND run_id=(SELECT MAX(run_id) FROM run_clusters WHERE cluster_id='local')")
        assert local(check_preset(conn, present)) == "unknown"
        conn.execute("UPDATE run_clusters SET status='ok' WHERE cluster_id='local'")
        conn.execute("UPDATE run_kinds SET status='failed' WHERE cluster_id='local' AND kind='deployments'")
        assert local(check_preset(conn, present)) == "unknown"
    finally:
        conn.close()
