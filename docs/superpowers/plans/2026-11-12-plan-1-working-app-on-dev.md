# Plan 1: Working App on Dev Rancher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build phases 1 to 5 of the spec as one Docker image, and run it against the dev Rancher (`rancher.local`) so the web app can be logged into and browsed with real data.

**Architecture:** One Python process. `inventory serve` runs a FastAPI web app and a collector thread with its own asyncio loop. The collector reads Rancher through `/v3` and the `/k8s/clusters/<id>/` proxy and writes to one SQLite file. The web app only reads that file.

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, httpx, Jinja2, HTMX 2, Chart.js 4, pydantic-settings, PyYAML, python-multipart, SQLite (stdlib), pytest. Docker Desktop on the dev Mac (arm64).

**Spec:** `Rancher Inventory Tool Implementation Plan (single image, airgap).md` (repo root). Sections 1 to 6.8, 7 (pages listed for phases 3 to 5), 8 and 9 phases 1 to 5. Plan 2 (`2026-11-12-plan-2-detail-hardening-airgap.md`) covers phases 6 to 10.

## Global Constraints

- Python 3.12 in the image (`python:3.12-slim`). Locally use `uv` with Python 3.12; never run Python outside the uv environment.
- Runtime dependencies are only: `fastapi`, `uvicorn`, `httpx`, `jinja2`, `pydantic-settings`, `pyyaml`, `python-multipart`. Dev only: `pytest`.
- Nothing is fetched at run time. `htmx.min.js` and `chart.umd.min.js` are committed under `inventory/web/static/`.
- SQLite pragmas on every connection: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.
- Times are stored as `TEXT`, ISO 8601, UTC.
- The query and check pages use a read-only connection (`mode=ro`) with a 10 s limit and at most 5,000 rows.
- Rancher tokens are never logged or shown. Mask as `token-xxxxx:****`.
- Secrets, Helm values and ConfigMap data are never requested or stored. Transforms keep only listed fields.
- Passwords: `hashlib.scrypt`, per-user salt, minimum length 12.
- Session cookie: random 32 bytes, `HttpOnly`, `SameSite=Lax`, `Secure` unless `COOKIE_SECURE=false`; only its SHA-256 is stored.
- Five failed logins for one username lock it for 15 minutes.
- Container runs as uid 10001, `--read-only --tmpfs /tmp --cap-drop ALL`, only `/data` writable.
- No mock, fake or synthetic Rancher data. Tests that need Kubernetes objects read them live from the dev Rancher (marker `live`). Pure functions are tested with literal values taken from the spec.

### Dev environment facts (verified 2026-11-12)

- Rancher v2.14.0 at `https://rancher.local`, IP `192.168.50.210`. Bare-IP requests return 404.
- TLS uses the ingress placeholder certificate. TLS checks are skipped in dev through `tls_verify: false` in `sites.yaml` (decided by the user; not in the spec).
- Service user `inventory-reader`, token with no scope, expires 2027-02-10.
- One visible cluster: `local` (RKE2 v1.34.3, 3 nodes). All 36 kinds return 200; Secrets and `provisioning.cattle.io` return 403.
- `/v3/clusters` returns `allocatable` and `requested` (`cpu`, `memory`, `pods`).
- `apps.catalog.cattle.io` has 18 releases and `spec.resources` lists `apiVersion`, `kind`, `name`, `namespace` per object.

## Review Focus

1. **Paginated lists that break mid-way.** A `continue` token that expires (HTTP 410) or a failure on page 2 must not delete stale rows for that kind. Test in Task 7.
2. **A kind returning 403 or 404.** The cluster still ends `ok` with that kind recorded as skipped, and old rows for that kind stay. Test in Task 7.
3. **Restart during a run.** Any `running` run becomes `interrupted` on start, and clusters already done keep their new data. Test in Task 8.
4. **Two refreshes at once.** A second `POST /runs`, or a daily start during a manual run, is refused while a run is active. Test in Task 8.
5. **Disabled user with a live session.** The next request is treated as logged out. Test in Task 9.

## File Structure

```
pyproject.toml
Dockerfile
.dockerignore
config/defaults/kinds.yaml          built-in kinds (copied to /app/defaults)
config/defaults/rules.yaml          built-in classification + annotation rules
dev/config/sites.yaml               dev site, committed (no secrets)
dev/data/                           dev database, gitignored
inventory/
  __init__.py                       __version__ = "1.0.0"
  settings.py                       Settings, Site, load_sites, site_token, load_env_file
  cli.py                            argparse entry point `inventory`
  db/connection.py                  connect, run_readonly_query
  db/migrate.py                     migrate
  db/migrations/001_initial.sql     every table used in plan 1
  db/repo.py                        collector writes
  rancher/client.py                 RancherClient, RancherError
  collector/units.py                parse_cpu, parse_bytes
  collector/kinds.py                KindSpec, load_kinds
  collector/rules.py                Rules, load_rules
  collector/transform.py            Trimmed, trim
  collector/annotations.py          matches, filter_annotations
  collector/addresses.py            Address, extract_addresses
  collector/classify.py             Flags, class_label, build_profiles
  collector/cluster.py              collect_cluster
  collector/runner.py               Scope, RunManager, mark_interrupted
  collector/thread.py               CollectorThread (daily loop + manual requests)
  checkconfig.py                    check_config
  web/app.py                        create_app
  web/auth.py                       passwords, users, sessions, CSRF, lockout
  web/deps.py                       current_user, require_admin, db dependencies
  web/queries.py                    read queries for pages, build_query
  web/routes/*.py                   one module per page group
  web/templates/*.html              Jinja2 templates
  web/static/htmx.min.js, chart.umd.min.js, app.css
tests/
  conftest.py                       env loading, tmp DB, live fixtures
  test_*.py
```

Shared test fixtures in `tests/conftest.py`, added by the task that first needs them: `settings`, `dev_site` (Task 1); `db`, `db_path` (Task 2); `live_client`, `dev_clusters`, `local_cluster`, `dev_node_usage` (Tasks 3 and 6); `kinds` (Task 7); `collected_db` (session tmp DB after one real `execute_run`), `client`, `admin_client`, `viewer_client`, `admin_client_collected` (Tasks 8 and 10); `dev_lists`, `rules` (Tasks 11 and 12). Live fixtures are session scoped so the dev Rancher is read once per test run.

Original-plan tables that the spec names but does not define (`runs`, `clusters`, `cluster_metrics`, `presets`) are defined in Task 2. Those column lists are this plan's call.

---

### Task 1: Project skeleton and settings

