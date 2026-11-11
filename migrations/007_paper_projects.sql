CREATE TABLE paper_projects (
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 field_id TEXT NOT NULL REFERENCES fields(id),
 PRIMARY KEY(paper_id,field_id)
);
CREATE INDEX paper_projects_field ON paper_projects(field_id,paper_id);
INSERT INTO paper_projects(paper_id,field_id) SELECT id,field FROM papers WHERE field IS NOT NULL;
INSERT INTO schema_migrations(version) VALUES(7);
