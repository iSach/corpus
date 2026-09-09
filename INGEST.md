# Ingest API

`POST /api/ingest` accepts a batch of proposed papers from an LLM agent. It requires the Bearer token in `data/ingest-token`, or the value of `CORPUS_INGEST_TOKEN` when that variable is configured. The endpoint is rate-limited to 20 batches per minute, accepts 1–100 papers per batch, and rejects request bodies over 2 MiB.

```sh
curl -fsS -X POST http://127.0.0.1:8000/api/ingest \
  -H 'Authorization: Bearer <INGEST_TOKEN>' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "agent": "research-agent/1.0",
  "query": "simulation-based inference neural posterior estimation",
  "papers": [
    {
      "title": "A paper title returned by the source",
      "authors": ["First Author", "Second Author"],
      "venue": "arXiv",
      "year": 2024,
      "abstract": "A short source-grounded abstract.",
      "canonical_url": "https://arxiv.org/abs/2401.01234",
      "arxiv_id": "2401.01234",
      "field": "unfiled",
      "tags": ["neural-posterior", "calibration"],
      "artifacts": [
        {"kind": "pdf", "url": "https://arxiv.org/pdf/2401.01234.pdf"},
        {"kind": "code", "url": "https://github.com/example/project"}
      ],
      "proposed_scores": {
        "relevance": 8,
        "credibility": 7,
        "quality": 8,
        "reimplementation": 6,
        "reason": "The source directly evaluates amortized neural posterior estimation."
      },
      "query": "optional per-paper query override"
    }
  ],
  "extra": {"source": "arxiv-search", "run_label": "weekly-sbi"}
}
JSON
```

`<INGEST_TOKEN>` is a placeholder. Read a generated token with `umask 077; cat data/ingest-token`; do not paste a real token into scripts committed to the repository. `agent` may also be supplied as `agent_identifier`. The batch `query` is stored in provenance; an item's optional `query` overrides it for that item. `extra` is retained as JSON on the ingest run.

Each item must have a title and an ordered author list. DOI and arXiv identifiers are normalized and can be supplied as bare IDs or recognized source URLs. `field` must be one of the configured field IDs. Artifact kinds are `pdf`, `code`, `data`, `slides`, `video`, and `other`; URLs must be HTTP(S) without embedded credentials. If an arXiv ID is present and no artifact list is supplied, the server adds the arXiv PDF link.

Proposed scores belong inside `proposed_scores`. Both short keys (`rel`, `cred`, `qual`, `reimpl`) and long keys (`relevance`, `credibility`, `quality`, `reimplementation`) are accepted, with integer values from 0 through 10. The optional `reason` is stored as the LLM draft explanation. Ingest never writes the reviewer's four personal score columns: accepted papers enter as `unreviewed` with personal scores null.

## Response

The response contains the ingest run ID, aggregate counts, and one result per input index:

```json
{
  "run_id": 12,
  "accepted": 1,
  "duplicate": 0,
  "rejected": 0,
  "results": [
    {
      "index": 0,
      "status": "accept",
      "id": "2401.01234",
      "reason": "Added as unreviewed; PDF queued"
    }
  ]
}
```

The per-item status is exactly one of `accept`, `duplicate`, or `reject`. A duplicate includes the existing paper ID, similarity, and reason. A rejection includes a validation reason, while other items in the batch continue independently. Dedupe checks exact DOI and arXiv matches first, then title/author similarity against `CORPUS_DEDUPE_THRESHOLD` (default `0.91`). Duplicate attempts are recorded in the existing paper's provenance and are not silently discarded.

Accepted PDF artifacts are queued for the in-process worker and downloaded asynchronously. The worker verifies the PDF, extracts text page by page, populates the full-text index, and records failures on the artifact and Stats screen. A slow source therefore does not hold the ingest request open. The cache quota is `CORPUS_CACHE_MAX_BYTES` (10 GiB by default); each PDF is capped at 50 MiB. The detail pane's `CACHE` action calls `POST /api/artifacts/{id}/cache`; it queues linked or failed PDFs. `POST /api/artifacts/{id}/retry` is an alias for the same transition, so both actions are safe to repeat and do nothing for an already cached or pending PDF.

## Related endpoints

- `POST /api/metadata` with `{"identifier":"10...."}` or an arXiv ID fetches normalized metadata from Crossref or arXiv for the manual Add/Edit form.
- `POST /api/duplicates` with a draft paper returns near-duplicate candidates without writing.
- `POST /api/artifacts/{id}/cache` queues a linked or failed PDF for caching.
- `POST /api/artifacts/{id}/retry` is an alias that queues a linked or failed PDF as well.

All routes except `/api/config`, `/api/session`, `/api/login`, and `/api/ingest` require the browser session cookie. Agents should use `/api/ingest`; it is the only write endpoint designed for unattended ingestion.
