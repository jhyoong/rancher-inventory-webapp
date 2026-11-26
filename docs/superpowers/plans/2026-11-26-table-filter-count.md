# Table filter count, column filter, cards removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Row counts and a column picker on filtered tables, no cluster Cards view, and a clickable "special network config" tile.

**Architecture:** Count and column picker are added generically by `inventory/web/static/tables.js`; its pure helpers are exported under Node for tests. Cards removal and the tile link are Jinja/route/CSS changes.

**Tech Stack:** FastAPI + Jinja, vanilla JS (no build), pytest; Node (if installed) runs the JS helper test.

**Spec:** `docs/superpowers/specs/2026-11-26-table-filter-count-design.md`

## Global Constraints

- Airgap: no new assets, CDN or dependencies.
- Count copy: `240 rows` when not filtering (`1 row` for one), `12 of 240 rows shown` while filtering.
- Column picker first option: `All columns`.
- Bulk edit: a row hidden by the filter loses its `input[name=cluster]` tick; "Select all shown" only touches visible rows (existing behaviour, keep).
- Never commit on the user's behalf (user's CLAUDE.md) — commit steps are left for the user; stop and report instead.

## Review Focus

- Empty result ("No clusters match." colspan row) → count reads `0 rows`, not `1 row`. (Task 3 node test on `countText(0,0)`; the placeholder is excluded by the existing `isData` rule.)
- Column picker set to a column, then typing → only that column's text is matched; switching back to `All columns` re-applies on the whole row. (Task 3 node test on `matches`.)
- Column filter after sorting a column → still matches the right column (cells are matched by index, rows move whole). Covered by index-based `matches`.
- Custom columns / "Matched" column appear in the picker because headers are read from the DOM (manual check in Task 3 Step 5).
- Old bookmarks with `?view=cards` → 200 and the table (Task 1 test).

---

### Task 1: Remove the cluster Cards view

