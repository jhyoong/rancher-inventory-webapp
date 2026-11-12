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
