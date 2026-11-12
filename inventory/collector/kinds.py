from dataclasses import dataclass, field
from pathlib import Path

import yaml

from inventory.settings import ConfigError, Settings


@dataclass
class KindSpec:
    name: str
    path: str
    scope: str  # "cluster" or "namespaced"
    keep: list[str] = field(default_factory=list)
    annotations: bool = False
    addresses: bool = False
    metadata_only: bool = False
    podspec: bool = False  # derive pod spec fields (spec 6.9)


def load_kinds(settings: Settings) -> dict[str, KindSpec]:
    path = Path(settings.config_dir) / "kinds.yaml"
    if not path.is_file():
        path = Path(settings.defaults_dir) / "kinds.yaml"
    if not path.is_file():
        raise ConfigError(f"kinds file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("kinds") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"no kinds defined in {path}")
    kinds: dict[str, KindSpec] = {}
    for name, item in raw.items():
        scope = item.get("scope")
        if scope not in ("cluster", "namespaced"):
            raise ConfigError(f"kind {name}: scope must be cluster or namespaced")
        if not item.get("path"):
            raise ConfigError(f"kind {name}: path is required")
        kinds[name] = KindSpec(
            name=name,
            path=item["path"],
            scope=scope,
            keep=list(item.get("keep") or []),
            annotations=bool(item.get("annotations", False)),
            addresses=bool(item.get("addresses", False)),
            metadata_only=bool(item.get("metadata_only", False)),
            podspec=bool(item.get("podspec", False)),
        )
    return kinds
