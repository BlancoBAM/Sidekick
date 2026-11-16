#!/usr/bin/env bash
# Runs every test. Needs only python3 and node. No network, no API keys, no models.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== python ==";    (cd tests && python3 -m unittest -q test_core test_server test_cli)
echo "== extension =="; node tests/test_extension.js
echo "== worker ==";    node tests/test_worker.js
echo "== syntax ==";    python3 -m py_compile core.py server.py doctor.py scripts/monitor.py
                        node --check extension/background.js; node --check static/whisper-worker.js
                        bash -n run.sh
                        python3 - <<'PY'
import json, re
json.load(open("extension/manifest.json")); json.load(open("models.json.example"))
js = re.search(r"<script>(.*)</script>", open("static/index.html").read(), re.S).group(1)
open("/tmp/_sidekick_page.js", "w").write(js)
PY
                        node --check /tmp/_sidekick_page.js
echo "ALL OK"
