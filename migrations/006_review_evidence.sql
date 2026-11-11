ALTER TABLE fields ADD COLUMN relevance_criteria TEXT NOT NULL DEFAULT '';
ALTER TABLE papers ADD COLUMN inbox_origin TEXT NOT NULL DEFAULT 'discovery';
ALTER TABLE papers ADD COLUMN deferred_until TEXT;
UPDATE papers SET inbox_origin='existing'
 WHERE EXISTS (SELECT 1 FROM inbox_decisions d WHERE d.paper_id=papers.id
   AND d.id=(SELECT min(first.id) FROM inbox_decisions first WHERE first.paper_id=papers.id)
   AND d.before_admission='approved' AND d.after_admission='pending');
CREATE TABLE review_assessments (
 id INTEGER PRIMARY KEY,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 field TEXT NOT NULL REFERENCES fields(id),
 stage TEXT NOT NULL CHECK(stage IN ('abstract','full_text')),
 priority TEXT NOT NULL CHECK(priority IN ('high','medium','low','exclude','unknown')),
 role TEXT NOT NULL CHECK(role IN ('model','evaluation','supporting','adjacent','unknown')),
 confidence TEXT NOT NULL CHECK(confidence IN ('high','moderate','low','unknown')),
 explanation TEXT NOT NULL,
 limitation TEXT NOT NULL DEFAULT '',
 questions TEXT NOT NULL DEFAULT '[]',
 sections TEXT NOT NULL DEFAULT '[]',
 evidence TEXT NOT NULL DEFAULT '[]',
 source_url TEXT NOT NULL DEFAULT '',
 source_version TEXT NOT NULL DEFAULT '',
 assessor TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 snapshot TEXT NOT NULL,
 compared_to INTEGER REFERENCES review_assessments(id),
 created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX assessments_paper ON review_assessments(paper_id,field,id);
CREATE TABLE full_text_checks (
 id INTEGER PRIMARY KEY,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 abstract_id INTEGER NOT NULL UNIQUE REFERENCES review_assessments(id),
 artifact_id INTEGER REFERENCES artifacts(id) ON DELETE SET NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','preparing','ready','checked','unavailable')),
 reason TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '',
 updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX checks_status ON full_text_checks(status,paper_id);
ALTER TABLE inbox_decisions ADD COLUMN assessment_id INTEGER REFERENCES review_assessments(id);
ALTER TABLE inbox_decisions ADD COLUMN before_deferred_until TEXT;
ALTER TABLE inbox_decisions ADD COLUMN after_deferred_until TEXT;
INSERT INTO schema_migrations(version) VALUES(6);
