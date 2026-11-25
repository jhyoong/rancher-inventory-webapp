-- Admin-defined custom columns on clusters, their per-cluster values and a change log.
-- Values are keyed by site + cluster_id, so they survive collection runs and cluster removal.
-- updated_by is a plain user id with no foreign key, so the log survives user changes.

CREATE TABLE custom_columns (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
    type       TEXT NOT NULL,
    choices    TEXT NOT NULL DEFAULT '[]',
    position   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE custom_values (
    site       TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    column_id  INTEGER NOT NULL,
    value      TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (site, cluster_id, column_id)
);

CREATE TABLE custom_values_log (
    id          INTEGER PRIMARY KEY,
    site        TEXT NOT NULL,
    cluster_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    column_id   INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_by  INTEGER,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_custom_values_log_cluster ON custom_values_log (site, cluster_id);
