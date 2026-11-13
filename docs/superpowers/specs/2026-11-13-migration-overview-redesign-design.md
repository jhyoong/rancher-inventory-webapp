# Migration overview redesign

Nov 13, 2026

## Why

The app keeps a daily inventory of every downstream cluster onboarded onto Rancher at a site. Its main use is planning a hardware migration of those clusters to a different hypervisor stack. Anything that would affect or complicate that migration must be easy to find.

The data collection is close to complete, but the web UI does not support this use well:

- 15 top-level pages, each a wide flat table. No page answers "what will make this cluster hard to move".
- The overview shows only three counts, cards per class, and failed clusters. No charts except usage on the cluster page.
- Risk signals (node-pinned PVs, hostPath, privileged pods, external IPs) are spread over several pages.
- Some migration-relevant data is collected but not shown (node labels), some is not stored (node OS, kernel, runtime in the dev data), and some is not collected at all (machine pools, CNI and CIDRs, databases).

## Who uses it

Platform engineers who carry out the migration. When they plan a batch of clusters, they look first at:

1. Hardware resources needed (vCPU, memory)
2. Number of nodes
3. OS of the nodes
4. IP addresses that affect external networking: floating IPs, load balancers, ingress addresses, external IPs, NodePorts
5. PVs
6. Labels: node labels, machine pool labels, cluster labels

## Goals

- An overview page that shows the site's migration footprint at a glance, with charts, and a cluster list that switches between a table and cards.
- A cluster detail page laid out as a migration sheet.
- Every label type tracked and shown: node labels, Rancher machine pool labels, cluster labels.
- Databases detected and flagged.
- A per-cluster "Watch out" summary of things that complicate migration.
- Sortable, filterable tables across the app.
- Fewer top-level pages.

## Non-goals

- No migration planning features (waves, scheduling, assignments, status tracking).
- No changes to auth, runs, the collector schedule, backup, or the discovery export format.
- No writes to Rancher.
- No full visual rebrand.

## Anti-goals

- Blank cells that look like zero. When a value is not available (for example an imported cluster has no machine pools, or a field was not collected), the page says "not available" or "not collected".
- Mock, sample, or synthetic data anywhere in the app or tests that pretends to be real inventory. Tests use fixtures built from real Kubernetes object shapes, as the existing tests do.
- New runtime downloads. The app runs in an airgap; all JS and CSS ship in the image.
- New front-end frameworks or a build step.

## Constraints

- Stack stays as is: FastAPI, Jinja templates, HTMX, Chart.js (already in `inventory/web/static/`), SQLite.
- Any new JS is a small hand-written file in `inventory/web/static/`.
- New collected fields need only RBAC that is already documented. Machine pools, CNI and CIDRs use the `provisioning.cattle.io` GlobalRole in README step 4, which is optional: when it is missing (401/403), those fields show "not collected" and the cluster does not fail.
- Database schema changes go through a new numbered migration in `inventory/db/migrations/`.
- Existing CSV links keep working on every table.

## Design

### 1. Data fixes and additions (do first)

**1a. Node OS, kernel, runtime not stored.** `config/defaults/kinds.yaml` keeps `status.nodeInfo.osImage`, `kernelVersion`, `containerRuntimeVersion` and `architecture` for nodes, and `transform.py` derives `ready`, `taints` and `provider`. The node rows in `dev/data/inventory.db` (run 7) have only `kubeletVersion` and `roles`. Find the cause (likely an older image wrote them, or a bug in the keep path), fix it, and add a test that a node object round-trips these fields into `resources.data`.

**1b. Provisioning cluster reader (new).** No code reads `provisioning.cattle.io/v1` clusters today; `cluster_profile.network` is always NULL, though the README says CNI and CIDRs come from there. Add one reader per site that lists these objects from the Rancher local cluster and stores, per downstream cluster:

- CNI and cluster/service CIDRs (fills the existing `network` field).
- Machine pools from `spec.rkeConfig.machinePools[]`: name, quantity, roles (controlPlane, etcd, worker), labels, taints, and the machine config kind (for example `VmwarevsphereConfig`). The config kind shows which infrastructure a pool runs on today.

