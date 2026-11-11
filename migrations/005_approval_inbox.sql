ALTER TABLE papers ADD COLUMN admission TEXT NOT NULL DEFAULT 'approved'
 CHECK(admission IN ('pending','approved','dismissed'));
CREATE INDEX papers_admission_added ON papers(admission,added);
CREATE TABLE inbox_decisions (
 id INTEGER PRIMARY KEY,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 before_admission TEXT NOT NULL,
 after_admission TEXT NOT NULL,
 paper_modified TEXT NOT NULL,
 timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 undo_of INTEGER REFERENCES inbox_decisions(id)
);
CREATE INDEX inbox_decisions_paper ON inbox_decisions(paper_id,id);
INSERT INTO schema_migrations(version) VALUES(5);
