"""Finding the published version of arXiv preprints, without the network."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from corpus import sources, venues
from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection

FIELDS = [{"id": "unfiled", "label": "Unfiled"}]
TITLE = "Amortized inference with implicit flows"


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, dev=True, password="pw", ingest_token="token", pdf_jobs=False, fields=FIELDS)
    with TestClient(create_app(settings)) as c:
        yield c, settings


def add(client, arxiv_id="2101.00001", title=TITLE, **extra):
    body = {"title": title, "authors": ["Ada Lovelace"], "venue": "arXiv", "year": 2020, "arxiv_id": arxiv_id, **extra}
    response = client.post("/api/papers", json=body)
    assert response.status_code == 200, response.text
    return response.json()["paper"]["id"]


def fake(responses):
    """URL substring -> JSON payload or exception; anything else is a 404."""
    calls = []

    def fetch(url, headers):
        calls.append(url)
        for pattern, payload in responses.items():
            if pattern in url:
                if isinstance(payload, Exception):
                    raise payload
                return json.dumps(payload).encode()
        raise sources.SourceError("source returned HTTP 404")

    fetch.calls = calls
    return fetch


def s2(title=TITLE, venue="", dblp=None, doi=None, year=2020):
    ids = {"ArXiv": "2101.00001", **({"DBLP": dblp} if dblp else {}), **({"DOI": doi} if doi else {})}
    return {"semanticscholar": {"title": title, "venue": venue, "year": year, "publicationVenue": None, "externalIds": ids}}


def openalex(*locations, title=TITLE, year=2020):
    return {"openalex": {"title": title, "publication_year": year, "locations": [
        {"source": {"display_name": "arXiv (Cornell University)", "type": "repository"}, "landing_page_url": "https://arxiv.org/abs/2101.00001"},
        *locations]}}


def crossref(title=TITLE, container="Physical Review Letters", kind="journal-article", year=2021):
    return {"crossref.org": {"message": {"title": [title], "container-title": [container], "type": kind, "published-print": {"date-parts": [[year, 3]]}}}}


def run(settings, paper_id, responses):
    with connection(settings) as db:
        return venues.check(db, paper_id, settings, fake(responses))


def paper(client, paper_id):
    return client.get("/api/paper", params={"id": paper_id}).json()


def test_dblp_entry_applies_venue_and_proceedings_year(client):
    c, settings = client
    pid = add(c)
    result = run(settings, pid, s2(venue="International Conference on Learning Representations", dblp="conf/iclr/LovelaceT21"))
    assert result.status == "applied"
    p = paper(c, pid)
    assert p["venue"] == "International Conference on Learning Representations (ICLR)"
    assert (p["year"], p["venue_class"], p["venue_source"]) == (2021, "conference", "auto:dblp")
    assert p["arxiv_id"] == "2101.00001" and p["venue_proposal"] is None


def test_journal_doi_is_confirmed_through_crossref(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "Physical Review Letters", "type": "journal"}, "landing_page_url": "https://doi.org/10.1103/PhysRevLett.127.000001"}
    result = run(settings, pid, {**openalex(location), **crossref()})
    assert result.status == "applied"
    p = paper(c, pid)
    assert (p["venue"], p["year"], p["venue_class"], p["doi"]) == ("Physical Review Letters", 2021, "journal", "10.1103/physrevlett.127.000001")


def test_doi_with_a_different_title_needs_the_owner(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "Nature", "type": "journal"}, "landing_page_url": "https://doi.org/10.1038/s41586-024-00001-1"}
    result = run(settings, pid, {**openalex(location), **crossref(title="A renamed journal version", container="Nature", year=2024)})
    assert result.status == "proposed"
    p = paper(c, pid)
    assert p["venue_class"] == "preprint"
    assert (p["venue_proposal"]["venue"], p["venue_proposal"]["year"]) == ("Nature", 2024)


def test_single_uncurated_source_becomes_a_proposal_the_owner_applies(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "neural information processing systems", "type": "conference"}, "landing_page_url": "https://proceedings.neurips.cc/paper/2022/hash/abc"}
    assert run(settings, pid, openalex(location)).status == "proposed"
    p = paper(c, pid)
    assert p["venue_class"] == "preprint"
    proposal = p["venue_proposal"]
    assert (proposal["venue"], proposal["year"], proposal["sources"]) == ("Advances in Neural Information Processing Systems (NeurIPS)", 2022, ["OpenAlex"])
    listed = c.get("/api/papers", params={"q": "is:venue-proposed"}).json()
    assert [x["id"] for x in listed["papers"]] == [pid]

    applied = c.post(f"/api/venue-proposals/{proposal['id']}", json={"action": "apply"}).json()["paper"]
    assert (applied["venue_class"], applied["year"], applied["venue_source"]) == ("conference", 2022, "manual")
    assert applied["venue_proposal"] is None
    assert c.get("/api/papers", params={"q": "is:venue-proposed"}).json()["total"] == 0
    assert c.post(f"/api/venue-proposals/{proposal['id']}", json={"action": "apply"}).status_code == 409


def test_two_sources_agreeing_apply_without_a_curated_record(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "neural information processing systems", "type": "conference"}, "landing_page_url": "https://proceedings.neurips.cc/paper/2021/hash/abc"}
    result = run(settings, pid, {**openalex(location), **s2(venue="Neural Information Processing Systems", year=2021)})
    assert result.status == "applied"
    assert paper(c, pid)["venue_source"] == "auto:openalex+semanticscholar"


def test_disagreeing_curated_sources_need_the_owner(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "Physical Review Letters", "type": "journal"}, "landing_page_url": "https://doi.org/10.1103/x"}
    result = run(settings, pid, {**openalex(location), **crossref(), **s2(venue="ICML", dblp="conf/icml/Lovelace21")})
    assert result.status == "proposed"


def test_dismissed_answer_is_not_proposed_again(client):
    c, settings = client
    pid = add(c)
    workshop = s2(venue="NeurIPS 2021 Workshop on Deep Generative Models", year=2021)
    assert run(settings, pid, workshop).status == "proposed"
    proposal = paper(c, pid)["venue_proposal"]
    assert proposal["venue_class"] == "other"
    assert c.post(f"/api/venue-proposals/{proposal['id']}", json={"action": "reject"}).status_code == 200
    assert run(settings, pid, workshop).status == "rejected-before"
    assert paper(c, pid)["venue_proposal"] is None


def test_owner_edits_are_never_overridden(client):
    c, settings = client
    pid = add(c)
    edited = c.post("/api/papers", json={"id": pid, "venue": "International Conference on Learning Representations (ICLR)", "year": 2021}).json()["paper"]
    assert (edited["venue_class"], edited["venue_source"]) == ("conference", "manual")
    c.post("/api/papers", json={"id": pid, "venue": "arXiv", "venue_class": "preprint", "year": 2020})
    result = run(settings, pid, s2(venue="ICML", dblp="conf/icml/Lovelace22"))
    assert result.status == "none"
    assert paper(c, pid)["venue"] == "arXiv"
    with connection(settings) as db:
        assert db.execute(venues.DUE).fetchone() is None


def test_notes_only_edit_keeps_automatic_checks(client):
    c, settings = client
    pid = add(c)
    c.post("/api/papers", json={"id": pid, "notes": "read later"})
    assert paper(c, pid)["venue_source"] == ""


def test_source_failure_is_recorded_and_changes_nothing(client):
    c, settings = client
    pid = add(c)
    result = run(settings, pid, {"openalex": sources.SourceError("source returned HTTP 503"), "semanticscholar": sources.SourceError("source returned HTTP 429")})
    assert result.status == "error"
    p = paper(c, pid)
    assert p["venue_class"] == "preprint" and "HTTP 429" in p["venue_check_error"] and p["venue_checked"]
    with connection(settings) as db:  # retried the next day, not next week
        assert db.execute(venues.DUE).fetchone() is None
        db.execute("UPDATE papers SET venue_checked=strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 days') WHERE id=?", (pid,))
        assert db.execute(venues.DUE).fetchone()["id"] == pid


def test_one_source_failing_with_nothing_found_is_retried(client):
    c, settings = client
    pid = add(c)
    assert run(settings, pid, {"semanticscholar": sources.SourceError("source returned HTTP 429")}).status == "error"


def test_unconfirmed_year_is_left_out_of_the_proposal(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "International Conference on Machine Learning", "type": "conference"}, "landing_page_url": "https://proceedings.mlr.press/v119/lovelace20a.html"}
    assert run(settings, pid, openalex(location)).status == "proposed"
    proposal = paper(c, pid)["venue_proposal"]
    assert proposal["year"] is None
    applied = c.post(f"/api/venue-proposals/{proposal['id']}", json={"action": "apply"}).json()["paper"]
    assert (applied["venue_class"], applied["year"]) == ("conference", 2020)


def test_schedule_rechecks_after_the_interval(client):
    c, settings = client
    pid = add(c)
    with connection(settings) as db:
        assert db.execute(venues.DUE).fetchone()["id"] == pid
    assert venues.check_next(settings, fake({})).status == "none"
    with connection(settings) as db:
        assert db.execute(venues.DUE).fetchone() is None
        db.execute("UPDATE papers SET venue_checked=strftime('%Y-%m-%dT%H:%M:%fZ','now','-40 days') WHERE id=?", (pid,))
        assert db.execute(venues.DUE).fetchone()["id"] == pid


def test_published_doi_already_held_by_another_paper_is_not_duplicated(client):
    c, settings = client
    other = add(c, arxiv_id=None, title="The journal version imported separately", doi="10.1103/physrevlett.127.000001")
    pid = add(c)
    location = {"source": {"display_name": "Physical Review Letters", "type": "journal"}, "landing_page_url": "https://doi.org/10.1103/PhysRevLett.127.000001"}
    assert run(settings, pid, {**openalex(location), **crossref()}).status == "applied"
    p = paper(c, pid)
    assert p["venue"] == "Physical Review Letters" and p["doi"] is None
    assert paper(c, other)["doi"] == "10.1103/physrevlett.127.000001"


def test_bibtex_cites_published_version_and_keeps_eprint(client):
    c, settings = client
    published = add(c)
    preprint = add(c, arxiv_id="2202.00002", title="A second example preprint")
    run(settings, published, s2(venue="ICLR", dblp="conf/iclr/LovelaceT21"))
    text = c.post("/api/export", json={"format": "bibtex", "scope": "all", "include": {"scores": False, "notes": False}}).json()["text"]
    entries = {e.split("{", 1)[1].split(",", 1)[0]: e for e in text.split("\n\n")}
    ddim = next(e for e in entries.values() if "implicit flows" in e)
    cfg = next(e for e in entries.values() if "second example" in e)
    assert ddim.startswith("@inproceedings{") and "booktitle  = {International Conference on Learning Representations (ICLR)}" in ddim
    assert "year       = {2021}" in ddim and "eprint     = {2101.00001}" in ddim and "archiveprefix = {arXiv}" in ddim
    assert cfg.startswith("@article{") and "journal    = {arXiv preprint arXiv:2202.00002}" in cfg


def test_decide_prefers_curated_year_over_preprint_year():
    weak = venues.make_candidate("semanticscholar", "ICLR", year=2020)
    strong = venues.make_candidate("dblp", "", dblp="conf/iclr/LovelaceT21", year=2021, year_sure=True, strong=True)
    chosen, auto, used = venues.decide([weak, strong], preprint_year=2020)
    assert (chosen.key, chosen.year, auto, used) == ("iclr", 2021, True, ["dblp", "semanticscholar"])
    assert venues.make_candidate("openalex", "arXiv (Cornell University)") is None
    assert venues.make_candidate("dblp", "", dblp="journals/corr/abs-2010-02502") is None


def test_retitled_publication_found_through_openalex_is_proposed(client):
    c, settings = client
    pid = add(c, arxiv_id="2312.00003", title="An example preprint title")
    published = "A renamed journal version"
    location = {"source": {"display_name": "Nature", "type": "journal"}, "landing_page_url": "https://doi.org/10.1038/s41586-024-00001-1"}
    result = run(settings, pid, {**openalex(location, title=published), **crossref(title=published, container="Nature", year=2024)})
    assert result.status == "proposed"
    proposal = paper(c, pid)["venue_proposal"]
    assert (proposal["venue"], proposal["year"], proposal["doi"]) == ("Nature", 2024, "10.1038/s41586-024-00001-1")
