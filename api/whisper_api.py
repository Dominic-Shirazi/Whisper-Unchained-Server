# This file is called by Uvicorn (or LAUNCHER.bat / Start-WhisperAPI.ps1) to host the Faster Whisper API.
# It receives audio uploads at /transcribe, runs transcription using Faster-Whisper on CUDA/CPU,
# handles voice commands (like 'prompt AI' editing and mean-mode roasts via Ollama), logs transcripts,
# and coordinates audio archiving and background noise reduction.
# Inputs: Audio files via multipart/form-data POST to /transcribe (and related endpoints).
# Outputs: JSON response containing transcribed/edited text, confidence scores, and timing metrics.
#
# Reason for edit: Updated to automatically preload and warm up the Ollama edit model
# (qwen2.5:7b-instruct-q4_K_M) on startup in a background thread via api/ollama_warmup.py

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import List

# Must run before faster_whisper/ctranslate2 load: lets them find CUDA DLLs
# the installer put in the venv (see api/cuda_paths.py).
try:
    from api.cuda_paths import add_nvidia_dll_dirs
except ImportError:
    from cuda_paths import add_nvidia_dll_dirs
add_nvidia_dll_dirs()

from faster_whisper import WhisperModel  # noqa: E402
import hmac
import os
import threading
import shutil
import subprocess
import uuid
import uvicorn
from dotenv import load_dotenv
import re
import requests
import json
import math
import time
import traceback

try:
    from api.ollama_warmup import start_async_ollama_warmup
    from api.fillers import remove_fillers
    from api import cleanup
    from api import edit_commands
except ImportError:
    from ollama_warmup import start_async_ollama_warmup
    from fillers import remove_fillers
    import cleanup
    import edit_commands


# Load configuration from .env file
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# Configuration
MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "large-v3-turbo")
DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
# Mean-mode (roast/tiktok/disses) runs on this box against the local Ollama.
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://127.0.0.1:11434/api/generate")
# Base 'prompt ai' editing can run on a separate machine (e.g. AI_worker2).
# Falls back to the local Ollama if OLLAMA_EDIT_API_URL is not set.
OLLAMA_EDIT_API_URL = os.getenv("OLLAMA_EDIT_API_URL", OLLAMA_API_URL)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")

# Optional OpenAI-compatible edit backend (e.g. llama.cpp server on ai-worker3).
# When EDIT_LLM_BASE_URL is set, 'prompt ai' edits go to <base>/chat/completions
# instead of Ollama, and the local Ollama warmup is skipped.
EDIT_LLM_BASE_URL = os.getenv("EDIT_LLM_BASE_URL", "").strip().rstrip("/")
EDIT_LLM_MODEL = os.getenv("EDIT_LLM_MODEL", "").strip()
EDIT_LLM_API_KEY = os.getenv("EDIT_LLM_API_KEY", "").strip()
# Reasoning models think by default (~200 tokens, several seconds per edit).
# true sends chat_template_kwargs.enable_thinking=false so it answers directly.
EDIT_LLM_NO_THINK = os.getenv("EDIT_LLM_NO_THINK", "true").strip().lower() in ("1", "true", "yes", "on")
EDIT_LLM_TIMEOUT = float(os.getenv("EDIT_LLM_TIMEOUT_SECONDS", "60"))
# EDIT_BACKEND (Settings > AI Editing) picks explicitly, so the OpenAI-compatible
# URL can stay filled in while Ollama is in use. Older .env files without it:
# a set EDIT_LLM_BASE_URL means OpenAI-compatible, as before.
_EDIT_BACKEND = os.getenv("EDIT_BACKEND", "").strip().lower()
USE_OPENAI_EDIT = bool(EDIT_LLM_BASE_URL) and _EDIT_BACKEND != "ollama"
if _EDIT_BACKEND == "openai" and not EDIT_LLM_BASE_URL:
    print("[API] EDIT_BACKEND=openai but EDIT_LLM_BASE_URL is empty; using Ollama for edits.")
EDIT_MODEL_NAME = EDIT_LLM_MODEL if USE_OPENAI_EDIT else OLLAMA_MODEL
EDIT_ENDPOINT = f"{EDIT_LLM_BASE_URL}/chat/completions" if USE_OPENAI_EDIT else OLLAMA_EDIT_API_URL


def _parse_keep_alive(raw: str):
    """Ollama's keep_alive accepts a number of seconds or a duration string
    ("30m"). A bare "-1" must be sent as a NUMBER; the string "-1" is not a valid
    Go duration and Ollama would reject it."""
    raw = (raw or "").strip()
    try:
        return int(raw)
    except ValueError:
        return raw


# How long Ollama keeps the edit model resident after a request. -1 means "until
# unloaded manually" (`ollama stop <model>`), so only the first request after a
# load pays for it; every later one goes straight to generating. Measured on this
# box with qwen2.5:7b-instruct-q4_K_M: 5.5s cold vs 0.85s warm, ~7.5 GB resident.
OLLAMA_EDIT_KEEP_ALIVE = _parse_keep_alive(os.getenv("OLLAMA_EDIT_KEEP_ALIVE", "-1"))
# Preload + pin the Ollama edit model on every API start (boot and each
# RESTART_INTERVAL_HOURS restart). false = let it load on first use instead.
OLLAMA_EDIT_PRELOAD = os.getenv("OLLAMA_EDIT_PRELOAD", "true").strip().lower() in ("1", "true", "yes", "on")
OLLAMA_ROAST_MODEL = os.getenv("OLLAMA_ROAST_MODEL", "huihui_ai/qwen2.5-abliterate:7b")
# Mean mode's model can live anywhere (Settings > Mean Mode):
#   ollama  OLLAMA_API_URL + OLLAMA_ROAST_MODEL (default: this PC's Ollama)
#   edit    whatever AI Editing uses (e.g. a llama.cpp server)
#   openai  its own OpenAI-compatible server: MEAN_LLM_BASE_URL / _MODEL / _API_KEY
MEAN_BACKEND = os.getenv("MEAN_BACKEND", "ollama").strip().lower()
MEAN_LLM_BASE_URL = os.getenv("MEAN_LLM_BASE_URL", "").strip().rstrip("/")
MEAN_LLM_MODEL = os.getenv("MEAN_LLM_MODEL", "").strip()
MEAN_LLM_API_KEY = os.getenv("MEAN_LLM_API_KEY", "").strip()
MEAN_LLM_NO_THINK = os.getenv("MEAN_LLM_NO_THINK", "true").strip().lower() in ("1", "true", "yes", "on")
# float16 needs a GPU; CTranslate2 refuses it on CPU, so CPU defaults to int8.
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "").strip() or (
    "int8" if DEVICE == "cpu" else "float16")
# Shared model folder (see client/model_catalog.py). Unset = the HF cache of
# whoever runs the server (for the service, LocalSystem's, which the Settings
# model picker can't see). Relative paths are under the repo.
_models_dir = os.getenv("WHISPER_MODELS_DIR", "").strip()
MODELS_DIR = (_models_dir if os.path.isabs(_models_dir) else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _models_dir)) if _models_dir else None
TEMP_DIR = os.path.dirname(__file__)
# Direct launches (python api/whisper_api.py) use this; the service gets its
# port from its own command line, which Settings > Save & Restart API rewrites.
API_PORT = int(os.getenv("WHISPER_API_PORT", "5000") or 5000)

# Optional API key (contract: WhisperRemote contracts/01-transcription-api.md §4).
# Empty = no auth, which keeps today's LAN/Tailscale setup working unchanged.
# When set, every route except GET /health needs "Authorization: Bearer <key>".
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "").strip()
# The key can stay saved while not required (Settings > Phone switch), so devices
# that have it keep working and other programs can use the server without one.
API_KEY_REQUIRED = os.getenv("WHISPER_API_KEY_REQUIRED", "true").strip().lower() \
    not in ("0", "false", "no", "off")


def _auth_on() -> bool:
    return bool(WHISPER_API_KEY) and API_KEY_REQUIRED
# OpenAI's own limit is 25 MB; we allow more for long file transcriptions.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
API_VERSION = 1
# Strip standalone "um"/"uh" sounds from dictation (api/fillers.py). Off by
# default: a filter must never risk deleting real words. Every removal is
# logged as a FILL line in transcripts.log so it can be audited.
REMOVE_FILLERS = os.getenv("REMOVE_FILLERS", "false").strip().lower() in ("1", "true", "yes", "on")
# Dictation cleanup (api/cleanup.py, contract 02-cleanup.md): one LLM pass on
# the edit backend. Off unless CLEANUP_ALWAYS=true or a request asks for
# processing=clean. CLEANUP_* style values are this server's defaults; a request
# can override each one.
CLEANUP_ALWAYS = os.getenv("CLEANUP_ALWAYS", "false").strip().lower() in ("1", "true", "yes", "on")
CLEANUP_STYLE, _cleanup_err = cleanup.normalize_style({
    "punctuation": os.getenv("CLEANUP_PUNCTUATION", ""),
    "rewrite": os.getenv("CLEANUP_REWRITE", ""),
    "shorten": os.getenv("CLEANUP_SHORTEN", ""),
    "tone": os.getenv("CLEANUP_TONE", ""),
    "emoji": "on" if os.getenv("CLEANUP_EMOJI", "false").strip().lower() in ("1", "true", "yes", "on") else "off",
})
if _cleanup_err:
    print(f"[API] .env cleanup setting ignored ({_cleanup_err}); using defaults.")
    CLEANUP_STYLE = dict(cleanup.DEFAULTS)

