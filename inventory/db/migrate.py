import re
import sqlite3
from pathlib import Path

from inventory.settings import ConfigError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA_VERSION = 1


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for p in MIGRATIONS_DIR.glob("*.sql"):
        m = re.match(r"(\d+)_", p.name)
        if m:
            files.append((int(m.group(1)), p))
    return sorted(files)


def apply_migration(conn: sqlite3.Connection, version: int) -> None:
    """Apply one numbered migration and set user_version to it."""
    path = dict(_migration_files())[version]
    script = f"BEGIN;\n{path.read_text()}\nPRAGMA user_version = {version};\nCOMMIT;"
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise ConfigError(f"database schema {current} is newer than this image ({SCHEMA_VERSION})")
    for version, _ in _migration_files():
        if version <= current:
            continue
        apply_migration(conn, version)
        current = version
    return current
