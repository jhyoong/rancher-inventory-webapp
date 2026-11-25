# Custom Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin-defined custom columns (free text or ordered choice list) with per-cluster values, history, sorting and filtering on the cluster lists.

**Architecture:** A new data module `inventory/web/custom_columns.py` over three new tables (migration 005). `migration.cluster_rows` attaches values and applies `cf<id>` filters; templates render one column per definition; a new admin page manages definitions; the existing tracking routes gain custom fields.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLite, vanilla JS (`static/tables.js`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-custom-columns-design.md`

## Global Constraints

- **No commits by the agent.** The user's global rules say "NEVER commit on behalf of user". Each task ends with a checkpoint (tests green, files listed); the user commits.
- Migration file `inventory/db/migrations/005_custom_columns.sql`; `SCHEMA_VERSION = 5` in `inventory/db/migrate.py`; update `tests/test_migrate_upgrade.py` assertions (`SCHEMA_VERSION == 4` → `5`).
- Column name 1–40 characters, unique ignoring case. Value 1–100 characters after trim. `none` (any case) is rejected as a choice or text value.
- Unset value displays `none`, sorts last in both directions.
- Writes: POST + `check_csrf` + `write_db`. Column management requires `require_admin`; values editable by `admin` and `viewer`.
- Filter URL parameter is `cf<id>` (e.g. `cf3=critical`); choice filter value `none` means "no value"; text filter matches exactly, ignoring case.
- No new dependencies, no remote assets, templates and static files stay under `inventory/web/`.
- Offline test command: `SESSION_SECRET=x RANCHER_DEV_TOKEN=dummy uv run pytest -m "not live and not slow"` (or plain `uv run pytest -m "not live and not slow"` with a `.env`).

## Review Focus

1. A choice value or text value spelled `none`/`NONE` submitted through any path (column form, cluster form, bulk bar) → rejected with 400 / form error, nothing written. Test in Tasks 1, 2 and 5.
2. A bookmark `/clusters?cf99=x` for a deleted column, or `cf3=bogus` for a value not in the list → page loads 200, other filters still apply, a notice names the ignored filter. Test in Task 7.
3. Collection must not touch custom data: the `_remove_unlisted` path deletes cluster data but must leave `custom_values`/`custom_values_log` intact, and a cluster re-listed later shows its values again. Test in Task 2.
4. Removing a choice used only by a removed cluster → blocked, message lists the removed cluster with a link to its page, where the value can be changed. Test in Tasks 1 and 4.
5. Bulk edit with only a custom field set (no status, no wave) → accepted, not "nothing to change"; bulk edit with nothing set at all → 400 as today. Test in Task 5.

---

### Task 1: Migration 005 and column definitions

**Files:**
- Create: `inventory/db/migrations/005_custom_columns.sql`
- Create: `inventory/web/custom_columns.py`
- Modify: `inventory/db/migrate.py` (`SCHEMA_VERSION = 5`), `tests/test_migrate_upgrade.py` (`== 4` → `== 5`)
- Test: `tests/test_custom_columns.py`

**Interfaces:**
- Produces (in `inventory/web/custom_columns.py`):
  - `NAME_MAX = 40`, `VALUE_MAX = 100`, `RESERVED = "none"`, `TYPES = ("text", "choice")`
  - `@dataclass(frozen=True) class Column: id: int; name: str; type: str; choices: list[str]; position: int`
  - `class InUse(ValueError)` with attributes `value: str`, `live: int`, `removed: list[dict]` (each `{"site", "cluster_id", "name"}`)
  - `list_columns(conn) -> list[Column]` (by position)
  - `get_column(conn, column_id: int) -> Column | None`
  - `create_column(conn, name: str, type: str, choices: list[str]) -> int`
  - `rename_column(conn, column_id: int, name: str) -> None`
  - `move_column(conn, column_id: int, direction: str) -> None` (`"up"`/`"down"`, no-op at the ends)
  - `set_choices(conn, column_id: int, choices: list[str]) -> None` (add/reorder/remove; raises `InUse` for a removed value still stored)
  - `rename_choice(conn, user_id: int, column_id: int, old: str, new: str, now: datetime | None = None) -> int` (updates stored values, logs one row per changed cluster, returns count)
  - `change_type(conn, user_id: int, column_id: int, type: str, choices: list[str], now: datetime | None = None) -> None` (text→choice raises `ValueError` listing unmatched values; matching re-spells and logs)
  - `delete_column(conn, column_id: int) -> None` (deletes column, values, log rows)
  - `usage(conn, column_id: int, value: str | None = None) -> dict` → `{"live": int, "removed": list[dict]}`; live = clusters in `clusters`, removed = rows joined to `removed_clusters`

- [ ] **Step 1: Write the migration** — tables exactly as the spec's "Data (migration 005)" section: `custom_columns` (`name ... COLLATE NOCASE UNIQUE`), `custom_values` (PK `(site, cluster_id, column_id)`), `custom_values_log` (+ index `idx_custom_values_log_cluster` on `(site, cluster_id)`). Bump `SCHEMA_VERSION` and the two upgrade-test assertions.

- [ ] **Step 2: Write failing tests** in `tests/test_custom_columns.py` (use the `db` fixture; insert clusters with `INSERT INTO clusters (site, cluster_id, name) VALUES (...)`, removed ones into `removed_clusters`):

```python
def test_create_and_list_in_position_order(db):
    a = cc.create_column(db, "Tier", "choice", ["critical", "standard", "dev"])
    b = cc.create_column(db, "Owner team", "text", [])
    assert [(c.id, c.name, c.type, c.choices) for c in cc.list_columns(db)] == [
        (a, "Tier", "choice", ["critical", "standard", "dev"]), (b, "Owner team", "text", [])]

@pytest.mark.parametrize("name,type_,choices", [
    ("", "text", []), ("x" * 41, "text", []), ("Tier", "number", []),
    ("C", "choice", []), ("C", "choice", ["a", "A"]), ("C", "choice", ["none"]),
    ("C", "choice", ["x" * 101]), ("C", "text", ["a"])])
def test_create_rejects(db, name, type_, choices):
    with pytest.raises(ValueError):
        cc.create_column(db, name, type_, choices)

def test_name_unique_ignoring_case(db):
    cc.create_column(db, "Tier", "text", [])
    with pytest.raises(ValueError):
        cc.create_column(db, "tier", "text", [])

def test_move_and_rename(db): ...           # move b up -> [b, a]; rename keeps id
def test_remove_used_choice_blocked_with_removed_cluster(db):
    # value "dev" stored only on a removed cluster -> InUse(live=0, removed=[{"site":..,"cluster_id":..,"name":..}])
def test_rename_choice_updates_values_and_logs(db): ...   # returns 1; value now "prod"; one log row by user 1
def test_change_type_text_to_choice(db):
    # stored "Critical" + choices ["critical"] -> value re-spelled "critical", logged
    # stored "weird" -> ValueError mentioning "weird", nothing changed
def test_delete_column_removes_values_and_log(db): ...
```

Fill the `...` bodies with the assertions named in their comments.

- [ ] **Step 3: Run** `uv run pytest tests/test_custom_columns.py tests/test_db.py tests/test_migrate_upgrade.py -v` — expected: new tests FAIL (module missing).

- [ ] **Step 4: Implement** the module functions above. Store `choices` as JSON. Positions: new column gets `max(position)+1`; `move_column` swaps with the neighbour. Validation helper `_check_value(v: str) -> str` (trim, 1–100, not `none` ignoring case) is shared with Task 2.

- [ ] **Step 5: Run** the Step 3 command — expected: PASS. Then the full offline suite — expected: PASS.

- [ ] **Step 6: Checkpoint** — list changed files for the user to commit.

---

### Task 2: Values, log and history

**Files:**
- Modify: `inventory/web/custom_columns.py`
- Test: `tests/test_custom_columns.py`

**Interfaces:**
- Consumes: Task 1 (`Column`, `list_columns`, `_check_value`).
- Produces:
  - `values(conn, site: str | None = None) -> dict[tuple[str, str], dict[int, str]]`
  - `normalize(column: Column, value: str) -> str` (trims; choice matches ignoring case and returns the choice's spelling; raises `ValueError` otherwise or for `none`)
  - `save_values(conn, user_id: int, site: str, cluster_id: str, name: str, changes: dict[int, str | None], now: datetime | None = None) -> int` — `None` keeps, `""` clears, other strings set; unknown column ids are ignored; validates every change before the first write; writes only changed values; one log row per change (`value=''` for a clear) with the column's current name; returns the number of changes.
  - `history(conn, site: str, cluster_id: str) -> list[dict]` newest first, keys `updated_at, updated_by_name, column_name, value`.
  - `merged_history(tracking_history: list[dict], custom_history: list[dict]) -> list[dict]` — newest first; each item gets `kind` = `"tracking"` or `"custom"`.
  - `sort_key(column: Column, value: str | None) -> str | None` — choice: the choice's index as a string; text: the value; `None` for unset.

- [ ] **Step 1: Write failing tests:**

```python
def test_save_set_keep_clear_and_log(db):
    t, o = _cols(db)   # Tier choice [critical, standard, dev], Owner text
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: "Critical", o: "payments"}) == 2
    assert cc.values(db)[("dev", "c1")] == {t: "critical", o: "payments"}
    assert cc.save_values(db, 1, "dev", "c1", "c1", {t: None, o: ""}) == 1
    assert cc.values(db)[("dev", "c1")] == {t: "critical"}
    assert [h["value"] for h in cc.history(db, "dev", "c1")] == ["", "payments", "critical"]

