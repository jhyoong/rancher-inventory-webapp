from inventory.collector.units import parse_bytes, parse_cpu


def test_parse_cpu():
    assert parse_cpu("3495m") == 3495
    assert parse_cpu("2") == 2000
    assert parse_cpu("0.5") == 500
    assert parse_cpu("250000n") == 0
    assert parse_cpu("1500u") == 1


def test_parse_bytes():
    assert parse_bytes("5222Mi") == 5475663872
    assert parse_bytes("1Gi") == 1073741824
    assert parse_bytes("1G") == 1000000000
    assert parse_bytes("128974848") == 128974848
    assert parse_bytes("1k") == 1000


def test_parse_bytes_milli():
    assert parse_bytes("1503238553600m") == 1503238553
    assert parse_bytes("1500m") == 1


def test_metrics_from_cluster_missing_fields(local_cluster):
    """Missing maps or values give None instead of raising (real cluster with keys removed)."""
    from inventory.collector.units import metrics_from_cluster

    bare = {k: v for k, v in local_cluster.items() if k not in ("allocatable", "requested", "nodeCount")}
    m = metrics_from_cluster(bare, [{"metadata": {"name": "n1"}}])
    assert m.nodes is None and m.pods is None and m.cpu_alloc_m is None and m.mem_req_b is None
    assert m.cpu_used_m is None and m.mem_used_b is None
    full = metrics_from_cluster(local_cluster, None)
    assert full.nodes == 3 and full.cpu_alloc_m > 0


def test_parse_cpu_k_suffix():
    assert parse_cpu("1k") == 1_000_000
    assert parse_cpu("2M") == 2_000_000_000
    assert parse_cpu("1G") == 1_000_000_000_000
