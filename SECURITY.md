# Security policy

Please do not file public issues for vulnerabilities that could expose a Corpus
library, its PDF cache, an ingest token, or a deployment credential. Report
them privately to the maintainer of the GitHub repository instead.

Corpus supports a small, invite-only group with private per-account libraries.
The existing owner is the sole administrator; invitations cannot grant that role.
The administrator can manage access, while the host operator can read data on disk.
Keep `CORPUS_PASSWORD` and
`CORPUS_INGEST_TOKEN` outside version control, run the application behind TLS,
and do not expose port 8000 directly to the internet. The deployment section in
the README describes the intended nginx and systemd arrangement.

Back up the account registry and every library with `--all-accounts`. Invitation
links and reset links grant access to their recipient account; send them privately.
The ingest token authorizes access only to the administrator library.
