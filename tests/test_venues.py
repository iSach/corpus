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


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(venues, "S2", venues.Pacer(sleep=lambda seconds: None))


class Clock:
    def __init__(self):
        self.now, self.sleeps = 100.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


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


def test_single_uncurated_source_is_applied_too(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "neural information processing systems", "type": "conference"}, "landing_page_url": "https://proceedings.neurips.cc/paper/2022/hash/abc"}
    assert run(settings, pid, openalex(location)).status == "applied"
    p = paper(c, pid)
    assert (p["venue"], p["venue_class"], p["year"], p["venue_source"]) == ("Advances in Neural Information Processing Systems (NeurIPS)", "conference", 2022, "auto:openalex")
    assert p["venue_proposal"] is None
    assert c.get("/api/papers", params={"q": "is:venue-proposed"}).json()["total"] == 0


def test_disagreeing_sources_apply_the_best_supported_venue(client):
    c, settings = client
    pid = add(c)
    location = {"source": {"display_name": "Physical Review Letters", "type": "journal"}, "landing_page_url": "https://doi.org/10.1103/x"}
    result = run(settings, pid, {**openalex(location), **crossref(), **s2(venue="ICML", dblp="conf/icml/Lovelace21")})
    assert result.status == "applied"
    assert paper(c, pid)["venue_class"] != "preprint"


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


def test_semantic_scholar_calls_are_spaced_across_callers():
    clock = Clock()
    pacer = venues.Pacer(clock, clock.sleep)
    for _ in range(3):
        pacer.call(1.1, lambda: "ok")
        clock.now += 0.2   # the rest of a check takes a moment
    assert clock.sleeps == [0.9, 0.9]


def test_rate_limit_pauses_every_caller_and_retries():
    clock = Clock()
    pacer = venues.Pacer(clock, clock.sleep)
    answers = [sources.HTTPStatusError(429, "7"), "found"]

    def request():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert pacer.call(1.1, request) == "found"
    assert clock.sleeps == [7.0]   # Retry-After honoured, then retried
    assert pacer.call(1.1, lambda: "next") == "next"
    assert clock.sleeps == [7.0, 1.1]


def test_repeated_rate_limits_back_off_then_give_up():
    clock = Clock()
    pacer = venues.Pacer(clock, clock.sleep)
    with pytest.raises(sources.SourceError, match="HTTP 429"):
        pacer.call(1.1, lambda: (_ for _ in ()).throw(sources.SourceError("source returned HTTP 429")))
    assert clock.sleeps == [5.0, 10.0]
    assert pacer.next_slot - clock.now == 20.0   # the next caller waits too


def test_other_errors_are_not_retried():
    clock = Clock()
    pacer = venues.Pacer(clock, clock.sleep)
    calls = []

    def request():
        calls.append(1)
        raise sources.HTTPStatusError(503)

    with pytest.raises(sources.HTTPStatusError):
        pacer.call(1.1, request)
    assert calls == [1] and clock.sleeps == []


def test_interval_defaults_follow_the_api_key():
    assert venues.s2_interval(Settings(s2_api_key="k", s2_interval=0)) == 1.1
    assert venues.s2_interval(Settings(s2_api_key="", s2_interval=0)) == 3.0
    assert venues.s2_interval(Settings(s2_api_key="k", s2_interval=2.5)) == 2.5
