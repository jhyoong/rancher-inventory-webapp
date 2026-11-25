-- Rancher members (users and groups with access) of each downstream cluster.
-- Rows are replaced per cluster after each member read; ord keeps the display order.

CREATE TABLE cluster_members (
    site         TEXT NOT NULL,
    cluster_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    subject      TEXT NOT NULL,
    display_name TEXT NOT NULL,
    username     TEXT,
    disabled     INTEGER NOT NULL DEFAULT 0,
    grants       TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    PRIMARY KEY (site, cluster_id, kind, subject)
);

ALTER TABLE clusters ADD COLUMN members_status TEXT;
ALTER TABLE clusters ADD COLUMN members_at TEXT;
