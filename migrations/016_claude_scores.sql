-- Claude's review scores are the paper's scores, not drafts to accept: fill every
-- score not set by hand from the latest Claude review, and drop the "draft" label.
UPDATE papers SET rel = coalesce(rel, proposed_rel), cred = coalesce(cred, proposed_cred),
  qual = coalesce(qual, proposed_qual), reimpl = coalesce(reimpl, proposed_reimpl)
  WHERE proposed_reason LIKE 'Claude draft · %';
UPDATE papers SET proposed_reason = 'Claude · ' || substr(proposed_reason, 16) WHERE proposed_reason LIKE 'Claude draft · %';
INSERT INTO schema_migrations(version) VALUES(16);
