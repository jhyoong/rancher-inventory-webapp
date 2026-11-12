from inventory.collector.rules import Rules


def matches(key: str, entry: str) -> bool:
    """True when the key equals the entry, or its domain is the entry or a subdomain of it."""
    if key == entry:
        return True
    domain = key.split("/", 1)[0] if "/" in key else ""
    return domain == entry or domain.endswith("." + entry)


def _any(key: str, entries: list[str]) -> bool:
    return any(matches(key, e) for e in entries)


def filter_annotations(ann: dict[str, str], rules: Rules) -> list[tuple[str, str, bool]]:
    """Return (key, value, custom) for annotations worth storing."""
    out = []
    for key, value in sorted((ann or {}).items()):
        if _any(key, rules.drop):
            continue
        if _any(key, rules.keep):
            custom = False
        elif _any(key, rules.system_domains):
            continue
        else:
            custom = True
        out.append((key, str(value)[: rules.max_value_chars], custom))
    return out
