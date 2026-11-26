# Table filter count, column filter, cards removal (design)

Date: 2026-11-26. Size: small change.

## Intent

Make filtered tables easier to read: see how many rows match without counting by hand, filter
on one column, drop the unused cluster Cards view, and let the Overview "special network config"
tile open the list of those clusters.

## Goals

1. **Row count** under every table that has the "Filter rows" box, and under the cluster result
   table on Overview/Clusters (server-filtered) even when it has 10 rows or fewer. Text: `240 rows`
   when the box is empty, `12 of 240 rows shown` while filtering. Detail rows (single colspan rows)
   are not counted. An empty-result placeholder row ("No clusters match.") counts as 0.
2. **Column dropdown** beside every filter box: "All columns" plus each header with text (the
   checkbox column and blank headers are skipped). Choosing a column restricts the match to that
   column's cell text. Changing the dropdown re-applies the filter.
3. **Remove the cluster Cards view** (Table | Cards switch, `view` query parameter, card markup,
   `.cluster-cards` CSS, card tests) on Overview and Clusters. `?view=cards` is ignored and the
   table shows. The Overview summary tiles stay.
4. **Special network tile** on the Overview links to `/clusters?flag=special-network-config`,
   plus `&site=<site>` when a site is selected, matching the flag chart's links.

## Non-goals

- No server-side changes to filtering or caps; the existing "Showing the first N rows" warnings stay.
- Other summary tiles are not linked.

## Approaches

- **Chosen: generic, in `static/tables.js`.** One place adds the count and dropdown to every
  filtered table; server-filtered cluster tables opt in to the count with a `data-count` attribute.
- Per-template counts in Jinja: rejected, it would not follow the client-side filter and needs
  edits in many templates.

## Constraints

- Airgap: no new assets or dependencies; plain JS as in the existing `tables.js`.
- Keep bulk-edit behaviour: hidden rows lose their checkbox tick; "Select all shown" respects the filter.

## Left to the builder

Exact markup/CSS of the count line and dropdown; test layout.
