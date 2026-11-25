# Cluster Members Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect each downstream cluster's Rancher members (cluster and project role bindings, users and groups) on every run and show them on the cluster page, as a sortable Members count, and as a `member=` filter.

**Architecture:** New `inventory/collector/members.py` reads binding, project, role-template objects from the Rancher local cluster and users from `/v3/users` once per site, builds members per cluster in a pure function, and the runner writes them per cluster into a new `cluster_members` table (migration 006). The web layer reads them in `migration.cluster_rows` and the cluster page.

**Tech Stack:** Python 3.12, httpx (existing `RancherClient`), SQLite, FastAPI/Jinja, pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-cluster-members-design.md`

## Global Constraints

- **No commits by the agent.** The user's global rules say "NEVER commit on behalf of user". Each task ends with a checkpoint; the user commits.
- Build after the custom columns plan: migration `inventory/db/migrations/006_cluster_members.sql`, `SCHEMA_VERSION = 6`, update `tests/test_migrate_upgrade.py` (`== 5` → `== 6`). If custom columns is not merged yet, use 005/5 instead and tell the user.
- Read-only against Rancher. The collector never fetches `/apis/management.cattle.io/v3/users`; user names come only from `/v3/users`. Never request Secrets.
- Left out of members: CRTBs/PRTBs carrying the label `authz.management.cattle.io/grb-owner`, users whose `principalIds` are all `system://...`, and the app's own service user. Global admins with explicit bindings are **kept and counted** (user's decision).
- Disabled users (`enabled: false`) are listed, marked, and not counted.
- Statuses stored in `clusters.members_status`: `ok`, `forbidden`, `failed`, NULL (never collected).
- Members cell text: `N user(s), M group(s)`; `(stale)` appended for `failed` with old rows; `not collected` for NULL/`forbidden`/`failed` with no `members_at`.
- No names in CSV; CSV columns `member_users`, `member_groups`, `members_status` (`ok` | `stale` | `not collected`).
- A member read failure never fails a cluster or a run.

## Review Focus

1. A 403 on only one of the five list calls (e.g. `roletemplates` missing from the GlobalRole) → status `forbidden` for every cluster of that site, rows cleared, "not collected" shown. Test in Task 2.
2. A binding naming a user id that `/v3/users` does not return (deleted user), or a `userPrincipalName` with no `userName` → still listed, display name from the principal CN, else the id. Test in Task 1.
3. A user with both a cluster binding and two project bindings → one member, three grants, counted once. Test in Task 1.
4. A scoped run (one cluster) → only that cluster's members rewritten; other clusters' rows and status untouched. Test in Task 3.
5. A cluster removed from Rancher → its `cluster_members` rows deleted with the rest of its data. Test in Task 3.

---

### Task 1: Member building (pure)

**Files:**
- Create: `inventory/collector/members.py`
- Test: `tests/test_members.py`

**Interfaces:**
- Produces:
  - `@dataclass class Member: kind: str; subject: str; display_name: str; username: str | None; disabled: bool; grants: list[dict]` — `kind` is `"user"`/`"group"`; each grant `{"role": str, "template": str, "scope": str}` with scope `"cluster"` or `"project: <display name>"`.
  - `@dataclass class MembersResult: status: str; clusters: dict[str, list[Member]]; error: str | None = None`
  - `GRB_OWNER_LABEL = "authz.management.cattle.io/grb-owner"`
  - `principal_cn(principal: str) -> str | None` — `"activedirectory_user://CN=Alice Tan,OU=X,DC=y"` → `"Alice Tan"`; no `CN=` → `None`.
  - `build_members(crtbs: list[dict], prtbs: list[dict], projects: list[dict], roletemplates: list[dict], users: list[dict], self_id: str | None) -> dict[str, list[Member]]` — keyed by cluster id; each list sorted: members with a `cluster-owner` template grant first, then other cluster-scope members, then project-only members, then by `display_name` casefolded. Project cluster id comes from `projectName` (`c-xxxxx:p-yyyyy`, the part before `:`). User subject: `userName` if set, else `userPrincipalName`; group subject: `groupPrincipalName`. Display names per the spec's fallback chains. Role display name from the role template's `displayName`, else its name. A malformed item is skipped with a `log.warning`.
  - `counts(members: list[Member]) -> tuple[int, int]` — (enabled users, groups).

- [ ] **Step 1: Write failing tests** with small hand-written objects shaped like Rancher's (`metadata.name`, `metadata.namespace`, `metadata.labels`, top-level `clusterName`, `roleTemplateName`, `userName`, `userPrincipalName`, `groupPrincipalName`, `projectName`; projects with `metadata.name`, `metadata.namespace`, `spec.displayName`; roletemplates with `metadata.name`, `displayName`; users as `/v3/users` returns them: `id`, `username`, `name`, `principalIds`, `enabled`):

