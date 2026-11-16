#!/usr/bin/env python3
"""Sidekick HTTP server (stdlib only). Run: python server.py"""
from __future__ import annotations

import json
import mimetypes
import socket
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core

STATIC = core.ROOT / "static"
MAX_AUDIO = 8 * 1024 * 1024      # ~4 minutes of 16 kHz int16 - chunks are ~10 s
MAX_JSON = 16 * 1024 * 1024      # page text + base64 screenshot

# Common local LLM server backends -> default base URL
BACKEND_DEFAULTS = {
    "lm-studio": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "llama.cpp": "http://127.0.0.1:8080/v1",
    "lemonade": "http://127.0.0.1:13305/v1",
    "vllm": "http://127.0.0.1:8000/v1",
    "ramallama": "http://127.0.0.1:8080/v1",
}

_AUTODETECT_PORTS = [11434, 1234, 8080, 13305, 8000, 9000, 5000, 3000]


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _autodetect_backends() -> list[dict]:
    found = []
    for port in _AUTODETECT_PORTS:
        if _port_open(port):
            found.append({"port": port, "base_url": f"http://127.0.0.1:{port}/v1"})
    return found


def _fetch_backend_models(base_url: str) -> dict:
    """Proxy a backend's /v1/models so the browser stays same-origin and keys stay server-side."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "models": []}


def make_handler(app: core.App):
    port = app.s.port

    class Handler(BaseHTTPRequestHandler):
        server_version = "Sidekick/1.0"

        # -- plumbing ------------------------------------------------------ #
        def log_message(self, fmt, *args):  # quiet, no bodies, no secrets
            if "/api/state" not in (args[0] if args else ""):
                sys.stderr.write("[sidekick] " + (fmt % args) + "\n")

        def _host_ok(self) -> bool:
            # Defeats DNS-rebinding: only accept requests addressed to loopback.
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"} \
                or host.startswith("[::1]")

        def _cors(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://") or origin in (
                    f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _err(self, code: int, msg: str) -> None:
            self._json(code, {"error": msg})

        def _body(self, limit: int) -> bytes | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > limit:
                self._err(413, f"body too large (limit {limit} bytes)")
                return None
            return self.rfile.read(n)

        def _json_body(self, limit: int = MAX_JSON):
            raw = self._body(limit)
            if raw is None:
                return None
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":  # blocks cross-site "simple" form posts
                self._err(415, "Content-Type must be application/json")
                return None
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._err(400, "invalid JSON")
                return None
            if not isinstance(data, dict):
                self._err(400, "JSON body must be an object")
                return None
            return data

        def _vid(self, v):
            if v in (None, "", "current"):
                return None
            return int(v)

        # -- routes -------------------------------------------------------- #
        def do_OPTIONS(self):
            if not self._host_ok():
                return self._err(403, "bad Host header")
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._host_ok():
                return self._err(403, "bad Host header")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if u.path.startswith("/static/"):
                return self._file(STATIC / u.path[len("/static/"):])
            if u.path == "/api/health":
                return self._json(200, {"ok": True})
            if u.path == "/api/state":
                return self._json(200, app.state())
            if u.path == "/api/transcript":
                try:
                    v = app.store.get(self._vid((q.get("video") or [None])[0]))
                except (KeyError, ValueError) as e:
                    return self._err(404, str(e))
                return self._send(200, v.text().encode(), "text/plain; charset=utf-8")
            if u.path == "/api/backend/autodetect":
                return self._json(200, _autodetect_backends())
            if u.path == "/api/backend/models":
                base = (q.get("base_url") or [None])[0]
                if not base:
                    return self._err(400, "base_url required")
                return self._json(200, _fetch_backend_models(base))
            self._err(404, "not found")

        def _file(self, path: Path):
            try:
                path = path.resolve()
                if STATIC.resolve() not in path.parents or not path.is_file():
                    return self._err(404, "not found")
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if path.suffix in (".js", ".mjs"):
                    ctype = "text/javascript"
                self._send(200, path.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            except OSError:
                self._err(404, "not found")

        def do_POST(self):
            if not self._host_ok():
                return self._err(403, "bad Host header")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/api/video/new":
                    d = self._json_body(64 * 1024)
                    if d is None:
                        return
                    v = app.store.new_video(str(d.get("title") or ""))
                    return self._json(200, {"id": v.id, "title": v.title, "file": v.path.name})

                if u.path == "/api/audio":  # raw little-endian int16 mono 16 kHz
                    raw = self._body(MAX_AUDIO)
                    if raw is None:
                        return
                    t0 = float((q.get("t0") or ["0"])[0])
                    vid = self._vid((q.get("video") or [None])[0])
                    n = app.add_audio(vid, raw, t0)
                    return self._json(200, {"added": n})

                if u.path == "/api/segments":  # browser-side Whisper results
                    d = self._json_body(2 * 1024 * 1024)
                    if d is None:
                        return
                    segs = [(float(s["start"]), str(s["text"])) for s in d.get("segments", [])]
                    n = app.store.add(self._vid(d.get("video")), segs)
                    return self._json(200, {"added": n})

                if u.path == "/api/ask":
                    d = self._json_body()
                    if d is None:
                        return
                    res = app.ask(str(d.get("question") or d.get("page_text") or ""),
                                  str(d.get("page_url") or ""), d.get("image") or None,
                                  self._vid(d.get("video")), d.get("model") or None)
                    return self._json(200, res)

                if u.path == "/api/backend/ask":
                    d = self._json_body()
                    if d is None:
                        return
                    base = str(d.get("base_url") or "").rstrip("/")
                    model = str(d.get("model") or "")
                    key = str(d.get("api_key") or "")
                    if not base or not model:
                        return self._err(400, "base_url and model required")
                    entry = core.ModelEntry("backend", base, model, key, False, {})
                    video = app.store.get(self._vid(d.get("video")))
                    user = core.build_user_message(video, str(d.get("question") or d.get("page_text") or ""),
                                                   str(d.get("page_url") or ""), app.s.max_transcript_chars, False)
                    try:
                        answer = core._call_chat(entry,
                            [{"role": "system", "content": core.SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                            None, app.s.max_tokens, app.s.request_timeout)
                        return self._json(200, {"answer": answer, "model": model, "video": video.id,
                                                "transcript_words": video.words})
                    except core.LLMError as e:
                        return self._err(502, str(e))

                if u.path == "/api/reload":
                    r = app.reload_chain()
                    return self._json(200, {"models": [e.name for e in r.active],
                                            "skipped": [{"name": n, "reason": why} for n, why in r.skipped]})
            except core.LLMError as e:
                return self._err(502, str(e))
            except (ValueError, KeyError, TypeError) as e:
                return self._err(400, str(e))
            except RuntimeError as e:  # e.g. faster-whisper missing
                return self._err(500, str(e))
            except Exception as e:  # last resort: never leak a traceback to the page
                sys.stderr.write(f"[sidekick] internal error: {e!r}\n")
                return self._err(500, "internal error (see server log)")
            self._err(404, "not found")

    return Handler


def main() -> None:
    core.load_dotenv()
    app = core.App()
    srv = ThreadingHTTPServer((app.s.host, app.s.port), make_handler(app))
    srv.daemon_threads = True
    st = app.state()
    print(f"Sidekick on http://{app.s.host}:{app.s.port}")
    print(f"  transcripts: {app.s.transcripts_dir}")
    print(f"  models in chain: {', '.join(st['models']) or 'NONE'}")
    for s in st["skipped"]:
        print(f"  skipped {s['name']}: {s['reason']}")
    if not st["models"]:
        print("  WARNING: no usable model. Transcription still works; Ask will not. Run: python doctor.py")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
