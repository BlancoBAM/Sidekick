#!/usr/bin/env python3
"""Optional: transcribe SYSTEM audio (any app, incl. Bluetooth headphones) on Linux.

Records the default output's monitor via ffmpeg/PulseAudio (PipeWire-Pulse works) and
posts 10 s chunks to the running Sidekick server (server-side Whisper).
Usage: python scripts/monitor.py [--source NAME.monitor]
Find sources: pactl list short sources
"""
import json
import subprocess
import sys
import time
import urllib.request

CHUNK_SEC = 10
RATE = 16000
SERVER = "http://127.0.0.1:8765"


def post(path, data, ctype):
    req = urllib.request.Request(SERVER + path, data=data, headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    source = "@DEFAULT_MONITOR@"
    if "--source" in sys.argv:
        source = sys.argv[sys.argv.index("--source") + 1]
    state = json.loads(urllib.request.urlopen(SERVER + "/api/state", timeout=10).read())
    vid = state["current"] or post("/api/video/new", b"{}", "application/json")["id"]
    cmd = ["ffmpeg", "-loglevel", "error", "-f", "pulse", "-i", source, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    need = CHUNK_SEC * RATE * 2
    t0 = 0.0
    print(f"capturing {source} into video #{vid}. Ctrl-C to stop. (Use 'Start new video' in the web page to split videos.)")
    try:
        while True:
            data = b""
            while len(data) < need:
                piece = p.stdout.read(need - len(data))
                if not piece:
                    print("ffmpeg ended"); return
                data += piece
            cur = json.loads(urllib.request.urlopen(SERVER + "/api/state", timeout=10).read())["current"]
            if cur != vid:
                vid, t0 = cur, 0.0
            try:
                post(f"/api/audio?video={vid}&t0={t0:.2f}", data, "application/octet-stream")
            except Exception as e:
                print("post failed:", e)
            t0 += CHUNK_SEC
    except KeyboardInterrupt:
        p.terminate()


if __name__ == "__main__":
    main()
