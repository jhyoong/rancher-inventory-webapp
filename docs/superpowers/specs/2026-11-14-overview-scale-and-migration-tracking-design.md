# Overview at scale and manual migration tracking

Nov 14, 2026

## Why

The overview page (`/`) was redesigned in `2026-11-13-migration-overview-redesign-design.md`. Three of its charts draw one group of bars per cluster: vCPU per cluster, memory per cluster, and floating IP needs by source. Sites have 100+ clusters. At that count each bar is about 1px wide in the 32rem chart box and Chart.js hides most cluster names, so the charts cannot be read.

The platform engineers who use the app are also the people who plan the VMware to Nutanix move and report on its progress. The move is a greenfield build on new Nutanix hardware, and the Nutanix clusters will not be added to Rancher. The app has no way to tell which VMware clusters have already moved, so progress must be tracked by hand.

The earlier spec listed "status tracking" as a non-goal. This spec reverses that decision for a small, manual tracker.

## Who uses it

Only platform engineers. They use the overview to plan the migration and report on progress. Everyone who logs in is a platform engineer.

## Goals

Part 1, overview charts that work at any cluster count:

- No overview chart grows with the number of clusters.
- Answer "how much hard work is ahead" and "where are the big ones" at a glance.
- Per-cluster detail stays in the existing sortable cluster table.

Part 2, manual migration tracking:

- Each cluster has a migration status, a wave and a note, set by hand.
- Progress can be reported now and over time.
- Engineers can update many clusters at once.

## Non-goals

- Capacity planning for Nutanix. The hardware budget is already fixed, so capacity stays a simple total.
- Any data about the Nutanix side. Nutanix clusters are not in Rancher, so the app does not collect them. Engineers record where a cluster moved to in the free-text note.
- Scheduling, dates, owners or assignments.
- Automatic status changes based on collected data.

## Anti-goals

- Charts with one bar per cluster on the overview.
- A collection run that changes or erases tracking data.
- Blank cells that look like zero (carried over from the earlier spec): show "not available" or "not collected".
- Mock, sample or synthetic data in the app or tests that pretends to be real inventory (carried over).
- New runtime downloads, front-end frameworks or a build step (carried over; the app runs in an airgap).

## Constraints

- Stack stays as is: FastAPI, Jinja templates, HTMX, Chart.js (`inventory/web/static/chart.umd.min.js`), SQLite.
- Charts use `VizPalette` in `inventory/web/static/overview.js`. No generated colors.
- Schema changes go through a new numbered migration, `inventory/db/migrations/004_migration_tracking.sql`.
- Writes follow the pattern in `inventory/web/routes/presets.py`: POST, `check_csrf`, `write_db`.
- The site filter at the top of the overview applies to every chart, as it does today.
- Existing CSV links keep working.

## Part 1: Overview charts

The charts data comes from `migration.site_overview()` and is drawn in `static/overview.js`. Replace the per-cluster charts and add the new ones. The final set, in this order:

1. **Capacity.** For vCPU and for memory, three horizontal bars: used, requested and allocatable, each a sum across clusters in scope, with the number shown on the bar. They are separate bars, not nested, because CPU used is often above requested. Each sum leaves out clusters with no value for that metric, and the chart says how many clusters each sum covers (for example "used: 87 of 120 clusters report usage"). If no cluster has a value, show "not available". This replaces "vCPU per cluster" and "Memory per cluster". (Builder check call: separate bars and coverage counts.)
2. **Clusters by class.** One horizontal bar per class label from `classify.class_label()`: empty, stateless, stateful, stateful-helm, stateful-crd, stateful-helm-crd. Clusters that have never been collected count as "not collected". Each bar links to `/clusters?cls=<class>` (and `&site=` when a site is selected).
3. **Clusters per flag.** One horizontal bar per complexity or watch-out flag (`migration.ALL_FLAGS`), showing how many clusters have it, sorted by count with the highest first. Flags with zero clusters are hidden. Each bar links to `/clusters?flag=<flag>` (plus site).
4. **Top 10 largest clusters.** Horizontal bars with cluster names, by requested vCPU, with a toggle for requested memory. Below the chart: "Top 10 = N% of all requested vCPU" (or memory). Each bar links to the cluster page. Clusters with no requested value are left out.
5. **Clusters per site, by class.** One stacked horizontal bar per site, split by class. When one site is selected, it shows that site only.
6. **Floating IP needs by source.** One stacked horizontal bar for all clusters in scope. The segments are load balancer, external IP, ingress LB, node external and control plane. This replaces the per-cluster stacked chart.
7. **Unchanged:** Nodes by OS image (donut) and Persistent volumes by storage class.

Horizontal bars are used wherever a chart has category names, so long names stay readable.