```python
def test_cluster_and_project_grants_merge_into_one_member(): ...   # alice: cluster-owner on c-1 + project-member on two projects -> one Member, 3 grants, first in list
def test_group_member_named_from_cn(): ...                          # groupPrincipalName "activedirectory_group://CN=Ops-Team,OU=G,DC=x" -> display "Ops-Team", username None
def test_unknown_user_falls_back_to_principal_cn_then_id(): ...
def test_excluded_subjects(): ...                                   # grb-owner labelled CRTB, system:// user, self_id user -> absent
def test_disabled_user_listed_not_counted(): ...                    # counts == (1, 1) with one enabled user, one disabled, one group
def test_order_owner_then_cluster_then_project_then_name(): ...
def test_malformed_item_skipped(): ...                              # a CRTB that is a string / missing clusterName -> skipped, others kept
```

- [ ] **Step 2: Run** `uv run pytest tests/test_members.py -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 2: Reading from Rancher

**Files:**
- Modify: `inventory/collector/members.py`, `inventory/rancher/client.py`
- Test: `tests/test_members.py`, `tests/test_rancher_client.py`

**Interfaces:**
- Consumes: Task 1 `build_members`, `MembersResult`.
- Produces:
  - `RancherClient.me() -> dict` returning the first item of `/v3/users?me=true` (raise `RancherError` on an unexpected shape, as `whoami` does); `whoami()` becomes `return (await self.me())["username"]`.
  - `RancherClient.list_users() -> list[dict]` — `/v3/users`, following `pagination.next` with the same host/loop checks as `list_clusters` (factor the pagination loop into a private `_list_v3(path, page_size)` used by both).
  - Paths in `members.py`: `CRTB_PATH = "/apis/management.cattle.io/v3/clusterroletemplatebindings"`, `PRTB_PATH = ".../projectroletemplatebindings"`, `PROJECT_PATH = ".../projects"`, `ROLETEMPLATE_PATH = ".../roletemplates"`.
  - `async def read_members(client: RancherClient) -> MembersResult` — the four `client.list_kind("local", ...)` calls, `client.list_users()`, `client.me()["id"]`; any `RancherError` with status 401/403 → `MembersResult("forbidden", {}, str(e))`, any other `RancherError` → `"failed"`; else `"ok"` with `build_members(...)`.

- [ ] **Step 1: Write failing tests** using a fake client object (a small class with async `list_kind`, `list_users`, `me`):

```python
def test_read_members_ok(): ...                 # asyncio.run(read_members(fake)) -> status "ok", clusters["c-1"] non-empty
def test_one_forbidden_call_marks_site_forbidden(): ...   # list_kind raises RancherError(..., 403, url) for ROLETEMPLATE_PATH only -> "forbidden", {}
def test_other_error_is_failed(): ...           # RancherError status 500 -> "failed"
```

In `tests/test_rancher_client.py`, add a pagination test for `list_users` mirroring the existing `list_clusters` tests (same mocking approach that file already uses).

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** both files + full offline suite — PASS. **Step 5: Checkpoint.**

---

### Task 3: Storage and runner integration

**Files:**
- Create: `inventory/db/migrations/006_cluster_members.sql`
- Modify: `inventory/db/migrate.py`, `tests/test_migrate_upgrade.py`, `inventory/db/repo.py`, `inventory/collector/runner.py`
- Test: `tests/test_repo_members.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: Task 2 `read_members`, Task 1 `MembersResult`, `Member`.
- Produces:
  - Migration: `cluster_members` table as in the spec (`grants` JSON text) plus `ord INTEGER NOT NULL` (position in the `build_members` list, written by `write_members`); `ALTER TABLE clusters ADD COLUMN members_status TEXT; ALTER TABLE clusters ADD COLUMN members_at TEXT;`
  - `repo.write_members(conn, site: str, cluster_id: str, result: MembersResult, now: str) -> None` — one transaction; does nothing if the cluster has no `clusters` row; `ok` → replace rows, `members_status='ok'`, `members_at=now`; `forbidden` → delete rows, `members_status='forbidden'`; `failed` → keep rows and `members_at`, `members_status='failed'`.
  - Runner: in `_execute`, `members = {name: await read_members(c) for name, (c, _) in clients.items()}` next to `provisioning`; after `statuses = await asyncio.gather(...)`, call `write_members(conn, site.name, cluster["id"], members[site.name], _now())` for each `(site, cluster)` in `work`, each wrapped so an exception is logged and swallowed; log `failed`/`forbidden` once per site at warning level with the scrubbed error. `_remove_unlisted` adds `"cluster_members"` to its delete list.

- [ ] **Step 1: Write failing tests** in `tests/test_repo_members.py` (use `db`; seed `clusters` rows):

```python
def test_ok_replaces_rows_and_sets_status(db): ...
def test_forbidden_clears_rows(db): ...
def test_failed_keeps_rows_and_date(db): ...      # members_at unchanged, status "failed"
def test_no_cluster_row_writes_nothing(db): ...
def test_remove_unlisted_deletes_members(db): ... # runner._remove_unlisted(db, "dev", set())
```