# Per-segment confidence gates. faster-whisper already returns these scores on
# every Segment; we just drop the ones the model itself distrusts. Values mirror
# OpenAI Whisper's own silence/degenerate-output defaults. Tunable via .env.
# The silence gate deliberately requires BOTH a high no-speech probability AND a
# low decode confidence, so a genuine quiet word (which carries real audio) is
# never cut -- only model-invented filler over true silence.
NO_SPEECH_THRESHOLD = float(os.getenv("NO_SPEECH_THRESHOLD", 0.6))
LOGPROB_THRESHOLD = float(os.getenv("LOGPROB_THRESHOLD", -1.0))
COMPRESSION_RATIO_THRESHOLD = float(os.getenv("COMPRESSION_RATIO_THRESHOLD", 2.4))
# Log every raw+final transcript to transcripts.log to build a real corpus of
# your own speech (trigger-catch coverage, hallucinations, edit results).
# Off unless the user opts in (Settings > Audio Corpus): it keeps every dictation.
LOG_TRANSCRIPTS = os.getenv("LOG_TRANSCRIPTS", "false").lower() in ("1", "true", "yes")

# --- Rolling audio corpus ---------------------------------------------------
# transcripts.log records what the model HEARD but the audio was deleted on every
# request, so a model comparison could never be re-run against real speech -- the
# first 762 clips are gone. Keep the last N source clips (the original upload, not
# the denoised copy: an A/B needs the same input the current model got) so
# distil-large-v3.5 / large-v3-turbo / parakeet can be scored on actual dictation
# instead of synthetic audio. Bounded by count, so it cannot grow forever.
# Filenames carry the timestamp, clip duration and wlogp, which makes the hard
# clips greppable: `ls audio_corpus | grep wlogp-0.2` finds the low-confidence ones.
# Anything moved into audio_corpus/keep/ is NEVER pruned -- that is where the
# outdoor / crowd / restaurant recordings go once you spot a good one.
# Off unless the user opts in, like the transcript log.
KEEP_AUDIO = os.getenv("KEEP_AUDIO", "false").lower() in ("1", "true", "yes")
KEEP_AUDIO_MAX = int(os.getenv("KEEP_AUDIO_MAX", 100))
AUDIO_CORPUS_DIR = os.path.join(TEMP_DIR, "audio_corpus")
AUDIO_KEEP_DIR = os.path.join(AUDIO_CORPUS_DIR, "keep")
AUDIO_HARD_DIR = os.path.join(AUDIO_CORPUS_DIR, "hard")
# Say one of these while dictating and the clip is filed permanently in keep/,
# named with the phrase, so a condition can be labelled by voice in the moment
# ("loud in here" in a restaurant) and grepped for later. The matched phrase is
# the label -- add a category by adding a phrase, no code change. Kept out of the
# 'prompt ai' trigger family on purpose: those rewrite the text, these only file
# the audio and leave the transcript alone.
CORPUS_TAG_TRIGGERS = [
    p.strip() for p in os.getenv(
        "CORPUS_TAG_TRIGGERS",
        "tag this clip,loud in here,restaurant test,outdoor test,crowd test,loud",
    ).split(",") if p.strip()
]
# Auto-pin clips the decoder struggled on into hard/, since the conditions worth
# collecting are exactly the ones where a spoken trigger is most likely to be
# misheard. -0.25 is the 5th percentile of 414 real logged clips (median -0.128,
# p10 -0.199, worst -0.701) -- roughly 1 clip in 20, so hard/ fills slowly.
KEEP_AUDIO_PIN_BELOW = float(os.getenv("KEEP_AUDIO_PIN_BELOW", -0.25))
KEEP_AUDIO_HARD_MAX = int(os.getenv("KEEP_AUDIO_HARD_MAX", 50))
# ...but wlogp is not a difficulty signal on its own: it rises with utterance
# length. Over the 54 clips logged 2026-08-06 corr(duration, wlogp) was +0.55 and
# the bucket means climbed monotonically -- -0.317 under 5s, -0.242 at 5-15s,
# -0.198 at 15-30s, -0.175 over 30s. Pinning the raw value therefore fills hard/
# with clips that are merely SHORT: 19 of the 21 auto-pinned that night were,
# while the genuinely adversarial ones (moving car, music over bluetooth, FM
# radio, mixed mic distance) averaged -0.169 -- BETTER than quiet dictation --
# and were never collected at all. A corpus of short clips is the one thing a
# noise A/B cannot use.
# So restate each clip's score as if it were REF_DUR seconds long before
# comparing it to the threshold. The slope is ~0.05 wlogp per natural-log unit
# of duration, measured off those same buckets. A 3s clip is then graded against
# short-clip expectations and a 45s clip against long-clip ones.
KEEP_AUDIO_PIN_REF_DUR = float(os.getenv("KEEP_AUDIO_PIN_REF_DUR", 15.0))
KEEP_AUDIO_PIN_DUR_SLOPE = float(os.getenv("KEEP_AUDIO_PIN_DUR_SLOPE", 0.05))

# --- Optional background-noise denoise (experimental) -----------------------
# When enabled, each clip is ALSO run through DeepFilterNet -- a standalone,
# self-contained CLI (no torch, no extra Python deps, model baked in) -- and
# transcribed a second time. Whichever pass the decoder is more confident about
# (higher duration-weighted avg_logprob) is the one returned. Keeping the better
# of the two means denoise can only ever help: if it scrubbed a real word, that
# pass scores lower and the raw pass wins. Off by default; costs one extra
# ~0.7s denoise + one extra decode per clip when on. Targets near-field-in-noise
# (a phone call / voice memo held close in a loud room), NOT far-field competing
# speech (a phone flat on a table), which no denoiser can separate.
DENOISE_ENABLED = os.getenv("DENOISE_ENABLED", "false").lower() in ("1", "true", "yes")
# Path to the deep-filter binary: DENOISE_BIN if set, else the release binary
# dropped into api/bin/ (deep-filter.exe on Windows, deep-filter elsewhere),
# else one on PATH. Keeps the server free of Windows-only assumptions (Docker).
def _default_denoise_bin() -> str:
    name = "deep-filter.exe" if os.name == "nt" else "deep-filter"
    bundled = os.path.join(os.path.dirname(__file__), "bin", name)
    if os.path.exists(bundled):
        return bundled
    return shutil.which("deep-filter") or bundled


DENOISE_BIN = os.getenv("DENOISE_BIN", "").strip() or _default_denoise_bin()
# deep-filter -a: attenuation limit in dB. 100 = full noise reduction; lower is
# gentler (mixes some original signal back in). Full strength is safe here since
# we keep the higher-confidence pass, but it is exposed for tuning.
DENOISE_ATTEN_LIMIT_DB = os.getenv("DENOISE_ATTEN_LIMIT_DB", "100")
# Denoise subprocess timeout, as a multiple of the clip's own duration plus a
# fixed floor for model load. deep-filter runs ~10x realtime on an idle box, but
# this machine also serves Ollama, so under CPU contention a long clip could blow
# through the old flat 60s cap. Scaling with duration means a 4-minute clip gets
# 4 minutes of headroom and only a genuinely hung process trips it. (This was a
# suspect for the first trial's 39% silent-failure rate, but the actual cause
# turned out to be non-WAV input -- see _denoise_wav. Kept as cheap insurance.)
DENOISE_TIMEOUT_FLOOR_SEC = float(os.getenv("DENOISE_TIMEOUT_FLOOR_SEC", 60))
DENOISE_TIMEOUT_RATIO = float(os.getenv("DENOISE_TIMEOUT_RATIO", 1.0))

# Roast (mean-mode) runs on a local reasoning model that cold-loads into VRAM
# already occupied by Whisper, so a roast fired right after a transcription is far
# slower than a warm run (~30s). Give it generous headroom or it times out and
# silently falls back to the raw text with no roast. Tune via .env.
ROAST_TIMEOUT = int(os.getenv("ROAST_TIMEOUT_SECONDS", 300))
# Roast command detector, used as a safety net: if the edit trigger fires but the
# spoken instruction is really a roast command (e.g. Whisper heard "Prompt AI roast"
# as "Prompte I roast", which the roast patterns miss), route it to the roaster.
# Anchored at the start (after an optional polite lead-in) so a real command like
# "roast this guy" matches while an incidental mention ("clean up my pot roast
# recipe") does not.
ROAST_COMMAND_RE = re.compile(
    r"^(?:(?:will|would|could|can)\s+you\s+|please\s+)*"
    r"(?:roast|tik\s*tok|tick\s*tock|(?:some\s*)?diss(?:es)?)\b",
    re.IGNORECASE,
)


def _port_already_serving(host: str = "127.0.0.1", port: int = API_PORT) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


# Single-instance guard for direct launches (python api/whisper_api.py,
# LAUNCHER.bat). If something already serves the port, this is a duplicate:
# refuse to start so we never squat the port with a second, possibly stale,
# instance -- the exact failure that let an old manual run shadow the service.
# The NSSM service starts via `uvicorn api.whisper_api:app`, so __name__ is not
# "__main__" there and this guard is skipped (uvicorn owns the bind). Runs before
# the model load so a duplicate exits instantly without touching the GPU.
if __name__ == "__main__" and _port_already_serving():
    print(
        f"[API] Port {API_PORT} is already serving; another instance (likely the "
        "FasterWhisperAPI service) is running. Not starting a duplicate."
    )
    raise SystemExit(1)

app = FastAPI(title="Faster Whisper API")


