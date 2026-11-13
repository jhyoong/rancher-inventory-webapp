-- Migration overview: cluster API endpoint and machine pools.

ALTER TABLE clusters ADD COLUMN api_endpoint TEXT;
ALTER TABLE clusters ADD COLUMN pools_status TEXT;  -- ok | none | not_collected, NULL before the first run
ALTER TABLE clusters ADD COLUMN prov_name TEXT;

CREATE TABLE machine_pools (
    site                TEXT NOT NULL,
    cluster_id          TEXT NOT NULL,
    name                TEXT NOT NULL,
    quantity            INTEGER,
    roles               TEXT,   -- JSON list
    labels              TEXT,   -- JSON map
    taints              TEXT,   -- JSON list of key:effect
    machine_config_kind TEXT,
    PRIMARY KEY (site, cluster_id, name)
);
