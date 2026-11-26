-- The AD (non-local) principal of each user member, used to match one person across sites.
-- NULL for groups, local-only accounts, and rows collected before this migration.

ALTER TABLE cluster_members ADD COLUMN principal TEXT;
CREATE INDEX cluster_members_principal ON cluster_members (principal) WHERE principal IS NOT NULL;
