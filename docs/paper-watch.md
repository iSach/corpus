# Scheduled paper watch

Corpus can check arXiv every day and add new matches to your own library. The watcher runs in GitHub Actions, so it does not depend on an open desktop app or a machine at home. It sends metadata only to the dedicated `POST /api/ingest` endpoint; browser passwords are never used.

The workflow lives at [.github/workflows/paper-watch.yml](../.github/workflows/paper-watch.yml). It runs daily at 07:17 UTC and can also be started manually from the **Actions → Watch papers → Run workflow** page. GitHub may delay scheduled workflows during periods of high load.

## Configure your instance

Set the public ingest URL as a GitHub Actions repository variable and the server's ingest token as a repository secret. Run these commands from a checkout authenticated with `gh`:

```sh
gh variable set CORPUS_INGEST_URL --repo OWNER/REPO --body https://your-corpus.example/api/ingest
ssh deploy@your-server 'sudo -n cat /var/lib/corpus/data/ingest-token' | gh secret set CORPUS_INGEST_TOKEN --repo OWNER/REPO
```

The pipeline passes the token directly from SSH to GitHub; it does not print or save it locally. If `/etc/corpus/corpus.env` defines `CORPUS_INGEST_TOKEN`, use that value instead of the generated token file. A token rotation needs the server value and GitHub secret to be changed together.

## Choose topics

`CORPUS_TOPICS` is a GitHub Actions repository variable. It is JSON and stays out of the public repository, so the published project keeps its generic defaults. Each topic supplies an arXiv `search_query`, an existing Corpus field ID, optional tags, and a result cap.

For example, save this as a local file and upload it without placing it in shell history:

```json
[
  {
    "name": "My research area",
    "query": "cat:cs.LG AND all:\"your research phrase\"",
    "field": "unfiled",
    "tags": ["to-review"],
    "max_results": 25
  }
]
```

Then run:

```sh
gh variable set CORPUS_TOPICS --repo OWNER/REPO --body-file ./my-corpus-topics.json
rm ./my-corpus-topics.json
```

An empty or unset topic variable makes the workflow finish without contacting arXiv or Corpus. `field` must match a field configured on the server. Use `unfiled` until you have added custom fields. The watcher adds a `topic:<topic-name>` tag automatically.

The query is passed to the [arXiv API](https://info.arxiv.org/help/api/user-manual.html) as `search_query`. `all:"phrase"`, `cat:cs.LG`, `AND`, and `OR` are useful building blocks. Keep each topic focused; arXiv's returned papers are sorted by submission date and capped by `max_results` (1–100, default 25).

## What happens on each run

The watcher queries topics sequentially with a three-second pause, normalizes title, authors, abstract, date, DOI, arXiv ID, and PDF URL, then sends at most 100 papers per ingest request. New papers enter Corpus as **unreviewed**, with a source-provided `arXiv` venue and topic tags. It does not assign personal scores.

A small GitHub Actions cache remembers the arXiv IDs already sent for each topic. If that cache is evicted, Corpus is still the final safety net: it deduplicates exact arXiv and DOI matches before it creates papers. The process imports new records; it intentionally does not overwrite metadata when arXiv publishes a later version of an existing paper.

The workflow summary shows accepted, duplicate, and rejected counts in its `Discover and import` log. Check the Corpus **Stats** screen for the associated ingest run and provenance.
