import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from inventory.db.repo import transaction

MIN_PASSWORD = 12
LOCK_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)
ROLES = ("admin", "viewer")
_N, _R, _P, _DKLEN = 16384, 8, 1, 64


class LoginLocked(Exception):
    pass


@dataclass
class User:
    id: int
    username: str
    role: str
    disabled: bool


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=128 * n * r * 2 + (1 << 20)
    )


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or not (1 < n <= 2**16 and 1 <= r <= 16 and 1 <= p <= 4):
            return False
        got = _scrypt(password, bytes.fromhex(salt), n, r, p)
        return hmac.compare_digest(got, bytes.fromhex(digest))
    except (ValueError, TypeError, OverflowError, MemoryError):
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _user(row: sqlite3.Row) -> User:
    return User(row["id"], row["username"], row["role"], bool(row["disabled"]))


def create_user(conn, username: str, password: str, role: str) -> User:
    if role not in ROLES:
        raise ValueError("role must be admin or viewer")
    if not username.strip():
        raise ValueError("username is required")
    pw_hash = hash_password(password)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, disabled, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (username, pw_hash, role, _iso(datetime.now(timezone.utc))),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"user {username} already exists") from None
    return User(cur.lastrowid, username, role, False)


def _require_user(conn, username: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        raise ValueError(f"unknown user {username}")
    return row["id"]


def set_password(conn, username: str, password: str) -> None:
    pw_hash = hash_password(password)
    with transaction(conn):
        uid = _require_user(conn, username)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


def set_disabled(conn, username: str, disabled: bool) -> None:
    with transaction(conn):
        uid = _require_user(conn, username)
        conn.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


def list_users(conn) -> list[User]:
    rows = conn.execute("SELECT id, username, role, disabled FROM users ORDER BY username")
    return [_user(r) for r in rows]


def ensure_first_admin(conn, settings) -> bool:
    if not settings.admin_password:
        return False
    with transaction(conn):
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return False
        create_user(conn, "admin", settings.admin_password, "admin")
    return True


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(conn, username: str, password: str, hours: int, now: datetime) -> str | None:
    since = _iso(now - LOCK_WINDOW)
    failures = conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE username=? AND failed_at>?", (username, since)
    ).fetchone()[0]
    if failures >= LOCK_FAILURES:
        raise LoginLocked(username)
    row = conn.execute(
        "SELECT id, password_hash, disabled FROM users WHERE username=?", (username,)
    ).fetchone()
    # Always run one scrypt verify so unknown users cost the same time.
    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not ok or row["disabled"]:
        with transaction(conn):
            conn.execute(
                "INSERT INTO login_failures (username, failed_at) VALUES (?, ?)",
                (username, _iso(now)),
            )
        return None
    token = secrets.token_urlsafe(32)
    with transaction(conn):
        conn.execute("DELETE FROM login_failures WHERE username=?", (username,))
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (_iso(now),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_hash_token(token), row["id"], _iso(now + timedelta(hours=hours))),
        )
    return token


def session_user(conn, token: str, now: datetime) -> User | None:
    row = conn.execute(
        "SELECT u.id, u.username, u.role, u.disabled, s.expires_at FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
        (_hash_token(token),),
    ).fetchone()
    if row is None or row["disabled"] or row["expires_at"] <= _iso(now):
        return None
    return _user(row)


def logout(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def csrf_token(secret: str, token: str) -> str:
    return hmac.new(secret.encode(), _hash_token(token).encode(), hashlib.sha256).hexdigest()
