# Migration Overview Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the inventory web app into a migration planning tool for the move from VMware/NSX to Nutanix NKP: a site overview with totals and charts, a Table/Cards cluster list, a per-cluster migration sheet, machine pools and all labels, floating IP needs, database detection and Watch out flags.

**Architecture:** Same single process. New collection: one read of `provisioning.cattle.io` clusters per site (machine pools, CNI, CIDRs), stored by migration `003`. Everything else is derived at page time from data already stored, in a new module `inventory/web/migration.py`, so `queries.py` (909 lines) does not grow. Pages keep the route + Jinja template pattern; charts use the bundled Chart.js 4.4.7; a small hand-written `tables.js` adds sort and filter.

**Tech Stack:** Python 3, FastAPI, Jinja2, HTMX, Chart.js 4.4.7 (bundled), SQLite. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-13-migration-overview-redesign-design.md`. Earlier plans' constraints still apply: `docs/superpowers/plans/2026-11-12-plan-1-working-app-on-dev.md`, `docs/superpowers/plans/2026-11-12-plan-2-detail-hardening-airgap.md`.

## Global Constraints

- Everything in plan 1 and plan 2 Global Constraints (no Secrets, read-only, env/commands/args/ConfigMap values never stored, export never contains IPs or real names).
- No mock, sample or synthetic Kubernetes objects in tests. Tests use live dev data (`collected_db`, `dev_lists`) or real objects exported by the user (Prerequisite P2). A copy of a real object or row with one field changed is accepted, as in plan 2.
- Missing data shows "not available" or "not collected", never a blank cell or 0.
- No runtime downloads, no new front-end library, no build step. New JS lives in `inventory/web/static/`.
- The provisioning read is optional: a 401/403 never fails a cluster or a run.
- Existing flag names (`stateful`, `helm`, `custom_crds`, `hostpath` and the ten `COMPLEXITY_FLAGS`) keep working in the Clusters filter, the Query `flag` filter and saved presets.
- Every table keeps a CSV link; old CSV URLs keep returning the same table.
- Run Python only through `uv run` (the project venv is `.venv`).
- Live tests need the dev Rancher (`rancher.local` in `/etc/hosts`) and are marked `live`.

## Prerequisites (from the user, before the tasks that need them)

- **P1. Provisioning read on dev.** Apply README "Step 4" (GlobalRole `inventory-provisioning-reader` and its binding to the `inventory-reader` user) on the dev Rancher. Checked on Nov 13: the dev token gets HTTP 403 on `/k8s/clusters/local/apis/provisioning.cattle.io/v1/clusters`. Needed by Tasks 3 and 4.
- **P2. Real objects from a VMware site**, saved under `tests/fixtures/real/` (this folder is added to `.gitignore` in Task 3 because the files hold real names and IPs):
  - `provisioning-clusters.json`: `kubectl get clusters.provisioning.cattle.io -A -o json` on that site's Rancher local cluster, with at least one cluster that has `spec.rkeConfig.machinePools`.
  - `nodes.json`: `kubectl get nodes -o json` of one of those clusters; `nodes-cluster.txt` holds that cluster's `status.clusterName` (`c-m-...`).
  - `services-lb.json`: `kubectl get svc -A -o json` of a cluster with LoadBalancer services from NSX, AKO, MetalLB or kube-vip.
  Tests that need P2 are marked `real_fixtures` and skip with the reason "P2 fixtures missing" when a file is absent. The final task reports any skipped test.

## Review Focus

1. **A site without the provisioning GlobalRole.** Every cluster of that site shows pools "not collected", CNI comes from daemonsets, the run ends `ok`. Test in Task 4.
2. **One IP behind several objects.** An ingress address that equals its controller's LoadBalancer IP, or two services sharing one IP, counts once. Test in Task 5.
3. **Old flag names and presets.** `/clusters?flag=k8s<1.32` and `/clusters?flag=hostpath` still filter; a Query preset with `flag=stateful` still runs; an unknown flag gives 400. Test in Task 7.
4. **Old URLs with query strings and `format=csv`.** `/helm?chart=X`, `/crds?custom_only=1`, `/annotations?key=K`, `/namespaces?sort=S` and their `format=csv` forms land on the same data and table. Test in Task 10.
5. **A `rules.yaml` override written before this plan** (no `network` or `databases` section) loads, and the built-in defaults are used for the missing sections. Test in Task 1.

## File Structure

```
config/defaults/rules.yaml                    + network, databases sections; annotations.keep + NSX/AKO domains
inventory/collector/rules.py                  Rules.network, Rules.databases + validation
inventory/db/migrations/003_migration_view.sql clusters.api_endpoint/pools_status/prov_name, machine_pools
inventory/db/migrate.py                       SCHEMA_VERSION = 3
inventory/db/repo.py                          upsert_cluster stores api_endpoint; write_pools
inventory/collector/provisioning.py           NEW: parse and read provisioning.cattle.io clusters
inventory/collector/network.py                NEW: detect_cni, cluster_network
inventory/collector/netpolicy.py              derive_service + source_ranges
inventory/collector/cluster.py                collect_cluster(provisioning=...), writes pools and network
inventory/collector/classify.py               build_profiles(network=...)
inventory/collector/runner.py                 one provisioning read per site; delete machine_pools on removal
inventory/checkconfig.py                      provisioning probe line
inventory/web/migration.py                    NEW: LB, floating IPs, databases, Watch out, cluster rows, overview, node groups
inventory/web/queries.py                      site/cluster filters on pvs, pvcs, webhooks; cluster_annotations; _decode_profile CIDRs
inventory/web/routes/{home,clusters,platform,workloads}.py   rewritten pages
inventory/web/routes/{inventory,helm,crds,namespaces,annotations}.py  become redirects
inventory/web/templates/{home,clusters,cluster,platform,workloads,base}.html  rewritten
inventory/web/templates/_cluster_list.html    NEW: Table/Cards list macro
inventory/web/templates/{inventory,helm,crds,namespaces,annotations}.html  deleted
inventory/web/static/{overview.js,tables.js}  NEW
inventory/web/static/app.css                  refresh
tests/test_rules_network.py, test_provisioning.py, test_migration_view.py, test_web_migration.py  NEW
```

---

### Task 1: Rules for networking and databases

**Files:**
- Modify: `config/defaults/rules.yaml`, `inventory/collector/rules.py`
- Test: `tests/test_rules_network.py`

**Interfaces:**
- Produces: `Rules.network: dict` with keys `lb_implementations: dict[str, {"classes": list[str], "annotation_domains": list[str]}]`, `pinned_ip_annotations: list[str]`, `shared_ip_annotations: list[str]`, `vendor_annotation_domains: list[str]`, `cni_daemonsets: dict[str, list[str]]`. `Rules.databases: dict` with keys `images: dict[str, list[str]]` (engine -> fnmatch patterns on the image repository name), `exclude: list[str]`, `operator_crd_groups: dict[str, list[str]]`.

Default values (the builder verifies the NSX/AKO domains and classes against P2 `services-lb.json` and adjusts):

```yaml
network:
  lb_implementations:
    nsx-ncp:  {classes: [], annotation_domains: [ncp, nsx.vmware.com]}
    avi-ako:  {classes: [ako.vmware.com/avi-lb], annotation_domains: [ako.vmware.com, avi.vmware.com]}
    metallb:  {classes: [metallb.io/metallb], annotation_domains: [metallb.universe.tf, metallb.io]}
    kube-vip: {classes: [kube-vip.io/kube-vip-class], annotation_domains: [kube-vip.io]}
  pinned_ip_annotations: [metallb.universe.tf/loadBalancerIPs, metallb.io/loadBalancerIPs, kube-vip.io/loadbalancerIPs]
  shared_ip_annotations: [metallb.universe.tf/allow-shared-ip, metallb.io/allow-shared-ip]
  vendor_annotation_domains: [ncp, nsx.vmware.com, ako.vmware.com, avi.vmware.com]
  cni_daemonsets:
    calico: [calico-node]
    canal: [rke2-canal, canal]
    cilium: [cilium]
    flannel: [kube-flannel-ds, kube-flannel]
    antrea: [antrea-agent]