In `tests/test_runner.py`, add an offline test following `test_unexpected_cluster_error_is_contained`'s monkeypatch style: patch `runner.read_members` to raise, run a scoped run with `collect_cluster` patched to a fast stub, assert the run status is unaffected. Add a scoped-run test: two clusters with existing member rows, scope one cluster, patched `read_members` returns new members for both → only the scoped cluster's rows change.

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** new tests, `tests/test_db.py`, `tests/test_migrate_upgrade.py`, full offline suite — PASS. **Step 5: Checkpoint.**

---

### Task 4: Web — tables, filter, cluster page, CSV

**Files:**
- Modify: `inventory/web/migration.py` (`cluster_rows`), `inventory/web/queries.py`, `inventory/web/routes/clusters.py`, `inventory/web/templates/_cluster_list.html`, `inventory/web/templates/clusters.html`, `inventory/web/templates/cluster.html`
- Test: `tests/test_web_members.py`

**Interfaces:**
- Consumes: Task 3 tables and columns.
- Produces:
  - `queries.cluster_members(conn, site: str, cluster_id: str) -> list[dict]` — rows `ORDER BY ord`, `grants` decoded.
  - `queries.member_counts(conn, site: str | None) -> dict[tuple[str, str], tuple[int, int]]` (enabled users, groups).
  - `cluster_rows(..., member: str | None = None)` — row keys `members_status`, `members_at`, `member_users`, `member_groups` (both `None` when not collected); `member` filter keeps clusters with a member whose `display_name` or `username` contains the text, case-insensitive.
  - `members_cell(row: dict) -> str` Jinja-usable helper (put it in `migration.py` and register as a template global or filter where `deps.templates` is set up): `"1 user, 0 groups"`, `"12 users, 2 groups"`, `+ " (stale)"` for `failed` with `members_at`, else `"not collected"`.
  - Table/cards column **Members** after the custom columns (or after Wave if custom columns is not present), with `title="last collected <members_at>"` on stale cells.
  - `/clusters` filter input `member` (label "Member", placeholder `name or username`), added to the route's `f` dict.
  - Cluster page section `<h2 id="members">Members</h2>` after Summary: table Name | Username | Type | Roles | Scope (roles and scopes from grants, one line each), `disabled` marker, the line "Rancher global admins can also access this cluster.", and the state messages from the spec (not collected with a pointer to the README step "Allow reading cluster members"; stale with date; ok and empty → "No members besides global admins.").
  - CSV: `member_users`, `member_groups`, `members_status` keys per the Global Constraints.

- [ ] **Step 1: Write failing tests** (seed `clusters`, `cluster_members`, statuses directly):

```python
def test_members_column_and_sort_text(admin_client, ...):   # "12 users, 2 groups" style text present; not-collected cluster shows "not collected"
def test_stale_cell(admin_client, ...):                      # failed + members_at -> "(stale)"; failed + no members_at -> "not collected"
def test_member_filter_matches_name_or_username(admin_client, ...):  # ?member=ALICE and ?member=atan both -> c1 only
def test_cluster_page_lists_members_owner_first(admin_client, ...):
def test_csv_counts_no_names(admin_client, ...):             # member_users/member_groups/members_status present; no display names in the CSV text
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** file, `tests/test_web_migration.py`, full offline suite — PASS. **Step 5: Checkpoint.**

---

### Task 5: README, AGENTS.md and live check

**Files:**
- Modify: `README.md` (new "Step 5. Allow reading cluster members (optional)" after step 4, renumbering token/test steps; contents per the spec's "Rancher permissions" section, including why `users` is needed and that the app reads only `/v3/users`; a `curl` example in the test step: `curl -sk -H "Authorization: Bearer $TOKEN" https://<rancher>/k8s/clusters/local/apis/management.cattle.io/v3/clusterroletemplatebindings?limit=1`), `AGENTS.md` (collector pipeline: one line on `members.py`)
- Test: `tests/test_members.py` (live test)

- [ ] **Step 1: Add a live test** `@pytest.mark.live def test_read_members_dev(live_loop, live_client)` asserting `status == "ok"` and that the keys of `result.clusters` are a subset of the `dev_clusters` ids. Before running, grant the README rule on the dev Rancher's `inventory-reader-global` GlobalRole (ask the user; it is their Rancher).
- [ ] **Step 2: Run** `uv run pytest tests/test_members.py -m live -v` — expected PASS. While there, confirm on the dev Rancher that inherited global-role CRTBs carry `authz.management.cattle.io/grb-owner`; if the label differs, update `GRB_OWNER_LABEL` and the spec, and tell the user.
- [ ] **Step 3: Edit** README and AGENTS.md. **Step 4: Run** full offline suite — PASS. **Step 5: Checkpoint** — final list of changed files for the user to commit.