class OpenAIError(Exception):
    """An error returned in OpenAI's shape: {"error": {message, type, code}}."""

    def __init__(self, status: int, code: str, message: str,
                 err_type: str = "invalid_request_error", headers: dict = None):
        self.status, self.code, self.message = status, code, message
        self.err_type, self.headers = err_type, headers


def _openai_error_response(err: OpenAIError) -> JSONResponse:
    return JSONResponse(
        status_code=err.status,
        content={"error": {"message": err.message, "type": err.err_type, "code": err.code}},
        headers=err.headers,
    )


@app.exception_handler(OpenAIError)
async def _handle_openai_error(request: Request, err: OpenAIError):
    return _openai_error_response(err)


# /health stays open so the phone can tell "PC unreachable" from "wrong key".
_AUTH_EXEMPT_PATHS = {"/health"}


# Browsers add an Origin header to requests a web page makes. None of our
# clients is a web page, so any such request is a site trying to use the server
# behind the user's back (a page can POST to http://127.0.0.1:5000 without
# reading the answer). ALLOWED_ORIGINS (comma-separated) lets a trusted web UI in.
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "").split(",")
                   if o.strip()}


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    # Refuse oversized uploads before the body is read: the framework would
    # otherwise take in (and spool to disk) the whole thing first. _save_upload
    # still counts the bytes, for clients that send no Content-Length.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_UPLOAD_BYTES + 1024 * 1024:   # + room for the form fields
        return _openai_error_response(OpenAIError(
            413, "file_too_large", "Audio file is larger than 100 MB."))
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
        return _openai_error_response(OpenAIError(
            403, "origin_not_allowed", "Requests from web pages aren't accepted."))
    if _auth_on() and request.url.path not in _AUTH_EXEMPT_PATHS:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        # compare_digest: constant time, so the key can't be guessed byte by byte.
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                token.strip().encode(), WHISPER_API_KEY.encode()):
            return _openai_error_response(OpenAIError(
                401, "invalid_api_key", "Invalid or missing API key."))
    return await call_next(request)

# Load model globally (Always Hot)
print(f"[API] Loading {MODEL_SIZE} whisper model on {DEVICE}...")
try:
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE,
                         download_root=MODELS_DIR)
    print("[API] Model loaded and ready.")
except Exception as e:
    print(f"[API] Error loading model: {e}")
    model = None


def load_config():
    config_path = os.path.join(TEMP_DIR, "processing_config.json")
    example_path = os.path.join(TEMP_DIR, "processing_config.example.json")

    config = {
        "vocabulary": [],
        "name_corrections": {"Leslie": "Lesley", "Emma": "Ame"},
        "trigger_patterns": [
            # 'prompt' + up to two non-letters + 'a' + up to two non-letters + 'i',
            # with 'i' optionally spelled out phonetically as 'eye'. Case-insensitive
            # (the caller passes re.IGNORECASE). Absorbs every way Whisper renders the
            # spoken trigger -- "prompt ai", "A.I.", "A-I", "a eye", "A. Eye",
            # "promptai" -- while the trailing word boundary keeps "prompt a idea" /
            # "prompt a image" from firing on the 'i' inside the next word.
            "prompt[^a-zA-Z]{0,2}a[^a-zA-Z]{0,2}(?:eye|i)",
            "prompte[\\s.,\\-]*i\\.?",
            "end\\s*prompt",
        ],
        "roast_trigger_patterns": [
            "prompt\\s*a\\.?i\\.?\\W*(?:to\\s*)?roast",
            "prompt\\W*(?:to\\s*)?roast",
            "prompt\\s*a\\.?i\\.?\\W*(?:tik\\s*tok|tick\\s*tock)",
            "prompt\\W*(?:tik\\s*tok|tick\\s*tock)",
            "prompt\\s*a\\.?i\\.?\\W*(?:some\\s*)?diss(?:es)?",
            "prompt\\W*(?:some\\s*)?diss(?:es)?",
            "prompt\\s*a\\.?i\\.?\\W*(?:mean|meme)\\s*mode",
            "prompt\\W*(?:mean|meme)\\s*mode",
        ],
        "insults_file_path": "insults.md",
    }

    for path in [config_path, example_path]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    config.update(loaded)
                    return config
            except Exception as e:
                print(f"[API] Error loading config from {path}: {e}")

    return config


CONFIG = load_config()


def _build_hotwords(config: dict):
    """Words to bias Whisper's decoder toward: the configured vocabulary plus
    every name-correction target, so Whisper spells them right up front and the
    regex corrections are only a backstop. Whisper caps the prompt at ~223
    tokens and silently truncates past that, so keep the list short."""
    words = list(config.get("vocabulary", [])) + list(config.get("name_corrections", {}).values())
    unique = list(dict.fromkeys(w.strip() for w in words if w and w.strip()))
    return ", ".join(unique) or None


HOTWORDS = _build_hotwords(CONFIG)
print(f"[API] Whisper hotwords: {HOTWORDS}")

# Word lists change at runtime: the Settings window saves processing_config.json
# and the phone adds words via POST /v1/vocabulary. Reload when the file's
# modified time changes, so neither needs a server restart.
_CONFIG_PATH = os.path.join(TEMP_DIR, "processing_config.json")
_config_lock = threading.Lock()


def _config_mtime():
    try:
        return os.path.getmtime(_CONFIG_PATH)
    except OSError:
        return None


_config_seen_mtime = _config_mtime()


def _reload_config_if_changed() -> None:
    global CONFIG, HOTWORDS, _config_seen_mtime
    mtime = _config_mtime()
    if mtime == _config_seen_mtime:
        return
    with _config_lock:
        CONFIG = load_config()
        HOTWORDS = _build_hotwords(CONFIG)
        _config_seen_mtime = mtime
    print(f"[API] Word lists reloaded. Hotwords: {HOTWORDS}")


def _fastlog(msg: str) -> None:
    # NSSM does not capture stdout, so write fast-path diagnostics to a file we
    # can actually read when a trigger fires but editing/roasting misbehaves.
    try:
        with open(os.path.join(TEMP_DIR, "fastpath.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# Reason for edit: Automatically preload and keep Ollama edit model in VRAM on startup
_warmup_started = False


def trigger_ollama_warmup():
    global _warmup_started
    if _warmup_started:
        return
    _warmup_started = True
    if not OLLAMA_EDIT_PRELOAD:
        _fastlog("[Warmup] OLLAMA_EDIT_PRELOAD=false; skipping Ollama preload.")
        return
    if USE_OPENAI_EDIT:
        # Remote server keeps its own model loaded; don't pin a local Ollama
        # model in VRAM that nothing will use.
        _fastlog(f"[Warmup] Edit backend is {EDIT_ENDPOINT} ({EDIT_MODEL_NAME}); skipping Ollama preload.")
        return
    try:
        start_async_ollama_warmup(
            api_url=OLLAMA_EDIT_API_URL,
            model=OLLAMA_MODEL,
            keep_alive=OLLAMA_EDIT_KEEP_ALIVE,
            logger_fn=_fastlog,
        )
    except Exception as e:
        print(f"[API] Error initiating Ollama warmup: {e}")
        _fastlog(f"[Warmup] Error initiating Ollama warmup: {e}")


@app.on_event("startup")
def on_startup():
    trigger_ollama_warmup()


# Trigger warmup in background as soon as module loads so model weights begin loading immediately
trigger_ollama_warmup()



def strip_think(text: str) -> str:
    # Reasoning models (e.g. qwythos) emit a <think>...</think> block; the real
    # reply is whatever follows the last </think>. Sometimes only the closing tag
    # is present. Keep just the final answer so we never paste the reasoning.
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1].strip()
    if "<think>" in text:
        return text.split("<think>", 1)[0].strip()
    return text


def sanitize_social_output(text: str) -> str:
    # Mean-mode output should be paste-ready for a social reply. The model
    # ignores the formatting rules often, so enforce them deterministically.
    # 1. No em/en dashes -> comma (then collapse any doubled commas that makes).
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r",\s*,", ",", text)
    # 2. Strip hashtags and emoji. Voice dictation never produces these, so any
    #    in the output were invented by the model against the "no hashtags/emoji"
    #    rule. Covers emoji planes, misc symbols/dingbats, flags, arrows, plus
    #    the joiner/variation-selector glue that would otherwise be orphaned.
    text = re.sub(r"#[\w-]+", "", text)
    text = re.sub(
        "[\U0001F000-\U0001FAFF"  # emoji planes (symbols, faces, etc.)
        "\U00002600-\U000027BF"   # misc symbols + dingbats
        "\U0001F1E6-\U0001F1FF"   # regional-indicator flag letters
        "\U00002190-\U000021FF"   # arrows
        "\U00002B00-\U00002BFF"   # misc symbols and arrows
        "\U0000FE0F\U0000200D]",   # variation selector + zero-width joiner
        "",
        text,
    )
    # 3. Tidy whitespace left behind by the removals.
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


def read_insults(filepath: str) -> str:
    path = filepath
    if not os.path.isabs(path):
        # Resolve relative to the project root (one directory up from api/)
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _segment_is_reliable(seg) -> bool:
    """Return False for segments the model itself flags as junk.

    Two gates, both from scores faster-whisper already returns:
      1. Silence filler: no_speech_prob high AND avg_logprob low -- the model
         thinks this window is silence and isn't confident in the words. This
         is the exact signature of the "Thank you." / "I don't know." filler
         Whisper emits over a long pause. Requiring BOTH conditions keeps a
         real, quietly-spoken word (which has audio behind it, so a low
         no_speech_prob) from being dropped.
      2. Decode loop: compression_ratio above threshold means the text is so
         repetitive it compresses hard ("I'm I'm I'm I'm..."), i.e. the decoder
         got stuck. Normal speech stays well under 2.4.
    """
    if seg.no_speech_prob > NO_SPEECH_THRESHOLD and seg.avg_logprob < LOGPROB_THRESHOLD:
        return False
    if seg.compression_ratio > COMPRESSION_RATIO_THRESHOLD:
        return False
    return True