The totals cards and the cluster list below the charts do not change in Part 1.

**Remaining work only (decided by the user).** Once Part 2 exists, charts 1 to 6 count only clusters still left to move: status `not started`, `planned` or `in progress`. A toggle above the charts, "Remaining clusters / All clusters", switches to all clusters, and remaining is the default. The OS donut and the storage class chart follow the same toggle. Clusters removed from Rancher have no collected data, so they never appear in these charts. Part 1 can be built first without the toggle, counting all clusters, and the toggle is added with Part 2.

## Part 2: Manual migration tracking

### Data

New table `migration_tracking`, one row per cluster, keyed by `(site, cluster_id)`:

- `status`: one of `not started`, `planned`, `in progress`, `migrated`, `decommissioned`, `won't migrate`
- `wave`: free text, may be empty (for example "wave 1" or "Q1")
- `note`: free text, may be empty. Engineers use it to record where the cluster moved to and anything else.
- `name`: the cluster name, copied from `clusters.name` on every save, so the row can still be shown after the cluster is removed from Rancher
- `updated_by` (user id), `updated_at` (UTC ISO timestamp)

A cluster with no row counts as `not started` with an empty wave and note. No rows are created ahead of time.

New table `migration_tracking_log`, one row per change: site, cluster_id, the new status, wave and note, plus user id and timestamp. It is append-only. The app never edits or deletes log rows.

Collection runs never read or write these two tables.

### Clusters removed from Rancher (decided by the user)

When Rancher stops listing a cluster, `_remove_unlisted()` in `inventory/collector/runner.py` deletes its rows from `clusters` and the other per-cluster tables. That behaviour stays. In the same transaction, it now also writes a row to a new table `removed_clusters`: site, cluster_id, name and `removed_at` (UTC ISO timestamp). This happens for every removed cluster, whether or not it has tracking data. Tracking rows and log rows are not deleted.

- The progress numbers and charts count current clusters plus removed clusters. A removed cluster uses its tracking status, or `not started` if it has none.
- The overview gets a "Removed from Rancher" table: site, name, removed date, status, wave and note, filtered by the selected site. Status, wave and note can be edited there with the same bulk edit form. Each row links to a small page at `/clusters/{site}/{cluster_id}` that shows the Migration section and history in place of the collected data.
- A removed cluster that has no status of `migrated`, `decommissioned` or `won't migrate` is highlighted in that table, because it left Rancher without being marked as done.
- If a cluster with the same site and cluster_id is listed by Rancher again, its `removed_clusters` row is deleted and it shows as a normal cluster, keeping its tracking.
- A cluster re-imported into Rancher gets a new cluster_id and starts with no tracking. Engineers can use the note to link the two. (My call.)

### Who can edit

Both roles, admin and viewer, can edit tracking, because every user is a platform engineer. Viewers can already save presets, so this follows an existing pattern.

### Editing

- **Cluster page** (`/clusters/{site}/{cluster_id}`): a "Migration" section with status, wave and note, and a Save button. It shows who last changed it and when, and the change history from the log, newest first.
- **Bulk edit** on the cluster table, on the overview and on `/clusters`: a checkbox per row and a "select all shown" checkbox. A small form sets status and/or wave for the ticked clusters. A field left empty in the bulk form is not changed. A "clear wave" checkbox sets wave to empty for the ticked clusters. Bulk edit does not change notes. (Builder check call: clear wave checkbox.) Each changed cluster gets its own log row.

### Display

- The cluster table and cards get Status and Wave columns. Both are sortable and filterable with the existing `tables.js`, and both are included in the clusters CSV.
- `/clusters` gets `status` and `wave` filters, next to the existing site, class, flag and label filters.
- **Overview, progress charts** (shown above the Part 1 charts):
  - One stacked horizontal bar of clusters per status, across all clusters in scope (current plus removed), with counts and percentages.
  - Clusters per status for each site, as one stacked bar per site.
  - Clusters per status for each wave, as one stacked bar per wave, plus a "no wave" bar.
  - Each status segment links to `/clusters?status=<status>` (plus site). Removed clusters are not in `/clusters`; the "Removed from Rancher" table shows them.
- **Progress over time** (my call; you asked for reporting over time, the chart form is mine): a line chart on the overview showing the number of clusters in `migrated` or `decommissioned` per day, built from the log. Counted per cluster by the latest log entry on or before each day.

## Left to the builder

- Exact layout, sizes and spacing of the charts, within the existing `.charts` / `.chart` styles (a wider chart box is fine).
- Whether the vCPU/memory toggle in the Top 10 chart is a Chart.js redraw or two canvases.
- Exact column types and indexes for the three new tables.
- The bulk edit form's markup and where it sits on the table.
- How to show an empty log on the cluster page.
