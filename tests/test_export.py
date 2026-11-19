"""BibTeX options, the Export page's memory, and Inbox topic counts."""

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings

TOPICS = [{"id": "alpha", "label": "Alpha"}, {"id": "beta", "label": "Beta"}, {"id": "unfiled", "label": "Unfiled"}]


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=True, password="test-password", ingest_token="token", pdf_jobs=False, fields=TOPICS)
    with TestClient(create_app(settings)) as c:
        yield c


def add(client, **paper):
    body = {"title": "A paper", "authors": ["Ada Lovelace"], "year": 2024, "field": "alpha", "artifacts": [], "tags": [], **paper}
    response = client.post("/api/papers", json=body)
    assert response.status_code == 200, response.text
    return response.json()["paper"]["id"]


def bib(client, ids, **options):
    response = client.post("/api/export", json={"scope": "selection", "ids": ids, "format": "bibtex", **options})
    assert response.status_code == 200, response.text
    return response.json()["text"]


def test_minimal_preset_and_link_choices(client):
    pid = add(client, title="Conference paper", venue="Advances in Neural Information Processing Systems", venue_class="conference",
              doi="10.5555/neurips", arxiv_id="2301.00001", canonical_url="https://example.org/paper", notes="private")
    minimal = bib(client, [pid], include={k: False for k in ("doi", "eprint", "keywords", "scores", "notes", "code", "local_paths")}, bibtex={"url": "none"})
    fields = {line.split("=")[0].strip() for line in minimal.splitlines()[1:-1]}
    assert fields == {"title", "author", "year", "booktitle"}
    assert "https://arxiv.org/abs/2301.00001" in bib(client, [pid], bibtex={"url": "arxiv"})
    assert "url        = {https://doi.org/10.5555/neurips}" in bib(client, [pid], bibtex={"url": "published"})
    assert "booktitle  = {NeurIPS}" in bib(client, [pid], bibtex={"venue": "short"})


def test_published_link_skips_arxiv_and_falls_back_with_best(client):
    pid = add(client, title="Preprint only", venue="arXiv", arxiv_id="2302.00002", canonical_url="https://arxiv.org/abs/2302.00002")
    assert "url" not in bib(client, [pid], bibtex={"url": "published"})
    assert "url        = {https://arxiv.org/abs/2302.00002}" in bib(client, [pid], bibtex={"url": "best"})
    text = bib(client, [pid], bibtex={"preprints": "misc"})
    assert text.startswith("@misc{") and "howpublished = {arXiv preprint arXiv:2302.00002}" in text


def test_export_page_remembers_settings_and_exclusions(client):
    keep = add(client, title="Keep", field="alpha")
    skip = add(client, title="Skip for now", field="beta")
    listing = client.get("/api/export/papers").json()
    assert listing["settings"] is None and listing["excluded"] == []
    assert {p["id"]: p["topics"] for p in listing["papers"]} == {keep: ["alpha"], skip: ["beta"]}
    assert client.put("/api/export/settings", json={"settings": {"topics": ["alpha"], "format": "bibtex"}}).status_code == 200
    assert client.post("/api/export/exclusions", json={"ids": [skip, "not-a-paper"], "excluded": True}).json()["excluded"] == [skip]
    listing = client.get("/api/export/papers").json()
    assert listing["settings"]["topics"] == ["alpha"] and listing["excluded"] == [skip]
    # Excluded papers stay in the library.
    assert client.get("/api/papers", params={"q": "Skip"}).json()["total"] == 1
    assert client.post("/api/export/exclusions", json={"ids": [skip], "excluded": False}).json()["excluded"] == []
    assert client.post("/api/export/exclusions", json={"ids": [], "excluded": True}).status_code == 400
    assert client.put("/api/export/settings", json={"settings": "x" * 10}).status_code == 400


def test_inbox_lists_topic_counts(client):
    papers = [{"title": f"Pending {i}", "authors": ["Ada Lovelace"], "year": 2024, "field": field, "doi": f"10.5555/p{i}", "artifacts": [], "tags": []}
              for i, field in enumerate(["alpha", "alpha", "beta"])]
    assert client.post("/api/ingest", json={"papers": papers}, headers={"Authorization": "Bearer token"}).status_code == 200
    data = client.get("/api/inbox", params={"field": "beta"}).json()
    assert data["total"] == 1 and data["topics"] == {"alpha": 2, "beta": 1}
