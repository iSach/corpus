CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE fields (id TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE credential (id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE ingest_run (
 id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 agent TEXT NOT NULL, query TEXT NOT NULL DEFAULT '', proposed INTEGER NOT NULL DEFAULT 0,
 accepted INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, duplicate INTEGER NOT NULL DEFAULT 0,
 extra TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE papers (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]', venue TEXT NOT NULL DEFAULT '',
 venue_class TEXT NOT NULL DEFAULT 'preprint', year INTEGER, abstract TEXT NOT NULL DEFAULT '',
 canonical_url TEXT NOT NULL DEFAULT '', arxiv_id TEXT UNIQUE, doi TEXT UNIQUE,
 added TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 modified TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 state TEXT NOT NULL DEFAULT 'unreviewed' CHECK(state IN ('unreviewed','reviewed','archived')),
 field TEXT REFERENCES fields(id), starred INTEGER NOT NULL DEFAULT 0 CHECK(starred IN (0,1)),
 rel INTEGER CHECK(rel BETWEEN 0 AND 10), cred INTEGER CHECK(cred BETWEEN 0 AND 10),
 qual INTEGER CHECK(qual BETWEEN 0 AND 10), reimpl INTEGER CHECK(reimpl BETWEEN 0 AND 10),
 proposed_rel INTEGER CHECK(proposed_rel BETWEEN 0 AND 10), proposed_cred INTEGER CHECK(proposed_cred BETWEEN 0 AND 10),
 proposed_qual INTEGER CHECK(proposed_qual BETWEEN 0 AND 10), proposed_reimpl INTEGER CHECK(proposed_reimpl BETWEEN 0 AND 10),
 proposed_reason TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
 ingest_run_id INTEGER NOT NULL REFERENCES ingest_run(id), bibtex_key TEXT NOT NULL UNIQUE
);
CREATE INDEX papers_field ON papers(field);
CREATE INDEX papers_state ON papers(state);
CREATE INDEX papers_year ON papers(year);
CREATE INDEX papers_added ON papers(added);
CREATE INDEX papers_venue_class ON papers(venue_class);
CREATE INDEX papers_rel ON papers(rel);
CREATE INDEX papers_cred ON papers(cred);
CREATE INDEX papers_qual ON papers(qual);
CREATE INDEX papers_reimpl ON papers(reimpl);
CREATE TABLE artifacts (
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('pdf','code','data','slides','video','other')), url TEXT NOT NULL,
 local_path TEXT, fetched_at TEXT, fetch_status TEXT NOT NULL DEFAULT 'pending'
 CHECK(fetch_status IN ('pending','fetching','cached','failed','linked')), error TEXT,
 UNIQUE(paper_id,kind,url)
);
CREATE INDEX artifacts_paper ON artifacts(paper_id,kind);
CREATE INDEX artifacts_status ON artifacts(fetch_status);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE paper_tags (paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE, tag_id INTEGER NOT NULL REFERENCES tags(id), PRIMARY KEY(paper_id,tag_id));
CREATE INDEX paper_tags_tag ON paper_tags(tag_id,paper_id);
CREATE TABLE provenance (
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 run_id INTEGER NOT NULL REFERENCES ingest_run(id), query TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL, similarity REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX provenance_paper ON provenance(paper_id);
CREATE TABLE pdf_text (
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE, page INTEGER NOT NULL, text TEXT NOT NULL,
 UNIQUE(artifact_id,page)
);
CREATE INDEX pdf_text_paper ON pdf_text(paper_id);
CREATE VIRTUAL TABLE paper_fts USING fts5(paper_id UNINDEXED,title,authors,abstract,tags,notes,tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE pdf_fts USING fts5(paper_id UNINDEXED,page UNINDEXED,text,content='pdf_text',content_rowid='id',tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER pdf_insert AFTER INSERT ON pdf_text BEGIN
 INSERT INTO pdf_fts(rowid,paper_id,page,text) VALUES(new.id,new.paper_id,new.page,new.text);
END;
CREATE TRIGGER pdf_delete AFTER DELETE ON pdf_text BEGIN
 INSERT INTO pdf_fts(pdf_fts,rowid,paper_id,page,text) VALUES('delete',old.id,old.paper_id,old.page,old.text);
END;
CREATE TRIGGER pdf_update AFTER UPDATE ON pdf_text BEGIN
 INSERT INTO pdf_fts(pdf_fts,rowid,paper_id,page,text) VALUES('delete',old.id,old.paper_id,old.page,old.text);
 INSERT INTO pdf_fts(rowid,paper_id,page,text) VALUES(new.id,new.paper_id,new.page,new.text);
END;
CREATE TABLE smart_lists (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, query TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'query' CHECK(kind IN ('query','sql')));
INSERT INTO schema_migrations(version) VALUES(1);
