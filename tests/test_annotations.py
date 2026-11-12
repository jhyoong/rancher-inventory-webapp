import pytest

from inventory.collector.annotations import filter_annotations, matches
from inventory.collector.rules import load_rules
from inventory.collector.transform import trim


@pytest.fixture
def rules(settings):
    return load_rules(settings)


@pytest.mark.parametrize("key,entry,expected", [
    ("node.kubernetes.io/foo", "kubernetes.io", True),
    ("kubernetes.io/foo", "kubernetes.io", True),
    ("notkubernetes.io/foo", "kubernetes.io", False),
    ("meta.helm.sh/release-name", "meta.helm.sh/release-name", True),
    ("team.example.com/owner", "cattle.io", False),
])  # fmt: skip
def test_matches(key, entry, expected):
    assert matches(key, entry) is expected


def test_load_rules_defaults(rules):
    assert rules.max_value_chars == 256
    assert "kubernetes.io/ingress.class" in rules.keep
    assert "kube-*" in rules.system_namespaces
    assert "*.cattle.io" in rules.system_crd_groups


def test_load_rules_override(monkeypatch, tmp_path):
    from inventory.settings import Settings

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "rules.yaml").write_text("annotations:\n  max_value_chars: 10\n  drop: [a.io]\n")
    monkeypatch.setenv("SITES_FILE", str(cfg / "sites.yaml"))
    r = load_rules(Settings())
    assert r.max_value_chars == 10 and r.drop == ["a.io"] and r.system_namespaces == []


def test_filter_annotations_rules(rules):
    out = dict((k, (v, c)) for k, v, c in filter_annotations({
        "kubectl.kubernetes.io/last-applied-configuration": "{}",
        "field.cattle.io/projectId": "c-1:p-1",
        "deployment.kubernetes.io/revision": "3",
        "team.example.com/owner": "x" * 300,
    }, rules))  # fmt: skip
    assert set(out) == {"field.cattle.io/projectId", "team.example.com/owner"}
    assert out["field.cattle.io/projectId"][1] is False
    assert out["team.example.com/owner"] == ("x" * 256, True)


def test_trim_annotations_only_for_marked_kinds(settings, rules):
    from inventory.collector.kinds import load_kinds

    kinds = load_kinds(settings)
    obj = {"metadata": {"name": "a", "annotations": {"team.example.com/owner": "me"}}, "spec": {}}
    assert trim(kinds["deployments"], obj, rules).annotations == [("team.example.com/owner", "me", True)]
    assert trim(kinds["pods"], obj, rules).annotations == []
    assert trim(kinds["deployments"], obj).annotations == []


def test_pod_unparseable_quantity_does_not_raise(settings):
    from inventory.collector.kinds import load_kinds

    pod = {"metadata": {"name": "p"}, "spec": {"containers": [
        {"resources": {"requests": {"cpu": "100m", "memory": "128974848000m"}}},
        {"resources": {"requests": {"cpu": "1e3", "memory": "1e3"}}},
        {"resources": {"requests": {"cpu": "bogus", "memory": "64Mi"}}},
    ]}}  # fmt: skip
    d = trim(load_kinds(settings)["pods"], pod).data
    # "bogus" cpu is skipped; the milli memory value parses to 128974848 bytes
    assert d["cpu_req_m"] == 1000100 and d["mem_req_b"] == 128974848 + 1000 + 64 * 2**20
