# Ingest API

`POST /api/ingest` accepts a batch of proposed papers from an LLM agent. It requires the Bearer token in `data/ingest-token`, or the value of `CORPUS_INGEST_TOKEN` when that variable is configured. The endpoint is rate-limited to 20 batches per minute, accepts 1–100 papers per batch, and rejects request bodies over 2 MiB.

Valid proposals wait in the owner's approval Inbox. Ingest does not add them to library search or exports. PDFs can be prepared before approval for a requested full-text check. The ingest token cannot approve papers, including when a submitted object contains an `admission` or review-state override.

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

Research topics created through **Topics** use these same field IDs. After adding the example uncertainty-calibration topic, file related proposals with `"field":"uncertainty-calibration"`. Its research brief is in `topics/uncertainty-calibration.json`; the authenticated `GET /api/topics` endpoint returns stored topic descriptions for a signed-in user.

An optional `projects` list assigns a proposal to several existing topic IDs, for example `"projects":["uncertainty-calibration","sbi-pretrain"]`. `field` is the primary topic and must belong to that list; when it does not, the first listed project becomes primary. Omitting `projects` preserves the single-topic ingest format. Multiple assignments do not change the approval gate: all automatic proposals remain pending. Library filtering and exports use all project memberships.

Proposed scores belong inside `proposed_scores`. Both short keys (`rel`, `cred`, `qual`, `reimpl`) and long keys (`relevance`, `credibility`, `quality`, `reimplementation`) are accepted, with integer values from 0 through 10. Include a short, source-grounded `reason` when available; it is shown as the suggested match in the Inbox. The discovery query is shown when no explanation was supplied. Ingest never writes the reviewer's four personal score columns: accepted proposals enter with `admission: "pending"`, review state `unreviewed`, and personal scores null. Approval and reading are separate decisions.

## Response

The response contains the ingest run ID, aggregate counts, and one result per input index:

```json
{
  "run_id": 12,
  "accepted": 1,
  "queued": 1,
  "duplicate": 0,
  "rejected": 0,
  "results": [
    {
      "index": 0,
      "status": "accept",
      "id": "2401.01234",
      "admission": "pending",
      "reason": "Queued in Inbox for approval"
    }
  ]
}
```

The per-item status is exactly one of `accept`, `duplicate`, or `reject`. `accepted` and `accept` indicate that a proposal passed validation and entered the Inbox; they do not indicate human approval. `queued` is the same count and makes that meaning explicit. A duplicate includes the existing paper ID, admission status, similarity, and reason. A rejection includes a validation reason, while other items in the batch continue independently. Dedupe checks exact DOI and arXiv matches first, then title/author similarity against `CORPUS_DEDUPE_THRESHOLD` (default `0.91`), across approved, pending, and dismissed papers. Previously dismissed papers are not queued again. Duplicate attempts are recorded in the existing paper's provenance and are not silently discarded.

PDF artifacts initially remain linked unless an abstract assessment requests full-text preparation. After approval, the in-process worker downloads any remaining PDFs asynchronously, verifies them, extracts page text, populates the full-text index, and records failures on the artifact and Stats screen. The cache quota is `CORPUS_CACHE_MAX_BYTES` (10 GiB by default); each PDF is capped at 50 MiB. The detail pane's `CACHE` action calls `POST /api/artifacts/{id}/cache`; it queues linked or failed PDFs for approved papers. `POST /api/artifacts/{id}/retry` is an alias. Both reject unapproved papers with HTTP 409; use the explicit review workflow for pre-approval preparation.

## Structured relevance assessments

Each newly ingested paper may include an `assessment` object. Ingest accepts abstract-only evidence; do not include methods or results learned from the full text in that initial assessment. An invalid assessment rejects that item atomically without rejecting other items. Duplicate submissions do not overwrite existing assessments.

```json
{
  "stage": "abstract",
  "priority": "high",
  "role": "evaluation",
  "confidence": "moderate",
  "explanation": "The abstract reports calibration error on held-out and shifted test sets. This directly evaluates calibration under distribution shift. Whether the shifts are natural or synthetic is unclear.",
  "limitation": "The abstract does not say which calibration metric or binning scheme is used.",
  "questions": ["Is the method compared against temperature scaling?"],
  "source_version": "v2"
}
```

`priority` is `high`, `medium`, `low`, or `exclude`; `role` is `model`, `evaluation`, `supporting`, or `adjacent`; `confidence` is `high`, `moderate`, or `low`. Relevance, confidence, and model quality are distinct. `questions` contains only uncertainties that could change the keep/dismiss decision or the paper's research use; these and high relevance trigger automatic PDF preparation. Clearly useful supporting work and clearly weak matches do not require automatic preparation. No judgments or evidence levels are inferred from legacy scores or search queries.

Use a versioned canonical URL and PDF URL when a revision is known. An explicit arXiv version must match the PDF link; otherwise preparation reports the mismatch. Leave `source_version` empty when unknown. Both stages must use the same recorded version.

The following routes accept the same Bearer token as ingestion, or the owner's browser session:

1. `GET /api/review/work?limit=10` returns up to 50 work items with current/abstract assessments and the topic's current brief and criteria in `review.topic`. `review.current.snapshot.topic` records the topic as that assessment saw it; its name and description may have been edited since, but its criteria always match. Save the current assessment ID. The queue includes unassessed pending papers and ready checks, including checks completed after the owner approved early.
2. `GET /api/review/text?id=PAPER_ID&page=1` reads an extracted PDF page once preparation is ready. It returns `page`, `pages`, `text`, `artifact_id`, and `abstract_id`. Read the sections needed to resolve the recorded questions; a downloaded PDF alone is not evidence of a completed check.
3. `POST /api/review/assessments` appends an assessment. Supply `id`, the fields above, and `expected_assessment_id` from the work item. Stale assessments return HTTP 409. New abstract assessments contain neither `sections` nor page evidence.

For a full-text check, supply `stage: "full_text"`, `abstract_id` from the work item's abstract assessment, the same `source_version`, the actual `sections` inspected, and `evidence` such as `[{"page": 7, "quote": "An exact supporting passage copied from that PDF page."}]`. At least one passage of 12–3,000 characters is required and checked against the extracted page, allowing whitespace differences. Describe the verified contribution and its limits; preserve missing information as unknown. Previous assessments and their source/topic snapshots remain immutable. The UI compares the new judgment with its abstract baseline.

These endpoints append evidence only. They cannot approve, dismiss, change paper metadata, or set personal ratings. Full-text preparation uses the same downloader protections and quotas as normal caching. No external model service is invoked by the server; the owner or discovery agent supplies assessments. `POST /api/review/check` is a browser-session-only action for explicitly requesting or retrying preparation.

## Related endpoints

- `POST /api/metadata` with `{"identifier":"10...."}` or an arXiv ID fetches normalized metadata from Crossref or arXiv for the manual Add/Edit form.
- `POST /api/duplicates` with a draft paper returns near-duplicate candidates without writing.
- `POST /api/artifacts/{id}/cache` queues a linked or failed PDF for caching.
- `POST /api/artifacts/{id}/retry` is an alias that queues a linked or failed PDF as well.

All routes except the public sign-in/configuration endpoints and the explicitly listed agent routes require the browser session cookie. Agents use `/api/ingest` for proposals and `/api/review/work`, `/api/review/text`, and `/api/review/assessments` for source-grounded assessments. Passkeys apply to interactive browser sign-in and do not replace the ingest Bearer token.
