ALTER TABLE smart_lists ADD COLUMN facets TEXT NOT NULL DEFAULT '{}';
INSERT INTO schema_migrations(version) VALUES(2);