def test_unchanged_writes_nothing(db): ...                     # same value again -> 0, no new log row
@pytest.mark.parametrize("v", ["none", "NONE", "bogus", "x" * 101])
def test_save_rejects_and_writes_nothing(db, v): ...           # on Tier (and Owner for "none"/101 chars): ValueError, values() empty, history empty
def test_unknown_column_ignored(db): ...                        # {999: "x"} -> 0
def test_values_survive_cluster_removal(db):
    # save a value on dev/c1, run runner._remove_unlisted(db, "dev", set()), then:
    # custom_values and history still there; upsert_cluster(...) for c1 again -> values(db) still has it
def test_merged_history_orders_by_time(): ...
def test_sort_key(): ...                                         # choice index "2" for "dev"; text -> value; None -> None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_custom_columns.py -v` — expected: new tests FAIL.
- [ ] **Step 3: Implement** the functions above (follow `tracking.save`'s style for timestamps: `(now or datetime.now(timezone.utc)).isoformat(timespec="seconds")`).
- [ ] **Step 4: Run** Step 2 command, then the full offline suite — expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 3: Admin page `/admin/columns`

**Files:**
- Create: `inventory/web/routes/columns.py`, `inventory/web/templates/admin_columns.html`, `inventory/web/templates/admin_column_delete.html`
- Modify: `inventory/web/app.py` (register router), `inventory/web/templates/base.html` (link "Columns" after "Users" inside the admin block)
- Test: `tests/test_web_custom_columns.py`

**Interfaces:**
- Consumes: Task 1 functions and `InUse`, Task 1 `usage`.
- Produces routes (all `require_admin`; POSTs `check_csrf`, redirect 303 to `/admin/columns` on success, re-render with `error` on `ValueError`/`InUse` at status 400):
  - `GET /admin/columns` — table: name, type, choices, `usage(...)["live"]` + removed count; add form; per-column forms.
  - `POST /admin/columns` with `action` ∈ `add` (`name`, `type`, `choices` textarea, one per line, blank lines dropped), `rename` (`column_id`, `name`), `move` (`column_id`, `direction`), `choices` (`column_id`, `choices` textarea), `rename_choice` (`column_id`, `old`, `new`), `type` (`column_id`, `type`, `choices`).
  - `GET /admin/columns/{column_id}/delete` — confirmation page with the usage count; `POST /admin/columns/{column_id}/delete` deletes.
  - `InUse` error text: `"<value>" is used by N clusters` with a link to `/clusters?cf<id>=<value>`, then each removed cluster as a link to `/clusters/<site>/<cluster_id>`.

- [ ] **Step 1: Write failing tests** (`admin_client`, `viewer_client`, `client.csrf`):

```python
def test_viewer_cannot_manage_columns(viewer_client):
    assert viewer_client.get("/admin/columns").status_code == 403
    r = viewer_client.post("/admin/columns", data={"csrf": viewer_client.csrf, "action": "add", "name": "Tier", "type": "text"})
    assert r.status_code == 403

def test_add_choice_column_and_list(admin_client): ...          # add Tier with "critical\nstandard\n\ndev" -> page lists critical, standard, dev in order; nav has href="/admin/columns"
def test_add_rejects_none_choice(admin_client): ...             # 400, error shown, list_columns empty
def test_remove_used_choice_shows_links(admin_client, web_settings): ...   # seed live + removed cluster values; 400; body has /clusters?cf<id>=dev and /clusters/dev/c-gone
def test_delete_needs_confirmation_page(admin_client): ...      # GET shows count; POST deletes
def test_post_without_csrf_rejected(admin_client): ...
```

Seed DB rows through `connect(web_settings.db_path)` as `viewer_client` does in `conftest.py`.

- [ ] **Step 2: Run** `uv run pytest tests/test_web_custom_columns.py -v` — expected: FAIL.
- [ ] **Step 3: Implement** routes and templates, following `routes/admin.py` and `admin_users.html` structure and existing CSS classes.
- [ ] **Step 4: Run** Step 2 command and full offline suite — expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 4: Custom fields on the cluster page and removed-cluster page

**Files:**
- Create: `inventory/web/templates/_custom_fields.html` (macro `custom_form(site, cluster_id, columns, current)`)
- Modify: `inventory/web/routes/tracking.py` (new route), `inventory/web/routes/clusters.py` (pass `custom_columns`, `custom_current`, merged history to both `cluster.html` and `cluster_removed.html`), `inventory/web/templates/_migration.html` (history renders `kind == "custom"` rows as `<td>time</td><td>user</td><td colspan="3">{column_name}: {value or "cleared"}</td>`), `cluster.html`, `cluster_removed.html`
- Test: `tests/test_web_custom_columns.py`

**Interfaces:**
- Consumes: Task 2 `save_values`, `values`, `history`, `merged_history`; `tracking.cluster_name`.
- Produces: `POST /clusters/{site}/{cluster_id}/custom` (CSRF, any logged-in role). Form fields `cf<id>` for every column; choice select options: `""` labelled `none`, then choices; text input `maxlength=100`. The route builds `changes` as `{id: submitted_value}` for every column present in the form (empty string clears), 404 for an unknown cluster, 400 on `ValueError`, redirect 303 to `/clusters/<site>/<cluster_id>#custom`. Section heading `<h2 id="custom">Custom fields</h2>`, placed just before the Migration section. Not shown when no columns exist.

- [ ] **Step 1: Write failing tests:**

```python
def test_viewer_sets_values_on_cluster_page(viewer_client, ...):  # seed cluster dev/c1 + Tier column; POST cf<id>=critical -> 303; GET /clusters/dev/c1 shows selected critical and history row "Tier: critical"
def test_removed_cluster_page_edits_values(admin_client, ...):    # seed removed_clusters dev/c-gone; POST works; page shows form + history
def test_cluster_post_rejects_none_text(admin_client, ...):       # Owner text "none" -> 400, nothing stored
def test_unknown_cluster_404(admin_client): ...
```

- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement.** The tracking history passed to `migration_form` becomes `merged_history(tracking.history(...), custom_columns.history(...))`; tracking rows keep their current rendering.
- [ ] **Step 4: Run** the file's tests, `tests/test_web_migration.py -m "not live"`, and the full offline suite — expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 5: Custom fields in the bulk-edit bar

**Files:**
- Modify: `inventory/web/templates/_cluster_list.html` (`bulk_bar(next, custom=True)`), `inventory/web/templates/home.html` (removed table calls `bulk_bar(removed_next, custom=False)`), `inventory/web/routes/tracking.py` (`save_bulk`), `inventory/web/routes/home.py` and `routes/clusters.py` (pass `custom_columns`)
- Test: `tests/test_web_custom_columns.py`

**Interfaces:**
- Consumes: Task 2 `save_values`, Task 1 `list_columns`.
- Produces: bulk bar controls per column after Wave. Choice: `<select name="cf<id>">` with `""` = `no change`, `"none"` = `clear`, then choices. Text: `<input name="cf<id>" placeholder="no change">` and `<input type="checkbox" name="cf<id>_clear" value="1">` labelled `Clear <name>`. `save_bulk` reads them from `await request.form()` (add `request: Request`), maps choice `"none"` and text-clear to `""`, empty to `None`, rejects text with both value and clear (400), validates every change with `normalize` before writing, and writes tracking + custom changes for all targets inside the existing `BEGIN IMMEDIATE` transaction. "nothing to change" now means no status, no wave and no custom change.

- [ ] **Step 1: Write failing tests:**

```python
def test_bulk_custom_only(admin_client, ...):     # two clusters, cf<tier>=standard only -> 303; both have standard; tracking rows unchanged
def test_bulk_clear_choice_and_text(admin_client, ...):  # cf<tier>=none and cf<owner>_clear=1 -> values removed, logged as clears
def test_bulk_nothing_to_change(admin_client, ...):      # -> 400 "nothing to change"
def test_bulk_rejects_invalid_choice_atomically(admin_client, ...):  # status=planned + cf<tier>=bogus -> 400, no tracking or custom rows written
def test_removed_table_has_no_custom_controls(admin_client, ...):    # GET / with a removed cluster: the removed section's form has no name="cf<id>"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** file + full offline suite — PASS. **Step 5: Checkpoint.**

---

### Task 6: Columns in the tables, cards, sorting, links and CSV

**Files:**
- Modify: `inventory/web/migration.py` (`cluster_rows` attaches `row["custom"] = values(...).get(key, {})`), `inventory/web/templates/_cluster_list.html` (headers/cells/cards), `inventory/web/static/tables.js`, `inventory/web/routes/clusters.py` (CSV), `inventory/web/custom_columns.py` (`csv_fields`)
- Test: `tests/test_web_custom_columns.py`, a JS-free check of rendered attributes

**Interfaces:**
- Consumes: Task 2 `values`, `sort_key`.
- Produces:
  - `cluster_list(rows, view)` uses `custom_columns` from context (both routes already pass it after Task 5). Columns after Wave in table and cards. Cell: unset → `<td data-sort-last="1">none</td>`; set → `<td data-sort="{{ sort_key }}">value</td>`; on the overview page (`request.url.path == "/"`) a set value is wrapped in `<a href="/clusters?cf<id>=<value|urlencode>">`.
  - `tables.js`: in `sortBy`, a cell with `data-sort-last` gets `{last: true}`; a cell with `data-sort` uses `key(cell.dataset.sort)` instead of `textContent`. No other behaviour changes.
  - `csv_fields(columns: list[Column], row: dict) -> dict[str, str]` → `{"custom_<name>": value or "none"}`; the `/clusters` CSV `clusters` table rows get these keys merged after `flat_row(r)` (and `flat_row` must skip the `custom` dict key).

- [ ] **Step 1: Write failing tests:**

```python
def test_table_shows_custom_columns_with_sort_keys(admin_client, ...):
    # Tier choices [critical, standard, dev]; c1=dev, c2 unset
    # /clusters body: "<th>Tier</th>", 'data-sort="2"' before ">dev<", 'data-sort-last="1">none<'
def test_overview_links_value_to_filter(admin_client, ...):   # GET / contains href="/clusters?cf<id>=dev"
def test_cards_show_custom(admin_client, ...):                # /clusters?view=cards has <dt>Tier</dt><dd>dev</dd>
def test_csv_has_custom_columns(admin_client, ...):           # /clusters?format=csv DictReader rows have custom_Tier == "dev" / "none"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** file, `tests/test_web_migration.py`, full offline suite — PASS. Manually sort the Tier column in a browser once (`uv run inventory serve` against a dev DB) and confirm `critical, standard, dev, none` ascending and `dev, standard, critical, none` descending. **Step 5: Checkpoint.**

---

### Task 7: Filtering on `/clusters`

**Files:**
- Modify: `inventory/web/custom_columns.py`, `inventory/web/migration.py` (`cluster_rows(..., custom: dict[int, str] | None = None)`), `inventory/web/routes/clusters.py`, `inventory/web/templates/clusters.html`
- Test: `tests/test_web_custom_columns.py`, `tests/test_custom_columns.py`

**Interfaces:**
- Consumes: Task 1 `list_columns`, Task 2 `values`.
- Produces:
  - `parse_filters(columns: list[Column], params: Mapping[str, str]) -> tuple[dict[int, str], list[str]]` — reads keys matching `^cf\d+$` with non-empty values; unknown column → notice `Ignored filter cf<id>: the column no longer exists`; choice value not in choices and not `none` → notice `Ignored filter <name>: "<value>" is not a choice`; returns valid filters and notices.
  - `matches(columns_by_id: dict[int, Column], filters: dict[int, str], row_values: dict[int, str]) -> bool` — choice `none` → no value; choice value → equal; text → equal ignoring case.
  - `cluster_rows` applies `matches` after the wave filter when `custom` is given.
  - `clusters.html`: one control per column in the filter form after Wave (choice: `any` = `""`, `none`, choices; text: input placeholder `exact value`), selected state from the URL; notices rendered above the results in a `<p class="warn">`.

- [ ] **Step 1: Write failing tests:**

```python
def test_parse_filters_notices(): ...      # {"cf1": "dev", "cf99": "x", "cf1x": "y", "cf2": ""} with col 1 choice -> ({1: "dev"}, [notice for cf99])
def test_filter_choice_none_and_text_case(admin_client, ...):
    # c1 Tier=dev Owner=Payments, c2 unset
    # ?cf<tier>=dev -> c1 only; ?cf<tier>=none -> c2 only; ?cf<owner>=payments -> c1 only
def test_filter_combines_with_status(admin_client, ...): ...
def test_stale_bookmark_loads_with_notice(admin_client, ...):   # ?cf999=x&cf<tier>=bogus -> 200, both notices, all clusters listed
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** file + full offline suite — PASS. **Step 5: Checkpoint.**

---

### Task 8: Docs

**Files:**
- Modify: `README.md` (backup section "Only users, presets and trend history cannot be collected again" → add custom column values and migration tracking; "Migration views" section: a short "Custom columns" subsection: admins manage at `/admin/columns`, values on the cluster page and bulk bar, filter with `cf<id>`), `AGENTS.md` (the "Users, presets and migration tracking ... are the only data that cannot be re-collected" sentence adds custom columns; mention `custom_columns.py` in the Web paragraph)

- [ ] **Step 1: Edit** both files.
- [ ] **Step 2: Run** the full offline suite once more — PASS.
- [ ] **Step 3: Checkpoint** — final list of all changed files for the user to commit.
