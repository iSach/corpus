import json
from pathlib import Path

import pytest

from corpus import arxiv_watch


ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2609.01234v2</id>
    <published>2026-09-07T12:00:00Z</published>
    <title> A   useful\n paper </title>
    <summary> A source-grounded\n abstract. </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:doi>10.1000/example</arxiv:doi>
  </entry>
  <entry><id>https://example.test/not-a-paper</id><title>Ignored</title></entry>
</feed>'''


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=None):
        return self.body


def test_fetch_arxiv_normalizes_atom_entries_without_network():
    requested = []

    def opener(request, timeout):
        requested.append((request.full_url, timeout, request.headers["User-agent"]))
        return Response(ATOM)

    papers = arxiv_watch.fetch_arxiv('all:"useful paper"', 10, opener=opener)

    assert "search_query=all%3A%22useful+paper%22" in requested[0][0]
    assert requested[0][1] == 30
    assert requested[0][2] == "Corpus paper watch/1.0"
    assert papers == [{
        "title": "A useful paper",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "venue": "arXiv",
        "year": 2026,
        "abstract": "A source-grounded abstract.",
        "canonical_url": "https://arxiv.org/abs/2609.01234",
        "arxiv_id": "2609.01234",
        "doi": "10.1000/example",
        "artifacts": [{"kind": "pdf", "url": "https://arxiv.org/pdf/2609.01234.pdf"}],
    }]


def test_run_remembers_seen_papers_and_merges_topic_tags(tmp_path):
    state_path = tmp_path / "state.json"
    topics = [
        {"name": "Inference", "query": 'all:"inference"', "field": "unfiled", "tags": ["reading"], "max_results": 5},
        {"name": "Methods", "query": 'all:"methods"', "field": "unfiled", "max_results": 5},
    ]
    responses = {
        'all:"inference"': [{"title": "One paper", "authors": ["Ada"], "arxiv_id": "2609.00001"}],
        'all:"methods"': [{"title": "One paper", "authors": ["Ada"], "arxiv_id": "2609.00001"}],
    }
    imported = []

    def fetch(query, maximum):
        assert maximum == 5
        return responses[query]

    def post(url, token, papers, sent_topics):
        assert url == "https://corpus.example/api/ingest"
        assert token == "test-token"
        assert [topic["name"] for topic in sent_topics] == ["Inference", "Methods"]
        imported.extend(papers)
        return {"accepted": len(papers), "duplicate": 0, "rejected": 0}

    environment = {
        "CORPUS_TOPICS": json.dumps(topics),
        "CORPUS_INGEST_URL": "https://corpus.example/api/ingest",
        "CORPUS_INGEST_TOKEN": "test-token",
        "CORPUS_WATCH_STATE": str(state_path),
    }
    first = arxiv_watch.run(environment, fetch=fetch, post=post, pause=lambda _seconds: None)
    second = arxiv_watch.run(environment, fetch=fetch, post=post, pause=lambda _seconds: None)

    assert first == {"topics": 2, "discovered": 1, "accepted": 1, "duplicate": 0, "rejected": 0}
    assert second == {"topics": 2, "discovered": 0, "accepted": 0, "duplicate": 0, "rejected": 0}
    assert imported[0]["field"] == "unfiled"
    assert imported[0]["tags"] == ["topic:inference", "reading", "topic:methods"]
    assert imported[0]["query"] == 'all:"inference" OR all:"methods"'
    assert set(json.loads(state_path.read_text())["topics"].values().__iter__().__next__()) == {"2609.00001"}


def test_topic_configuration_is_validated():
    with pytest.raises(arxiv_watch.WatchError, match="unique"):
        arxiv_watch.load_topics('[{"name":"A","query":"all:a"},{"name":"a","query":"all:b"}]')
    with pytest.raises(arxiv_watch.WatchError, match="valid JSON"):
        arxiv_watch.load_topics("not json")
    assert arxiv_watch.load_topics("") == []
