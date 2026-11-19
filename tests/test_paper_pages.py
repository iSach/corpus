"""Papers added from landing pages: publisher citation tags and conference sites."""
import json

import pytest

from corpus import sources
from corpus.catalog import Catalog


CITATION_PAGE = b"""<html><head>
<meta name="citation_title" content="Stable  rollouts for &lt;i&gt;toy&lt;/i&gt; flows">
<meta name="citation_author" content="Example, Ada">
<meta name="citation_author" content="Sample, Ben Q.">
<meta name="citation_journal_title" content="Journal of Examples">
<meta name="citation_publication_date" content="2025/03/14">
<meta name="citation_abstract" content="&lt;p&gt;&lt;strong&gt;Abstract.&lt;/strong&gt; We study toy flows.&lt;/p&gt;">
<meta name="citation_pdf_url" content="/articles/1/2/2025/paper.pdf">
</head><body></body></html>"""


def conference_page(badge='Poster<br> in <br>Workshop: <a href="/virtual/2025/workshop/1">Toy Models in Science</a>', openreview='AbCdE12345'):
    link = f'<a href="https://openreview.net/forum?id={openreview}" title="OpenReview">OpenReview</a>' if openreview else ''
    return f"""<html><body><nav><a href="/FAQ">FAQ</a></nav>
<div class="hero-card"><span class="event-type-badge"><span class='small'>{badge}</span></span>
<h1 class="event-title">A Toy Study of Rollout Error</h1>
<div class="event-organizers">
    Ada Example \u22c5 Ben Sample \u22c5 Cy Placeholder
</div>
<div class="action-buttons">{link}</div>
<div class="abstract-section"><div class="abstract-text-inner">
    We measure <em>toy</em> rollout error.<br>It grows.
</div></div></div></body></html>""".encode()


@pytest.mark.parametrize("value,kind,expected", [
    ("https://gmd.copernicus.org/articles/12/345/2025/", "doi", "10.5194/gmd-12-345-2025"),
    ("https://tc.copernicus.org/articles/7/1/2013", "doi", "10.5194/tc-7-1-2013"),
    ("https://www.neurips.cc/virtual/2025/poster/12345/?tab=1#top", "conference", "https://neurips.cc/virtual/2025/poster/12345"),
    ("https://icml.cc/virtual/2025/678", "conference", "https://icml.cc/virtual/2025/678"),
    ("https://openreview.net/forum?id=AbCdE12345&noteId=x", "openreview", "AbCdE12345"),
    ("https://openreview.net/pdf?id=AbCdE12345", "openreview", "AbCdE12345"),
    ("https://publisher.example/a/b?x=1#frag", "page", "https://publisher.example/a/b?x=1"),
])
def test_links_are_recognized_by_kind(value, kind, expected):
    assert sources._recognize(value) == sources._Identifier(kind, expected)


def test_citation_tags_become_a_paper():
    paper = sources.page_metadata("page", CITATION_PAGE, "https://journal.example/articles/1/2/2025/")
    assert paper == {
        "title": "Stable rollouts for toy flows",
        "authors": ["Ada Example", "Ben Q. Sample"],
        "venue": "Journal of Examples",
        "year": 2025,
        "abstract": "We study toy flows.",
        "canonical_url": "https://journal.example/articles/1/2/2025/",
        "arxiv_id": None,
        "doi": None,
        "artifacts": [{"kind": "pdf", "url": "https://journal.example/articles/1/2/2025/paper.pdf"}],
    }


def test_conference_workshop_paper_uses_openreview_as_home():
    url = "https://neurips.cc/virtual/2025/poster/12345"
    paper = sources.page_metadata("conference", conference_page(), url)
    assert paper["title"] == "A Toy Study of Rollout Error"
    assert paper["authors"] == ["Ada Example", "Ben Sample", "Cy Placeholder"]
    assert paper["abstract"] == "We measure toy rollout error. It grows."
    assert (paper["venue"], paper["venue_class"], paper["year"]) == ("NeurIPS 2025 Workshop: Toy Models in Science", "other", 2025)
    assert paper["openreview_id"] == "AbCdE12345"
    assert paper["canonical_url"] == "https://openreview.net/forum?id=AbCdE12345"
    assert paper["artifacts"] == [{"kind": "other", "url": url}]


def crossref(doi):
    return json.dumps({"message": {"DOI": doi, "title": ["Published toy flows"], "author": [{"given": "Ada", "family": "Example"}],
                                   "container-title": ["Journal of Examples"], "published": {"date-parts": [[2025]]}}}).encode()


def test_catalog_prefers_crossref_for_pages_with_a_doi(monkeypatch):
    calls = []
    page = CITATION_PAGE.replace(b"</head>", b'<meta name="citation_doi" content="10.5555/toy.1"></head>')
    def fetch(url, **kwargs):
        calls.append((url, kwargs["headers"]["Accept"]))
        return crossref("10.5555/toy.1") if "crossref" in url else page
    monkeypatch.setattr(sources, "_fetch_url", fetch)
    paper = Catalog().resolve(["https://journal.example/articles/1/2/2025/"])[0]["paper"]
    assert calls[0] == ("https://journal.example/articles/1/2/2025/", "text/html,application/xhtml+xml;q=0.9")
    assert paper["title"] == "Published toy flows" and paper["doi"] == "10.5555/toy.1"
    # The landing page's abstract and PDF fill what Crossref lacks.
    assert paper["abstract"] == "We study toy flows."
    assert paper["artifacts"] == [{"kind": "pdf", "url": "https://journal.example/articles/1/2/2025/paper.pdf"}]


def test_conference_paper_is_saved_once_under_its_openreview_id(app_client, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr(sources, "_fetch_url", lambda url, **kwargs: conference_page())
    resolved = client.post("/api/catalog/resolve", json={"identifiers": ["https://neurips.cc/virtual/2025/poster/12345"]}).json()["results"][0]["paper"]
    saved = client.post("/api/capture", json={"projects": ["sbi-pretrain"], "papers": [resolved]})
    assert saved.status_code == 200, saved.text
    paper = saved.json()["results"][0]["paper"]
    assert paper["openreview_id"] == "AbCdE12345" and paper["venue_class"] == "other"

    def unexpected(*args, **kwargs): raise AssertionError("A saved OpenReview id must resolve locally")
    monkeypatch.setattr(sources, "_fetch_url", unexpected)
    local = client.post("/api/catalog/resolve", json={"identifiers": ["https://openreview.net/forum?id=AbCdE12345"]}).json()["results"][0]
    assert local["paper"]["existing"]["id"] == paper["id"]

    # The same paper from another link is an exact duplicate, even retitled.
    again = client.post("/api/capture", json={"projects": ["diff-compose"], "papers": [{**resolved, "title": "Something else entirely"}]}).json()["results"][0]
    assert again["status"] in ("filed", "exists")
    assert set(client.get("/api/paper", params={"id": paper["id"]}).json()["projects"]) == {"sbi-pretrain", "diff-compose"}