Match a provisioning object to a downstream cluster through `status.clusterName` (the `c-xxxxx` ID). Custom and imported clusters have no machine pools; store that as "no pools", not as missing data.

**CNI and CIDRs for clusters without `rkeConfig` (call made after builder check).** The `local` cluster and imported clusters have no `rkeConfig`. For these, detect the CNI from daemonsets already collected (for example `calico-node`, `cilium`, `canal`, `kube-flannel`, `rke2-canal`), and derive the pod range from node `spec.podCIDR` values. Service CIDR shows "not available" when no `rkeConfig` exists. Provisioning values win when both exist.

**1c. Database detection (new).** Add a `databases` section to `rules.yaml` with image name patterns, for example postgres, postgresql, mysql, mariadb, mongo, redis, valkey, elasticsearch, opensearch, cassandra, kafka, zookeeper, etcd (non-system), clickhouse, cockroach, influxdb, rabbitmq, minio. Match against `resource_images` of workloads in user namespaces. Users can edit the list without rebuilding the image. Also treat known operator CRD groups (for example CloudNativePG, Percona, Zalando postgres-operator) as a database signal if their CRDs are present.

### 2. Watch out flags

One function computes a list of flags per cluster, used on the overview table, the cards, and the cluster page. Each flag links to the rows behind it. Initial set:

| Flag | Meaning |
| --- | --- |
| node-pinned PV | PV with node affinity, local, or hostPath source (for example `local-path`) |
| hostPath workload | Workload mounts a hostPath |
| database | Database detected (1c), named |
| external IP | LoadBalancer IP, Service externalIPs, or ingress LB address |
| NodePort | NodePort service in a user namespace |
| hostNetwork / privileged | Workload uses either |
| old k8s | Below `complexity.min_k8s_minor` |
| custom webhooks | Non-Rancher admission webhooks |
| not collected | Part of the cluster's data was not collected in the last run |

The builder may add flags that come from existing data. Thresholds live in `rules.yaml`.

**Relation to existing flags (call made after builder check).** The existing class flags and complexity flags (`rollup.py`, `cluster_profile.complexity_flags`) stay as they are, with the same names, so the Clusters flag filter, the Query `flag` filter and saved presets keep working. Watch out is a display layer: one badge set per cluster built from the complexity flags plus the new flags above (database, external IP, NodePort, node-pinned PV, not collected). Where a new flag duplicates an existing one (old k8s and `k8s<1.32`, hostPath and `privileged/hostpath`), show it once. The class and complexity badges are no longer shown separately on the overview and cluster pages; the flag filter accepts both old and new flag names.

**When "not collected" fires (call made after builder check).** Only when a kind in the base kinds list errored for that cluster in its last run (`run_kinds` status error). It does not fire for: kinds that are not installed, optional collectors that are off, a 401/403 on the optional provisioning reader, or missing metrics-server. Those gaps show as "not available" or "not collected" next to the affected value, not as a cluster flag.

### 3. Overview page (`/`)

Layout A, chosen by the user from a mockup:

- **Site picker** at the top, with "all sites". The whole page follows it.
- **Totals row:** clusters, nodes, vCPU allocatable, memory allocatable, PV storage (count and total size), external IPs and LBs.
- **Three charts** (Chart.js):
  - Nodes by OS image (donut).
  - vCPU and memory per cluster: allocatable, requested, used (bars).
  - PV storage by storage class (bars).
- **Cluster list** with a Table / Cards switch. The choice is kept in the URL so it can be shared.
  - Table columns: cluster, Kubernetes version, nodes, pools, OS, vCPU, memory, PVs (count, size), external IPs, Watch out flags. "Pools" shows the count; hovering lists pool names.
  - Cards show the same data per cluster in a block.
- Failed clusters of the last run stay on the page, below the list.

### 4. Cluster detail page (`/clusters/{site}/{id}`)

Reordered as a migration sheet, each section with its CSV link:

