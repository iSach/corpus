"""Scheduled arXiv discovery and import for a Corpus instance.

This module is deliberately dependency-free so it can run in GitHub Actions.
Topics are supplied by ``CORPUS_TOPICS`` as JSON and the resulting metadata is
sent only to Corpus's dedicated Bearer-authenticated ingest endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
MAX_TOPICS = 20
STATE_LIMIT = 1_000


class WatchError(ValueError):
    """A configuration or remote-service failure for the paper watcher."""


def _text(value: str | None) -> str:
    return " ".join((value or "").split())


def _tag_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return "topic:" + (slug[:90] or "watch")


def load_topics(raw: str) -> list[dict[str, Any]]:
    """Validate the JSON topic list stored in ``CORPUS_TOPICS``."""
    if not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WatchError("CORPUS_TOPICS must be valid JSON") from exc
    if not isinstance(decoded, list) or len(decoded) > MAX_TOPICS:
        raise WatchError(f"CORPUS_TOPICS must be a list of at most {MAX_TOPICS} topics")

    topics: list[dict[str, Any]] = []
    names: set[str] = set()
    for value in decoded:
        if not isinstance(value, Mapping):
            raise WatchError("every topic must be an object")
        name, query = value.get("name"), value.get("query")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise WatchError("each topic needs a name of 1–100 characters")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1_000:
            raise WatchError("each topic needs an arXiv query of 1–1,000 characters")
        name = name.strip()
        if name.casefold() in names:
            raise WatchError("topic names must be unique")
        names.add(name.casefold())
        field = value.get("field", "unfiled")
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", field):
            raise WatchError("topic field must be a Corpus field ID")
        tags = value.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 99 or any(
            not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 100 for tag in tags
        ):
            raise WatchError("topic tags must be a list of 0–99 nonempty strings")
        maximum = value.get("max_results", 25)
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise WatchError("topic max_results must be an integer from 1 through 100")
        topics.append(
            {
                "name": name,
                "query": query.strip(),
                "field": field,
                "tags": list(dict.fromkeys([_tag_name(name), *(tag.strip() for tag in tags)])),
                "max_results": maximum,
            }
        )
    return topics


def topic_key(topic: Mapping[str, Any]) -> str:
    """Scope remembered IDs to a topic's actual selection behavior."""
    relevant = {key: topic[key] for key in ("name", "query", "field", "tags", "max_results")}
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def load_state(path: Path) -> dict[str, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchError(f"could not read watcher state at {path}") from exc
    topics = value.get("topics") if isinstance(value, Mapping) else None
    if not isinstance(topics, Mapping):
        raise WatchError(f"watcher state at {path} is invalid")
    clean: dict[str, list[str]] = {}
    for key, ids in topics.items():
        if isinstance(key, str) and isinstance(ids, list):
            clean[key] = [item for item in ids if isinstance(item, str)][-STATE_LIMIT:]
    return clean


def save_state(path: Path, state: Mapping[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "topics": state}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def arxiv_url(query: str, maximum: int) -> str:
    return ARXIV_API + "?" + urllib.parse.urlencode(
        {"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": maximum}
    )


def fetch_arxiv(query: str, maximum: int, *, opener: Callable[..., Any] = urllib.request.urlopen) -> list[dict[str, Any]]:
    """Return normalized public arXiv Atom entries for one search query."""
    request = urllib.request.Request(arxiv_url(query, maximum), headers={"User-Agent": "Corpus paper watch/1.0"})
    try:
        with opener(request, timeout=30) as response:
            body = response.read(4 * 1024 * 1024 + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise WatchError("arXiv search request failed") from exc
    if len(body) > 4 * 1024 * 1024:
        raise WatchError("arXiv search response exceeds 4 MiB")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise WatchError("arXiv returned malformed Atom XML") from exc

    papers: list[dict[str, Any]] = []
    for entry in root.findall(ATOM + "entry"):
        identifier = _text(entry.findtext(ATOM + "id"))
        match = re.search(r"arxiv\.org/abs/([^/]+)$", identifier, re.IGNORECASE)
        if not match:
            continue
        arxiv_id = re.sub(r"v\d+$", "", match.group(1), flags=re.IGNORECASE)
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})", arxiv_id):
            continue
        title = _text(entry.findtext(ATOM + "title"))
        if not title:
            continue
        authors = [_text(author.findtext(ATOM + "name")) for author in entry.findall(ATOM + "author")]
        doi = _text(entry.findtext(ARXIV + "doi")) or None
        journal = _text(entry.findtext(ARXIV + "journal_ref"))
        published = _text(entry.findtext(ATOM + "published"))
        year = int(published[:4]) if re.fullmatch(r"\d{4}-\d\d-\d\dT.*", published) else None
        papers.append(
            {
                "title": title,
                "authors": [author for author in authors if author],
                "venue": journal or "arXiv",
                "year": year,
                "abstract": _text(entry.findtext(ATOM + "summary")),
                "canonical_url": f"https://arxiv.org/abs/{arxiv_id}",
                "arxiv_id": arxiv_id,
                "doi": doi,
                "artifacts": [{"kind": "pdf", "url": f"https://arxiv.org/pdf/{arxiv_id}.pdf"}],
            }
        )
    return papers


def discover(
    topics: list[dict[str, Any]], state: Mapping[str, list[str]], *, fetch: Callable[[str, int], list[dict[str, Any]]] = fetch_arxiv,
    pause: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Fetch unseen papers, merging papers that match more than one topic."""
    next_state = {key: list(ids)[-STATE_LIMIT:] for key, ids in state.items()}
    merged: dict[str, dict[str, Any]] = {}
    for index, topic in enumerate(topics):
        if index:
            pause(3.0)  # arXiv asks API clients to pace successive requests.
        key = topic_key(topic)
        seen = set(next_state.get(key, []))
        entries = fetch(topic["query"], topic["max_results"])
        fresh_ids: list[str] = []
        for paper in entries:
            paper_id = paper["arxiv_id"]
            if paper_id in seen:
                continue
            seen.add(paper_id)
            fresh_ids.append(paper_id)
            if paper_id not in merged:
                merged[paper_id] = {**paper, "field": topic["field"], "tags": list(topic["tags"]), "query": topic["query"]}
            else:
                existing = merged[paper_id]
                existing["tags"] = list(dict.fromkeys([*existing["tags"], *topic["tags"]]))
                existing["query"] += " OR " + topic["query"]
        if fresh_ids:
            next_state[key] = [*next_state.get(key, []), *fresh_ids][-STATE_LIMIT:]
    return list(merged.values()), next_state


def post_ingest(url: str, token: str, papers: list[dict[str, Any]], topics: list[dict[str, Any]], *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise WatchError("CORPUS_INGEST_URL must be an HTTPS URL without credentials")
    payload = json.dumps(
        {
            "agent": "corpus-arxiv-watch/1.0",
            "query": "scheduled arXiv topic watch",
            "papers": papers,
            "extra": {"source": "arxiv", "topics": [topic["name"] for topic in topics]},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "Corpus paper watch/1.0"},
    )
    try:
        with opener(request, timeout=45) as response:
            response_body = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        raise WatchError(f"Corpus ingest returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise WatchError("Corpus ingest request failed") from exc
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise WatchError("Corpus ingest returned invalid JSON") from exc
    if not isinstance(result, Mapping):
        raise WatchError("Corpus ingest returned an invalid response")
    return dict(result)


def run(environment: Mapping[str, str] = os.environ, *, fetch: Callable[[str, int], list[dict[str, Any]]] = fetch_arxiv, post: Callable[..., dict[str, Any]] = post_ingest, pause: Callable[[float], None] = time.sleep) -> dict[str, int]:
    topics = load_topics(environment.get("CORPUS_TOPICS", ""))
    if not topics:
        return {"topics": 0, "discovered": 0, "accepted": 0, "duplicate": 0, "rejected": 0}
    url, token = environment.get("CORPUS_INGEST_URL", ""), environment.get("CORPUS_INGEST_TOKEN", "")
    if not token:
        raise WatchError("CORPUS_INGEST_TOKEN is required when topics are configured")
    state_path = Path(environment.get("CORPUS_WATCH_STATE", ".automation/arxiv-watch.json"))
    state = load_state(state_path)
    papers, next_state = discover(topics, state, fetch=fetch, pause=pause)
    if not papers:
        save_state(state_path, next_state)
        return {"topics": len(topics), "discovered": 0, "accepted": 0, "duplicate": 0, "rejected": 0}
    totals = {"topics": len(topics), "discovered": len(papers), "accepted": 0, "duplicate": 0, "rejected": 0}
    for start in range(0, len(papers), 100):
        result = post(url, token, papers[start:start + 100], topics)
        for key in ("accepted", "duplicate", "rejected"):
            value = result.get(key, 0)
            if type(value) is not int or value < 0:
                raise WatchError("Corpus ingest returned invalid counts")
            totals[key] += value
    save_state(state_path, next_state)
    return totals


def main() -> int:
    try:
        print(json.dumps(run(), sort_keys=True))
    except WatchError as exc:
        print(f"Corpus paper watch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