def _is_hint_echo(seg, hotwords: str, audio_duration: float) -> bool:
    """True when a segment is Whisper reading the hotword list back instead of
    hearing speech. A faint noise after the last word (stop beep, key release)
    decodes as "Ollama, Lesley, Ame, Sara, Lesley, Ame, ..." (the hint list,
    looped); seen in 7 dictations up to 2026-11-10.

    Every word must be a hotword, AND at least one sign it isn't speech:
      - it ends past the end of the audio (35.4-65.3 s on a 36.5 s clip);
      - it's faster than anyone talks (10 words in 0.1 s on another clip);
      - one hotword repeats 3+ times (the list looping).
    So someone who really says "Lesley and Sara" or "Sara, Sara!" keeps it.
    """
    if not hotwords:
        return False
    hint_words = {w.lower() for w in re.findall(r"[\w']+", hotwords)}
    said = [w.lower() for w in re.findall(r"[\w']+", seg.text)]
    if not said or not all(w in hint_words for w in said):
        return False
    past_end = seg.end > audio_duration + 0.5
    too_fast = len(said) / max(seg.end - seg.start, 0.01) > 8
    looping = max(said.count(w) for w in set(said)) >= 3
    return past_end or too_fast or looping


def _confidence_summary(segments: list) -> dict:
    """Collapse a list of segments into one comparable confidence score.

    faster-whisper reports avg_logprob per segment (higher = more confident,
    typically ~-0.15 on clean near-field speech, dipping past ~-0.7 as noise
    degrades the decode). We weight by segment duration so a long clip and a
    short one compare fairly, and also surface the single worst segment and the
    mean no-speech probability. This is the score the adaptive dual-pass will
    compare between the raw and denoised passes; logging it now, before that
    feature exists, is how we discover where the quiet-room vs. noisy cutoff
    actually falls on real speech instead of guessing.
    """
    if not segments:
        return {"wlogp": 0.0, "minlogp": 0.0, "mean_nsp": 0.0, "n": 0}
    total_dur = 0.0
    weighted = 0.0
    for s in segments:
        dur = max(s.end - s.start, 1e-3)
        weighted += s.avg_logprob * dur
        total_dur += dur
    return {
        "wlogp": weighted / total_dur if total_dur else 0.0,
        "minlogp": min(s.avg_logprob for s in segments),
        "mean_nsp": sum(s.no_speech_prob for s in segments) / len(segments),
        "n": len(segments),
    }


def _log_transcript(raw_text: str, final_text: str, info, kept: list, dropped: list,
                    denoise: str = None, audio: str = "", fillers: list = None) -> None:
    """Append one record per transcription to transcripts.log for corpus building.

    Logs the raw Whisper output, the final post-processed text (so trigger/edit
    effects are visible), a per-clip confidence summary (so we can see the real
    distribution of decode confidence on actual speech), an optional denoise
    comparison line (raw vs. denoised confidence and which pass won), and any
    segments the confidence gate removed with their scores (so the thresholds
    can be audited and tuned). Best-effort; never raises.
    """
    if not LOG_TRANSCRIPTS:
        return
    try:
        path = os.path.join(TEMP_DIR, "transcripts.log")
        with open(path, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(
                f"--- {ts} | lang={info.language} dur={info.duration:.1f}s "
                f"dropped={len(dropped)} ---\n"
            )
            c = _confidence_summary(kept)
            f.write(
                f"CONF : wlogp={c['wlogp']:.3f} minlogp={c['minlogp']:.3f} "
                f"mean_nsp={c['mean_nsp']:.2f} segs={c['n']}\n"
            )
            if denoise:
                f.write(f"DN   : {denoise}\n")
            # The bridge from text back to audio. Without it the log and the
            # corpus are two unrelated piles: you can grep a phrase you remember
            # saying, but never get to the recording of it.
            if audio:
                f.write(f"AUDIO: {audio}\n")
            f.write(f"RAW  : {raw_text!r}\n")
            if final_text != raw_text:
                f.write(f"FINAL: {final_text!r}\n")
            if fillers:
                f.write(f"FILL : removed {', '.join(fillers)}\n")
            for d in dropped:
                f.write(
                    f"DROP : nsp={d.no_speech_prob:.2f} logp={d.avg_logprob:.2f} "
                    f"cr={d.compression_ratio:.2f} {d.text.strip()!r}\n"
                )
    except Exception:
        pass


def _spoken_tag(text: str) -> str:
    """Return a filename-safe slug for the first CORPUS_TAG phrase heard, else "".

    Lets a clip label itself out loud: say "loud in here" while dictating in a
    restaurant and the clip lands in keep/ named with that tag, so months later
    `ls keep | grep loud-in-here` finds every one. The matched phrase IS the
    label, so adding a new category is just adding a phrase to the .env list --
    no code change.

    The phrase is deliberately NOT stripped from the transcript. Removing words
    the model actually heard is the one thing this pipeline refuses to do; a
    stray tag in the text is a far smaller cost than a filter that can eat real
    dictation.
    """
    for phrase in CORPUS_TAG_TRIGGERS:
        if re.search(r"\b" + re.escape(phrase) + r"\b", text, flags=re.IGNORECASE):
            return re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-")
    return ""


def _length_adjusted_wlogp(wlogp: float, duration: float) -> float:
    """Restate wlogp as if the clip had been KEEP_AUDIO_PIN_REF_DUR seconds long.

    Expected confidence rises with length (~KEEP_AUDIO_PIN_DUR_SLOPE per
    natural-log unit of duration), so comparing a 2s clip and a 45s clip against
    one fixed threshold compares them against different implicit standards.
    Subtracting the length term removes that, leaving a number that means the
    same thing at every duration.

    Short clips are graded up and long clips down, which is the intended effect
    in both directions: -0.30 on a 2s fragment is ordinary, while -0.30 across
    45s of continuous speech is genuinely unusual and worth keeping.

    Setting KEEP_AUDIO_PIN_DUR_SLOPE=0 restores the old raw-wlogp behaviour.
    """
    if wlogp == 0.0 or duration <= 0 or KEEP_AUDIO_PIN_DUR_SLOPE == 0:
        return wlogp
    return wlogp - KEEP_AUDIO_PIN_DUR_SLOPE * math.log(
        duration / KEEP_AUDIO_PIN_REF_DUR
    )


def _prune_dir(path: str, cap: int) -> None:
    """Trim a corpus folder to its newest `cap` files. Subfolders are never touched."""
    clips = sorted(f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f)))
    for stale in clips[:max(0, len(clips) - cap)]:
        try:
            os.remove(os.path.join(path, stale))
        except OSError:
            pass


def _archive_audio(src_path: str, orig_name: str, info, conf: dict,
                   raw_text: str = "") -> str:
    """File one source clip into the corpus and return its path relative to it.

    Archives the ORIGINAL upload, never the denoised copy -- a model A/B has to
    replay the same bytes the current model was given. The original extension is
    preserved because the client does not always send WAV (the Android path sends
    a compressed container), and re-testing has to reproduce that exact input.

    Three destinations, by intent:
      keep/  -- a CORPUS_TAG phrase was spoken. Deliberate, so it is permanent
                and never pruned. This is also the folder you drag clips into
                by hand.
      hard/  -- length-adjusted wlogp at or below KEEP_AUDIO_PIN_BELOW (see
                _length_adjusted_wlogp: raw wlogp would pin short clips, not
                difficult ones), i.e. the decoder
                struggled. Auto-detected rather than chosen, so it is capped
                (KEEP_AUDIO_HARD_MAX) and cannot grow forever. Catches the
                noisy clips a spoken trigger would miss -- in the exact
                conditions worth capturing, the trigger word is the thing most
                likely to be misheard, so confidence is the more reliable
                signal.
      root   -- everything else, rolling window of KEEP_AUDIO_MAX.

    The name encodes timestamp, duration, confidence and tag:
        20260805-160331_dur65.2s_wlogp-0.138_tag-loud-in-here_a1b2c3d4.wav
    The timestamp prefix sorts chronologically as plain text, which is what the
    prune relies on -- mtime would be rewritten by a file copy or a sync tool
    and silently reorder the corpus.

    Best-effort: an archiving failure must never fail a transcription.
    """
    if not KEEP_AUDIO:
        return ""
    try:
        for d in (AUDIO_CORPUS_DIR, AUDIO_KEEP_DIR, AUDIO_HARD_DIR):
            os.makedirs(d, exist_ok=True)

        tag = _spoken_tag(raw_text)
        wlogp = conf.get("wlogp", 0.0)
        duration = getattr(info, "duration", 0.0)
        # wlogp == 0.0 is _confidence_summary's empty sentinel (no kept segments),
        # not a perfect decode -- it must not read as "confident" and skip the pin.
        # It is also checked BEFORE the length adjustment: an empty transcript is a
        # real failure at any duration, and the 2.4s silent-failure clip on
        # 2026-08-06 is exactly the case that must never be normalised away.
        is_hard = (
            wlogp == 0.0
            or _length_adjusted_wlogp(wlogp, duration) <= KEEP_AUDIO_PIN_BELOW
        )

        if tag:
            dest, cap, sub = AUDIO_KEEP_DIR, None, "keep"
        elif is_hard:
            dest, cap, sub = AUDIO_HARD_DIR, KEEP_AUDIO_HARD_MAX, "hard"
        else:
            dest, cap, sub = AUDIO_CORPUS_DIR, KEEP_AUDIO_MAX, ""

        name = (
            f"{time.strftime('%Y%m%d-%H%M%S')}"
            f"_dur{duration:.1f}s"
            f"_wlogp{wlogp:.3f}"
            + (f"_tag-{tag}" if tag else "")
            + f"_{uuid.uuid4().hex[:8]}{os.path.splitext(orig_name)[1] or '.wav'}"
        )
        shutil.copy2(src_path, os.path.join(dest, name))
        if cap is not None:
            _prune_dir(dest, cap)
        return f"{sub}/{name}" if sub else name
    except Exception as e:
        print(f"[API] Audio archive failed (transcription unaffected): {e}")
        return ""


