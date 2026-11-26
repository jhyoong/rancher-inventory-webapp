"""Rancher users across sites, built at request time from cluster_members.

A user is matched across sites by AD principal; users without one (local accounts, or rows collected
before migration 007) are kept apart per (site, Rancher user id). Groups are not listed.
"""

import json
import sqlite3
from collections import Counter

# Same rule as the Members column: an ok read, or a failed read with an earlier list (stale).
_SQL = """SELECT m.site, m.cluster_id, c.name AS cluster_name, m.subject, m.display_name, m.username,
                 m.disabled, m.grants, m.principal, c.members_status
          FROM cluster_members m JOIN clusters c ON c.site = m.site AND c.cluster_id = m.cluster_id
          WHERE m.kind = 'user'
            AND (c.members_status = 'ok' OR (c.members_status = 'failed' AND c.members_at IS NOT NULL))"""


def _key(r) -> tuple:
    return ("p", r["principal"]) if r["principal"] else ("l", r["site"], r["subject"])


def _ranked(values) -> list[str]:
    """Distinct non-empty values, most common first (ties by name)."""
    counts = Counter(v for v in values if v)
    return sorted(counts, key=lambda v: (-counts[v], v.casefold()))


def _summary(key: tuple, rows: list) -> dict:
    accounts = {(r["site"], r["subject"]): bool(r["disabled"]) for r in rows}
    disabled = sum(accounts.values())
    names, usernames = _ranked(r["display_name"] for r in rows), _ranked(r["username"] for r in rows)
    return {
        "key": key,
        "matched": key[0] == "p",
        "principal": key[1] if key[0] == "p" else None,
        "site": None if key[0] == "p" else key[1],
        "subject": None if key[0] == "p" else key[2],
        "name": names[0] if names else (key[1] if key[0] == "p" else key[2]),
        "other_names": names[1:],
        "usernames": usernames,
        "clusters": len({(r["site"], r["cluster_id"]) for r in rows}),
        "sites": sorted({r["site"] for r in rows}),
        "disabled": "yes" if disabled == len(accounts) else "on some sites" if disabled else "no",
    }


def _grouped(conn: sqlite3.Connection, where: str = "", args: tuple = ()) -> dict[tuple, list]:
    groups: dict[tuple, list] = {}
    for r in conn.execute(_SQL + where, args):
        groups.setdefault(_key(r), []).append(r)
    return groups


def users(conn: sqlite3.Connection, q: str = "", site: str = "") -> list[dict]:
    """One summary per user, most clusters first. q matches name, username or principal; site keeps users on it."""
    needle = q.casefold()  # SQLite lower() folds ASCII only, so match here
    out = []
    for key, rows in _grouped(conn).items():
        if site and not any(r["site"] == site for r in rows):
            continue
        if needle and not any(
            needle in (v or "").casefold() for r in rows for v in (r["display_name"], r["username"], r["principal"])
        ):
            continue
        out.append(_summary(key, rows))
    return sorted(out, key=lambda u: (-u["clusters"], u["name"].casefold()))


def user(conn: sqlite3.Connection, principal: str = "", site: str = "", subject: str = "") -> dict | None:
    """One user's summary plus their clusters, or None when no such user is a member anywhere."""
    if principal:
        key, where, args = ("p", principal), " AND m.principal = ?", (principal,)
    elif site and subject:
        key, where, args = ("l", site, subject), " AND m.principal IS NULL AND m.site = ? AND m.subject = ?", (site, subject)
    else:
        return None
    rows = _grouped(conn, where, args).get(key)
    if not rows:
        return None
    clusters = [
        {"site": r["site"], "cluster_id": r["cluster_id"], "cluster_name": r["cluster_name"] or r["cluster_id"],
         "username": r["username"], "disabled": bool(r["disabled"]), "grants": json.loads(r["grants"]),
         "stale": r["members_status"] == "failed"}
        for r in rows
    ]  # fmt: skip
    clusters.sort(key=lambda c: (c["site"], c["cluster_name"].casefold()))
    return _summary(key, rows) | {"cluster_rows": clusters}


def sites(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT site FROM cluster_members WHERE kind = 'user' ORDER BY site")]
