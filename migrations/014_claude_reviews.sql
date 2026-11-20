-- Claude reviews: a digest of the paper's contributions plus a relevance
-- judgment for each of its topics, from the abstract or the cached full text.
-- One row per completed review; the newest row for a paper is shown.
CREATE TABLE ai_reviews (
 id INTEGER PRIMARY KEY,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 job_id INTEGER REFERENCES ai_review_jobs(id) ON DELETE SET NULL,
 evidence TEXT NOT NULL CHECK(evidence IN ('abstract','full_text')),
 context_hash TEXT NOT NULL,
 model TEXT NOT NULL,
 prompt_version TEXT NOT NULL,
 result TEXT NOT NULL,
 created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX ai_reviews_paper ON ai_reviews(paper_id,id);
CREATE INDEX ai_reviews_context ON ai_reviews(paper_id,context_hash);

ALTER TABLE ai_review_jobs ADD COLUMN evidence TEXT NOT NULL DEFAULT 'abstract';
ALTER TABLE ai_review_jobs ADD COLUMN backend TEXT NOT NULL DEFAULT '';
ALTER TABLE ai_review_jobs ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_review_jobs ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_review_jobs ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0;

-- The keyword matcher is retired. Its judgments stay in each paper's history,
-- but a fresh "not assessed" row becomes current so Claude (or you) can review
-- the paper. Keyword draft scores are cleared; personal scores are untouched.
INSERT INTO review_assessments(paper_id,field,stage,priority,role,confidence,explanation,limitation,source_url,source_version,assessor,input_hash,snapshot)
SELECT a.paper_id,a.field,'abstract','unknown','unknown','unknown','No relevance assessment has been supplied yet.',
       'Review the abstract against the topic criteria.',a.source_url,a.source_version,'not-assessed',a.input_hash,a.snapshot
FROM review_assessments a
WHERE a.assessor='automatic-topic-match-v1'
  AND a.id=(SELECT max(b.id) FROM review_assessments b WHERE b.paper_id=a.paper_id AND b.field=a.field);
UPDATE papers SET proposed_rel=NULL,proposed_reason='' WHERE proposed_reason LIKE 'Automatic topic match%';
INSERT INTO schema_migrations(version) VALUES(14);