1. Summary and Watch out flags.
2. **Nodes, grouped by machine pool** (or by role for custom and imported clusters). Per group: node count, roles, OS, vCPU, memory, pool labels and taints, machine config kind. Expands to a per-node table: name, internal IP, OS, kernel, runtime, ready, all labels. Built-in labels (`kubernetes.io/*`, `beta.kubernetes.io/*`, `node-role.kubernetes.io/*`, `node.kubernetes.io/*`) are collapsed by default.
3. External networking: LBs, ingress hosts, node IPs, NodePort and external IP services, CNI and CIDRs.
4. Storage: PVs and PVCs, node-pinned ones marked.
5. Databases and stateful workloads.
6. Platform dependencies: Helm releases, custom CRD groups, webhooks.
7. Namespaces (moved here from the Namespaces page) and custom annotations (moved here from the Annotations page).
8. Usage charts (existing).
9. Cluster labels.

### 5. Navigation

Main links: Overview, Clusters, Network, Storage, Workloads, Platform.

A "Tools" menu: Query, Presets, Check, Runs, Export, Users (admin only, as today).

- Clusters and Inventory merge into one Clusters page with the Table / Cards switch and the existing filters (site, class, flag, label).
- Helm and CRDs become sections of Platform.
- Namespaces and Annotations move into the cluster page; cross-cluster lookup stays possible on the Query page.
- Old URLs (`/inventory`, `/helm`, `/crds`, `/namespaces`, `/annotations`) redirect to their new place, so saved links and presets keep working.
- **Cross-cluster views kept (call made after builder check).** The cross-cluster namespace ranking from `/namespaces` (sorted by CPU and memory requested and used) becomes a "Namespaces" section of Workloads; `/namespaces` redirects there with its sort kept. The cross-cluster annotation key lookup from `/annotations` becomes a section of Platform; `/annotations` redirects there with its query kept.

### 6. Label filter

The existing "key or key=value" filter on the Clusters page matches cluster labels, node labels, and machine pool labels. A match on a node or pool label shows the cluster, with the matching node or pool named.

### 7. Tables

One small script (`inventory/web/static/tables.js`) gives every table:

- Click-to-sort on column headers (numbers sort as numbers, sizes as bytes).
- A filter box that hides rows not containing the text.
- A sticky header.

Server-side caps (`PAGE_LIMIT`, `NETWORK_LIMIT`) stay; the client sort and filter work on the rows shown.

### 8. Look

Light refresh of `app.css`: spacing, readable table density, zebra rows, consistent colours for flags (warning amber, error red), a header that does not wrap with the shorter nav. Chart colours follow one palette across pages.

## Decided by the user

- Overview layout A (site totals and charts first, then the cluster list).
- Table / Cards switch on the cluster list.
- First things to see: hardware resources, node count, OS, external networking IPs, PVs.
- All label types tracked: node, machine pool, cluster. Design for fields the dev cluster lacks.

## Calls made in the brainstorm (not asked of the user)

- Navigation regrouping and page merges in section 5.
- Cluster page section order in section 4.
- Database detection by image patterns in `rules.yaml`, plus operator CRD groups.
- Watch out flag list in section 2.
- Client-side sort and filter by a small hand-written script, no library.
- Old URLs redirect rather than 404.

## Left to the builder

- Exact markup, CSS, and chart styling.
- Schema for machine pools (new table or JSON in `cluster_profile`).
- Query structure and how the flags function is split.
- Test layout, following the existing `tests/` style.

## Testing

- Unit tests for: node field round-trip (1a), provisioning reader parsing including custom and imported clusters and a 403 (1b), database pattern matching (1c), each Watch out flag.
- Web tests for: overview with site picker, Table / Cards switch, cluster page sections, redirects of old URLs, label filter on node and pool labels.
- A collection run against the dev Rancher, checking that OS, kernel, runtime appear for `dev/local`, that the CNI is detected from daemonsets (calico on dev), that the pod range comes from node podCIDRs, and that the cluster shows "no pools".

## Starting point

The working tree on `plan-2` has uncommitted changes (row capping via `cap_rows` on network, storage, workloads, namespaces and annotations, plus a test) that touch pages this redesign moves. These are committed or dropped before the plan starts.