def _is_riff_wave(path: str) -> bool:
    """True if the file's own bytes are a RIFF/WAVE container.

    Checked by content, not extension: deep-filter sniffs content too (WAV bytes
    named .m4a denoise fine), so the extension tells us nothing useful.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except Exception:
        return False


def _transcode_to_wav(src_path: str, dst_path: str, rate: int = 48000):
    """Decode any audio container to mono 16-bit WAV at `rate`. (ok, reason).

    Uses PyAV, which faster-whisper already depends on and already has loaded --
    so this adds no dependency and no new binary. 48k is DeepFilterNet's native
    rate, so it does no resampling of its own. Imported locally so a broken or
    missing av can only ever disable denoise, never take down the API at import.
    """
    try:
        import av
        import numpy as np
        import wave as wave_mod

        chunks = []
        with av.open(src_path) as container:
            if not container.streams.audio:
                return False, "no audio stream in file"
            resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
            for frame in container.decode(container.streams.audio[0]):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray())
            # Flush whatever the resampler is still holding.
            try:
                for out in resampler.resample(None):
                    chunks.append(out.to_ndarray())
            except Exception:
                pass

        if not chunks:
            return False, "decoded to zero audio frames"

        data = np.concatenate(chunks, axis=1)[0].astype("<i2")
        with wave_mod.open(dst_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data.tobytes())
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _denoise_wav(src_path: str, file_id: str, duration: float = 0.0):
    """Run DeepFilterNet on src_path. Returns (out_path, failure_reason).

    Exactly one of the two is None. Best-effort: any failure (missing binary,
    non-zero exit, panic, timeout, missing output) yields (None, reason) so the
    caller falls back to the raw audio -- an absent binary is a no-op, never a
    broken transcription. deep-filter writes <out_dir>/<basename>, so we give it
    a per-request out dir to avoid collisions between concurrent transcriptions.

    The reason string is surfaced into transcripts.log rather than only printed.
    The first trial logged a flat "denoise pass unavailable" for 39% of clips
    with the real cause going to a service stdout nobody reads, which made the
    failure undiagnosable after the fact -- and deep-filter is perfectly
    informative when asked (it reports e.g. a hound WAV parse error, or panics
    with 'TooWide' on 32-bit PCM). Costs nothing to keep.
    """
    if not os.path.exists(DENOISE_BIN):
        return None, f"binary not found at {DENOISE_BIN}"

    out_dir = os.path.join(TEMP_DIR, f"{file_id}_dn")
    # Scale the cap with clip length; see DENOISE_TIMEOUT_* above.
    timeout = DENOISE_TIMEOUT_FLOOR_SEC + DENOISE_TIMEOUT_RATIO * max(duration, 0.0)
    started = time.time()
    pre_path = None
    try:
        # deep-filter's WAV reader (hound) accepts nothing else, so a compressed
        # upload dies instantly with "Ill-formed WAVE file: no RIFF tag found".
        # Whisper decodes those happily via PyAV, so transcription looked fine
        # while the denoise pass silently never ran -- which is what the Android
        # app hit on every single clip during the first trial. Normalise first.
        feed_path = src_path
        if not _is_riff_wave(src_path):
            pre_path = os.path.join(TEMP_DIR, f"{file_id}_pre.wav")
            ok, err = _transcode_to_wav(src_path, pre_path)
            if not ok:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None, f"input is not WAV and transcode failed: {err}"
            feed_path = pre_path

        os.makedirs(out_dir, exist_ok=True)
        proc = subprocess.run(
            [DENOISE_BIN, "-a", str(DENOISE_ATTEN_LIMIT_DB), "-o", out_dir, feed_path],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        elapsed = time.time() - started
        out_path = os.path.join(out_dir, os.path.basename(feed_path))
        if proc.returncode == 0 and os.path.exists(out_path):
            return out_path, None

        err = (proc.stderr or b"").decode(errors="ignore").strip().replace("\n", " ")
        if proc.returncode == 0:
            reason = f"exit 0 but no output file after {elapsed:.1f}s"
        else:
            reason = f"exit {proc.returncode} after {elapsed:.1f}s: {err[:180]}"
    except subprocess.TimeoutExpired:
        reason = f"timed out after {timeout:.0f}s (clip {duration:.1f}s)"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
    finally:
        # The transcoded copy is scratch either way -- the caller only ever needs
        # deep-filter's output. Removed on the success path too, which is why
        # this is a finally and not part of the failure cleanup below.
        if pre_path and os.path.exists(pre_path):
            try:
                os.remove(pre_path)
            except OSError:
                pass

    # Cleanup on EVERY failure path. out_dir is created before the run, so the
    # old code leaked one empty directory per failure -- 100 of them had piled
    # up in api/ by the end of the first trial.
    shutil.rmtree(out_dir, ignore_errors=True)
    print(f"[API] Denoise failed: {reason}")
    return None, reason


def _merge_hotwords(extra: str = None):
    """Server vocabulary plus a caller's per-request hints (the OpenAI `prompt`
    field), deduplicated, server words first so a long client list can't push
    them past Whisper's ~223-token prompt cap."""
    if not extra or not extra.strip():
        return HOTWORDS
    words = (HOTWORDS or "").split(",") + extra.split(",")
    seen, unique = set(), []
    for w in (w.strip() for w in words):
        # Case-insensitive: the phone's "ollama" shouldn't repeat the server's "Ollama".
        if w and w.lower() not in seen:
            seen.add(w.lower())
            unique.append(w)
    return ", ".join(unique) or None


def _decode(path: str, prompt: str = None, language: str = None) -> dict:
    """Transcribe one wav and bundle everything the caller needs to compare passes.

    Returns the info object, the kept/dropped segment split, the raw and
    confidence-gated text, and the per-clip confidence summary -- so the raw and
    denoised passes can be scored against each other with identical logic.
    `prompt` adds per-request vocabulary hints; `language` skips auto-detect.
    """
    hints = _merge_hotwords(prompt)
    segments_gen, info = model.transcribe(
        path,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        hotwords=hints,
        language=language or None,
    )
    segments = list(segments_gen)
    kept, dropped = [], []
    for s in segments:
        ok = _segment_is_reliable(s) and not _is_hint_echo(s, hints, info.duration)
        (kept if ok else dropped).append(s)
    return {
        "info": info,
        "kept": kept,
        "dropped": dropped,
        "raw_text": "".join(s.text for s in segments).strip(),
        "text": "".join(s.text for s in kept).strip(),
        "conf": _confidence_summary(kept),
    }


def _split_on_trigger(text: str, patterns: list) -> tuple:
    """Split dictated text at the 'prompt AI' trigger into (content, instruction).

    Everything before the trigger is the material to work on; everything after it
    is the spoken instruction. Doing this in code keeps the trigger phrase and the
    directions from leaking back into the output. Implemented in edit_commands
    (contract 03) so the app splits exactly the same way."""
    return edit_commands.split(text, patterns)


def _do_roast(brain_dump: str) -> str:
    """Rewrite a dictated brain-dump as a witty social-media clapback, weaving in a
    matching line from the insults bank. Shared by the direct roast trigger and the
    edit-path safety net (an edit trigger whose instruction turns out to be a roast).
    On any failure returns the brain-dump unchanged."""
    print("[API] Roast/TikTok mode: processing via Ollama...")
    insults = read_insults(CONFIG.get("insults_file_path", "insults.md"))
    system_prompt = _mean_system_prompt(insults)
    where, model = _mean_target()
    try:
        reply = strip_think(_mean_llm(system_prompt, brain_dump).strip())
        return sanitize_social_output(reply) if reply else brain_dump
    except Exception as e:
        _fastlog(f"ROAST FAIL @ {where} ({model}): {e!r}\n{traceback.format_exc()}")
        print(f"[API] Mean mode error: {e}")
        return brain_dump


MEAN_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mean_prompt.txt")


def _mean_system_prompt(insults: str) -> str:
    """Mean mode's system prompt: api/mean_prompt.txt with {cleanup_rules} (the
    editor prompt's "Always:" paragraph, so cleanup matches "prompt AI") and
    {insults} filled in. Falls back to the original built-in prompt if the file
    is missing or unreadable."""
    try:
        with open(MEAN_PROMPT_PATH, encoding="utf-8") as f:
            text = "".join(l for l in f if not l.startswith("#")).strip()
        rules = next((p for p in edit_commands.SYSTEM_PROMPT.split("\n\n")
                      if p.startswith("Always:")), "")
        if text and "{insults}" in text:
            return text.replace("{cleanup_rules}", rules).replace("{insults}", insults)
    except OSError:
        pass
    return _mean_system_prompt_builtin(insults)


