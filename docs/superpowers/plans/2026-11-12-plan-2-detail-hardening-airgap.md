# Plan 2: Detail, Hardening and Airgap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build phases 6 to 10 of the spec on top of plan 1, test every feature in Docker against the dev Rancher, then prove the image works in an airgap as the last step.

**Architecture:** Same single process as plan 1. New kinds go into `config/defaults/kinds.yaml`; derived fields are computed in `transform.py` helpers; new tables come from migration `002`. New pages follow the plan 1 route and template pattern.

**Tech Stack:** Same as plan 1. No new runtime dependencies.

**Spec:** `Rancher Inventory Tool Implementation Plan (single image, airgap).md` (repo root), sections 6.9 to 6.14, 7 (discovery pages and shareable export), 8 and 9 phases 6 to 10. Builds on `docs/superpowers/plans/2026-11-12-plan-1-working-app-on-dev.md`; all its Global Constraints apply.

## Global Constraints

- Everything in plan 1's Global Constraints.
- Environment variables, commands, args, ConfigMap values, Helm values, hostPath paths and NFS servers are never stored.
- Jobs owned by a CronJob are skipped.
- Pod usage is stored per namespace only, not per pod.
- Opt-in collectors are off by default and make zero calls when off.
- Export hash: `<type>-<first 10 hex of SHA-256(salt|type|value)>`, same as `k8s-discovery.sh` 1.0.0.
- The export never contains IPs, hostnames, ingress hosts, URLs, label or annotation values, NFS paths or private registry hosts. The self-check blocks the download on any hit.
- `export_name_map` is never inside the bundle.
- The airgap image is built for `linux/amd64` unless the airgap VM is found to be arm64.

## State after plan 1 (merged to main 2026-11-12)

- 189 tests pass; live tests read the dev Rancher (`rancher.local` must be in `/etc/hosts`; on the Mac each new connection costs about 5 s for the `.local` lookup, so reuse one client per test session).
- Real module names to build on: `inventory/rancher/client.py` (`RancherClient`, `RancherError`), `inventory/collector/{transform,cluster,runner,thread,classify,rules,annotations,addresses,units,kinds}.py`, `inventory/db/{connection,migrate,repo}.py`, `inventory/web/{app,deps,auth,queries}.py`, routes in `inventory/web/routes/` (one module per page group), templates in `inventory/web/templates/`.
- Test fixtures in `tests/conftest.py`: `settings`, `dev_site`, `db`, `db_path`, `dev_live`, `dev_clusters`, `local_cluster`, `live_loop`, `live_client`, `kinds`, `dev_lists`, `collected_db` (session tmp DB after one real run), `client`, `admin_client`, `viewer_client`, `admin_client_collected`.
- `transform.trim` stores keep paths nested in `data`; derived fields use the `_DERIVE` table in `transform.py`. New derive helpers in this plan register there.
- `cluster_profile` counts for StatefulSets, PVCs and Helm releases cover user namespaces only.
- Clusters missing from a full-scope run's cluster list are deleted (daily metrics history kept).
- Dev `local` is classed `stateful-helm-crd`. Still counted as custom CRD groups: `operator.tigera.io`, `kagent.dev`, `cluster.x-k8s.io`, `addons.cluster.x-k8s.io`, `ipam.cluster.x-k8s.io`, `runtime.cluster.x-k8s.io`.
- Known spec deviation kept from plan 1: data is committed per cluster and kind, not per cluster, so during a run (or after an interrupted one) a cluster can show new data for some kinds and old data for others. The stale-row rule is unaffected. Document this in the README in Task 12; do not change it.
- Tests that need a "failed" or "removed" state copy real collected rows in a tmp DB and change one status row; this is accepted under the no-synthetic-data rule. Literal hand-built Kubernetes objects are not.

## Prerequisites (before Task 1)

These come from the user. Tasks that need them say so.

1. **`k8s-discovery.sh` 1.0.0** in `reference/k8s-discovery.sh`. Done (committed). Tasks 4, 7 and 8 copy its CSV file names, column names and hashing rules from it.
2. **An admin kubeconfig for the dev `local` cluster** (download from Rancher), used only on the dev Mac to run the script with `--clear-names` and to run `helm list -A` for comparison. Not used by the app.
3. **Airgap test host details:** CPU architecture, Docker or Podman, how files are carried across.

## Review Focus

1. **Migration from a plan 1 database.** A `dev/data/inventory.db` created by plan 1 must upgrade to the new schema on start and keep users, presets and metrics. Test in Task 1.
2. **Kinds that are not installed.** `volumesnapshots` (or any CRD-backed kind) returning 404 is recorded `not_found` and shown as "not installed", not as an error. Test in Task 2.
3. **Export leaking real names.** A cluster name, node name, internal IP or a string from `EXPORT_SENSITIVE_STRINGS` in any CSV blocks the download. Test in Task 9.
4. **Opt-in switched off.** With every opt-in disabled, no request goes to their endpoints. Test in Task 10.
5. **Rancher-generated RBAC counted as custom.** The `rancher_generated` filter must exclude the roles Rancher creates on `local`, or the "custom roles" count is meaningless. Test in Task 5.
6. **A truncated cluster list deleting real clusters.** Since plan 1, a full run deletes clusters not in the site's list. `list_clusters` must read every page, or removal must be skipped. Test in Task 0A.

