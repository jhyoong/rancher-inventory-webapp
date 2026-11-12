import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from inventory.cli import backup, main
from inventory.web.queries import token_warnings


def test_backup_while_writing(db_path, db, tmp_path):
    db.execute("CREATE TABLE t (x INTEGER)")
    stop = threading.Event()

    def writer():
        from inventory.db.connection import connect

        conn = connect(db_path)
        i = 0
        while not stop.is_set():
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            i += 1
        conn.close()

    th = threading.Thread(target=writer)
    th.start()
    try:
        time.sleep(0.2)
        backup(db_path, tmp_path / "copy.db")
    finally:
        stop.set()
        th.join()
    copy = sqlite3.connect(tmp_path / "copy.db")
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] > 0
    assert copy.execute("SELECT count(*) FROM users").fetchone()[0] == 0  # the schema came along


def test_backup_refuses_existing_file(db_path, db, tmp_path):
    (tmp_path / "copy.db").write_text("x")
    with pytest.raises(FileExistsError):
        backup(db_path, tmp_path / "copy.db")


def test_backup_cli(db_path, db, tmp_path, capsys):
    assert main(["backup", str(tmp_path / "b.db")]) == 0
    assert "integrity ok" in capsys.readouterr().out
    assert main(["backup", str(tmp_path / "b.db")]) == 1


def _today() -> date:
    return datetime.now(timezone.utc).date()  # the app counts days in UTC


def _sites(tmp_path, monkeypatch, settings, expires: date):
    f = tmp_path / "sites.yaml"
    f.write_text(
        "sites:\n"
        f"  - {{name: dev, url: 'https://rancher.local', token_env: RANCHER_DEV_TOKEN, token_expires: {expires}}}\n"
        f"  - {{name: far, url: 'https://far.example', token_env: RANCHER_FAR_TOKEN, token_expires: {expires + timedelta(days=60)}}}\n"
        "  - {name: none, url: 'https://none.example', token_env: RANCHER_NONE_TOKEN}\n"
    )
    return settings.model_copy(update={"sites_file": f})


def test_token_warning(tmp_path, monkeypatch, settings):
    s = _sites(tmp_path, monkeypatch, settings, _today() + timedelta(days=10))
    assert token_warnings(s) == ["Token for dev expires in 10 days"]
    s = _sites(tmp_path, monkeypatch, settings, _today() - timedelta(days=2))
    assert token_warnings(s) == ["Token for dev expired 2 days ago"]


def test_token_warning_admins_only(admin_client, viewer_client, tmp_path, monkeypatch, web_settings):
    s = _sites(tmp_path, monkeypatch, web_settings, _today() + timedelta(days=5))
    for c in (admin_client, viewer_client):
        c.app.state.settings = s
    assert "Token for dev expires in 5 days" in admin_client.get("/").text
    assert "Token for dev expires" not in viewer_client.get("/").text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_tls_serving(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    port = _free_port()
    env = {
        **__import__("os").environ, "TLS_CERT_FILE": str(cert), "TLS_KEY_FILE": str(key), "LISTEN_PORT": str(port),
        "DATA_DIR": str(tmp_path / "data"), "COLLECTOR_ENABLED": "false",
    }  # fmt: skip
    proc = subprocess.Popen([sys.executable, "-m", "inventory.cli", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                r = httpx.get(f"https://127.0.0.1:{port}/healthz", verify=False, timeout=2)
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.3)
        assert r.status_code == 200 and "ok" in r.json()
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)  # plain HTTP is not served
    finally:
        proc.terminate()
        proc.wait(timeout=30)
