# Dev test checklist: rancher-inventory 1.0.1-dev

Things to check by hand on the dev cluster with image `rancher-inventory:1.0.1-dev`. The app runs on port 8443 and listens on all interfaces (`0.0.0.0`).

## 1. Start and upgrade

- [ ] Container status is `healthy`: `docker inspect --format '{{.State.Health.Status}}' rancher-inventory`.
- [ ] `docker logs rancher-inventory` shows no errors at start, and no migration error.
- [ ] Users, presets and trend history from 1.0.0-dev are still there (the same `dev/data` is mounted).
- [ ] `docker exec rancher-inventory inventory check-config` passes for the dev site.

## 2. Network access

- [ ] Opens on the Mac: `http://localhost:8443`.
- [ ] Opens from another device on the LAN: `http://192.168.50.212:8443` (the Mac's current IP; check it with `ipconfig getifaddr en0`).
- [ ] Login works from the other device. `COOKIE_SECURE=false` is needed while serving plain HTTP.
- [ ] Browser network tab: every request goes to the same host and port, none to the internet.

## 3. Collection

- [ ] Admin "Run now" (or "Refresh this site") finishes with status `ok`.
- [ ] Cluster count on `/clusters` matches the Rancher dashboard.
- [ ] `/runs/<id>`: `volumesnapshots` is `ok` or "not installed", not an error.

## 4. Pages

- [ ] `/inventory`: complexity flags show as badges; the `flag` filter narrows the list.
- [ ] `/workloads`: each filter (`privileged`, `host_network`, `hostpath`, `no_requests`, `no_limits`, `no_probes`, `operator_owned`, `gpu`) returns a subset. The ingress-nginx daemonset shows host ports.
- [ ] `/storage`: filters `rwx`, `block`, `unmounted`, `source`, `storage_class` work; PV and snapshot tables load.
- [ ] `/namespaces`: sort by `cpu_req_m`, `cpu_used_m`, `mem_req_b`, `mem_used_b`, `restarts`. `kube-system` has CPU usage above 0.
- [ ] `/platform`: shows the `rancher.cattle.io` webhook, aggregated APIServices, custom ClusterRoles (only non-Rancher ones), cluster-admin bindings, and API versions served and in use.
- [ ] `/query`: try the new filters `annotation`, `service_type`, `address`, `helm_chart`, `crd_group`, `class`, `flag`.
- [ ] `/check` with a preset shows present, absent or unknown per cluster.
- [ ] Error pages: an unknown URL shows the HTML 404 page, not JSON.

## 5. CSV and API

- [ ] Each page table has a CSV link, and the file opens in a spreadsheet with a header row.
- [ ] `docker logs` shows a `csv download user=...` line per download.
- [ ] `/export/clusters.csv` and `/export/resources.csv?kind=pods` download.
- [ ] `/api/clusters` and `/api/resources?kind=pods` return JSON when logged in, and a 401 or login redirect when logged out.

## 6. Discovery export

- [ ] `/export/discovery` (admin) downloads a `tar.gz`; the self-check passes.
- [ ] The bundle opens. No CSV contains a real cluster name, node name, IP or `rancher.local`.
- [ ] `/export/discovery/name-map.csv` downloads, and the name map is not inside the bundle.
- [ ] As a viewer, `/export/discovery` is refused (403).

## 7. Admin and hardening

- [ ] Viewer user cannot reach `/admin/*` or run actions.
- [ ] 5 wrong passwords lock the account for 15 minutes.
- [ ] Usernames with capitals log in (they are stored lower-case).
- [ ] Token expiry warning shows in the header for admins when `token_expires` in `sites.yaml` is within 30 days.
- [ ] Backup: `docker exec rancher-inventory inventory backup /data/backup.db` reports integrity `ok`.

## 8. Opt-in collectors (optional)

All are off by default. To test one, enable it in a `rules.yaml` override, apply its rule from `deploy/inventory-reader-extended.yaml` to the dev Rancher, run a collection, and check the result. Remove the binding afterwards.

- [ ] With all off, a run makes no calls to `/metrics`, `/proxy/stats` or `/api/v1/secrets`.
- [ ] `cr_counts`
- [ ] `pvc_usage`
- [ ] `deprecated_api_metrics`
- [ ] `secret_types`
- [ ] `nginx_config_keys`
- [ ] The home page lists the enabled opt-ins.

## 9. Open items from plan 2 (not checked by the tests)

- [ ] Run `k8s-discovery.sh --clear-names --out /tmp/k8sd-local` with an admin kubeconfig, then rerun `uv run pytest tests/test_rollup.py tests/test_export.py`. The namespace-total comparison and the header comparison skip without it.
- [ ] Decide whether `operator.tigera.io` and the Cluster API groups (`*.x-k8s.io`) count as platform CRDs. They are counted as custom now.
- [ ] Compare webhook, CRD and RBAC counts on `local` with the script output (plan Task 6 Step 3).
- [ ] Second-cluster comparison: open until a downstream cluster exists.
- [ ] Airgap host install (plan Task 12 Steps 2 to 4).
