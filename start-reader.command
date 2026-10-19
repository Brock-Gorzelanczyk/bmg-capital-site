#!/bin/bash
# BMG Capital reader on this Mac: the spider, the live report and the finished report,
# in your browser at http://localhost:8000/desk/read/. Nothing is hosted; only this Mac can open it.
#
# Start:  double-click this file (first time: right-click > Open), or in Terminal:
#         bash ~/Downloads/bmg-capital-site/start-reader.command
# Stop:   close the Terminal window, or press Control-C in it.
#
# First run sets up Python libraries in .venv inside this folder (a few minutes, one time).
#
# Stays up to date by itself: on start, and every 2 minutes while it runs, it pulls the branch it follows
# from GitHub (fast-forward only). New Python code restarts the server; the open page loads a new version by
# itself when nothing would be lost.
# Which branch: the one this folder is on, unless ~/.bmg-reader-follow names another one, e.g.
#   echo main > ~/.bmg-reader-follow
# It updates only while this folder is on that branch: another branch checked out here (an agent's codex/*
# work, say) is never moved. Uncommitted edits are never touched: an update goes ahead past them unless it
# changes the same file (as git pull does), and then it waits and the window says so. A copy with commits
# GitHub does not have and nothing new from GitHub is left as it is; one that has both (an old local merge)
# keeps its commits on a branch named reader-backup-<date> and moves to GitHub's version.

cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8000}"
URL="http://localhost:$PORT/desk/read/"

pause_exit() { echo; read -r -p "Press Return to close this window." _; exit "$1"; }

# Already running? Just open the page.
if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  echo "The reader is already running. Opening $URL"
  open "$URL" 2>/dev/null
  # the window that runs it checks GitHub every 2 minutes; its process number is in .local-data/updater.pid. No
  # such window running: the reader was started some other way and is not updating itself
  [ -d .git ] || { echo "This copy is not a git clone: it is updated by downloading it again."; exit 0; }
  upid="$(cat .local-data/updater.pid 2>/dev/null)"
  if [ -n "$upid" ] && kill -0 "$upid" 2>/dev/null && ps -p "$upid" -o command= 2>/dev/null | grep -q start-reader; then
    [ -f .local-data/update.log ] && echo "It updates itself every 2 minutes. Last check: $(tail -n 1 .local-data/update.log)"
    exit 0
  fi
  echo "The running reader was not started with this file, so it is not updating itself."
  echo "Stop it (close the window that runs it, or Control-C there), then open this file again."
  pause_exit 0
fi

# Latest version (only for a git clone; a ZIP download is updated by downloading again).
FOLLOW=""
[ -s "$HOME/.bmg-reader-follow" ] && FOLLOW="$(tr -d '[:space:]' < "$HOME/.bmg-reader-follow")"
HAS_GIT=0
[ -d .git ] && command -v git >/dev/null 2>&1 && HAS_GIT=1

clean() { [ -z "$(git status --porcelain --untracked-files=no)" ]; }

ULOG=".local-data/update.log"   # one line per check, so a stalled update always shows its reason
ulog() { mkdir -p .local-data; echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$ULOG"; }
say_once() {   # print a problem once per kind, log it every time
  ulog "$2"
  case " $SAID " in *" $1 "*) ;; *) echo "  $2"; SAID="$SAID $1";; esac
}

update() {   # returns 0 and sets CHANGED when new commits were pulled
  [ "$HAS_GIT" = 1 ] || return 1
  local before after b remote
  # FETCH_HEAD is what GitHub just sent, whatever this clone's remote settings track
  if ! git fetch -q origin "$FOLLOW" 2>>"$ULOG"; then
    say_once fetch "Could not reach GitHub for $FOLLOW; will keep trying every 2 minutes."
    return 1
  fi
  remote="$(git rev-parse FETCH_HEAD)"
  before="$(git rev-parse HEAD)"
  # another branch checked out in this folder since the reader started: never moved
  local on; on="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$on" != "$FOLLOW" ]; then say_once branch "Not updating: this folder is on $on, not $FOLLOW."; return 1; fi
  if [ "$before" = "$remote" ]; then ulog "up to date ${before:0:7}"; return 1; fi
  # commits made here that GitHub does not have yet, and nothing new from GitHub: left as they are
  if git merge-base --is-ancestor "$remote" HEAD; then say_once ahead "This copy has commits GitHub does not have yet; not updating."; return 1; fi
  if git merge-base --is-ancestor HEAD "$remote"; then
    # a file edited on this Mac holds the update up only when the update changes that file too (as git pull does)
    git merge --ff-only -q "$remote" 2>>"$ULOG" || {
      local edited; edited="$(git status --porcelain --untracked-files=no | cut -c4- | tr '\n' ' ')"
      if [ -n "$edited" ]; then say_once merge "Updates are waiting for ${remote:0:7}: files edited on this Mac are changed by it too: ${edited}(run: git stash; details in $ULOG)"
      else say_once merge "Could not update to ${remote:0:7}; see $ULOG"; fi
      return 1
    }
  else
    if ! clean; then
      say_once dirty "Updates are waiting: these files were edited on this Mac: $(git status --porcelain --untracked-files=no | awk '{print $2}' | tr '\n' ' ')(run: git stash)"
      return 1
    fi
    b="reader-backup-$(date '+%Y%m%d-%H%M%S')"
    git branch "$b" HEAD && git reset -q --hard "$remote" || return 1
    echo "  This copy had commits GitHub does not; they are kept on branch $b."
  fi
  SAID=""
  ulog "updated ${before:0:7} -> ${remote:0:7}"
  after="$(git rev-parse HEAD)"
  [ "$before" != "$after" ] || return 1
  CHANGED="$(git diff --name-only "$before" "$after")"
  return 0
}

