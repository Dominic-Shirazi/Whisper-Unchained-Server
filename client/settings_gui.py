"""Settings window for the Faster-Whisper stack.

Launched from the listener tray icon ("Settings..."). Edits the project's .env
in place and offers to restart the API service so the change takes effect.

Two constraints shaped this file:

1. It writes .env LINE BY LINE, never by regenerating the file. That .env is
   half configuration and half lab notebook -- the comments carry measured
   numbers (wlogp percentiles, denoise timings, VRAM costs) that took real
   experiments to produce. A naive dump-the-dict save would silently destroy
   all of it, so write_env only ever replaces the value on an existing
   `KEY=` line and leaves every other byte untouched.

2. It runs as its OWN PROCESS, not a thread of the watchdog. pystray owns the
   main thread and Tk insists on being on the main thread of its interpreter;
   the two cannot share one. Spawning a subprocess is the only arrangement
   where the tray stays responsive while this window is open.

Dependencies: stdlib tkinter, plus `qrcode` (pure Python) for the Phone
section's pairing code. The QR is drawn straight onto a Tk canvas from its
module matrix, so no image library is involved.
"""

import ctypes
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import webbrowser

import hotkey_config
import llm_detect
import local_api
import model_catalog
import transcribe_client
import words_config
import threading
import tkinter as tk
from tkinter import ttk

# Crisper text on high-DPI displays. Must run before any widget exists, and is
# purely cosmetic, so a failure here is never worth crashing over.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# Our own taskbar identity. Without it Windows groups every window under
# pythonw.exe and shows the Python icon instead of ours.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CyrusStudio.WhisperUnchained")
except Exception:
    pass

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_DIR, ".env")
SERVICE_NAME = "FasterWhisperAPI"

# --- Palette ---------------------------------------------------------------
# Near-black neutrals with a single teal accent. Deliberately no purple.
BG       = "#15171C"   # window ground
SURFACE  = "#1B1E25"   # sidebar / footer
RAISED   = "#222731"   # input fields
BORDER   = "#2B313C"
TEXT     = "#E7E9EC"
MUTED    = "#A3AAB6"   # help text, secondary labels (brighter: dim grey on near-black read thin)
ACCENT   = "#2FB79A"
ACCENT_H = "#3ACFAF"
DANGER   = "#D9694A"
WARN     = "#D9A441"

# Fonts: Noto Sans (SIL Open Font License, client/assets/fonts) loaded privately
# for this process, in the Medium weight for normal text. Regular-weight Segoe UI
# read as too thin on the dark background; Medium is a little sturdier without
# looking bold; the SemiCondensed cut keeps words from looking wide. Falls back to Segoe UI if the files can't be loaded.
def _load_bundled_fonts():
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
    loaded = 0
    for name in ("NotoSans-SemiCondensedMedium.ttf", "NotoSans-SemiCondensedSemiBold.ttf"):
        try:
            loaded += bool(ctypes.windll.gdi32.AddFontResourceExW(os.path.join(folder, name), 0x10, 0))
        except Exception:
            pass
    return loaded == 2


if _load_bundled_fonts():
    # (Windows truncates the SemiBold family name to 31 characters: "...SemiBol".)
    _FONT, _FONT_SB = "Noto Sans SemiCondensed Medium", "Noto Sans SemiCondensed SemiBol"
    _FONT_DISPLAY = _FONT_SB
else:
    _FONT, _FONT_SB, _FONT_DISPLAY = "Segoe UI", "Segoe UI Semibold", "Segoe UI Semibold"
F_BODY    = (_FONT, -15)   # negative = pixels
F_TITLE   = (_FONT_DISPLAY, 18)
F_LABEL   = (_FONT, -15)
F_HELP    = (_FONT, -14)
F_HELP_I  = (_FONT, -14, "italic")
F_BUTTON  = (_FONT_SB, 11)
F_SECTION = (_FONT_SB, 14)
F_BRAND   = (_FONT_SB, 12)
F_TAB     = (_FONT_SB, -15)   # sidebar section names

# keep_alive is the one setting whose raw value is genuinely cryptic (-1 does
# not read as "forever" to anyone), so it gets spelled out. Values are sent to
# Ollama verbatim; -1 must stay a bare number, not a duration string.
SOURCE_CHOICES = [
    ("This PC (runs Whisper here)", "local"),
    ("Another PC running Whisper Unchained Server", "remote"),
    ("A cloud service (Groq, OpenAI, ...)", "cloud"),
]
CLOUD_PROVIDER_CHOICES = [(label, key) for key, (label, _u, _m)
                          in transcribe_client.CLOUD_PRESETS.items()]
SOURCE_INTROS = {
    "local": "The speech-to-text engine (Whisper). It runs on this PC, and both the "
             "hotkey and your phone send audio to it.",
    "remote": "Your dictation is sent to another PC that runs Whisper Unchained Server "
              "(e.g. over Tailscale). That PC does the transcribing and its own extras.",
    "cloud": "Your dictation is sent to a cloud transcription service with your own "
             "API key. Fast and needs no graphics card, but the audio leaves this PC.",
}
# Pages that only matter when this PC runs the server itself.
LOCAL_ONLY_SECTIONS = {"AI Editing (LLM)", "Mean Mode", "Audio Corpus", "Denoise", "Phone"}

EDIT_BACKEND_CHOICES = [
    ("Ollama (this PC or LAN)", "ollama"),
    ("OpenAI-compatible server or cloud", "openai"),
]

KEEP_ALIVE_CHOICES = [
    ("Until unloaded manually", "-1"),
    ("5 minutes (Ollama default)", "300"),
    ("30 minutes", "1800"),
    ("Unload immediately", "0"),
]


class Field:
    """One editable setting: how to render it, and how to validate it."""

    def __init__(self, key, label, kind, help="", choices=None,
                 labelled_choices=None, default="", group=None, emphasis=""):
        self.key = key
        # A short italic line under the help text, for the one fact that matters.
        self.emphasis = emphasis
        # Fields sharing a group are shown or hidden together (see _show_group).
        self.group = group
        # Shown when .env has no line for this key yet (newer settings).
        self.default = default
        self.label = label
        # toggle | choice | pick | hotkey | text | otext (optional text) |
        # secret (optional, masked) | int | port | float | list
        self.kind = kind
        self.help = help
        self.choices = choices or []
        self.labelled_choices = labelled_choices or []
        self.var = None
        self.widget = None

    def coerce(self, raw):
        """Return (value, error). Keeps a bad entry from ever reaching .env."""
        raw = raw.strip()
        if self.kind == "int":
            try:
                int(raw)
            except ValueError:
                return None, "%s: needs a whole number" % self.label
        elif self.kind == "float":
            try:
                float(raw)
            except ValueError:
                return None, "%s: needs a number" % self.label
        elif self.kind == "secret":
            if raw and not re.fullmatch(r"[A-Za-z0-9_\-.~]{12,128}", raw):
                return None, ("%s: 12-128 letters, digits or - _ . ~ "
                              "(or empty for no key)" % self.label)
        elif self.kind == "port":
            if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
                return None, "%s: a number from 1024 to 65535" % self.label
        elif self.kind in ("text", "choice", "pick", "hotkey", "model") and not raw:
            return None, "%s: cannot be empty" % self.label
        return raw, None


CUSTOM_LABEL = "Custom..."


