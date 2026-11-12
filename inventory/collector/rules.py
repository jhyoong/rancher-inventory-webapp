from dataclasses import dataclass, field
from pathlib import Path

import yaml

from inventory.settings import ConfigError


@dataclass
class Rules:
    max_value_chars: int
    system_domains: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    system_namespaces: list[str] = field(default_factory=list)
    system_crd_groups: list[str] = field(default_factory=list)


def load_rules(settings) -> Rules:
    """Read rules.yaml from the config dir, else from the defaults dir."""
    path = Path(settings.config_dir) / "rules.yaml"
    if not path.is_file():
        path = Path(settings.defaults_dir) / "rules.yaml"
    if not path.is_file():
        raise ConfigError(f"rules file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"rules file is not a mapping: {path}")
    ann = data.get("annotations") or {}
    try:
        max_chars = int(ann.get("max_value_chars", 256))
    except (TypeError, ValueError):
        raise ConfigError(f"max_value_chars must be a number in {path}") from None

    def strings(value, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{name} must be a list of strings in {path}")
        return list(value)

    return Rules(
        max_value_chars=max_chars,
        system_domains=strings(ann.get("system_domains"), "annotations.system_domains"),
        keep=strings(ann.get("keep"), "annotations.keep"),
        drop=strings(ann.get("drop"), "annotations.drop"),
        system_namespaces=strings(data.get("system_namespaces"), "system_namespaces"),
        system_crd_groups=strings(data.get("system_crd_groups"), "system_crd_groups"),
    )
