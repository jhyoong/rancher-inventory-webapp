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
    rancher_generated: dict[str, list[str]] = field(default_factory=dict)
    platform_roles: dict = field(default_factory=dict)
    complexity: dict[str, int] = field(default_factory=dict)
    optional_collectors: dict[str, dict] = field(default_factory=dict)


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"cannot read rules file {path}: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"rules file is not a mapping: {path}")
    return data


def load_rules(settings) -> Rules:
    """Read rules.yaml from the config dir, else from the defaults dir.

    A key missing from an override file falls back to the built-in default.
    """
    default_path = Path(settings.defaults_dir) / "rules.yaml"
    path = Path(settings.config_dir) / "rules.yaml"
    if not path.is_file():
        path = default_path
    if not path.is_file():
        raise ConfigError(f"rules file not found: {path}")
    data = _read_yaml(path)
    if path != default_path and default_path.is_file():
        defaults = _read_yaml(default_path)
        ann = {**(defaults.get("annotations") or {}), **(data.get("annotations") or {})}
        data = {**defaults, **data, "annotations": ann}
    ann = data.get("annotations") or {}
    if not isinstance(ann, dict):
        raise ConfigError(f"annotations must be a mapping in {path}")
    try:
        max_chars = int(ann.get("max_value_chars", 256))
    except (TypeError, ValueError):
        raise ConfigError(f"max_value_chars must be a number in {path}") from None
    if max_chars < 1:
        raise ConfigError(f"max_value_chars must be at least 1 in {path}")

    def strings(value, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ConfigError(f"{name} must be a list of non-empty strings in {path}")
        return list(value)

    def string_map(value, name: str, flags: tuple[str, ...] = ()) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a mapping in {path}")
        out: dict = {}
        for key, item in value.items():
            out[key] = bool(item) if key in flags else strings(item, f"{name}.{key}")
        return out

    complexity = data.get("complexity") or {}
    if not isinstance(complexity, dict):
        raise ConfigError(f"complexity must be a mapping in {path}")
    try:
        complexity = {k: int(v) for k, v in complexity.items()}
    except (TypeError, ValueError):
        raise ConfigError(f"complexity values must be numbers in {path}") from None

    known = ("cr_counts", "pvc_usage", "deprecated_api_metrics", "secret_types", "nginx_config_keys")
    raw_oc = data.get("optional_collectors") or {}
    if not isinstance(raw_oc, dict):
        raise ConfigError(f"optional_collectors must be a mapping in {path}")
    optional = {name: {"enabled": False} for name in known}
    for name, cfg in raw_oc.items():
        if name not in known:
            raise ConfigError(f"unknown optional collector {name!r} in {path} (known: {', '.join(known)})")
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise ConfigError(f"optional_collectors.{name} must be a mapping in {path}")
        optional[name] = {
            **cfg, "enabled": bool(cfg.get("enabled", False)),
            **({"groups": strings(cfg.get("groups"), f"optional_collectors.{name}.groups")} if name == "cr_counts" else {}),
        }  # fmt: skip

    return Rules(
        optional_collectors=optional,
        complexity=complexity,
        rancher_generated=string_map(data.get("rancher_generated"), "rancher_generated"),
        platform_roles=string_map(data.get("platform_roles"), "platform_roles", ("helm_in_system_namespace",)),
        max_value_chars=max_chars,
        system_domains=strings(ann.get("system_domains"), "annotations.system_domains"),
        keep=strings(ann.get("keep"), "annotations.keep"),
        drop=strings(ann.get("drop"), "annotations.drop"),
        system_namespaces=strings(data.get("system_namespaces"), "system_namespaces"),
        system_crd_groups=strings(data.get("system_crd_groups"), "system_crd_groups"),
    )