def _mean_system_prompt_builtin(insults: str) -> str:
    return (
        "You are the ghostwriter for a witty, condescending person who is "
        "firing back at someone online (often a reply to a TikTok comment or "
        "a social media post). You are given their unstructured, dictated "
        "brain-dump. Turn it into the reply they would actually post.\n\n"
        "Do this:\n"
        "1. Figure out the reasoning failure the OTHER person is making (are "
        "they confidently wrong, ignoring evidence, faking expertise, just "
        "slow, closed-minded?). The user's brain-dump tells you what the "
        "other person got wrong; that flaw is your target.\n"
        "2. Reorganize the user's scattered thoughts into a tight, clear, "
        "succinct reply. Keep every point they actually made; cut the "
        "rambling and filler.\n"
        "3. Mirror the USER'S own voice and speech pattern from the "
        "brain-dump (their slang, rhythm, and casualness). You are writing "
        "AS the user, not to them. Do not sound like an AI.\n"
        "4. Weave in at least one insult from the category below that "
        "matches the other person's specific reasoning failure (more for a "
        "longer reply, where it fits). You may reword one or invent a "
        "similar one in the same spirit so it flows naturally. Each should "
        "feel earned, not bolted on.\n\n"
        "Hard formatting rules:\n"
        "- Plain text only. No markdown, no bullet points, no headings.\n"
        "- NEVER use em-dashes (—). Use commas, periods, or ellipses.\n"
        "- No hashtags and no emoji unless the user used them first.\n"
        "- Keep it short enough for a social media reply.\n"
        "- Output ONLY the finished reply. No preamble, no quotes around it, "
        "no explanation of what you did.\n\n"
        "Example brain-dump: 'the bible isn't real, if it was we'd expect "
        "talking snakes and burning bushes to talk too'\n"
        "Example reply: 'The bible isn't real. If it was, we'd expect the "
        "talking snakes and burning bushes to still be talking, right? Look, "
        "I could explain it further, but I left my crayons at home.'\n\n"
        f"Insults, grouped by the reasoning failure they target:\n{insults}"
    )


def _mean_target() -> tuple:
    """(url, model) that mean mode sends to (see MEAN_BACKEND)."""
    if MEAN_BACKEND == "edit":
        return ((EDIT_ENDPOINT, EDIT_LLM_MODEL) if USE_OPENAI_EDIT
                else (OLLAMA_EDIT_API_URL, OLLAMA_MODEL))
    if MEAN_BACKEND == "openai" and MEAN_LLM_BASE_URL:
        return (MEAN_LLM_BASE_URL + "/chat/completions", MEAN_LLM_MODEL)
    return (OLLAMA_API_URL, OLLAMA_ROAST_MODEL)


def _mean_llm(system_prompt: str, user_prompt: str) -> str:
    """One mean-mode request to whichever server Settings > Mean Mode picks.
    Creative task, so a higher temperature than editing. Raises on failure."""
    url, model = _mean_target()
    openai_style = ((MEAN_BACKEND == "edit" and USE_OPENAI_EDIT)
                    or (MEAN_BACKEND == "openai" and MEAN_LLM_BASE_URL))
    if openai_style:
        key = EDIT_LLM_API_KEY if MEAN_BACKEND == "edit" else MEAN_LLM_API_KEY
        no_think = EDIT_LLM_NO_THINK if MEAN_BACKEND == "edit" else MEAN_LLM_NO_THINK
        payload = {"model": model, "temperature": 0.8, "stream": False,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}]}
        if no_think:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = requests.post(url, json=payload, headers=headers, timeout=ROAST_TIMEOUT)
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""
    r = requests.post(url, json={"model": model, "prompt": user_prompt,
                                 "system": system_prompt, "stream": False},
                      timeout=ROAST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("response") or ""


def _call_edit_llm(system_prompt: str, user_prompt: str, fallback: str) -> tuple:
    """Send one request to the configured edit backend (OpenAI-compatible or
    Ollama). Returns (text, ok). On any error returns (fallback, False) and logs
    it, so callers never paste an error or an empty string."""
    try:
        if USE_OPENAI_EDIT:
            payload = {
                "model": EDIT_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "stream": False,
            }
            if EDIT_LLM_NO_THINK:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            headers = {"Authorization": f"Bearer {EDIT_LLM_API_KEY}"} if EDIT_LLM_API_KEY else {}
            response = requests.post(EDIT_ENDPOINT, json=payload, headers=headers, timeout=EDIT_LLM_TIMEOUT)
            response.raise_for_status()
            msg = response.json()["choices"][0]["message"]
            # strip_think is a safety net in case thinking leaks into content.
            out = strip_think(msg.get("content") or "").strip()
        else:
            response = requests.post(
                OLLAMA_EDIT_API_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": user_prompt,
                    "system": system_prompt,
                    "stream": False,
                    # Copy-editing is a low-creativity task; Ollama defaults to
                    # 0.8 which invites the model to drift, invent, and add
                    # preambles. 0.2 keeps it close to the dictated text.
                    "options": {"temperature": 0.2},
                    # Pin the model in VRAM (see OLLAMA_EDIT_KEEP_ALIVE) so an
                    # edit fired hours after the last one is still warm.
                    "keep_alive": OLLAMA_EDIT_KEEP_ALIVE,
                },
                timeout=60,
            )
            response.raise_for_status()
            out = (response.json().get("response") or "").strip()
        return (out, True) if out else (fallback, False)
    except Exception as e:
        _fastlog(f"EDIT FAIL @ {EDIT_ENDPOINT}: {e!r}\n{traceback.format_exc()}")
        print(f"[API] Edit LLM error: {e}")
        return fallback, False


def _trigger_fired(text: str) -> bool:
    """True if the text contains a "prompt AI" or mean-mode trigger. Those
    already go to an LLM with the speaker's instruction, so cleanup skips them."""
    for key in ("roast_trigger_patterns", "trigger_patterns"):
        patterns = CONFIG.get(key, [])
        if patterns and re.search(r"\b(" + "|".join(patterns) + r")\b", text, flags=re.IGNORECASE):
            return True
    return False


def _clean_dictation(text: str, style: dict) -> tuple:
    """One cleanup pass. Returns (text, ok). On any LLM failure the dictation
    comes back uncleaned (never an error message or an empty paste)."""
    vocabulary = list(CONFIG.get("vocabulary", [])) + list(CONFIG.get("name_corrections", {}).values())
    system_prompt = cleanup.build_system_prompt(style, vocabulary=list(dict.fromkeys(vocabulary)))
    out, ok = _call_edit_llm(system_prompt, cleanup.user_message(text), fallback=text)
    if not ok:
        _fastlog("CLEAN failed; returned the dictation uncleaned")
        return text, False
    out = cleanup.tidy_output(out, text) or text
    _fastlog(f"CLEAN ok style={style} {len(text)} -> {len(out)} chars")
    return out, True


def process_transcribed_text(text: str) -> str:
    # Auto-Correct Names
    for old_name, new_name in CONFIG.get("name_corrections", {}).items():
        # re.escape: a name like "C++" or "St. Louis" must match literally, and
        # (?<!\w)/(?!\w) instead of \b so names ending in a symbol still match.
        # The replacement is a function so a backslash in new_name stays literal.
        text = re.sub(rf"(?<!\w){re.escape(old_name)}(?!\w)", lambda _m, n=new_name: n,
                      text, flags=re.IGNORECASE)

    # Scan for Roast Triggers first
    roast_list = CONFIG.get("roast_trigger_patterns", [])
    if roast_list:
        roast_pattern = r"\b(" + "|".join(roast_list) + r")\b"
        if re.search(roast_pattern, text, flags=re.IGNORECASE):
            # Remove the trigger phrase from the brain-dump so it can't surface in
            # the roast reply. The instruction (roast) is fixed in the system prompt.
            roast_content = re.sub(roast_pattern, " ", text, flags=re.IGNORECASE).strip(" \t\n,.;:!?-")
            return _do_roast(roast_content)

    # Scan for Triggers
    trigger_list = CONFIG.get("trigger_patterns", [])
    if trigger_list:
        trigger_pattern = r"\b(" + "|".join(trigger_list) + r")\b"
        if re.search(trigger_pattern, text, flags=re.IGNORECASE):
            print(
                f"[API] Trigger word found, processing via edit LLM "
                f"({EDIT_MODEL_NAME} @ {EDIT_ENDPOINT})..."
            )

            # Strip the trigger in code and separate the spoken directions from the
            # content, so neither can leak into the output (see _split_on_trigger).
            content, instruction = _split_on_trigger(text, trigger_list)

            # Safety net: if the spoken instruction is really a roast command that
            # the roast patterns missed (e.g. Whisper heard "Prompt AI roast" as
            # "Prompte I roast"), route it to the roaster instead of the copy-editor.
            if instruction and ROAST_COMMAND_RE.match(instruction):
                print("[API] Edit instruction is a roast command; rerouting to roaster.")
                return _do_roast(content or text)

            # Message templates + system prompt: api/edit_prompt.json (contract 03).
            user_prompt = edit_commands.user_message(content, instruction)
            if user_prompt is None:
                # The whole utterance was only the trigger; nothing to do.
                return ""

            _fastlog(
                f"EDIT trigger fired -> POST {EDIT_ENDPOINT} model={EDIT_MODEL_NAME} "
                f"no_think={EDIT_LLM_NO_THINK if USE_OPENAI_EDIT else 'n/a'} | "
                f"content={content!r} instruction={instruction!r}"
            )
            system_prompt = edit_commands.SYSTEM_PROMPT
            edited, ok = _call_edit_llm(system_prompt, user_prompt, fallback=content or text)
            if ok:
                _fastlog(f"EDIT ok <- {len(edited)} chars")
            # On failure _call_edit_llm returns the trigger-stripped content, so a
            # failure never pastes back "prompt AI ...".
            return edited

    # No trigger found, return text with only the name corrections
    return text


