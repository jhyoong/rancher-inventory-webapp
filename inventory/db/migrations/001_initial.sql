CREATE TABLE resources (
    id            INTEGER PRIMARY KEY,
    site          TEXT NOT NULL,
    cluster_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    namespace     TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    data          TEXT NOT NULL DEFAULT '{}',
    hash          TEXT NOT NULL,
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

CREATE TABLE resource_annotations (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    custom      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (resource_id, key)
) WITHOUT ROWID;
CREATE INDEX resource_annotations_kv ON resource_annotations (key, value);

CREATE TABLE resource_images (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    image       TEXT NOT NULL,
    PRIMARY KEY (resource_id, image)
) WITHOUT ROWID;
CREATE INDEX resource_images_image ON resource_images (image);

CREATE TABLE addresses (
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    address     TEXT NOT NULL,
    ports       TEXT,
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
    class         TEXT NOT NULL,
    stateful      INTEGER NOT NULL,
    helm          INTEGER NOT NULL,
    custom_crds   INTEGER NOT NULL,
    hostpath      INTEGER NOT NULL,
    statefulsets  INTEGER, pvc_bound INTEGER, pvc_bytes INTEGER,
    helm_releases INTEGER, crd_groups TEXT,
    lb_services   INTEGER, ingresses INTEGER,
    network       TEXT,
    updated_run   INTEGER,
    PRIMARY KEY (site, cluster_id)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL,
    triggered_by TEXT,
    scope TEXT NOT NULL DEFAULT '{}',
    clusters_total INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE run_clusters (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    site        TEXT NOT NULL,
    cluster_id  TEXT NOT NULL,
    status      TEXT NOT NULL,
    kinds_total INTEGER,
    kinds_done  INTEGER,
    objects     INTEGER,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (run_id, site, cluster_id)
);

CREATE TABLE clusters (
    site TEXT NOT NULL, cluster_id TEXT NOT NULL,
    name TEXT NOT NULL, state TEXT, provider TEXT, k8s_version TEXT,
    labels TEXT NOT NULL DEFAULT '{}', node_count INTEGER,
    last_ok_run INTEGER, last_ok_at TEXT, last_error TEXT, last_seen_run INTEGER,
    PRIMARY KEY (site, cluster_id)
);

CREATE TABLE cluster_metrics (
    site TEXT NOT NULL, cluster_id TEXT NOT NULL, day TEXT NOT NULL,
    run_id INTEGER NOT NULL, nodes INTEGER, pods INTEGER,
    cpu_alloc_m INTEGER, cpu_req_m INTEGER, cpu_used_m INTEGER,
    mem_alloc_b INTEGER, mem_req_b INTEGER, mem_used_b INTEGER,
    PRIMARY KEY (site, cluster_id, day)
);

CREATE TABLE cluster_metrics_latest (
    site TEXT NOT NULL, cluster_id TEXT NOT NULL,
    run_id INTEGER NOT NULL, nodes INTEGER, pods INTEGER,
    cpu_alloc_m INTEGER, cpu_req_m INTEGER, cpu_used_m INTEGER,
    mem_alloc_b INTEGER, mem_req_b INTEGER, mem_used_b INTEGER,
    PRIMARY KEY (site, cluster_id)
);

CREATE TABLE run_kinds (
    run_id INTEGER NOT NULL, site TEXT NOT NULL, cluster_id TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL,
    objects INTEGER, error TEXT,
    PRIMARY KEY (run_id, site, cluster_id, kind)
);

CREATE TABLE presets (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
    filters TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
);

CREATE TABLE login_failures (username TEXT NOT NULL, failed_at TEXT NOT NULL);
CREATE INDEX login_failures_user ON login_failures (username, failed_at);