databases:
  exclude: ["*exporter*"]
  images:
    postgresql: ["postgres", "postgres-*", "postgresql*", "*-postgresql", "cloudnative-pg*", "spilo*", "patroni*"]
    mysql: ["mysql*", "percona-server*", "percona-xtradb*"]
    mariadb: ["mariadb*"]
    mongodb: ["mongo", "mongo-*", "mongodb*", "percona-server-mongodb*"]
    redis: ["redis", "redis-*", "valkey*"]
    elasticsearch: ["elasticsearch*", "opensearch*"]
    cassandra: ["cassandra*", "scylla*"]
    kafka: ["kafka*", "cp-kafka*", "zookeeper*"]
    clickhouse: ["clickhouse*"]
    cockroachdb: ["cockroach*"]
    influxdb: ["influxdb*"]
    rabbitmq: ["rabbitmq*"]
    minio: ["minio", "minio-*"]
    etcd: ["etcd"]
  operator_crd_groups:
    postgresql: [postgresql.cnpg.io, pgv2.percona.com, acid.zalan.do, postgres-operator.crunchydata.com]
    mysql: [ps.percona.com, pxc.percona.com, mysql.oracle.com]
    mongodb: [psmdb.percona.com, mongodbcommunity.mongodb.com]
    redis: [redis.redis.opstreelabs.in, databases.spotahome.com]
    elasticsearch: [elasticsearch.k8s.elastic.co]
    kafka: [kafka.strimzi.io]
```

Also add `ncp`, `nsx.vmware.com`, `ako.vmware.com`, `avi.vmware.com` to `annotations.keep`.

- [ ] **Step 1: Write the failing tests** in `tests/test_rules_network.py`:
  - `test_defaults_load`: `load_rules(settings)` gives `rules.network["lb_implementations"]["metallb"]["annotation_domains"] == ["metallb.universe.tf", "metallb.io"]`, `rules.databases["images"]["postgresql"][0] == "postgres"`, `"calico-node" in rules.network["cni_daemonsets"]["calico"]`, `"nsx.vmware.com" in rules.keep`.
  - `test_old_override_falls_back` (Review Focus 5): write a `rules.yaml` into `settings.config_dir` that holds only `system_namespaces: [kube-*]`; `load_rules` returns that list and the default `network` and `databases` sections.
  - `test_bad_shapes_rejected`, parametrized over `{"network": {"lb_implementations": {"x": {"classes": "nope"}}}}`, `{"network": {"cni_daemonsets": []}}`, `{"databases": {"images": {"pg": "postgres"}}}`: each raises `ConfigError`.
- [ ] **Step 2: Run** `uv run pytest tests/test_rules_network.py -v`. Expected: FAIL (`Rules` has no attribute `network`).
- [ ] **Step 3: Implement** the YAML above and `Rules.network`, `Rules.databases` (both `dict`, default `{}`) in `load_rules`, validating with the existing `strings()` and a nested mapping check. Unknown keys inside `network`/`databases` raise `ConfigError`.
- [ ] **Step 4: Run** `uv run pytest tests/test_rules_network.py tests/test_settings.py tests/test_annotations.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: rules for LB implementations, CNI daemonsets and database detection`.

---

### Task 2: Schema 003 and cluster endpoint

**Files:**
- Create: `inventory/db/migrations/003_migration_view.sql`
- Modify: `inventory/db/migrate.py`, `inventory/db/repo.py`, `tests/test_migrate_upgrade.py`
- Test: `tests/test_repo_clusters.py`

**Interfaces:**
- Produces: columns `clusters.api_endpoint TEXT`, `clusters.pools_status TEXT` (`ok` | `none` | `not_collected`, NULL before the first run), `clusters.prov_name TEXT`; table `machine_pools (site, cluster_id, name, quantity INTEGER, roles TEXT /*JSON list*/, labels TEXT /*JSON map*/, taints TEXT /*JSON list of key:effect*/, machine_config_kind TEXT, PRIMARY KEY (site, cluster_id, name))`.
- Produces: `repo.cluster_api_endpoint(cluster: dict) -> str | None`: `localClusterAuthEndpoint.fqdn` when `localClusterAuthEndpoint.enabled` is true and fqdn is set, else `apiEndpoint`, else None. `upsert_cluster` stores it.
- Produces: `repo.write_pools(conn, site: str, cluster_id: str, status: str, prov_name: str | None, pools: list[MachinePool]) -> None`: sets `clusters.pools_status` and `prov_name`, replaces the cluster's `machine_pools` rows. `MachinePool` comes from Task 3; type it as `list` here and import in Task 3 to avoid a cycle.

- [ ] **Step 1: Write the failing tests:**
  - In `tests/test_migrate_upgrade.py`: change `SCHEMA_VERSION == 2` to `== 3`; add `test_upgrade_v2_to_v3_keeps_data` (live) on the same pattern: apply migrations 1 and 2, copy real rows from `collected_db` (v2 columns only), `migrate` returns 3, users/presets/metrics counts unchanged, `machine_pools` exists and is empty.
  - In `tests/test_repo_clusters.py` (live): `test_api_endpoint_from_v3` with `local_cluster`: `cluster_api_endpoint(local_cluster) is None` (dev has `localClusterAuthEndpoint.enabled == False` and no `apiEndpoint`); a copy of `local_cluster` with `localClusterAuthEndpoint = {"enabled": True, "fqdn": "api.dev.example"}` gives `"api.dev.example"`.
- [ ] **Step 2: Run** `uv run pytest tests/test_migrate_upgrade.py tests/test_repo_clusters.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration, `SCHEMA_VERSION = 3`, `cluster_api_endpoint`, `upsert_cluster` writing `api_endpoint`, and `write_pools` (inside `transaction`).
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/test_db.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: schema 3 with machine pools and cluster API endpoint`.

---

### Task 3: Provisioning reader

**Files:**
- Create: `inventory/collector/provisioning.py`, `tests/test_provisioning.py`
- Modify: `inventory/checkconfig.py`, `.gitignore` (add `tests/fixtures/real/`), `pyproject.toml` (marker `real_fixtures: needs the user's real objects in tests/fixtures/real`), `tests/conftest.py` (fixture `real_fixture(name)` that loads a JSON file or `pytest.skip("P2 fixtures missing: <name>")`)
- Needs: P1 (live test), P2 (`provisioning-clusters.json`)

