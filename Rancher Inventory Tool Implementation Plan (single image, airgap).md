# Rancher Inventory Tool: Implementation Plan (single image, airgap)

Nov 12, 2026 · @JiaHui

## 1. Decisions

The tool ships as one container image that runs the collector, the web app and an embedded SQLite database in a single process. Everything in the original build plan that is not listed below stays as it was: Python, the read-only service user per site, the transform rules, the check and query pages.

| Topic | Original plan | This plan |
| --- | --- | --- |
| Packaging | 4 containers with docker-compose | 1 image, 1 container, 1 process |
| Data freshness | Snapshot every 15 minutes | One run a day, plus a manual refresh button with progress tracking |
| Database | PostgreSQL 16, JSONB, GIN indexes | SQLite file on a mounted volume, WAL mode, side tables instead of GIN |
| Collector | Separate container | Background worker inside the app, same image also runs it as a one-off command |
| Login | oauth2-proxy and company SSO | Built-in local users and passwords, session cookie |
| DB roles | `collector` and `web_ro` | One file; query page uses a read-only connection with a 10 s time limit |
| Config | YAML files and several env vars | One `.env` file plus one `sites.yaml`; kinds and rules have defaults baked into the image |
| Front-end assets | Chart.js from a CDN | HTMX and Chart.js copied into the image |
| Helm detection | Not collected | Rancher `apps.catalog.cattle.io`, with workload labels as fallback |
| Inventory | 10 kinds, annotations dropped | Adds services, ingresses, CRDs, Helm releases, IPs, annotations, a cluster classification, and the fields of k8s-discovery.sh with an anonymised export |
| Runtime | VM with docker-compose | Docker or Podman on one VM in the airgap |

### Why Rancher catalog apps for Helm

Rancher keeps one `apps.catalog.cattle.io` object per Helm release in each downstream cluster, and that is what its own Installed Apps page reads. Reading these needs one extra list rule and no access to Secrets. The alternative, listing Helm release Secrets, needs list permission on all Secrets, and that permission also allows reading their contents. That is too broad for a read-only inventory user.

The collector also reads the `app.kubernetes.io/managed-by: Helm` label and the `meta.helm.sh/release-name` annotation on workloads. This costs no extra RBAC and covers any cluster where the catalog objects are missing.

## 2. Architecture

&#91;embedded content: architecture · 1 container, 2 mounts\]

The collector is the only part that calls Rancher. The web app only reads the SQLite file, so nothing a user does triggers a Rancher API call.

- **One process.** `inventory serve` starts the web server and a background collector thread with its own event loop.
- **No scheduler library.** A plain loop waits for the daily start time. A manual refresh starts a run at once if none is in progress.
- **Collector switch.** `COLLECTOR_ENABLED=false` turns the loop off, for a read-only copy of the app or for driving runs from cron with `collect-once`.
- **Restart behaviour.** After a restart the web app serves the last snapshot at once and the collector starts a run only if the last good one is older than STALE\_AFTER\_HOURS.

### Refresh and progress

Data is collected once a day, and on demand from a button. There are no live updates; pages always show the last finished collection.

- **Daily run.** Starts at `COLLECT_DAILY_AT`, in the container's time zone (UTC unless `TZ` is set).
- **Manual refresh.** A "Refresh now" button in the page header, for admins. It can refresh everything, one site, or one cluster from that cluster's page.
- **One run at a time.** While a run is active the button is replaced by the progress bar. A daily start that falls inside a manual run is skipped.
- **Progress bar.** Every page header shows, for example, "Refreshing: 37 of 124 clusters, 2 failed". It updates every 3 seconds by HTMX polling, only while a run is active.
- **Run page.** `/runs/{id}` lists every cluster with its status (pending, running, ok, failed, skipped), kinds done out of total, object count, duration and error. Failed clusters are listed first, with totals per site.
- **Cancel and retry.** An admin can cancel a run: no new clusters start, the ones in progress finish, and the run ends as `cancelled`. "Retry failed" on the run page reruns only the failed clusters.
- **During a run.** Pages keep showing the previous data. Each cluster switches to its new data when that cluster finishes.
- **Restart during a run.** On start, any run still marked `running` is set to `interrupted`. Clusters already collected keep their new data; the rest keep the old.
- **Staleness.** Every page header shows "Data as of" with the date and time of the last good run, and a warning when it is older than `STALE_AFTER_HOURS` (default 36). This replaces the 45-minute thresholds of the original plan, including the "unknown" state on the check page.

Progress is stored in two places. `runs` gains `trigger` (schedule or manual), `triggered_by`, `scope`, `clusters_total`, and the statuses `cancelled` and `interrupted`. A new table holds one row per cluster per run:

```sql
CREATE TABLE run_clusters (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    site        TEXT NOT NULL,
    cluster_id  TEXT NOT NULL,
    status      TEXT NOT NULL,     -- pending | running | ok | failed | skipped
    kinds_total INTEGER,
    kinds_done  INTEGER,
    objects     INTEGER,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (run_id, site, cluster_id)
);
```

Rows are kept for the last 30 runs. Trend charts get one point per cluster per day.

## 3. Configuration

Two files are required: `.env` holds settings and secrets, `sites.yaml` holds the Rancher URLs. Tokens live only in `.env`, so `sites.yaml` contains nothing secret and can be kept in git.

Folder on the airgap VM:

```
/opt/rancher-inventory/
├── .env                  # chmod 600
├── config/
│   ├── sites.yaml        # required
│   ├── ca/               # private CA bundles for Rancher
│   ├── tls/              # server.crt and server.key for the web app
│   ├── kinds.yaml        # optional, overrides the built-in list
│   └── rules.yaml        # optional, overrides classification and annotation rules
└── data/                 # inventory.db lives here
```