## File Structure

```
config/defaults/kinds.yaml                  + new kinds
config/defaults/rules.yaml                  + rancher_generated, complexity, optional_collectors, export
inventory/db/migrations/002_discovery.sql   new columns and tables
inventory/collector/podspec.py              derive_podspec, derive_pod
inventory/collector/storage.py              derive_pv, derive_pvc, derive_storageclass
inventory/collector/netpolicy.py            derive_service, derive_ingress, derive_pdb, derive_hpa, derive_netpol, ...
inventory/collector/platform.py             derive_webhook, derive_apiservice, derive_rbac, api_versions
inventory/collector/rollup.py               build_namespace_rollup, complexity_flags
inventory/collector/optional/*.py           one module per opt-in collector
inventory/export/discovery.py               build_export, self_check
inventory/export/hashing.py                 hash_name
inventory/export/csv.py                     table_to_csv response helper
inventory/web/routes/{workloads,storage,namespaces,platform,export,api}.py
inventory/cli.py                            + backup, export-discovery
reference/k8s-discovery.sh                  from the user
deploy/inventory-reader-extended.yaml       second RoleTemplate
```

---

## Carry-over from plan 1

Items the plan 1 reviews deferred. Do Tasks 0A to 0C first; they touch code the later tasks build on. Items that belong to a later task are listed there instead (Tasks 7 and 12).

### Task 0A: Collector robustness

**Files:**
- Modify: `inventory/rancher/client.py`, `inventory/collector/runner.py`, `inventory/collector/thread.py`, `inventory/collector/units.py`, `inventory/collector/rules.py`, `inventory/collector/transform.py`, `inventory/db/connection.py`, `inventory/db/migrate.py`
- Test: `tests/test_rancher_client.py`, `tests/test_runner.py`, `tests/test_units.py`, `tests/test_annotations.py`, `tests/test_db.py`

**Interfaces:**
- `RancherClient.list_clusters()` follows `pagination.next` until absent and returns every cluster. It raises `RancherError` if a page fails, so removal never runs on a partial list.
- `list_kind` raises `RancherError` when a `continue` token repeats (loop guard).
- `whoami()` and `list_clusters()` raise `RancherError` (not `KeyError`) on an unexpected body.
- `parse_cpu` accepts `k`, `M`, `G` suffixes. `repo.transaction` rolls back if `COMMIT` fails.
- `load_rules`: a key missing from an override `rules.yaml` falls back to the built-in default for that key (so an override without `drop` still drops `last-applied-configuration`). `yaml.YAMLError` and `OSError` become `ConfigError` naming the file. `max_value_chars` must be at least 1; empty rule entries are rejected.
- `_derive_crd` and `_derive_helmrelease` drop the field (store nothing) when `spec.versions` or `spec.resources` is not a list.
- `migrate` raises `ConfigError("database schema N is newer than this image (M)")` when `user_version > SCHEMA_VERSION`. `SCHEMA_VERSION` equals the highest migration file number (test).
- `connect(readonly=True)` builds the URI with `Path.as_uri()` so paths with `?`, `#`, `%` work. `run_readonly_query` raises `ValueError` when given a writer connection (check `PRAGMA query_only` or the connection flag).
- `CollectorThread`: if a scheduled start raises, retry every 60 s until the start succeeds or the next day's start arrives (today's run is not skipped). `request_run` after `stop()` never leaves a `running` row (create the row only after the loop-alive check, under the same lock).

- [ ] **Step 1: Write the failing tests**
  - `test_list_clusters_reads_all_pages` (live): `list_clusters` with page limit 1 (pass `limit=1` through a `page_size` parameter) returns the same ids as the default call.
  - `test_removal_skipped_when_listing_fails` (live, copied real rows as in plan 1): a site whose token is invalid keeps its stored clusters after a full run.
  - `test_bad_continue_token_is_error` (live): `get_json("/k8s/clusters/local/api/v1/pods", params={"limit": 1, "continue": "bogus"})` raises `RancherError` with a 4xx status. Together with the existing rule (any page error leaves the kind un-deleted), this covers Review Focus 1 of plan 1 without a test hook in app code.
  - `test_override_rules_fall_back_per_key`, `test_bad_yaml_is_config_error`, `test_schema_newer_than_image`, `test_schema_version_matches_files`, `test_readonly_query_rejects_writer`, `test_parse_cpu_k_suffix`.
- [ ] **Step 2: Run, see them fail, implement, run, see them pass**
- [ ] **Step 3: Commit** — `git commit -m "fix: collector robustness carry-over from plan 1"`

### Task 0B: Web and auth fixes

**Files:**
- Modify: `inventory/web/auth.py`, `inventory/web/deps.py`, `inventory/web/queries.py`, `inventory/web/routes/{auth,admin,runs,clusters,inventory,presets,check}.py`, templates `base.html`, `cluster.html`, `clusters.html`, `run.html`, `inventory.html`, `network.html`, new `error.html`; `inventory/cli.py`
- Test: `tests/test_auth.py`, `tests/test_web_core.py`, `tests/test_web_inventory.py`

