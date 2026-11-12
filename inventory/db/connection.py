import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path


class QueryTimeout(Exception):
    pass


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.isolation_level = None  # explicit transactions only
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_readonly_query(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence | Mapping,
    timeout_s: float = 10.0,
    max_rows: int = 5000,
) -> list[sqlite3.Row]:
    deadline = time.monotonic() + timeout_s
    timed_out = False

    def check() -> int:
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1
        return 0

    conn.set_progress_handler(check, 10_000)
    try:
        cur = conn.execute(sql, params)
        return cur.fetchmany(max_rows)
    except sqlite3.OperationalError:
        if timed_out:
            raise QueryTimeout(f"query exceeded {timeout_s} seconds") from None
        raise
    finally:
        conn.set_progress_handler(None, 0)