### `.env`

```
# Web
LISTEN_PORT=8443
TLS_CERT_FILE=/config/tls/server.crt
TLS_KEY_FILE=/config/tls/server.key
SESSION_SECRET=change-me-to-64-random-hex-chars
SESSION_HOURS=12

# First start only: creates the first admin if no users exist
ADMIN_USER=admin
ADMIN_PASSWORD=change-me

# Collector
COLLECTOR_ENABLED=true
COLLECT_DAILY_AT=02:00
STALE_AFTER_HOURS=36
CONCURRENCY_PER_SITE=4
REQUEST_TIMEOUT_S=30
METRICS_RETENTION_DAYS=365

# Paths inside the container
DATA_DIR=/data
SITES_FILE=/config/sites.yaml

# Rancher tokens, referenced by name from sites.yaml
RANCHER_SITE_1_TOKEN=token-abc12:xxxxxxxxxxxxxxxxxxxxxxxx
RANCHER_SITE_2_TOKEN=token-def34:xxxxxxxxxxxxxxxxxxxxxxxx
```

If `TLS_CERT_FILE` is empty the app serves plain HTTP, for use behind an existing reverse proxy.

Settings added for the shareable export:

```
# Shareable export (section 7)
EXPORT_SALT=change-me-and-keep-it
EXPORT_SENSITIVE_STRINGS=example.com,ExampleCorp
```

### `config/sites.yaml`

```yaml
sites:
  - name: rancher-site-1
    url: https://rancher-site-1.example.com
    token_env: RANCHER_SITE_1_TOKEN
    ca_file: /config/ca/rancher-site-1.pem   # optional
    token_expires: 2026-12-20                # for the rotation warning
  - name: rancher-site-2
    url: https://rancher-site-2.example.com
    token_env: RANCHER_SITE_2_TOKEN
    token_expires: 2026-12-20
```

### Config check command

`inventory check-config` runs inside the same image and is the first thing to run after loading it in the airgap. It reports, per site:

- URL reachable and TLS trusted with the given CA
- token valid, and days until `token_expires`
- number of clusters visible
- for one sample cluster, which kinds return 200 and which return 403, so RBAC gaps show before the first run

## 4. Packaging and airgap transfer

The image is built once on a connected machine, saved to one `.tar.gz` file, carried across, loaded and run. Nothing is downloaded at run time.

### What goes in the image

- Python 3.12 slim base, pinned by digest
- Python packages installed from wheels: `fastapi`, `uvicorn`, `httpx`, `jinja2`, `pydantic-settings`, `pyyaml`, `python-multipart`
- SQLite from the Python standard library, no database server
- Password hashing with `hashlib.scrypt` from the standard library, no extra package
- `htmx.min.js` and `chart.umd.min.js` committed under `inventory/web/static/`
- Default `kinds.yaml` and `rules.yaml` under `/app/defaults/`

### Dockerfile outline

```dockerfile
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml .
COPY inventory/ inventory/
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home inventory
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links /wheels rancher-inventory \
    && rm -rf /wheels
COPY config/defaults/ /app/defaults/
USER 10001
VOLUME /data
EXPOSE 8443
HEALTHCHECK --interval=1m CMD ["inventory", "healthcheck"]
ENTRYPOINT ["inventory"]
CMD ["serve"]
```

### On the connected machine

```bash
docker build --platform linux/amd64 -t rancher-inventory:1.0.0 .
docker save rancher-inventory:1.0.0 | gzip > rancher-inventory-1.0.0.tar.gz
sha256sum rancher-inventory-1.0.0.tar.gz > rancher-inventory-1.0.0.tar.gz.sha256
```

Before saving, run the image once and open every page with the browser's network tab open. Every request must go to the app's own host. Any request to another host is a dependency that will fail in the airgap.

### In the airgap

```bash
sha256sum -c rancher-inventory-1.0.0.tar.gz.sha256
docker load -i rancher-inventory-1.0.0.tar.gz

cd /opt/rancher-inventory
chown 10001 data

# 1. Check URLs, tokens and RBAC
docker run --rm --env-file .env -v $PWD/config:/config:ro \
  rancher-inventory:1.0.0 check-config

# 2. Run
docker run -d --name rancher-inventory --restart unless-stopped \
  --env-file .env \
  -v $PWD/config:/config:ro \
  -v $PWD/data:/data \
  -p 8443:8443 \
  --read-only --tmpfs /tmp --cap-drop ALL \
  rancher-inventory:1.0.0
```

Podman takes the same commands. On SELinux hosts add `:Z` to the two volume mounts.

### Commands in the image

| Command | Purpose |
| --- | --- |
| `serve` | Web app plus the collector loop (default) |
| `check-config` | Validate sites, tokens, CA files and RBAC |
| `collect-once` | One collection run, then exit |
| `user add`, `user passwd`, `user list`, `user disable` | Manage local users from the shell |
| `backup <file>` | Consistent copy of the database while the app runs |
| `healthcheck` | Exit 0 if the app answers and the last good run is newer than STALE\_AFTER\_HOURS |

### Upgrade and backup

1. Load the new image tar, stop and remove the container, start it again with the same two mounts.
2. On start the app compares `PRAGMA user_version` with its own schema version and applies any missing migration scripts in order.
3. Run `backup` daily from cron. Only users, presets and trend history cannot be re-collected; everything else returns within one run.

## 5. Data model on SQLite

