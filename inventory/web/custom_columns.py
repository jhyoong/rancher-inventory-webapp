"""Admin-defined custom columns: definitions, choices and their maintenance."""

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

NAME_MAX = 40
VALUE_MAX = 100
RESERVED = "none"
TYPES = ("text", "choice")


@dataclass(frozen=True)
class Column:
    id: int
    name: str
    type: str
    choices: list[str]
    position: int


class InUse(ValueError):
    """A choice cannot be removed because clusters still hold it."""

    def __init__(self, value: str, live: int, removed: list[dict]):
        self.value, self.live, self.removed = value, live, removed
        super().__init__(f"{value!r} is in use by {live} cluster(s) and {len(removed)} removed cluster(s)")


class Unmatched(ValueError):
    """Changing text to choice is blocked: stored values match no choice."""

    def __init__(self, items: list[dict]):
        self.items = items  # [{"value", "live", "removed": [...]}]
        super().__init__("values do not match any choice: " + ", ".join(repr(i["value"]) for i in items))


def column_id_from_key(key: str) -> int | None:
    """The column id of a form key like "cf12"; ASCII digits only, else None."""
    m = re.fullmatch(r"cf(\d+)", key, re.ASCII)
    return int(m.group(1)) if m else None


def _check_value(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("value is empty")
    if len(v) > VALUE_MAX:
        raise ValueError(f"value is longer than {VALUE_MAX} characters")
    if v.lower() == RESERVED:
        raise ValueError(f"{RESERVED!r} is reserved")
    return v


def _check_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("name is empty")
    if len(name) > NAME_MAX:
        raise ValueError(f"name is longer than {NAME_MAX} characters")
    return name


def _check_choices(type_: str, choices: list[str]) -> list[str]:
    if type_ not in TYPES:
        raise ValueError(f"unknown type: {type_!r}")
    if type_ == "text":
        if choices:
            raise ValueError("a text column has no choices")
        return []
    out = [_check_value(c) for c in choices]
    if not out:
        raise ValueError("a choice column needs at least one choice")
    if len({c.lower() for c in out}) != len(out):
        raise ValueError("choices must be unique ignoring case")
    return out


def _shape(row: sqlite3.Row) -> Column:
    return Column(row["id"], row["name"], row["type"], json.loads(row["choices"]), row["position"])


@contextmanager
def _tx(conn: sqlite3.Connection):
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    if own:
        conn.execute("COMMIT")


def list_columns(conn: sqlite3.Connection) -> list[Column]:
    return [_shape(r) for r in conn.execute("SELECT * FROM custom_columns ORDER BY position, id")]


def get_column(conn: sqlite3.Connection, column_id: int) -> Column | None:
    row = conn.execute("SELECT * FROM custom_columns WHERE id=?", (column_id,)).fetchone()
    return _shape(row) if row else None


def _require(conn: sqlite3.Connection, column_id: int) -> Column:
    col = get_column(conn, column_id)
    if col is None:
        raise ValueError(f"no such column: {column_id}")
    return col


def _name_taken(conn: sqlite3.Connection, name: str, except_id: int | None = None) -> bool:
    row = conn.execute("SELECT id FROM custom_columns WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return row is not None and row["id"] != except_id


def create_column(conn: sqlite3.Connection, name: str, type: str, choices: list[str]) -> int:
    name = _check_name(name)
    choices = _check_choices(type, choices)
    with _tx(conn):
        if _name_taken(conn, name):
            raise ValueError(f"a column named {name!r} already exists")
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM custom_columns").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO custom_columns (name, type, choices, position, created_at) VALUES (?,?,?,?,?)",
            (name, type, json.dumps(choices), pos, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def rename_column(conn: sqlite3.Connection, column_id: int, name: str) -> None:
    name = _check_name(name)
    with _tx(conn):
        _require(conn, column_id)
        if _name_taken(conn, name, column_id):
            raise ValueError(f"a column named {name!r} already exists")
        conn.execute("UPDATE custom_columns SET name=? WHERE id=?", (name, column_id))


def move_column(conn: sqlite3.Connection, column_id: int, direction: str) -> None:
    if direction not in ("up", "down"):
        raise ValueError(f"unknown direction: {direction!r}")
    with _tx(conn):
        cols = list_columns(conn)
        ids = [c.id for c in cols]
        if column_id not in ids:
            raise ValueError(f"no such column: {column_id}")
        i = ids.index(column_id)
        j = i - 1 if direction == "up" else i + 1
        if not 0 <= j < len(cols):
            return
        ids[i], ids[j] = ids[j], ids[i]
        for pos, cid in enumerate(ids, 1):
            conn.execute("UPDATE custom_columns SET position=? WHERE id=?", (pos, cid))


def usage(conn: sqlite3.Connection, column_id: int, value: str | None = None) -> dict:
    """Clusters holding a value in the column: live ones counted, removed ones listed."""
    vsql, vparams = ("", ()) if value is None else (" AND v.value=?", (value,))
    live = conn.execute(
        "SELECT COUNT(*) FROM custom_values v JOIN clusters c ON c.site=v.site AND c.cluster_id=v.cluster_id"
        " WHERE v.column_id=?" + vsql,
        (column_id, *vparams),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT r.site, r.cluster_id, r.name FROM custom_values v"
        " JOIN removed_clusters r ON r.site=v.site AND r.cluster_id=v.cluster_id"
        " WHERE v.column_id=?" + vsql + " ORDER BY r.site, r.name, r.cluster_id",
        (column_id, *vparams),
    ).fetchall()
    return {"live": live, "removed": [dict(r) for r in rows]}


def set_choices(conn: sqlite3.Connection, column_id: int, choices: list[str]) -> None:
    with _tx(conn):
        col = _require(conn, column_id)
        if col.type != "choice":
            raise ValueError("not a choice column")
        new = _check_choices("choice", choices)
        for value in col.choices:
            if value not in new:
                u = usage(conn, column_id, value)
                if u["live"] or u["removed"]:
                    raise InUse(value, u["live"], u["removed"])
        conn.execute("UPDATE custom_columns SET choices=? WHERE id=?", (json.dumps(new), column_id))


def _respell(conn: sqlite3.Connection, user_id: int, col: Column, old: str, new: str, ts: str) -> int:
    """Change stored value old to new on every cluster, logging one row per cluster."""
    rows = conn.execute(
        """SELECT v.site, v.cluster_id,
                  COALESCE(c.name, r.name, v.cluster_id) AS name
           FROM custom_values v
           LEFT JOIN clusters c ON c.site=v.site AND c.cluster_id=v.cluster_id
           LEFT JOIN removed_clusters r ON r.site=v.site AND r.cluster_id=v.cluster_id
           WHERE v.column_id=? AND v.value=?""",
        (col.id, old),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE custom_values SET value=?, updated_by=?, updated_at=?"
            " WHERE site=? AND cluster_id=? AND column_id=?",
            (new, user_id, ts, r["site"], r["cluster_id"], col.id),
        )
        conn.execute(
            """INSERT INTO custom_values_log
               (site, cluster_id, name, column_id, column_name, value, updated_by, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (r["site"], r["cluster_id"], r["name"], col.id, col.name, new, user_id, ts),
        )
    return len(rows)


def rename_choice(
    conn: sqlite3.Connection, user_id: int, column_id: int, old: str, new: str,
    now: datetime | None = None,
) -> int:
    new = _check_value(new)
    with _tx(conn):
        col = _require(conn, column_id)
        if col.type != "choice" or old not in col.choices:
            raise ValueError(f"{old!r} is not a choice of this column")
        if any(c.lower() == new.lower() and c != old for c in col.choices):
            raise ValueError(f"{new!r} is already a choice")
        choices = [new if c == old else c for c in col.choices]
        conn.execute("UPDATE custom_columns SET choices=? WHERE id=?", (json.dumps(choices), column_id))
        if new == old:
            return 0
        return _respell(conn, user_id, col, old, new, (now or datetime.now(timezone.utc)).isoformat())


def change_type(
    conn: sqlite3.Connection, user_id: int, column_id: int, type: str, choices: list[str],
    now: datetime | None = None,
) -> None:
    choices = _check_choices(type, choices)
    with _tx(conn):
        col = _require(conn, column_id)
        if type == "choice":
            by_lower = {c.lower(): c for c in choices}
            stored = [r["value"] for r in conn.execute(
                "SELECT DISTINCT value FROM custom_values WHERE column_id=? ORDER BY value", (column_id,))]
            unmatched = [v for v in stored if v.lower() not in by_lower]
            if unmatched:
                raise Unmatched([{"value": v, **usage(conn, column_id, v)} for v in unmatched])
            ts = (now or datetime.now(timezone.utc)).isoformat()
            for v in stored:
                if by_lower[v.lower()] != v:
                    _respell(conn, user_id, col, v, by_lower[v.lower()], ts)
        conn.execute("UPDATE custom_columns SET type=?, choices=? WHERE id=?",
                     (type, json.dumps(choices), column_id))


def delete_column(conn: sqlite3.Connection, column_id: int) -> None:
    with _tx(conn):
        conn.execute("DELETE FROM custom_values_log WHERE column_id=?", (column_id,))
        conn.execute("DELETE FROM custom_values WHERE column_id=?", (column_id,))
        conn.execute("DELETE FROM custom_columns WHERE id=?", (column_id,))


def values(conn: sqlite3.Connection, site: str | None = None) -> dict[tuple[str, str], dict[int, str]]:
    """Stored values as {(site, cluster_id): {column_id: value}}, optionally for one site."""
    sql, params = "SELECT site, cluster_id, column_id, value FROM custom_values", ()
    if site is not None:
        sql, params = sql + " WHERE site=?", (site,)
    out: dict[tuple[str, str], dict[int, str]] = {}
    for r in conn.execute(sql, params):
        out.setdefault((r["site"], r["cluster_id"]), {})[r["column_id"]] = r["value"]
    return out


def normalize(column: Column, value: str) -> str:
    """Trim and validate a value; a choice is matched ignoring case and returned as spelled."""
    value = _check_value(value)
    if column.type == "choice":
        for c in column.choices:
            if c.lower() == value.lower():
                return c
        raise ValueError(f"{value!r} is not a choice of {column.name!r}")
    return value


def save_values(
    conn: sqlite3.Connection,
    user_id: int,
    site: str,
    cluster_id: str,
    name: str,
    changes: dict[int, str | None],
    now: datetime | None = None,
) -> int:
    """Apply per-column changes: None keeps, "" clears, any other string sets.

    Unknown column ids are ignored. Every change is validated before the first
    write. Only changed values are written, each with one log row. Returns the
    number of changes.
    """
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with _tx(conn):
        cols = {c.id: c for c in list_columns(conn)}
        wanted: dict[int, str] = {}
        for cid, raw in changes.items():
            if raw is None or cid not in cols:
                continue
            wanted[cid] = "" if not raw.strip() else normalize(cols[cid], raw)
        current = values(conn, site).get((site, cluster_id), {})
        n = 0
        for cid, new in wanted.items():
            if new == current.get(cid, ""):
                continue
            if new:
                conn.execute(
                    """INSERT INTO custom_values (site, cluster_id, column_id, value, updated_by, updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT (site, cluster_id, column_id) DO UPDATE SET
                         value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                    (site, cluster_id, cid, new, user_id, ts),
                )
            else:
                conn.execute("DELETE FROM custom_values WHERE site=? AND cluster_id=? AND column_id=?",
                             (site, cluster_id, cid))
            conn.execute(
                """INSERT INTO custom_values_log
                   (site, cluster_id, name, column_id, column_name, value, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (site, cluster_id, name, cid, cols[cid].name, new, user_id, ts),
            )
            n += 1
        return n


def history(conn: sqlite3.Connection, site: str, cluster_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT l.updated_at, u.username AS updated_by_name, l.column_name, l.value
           FROM custom_values_log l LEFT JOIN users u ON u.id = l.updated_by
           WHERE l.site=? AND l.cluster_id=? ORDER BY l.updated_at DESC, l.id DESC""",
        (site, cluster_id),
    ).fetchall()
    return [dict(r) for r in rows]


def merged_history(tracking_history: list[dict], custom_history: list[dict]) -> list[dict]:
    """Both change logs in one list, newest first; ties keep tracking entries first."""
    items = [{**h, "kind": "tracking"} for h in tracking_history]
    items += [{**h, "kind": "custom"} for h in custom_history]
    return sorted(items, key=lambda h: h["updated_at"], reverse=True)


def sort_key(column: Column, value: str | None) -> str | None:
    if value is None:
        return None
    if column.type == "choice":
        return str(column.choices.index(value)) if value in column.choices else None
    return value


def csv_fields(columns: list[Column], row: dict) -> dict[str, str]:
    """One CSV field per column, "custom_<name>": the cluster's value or "none"."""
    custom = row.get("custom", {})
    return {f"custom_{c.name}": custom.get(c.id) or RESERVED for c in columns}


def parse_filters(columns: list[Column], params: Mapping[str, str]) -> tuple[dict[int, str], list[str]]:
    """Valid {column_id: value} filters from cf<id> URL params, plus notices for ignored ones."""
    by_id = {c.id: c for c in columns}
    filters: dict[int, str] = {}
    notices: list[str] = []
    for key, raw in params.items():
        cid = column_id_from_key(key)
        value = raw.strip()
        if cid is None or not value:
            continue
        col = by_id.get(cid)
        if col is None:
            notices.append(f"Ignored filter {key}: the column no longer exists")
        elif col.type == "choice" and value.lower() != RESERVED and value not in col.choices:
            notices.append(f'Ignored filter {col.name}: "{value}" is not a choice')
        else:
            filters[cid] = RESERVED if col.type == "choice" and value.lower() == RESERVED else value
    return filters, notices


def matches(columns_by_id: dict[int, Column], filters: dict[int, str], row_values: dict[int, str]) -> bool:
    """Whether a cluster's values satisfy every filter."""
    for cid, wanted in filters.items():
        col, have = columns_by_id.get(cid), row_values.get(cid)
        if col is None:
            continue
        if col.type == "choice":
            ok = have is None if wanted == RESERVED else have == wanted
        else:
            ok = have is not None and have.lower() == wanted.lower()
        if not ok:
            return False
    return True
