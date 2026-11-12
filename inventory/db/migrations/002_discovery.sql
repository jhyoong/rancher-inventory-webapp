-- Plan 2: workload, storage and platform detail (spec 6.9 to 6.14), export and opt-in tables.

ALTER TABLE namespace_profile ADD COLUMN pods INTEGER;
ALTER TABLE namespace_profile ADD COLUMN pods_running INTEGER;
ALTER TABLE namespace_profile ADD COLUMN pods_standalone INTEGER;
ALTER TABLE namespace_profile ADD COLUMN restarts INTEGER;
ALTER TABLE namespace_profile ADD COLUMN cpu_req_m INTEGER;
ALTER TABLE namespace_profile ADD COLUMN cpu_lim_m INTEGER;
ALTER TABLE namespace_profile ADD COLUMN cpu_used_m INTEGER;
ALTER TABLE namespace_profile ADD COLUMN mem_req_b INTEGER;
ALTER TABLE namespace_profile ADD COLUMN mem_lim_b INTEGER;
ALTER TABLE namespace_profile ADD COLUMN mem_used_b INTEGER;
ALTER TABLE namespace_profile ADD COLUMN gpu INTEGER;
ALTER TABLE namespace_profile ADD COLUMN deployments INTEGER;
ALTER TABLE namespace_profile ADD COLUMN daemonsets INTEGER;
ALTER TABLE namespace_profile ADD COLUMN jobs INTEGER;
ALTER TABLE namespace_profile ADD COLUMN cronjobs INTEGER;
ALTER TABLE namespace_profile ADD COLUMN pvc_count INTEGER;
ALTER TABLE namespace_profile ADD COLUMN pvc_rwx INTEGER;
ALTER TABLE namespace_profile ADD COLUMN nodeport_services INTEGER;
ALTER TABLE namespace_profile ADD COLUMN netpols INTEGER;
ALTER TABLE namespace_profile ADD COLUMN has_quota INTEGER;
ALTER TABLE namespace_profile ADD COLUMN has_limitrange INTEGER;

ALTER TABLE cluster_profile ADD COLUMN cluster_uid TEXT;
ALTER TABLE cluster_profile ADD COLUMN complexity_flags TEXT;  -- JSON list
ALTER TABLE cluster_profile ADD COLUMN summary TEXT;           -- JSON of the script's summary columns

CREATE TABLE api_versions (
    site          TEXT NOT NULL,
    cluster_id    TEXT NOT NULL,
    group_version TEXT NOT NULL,
    source        TEXT NOT NULL,   -- served | in_use
    kind          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (site, cluster_id, group_version, source, kind)
);

CREATE TABLE export_name_map (
    type    TEXT NOT NULL,
    value   TEXT NOT NULL,
    hashed  TEXT NOT NULL,
    salt_id TEXT NOT NULL,   -- first 8 hex of SHA-256 of the salt
    PRIMARY KEY (type, value, salt_id)
);

CREATE TABLE optional_results (
    site       TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    collector  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    run_id     INTEGER,
    PRIMARY KEY (site, cluster_id, collector, key)
);