The original tables stay, with five translations and a few side tables that replace the PostgreSQL GIN indexes. The database is one file, `/data/inventory.db`.

| PostgreSQL in the original plan | SQLite in this plan |
| --- | --- |
| `BIGSERIAL` | `INTEGER PRIMARY KEY` |
| `TIMESTAMPTZ` | `TEXT`, ISO 8601 in UTC |
| `JSONB` column | `TEXT` holding JSON, read with `json_extract` or `->>` |
| GIN index on labels, images in a JSON array | Side tables `resource_labels`, `resource_annotations`, `resource_images` with plain indexes |
| `DISTINCT ON` for latest metrics | `cluster_metrics_latest`, one row per cluster, overwritten each run |
| `collector` and `web_ro` roles | Query and check pages open the file read-only (`mode=ro`) |
| `statement_timeout = 10s` | A progress handler that aborts the query after 10 s |

Settings applied on every connection: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`. WAL lets the web pages read while the collector writes.

### Changed and new tables

```sql
CREATE TABLE resources (
    id            INTEGER PRIMARY KEY,
    site          TEXT NOT NULL,
    cluster_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    namespace     TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    data          TEXT NOT NULL DEFAULT '{}',   -- trimmed fields as JSON
    hash          TEXT NOT NULL,                -- hash of the trimmed object
    created_at    TEXT,
    last_seen_run INTEGER NOT NULL,
    UNIQUE (site, cluster_id, kind, namespace, name)
);
CREATE INDEX resources_kind_name ON resources (kind, name);

CREATE TABLE resource_labels (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (resource_id, key)
) WITHOUT ROWID;
CREATE INDEX resource_labels_kv ON resource_labels (key, value);

-- resource_annotations: same shape as resource_labels

CREATE TABLE resource_images (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    image       TEXT NOT NULL,
    PRIMARY KEY (resource_id, image)
) WITHOUT ROWID;
CREATE INDEX resource_images_image ON resource_images (image);

-- Every IP and hostname found anywhere, in one searchable place
CREATE TABLE addresses (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,   -- node-internal | node-external | cluster-ip |
                                 -- external-ip | lb | ingress-lb | ingress-host
    address     TEXT NOT NULL,   -- IP or DNS name
    ports       TEXT,            -- e.g. "443/TCP,80/TCP"
    PRIMARY KEY (resource_id, role, address)
) WITHOUT ROWID;
CREATE INDEX addresses_address ON addresses (address);

CREATE TABLE namespace_profile (
    site TEXT, cluster_id TEXT, namespace TEXT,
    is_system          INTEGER NOT NULL,
    workloads          INTEGER, statefulsets INTEGER,
    pvc_bound          INTEGER, pvc_bytes    INTEGER,
    hostpath_workloads INTEGER,
    helm_releases      INTEGER,
    lb_services        INTEGER, ingresses    INTEGER,
    custom_annotations INTEGER,
    PRIMARY KEY (site, cluster_id, namespace)
);

CREATE TABLE cluster_profile (
    site TEXT, cluster_id TEXT,
    class         TEXT NOT NULL,     -- see section 6.5
    stateful      INTEGER NOT NULL,  -- 0 or 1
    helm          INTEGER NOT NULL,
    custom_crds   INTEGER NOT NULL,
    hostpath      INTEGER NOT NULL,
    statefulsets  INTEGER, pvc_bound INTEGER, pvc_bytes INTEGER,
    helm_releases INTEGER, crd_groups TEXT,   -- JSON list of custom groups
    lb_services   INTEGER, ingresses INTEGER,
    network       TEXT,              -- JSON: cni, cluster_cidr, service_cidr
    updated_run   INTEGER,
    PRIMARY KEY (site, cluster_id)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL,   -- admin | viewer
    disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);
```

`runs`, `clusters`, `cluster_metrics` and `presets` keep their columns from the original plan, with the type translations above.

### Write rules for the collector

- One writer connection, one transaction per cluster and kind.
- For each object, compare the new `hash` with the stored one. If equal, only `last_seen_run` is updated. If different, the row and its side-table rows are rewritten. Most objects do not change between runs, so this keeps each run's write volume small.
- The stale-row rule is unchanged: rows are deleted only for a cluster and kind that was fully listed in this run.
- Profiles in `namespace_profile` and `cluster_profile` are rebuilt for a cluster at the end of its successful collection.

### Size

At 300 clusters, expect about 450,000 resource rows and a few million side-table rows, roughly 1 to 2 GB on disk before the discovery fields of sections 6.9 to 6.11, which add to both numbers. This is an estimate to confirm after the first full run. SQLite handles this size for a small team; the one constraint is a single writer, which the collector design already respects.

## 6. Extended inventory

Sections 6.1 to 6.7 give each cluster a profile: stateless or stateful, Helm releases, custom CRDs, IPs, load balancers, services and custom annotations. Sections 6.8 to 6.14 add the fields collected by `k8s-discovery.sh`. The default set needs no access to Secrets; six collectors that need broader rights are off by default.

### 6.1 New kinds in `kinds.yaml`

```yaml
  services:
    path: /api/v1/services
    scope: namespaced
    keep: [spec.type, spec.clusterIPs, spec.externalIPs, spec.loadBalancerIP,
           spec.externalName, spec.ports, status.loadBalancer.ingress]
    annotations: true
    addresses: true

  ingresses:
    path: /apis/networking.k8s.io/v1/ingresses
    scope: namespaced
    keep: [spec.ingressClassName, spec.rules, spec.tls, status.loadBalancer.ingress]
    annotations: true
    addresses: true

  customresourcedefinitions:
    path: /apis/apiextensions.k8s.io/v1/customresourcedefinitions
    scope: cluster
    keep: [spec.group, spec.names.kind, spec.scope, spec.versions,
           status.storedVersions, spec.conversion.strategy]
    # versions are trimmed to name, served, storage, deprecated; schemas are dropped
    annotations: true

  helmreleases:
    path: /apis/catalog.cattle.io/v1/apps
    scope: namespaced
    keep: [spec.name, spec.namespace, spec.version,
           spec.chart.metadata.name, spec.chart.metadata.version,
           spec.chart.metadata.appVersion,
           spec.info.status, spec.info.lastDeployed]