**Files:**
- Modify: `inventory/web/templates/_cluster_list.html` (drop `.view-switch` block and the `{% if view == "cards" %}…{% else %}` branch; macro becomes `cluster_list(rows)`)
- Modify: `inventory/web/templates/home.html`, `inventory/web/templates/clusters.html` (drop hidden `view` inputs; call `cluster_list(rows)`)
- Modify: `inventory/web/routes/clusters.py` (remove `VIEWS`, `view`), `inventory/web/routes/home.py` (remove `VIEWS` import, `view`, and `("view", view)` from `keep`)
- Modify: `inventory/web/static/app.css` (delete `/* Cluster cards */` block; drop `.view-switch` from the two selectors)
- Modify: `README.md:511,517` (no "switches between Table and Cards" / "Table and Cards views")
- Test: `tests/test_web_tables.py` (create; offline, no `live` marker), remove card assertions in `tests/test_web_members.py:38-39`, `tests/test_tracking.py:213-214` (also drop `&view=table` from line 212's URL and expected value), `tests/test_web_custom_columns.py:325-328` (delete `test_cards_show_custom`), `tests/test_web_migration.py:81-91` (rename to `test_clusters_page_table`, drop cards/bogus-view assertions; keep a `?view=cards` request asserting the table), `:202`, `:310`, `:333-335`.

**Interfaces:** Produces `cluster_list(rows)` (Jinja macro, no `view` arg).

- [ ] **Step 1: Write the failing test** in `tests/test_web_tables.py`:

```python
def test_cards_view_removed(admin_client):
    for path in ("/clusters", "/clusters?view=cards", "/", "/?view=cards"):
        body = admin_client.get(path).text
        assert "<th>Watch out</th>" in body, path
        assert "view-switch" not in body and "cluster-cards" not in body and "view=cards" not in body, path
```

- [ ] **Step 2:** `SESSION_SECRET=x RANCHER_DEV_TOKEN=dummy uv run pytest tests/test_web_tables.py::test_cards_view_removed -v` → FAIL (view-switch present).
- [ ] **Step 3:** Make the template, route, CSS and README changes listed above; update the listed tests.
- [ ] **Step 4:** `SESSION_SECRET=x RANCHER_DEV_TOKEN=dummy uv run pytest -m "not live and not slow"` → all pass. `grep -rn "cards\|VIEWS\|view=" inventory/web/routes inventory/web/templates/_cluster_list.html inventory/web/templates/clusters.html` → only the `.cards` stat tiles in `home.html` remain.
- [ ] **Step 5:** Stop for user review/commit.

### Task 2: Link the special-network tile

**Files:**
- Modify: `inventory/web/templates/home.html:22`
- Modify: `inventory/web/static/app.css` (stat cards section)
- Test: `tests/test_web_tables.py`

- [ ] **Step 1: Write the failing test:**

```python
def test_special_network_tile_links_to_clusters(admin_client):
    assert 'href="/clusters?flag=special-network-config"' in admin_client.get("/").text
    assert 'href="/clusters?flag=special-network-config&amp;site=dev"' in admin_client.get("/?site=dev").text
```

- [ ] **Step 2:** run it → FAIL.
- [ ] **Step 3:** Replace the tile `div` with `<a class="card" href="/clusters?flag=special-network-config{% if site %}&site={{ site | urlencode }}{% endif %}">…same content…</a>`. CSS: `a.card { display: block; text-decoration: none; } a.card:hover, a.card:focus-visible { border-color: var(--accent); }`.
- [ ] **Step 4:** run the test and the offline suite → PASS.
- [ ] **Step 5:** Stop for user review/commit.

### Task 3: Row count and column picker in `tables.js`

**Files:**
- Modify: `inventory/web/static/tables.js`
- Modify: `inventory/web/templates/_cluster_list.html` (add `data-count` to the cluster `<table>`)
- Modify: `inventory/web/static/app.css` (`.table-filter` rules)
- Create: `tests/test_tables_js.py`
- Test: `tests/test_web_tables.py`

**Interfaces:**
- Produces (exported as `module.exports` only when `document` is undefined, i.e. under Node; the browser path is unchanged):
  - `countText(shown: number, total: number, filtering: boolean) -> string`
  - `matches(cellTexts: string[], query: string, col: number) -> boolean` — `query` already lower-cased; `col === -1` means all columns (match against the joined cell texts); otherwise only `cellTexts[col]`; empty query → `true`; case-insensitive substring.
- DOM: filter bar `<div class="table-filter">` containing `<input type="search">` (placeholder/aria-label `Filter rows`) and `<select aria-label="Filter column">`; count `<p class="table-count" aria-live="polite">` inserted directly after the table.

- [ ] **Step 1: Write the failing tests.** `tests/test_tables_js.py`:

```python
import json, shutil, subprocess
from pathlib import Path
import pytest

JS = Path(__file__).parents[1] / "inventory/web/static/tables.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

def run(expr):
    out = subprocess.run(["node", "-e", f"const t=require({json.dumps(str(JS))});console.log(JSON.stringify({expr}))"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)

def test_count_text():
    assert run("[t.countText(240,240,false), t.countText(1,1,false), t.countText(0,0,false), t.countText(12,240,true), t.countText(0,240,true)]") == [
        "240 rows", "1 row", "0 rows", "12 of 240 rows shown", "0 of 240 rows shown"]

def test_matches():
    assert run("""[t.matches(["dev","alpha","migrated"],"alpha",-1), t.matches(["dev","alpha"],"dev",1),
                   t.matches(["dev","Alpha"],"alpha",1), t.matches(["dev","alpha"],"",1), t.matches(["dev","alpha"],"x",-1)]""") == [
        True, False, True, True, False]
```

and in `tests/test_web_tables.py`:

```python
def test_cluster_table_opts_into_count(admin_client):
    for path in ("/", "/clusters"):
        assert "<table data-count" in admin_client.get(path).text, path
```

- [ ] **Step 2:** run both → FAIL (`t.countText is not a function`; no `data-count`).
- [ ] **Step 3: Implement.**
  - Helpers `countText`, `matches` at the top of the IIFE; then `if (typeof document === "undefined") { module.exports = { countText: countText, matches: matches }; return; }`.
  - `addFilter(table)` builds the bar (input + select: `All columns` value `-1`, then one option per header whose trimmed text is non-empty, value = column index) and the count line; one `apply()` runs on input `input` and select `change`: for each group, `row.hidden = !matches(cellTexts(row), q, col)`, extras follow, then the existing checkbox untick and select-all logic, then update the count (`shown` = visible groups, `total` = all groups, `filtering = q !== ""`). Keep the Enter-key guard.
  - `addCount(table)` creates the count line; called from `addFilter` and, in `setup`, for tables with fewer than 11 groups that have `data-count`.
  - CSS: `.table-filter { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; } .table-filter input { min-width: 18rem; } .table-count { color: var(--muted); font-size: 0.88rem; margin: 0.4rem 0 1rem; }`.
  - `_cluster_list.html`: `<table data-count>`.
- [ ] **Step 4:** `uv run pytest tests/test_tables_js.py -v` and the offline suite → PASS.
- [ ] **Step 5: Manual check** in the running app (`run` skill or `uv run inventory serve`): Overview cluster table shows the count; picking "Status" and typing filters only that column; a custom column and "Matched" (Clusters `?label=`) appear in the picker; sort then filter still correct; bulk "Select all shown" ticks only visible rows; Workloads/Network tables with >10 rows show both picker and count.
- [ ] **Step 6:** Stop for user review/commit.
