#!/bin/bash
# Forced command for the GitHub Actions deploy key (see docs/deploy.md).
# Ignores whatever the client asked to run: the key can only start the deploy
# and read its log. Install at /usr/local/bin/corpus-deploy-trigger.
set -uo pipefail
since=$(date '+%Y-%m-%d %H:%M:%S')
sudo -n /usr/bin/systemctl start corpus-deploy.service
status=$?
# Starting a unit that is already running joins that run, so this log may
# include output from a deploy someone else started.
sudo -n /usr/bin/journalctl -u corpus-deploy.service --since "$since" --no-pager -o cat
exit $status