```

Helm values are never stored. The transform keeps only the listed fields, so `spec.values` and `spec.chart.values` are discarded in memory.

### 6.2 RBAC to add to the `inventory-reader` RoleTemplate

```yaml
  - apiGroups: [""]
    resources: [services]
    verbs: [get, list]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses]
    verbs: [get, list]
  - apiGroups: [apiextensions.k8s.io]
    resources: [customresourcedefinitions]
    verbs: [get, list]
  - apiGroups: [catalog.cattle.io]
    resources: [apps]
    verbs: [get, list]
```

Apply on every site before the kinds are enabled. A missing rule shows as 403 for that kind in `check-config` and does not fail the cluster.

### 6.3 Extra fields on existing kinds

| Kind | Added | Used for |
| --- | --- | --- |
| nodes | `status.addresses`, roles from `node-role.kubernetes.io/*` labels, `spec.podCIDR` | Node IPs, role counts |
| deployments, statefulsets, daemonsets | `data.pvc_claims` from pod template volumes, `data.hostpath` true or false, annotations | Linking workloads to storage, Helm fallback |
| statefulsets | `data.volume_claim_templates`: name, storage class, size | Stateful detail |
| namespaces | annotations, including `field.cattle.io/projectId` | Rancher project per namespace |
| persistentvolumes | `data.local` true when the PV is `local` or `hostPath` | State tied to one node |
| persistentvolumeclaims | annotations | Custom annotations |

### 6.4 IPs and load balancers

The transform writes one row to `addresses` for every IP or DNS name it finds:

| Role | Source |
| --- | --- |
| `node-internal`, `node-external` | Node `status.addresses` |
| `cluster-ip` | Service `spec.clusterIPs`, skipping headless services |
| `external-ip` | Service `spec.externalIPs` |
| `lb` | Service of type LoadBalancer: `status.loadBalancer.ingress` IP or hostname, with ports |
| `ingress-lb` | Ingress `status.loadBalancer.ingress` |
| `ingress-host` | Ingress `spec.rules[].host` and `spec.tls[].hosts` |

NodePort numbers stay in the service's `data` and are shown on the services view. Cluster and service CIDRs and the CNI are read from the `provisioning.cattle.io` cluster object where they are set, and stored in `cluster_profile.network`. Imported clusters leave this empty.

### 6.5 Annotations

Annotations were dropped in the original plan. Now they are kept for kinds marked `annotations: true`, filtered by `rules.yaml`:

```yaml
annotations:
  max_value_chars: 256
  system_domains:            # annotations under these are not "custom"
    - kubernetes.io
    - k8s.io
    - cattle.io
    - helm.sh
    - rke.cattle.io
    - projectcalico.org
  keep:                      # system annotations stored anyway
    - field.cattle.io/projectId
    - meta.helm.sh/release-name
    - meta.helm.sh/release-namespace
    - metallb.universe.tf
    - kube-vip.io
    - nginx.ingress.kubernetes.io
    - cert-manager.io
  drop:                      # never stored
    - kubectl.kubernetes.io/last-applied-configuration
```

A rule entry matches an annotation when it equals the full key, or when the key's domain equals the entry or ends with a dot plus the entry. So `kubernetes.io` covers `node.kubernetes.io/...`.

- An annotation whose domain is not under `system_domains` is **custom**. It is stored with `custom = 1` in `resource_annotations`, which has that one extra column compared with `resource_labels`.
- An annotation under `system_domains` is stored only if it matches `keep`.
- Values longer than 256 characters are cut. Annotations can carry sensitive text, so add any such key to `drop`.

This is a denylist rather than a plain allowlist: custom annotations cannot be listed in advance, so the rule lists what is *not* custom instead.

### 6.6 Classification

Each cluster gets four independent flags and one class label derived from them. Flags are computed from user namespaces only.

System namespaces are set in `rules.yaml` as name patterns. Defaults: `kube-*`, `cattle-*`, `fleet-*`, `local`, `longhorn-system`, `calico-system`, `tigera-operator`, `ingress-nginx`, `cert-manager`, `metallb-system`.

| Flag | True when |
| --- | --- |
| `stateful` | A user namespace has a StatefulSet, or a bound PVC |
| `helm` | A stateful namespace has a deployed Helm release, or a stateful workload carries the Helm managed-by label |
| `custom_crds` | The cluster has a CRD whose group is not in `system_crd_groups` |
| `hostpath` | A user workload mounts a `hostPath` volume, or a `local` PV exists |

Default `system_crd_groups`: `*.cattle.io`, `*.k8s.io`, `projectcalico.org`, `cilium.io`, `monitoring.coreos.com`, `longhorn.io`, `metallb.io`, `cert-manager.io`. Tune this list after the first run: a group present on every cluster is usually platform, not custom.

| Class | Rule |
| --- | --- |
| `empty` | No workloads in user namespaces |
| `stateless` | Workloads exist, `stateful` is false |
| `stateful` | `stateful` only |
| `stateful-helm` | `stateful` and `helm` |
| `stateful-crd` | `stateful` and `custom_crds` |
| `stateful-helm-crd` | `stateful`, `helm` and `custom_crds` |

`hostpath` is shown as a warning badge beside any class, because it marks state that no PVC reveals. The same flags, except `custom_crds`, are stored per namespace in `namespace_profile`, so the cluster page shows which namespaces make a cluster stateful.

### 6.7 Custom resource instances (opt-in)

The default collects which CRDs exist, not their instances. Counting instances for all CRDs would need wildcard read access, which also covers Secrets. To track a specific custom resource, add it to `kinds.yaml` with `metadata_only: true` and add one matching RBAC rule, for example for a database operator's cluster objects.

### 6.8 One daily run

All kinds are collected in the same run, once a day or on manual refresh (section 2). There is no split into fast and slow schedules.

A run makes about 35 list calls per cluster, plus one per extra page of results. With 100 or more clusters a run can take tens of minutes. That is fine for a daily refresh, so `CONCURRENCY_PER_SITE` defaults to 4 to keep the load on Rancher low.

No run-time target applies. The duration of each run is shown on the runs page, and the first full run in phase 2 gives the real number.

### 6.9 Workload and pod detail

Kinds marked `podspec: true` (deployments, statefulsets, daemonsets, jobs, cronjobs, replicationcontrollers) get these derived fields in `data`. The raw pod spec is not stored. Environment variables, commands and arguments are never stored.

| Group | Fields |
| --- | --- |
| Resources | containers, init containers; per pod: CPU and memory requests and limits, GPU; containers without requests, without limits, without probes |
| Security | privileged, hostNetwork, hostPID, hostPort count, added capabilities |
| Volumes | PVC claims, counts of hostPath, NFS and emptyDir volumes, inline CSI drivers. Paths and NFS servers are not stored |
| Scheduling | nodeSelector keys, affinity yes or no, topology spread yes or no, toleration keys, priority class |
| Other | owner apiVersion and kind (shows operator-owned workloads), non-default service account, update strategy, cron schedule, suspended |

Jobs owned by a CronJob are skipped, as in the script.

Pods gain `status.qosClass`, owner kind, restart count, summed requests and limits, and PVC claims. Pod usage comes from `metrics.k8s.io/v1beta1/pods` and is stored per namespace only, not per pod.

`namespace_profile` gains the script's namespace roll-up: pods, running pods, standalone pods, restarts, CPU and memory requested, limited and used, GPU, workload counts per kind, PVC count and size, RWX PVCs, LoadBalancer and NodePort services, ingresses, NetworkPolicies, quota and LimitRange present.

### 6.10 Storage, policy and network detail

| Kind | Added |
| --- | --- |
| persistentvolumes | Access modes, volume mode, reclaim policy, source type (`csi:<driver>`, nfs, hostPath, local, vsphereVolume, iscsi), pinned to a node yes or no |
| persistentvolumeclaims | Access modes, volume mode, volume name, data source kind, `mounted` (derived from pod PVC claims), `used_by` owner kinds |
| storageclasses | Binding mode, expansion allowed, default class, parameter keys (not values) |
| csidrivers, volumesnapshots | New. Driver names and modes; snapshot count. A 404 for snapshots means not installed, not an error |
| services | `externalTrafficPolicy`, `loadBalancerClass`, has selector, IP families, session affinity |
| ingresses | Default backend, legacy `kubernetes.io/ingress.class` annotation (added to the annotation `keep` list), nginx annotation categories and a snippet flag |
| ingressclasses, priorityclasses | New. Controller, default flag; value, global default |
| poddisruptionbudgets | New. minAvailable, maxUnavailable, expected pods, disruptions allowed, blocks drain now |
| horizontalpodautoscalers | New. Target kind, min, max, metric types |
| resourcequotas, limitranges | New. Hard CPU, memory and storage; LimitRange presence |
| networkpolicies | New. Policy types, namespace-wide yes or no, number of ipBlock peers |
| nodes | Provider from `spec.providerID` |
| clusters | `cluster_uid` = UID of the kube-system namespace. The same UID under two sites means one cluster is registered twice |

### 6.11 Platform kinds

- **CRD details.** CRDs are fetched in full and trimmed to group, kind, scope, versions (served, storage, deprecated), stored versions and conversion strategy. With one run a day, the cheaper names-only fetch is not needed.
- **Webhooks.** Mutating and validating configurations: failure policies, service namespaces, URL-based yes or no, rules, intercepts-all yes or no, timeouts, namespace selector present, Helm release.
- **APIServices.** Service namespace and Available status, for aggregated APIs only.
- **RBAC.** ClusterRoles (rule count, aggregated, Helm-managed), ClusterRoleBindings and RoleBindings (role, subject kinds and names), Role count, ServiceAccount count.
- **API versions served.** From the `/api` and `/apis` discovery endpoints.
- **API versions in use.** Taken from the `last-applied-configuration` annotation of objects already fetched. The transform keeps only apiVersion and kind, then drops the annotation, so this costs no extra requests.

Rancher creates many ClusterRoles, bindings and its own webhook on every downstream cluster. These are flagged `rancher_generated` and left out of the "custom roles" and "non-system cluster-admin bindings" counts. Subjects appear as Rancher user IDs and group principals, not directory names; they are stored as they are.

### 6.12 Opt-in collectors

Six collectors are off by default because each needs rights beyond a plain read-only inventory. Each is switched on separately in `rules.yaml` and is listed on the overview page when active.

| Collector | Adds | Extra RBAC | Risk |
| --- | --- | --- | --- |
| `cr_counts` | Instance count per CRD, for the API groups you list | get and list on those groups; `*` only if you accept the wildcard | A wildcard also covers Secrets. One call per CRD per cluster |
| `pvc_usage` | Used bytes per PVC from kubelet stats | get on `nodes/proxy` | Reaches the kubelet API, which has been reported as usable for command execution. One call per node |
| `helm_secrets` | Kinds and API versions of the objects in each release | get and list on Secrets | The payload holds Helm values. It is decoded in memory; only chart data and kind pairs are kept |
| `deprecated_api_metrics` | Deprecated APIs requested, from API server `/metrics` | Non-resource URL `/metrics` | Large response, one API server only, includes Rancher's own agent requests |
| `secret_types` | Count of Secrets per type | list on Secrets | Grants read of all Secrets, though only the type column is requested |
| `nginx_config_keys` | Keys set in ingress-nginx ConfigMaps | get on ConfigMaps (already granted) | Reads ConfigMap data in memory; stores keys only |

```yaml
optional_collectors:
  cr_counts:              {enabled: false, groups: []}
  pvc_usage:              {enabled: false}
  helm_secrets:           {enabled: false}
  deprecated_api_metrics: {enabled: false}
  secret_types:           {enabled: false}
  nginx_config_keys:      {enabled: false}
```

Two measures keep the cost down. `cr_counts` asks for one item per CRD and reads the total from `remainingItemCount`, so it gets a count without listing every object; it does not report how many namespaces hold instances. `helm_secrets` fetches a release Secret only when its revision differs from the stored one.

With every opt-in off, these script columns stay empty: CRD instances, PVC used bytes, Helm objects and kinds, deprecated API hits, secret types, nginx config keys.

### 6.13 RBAC for 6.9 to 6.11

All rules use `verbs: [get, list]`.

```yaml
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
  - apiGroups: [""]
    resources: [replicationcontrollers, resourcequotas, limitranges, serviceaccounts]
  - apiGroups: [policy]
    resources: [poddisruptionbudgets]
  - apiGroups: [autoscaling]
    resources: [horizontalpodautoscalers]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies, ingressclasses]
  - apiGroups: [storage.k8s.io]
    resources: [csidrivers]
  - apiGroups: [snapshot.storage.k8s.io]
    resources: [volumesnapshots]
  - apiGroups: [scheduling.k8s.io]
    resources: [priorityclasses]
  - apiGroups: [admissionregistration.k8s.io]
    resources: [mutatingwebhookconfigurations, validatingwebhookconfigurations]
  - apiGroups: [apiregistration.k8s.io]
    resources: [apiservices]
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [clusterroles, clusterrolebindings, roles, rolebindings]
  - apiGroups: [metrics.k8s.io]
    resources: [pods]
```

The opt-in rules from 6.12 go in a second RoleTemplate, `inventory-reader-extended`, so they can be granted per site and removed without touching the base role.

### 6.14 Cluster summary and complexity flags

`cluster_profile` gains the script's summary columns and a `complexity_flags` list, computed after each run:

| Flag | True when |
| --- | --- |
| `k8s<1.32` | Kubernetes minor version below 32 |
| `stateful` | StatefulSets or PVCs in user namespaces |
| `rwx` | Any ReadWriteMany PVC |
| `nginx-snippets` | Any ingress with a snippet annotation |
| `many-crds` | More than 30 CRDs in use (needs `cr_counts`) |
| `deprecated-apis` | Any deprecated API hit (needs `deprecated_api_metrics`) |
| `privileged/hostpath` | Any user workload that is privileged or mounts hostPath |
| `gpu` | Any node with GPU capacity |
| `operators` | Any workload owned by a custom resource |
| `intercept-all-webhooks` | Any webhook with a wildcard rule |

The version threshold and the CRD count are settings in `rules.yaml`.

## 7. Web app pages

The original routes stay. Inventory, discovery, export and login pages are added, and three existing pages gain content. Every table has a CSV export.

| Route | Status | Purpose |
| --- | --- | --- |
| `/login`, `/logout` | New | Local login |
| `/inventory` | New | One row per cluster with its class, flags and key counts |
| `/network` | New | Search any IP or hostname; lists of load balancers, ingress hosts, node IPs |
| `/helm` | New | Helm releases across clusters, with chart version spread |
| `/crds` | New | CRD groups and how many clusters have each; custom-only toggle |
| `/annotations` | New | Custom annotation keys with counts, drill down to objects |
| `/admin/users` | New | Add, disable and reset users (admin only) |
| `/` | Changed | Adds a card with clusters per class |
| `/clusters/{site}/{cluster_id}` | Changed | Adds profile, namespace table, services, ingresses, Helm releases, CRDs |
| `/query` | Changed | New filters, listed below |
| `/check`, `/clusters`, `/export/*`, `/api/*`, `/presets`, `/healthz` | Unchanged | As in the original plan |

### `/inventory`

This is the easy overview page. Columns: site, cluster, class badge, flag badges, Kubernetes version, nodes, StatefulSets, bound PVCs with total capacity, Helm releases, custom CRD groups, LoadBalancer services, ingresses, CNI and CIDRs. Filters: site, class, any flag, cluster label. Clicking a row opens the cluster page, where the namespace table shows which namespaces carry the state.

### `/network`

One search box answers "which cluster owns 10.20.30.40" or "where is app.example.com served". It searches the `addresses` table:

```sql
SELECT r.site, c.name AS cluster, r.kind, r.namespace, r.name,
       a.role, a.address, a.ports
FROM addresses a
JOIN resources r ON r.id = a.resource_id
JOIN clusters  c ON c.site = r.site AND c.cluster_id = r.cluster_id
WHERE a.address = :q OR a.address LIKE :q || '%'
ORDER BY r.site, c.name;
```

Tabs below the search: load balancers (with their annotations), ingress hosts, node IPs, NodePort and external IP services. A duplicates view lists any load balancer IP or ingress host that appears in more than one cluster.

### `/helm`, `/crds`, `/annotations`

- `/helm`: one row per chart with the number of clusters per chart version, so version drift is visible. Drill down to releases with status and last deployed time.
- `/crds`: one row per API group with cluster count. This is also the page used to tune `system_crd_groups`.
- `/annotations`: one row per custom annotation key with object and cluster counts. Drill down to the objects and values.

### New filters on `/query`

| Filter | Example |
| --- | --- |
| Annotation key, or key=value | `team.example.com/owner=payments` |
| Service type | `LoadBalancer` |
| Address equals or starts with | `10.20.` |
| Helm chart, optional version | `postgresql`, `15.5.0` |
| CRD group | `acid.zalan.do` |
| Cluster class or flag | `stateful-helm`, `hostpath` |

The filter builder keeps the same safety rules: whitelisted filter types only, values always passed as parameters, 5,000 rows on screen, read-only connection, 10 s limit.

### Pages for the discovery fields

| Route | Purpose |
| --- | --- |
| `/workloads` | Workloads across clusters with filters for privileged, hostNetwork, hostPath, no requests, no limits, no probes, operator-owned, GPU |
| `/storage` | PVCs and PVs: RWX, block mode, unmounted, source types, storage classes, snapshots, used bytes when `pvc_usage` is on |
| `/platform` | Webhooks, APIServices, custom ClusterRoles, non-system cluster-admin bindings, API versions served and in use, deprecated API hits when collected |
| `/namespaces` | The namespace roll-up from 6.9 across all clusters, sortable by requests, usage, restarts |
| `/export/discovery` | The shareable export below (admin only) |

`/inventory` gains the complexity flags from 6.14 as badges and as a filter.

Run pages: `/runs` lists recent runs and `/runs/{id}` shows per-cluster progress. `POST /runs` (refresh) and `POST /runs/{id}/cancel` are for admins only. Section 2 describes the behaviour.

### Shareable export

The export produces the same per-cluster CSV files and column names as `k8s-discovery.sh` 1.0.0, plus `fleet_summary.csv`, so anything that already reads the script's output keeps working. It is built from the database; it makes no Rancher calls.

- **How to run.** `/export/discovery` in the UI for admins, or `inventory export-discovery --out /data/export` from the shell. Scope can be all clusters, one site, or a list of clusters. Output is one `tar.gz`.
- **Hashing.** Names of clusters, nodes, namespaces, workloads, PVCs, PVs, services, ingresses, Helm releases, registry projects and RBAC groups are replaced by `<type>-<first 10 hex of SHA-256(salt|type|value)>`. This is the script's formula, so the same `EXPORT_SALT` gives the same hashes as the script.
- **Left readable.** System namespaces, public registries, and label and annotation keys under well-known prefixes. Unknown key prefixes are hashed.
- **Never exported.** IPs, hostnames, ingress hosts, URLs, annotation and label values, NFS paths, private registry hosts. The `addresses` table is not part of the export.
- **Sensitive strings.** Any value containing a string from `EXPORT_SENSITIVE_STRINGS` is hashed wherever it appears.
- **Strict mode.** An option that also hashes image names, chart names, CRD names and groups, and StorageClass names.
- **Self-check.** Before the file is offered, the output is scanned for IPv4-like strings and for the sensitive strings. A hit blocks the download and shows the file and column. The script only warns here; blocking is safer for a web download.
- **Name map.** The hash-to-name map is kept in the database table `export_name_map`. Admins can download it separately. It is never inside the bundle.

The Tanzu files of the script are not produced. Columns that depend on an opt-in collector are empty unless that collector is on.

## 8. Login and security

Users are local accounts stored in the SQLite file; there is no dependency on SSO or a proxy.

### Accounts

- On first start, if the `users` table is empty, the app creates one admin from `ADMIN_USER` and `ADMIN_PASSWORD`. Remove `ADMIN_PASSWORD` from `.env` afterwards.
- Two roles: `admin` manages users and sees collector errors; `viewer` reads everything and can save presets.
- Passwords are hashed with scrypt and a per-user salt. Minimum length 12.
- Users are managed on `/admin/users` or with `inventory user ...` from the shell, which also recovers a lost admin password.
- Every user sees every cluster on every site, which is the service user's view. This is the same as the original plan.

### Sessions

- Login sets a random 32-byte token in a cookie marked `HttpOnly`, `Secure` and `SameSite=Lax`. Only its hash is stored in `sessions`.
- Sessions expire after `SESSION_HOURS`. Logout deletes the row.
- Forms that change data (presets, users) carry a CSRF token derived from the session and `SESSION_SECRET`.
- Five failed logins for one username lock it for 15 minutes.
- Every route needs a session except `/login`, `/static` and `/healthz`. Without a session, `/healthz` returns only ok or not ok.

### Secrets and container

- Rancher tokens exist only as environment variables in the container. They are never logged, never shown in the UI, and masked in `check-config` output.
- `.env` is `chmod 600` and owned by the account that starts the container.
- TLS is served by the app itself from the mounted certificate and key.
- The container runs as uid 10001 with a read-only root filesystem and all capabilities dropped. Only `/data` is writable.
- ConfigMap values and Helm values are never stored. Secrets are never requested.
- Logins, failed logins and CSV exports are written to the container log with the username.

## 9. Build phases

Ten phases, each ending in something that can be loaded and checked in the airgap. Packaging comes first so the transfer path is proven before any feature depends on it.

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 1. Skeleton and image | Project layout, settings, SQLite schema with migrations, CLI, Dockerfile, `check-config` | The image tar loads on a host with no internet; `check-config` passes for every site and lists RBAC gaps |
| 2. Collector core | Clusters, nodes, `cluster_metrics`, daily schedule, run and per-cluster progress tracking, hash-based upserts | A full run over all sites completes and its duration is recorded; cluster count matches the Rancher dashboard on each site; a container restart mid-run leaves the run `interrupted` and the data intact |
| 3. Login and first pages | Local users, sessions, overview page, clusters page, refresh button, progress bar, run page, cancel and retry | No page opens without login; the progress bar and run page follow a manual refresh across all clusters; totals match a manual check on 3 clusters |
| 4. Core kinds | The 10 original kinds plus services, ingresses, CRDs, Helm releases; `addresses`; annotations; resource check page and presets | A preset matches a manual check on 5 clusters; unreachable clusters show as unknown |
| 5. Inventory views | Classification, `/inventory`, `/network`, `/helm`, `/crds`, `/annotations`, extended cluster page | Class is correct on one hand-picked cluster per class; IP search finds a known load balancer IP; Helm list matches `helm list -A` on 3 clusters |
| 6. Workload and storage detail | Pod spec fields, pod metrics, jobs, cronjobs, PDBs, HPAs, storage detail, namespace roll-up, `/workloads`, `/storage`, `/namespaces` | Namespace totals match the script's `namespaces.csv` on 2 clusters run with `--clear-names` |
| 7. Platform | CRD details, webhooks, APIServices, RBAC with the Rancher filter, API versions, complexity flags, `/platform` | Counts match the script's output on 2 clusters |
| 8. Query and exports | Query page with all filters, CSV export, anonymised discovery export | Queries return the right clusters; export headers equal the script's; self-check finds no IP, hostname or sensitive string |
| 9. Opt-in collectors | The six collectors of 6.12, each off by default, with `inventory-reader-extended` | Each is tested on one site with its extra RBAC; switching it off stops its calls |
| 10. Hardening and rollout | TLS, token expiry warning, backup command, upgrade path | Restore from backup tested once; upgrade from 1.0.0 to 1.0.1 keeps users, presets and trends |

### Project layout changes

- Added: `Dockerfile` at the root, `inventory/cli.py`, `inventory/db/migrations/`, `inventory/collector/addresses.py`, `inventory/collector/classify.py`, `inventory/web/auth.py`, `config/defaults/kinds.yaml`, `config/defaults/rules.yaml`.
- Removed: `deploy/docker-compose.yml`, `deploy/oauth2-proxy.cfg`, the PostgreSQL role setup.
- Changed: `db/repo.py` and `web/queries.py` are rewritten for SQLite; `transform.py` gains annotations, volumes and addresses.

### Tests

- Unit: unit parsing, transform trimming, annotation rule matching, address extraction, classification rules with one fixture per class, filter builder rejecting non-whitelisted fields, password hashing.
- Integration: collector against one small test cluster, comparing row counts with `kubectl get ... -A --no-headers | wc -l` for each kind.
- Failure: one site with a wrong token and one disconnected cluster; the run ends as `partial`, errors are recorded, other data is intact.
- Airgap: the phase 1 check is repeated for every release, on a host with no route to the internet.

## 10. Verify before starting

The first three items decide whether the Helm design holds as written. The catalog app behaviour and field names come from general Rancher knowledge and were not tested against your Rancher version.

- [ ] On one downstream cluster, `kubectl get apps.catalog.cattle.io -A` lists the same releases as `helm list -A`, including releases installed outside Rancher.
- [ ] `kubectl get apps.catalog.cattle.io -n <ns> <name> -o yaml` shows the field paths used in section 6.1 (`spec.chart.metadata.*`, `spec.info.status`).
- [ ] After the RoleTemplate change, the service user can list services, ingresses, CRDs and catalog apps through `/k8s/clusters/<id>/`.
- [ ] CPU architecture of the airgap VM (amd64 or arm64), and whether it runs Docker or Podman.
- [ ] A TLS certificate and key for the web app from the internal CA, and the CA bundle for each Rancher site.
- [ ] Number of sites and clusters, to confirm the sizing in section 5.
- [ ] The team accepts that every user sees every cluster.
- [ ] The default system namespace patterns and system CRD groups in section 6.6 fit your platform.

If the first check fails, Helm detection falls back to workload labels only. Classification still works, but the `/helm` page loses chart versions for releases without labelled workloads.

Added with the discovery fields:

- [ ] Which labels or name patterns mark Rancher-generated ClusterRoles and bindings on a downstream cluster, for the `rancher_generated` filter in 6.11.
- [ ] Whether `apps.catalog.cattle.io` objects list the resources of a release. If they do, object kinds per release need no Secret access and `helm_secrets` can stay off.
- [ ] Whether a RoleTemplate rule with `nonResourceURLs: [/metrics]` works through the Rancher proxy. Only needed for `deprecated_api_metrics`.
- [ ] Security sign-off for each opt-in collector before it is enabled, in particular `nodes/proxy` and Secret access.
- [ ] The export salt: reuse the salt from earlier script runs if old and new output must be comparable.
