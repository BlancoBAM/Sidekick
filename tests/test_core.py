import os
import tempfile
import unittest
from pathlib import Path

from helpers import StubLLM, write_models
import core


class FakeTranscriber:
    loaded = False
    def transcribe_pcm16(self, pcm, t0):
        return [(t0, "hello world"), (t0 + 2, "[BLANK_AUDIO]")]


def make_app(tmp, entries):
    mf = Path(tmp) / "models.json"
    write_models(mf, entries)
    s = core.Settings(transcripts_dir=Path(tmp) / "t", models_file=mf, images=True)
    app = core.App(s)
    app.transcriber = FakeTranscriber()
    return app


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for k in list(os.environ):
            if k.startswith("T_"): del os.environ[k]

    def test_skips_unset_placeholder_disabled_and_keyless(self):
        os.environ["T_KEY"] = "k"
        mf = Path(self.tmp) / "m.json"
        write_models(mf, [
            {"name": "unset-var", "base_url": "http://x/v1", "model": "${T_MODEL}", "api_key_env": "T_KEY"},
            {"name": "placeholder", "base_url": "http://x/v1", "model": "REPLACE_ME", "api_key_env": "T_KEY"},
            {"name": "off", "base_url": "http://x/v1", "model": "m", "enabled": False},
            {"name": "nokey", "base_url": "http://x/v1", "model": "m", "api_key_env": "T_NOPE"},
            {"name": "optional-key", "base_url": "http://x/v1/", "model": "m", "api_key_env": "T_NOPE", "key_optional": True},
            {"name": "good", "base_url": "http://x/v1", "model": "m", "api_key_env": "T_KEY"},
        ])
        r = core.load_chain(mf)
        self.assertEqual([e.name for e in r.active], ["optional-key", "good"])
        self.assertEqual(r.active[0].base_url, "http://x/v1")  # trailing slash stripped
        reasons = dict(r.skipped)
        self.assertIn("T_MODEL", reasons["unset-var"])
        self.assertIn("REPLACE", reasons["placeholder"])
        self.assertIn("disabled", reasons["off"])
        self.assertIn("T_NOPE", reasons["nokey"])

    def test_missing_and_bad_file(self):
        self.assertTrue(core.load_chain(Path(self.tmp) / "nope.json").skipped)
        bad = Path(self.tmp) / "bad.json"; bad.write_text("{oops")
        self.assertIn("invalid JSON", core.load_chain(bad).skipped[0][1])

    def test_fallback_order_and_secret_not_leaked(self):
        stub = StubLLM({"k1": (500, ""), "k2": (200, "Answer: B")})
        try:
            os.environ.update(T_K1="k1", T_K2="k2")
            app = make_app(self.tmp, [
                {"name": "first", "base_url": stub.url, "model": "m1", "api_key_env": "T_K1"},
                {"name": "second", "base_url": stub.url, "model": "m2", "api_key_env": "T_K2"},
            ])
            app.store.add(None, [(5, "the capital is Paris")])
            r = app.ask("Q1 Capital? A) Rome B) Paris")
            self.assertEqual(r["model"], "second")
            self.assertIn("first", r["fallbacks"][0])
            self.assertNotIn("k1", r["fallbacks"][0].replace("first", ""))  # error text redacts key
            self.assertEqual([q["key"] for q in stub.requests], ["k1", "k2"])
            self.assertTrue(stub.requests[0]["path"].endswith("/chat/completions"))
        finally:
            stub.close()

    def test_all_fail_raises(self):
        stub = StubLLM({"k1": (500, "")})
        try:
            os.environ["T_K1"] = "k1"
            app = make_app(self.tmp, [{"name": "only", "base_url": stub.url, "model": "m", "api_key_env": "T_K1"}])
            with self.assertRaises(core.LLMError) as cm:
                app.ask("anything")
            self.assertIn("only", str(cm.exception))
        finally:
            stub.close()

    def test_unreachable_endpoint_falls_through(self):
        stub = StubLLM({})
        try:
            app = make_app(self.tmp, [
                {"name": "dead", "base_url": "http://127.0.0.1:1/v1", "model": "m", "key_optional": True},
                {"name": "live", "base_url": stub.url, "model": "m", "key_optional": True},
            ])
            self.assertEqual(app.ask("hi")["model"], "live")
        finally:
            stub.close()

    def test_image_goes_only_to_vision_models_and_image_only_skips_text_models(self):
        stub = StubLLM({})
        try:
            app = make_app(self.tmp, [
                {"name": "text", "base_url": stub.url, "model": "t", "key_optional": True, "vision": False},
                {"name": "vis", "base_url": stub.url, "model": "v", "key_optional": True, "vision": True},
            ])
            r = app.ask("", image_b64="QUJD")            # image-only: text model skipped
            self.assertEqual(r["model"], "vis")
            self.assertEqual(len(stub.requests), 1)
            content = stub.requests[0]["body"]["messages"][1]["content"]
            self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64,QUJD")
            stub.requests.clear()
            app.report.active = app.report.active[:1]     # only text model, text question + image
            app.ask("a question", image_b64="QUJD")
            self.assertIsInstance(stub.requests[0]["body"]["messages"][1]["content"], str)  # image stripped
        finally:
            stub.close()

    def test_images_off_ignores_image(self):
        stub = StubLLM({})
        try:
            app = make_app(self.tmp, [{"name": "v", "base_url": stub.url, "model": "v", "key_optional": True, "vision": True}])
            app.s.images = False
            with self.assertRaises(ValueError):
                app.ask("", image_b64="QUJD")
            app.ask("text q", image_b64="QUJD")
            self.assertIsInstance(stub.requests[0]["body"]["messages"][1]["content"], str)
        finally:
            stub.close()


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = core.TranscriptStore(Path(self.tmp))

    def test_blank_and_dupes_filtered(self):
        v = self.st.new_video("A")
        n = self.st.add(v.id, [(0, "hello"), (3, "[BLANK_AUDIO]"), (4, "(silence)"), (5, "hello"), (7, "  world  ")])
        self.assertEqual(n, 2)
        self.assertEqual(self.st.get(v.id).text(), "[00:00] hello\n[00:07] world")

    def test_current_is_latest_even_when_created_in_same_second(self):
        a = self.st.new_video("first")
        b = self.st.new_video("second")
        self.assertEqual(self.st.current().id, b.id)
        self.assertGreater(b.id, a.id)

    def test_persist_and_reload(self):
        v = self.st.new_video("Intro: Part 1")
        self.st.add(v.id, [(65, "one minute in"), (3700, "an hour in")])
        again = core.TranscriptStore(Path(self.tmp))
        got = again.get(v.id)
        self.assertEqual(got.segments, [(65.0, "one minute in"), (3700.0, "an hour in")])
        self.assertEqual(again.new_video().id, v.id + 1)

    def test_out_of_order_segments_sorted_and_unknown_video(self):
        v = self.st.new_video()
        self.st.add(v.id, [(10, "b"), (2, "a")])
        self.assertEqual([t for _, t in self.st.get(v.id).segments], ["a", "b"])
        with self.assertRaises(KeyError):
            self.st.get(99)

    def test_trim_keeps_latest_and_line_boundaries(self):
        text = "\n".join(f"[00:{i:02d}] line {i}" for i in range(50))
        out = core.trim_transcript(text, 120)
        self.assertTrue(out.startswith("[...earlier"))
        self.assertIn("line 49", out)
        self.assertNotIn("line 0\n", out)

    def test_prompt_contains_transcript_question_and_marker(self):
        v = self.st.new_video("Vid")
        self.st.add(v.id, [(0, "mitochondria make ATP")])
        m = core.build_user_message(v, "Q: powerhouse? [x] A", "http://q", 1000, False)
        for s in ("mitochondria make ATP", "[x] A", "http://q", "VIDEO: Vid"):
            self.assertIn(s, m)

    def test_dotenv(self):
        p = Path(self.tmp) / ".env"
        p.write_text("# c\nT_A=1\nexport T_B='two words'\nT_C=3 # trailing\nbad line\nT_D=\n")
        os.environ["T_A"] = "keep"
        core.load_dotenv(p)
        self.assertEqual((os.environ["T_A"], os.environ["T_B"], os.environ["T_C"], os.environ["T_D"]), ("keep", "two words", "3", ""))


class AudioPath(unittest.TestCase):
    def test_add_audio_offsets_and_filters(self):
        tmp = tempfile.mkdtemp()
        app = make_app(tmp, [])
        v = app.store.new_video()
        self.assertEqual(app.add_audio(v.id, b"\0" * 4000, 30.0), 1)
        self.assertEqual(app.store.get(v.id).segments[0][0], 30.0)


if __name__ == "__main__":
    unittest.main()
