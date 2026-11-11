#!/bin/zsh
# Pull Corpus nightly backups from the server onto this Mac.
#
#   zsh pull_backups_macos.zsh install SSH_HOST [DESTINATION]
#   zsh pull_backups_macos.zsh pull         # what the LaunchAgent runs
#   zsh pull_backups_macos.zsh uninstall
#
# SSH_HOST is what you type after `ssh` to reach the server. DESTINATION
# defaults to "Corpus Backups" in iCloud Drive; a folder on an external drive
# works too, and a run is skipped while the drive is disconnected.
#
# install creates a dedicated key (~/.ssh/corpus_backup_pull) and authorizes
# it on the server, through your normal login, for one forced command:
# `backup.py --serve`, which can only list finished snapshots and stream one.
# It then runs a first pull and installs a LaunchAgent that pulls every four
# hours and at login. Each download is kept only if its SHA-256 matches the
# server's; the newest CORPUS_PULL_KEEP (default 30) snapshots are kept.
set -u -o pipefail

LABEL=local.corpus-backup-pull
KEY=${CORPUS_PULL_KEY:-$HOME/.ssh/corpus_backup_pull}
APP_DIR="$HOME/Library/Application Support/corpus-backup-pull"
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG=$HOME/Library/Logs/corpus-backup-pull.log
SERVE_COMMAND=${CORPUS_SERVE_COMMAND:-/usr/bin/python3 -I /usr/local/lib/corpus-backup/backup.py --serve /var/backups/corpus/nightly}
HOST=${CORPUS_PULL_HOST:-}
DEST=${CORPUS_PULL_DEST:-}
KEEP=${CORPUS_PULL_KEEP:-30}

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" }

# Only the pull key, never a shared connection that may carry your normal login.
remote() {
  ssh -n -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ControlMaster=no -o ControlPath=none \
    -o ForwardAgent=no -o ClearAllForwardings=yes -o ConnectTimeout=30 ${=CORPUS_PULL_SSH_OPTIONS:-} \
    "$HOST" "$@"
}

pull() {
  if [[ -z $HOST || -z $DEST ]] || (( KEEP < 1 )); then
    log "CORPUS_PULL_HOST, CORPUS_PULL_DEST and CORPUS_PULL_KEEP >= 1 are required"; return 2
  fi
  if [[ ! -d ${DEST:h} ]]; then
    log "skipped: ${DEST:h} is not available (drive disconnected?)"; return 1
  fi
  if ! mkdir -p -m 700 "$DEST" || ! ls -A -- "$DEST" >/dev/null; then log "cannot use $DEST"; return 1; fi

  local listing
  if ! listing=$(remote list); then log "could not list backups on $HOST"; return 1; fi

  local name size sum tmp got failed=0 fetched=0
  local -A sums
  for name size sum in ${=listing}; do
    if [[ $name != corpus-<->T<->Z.sqlite3 ]] || (( ${#sum} != 64 )); then
      log "ignored unexpected listing entry: $name"; failed=1; continue
    fi
    sums[$name]=$sum
  done

  # Fetch only what would survive pruning, so pruned snapshots are not fetched again.
  local -a have=("$DEST"/corpus-<->T<->Z.sqlite3(N:t)) union keepers
  union=($have ${(k)sums}); union=(${(ou)union})
  local first=$(( ${#union} > KEEP ? ${#union} - KEEP + 1 : 1 ))
  keepers=(${union[first,-1]})
  for name in ${(ok)sums}; do
    sum=$sums[$name]
    [[ -e $DEST/$name ]] || (( ! ${keepers[(Ie)$name]} )) && continue
    tmp=$DEST/.$name.partial
    if remote get "$name" >| "$tmp" && got=$(shasum -a 256 "$tmp") && [[ ${got%% *} == $sum ]]; then
      chmod 600 "$tmp" && mv -f "$tmp" "$DEST/$name" && fetched=$((fetched + 1))
    else
      rm -f "$tmp"; log "download of $name failed or did not match its checksum"; failed=1
    fi
  done

  local -a files=("$DEST"/corpus-<->T<->Z.sqlite3(N))   # sorted by name, oldest first
  local pruned=0
  if (( ${#files} > KEEP )); then
    pruned=$(( ${#files} - KEEP ))
    rm -f -- "${(@)files[1,pruned]}"
  fi
  log "fetched $fetched, pruned $pruned, keeping $(( ${#files} - pruned )) in $DEST"
  return $failed
}

xml() { local s=$1; s=${s//&/&amp;}; s=${s//</&lt;}; s=${s//>/&gt;}; print -r -- "$s" }

install() {
  [[ $(uname -s) == Darwin ]] || { print -u2 "install sets up a macOS LaunchAgent; elsewhere, schedule 'pull' yourself"; return 1 }
  HOST=${1:-}
  DEST=${2:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Corpus Backups}
  [[ -n $HOST ]] || { print -u2 "usage: zsh $0 install SSH_HOST [DESTINATION]"; return 2 }
  [[ -d ${DEST:h} ]] || { print -u2 "missing folder: ${DEST:h}"; return 1 }
  local self=${ZSH_SCRIPT:-$0}
  [[ -f $self ]] || { print -u2 "run this script from a file, not a pipe"; return 1 }

  mkdir -p "$APP_DIR" "${PLIST:h}" "${LOG:h}" "${KEY:h}" || return 1
  [[ $self -ef $APP_DIR/pull.zsh ]] || cp -f -- "$self" "$APP_DIR/pull.zsh" || return 1
  [[ -f $KEY ]] || ssh-keygen -q -t ed25519 -N '' -C "corpus-backup-pull@$(hostname -s)" -f "$KEY" || return 1

  print "Authorizing the pull key on $HOST (it can only list and download backups)..."
  print -r -- "restrict,command=\"$SERVE_COMMAND\" $(<$KEY.pub)" | ssh "$HOST" \
    'umask 077; mkdir -p ~/.ssh; read -r line; touch ~/.ssh/authorized_keys; grep -qxF -- "$line" ~/.ssh/authorized_keys || printf "%s\n" "$line" >> ~/.ssh/authorized_keys' \
    || { print -u2 "could not reach $HOST with your normal SSH login"; return 1 }

  print "First pull into $DEST ..."
  pull | tee -a "$LOG" || { print -u2 "first pull failed; see above"; return 1 }

  cat >| "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>$(xml "$APP_DIR/pull.zsh")</string><string>pull</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CORPUS_PULL_HOST</key><string>$(xml "$HOST")</string>
    <key>CORPUS_PULL_DEST</key><string>$(xml "$DEST")</string>
    <key>CORPUS_PULL_KEEP</key><string>$KEEP</string>
  </dict>
  <key>StartInterval</key><integer>14400</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null || return 1
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null
  launchctl bootstrap "gui/$UID" "$PLIST" || return 1
  print "Installed: pulls every 4 hours and at login. Checking the first background run..."
  sleep 15
  tail -n 2 "$LOG"
  print "If the last line says 'Operation not permitted', macOS blocked background access to $DEST:"
  print "allow it under System Settings > Privacy & Security, or reinstall with a folder outside iCloud Drive."
}

uninstall() {
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null
  rm -f "$PLIST"
  print "Removed the LaunchAgent. Your backups, $KEY and its line in the server's ~/.ssh/authorized_keys are kept."
}

case ${1:-} in
  install) shift; install "$@" ;;
  pull) pull ;;
  uninstall) uninstall ;;
  *) print -u2 "usage: zsh $0 install SSH_HOST [DESTINATION] | pull | uninstall"; exit 2 ;;
esac
