"""Integration and security coverage for the Corpus HTTP API."""

from __future__ import annotations


from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection
from corpus import jobs, sources
from helpers import TOPIC_FIELDS, paper_body, pdf_bytes


def test_create_edit_persists_normalized_values_and_manual_scores(app_client):
    client, _ = app_client
    payload = paper_body(
        "edit-me",
        doi="DOI:10.5555/MixedCase",
        arxiv_id="https://arxiv.org/pdf/2401.12345v2.pdf",
        rel=8,
        proposed_scores={"rel": 2, "reason": "Draft only"},
    )
    created_response = client.post("/api/papers", json=payload)
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()["paper"]
    assert created["id"] == "2401.12345"
    assert created["doi"] == "10.5555/mixedcase"
    assert created["arxiv_id"] == "2401.12345"
    assert created["rel"] == 8
    assert created["proposed_rel"] == 2

    edited = client.post(
        "/api/papers",
        json={
            "id": created["id"],
            "title": "Edited title",
            "proposed_scores": {"rel": 3, "reason": "Updated draft"},
        },
    )
    assert edited.status_code == 200, edited.text
    result = client.get("/api/paper", params={"id": created["id"]})
    assert result.status_code == 200
    paper = result.json()
    assert paper["title"] == "Edited title"
    assert paper["rel"] == 8, "an LLM proposal must not replace the manual score"
    assert paper["proposed_rel"] == 3
    assert paper["proposed_reason"] == "Updated draft"


def test_ingest_token_batch_accepts_duplicates_and_rejects_malformed_items(app_client):
    client, _ = app_client
    existing = client.post("/api/papers", json=paper_body("already-there", rel=4)).json()["paper"]
    batch = {
        "agent": "research-agent",
        "query": "simulation inference",
        "papers": [
            paper_body(
                "new-from-agent",
                rel=None,
                cred=None,
                qual=None,
                reimpl=None,
                proposed_scores={"rel": 9, "cred": 7, "reason": "Strong match"},
            ),
            {"title": "Duplicate", "authors": ["Someone"], "doi": existing["doi"]},
            {"title": "", "authors": "not a list"},
        ],
    }
    response = client.post(
        "/api/ingest",
        headers={"Authorization": "Bearer test-ingest-token"},
        json=batch,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] == 1
    assert result["duplicate"] == 1
    assert result["rejected"] == 1
    assert [item["status"] for item in result["results"]] == ["accept", "duplicate", "reject"]

    accepted_id = result["results"][0]["id"]
    accepted = client.get("/api/paper", params={"id": accepted_id}).json()
    assert accepted["state"] == "unreviewed"
    assert accepted["rel"] is None
    assert accepted["proposed_rel"] == 9
    duplicate = client.get("/api/paper", params={"id": existing["id"]}).json()
    assert any(p["status"] == "duplicate" for p in duplicate["provenance"])


def test_sql_console_is_read_only_and_times_out_recursive_queries(app_client):
    client, _ = app_client
    selected = client.post("/api/sql", json={"sql": "SELECT 1 AS one"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["rows"] == [{"one": 1}]

    for statement in (
        "INSERT INTO fields(id,label) VALUES('bad','Bad')",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA journal_mode",
    ):
        rejected = client.post("/api/sql", json={"sql": statement})
        assert rejected.status_code == 400, (statement, rejected.text)

    timeout = client.post(
        "/api/sql",
        json={
            "sql": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) AS n FROM c"
        },
    )
    assert timeout.status_code == 400, timeout.text
    assert "interrupted" in timeout.json()["detail"].lower()


def test_auth_gate_login_logout_and_cross_origin_write(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=False, password="correct-horse", ingest_token="secret", pdf_jobs=False, fields=TOPIC_FIELDS)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/papers").status_code == 401
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        logged_in = client.post("/api/login", json={"password": "correct-horse"})
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()["authenticated"] is True
        assert logged_in.json()["user"]["role"] == "admin"
        assert client.get("/api/session").json()["authenticated"] is True

        cross_origin = client.post(
            "/api/papers",
            headers={"Origin": "https://attacker.example"},
            json=paper_body("cross-origin"),
        )
        assert cross_origin.status_code == 403
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/session").json() == {"authenticated": False}
        assert client.get("/api/papers").status_code == 401


def test_pdf_job_caches_fixture_and_exposes_full_text_hits(app_client, monkeypatch):
    client, settings = app_client
    paper = client.post(
        "/api/papers",
        json=paper_body(
            "pdf-paper",
            artifacts=[{"kind": "pdf", "url": "https://files.example/paper.pdf"}],
        ),
    ).json()["paper"]
    with connection(settings, True) as db:
        artifact = dict(db.execute("SELECT * FROM artifacts WHERE paper_id=?", (paper["id"],)).fetchone())
    fixture = pdf_bytes()
    monkeypatch.setattr(sources, "safe_download", lambda url, max_bytes: fixture)
    jobs.fetch_one(settings, artifact)

    with connection(settings, True) as db:
        row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact["id"],)).fetchone()
        assert row["fetch_status"] == "cached"
        assert row["local_path"]
        assert db.execute("SELECT count(*) FROM pdf_text WHERE artifact_id=?", (artifact["id"],)).fetchone()[0] == 1

    detail = client.get(
        "/api/paper",
        params={"id": paper["id"], "q": 'pdf:"posterior collapse"'},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["pdf_hits"]
    assert detail.json()["pdf_hits"][0]["page"] == 1
    assert "<mark>posterior collapse</mark>" in detail.json()["pdf_hits"][0]["snippet"]

    fts = client.post(
        "/api/sql",
        json={
            "sql": "SELECT paper_id, page, text FROM pdf_fts WHERE pdf_fts MATCH '\"posterior collapse\"'"
        },
    )
    assert fts.status_code == 200, fts.text
    assert fts.json()["rows"] == [{"paper_id": paper["id"], "page": 1, "text": "posterior collapse"}]


def test_delete_paper_removes_related_data_and_search_entries(app_client):
    client, settings = app_client
    paper = client.post('/api/papers', json=paper_body('remove-entry', artifacts=[{'kind': 'pdf', 'url': 'https://example.com/paper.pdf'}])).json()['paper']
    pid = paper['id']
    with connection(settings) as db:
        artifact_id = db.execute('SELECT id FROM artifacts WHERE paper_id=?', (pid,)).fetchone()[0]
        db.execute('INSERT INTO pdf_text(paper_id,artifact_id,page,text) VALUES(?,?,?,?)', (pid, artifact_id, 1, 'uniquedeletionterm'))
    assert client.delete('/api/paper', params={'id': pid}).json() == {'deleted': True}
    assert client.get('/api/paper', params={'id': pid}).status_code == 404
    assert client.delete('/api/paper', params={'id': pid}).status_code == 404
    with connection(settings, True) as db:
        for table in ('paper_fts', 'artifacts', 'paper_tags', 'provenance', 'pdf_text', 'paper_projects'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE paper_id=?', (pid,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM pdf_fts WHERE pdf_fts MATCH 'uniquedeletionterm'").fetchone()[0] == 0
    assert client.post('/api/papers', json=paper_body('remove-entry')).status_code == 200
