# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## What this is

A single-container app that inventories every downstream cluster behind one or more Rancher servers: a background collector, a FastAPI + Jinja/htmx web UI, and an embedded SQLite database, all in one Python package (`inventory/`). It targets an **airgap**: no runtime network access other than to Rancher, no CDN assets (Chart.js and htmx are vendored in `inventory/web/static/`), and the Docker build installs only hash-pinned versions from `uv.lock`. The main use is planning a move from VMware/NSX to Nutanix NKP (the "migration" views).

The design lives in `Rancher Inventory Tool Implementation Plan (single image, airgap).md`; code comments refer to its sections as "spec 6.x". Later designs and plans are in `docs/superpowers/specs/` and `docs/superpowers/plans/`. `README.md` is the operator guide (Rancher RBAC setup, `.env`, `sites.yaml`, airgap install, migration view semantics).

## Commands

Python 3.12 only (`>=3.12,<3.13`), managed with `uv`.

```bash
uv sync                                   # install with the dev group (pytest)
uv run pytest -m "not live and not slow"  # offline tests
uv run pytest tests/test_classify.py::test_name   # one test
uv run pytest -m slow                     # slow tests (excluded by default via addopts)
uv run inventory <command>                # CLI: serve | check-config | collect-once | healthcheck | backup FILE | export-discovery --out DIR | user add|passwd|list|disable
docker build -t rancher-inventory:1.2.0-dev .        # local image
docker buildx build --platform linux/amd64 -t rancher-inventory:1.2.0 --load .   # airgap image
```

There is no linter or formatter configured.

### Test environment

- `tests/conftest.py` loads the repo-root `.env` and points `SITES_FILE` at `dev/config/sites.yaml`. Without a `.env`, offline tests still need `SESSION_SECRET` and `RANCHER_DEV_TOKEN` set (any value), e.g. `SESSION_SECRET=x RANCHER_DEV_TOKEN=dummy uv run pytest -m "not live and not slow"`.
- Markers: `live` needs the dev Rancher (`https://rancher.local`, `<dev-rancher-ip>` in `/etc/hosts`, real `RANCHER_DEV_TOKEN`); `slow` is deselected unless `-m slow`; `real_fixtures` need git-ignored JSON in `tests/fixtures/real/` and skip with "P2 fixtures missing" when absent. Passing `-m` on the command line replaces the default `-m 'not slow'`.
- Live fixtures (`dev_live`, `live_client`, `collected_db`, `dev_lists`) are session-scoped and share one event loop (`live_loop`) so the suite opens one Rancher connection.
- Web tests use `web_app`/`client`/`admin_client`/`viewer_client` fixtures; POSTs need the CSRF token the login helper stores on `client.csrf`.

## Architecture

**Process model** (`inventory/cli.py` `_serve`): run DB migrations, start `CollectorThread` (its own thread + asyncio loop, daily start at `COLLECT_DAILY_AT`, plus a first run if no good run exists), then run uvicorn on `create_app(settings, collector)`. The web app and collector share only the SQLite file; both open their own connections via `inventory/db/connection.py`.

**Configuration** (`inventory/settings.py`): pydantic-settings `Settings` from env vars; `sites.yaml` lists Rancher servers and names the env var holding each token (`token_env`). `config/defaults/kinds.yaml` (what to list and which fields to keep) and `config/defaults/rules.yaml` (classification, network/LB rules, database detection, complexity thresholds, opt-in collectors) ship in the image at `DEFAULTS_DIR`; a `config/rules.yaml` or `config/kinds.yaml` on the host overrides them. A rules override replaces a whole top-level section (`network`, `databases`), and unknown keys there are a config error.

**Collector pipeline** (`inventory/collector/`):
- `runner.py`: a *run* has a `Scope` (all, one site, one cluster, or a list) and goes through create → execute → ok/partial/failed/cancelled/interrupted. Only one run at a time (`RunInProgress`). Per site it opens a `RancherClient` (`inventory/rancher/client.py`, which goes through Rancher's `/k8s/clusters/<id>` proxy), reads `provisioning.cattle.io` data, and collects clusters with `CONCURRENCY_PER_SITE`.
- `cluster.py` `collect_cluster`: lists each kind, `transform.trim()`s objects down to the `keep` fields plus derived values (`podspec.py`, `network.py`, `netpolicy.py`, `addresses.py`, `storage.py`, `annotations.py`), and writes them per **(cluster, kind)** transaction. Rows are deleted only for kinds listed in full this run, so a cluster can mix fresh and previous data after a partial run. A kind that returns 404 is "not installed", not an error.
- `classify.py` builds cluster profiles; `rollup.py` does the namespace roll-up and complexity ("Watch out") flags using the same counting rules as `reference/k8s-discovery.sh`.
- `members.py`: read once per site from the Rancher local cluster (role bindings, projects, role templates, `/v3/users`), written per cluster after that cluster is collected. A 401/403 marks members "not collected".
- `optional/`: opt-in collectors, off by default in `rules.yaml`, needing the extra RoleTemplate in `deploy/inventory-reader-extended.yaml`. A 401/403 from them is logged and skipped.

**Database** (`inventory/db/`): numbered SQL files in `migrations/` applied in order, version tracked in `PRAGMA user_version`. When adding a migration, add the next-numbered file **and bump `SCHEMA_VERSION` in `migrate.py`**; an older image refuses a newer DB. `repo.py` holds collector writes. Users, presets, migration tracking (status/wave/note history) and custom column values (migration `005`) are the only data that cannot be re-collected.

**Web** (`inventory/web/`): `app.py` `create_app` registers one router per module in `routes/`. `deps.py` provides DB connections, current user, admin check, CSRF and the shared `templates` object. Reads are in `queries.py` (generic) and `migration.py` (migration views computed at request time from stored inventory, including floating-IP counting and flag anchors); `tracking.py` handles migration status/wave edits; `custom_columns.py` holds custom column definitions, values, history and `cf<id>` filters (admin page in `routes/columns.py`, value edits in `routes/tracking.py`). Two roles: `admin` and `viewer` (both may edit tracking). htmx requests and `/api/*` get plain error responses, others get the `error.html` page. Old URLs (`/inventory`, `/helm`, `/crds`, `/annotations`, `/namespaces`) redirect with 307 and keep the query string.

**Discovery export** (`inventory/export/discovery.py`): produces the same per-cluster CSVs and column names as `reference/k8s-discovery.sh` 1.0.0, with names hashed by `EXPORT_SALT` (same salt → same hashes as the script). A self-check blocks the export if IPs, `EXPORT_SENSITIVE_STRINGS` or real names appear in hashed columns. Keep output compatible with that script.

## Constraints to keep

- Read-only against Rancher; never request Secrets. New kinds need a matching rule in the `inventory-reader` RoleTemplate in `README.md`.
- No external network calls or remote assets from the web UI (airgap).
- New Python dependencies must go through `uv.lock` (`uv add`), since the Dockerfile installs only hash-checked locked versions.
- Templates, static files and migrations must stay under the paths `pyproject.toml` includes in the wheel.
