ALTER TABLE fields ADD COLUMN symbol TEXT NOT NULL DEFAULT '';
INSERT INTO schema_migrations(version) VALUES(9);
