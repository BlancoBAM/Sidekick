#!/usr/bin/env bash
# Sidekick launcher: creates .venv on first run, installs deps (if needed), starts the server.
#   ./run.sh              start server
#   ./run.sh --no-venv    use system python (server is stdlib-only; only faster-whisper needs pip)
#   ./run.sh --monitor    also record system audio (needs ffmpeg + faster-whisper)
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example - edit it, then re-run."; }
[ -f models.json ] || { cp models.json.example models.json; echo "Created models.json from models.json.example."; }

PY=python3
if [[ " $* " != *" --no-venv "* ]]; then
  if [ ! -d .venv ]; then python3 -m venv .venv; fi
  PY=.venv/bin/python
  if ! "$PY" -c "import faster_whisper" 2>/dev/null; then
    echo "Installing faster-whisper (only for server-side transcription; browser mode does not need it). Use ./run.sh --no-venv to skip this."
    "$PY" -m pip install -q -r requirements.txt || echo "pip install failed - browser-side transcription still works."
  fi
fi

"$PY" doctor.py || true
echo
if [[ " $* " == *" --monitor "* ]]; then
  ( sleep 3; exec "$PY" scripts/monitor.py ) &
  MON=$!
  trap 'kill "$MON" 2>/dev/null || true' EXIT
  "$PY" server.py
else
  exec "$PY" server.py
fi