def _run_pipeline(temp_path: str, file_id: str, filename: str,
                  prompt: str = None, language: str = None,
                  processing: str = "full", cleanup_style: dict = None) -> dict:
    """The dictation pipeline shared by /transcribe and /v1/audio/transcriptions:
    decode (+ optional denoise best-of), fast-path processing (name corrections,
    spoken edit commands), archive, log. processing="raw" skips the fast-path
    layer and returns Whisper's confidence-gated text untouched.

    Returns the final text plus everything a verbose response needs."""
    _reload_config_if_changed()
    # vad_filter strips silent regions (Silero VAD) before the decoder sees
    # them, so a long thinking pause can't decode into the language prior's
    # favorite end-of-clip filler ("Thank you."). condition_on_previous_text
    # is off so a hallucination in one window can't seed the next.
    raw = _decode(temp_path, prompt=prompt, language=language)

    # Optional best-of denoise pass. Run DeepFilterNet on the same audio,
    # transcribe the cleaned wav, and keep whichever pass the decoder trusts
    # more (higher weighted avg_logprob). Guarded so it can only ever help:
    # if denoise scrubbed a real word the denoised pass scores lower and we
    # fall back to raw. Only switch if the denoised pass actually kept speech.
    chosen, den, dn_note = raw, None, None
    if DENOISE_ENABLED:
        # Pass the clip's real duration so the subprocess cap scales with it.
        den_path, dn_fail = _denoise_wav(
            temp_path, file_id, getattr(raw["info"], "duration", 0.0)
        )
        if den_path:
            try:
                den = _decode(den_path, prompt=prompt, language=language)
                if den["kept"] and den["conf"]["wlogp"] > raw["conf"]["wlogp"]:
                    chosen = den
                winner = "DENOISED" if chosen is den else "RAW"
                # A denoised pass that kept no segments scores 0.000 from
                # _confidence_summary([]) -- which reads as "better" than any
                # negative logprob. Label it so the log can't be misread as a
                # denoise win, and so analysis can exclude it: it is a total
                # denoise failure (all speech scrubbed) that the guard caught,
                # not a real comparison.
                if not den["kept"]:
                    winner += " (denoised pass kept no speech)"
                dn_note = (f"raw_wlogp={raw['conf']['wlogp']:.3f} "
                           f"den_wlogp={den['conf']['wlogp']:.3f} -> kept {winner}")
            finally:
                shutil.rmtree(os.path.dirname(den_path), ignore_errors=True)
        else:
            dn_note = f"unavailable -- {dn_fail}"

    info = chosen["info"]
    raw_text = chosen["raw_text"]
    text = chosen["text"]

    # Filler sounds go first, so the fast-path layer (name corrections, spoken
    # edit commands) works on clean text.
    fillers = []
    if processing != "raw" and REMOVE_FILLERS:
        text, fillers = remove_fillers(text)

    # Fast-Path Processing Layer
    triggered = processing != "raw" and _trigger_fired(text)
    if processing != "raw":
        text = process_transcribed_text(text)

    # processing=mean: the whole dictation goes to mean mode, no trigger needed.
    if processing == "mean" and not triggered and text.strip():
        text = _do_roast(text)
        triggered = True

    # Optional cleanup pass: asked for, no trigger already handled it, not empty.
    if (not triggered and text.strip()
            and (processing == "clean" or (processing == "full" and CLEANUP_ALWAYS))):
        text, _ok = _clean_dictation(text, cleanup_style or CLEANUP_STYLE)

    # Corpus log: raw Whisper output, final text, per-clip confidence, the
    # denoise comparison (when enabled), and any dropped segments.
    # Archive BEFORE logging so the log can name the file it produced --
    # that AUDIO line is what turns "I remember saying this somewhere loud"
    # into the actual recording. Tag detection runs on the raw text, before
    # process_transcribed_text can rewrite a trigger phrase out of it.
    archived = _archive_audio(temp_path, filename or "clip.wav", info,
                              chosen["conf"], raw_text)

    _log_transcript(raw_text, text, info, chosen["kept"], chosen["dropped"],
                    denoise=dn_note, audio=archived, fillers=fillers)

    return {"text": text, "raw_text": raw_text, "info": info, "kept": chosen["kept"]}


# The pipeline runs in a worker thread so /health stays instant while a clip
# (or a ~10 s spoken edit) is processing -- the phone's connectivity check must
# not read a busy server as a dead one. The lock keeps GPU work one clip at a
# time, exactly as it was when this ran on the event loop.
_PIPELINE_LOCK = threading.Lock()


def _run_pipeline_serialized(*args, **kwargs) -> dict:
    with _PIPELINE_LOCK:
        return _run_pipeline(*args, **kwargs)


# --- Soft pause (contract 01 §9) ---------------------------------------------------
# Frees the GPU without stopping the service (which needs an admin prompt): the
# model moves to normal RAM and comes back in about a second. While paused,
# dictation is REFUSED rather than silently reloading, so a phone dictation in
# the middle of a game can't grab the VRAM back. reason: "user" (tray menu) or
# "game" (the tray's gaming detection, which also resumes it afterwards).
PAUSE = {"paused": False, "reason": None, "since": None}


def _model_on_gpu() -> bool:
    try:
        return bool(model is not None and model.model.model_is_loaded)
    except Exception:
        return model is not None


def pause_model(reason: str = "user") -> None:
    with _PIPELINE_LOCK:  # waits for a dictation in progress to finish
        if model is not None and _model_on_gpu():
            try:
                model.model.unload_model(to_cpu=True)
            except Exception as e:
                print(f"[API] Could not unload the model: {e}")
        PAUSE.update(paused=True, reason=reason, since=time.time())
    print(f"[API] Paused ({reason}): model moved off the GPU.")


def resume_model() -> None:
    with _PIPELINE_LOCK:
        if model is not None and not _model_on_gpu():
            model.model.load_model()
        PAUSE.update(paused=False, reason=None, since=None)
    print("[API] Resumed: model back on the GPU.")


def _paused_message() -> str:
    why = "for gaming" if PAUSE["reason"] == "game" else "from the tray menu"
    return f"Dictation is paused on the PC ({why}). Resume it from the tray icon."


class UploadTooLarge(Exception):
    pass


def _safe_upload_name(filename: str) -> str:
    """Only the last part of a client-supplied name, and only plain characters:
    no folders, no "..", no drive letters, no reserved characters. The server
    runs as a Windows service, so a name like "..\\..\\Windows\\x" must never
    reach the filesystem."""
    name = re.split(r"[\\/]", filename or "")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:80]
    return name or "clip.wav"


def _save_upload(file: UploadFile) -> tuple:
    """Write an upload to a unique temp path, refusing anything over
    MAX_UPLOAD_BYTES (counted while writing: the client's declared size can lie
    or be missing). Returns (file_id, temp_path)."""
    file_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_DIR, f"{file_id}_{_safe_upload_name(file.filename)}")
    written = 0
    try:
        with open(temp_path, "wb") as buffer:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise UploadTooLarge()
                buffer.write(chunk)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    return file_id, temp_path


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    if PAUSE["paused"]:
        raise HTTPException(status_code=503, detail=_paused_message())
    temp_path = None
    try:
        file_id, temp_path = _save_upload(file)
        r = await run_in_threadpool(_run_pipeline_serialized, temp_path, file_id,
                                    file.filename)
        return {"text": r["text"], "language": r["info"].language,
                "duration": r["info"].duration}
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="Audio file is larger than 100 MB.")
    except Exception as e:
        print(f"[API] Transcription error: {e}")
        raise HTTPException(status_code=500, detail="Transcription failed (%s); see the "
                            "server log." % type(e).__name__)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


_RESPONSE_FORMATS = ("json", "text", "verbose_json")