**Interfaces:**
- Produces:
  - `PROVISIONING_PATH = "/apis/provisioning.cattle.io/v1/clusters"` (listed through cluster `local`).
  - `@dataclass MachinePool: name: str; quantity: int | None; roles: list[str]; labels: dict[str, str]; taints: list[str]; machine_config_kind: str | None`. Roles in the order `control-plane`, `etcd`, `worker` from `controlPlaneRole`, `etcdRole`, `workerRole`. Taints as `key:effect`; taint values are never kept.
  - `@dataclass ClusterProvisioning: prov_name: str; has_rke_config: bool; pools: list[MachinePool]; cni: str | None; cluster_cidr: str | None; service_cidr: str | None`. CNI and CIDRs come from `spec.rkeConfig.machineGlobalConfig` keys `cni`, `cluster-cidr`, `service-cidr`.
  - `@dataclass ProvisioningResult: status: str  # ok | forbidden | failed; clusters: dict[str, ClusterProvisioning]; error: str | None = None`.
  - `parse_provisioning(obj: dict) -> tuple[str, ClusterProvisioning] | None`: key is `status.clusterName`; None when it is missing.
  - `async read_provisioning(client: RancherClient) -> ProvisioningResult`: 401/403 -> `forbidden`, other `RancherError` -> `failed`; never raises `RancherError`.
  - `pools_status(result: ProvisioningResult | None, cluster_id: str) -> str`: `not_collected` when result is None or not `ok`; `ok` when the cluster has at least one pool; else `none` (imported, custom, or `local`).
  - `SiteCheck.provisioning: int | None` (HTTP status of a `page_size=1` probe); `format_report` prints `provisioning clusters (optional): readable` or `provisioning clusters (optional): HTTP 403, machine pools and CNI from Rancher not available`.

- [ ] **Step 1: Write the failing tests** in `tests/test_provisioning.py`:
  - `test_parse_real_pools` (`real_fixtures`): for every item in `provisioning-clusters.json` with `spec.rkeConfig.machinePools`, `parse_provisioning(item)` returns `(item["status"]["clusterName"], p)` with `[m.name for m in p.pools] == [mp["name"] for mp in machinePools]`, each `quantity`, `labels` equal to the pool's `labels or {}`, `machine_config_kind == mp["machineConfigRef"]["kind"]`, and every taint string has no `=`.
  - `test_parse_without_cluster_name` (`real_fixtures`): a copy of a real item with `status.clusterName` removed gives None.
  - `test_pools_status`: `pools_status(None, "c") == "not_collected"`; `ProvisioningResult("forbidden", {})` -> `not_collected`; `ProvisioningResult("ok", {})` -> `none` for an unknown cluster.
  - `test_read_dev` (`live`, needs P1): `read_provisioning(live_client)` gives `status == "ok"` and `"local" in result.clusters`; `pools_status(result, "local") == "none"`.
  - `test_check_config_reports_provisioning` (`live`): `check_config(settings)` gives `checks[0].provisioning == 200` and the report contains `provisioning clusters (optional): readable`.
- [ ] **Step 2: Run** `uv run pytest tests/test_provisioning.py -v`. Expected: FAIL (module not found).
- [ ] **Step 3: Implement** `provisioning.py` and the check-config probe (reuse `probe` with `cid="local"`).
- [ ] **Step 4: Run** `uv run pytest tests/test_provisioning.py tests/test_checkconfig.py -v`. Expected: PASS (P2 tests skip only if the files are absent).
- [ ] **Step 5: Commit** `feat: read machine pools, CNI and CIDRs from provisioning clusters`.

---

### Task 4: Wire provisioning and network into runs

