-- An assessment stays current while the paper source and the topic's ID and
-- relevance criteria are unchanged; renaming a topic or editing its description
-- no longer invalidates it. Recompute every stored digest from the row's own
-- snapshot with the same function the application uses. Row IDs are unchanged,
-- so inbox decisions and full-text checks keep referring to the same evidence.
UPDATE review_assessments SET input_hash=assessment_input_hash(snapshot);
INSERT INTO schema_migrations(version) VALUES(8);
