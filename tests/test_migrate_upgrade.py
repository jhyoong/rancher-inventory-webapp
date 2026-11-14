import json

import pytest

from inventory.db.migrate import SCHEMA_VERSION, apply_migration, migrate
from inventory.web.auth import create_user, list_users


def _copy_v1_rows(src, dst) -> None:
    """Fill a version 1 DB with the real rows of a collected DB, v1 columns only."""
    accounts = {"users", "sessions", "presets", "login_failures"}  # this test creates its own
    for (table,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if table in accounts:
            continue
        cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        if rows:
            marks = ", ".join("?" * len(cols))
            dst.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", [tuple(r) for r in rows])


@pytest.mark.live
def test_upgrade_v1_to_v2_keeps_data(tmp_path, collected_db, open_db):
    conn = open_db(tmp_path / "i.db")
    apply_migration(conn, 1)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.execute("BEGIN")
    _copy_v1_rows(open_db(collected_db.db_path, readonly=True), conn)
    conn.execute("COMMIT")
    user = create_user(conn, "keep", "keep-password-123", "viewer")
    conn.execute(
        "INSERT INTO presets (name, filters, created_by, created_at) VALUES ('p', ?, ?, '2026-11-12T00:00:00+00:00')",
        (json.dumps({"kind": "pods"}), user.id),
    )
    metrics = conn.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0]
    assert metrics >= 1

    assert migrate(conn) == SCHEMA_VERSION == 4
    assert [u.username for u in list_users(conn)] == ["keep"]
    assert conn.execute("SELECT count(*) FROM presets").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0] == metrics

    ns_cols = {r[1] for r in conn.execute("PRAGMA table_info(namespace_profile)")}
    assert {"pods", "pods_running", "cpu_used_m", "mem_used_b", "gpu", "has_quota", "has_limitrange"} <= ns_cols
    cp_cols = {r[1] for r in conn.execute("PRAGMA table_info(cluster_profile)")}
    assert {"cluster_uid", "complexity_flags", "summary"} <= cp_cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"api_versions", "export_name_map", "optional_results"} <= tables
    # old rows read back with the new columns empty
    assert conn.execute("SELECT count(*) FROM namespace_profile WHERE pods IS NULL").fetchone()[0] > 0


@pytest.mark.live
def test_upgrade_v2_to_v3_keeps_data(tmp_path, collected_db, open_db):
    conn = open_db(tmp_path / "i.db")
    apply_migration(conn, 1)
    apply_migration(conn, 2)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.execute("BEGIN")
    _copy_v1_rows(open_db(collected_db.db_path, readonly=True), conn)  # reads whatever columns v2 has
    conn.execute("COMMIT")
    user = create_user(conn, "keep", "keep-password-123", "viewer")
    conn.execute(
        "INSERT INTO presets (name, filters, created_by, created_at) VALUES ('p', ?, ?, '2026-11-12T00:00:00+00:00')",
        (json.dumps({"kind": "pods"}), user.id),
    )
    metrics = conn.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0]
    assert metrics >= 1

    assert migrate(conn) == SCHEMA_VERSION == 4
    assert [u.username for u in list_users(conn)] == ["keep"]
    assert conn.execute("SELECT count(*) FROM presets").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0] == metrics
    cluster_cols = {r[1] for r in conn.execute("PRAGMA table_info(clusters)")}
    assert {"api_endpoint", "pools_status", "prov_name"} <= cluster_cols
    assert conn.execute("SELECT count(*) FROM machine_pools").fetchone()[0] == 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"migration_tracking", "migration_tracking_log", "removed_clusters"} <= tables