**Files:**
- Create: `pyproject.toml`, `inventory/__init__.py`, `inventory/settings.py`, `dev/config/sites.yaml`, `tests/conftest.py`, `tests/test_settings.py`
- Modify: `.gitignore` (add `dev/data/`), `.env` (rename variable, add dev values)

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` fields (env names are upper case): `listen_port: int = 8443`, `tls_cert_file: str = ""`, `tls_key_file: str = ""`, `session_secret: str`, `session_hours: int = 12`, `cookie_secure: bool = True`, `admin_user: str = "admin"`, `admin_password: str = ""`, `collector_enabled: bool = True`, `collect_daily_at: str = "02:00"`, `stale_after_hours: int = 36`, `concurrency_per_site: int = 4`, `request_timeout_s: float = 30`, `metrics_retention_days: int = 365`, `data_dir: Path = Path("/data")`, `sites_file: Path = Path("/config/sites.yaml")`, `defaults_dir: Path = Path("/app/defaults")`. Property `db_path -> Path` = `data_dir / "inventory.db"`. Property `config_dir -> Path` = `sites_file.parent`.
  - `class Site(BaseModel)`: `name: str`, `url: str` (trailing `/` stripped), `token_env: str`, `ca_file: str | None = None`, `tls_verify: bool = True`, `token_expires: date | None = None`.
  - `def load_sites(settings: Settings) -> list[Site]` — raises `ConfigError` on missing file, empty list, or duplicate names.
  - `def site_token(site: Site) -> str` — reads `os.environ[site.token_env]`; raises `ConfigError("RANCHER_X_TOKEN is not set")`.
  - `def mask_token(token: str) -> str` — `"token-k6lm8:abc"` gives `"token-k6lm8:****"`.
  - `def load_env_file(path: Path) -> None` — for tests and local runs only; `KEY=VALUE` lines, `#` comments, uses `os.environ.setdefault`.
  - `class ConfigError(Exception)`.

- [ ] **Step 1: Create the uv project**

`pyproject.toml`: project name `rancher-inventory`, `requires-python = ">=3.12,<3.13"`, the seven runtime dependencies, dev group with `pytest`, script `inventory = "inventory.cli:main"`, pytest marker `live: needs the dev Rancher`. Package data: `inventory/web/templates/*`, `inventory/web/static/*`, `inventory/db/migrations/*.sql`.

Run: `uv python pin 3.12 && uv sync`
Expected: `.venv` created, no errors.

- [ ] **Step 2: Rename the dev token and fill `.env`**

`.env` (gitignored, `chmod 600`). Rename `TOKEN` to `RANCHER_DEV_TOKEN` and add:

```
SESSION_SECRET=<openssl rand -hex 32>
ADMIN_USER=admin
ADMIN_PASSWORD=<12+ chars>
COOKIE_SECURE=false
COLLECT_DAILY_AT=02:00
DATA_DIR=/data
SITES_FILE=/config/sites.yaml
```

`dev/config/sites.yaml`:

```yaml
sites:
  - name: dev
    url: https://rancher.local
    token_env: RANCHER_DEV_TOKEN
    tls_verify: false
    token_expires: 2027-02-10
```

- [ ] **Step 3: Write `tests/conftest.py`**

Calls `load_env_file(Path(".env"))` at import. Autouse fixture `env` sets with `monkeypatch`: `DATA_DIR=<tmp_path>`, `SITES_FILE=dev/config/sites.yaml`, `DEFAULTS_DIR=config/defaults`, `COLLECTOR_ENABLED=false`. Fixture `settings` returns `Settings()`. Fixture `dev_site` returns `load_sites(settings)[0]`.

- [ ] **Step 4: Write the failing tests** in `tests/test_settings.py`

```python
def test_dev_site_loads(settings):
    sites = load_sites(settings)
    assert [s.name for s in sites] == ["dev"]
    assert sites[0].url == "https://rancher.local"
    assert sites[0].tls_verify is False

def test_site_token_missing(monkeypatch, dev_site):
    monkeypatch.delenv("RANCHER_DEV_TOKEN")
    with pytest.raises(ConfigError, match="RANCHER_DEV_TOKEN"):
        site_token(dev_site)

def test_duplicate_site_names_rejected(tmp_path, monkeypatch): ...  # two entries named "a" -> ConfigError

def test_mask_token():
    assert mask_token("token-k6lm8:secretvalue") == "token-k6lm8:****"
```

- [ ] **Step 5: Run and see them fail**

Run: `uv run pytest tests/test_settings.py -v`
Expected: FAIL, `ImportError` on `inventory.settings`.

- [ ] **Step 6: Implement `inventory/settings.py`**

- [ ] **Step 7: Run and see them pass**

