-- Export page memory. Papers listed here stay in the library but are left out
-- of exports until the owner includes them again. Version 11 is reserved for
-- AI reviews, which may land after this migration; the runner applies any
-- missing version, so the gap is safe.
CREATE TABLE export_exclusions (
 paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
 created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_migrations(version) VALUES(12);
