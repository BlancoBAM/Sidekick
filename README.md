# Sidekick

Local helper for course videos + quizzes. While you watch a video it **transcribes the audio** into a per-video text file; at the quiz you ask it a question and it answers **from that transcript** using a model from your router chain (free tiers / OmniRoute / local).

---

<p align="center">
  <a href="#quick-start"><img src="https://img.shields.io/badge/Quick%20Start-Getting%20Started-blue?style=flat-square" alt="Quick Start"></a>
  <a href="#installation"><img src="https://img.shields.io/badge/Install-One%20Command-4f46e5?style=flat-square" alt="Install"></a>
  <a href="#usage"><img src="https://img.shields.io/badge/Usage-Course%20Flow-10b981?style=flat-square" alt="Usage"></a>
  <a href="#configuration"><img src="https://img.shields.io/badge/Configuration-.env%2Fmodels.json-8b5cf6?style=flat-square" alt="Config"></a>
  <a href="#security"><img src="https://img.shields.io/badge/Security-Loopback%20Only-ef4444?style=flat-square" alt="Security"></a>
  <a href="#planned-features"><img src="https://img.shields.io/badge/Roadmap-Rust%20Rewrite-f59e0b?style=flat-square" alt="Roadmap"></a>
</p>

---

## What it does

You watch a course video. Sidekick **listens** and writes down what the video says, into one text file per video. When you hit a quiz question you're stuck on, you paste it into Sidekick (or click the browser extension button on the quiz page). Sidekick reads its transcript notes, asks an AI helper, and gives you an **answer, a short reason why, and how sure it is**.

You still watch the videos and answer the questions yourself. Sidekick only helps when you ask — it's a study aid, not an autopilot.

## Directory layout

```
sidekick/
├── server.py            HTTP server (stdlib only)        → python server.py
├── core.py              config, model chain, transcript store, prompts, Whisper
├── doctor.py            preflight: active/skipped models, --ping tests them
├── run.sh               launcher (venv, deps, doctor, server)
├── requirements.txt     faster-whisper (server-side transcription only)
├── .env.example         all settings + key names (copy to .env)
├── models.json.example  the router chain (copy to models.json)
├── static/
│   ├── index.html       capture page (tab audio → transcription, Ask box)
│   └── whisper-worker.js  Whisper on WebGPU via Transformers.js (module worker)
├── extension/           browser extension: reads the quiz page text, shows answer on page
├── scripts/
│   ├── monitor.py       system-audio capture (any app, Bluetooth OK) via ffmpeg/PipeWire
│   └── sidekick.service optional systemd --user unit
├── tests/               run_all.sh — no network/keys/models needed
└── transcripts/         output: 001-<title>.txt, 002-... ("[mm:ss] text" per line)
```

## Quick start

```bash
cd sidekick
./run.sh                  # first run creates .env + models.json, runs doctor, starts server
# edit .env (see "Configuration"), run again; open http://127.0.0.1:8765
```

Python 3.10+. The server, doctor and tests use only the standard library. `faster-whisper` is installed by `run.sh` but only matters for server-side transcription.

### Step-by-step (ELI10)

1. **Start sharing.** Click **Start sharing**, choose the **Tab** with your course video, tick **Also share tab audio** (not Window or Entire screen). Wait for `browser whisper: ready` (first run downloads ~200 MB).
2. **Start a new video.** Each time a new video begins, click **Next video** (optional title). Transcription runs continuously until you click **Stop**.
3. **Ask a question.** Paste your quiz question (+ options) into **Ask** and click **Analyze question**. You'll get `Answer / Why [timestamp] / Confidence HIGH|MEDIUM|LOW`. Treat **LOW** seriously near your pass mark — it means the video probably didn't cover it.

### One-time setup

