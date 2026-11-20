#!/bin/bash
# Deploy the tip of origin/main to /opt/corpus.
#
# Runs as root from corpus-deploy.service, so systemd allows only one deploy at
# a time; the flock below also covers anyone running this script by hand.
# Install a copy at /usr/local/sbin/corpus-deploy (see docs/deploy.md). The
# installed copy is not updated by a deploy: a push to main must never change
# what runs as root.
#
# Steps: fetch main, require a passing "test" check on that commit, refuse a
# dirty tree or a checkout that would drop commits, back up, rehearse the new
# code's startup (migrations included) on a copy of that backup with no
# network, then switch, restart and health-check. If the restarted server does
# not come up, the previous commit is checked out and restarted.
#
# Exit status: 0 when deployed or nothing to do, 1 when refused or failed.
# CORPUS_DEPLOY_FORCE=1 skips the check and ancestry gates (manual use only).
# CORPUS_DEPLOY_DRY_RUN=1 stops after the rehearsal, leaving the server as is.
set -euo pipefail

APP_DIR=${CORPUS_APP_DIR:-/opt/corpus}
REPO=${CORPUS_DEPLOY_REPO:-iSach/corpus}
BRANCH=main
CHECK_NAME=${CORPUS_DEPLOY_CHECK:-test}
HEALTH_URL=http://127.0.0.1:8000/api/config
BACKUP_DIR=/var/backups/corpus/nightly
BACKUP_COPY=/usr/local/lib/corpus-backup/backup.py
FORCE=${CORPUS_DEPLOY_FORCE:-0}
DRY_RUN=${CORPUS_DEPLOY_DRY_RUN:-0}

log() { echo "corpus-deploy: $*"; }
fail() { log "FAILED: $*"; exit 1; }
git_() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }

exec 9>/run/lock/corpus-deploy.lock
flock -n 9 || { log "another deploy is running; waiting"; flock 9; }

git_ fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
target=$(git_ rev-parse "refs/remotes/origin/$BRANCH")
current=$(git_ rev-parse HEAD)
log "live ${current:0:7}, origin/$BRANCH ${target:0:7}"
if [[ $target == "$current" ]]; then log "already up to date"; exit 0; fi

if [[ $FORCE != 1 ]]; then
  # Only deploy a commit whose test job passed. A newer push whose tests are
  # still running is left to its own deploy job.
  checks=$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$REPO/commits/$target/check-runs?check_name=$CHECK_NAME&filter=latest") \
    || fail "could not read check runs for ${target:0:7}"
  verdict=$(python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs: print("missing")
elif any(r["status"] != "completed" for r in runs): print("pending")
elif all(r["conclusion"] == "success" for r in runs): print("success")
else: print("failure")' <<<"$checks")
  case $verdict in
    success) ;;
    pending|missing) log "'$CHECK_NAME' is $verdict for ${target:0:7}; its own deploy will follow"; exit 0 ;;
    *) fail "'$CHECK_NAME' did not pass for ${target:0:7}" ;;
  esac
  git_ merge-base --is-ancestor "$current" "$target" \
    || fail "live ${current:0:7} is not an ancestor of ${target:0:7}; deploying would drop commits (merge them, or rerun with CORPUS_DEPLOY_FORCE=1)"
fi

[[ -z $(git_ status --porcelain --untracked-files=normal) ]] \
  || fail "$APP_DIR has uncommitted changes; stash or commit them first"

changed() { ! git_ diff --quiet "$current" "$target" -- "$@"; }

log "backing up"
systemctl start corpus-backup.service || fail "backup failed"
snapshot=$(find "$BACKUP_DIR" -maxdepth 1 -name 'corpus-*.tar.gz' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)
[[ -n $snapshot ]] || fail "no snapshot found in $BACKUP_DIR"
log "snapshot $(basename "$snapshot")"

if changed requirements.txt && [[ $DRY_RUN == 1 ]]; then
  log "requirements.txt changed; dry run, so not installing (the rehearsal may fail)"
elif changed requirements.txt; then
  log "requirements.txt changed; installing"
  "$APP_DIR/.venv/bin/pip" install --quiet -r <(git_ show "$target:requirements.txt") || fail "pip install failed"
fi

# Rehearse the new code's startup on a copy of the snapshot: every library's
# migrations and prepare_pending run, then workers stop at once. No network, so
# workers cannot fetch, mail or call an API from the copy.
# The scratch copy lives outside /tmp and /var/tmp, which PrivateTmp hides.
install -d -m 0711 /var/lib/corpus-deploy
work=$(mktemp -d /var/lib/corpus-deploy/rehearsal.XXXXXX)
trap 'rm -rf "$work"' EXIT
mkdir "$work/tree" "$work/data"
git_ archive "$target" | tar -x -C "$work/tree"
tar -xzf "$snapshot" -C "$work/data"
# EnvironmentFile= beats Environment=, so build an env file that points the
# data paths at the copy instead of passing them with -E.
grep -Ev '^[[:space:]]*(export[[:space:]]+)?CORPUS_(DATA_DIR|PDF_CACHE)=' /etc/corpus/corpus.env >"$work/env" || true
printf 'CORPUS_DATA_DIR=%s\nCORPUS_PDF_CACHE=%s\n' "$work/data" "$work/data/pdfs" >>"$work/env"
chown -R corpus:corpus "$work"
chmod 0600 "$work/env"
log "rehearsing startup of ${target:0:7} on the snapshot"
systemd-run --quiet --wait --pipe --collect \
  --uid=corpus --gid=corpus \
  -p PrivateNetwork=yes -p PrivateTmp=yes -p ProtectHome=yes -p NoNewPrivileges=yes \
  -p EnvironmentFile="$work/env" \
  -p WorkingDirectory="$work/tree" \
  "$APP_DIR/.venv/bin/python" -I -c '
import asyncio, sys
sys.path.insert(0, ".")
from corpus.app import create_app
app = create_app()
async def main():
    async with app.router.lifespan_context(app): pass
asyncio.run(asyncio.wait_for(main(), 120))
print("rehearsal ok")' || fail "rehearsal failed; live server untouched"
if [[ $DRY_RUN == 1 ]]; then log "dry run: stopping before the switch"; exit 0; fi

log "switching to ${target:0:7}"
git_ checkout --quiet --detach "$target"

if changed corpus/backup.py; then
  log "corpus/backup.py changed; reinstalling $BACKUP_COPY"
  install -m 0644 "$APP_DIR/corpus/backup.py" "$BACKUP_COPY"
fi
for unit in corpus.service corpus-backup.service corpus-backup.timer; do
  changed "docs/$unit" && log "WARNING: docs/$unit changed; the installed unit is not updated automatically"
done

healthy() {
  for _ in $(seq 30); do
    curl -fsS --max-time 3 -o /dev/null "$HEALTH_URL" && return 0
    sleep 1
  done
  return 1
}

systemctl restart corpus
if healthy; then
  log "deployed ${target:0:7} ($(git_ log -1 --format=%s "$target"))"
  exit 0
fi

log "server did not come up on ${target:0:7}; rolling back to ${current:0:7}"
git_ checkout --quiet --detach "$current"
systemctl restart corpus
healthy && fail "rolled back to ${current:0:7}; if a migration ran, restore $(basename "$snapshot")" \
  || fail "rollback to ${current:0:7} is not healthy either; check journalctl -u corpus"
