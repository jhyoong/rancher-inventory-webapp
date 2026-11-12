from dataclasses import dataclass, field


@dataclass
class Rules:
    max_value_chars: int
    system_domains: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    system_namespaces: list[str] = field(default_factory=list)
    system_crd_groups: list[str] = field(default_factory=list)
