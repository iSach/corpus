ALTER TABLE fields ADD COLUMN description TEXT NOT NULL DEFAULT '';
INSERT INTO schema_migrations(version) VALUES(3);