# OpenAI-compatible route (contract: WhisperRemote contracts/01-transcription-api.md §1).
# `model` and `temperature` are accepted so stock OpenAI clients work, and ignored:
# the server runs whatever model it has loaded. `processing` is our own extra.
@app.post("/v1/audio/transcriptions")
async def v1_transcriptions(
    file: UploadFile = File(None),
    model_name: str = Form(None, alias="model"),
    prompt: str = Form(None),
    language: str = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(None),
    processing: str = Form("full"),
    cleanup_punctuation: str = Form(None),
    cleanup_rewrite: str = Form(None),
    cleanup_shorten: str = Form(None),
    cleanup_tone: str = Form(None),
    cleanup_emoji: str = Form(None),
):
    if file is None:
        raise OpenAIError(400, "missing_file", "No audio file in the 'file' field.")
    response_format = (response_format or "json").strip().lower()
    if response_format not in _RESPONSE_FORMATS:
        raise OpenAIError(400, "invalid_response_format",
                          f"response_format must be one of {', '.join(_RESPONSE_FORMATS)}.")
    processing = (processing or "full").strip().lower()
    if processing not in ("full", "raw", "clean", "mean"):
        raise OpenAIError(400, "invalid_processing",
                          "processing must be 'full', 'raw', 'clean' or 'mean'.")
    # Per-request style overrides on top of this server's defaults.
    overrides = {k: v for k, v in (("punctuation", cleanup_punctuation),
                                   ("rewrite", cleanup_rewrite),
                                   ("shorten", cleanup_shorten),
                                   ("tone", cleanup_tone),
                                   ("emoji", cleanup_emoji)) if v}
    style, style_err = cleanup.normalize_style(dict(CLEANUP_STYLE, **overrides))
    if style_err:
        raise OpenAIError(400, "invalid_cleanup_option", style_err + ".")
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise OpenAIError(413, "file_too_large", "Audio file is larger than 100 MB.")
    if model is None:
        raise OpenAIError(503, "model_unavailable", "The Whisper model failed to load.",
                          err_type="server_error", headers={"Retry-After": "30"})
    if PAUSE["paused"]:
        raise OpenAIError(503, "server_paused", _paused_message(),
                          err_type="server_error", headers={"Retry-After": "60"})

    temp_path = None
    try:
        file_id, temp_path = _save_upload(file)
        r = await run_in_threadpool(
            _run_pipeline_serialized, temp_path, file_id, file.filename, prompt=prompt,
            language=language, processing=processing, cleanup_style=style)
    except UploadTooLarge:
        raise OpenAIError(413, "file_too_large", "Audio file is larger than 100 MB.")
    except Exception as e:
        print(f"[API] /v1 transcription error: {e}")
        raise OpenAIError(500, "server_error", "Transcription failed (%s); see the server "
                          "log." % type(e).__name__, err_type="server_error")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    if response_format == "text":
        return PlainTextResponse(r["text"])
    if response_format == "json":
        return {"text": r["text"]}
    info = r["info"]
    return {
        "task": "transcribe",
        "language": info.language,
        "duration": info.duration,
        "text": r["text"],
        "segments": [
            {"id": i, "start": s.start, "end": s.end, "text": s.text,
             "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob}
            for i, s in enumerate(r["kept"])
        ],
        "model": MODEL_SIZE,
        "raw_text": r["raw_text"],
    }


@app.get("/v1/models")
async def v1_models():
    return {"object": "list",
            "data": [{"id": MODEL_SIZE, "object": "model", "owned_by": "local"}]}


# --- Shared vocabulary (contract: WhisperRemote contracts/01-transcription-api.md §8) ---
# The server's list is THE list: the phone shows it and adds to it, so a word
# typed on the phone while out also works at the desk later. Adding only;
# removing words is done in the Settings window on purpose.

VOCAB_HINT_BUDGET = 540   # ~ the 224 tokens of hints Whisper reads
VOCAB_MAX_TERM = 60
VOCAB_MAX_ADD = 50


class VocabularyAdd(BaseModel):
    add: List[str]


def _vocabulary_state() -> dict:
    return {
        "vocabulary": list(CONFIG.get("vocabulary", [])),
        # Name-correction targets are hinted automatically; listed so the app
        # can show them, but they're edited as corrections, not here.
        "from_name_corrections": list(dict.fromkeys(CONFIG.get("name_corrections", {}).values())),
        # Full pairs (heard -> written), so the app can show them read-only.
        "name_corrections": dict(CONFIG.get("name_corrections", {})),
        "chars": len(HOTWORDS or ""),
        "max_chars": VOCAB_HINT_BUDGET,
    }


@app.get("/v1/vocabulary")
async def v1_get_vocabulary():
    _reload_config_if_changed()
    return _vocabulary_state()


@app.post("/v1/vocabulary")
async def v1_add_vocabulary(body: VocabularyAdd):
    global CONFIG, HOTWORDS, _config_seen_mtime
    words = [w.strip() for w in body.add if w and w.strip()]
    if not words or len(words) > VOCAB_MAX_ADD:
        raise OpenAIError(400, "invalid_vocabulary",
                          "Send 1 to %d words in 'add'." % VOCAB_MAX_ADD)
    bad = [w for w in words if len(w) > VOCAB_MAX_TERM or "\n" in w or "," in w]
    if bad:
        raise OpenAIError(400, "invalid_vocabulary",
                          "Each word must be under %d characters with no commas or "
                          "line breaks: %s" % (VOCAB_MAX_TERM, bad[0][:80]))
    _reload_config_if_changed()
    with _config_lock:
        data = load_config()
        vocab = list(data.get("vocabulary", []))
        known = {w.lower() for w in vocab} | {
            v.lower() for v in data.get("name_corrections", {}).values()}
        added = []
        for w in words:
            if w.lower() not in known:
                known.add(w.lower())
                vocab.append(w)
                added.append(w)
        if added:
            # Keep every other key (triggers, comments) exactly as it was.
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    on_disk = json.load(f)
            except (OSError, ValueError):
                on_disk = {k: v for k, v in data.items()}
            on_disk["vocabulary"] = vocab
            tmp = _CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(on_disk, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, _CONFIG_PATH)
            CONFIG = load_config()
            HOTWORDS = _build_hotwords(CONFIG)
            _config_seen_mtime = _config_mtime()
            print(f"[API] Vocabulary added: {added}")
    return dict(_vocabulary_state(), added=added)


# Added /transcribe_time_stamped endpoint to provide segment-level timestamps without breaking existing /transcribe endpoint
@app.post("/transcribe_time_stamped")
async def transcribe_time_stamped(file: UploadFile = File(...)):
    if PAUSE["paused"]:
        raise HTTPException(status_code=503, detail=_paused_message())
    temp_path = None
    try:
        file_id, temp_path = _save_upload(file)

        # Transcribe (same VAD / no-carryover settings as /transcribe; see there).
        segments_gen, info = model.transcribe(
            temp_path,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            hotwords=HOTWORDS,
        )

        # segments is a generator, consume it to get all items
        segments = list(segments_gen)
        segments = [x for x in segments if not _is_hint_echo(x, HOTWORDS, info.duration)]
        
        # Keep original text exactly as Faster Whisper output it
        full_text = "".join([segment.text for segment in segments]).strip()
        
        # Build segment-level data
        formatted_segments = []
        for segment in segments:
            formatted_segments.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
            })

        # We intentionally skip process_transcribed_text() here because AI rewriting 
        # (like roasting or fixing grammar) would break the alignment with the timestamps.
        return {
            "text": full_text,
            "segments": formatted_segments,
            "language": info.language,
            "duration": info.duration
        }
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="Audio file is larger than 100 MB.")
    except Exception as e:
        print(f"[API] Time-stamped transcription error: {e}")
        raise HTTPException(status_code=500, detail="Transcription failed (%s)."
                            % type(e).__name__)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


# Reason for edit: Added /transcribe_words for aMemory (per-channel call transcripts).
# Whisper merges speech across pauses into one segment; per-word times let the
# caller split at pauses and interleave both sides of a call. Same model and the
# same decode settings as /transcribe_time_stamped, so text/segments match it.
# Never logs transcript text.
@app.post("/transcribe_words")
async def transcribe_words(file: UploadFile = File(...)):
    if PAUSE["paused"]:
        raise HTTPException(status_code=503, detail=_paused_message())
    temp_path = None
    try:
        file_id, temp_path = _save_upload(file)

        segments_gen, info = model.transcribe(
            temp_path,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            hotwords=HOTWORDS,
            word_timestamps=True,
        )
        segments = list(segments_gen)
        segments = [x for x in segments if not _is_hint_echo(x, HOTWORDS, info.duration)]

        full_text = "".join([segment.text for segment in segments]).strip()

        formatted_segments = []
        for segment in segments:
            formatted_segments.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "words": [
                    {
                        "start": w.start,
                        "end": w.end,
                        "word": w.word,
                        "probability": w.probability,
                    }
                    for w in (segment.words or [])
                ],
            })

        return {
            "text": full_text,
            "segments": formatted_segments,
            "language": info.language,
            "duration": info.duration,
        }
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="Audio file is larger than 100 MB.")
    except Exception as e:
        print(f"[API] Word-timestamp transcription error: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Transcription failed (%s)."
                            % type(e).__name__)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


# Reason for edit: Added Ollama edit model metadata to /health and added /warmup endpoint to trigger manual preload
class PauseRequest(BaseModel):
    reason: str = "user"


@app.post("/v1/pause")
async def v1_pause(body: PauseRequest = None):
    reason = (body.reason if body else "user") or "user"
    if reason not in ("user", "game"):
        raise OpenAIError(400, "invalid_reason", "reason must be 'user' or 'game'.")
    await run_in_threadpool(pause_model, reason)
    return {"status": "paused", "paused_reason": reason}


@app.post("/v1/resume")
async def v1_resume():
    if model is None:
        raise OpenAIError(503, "model_unavailable", "The Whisper model failed to load.",
                          err_type="server_error")
    await run_in_threadpool(resume_model)
    return {"status": "ok"}


@app.get("/health")
async def health():
    # Contract §2: status / api_version / model / auth_required are the stable
    # fields; the rest are informational. Never needs auth (see middleware).
    # It's the one route anyone can call, so it never names internal addresses,
    # and with a key set it says only what the phone needs before it has one.
    body = {
        "status": ("error" if model is None else "paused" if PAUSE["paused"] else "ok"),
        "paused_reason": PAUSE["reason"],
        "api_version": API_VERSION,
        "auth_required": _auth_on(),
        "model": MODEL_SIZE,
    }
    if not _auth_on():
        body.update(device=DEVICE, edit_model=EDIT_MODEL_NAME, ollama_model=OLLAMA_MODEL)
    return body


@app.post("/warmup")
async def warmup():
    trigger_ollama_warmup()
    return {"status": "warming_up", "model": EDIT_MODEL_NAME}



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
