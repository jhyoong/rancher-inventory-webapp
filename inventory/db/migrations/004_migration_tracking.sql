-- Manual migration tracking: status, wave and note per cluster, with a change log.
-- updated_by is a plain user id with no foreign key, so the log survives user changes.

CREATE TABLE migration_tracking (
    site       TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    wave       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    updated_by INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (site, cluster_id)
);

CREATE TABLE migration_tracking_log (
    id         INTEGER PRIMARY KEY,
    site       TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    wave       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    updated_by INTEGER,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_migration_tracking_log_cluster ON migration_tracking_log (site, cluster_id);

CREATE TABLE removed_clusters (
    site       TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    removed_at TEXT NOT NULL,
    PRIMARY KEY (site, cluster_id)
);