SCHEMA = [
    ("Transcription", [
        Field("TRANSCRIBE_SOURCE", "Where transcription runs", "choice",
              "Only the settings for your choice are shown below.",
              labelled_choices=SOURCE_CHOICES, default="local"),
        # --- This PC ---
        # Shown, not edited, here: the Models list below is the one place to
        # choose, so nobody picks from bare names without the notes.
        Field("WHISPER_MODEL_SIZE", "Whisper model", "model",
              "Bigger models are more accurate. On a GPU even the large ones stay "
              "fast (turbo: ~0.7 s for a 16 s clip); on CPU, stick to small or "
              "smaller.", group="src_local"),
        Field("WHISPER_DEVICE", "Compute device", "choice",
              "cuda uses the GPU. Falls back to CPU if unavailable.",
              choices=["cuda", "cpu", "auto"], group="src_local"),
        Field("WHISPER_MODELS_DIR", "Model folder", "text",
              "Where the service loads models from (relative = inside this "
              "project). Shared with this window so it can show and download models.",
              default="models", group="src_local"),
        # --- Another PC ---
        Field("REMOTE_SERVER_URL", "Server address", "otext",
              "The other PC's Whisper Unchained Server, ending in /v1, e.g. "
              "http://100.x.y.z:5000/v1 (its Tailscale address). That PC applies its "
              "own vocabulary, \"prompt AI\" commands and cleanup.",
              group="src_remote"),
        Field("REMOTE_API_KEY", "Server API key", "secret",
              "Only if that server has a key (its Settings > Phone page).",
              group="src_remote"),
        Field("REMOTE_CERT_SHA256", "Certificate fingerprint", "otext",
              "Only if that server uses HTTPS (its Settings > Phone page, "
              "\"Encrypted connection\"): the SHA-256 fingerprint it shows. This PC "
              "then trusts exactly that server.",
              group="src_remote"),
        # --- Cloud ---
        Field("CLOUD_PROVIDER", "Cloud service", "choice",
              "Choosing one fills in its address and a recommended model.",
              labelled_choices=CLOUD_PROVIDER_CHOICES, default="groq", group="src_cloud"),
        Field("CLOUD_BASE_URL", "Service address", "otext",
              "Ends in /v1 (or /openai/v1 for Groq).", group="src_cloud"),
        Field("CLOUD_API_KEY", "API key", "secret",
              "From your account on that service. Stays on this PC.", group="src_cloud"),
        Field("CLOUD_MODEL", "Model", "otext",
              "Your vocabulary is sent as hints and name corrections run on this PC. "
              "\"Prompt AI\" commands and cleanup aren't available with a cloud "
              "service yet.", group="src_cloud"),
        Field("REMOVE_FILLERS", "Remove filler sounds", "toggle",
              "Deletes standalone um, umm, uh, uhh and hmm. Never \"like\" or "
              "\"you know\": those are usually real words. Every removal is "
              "logged, so nothing disappears silently. Instant, no AI. Not needed "
              "if AI Editing > \"Clean up every dictation\" is on (that removes "
              "fillers too), but it still helps when the AI can't be reached.",
              default="false"),
    ]),
    # The LLM that runs spoken edit commands ("prompt AI, make this formal").
    # Help text on these fields is rewritten live by _refresh_ai_help().
    # Custom panel (see _build_words_panel): saved to api/processing_config.json.
    ("Words & Triggers", []),
    ("AI Editing (LLM)", [
        Field("EDIT_BACKEND", "Edit backend", "choice",
              "Which kind of server runs spoken edit commands. Only the "
              "settings for the one you pick are shown.",
              labelled_choices=EDIT_BACKEND_CHOICES, default="ollama"),
        Field("OLLAMA_EDIT_API_URL", "Ollama endpoint", "otext",
              "Ollama on this PC or your LAN. Use an IP, not a hostname: the API "
              "runs as a LocalSystem service and cannot resolve MagicDNS names.", group='ollama'),
        Field("OLLAMA_MODEL", "Ollama edit model", "pick", "x", group='ollama'),
        Field("OLLAMA_EDIT_KEEP_ALIVE", "Keep edit model in VRAM", "pick", "x",
              labelled_choices=KEEP_ALIVE_CHOICES, group='ollama'),
        Field("EDIT_LLM_BASE_URL", "Server base URL", "otext",
              "Any OpenAI-compatible server, ending in /v1: llama.cpp or LM Studio "
              "on your LAN, or a cloud service (OpenRouter, OpenAI, Groq...).", group='openai'),
        Field("EDIT_LLM_MODEL", "Server model", "otext",
              "The model name that server expects.", group='openai'),
        Field("EDIT_LLM_API_KEY", "Server API key", "secret",
              "Needed for cloud services and for servers started with --api-key. "
              "Leave empty for an open local server.", group='openai'),
        Field("EDIT_LLM_NO_THINK", "Skip thinking (reasoning)", "toggle",
              "For thinking / reasoning models: answer directly instead of "
              "thinking first. Ignored by models that don't think.",
              group='openai', emphasis="Much faster per edit."),
        Field("_h_cleanup", "Dictation cleanup", "heading",
              "Optional: let the edit model above tidy every dictation (fillers, "
              "\"no wait, I mean\" corrections, spoken \"new paragraph\"), with no "
              "trigger phrase needed. Your phone can turn it on for itself separately."),
        Field("CLEANUP_ALWAYS", "Clean up every dictation", "toggle",
              "Uses the AI model above, so it also removes um/uh (the \"Remove filler "
              "sounds\" switch under Transcription is the no-AI version). "
              "Adds about a second per dictation. Skipped when you say \"prompt AI\" "
              "(that already goes to the editor). If the editor can't be reached, "
              "you get your dictation uncleaned.",
              default="false"),
        Field("CLEANUP_PUNCTUATION", "Punctuation", "choice",
              "How much punctuation the cleaned text gets.",
              labelled_choices=[("Standard", "standard"),
                                ("Light (no final period, fewer commas)", "light"),
                                ("Minimal (lowercase, almost none)", "minimal")],
              default="standard"),
        Field("CLEANUP_REWRITE", "Rewriting", "choice",
              "How much the editor may change your wording.",
              labelled_choices=[("Keep my words (only remove fillers)", "keep"),
                                ("Fix grammar", "fix"),
                                ("Polish (clearer, a bit more formal)", "polish")],
              default="keep"),
        Field("CLEANUP_TONE", "Tone", "choice",
              "Used with Fix grammar or Polish. (With Keep my words your own tone "
              "is kept, since changing it means rewording.) For a longer version, "
              "say \"prompt AI, make this longer\" instead.",
              labelled_choices=[("My own tone", "neutral"), ("Professional", "professional"),
                                ("Casual", "casual"), ("Friendly", "friendly")],
              default="neutral"),
        Field("CLEANUP_SHORTEN", "Conciseness", "choice",
              "Whether to condense what you said.",
              labelled_choices=[("Full length (keep everything)", "off"),
                                ("Shorter (cut repetition)", "light"),
                                ("Shortest (short summary)", "strong")],
              default="off"),
        Field("CLEANUP_EMOJI", "Allow an emoji", "toggle",
              "Lets the editor add one fitting emoji to casual messages.",
              default="false"),
    ]),
    ("Mean Mode", [
        Field("MEAN_BACKEND", "Mean-mode AI", "choice",
              "Which model writes roasts (\"prompt roast\", \"prompt TikTok\", "
              "\"prompt disses\"). Any server works; a model that won't refuse helps.",
              labelled_choices=[("Ollama (this PC or LAN)", "ollama"),
                                ("Same as AI Editing", "edit"),
                                ("OpenAI-compatible server or cloud", "openai")],
              default="ollama"),
        Field("OLLAMA_API_URL", "Mean-mode Ollama endpoint", "text",
              "Ollama on this PC or your LAN.", group="mean_ollama"),
        Field("OLLAMA_ROAST_MODEL", "Mean-mode model", "pick", "x", group="mean_ollama"),
        Field("MEAN_LLM_BASE_URL", "Server base URL", "otext",
              "Any OpenAI-compatible server, ending in /v1: llama.cpp, vLLM, LM Studio "
              "on your LAN, or a cloud service.", group="mean_openai"),
        Field("MEAN_LLM_MODEL", "Server model", "otext",
              "The model name that server expects.", group="mean_openai"),
        Field("MEAN_LLM_API_KEY", "Server API key", "secret",
              "Needed for cloud services and servers started with --api-key.",
              group="mean_openai"),
        Field("MEAN_LLM_NO_THINK", "Skip thinking (reasoning)", "toggle",
              "For thinking models: answer directly. Much faster.",
              default="true", group="mean_openai"),
    ]),
    ("Audio Corpus", [
        Field("LOG_TRANSCRIPTS", "Keep a log of transcripts", "toggle",
              "Saves the text of every dictation to api/transcripts.log, to tune "
              "triggers and prompts against your real speech.", default="false"),
        Field("KEEP_AUDIO", "Keep source clips", "toggle",
              "Archives the original audio so model A/B tests can replay real "
              "dictation instead of synthetic audio.", default="false"),
        Field("KEEP_AUDIO_MAX", "Rolling clip limit", "int",
              "Ordinary clips kept before the oldest is pruned. keep/ is never pruned."),
        Field("KEEP_AUDIO_HARD_MAX", "Hard-clip limit", "int",
              "Cap on auto-collected low-confidence clips in hard/."),
        Field("KEEP_AUDIO_PIN_BELOW", "Hard-clip threshold", "float",
              "Length-adjusted confidence at or below this is filed as hard. "
              "-0.25 is roughly the worst 1 clip in 20."),
        Field("CORPUS_TAG_TRIGGERS", "Spoken tags", "list",
              "Say one of these while dictating and the clip is filed permanently "
              "in keep/, named with the phrase. One per line."),
    ]),
    ("Denoise", [
        Field("DENOISE_ENABLED", "Dual-pass denoise", "toggle",
              "Transcribes each clip raw AND denoised, then keeps whichever pass the "
              "decoder was more confident about. Roughly doubles transcription time."),
        Field("DENOISE_ATTEN_LIMIT_DB", "Attenuation limit (dB)", "int",
              "100 is full strength. Lower mixes some original signal back in."),
    ]),
    ("Startup", [
        Field("_h_server", "Transcription server", "heading",
              "The background program that turns audio into text.", group="src_local_startup"),
        Field("WHISPER_API_PORT", "API port", "port",
              "The network port the server listens on. 5000 is a common choice; "
              "change it only if another program already uses 5000. Save & "
              "Restart API applies it everywhere. Re-pair your phone afterwards.",
              default="5000", group="src_local_startup"),
        Field("GAME_MODE", "Free GPU memory while gaming", "toggle",
              "When a game runs full screen for 15 seconds, dictation pauses and the "
              "speech model leaves the graphics card; it comes back 10 seconds after "
              "you stop. Full-screen video in a browser or player doesn't count. "
              "Takes effect within seconds, no restart.",
              default="false", group="src_local_startup"),
        Field("GAME_PROCESSES", "Also pause for these programs", "otext",
              "Optional, for games that run in a window: program names separated by "
              "commas, e.g. minecraft.exe, blender.exe.", group="src_local_startup"),
        Field("RESTART_INTERVAL_HOURS", "Scheduled restart (hours)", "int",
              "x", group="src_local_startup"),  # help set live by _refresh_restart_help()
        Field("API_VISIBLE", "LAUNCHER.bat: show server window", "toggle",
              "Only matters if you start things with scripts/LAUNCHER.bat "
              "instead of the background service. On: LAUNCHER runs the server "
              "in a visible console window, handy for watching it work. Off: "
              "LAUNCHER starts the background service instead.",
              default="false", group="src_local_startup"),
        Field("_h_listener", "Hotkey listener", "heading",
              "The small program behind the tray icon that records when you "
              "press the hotkey and pastes the text."),
        Field("LISTENER_RESTART_INTERVAL_MINS", "Refresh after idle (mins)", "int",
              "After this many minutes without dictation, the listener quietly "
              "restarts itself. This keeps the hotkey from going dead after "
              "long idle periods."),
        Field("LISTEN_DELAY_SECONDS", "LAUNCHER.bat: wait before listener (secs)", "int",
              "Only used by scripts/LAUNCHER.bat: how long it waits for the "
              "server to load the model before starting the listener."),
    ]),
    ("Hotkey", [
        Field("CLIPBOARD_PRIVATE", "Keep dictations out of clipboard history", "toggle",
              "Text reaches your app through the clipboard. On: Windows clipboard "
              "history (Win+V) and cloud clipboard skip your dictations, the way "
              "password managers do. May stop pasting into Chrome Remote Desktop. "
              "Applies to the next dictation.", default="false"),
        Field("HOTKEY", "Dictation hotkey", "hotkey",
              "Pick a preset, or choose Custom... and type any key or combo "
              "(e.g. ctrl+alt+d). Keys that type nothing (F9, Pause, Ctrl+Alt+Space) "
              "are best: the backtick types a ` that has to be deleted before "
              "pasting. Applies after Restart Listener in the tray.",
              # PRESETS are (key, label); the dropdown wants (label, value).
              labelled_choices=[(lbl, key) for key, lbl in hotkey_config.PRESETS],
              default=hotkey_config.DEFAULT_HOTKEY),
        Field("HOTKEY_MODE", "Mode", "choice",
              "Hold to talk records while the key is held down. It needs a key that "
              "types nothing, so it can't be used with the backtick.",
              labelled_choices=[("Tap to start, tap to stop", "toggle"),
                                ("Hold to talk", "hold")],
              default="toggle"),
    ]),
    # Custom panel (see _build_phone_panel): pairing QR, API key, app downloads.
    ("Phone", []),
]

SECTION_INTROS = {
    "Transcription": "The speech-to-text engine (Whisper). It runs on this PC, "
                     "and both the hotkey and your phone send audio to it.",
    "Words & Triggers": "Names and terms Whisper should spell your way. Used for "
                        "every dictation, from this PC and from your phone.",
    "AI Editing (LLM)": "Optional. Say \"prompt AI\" and an instruction (e.g. "
                        "\"make this more formal\") and a language model rewrites "
                        "your dictation. Plain dictation never uses it.",
    "Mean Mode": "Optional, for fun: \"prompt roast\", \"prompt TikTok\" or "
                 "\"prompt disses\" sends your dictation to an uncensored model.",
    "Audio Corpus": "Optional, off unless you turn it on. Keeps your transcripts and "
                    "copies of your recordings on this PC so models can be compared on "
                    "your real voice. Nothing is uploaded.",
    "Denoise": "Optional. Cleans background noise before transcribing. Most "
               "microphones don't need it.",
    "Startup": "How the server and the hotkey listener start and stay healthy. "
               "Most people never need to change these.",
    "Hotkey": "The key you press on this PC to dictate into any app.",
    "Phone": "Use your Android phone as a microphone for this PC's server.",
}


# --- Phone pairing ---------------------------------------------------------
# Contract: Whisper-Unchained-Android contracts/01-transcription-api.md §7.