1. Open `.env` and fill in **one** AI service you already have a key for (OmniRoute, OpenRouter, Gemini, Groq, …). You only need ONE to begin.
   - the key, e.g. `OPENROUTER_API_KEY=paste-your-key-here`
   - the model name, e.g. `OPENROUTER_MODEL=paste-the-exact-model-name-here`
   (Copy the model name **exactly** from that service's model list.)
2. Save. No spaces around `=`, no quotes.
3. Test: `python3 doctor.py --ping`. Look for your service with **OK** next to it.

## Configuration

### `.env` (copy from `.env.example`)

Keys live only in `.env` (git-ignored) and are redacted from errors. Never commit or share `.env`.

| Var | Default | Meaning |
|-----|---------|---------|
| `SIDEKICK_HOST` / `SIDEKICK_PORT` | 127.0.0.1 / 8765 | Keep on loopback. |
| `SIDEKICK_TRANSCRIPTS` | `./transcripts` | Output dir (can be an rclone-synced path). |
| `SIDEKICK_IMAGES` | 0 | 1 = send screenshots to vision models. |
| `SIDEKICK_WHISPER` | base.en | Server Whisper: tiny.en / base.en / small.en. |
| `SIDEKICK_THREADS` | 2 | CPU threads for server Whisper. |
| `SIDEKICK_MAX_TRANSCRIPT_CHARS` | 60000 | Older transcript trimmed beyond this. |
| `SIDEKICK_MAX_TOKENS` / `SIDEKICK_TIMEOUT` | 700 / 60 | Per-model answer length / seconds. |

### `models.json` (copy from `models.json.example`)

An **ordered chain**; the first entry that answers wins, errors fall through to the next. Any OpenAI-compatible `/chat/completions` endpoint works. Entries are skipped automatically if a `${VAR}` or key is empty, the model is a placeholder, or `"enabled": false` — `python doctor.py` explains why.

1. **OmniRoute** (recommended first slot): `npm i -g omniroute`, run `omniroute`, serves `http://localhost:20128/v1`. Connect your providers/credits in its dashboard, optionally define a **combo**, set `OMNIROUTE_MODEL` to the exact name shown.
2. **Direct free tiers** (OpenRouter `:free`, Gemini, Groq, Cerebras, Mistral…): add the key, copy the model ID exactly from the provider's list into the matching `*_MODEL` var. Set `"vision": true` only for image-capable models (vision matters only for the paste/screenshot method).
3. **Local fallback:** Ollama (`ollama pull <model>`; set `OLLAMA_MODEL`). On 8 GB RAM use ≤ ~3 GB models, text only.
4. **MiniCPM-o API:** included but disabled; set `MINICPM_API_KEY` and enable to A/B it.

To add a provider:
```json
{"name":"my-provider","base_url":"https://host/v1","model":"${MY_MODEL}","api_key_env":"MY_KEY","vision":false}
```

Model IDs are yours to supply — verify them against each provider's current list. Free tiers have rate limits, and some providers may log or train on prompts: your transcripts and quiz text go to whichever provider answers.

### Browser extension (quiz-page helper)

Chrome/Chromium/BrowserOS: open `chrome://extensions`, enable **Developer mode**, **Load unpacked**, select `extension/`. It acts only when you click/press **Alt+Shift+S**. It requests all-sites access so it can read cross-origin quiz iframes — to narrow it, replace `<all_urls>` in `extension/manifest.json` with your course domain and reload. It cannot read closed shadow DOM or text inside images.

## Methods

| Method | Use when | Where |
|--------|----------|-------|
| **A. Browser tab audio + Whisper on WebGPU** (default) | WebGPU works (`await navigator.gpu?.requestAdapter()` non-null). Offloads transcription from your CPU to the iGPU; server holds no model. | `static/index.html` mode "browser" + `whisper-worker.js` |
| **B. Browser tab audio + server Whisper (CPU)** | WebGPU missing/slow. Automatic fallback from A, or pick "server". | mode "server" → `/api/audio` → `core.Transcriber` |
| **C. System audio monitor** | Audio from a non-browser app, or tab capture unavailable. | `./run.sh --monitor` or `python scripts/monitor.py [--source X.monitor]` |
| **D. Extension text grabber** (preferred for quizzes) | Quiz is normal HTML. Exact text, no OCR, no vision cost. | `extension/` |
| **E. Paste / screenshot** | Quiz is a canvas or image. Screenshot only if `SIDEKICK_IMAGES=1` and a vision model is in the chain. | Ask box |

Fallbacks: A→B is automatic (WebGPU error, or browser queue > 12 chunks, or 2 consecutive failures). Failed browser chunks are re-sent to the server with original timestamps; silent chunks are skipped.

## Security model

Server binds to loopback; rejects requests whose `Host` isn't loopback (DNS-rebinding); CORS only for the extension and its own page; POSTs must be `application/json` (or raw audio); size-capped bodies; static files can't escape `static/`; API keys live only in `.env` (git-ignored). There is no login: anyone on this machine can use the API. Don't expose the port (tailscale/docker port-forward) without adding auth.

## Tests

`tests/run_all.sh` — 29 Python tests (stub LLM server, real HTTP server, CLI), extension tests (fake DOM), worker tests (mocked Transformers.js), syntax checks. Not covered (need your machine): real WebGPU, real Whisper downloads, real faster-whisper, real PipeWire capture, a real provider, real Chrome/BrowserOS.

## Planned features

- **Rust re-write.** Intentionally Python for now: the heavy work is native (CTranslate2/ONNX/ffmpeg); a Rust port adds a toolchain for no immediate speedup. The server is small and stdlib-only, so a later port is straightforward and remains on the roadmap.
- Persistent per-course libraries with search across transcripts.
- Vision question answering from live frames (MiniCPM-o / local VLM).
- Offline-first mode with fully local STT + LLM.

## Upstream credits

Sidekick stands on the shoulders of these open-source projects:

- **Transformers.js** — Whisper on WebGPU in the browser (`static/whisper-worker.js`) — https://github.com/huggingface/transformers.js
- **faster-whisper** — CTranslate2 Whisper for server-side CPU transcription (`requirements.txt`) — https://github.com/guillaumekln/faster-whisper
- **Ollama** — local LLM server (`OLLAMA_MODEL` backend) — https://github.com/ollama/ollama
- **Lemonade** — local LLM server (`lemonade` backend) — https://github.com/lemonade-sdk/lemonade
- **OmniRoute** — self-hosted multi-provider gateway (`OMNIROUTE_*` chain entry) — https://github.com/kiloplt/omniroute
- **MiniCPM-o** — vision-language model API — https://github.com/OpenBMB/MiniCPM-o
- **PipeWire / PulseAudio** — system-audio capture (`scripts/monitor.py`) — https://pipewire.org / https://www.freedesktop.org/wiki/Software/PulseAudio/

## Lilith Linux / the demoness credit

Every feature, package, and setting in this project was inspired by — and is the sole consideration of — my other half, the demoness who inspired **Lilith Linux**, which (hopefully) will be officially released before 2027.

> It's extra…lit (⬅️ ⬅️ obligatory infernal/demonic/satanic/left-hand-path-following/mystical/witchy/sinful reference/play on words…lit? Like a flame? Like Hell?). Follow/Star for more coding/comedy.

Check out the progress we've made so far and subscribe to notifications for the repos related to it on my profile.

---

<sub>Maintained as a single canonical source. Earlier v1–v4 zips are superseded.</sub>
# Sidekick
