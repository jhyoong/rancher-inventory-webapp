import sqlite3

import pytest

from inventory.db.connection import QueryTimeout, connect, run_readonly_query
from inventory.db.migrate import SCHEMA_VERSION, migrate


def test_migrate_fresh_db(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert migrate(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"resources", "runs", "run_clusters", "run_kinds", "users", "presets"} <= names


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    first = migrate(conn)
    assert migrate(conn) == first


def test_readonly_rejects_writes(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    conn.close()
    ro = connect(tmp_path / "t.db", readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO login_failures VALUES ('a', 'b')")


def test_query_timeout(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(QueryTimeout):
        run_readonly_query(conn, sql, [], timeout_s=0.2)


def test_query_row_cap(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 6000) SELECT x FROM c"
    assert len(run_readonly_query(conn, sql, [])) == 5000
