import json

import pytest

from corpus import sources


def test_crossref_metadata_is_normalized_without_network(monkeypatch):
    calls = []
    payload = {
        "message": {
            "DOI": "10.5555/ABC",
            "title": ["  A <i>useful</i> result  "],
            "author": [
                {"given": "Ada", "family": "Lovelace"},
                {"name": "Alan Turing"},
            ],
            "container-title": ["Journal of Tests"],
            "published-print": {"date-parts": [[2024, 5, 1]]},
            "abstract": "<jats:p>A &amp; B.</jats:p>",
            "URL": "https://doi.org/10.5555/ABC",
            "link": [{"URL": "https://publisher.example/paper.pdf", "content-type": "application/pdf"}],
        }
    }

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(sources, "_fetch_url", fake_fetch)
    result = sources.fetch_metadata("doi:10.5555/ABC")

    assert calls[0][0] == "https://api.crossref.org/works/10.5555%2Fabc"
    assert result == {
        "title": "A useful result",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "venue": "Journal of Tests",
        "year": 2024,
        "abstract": "A & B.",
        "canonical_url": "https://doi.org/10.5555/ABC",
        "arxiv_id": None,
        "doi": "10.5555/abc",
        "artifacts": [{"kind": "pdf", "url": "https://publisher.example/paper.pdf"}],
    }


def test_arxiv_atom_metadata_is_normalized_without_network(monkeypatch):
    atom = b'''<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.01234v2</id>
        <title> A paper about inference </title>
        <summary> An abstract with\nextra whitespace. </summary>
        <published>2024-01-12T00:00:00Z</published>
        <author><name>First Author</name></author>
        <author><name>Second Author</name></author>
        <arxiv:journal_ref>Test Proceedings</arxiv:journal_ref>
        <arxiv:doi>10.5555/arxiv-paper</arxiv:doi>
        <link rel="alternate" href="http://arxiv.org/abs/2401.01234v2" />
      </entry>
    </feed>'''
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append(url)
        return atom

    monkeypatch.setattr(sources, "_fetch_url", fake_fetch)
    result = sources.fetch_metadata("https://arxiv.org/pdf/2401.01234v2.pdf")

    assert calls[0] == "https://export.arxiv.org/api/query?id_list=2401.01234"
    assert result["title"] == "A paper about inference"
    assert result["authors"] == ["First Author", "Second Author"]
    assert result["venue"] == "Test Proceedings"
    assert result["year"] == 2024
    assert result["arxiv_id"] == "2401.01234"
    assert result["doi"] == "10.5555/arxiv-paper"
    assert result["artifacts"] == [{"kind": "pdf", "url": "https://arxiv.org/pdf/2401.01234.pdf"}]


def test_generic_metadata_urls_are_rejected():
    with pytest.raises(sources.SourceError, match="generic URLs"):
        sources.fetch_metadata("https://publisher.example/articles/10.5555/not-recognized")

    with pytest.raises(sources.SourceError, match="http or https"):
        sources.fetch_metadata("ftp://doi.org/10.5555/abc")


def test_recognized_metadata_failures_are_distinct_from_invalid_identifiers(monkeypatch):
    def failed_fetch(url, **kwargs):
        raise sources.SourceError("Crossref request failed")

    monkeypatch.setattr(sources, "_fetch_url", failed_fetch)
    with pytest.raises(sources.MetadataFetchError, match="Crossref request failed"):
        sources.fetch_metadata("10.5555/recognized")

    # Recognition happens before the fetch wrapper, so invalid input remains the
    # ordinary validation error that the API can report as HTTP 400.
    with pytest.raises(sources.SourceError) as invalid:
        sources.fetch_metadata("https://publisher.example/no-identifier")
    assert type(invalid.value) is sources.SourceError


def test_redirect_chain_uses_one_total_timeout(monkeypatch):
    clock = [0.0]
    calls = []

    def monotonic():
        return clock[0]

    def fake_request(url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        clock[0] += 11.0
        return 302, {"location": "/next"}, b""

    monkeypatch.setattr(sources.time, "monotonic", monotonic)
    monkeypatch.setattr(sources, "_request_once", fake_request)
    with pytest.raises(sources.SourceError, match="time limit"):
        sources.safe_download("https://files.example/start")
    assert len(calls) == 2
    assert calls[0][1] == pytest.approx(20.0)
    assert calls[1][1] == pytest.approx(9.0)


def test_safe_download_follows_only_safe_redirects_and_has_a_byte_limit(monkeypatch):
    responses = {
        "https://files.example/start": (302, {"location": "/paper.pdf"}, b""),
        "https://files.example/paper.pdf": (200, {"content-length": "4"}, b"data"),
    }
    calls = []

    def fake_request(url, **kwargs):
        calls.append(url)
        return responses[url]

    monkeypatch.setattr(sources, "_request_once", fake_request)
    assert sources.safe_download("https://files.example/start", max_bytes=4) == b"data"
    assert calls == ["https://files.example/start", "https://files.example/paper.pdf"]

    responses["https://files.example/paper.pdf"] = (200, {"content-length": "5"}, b"12345")
    with pytest.raises(sources.SourceError, match="exceeds"):
        sources.safe_download("https://files.example/start", max_bytes=4)


def test_redirect_to_private_host_is_rejected_before_request(monkeypatch):
    def fake_request(url, **kwargs):
        return 302, {"location": "http://127.0.0.1/internal"}, b""

    monkeypatch.setattr(sources, "_request_once", fake_request)
    with pytest.raises(sources.SourceError):
        sources.safe_download("https://files.example/start")


def test_resolver_rejects_private_dns_result(monkeypatch):
    def fake_getaddrinfo(*args, **kwargs):
        return [(sources.socket.AF_INET, sources.socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]

    monkeypatch.setattr(sources.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(sources.SourceError, match="non-public"):
        sources._resolve("public-looking.example", 443)
