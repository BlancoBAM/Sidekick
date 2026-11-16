"""Sidekick core: config, model-router chain, transcript store, prompt building.

Standard library only (faster-whisper is imported lazily, and only when the
server-side transcription path is actually used).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# .env + settings
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, val)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    transcripts_dir: Path = ROOT / "transcripts"
    models_file: Path = ROOT / "models.json"
    whisper_model: str = "base.en"
    whisper_threads: int = 2
    images: bool = False          # send screenshots to vision models
    max_transcript_chars: int = 60000
    max_tokens: int = 700
    request_timeout: int = 60
    browser_whisper_model: str = "onnx-community/whisper-base.en"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("SIDEKICK_HOST", "127.0.0.1"),
            port=_int("SIDEKICK_PORT", 8765),
            transcripts_dir=Path(os.environ.get("SIDEKICK_TRANSCRIPTS") or ROOT / "transcripts").expanduser(),
            models_file=Path(os.environ.get("SIDEKICK_MODELS_FILE") or ROOT / "models.json").expanduser(),
            whisper_model=os.environ.get("SIDEKICK_WHISPER", "base.en"),
            whisper_threads=_int("SIDEKICK_THREADS", 2),
            images=_bool("SIDEKICK_IMAGES", False),
            max_transcript_chars=_int("SIDEKICK_MAX_TRANSCRIPT_CHARS", 60000),
            max_tokens=_int("SIDEKICK_MAX_TOKENS", 700),
            request_timeout=_int("SIDEKICK_TIMEOUT", 60),
            browser_whisper_model=os.environ.get("SIDEKICK_BROWSER_WHISPER", "onnx-community/whisper-base.en"),
        )


# --------------------------------------------------------------------------- #
# Model router chain
# --------------------------------------------------------------------------- #

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(value: str | None) -> tuple[str, list[str]]:
    """Substitute ${VAR} from the environment. Returns (text, missing_vars)."""
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        v = os.environ.get(m.group(1), "")
        if not v:
            missing.append(m.group(1))
        return v

    return _VAR.sub(sub, value or ""), missing


@dataclass
class ModelEntry:
    name: str
    base_url: str
    model: str
    api_key: str = ""
    vision: bool = False
    headers: dict = field(default_factory=dict)


@dataclass
class ChainReport:
    active: list[ModelEntry]
    skipped: list[tuple[str, str]]  # (name, reason) - never contains secrets


def load_chain(models_file: Path) -> ChainReport:
    """Read models.json, drop unusable entries, and say why."""
    if not models_file.is_file():
        return ChainReport([], [(str(models_file), "file not found - copy models.json.example to models.json")])
    try:
        raw = json.loads(models_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return ChainReport([], [(str(models_file), f"invalid JSON: {e}")])
    if isinstance(raw, dict):
        raw = raw.get("models", [])
    active: list[ModelEntry] = []
    skipped: list[tuple[str, str]] = []
    for i, item in enumerate(raw):
        name = str(item.get("name") or f"entry{i}")
        if item.get("enabled") is False:
            skipped.append((name, "disabled in models.json"))
            continue
        base, miss_b = expand(item.get("base_url"))
        model, miss_m = expand(item.get("model"))
        missing = miss_b + miss_m
        if missing:
            skipped.append((name, "env var not set: " + ", ".join(sorted(set(missing)))))
            continue
        if not base or not model:
            skipped.append((name, "base_url or model empty"))
            continue
        if "REPLACE" in model or "REPLACE" in base:
            skipped.append((name, "placeholder value (REPLACE_...) not filled in"))
            continue
        key = ""
        key_env = item.get("api_key_env")
        if key_env:
            key = os.environ.get(key_env, "")
            if not key and not item.get("key_optional"):
                skipped.append((name, f"env var not set: {key_env}"))
                continue
        headers = {}
        for hk, hv in (item.get("headers") or {}).items():
            headers[hk], _ = expand(hv)
        active.append(ModelEntry(name, base.rstrip("/"), model, key, bool(item.get("vision")), headers))
    return ChainReport(active, skipped)


class LLMError(Exception):
    pass


def _call_chat(entry: ModelEntry, messages: list[dict], max_tokens: int, timeout: int) -> str:
    url = entry.base_url + "/chat/completions"
    body = json.dumps({
        "model": entry.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "sidekick/1.0"}
    if entry.api_key:
        headers["Authorization"] = f"Bearer {entry.api_key}"
    headers.update(entry.headers)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200].replace(entry.api_key or "\0", "***")
        raise LLMError(f"HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"connection failed: {getattr(e, 'reason', e)}") from None
    except json.JSONDecodeError:
        raise LLMError("response was not JSON") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("unexpected response shape") from None
    if isinstance(content, list):  # some gateways return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = (content or "").strip()
    if not content:
        raise LLMError("empty answer")
    return content


def run_chain(chain: list[ModelEntry], messages_text: list[dict], image_b64: str | None,
              max_tokens: int, timeout: int) -> tuple[str, str, list[str]]:
    """Try each model in order. Returns (answer, model_name, errors_so_far).

    `messages_text` is a [system, user] pair where user content is a string.
    If image_b64 is given, vision entries receive it; text-only entries are
    skipped when there is nothing but the image to go on (caller decides via
    the text in the user message) - here we simply attach the image only for
    vision entries and let text-only entries answer from the text.
    """
    errors: list[str] = []
    if not chain:
        raise LLMError("no usable models in the chain - run `python doctor.py`")
    for entry in chain:
        msgs = [dict(messages_text[0])]
        user_text = messages_text[1]["content"]
        if image_b64 and entry.vision:
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
            ]})
        else:
            msgs.append({"role": "user", "content": user_text})
        try:
            return _call_chat(entry, msgs, max_tokens, timeout), entry.name, errors
        except LLMError as e:
            errors.append(f"{entry.name}: {e}")
    raise LLMError("all models failed - " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# Transcript store
# --------------------------------------------------------------------------- #

_BLANK = re.compile(r"^\s*[\[\(\*]?\s*(blank[_ ]audio|silence|music|applause|inaudible|no speech)[^\]\)]*[\]\)\*]?\s*$", re.I)
_LINE = re.compile(r"^\[(\d+):(\d{2})(?::(\d{2}))?\]\s*(.*)$")
_FILE = re.compile(r"^(\d{3})-(.*)\.txt$")


def clean_text(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t or _BLANK.match(t):
        return ""
    return t


def fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


@dataclass
class Video:
    id: int
    title: str
    path: Path
    started: float
    segments: list[tuple[float, str]] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(f"[{fmt_ts(t)}] {x}" for t, x in self.segments)

    @property
    def words(self) -> int:
        return sum(len(x.split()) for _, x in self.segments)


class TranscriptStore:
    """One Video per 'Start new video'. Ordering is by a monotonically
    increasing id (not wall-clock time), so 'current video' is always the last
    one created, even if two are created in the same second."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.videos: dict[int, Video] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        for p in sorted(self.dir.glob("*.txt")):
            m = _FILE.match(p.name)
            if not m:
                continue
            vid = int(m.group(1))
            v = Video(vid, m.group(2).replace("_", " "), p, p.stat().st_mtime)
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                lm = _LINE.match(line)
                if not lm:
                    continue
                a, b, c, txt = lm.groups()
                secs = int(a) * 60 + int(b) if c is None else int(a) * 3600 + int(b) * 60 + int(c)
                v.segments.append((float(secs), txt))
            self.videos[vid] = v

    def new_video(self, title: str = "") -> Video:
        with self._lock:
            vid = (max(self.videos) + 1) if self.videos else 1
            title = re.sub(r"\s+", " ", title or "").strip() or f"Video {vid}"
            slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:50] or f"video_{vid}"
            path = self.dir / f"{vid:03d}-{slug}.txt"
            path.write_text("", encoding="utf-8")
            v = Video(vid, title, path, time.time())
            self.videos[vid] = v
            return v

    def current(self) -> Video:
        with self._lock:
            if not self.videos:
                return self.new_video()
            return self.videos[max(self.videos)]

    def get(self, vid: int | None) -> Video:
        with self._lock:
            if vid is None:
                return self.current()
            if vid not in self.videos:
                raise KeyError(f"no such video: {vid}")
            return self.videos[vid]

    def add(self, vid: int | None, segments: list[tuple[float, str]]) -> int:
        """Append (start_seconds, text) segments. Returns number actually stored."""
        added = 0
        with self._lock:
            v = self.get(vid)
            lines = []
            for start, raw in sorted(segments, key=lambda s: s[0]):
                txt = clean_text(raw)
                if not txt:
                    continue
                if v.segments and v.segments[-1][1] == txt:  # exact repeat (overlap/hallucination loop)
                    continue
                v.segments.append((float(start), txt))
                lines.append(f"[{fmt_ts(start)}] {txt}\n")
                added += 1
            if lines:
                with v.path.open("a", encoding="utf-8") as f:
                    f.writelines(lines)
        return added

    def summary(self) -> list[dict]:
        with self._lock:
            return [{"id": v.id, "title": v.title, "words": v.words, "file": v.path.name}
                    for _, v in sorted(self.videos.items())]