**Files:**
- Create: `inventory/collector/network.py`
- Modify: `inventory/collector/cluster.py`, `inventory/collector/classify.py`, `inventory/collector/runner.py`, `inventory/web/queries.py` (`_decode_profile`)
- Test: `tests/test_collect_cluster.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: Task 3 `ProvisioningResult`, `ClusterProvisioning`, `pools_status`, `read_provisioning`; Task 2 `write_pools`.
- Produces:
  - `network.detect_cni(daemonset_names: list[str], rules: Rules) -> str | None`: first CNI in `rules.network["cni_daemonsets"]` order whose names include one of the daemonsets.
  - `network.cluster_network(conn, site: str, cluster_id: str, prov: ClusterProvisioning | None, rules: Rules) -> dict` with keys `cni`, `cni_source` (`provisioning` | `daemonset` | None), `cluster_cidr`, `service_cidr` (provisioning values, else None), `pod_cidrs` (sorted distinct node `spec.podCIDR`). Provisioning values win when both exist.
  - `build_profiles(..., network: dict | None = None)` writes `json.dumps(network)` into `cluster_profile.network` instead of NULL.
  - `collect_cluster(..., provisioning: ProvisioningResult | None = None)`: after the kinds loop, calls `write_pools(conn, site, cid, pools_status(provisioning, cid), prov.prov_name if prov else None, prov.pools if prov else [])` and passes `cluster_network(...)` to `build_profiles`.
  - `runner._execute` calls `read_provisioning` once per site after the clients are made and passes the result to every `_run_cluster` of that site. `_remove_unlisted` also deletes `machine_pools` rows.
  - `_decode_profile`: `cni` from `network["cni"]`; `cidrs` = cluster and service CIDR when set, else `"pod ranges from nodes: " + ", ".join(pod_cidrs)`, else `"not available"`.

- [ ] **Step 1: Write the failing tests** (all `live`, on `collected_db` and `dev_lists`):
  - `test_network_profile_dev`: `cluster_profile.network` of `local` decodes to `cni == "calico"`, `cni_source == "daemonset"` (dev `local` has no `rkeConfig`), `pod_cidrs == sorted({n["spec"]["podCIDR"] for n in dev_lists["nodes"]})`, `service_cidr is None`.
  - `test_pools_status_dev` (needs P1): `clusters.pools_status` of `local` is `none`; `machine_pools` has no rows for it.
  - `test_node_os_stored` (spec 1a): for each node in `dev_lists["nodes"]`, the stored `data.status.nodeInfo` has `osImage`, `kernelVersion`, `containerRuntimeVersion`, `architecture` equal to the live values.
  - `test_forbidden_provisioning_keeps_run_ok` (Review Focus 1): `collect_cluster` with `provisioning=ProvisioningResult("forbidden", {}, "HTTP 403")` against `local` returns status `ok`, `pools_status` is `not_collected`, and the network `cni` is still `calico`.
  - `test_detect_cni`: `detect_cni(["calico-node", "x"], rules) == "calico"`; `detect_cni([], rules) is None`.
- [ ] **Step 2: Run** `uv run pytest tests/test_collect_cluster.py tests/test_runner.py -v -k "network or pools or node_os or forbidden_provisioning or detect_cni"`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** `uv run pytest tests/test_collect_cluster.py tests/test_runner.py tests/test_classify.py tests/test_complexity.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: store machine pools and cluster network in each run`.

---

### Task 5: Load balancers, special network config and floating IPs

**Files:**
- Create: `inventory/web/migration.py`, `tests/test_migration_view.py`
- Modify: `inventory/collector/netpolicy.py` (`derive_service`)
- Needs: P2 (`services-lb.json`) for one test

**Interfaces:**
- Produces in `netpolicy.derive_service`: `source_ranges: int` = `len(spec.loadBalancerSourceRanges or [])`. CIDR values are not stored.
- Produces in `inventory/web/migration.py`:
  - `SERVICE_SIGNALS = ("pinned-ip", "local-traffic-policy", "source-ranges", "shared-ip", "vendor-annotations")`.
  - `lb_implementation(lb_class: str | None, annotation_keys: list[str], rules: Rules) -> str`: first entry of `rules.network["lb_implementations"]` whose `classes` contains `lb_class` or whose `annotation_domains` match a key (use `inventory.collector.annotations.matches` on each key); else `"unidentified"`.
  - `service_signals(svc: dict, rules: Rules) -> list[str]` in `SERVICE_SIGNALS` order. `svc` keys: `type`, `lb_class`, `annotation_keys`, `external_traffic_policy`, `load_balancer_ip`, `source_ranges`. `pinned-ip`: `load_balancer_ip` set or a key in `pinned_ip_annotations`; `local-traffic-policy`: type LoadBalancer or NodePort and policy `Local`; `source-ranges`: `source_ranges > 0`; `shared-ip`: a key in `shared_ip_annotations`; `vendor-annotations`: a key matching `vendor_annotation_domains`.
  - `lb_services(conn, rules, site: str | None = None, cluster_id: str | None = None) -> list[dict]`: one row per LoadBalancer service: `site`, `cluster_id`, `cluster`, `namespace`, `name`, `addresses` (sorted list of `lb` addresses plus `spec.loadBalancerIP`), `implementation`, `signals`.
  - `floating_ips(conn, rules, site: str | None = None, cluster_id: str | None = None) -> dict[tuple[str, str], dict]` for every cluster in scope, value keys: `lb`, `external_ip`, `ingress_lb`, `node_external` (ints: distinct addresses per source; an `ingress-lb` address already counted under `lb` is not counted again), `control_plane` (`clusters.api_endpoint` or None), `addresses` (sorted distinct list over all sources), `total` (`len(addresses)` plus 1 when `control_plane` is set), `implementations` (sorted distinct implementations of the cluster's LB services), `special` (sorted distinct service signals, plus `several-lb-implementations` when more than one implementation other than `unidentified`).

- [ ] **Step 1: Write the failing tests** in `tests/test_migration_view.py`:
  - `test_source_ranges_dev` (`live`): for each service in `dev_lists["services"]`, `derive_service(s)["source_ranges"] == len(s["spec"].get("loadBalancerSourceRanges") or [])`.
  - `test_floating_ips_dev` (`live`): on `collected_db`, `fi = floating_ips(conn, rules)[("dev", "local")]`; expected addresses come from the stored `addresses` rows with roles `lb`, `ingress-lb`, `external-ip`, `node-external`; `fi["total"] == len(fi["addresses"])` (dev has no control plane endpoint); `"192.168.50.210" in fi["addresses"]`.
  - `test_same_ip_counted_once` (`live`, Review Focus 2): copy `collected_db` to `tmp_path`, insert a second `addresses` row with role `lb` and the same address as the existing `ingress-lb` row on a real dev service id; `floating_ips(...)["total"]` is unchanged and `fi["ingress_lb"] == 0`.
  - `test_real_lb_services` (`real_fixtures`): for each LoadBalancer item in `services-lb.json`, trim it with `trim(kinds["services"], obj, rules)`, build `svc` from the trimmed data, and assert `lb_implementation(...) != "unidentified"` for every item that has a `loadBalancerClass` or an annotation under a configured domain; print the implementation per service so the builder can check the defaults in Task 1.
  - `test_service_signals_from_real_copy` (`live`): take a real dev service, copy it, set `spec.type="LoadBalancer"`, `spec.externalTrafficPolicy="Local"`, `spec.loadBalancerIP` to its own clusterIP, and add the annotation key `metallb.universe.tf/allow-shared-ip`; `service_signals` gives `["pinned-ip", "local-traffic-policy", "shared-ip"]`.
- [ ] **Step 2: Run** `uv run pytest tests/test_migration_view.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. Read service fields with `json_extract` from `resources.data` (`$.spec.type`, `$.lb_class`, `$.annotation_keys`, `$.external_traffic_policy`, `$.spec.loadBalancerIP`, `$.source_ranges`).
- [ ] **Step 4: Run** `uv run pytest tests/test_migration_view.py tests/test_detail_kinds.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: floating IP needs, LB implementation and special network signals`.

