"""Integration and security coverage for the Corpus HTTP API."""

from __future__ import annotations

import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection
from corpus import jobs, sources


def _settings(tmp_path, *, dev=True, password="test-password", ingest_token="test-ingest-token"):
    return Settings(
        data_dir=tmp_path,
        dev=dev,
        password=password,
        ingest_token=ingest_token,
        pdf_jobs=False,
    )


@pytest.fixture
def app_client(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        yield client, settings


def _paper(slug, *, field="sbi-pretrain", rel=7, doi=None, arxiv_id=None, **extra):
    doi = doi or f"10.5555/{slug}"
    body = {
        "title": f"Paper {slug} on posterior inference",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "venue": "ICML",
        "year": 2024,
        "abstract": f"Abstract for {slug}.",
        "canonical_url": f"https://doi.org/{doi}",
        "doi": doi,
        "arxiv_id": arxiv_id,
        "field": field,
        "rel": rel,
        "cred": 6,
        "qual": 8,
        "reimpl": 5,
        "notes": f"Notes for {slug}",
        "tags": ["score-matching"],
        "artifacts": [],
    }
    body.update(extra)
    return body


def _pdf_fixture():
    """A one-page, valid PDF whose text extractor yields the query phrase."""

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 59 >>\nstream\nBT\n/F1 12 Tf\n72 720 Td\n(posterior collapse) Tj\nET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    data = bytes(output)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    assert "posterior collapse" in reader.pages[0].extract_text()
    return data


def test_create_edit_persists_normalized_values_and_manual_scores(app_client):
    client, _ = app_client
    payload = _paper(
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
    existing = client.post("/api/papers", json=_paper("already-there", rel=4)).json()["paper"]
    batch = {
        "agent": "research-agent",
        "query": "simulation inference",
        "papers": [
            _paper(
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


def test_facets_narrow_the_current_query(app_client):
    client, _ = app_client
    high = client.post(
        "/api/papers",
        json=_paper("sbi-high", rel=8, title="Neural posterior calibration for simulators"),
    ).json()["paper"]
    low = client.post(
        "/api/papers",
        json=_paper("sbi-low", rel=3, title="Likelihood free benchmarks with amortized inference"),
    ).json()["paper"]
    other = client.post(
        "/api/papers",
        json=_paper(
            "diff-high",
            field="diff-compose",
            rel=9,
            title="Composing local diffusion priors for materials science",
        ),
    ).json()["paper"]
    response = client.get(
        "/api/papers",
        params={
            "q": "tag:score-matching",
            "facets": json.dumps({"field": ["sbi-pretrain"], "rel": 7}),
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert {paper["id"] for paper in result["papers"]} == {high["id"]}
    assert low["id"] not in {paper["id"] for paper in result["papers"]}
    assert other["id"] not in {paper["id"] for paper in result["papers"]}
    assert result["facets"]["field"] == [{"value": "sbi-pretrain", "count": 1}]


def test_exports_keep_stable_bibtex_keys_and_respect_include_toggles(app_client):
    client, _ = app_client
    payload = _paper(
        "export-me",
        rel=7,
        artifacts=[{"kind": "code", "url": "https://code.example/repo"}],
    )
    paper = client.post("/api/papers", json=payload).json()["paper"]
    full_options = {
        "scope": "selection",
        "ids": [paper["id"]],
        "format": "bibtex",
        "include": {"scores": True, "notes": True, "code": True, "local_paths": True},
    }
    first = client.post("/api/export", json=full_options).json()
    second = client.post("/api/export", json=full_options).json()
    assert first["text"] == second["text"]
    assert re.search(r"@\w+\{([^,]+),", first["text"]).group(1)
    assert "rel=7" in first["text"]
    assert "Notes for export-me" in first["text"]
    assert "code: https://code.example/repo" in first["text"]

    compact = client.post(
        "/api/export",
        json={
            "scope": "selection",
            "ids": [paper["id"]],
            "format": "json",
            "include": {"scores": False, "notes": False, "code": False, "local_paths": False},
        },
    )
    assert compact.status_code == 200, compact.text
    exported = json.loads(compact.json()["text"])[0]
    assert "rel" not in exported
    assert "notes" not in exported
    assert exported["artifacts"] == []


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
    settings = _settings(tmp_path, dev=False, password="correct-horse", ingest_token="secret")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/papers").status_code == 401
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        logged_in = client.post("/api/login", json={"password": "correct-horse"})
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json() == {"authenticated": True}
        assert client.get("/api/session").json() == {"authenticated": True}

        cross_origin = client.post(
            "/api/papers",
            headers={"Origin": "https://attacker.example"},
            json=_paper("cross-origin"),
        )
        assert cross_origin.status_code == 403
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/session").json() == {"authenticated": False}
        assert client.get("/api/papers").status_code == 401


def test_pdf_job_caches_fixture_and_exposes_full_text_hits(app_client, monkeypatch):
    client, settings = app_client
    paper = client.post(
        "/api/papers",
        json=_paper(
            "pdf-paper",
            artifacts=[{"kind": "pdf", "url": "https://files.example/paper.pdf"}],
        ),
    ).json()["paper"]
    with connection(settings, True) as db:
        artifact = dict(db.execute("SELECT * FROM artifacts WHERE paper_id=?", (paper["id"],)).fetchone())
    fixture = _pdf_fixture()
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


def test_stats_endpoint_returns_aggregates_and_ingest_volume(app_client):
    client, _ = app_client
    manual = client.post(
        "/api/papers",
        json=_paper(
            "stats-manual",
            title="A distinct manual statistics paper",
            rel=9,
            field="sbi-pretrain",
            state="reviewed",
        ),
    )
    assert manual.status_code == 200, manual.text
    ingested = client.post(
        "/api/ingest",
        headers={"Authorization": "Bearer test-ingest-token"},
        json={
            "agent": "stats-agent",
            "query": "stats test",
            "papers": [
                _paper(
                    "stats-ingested",
                    title="A separate ingested diffusion paper",
                    field="diff-compose",
                    rel=None,
                    cred=None,
                    qual=None,
                    reimpl=None,
                )
            ],
        },
    )
    assert ingested.status_code == 200, ingested.text

    response = client.get("/api/stats")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert data["states"]["unreviewed"] == 1
    assert data["states"]["reviewed"] == 1
    assert data["accepted"] == 2
    assert data["proposed"] == 2
    assert data["keep_rate"] == 100.0
    by_field = {row["field"]: row["count"] for row in data["fields"]}
    assert by_field["sbi-pretrain"] == 1
    assert by_field["diff-compose"] == 1
    assert len(data["distributions"]["rel"]["bins"]) == 11
    assert data["distributions"]["rel"]["bins"][9] == 1
    assert data["distributions"]["rel"]["mean"] == 9.0
    assert len(data["runs"]) == 2
    assert data["pdf_pending"] == 0


def test_smart_lists_save_query_and_sql_then_delete(app_client):
    client, _ = app_client
    saved_query = client.post(
        "/api/smart-lists",
        json={
            "name": "High SBI",
            "query": "field:sbi-pretrain AND rel:>=7",
            "facets": {"has": ["code"], "year": ["2025"], "cred": 5},
            "kind": "query",
        },
    )
    assert saved_query.status_code == 200, saved_query.text
    saved_sql = client.post(
        "/api/smart-lists",
        json={"name": "All paper ids", "query": "SELECT id, title FROM papers", "kind": "sql"},
    )
    assert saved_sql.status_code == 200, saved_sql.text

    listing = client.get("/api/smart-lists")
    assert listing.status_code == 200, listing.text
    lists = {row["name"]: row for row in listing.json()["lists"]}
    assert lists["High SBI"]["query"] == "field:sbi-pretrain AND rel:>=7"
    assert lists["High SBI"]["kind"] == "query"
    assert lists["High SBI"]["facets"] == {"has": ["code"], "year": ["2025"], "cred": 5}
    assert lists["All paper ids"]["kind"] == "sql"
    assert lists["All paper ids"]["facets"] == {}

    deleted = client.delete(f"/api/smart-lists/{lists['High SBI']['id']}")
    assert deleted.status_code == 200, deleted.text
    deleted_sql = client.delete(f"/api/smart-lists/{lists['All paper ids']['id']}")
    assert deleted_sql.status_code == 200, deleted_sql.text
    assert client.get("/api/smart-lists").json()["lists"] == []


def test_pdf_cache_and_retry_endpoints_queue_only_pdf_artifacts(app_client):
    client, settings = app_client
    paper = client.post(
        "/api/papers",
        json=_paper(
            "queue-pdf",
            artifacts=[{"kind": "pdf", "url": "https://files.example/queue.pdf"}],
        ),
    ).json()["paper"]
    artifact = paper["artifacts"][0]
    artifact_id = artifact["id"]

    # Linked is the state used for an externally supplied artifact that has not
    # been cached yet; both cache and retry should move it back to pending.
    with connection(settings) as db:
        db.execute("UPDATE artifacts SET fetch_status='linked', error='old error' WHERE id=?", (artifact_id,))
    queued = client.post(f"/api/artifacts/{artifact_id}/cache")
    assert queued.status_code == 200, queued.text
    assert queued.json() == {"queued": True}
    with connection(settings, True) as db:
        row = db.execute("SELECT kind,fetch_status,error FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        assert dict(row) == {"kind": "pdf", "fetch_status": "pending", "error": None}

    with connection(settings) as db:
        db.execute("UPDATE artifacts SET fetch_status='failed', error='network failed' WHERE id=?", (artifact_id,))
    retried = client.post(f"/api/artifacts/{artifact_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json() == {"queued": True}
    with connection(settings, True) as db:
        assert db.execute("SELECT fetch_status FROM artifacts WHERE id=?", (artifact_id,)).fetchone()[0] == "pending"

    # A pending item is already queued and should not be duplicated.
    assert client.post(f"/api/artifacts/{artifact_id}/retry").json() == {"queued": False}

    code = client.post(
        "/api/papers",
        json=_paper(
            "queue-code",
            title="A separate source repository for evaluation",
            artifacts=[{"kind": "code", "url": "https://code.example/repo"}],
        ),
    ).json()["paper"]["artifacts"][0]
    assert client.post(f"/api/artifacts/{code['id']}/cache").json() == {"queued": False}