# --------------------------------------------------------------------------- #
# Server-side Whisper (lazy)
# --------------------------------------------------------------------------- #

class Transcriber:
    """faster-whisper on CPU, loaded on first use so browser mode costs no RAM."""

    def __init__(self, size: str, threads: int):
        self.size, self.threads = size, threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError("faster-whisper is not installed (pip install -r requirements.txt)") from e
            self._model = WhisperModel(self.size, device="cpu", compute_type="int8", cpu_threads=self.threads)
        return self._model

    def transcribe_pcm16(self, pcm: bytes, t0: float) -> list[tuple[float, str]]:
        import numpy as np  # shipped with faster-whisper
        if len(pcm) < 3200:  # < 0.1 s
            return []
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype("float32") / 32768.0
        if float(np.sqrt(np.mean(audio ** 2))) < 0.003:  # silence
            return []
        with self._lock:
            model = self._ensure()
            segs, _ = model.transcribe(audio, language="en", vad_filter=True,
                                       condition_on_previous_text=False, beam_size=1)
            return [(t0 + s.start, s.text) for s in segs]


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You help a student answer quiz questions about a course video they just watched. "
    "You are given the video's timestamped transcript and the quiz question (often multiple choice). "
    "Rules: (1) Base the answer on the transcript. (2) If the transcript does not cover the point, say so, "
    "give your best general-knowledge answer, and mark confidence LOW. (3) The transcript is machine-generated "
    "and may mishear names or numbers; allow for that. (4) Be brief.\n"
    "Reply in exactly this format:\n"
    "Answer: <option letter(s)/number(s) and the option text, or the short answer>\n"
    "Why: <one sentence, citing a transcript timestamp like [02:15] when possible>\n"
    "Confidence: HIGH | MEDIUM | LOW"
)


