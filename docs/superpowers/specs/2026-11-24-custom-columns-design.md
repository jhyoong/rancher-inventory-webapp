# Custom columns on the cluster list

Nov 24, 2026

## Why

Platform engineers want to record facts about each cluster that Rancher does not know, starting with the type of workload a cluster supports (a `Tier` column), and use them to sort, filter and organise the cluster list. More columns will be needed later, so columns are defined in the app at runtime, not in code or config.

"Syncs with the cluster info" (user's words) means: values are typed into the app, stay attached to the cluster (site + cluster ID), and collection runs never change or erase them, the same guarantee as migration tracking (status, wave, note).

## Who uses it

Platform engineers, the same people who use migration tracking. Both roles (`admin`, `viewer`) can set values. Only admins define columns.

## Goals

- Admins can add, rename, reorder and delete custom columns at any time.
- Each column is either **free text** or a **choice list** with an ordered set of values.
- Engineers set values per cluster (cluster page) and for many clusters at once (bulk-edit bar).
- Custom columns appear in the cluster tables and can be sorted; a choice column sorts in its defined order.
- `/clusters` can be filtered by custom column values, combined with the existing filters.
- Every value change is logged (who, when, old state readable), like migration tracking (decided by the user: "keep it consistent").

## Non-goals

- Values read from Rancher labels or annotations (decided by the user: typed into the app).
- Custom columns in the overview charts, on the `/query` page, in presets or in the discovery export.
- Per-column permissions, required columns, default values, number or date column types.
- Custom columns on removed clusters' list on the overview (values are kept, just not shown there).

## Anti-goals

- A collection run that changes or erases custom values or columns.
- Blank cells that look like data: an unset value shows `none`.
- New runtime downloads, frontend frameworks or a build step (airgap).

## Constraints

- Stack as is: FastAPI, Jinja, htmx, vanilla JS in `inventory/web/static/`, SQLite.
- Schema change through a new migration `inventory/db/migrations/005_custom_columns.sql`, and `SCHEMA_VERSION` in `inventory/db/migrate.py` bumped to 5.
- Writes follow the existing pattern: POST, `check_csrf`, `write_db` (see `routes/tracking.py`, `routes/presets.py`).
- Custom values are data that cannot be re-collected: README's backup section and AGENTS.md ("Users, presets and migration tracking ... are the only data that cannot be re-collected") must mention them.

## Approaches

- **A. Two new tables, one row per value (chosen by the user).** `custom_columns` for definitions, `custom_values` for one row per (site, cluster, column), `custom_values_log` for history. Adding a column needs no schema change; filtering is a plain join; `migration_tracking` code is untouched. Cost: extra queries to attach values to table rows.
- B. One JSON field on `migration_tracking`. Fewer tables, but filtering searches JSON, renames and deletes rewrite every row, per-field history is hard to read, and custom data mixes with migration status.
- C. Columns defined in `config/rules.yaml`, values in the DB. Changing columns needs a host file edit and a container restart, which is clumsy in the airgap and against "add more later from the app".

## Design

### Data (migration 005)

- `custom_columns`: `id INTEGER PRIMARY KEY`, `name TEXT NOT NULL` (unique, case-insensitive: `COLLATE NOCASE`), `type TEXT NOT NULL` (`text` or `choice`), `choices TEXT NOT NULL DEFAULT '[]'` (JSON array, in display/sort order; empty for text), `position INTEGER NOT NULL`, `created_at TEXT NOT NULL`.
- `custom_values`: `site`, `cluster_id`, `column_id`, `value TEXT NOT NULL`, `updated_by INTEGER`, `updated_at TEXT NOT NULL`, primary key `(site, cluster_id, column_id)`. No row means `none`; clearing a value deletes the row.
- `custom_values_log`: `id INTEGER PRIMARY KEY`, `site`, `cluster_id`, `name` (cluster name at the time), `column_id`, `column_name` (at the time), `value TEXT NOT NULL` (`''` for a clear), `updated_by INTEGER`, `updated_at`. Index on `(site, cluster_id)`. `updated_by` is a plain user id with no foreign key, as in `migration_tracking_log`.
- Values are keyed by site + cluster ID, so they survive collection runs, cluster renames and cluster removal from Rancher (as tracking does).

### Managing columns (admin only)

A new page `/admin/columns`, linked next to Users in the admin navigation:

- List of columns in position order with type, choices and how many clusters have a value.
- Add: name (1 to 40 characters, unique ignoring case), type, and for a choice list the values one per line (each 1 to 100 characters, unique ignoring case, at least one).
- Rename a column. Move up/down to reorder. The order is the order columns appear in tables, cards, forms and filters.
- Edit choices: add values, reorder them, rename a value (stored values are updated and a log row is written per changed cluster, by the admin), and remove a value only when no cluster uses it. If it is in use, the page says how many clusters and links to `/clusters` filtered by it. *(Builder's call recorded here: block removal instead of silently clearing.)*
- Change type: choice → text is always allowed (values are kept as text). Text → choice is allowed only when every stored value matches one of the new choices (matching ignores case and stores the choice's spelling); otherwise the page lists the values that do not match.
- Delete: a confirmation page showing how many clusters have a value. Deleting removes the column, its values and its log rows. *(My call: a deleted column leaves nothing behind.)*

### Setting values

- **Cluster page:** a "Custom fields" form next to the migration tracking form, one control per column: a dropdown (`none` + choices) for choice columns, a text input (max 100 characters, trimmed) for text columns. Posts to `/clusters/{site}/{cluster_id}/custom`. Only changed values are saved and logged. An empty text input clears the value.
- **Bulk-edit bar** (`bulk_bar` in `_cluster_list.html`, posted to `/migration/bulk`): one control per custom column, after Wave. Choice: `no change` / `clear` / each value. Text: input with placeholder `no change` plus a "Clear <name>" checkbox, as Wave does today. Unchanged columns are not touched.
- **History:** the cluster page shows custom value changes in the same history list as tracking changes, merged by time, e.g. "Tier: critical, by alice, 2026-11-24 10:02". A clear shows as "Tier: cleared".
- Values for a column that was deleted between page load and submit are ignored. A value not in a choice list is rejected with 400.
- **Reserved word:** `none` (any case) is not allowed as a choice value or a text value, because it labels unset cells and is the "no value" filter token. Rejected in column definitions, the cluster form and the bulk bar. *(My call.)*
- **Removed clusters** (no longer in Rancher) keep their values, as tracking does. The removed-cluster page (`cluster_removed.html`) shows the Custom fields form and the custom history, and the POST route accepts removed clusters, like `save_one` does for tracking. The bulk bar on the overview's "Removed from Rancher" table does not show custom controls (`bulk_bar` takes a flag), since that table does not show custom columns. *(My call, for consistency with tracking.)*
- **Admin counts include removed clusters.** When removing a choice or changing text → choice is blocked, the message gives the live count with a link to `/clusters` filtered by the value, and lists any removed clusters holding it, each linking to its removed-cluster page where the value can be changed. *(My call.)*
- History rows show the column name stored at the time of the change. Re-spellings made by a text → choice change are logged, by the admin.

### Where values show

- **Tables:** the overview table on `/` and the `/clusters` table get one column per custom column, after Wave, in column order. Unset shows `none`.
- **Sorting:** `static/tables.js` sorts cells by text. Custom cells carry a sort key attribute: for choice columns, the choice's index (so the defined order sorts); for text, the text. `none` cells always sort last, like `not available` today. `tables.js` gains support for a per-cell `data-sort` key (and a way to mark a cell as "last") without changing how other columns sort. *(Builder decides the attribute details.)*
- **Cards view:** one `dt/dd` pair per column, after Wave.
- **Cluster page:** the Custom fields form shows the current values.
- **Links:** in the overview table, a set value links to `/clusters?cf<id>=<value>`.
- **CSV** of `/clusters`: one column per custom column named `custom_<name>`, empty values written as `none`. The legacy `inventory` CSV table is unchanged.

### Filtering on `/clusters`

- URL parameter per column: `cf<id>`, using the column id so renaming a column keeps bookmarks working. *(My call; replaces the `cf_tier` example from the discussion.)*
- Choice column: a dropdown `any` / `none` / each value. `none` matches clusters with no value.
- Text column: a text input matching the exact value, ignoring case. *(My call; Wave matches exactly today.)*
- Custom filters combine with the existing site, class, flag, label, status and wave filters (all must match). Done in `migration.cluster_rows` alongside the existing filters.
- A `cf<id>` for a column that no longer exists, or a choice value that is not in the list, is ignored and the page shows a short notice naming it. Old bookmarks still load.
- Correction to the discussion: presets belong to the `/query` page (resource filters), not `/clusters`, so presets do not carry custom filters. Bookmarks of the `/clusters` URL do.

### Overview page

The new columns and sorting appear in the overview table. The charts, totals, site filter and remaining/all toggle are unchanged and ignore custom columns.

## Left to the builder

- Exact markup and CSS of the admin page, forms and bulk bar, following existing styles in `app.css`.
- How values are attached to rows (one query per page for all values, then merged in Python is fine).
- The `data-sort` mechanics in `tables.js`.
- Test layout, following existing web tests (`admin_client`, `viewer_client`, CSRF via `client.csrf`).