Run: `uv run pytest tests/test_settings.py -v` — Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock .python-version .gitignore inventory/ dev/config/ tests/
git commit -m "feat: project skeleton, settings and dev site config"
```

---

### Task 2: SQLite connection, migrations and schema

**Files:**
- Create: `inventory/db/connection.py`, `inventory/db/migrate.py`, `inventory/db/migrations/001_initial.sql`, `tests/test_db.py`

**Interfaces:**
- Produces:
  - `def connect(path: Path, readonly: bool = False) -> sqlite3.Connection` — `row_factory = sqlite3.Row`, applies the four pragmas (WAL only on the writer). Read-only uses URI `file:{path}?mode=ro`.
  - `def run_readonly_query(conn, sql: str, params: Sequence | Mapping, timeout_s: float = 10.0, max_rows: int = 5000) -> list[sqlite3.Row]` — uses `conn.set_progress_handler` (every 10,000 VM steps), raises `QueryTimeout` past the deadline; returns at most `max_rows`.
  - `class QueryTimeout(Exception)`.
  - `SCHEMA_VERSION = 1`; `def migrate(conn) -> int` — applies `migrations/NNN_*.sql` with `NNN > PRAGMA user_version`, in order, each in one transaction, then sets `user_version`. Returns the new version.

Tables in `001_initial.sql`: `resources`, `resource_labels`, `resource_annotations` (same as labels plus `custom INTEGER NOT NULL DEFAULT 0`), `resource_images`, `addresses`, `namespace_profile`, `cluster_profile`, `users`, `sessions`, `run_clusters`, exactly as in spec sections 2 and 5. Plus these (this plan's definitions):

```sql
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL,          -- running | ok | partial | failed | cancelled | interrupted
    trigger TEXT NOT NULL,         -- schedule | manual
    triggered_by TEXT,             -- username, NULL for schedule
    scope TEXT NOT NULL DEFAULT '{}',  -- JSON of Scope
    clusters_total INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE clusters (
    site TEXT NOT NULL, cluster_id TEXT NOT NULL,
    name TEXT NOT NULL, state TEXT, provider TEXT, k8s_version TEXT,
    labels TEXT NOT NULL DEFAULT '{}', node_count INTEGER,
    last_ok_run INTEGER, last_ok_at TEXT, last_error TEXT, last_seen_run INTEGER,
    PRIMARY KEY (site, cluster_id)
);
CREATE TABLE cluster_metrics (            -- one point per cluster per day
    site TEXT NOT NULL, cluster_id TEXT NOT NULL, day TEXT NOT NULL,  -- YYYY-MM-DD
    run_id INTEGER NOT NULL, nodes INTEGER, pods INTEGER,
    cpu_alloc_m INTEGER, cpu_req_m INTEGER, cpu_used_m INTEGER,
    mem_alloc_b INTEGER, mem_req_b INTEGER, mem_used_b INTEGER,
    PRIMARY KEY (site, cluster_id, day)
);
CREATE TABLE cluster_metrics_latest (...same columns without day, PRIMARY KEY (site, cluster_id));
CREATE TABLE run_kinds (                  -- which kinds were fully listed per cluster per run
    run_id INTEGER NOT NULL, site TEXT NOT NULL, cluster_id TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL,                 -- ok | forbidden | not_found | failed
    objects INTEGER, error TEXT,
    PRIMARY KEY (run_id, site, cluster_id, kind)
);
CREATE TABLE presets (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
    filters TEXT NOT NULL,                -- JSON accepted by build_query
    created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE login_failures (username TEXT NOT NULL, failed_at TEXT NOT NULL);
CREATE INDEX login_failures_user ON login_failures (username, failed_at);
```

- [ ] **Step 1: Write the failing tests** in `tests/test_db.py`

```python
def test_migrate_fresh_db(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert migrate(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"resources", "runs", "run_clusters", "run_kinds", "users", "presets"} <= names

def test_migrate_is_idempotent(tmp_path): ...  # second migrate() returns same version, no error

def test_readonly_rejects_writes(tmp_path):  # connect(readonly=True) -> INSERT raises sqlite3.OperationalError

def test_query_timeout(tmp_path):
    conn = connect(tmp_path / "t.db"); migrate(conn)
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(QueryTimeout):
        run_readonly_query(conn, sql, [], timeout_s=0.2)

def test_query_row_cap(tmp_path):  # recursive CTE LIMIT 6000 -> len(result) == 5000
```

- [ ] **Step 2: Run and see them fail** — `uv run pytest tests/test_db.py -v`, Expected: ImportError.
- [ ] **Step 3: Implement** `connection.py`, `migrate.py`, `001_initial.sql`.
- [ ] **Step 4: Run and see them pass** — Expected: 5 passed.
- [ ] **Step 5: Commit** — `git commit -m "feat: SQLite connection, migrations and initial schema"`

---

### Task 3: Rancher client

**Files:**
- Create: `inventory/rancher/client.py`, `tests/test_rancher_client.py`

**Interfaces:**
- Consumes: `Site`, `site_token`, `Settings.request_timeout_s`.
- Produces:
  - `class RancherError(Exception)` with `.status: int | None` and `.url: str` (no token in message).
  - `class RancherClient` — `RancherClient(site: Site, token: str, timeout_s: float)`; async context manager wrapping one `httpx.AsyncClient` with `Authorization: Bearer`, `verify=False` when `site.tls_verify` is false, else `site.ca_file` or system trust.
  - `async def whoami(self) -> str` — `GET /v3/users?me=true`, returns `username`.
  - `async def list_clusters(self) -> list[dict]` — `GET /v3/clusters`, returns `data`.
  - `async def list_kind(self, cluster_id: str, path: str, page_size: int = 500) -> list[dict]` — `GET /k8s/clusters/{cluster_id}{path}?limit=N`, follows `metadata.continue` until empty. Any non-200 raises `RancherError` with the status.
  - `async def get_json(self, path: str) -> dict` — any other GET, same error rule.

- [ ] **Step 1: Write the failing live tests**

```python
pytestmark = pytest.mark.live

async def _client(dev_site, settings):
    return RancherClient(dev_site, site_token(dev_site), settings.request_timeout_s)

def test_whoami(dev_site, settings):
    async def go():
        async with await _client(dev_site, settings) as c:
            return await c.whoami()
    assert asyncio.run(go()) == "inventory-reader"

def test_list_clusters_has_local(...):    # "local" in {c["id"] for c in clusters}
def test_pagination_returns_all(...):     # list_kind("local", "/api/v1/pods", page_size=5) has same names as page_size=500
def test_secrets_forbidden(...):          # list_kind("local", "/api/v1/secrets") raises RancherError, .status == 403
def test_unknown_group_404(...):          # "/apis/example.invalid/v1/things" -> .status == 404
def test_token_not_in_error(...):         # str(err) does not contain the token value
```

- [ ] **Step 2: Run and see them fail** — `uv run pytest tests/test_rancher_client.py -v`
- [ ] **Step 3: Implement `inventory/rancher/client.py`**
- [ ] **Step 4: Run and see them pass** — Expected: 6 passed.
- [ ] **Step 5: Commit** — `git commit -m "feat: async Rancher client with pagination"`

---

### Task 4: CLI skeleton and `check-config`

**Files:**
- Create: `inventory/cli.py`, `inventory/checkconfig.py`, `config/defaults/kinds.yaml` (stub with `nodes` only, filled in Task 11), `inventory/collector/kinds.py`, `tests/test_checkconfig.py`

**Interfaces:**
- Consumes: `RancherClient`, `load_sites`, `site_token`, `mask_token`.
- Produces:
  - `@dataclass KindSpec`: `name: str`, `path: str`, `scope: str` (`cluster` | `namespaced`), `keep: list[str]`, `annotations: bool = False`, `addresses: bool = False`, `metadata_only: bool = False`.
  - `def load_kinds(settings: Settings) -> dict[str, KindSpec]` — `config_dir/kinds.yaml` if it exists, else `defaults_dir/kinds.yaml`. Top-level key `kinds:`.
  - `@dataclass SiteCheck`: `site`, `reachable: bool`, `tls: str` (`verified` | `skipped` | `error: ...`), `user: str | None`, `token_days_left: int | None`, `clusters: int | None`, `sample_cluster: str | None`, `kinds: dict[str, int]` (kind name to HTTP status, 200/403/404), `errors: list[str]`.
  - `async def check_config(settings: Settings) -> list[SiteCheck]`.
  - `def format_report(checks: list[SiteCheck]) -> str` — plain text; masked token; prints `WARNING: TLS verification is off` for `tls_verify: false`; warns when `token_days_left < 30`.
  - `def main(argv: list[str] | None = None) -> int` in `cli.py` with subcommands `serve`, `check-config`, `collect-once`, `user {add,passwd,list,disable}`, `healthcheck`. Unimplemented ones print `not implemented yet` and return 2 until their task. `check-config` returns 0 when every site is reachable with a valid token, else 1.

- [ ] **Step 1: Write the failing live test**

```python
@pytest.mark.live
def test_check_config_dev(settings):
    [c] = asyncio.run(check_config(settings))
    assert c.reachable and c.user == "inventory-reader"
    assert c.tls == "skipped"
    assert c.clusters >= 1 and c.sample_cluster == "local"
    assert c.kinds["nodes"] == 200

@pytest.mark.live
def test_report_masks_token(settings):
    text = format_report(asyncio.run(check_config(settings)))
    assert os.environ["RANCHER_DEV_TOKEN"].split(":")[1] not in text
    assert "TLS verification is off" in text

@pytest.mark.live
def test_cli_check_config_exit_code_bad_token(monkeypatch):
    monkeypatch.setenv("RANCHER_DEV_TOKEN", "token-bad00:nope")
    assert main(["check-config"]) == 1
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement** `kinds.py`, `checkconfig.py`, `cli.py`.
- [ ] **Step 4: Run and see them pass**; also run `uv run --env-file .env env SITES_FILE=dev/config/sites.yaml DEFAULTS_DIR=config/defaults inventory check-config` and read the report.
- [ ] **Step 5: Commit** — `git commit -m "feat: CLI and check-config"`

---

### Task 5: Docker image and check-config in the container (phase 1 milestone)

**Files:**
- Create: `Dockerfile`, `.dockerignore`

**Interfaces:**
- Consumes: `inventory` CLI from Task 4.

- [ ] **Step 1: Get the base image digest**

Run: `docker buildx imagetools inspect python:3.12-slim | grep Digest`
Use that index digest in both `FROM` lines: `python:3.12-slim@sha256:<digest>`.

- [ ] **Step 2: Write `Dockerfile`** as in spec section 4, with these changes: pinned digest; `COPY config/defaults/ /app/defaults/`; `ENV DEFAULTS_DIR=/app/defaults DATA_DIR=/data SITES_FILE=/config/sites.yaml PYTHONDONTWRITEBYTECODE=1`. `.dockerignore` excludes `.env`, `.venv`, `dev/`, `tests/`, `.git`, `docs/`.

- [ ] **Step 3: Build** (Docker Desktop must be running)

Run: `docker build -t rancher-inventory:dev .`
Expected: build succeeds.

- [ ] **Step 4: Run check-config in the container**

```bash
docker run --rm --env-file .env --add-host rancher.local:192.168.50.210 \
  -v $PWD/dev/config:/config:ro rancher-inventory:dev check-config
```

Expected: site `dev` reachable, user `inventory-reader`, TLS skipped warning, 1 cluster, `nodes: 200`, exit code 0.

- [ ] **Step 5: Confirm `.env` is not in the image**

Run: `docker run --rm --entrypoint sh rancher-inventory:dev -c 'find / -name .env 2>/dev/null | wc -l'` — Expected: `0`.

- [ ] **Step 6: Commit** — `git commit -m "feat: Dockerfile; check-config runs in container"`

---

### Task 6: Units, cluster records and metrics

**Files:**
- Create: `inventory/collector/units.py`, `inventory/db/repo.py`, `tests/test_units.py`, `tests/test_repo_clusters.py`

**Interfaces:**
- Produces:
  - `def parse_cpu(value: str) -> int` — millicores. `"3495m"` gives 3495, `"2"` gives 2000, `"0.5"` gives 500, `"250000n"` gives 0 (rounded down), `"1500u"` gives 1.
  - `def parse_bytes(value: str) -> int` — `"5222Mi"` gives 5475663872, `"1Gi"` gives 1073741824, `"1G"` gives 1000000000, `"128974848"` gives 128974848, `"1k"` gives 1000.
  - `def upsert_cluster(conn, site: str, cluster: dict, run_id: int) -> None` — from a `/v3/clusters` item: `id`, `name`, `state`, `provider`, `version.gitVersion`, `labels`, `nodeCount`.
  - `@dataclass ClusterMetrics`: `nodes`, `pods`, `cpu_alloc_m`, `cpu_req_m`, `cpu_used_m: int | None`, `mem_alloc_b`, `mem_req_b`, `mem_used_b: int | None`.
  - `def metrics_from_cluster(cluster: dict, node_usage: list[dict] | None) -> ClusterMetrics` — allocatable and requested from the `/v3/clusters` item; used from the sum of `metrics.k8s.io` node `usage` (None if the call failed); `pods` from `requested.pods`.
  - `def write_metrics(conn, site: str, cluster_id: str, run_id: int, day: str, m: ClusterMetrics) -> None` — upserts `cluster_metrics` for `day` and overwrites `cluster_metrics_latest`; deletes `cluster_metrics` rows older than `METRICS_RETENTION_DAYS` (passed as `retention_days` keyword).
  - `def mark_cluster_ok(conn, site, cluster_id, run_id, now: str)` / `def mark_cluster_failed(conn, site, cluster_id, error: str)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_units.py`: one assertion per example above.

`tests/test_repo_clusters.py` (live):

```python
@pytest.mark.live
def test_local_cluster_and_metrics(db, dev_clusters, dev_node_usage):
    local = next(c for c in dev_clusters if c["id"] == "local")
    upsert_cluster(db, "dev", local, run_id=1)
    m = metrics_from_cluster(local, dev_node_usage)
    write_metrics(db, "dev", "local", 1, "2026-11-12", m, retention_days=365)
    row = db.execute("SELECT * FROM clusters WHERE cluster_id='local'").fetchone()
    assert row["provider"] == "rke2" and row["node_count"] == local["nodeCount"]
    assert m.cpu_req_m == parse_cpu(local["requested"]["cpu"])
    assert m.cpu_used_m is not None and m.cpu_used_m > 0
    write_metrics(db, "dev", "local", 2, "2026-11-12", m, retention_days=365)
    assert db.execute("SELECT count(*) FROM cluster_metrics").fetchone()[0] == 1
```

Add fixtures to `conftest.py`: `db` (migrated connection in `tmp_path`), `dev_clusters` (live `list_clusters`), `dev_node_usage` (live `list_kind("local", "/apis/metrics.k8s.io/v1beta1/nodes")`). Session-scoped where live.

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement** `units.py` and the cluster/metrics part of `repo.py`.
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: unit parsing, cluster records and daily metrics"`

---

### Task 7: Resource upserts and per-cluster collection

**Files:**
- Create: `inventory/collector/transform.py` (minimal: metadata, labels, keep paths, images, hash), `inventory/collector/cluster.py`
- Modify: `inventory/db/repo.py`
- Test: `tests/test_collect_cluster.py`

**Interfaces:**
- Consumes: `KindSpec`, `RancherClient`, repo cluster functions.
- Produces:
  - `@dataclass Trimmed`: `kind: str`, `namespace: str`, `name: str`, `created_at: str | None`, `data: dict`, `labels: dict[str, str]`, `annotations: list[tuple[str, str, bool]]` (key, value, custom), `images: list[str]`, `addresses: list[Address]`, `hash: str`. Task 7 leaves `annotations` and `addresses` empty; Task 12 fills them.
  - `def trim(kind: KindSpec, obj: dict, rules: Rules | None = None) -> Trimmed` — copies only `keep` dotted paths into `data`; images from `spec.containers`, `spec.initContainers`, `spec.template.spec.*`, `spec.jobTemplate.spec.template.spec.*`; `hash` is SHA-256 of `json.dumps([data, labels, annotations, images, addresses], sort_keys=True)`.
  - `def upsert_resources(conn, site: str, cluster_id: str, run_id: int, items: list[Trimmed]) -> int` — per spec "Write rules": unchanged hash updates only `last_seen_run`; changed hash rewrites the row and its side-table rows. Returns rows rewritten.
  - `def delete_stale(conn, site: str, cluster_id: str, kind: str, run_id: int) -> int`.
  - `@dataclass ClusterResult`: `status: str` (`ok` | `failed`), `kinds_total: int`, `kinds_done: int`, `objects: int`, `error: str | None`, `kind_status: dict[str, str]`.
  - `async def collect_cluster(client: RancherClient, conn, site: str, cluster: dict, run_id: int, kinds: dict[str, KindSpec], rules: Rules | None, on_progress: Callable[[int, int], None], should_stop: Callable[[], bool]) -> ClusterResult`.

Rules for `collect_cluster`:
- Cluster `state != "active"` gives `failed` with `error="cluster state: <state>"`, no list calls.
- Per kind: `list_kind`, then one transaction: `upsert_resources` + `delete_stale` + `run_kinds` row `ok`.
- `RancherError` 403 gives `run_kinds` `forbidden`; 404 gives `not_found`; neither fails the cluster nor deletes rows.
- Any other error (timeout, 5xx, 410 mid-pagination) gives `run_kinds` `failed`, no delete for that kind, and the cluster ends `failed` after trying the remaining kinds.
- Also fetches node usage once (`metrics.k8s.io` nodes) and calls `write_metrics`.
- Calls `on_progress(kinds_done, kinds_total)` after each kind.

- [ ] **Step 1: Write the failing tests** (live, against `local`, using the `nodes` kind from the stub `kinds.yaml` plus an extra `KindSpec("secrets", "/api/v1/secrets", "namespaced", [])` built in the test)

```python
def test_collect_local_nodes(db, live_client, local_cluster, kinds):
    r = asyncio.run(collect_cluster(live_client, db, "dev", local_cluster, 1, kinds, None, noop, never))
    assert r.status == "ok"
    assert db.execute("SELECT count(*) FROM resources WHERE kind='nodes'").fetchone()[0] == local_cluster["nodeCount"]

def test_second_run_rewrites_nothing(...):        # run 2: upsert_resources returns 0; last_seen_run == 2 for all rows

def test_forbidden_kind_does_not_fail(...):       # with "secrets" added: r.status == "ok", r.kind_status["secrets"] == "forbidden"

def test_stale_rows_deleted_only_after_full_list(...):
    # insert a row kind='nodes' name='gone' last_seen_run=0 -> after run it is deleted
    # insert a row kind='secrets' name='keep' -> after run it still exists (kind was forbidden)

def test_failed_kind_keeps_rows_and_fails_cluster(...):
    # KindSpec("badpods", "/api/v1/pods?fieldSelector=bogus%3D1", "namespaced", []) -> Rancher returns 400
    # pre-inserted kind='badpods' row still exists; r.status == "failed"; r.kind_status["badpods"] == "failed";
    # every other kind still ran (r.kinds_done == r.kinds_total)
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: per-cluster collection with hash upserts and stale-row rule"`

---

### Task 8: Runs, progress, cancel, retry, collector thread and `collect-once` (phase 2 milestone)

**Files:**
- Create: `inventory/collector/runner.py`, `inventory/collector/thread.py`
- Modify: `inventory/cli.py` (`collect-once`)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `collect_cluster`, `RancherClient`, `load_sites`, `load_kinds`.
- Produces:
  - `@dataclass Scope`: `site: str | None = None`, `cluster_id: str | None = None`, `clusters: list[tuple[str, str]] | None = None`; `to_json()`, `from_json()`.
  - `class RunInProgress(Exception)` with `.run_id`.
  - `def create_run(conn, trigger: str, triggered_by: str | None, scope: Scope) -> int` — raises `RunInProgress` if a `running` row exists (checked and inserted in one `BEGIN IMMEDIATE` transaction).
  - `async def execute_run(settings: Settings, run_id: int) -> str` — lists clusters per site, inserts `run_clusters` rows `pending`, sets `clusters_total`, runs `collect_cluster` with an `asyncio.Semaphore(concurrency_per_site)` per site. Before starting each cluster it checks `runs.cancel_requested`; if set, the remaining clusters become `skipped`. Final status: `cancelled` if cancel requested, else `ok` if all ok, `failed` if all failed, else `partial`. A site whose `list_clusters` fails marks the run `partial` (or `failed` if no site worked) and records the error in `runs.error`. Keeps `run_clusters` for the last 30 runs only. Uses one writer connection.
  - `def request_cancel(conn, run_id: int) -> None`.
  - `def retry_scope(conn, run_id: int) -> Scope` — failed clusters of that run as `Scope(clusters=[...])`.
  - `def mark_interrupted(conn) -> int` — `running` runs become `interrupted`, their `pending`/`running` clusters become `skipped`.
  - `def last_good_run(conn) -> sqlite3.Row | None` — newest run with status `ok` or `partial`.
  - `class CollectorThread(threading.Thread)` — `CollectorThread(settings)`; `start()`; `request_run(trigger: str, triggered_by: str | None, scope: Scope) -> int` (thread-safe; raises `RunInProgress`); `stop()`. On start: `mark_interrupted`, then starts a run if `last_good_run` is missing or older than `stale_after_hours`. Daily loop: sleeps until the next `collect_daily_at` (container local time) and skips that start if a run is active. Does nothing when `collector_enabled` is false except serve `request_run`.
  - `collect-once`: `create_run("manual", "cli", Scope())`, `asyncio.run(execute_run(...))`, prints the final status, exit 0 for `ok`/`partial`, 1 otherwise.

- [ ] **Step 1: Write the failing tests**

```python
def test_second_run_refused(db):
    rid = create_run(db, "manual", "a", Scope())
    with pytest.raises(RunInProgress) as e:
        create_run(db, "schedule", None, Scope())
    assert e.value.run_id == rid

def test_mark_interrupted(db):  # running run + one 'ok' and one 'running' cluster -> run 'interrupted', 'ok' cluster unchanged, other 'skipped'

@pytest.mark.live
def test_full_run_dev(settings, db_path):
    rid = create_run(connect(db_path), "manual", "t", Scope())
    assert asyncio.run(execute_run(settings, rid)) == "ok"
    row = connect(db_path).execute("SELECT * FROM run_clusters WHERE run_id=?", [rid]).fetchone()
    assert row["cluster_id"] == "local" and row["status"] == "ok"
    assert row["kinds_done"] == row["kinds_total"]

@pytest.mark.live
def test_cancel_before_start(settings, db_path):  # request_cancel right after create_run -> status 'cancelled', cluster 'skipped'

@pytest.mark.live
def test_bad_token_site_fails(settings, db_path, monkeypatch):  # token 'token-bad00:x' -> 'failed', runs.error mentions 401

def test_next_daily_start():  # helper next_start(now=2026-11-12T03:00, at="02:00") == 2026-11-13T02:00; now=01:00 -> same day 02:00
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Run `collect-once` against dev and record the duration**

Run: `env DATA_DIR=dev/data SITES_FILE=dev/config/sites.yaml DEFAULTS_DIR=config/defaults uv run --env-file .env inventory collect-once`
Expected: `ok`, printed duration.

- [ ] **Step 6: Commit** — `git commit -m "feat: runs with progress, cancel, retry, interruption and collector thread"`

---

### Task 9: Users, sessions and login

**Files:**
- Create: `inventory/web/auth.py`, `tests/test_auth.py`
- Modify: `inventory/cli.py` (`user add|passwd|list|disable`)

**Interfaces:**
- Produces:
  - `def hash_password(password: str) -> str` — format `scrypt$16384$8$1$<salt hex>$<hash hex>`, 16-byte salt, `dklen=64`. Raises `ValueError` if shorter than 12.
  - `def verify_password(password: str, stored: str) -> bool` — constant-time compare.
  - `@dataclass User`: `id`, `username`, `role` (`admin` | `viewer`), `disabled: bool`.
  - `def create_user(conn, username: str, password: str, role: str) -> User`, `def set_password(conn, username, password)`, `def set_disabled(conn, username, disabled: bool)`, `def list_users(conn) -> list[User]`.
  - `def ensure_first_admin(conn, settings) -> bool` — creates the admin only if `users` is empty and `ADMIN_PASSWORD` is set.
  - `class LoginLocked(Exception)`.
  - `def login(conn, username: str, password: str, hours: int, now: datetime) -> str | None` — returns a raw session token (`secrets.token_urlsafe(32)`) or None; records failures; raises `LoginLocked` after 5 failures within 15 minutes; disabled users cannot log in.
  - `def session_user(conn, token: str, now: datetime) -> User | None` — None if missing, expired or user disabled.
  - `def logout(conn, token: str) -> None`.
  - `def csrf_token(secret: str, token: str) -> str` — HMAC-SHA256 hex of the session token hash.
  - CLI: `inventory user add <name> --role admin|viewer` (reads password with `getpass`), `user passwd <name>`, `user list`, `user disable <name>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$16384$8$1$") and verify_password("correct horse battery", h)
    assert not verify_password("wrong password!!", h)

def test_short_password_rejected():  # "short" -> ValueError

def test_lockout_after_five(db):  # 5 wrong -> 6th call with right password raises LoginLocked; at now+16min succeeds

def test_disabled_user_session_invalid(db):
    create_user(db, "v", "viewer-password-1", "viewer")
    t = login(db, "v", "viewer-password-1", 12, NOW)
    set_disabled(db, "v", True)
    assert session_user(db, t, NOW) is None

def test_session_expires(db):  # session_user at NOW + 13h -> None
def test_only_hash_stored(db):  # raw token not found in sessions.token_hash
def test_first_admin_once(db, settings):  # ensure_first_admin -> True, then False
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: local users, scrypt passwords, sessions and lockout"`

---

### Task 10: Web app, layout, overview, clusters and run pages (phase 3 milestone)

**Files:**
- Create: `inventory/web/app.py`, `inventory/web/deps.py`, `inventory/web/queries.py`, `inventory/web/routes/{auth,home,clusters,runs,admin,health}.py`, templates `base.html`, `login.html`, `home.html`, `clusters.html`, `cluster.html`, `runs.html`, `run.html`, `_progress.html`, `admin_users.html`, `inventory/web/static/{htmx.min.js,chart.umd.min.js,app.css}`
- Modify: `inventory/cli.py` (`serve`, `healthcheck`)
- Test: `tests/test_web_core.py`

**Interfaces:**
- Consumes: `auth.*`, `CollectorThread`, `create_run`, `request_cancel`, `retry_scope`, `last_good_run`, `connect`.
- Produces:
  - `def create_app(settings: Settings, collector: CollectorThread | None) -> FastAPI` — mounts `/static`; runs `migrate` and `ensure_first_admin` at startup.
  - Dependencies in `deps.py`: `current_user` (redirect to `/login` for pages, 401 for HTMX/API), `require_admin` (403), `check_csrf` (form field `csrf`), `read_db` (read-only connection per request).
  - `def header_context(conn, settings) -> dict` in `queries.py` — `data_as_of`, `stale: bool`, `active_run: {id, done, total, failed} | None`.
  - Routes: `GET/POST /login`, `POST /logout`, `GET /`, `GET /clusters`, `GET /clusters/{site}/{cluster_id}`, `GET /runs`, `GET /runs/{id}`, `POST /runs` (form: `site`, `cluster_id` optional), `POST /runs/{id}/cancel`, `POST /runs/{id}/retry`, `GET /runs/progress` (HTMX fragment), `GET/POST /admin/users`, `GET /healthz`.
  - `serve`: starts `CollectorThread` then uvicorn on `0.0.0.0:LISTEN_PORT`, with TLS if both cert and key are set.
  - `healthcheck`: GET `/healthz` on `127.0.0.1` (https with verify off if TLS); exit 0 only if `{"ok": true}`.

Page content:
- Header on every page: nav links, "Data as of <time>" from `last_good_run`, a warning when older than `STALE_AFTER_HOURS`, for admins a "Refresh now" button or, during a run, `_progress.html` ("Refreshing: 37 of 124 clusters, 2 failed") polled with `hx-get="/runs/progress" hx-trigger="every 3s"` only while a run is active.
- `/`: counts of sites, clusters, nodes; failed clusters from the last run.
- `/clusters`: site, name, state, provider, Kubernetes version, nodes, CPU and memory allocatable/requested/used, last ok time, last error (admins only).
- `/clusters/{site}/{id}`: the same fields, a "Refresh this cluster" button (admin), and a Chart.js line chart of `cluster_metrics` per day.
- `/runs`: last 30 runs. `/runs/{id}`: per-cluster rows (status, kinds done/total, objects, duration, error), failed first, totals per site; "Cancel" while running, "Retry failed" when any failed.
- `/healthz`: without session `{"ok": bool}` only, where ok means the DB opens and the last good run is newer than `STALE_AFTER_HOURS`.

- [ ] **Step 1: Download front-end assets once** (connected machine)

```bash
curl -fsSLo inventory/web/static/htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
curl -fsSLo inventory/web/static/chart.umd.min.js https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js
```

- [ ] **Step 2: Write the failing tests** (FastAPI `TestClient`, `collector=None`; the live fixture `collected_db` runs one real `execute_run` against dev into a session tmp DB)

```python
@pytest.mark.parametrize("path", ["/", "/clusters", "/runs", "/admin/users", "/clusters/dev/local"])
def test_pages_need_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

def test_healthz_public(client):  # 200, body keys == {"ok"}

def test_viewer_cannot_refresh(viewer_client):  # POST /runs -> 403
def test_post_without_csrf_rejected(admin_client):  # POST /runs without csrf -> 403
def test_refresh_while_running_refused(admin_client, db):  # existing running run -> POST /runs returns 409

@pytest.mark.live
def test_clusters_page_shows_local(admin_client_collected):
    r = admin_client_collected.get("/clusters")
    assert "local" in r.text and "rke2" in r.text and "Data as of" in r.text

@pytest.mark.live
def test_run_page_lists_cluster(admin_client_collected):  # /runs/1 contains "local" and "ok"

def test_no_external_urls_in_templates():
    for p in Path("inventory/web/templates").glob("*.html"):
        assert not re.search(r'(src|href)="https?://', p.read_text())
```

- [ ] **Step 3: Run and see them fail**
- [ ] **Step 4: Implement**
- [ ] **Step 5: Run and see them pass**
- [ ] **Step 6: Run locally and click through**

Run: `env DATA_DIR=dev/data SITES_FILE=dev/config/sites.yaml DEFAULTS_DIR=config/defaults LISTEN_PORT=8080 uv run --env-file .env inventory serve`
Open `http://localhost:8080`, log in as admin, press "Refresh now", watch the progress bar and run page, then cancel a second run.

- [ ] **Step 7: Commit** — `git commit -m "feat: web app with login, overview, clusters, runs and refresh"`

---

### Task 11: Core kinds and side tables

**Files:**
- Modify: `config/defaults/kinds.yaml`, `inventory/collector/transform.py`, `inventory/db/repo.py` (labels, images side tables), `inventory/collector/cluster.py` (pod request totals into metrics)
- Test: `tests/test_transform.py`

**Interfaces:**
- Consumes: `KindSpec`, `Trimmed`, `trim`.
- Produces: the full `kinds.yaml`. The 10 original kinds (this plan's choice, since the spec does not list them): `nodes`, `namespaces`, `pods`, `deployments`, `statefulsets`, `daemonsets`, `persistentvolumes`, `persistentvolumeclaims`, `storageclasses`, `configmaps` (`metadata_only: true`, so `data` is never read). Plus `services`, `ingresses`, `customresourcedefinitions`, `helmreleases` with `path`, `scope` and `keep` exactly as spec 6.1. Extra fields per spec 6.3:
  - nodes: `status.addresses`, `spec.podCIDR`, `status.nodeInfo.kubeletVersion`, `status.capacity`, `status.allocatable`; `data.roles` from `node-role.kubernetes.io/<role>` label keys.
  - deployments, statefulsets, daemonsets: `spec.replicas`, `status.readyReplicas`; `data.pvc_claims` (claim names from pod template volumes), `data.hostpath` bool; `annotations: true`.
  - statefulsets: `data.volume_claim_templates` = list of `{name, storage_class, size}`.
  - namespaces: `annotations: true`, `status.phase`.
  - persistentvolumes: `spec.capacity.storage`, `spec.storageClassName`, `spec.claimRef.namespace`, `spec.claimRef.name`, `status.phase`; `data.local` true when `spec.local` or `spec.hostPath` is set.
  - persistentvolumeclaims: `spec.storageClassName`, `spec.resources.requests.storage`, `status.phase`, `status.capacity.storage`; `annotations: true`.
  - pods: `spec.nodeName`, `status.phase`, `status.qosClass`; `data.cpu_req_m`, `data.mem_req_b` summed over containers.
  - customresourcedefinitions: `spec.versions` trimmed to `name`, `served`, `storage`, `deprecated`.
  - helmreleases: also `spec.resources` trimmed to `apiVersion`, `kind` (for plan 2; no names stored).
- `cluster_metrics.pods` and `cpu_req_m`/`mem_req_b` stay from `/v3/clusters` (verified fields); pod sums are only stored in `data`.

- [ ] **Step 1: Write the failing live tests** (session fixture `dev_lists`: kind name to items from `local`, fetched once)

```python
def test_helm_values_never_kept(dev_lists, kinds):
    for o in dev_lists["helmreleases"]:
        t = trim(kinds["helmreleases"], o)
        assert "values" not in json.dumps(t.data["spec"]["chart"]) and "values" not in t.data["spec"]
        assert t.data["spec"]["chart"]["metadata"]["name"]

def test_crd_schema_dropped(dev_lists, kinds):
    for o in dev_lists["customresourcedefinitions"]:
        for v in trim(kinds["customresourcedefinitions"], o).data["spec"]["versions"]:
            assert set(v) <= {"name", "served", "storage", "deprecated"}

def test_configmap_has_no_data(dev_lists, kinds):  # trimmed data == {} for every configmap
def test_node_roles(dev_lists, kinds):  # every trimmed node has data["roles"] list; at least one has "control-plane"
def test_images_found(dev_lists, kinds):  # every deployment has >= 1 image
def test_hash_stable(dev_lists, kinds):  # trim twice -> same hash

@pytest.mark.live
def test_counts_match_rancher(collected_db, dev_lists):
    for kind, items in dev_lists.items():
        n = collected_db.execute("SELECT count(*) FROM resources WHERE kind=?", [kind]).fetchone()[0]
        assert n == len(items), kind
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: core kinds, services, ingresses, CRDs and Helm releases"`

---

### Task 12: Annotation rules and addresses

**Files:**
- Create: `config/defaults/rules.yaml`, `inventory/collector/rules.py`, `inventory/collector/annotations.py`, `inventory/collector/addresses.py`, `tests/test_annotations.py`, `tests/test_addresses.py`
- Modify: `inventory/collector/transform.py`, `inventory/db/repo.py` (annotations, addresses side tables)

**Interfaces:**
- Produces:
  - `rules.yaml` with `annotations` (spec 6.5, plus `kubernetes.io/ingress.class` in `keep`), `system_namespaces`, `system_crd_groups` (spec 6.6).
  - `@dataclass Rules`: `max_value_chars: int`, `system_domains: list[str]`, `keep: list[str]`, `drop: list[str]`, `system_namespaces: list[str]`, `system_crd_groups: list[str]`. `def load_rules(settings) -> Rules` (override file, else default).
  - `def matches(key: str, entry: str) -> bool` — true when `key == entry`, or the key's domain (part before `/`) equals `entry` or ends with `"." + entry`.
  - `def filter_annotations(ann: dict[str, str], rules: Rules) -> list[tuple[str, str, bool]]` — drop wins, then keep, then system domains are skipped, others are custom; values cut to `max_value_chars`.
  - `@dataclass(frozen=True) Address`: `role: str`, `address: str`, `ports: str | None`.
  - `def extract_addresses(kind: str, obj: dict) -> list[Address]` — roles per spec 6.4; `cluster-ip` skips `None` (headless); ports formatted `"443/TCP,80/TCP"`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("key,entry,expected", [
    ("node.kubernetes.io/foo", "kubernetes.io", True),
    ("kubernetes.io/foo", "kubernetes.io", True),
    ("notkubernetes.io/foo", "kubernetes.io", False),
    ("meta.helm.sh/release-name", "meta.helm.sh/release-name", True),
    ("team.example.com/owner", "cattle.io", False),
])
def test_matches(key, entry, expected): ...

def test_filter_annotations_rules(rules):
    out = dict((k, (v, c)) for k, v, c in filter_annotations({
        "kubectl.kubernetes.io/last-applied-configuration": "{}",
        "field.cattle.io/projectId": "c-1:p-1",
        "deployment.kubernetes.io/revision": "3",
        "team.example.com/owner": "x" * 300,
    }, rules))
    assert set(out) == {"field.cattle.io/projectId", "team.example.com/owner"}
    assert out["field.cattle.io/projectId"][1] is False
    assert out["team.example.com/owner"] == ("x" * 256, True)
```

Live: `test_node_addresses` (each node from `dev_lists` gives a `node-internal` address equal to its `InternalIP`), `test_service_cluster_ips` (non-headless services give `cluster-ip`, headless give none), `test_ingress_nginx_lb` (`collected_db` has at least one `addresses` row for `192.168.50.210`, since ingress-nginx serves that IP; if this fails, record what `status.loadBalancer` actually holds on dev and adjust the expected role, not the code).

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: annotation rules and address extraction"`

---

### Task 13: Query page, presets and check page (phase 4 milestone)

**Files:**
- Create: `inventory/web/routes/{query,presets,check}.py`, templates `query.html`, `presets.html`, `check.html`
- Modify: `inventory/web/queries.py`
- Test: `tests/test_query.py`

**Interfaces:**
- Produces:
  - `ALLOWED_FILTERS = {"site", "cluster", "kind", "namespace", "name", "label", "image"}`.
  - `def build_query(filters: dict[str, str]) -> tuple[str, dict]` — returns SQL over `resources` joined to `clusters`, values only as named parameters; `name` and `image` are substring (`LIKE '%' || :x || '%'`); `label` is `key` or `key=value` through `resource_labels`. Raises `ValueError` for any key not in `ALLOWED_FILTERS`. Plan 2 extends this set.
  - `GET /query?…` — form plus results (site, cluster, kind, namespace, name), run with `run_readonly_query`.
  - `GET /presets`, `POST /presets` (name, filters JSON from current query), `POST /presets/{id}/delete` (creator or admin).
  - `def check_preset(conn, preset_id: int) -> list[dict]` — one row per cluster: `present` (any match), `absent`, or `unknown` (cluster never collected ok, or its latest `run_clusters` row is `failed`, or the kind in the filter was not `ok` in `run_kinds` for that cluster's last ok run).
  - `GET /check?preset=<id>` — table of that result.

- [ ] **Step 1: Write the failing tests**

```python
def test_unknown_filter_rejected():
    with pytest.raises(ValueError): build_query({"data": "x"})

def test_values_are_parameters():
    sql, params = build_query({"name": "x'; DROP TABLE users; --"})
    assert "DROP" not in sql and params

@pytest.mark.live
def test_query_finds_coredns(collected_db):
    sql, p = build_query({"kind": "deployments", "name": "coredns"})
    assert any(r["cluster_id"] == "local" for r in run_readonly_query(collected_db, sql, p))

@pytest.mark.live
def test_check_present_and_unknown(collected_db):
    # preset kind=deployments name=rke2-coredns -> local is 'present'
    # after UPDATE run_clusters SET status='failed' for local -> 'unknown'
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: query page, presets and check page"`

---

### Task 14: Classification and profiles

**Files:**
- Create: `inventory/collector/classify.py`, `tests/test_classify.py`
- Modify: `inventory/collector/cluster.py` (call `build_profiles` at the end of a successful cluster)

**Interfaces:**
- Consumes: `Rules.system_namespaces`, `Rules.system_crd_groups`, rows in `resources`, `resource_labels`.
- Produces:
  - `def is_system_namespace(ns: str, rules: Rules) -> bool` — `fnmatch` on patterns.
  - `def is_system_crd_group(group: str, rules: Rules) -> bool` — `fnmatch`.
  - `@dataclass Flags`: `has_workloads`, `stateful`, `helm`, `custom_crds`, `hostpath` (all bool).
  - `def class_label(f: Flags) -> str` — table in spec 6.6. `stateful` with `custom_crds` but not `helm` gives `stateful-crd`.
  - `def build_profiles(conn, site: str, cluster_id: str, run_id: int, rules: Rules) -> Flags` — rebuilds `namespace_profile` rows (spec section 5 columns) and the `cluster_profile` row from stored resources. `helm_releases` counts `helmreleases` with `spec.info.status == "deployed"`. `network` stays NULL in dev (provisioning API is 403).

- [ ] **Step 1: Write the failing tests**

Pure (spec table, literal flags):

```python
@pytest.mark.parametrize("flags,label", [
    (Flags(False, False, False, False, False), "empty"),
    (Flags(True, False, False, False, False), "stateless"),
    (Flags(True, True, False, False, False), "stateful"),
    (Flags(True, True, True, False, False), "stateful-helm"),
    (Flags(True, True, False, True, False), "stateful-crd"),
    (Flags(True, True, True, True, False), "stateful-helm-crd"),
])
def test_class_label(flags, label): assert class_label(flags) == label

@pytest.mark.parametrize("ns,system", [("kube-system", True), ("cattle-fleet-system", True),
    ("local", True), ("cert-manager", True), ("kagent", False), ("default", False)])
def test_system_namespace(ns, system, rules): ...
```

Live (against what `local` really holds):

```python
def test_local_profile(collected_db, rules, dev_lists):
    f = build_profiles(collected_db, "dev", "local", 1, rules)
    user_ns_workloads = [d for d in dev_lists["deployments"] + dev_lists["statefulsets"] + dev_lists["daemonsets"]
                         if not is_system_namespace(d["metadata"]["namespace"], rules)]
    assert f.has_workloads == bool(user_ns_workloads)
    row = collected_db.execute("SELECT * FROM cluster_profile WHERE cluster_id='local'").fetchone()
    assert row["class"] == class_label(f)
    assert row["helm_releases"] == sum(1 for h in dev_lists["helmreleases"] if h["spec"]["info"]["status"] == "deployed")
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: cluster and namespace classification"`

Note: with one dev cluster only one class is observed. To see others on dev, deploy a test StatefulSet with a PVC in a user namespace, refresh, and check the class changes. That is a manual step in Task 16.

---

### Task 15: Inventory views

**Files:**
- Create: `inventory/web/routes/{inventory,network,helm,crds,annotations}.py`, templates `inventory.html`, `network.html`, `helm.html`, `crds.html`, `annotations.html`, `_class_badge.html`
- Modify: `home.html` (clusters per class card), `cluster.html` (profile, namespace table, services, ingresses, Helm releases, CRDs), `inventory/web/queries.py`
- Test: `tests/test_web_inventory.py`

**Interfaces:**
- Consumes: `cluster_profile`, `namespace_profile`, `addresses`, `resource_annotations`, `resources`.
- Produces (query functions in `queries.py`, each returning `list[dict]`):
  - `inventory_rows(conn, site=None, cls=None, flag=None, label=None)` — columns per spec section 7 `/inventory`.
  - `search_addresses(conn, q: str)` — the SQL in spec section 7 `/network`. Plus `load_balancers(conn)`, `ingress_hosts(conn)`, `node_ips(conn)`, `nodeport_and_external_services(conn)`, `duplicate_addresses(conn)` (an `lb`, `ingress-lb` or `ingress-host` address in more than one cluster).
  - `helm_charts(conn)` — one row per chart: chart name, versions with cluster counts. `helm_releases(conn, chart)`.
  - `crd_groups(conn, custom_only: bool, rules)` — group, cluster count, custom flag.
  - `annotation_keys(conn)` — custom keys with object and cluster counts. `annotation_objects(conn, key)`.

- [ ] **Step 1: Write the failing live tests**

```python
def test_inventory_has_local(admin_client_collected):  # /inventory contains "local" and its class label
def test_network_search_node_ip(admin_client_collected, dev_lists):
    ip = next(a["address"] for a in dev_lists["nodes"][0]["status"]["addresses"] if a["type"] == "InternalIP")
    assert "local" in admin_client_collected.get(f"/network?q={ip}").text
def test_network_prefix(admin_client_collected):  # q=192.168.50. returns >= 1 row
def test_helm_page_lists_rancher_chart(admin_client_collected):  # /helm contains "rancher-webhook" and "cert-manager"
def test_helm_matches_catalog(collected_db, dev_lists):  # helm_releases total == len(dev_lists["helmreleases"])
def test_crds_custom_toggle(admin_client_collected):  # /crds?custom_only=1 does not contain "management.cattle.io"
def test_cluster_page_profile(admin_client_collected):  # /clusters/dev/local contains "Namespaces" and "Helm releases"
```

- [ ] **Step 2: Run and see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run and see them pass**
- [ ] **Step 5: Commit** — `git commit -m "feat: inventory, network, helm, CRD and annotation pages"`

---

### Task 16: Build the image and run it against dev (plan 1 done)

**Files:**
- Modify: `README.md` (add "Run on the dev Mac" section with the commands below)

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -v`
Expected: all pass, including `live`.

- [ ] **Step 2: Build**

Run: `docker build -t rancher-inventory:1.0.0-dev .`

- [ ] **Step 3: Run**

```bash
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

Expected: log shows first admin created, a run started (no good run yet) and finished `ok`. If `/data` is not writable, run `chmod 777 dev/data` (Docker Desktop on macOS maps ownership; this is dev only).

- [ ] **Step 4: Container health**

Run: `docker inspect --format '{{.State.Health.Status}}' rancher-inventory` (after 1 minute)
Expected: `healthy`.

- [ ] **Step 5: Walk through in the browser** at `http://localhost:8443`

Check each item and note the result:
- [ ] Login works; every page redirects to `/login` when logged out.
- [ ] Header shows "Data as of" with the run time.
- [ ] "Refresh now" shows the progress bar, which disappears when the run ends; `/runs/{id}` shows `local` with all kinds done.
- [ ] `/clusters` totals (nodes, CPU, memory) match the Rancher dashboard for `local`.
- [ ] `/helm` lists the same 18 releases as Rancher's Apps > Installed Apps, including the two `failed` ones.
- [ ] `/network?q=192.168.50.210` finds the ingress or node.
- [ ] `/inventory` shows a class for `local`. Then create a test StatefulSet with a PVC in a new namespace (as a Rancher admin), refresh, and confirm the class becomes `stateful`. Delete it afterwards and refresh again.
- [ ] A preset for `deployments` named `rke2-coredns` shows `local` as present on `/check`.
- [ ] Restart the container during a run (`docker restart rancher-inventory`); the run shows `interrupted` and the pages still show data.
- [ ] Browser network tab: every request goes to `localhost:8443`.

- [ ] **Step 6: Commit** — `git commit -m "docs: run the image against the dev Rancher"`
