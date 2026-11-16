import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from helpers import StubLLM, write_models
import core
import server


class FakeTranscriber:
    loaded = False
    def transcribe_pcm16(self, pcm, t0):
        return [(t0, "spoken words")]


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = StubLLM({"": (200, "Answer: B\nWhy: [00:05] said so\nConfidence: HIGH")})
        cls.tmp = tempfile.mkdtemp()
        mf = Path(cls.tmp) / "models.json"
        write_models(mf, [{"name": "stub", "base_url": cls.stub.url, "model": "m", "key_optional": True, "vision": True}])
        s = core.Settings(transcripts_dir=Path(cls.tmp) / "t", models_file=mf, images=False)
        cls.app = core.App(s)
        cls.app.transcriber = FakeTranscriber()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        cls.port = cls.httpd.server_port
        s.port = cls.port
        cls.httpd.RequestHandlerClass = server.make_handler(cls.app)  # rebuild with real port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.stub.close()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body); h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse(); data = r.read()
        return r.status, dict(r.getheaders()), data

    def j(self, *a, **k):
        st, h, d = self.req(*a, **k)
        return st, json.loads(d or b"{}")

    def test_01_serves_page_and_worker(self):
        st, h, d = self.req("GET", "/")
        self.assertEqual(st, 200); self.assertIn(b"Sidekick", d)
        st, h, d = self.req("GET", "/static/whisper-worker.js")
        self.assertEqual(st, 200); self.assertTrue(h["Content-Type"].startswith("text/javascript"))

    def test_02_path_traversal_blocked(self):
        for p in ("/static/../server.py", "/static/..%2fserver.py", "/static/../../etc/passwd"):
            self.assertEqual(self.req("GET", p)[0], 404, p)

    def test_03_host_header_rebinding_blocked(self):
        self.assertEqual(self.req("GET", "/api/state", host="evil.example.com")[0], 403)
        self.assertEqual(self.req("POST", "/api/ask", {"question": "x"}, host="evil.example.com")[0], 403)

    def test_04_cors_only_for_extension_and_self(self):
        _, h, _ = self.req("GET", "/api/state", headers={"Origin": "chrome-extension://abc"})
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "chrome-extension://abc")
        _, h, _ = self.req("GET", "/api/state", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", h)
        st, h, _ = self.req("OPTIONS", "/api/ask", headers={"Origin": "chrome-extension://abc"})
        self.assertEqual(st, 204)

    def test_05_post_requires_json_content_type(self):
        st, d = self.j("POST", "/api/ask", b'{"question":"x"}', headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_06_video_audio_segments_flow(self):
        st, d = self.j("POST", "/api/video/new", {"title": "Lesson 1"})
        self.assertEqual((st, d["id"]), (200, 1))
        st, d = self.j("POST", f"/api/audio?video=1&t0=12.5", b"\0" * 4000, headers={"Content-Type": "application/octet-stream"})
        self.assertEqual((st, d["added"]), (200, 1))
        st, d = self.j("POST", "/api/segments", {"video": 1, "segments": [{"start": 20, "text": "browser words"}, {"start": 22, "text": "[BLANK_AUDIO]"}]})
        self.assertEqual(d["added"], 1)
        st, h, body = self.req("GET", "/api/transcript?video=1")
        self.assertEqual(body.decode(), "[00:12] spoken words\n[00:20] browser words")
        self.assertEqual(self.req("GET", "/api/transcript?video=99")[0], 404)

    def test_07_ask_returns_answer_and_uses_transcript(self):
        st, d = self.j("POST", "/api/ask", {"question": "Q1 pick one [x] B", "page_url": "http://quiz", "video": "current"})
        self.assertEqual(st, 200); self.assertIn("Answer: B", d["answer"]); self.assertEqual(d["model"], "stub")
        sent = self.stub.requests[-1]["body"]["messages"][1]["content"]
        self.assertIn("spoken words", sent); self.assertIn("[x] B", sent)

    def test_08_ask_errors_are_clean(self):
        st, d = self.j("POST", "/api/ask", {"question": "", "image": "QUJD"})  # images off, no text
        self.assertEqual(st, 400); self.assertIn("nothing to ask", d["error"])
        st, d = self.j("POST", "/api/ask", b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, d = self.j("POST", "/api/ask", {"question": "q", "video": "abc"})
        self.assertEqual(st, 400)

    def test_09_oversize_rejected(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/audio"); c.putheader("Host", f"127.0.0.1:{self.port}")
        c.putheader("Content-Length", str(server.MAX_AUDIO + 1)); c.endheaders()
        self.assertEqual(c.getresponse().status, 413)

    def test_10_state_lists_models_and_hides_secrets(self):
        st, d = self.j("GET", "/api/state")
        self.assertEqual(d["models"], ["stub"]); self.assertFalse(d["images"])
        self.assertNotIn("api_key", json.dumps(d))

    def test_11_llm_down_gives_502(self):
        self.app.report.active[0].base_url = "http://127.0.0.1:1/v1"
        try:
            st, d = self.j("POST", "/api/ask", {"question": "q"})
            self.assertEqual(st, 502); self.assertIn("all models failed", d["error"])
        finally:
            self.app.report.active[0].base_url = self.stub.url


if __name__ == "__main__":
    unittest.main()