---

### Task 6: Database detection

**Files:**
- Modify: `inventory/web/migration.py`
- Test: `tests/test_migration_view.py`

**Interfaces:**
- Produces:
  - `image_repo_name(image: str) -> str`: last path segment of the repository, without tag or digest (`docker.io/library/postgres:18` -> `postgres`; `registry:5000/team/redis@sha256:ab` -> `redis`).
  - `image_engine(image: str, rules: Rules) -> str | None`: None when the name matches `databases.exclude`; else the first engine whose patterns match (`fnmatchcase`).
  - `databases(conn, rules, site: str | None = None, cluster_id: str | None = None) -> dict[tuple[str, str], list[dict]]`: per cluster, workload hits `{"engine", "namespace", "kind", "name", "image"}` for `deployments`, `statefulsets`, `daemonsets`, `cronjobs`, `jobs` in user namespaces (`namespace_profile.is_system = 0`), plus CRD hits `{"engine", "crd_group"}` when a CRD of a configured operator group exists. One row per (engine, namespace, kind, name).

- [ ] **Step 1: Write the failing tests:**
  - `test_image_repo_name`, parametrized: the two examples above, plus `postgres` -> `postgres`, `quay.io/prometheuscommunity/postgres-exporter:v0.15` -> `postgres-exporter`.
  - `test_image_engine`: `postgres:18` -> `postgresql`; `quay.io/prometheuscommunity/postgres-exporter:v0.15` -> None; `rancher/local-path-provisioner:v0.0.31` -> None.
  - `test_databases_dev` (`live`): `databases(conn, rules)[("dev", "local")]` contains `{"engine": "postgresql", "namespace": "kagent", "kind": "deployments", "name": "kagent-postgresql", "image": "docker.io/library/postgres:18"}` (value read from the stored `resource_images` row, not hard-coded, if the image tag changed); no hit in a system namespace.
