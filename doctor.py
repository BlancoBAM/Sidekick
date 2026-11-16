#!/usr/bin/env python3
"""Preflight check. Usage: python doctor.py [--ping]

Prints which models in models.json are active/skipped (never prints keys).
--ping sends a one-word prompt to each active model to prove it answers.
Exit code 0 = at least one usable model, 1 = none.
"""
import importlib.util
import shutil
import sys

import core


def main() -> int:
    core.load_dotenv()
    s = core.Settings.from_env()
    rep = core.load_chain(s.models_file)
    ok = True
    print(f"models file : {s.models_file}")
    print(f"transcripts : {s.transcripts_dir}")
    print(f"images      : {'ON' if s.images else 'off'}")
    print("\nchain (in order):")
    for i, e in enumerate(rep.active, 1):
        print(f"  {i}. {e.name:<18} {e.model}  @ {e.base_url}  vision={'yes' if e.vision else 'no'}  key={'set' if e.api_key else 'none'}")
    for n, why in rep.skipped:
        print(f"  -  {n:<18} SKIPPED: {why}")
    if not rep.active:
        print("\nERROR: no usable model. Edit .env / models.json (see README 'Models').")
        ok = False

    print("\ntranscription:")
    has_fw = importlib.util.find_spec("faster_whisper") is not None
    print(f"  server (faster-whisper): {'installed' if has_fw else 'NOT installed (only needed for server mode)'}")
    print(f"  ffmpeg: {'found' if shutil.which('ffmpeg') else 'not found (only needed for --monitor)'}")
    print("  browser Whisper: needs WebGPU; check in the browser console: await navigator.gpu?.requestAdapter()")

    if "--ping" in sys.argv and rep.active:
        print("\nping:")
        for e in rep.active:
            try:
                out = core._call_chat(e, [{"role": "user", "content": "Reply with the single word: pong"}], 16, s.request_timeout)
                print(f"  {e.name}: OK -> {out[:40]!r}")
            except core.LLMError as err:
                print(f"  {e.name}: FAIL -> {err}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