PAIRING_VERSION = 1
APP_REPO_URL = "https://github.com/Dominic-Shirazi/Whisper-Unchained-Android"
APP_RELEASES_URL = APP_REPO_URL + "/releases"
SERVER_REPO_URL = "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server"
# No listing yet. Set this once the app is live on Google Play.
PLAY_STORE_URL = ""


def generate_api_key():
    """Installer-format key: wr_sec_ + 32 url-safe random characters."""
    return "wr_sec_" + secrets.token_urlsafe(24)


def pairing_payload(host, api_key, model, name, port=5000, cert_sha256=""):
    """The JSON the phone scans. api_key is OMITTED (not empty) with auth off.

    With HTTPS (cert_sha256 given) it's version 2: an https:// address plus the
    certificate fingerprint the phone pins (Android contract 06).
    """
    scheme = "https" if cert_sha256 else "http"
    payload = {"v": 2 if cert_sha256 else PAIRING_VERSION,
               "base_url": "%s://%s:%d/v1" % (scheme, host, port),
               "model": model, "name": name}
    if cert_sha256:
        payload["cert_sha256"] = cert_sha256
    if api_key:
        payload["api_key"] = api_key
    return json.dumps(payload, separators=(",", ":"))


def detect_hosts():
    """Addresses the phone could reach this PC on, best first:
    the Tailscale 100.x address, then LAN IPv4s. Never raises."""
    hosts = []
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                                 timeout=2, creationflags=0x08000000)  # no console
            hosts += [l.strip() for l in out.stdout.splitlines() if l.strip()]
            if hosts:
                break
        except Exception:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in hosts:
                hosts.append(ip)
    except Exception:
        pass
    return hosts or ["127.0.0.1"]


def qr_matrix(data):
    """QR modules as rows of booleans, or None if qrcode isn't installed."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


# --- .env I/O --------------------------------------------------------------

def read_env():
    """Parse .env into {key: value}. Commented-out lines are deliberately ignored."""
    values = {}
    if not os.path.exists(ENV_PATH):
        return values
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def write_env(updates):
    """Replace values in place, preserving every comment, blank line and ordering.

    Only lines that are already live assignments are rewritten -- a commented
    `# OLLAMA_EDIT_API_URL=...` fallback stays commented, because those are kept
    on purpose as one-line revert switches. Written via a temp file + os.replace
    so an interrupted save cannot leave a half-written .env behind.
    """
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = "%s=%s%s" % (key, updates[key], newline)
            seen.add(key)

    # A key in the schema but absent from .env (someone deleted the line) is
    # appended rather than dropped, so saving cannot quietly lose a setting.
    missing = [k for k in updates if k not in seen]
    if missing:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# Added by the settings window\n")
        lines.extend("%s=%s\n" % (k, updates[k]) for k in missing)

    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, ENV_PATH)


ICON_ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "app_icon.ico")
LOGO_PNG = os.path.join(os.path.dirname(ICON_ICO), "app_icon.png")


def load_logo(root, size: int = 26):
    """The waveform logo as a Tk image, `size` px at 100% scaling (bigger on
    high-DPI screens so it matches the text). None if it can't load: the
    header still shows the name, so a missing logo is never an error."""
    try:
        from PIL import Image, ImageTk
        px = round(size * root.winfo_fpixels("1i") / 96)
        img = Image.open(LOGO_PNG).convert("RGBA").resize((px, px), Image.LANCZOS)
        return ImageTk.PhotoImage(img, master=root)
    except Exception:
        return None


def set_window_icon(root):
    """Title bar + taskbar icon: the app's waveform logo (shared with Android)."""
    try:
        root.iconbitmap(default=ICON_ICO)
    except Exception:
        pass


def scheduled_restart_installed():
    """True if install_service.ps1's 'Restart FasterWhisperAPI' task exists."""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", "Restart FasterWhisperAPI"],
                           capture_output=True, timeout=3,
                           creationflags=0x08000000)  # no console window
        return r.returncode == 0
    except Exception:
        return False


def _infer_edit_backend(values):
    """Older .env files have no EDIT_BACKEND: the server picks OpenAI-compatible
    whenever EDIT_LLM_BASE_URL is set, so show that same choice."""
    if not values.get("EDIT_BACKEND"):
        values["EDIT_BACKEND"] = "openai" if values.get("EDIT_LLM_BASE_URL") else "ollama"


def api_port(values):
    raw = (values.get("WHISPER_API_PORT") or "").strip()
    return int(raw) if raw.isdigit() else 5000


def installed_ollama_sizes(base):
    """{model name: size in bytes} on the Ollama at base, or None if it can't be
    reached. Ollama cloud models report size 0 (they use no local VRAM)."""
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            data = json.load(r)
        return {m["name"]: m.get("size", 0) for m in data.get("models", [])}
    except Exception:
        return None


def installed_ollama_models(url):
    """Best-effort list of local Ollama models, for the model dropdowns.

    Never blocks the UI for long and never raises: if Ollama is down or remote,
    the dropdown falls back to free text with the current value intact.
    """
    try:
        import urllib.request
        base = re.sub(r"/api/.*$", "", url or "")
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            data = json.load(r)
        return sorted(m["name"] for m in data.get("models", []))
    except Exception:
        return []


# --- Widgets ---------------------------------------------------------------

class Toggle(tk.Canvas):
    """A pill switch. ttk.Checkbutton cannot be themed dark on Windows without
    fighting the native renderer, and a checkbox reads as a form; a switch reads
    as a setting that takes effect."""

    W, H = 46, 24

    def __init__(self, parent, variable, command=None):
        super().__init__(parent, width=self.W, height=self.H, bg=BG,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.command = command
        self._pos = 1.0 if variable.get() else 0.0
        self.bind("<Button-1>", self._click)
        # Redraw whenever the value changes, not only on a click: two switches
        # can share one setting (the wizard's Install page and the Startup page).
        self._trace = variable.trace_add("write", lambda *_: self._animate())
        self.bind("<Destroy>", self._untrace)
        self._draw()

    def _untrace(self, _event=None):
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def _click(self, _event=None):
        self.var.set(not self.var.get())  # the trace animates it
        if self.command:
            self.command()

    def _animate(self, step=0):
        target = 1.0 if self.var.get() else 0.0
        self._pos += (target - self._pos) * 0.35
        self._draw()
        if step < 12 and abs(target - self._pos) > 0.01:
            self.after(12, lambda: self._animate(step + 1))
        else:
            self._pos = target
            self._draw()

    def _draw(self):
        self.delete("all")
        r = self.H // 2
        track = self._blend("#39404E", ACCENT, self._pos)
        # A pill is two end caps plus the span between them.
        self.create_oval(0, 0, self.H, self.H, fill=track, outline=track)
        self.create_oval(self.W - self.H, 0, self.W, self.H, fill=track, outline=track)
        self.create_rectangle(r, 0, self.W - r, self.H, fill=track, outline=track)
        knob_x = 3 + self._pos * (self.W - self.H)
        self.create_oval(knob_x, 3, knob_x + self.H - 6, self.H - 3,
                         fill="#F2F4F6", outline="")

    @staticmethod
    def _blend(a, b, t):
        a = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Button(tk.Label):
    """Flat label-as-button. tk.Button on Windows draws a raised 3D chrome that
    no amount of configuration removes."""

    def __init__(self, parent, text, command, kind="ghost"):
        self.kind = kind
        colors = {
            "primary": (ACCENT, "#0E1418", ACCENT_H),
            "ghost":   (RAISED, TEXT, "#2B313C"),
        }[kind]
        self.bg, self.fg, self.hover = colors
        super().__init__(parent, text=text, font=F_BUTTON, bg=self.bg, fg=self.fg,
                         padx=18, pady=9, cursor="hand2")
        self.command = command
        self._enabled = True
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, _e):
        if self._enabled:
            self.config(bg=self.hover)

    def _on_leave(self, _e):
        if self._enabled:
            self.config(bg=self.bg)

    def _click(self, _event=None):
        if self._enabled and self.command:
            self.command()

    def set_enabled(self, on):
        self._enabled = on
        self.config(fg=self.fg if on else MUTED,
                    bg=self.bg if on else RAISED,
                    cursor="hand2" if on else "arrow")


