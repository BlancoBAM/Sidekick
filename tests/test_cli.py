import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from helpers import StubLLM, write_models

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class CLI(unittest.TestCase):
    def env(self, tmp, models):
        mf = Path(tmp) / "models.json"; write_models(mf, models)
        e = {k: v for k, v in os.environ.items() if not k.startswith(("SIDEKICK_", "OMNIROUTE_", "T_"))}
        e.update(SIDEKICK_MODELS_FILE=str(mf), SIDEKICK_TRANSCRIPTS=str(Path(tmp) / "t"), SIDEKICK_PORT=str(free_port()), PYTHONDONTWRITEBYTECODE="1")
        return e

    def test_doctor_ping_ok_and_no_secret_in_output(self):
        stub = StubLLM({"sk-SECRET-123": (200, "pong")})
        try:
            tmp = tempfile.mkdtemp()
            e = self.env(tmp, [{"name": "a", "base_url": stub.url, "model": "m", "api_key_env": "T_K"},
                               {"name": "b", "base_url": stub.url, "model": "${T_MISSING}"}])
            e["T_K"] = "sk-SECRET-123"
            p = subprocess.run([sys.executable, str(ROOT / "doctor.py"), "--ping"], env=e, capture_output=True, text=True, cwd=tmp)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("a: OK", p.stdout); self.assertIn("T_MISSING", p.stdout)
            self.assertNotIn("sk-SECRET-123", p.stdout + p.stderr)
        finally:
            stub.close()

    def test_doctor_fails_when_no_models(self):
        tmp = tempfile.mkdtemp()
        p = subprocess.run([sys.executable, str(ROOT / "doctor.py")], env=self.env(tmp, []), capture_output=True, text=True, cwd=tmp)
        self.assertEqual(p.returncode, 1); self.assertIn("no usable model", p.stdout)

    def test_server_boots_and_serves(self):
        tmp = tempfile.mkdtemp(); e = self.env(tmp, [])
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp)
        try:
            url = f"http://127.0.0.1:{e['SIDEKICK_PORT']}"
            for _ in range(50):
                try:
                    if json.load(urllib.request.urlopen(url + "/api/health", timeout=1))["ok"]: break
                except Exception: time.sleep(0.1)
            else:
                self.fail("server did not start")
            st = json.load(urllib.request.urlopen(url + "/api/state", timeout=2))
            self.assertEqual(st["models"], [])
        finally:
            proc.terminate(); out = proc.communicate(timeout=5)[0]
        self.assertIn("WARNING: no usable model", out)


if __name__ == "__main__":
    unittest.main()
