#!/bin/bash
# BMG Capital reader on this Mac: the spider, the live report and the finished report,
# in your browser at http://localhost:8000/desk/read/. Nothing is hosted; only this Mac can open it.
#
# Start:  double-click this file (first time: right-click > Open), or in Terminal:
#         bash ~/Downloads/bmg-capital-site/start-reader.command
# Stop:   close the Terminal window, or press Control-C in it.
#
# First run sets up Python libraries in .venv inside this folder (a few minutes, one time).
# Each run pulls the latest version from GitHub when this folder is a git clone.

cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8000}"
URL="http://localhost:$PORT/desk/read/"

pause_exit() { echo; read -r -p "Press Return to close this window." _; exit "$1"; }

# Already running? Just open the page.
if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  echo "The reader is already running. Opening $URL"
  open "$URL" 2>/dev/null
  exit 0
fi

# Latest version (only for a git clone; a ZIP download is updated by downloading again).
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  echo "Checking for updates..."
  git pull --ff-only -q 2>/dev/null || echo "  (could not update; using the copy on this Mac)"
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
echo
exec .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port "$PORT" --log-level warning