**Interfaces:**
- Lockout is atomic: the failure insert and the count happen in one `BEGIN IMMEDIATE` transaction, re-checked after the scrypt verify. `login_failures` rows older than 15 minutes are deleted on every login attempt.
- Usernames are stripped and lower-cased on create and on login.
- Log lines with a user-supplied username use `%r`.
- An admin who resets their own password on `/admin/users` gets a fresh session (stays logged in) with a notice.
- `GET /` and the clusters page gain a "Refresh this site" button (admin) that posts `site`.
- `POST /runs/{id}/cancel` on a finished or unknown run shows a message on the runs page instead of a bare redirect or 404.
- `serve` and `healthcheck` exit 1 with a plain message when only one of `TLS_CERT_FILE` / `TLS_KEY_FILE` is set.
- HTML error pages (`error.html`) for 400, 403, 404, 409, 500 and 503 on normal page requests; JSON stays for `/healthz` and `/api/*`.
- The failed marker on `/inventory` and the cluster page uses the newest `run_clusters` row that is `ok` or `failed` (a `running` row no longer hides an earlier failure).
- Daemonsets show `status.numberReady` / `status.desiredNumberScheduled`; add both to the daemonset keep list and stop keeping `spec.replicas` / `status.readyReplicas` for daemonsets.
- `_dt` returns the raw string when it cannot parse a time. Malformed `clusters.labels` JSON is treated as `{}` in the label filter (use `json_valid`).
- `annotation_objects` shows a note when it hits its 5,000-row cap.
- Response header `X-Frame-Options: DENY` on every page.

- [ ] **Step 1: Write the failing tests** — one per bullet, using `admin_client`, `viewer_client` and copies of `collected_db`.
- [ ] **Step 2: Run, see them fail, implement, run, see them pass**
- [ ] **Step 3: Commit** — `git commit -m "fix: web and auth carry-over from plan 1"`

### Task 0C: Test gaps

**Files:**
- Test only: `tests/test_settings.py`, `tests/test_db.py`, `tests/test_runner.py`, `tests/test_auth.py`, `tests/test_checkconfig.py`, `tests/test_classify.py`, `tests/conftest.py`

Add these tests (no app code changes unless a test finds a bug):
- `Site.url` trailing slash is stripped; `mask_token` on a token without `:` returns `****`.
- `conftest` loads `.env` from the repo root by absolute path, so tests run from any directory.
- Pragmas `foreign_keys`, `busy_timeout`, `synchronous` are set; a failing migration rolls back and leaves `user_version` unchanged.
- `_final_status` for every combination: all ok, mixed, all failed, cancelled, stopped, site errors.
- Cancel during a run (between clusters, not only before the first).
- The scheduler fires: `CollectorThread` with `COLLECT_DAILY_AT` one minute ahead starts a run (live, marked `slow`).
- `namespace_profile` has exactly one row per namespace seen on `local`.
- `check-config` exit 1 path for a settings error.
- Test connections are closed (use fixtures that close them).
- `test_cancelled_task_ends_interrupted` no longer depends on a 0.5 s sleep: cancel after the run's first `run_clusters` row is `running` (poll the DB).

- [ ] **Step 1: Write the tests and run them**
- [ ] **Step 2: Fix any bug they find, in the module that owns it**
- [ ] **Step 3: Commit** — `git commit -m "test: close plan 1 test gaps"`

---

### Task 1: Migration 002 and upgrade test

**Files:**
- Create: `inventory/db/migrations/002_discovery.sql`, `tests/test_migrate_upgrade.py`
- Modify: `inventory/db/migrate.py` (`SCHEMA_VERSION = 2`)

