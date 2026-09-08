# Security policy

Please do not file public issues for vulnerabilities that could expose a Corpus
library, its PDF cache, an ingest token, or a deployment credential. Report
them privately to the maintainer of the GitHub repository instead.

Corpus is designed for a single trusted user. Keep `CORPUS_PASSWORD` and
`CORPUS_INGEST_TOKEN` outside version control, run the application behind TLS,
and do not expose port 8000 directly to the internet. The deployment section in
the README describes the intended nginx and systemd arrangement.
