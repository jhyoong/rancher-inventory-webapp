import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA_VERSION = 1


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for p in MIGRATIONS_DIR.glob("*.sql"):
        m = re.match(r"(\d+)_", p.name)
        if m:
            files.append((int(m.group(1)), p))
    return sorted(files)


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, path in _migration_files():
        if version <= current:
            continue
        script = f"BEGIN;\n{path.read_text()}\nPRAGMA user_version = {version};\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current
