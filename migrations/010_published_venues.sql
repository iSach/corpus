-- Published versions of arXiv preprints. venue_source records who set the
-- venue: '' (as imported), 'manual' (the owner edited it; never overwritten),
-- or 'auto:<sources>' (applied by the published-version check).
ALTER TABLE papers ADD COLUMN venue_source TEXT NOT NULL DEFAULT '';
ALTER TABLE papers ADD COLUMN venue_checked TEXT;
ALTER TABLE papers ADD COLUMN venue_check_error TEXT;
CREATE TABLE venue_proposals (
 id INTEGER PRIMARY KEY,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 venue_key TEXT NOT NULL, venue TEXT NOT NULL,
 venue_class TEXT NOT NULL CHECK(venue_class IN ('conference','journal','other')),
 year INTEGER, doi TEXT, url TEXT NOT NULL DEFAULT '',
 sources TEXT NOT NULL DEFAULT '[]', candidates TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','rejected')),
 created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 decided TEXT,
 UNIQUE(paper_id, venue_key, year)
);
CREATE INDEX venue_proposals_status ON venue_proposals(status, paper_id);
INSERT INTO schema_migrations(version) VALUES(10);
