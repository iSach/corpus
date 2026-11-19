-- OpenReview forum ids identify papers that have neither a DOI nor an arXiv id,
-- such as workshop papers added from a NeurIPS/ICML/ICLR conference page.
ALTER TABLE papers ADD COLUMN openreview_id TEXT;
CREATE UNIQUE INDEX papers_openreview_id ON papers(openreview_id);
INSERT INTO schema_migrations(version) VALUES(13);
