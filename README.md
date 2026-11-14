# rancher-inventory-webapp

A web app that keeps a daily inventory of every downstream cluster managed by one or more Rancher servers. One container image runs the collector, the web app and an embedded SQLite database. It is built to run in an airgap.

The full design is in [Rancher Inventory Tool Implementation Plan (single image, airgap).md](<Rancher Inventory Tool Implementation Plan (single image, airgap).md>).

Status: plans 1 and 2 are built and tested on the dev Mac against the dev Rancher, in Docker, including a restore and an upgrade drill. The `linux/amd64` image is built for the airgap; the install on the airgap host itself is not done yet (see [Airgap install](#airgap-install)).

## Contents

1. [What you need](#what-you-need)
2. [Rancher setup (per site)](#rancher-setup-per-site)
3. [Files on the host](#files-on-the-host)
4. [The `.env` file](#the-env-file)
5. [The `config/sites.yaml` file](#the-configsitesyaml-file)
6. [Check and run](#check-and-run)
7. [Airgap install](#airgap-install)
8. [Backup, restore and upgrade](#backup-restore-and-upgrade)
9. [Discovery export](#discovery-export)
10. [Migration views](#migration-views)
11. [Rules for networking and databases](#rules-for-networking-and-databases)
12. [Known behaviour](#known-behaviour)
13. [Dev cluster notes](#dev-cluster-notes)
14. [Run on the dev Mac](#run-on-the-dev-mac)

## What you need

- One VM with Docker or Podman. Note its CPU architecture (amd64 or arm64).
- Network access from that VM to the Rancher URL of every site, on port 443.
- Admin access to each Rancher server, to create the service user and role.
- A TLS certificate and key for the web app, if it serves HTTPS itself.
- The CA bundle of each Rancher server, if its certificate is not from a public CA.

## Rancher setup (per site)

Do these steps on every Rancher server ("site") you want in the inventory. The app only reads. It never needs Secrets, and it never writes to Rancher.

### Step 1. Create the service user

In the Rancher UI:

1. Go to **Users & Authentication > Users > Create**.
2. Username: `inventory-reader`. Set a long random password and store it in your password manager.
3. Global Permissions: select **User-Base** only. Clear every other option.
4. Click **Create**. Note the user ID shown in the list (looks like `u-abc12`).

### Step 2. Create the cluster RoleTemplate `inventory-reader`

This role gives read access to the kinds the collector lists. It holds no Secret access.

Option A, the Rancher UI:

1. Go to **Users & Authentication > Role Templates > Cluster > Create**.
2. Name: `inventory-reader`.
3. Add each rule from the YAML below, with verbs `get` and `list`.

Option B, `kubectl` against the Rancher **local** cluster (the one Rancher runs on). Save this as `inventory-reader.yaml` and run `kubectl apply -f inventory-reader.yaml`:

```yaml
apiVersion: management.cattle.io/v3
kind: RoleTemplate
metadata:
  name: inventory-reader
displayName: inventory-reader
context: cluster
rules:
  # Core kinds
  - apiGroups: [""]
    resources: [nodes, namespaces, pods, persistentvolumes, persistentvolumeclaims,
                configmaps, services, replicationcontrollers, resourcequotas,
                limitranges, serviceaccounts]
    verbs: [get, list]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets]
    verbs: [get, list]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list]
  - apiGroups: [storage.k8s.io]
    resources: [storageclasses, csidrivers]
    verbs: [get, list]
  - apiGroups: [snapshot.storage.k8s.io]
    resources: [volumesnapshots]
    verbs: [get, list]
  # Network
  - apiGroups: [networking.k8s.io]
    resources: [ingresses, ingressclasses, networkpolicies]
    verbs: [get, list]
  # Platform
  - apiGroups: [apiextensions.k8s.io]
    resources: [customresourcedefinitions]
    verbs: [get, list]
  - apiGroups: [catalog.cattle.io]
    resources: [apps]
    verbs: [get, list]
  - apiGroups: [policy]
    resources: [poddisruptionbudgets]
    verbs: [get, list]
  - apiGroups: [autoscaling]
    resources: [horizontalpodautoscalers]
    verbs: [get, list]
  - apiGroups: [scheduling.k8s.io]
    resources: [priorityclasses]
    verbs: [get, list]
  - apiGroups: [admissionregistration.k8s.io]
    resources: [mutatingwebhookconfigurations, validatingwebhookconfigurations]
    verbs: [get, list]
  - apiGroups: [apiregistration.k8s.io]
    resources: [apiservices]
    verbs: [get, list]
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [clusterroles, clusterrolebindings, roles, rolebindings]
    verbs: [get, list]
  # Usage metrics (needs metrics-server on the cluster)
  - apiGroups: [metrics.k8s.io]
    resources: [nodes, pods]
    verbs: [get, list]
```

Notes:

- `configmaps` is in the list because the plan treats ConfigMap read as part of the base role. Remove it if your security team does not accept it. Only the opt-in `nginx_config_keys` collector reads ConfigMap data.
- The opt-in collectors (spec section 6.12) need a second RoleTemplate, `inventory-reader-extended`, in [deploy/inventory-reader-extended.yaml](deploy/inventory-reader-extended.yaml). Keep only the blocks of the collectors you switch on, and do not create it until each has security sign-off. All collectors are off by default in `rules.yaml` (`optional_collectors`) and make no calls when off. A 401 or 403 from one of them is logged and skipped.

### Step 3. Bind the role on every downstream cluster

The user only sees clusters where it has this role. Repeat for each downstream cluster, and for every new cluster added later.

Option A, the Rancher UI:

1. Open the cluster, go to **Cluster > Cluster Members > Add**.
2. Select the user `inventory-reader`.
3. Cluster Permissions: **Custom**, then tick **inventory-reader** only.
4. Click **Create**.

Option B, `kubectl` against the Rancher local cluster. Replace `c-xxxxx` with the cluster ID (from `kubectl get clusters.management.cattle.io`) and `u-abc12` with the user ID from step 1:

```yaml
apiVersion: management.cattle.io/v3
kind: ClusterRoleTemplateBinding
metadata:
  name: inventory-reader-binding
  namespace: c-xxxxx
clusterName: c-xxxxx
roleTemplateName: inventory-reader
userName: u-abc12
```

Do not bind it on the `local` cluster unless you want Rancher's own cluster in the inventory.

### Step 4. Allow reading cluster network settings (optional)

The machine pools (with their labels), the CNI and the cluster CIDRs (plan section 6.4) are read from the `provisioning.cattle.io` cluster objects on the Rancher local cluster. This step is optional. Without it:

- machine pools show "not collected" on the cluster page;
- the CNI is guessed from the cluster's DaemonSets (see `cni_daemonsets` in [Rules for networking and databases](#rules-for-networking-and-databases));
- pod ranges come from the node `podCIDR` values.

With this step, custom and imported clusters (which have no machine pools) show pools as "none". Without it, every cluster shows "not collected".

```yaml
apiVersion: management.cattle.io/v3
kind: GlobalRole
metadata:
  name: inventory-provisioning-reader
displayName: inventory-provisioning-reader
rules:
  - apiGroups: [provisioning.cattle.io]
    resources: [clusters]
    verbs: [get, list]
---
apiVersion: management.cattle.io/v3
kind: GlobalRoleBinding
metadata:
  name: inventory-provisioning-reader-binding
globalRoleName: inventory-provisioning-reader
userName: u-abc12
```

### Step 5. Create the API token

1. Log out, then log in to Rancher as `inventory-reader`.
2. Go to the user menu (top right) **> Account & API Keys > Create API Key**.
3. Description: `rancher-inventory`. Scope: **No Scope** (a cluster-scoped token cannot reach other clusters).
4. Expires: pick a date. Rancher may cap this with the `auth-token-max-ttl-minutes` setting.
5. Click **Create** and copy the **Bearer Token** (looks like `token-abc12:xxxxxxxx`). It is shown only once.
6. Note the expiry date. It goes into `sites.yaml` as `token_expires`.

### Step 6. Test the token

From the VM that will run the app:

```bash
export TOKEN='token-abc12:xxxxxxxx'
RANCHER=https://rancher-site-1.example.com

# 1. Token works, lists visible clusters
curl -s -H "Authorization: Bearer $TOKEN" "$RANCHER/v3/clusters" | grep -o '"id":"c-[^"]*"'

# 2. RBAC works through the Rancher proxy (replace c-xxxxx)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  "$RANCHER/k8s/clusters/c-xxxxx/apis/catalog.cattle.io/v1/apps"

# 3. No Secret access (expect 403)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  "$RANCHER/k8s/clusters/c-xxxxx/api/v1/secrets"
```

Add `--cacert /path/to/ca.pem` if Rancher uses a private CA.

## Files on the host

```
/opt/rancher-inventory/
├── .env                  # settings and secrets, chmod 600
├── config/
│   ├── sites.yaml        # required: Rancher URLs, no secrets
│   ├── ca/               # private CA bundles for Rancher (optional)
│   ├── tls/              # server.crt and server.key for the web app (optional)
│   ├── kinds.yaml        # optional, overrides the built-in list
│   └── rules.yaml        # optional, overrides classification and annotation rules
└── data/                 # inventory.db lives here, owned by uid 10001
```

```bash
sudo mkdir -p /opt/rancher-inventory/{config/ca,config/tls,data}
sudo chown 10001 /opt/rancher-inventory/data
sudo chmod 600 /opt/rancher-inventory/.env
```

`.env` is in `.gitignore`. Never commit it.

## The `.env` file

Copy this, then change every value marked **required**.

```bash
# ---- Web ----
LISTEN_PORT=8443
# Leave both empty to serve plain HTTP behind a reverse proxy
TLS_CERT_FILE=/config/tls/server.crt
TLS_KEY_FILE=/config/tls/server.key
# Required. Generate with: openssl rand -hex 32
SESSION_SECRET=
SESSION_HOURS=12
# Keep true. Set false only for plain-HTTP dev on localhost
COOKIE_SECURE=true

# ---- First admin ----
# Used only on first start, when no users exist. Remove ADMIN_PASSWORD after.
ADMIN_USER=admin
# Required on first start. Minimum 12 characters.
ADMIN_PASSWORD=

# ---- Collector ----
COLLECTOR_ENABLED=true
COLLECT_DAILY_AT=02:00          # 24h time in the container's time zone
TZ=UTC                          # optional, e.g. Asia/Singapore
STALE_AFTER_HOURS=36
CONCURRENCY_PER_SITE=4
REQUEST_TIMEOUT_S=30
METRICS_RETENTION_DAYS=365

# ---- Paths inside the container (leave as is) ----
DATA_DIR=/data
SITES_FILE=/config/sites.yaml

# ---- Rancher tokens ----
# Required. One line per site. The name must match token_env in sites.yaml.
RANCHER_SITE_1_TOKEN=token-abc12:xxxxxxxxxxxxxxxxxxxxxxxx
RANCHER_SITE_2_TOKEN=token-def34:xxxxxxxxxxxxxxxxxxxxxxxx

# ---- Shareable export ----
# Required before using the export. Reuse the salt from earlier
# k8s-discovery.sh runs if old and new output must match.
EXPORT_SALT=
# Comma-separated strings to hash wherever they appear (domains, company names)
EXPORT_SENSITIVE_STRINGS=example.com,ExampleCorp
```

| Variable | Required | What to put |
| --- | --- | --- |
| `SESSION_SECRET` | Yes | Output of `openssl rand -hex 32` |
| `ADMIN_PASSWORD` | First start | A password of 12 or more characters. Remove the line after the first start |
| `RANCHER_<NAME>_TOKEN` | Yes, per site | The bearer token from Rancher setup step 5 |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | No | Paths inside the container. Empty means plain HTTP |
| `COOKIE_SECURE` | No | Default `true`. Set `false` only for plain-HTTP dev on localhost |
| `COLLECT_DAILY_AT` | No | Daily start time, default `02:00` |
| `EXPORT_SALT` | For export | A long random string. Keep it; changing it changes every hash |
| `EXPORT_SENSITIVE_STRINGS` | For export | Your internal domains and company names |

## The `config/sites.yaml` file

One entry per Rancher server. This file has no secrets and can be kept in git.

```yaml
sites:
  - name: rancher-site-1                       # short name shown in the UI
    url: https://rancher-site-1.example.com    # Rancher URL, no trailing slash
    token_env: RANCHER_SITE_1_TOKEN            # name of the variable in .env
    ca_file: /config/ca/rancher-site-1.pem     # optional, for a private CA
    token_expires: 2026-12-20                  # from Rancher setup step 5
  - name: rancher-site-2
    url: https://rancher-site-2.example.com
    token_env: RANCHER_SITE_2_TOKEN
    token_expires: 2026-12-20
```

To get a Rancher server's CA bundle:

```bash
openssl s_client -connect rancher-site-1.example.com:443 -showcerts </dev/null \
  | awk '/BEGIN CERT/,/END CERT/' > config/ca/rancher-site-1.pem
```

Check that the file holds the CA certificate, not only the server certificate.

## Check and run

```bash
cd /opt/rancher-inventory

# 1. Check URLs, tokens, CA files and RBAC for every site
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

`check-config` reports for each site: URL reachable and TLS trusted, token valid and days to expiry, number of clusters visible, and which kinds return 403 on one sample cluster. Fix any 403 by adding the missing rule to the RoleTemplate.

Podman takes the same commands. On SELinux hosts add `:Z` to both volume mounts.

## Airgap install

The image tar is built on a connected machine and carried across. Nothing is downloaded on the airgap host.

### On the connected machine

The build installs exactly the package versions in `uv.lock`, checked by hash. Build for the CPU architecture of the airgap VM (`linux/amd64` here; use `linux/arm64` if `uname -m` on the VM prints `aarch64`).

```bash
docker buildx build --platform linux/amd64 -t rancher-inventory:1.0.0 --load .
mkdir -p dist
docker save rancher-inventory:1.0.0 | gzip > dist/rancher-inventory-1.0.0.tar.gz
(cd dist && shasum -a 256 rancher-inventory-1.0.0.tar.gz > rancher-inventory-1.0.0.tar.gz.sha256)
```

Carry across: the tar, its `.sha256` file, `config/sites.yaml`, any CA and TLS files, and `deploy/` if you use the extended role. Do not carry `.env`; write it on the airgap host.

### On the airgap host

```bash
# 0. Confirm there is no route to the internet (this must fail)
curl -m 5 https://pypi.org

# 1. Verify and load the image
sha256sum -c rancher-inventory-1.0.0.tar.gz.sha256
docker load < rancher-inventory-1.0.0.tar.gz

# 2. Folders and .env as in "Files on the host" and "The .env file"
sudo chown 10001 /opt/rancher-inventory/data

# 3. Check every site, then run (commands in "Check and run")
```

Check after the first run: `check-config` passes for every site and lists RBAC gaps; the cluster count on `/clusters` matches each Rancher dashboard; every page loads with no request to another host.

## Backup, restore and upgrade

Only users, presets and trend history cannot be collected again. Back them up daily, for example from cron:

```bash
docker exec rancher-inventory inventory backup /data/backup-$(date +%F).db
```

`backup` copies the database while the app runs and checks the copy (`PRAGMA integrity_check`). It refuses to overwrite a file.

Restore:

```bash
docker stop rancher-inventory
cd /opt/rancher-inventory/data
mv inventory.db inventory.db.old && rm -f inventory.db-wal inventory.db-shm
cp backup-YYYY-MM-DD.db inventory.db && chown 10001 inventory.db
docker start rancher-inventory
```

Upgrade: load the new image tar, stop and remove the container, and start it again with the same two mounts. On start the app applies any missing database migrations. An older image refuses a database from a newer one with "database schema N is newer than this image".

## Discovery export

`/export/discovery` (admins) or `inventory export-discovery --out /data/export` builds a `tar.gz` with the same per-cluster CSV files and column names as `k8s-discovery.sh` 1.0.0, plus `fleet_summary.csv`. Names are hashed with `EXPORT_SALT`; the same salt gives the same hashes as the script. Before the file is offered, a self-check looks for IPv4 addresses, the `EXPORT_SENSITIVE_STRINGS`, and real cluster, node, namespace and workload names in the hashed columns. Any hit blocks the download and lists the file and column. The hash-to-name map is stored in the database and downloaded separately from `/export/discovery/name-map.csv`; it is never in the bundle.

## Migration views

The pages are built for planning a move from VMware and NSX to Nutanix NKP.

- **Overview (`/`)**: a site picker, fleet totals, charts, a list of clusters that switches between Table and Cards, and a "Removed from Rancher" table. The summary charts show totals, not one bar per cluster, so they stay readable as the fleet grows: capacity, clusters by class, clusters per flag, the 10 largest clusters (switch between vCPU and memory), clusters per site by class, floating IP needs by source, node operating system images, and persistent volumes by storage class.
- **Remaining / All clusters**: a toggle on the overview. "Remaining" is the default and counts only clusters with status not started, planned or in progress. "All" counts every cluster.
- **Migration progress charts** (on the overview): clusters per status, status per site, status per wave, and clusters finished over time (only clusters marked migrated or decommissioned count as finished).
- **Migration tracking**: each cluster has a status (not started, planned, in progress, migrated, decommissioned, won't migrate), a wave and a note. Edit them on the cluster page, where the change history is also shown. Both roles, admin and viewer, can edit them. The collector does not change them.
- **Bulk edit**: on the cluster lists, tick clusters and set a status or a wave, or clear the wave, for all of them at once. Notes are not changed in bulk.
- **Removed from Rancher**: clusters that the collector no longer sees in Rancher. A row is highlighted when the cluster is not marked migrated, decommissioned or won't migrate. Status, wave and note can be edited here too.
- **Clusters (`/clusters`)**: the same Table and Cards views, with filters for flags, labels (cluster, node and machine pool labels), status and wave. The CSV export has status and wave columns.
- **Cluster page (`/clusters/<site>/<cluster>`)**: a migration sheet with nodes grouped by machine pool, CNI, CIDRs, databases found, and the Watch out flags for that cluster.
- **Watch out flags** mark things that need a decision before a move, for example `node-pinned-pv`, `database`, `external-ip`, `nodeport`, `host-network`, `k8s<1.32` and `privileged/hostpath`. Each flag links to the cluster page section with the rows behind it.
- **Floating IPs**: the number of distinct addresses a cluster needs to keep. It counts LoadBalancer Service addresses (status ingress and a pinned `spec.loadBalancerIP`), Service `spec.externalIPs`, ingress controller LB addresses and node ExternalIP addresses, plus 1 for the control plane endpoint when Rancher shows one. Each address counts once, under the first source that has it (LoadBalancer, then ingress LB, then `spec.externalIPs`, then node ExternalIP), so the sources add up to the total. The `external-ip` Watch out flag shows when a LoadBalancer address, a Service `spec.externalIPs` address or an ingress LB address exists; node ExternalIP addresses and the control plane endpoint count as floating IPs but do not set it. The "External IP" column on the cluster page is only the `spec.externalIPs` source, so `external-ip` can show while that column is 0 (for example when the only address is an ingress LB address). IPs that NSX assigns outside Kubernetes (for example SNAT or manual NAT rules) are not visible to the app and are not in this count.
- **Old URLs** `/inventory`, `/helm`, `/crds`, `/annotations` and `/namespaces` redirect (307) to the new pages and keep the query string, so saved links still work.

`tests/fixtures/real/` holds fixtures from real clusters. It is git-ignored and exists only on the dev Mac. Tests that need it are skipped when it is missing ("P2 fixtures missing").

## Rules for networking and databases

The built-in rules are in [config/defaults/rules.yaml](config/defaults/rules.yaml). Two sections drive the migration views.

`network`:

| Key | What it does |
| --- | --- |
| `lb_implementations` | Which load balancer stack serves a Service, matched by `loadBalancerClass` (`classes`) or annotation domain (`annotation_domains`) |
| `pinned_ip_annotations` | Annotations that pin a Service to a fixed IP |
| `shared_ip_annotations` | Annotations that let Services share one IP |
| `vendor_annotation_domains` | Annotation domains reported as vendor-specific |
| `cni_daemonsets` | CNI name to the DaemonSet names that identify it |

`databases`:

| Key | What it does |
| --- | --- |
| `images` | Engine name to image repository patterns (fnmatch) |
| `exclude` | Image patterns to ignore, for example `*exporter*` |
| `operator_crd_groups` | Engine name to the CRD API groups of its operators |

To extend a list, copy `config/defaults/rules.yaml` to `config/rules.yaml` and edit it. An override file that sets `network` or `databases` replaces that whole section. Any sub-key you leave out becomes empty, so copy the full section and then change it. If the override file does not set a section, the built-in defaults are used for it. Unknown keys inside `network` or `databases` are rejected with a config error.

## Known behaviour

- Data is written per cluster and kind, not per cluster. During a run, or after an interrupted one, a cluster can show new data for some kinds and the previous run's data for others. Rows are only deleted for a kind that was listed in full in the current run.
- Kinds that are not installed on a cluster (for example `volumesnapshots`) show as "not installed", not as an error.
- Pod usage is stored per namespace only. Jobs owned by a CronJob are not stored.

## Dev cluster notes

The dev Rancher server at `192.168.50.210`:

- Rancher v2.14.0, served by ingress-nginx under the hostname `rancher.local`. Requests to the bare IP return 404.
- `rancher.local` does not resolve from the dev Mac. Add it to `/etc/hosts`:

  ```
  192.168.50.210 rancher.local
  ```

- The ingress serves its self-signed placeholder certificate (`Kubernetes Ingress Controller Fake Certificate`, SAN `ingress.local`). It does not match `rancher.local`, so a `ca_file` alone will not make TLS verify. The dev site therefore uses `tls_verify: false`. This is for dev only. Fix it on the cluster by giving the Rancher ingress a certificate for `rancher.local`.

Example dev `sites.yaml`:

```yaml
sites:
  - name: dev
    url: https://rancher.local
    token_env: RANCHER_DEV_TOKEN
    tls_verify: false
    token_expires: 2027-02-10
```

## Run on the dev Mac

Build the image and run it against the dev Rancher. This needs `.env` and `dev/config/sites.yaml` in the repo.

Run these commands from the repo root.

```bash
docker build -t rancher-inventory:1.0.0-dev .

mkdir -p dev/data
docker run -d --name rancher-inventory \
  --env-file .env \
  --add-host rancher.local:192.168.50.210 \
  -v $PWD/dev/config:/config:ro \
  -v $PWD/dev/data:/data \
  -p 8443:8443 \
  --read-only --tmpfs /tmp --cap-drop ALL \
  rancher-inventory:1.0.0-dev
docker logs -f rancher-inventory
```

- The log shows the first admin being created and a first run starting when there is no good run yet.
- If `/data` is not writable, run `chmod 777 dev/data`. Docker Desktop on macOS maps ownership. This is for dev only.
- Check health after about a minute: `docker inspect --format '{{.State.Health.Status}}' rancher-inventory` should print `healthy`.
- Open `http://localhost:8443` and log in with `ADMIN_USER` and `ADMIN_PASSWORD` from `.env`.
- Stop and remove: `docker rm -f rancher-inventory`. The data stays in `dev/data`.
