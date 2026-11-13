import pytest
import yaml

from inventory.collector.rules import load_rules
from inventory.settings import ConfigError, Settings


def test_defaults_load(settings):
    rules = load_rules(settings)
    assert rules.network["lb_implementations"]["metallb"]["annotation_domains"] == ["metallb.universe.tf", "metallb.io"]
    assert rules.databases["images"]["postgresql"][0] == "postgres"
    assert "calico-node" in rules.network["cni_daemonsets"]["calico"]
    assert "nsx.vmware.com" in rules.keep


def override_settings(monkeypatch, tmp_path, text):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "rules.yaml").write_text(text)
    monkeypatch.setenv("SITES_FILE", str(cfg / "sites.yaml"))
    return Settings()


def test_old_override_falls_back(monkeypatch, tmp_path):
    rules = load_rules(override_settings(monkeypatch, tmp_path, "system_namespaces: [kube-*]\n"))
    assert rules.system_namespaces == ["kube-*"]
    assert "nsx-ncp" in rules.network["lb_implementations"]
    assert "postgresql" in rules.databases["images"]


@pytest.mark.parametrize(
    "override",
    [
        {"network": {"lb_implementations": {"x": {"classes": "nope"}}}},
        {"network": {"cni_daemonsets": []}},
        {"databases": {"images": {"pg": "postgres"}}},
        {"network": {"bogus": []}},
    ],
)
def test_bad_shapes_rejected(monkeypatch, tmp_path, override):
    with pytest.raises(ConfigError):
        load_rules(override_settings(monkeypatch, tmp_path, yaml.safe_dump(override)))