- [ ] **Step 2: Run** `uv run pytest tests/test_migration_view.py -v -k "image or databases"`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: detect databases from images and operator CRDs`.

---

### Task 7: Watch out flags, cluster rows and the merged Clusters page

**Files:**
- Modify: `inventory/web/migration.py`, `inventory/web/routes/clusters.py`, `inventory/web/routes/inventory.py` (redirect only), `inventory/web/templates/clusters.html`
- Create: `inventory/web/templates/_cluster_list.html`
- Delete: `inventory/web/templates/inventory.html`
- Test: `tests/test_web_migration.py`; update `tests/test_web_inventory.py`, `tests/test_complexity.py`

**Interfaces:**
- Consumes: Tasks 5 and 6.
- Produces in `migration.py`:
  - `WATCH_OUT_FLAGS = ("node-pinned-pv", "database", "external-ip", "nodeport", "host-network", "custom-webhooks", "special-network-config", "not-collected")`.
  - `watch_out(conn, rules, site: str | None = None) -> dict[tuple[str, str], list[str]]`: `WATCH_OUT_FLAGS` that apply, in that order, followed by the stored complexity flags. Rules: `node-pinned-pv`: a PV with `data.local` or `data.pinned`; `database`: a hit from `databases()`; `external-ip`: `floating_ips()` total > 0; `nodeport`: a `NodePort` service in a user namespace; `host-network`: a workload in a user namespace with `host_network` true or `host_ports > 0`; `custom-webhooks`: a webhook configuration with `rancher_generated` false and (`url_based` or a service namespace that is not a system namespace); `special-network-config`: `floating_ips()[...]["special"]` not empty; `not-collected`: a `run_kinds` row with status `failed` in the cluster's latest run.
  - `ALL_FLAGS: tuple[str, ...]` = `FLAG_COLUMNS` keys + `COMPLEXITY_FLAGS` + `WATCH_OUT_FLAGS`, without duplicates.
  - `label_matches(conn, label: str) -> dict[tuple[str, str], list[str]]`: `label` is `key` or `key=value`; matches cluster labels (`clusters.labels`), node labels (`resource_labels` of kind `nodes`) and pool labels (`machine_pools.labels`). Value list names what matched: `"cluster"`, `"node <name>"`, `"pool <name>"`.
  - `cluster_rows(conn, rules, site=None, cls=None, flag=None, label=None) -> list[dict]`. Keys per row: everything `queries.inventory_rows` returns, plus `pools_status`, `pools` (names), `os_images` (sorted distinct `osImage`; `["not collected"]` when no node has it), `cpu_alloc_m`, `cpu_req_m`, `cpu_used_m`, `mem_alloc_b`, `mem_req_b`, `mem_used_b`, `pv_count`, `pv_bytes`, `floating` (Task 5 dict), `databases` (engine names), `flags` (from `watch_out`), `label_hits` (from `label_matches`, empty when no label filter). `flag` accepts any name in `ALL_FLAGS`; others raise `ValueError`.
- Produces routes: `GET /clusters?site=&cls=&flag=&label=&view=table|cards` (default `table`, invalid view -> `table`); CSV tables `clusters` (one flat row per cluster: list fields joined with `; `, floating as `total` plus one column per source). `GET /inventory` -> `307` to `/clusters` with the same query string.
- `_cluster_list.html` macro `cluster_list(rows, view)` with the Table/Cards switch as two links that keep the current query and set `view`. Table columns, in order: Site, Cluster (link + `state` marker from `_class_badge.html`), Kubernetes, Nodes, Pools (count, `title` lists names; "none" or "not collected" from `pools_status`), OS, vCPU, Memory GiB, PVs (count and size), Floating IPs (total, `title` lists the breakdown), LB, Watch out (badges), and "Matched" when a label filter is set. Cards show the same fields per cluster. Empty result text stays `No clusters match.`.

- [ ] **Step 1: Write the failing tests** in `tests/test_web_migration.py` (all `live` on `admin_client_collected` / `collected_db`):
  - `test_watch_out_dev`: flags of `("dev", "local")` include `node-pinned-pv`, `database`, `external-ip`; do not include `not-collected`; end with the stored `complexity_flags` list.
  - `test_not_collected_flag`: copy `collected_db`, set one `run_kinds` row of the latest run to `failed`; flags include `not-collected`.
  - `test_clusters_page_table_and_cards`: `/clusters` shows `/clusters/dev/local`, the OS string of a dev node, and `node-pinned-pv`; `/clusters?view=cards` has the same values in card markup; `/clusters?view=bogus` renders the table.
  - `test_flag_filter_old_and_new` (Review Focus 3): `/clusters?flag=hostpath`, `/clusters?flag=` + first stored complexity flag, `/clusters?flag=database` all show local; `/clusters?flag=nodeport` shows `No clusters match.`; `/clusters?flag=bogus` is 400; `/query?flag=stateful` still returns 200.
  - `test_label_filter_node_label`: `/clusters?label=kubernetes.io/hostname%3D<first dev node name>` shows local and `node <name>`; `/clusters?label=nope%3D1` shows `No clusters match.`.
  - `test_inventory_redirect`: `/inventory?cls=x` without following redirects is 307 to `/clusters?cls=x`.
  - Update `tests/test_web_inventory.py` and `tests/test_complexity.py`: requests to `/inventory` keep working through the redirect; `test_home_class_card` moves to Task 8.
- [ ] **Step 2: Run** `uv run pytest tests/test_web_migration.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces and templates. Read PV size with `units.parse_bytes` on `spec.capacity.storage`.
- [ ] **Step 4: Run** `uv run pytest tests/test_web_migration.py tests/test_web_inventory.py tests/test_complexity.py tests/test_query_full.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: Watch out flags and merged Clusters page with table and cards`.

---

### Task 8: Overview page

**Files:**
- Modify: `inventory/web/migration.py`, `inventory/web/routes/home.py`, `inventory/web/templates/home.html`
- Create: `inventory/web/static/overview.js`
- Test: `tests/test_web_migration.py`, update `tests/test_web_inventory.py`

**Interfaces:**
- Consumes: `cluster_rows` (Task 7).
- Produces: `site_overview(conn, rules, site: str | None = None) -> dict` with:
  - `totals`: `clusters`, `nodes`, `cpu_alloc_m`, `mem_alloc_b`, `pv_count`, `pv_bytes`, `floating_ips` (sum of `floating.total`), `special_network_clusters` (clusters with `special-network-config`).
  - `charts`: `os_images` (`[{"os", "nodes"}]`, nodes without `osImage` as `"not collected"`), `capacity` (`[{"cluster", "cpu_alloc_m", "cpu_req_m", "cpu_used_m", "mem_alloc_b", "mem_req_b", "mem_used_b"}]`), `pv_by_class` (`[{"storage_class", "pv_count", "bytes"}]`, no class as `"none"`), `floating` (`[{"cluster", "lb", "external_ip", "ingress_lb", "node_external", "control_plane"}]`, `control_plane` 0 or 1).
  - `clusters`: `cluster_rows(conn, rules, site=site)`.