if [ "$HAS_GIT" = 1 ]; then
  CURRENT="$(git rev-parse --abbrev-ref HEAD)"
  [ -n "$FOLLOW" ] || FOLLOW="$CURRENT"
  echo "Following branch: $FOLLOW"
  git fetch -q origin "$FOLLOW" 2>/dev/null || echo "  (could not reach GitHub, or it has no branch $FOLLOW; using the copy on this Mac)"
  if [ "$CURRENT" != "$FOLLOW" ]; then
    if clean; then
      git checkout -q "$FOLLOW" 2>/dev/null || git checkout -q -b "$FOLLOW" "origin/$FOLLOW" 2>/dev/null \
        || echo "  (could not switch from $CURRENT to $FOLLOW; staying on $CURRENT)"
    else
      echo "  Staying on $CURRENT: it has uncommitted changes. Commit or stash them to follow $FOLLOW."
      FOLLOW="$CURRENT"
    fi
  fi
  update && echo "  Updated to $(git log -1 --format='%h %s' | cut -c1-90)"
fi

# Python 3.10 or newer.
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed and was not found."
  echo "Install it from python.org (the page is opening now), then run this file again."
  open "https://www.python.org/downloads/macos/" 2>/dev/null
  pause_exit 1
fi

# One-time setup, and again whenever requirements.txt changes.
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up with $PY (a few minutes, one time only)..."
  "$PY" -m venv .venv || { echo "Could not create .venv"; pause_exit 1; }
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing libraries..."
  .venv/bin/python -m pip install -q --upgrade pip &&
  .venv/bin/python -m pip install -q -r requirements.txt &&
  touch .venv/.installed || { echo "Library install failed (see the messages above)."; pause_exit 1; }
fi

# Local mode: no background intake jobs; data and cache stay in this folder.
mkdir -p .local-data/cache
export BMG_LOCAL=1 INTAKE_DATA_DIR="$PWD/.local-data" BMG_CACHE_DIR="$PWD/.local-data/cache"

# Optional second check: Claude re-reads each statement figure on its page (a few cents per filing at most).
# Put your Anthropic API key, alone on one line, in ~/.bmg-anthropic-key (outside this folder, so it is never
# committed). No file, no check: the reader works exactly as before. Flagged figures are logged to
# .local-data/verify-log.jsonl.
if [ -z "$ANTHROPIC_API_KEY" ] && [ -f "$HOME/.bmg-anthropic-key" ]; then
  ANTHROPIC_API_KEY="$(tr -d '[:space:]' < "$HOME/.bmg-anthropic-key")"
  export ANTHROPIC_API_KEY
fi
if [ -n "$ANTHROPIC_API_KEY" ]; then echo "Second check: on (Claude re-reads each statement figure)."; else echo "Second check: off (no ~/.bmg-anthropic-key)."; fi

# Open the browser once the server answers.
(
  for _ in $(seq 1 60); do
    if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then open "$URL" 2>/dev/null; exit 0; fi
    sleep 1
  done
) &

echo
echo "BMG Capital reader: $URL"
echo "Leave this window open while you use it. Close it (or press Control-C) to stop."
echo "Updates from GitHub ($FOLLOW) are pulled every 2 minutes while it runs."
echo

start_server() { .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port "$PORT" --log-level warning & SERVER=$!; }
trap 'kill "$SERVER" 2>/dev/null; exit 0' INT TERM HUP
start_server
mkdir -p .local-data && echo $$ > .local-data/updater.pid      # for a second open of this file: updates are running
TICK=0
while kill -0 "$SERVER" 2>/dev/null; do
  sleep 5
  TICK=$((TICK + 5))
  [ "$TICK" -ge 120 ] || continue
  TICK=0
  if update; then
    echo "$(date '+%H:%M') Updated to $(git log -1 --format='%h %s' | cut -c1-90)"
    if echo "$CHANGED" | grep -q '^requirements.txt$'; then
      .venv/bin/python -m pip install -q -r requirements.txt && touch .venv/.installed
    fi
    if echo "$CHANGED" | grep -qE '\.py$|^requirements.txt$'; then
      echo "  Server code changed: restarting (saved reads are kept)."
      kill "$SERVER" 2>/dev/null; wait "$SERVER" 2>/dev/null
      start_server
    else
      echo "  Page changed: the open Crawler page reloads by itself (or shows a Reload button mid-read)."
    fi
  fi
done
echo "The reader stopped."
pause_exit 0
