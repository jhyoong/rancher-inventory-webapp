from datetime import datetime, timedelta, timezone

import pytest

from inventory.web.auth import (
    LoginLocked,
    create_user,
    csrf_token,
    ensure_first_admin,
    hash_password,
    list_users,
    login,
    logout,
    session_user,
    set_disabled,
    set_password,
    verify_password,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$16384$8$1$") and verify_password("correct horse battery", h)
    assert not verify_password("wrong password!!", h)


def test_short_password_rejected():
    with pytest.raises(ValueError):
        hash_password("short")


def test_lockout_after_five(db):
    create_user(db, "a", "right-password-1", "admin")
    for _ in range(5):
        assert login(db, "a", "wrong-password-1", 12, NOW) is None
    with pytest.raises(LoginLocked):
        login(db, "a", "right-password-1", 12, NOW)
    assert login(db, "a", "right-password-1", 12, NOW + timedelta(minutes=16))


def test_old_failures_do_not_count_and_success_clears(db):
    create_user(db, "a", "right-password-1", "admin")
    for _ in range(4):
        login(db, "a", "wrong-password-1", 12, NOW)
    assert login(db, "a", "right-password-1", 12, NOW)
    assert db.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 0


def test_unknown_user_failures_recorded(db):
    assert login(db, "ghost", "whatever-password", 12, NOW) is None
    assert db.execute("SELECT COUNT(*) FROM login_failures WHERE username='ghost'").fetchone()[0] == 1


def test_disabled_user_cannot_login(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    set_disabled(db, "v", True)
    assert login(db, "v", "viewer-password-1", 12, NOW) is None
    assert db.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 1


def test_disabled_user_session_invalid(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    t = login(db, "v", "viewer-password-1", 12, NOW)
    assert session_user(db, t, NOW).username == "v"
    set_disabled(db, "v", True)
    assert session_user(db, t, NOW) is None


def test_session_expires(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    t = login(db, "v", "viewer-password-1", 12, NOW)
    assert session_user(db, t, NOW + timedelta(hours=13)) is None


def test_logout(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    t = login(db, "v", "viewer-password-1", 12, NOW)
    logout(db, t)
    assert session_user(db, t, NOW) is None


def test_only_hash_stored(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    t = login(db, "v", "viewer-password-1", 12, NOW)
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE token_hash=?", (t,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_set_password_and_list(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    set_password(db, "v", "another-password-2")
    assert login(db, "v", "viewer-password-1", 12, NOW) is None
    assert login(db, "v", "another-password-2", 12, NOW)
    assert [(u.username, u.role, u.disabled) for u in list_users(db)] == [("v", "viewer", False)]
    with pytest.raises(ValueError):
        set_password(db, "nobody", "another-password-2")


def test_csrf_token_stable(db):
    assert csrf_token("s", "t") == csrf_token("s", "t")
    assert csrf_token("s", "t") != csrf_token("s", "u")


def test_first_admin_once(db, settings):
    settings.admin_password = "first-admin-password"
    assert ensure_first_admin(db, settings) is True
    assert ensure_first_admin(db, settings) is False
    assert list_users(db)[0].role == "admin"


def test_first_admin_needs_password(db, settings):
    settings.admin_password = ""
    assert ensure_first_admin(db, settings) is False


def test_cli_user_flow(monkeypatch, tmp_path, capsys):
    from inventory.cli import main

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    pw = iter(["cli-password-123", "cli-password-123"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(pw))
    assert main(["user", "add", "bob", "--role", "viewer"]) == 0
    assert main(["user", "list"]) == 0
    assert "bob" in capsys.readouterr().out
    assert main(["user", "disable", "bob"]) == 0
    assert main(["user", "disable", "nobody"]) == 1
    pw = iter(["short", "short"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(pw))
    assert main(["user", "passwd", "bob"]) == 1


def _session_count(db):
    return db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def test_set_password_removes_sessions(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    login(db, "v", "viewer-password-1", 12, NOW)
    set_password(db, "v", "another-password-2")
    assert _session_count(db) == 0


def test_set_disabled_removes_sessions(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    login(db, "v", "viewer-password-1", 12, NOW)
    set_disabled(db, "v", True)
    assert _session_count(db) == 0


def test_session_user_checks_disabled_flag(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    t = login(db, "v", "viewer-password-1", 12, NOW)
    db.execute("UPDATE users SET disabled=1 WHERE username='v'")
    assert _session_count(db) == 1
    assert session_user(db, t, NOW) is None


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "scrypt$16384$8$1$aa",
        "scrypt$16384$8$1$zz$zz",
        "scrypt$notanumber$8$1$aa$bb",
        "scrypt$99999999999999999999$8$1$aa$bb",
        "scrypt$1073741824$8$1$aa$bb",
        "scrypt$16384$99999999$1$aa$bb",
        "md5$16384$8$1$aa$bb",
    ],
)
def test_verify_password_malformed_is_false(stored):
    assert verify_password("some-password-123", stored) is False


def test_concurrent_wrong_logins_stop_at_lock(db_path, db, open_db):
    """Failures are counted and inserted atomically, so parallel tries cannot pass the lock."""
    from concurrent.futures import ThreadPoolExecutor

    from inventory.db.connection import connect

    create_user(db, "a", "right-password-1", "admin")

    def attempt(_):
        conn = open_db(db_path)
        try:
            return login(conn, "a", "wrong-password-1", 12, NOW)
        except LoginLocked:
            return "locked"
        finally:
            conn.close()

    with ThreadPoolExecutor(10) as pool:
        list(pool.map(attempt, range(10)))
    assert db.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 5


def test_old_failures_deleted_on_login(db):
    db.execute("INSERT INTO login_failures VALUES ('x', ?)", ((NOW - timedelta(hours=1)).isoformat(),))
    login(db, "someone", "whatever-password", 12, NOW)
    assert db.execute("SELECT COUNT(*) FROM login_failures WHERE username='x'").fetchone()[0] == 0


def test_usernames_stripped_and_lowercased(db):
    u = create_user(db, " Bob ", "bob-password-123", "viewer")
    assert u.username == "bob" and [x.username for x in list_users(db)] == ["bob"]
    assert login(db, "BOB ", "bob-password-123", 12, NOW)
    set_password(db, " BOB", "bob-password-456")
    with pytest.raises(ValueError):
        create_user(db, "bob", "bob-password-123", "viewer")


def test_login_log_uses_repr(client, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="inventory.web"):
        client.post("/login", data={"username": "evil\nINFO fake", "password": "wrong-password-x"})
    assert "'evil\\ninfo fake'" in caplog.text


def test_mixed_case_user_from_plan_1_can_still_log_in(db):
    """Plan 1 stored usernames as typed; login now lower-cases the input."""
    create_user(db, "tmp", "right-password-1", "admin")
    db.execute("UPDATE users SET username='Admin' WHERE username='tmp'")  # a plan 1 row
    assert login(db, "Admin", "right-password-1", 12, NOW)
    assert login(db, "admin", "right-password-1", 12, NOW)
    set_password(db, "ADMIN", "right-password-2")
    assert login(db, "admin", "right-password-2", 12, NOW)