def trim_transcript(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = text[-limit:]
    nl = keep.find("\n")
    return "[...earlier part of transcript omitted...]\n" + (keep[nl + 1:] if nl != -1 else keep)


def build_user_message(video: Video, question: str, page_url: str, limit: int, has_image: bool) -> str:
    transcript = trim_transcript(video.text(), limit) or "(no transcript captured for this video)"
    parts = [f"VIDEO: {video.title}", "TRANSCRIPT:", transcript, ""]
    if page_url:
        parts.append(f"QUIZ PAGE URL: {page_url}")
    if question.strip():
        parts += ["QUIZ PAGE TEXT (read from the page; ticked options are marked [x]):", question.strip()]
    if has_image:
        parts.append("A screenshot of the quiz page is attached." if question.strip()
                     else "The quiz question is in the attached screenshot.")
    parts.append("\nAnswer the quiz question(s) now.")
    return "\n".join(parts)


class App:
    """Everything the HTTP layer needs, in one object (also what the tests drive)."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings.from_env()
        self.store = TranscriptStore(self.s.transcripts_dir)
        self.transcriber = Transcriber(self.s.whisper_model, self.s.whisper_threads)
        self.report = load_chain(self.s.models_file)
        self.last_audio_error = ""

    def reload_chain(self) -> ChainReport:
        self.report = load_chain(self.s.models_file)
        return self.report

    def state(self) -> dict:
        cur = self.store.current() if self.store.videos else None
        return {
            "videos": self.store.summary(),
            "current": cur.id if cur else None,
            "images": self.s.images,
            "server_whisper": {"model": self.s.whisper_model, "loaded": self.transcriber.loaded},
            "browser_whisper_model": self.s.browser_whisper_model,
            "models": [e.name for e in self.report.active],
            "skipped": [{"name": n, "reason": r} for n, r in self.report.skipped],
            "last_audio_error": self.last_audio_error,
        }

    def add_audio(self, vid: int | None, pcm: bytes, t0: float) -> int:
        try:
            segs = self.transcriber.transcribe_pcm16(pcm, t0)
        except Exception as e:  # surfaced in /api/state, not fatal
            self.last_audio_error = str(e)[:300]
            raise
        self.last_audio_error = ""
        return self.store.add(vid, segs)

    def ask(self, question: str, page_url: str = "", image_b64: str | None = None,
            vid: int | None = None, model: str | None = None) -> dict:
        question = (question or "").strip()
        use_image = bool(image_b64) and self.s.images
        if not question and not use_image:
            raise ValueError("nothing to ask: no page text and no screenshot "
                             "(screenshots are off unless SIDEKICK_IMAGES=1)")
        video = self.store.get(vid)
        user = build_user_message(video, question, page_url, self.s.max_transcript_chars, use_image)
        chain = self.report.active
        if model:
            chain = [e for e in chain if e.name == model]
            if not chain:
                raise LLMError(f"model not in chain: {model}")
        if not question:  # image-only: text-only models cannot help
            chain = [e for e in chain if e.vision]
        answer, model, errs = run_chain(
            chain, [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            image_b64 if use_image else None, self.s.max_tokens, self.s.request_timeout)
        return {"answer": answer, "model": model, "video": video.id, "fallbacks": errs,
                "transcript_words": video.words}
