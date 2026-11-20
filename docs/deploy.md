# Automatic deployment

Every push to `main` that passes the `test` job is deployed. Don't deploy by
hand: merge to `main` and let the pipeline do it. That way deploys never overlap
and the server always runs a tested commit from `main`.

```
push to main → test job → deploy job (queued, never parallel)
             → ssh with a key that can only run corpus-deploy-trigger
             → systemctl start corpus-deploy.service (one at a time)
             → /usr/local/sbin/corpus-deploy
```

## What a deploy does

`scripts/deploy.sh`, installed as `/usr/local/sbin/corpus-deploy`:

1. Fetches `origin/main`. Stops here if the server already runs it.
2. Requires the commit's `test` check run to have passed (read from the public
   GitHub API). If the check is still running, it exits quietly, since that
   commit's own deploy job will follow.
3. Refuses if `/opt/corpus` has uncommitted changes, or if the live commit is not
   an ancestor of `main`. A deploy never silently drops work that is only on the
   server.
4. Takes a backup with `corpus-backup.service`.
5. Installs `requirements.txt` if it changed.
6. **Rehearses** the new code on a copy of that backup: it runs the app's full
   startup (every library's migrations and `prepare_pending`) as `corpus`, with
   no network. If that fails, the live server is not touched.
7. Checks out the commit, reinstalls the standalone `backup.py` copy if it
   changed, restarts `corpus` and waits for `/api/config`.
8. If the server doesn't come up, it checks out the previous commit and restarts
   that. If a migration already ran, restore the snapshot named in the log.

Changes to `docs/corpus*.service` are only reported, not installed.

## Seeing what happened

```bash
journalctl -u corpus-deploy.service -n 50 --no-pager
```

The deploy job's log on GitHub shows the same output.

## Running it by hand

```bash
sudo systemctl start corpus-deploy.service
```

This is the same path the pipeline takes, so it is safe to run while a deploy is
in progress: it joins that run instead of starting a second one. The script
itself also takes a lock.

For a check without changing anything (it still takes a backup):

```bash
sudo env CORPUS_DEPLOY_DRY_RUN=1 /usr/local/sbin/corpus-deploy
```

`CORPUS_DEPLOY_FORCE=1` skips the test and ancestry checks. Use it only when you
know why they failed.

## Installation (already done on the server)

The installed copies are not updated by deploys, so a push to `main` never
changes what runs as root. Reinstall them by hand when these files change:

```bash
sudo install -m 0755 scripts/deploy.sh /usr/local/sbin/corpus-deploy
sudo install -m 0755 scripts/deploy_trigger.sh /usr/local/bin/corpus-deploy-trigger
sudo install -m 0644 docs/corpus-deploy.service /etc/systemd/system/corpus-deploy.service
sudo install -m 0440 docs/corpus-deploy.sudoers /etc/sudoers.d/corpus-deploy
sudo systemctl daemon-reload
```

The deploy key is an ed25519 key in the deploying user's `authorized_keys`:

```
restrict,command="/usr/local/bin/corpus-deploy-trigger" ssh-ed25519 AAAA… corpus-deploy@github-actions
```

The forced command ignores what the client sends. The worst a leaked key can do
is start a deploy of the current tested `main`.

On GitHub, the `production` environment only accepts the `main` branch. It holds:

| Name | Kind | Value |
| --- | --- | --- |
| `CORPUS_DEPLOY_KEY` | secret | the private key |
| `CORPUS_DEPLOY_KNOWN_HOSTS` | secret | `known_hosts` lines for the server's host keys |
| `CORPUS_DEPLOY_HOST` | variable | the SSH host name |
| `CORPUS_DEPLOY_USER` | variable | the SSH user |

To rotate the key, generate a new one, replace the `authorized_keys` line, then
run `gh secret set CORPUS_DEPLOY_KEY --env production < key`.