- Route `GET /?site=&view=`: renders site picker (`all sites` + `queries.site_names`), totals row, four `<canvas>` charts, `cluster_list(rows, view)`, the existing failed-clusters list, the refresh-site control and the `Opt-in collectors:` line. Chart data goes in `<script type="application/json" id="overview-data">` and `overview.js` draws: donut (OS), grouped bars (capacity: allocatable / requested / used, vCPU and GiB as two charts or two axes, builder's choice), bars (PV by class), stacked bars (floating IPs by source). Values with no data show "not available" text instead of an empty chart.

- [ ] **Step 1: Write the failing tests** (`live`):
  - `test_site_overview_dev`: `totals["nodes"] == len(dev_lists["nodes"])`, `totals["pv_count"] == len(dev_lists["persistentvolumes"])`, `sum(o["nodes"] for o in charts["os_images"]) == totals["nodes"]`, `totals["floating_ips"] == floating_ips(conn, rules)[("dev","local")]["total"]`.
  - `test_home_page`: `/` contains `id="overview-data"`, `/static/overview.js`, `Floating IP`, `Opt-in collectors:`, `/clusters/dev/local`; `/?site=dev` and `/?site=other` return 200 and the second shows `No clusters match.`; `/?view=cards` renders cards.
  - Replace `test_home_class_card` in `tests/test_web_inventory.py` with the above (class cards are removed).
- [ ] **Step 2: Run** `uv run pytest tests/test_web_migration.py -v -k "overview or home"`. Expected: FAIL.
- [ ] **Step 3: Implement.** Before writing chart code, read the `dataviz` skill and follow its palette and mark rules; keep one palette for all charts in `overview.js` and the cluster page.
- [ ] **Step 4: Run** `uv run pytest tests/test_web_migration.py tests/test_web_inventory.py tests/test_optional.py tests/test_hardening.py tests/test_web_core.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: overview page with site totals, charts and cluster list`.

---

### Task 9: Cluster page as a migration sheet

**Files:**
- Modify: `inventory/web/migration.py`, `inventory/web/queries.py`, `inventory/web/routes/clusters.py`, `inventory/web/templates/cluster.html`
- Test: `tests/test_web_migration.py`
- Needs: P2 (`provisioning-clusters.json`, `nodes.json`, `nodes-cluster.txt`) for one test

**Interfaces:**
- Produces in `migration.py`:
  - `BUILTIN_LABEL_PREFIXES = ("kubernetes.io/", "beta.kubernetes.io/", "node-role.kubernetes.io/", "node.kubernetes.io/")`.
  - `assign_nodes(nodes: list[dict], pools: list[dict], prov_name: str | None) -> list[dict]`: a node belongs to the pool whose name prefix `f"{prov_name}-{pool['name']}-"` starts the node name (Rancher machine naming); nodes that match no pool are grouped by role set (`control-plane/etcd`, `worker`, ...). Each group: `{"kind": "pool" | "role", "name", "quantity", "roles", "labels", "taints", "machine_config_kind", "nodes"}`. If P2 shows the prefix rule does not hold, the builder switches to the rule P2 supports (for example a node label carrying the pool name) and records it in the task commit message.
  - `node_groups(conn, site, cluster_id) -> dict`: `{"pools_status", "groups"}`. Node row keys: `name`, `internal_ip`, `os`, `kernel`, `runtime`, `arch`, `ready`, `cpu_m`, `mem_b`, `labels_builtin` (dict), `labels_other` (dict).
- Produces in `queries.py`: optional `site` and `cluster_id` keyword filters on `pvs`, `pvcs`, `webhooks`; `cluster_annotations(conn, site, cluster_id) -> list[dict]` (`kind`, `namespace`, `name`, `key`, `value`, custom annotations only).
- Route `GET /clusters/{site}/{cluster_id}` sections in this order, each with its CSV link: Summary (state, provider, Kubernetes, nodes, vCPU/memory alloc/req/used, last ok, Watch out badges); Nodes (groups with expandable `<details>` node table; builtin labels in a nested closed `<details>`); External networking (floating IP total and breakdown, control plane endpoint or "not available", LB services with implementation and signals, ingresses, NodePort and external IP services, CNI and CIDRs, and the sentence "IPs that NSX assigns outside Kubernetes are not visible to this app."); Storage (PVs and PVCs, node-pinned marked); Databases and stateful workloads; Platform dependencies (Helm, custom CRD groups, webhooks); Namespaces; Custom annotations; Usage charts (existing); Cluster labels. CSV table names: `nodes`, `pools`, `lb_services`, `services`, `ingresses`, `pvs`, `pvcs`, `databases`, `helm`, `crd_groups`, `webhooks`, `namespaces`, `annotations`, `metrics`.

- [ ] **Step 1: Write the failing tests:**
  - `test_node_groups_dev` (`live`): `node_groups(conn, "dev", "local")["pools_status"] == "none"`; groups are `role` groups; the union of their nodes equals the dev node names; a node's `os` equals its live `osImage`; `kubernetes.io/hostname` is in `labels_builtin`, not `labels_other`.
  - `test_assign_nodes_real` (`real_fixtures`): with the provisioning item whose `status.clusterName` equals `nodes-cluster.txt`, parse its pools (Task 3), and `assign_nodes(nodes.json items, pools, prov_name)` puts every node in a `pool` group, and each pool group's node count equals that pool's `quantity`.
  - `test_cluster_page_sections` (`live`): `/clusters/dev/local` contains, in this order, the headings `Nodes`, `External networking`, `Storage`, `Databases`, `Platform dependencies`, `Namespaces`, `Custom annotations`, `Usage per day`, `Cluster labels`; contains `kagent-postgresql` and `not visible to this app`; `?format=csv&table=nodes` returns a CSV with header `name`.
  - The existing `test_failed_marker` for `/clusters/dev/local` still passes.
- [ ] **Step 2: Run** `uv run pytest tests/test_web_migration.py -v -k "node_groups or assign_nodes or cluster_page"`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_web_migration.py tests/test_web_inventory.py tests/test_query_full.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: cluster page as a migration sheet with nodes by pool`.

---

### Task 10: Navigation and page merges

**Files:**
- Modify: `inventory/web/templates/base.html`, `inventory/web/routes/{platform,workloads,helm,crds,annotations,namespaces}.py`, `inventory/web/templates/{platform,workloads}.html`, `inventory/web/deps.py`, `inventory/web/app.py`
- Delete: `inventory/web/templates/{helm,crds,annotations,namespaces}.html`
- Test: `tests/test_web_migration.py`; update `tests/test_query_full.py`, `tests/test_rollup.py`, `tests/test_web_inventory.py`

**Interfaces:**
- Produces: `deps.legacy_redirect(request: Request, target: str, csv_table: str) -> RedirectResponse`: 307 to `target` with the same query string; when `format=csv` is set and `table` is not, adds `table=csv_table`.
- Redirects: `/helm` -> `/platform` (`charts`), `/crds` -> `/platform` (`groups`), `/annotations` -> `/platform` (`keys`), `/namespaces` -> `/workloads` (`namespaces`). Fragment anchors are added to the target for browsers (`#helm`, `#crds`, `#annotations`, `#namespaces`).
- `/platform` accepts `chart`, `custom_only`, `key` and keeps the old table names: `webhooks`, `apiservices`, `roles`, `admin_bindings`, `api_versions`, `charts`, `releases` (when `chart`), `groups`, `keys`, `objects` (when `key`, capped as today).
- `/workloads` accepts `sort` and adds table `namespaces` (capped as today) after `workloads`.
- Nav in `base.html`: Overview, Clusters, Network, Storage, Workloads, Platform; then a `<details class="tools">` menu "Tools" with Query, Presets, Check, Runs, Export, Users (admin only, as today). Export stays admin-only as today.

- [ ] **Step 1: Write the failing tests:**
  - `test_legacy_redirects` (`live`, Review Focus 4), parametrized: `/helm?chart=cert-manager` -> 307 to `/platform?chart=cert-manager`; `/crds?custom_only=1`; `/annotations?key=k`; `/namespaces?sort=cpu_req_m` -> `/workloads?sort=cpu_req_m`; and `/helm?format=csv` -> `/platform?format=csv&table=charts`. Following each redirect returns 200; the helm CSV equals the old `charts` columns.
  - `test_nav_links`: `/` nav contains the six main links and `Tools`; it contains no `href="/helm"`, `href="/crds"`, `href="/annotations"`, `href="/namespaces"`, `href="/inventory"`.
  - Update `test_page_cap_note_only_when_more_rows` to use `/workloads?table=namespaces` for the namespaces case; update `tests/test_rollup.py` sort tests to `/workloads?sort=...` (the 400 for a bad sort stays); keep `CSV_PAGES` old paths (they now pass through redirects).
- [ ] **Step 2: Run** `uv run pytest tests/test_web_migration.py -v -k "legacy or nav"`. Expected: FAIL.
- [ ] **Step 3: Implement.** Remove the four old routers' page code; keep the modules as redirect-only routers so `app.py` imports stay simple.
- [ ] **Step 4: Run** `uv run pytest tests/test_web_migration.py tests/test_query_full.py tests/test_rollup.py tests/test_web_inventory.py tests/test_web_core.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: shorter navigation; Helm, CRDs and annotations under Platform, namespaces under Workloads`.

---

### Task 11: Sortable tables and style refresh

**Files:**
- Create: `inventory/web/static/tables.js`
- Modify: `inventory/web/static/app.css`, `inventory/web/templates/base.html`
- Test: `tests/test_web_migration.py`

**Interfaces:**
- `tables.js` (plain JS, no globals except an IIFE): on `DOMContentLoaded` and after HTMX swaps (`htmx:afterSwap`), for each `table` without class `facts` whose first row is all `th`: clicking a header sorts the other rows by that column, toggling ascending/descending and marking the header with `aria-sort`. Sort key: sizes with `B|KiB|MiB|GiB|TiB` as bytes; the first number in the cell (`"1.2 / 3.4"` -> 1.2); `-`, empty, `not available`, `not collected` sort last; otherwise case-insensitive text. Tables with more than 10 data rows get a text input above them that hides rows not containing the text (case-insensitive).
- `app.css`: sticky table headers (`th { position: sticky; top: 0 }`), zebra rows, comfortable cell padding, nav that does not wrap at 1280 px, one colour each for `.warn` (amber) and `.error` (red) badges, card grid for `cluster_list` cards, `<details class="tools">` dropdown styling. No external fonts.

- [ ] **Step 1: Write the failing test** `test_tables_js_served`: `/static/tables.js` returns 200 and `/` includes `<script src="/static/tables.js"`.
- [ ] **Step 2: Run** `uv run pytest tests/test_web_migration.py -v -k tables_js`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the test (PASS), then check by hand in a browser with the `run` skill: on `/clusters`, `/workloads` and `/clusters/dev/local`, clicking "Nodes"/"Memory" headers sorts numerically, the filter box hides rows, the header stays visible while scrolling. Take a screenshot of `/` and `/clusters/dev/local`.
- [ ] **Step 5: Commit** `feat: sortable and filterable tables, style refresh`.

---

### Task 12: Docs, full test run and live check in Docker

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README:** Step 4 now also gives machine pools and their labels; new `rules.yaml` sections `network` and `databases` and how to extend them; the overview, Clusters Table/Cards, the cluster migration sheet, the floating IP count and its limit (NSX-only IPs not visible); old URLs redirect; the P2 fixture folder is local only.
- [ ] **Step 2: Run** `uv run pytest -q`. Expected: all pass; list every skipped test with its reason in the hand-off.
- [ ] **Step 3: Live check in Docker on dev:** build the image as in README "Airgap install" for the dev Mac's arch, run it with `dev/config` and a fresh copy of `dev/data`, start a manual refresh, and confirm on `/`: OS chart shows `Ubuntu 24.04.3 LTS` for 3 nodes, floating IPs total 1 for `local`, Watch out on `local` shows `node-pinned-pv`, `database`, `external-ip`; on `/clusters/dev/local`: CNI `calico` (from daemonsets), pools "none", `kagent-postgresql` under Databases. Record the image tag used.
- [ ] **Step 4: Commit** `docs: migration overview, pools, floating IPs and rules sections`.

---

## Self-review notes

- Spec coverage: 1a -> Task 4 test; 1b -> Tasks 2-4; 1c -> Tasks 1, 6; 1d -> Tasks 1, 5; section 2 -> Task 7; 3 -> Task 8; 4 -> Task 9; 5 -> Tasks 7, 10; 6 -> Task 7; 7-8 -> Task 11; testing -> each task and Task 12.
- Deviation from the spec, kept small: the Query page `flag` filter keeps the old flag names only; the new Watch out names are accepted on the Clusters page (spec: "the flag filter accepts both old and new flag names", which refers to the Clusters filter).
- Deviation: "old k8s" and "hostPath / privileged" from spec section 2 are not new flags; they are the existing complexity flags `k8s<1.32` and `privileged/hostpath`, shown once in Watch out. `host-network` is added for hostNetwork and hostPort.
