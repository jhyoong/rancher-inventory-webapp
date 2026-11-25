# Cluster members

Nov 24, 2026

## Why

Platform engineers planning the VMware to Nutanix move need two things per cluster (user's words):

1. **Who to contact** before a cluster migrates.
2. **Which clusters have many users and which have few**, to compare clusters.

Rancher already records who has access to each downstream cluster. The app collects that list on every run and shows it.

## Who uses it

Platform engineers, both roles (`admin`, `viewer`), read-only.

## Goals

- Each collected cluster has a list of its Rancher members: cluster members and project members, users and groups, with names and roles.
- A sortable Members count in the cluster tables, so busy and quiet clusters stand out.
- Find clusters by member name ("which clusters is alice on?").
- Show "not collected" when the permission is missing, never an empty list that looks like "nobody".

## Non-goals

- Expanding AD group membership. Rancher stores only the group, not its members. Access is mostly given to individual users (user's answer), so groups are counted separately, not expanded.
- Implicit global-role access in the per-cluster list: bindings Rancher creates from GlobalRoles reach every cluster and are not memberships. The cluster page notes that global admins also have access.
- Filtering out global admins who hold **explicit** cluster or project bindings (for example as the cluster's creator). They are kept and counted (decided by the user): removing them could leave a cluster that only admins use looking empty.
- RoleBindings created directly inside downstream clusters, kubeconfig certificate users, service accounts.
- Email addresses or other contact details beyond what Rancher has (display name and username; users are synced from Microsoft AD, so display names are AD names).
- Member history over time. The list reflects the latest run, like other collected data.
- Members in the overview charts or the discovery export (the export must not carry real names).

## Anti-goals

- Reading anything secret. User names come from Rancher's `/v3/users` API, and the collector never fetches raw `management.cattle.io` `User` objects. Never request Secrets. The sites run Rancher 2.13, where password hashes are no longer stored on `User` objects, and the user says security is not a concern for the `users` read permission.
- Any write to Rancher.
- An empty Members cell that looks like "no members" when the data was not collected.

## Constraints

- Read-only against Rancher; new permissions documented in `README.md` the way step 4 (provisioning) is.
- Schema change through `inventory/db/migrations/006_cluster_members.sql`, `SCHEMA_VERSION` bumped to 6. (Custom columns, `2026-11-24-custom-columns-design.md`, takes 005. If this is built first, swap the numbers.)
- Airgap: no new dependencies are expected.

## Approaches

- **A. Read Rancher's member records from the Rancher local cluster, once per site per run (chosen by the user).** Same pattern as `collector/provisioning.py`: list role bindings, projects and role templates through `/k8s/clusters/local`, user names through `/v3/users`, compute members per cluster, store them. Needs a few read-only rules on `inventory-reader-global`.
- B. Read `rolebindings`/`clusterrolebindings` in each downstream cluster via `kinds.yaml`. Reuses per-cluster collection, but subjects are bare Rancher IDs (still needs `/v3/users`), system and service-account bindings must be filtered out, and project membership is hard to rebuild. More work, less accurate.

## Design

### Rancher permissions

Add to the `inventory-reader-global` GlobalRole (README step 3 option A), or to a separate GlobalRole bound to the service user for option B, following README step 4's layout. New README step "Allow reading cluster members (optional)":

```yaml
rules:
  - apiGroups: [management.cattle.io]
    resources: [clusterroletemplatebindings, projectroletemplatebindings, projects, roletemplates, users]
    verbs: [get, list]
```

`users` is needed for `/v3/users` to list other users. The collector itself never fetches `/apis/management.cattle.io/v3/users`. The README says so, and why. The test-the-token step gets a matching `curl` example.

### Collection

In `collector/runner.py`, next to `read_provisioning`, a new `read_members(client)` per site in scope (new module `collector/members.py`):

1. List from the local cluster (via `client.list_kind("local", ...)`):
   - `/apis/management.cattle.io/v3/clusterroletemplatebindings` (fields `clusterName`, `roleTemplateName`, `userName`, `userPrincipalName`, `groupName`, `groupPrincipalName`)
   - `/apis/management.cattle.io/v3/projectroletemplatebindings` (`projectName` as `c-xxxxx:p-yyyyy`, same subject fields)
   - `/apis/management.cattle.io/v3/projects` (`spec.displayName`, cluster from the namespace or `spec.clusterName`)
   - `/apis/management.cattle.io/v3/roletemplates` (`displayName`)
2. List `/v3/users` (paginated like `list_clusters`) for `id`, `username`, `name` (display name), `principalIds`, `enabled`.
3. Build members per cluster:
   - A subject is a **user** (by Rancher user id, or by principal when only `userPrincipalName` is set) or a **group** (by `groupPrincipalName`).
   - User display name: `/v3/users` `name`, falling back to the CN parsed from an `activedirectory_user://CN=...` principal, then the username, then the id. Group display name: the CN from `activedirectory_group://CN=...,OU=...`, falling back to the raw principal after `://`.
   - Each member has one or more grants: role display name (fallback: template name) and scope (`cluster`, or `project: <display name>`). A user who is both a cluster and a project member is one member with several grants.
   - **Left out:** bindings created for global roles (CRTBs owned by a GlobalRoleBinding, which Rancher marks with the `authz.management.cattle.io/grb-owner` label; the builder confirms the marker on the dev Rancher), users whose principals are all `system://`, and the app's own service user (its Rancher user `id` from the `/v3/users?me=true` response; `client.whoami()` returns the username today, so it needs to expose the id too).
   - Disabled users (`enabled: false`) are kept, marked disabled, and not counted.
4. Results: `ok` with members per cluster id, `forbidden` on 401/403 from any of the listed calls, `failed` on other errors, as `ProvisioningResult` does. A malformed item is skipped and logged, not fatal.

Scoped runs (one site, one cluster, a list) still read once per site in scope but write only clusters in scope.

### Storage (migration 006)

- `cluster_members`: `site`, `cluster_id`, `kind` (`user`/`group`), `subject` (Rancher user id or principal id), `display_name`, `username` (NULL for groups), `disabled INTEGER NOT NULL DEFAULT 0`, `grants TEXT NOT NULL` (JSON list of `{role, scope}`), primary key `(site, cluster_id, kind, subject)`.
- `clusters` gets `members_status TEXT` (`ok`, `forbidden`, `failed`, NULL = never collected) and `members_at TEXT`.
- Write per cluster after the read, in its own transaction:
  - `ok`: replace the cluster's rows, set status `ok` and `members_at`.
  - `forbidden`: delete the cluster's rows, set status `forbidden` (shown as "not collected"; the permission is absent, old data would mislead).
  - `failed`: keep the previous rows and `members_at`, set status `failed` and log the error. The UI shows the old list with "last collected <members_at>". *(My call.)*
- Rows are deleted when a cluster is removed (`_remove_unlisted` path).
- A member read failure never fails the cluster's collection or the run, like provisioning.

### Where it shows

- **Overview table (`/`) and `/clusters` table:** a **Members** column, e.g. `12 users, 2 groups`. It sorts by the user count (the cell text starts with the number, which `tables.js` already sorts numerically). `0 users` is a real value. Not collected shows `not collected` (sorts last). Cards view gets the same as a `dt/dd`.
- **Cluster page:** a Members section: name, username, type (user/group), roles, scope. Cluster owners first, then other cluster-scope members, then project-scope members, then by name. Disabled users are marked. A line under it: "Rancher global admins can also access this cluster." States: not collected (with a hint pointing at the README step), failed (old list with its date), ok with no members ("no members besides global admins").
- **`/clusters` filter:** `member=<text>`, case-insensitive substring match on display name or username of users and groups. Combines with other filters.
- **Failed read in the table:** if old rows exist, the cell shows the old counts with a `(stale)` marker and the `members_at` date as a tooltip; it still sorts by the count. If there was never an `ok` read, it shows `not collected`. *(My call.)*
- **CSV of `/clusters`:** `member_users`, `member_groups` (counts, or `not collected`), plus `members_status` (`ok`, `stale`, `not collected`). Names are not in the CSV. *(My call: CSV files travel; names stay in the app.)*

## Left to the builder

- Module layout inside `collector/members.py`, the merge with the existing runner flow, and exact markup.
- Whether role display names are cached across sites (they are per Rancher server, so per site).
- Tests: unit tests for member building from small hand-written binding objects, plus a `live` test against the dev Rancher once the permission is granted there. Rancher API field names are confirmed on the dev Rancher before relying on them.
