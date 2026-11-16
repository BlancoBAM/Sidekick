import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class StubLLM:
    """Tiny OpenAI-compatible server. `plan` maps api-key -> (status, content)."""

    def __init__(self, plan):
        self.plan, self.requests = plan, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                outer.requests.append({"path": self.path, "key": key, "body": body})
                status, content = outer.plan.get(key, (200, "ok"))
                payload = json.dumps({"choices": [{"message": {"content": content}}]} if status == 200
                                     else {"error": "boom " + key}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def write_models(path: Path, entries):
    path.write_text(json.dumps(entries))
