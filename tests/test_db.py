import sqlite3

import pytest

from inventory.db.connection import QueryTimeout, connect, run_readonly_query
from inventory.db.migrate import SCHEMA_VERSION, migrate


def test_migrate_fresh_db(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    assert migrate(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"resources", "runs", "run_clusters", "run_kinds", "users", "presets"} <= names


def test_migrate_is_idempotent(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    first = migrate(conn)
    assert migrate(conn) == first


def test_readonly_rejects_writes(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    migrate(conn)
    conn.close()
    ro = open_db(tmp_path / "t.db", readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO login_failures VALUES ('a', 'b')")


def test_query_timeout(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    migrate(conn)
    conn.close()
    conn = open_db(tmp_path / "t.db", readonly=True)
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(QueryTimeout):
        run_readonly_query(conn, sql, [], timeout_s=0.2)


def test_query_row_cap(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    migrate(conn)
    conn.close()
    conn = open_db(tmp_path / "t.db", readonly=True)
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 6000) SELECT x FROM c"
    assert len(run_readonly_query(conn, sql, [])) == 5000


def test_schema_version_matches_files():
    from inventory.db.migrate import _migration_files

    assert SCHEMA_VERSION == max(v for v, _ in _migration_files())


def test_schema_newer_than_image(tmp_path, open_db):
    from inventory.settings import ConfigError

    conn = open_db(tmp_path / "t.db")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(ConfigError, match="newer than this image"):
        migrate(conn)


def test_readonly_query_rejects_writer(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    migrate(conn)
    with pytest.raises(ValueError):
        run_readonly_query(conn, "SELECT 1", [])


def test_readonly_connect_odd_path(tmp_path, open_db):
    d = tmp_path / "a?b#c%d"
    d.mkdir()
    conn = open_db(d / "t.db")
    migrate(conn)
    conn.close()
    ro = open_db(d / "t.db", readonly=True)
    assert run_readonly_query(ro, "SELECT count(*) FROM users", [])[0][0] == 0


def test_transaction_rolls_back_when_commit_fails(tmp_path, open_db):
    from inventory.db.repo import transaction

    conn = open_db(tmp_path / "t.db")
    migrate(conn)
    conn.execute("PRAGMA defer_foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute("PRAGMA defer_foreign_keys=ON")
            conn.execute("INSERT INTO run_clusters (run_id, site, cluster_id, status) VALUES (999, 's', 'c', 'x')")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM run_clusters").fetchone()[0] == 0


def test_pragmas_set(tmp_path, open_db):
    conn = open_db(tmp_path / "t.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_failing_migration_rolls_back(tmp_path, open_db, monkeypatch):
    import shutil

    from inventory.db import migrate as migrate_mod

    mdir = tmp_path / "migrations"
    mdir.mkdir()
    shutil.copy(migrate_mod.MIGRATIONS_DIR / "001_initial.sql", mdir)
    (mdir / "002_bad.sql").write_text("CREATE TABLE half_done (x INTEGER);\nSELECT * FROM no_such_table;\n")
    monkeypatch.setattr(migrate_mod, "MIGRATIONS_DIR", mdir)
    monkeypatch.setattr(migrate_mod, "SCHEMA_VERSION", 2)
    conn = open_db(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0] == 0
    assert not conn.in_transaction
