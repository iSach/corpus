CREATE TABLE ai_review_jobs (
 id INTEGER PRIMARY KEY,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 input_hash TEXT NOT NULL,
 context_hash TEXT NOT NULL,
 model TEXT NOT NULL,
 prompt_version TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','failed','complete','superseded')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0,
 updated REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',
 result TEXT,
 assessment_id INTEGER REFERENCES review_assessments(id),
 UNIQUE(paper_id,context_hash,model,prompt_version)
);
CREATE INDEX ai_review_paper ON ai_review_jobs(paper_id,id);
INSERT INTO schema_migrations(version) VALUES(11);
