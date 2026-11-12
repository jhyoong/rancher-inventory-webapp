import pytest

from inventory.collector.cluster import collect_cluster
from inventory.collector.kinds import KindSpec

pytestmark = pytest.mark.live


def noop(done, total):
    pass


def never():
    return False


@pytest.fixture
def run(live_loop, live_client, db, local_cluster, kinds):
    def go(run_id, the_kinds=None, should_stop=never):
        return live_loop.run_until_complete(
            collect_cluster(
                live_client, db, "dev", local_cluster, run_id,
                the_kinds if the_kinds is not None else kinds,
                None, noop, should_stop,
            )
        )  # fmt: skip

    return go


def count(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def test_collect_local_nodes(db, run, local_cluster):
    r = run(1)
    assert r.status == "ok"
    assert count(db, "SELECT count(*) FROM resources WHERE kind='nodes'") == local_cluster["nodeCount"]
    assert count(db, "SELECT count(*) FROM run_kinds WHERE status='ok'") == 1
    assert count(db, "SELECT count(*) FROM cluster_metrics_latest") == 1
    assert count(db, "SELECT last_ok_run FROM clusters WHERE cluster_id='local'") == 1


def test_second_run_rewrites_nothing(db, run, kinds, live_loop, live_client, local_cluster):
    from inventory.collector.transform import trim
    from inventory.db.repo import upsert_resources

    run(1)
    raw = live_loop.run_until_complete(live_client.list_kind("local", kinds["nodes"].path))
    items = [trim(kinds["nodes"], o) for o in raw]
    assert upsert_resources(db, "dev", "local", 2, items) == 0
    assert run(2).status == "ok"
    assert count(db, "SELECT count(*) FROM resources WHERE last_seen_run != 2") == 0
    assert count(db, "SELECT count(*) FROM resources") == local_cluster["nodeCount"]


def test_changed_hash_rewrites_row(db, run, kinds, live_loop, live_client):
    from inventory.collector.transform import trim
    from inventory.db.repo import upsert_resources

    run(1)
    db.execute("UPDATE resources SET hash='old'")
    raw = live_loop.run_until_complete(live_client.list_kind("local", kinds["nodes"].path))
    items = [trim(kinds["nodes"], o) for o in raw]
    assert upsert_resources(db, "dev", "local", 2, items) == len(items)


def test_forbidden_kind_does_not_fail(db, run, kinds):
    kinds = {**kinds, "secrets": KindSpec("secrets", "/api/v1/secrets", "namespaced", [])}
    r = run(1, kinds)
    assert r.status == "ok"
    assert r.kind_status["secrets"] == "forbidden"
    assert count(db, "SELECT status FROM run_kinds WHERE kind='secrets'") == "forbidden"


def test_stale_rows_deleted_only_after_full_list(db, run, kinds):
    kinds = {**kinds, "secrets": KindSpec("secrets", "/api/v1/secrets", "namespaced", [])}
    for kind, name in (("nodes", "gone"), ("secrets", "keep")):
        db.execute(
            "INSERT INTO resources (site, cluster_id, kind, name, hash, last_seen_run)"
            " VALUES ('dev', 'local', ?, ?, 'h', 0)",
            (kind, name),
        )
    run(1, kinds)
    assert count(db, "SELECT count(*) FROM resources WHERE name='gone'") == 0
    assert count(db, "SELECT count(*) FROM resources WHERE name='keep'") == 1


def test_failed_kind_keeps_rows_and_fails_cluster(db, run, kinds):
    bad = KindSpec("badpods", "/api/v1/pods?fieldSelector=bogus%3D1", "namespaced", [])
    kinds = {"badpods": bad, **kinds}
    db.execute(
        "INSERT INTO resources (site, cluster_id, kind, name, hash, last_seen_run)"
        " VALUES ('dev', 'local', 'badpods', 'x', 'h', 0)"
    )
    r = run(1, kinds)
    assert r.status == "failed"
    assert r.kind_status["badpods"] == "failed"
    assert r.kinds_done == r.kinds_total == len(kinds)
    assert count(db, "SELECT count(*) FROM resources WHERE kind='badpods'") == 1
    assert count(db, "SELECT count(*) FROM resources WHERE kind='nodes'") > 0


def test_stop_before_kind_fails_with_stopped(db, run):
    r = run(1, should_stop=lambda: True)
    assert r.status == "failed"
    assert r.error == "stopped"
    assert r.kinds_done == 0


def test_inactive_cluster_fails_without_listing(db, live_loop, live_client, local_cluster, kinds):
    cluster = {**local_cluster, "state": "unavailable"}
    r = live_loop.run_until_complete(
        collect_cluster(live_client, db, "dev", cluster, 1, kinds, None, noop, never)
    )
    assert r.status == "failed"
    assert r.error == "cluster state: unavailable"
    assert count(db, "SELECT count(*) FROM run_kinds") == 0
