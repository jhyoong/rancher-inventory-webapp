import re
from pathlib import Path

import pytest

from inventory.collector.runner import Scope, create_run


@pytest.mark.parametrize("path", ["/", "/clusters", "/runs", "/admin/users", "/clusters/dev/local"])
def test_pages_need_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_htmx_without_session_gets_401(client):
    r = client.get("/runs/progress", headers={"HX-Request": "true"})
    assert r.status_code == 401


def test_healthz_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert set(r.json()) == {"ok"}
    assert r.json()["ok"] is False  # fresh DB: no good run yet


def test_healthz_with_session_has_detail(admin_client):
    body = admin_client.get("/healthz").json()
    assert body["ok"] is False and "data_as_of" in body


def test_login_sets_cookie_flags(client, test_password):
    r = client.post("/login", data={"username": "admin", "password": test_password}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=" in cookie


def test_login_wrong_password(client):
    r = client.post("/login", data={"username": "admin", "password": "wrong-password-x"})
    assert r.status_code == 401 and "Wrong username or password" in r.text


def test_login_locked(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "wrong-password-x"})
    r = client.post("/login", data={"username": "admin", "password": "wrong-password-x"})
    assert r.status_code == 429 and "Too many failed logins, try again later" in r.text


def test_logout_needs_csrf_and_ends_session(admin_client):
    assert admin_client.post("/logout", follow_redirects=False).status_code == 403
    r = admin_client.post("/logout", data={"csrf": admin_client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert admin_client.get("/", follow_redirects=False).status_code == 303


def test_home_page_for_viewer(viewer_client):
    r = viewer_client.get("/")
    assert r.status_code == 200 and "Data as of" in r.text
    assert "Refresh now" not in r.text  # admins only


def test_viewer_cannot_refresh(viewer_client):
    r = viewer_client.post("/runs", data={"csrf": viewer_client.csrf})
    assert r.status_code == 403


def test_viewer_cannot_open_admin_users(viewer_client):
    assert viewer_client.get("/admin/users").status_code == 403


def test_post_without_csrf_rejected(admin_client):
    assert admin_client.post("/runs").status_code == 403
    assert admin_client.post("/runs", data={"csrf": "bad"}).status_code == 403


def test_refresh_while_running_refused(admin_client, db):
    create_run(db, "manual", "t", Scope())
    r = admin_client.post("/runs", data={"csrf": admin_client.csrf})
    assert r.status_code == 409


def test_progress_idle_has_button_and_no_polling(admin_client):
    r = admin_client.get("/runs/progress")
    assert "Refresh now" in r.text and "hx-trigger" not in r.text


def test_progress_while_running_polls(admin_client, db):
    rid = create_run(db, "manual", "t", Scope())
    db.execute("UPDATE runs SET clusters_total=124 WHERE id=?", (rid,))
    db.executemany(
        "INSERT INTO run_clusters (run_id, site, cluster_id, status) VALUES (?, 'dev', ?, ?)",
        [(rid, f"c{i}", "ok") for i in range(35)] + [(rid, "f1", "failed"), (rid, "f2", "failed")],
    )
    r = admin_client.get("/runs/progress")
    assert "Refreshing: 37 of 124 clusters, 2 failed" in r.text
    assert 'hx-trigger="every 3s"' in r.text
    assert "Refresh now" not in r.text


def test_cancel_marks_run(admin_client, db):
    rid = create_run(db, "manual", "t", Scope())
    r = admin_client.post(f"/runs/{rid}/cancel", data={"csrf": admin_client.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert db.execute("SELECT cancel_requested FROM runs WHERE id=?", (rid,)).fetchone()[0] == 1


def test_admin_adds_and_disables_user(admin_client):
    data = {"csrf": admin_client.csrf, "action": "add", "username": "bob", "password": "bob-password-123", "role": "viewer"}
    r = admin_client.post("/admin/users", data=data)
    assert r.status_code == 200 and "bob" in r.text
    r = admin_client.post("/admin/users", data={"csrf": admin_client.csrf, "action": "disable", "username": "bob"})
    assert "disabled" in r.text


def test_unknown_cluster_404(admin_client):
    assert admin_client.get("/clusters/dev/nope").status_code == 404


def test_no_external_urls_in_templates():
    for p in Path("inventory/web/templates").glob("*.html"):
        assert not re.search(r'(src|href)="https?://', p.read_text())


def test_static_assets_are_js():
    for name in ("htmx.min.js", "chart.umd.min.js"):
        text = Path("inventory/web/static", name).read_text()
        assert len(text) > 40_000 and "<html" not in text.lower()[:500]


@pytest.mark.live
def test_clusters_page_shows_local(admin_client_collected):
    r = admin_client_collected.get("/clusters")
    assert "local" in r.text and "rke2" in r.text and "Data as of" in r.text


@pytest.mark.live
def test_run_page_lists_cluster(admin_client_collected):
    r = admin_client_collected.get("/runs/1")
    assert r.status_code == 200 and "local" in r.text and "ok" in r.text


@pytest.mark.live
def test_cluster_page_has_chart_data(admin_client_collected):
    r = admin_client_collected.get("/clusters/dev/local")
    assert r.status_code == 200 and 'id="metrics-data"' in r.text and "chart.umd.min.js" in r.text


@pytest.mark.live
def test_healthz_ok_after_collection(admin_client_collected):
    assert admin_client_collected.get("/healthz").json()["ok"] is True


def test_healthcheck_cli_fails_without_server(monkeypatch):
    from inventory.cli import main

    monkeypatch.setenv("LISTEN_PORT", "1")  # nothing listens here
    monkeypatch.setenv("TLS_CERT_FILE", "")
    assert main(["healthcheck"]) == 1