class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.values = read_env()
        _infer_edit_backend(self.values)
        self.dirty = False
        self.fields = {}

        root.title("Whisper Unchained Settings")
        set_window_icon(root)
        root.configure(bg=BG)
        root.geometry("1000x760")
        root.minsize(860, 640)
        self._style()

        self._build_header()
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True)
        self._build_footer()
        self._build_sidebar(body)
        self._build_content(body)

        self.show_section(SCHEMA[0][0])
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")   # the only built-in theme that honours custom colors
        except tk.TclError:
            pass
        s.configure("TCombobox", fieldbackground=RAISED, background=RAISED,
                    foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=RAISED, darkcolor=RAISED, borderwidth=0,
                    padding=7)
        s.map("TCombobox",
              fieldbackground=[("readonly", RAISED)],
              foreground=[("readonly", TEXT)],
              arrowcolor=[("active", ACCENT)])
        # The dropdown list is a Tk Listbox, unreachable from ttk styling.
        self.root.option_add("*TCombobox*Listbox.background", RAISED)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#0E1418")
        self.root.option_add("*TCombobox*Listbox.font", F_BODY)
        s.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED, borderwidth=0)
        # Drop the stepper arrows entirely -- clam draws them as chunky 3D
        # buttons that date the whole window. Layout surgery is the only way;
        # they are not a configurable option.
        try:
            s.layout("Vertical.TScrollbar", [
                ("Vertical.Scrollbar.trough", {
                    "sticky": "ns",
                    "children": [("Vertical.Scrollbar.thumb",
                                  {"expand": "1", "sticky": "nswe"})],
                }),
            ])
        except tk.TclError:
            pass

    def _build_header(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=28, pady=(18, 14))
        # App name + logo above the page title.
        brand = tk.Frame(head, bg=BG)
        brand.pack(anchor="w", pady=(0, 10))
        self._logo = load_logo(self.root)   # keep a reference or Tk drops it
        if self._logo:
            tk.Label(brand, image=self._logo, bg=BG).pack(side="left", padx=(0, 8))
        tk.Label(brand, text="Whisper Unchained", font=F_BRAND, bg=BG,
                 fg=ACCENT).pack(side="left")
        tk.Label(head, text="Settings", font=F_TITLE, bg=BG, fg=TEXT).pack(anchor="w")

    def _build_sidebar(self, parent):
        self.sidebar = tk.Frame(parent, bg=SURFACE, width=180)
        self.sidebar.pack(side="left", fill="y", padx=(28, 0))
        self.sidebar.pack_propagate(False)
        self.nav = {}
        for name, _ in SCHEMA:
            row = tk.Frame(self.sidebar, bg=SURFACE, cursor="hand2")
            row.pack(fill="x")
            bar = tk.Frame(row, bg=SURFACE, width=3)
            bar.pack(side="left", fill="y")
            lbl = tk.Label(row, text=name, font=F_TAB, bg=SURFACE, fg=MUTED,
                           anchor="w", padx=14, pady=11)
            lbl.pack(side="left", fill="x", expand=True)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, n=name: self.show_section(n))
            self.nav[name] = (row, bar, lbl)

    def _build_content(self, parent):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(side="left", fill="both", expand=True, padx=(0, 8))

        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview,
                               style="Vertical.TScrollbar")
        self.inner = tk.Frame(canvas, bg=BG)
        self.inner.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.canvas = canvas

        self.frames = {}
        self.groups = {}
        models = installed_ollama_models(self.values.get("OLLAMA_API_URL", ""))
        for name, fields in SCHEMA:
            frame = tk.Frame(self.inner, bg=BG)
            holder = None
            intro = SECTION_INTROS.get(name)
            if intro:
                lbl = tk.Label(frame, text=intro, font=F_BODY, bg=BG, fg=MUTED,
                               anchor="w", justify="left", wraplength=560)
                lbl.pack(anchor="w", padx=28, pady=(4, 2))
                self.intro_labels = getattr(self, "intro_labels", {})
                self.intro_labels[name] = lbl
            for f in fields:
                target = frame
                if f.group:
                    if holder is None:
                        holder = tk.Frame(frame, bg=BG)
                        holder.pack(fill="x")
                    if f.group not in self.groups:
                        self.groups[f.group] = tk.Frame(holder, bg=BG)
                    target = self.groups[f.group]
                if f.kind == "heading":
                    # A sub-heading, not a setting: never saved.
                    self._phone_heading(target, f.label, f.help)
                    continue
                self._build_field(target, f, models)
                self.fields[f.key] = f
            if name == "Phone":
                self._build_phone_panel(frame)
            if name == "Words & Triggers":
                self._build_words_panel(frame)
            if name == "Transcription":
                self._build_model_picker(self.groups["src_local"])
            if name == "AI Editing (LLM)":
                self._build_llm_finder(frame)
            if name == "Startup":
                self._build_integration_panel(frame)
            if name == "Audio Corpus":
                self._build_history_panel(frame)
                self._build_source_extras()
            self.frames[name] = frame
        self._setup_ai_help()
        self._refresh_restart_help()
        self.fields["TRANSCRIBE_SOURCE"].var.trace_add("write", lambda *_: self._refresh_source())
        self.fields["CLOUD_PROVIDER"].var.trace_add("write", lambda *_: self._apply_cloud_preset())
        self._refresh_source()

    # --- Transcription source ------------------------------------------------------

    def _build_source_extras(self):
        """A 'Test connection' button under the remote and cloud settings."""
        self._conn_labels = {}
        for group in ("src_remote", "src_cloud"):
            row = tk.Frame(self.groups[group], bg=BG)
            row.pack(fill="x", padx=28, pady=(14, 0))
            Button(row, "Test connection", self._test_connection).pack(side="left")
            lbl = tk.Label(row, text="", font=F_HELP, bg=BG, fg=MUTED)
            lbl.pack(side="left", padx=(12, 0))
            self._conn_labels[group] = lbl

    def _current_env(self):
        updates, _err = self.collect()
        return dict(self.values, **(updates or {}))

    def _test_connection(self):
        env = self._current_env()
        ok, msg = transcribe_client.check_connection(env)
        lbl = self._conn_labels.get("src_" + transcribe_client.source(env))
        if lbl is not None:
            lbl.config(text=("\u2713 " if ok else "\u2717 ") + msg, fg=ACCENT if ok else DANGER)

    def _apply_cloud_preset(self):
        """Fill the address and model from the chosen provider, without
        overwriting something the user typed for a custom service."""
        key = self._value("CLOUD_PROVIDER")
        _label, url, model = transcribe_client.CLOUD_PRESETS.get(key, ("", "", ""))
        known_urls = {u for _l, u, _m in transcribe_client.CLOUD_PRESETS.values() if u}
        known_models = {m for _l, _u, m in transcribe_client.CLOUD_PRESETS.values() if m}
        if url and self._value("CLOUD_BASE_URL") in known_urls | {""}:
            self.fields["CLOUD_BASE_URL"].var.set(url)
        if model and self._value("CLOUD_MODEL") in known_models | {""}:
            self.fields["CLOUD_MODEL"].var.set(model)

    def _refresh_source(self):
        src = self._value("TRANSCRIBE_SOURCE") or "local"
        for name, frame in self.groups.items():
            if not name.startswith("src_"):
                continue
            if name.split("_")[1] == src:
                if not frame.winfo_manager():
                    frame.pack(fill="x")
            else:
                frame.pack_forget()
        hidden = set(LOCAL_ONLY_SECTIONS) if src != "local" else set()
        if src == "remote":
            hidden.add("Words & Triggers")  # the words live on that PC
        self._hidden_sections = hidden
        for name, _f in SCHEMA:
            if name in self.nav:
                self.nav[name][0].pack_forget()
        for name, _f in SCHEMA:
            if name in self.nav and name not in hidden:
                self.nav[name][0].pack(fill="x")
        if getattr(self, "current_section", None) in hidden:
            self.show_section("Transcription")
        if src == "cloud" and not self._value("CLOUD_BASE_URL"):
            self._apply_cloud_preset()
        # The page intro, the filler switch and the footer button follow the choice.
        if "Transcription" in getattr(self, "intro_labels", {}):
            self.intro_labels["Transcription"].config(text=SOURCE_INTROS[src])
        fill = self.fields.get("REMOVE_FILLERS")
        if fill is not None and getattr(fill, "rows", None):
            row, sep = fill.rows
            if src == "remote":   # that PC's own setting applies
                row.pack_forget(); sep.pack_forget()
            elif not row.winfo_manager():
                row.pack(fill="x", padx=28, pady=(16, 0))
                sep.pack(fill="x", padx=28, pady=(16, 0))
        if hasattr(self, "btn_restart") and self.btn_restart is not getattr(self, "btn_save", None):
            if src == "local":
                if not self.btn_restart.winfo_manager():
                    self.btn_restart.pack(side="right", before=self.btn_save)
            else:
                self.btn_restart.pack_forget()   # no server on this PC to restart

    def _refresh_restart_help(self):
        """The scheduled restart only exists if install_service.ps1 created
        its task, so say which is true on this PC instead of guessing."""
        f = self.fields.get("RESTART_INTERVAL_HOURS")
        if not f or f.help_label is None:
            return
        if scheduled_restart_installed():
            text = ("Restarts the server every this many hours to clear GPU "
                    "memory. Changing it takes effect when "
                    "scripts/install_service.ps1 is run again.")
        else:
            text = ("NOT ACTIVE: no scheduled restart is set up on this PC, so the "
                    "server just keeps running (which is fine). Running "
                    "scripts/install_service.ps1 as Administrator sets one up.")
        self._set_help("RESTART_INTERVAL_HOURS", text)

    # --- AI Editing / Mean Mode: help that follows the current choices --------

    def _setup_ai_help(self):
        self._ollama_cache = {}
        keys = ("EDIT_BACKEND", "OLLAMA_EDIT_API_URL", "OLLAMA_MODEL",
                "OLLAMA_EDIT_KEEP_ALIVE", "OLLAMA_API_URL", "OLLAMA_ROAST_MODEL",
                "MEAN_BACKEND",
                "EDIT_LLM_BASE_URL")
        for k in keys:
            f = self.fields.get(k)
            if f and f.var is not None:
                f.var.trace_add("write", lambda *_: self._refresh_ai_help())
        self._refresh_ai_help()

    # --- Delete my history (Audio Corpus page) ------------------------------------

    def _history_paths(self):
        api = os.path.join(PROJECT_DIR, "api")
        return ([os.path.join(api, n) for n in ("transcripts.log", "fastpath.log")],
                os.path.join(api, "audio_corpus"))

    def _history_summary(self):
        logs, corpus = self._history_paths()
        size = sum(os.path.getsize(p) for p in logs if os.path.exists(p))
        clips = 0
        for base, _dirs, files in os.walk(corpus):
            for n in files:
                clips += 1
                size += os.path.getsize(os.path.join(base, n))
        return clips, size

    def _build_history_panel(self, frame):
        self._phone_heading(frame, "Your history",
                            "Everything saved above stays on this PC. Delete it any "
                            "time; the switches above decide what's saved from now on.")
        row = tk.Frame(frame, bg=BG)
        row.pack(fill="x", padx=28, pady=(10, 0))
        self._history_label = tk.Label(row, text="", font=F_HELP, bg=BG, fg=MUTED,
                                       anchor="w")
        self._history_label.pack(side="left")
        Button(row, "Delete my history", self.delete_history).pack(side="right")
        self._refresh_history()

    def _refresh_history(self, note=""):
        clips, size = self._history_summary()
        self._history_label.config(text=note or "Saved now: %d recording%s and logs, "
                                   "%.1f MB." % (clips, "" if clips == 1 else "s",
                                                 size / 1048576))

    def delete_history(self, confirm=None):
        from tkinter import messagebox
        ask = confirm or (lambda: messagebox.askyesno(
            "Delete my history",
            "Delete all saved transcripts and recordings on this PC, including "
            "clips you tagged to keep? This can't be undone.", parent=self.root))
        if not ask():
            return
        logs, corpus = self._history_paths()
        failed = 0
        for p in logs:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                failed += 1
        for base, _dirs, files in os.walk(corpus):
            for n in files:
                try:
                    os.remove(os.path.join(base, n))
                except OSError:
                    failed += 1
        self._refresh_history("Deleted." if not failed else
                              "Deleted, except %d file%s in use; try again in a moment."
                              % (failed, "" if failed == 1 else "s"))

    # --- Windows integration (Startup page) -------------------------------------
    # Both are per-user and need no admin. The installer offers them too; these
    # buttons add or remove them later.

    INTEGRATIONS = [
        ("menu", "Right-click \"Transcribe to Clipboard\"",
         "Right-click any audio or video file in File Explorer to copy its text.",
         "install_context_menu.ps1"),
        ("login", "Start when I log in",
         "Starts the tray icon and hotkey when you sign in to Windows.",
         "install_listener_startup.ps1"),
    ]

    def _integration_on(self, key):
        if key == "menu":
            import winreg
            try:
                # The merged view: also sees an entry added for all users by older versions.
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                               r"*\shell\TranscribeToClipboard"))
                return True
            except OSError:
                return False
        startup = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu"
                               r"\Programs\Startup", "FasterWhisperListener.lnk")
        return os.path.exists(startup)

    def _build_integration_panel(self, frame):
        self._phone_heading(frame, "Windows integration")
        self._integration_rows = {}
        for key, title, text, _script in self.INTEGRATIONS:
            row = tk.Frame(frame, bg=BG)
            row.pack(fill="x", padx=28, pady=(10, 0))
            btn = Button(row, "", lambda k=key: self._toggle_integration(k))
            btn.pack(side="right")
            tk.Label(row, text=title, font=F_LABEL, bg=BG, fg=TEXT, anchor="w").pack(anchor="w")
            status = tk.Label(row, text=text, font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                              justify="left", wraplength=420)
            status.pack(anchor="w")
            self._integration_rows[key] = (btn, status, text)
            self._refresh_integration(key)

    def _refresh_integration(self, key, note=""):
        btn, status, text = self._integration_rows[key]
        on = self._integration_on(key)
        btn.config(text="Remove" if on else "Add")
        status.config(text=(note or text) + ("  (on)" if on and not note else ""))

    def _toggle_integration(self, key):
        script = next(sc for k, _t, _x, sc in self.INTEGRATIONS if k == key)
        remove = self._integration_on(key)
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               os.path.join(PROJECT_DIR, "scripts", script), "-NoPause"] + (["-Remove"] if remove else [])
        env = dict(os.environ)
        env.pop("PSModulePath", None)  # started from PowerShell 7, 5.1 would lose cmdlets
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env,
                               creationflags=0x08000000)
            ok = r.returncode == 0
        except Exception:
            ok = False
        note = ""
        if not ok:
            note = "That didn't work. Run Setup again, or see installer/logs."
        elif remove and self._integration_on(key):
            note = ("Still on: it was added for all users by an older version. Remove it "
                    "with Setup, or ask an administrator.")
        self._refresh_integration(key, note)

    # --- Find AI servers ---------------------------------------------------------

    def _build_llm_finder(self, frame):
        """A box at the top of AI Editing: looks for LLM servers already running
        (this PC, or another PC the user names) and fills in the fields."""
        box = tk.Frame(frame, bg=SURFACE)
        intro = getattr(self, "intro_labels", {}).get("AI Editing (LLM)")
        if intro is not None:
            box.pack(fill="x", padx=28, pady=(10, 6), after=intro)
        else:
            box.pack(fill="x", padx=28, pady=(10, 6), before=frame.winfo_children()[0])
        tk.Label(box, text="Find AI servers", font=F_BUTTON, bg=SURFACE, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=14, pady=(10, 0))
        tk.Label(box, text="Looks on this PC for Ollama, LM Studio, llama.cpp, vLLM, "
                 "Unsloth Studio, Jan and KoboldCpp. To look on another PC, type its "
                 "address (add :port for a server on an unusual port).",
                 font=F_HELP, bg=SURFACE, fg=MUTED, anchor="w", justify="left",
                 wraplength=540).pack(anchor="w", padx=14, pady=(2, 0))
        row = tk.Frame(box, bg=SURFACE)
        row.pack(fill="x", padx=14, pady=(8, 0))
        self.llm_other = tk.StringVar(box)
        entry = tk.Entry(row, textvariable=self.llm_other, font=F_BODY, bg=RAISED, fg=TEXT,
                         insertbackground=ACCENT, relief="flat", width=28)
        entry.pack(side="left", ipady=6)
        entry.bind("<Return>", lambda _e: self.find_llm_servers())
        Button(row, "Look for AI servers", self.find_llm_servers).pack(side="left", padx=(10, 0))
        self.llm_results = tk.Frame(box, bg=SURFACE)
        self.llm_results.pack(fill="x", padx=14, pady=(6, 0))
        tips = "Suggested models:\n" + "\n".join(
            "  %s: %s" % (m, note) for m, note in llm_detect.SUGGESTED_MODELS)
        tk.Label(box, text=tips, font=F_HELP, bg=SURFACE, fg=MUTED, anchor="w",
                 justify="left", wraplength=540).pack(anchor="w", padx=14, pady=(6, 10))
        self.llm_found = None
        self.find_llm_servers()   # this PC only, in the background: about a second

    def find_llm_servers(self):
        other = self.llm_other.get().strip()
        skip = (api_port(self.values),)
        self._llm_message("Looking...")

        # The search runs on a thread; Tk may only be touched from this one, so
        # the result comes back through a holder that the window checks.
        result = []

        def work():
            try:
                found = llm_detect.detect(skip_ports=skip)
                if other:
                    found += llm_detect.detect_target(other)
            except Exception:
                found = []
            result.append(found)

        def check():
            try:
                if result:
                    self._show_llm_found(result[0], other)
                else:
                    self.root.after(100, check)
            except tk.TclError:
                pass  # window closed meanwhile
        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, check)

    def _llm_message(self, text, color=MUTED):
        for w in self.llm_results.winfo_children():
            w.destroy()
        if text:
            tk.Label(self.llm_results, text=text, font=F_HELP, bg=SURFACE, fg=color,
                     anchor="w", justify="left", wraplength=540).pack(anchor="w")

    def _show_llm_found(self, found, other=""):
        try:
            if not self.llm_results.winfo_exists():
                return
        except tk.TclError:
            return
        self.llm_found = found
        if not found:
            where = "this PC" + (" or " + other if other else "")
            self._llm_message("No AI server found on %s. Ollama (ollama.com) and LM Studio "
                              "(lmstudio.ai) are free and easy to start with." % where)
        else:
            self._llm_message("")
            for f in found:
                line = tk.Frame(self.llm_results, bg=SURFACE)
                line.pack(fill="x", pady=1)
                model = llm_detect.best_model(f.models)
                text = f.label() + ("  ·  would use %s" % model if model else "")
                tk.Label(line, text=text, font=F_HELP, bg=SURFACE, fg=TEXT,
                         anchor="w").pack(side="left")
                use = tk.Label(line, text="  Use this", font=F_LABEL, bg=SURFACE,
                               fg=ACCENT, cursor="hand2")
                use.pack(side="left")
                use.bind("<Button-1>", lambda _e, f=f: self.use_llm_server(f))
        self.on_llm_found(found)

    def on_llm_found(self, found):
        """Hook for the setup wizard."""

    def use_llm_server(self, found, model=None):
        """Fill the AI Editing fields from a detected server."""
        model = model or llm_detect.best_model(found.models) or ""
        def label(value):
            return next(l for l, v in EDIT_BACKEND_CHOICES if v == value)
        if found.is_ollama:
            self.fields["EDIT_BACKEND"].var.set(label("ollama"))
            self.fields["OLLAMA_EDIT_API_URL"].var.set(
                "http://%s:%d/api/generate" % (found.host, found.port))
            if model:
                self.fields["OLLAMA_MODEL"].var.set(model)
        else:
            self.fields["EDIT_BACKEND"].var.set(label("openai"))
            self.fields["EDIT_LLM_BASE_URL"].var.set(found.base_url)
            if model:
                self.fields["EDIT_LLM_MODEL"].var.set(model)
        note = "Using %s%s." % (found.name, " with " + model if model else "")
        if found.needs_key:
            note += " It needs an API key: enter it below."
        elif not model:
            note += " Load a model in it, then pick it below."
        self._llm_message(note, ACCENT)

    def _show_group(self, name):
        """Show one backend's fields and hide the others. Hidden fields keep
        their values and are still saved, so switching back loses nothing."""
        family = "mean_" if name.startswith("mean_") else ""
        for g, frame in self.groups.items():
            if g.startswith("src_"):
                continue  # transcription-source groups: see _refresh_source
            if g.startswith("mean_") != bool(family):
                continue  # AI Editing and Mean Mode switch their own groups
            if g == name:
                if not frame.winfo_manager():  # not packed yet
                    frame.pack(fill="x")
            else:
                frame.pack_forget()

    def _value(self, key):
        """A field's current (unsaved) value, labels mapped back to values."""
        f = self.fields[key]
        raw = f.var.get().strip()
        return next((v for l, v in f.labelled_choices if l == raw), raw)

    def _ollama_sizes(self, url):
        """{model: bytes} on the Ollama at url, or None if unreachable. Cached
        per URL so typing in another field doesn't re-query on every keystroke."""
        base = re.sub(r"/api/.*$", "", url or "").rstrip("/")
        if base not in self._ollama_cache:
            self._ollama_cache[base] = installed_ollama_sizes(base)
        return self._ollama_cache[base]

    def _set_help(self, key, text):
        label = self.fields[key].help_label
        if label is not None:
            warn = text.startswith(("NOT ACTIVE", "⚠"))
            label.config(text=text, fg=WARN if warn else MUTED)

    def _model_note(self, url, model):
        """'' if installed, else a warning naming the problem."""
        sizes = self._ollama_sizes(url)
        if sizes is None:
            return " Couldn't reach Ollama at that endpoint to check the model."
        if model and model not in sizes:
            return None  # caller words the "not installed" warning
        return ""

    def _refresh_ai_help(self):
        if "EDIT_BACKEND" not in self.fields or not hasattr(self, "_ollama_cache"):
            return
        backend = self._value("EDIT_BACKEND")
        self._show_group("openai" if backend == "openai" else "ollama")
        if "MEAN_BACKEND" in self.fields:
            self._show_group("mean_" + self._value("MEAN_BACKEND"))   # mean_edit: none shown

        # Ollama edit fields.
        url = self._value("OLLAMA_EDIT_API_URL")
        model = self._value("OLLAMA_MODEL")
        note = self._model_note(url, model)
        base = "Model used to clean up and edit dictation."
        if note is None:
            base = ("⚠ '%s' isn't installed on that Ollama, so edits will fail. "
                    "Pick an installed one, or run: ollama pull %s" % (model, model))
        else:
            base += note
        self._set_help("OLLAMA_MODEL", base)

        sizes = self._ollama_sizes(url) or {}
        if model in sizes:
            gb = sizes[model] / 1e9
            cost = ("Staying loaded costs about %.1f GB of VRAM for %s (%.1f GB of "
                    "weights plus 1-3 GB working memory), but cuts a cold edit from "
                    "~5 s to under 1 s." % (gb + 2, model, gb))
        else:
            cost = ("Staying loaded costs roughly the model's file size plus 1-3 GB "
                    "of VRAM, but cuts a cold edit from ~5 s to under 1 s.")
        self._set_help("OLLAMA_EDIT_KEEP_ALIVE",
                       "How long Ollama holds the edit model after a request. " + cost)

        url_help = ("Any OpenAI-compatible server, ending in /v1: llama.cpp or LM "
                    "Studio on your LAN, or a cloud service (OpenRouter, OpenAI, Groq...).")
        if backend == "openai" and not self._value("EDIT_LLM_BASE_URL"):
            url_help = "⚠ Needed: with no URL, edits fall back to Ollama. " + url_help
        self._set_help("EDIT_LLM_BASE_URL", url_help)

        # Mean mode.
        rurl = self._value("OLLAMA_API_URL")
        rmodel = self._value("OLLAMA_ROAST_MODEL")
        rnote = self._model_note(rurl, rmodel)
        rtext = "Needs an uncensored model, or it will refuse to roast."
        if rnote is None:
            rtext = ("⚠ '%s' isn't installed on that Ollama, so mean mode fails "
                     "with 'model not found'. Pick an installed one, or run: "
                     "ollama pull %s" % (rmodel, rmodel))
        else:
            rtext += rnote
        self._set_help("OLLAMA_ROAST_MODEL", rtext)

    def _build_field(self, parent, field, models):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=28, pady=(16, 0))

        left = tk.Frame(row, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=field.label, font=F_LABEL, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w")
        field.help_label = None
        if field.help:
            helper = tk.Label(left, text=field.help, font=F_HELP, bg=BG, fg=MUTED,
                              anchor="w", justify="left", wraplength=360)
            helper.pack(anchor="w", pady=(3, 0))
            field.help_label = helper
            if field.emphasis:
                tk.Label(left, text=field.emphasis, font=F_HELP_I, bg=BG, fg=TEXT,
                         anchor="w").pack(anchor="w", pady=(2, 0))
            # NOT ACTIVE / dead-config warnings earn the amber; ordinary help does not.
            if field.help.startswith("NOT ACTIVE"):
                helper.config(fg=WARN)

        raw = self.values.get(field.key, "") or field.default

        if field.kind == "toggle":
            var = tk.BooleanVar(value=raw.strip().lower() in ("1", "true", "yes"))
            widget = Toggle(row, var, command=self.mark_dirty)
            widget.pack(side="right", padx=(20, 0))
        elif field.kind == "model":
            # The current model plus a link to the Models list, which does the
            # choosing. Hovering lights up that list so it's clear where to go.
            var = tk.StringVar(value=raw)
            widget = tk.Frame(row, bg=BG)
            widget.pack(side="right", padx=(20, 0))
            tk.Label(widget, textvariable=var, font=F_BUTTON, bg=BG,
                     fg=TEXT).pack(side="left")
            link = tk.Label(widget, text="  Choose below ↓", font=F_LABEL,
                            bg=BG, fg=ACCENT, cursor="hand2")
            link.pack(side="left")
            link.bind("<Button-1>", lambda e: self._scroll_to_models())
            for w in (row, widget, link):
                w.bind("<Enter>", lambda e: self._highlight_models(True), add="+")
                w.bind("<Leave>", lambda e: self._highlight_models(False), add="+")
            var.trace_add("write", lambda *_: self.mark_dirty())
        elif field.kind == "hotkey":
            # Labelled presets, but still typeable: a key that isn't a preset is
            # shown (and saved) as itself. "Custom..." clears the box for typing.
            options = [lbl for lbl, _ in field.labelled_choices] + [CUSTOM_LABEL]
            current = next((lbl for lbl, val in field.labelled_choices
                            if val == raw), raw)
            var = tk.StringVar(value=current)
            widget = ttk.Combobox(row, textvariable=var, values=options,
                                  font=F_BODY, state="normal", width=32)
            widget.pack(side="right", padx=(20, 0))

            def on_pick(_e, v=var, w=widget):
                if v.get() == CUSTOM_LABEL:
                    v.set("")
                    w.focus_set()
                    self.status.config(
                        text="Type a key or combo, e.g. f10 or ctrl+alt+d, then Save.",
                        fg=MUTED)
            widget.bind("<<ComboboxSelected>>", on_pick)
            var.trace_add("write", lambda *_: self.mark_dirty())
        elif field.kind in ("choice", "pick"):
            if field.labelled_choices:
                options = [lbl for lbl, _ in field.labelled_choices]
                current = next((lbl for lbl, val in field.labelled_choices
                                if val == raw), raw)
                state = "readonly"
            else:
                options = list(field.choices)
                # Live Ollama inventory beats a hardcoded list, when reachable.
                if field.kind == "pick" and models:
                    options = models
                if raw and raw not in options:
                    options.insert(0, raw)
                current = raw
                state = "normal"
            var = tk.StringVar(value=current)
            widget = ttk.Combobox(row, textvariable=var, values=options,
                                  font=F_BODY, state=state, width=32)
            widget.pack(side="right", padx=(20, 0))
            var.trace_add("write", lambda *_: self.mark_dirty())
        elif field.kind == "list":
            var = None
            widget = tk.Text(row, height=5, width=34, bg=RAISED, fg=TEXT,
                             font=F_BODY, relief="flat", insertbackground=ACCENT,
                             padx=8, pady=6, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT,
                             wrap="word")
            widget.insert("1.0", "\n".join(
                p.strip() for p in raw.split(",") if p.strip()))
            widget.bind("<KeyRelease>", lambda e: self.mark_dirty())
            widget.pack(side="right", padx=(20, 0))
        else:
            var = tk.StringVar(value=raw)
            widget = tk.Entry(row, textvariable=var, font=F_BODY, bg=RAISED,
                              fg=TEXT, relief="flat", insertbackground=ACCENT,
                              width=34, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=ACCENT,
                              borderwidth=6,
                              show="•" if field.kind == "secret" else "")
            widget.pack(side="right", padx=(20, 0))
            var.trace_add("write", lambda *_: self.mark_dirty())

        field.var = var
        field.widget = widget
        sep = tk.Frame(parent, bg=BORDER, height=1)
        sep.pack(fill="x", padx=28, pady=(16, 0))
        field.rows = (row, sep)  # so a single field can be hidden (see _refresh_source)

    # --- Phone section -------------------------------------------------------

    # --- Words & Triggers (saved to api/processing_config.json) --------------

    def _build_words_panel(self, parent):
        self._words_cfg = words_config.load()
        self._words_dirty = False

        def changed(*_):
            self._words_dirty = True
            self.mark_dirty()
            self._refresh_hint_count()

        # Vocabulary
        self._phone_heading(
            parent, "Vocabulary",
            "Names, brands and jargon Whisper should spell correctly, one per line "
            "(e.g. Lesley, Ollama, Kubernetes). Keep it to the words it actually gets "
            "wrong: Whisper only reads a limited amount.")
        self.vocab_box = tk.Text(parent, height=7, width=40, bg=RAISED, fg=TEXT,
                                 font=F_BODY, relief="flat", insertbackground=ACCENT,
                                 padx=8, pady=6, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=ACCENT,
                                 wrap="none")
        self.vocab_box.insert("1.0", "\n".join(self._words_cfg.get("vocabulary", [])))
        self.vocab_box.pack(anchor="w", padx=28, pady=(8, 0))
        self.vocab_box.bind("<KeyRelease>", changed)
        self.hint_count = tk.Label(parent, text="", font=F_HELP, bg=BG, fg=MUTED,
                                   anchor="w")
        self.hint_count.pack(anchor="w", padx=28, pady=(4, 0))

        # Name corrections
        self._phone_heading(
            parent, "Name corrections",
            "When Whisper writes the left word, replace it with the right one "
            "(e.g. Leslie → Lesley). Whole words only, any capitalization. The right-"
            "hand words are also added to the vocabulary automatically.")
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", padx=28, pady=(8, 0))
        tk.Label(head, text="Heard as", font=F_HELP, bg=BG, fg=MUTED, width=24,
                 anchor="w").pack(side="left")
        tk.Label(head, text="Write as", font=F_HELP, bg=BG, fg=MUTED,
                 anchor="w").pack(side="left", padx=(30, 0))
        self.corr_box = tk.Frame(parent, bg=BG)
        self.corr_box.pack(fill="x", padx=28)
        self.corr_rows = []
        for heard, write_as in self._words_cfg.get("name_corrections", {}).items():
            self._add_correction_row(heard, write_as, changed)
        add = tk.Label(parent, text="+ Add a correction", font=F_LABEL, bg=BG,
                       fg=ACCENT, cursor="hand2")
        add.pack(anchor="w", padx=28, pady=(8, 0))
        add.bind("<Button-1>", lambda e: (self._add_correction_row("", "", changed),
                                          self.corr_rows[-1][1].focus_set()))

        # Triggers (read-only summary)
        counts = words_config.trigger_summary(self._words_cfg)
        self._phone_heading(
            parent, "Trigger phrases",
            "Say these, then an instruction, while dictating:\n"
            "• \"prompt AI\" sends your dictation to the AI editor "
            "(see AI Editing). %d spelling variants are recognised.\n"
            "• \"prompt roast\", \"prompt TikTok\", \"prompt disses\" use Mean Mode "
            "(%d variants).\n"
            "Changing the phrases themselves means editing the patterns "
            "(regular expressions) in the file below." % (counts["edit"], counts["mean"]))
        link = tk.Label(parent, text="Open processing_config.json", font=F_LABEL,
                        bg=BG, fg=ACCENT, cursor="hand2")
        link.pack(anchor="w", padx=28, pady=(6, 0))
        link.bind("<Button-1>", lambda e: self._open_words_file())
        self._refresh_hint_count()

    def _add_correction_row(self, heard, write_as, on_change):
        row = tk.Frame(self.corr_box, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        entries = []
        first_entry = None
        for value in (heard, write_as):
            var = tk.StringVar(row, value=value)
            e = tk.Entry(row, textvariable=var, font=F_BODY, bg=RAISED, fg=TEXT,
                         relief="flat", insertbackground=ACCENT, width=24,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, borderwidth=5)
            e.pack(side="left", padx=(0, 6))
            var.trace_add("write", on_change)
            entries.append(var)
            if first_entry is None:
                first_entry = e
                tk.Label(row, text="→", font=F_BODY, bg=BG, fg=MUTED).pack(
                    side="left", padx=(0, 6))
        record = (row, first_entry, entries)

        def remove(_e=None):
            row.destroy()
            self.corr_rows.remove(record)
            on_change()
        x = tk.Label(row, text="✕", font=F_BODY, bg=BG, fg=MUTED, cursor="hand2")
        x.pack(side="left", padx=(4, 0))
        x.bind("<Button-1>", remove)
        self.corr_rows.append(record)

    def collect_words(self):
        """(vocabulary, corrections, error) from the page. Tk thread only."""
        vocab = words_config.clean_vocabulary(
            self.vocab_box.get("1.0", "end").splitlines())
        corr, err = words_config.clean_corrections(
            [(h.get(), w.get()) for _row, _e, (h, w) in self.corr_rows])
        return vocab, corr, err

    def _refresh_hint_count(self):
        vocab, corr, _err = self.collect_words()
        corr = corr or {}
        n = words_config.hint_chars(vocab, corr)
        budget = words_config.HINT_BUDGET_CHARS
        over = n > budget
        words = len(dict.fromkeys(vocab + list(corr.values())))
        self.hint_count.config(
            text="%d words · %d of ~%d characters Whisper reads%s" % (
                words, n, budget,
                ": the words at the end will be ignored" if over else ""),
            fg=WARN if over else MUTED)

    def _open_words_file(self):
        path = words_config.CONFIG_PATH
        if not os.path.exists(path):
            vocab, corr, _ = self.collect_words()
            words_config.save(vocab, corr or {})  # create it from the example first
        os.startfile(path)

    # --- Model picker (Transcription section) --------------------------------

    def _model_root(self):
        return model_catalog.models_dir(
            {"WHISPER_MODELS_DIR": self.fields["WHISPER_MODELS_DIR"].var.get()})

    def _build_model_picker(self, parent):
        self._phone_heading(
            parent, "Models",
            "Every Whisper model adds punctuation and capitals on its own, without "
            "you saying \"comma\" or \"period\"; bigger models do it more reliably. "
            ".en models are English-only and a little more accurate in English. "
            "Download a model, then Use it and Save & Restart API.")
        self._model_rows = {}
        self._downloading = None
        # A 2px border that's invisible until the Whisper model row is hovered.
        box = tk.Frame(parent, bg=BG, highlightthickness=2,
                       highlightbackground=BG, highlightcolor=BG)
        box.pack(fill="x", padx=26, pady=(6, 0))
        self._models_box = box
        for info in model_catalog.CATALOG:
            # Rows sit on SURFACE so the RAISED ghost buttons read as buttons.
            row = tk.Frame(box, bg=SURFACE)
            row.pack(fill="x", pady=(6, 0))
            # Pack the buttons FIRST: Tk gives earlier-packed widgets their
            # space, so long notes wrap instead of pushing buttons off-screen.
            right = tk.Frame(row, bg=SURFACE)
            right.pack(side="right", padx=12)
            use = Button(right, "Use", lambda i=info: self._use_model(i))
            use.config(width=7, padx=8)
            use.pack(side="right", padx=(6, 0))
            dl = Button(right, "Download", lambda i=info: self._download_model(i))
            dl.config(width=12, padx=8)
            dl.pack(side="right")
            left = tk.Frame(row, bg=SURFACE)
            left.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            title = tk.Label(left, text=info.name, font=F_BUTTON, bg=SURFACE,
                             fg=TEXT, anchor="w")
            title.pack(anchor="w")
            detail = "VRAM %s  \u00b7  CPU: %s  \u00b7  %s  \u00b7  %s" % (
                info.vram, info.cpu, info.langs,
                "%.1f GB" % (info.size_mb / 1024) if info.size_mb >= 1000
                else "%d MB" % info.size_mb)
            tk.Label(left, text="\u2713 Auto punctuation & capitals: " + info.punct,
                     font=F_HELP, bg=SURFACE, fg=TEXT, anchor="w").pack(anchor="w")
            tk.Label(left, text=detail, font=F_HELP, bg=SURFACE, fg=MUTED,
                     anchor="w", justify="left", wraplength=380).pack(anchor="w")
            if info.notes:
                tk.Label(left, text=info.notes, font=F_HELP, bg=SURFACE, fg=MUTED,
                         anchor="w", justify="left", wraplength=380).pack(anchor="w")
            self._model_rows[info.name] = (title, dl, use)
        self.fields["WHISPER_MODEL_SIZE"].var.trace_add(
            "write", lambda *_: self._refresh_models())
        self.fields["WHISPER_MODELS_DIR"].var.trace_add(
            "write", lambda *_: self._refresh_models())
        self._refresh_models()

    def _highlight_models(self, on):
        if hasattr(self, "_models_box"):
            color = ACCENT if on else BG
            self._models_box.config(highlightbackground=color, highlightcolor=color)

    def _scroll_to_models(self):
        self.root.update_idletasks()
        top = self._models_box.winfo_rooty() - self.inner.winfo_rooty() - 60
        self.canvas.yview_moveto(max(0.0, top / max(1, self.inner.winfo_height())))
        # A short flash so the eye lands on the list after the jump.
        self._highlight_models(True)
        self.root.after(900, lambda: self._highlight_models(False))

    def _refresh_models(self):
        if not hasattr(self, "_model_rows"):
            return
        root = self._model_root()
        active = self.fields["WHISPER_MODEL_SIZE"].var.get().strip()
        for info in model_catalog.CATALOG:
            title, dl, use = self._model_rows[info.name]
            have = model_catalog.is_downloaded(info, root)
            busy = self._downloading is not None
            selected = info.name == active
            title.config(text=info.name + ("   \u2022 selected" if selected else ""),
                         fg=ACCENT if selected else TEXT)
            use.config(text="Selected" if selected else "Use")
            use.set_enabled(not selected)
            if self._downloading == info.name:
                continue  # the progress poller owns this row's button text
            # One control carries the state: "Download", or a disabled "Downloaded".
            dl.config(text="\u2713 Downloaded" if have else "Download")
            dl.set_enabled(not have and not busy and bool(root))
            if have:
                dl.config(fg=ACCENT)

    def _use_model(self, info):
        self.fields["WHISPER_MODEL_SIZE"].var.set(info.name)
        if not model_catalog.is_downloaded(info, self._model_root()):
            self.status.config(
                text="%s isn't downloaded: restarting will fetch it first and "
                     "dictation will be offline for a few minutes." % info.name, fg=WARN)

    def _download_model(self, info):
        root = self._model_root()
        if not root or self._downloading:
            return
        self._downloading = info.name
        self._download_result = None
        self._refresh_models()

        def work():
            try:
                self._download_result = ("ok", model_catalog.download(info, root))
            except Exception as e:
                self._download_result = ("error", str(e))
        threading.Thread(target=work, daemon=True).start()
        self._poll_download(info, root)

    def _poll_download(self, info, root):
        # Tk isn't thread-safe: the worker only sets _download_result, and this
        # poller (on the Tk thread) reads it and the folder size.
        _title, dl, _use = self._model_rows[info.name]
        if self._download_result is None:
            base = os.path.join(root, info.cache_folder)
            done = max(model_catalog.folder_size(base),
                       model_catalog.folder_size(base + ".partial"))
            pct = min(99, int(100 * done / (info.size_mb * 1024 * 1024)))
            dl.config(text="Downloading %d%%" % pct, fg=WARN)
            self.root.after(500, lambda: self._poll_download(info, root))
            return
        kind, detail = self._download_result
        self._downloading = None
        if kind == "ok":
            how = "Copied from your existing download" if detail == "copied" else "Downloaded"
            self.status.config(text="%s: %s." % (info.name, how), fg=ACCENT)
        else:
            self.status.config(text="Download failed: %s" % detail, fg=DANGER)
        self._refresh_models()

    def _live_port(self):
        """The port as typed on the Startup page (unsaved), if it's valid."""
        raw = self.fields["WHISPER_API_PORT"].var.get().strip()
        return int(raw) if raw.isdigit() and 1024 <= int(raw) <= 65535             else api_port(self.values)

    def _go_to_field(self, section, key):
        """Jump to a setting on another page and put the cursor in it."""
        self.show_section(section)
        widget = self.fields[key].widget
        self.root.update_idletasks()
        top = widget.winfo_rooty() - self.inner.winfo_rooty() - 120
        self.canvas.yview_moveto(max(0.0, top / max(1, self.inner.winfo_height())))
        try:
            widget.focus_set()
            widget.select_range(0, "end")
        except (AttributeError, tk.TclError):
            pass

    def _phone_heading(self, parent, title, help_text=""):
        tk.Label(parent, text=title, font=F_SECTION, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=28, pady=(20, 0))
        if help_text:
            tk.Label(parent, text=help_text, font=F_HELP, bg=BG, fg=MUTED,
                     anchor="w", justify="left", wraplength=560).pack(
                anchor="w", padx=28, pady=(3, 0))

    def _build_phone_panel(self, parent):
        # API key: a real schema field so Save / Save & Restart write it like
        # any other setting. Rendered by hand for the masked entry + buttons.
        key_field = Field("WHISPER_API_KEY", "API key", "secret")
        self.fields[key_field.key] = key_field
        self._phone_heading(
            parent, "API key",
            "Other devices need this key to use the server; your phone gets it by "
            "scanning the code below, so there's nothing to copy. Changes take "
            "effect after Save & Restart API.")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=28, pady=(10, 0))
        key_field.var = tk.StringVar(value=self.values.get("WHISPER_API_KEY", ""))
        key_field.widget = tk.Entry(
            row, textvariable=key_field.var, font=F_BODY, bg=RAISED, fg=TEXT,
            relief="flat", insertbackground=ACCENT, width=38, show="\u2022",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, borderwidth=6)
        key_field.widget.pack(side="left")
        key_field.var.trace_add("write", lambda *_: (self.mark_dirty(),
                                                     self._refresh_qr()))
        Button(row, "Generate", lambda: key_field.var.set(generate_api_key())).pack(
            side="left", padx=(10, 0))
        self._key_shown = False

        def toggle_show():
            self._key_shown = not self._key_shown
            key_field.widget.config(show="" if self._key_shown else "\u2022")
            show_btn.config(text="Hide" if self._key_shown else "Show")
        show_btn = Button(row, "Show", toggle_show)
        show_btn.pack(side="left", padx=(6, 0))
        Button(row, "Remove", lambda: key_field.var.set("")).pack(
            side="left", padx=(6, 0))
        required = Field(
            "WHISPER_API_KEY_REQUIRED", "Require the key", "toggle",
            "On: only devices with the key can use the server. Off: anything on "
            "your network or Tailscale can, for example other programs you want "
            "to use your Whisper with. The key stays saved either way, so devices "
            "that have it keep working when you switch back.",
            default="true")
        self._build_field(parent, required, None)
        self.fields[required.key] = required
        required.var.trace_add("write", lambda *_: self._refresh_qr())
        https = Field(
            "SERVER_HTTPS", "Encrypted connection (HTTPS)", "toggle",
            "On: phones and other PCs connect over HTTPS, using a security "
            "certificate this PC makes for itself (the pairing code carries its "
            "fingerprint, so your phone trusts this PC and nothing pretending to "
            "be it). Phones need Whisper Unchained 1.4 or later, and must scan the "
            "pairing code again after you switch this on.",
            default="false")
        self._build_field(parent, https, None)
        self.fields[https.key] = https
        https.var.trace_add("write", lambda *_: self._refresh_qr())
        fp = local_api.fingerprint()
        if fp:
            # Selectable, so another PC's "Certificate fingerprint" can be pasted from it.
            pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
            fp_entry = tk.Entry(parent, font=F_HELP, bg=BG, fg=MUTED, relief="flat",
                                readonlybackground=BG, width=100)
            fp_entry.insert(0, "This PC's certificate (SHA-256): " + pretty)
            fp_entry.config(state="readonly")
            fp_entry.pack(fill="x", padx=28, pady=(4, 0))

        # Pairing QR.
        self._phone_heading(
            parent, "Pair your phone",
            "Open Whisper Unchained on your phone and scan this code to fill in the "
            "server address, model and key. The code contains your API key, so "
            "it stays hidden until you ask for it.")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=28, pady=(10, 0))
        tk.Label(row, text="Phone reaches this PC at", font=F_LABEL, bg=BG,
                 fg=TEXT).pack(side="left")
        hosts = detect_hosts()
        self.phone_host = tk.StringVar(value=hosts[0])
        combo = ttk.Combobox(row, textvariable=self.phone_host, values=hosts,
                             font=F_BODY, width=18)
        combo.pack(side="left", padx=(10, 0))
        self.phone_host.trace_add("write", lambda *_: self._refresh_qr())
        port_field = self.fields["WHISPER_API_PORT"]
        port_link = tk.Label(row, textvariable=port_field.var, font=F_BODY, bg=BG,
                             fg=ACCENT, cursor="hand2")
        tk.Label(row, text=":", font=F_BODY, bg=BG, fg=MUTED).pack(side="left")
        port_link.pack(side="left")
        tk.Label(row, text="  (port: change it in Startup)", font=F_HELP, bg=BG,
                 fg=MUTED).pack(side="left")
        port_link.bind("<Button-1>", lambda e: self._go_to_field("Startup", "WHISPER_API_PORT"))
        port_field.var.trace_add("write", lambda *_: self._refresh_qr())

        btns = tk.Frame(parent, bg=BG)
        btns.pack(fill="x", padx=28, pady=(10, 0))
        self._qr_mode = None  # None | "pair" | "download"
        self._qr_pair_btn = Button(btns, "Show pairing QR",
                                   lambda: self._set_qr_mode("pair"), kind="primary")
        self._qr_pair_btn.pack(side="left")
        self._qr_dl_btn = Button(btns, "Show download QR",
                                 lambda: self._set_qr_mode("download"))
        self._qr_dl_btn.pack(side="left", padx=(10, 0))

        self.qr_canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0,
                                   width=0, height=0)
        self.qr_canvas.pack(anchor="w", padx=28, pady=(12, 0))
        self.qr_note = tk.Label(parent, text="", font=F_HELP, bg=BG, fg=MUTED,
                                anchor="w", justify="left", wraplength=560)
        self.qr_note.pack(anchor="w", padx=28, pady=(4, 0))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))

        # Downloads.
        self._phone_heading(parent, "Get the app")
        links = [
            ("Google Play", PLAY_STORE_URL, "coming soon"),
            ("Android APK (GitHub releases)", APP_RELEASES_URL,
             "includes the extras the Play version leaves out"),
            ("App source code", APP_REPO_URL, ""),
            ("This server's source code", SERVER_REPO_URL, ""),
        ]
        for label, url, note in links:
            line = tk.Frame(parent, bg=BG)
            line.pack(fill="x", padx=28, pady=(8, 0))
            link = tk.Label(line, text=label, font=F_LABEL, bg=BG,
                            fg=ACCENT if url else MUTED,
                            cursor="hand2" if url else "arrow")
            link.pack(side="left")
            if url:
                link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
            if note:
                tk.Label(line, text="  " + note, font=F_HELP, bg=BG,
                         fg=MUTED).pack(side="left")

    def _set_qr_mode(self, mode):
        # Clicking the button that is showing its code hides it again.
        self._qr_mode = None if self._qr_mode == mode else mode
        self._qr_pair_btn.config(
            text="Hide pairing QR" if self._qr_mode == "pair" else "Show pairing QR")
        self._qr_dl_btn.config(
            text="Hide download QR" if self._qr_mode == "download"
            else "Show download QR")
        self._refresh_qr()
        if self._qr_mode:
            # The code sits below the fold at the default window size: scroll
            # so its top edge (and the buttons above it) are on screen.
            self.root.update_idletasks()
            top = self.qr_canvas.winfo_rooty() - self.inner.winfo_rooty() - 90
            self.canvas.yview_moveto(max(0.0, top / max(1, self.inner.winfo_height())))

    def _refresh_qr(self):
        if not hasattr(self, "qr_canvas"):
            return
        c = self.qr_canvas
        c.delete("all")
        if self._qr_mode is None:
            c.config(width=0, height=0)
            self.qr_note.config(text="")
            return
        if self._qr_mode == "pair":
            key = self.fields["WHISPER_API_KEY"].var.get().strip()
            https_live = bool(self.fields["SERVER_HTTPS"].var.get()) if "SERVER_HTTPS" in self.fields else False
            data = pairing_payload(
                self.phone_host.get().strip() or "127.0.0.1", key,
                self.values.get("WHISPER_MODEL_SIZE", ""), socket.gethostname(),
                port=self._live_port(),
                cert_sha256=local_api.fingerprint() if https_live else "")
            note = "Scan in Whisper Unchained: Settings > Scan pairing code."
            if https_live and (not local_api.fingerprint()
                               or not local_api.https_enabled(self.values)):
                note += (" HTTPS is switched on but not running yet: Save & Restart "
                         "API, then show this code again.")
            if self._live_port() != api_port(self.values):
                note += (" This code has your NEW port: Save & Restart API before "
                         "scanning.")
            if key != self.values.get("WHISPER_API_KEY", ""):
                note += (" This code has your NEW key: Save & Restart API before "
                         "the phone can use it.")
            elif not key:
                note += " No API key set, so the code holds only the address."
            elif not self.fields["WHISPER_API_KEY_REQUIRED"].var.get():
                note += (" The key isn't required right now; the phone keeps it for "
                         "when you switch it back on.")
        else:
            data = APP_RELEASES_URL
            note = "Scan with your phone's camera to open the download page."
        matrix = qr_matrix(data)
        if matrix is None:
            c.config(width=0, height=0)
            self.qr_note.config(
                text="Install the qrcode package to show codes: "
                     ".venv\\Scripts\\pip install qrcode", fg=WARN)
            return
        # Dark modules on a white plate with a 4-module quiet zone: phone
        # scanners expect dark-on-light, so the code ignores the dark theme.
        n, quiet = len(matrix), 4
        cell = max(3, 232 // (n + 2 * quiet))
        size = (n + 2 * quiet) * cell
        c.config(width=size, height=size)
        c.create_rectangle(0, 0, size, size, fill="#FFFFFF", outline="")
        for y, row in enumerate(matrix):
            for x, on in enumerate(row):
                if on:
                    x0, y0 = (x + quiet) * cell, (y + quiet) * cell
                    c.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                       fill="#000000", outline="")
        self.qr_note.config(text=note, fg=MUTED)

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=SURFACE)
        foot.pack(fill="x", side="bottom")
        inner = tk.Frame(foot, bg=SURFACE)
        inner.pack(fill="x", padx=28, pady=14)

        self.status = tk.Label(inner, text="", font=F_BODY, bg=SURFACE, fg=MUTED,
                               anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

        self.btn_restart = Button(inner, "Save & Restart API", self.save_and_restart,
                                  kind="primary")
        self.btn_restart.pack(side="right")
        self.btn_save = Button(inner, "Save", self.save, kind="ghost")
        self.btn_save.pack(side="right", padx=(0, 10))
        Button(inner, "Close", self.on_close, kind="ghost").pack(
            side="right", padx=(0, 10))
        self.set_saved_state(True)

    # --- behaviour ---------------------------------------------------------

    def show_section(self, name):
        self.current_section = name
        for f in self.frames.values():
            f.pack_forget()
        self.frames[name].pack(fill="both", expand=True)
        for n, (row, bar, lbl) in self.nav.items():
            active = n == name
            bar.config(bg=ACCENT if active else SURFACE)
            lbl.config(fg=TEXT if active else MUTED,
                       bg=RAISED if active else SURFACE)
            row.config(bg=RAISED if active else SURFACE)
        self.canvas.yview_moveto(0)

    def mark_dirty(self, *_):
        if not self.dirty:
            self.dirty = True
            self.set_saved_state(False)

    def set_saved_state(self, saved):
        self.btn_save.set_enabled(not saved)
        if saved:
            self.status.config(text="", fg=MUTED)
        else:
            self.status.config(text="Unsaved changes", fg=WARN)

    def collect(self):
        """Gather every field, validated. Returns (updates, error)."""
        updates = {}
        for key, f in self.fields.items():
            if f.kind == "toggle":
                updates[key] = "true" if f.var.get() else "false"
                continue
            if f.kind == "list":
                phrases = [p.strip() for p in
                           f.widget.get("1.0", "end").splitlines()]
                updates[key] = ",".join(p for p in phrases if p)
                continue
            raw = f.var.get()
            if f.labelled_choices:
                raw = next((val for lbl, val in f.labelled_choices if lbl == raw),
                           raw)
            value, err = f.coerce(raw)
            if err:
                return None, err
            updates[key] = value
        if "HOTKEY" in updates:
            try:
                import keyboard
                parse = keyboard.parse_hotkey
            except Exception:
                parse = None
            err, self._hotkey_warning = hotkey_config.validate(
                updates["HOTKEY"], updates.get("HOTKEY_MODE", "toggle"), parse)
            if err:
                return None, err
        return updates, None

    def save(self):
        updates, err = self.collect()
        words_dirty = getattr(self, "_words_dirty", False)
        if not err and words_dirty:
            vocab, corr, err = self.collect_words()
        if err:
            self.status.config(text=err, fg=DANGER)
            return False
        before = dict(self.values)
        try:
            write_env(updates)
        except Exception as e:
            self.status.config(text="Could not write .env: %s" % e, fg=DANGER)
            return False
        if words_dirty:
            try:
                words_config.save(vocab, corr, loaded_vocabulary=self._words_cfg.get("vocabulary", []))
                self._words_cfg = words_config.load()
                # Show what was saved, including words the phone added meanwhile.
                self.vocab_box.delete("1.0", "end")
                self.vocab_box.insert("1.0", "\n".join(self._words_cfg.get("vocabulary", [])))
                self._words_dirty = False
            except Exception as e:
                self.status.config(text="Could not save the word lists: %s" % e, fg=DANGER)
                return False
        self.values = read_env()
        self.dirty = False
        self.set_saved_state(True)
        hotkey_changed = any(before.get(k, "") != self.values.get(k, "")
                             for k in ("HOTKEY", "HOTKEY_MODE"))
        msg = "Saved. Restart the API to apply."
        if hotkey_changed:
            msg = "Saved. Use Restart Listener in the tray to apply the new hotkey."
        warning = getattr(self, "_hotkey_warning", None)
        selected = model_catalog.BY_NAME.get(self.values.get("WHISPER_MODEL_SIZE", ""))
        if selected and not model_catalog.is_downloaded(
                selected, model_catalog.models_dir(self.values)):
            warning = ("%s isn't in the model folder yet: the next API restart "
                       "downloads it first, with dictation offline for a few minutes."
                       % selected.name)
        if warning:
            self.status.config(text="Saved, but note: " + warning, fg=WARN)
        else:
            self.status.config(text=msg, fg=ACCENT)
        return True

    def save_and_restart(self):
        if not self.save():
            return
        # Same elevated path the tray menu uses: the service ACL grants
        # Interactive Users read-only rights, so stopping it needs a UAC prompt.
        # The port lives in the service's own command line (NSSM AppParameters),
        # not just .env, so rewrite it in the same elevated step. Idempotent.
        command = "Restart-Service -Name %s -Force" % SERVICE_NAME
        nssm = os.path.join(PROJECT_DIR, "nssm.exe")
        if os.path.exists(nssm):
            # The firewall rule from install_service.ps1 follows the port too.
            port = api_port(self.values)
            command = ("& '%s' set %s AppParameters '-m api.serve "
                       "--host 0.0.0.0 --port %d'; Set-NetFirewallRule -DisplayName "
                       "'Whisper Unchained Server' -LocalPort %d -ErrorAction "
                       "SilentlyContinue; %s" % (nssm, SERVICE_NAME, port, port, command))
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe",
                "-WindowStyle Hidden -Command \"%s\"" % command, None, 0)
            self.status.config(
                text="Saved. Approve the UAC prompt to restart the API.", fg=ACCENT)
        except Exception as e:
            self.status.config(text="Saved, but restart failed: %s" % e, fg=DANGER)

    def on_close(self):
        if self.dirty:
            self.status.config(
                text="Unsaved changes -- Save, or close again to discard.", fg=WARN)
            self.dirty = False   # a second close discards, deliberately
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    app = SettingsApp(root)

    # Deep-link straight to a section: `settings_gui.py --section "Voice AI"`.
    # Also what the screenshot tests drive, so every panel can be checked
    # without a human clicking through them.
    if "--section" in sys.argv:
        name = sys.argv[sys.argv.index("--section") + 1]
        match = next((n for n, _ in SCHEMA if n.lower() == name.lower()), None)
        if match:
            app.show_section(match)

    # Auto-close hook, used only by the smoke test so the GUI can be validated
    # without a human clicking anything.
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
