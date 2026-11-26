# Rancher users page: design

Date: 2026-11-26. Size: small change (short design).

## Intent

A new **Rancher users** tab that lists every Rancher user who is a member of at least one downstream cluster, across all sites, with how many clusters each is on. Clicking a user shows the clusters they can access. It extends the cluster members feature (`2026-11-24-cluster-members-design.md`).

## Goals

- One row per person across sites, matched by **AD principal**. The page states this matching rule plainly.
- Per user: number of clusters and sites, plus the list of clusters with roles and scope.

## Non-goals

- Groups (excluded for now).
- Users who aren't members of any cluster.
- Global-admin (GlobalRole) access. As on the cluster page, a note says global admins can reach every cluster.

## Constraints

- Read-only against Rancher; no new kinds or permissions (the principal already comes from `/v3/users` and the role bindings).
- Schema change goes through a new numbered migration and a `SCHEMA_VERSION` bump.
- Member names stay out of the discovery export (unchanged).

## Approaches

1. **Store the principal on each member row (picked).** Migration `007` adds `principal TEXT` to `cluster_members`; the collector fills it; the page groups at request time. Small and fits the existing pattern.
2. A per-site `rancher_users` table from `/v3/users`. Would also list users with no cluster access, which wasn't asked for. More schema and collection for little gain.
3. Match on username. No schema change, but usernames aren't reliable across Rancher servers, and you asked for the AD principal.

## Design

**Collection.** `Member` gets a `principal: str | None`. For users: the binding's `userPrincipalName` if it isn't `local://`; otherwise the first non-`local://`, non-`system://` entry in the user's `principalIds` (sorted, so the choice is stable). Local-only accounts get `None`. `repo.py` writes the new column. Rows collected before the upgrade have `NULL` until the next run.

**Page `/rancher-users`** (nav tab "Rancher users", visible to admin and viewer):
- Header note: "Users are matched across sites by AD principal. Local accounts, and users not yet re-collected since the upgrade, have no principal and are listed once per site, marked *local / unmatched*. Rancher global admins can also access every cluster."
- Table (sortable with the existing `tables.js`): Name, Username(s), Principal, Clusters, Sites, Disabled (yes / on some sites / no). Default sort: Clusters descending.
- Text filter on name / username / principal; site filter.
- Users are grouped by `principal`. Rows with no principal are grouped by `(site, subject)`.
- Only clusters whose members status is `ok`, or `failed` with an earlier list (stale), are counted, the same rule the Members column uses. Clusters removed from Rancher are excluded.

**Detail `/rancher-users/user?p=<principal>`** (or `?site=&id=` for an unmatched user): name, username(s), principal, then a table of Site, Cluster (linking to the cluster page `#members`), Roles, Scope, and a "stale" marker where the cluster's member read failed.

## My calls (check these)

- URL shape with query parameters, since principals contain `/`, `,` and `=`.
- When a user has several non-local principals, the first one in sorted order is used.
- Display name and username shown are the most common values across the user's rows, with alternatives listed if they differ.
- Not committed: your global instructions say not to commit for you.

## Left to the builder

- Template layout details, query placement (`queries.py` or a new `rancher_users.py` module), tests (unit tests for principal selection and grouping, web tests for both pages).