**Interfaces:**
- Produces in `002_discovery.sql`:
  - `namespace_profile` new columns for the spec 6.9 roll-up: `pods`, `pods_running`, `pods_standalone`, `restarts`, `cpu_req_m`, `cpu_lim_m`, `cpu_used_m`, `mem_req_b`, `mem_lim_b`, `mem_used_b`, `gpu`, `deployments`, `daemonsets`, `jobs`, `cronjobs`, `pvc_count`, `pvc_rwx`, `nodeport_services`, `netpols`, `has_quota`, `has_limitrange`.
  - `cluster_profile` new columns: `cluster_uid TEXT`, `complexity_flags TEXT` (JSON list), `summary TEXT` (JSON of the script's summary columns).
  - `CREATE TABLE api_versions (site, cluster_id, group_version TEXT, source TEXT, -- served | in_use`, `kind TEXT`, `PRIMARY KEY (site, cluster_id, group_version, source, kind))`.
  - `CREATE TABLE export_name_map (type TEXT, value TEXT, hashed TEXT, salt_id TEXT, PRIMARY KEY (type, value, salt_id))` where `salt_id` is the first 8 hex of SHA-256 of the salt.
  - `CREATE TABLE optional_results (site, cluster_id, collector TEXT, key TEXT, value TEXT, run_id INTEGER, PRIMARY KEY (site, cluster_id, collector, key))`.

  - `def apply_migration(conn, version: int) -> None` in `migrate.py` — applies one numbered migration (used by the upgrade test).

- [ ] **Step 1: Write the failing test**

The test builds a version 1 DB at run time rather than committing real dev data: apply only `001_initial.sql`, run one live `execute_run`, create a user and a preset, then call `migrate`.

```python
@pytest.mark.live
def test_upgrade_v1_to_v2_keeps_data(tmp_path, settings):
    conn = connect(tmp_path / "i.db"); apply_migration(conn, 1)   # helper exposed by migrate.py
    # live run, create_user("keep", ...), insert preset
    assert migrate(conn) == 2
    assert [u.username for u in list_users(conn)] == ["keep"]
    assert conn.execute("SELECT count(*) FROM presets").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0] >= 1
```

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Commit** — `git commit -m "feat: schema v2 for discovery fields"`

---

### Task 2: Workload and pod detail (spec 6.9)

**Files:**
- Create: `inventory/collector/podspec.py`, `tests/test_podspec.py`
- Modify: `config/defaults/kinds.yaml` (add `jobs`, `cronjobs`, `replicationcontrollers` with `podspec: true`; mark deployments, statefulsets, daemonsets `podspec: true`), `inventory/collector/kinds.py` (`podspec: bool = False`), `inventory/collector/transform.py`

**Interfaces:**
- Produces:
  - `def derive_podspec(pod_spec: dict) -> dict` with keys: `containers`, `init_containers`, `cpu_req_m`, `cpu_lim_m`, `mem_req_b`, `mem_lim_b`, `gpu` (sum of `nvidia.com/gpu` and `amd.com/gpu` limits), `no_requests`, `no_limits`, `no_probes` (container counts), `privileged`, `host_network`, `host_pid` (bool), `host_ports` (count), `added_caps` (sorted list), `pvc_claims` (list), `hostpath`, `nfs`, `emptydir` (counts), `csi_drivers` (list), `node_selector_keys`, `affinity`, `topology_spread` (bool), `toleration_keys`, `priority_class`, `service_account` (None when `default`).
  - Owner fields for workloads: `owner_api_version`, `owner_kind` from the first `ownerReference`; `update_strategy`; cronjobs add `schedule`, `suspended`.
  - `def derive_pod(pod: dict) -> dict` — `qos`, `owner_kind`, `restarts`, `cpu_req_m`, `mem_req_b`, `cpu_lim_m`, `mem_lim_b`, `pvc_claims`.
  - Collector skips a Job whose owner kind is `CronJob`.
  - Pod usage: list `metrics.k8s.io/v1beta1/pods` once per cluster, sum per namespace into `namespace_profile.cpu_used_m`/`mem_used_b`; nothing per pod.
- `transform.trim` must not copy `env`, `envFrom`, `command`, `args`, `hostPath.path`, `nfs.server`, `nfs.path`.

- [ ] **Step 1: Write the failing tests** (live `dev_lists`)

```python
def test_no_env_or_commands_stored(collected_db):
    for (data,) in collected_db.execute("SELECT data FROM resources WHERE kind IN ('deployments','statefulsets','daemonsets','jobs','cronjobs','pods')"):
        assert not re.search(r'"(env|envFrom|command|args)"', data)

def test_ingress_nginx_daemonset_host_ports(dev_lists):  # rke2-ingress-nginx daemonset: derive_podspec(...)["host_ports"] >= 2 (80, 443)
def test_cronjob_jobs_skipped(collected_db, dev_lists):  # no stored job has owner_kind CronJob
def test_pod_usage_per_namespace(collected_db):  # namespace_profile kube-system cpu_used_m > 0; no pod row has a "usage" key
def test_resource_sums(dev_lists):  # for one deployment, cpu_req_m equals the manual sum of its containers' requests via parse_cpu
```

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Commit** — `git commit -m "feat: workload and pod detail fields"`

---

### Task 3: Storage, policy and network detail (spec 6.10)

**Files:**
- Create: `inventory/collector/storage.py`, `inventory/collector/netpolicy.py`, `tests/test_detail_kinds.py`
- Modify: `config/defaults/kinds.yaml` (add `csidrivers`, `volumesnapshots`, `ingressclasses`, `priorityclasses`, `poddisruptionbudgets`, `horizontalpodautoscalers`, `resourcequotas`, `limitranges`, `networkpolicies`, `serviceaccounts`, `roles` count-only), `inventory/collector/cluster.py` (cluster UID)

**Interfaces:**
- Produces one `derive_<kind>(obj: dict) -> dict` per row of the spec 6.10 table, with the listed fields only. Notable rules:
  - `derive_pv`: `source` is `csi:<driver>`, `nfs`, `hostPath`, `local`, `vsphereVolume`, `iscsi` or `other`; `pinned` true when `spec.nodeAffinity` is set.
  - `derive_pvc` takes `(obj, mounted_claims: set[tuple[str, str]], used_by: dict)`; `mounted` and `used_by` come from the pod PVC claims collected in the same cluster.
  - `derive_storageclass`: `parameter_keys` only, never values.
  - `derive_ingress`: `nginx_categories` (annotation key groups after `nginx.ingress.kubernetes.io/`), `has_snippet` (any key ending in `-snippet`).
  - `derive_pdb`: `blocks_drain` true when `status.disruptionsAllowed == 0` and `status.expectedPods > 0`.
  - `derive_node_provider(provider_id: str) -> str` — scheme before `://`, or `none`.
  - `cluster_uid` = UID of namespace `kube-system`, stored in `cluster_profile.cluster_uid`.

- [ ] **Step 1: Write the failing tests**

```python
def test_volumesnapshots_ok_or_not_installed(collected_db):
    s = collected_db.execute("SELECT status FROM run_kinds WHERE kind='volumesnapshots' AND cluster_id='local'").fetchone()[0]
    assert s in ("ok", "not_found")      # rke2-snapshot-controller is installed on dev, so expect "ok"

def test_cluster_uid_is_kube_system_uid(collected_db, dev_lists): ...
def test_storageclass_no_values(collected_db):  # no stored storageclass data has a "parameters" key
def test_every_new_kind_listed(collected_db):  # run_kinds has status 'ok' for every new kind on local
def test_ingress_snippet_flag(dev_lists):  # derive_ingress on each dev ingress -> has_snippet is bool
```

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Commit** — `git commit -m "feat: storage, policy and network detail"`

---

### Task 4: Namespace roll-up and the workloads, storage, namespaces pages (phase 6 milestone)

Needs prerequisite 1 and 2.

**Files:**
- Create: `inventory/collector/rollup.py`, `inventory/web/routes/{workloads,storage,namespaces}.py`, templates `workloads.html`, `storage.html`, `namespaces.html`, `tests/test_rollup.py`

**Interfaces:**
- Produces:
  - `def build_namespace_rollup(conn, site, cluster_id) -> None` — fills the Task 1 `namespace_profile` columns; called from `build_profiles`.
  - Page filters: `/workloads` — `privileged`, `host_network`, `hostpath`, `no_requests`, `no_limits`, `no_probes`, `operator_owned`, `gpu`. `/storage` — `rwx`, `block`, `unmounted`, `source`, `storage_class`. `/namespaces` — sortable by `cpu_req_m`, `cpu_used_m`, `mem_req_b`, `mem_used_b`, `restarts`.

- [ ] **Step 1: Run the script on dev for the reference output**

Run: `KUBECONFIG=<admin kubeconfig> bash reference/k8s-discovery.sh --clear-names --out /tmp/k8sd-local` (exact flags from the script's help).

- [ ] **Step 2: Write the failing test**

```python
@pytest.mark.live
def test_namespace_totals_match_script(collected_db):
    ref = load_csv("/tmp/k8sd-local/namespaces.csv")   # column names from the script
    for row in ref:
        ours = collected_db.execute("SELECT * FROM namespace_profile WHERE namespace=?", [row["namespace"]]).fetchone()
        for col, ref_col in NAMESPACE_COLUMN_MAP.items():   # map filled from the script's header
            assert ours[col] == int(row[ref_col]), (row["namespace"], col)
```

The test skips with a clear message if `/tmp/k8sd-local` is missing.

- [ ] **Step 3: Run, fail, implement, run, pass.** Small differences in usage numbers (measured at different moments) are allowed only for `*_used_*` columns, within 20%.
- [ ] **Step 4: Page tests** — each page returns 200 for admin, contains `local`, and each filter returns a subset of the unfiltered rows.
- [ ] **Step 5: Commit** — `git commit -m "feat: namespace roll-up and workload, storage, namespace pages"`

The spec asks for 2 clusters; dev has one. Record the second comparison as open until a downstream cluster exists.

---

### Task 5: Platform kinds and the Rancher-generated filter (spec 6.11)

**Files:**
- Create: `inventory/collector/platform.py`, `tests/test_platform.py`
- Modify: `config/defaults/kinds.yaml` (webhooks, apiservices, clusterroles, clusterrolebindings, rolebindings, roles count), `config/defaults/rules.yaml` (`rancher_generated`)

**Interfaces:**
- Produces:
  - `derive_webhook(obj) -> dict` — per spec 6.11: `failure_policies`, `service_namespaces`, `url_based`, `rules`, `intercepts_all` (a rule with `*` in groups and resources), `timeouts`, `has_namespace_selector`, `helm_release`.
  - `derive_apiservice(obj) -> dict | None` — None for non-aggregated (`spec.service` is null); else `service_namespace`, `available`.
  - `derive_clusterrole(obj) -> dict` — `rule_count`, `aggregated`, `helm_managed`.
  - `derive_binding(obj) -> dict` — `role`, `subjects` as `[kind, name]` pairs.
  - `is_rancher_generated(obj: dict, rules: Rules) -> bool` — matches label keys and name patterns from `rules.yaml` `rancher_generated: {label_prefixes: [...], name_patterns: [...]}`.
  - `async def collect_api_versions(client, cluster_id) -> list[str]` from `/api` and `/apis`; stored in `api_versions` with `source='served'`.
  - In-use API versions: before dropping `kubectl.kubernetes.io/last-applied-configuration`, read its `apiVersion` and `kind` only; store with `source='in_use'`.

- [ ] **Step 1: Find the real Rancher markers on dev**

Run a short script against `dev_lists["clusterroles"]` and bindings that prints label keys and name prefixes, grouped. Verified so far: 149 ClusterRoles; label keys include `authz.management.cattle.io/globalrole`, `authz.management.cattle.io/gr-owner`, `authz.cluster.cattle.io/impersonator`, `management.cattle.io/ui-product`. Write the defaults from the full output.

- [ ] **Step 2: Write the failing tests**

```python
def test_no_rancher_role_counted_as_custom(dev_lists, rules):
    custom = [r["metadata"]["name"] for r in dev_lists["clusterroles"]
              if not is_rancher_generated(r, rules) and not is_system_role(r)]
    # every name in custom must be from a user-installed chart (kagent) or listed as reviewed
    assert all(n.startswith("kagent") for n in custom), custom

def test_rancher_webhook_found(dev_lists):  # a validatingwebhookconfiguration named "rancher.cattle.io" exists and derive_webhook works
def test_last_applied_dropped(collected_db):  # no resource_annotations row with key kubectl.kubernetes.io/last-applied-configuration
def test_api_versions_served(collected_db):  # api_versions has "apps/v1" served for local
```

If the first test finds non-Rancher, non-kagent roles, show the list to the user before adjusting the rule.

- [ ] **Step 3: Run, fail, implement, run, pass**
- [ ] **Step 4: Commit** — `git commit -m "feat: platform kinds and Rancher-generated filter"`

---

### Task 6: Complexity flags and the platform page (phase 7 milestone)

**Files:**
- Modify: `inventory/collector/rollup.py`, `config/defaults/rules.yaml` (`complexity: {min_k8s_minor: 32, many_crds: 30}`), `inventory/web/routes/inventory.py`, `inventory.html`
- Create: `inventory/web/routes/platform.py`, `platform.html`, `tests/test_complexity.py`

**Interfaces:**
- Produces:
  - `def complexity_flags(conn, site, cluster_id, rules) -> list[str]` — names exactly as spec 6.14: `k8s<1.32`, `stateful`, `rwx`, `nginx-snippets`, `many-crds`, `deprecated-apis`, `privileged/hostpath`, `gpu`, `operators`, `intercept-all-webhooks`. Stored in `cluster_profile.complexity_flags`.
  - `/platform`: webhooks, aggregated APIServices, custom ClusterRoles, non-system cluster-admin bindings, API versions served and in use.
  - `/inventory`: flags as badges and a `flag` filter.
  - `system_crd_groups` in `rules.yaml`: before this task, ask the user whether `operator.tigera.io` and the Cluster API groups (`*.x-k8s.io`) count as platform. If yes, add `tigera.io` and `x-k8s.io` (subdomain matching from plan 1 covers the rest) and update the dev class expectation in the tests from live data.

- [ ] **Step 1: Write the failing tests**

```python
def test_version_flag_from_k8s_version():
    assert k8s_minor("v1.34.3+rke2r1") == 34   # dev local -> no "k8s<1.32"
def test_local_flags(collected_db, rules):  # flags for local do not include "k8s<1.32"; "many-crds" absent (needs cr_counts)
def test_platform_page(admin_client_collected):  # /platform contains "rancher.cattle.io"
```

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Compare with the script output** from Task 4 Step 1 for webhooks, CRDs and RBAC counts on `local`; record any difference.
- [ ] **Step 4: Commit** — `git commit -m "feat: complexity flags and platform page"`

---

### Task 7: Full query filters, CSV on every table, `/export/*` and `/api/*`

**Files:**
- Modify: `inventory/web/queries.py` (`ALLOWED_FILTERS`), `query.html`, every page template (CSV link)
- Create: `inventory/export/csv.py`, `inventory/web/routes/{export,api}.py`, `tests/test_query_full.py`

**Interfaces:**
- Produces:
  - `ALLOWED_FILTERS` adds `annotation` (`key` or `key=value`), `service_type`, `address` (equals or prefix), `helm_chart` (`name` or `name@version`), `crd_group`, `class`, `flag`.
  - `def csv_response(rows: list[dict], filename: str) -> StreamingResponse` — every page table gets `?format=csv`; each download is logged with the username.
  - `/export/clusters.csv`, `/export/resources.csv?<query filters>`; `/api/clusters`, `/api/resources?<query filters>` returning JSON (session required).
- Carry-over from plan 1 (same files):
  - `name` and `image` filters escape `%` and `_` (`LIKE ... ESCAPE '\'`), as `/network` already does.
  - The 5,000-row note appears only when more than 5,000 rows exist (fetch `max_rows + 1`).
  - A public `validate_filters(filters: dict) -> None` in `queries.py` replaces the private `_where` call in `presets.py`; `/check` and `/presets` show "invalid preset" for a stored preset that fails it or has corrupt JSON, instead of a 500.
  - `/check?preset=abc` shows the preset list with a message instead of a 422.
  - Non-live tests of `build_query` against a copy of `collected_db`: `cluster` matches by id and by name, `image` substring, `label` key and key=value.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("f", [{"service_type": "LoadBalancer"}, {"address": "192.168.50."},
                               {"helm_chart": "cert-manager"}, {"class": "<class of local>"}])
def test_new_filters_find_local(collected_db, f): ...
def test_csv_on_every_page(admin_client_collected):  # each page with ?format=csv -> text/csv, header row present
def test_csv_download_logged(admin_client_collected, caplog):  # log line contains username and path
```

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Commit** — `git commit -m "feat: full query filters, CSV exports and JSON API"`

---

### Task 8: Hashing and the discovery export

Needs prerequisite 1.

**Files:**
- Create: `inventory/export/hashing.py`, `inventory/export/discovery.py`, `tests/test_export.py`
- Modify: `inventory/cli.py` (`export-discovery --out DIR [--site S] [--cluster ID ...] [--strict]`), `inventory/web/routes/export.py` (`/export/discovery`, admin only; `/export/discovery/name-map.csv`)

**Interfaces:**
- Produces:
  - `def hash_name(salt: str, type_: str, value: str) -> str` — `f"{type_}-{sha256(f'{salt}|{type_}|{value}').hexdigest()[:10]}"`. Confirm the exact join and casing against the script before writing the test.
  - `@dataclass ExportOptions`: `scope: Scope`, `strict: bool = False`.
  - `def build_export(conn, settings, opts: ExportOptions, out_dir: Path) -> Path` — writes per-cluster CSVs with the script's file and column names, plus `fleet_summary.csv`; returns the `tar.gz` path. Records every hash in `export_name_map`.
  - `@dataclass SelfCheckHit`: `file`, `column`, `value_sample` (masked).
  - `def self_check(bundle_dir: Path, sensitive: list[str], known_names: set[str]) -> list[SelfCheckHit]` — IPv4 regex `\b(?:\d{1,3}\.){3}\d{1,3}\b`, each sensitive string (case-insensitive), and every real cluster, node, namespace and workload name in `known_names` (except readable system namespaces).
  - Web: a hit returns 422 with a table of file and column; no download.

- [ ] **Step 1: Write the failing tests**

```python
def test_hash_matches_script():
    # run the script's hash function in bash with a fixed salt and value, compare
    out = subprocess.run(["bash", "-c", "source reference/k8s-discovery.sh --source-only; hash_name s cluster local"], ...)
    assert hash_name("s", "cluster", "local") == out.stdout.strip()

@pytest.mark.live
def test_export_has_no_real_names(collected_db, settings, tmp_path):
    path = build_export(collected_db, settings, ExportOptions(Scope()), tmp_path)
    assert self_check(extract(path), ["rancher.local"], real_names(collected_db)) == []

@pytest.mark.live
def test_headers_equal_script(collected_db, tmp_path):  # for each CSV in /tmp/k8sd-local: same file name exists and header row is identical

@pytest.mark.live
def test_planted_ip_blocks(tmp_path):  # write a CSV with "10.1.2.3" into a bundle dir -> one hit naming that file and column

def test_name_map_not_in_bundle(...):  # no member of the tar is named like export_name_map
```

The hashing test calls the real script function. If the script cannot be sourced that way, copy the exact formula from it into the test as a literal expected value computed once by running the script, and say so in the commit.

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Commit** — `git commit -m "feat: anonymised discovery export with self-check (phase 8 milestone)"`

---

### Task 9: Opt-in collectors (phase 9 milestone)

**Files:**
- Create: `inventory/collector/optional/{cr_counts,pvc_usage,helm_secrets,deprecated_api_metrics,secret_types,nginx_config_keys}.py`, `deploy/inventory-reader-extended.yaml`, `tests/test_optional.py`
- Modify: `config/defaults/rules.yaml` (`optional_collectors` block as in spec 6.12, all `enabled: false`), `inventory/collector/cluster.py`, `home.html` (active opt-ins listed)

**Interfaces:**
- Produces: each module has `NAME: str` and `async def collect(client, cluster_id, conn, site, run_id, cfg: dict) -> None` writing to `optional_results`. `cluster.py` calls only those with `enabled: true`.
  - `cr_counts`: `?limit=1`, count = `len(items) + metadata.remainingItemCount`; only for groups in `cfg["groups"]`.
  - `helm_secrets`: fetch a release Secret only when its revision differs from the stored one; keep chart name, version and `[apiVersion, kind]` pairs. Given the dev finding that `apps.catalog.cattle.io` `spec.resources` already lists kinds, ask the user whether to build this one at all before starting it.
  - `deprecated_api_metrics`: GET `/k8s/clusters/<id>/metrics`, parse `apiserver_requested_deprecated_apis`.
  - `secret_types`: list Secrets with `?limit=500`, store counts per `type` only.
  - `pvc_usage`: per node GET `/api/v1/nodes/<node>/proxy/stats/summary`, store `usedBytes` per PVC.
  - `nginx_config_keys`: ConfigMaps of the ingress-nginx controller; store keys only.
- `deploy/inventory-reader-extended.yaml`: the RoleTemplate with the extra rules from spec 6.12, one commented block per collector.

- [ ] **Step 1: Write the failing tests**

```python
def test_all_off_makes_no_calls(settings, db_path, caplog):
    # default rules; run once with httpx logging at DEBUG; no logged URL contains
    # "/metrics", "/proxy/stats", "/api/v1/secrets"
```

Then one live test per collector, each skipped unless an env var `OPTIN_<NAME>=1` is set. Before running each, apply its rule from `deploy/inventory-reader-extended.yaml` to the dev Rancher, bind it to `inventory-reader` on `local`, run the test, then remove the binding and confirm `check-config` shows 403 again.

- [ ] **Step 2: Run, fail, implement, run, pass** (one collector at a time; commit after each)
- [ ] **Step 3: Commit** — `git commit -m "feat: opt-in collector <name>"` per collector

---

### Task 10: TLS, token expiry warning, backup and upgrade path

**Files:**
- Modify: `inventory/cli.py` (`backup <file>`), `inventory/web/queries.py` (`header_context` adds `token_warnings`), `base.html`
- Create: `tests/test_hardening.py`

**Interfaces:**
- Produces:
  - `def backup(db_path: Path, dest: Path) -> None` — `sqlite3.Connection.backup` into `dest`, then `PRAGMA integrity_check` on the copy; fails if not `ok`.
  - `token_warnings`: for each site with `token_expires` within 30 days, "Token for <site> expires in N days" (admins only).
  - TLS: `serve` already supports cert and key from plan 1; here it gets a test.

- [ ] **Step 1: Write the failing tests**

```python
def test_backup_while_writing(db_path):  # writer thread inserting rows; backup completes, integrity "ok"
def test_token_warning(monkeypatch, settings):  # sites.yaml copy with token_expires today+10 -> warning text present
def test_tls_serving(tmp_path):  # self-signed cert made with openssl in the test; serve on a free port; https GET /healthz with verify off -> 200
```

- [ ] **Step 2: Run, fail, implement, run, pass**
- [ ] **Step 3: Restore drill on dev** — `docker exec rancher-inventory inventory backup /data/backup.db`, stop the container, replace `inventory.db` with `backup.db`, start, confirm users and presets are there.
- [ ] **Step 4: Upgrade drill on dev** — build `1.0.1-dev` with migration 002 on top of a `dev/data` created by the plan 1 image; start it with the same mounts; confirm users, presets and trend history survive.
- [ ] **Step 5: Commit** — `git commit -m "feat: backup, token expiry warning, TLS test (phase 10)"`

---

### Task 11: Full dev run in Docker

- [ ] **Step 1:** `uv run pytest -v` — all pass.
- [ ] **Step 2:** Build `rancher-inventory:1.0.0` (native arm64) and run it with the plan 1 Task 16 command.
- [ ] **Step 3:** Walk through `/workloads`, `/storage`, `/namespaces`, `/platform`, `/query` with each new filter, a CSV from each page, and `/export/discovery`. The self-check passes and the bundle opens.
- [ ] **Step 4:** Browser network tab: every request goes to `localhost:8443`.
- [ ] **Step 5:** Commit any fixes found.

---

### Task 12: Airgap build, transfer and test (last step)

Needs prerequisite 3.

**Files:**
- Modify: `README.md` (final airgap steps, replacing the status line), `Dockerfile`

- [ ] **Step 0: Pin the build to `uv.lock`** (carry-over from plan 1: `pip wheel .` resolves unpinned versions). In the build stage run `uv export --frozen --no-dev --format requirements-txt > requirements.txt` (copy the `uv` binary from `ghcr.io/astral-sh/uv` pinned by digest) and `pip wheel --require-hashes -r requirements.txt` plus `pip wheel --no-deps .`. Check: two builds a day apart install identical package versions (`pip freeze` in both images matches `uv.lock`).

- [ ] **Step 1: Build for the airgap architecture**

```bash
docker buildx build --platform linux/amd64 -t rancher-inventory:1.0.0 --load .
docker save rancher-inventory:1.0.0 | gzip > rancher-inventory-1.0.0.tar.gz
shasum -a 256 rancher-inventory-1.0.0.tar.gz > rancher-inventory-1.0.0.tar.gz.sha256
```

- [ ] **Step 2: Carry across** the tar, its `.sha256`, and the host folder layout from the README (no `.env`; it is written on the airgap host).

- [ ] **Step 3: On the airgap host**, follow spec section 4 "In the airgap": `sha256sum -c`, `docker load`, `chown 10001 data`, `check-config`, then `docker run`. Confirm the host has no route to the internet first: `curl -m 5 https://pypi.org` must fail.

- [ ] **Step 4: Check**
- [ ] `check-config` passes for every airgap site and lists RBAC gaps.
- [ ] First run completes; the cluster count matches each Rancher dashboard.
- [ ] Every page loads with no request to another host.
- [ ] Upgrade from 1.0.0 to 1.0.1 keeps users, presets and trends.

- [ ] **Step 5: Commit** — `git commit -m "docs: airgap install verified"`
