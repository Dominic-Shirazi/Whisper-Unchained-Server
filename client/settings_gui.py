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
import socket
import subprocess
import sys
import webbrowser

import hotkey_config
import llm_detect
import local_api
import model_catalog
# Re-exported: tests and callers use settings_gui.pairing_payload etc.
from pairing import (PAIRING_VERSION, detect_hosts, generate_api_key,  # noqa: F401
                     pairing_payload, qr_matrix)
import transcribe_client
import words_config
import app_styles
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
LOCAL_ONLY_SECTIONS = {"AI Editing (LLM)", "Mean Mode", "Audio Corpus", "Denoise", "Connections"}

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
        elif self.kind == "hotkey" and not raw:
            return None, ("%s: pick the custom key first (click the box next to "
                          "\"and press\", then press a key)" % self.label)
        elif self.kind in ("text", "choice", "pick", "model") and not raw:
            return None, "%s: cannot be empty" % self.label
        return raw, None


CUSTOM_LABEL = "Custom..."

# Settings most people never change: folded under "Advanced settings" at the
# end of their page (same detail, less to read past).
ADVANCED_KEYS = {
    "KEEP_AUDIO_MAX", "KEEP_AUDIO_HARD_MAX", "KEEP_AUDIO_PIN_BELOW",
    "DENOISE_ATTEN_LIMIT_DB",
    "RESTART_INTERVAL_HOURS", "API_VISIBLE",
    "LISTENER_RESTART_INTERVAL_MINS", "LISTEN_DELAY_SECONDS",
}

# Models always listed on the Transcription page (the rest fold away).
MODELS_FEATURED = ("large-v3-turbo", "small.en", "distil-large-v3.5")

# Light-gray examples shown in empty "Corrections and shortcuts" rows.
CORRECTION_EXAMPLES = (
    ("Leslie", "Lesley"),
    ("here's my link", "here's my link: cyrus-studio.com"),
    ("my email address", "name@example.com"),
)

# The example, in pieces: (text, part). "work" = what to work on (blue),
# "phrase" = the trigger phrase (bold), "ask" = what to do with it (amber).
# Blue and amber stay apart for red/green colour blindness; the words say it too.
EXAMPLE_WORK, EXAMPLE_ASK = "#6CB4FF", "#F0B44C"
TRIGGER_EXAMPLE = (
    ("You say: \"", None), ("hey can you send me the report by friday", "work"),
    (" ", None), ("prompt AI", "phrase"), (" ", None), ("make it polite", "ask"),
    ("\"\nIt pastes: \"Hi! Could you please send me the report by Friday? Thank you.\"\n\n",
     None),
    ("Before the phrase: what to work on.", "work"), ("   ", None),
    ("The phrase", "phrase"), ("   ", None),
    ("After it: what to do with it.", "ask"),
)
TRIGGER_ACTIONS_HELP = (
    "AI edit: an AI rewrites your words the way you ask (set it up in AI Editing).\n"
    "Mean mode: turns your words into a roast or a social-media comeback (Mean Mode).\n"
    "End of instruction: optional; say it after your instruction, it's removed.\n"
    "Capitals, commas and spacing don't matter, and a capitalised short word like AI "
    "also matches how Whisper may write it (A.I., a eye).")


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
        # --- Another PC ---
        Field("REMOTE_SERVER_URL", "Server address", "otext",
              "The other PC's Whisper Unchained Server, ending in /v1. Copy it from "
              "that PC's Settings > Connections page, e.g. https://100.x.y.z:5001/v1 "
              "(its Tailscale address). That PC applies its own vocabulary, trigger "
              "phrases and cleanup.",
              group="src_remote"),
        Field("REMOTE_API_KEY", "Server API key", "secret",
              "Only if that server has a key (its Settings > Connections page).",
              group="src_remote"),
        Field("REMOTE_CERT_SHA256", "Certificate fingerprint", "otext",
              "Only for an encrypted (https://) address: that server's certificate "
              "fingerprint (its Settings > Connections page has a Copy button). This "
              "PC then trusts exactly that server.",
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
        Field("HOTKEY", "Dictation hotkey", "hotkey",
              "Pick a preset, or choose Custom... to pick Ctrl, Alt or Shift (or none) "
              "and press any key. Keys that type nothing (F9, Pause, Ctrl+Alt+Space) "
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
        Field("FLOATING_MIC_BUTTON", "Floating mic button", "toggle",
              "A round mic button pops up next to the text cursor when you click "
              "into a text box, for dictating one-handed: click it to start, again "
              "to stop. It fades after 2 seconds and hides after 5; never in "
              "password or sign-in boxes. Right-click it to turn it off. Applies "
              "within a few seconds.", default="true"),
        Field("EDIT_SELECTION", "Edit highlighted text by voice", "toggle",
              "Highlight text, start dictating with the mic button or a hotkey that "
              "types nothing (F9, Ctrl+Alt+Space), and say what to do: \"make this "
              "more polite\", \"translate to Spanish\". The AI's version replaces "
              "the highlight. Uses the editor on the AI Editing page. Not with the "
              "backtick: it types over the highlight before it can be read.",
              default="true"),
        Field("CLIPBOARD_PRIVATE", "Keep dictations out of clipboard history", "toggle",
              "Text reaches your app through the clipboard. On: Windows clipboard "
              "history (Win+V) and cloud clipboard skip your dictations, the way "
              "password managers do. May stop pasting into Chrome Remote Desktop. "
              "Applies to the next dictation.", default="false"),
    ]),
    # Custom panel (see _build_phone_panel): ports, addresses, API key,
    # pairing QR, app downloads.
    ("Connections", []),
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
    "Hotkey": "How you start dictating on this PC: the hotkey, and the mic button that pops up at the text cursor.",
    "Connections": "How phones and other programs reach this PC's server: "
                   "ports, addresses, the API key and phone pairing.",
}


# --- Phone pairing ---------------------------------------------------------
# Contract: Whisper-Unchained-Android contracts/01-transcription-api.md §7.
# The payload, address detection and QR matrix live in pairing.py so the
# headless Linux pairing command can use them without Tk.

APP_REPO_URL = "https://github.com/Dominic-Shirazi/Whisper-Unchained-Android"
APP_RELEASES_URL = APP_REPO_URL + "/releases"
SERVER_REPO_URL = "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server"
# No listing yet. Set this once the app is live on Google Play.
PLAY_STORE_URL = ""


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


def dark_title_bar(root):
    """Dark title bar to match the window (Windows 10 2004+ / 11). Cosmetic, so
    any failure just leaves the default light one."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        on = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass


def style_scrollbar(style):
    """Slim dark scrollbar without the 3D arrow buttons (clam theme)."""
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                    bordercolor=BG, lightcolor=BORDER, darkcolor=BORDER,
                    arrowcolor=MUTED, borderwidth=0, gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", "#3A4250")])
    try:
        style.layout("Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {
                "sticky": "ns",
                "children": [("Vertical.Scrollbar.thumb",
                              {"expand": "1", "sticky": "nswe"})],
            }),
        ])
    except tk.TclError:
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

# Windows 11 geometry: 4 px corners on controls (buttons, boxes, drop-downs),
# 8 px on cards and panels. Tk draws only square corners, so each rounded
# widget gets four tiny anti-aliased corner pieces laid over its own corners,
# painted in the colour behind it. The widgets themselves stay ordinary Tk.
R_CONTROL, R_CARD = 4, 8


def _image_cache(widget):
    """Images made for this window. Kept on the window itself: a Tk image
    belongs to one window's interpreter and dies with it."""
    root = widget._root()
    cache = getattr(root, "_wu_images", None)
    if cache is None:
        cache = root._wu_images = {}
    return cache


def _hex_rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _corner_photo(widget, r, quadrant, outer, inner, border=None, bw=0):
    cache = _image_cache(widget)
    key = ("corner", r, quadrant, outer, inner, border, bw)
    if key in cache:
        return cache[key]
    from PIL import Image, ImageDraw, ImageTk
    s = 8                                   # draw 8x bigger, shrink = smooth edge
    size = 2 * r * s
    img = Image.new("RGB", (size, size), _hex_rgb(outer))
    d = ImageDraw.Draw(img)
    if bw and border:
        d.ellipse([0, 0, size - 1, size - 1], fill=_hex_rgb(border))
        d.ellipse([bw * s, bw * s, size - 1 - bw * s, size - 1 - bw * s], fill=_hex_rgb(inner))
    else:
        d.ellipse([0, 0, size - 1, size - 1], fill=_hex_rgb(inner))
    img = img.resize((2 * r, 2 * r), Image.LANCZOS)
    box = {"nw": (0, 0, r, r), "ne": (r, 0, 2 * r, r),
           "sw": (0, r, r, 2 * r), "se": (r, r, 2 * r, 2 * r)}[quadrant]
    photo = ImageTk.PhotoImage(img.crop(box), master=widget)
    cache[key] = photo
    return photo


def _bg_of(widget, default=BG):
    try:
        return widget.cget("bg")
    except tk.TclError:
        return default


class Corners:
    """Rounds a widget's corners. The colours are read from the widget each
    time (its background, its focus/hover border), so call refresh() after
    changing them; focus and hover changes refresh on their own."""

    def __init__(self, widget, r=R_CONTROL, inner=None):
        self.w, self.r, self.inner = widget, r, inner
        self.pieces = {}
        for q in ("nw", "ne", "sw", "se"):
            lbl = tk.Label(widget, bd=0, highlightthickness=0, padx=0, pady=0,
                           width=0, height=0)
            # bordermode=outside: the true outer corner, over any focus ring.
            lbl.place(relx=0 if "w" in q else 1, rely=0 if "n" in q else 1, anchor=q,
                      bordermode="outside")
            # Clicks on a corner belong to the widget underneath.
            for ev in ("<Button-1>", "<ButtonRelease-1>", "<Double-Button-1>"):
                lbl.bind(ev, lambda e, ev=ev: widget.event_generate(ev, x=e.x, y=e.y))
            self.pieces[q] = lbl
        for ev in ("<FocusIn>", "<FocusOut>", "<Enter>", "<Leave>"):
            widget.bind(ev, lambda e: widget.after_idle(self.refresh), add="+")
        widget._corners = self
        self.refresh()

    def _border(self):
        """(width, colour) of the widget's visible edge right now."""
        w = self.w
        try:
            focus = w.focus_get()
        except (tk.TclError, KeyError):
            focus = None
        if isinstance(w, ttk.Combobox):
            # ttk keeps the border colour in the style (see _style): 1 px.
            return 1, ACCENT if focus is w else BORDER
        try:
            bw = int(w.cget("highlightthickness"))
        except tk.TclError:
            return 0, None
        if not bw:
            return 0, None
        # A box made of a frame (the API key box) is focused when its entry is.
        focused = focus is w or (isinstance(w, tk.Frame) and focus is not None
                                 and str(focus).startswith(str(w) + "."))
        return bw, w.cget("highlightcolor") if focused else w.cget("highlightbackground")

    def refresh(self):
        w = self.w
        try:
            if not w.winfo_exists():
                return
            outer = _bg_of(w.master)
            inner = self.inner or _bg_of(w, RAISED)
            bw, border = self._border()
            # A border in the colour behind (a button's unfocused ring) is drawn
            # too, so the visible fill is the part that gets rounded.
            for q, lbl in self.pieces.items():
                lbl.config(image=_corner_photo(w, self.r, q, outer, inner, border, bw),
                           bg=outer)
                lbl.lift()
        except (tk.TclError, KeyError):
            pass


def _chevron_photo(widget, color, up=False, width=30):
    """A small chevron with room around it: drop-down boxes (down) and the
    Show / Hide links (down to open, up to close)."""
    cache = _image_cache(widget)
    key = ("chevron", color, up, width)
    if key in cache:
        return cache[key]
    from PIL import Image, ImageDraw, ImageTk
    s = 8
    img = Image.new("RGBA", (width * s, 16 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgb = _hex_rgb(color)
    c = width * s / 2
    y_out, y_in = (11 * s, 6 * s) if up else (6 * s, 11 * s)
    d.line([(c - 5 * s, y_out), (c, y_in), (c + 5 * s, y_out)], fill=rgb + (255,),
           width=int(1.6 * s), joint="curve")
    cache[key] = ImageTk.PhotoImage(img.resize((width, 16), Image.LANCZOS), master=widget)
    return cache[key]


def set_link_chevron(label, text, up):
    """A Show / Hide link: its words with a chevron in front."""
    label.config(text=text, image=_chevron_photo(label, ACCENT, up=up, width=18), compound="left")


class SlimScrollbar(tk.Canvas):
    """A thin rounded scrollbar thumb with no track box, wider while the
    pointer is over it, as in Windows 11. Drag it or click above/below it."""

    WIDTH = 12

    def __init__(self, parent, command):
        super().__init__(parent, width=self.WIDTH, bg=_bg_of(parent), highlightthickness=0,
                         bd=0)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self._hover = False
        self._drag = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _thumb(self):
        h = max(1, self.winfo_height())
        y0 = self.first * h
        y1 = max(y0 + 24, self.last * h)
        return y0, min(h, y1)

    def _draw(self):
        self.delete("all")
        if self.last - self.first >= 0.999:
            return   # everything fits: no scrollbar at all
        y0, y1 = self._thumb()
        w = 6 if self._hover or self._drag is not None else 3
        x1 = self.WIDTH - 3
        x0 = x1 - w
        color = "#8A929E" if self._hover else "#5A6270"
        r = w / 2
        self.create_oval(x0, y0 + 2, x1, y0 + 2 + w, fill=color, outline="")
        self.create_oval(x0, y1 - 2 - w, x1, y1 - 2, fill=color, outline="")
        self.create_rectangle(x0, y0 + 2 + r, x1, y1 - 2 - r, fill=color, outline="")

    def _press(self, e):
        y0, y1 = self._thumb()
        if y0 <= e.y <= y1:
            self._drag = (e.y, self.first)
            self._draw()
        else:
            self.command("scroll", -1 if e.y < y0 else 1, "pages")

    def _motion(self, e):
        if self._drag is None:
            return
        start_y, start_first = self._drag
        h = max(1, self.winfo_height())
        self.command("moveto", max(0.0, start_first + (e.y - start_y) / h))


def round_widgets(root):
    """Give every box, button, drop-down and card under root its rounded
    corners (once each). Safe to call again after adding rows."""
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if getattr(w, "_corners", None) is not None or getattr(w, "_no_round", False):
            continue
        try:
            if isinstance(w, Button):
                Corners(w, R_CONTROL + 2)   # + its 2 px focus ring
            elif isinstance(w, ttk.Combobox):
                Corners(w, R_CONTROL, inner=RAISED)
            elif isinstance(w, (tk.Entry, tk.Frame, tk.Label)) and \
                    int(w.cget("highlightthickness") or 0) > 0 and \
                    w.cget("highlightbackground") == BORDER:
                Corners(w, R_CONTROL)       # a text box (or a box-shaped picker)
            elif isinstance(w, (tk.Frame, tk.Text)) and _bg_of(w) == SURFACE \
                    and _bg_of(w.master) == BG:
                Corners(w, R_CARD)          # a card on the page
        except (tk.TclError, AttributeError):
            pass

class Toggle(tk.Canvas):
    """A pill switch. ttk.Checkbutton cannot be themed dark on Windows without
    fighting the native renderer, and a checkbox reads as a form; a switch reads
    as a setting that takes effect."""

    # Windows 11 proportions (40 x 20 track) plus the words On / Off to its
    # left, as in Windows Settings. Off is a hollow outline and On is filled,
    # so the state reads from shape and words, never from colour alone.
    TW, H, TEXT_W, PAD = 40, 20, 34, 3
    W = TEXT_W + TW + 2 * PAD

    def __init__(self, parent, variable, command=None, label=True):
        self.label = label
        width = (self.W if label else self.TW + 2 * self.PAD)
        super().__init__(parent, width=width, height=self.H + 2 * self.PAD,
                         bg=_bg_of(parent), highlightthickness=0, bd=0, cursor="hand2",
                         takefocus=1)
        self.var = variable
        self.command = command
        self._hover = False
        self._pos = 1.0 if variable.get() else 0.0
        self.bind("<Button-1>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Return>", self._click)
        for ev in ("<FocusIn>", "<FocusOut>"):
            self.bind(ev, lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
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
        self.focus_set()
        self.var.set(not self.var.get())  # the trace animates it
        if self.command:
            self.command()
        return "break"

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _animate(self, step=0):
        target = 1.0 if self.var.get() else 0.0
        self._pos += (target - self._pos) * 0.35
        self._draw()
        if step < 12 and abs(target - self._pos) > 0.01:
            self.after(12, lambda: self._animate(step + 1))
        else:
            self._pos = target
            self._draw()

    def _image(self, pos, hover, focus, behind):
        key = (round(pos, 2), hover, focus, behind)
        cache = _image_cache(self)
        key = ("toggle",) + key
        if key in cache:
            return cache[key]
        from PIL import Image, ImageDraw, ImageTk
        k = 8                                     # supersample, then shrink
        p, h, tw = self.PAD, self.H, self.TW
        W, H = (tw + 2 * p) * k, (h + 2 * p) * k
        img = Image.new("RGB", (W, H), _hex_rgb(behind))
        d = ImageDraw.Draw(img)
        if focus:   # keyboard focus ring, as on every Windows control
            d.rounded_rectangle([0, 0, W - 1, H - 1], radius=H // 2,
                                outline=_hex_rgb(TEXT), width=2 * k)
        x0, y0 = p * k, p * k
        x1, y1 = x0 + tw * k, y0 + h * k
        # Off: a hollow outline. On: filled with the accent.
        fill = _hex_rgb(self._blend(behind, ACCENT, pos))
        edge = _hex_rgb(self._blend("#9AA2AE", ACCENT, pos))
        d.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=h * k // 2, fill=fill,
                            outline=edge, width=k)
        knob = (14 if hover else 12) * k
        cx = x0 + h * k / 2 + pos * (tw - h) * k
        cy = y0 + h * k / 2
        color = _hex_rgb(self._blend("#C4CAD3", "#0E1418", pos))
        d.ellipse([cx - knob / 2, cy - knob / 2, cx + knob / 2, cy + knob / 2], fill=color)
        photo = ImageTk.PhotoImage(img.resize((tw + 2 * p, h + 2 * p), Image.LANCZOS),
                                   master=self)
        cache[key] = photo
        return photo

    def _draw(self):
        self.delete("all")
        try:
            focus = self.focus_get() is self
        except (tk.TclError, KeyError):
            focus = False
        x = 0
        if self.label:
            self.create_text(0, self.PAD + self.H / 2, anchor="w", fill=TEXT, font=F_HELP,
                             text="On" if self.var.get() else "Off")
            x = self.TEXT_W
        self._photo = self._image(self._pos, self._hover, focus, _bg_of(self))
        self.create_image(x, 0, anchor="nw", image=self._photo)

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
        behind = _bg_of(parent)
        # A 2 px ring in the colour behind the button, white while it has
        # keyboard focus (Tab to it, Space or Enter presses it).
        super().__init__(parent, text=text, font=F_BUTTON, bg=self.bg, fg=self.fg,
                         padx=16, pady=7, cursor="hand2", takefocus=1,
                         highlightthickness=2, highlightbackground=behind,
                         highlightcolor=TEXT)
        self.command = command
        self._enabled = True
        self.bind("<Button-1>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _recorner(self):
        c = getattr(self, "_corners", None)
        if c is not None:
            c.refresh()

    def _on_enter(self, _e):
        if self._enabled:
            self.config(bg=self.hover)

    def _on_leave(self, e):
        # Moving onto one of the button's own corner pieces isn't leaving it.
        if self._enabled and getattr(e, "detail", "") != "NotifyInferior":
            self.config(bg=self.bg)

    def _click(self, _event=None):
        if self._enabled and self.command:
            self.command()
        return "break"

    def set_enabled(self, on):
        self._enabled = on
        self.config(fg=self.fg if on else "#7C8592",
                    bg=self.bg if on else RAISED,
                    cursor="hand2" if on else "arrow",
                    takefocus=1 if on else 0)
        self._recorner()

    def config(self, cnf=None, **kw):
        out = super().config(cnf, **kw)
        if "bg" in kw or "background" in kw:
            self._recorner()
        return out

    configure = config


class Tip:
    """A small hover note, for icon-only controls (words, never colour alone)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.win = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Button-1>", self.hide, add="+")

    def show(self, _e=None):
        self.hide()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        self.win.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.win, text=self.text() if callable(self.text) else self.text,
                 font=F_HELP, bg=SURFACE, fg=TEXT, padx=8, pady=4,
                 highlightthickness=1, highlightbackground=BORDER).pack()

    def hide(self, _e=None):
        if self.win is not None:
            self.win.destroy()
            self.win = None


def icon_font():
    """Windows' own icon font (the eye in Windows' password boxes), or None."""
    try:
        from tkinter import font as tkfont
        fams = set(tkfont.families())
    except Exception:
        return None
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in fams:
            return name
    return None


class EyeButton(tk.Label):
    """Show/hide toggle: Windows' standard eye icons (View / Hide, the eye with
    a line through it), with a hover note. Falls back to the words Show / Hide."""

    SHOW, HIDE = "\ue890", "\ued1a"   # View, Hide in Segoe Fluent Icons / MDL2

    def __init__(self, parent, command, bg=RAISED):
        fam = icon_font()
        super().__init__(parent, bg=bg, fg=MUTED, cursor="hand2", padx=4,
                         font=(fam, 13) if fam else F_HELP)
        self.glyphs = fam is not None
        self.command, self.open = command, False
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self.config(fg=TEXT), add="+")
        self.bind("<Leave>", lambda e: self.config(fg=MUTED), add="+")
        self.tip = Tip(self, lambda: "Hide key" if self.open else "Show key")
        self.draw()

    def draw(self):
        # Hidden: the slashed eye (click to show). Shown: the open eye.
        if self.glyphs:
            self.config(text=self.SHOW if self.open else self.HIDE)
        else:
            self.config(text="Hide" if self.open else "Show")

    def set_open(self, on):
        self.open = on
        self.draw()


class SecretEntry(tk.Frame):
    """A password-style box: dots until the eye inside it is clicked."""

    def __init__(self, parent, var, width=34, on_toggle=None):
        super().__init__(parent, bg=RAISED, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        self.entry = tk.Entry(self, textvariable=var, font=F_BODY, bg=RAISED, fg=TEXT,
                              relief="flat", insertbackground=ACCENT, width=width,
                              show="•", borderwidth=6, highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True)
        self.eye = EyeButton(self, self.toggle)
        self.eye.pack(side="right", padx=(0, 8))
        self.on_toggle = on_toggle
        self.entry.bind("<FocusIn>", lambda e: self._edge(ACCENT))
        self.entry.bind("<FocusOut>", lambda e: self._edge(BORDER))

    def _edge(self, color):
        self.config(highlightbackground=color)
        c = getattr(self, "_corners", None)
        if c is not None:
            self.after_idle(c.refresh)

    def toggle(self):
        shown = not self.eye.open
        self.eye.set_open(shown)
        self.entry.config(show="" if shown else "•")
        if self.on_toggle:
            self.on_toggle(shown)


class Placeholder:
    """Light-gray example text over an empty box, gone while typing. A label on
    top of the box, so the example never ends up in the saved value."""

    def __init__(self, entry, var, text):
        self.entry, self.var = entry, var
        self.label = tk.Label(entry, text=text, font=F_BODY, bg=entry.cget("bg"),
                              fg=MUTED, cursor="xterm")
        self.label.bind("<Button-1>", lambda e: entry.focus_set())
        entry.bind("<FocusIn>", lambda e: self.update(), add="+")
        entry.bind("<FocusOut>", lambda e: self.update(), add="+")
        var.trace_add("write", lambda *_: self.update())
        self.update()

    def update(self):
        try:
            show = not self.var.get() and self.entry.focus_get() is not self.entry
        except (KeyError, tk.TclError):
            show = not self.var.get()
        if show:
            self.label.place(x=2, rely=0.5, anchor="w")
        else:
            self.label.place_forget()


class Expander(tk.Label):
    """A "Show ... / Hide ..." link that opens and closes a section, so long
    lists stay folded until wanted (Windows 11's expander, as a link)."""

    def __init__(self, parent, body, show_text, hide_text, opened=False):
        super().__init__(parent, font=F_LABEL, bg=_bg_of(parent), fg=ACCENT, cursor="hand2",
                         takefocus=1, highlightthickness=1,
                         highlightbackground=_bg_of(parent), highlightcolor=TEXT)
        self.body, self.show_text, self.hide_text = body, show_text, hide_text
        self.opened = not opened
        for ev in ("<Button-1>", "<space>", "<Return>"):
            self.bind(ev, lambda e: (self.toggle(), "break")[1])
        self.toggle()

    def _closed_text(self):
        text = self.show_text() if callable(self.show_text) else self.show_text
        return text

    def toggle(self):
        self.opened = not self.opened
        if self.opened:
            self.body.pack(fill="x", after=self)
            set_link_chevron(self, self.hide_text, up=True)
        else:
            self.body.pack_forget()
            set_link_chevron(self, self._closed_text(), up=False)

    def relabel(self):
        if not self.opened:
            set_link_chevron(self, self._closed_text(), up=False)


def _entry(parent, var, width=24):
    return tk.Entry(parent, textvariable=var, font=F_BODY, bg=RAISED, fg=TEXT,
                    relief="flat", insertbackground=ACCENT, width=width,
                    highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=ACCENT, borderwidth=5)


class RowList(tk.Frame):
    """A list edited one line at a time: a box per item, a remove (x) per row,
    and "+ Add ..." underneath. Enter in a box starts a new line below it."""

    def __init__(self, parent, values, on_change, add_text, width=30):
        super().__init__(parent, bg=BG)
        self.on_change, self.width = on_change, width
        self.rows = []
        self.box = tk.Frame(self, bg=BG)
        self.box.pack(fill="x")
        for v in values:
            self.add(v, notify=False)
        add = tk.Label(self, text="+ " + add_text, font=F_LABEL, bg=BG, fg=ACCENT,
                       cursor="hand2")
        add.pack(anchor="w", pady=(8, 0))
        add.bind("<Button-1>", lambda e: self.add("", focus=True))

    def add(self, value, notify=True, focus=False, after=None):
        row = tk.Frame(self.box, bg=BG)
        var = tk.StringVar(row, value=value)
        e = _entry(row, var, self.width)
        e.pack(side="left")
        x = tk.Label(row, text="✕", font=F_BODY, bg=BG, fg=MUTED, cursor="hand2")
        x.pack(side="left", padx=(8, 0))
        Tip(x, "Remove")
        record = (row, var, e)
        if after is not None and after in self.rows:
            i = self.rows.index(after) + 1
            self.rows.insert(i, record)
            for r, _v, _e in self.rows[i:]:
                r.pack_forget()
            for r, _v, _e in self.rows[i:]:
                r.pack(fill="x", pady=(4, 0))
        else:
            self.rows.append(record)
            row.pack(fill="x", pady=(4, 0))
        var.trace_add("write", lambda *_: self.on_change())
        x.bind("<Button-1>", lambda _e: self.remove(record))
        e.bind("<Return>", lambda _e: (self.add("", focus=True, after=record), "break")[1])
        if focus:
            e.focus_set()
        if notify:
            self.on_change()
        return record

    def remove(self, record):
        record[0].destroy()
        self.rows.remove(record)
        self.on_change()

    def values(self):
        return [v.get().strip() for _r, v, _e in self.rows if v.get().strip()]

    def set(self, values):
        for row, _v, _e in list(self.rows):
            row.destroy()
        self.rows = []
        for v in values:
            self.add(v, notify=False)


# Tk key names -> the `keyboard` library's names (what HOTKEY holds).
_KEYSYMS = {
    "grave": "`", "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "backslash": "\\", "semicolon": ";", "apostrophe": "'", "comma": ",",
    "period": ".", "slash": "/", "space": "space", "Pause": "pause",
    "Scroll_Lock": "scroll lock", "Insert": "insert", "Delete": "delete",
    "Home": "home", "End": "end", "Prior": "page up", "Next": "page down",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right", "Tab": "tab",
    "Print": "print screen", "App": "menu", "Menu": "menu",
    "Control_R": "right ctrl", "Alt_R": "right alt",
}
_MODIFIER_KEYSYMS = {"Control_L", "Shift_L", "Shift_R", "Alt_L", "Super_L",
                     "Super_R", "Win_L", "Win_R", "Caps_Lock", "Num_Lock"}
HOTKEY_MODIFIERS = [("None", ""), ("Ctrl", "ctrl"), ("Alt", "alt"), ("Shift", "shift"),
                    ("Ctrl + Alt", "ctrl+alt"), ("Ctrl + Shift", "ctrl+shift"),
                    ("Alt + Shift", "alt+shift"), ("Ctrl + Alt + Shift", "ctrl+alt+shift")]


def key_from_event(keysym, char=""):
    """The `keyboard` name for a key press, or "" to ignore it (a modifier on
    its own, Escape, Backspace)."""
    if keysym in _MODIFIER_KEYSYMS or keysym in ("Escape", "BackSpace", "Return"):
        return ""
    if keysym in _KEYSYMS:
        return _KEYSYMS[keysym]
    if re.fullmatch(r"F\d{1,2}", keysym):
        return keysym.lower()
    if len(keysym) == 1 and keysym.isalnum():
        return keysym.lower()
    if keysym.startswith("KP_"):
        return ""  # number-pad keys: the listener can't tell them apart reliably
    if char and len(char) == 1 and char.isprintable() and not char.isspace():
        return char.lower()
    return ""


def split_hotkey(hotkey):
    """("ctrl+alt", "d") from "ctrl+alt+d"; ("", "right ctrl") from "right ctrl"."""
    parts = [p.strip().lower() for p in (hotkey or "").split("+") if p.strip()]
    if not parts:
        return "", ""
    return "+".join(parts[:-1]), parts[-1]


def key_label(key):
    labels = {"`": "` (backtick)", "space": "Space", "pause": "Pause / Break"}
    if key in labels:
        return labels[key]
    return key.upper() if len(key) <= 3 else key.title()


class KeyCapture(tk.Label):
    """Click, then press the key you want. Shows its name; never typed into."""

    def __init__(self, parent, on_key):
        super().__init__(parent, text="", font=F_BODY, bg=RAISED, fg=TEXT, width=16,
                         padx=10, pady=7, cursor="hand2", highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT,
                         takefocus=1)
        self.on_key, self.key = on_key, ""
        self.bind("<Button-1>", lambda e: self.focus_set())
        self.bind("<FocusIn>", lambda e: self.config(text="Press a key...",
                                                     fg=MUTED, highlightbackground=ACCENT))
        self.bind("<FocusOut>", lambda e: self._show())
        self.bind("<KeyPress>", self._press)

    def set(self, key):
        self.key = key
        self._show()

    def _show(self):
        self.config(text=key_label(self.key) if self.key else "Click, then press a key",
                    fg=TEXT if self.key else MUTED, highlightbackground=BORDER)

    def _press(self, e):
        if e.keysym == "Escape":
            self.master.focus_set()
            return "break"
        key = key_from_event(e.keysym, e.char)
        if key:
            self.key = key
            self.on_key(key)
            self.master.focus_set()  # done: show it
        return "break"


class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.values = read_env()
        _infer_edit_backend(self.values)
        self.dirty = False
        self.fields = {}

        root.title("Whisper Unchained Settings")
        set_window_icon(root)
        dark_title_bar(root)
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
        round_widgets(root)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")   # the only built-in theme that honours custom colors
        except tk.TclError:
            pass
        s.configure("TCombobox", fieldbackground=RAISED, background=RAISED,
                    foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=RAISED, darkcolor=RAISED, borderwidth=1,
                    padding=(7, 7, 4, 7), selectbackground=RAISED,
                    selectforeground=TEXT)
        s.map("TCombobox",
              fieldbackground=[("readonly", RAISED)],
              foreground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", RAISED)], darkcolor=[("focus", RAISED)],
              selectbackground=[("readonly", RAISED)],
              selectforeground=[("readonly", TEXT)],
              arrowcolor=[("active", ACCENT)])
        # Windows 11's thin chevron instead of clam's boxed 3D arrow button.
        try:
            self._chevrons = (_chevron_photo(self.root, MUTED), _chevron_photo(self.root, TEXT))
            s.element_create("Chevron.downarrow", "image", self._chevrons[0],
                             ("active", self._chevrons[1]), sticky="")
            s.layout("TCombobox", [("Combobox.field", {"sticky": "nswe", "children": [
                ("Chevron.downarrow", {"side": "right", "sticky": "ns"}),
                ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                    ("Combobox.textarea", {"sticky": "nswe"})]})]})])
        except tk.TclError:
            pass
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
        tk.Frame(self.sidebar, bg=SURFACE, height=6).pack(fill="x")
        for name, _ in SCHEMA:
            # Windows 11 navigation: the selected page gets a rounded backplate
            # and a short accent pill on its left edge (plus brighter text, so
            # it never relies on colour alone).
            row = tk.Frame(self.sidebar, bg=SURFACE, cursor="hand2", takefocus=1,
                           highlightthickness=0)
            row.pack(fill="x", padx=6, pady=1)
            bar = tk.Frame(row, bg=SURFACE, width=3, height=16)
            bar.pack(side="left", padx=(0, 0))
            lbl = tk.Label(row, text=name, font=F_TAB, bg=SURFACE, fg=MUTED,
                           anchor="w", padx=11, pady=9)
            lbl.pack(side="left", fill="x", expand=True)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, n=name: self.show_section(n))
                w.bind("<Enter>", lambda e, n=name: self._nav_hover(n, True), add="+")
                w.bind("<Leave>", lambda e, n=name: self._nav_hover(n, False), add="+")
            row.bind("<space>", lambda e, n=name: self.show_section(n))
            row.bind("<Return>", lambda e, n=name: self.show_section(n))
            row.bind("<FocusIn>", lambda e, l=lbl: l.config(fg=TEXT), add="+")
            row.bind("<FocusOut>", lambda e, n=name, l=lbl: l.config(
                fg=TEXT if n == getattr(self, "current_section", None) else MUTED), add="+")
            Corners(row, R_CONTROL)
            self.nav[name] = (row, bar, lbl)

    def _nav_hover(self, name, on):
        row, _bar, lbl = self.nav[name]
        if name == getattr(self, "current_section", None):
            return
        color = "#20242C" if on else SURFACE
        row.config(bg=color)
        lbl.config(bg=color)
        if getattr(row, "_corners", None) is not None:
            row._corners.refresh()

    def _round_now(self):
        self._round_pending = False
        round_widgets(self.root)

    def _build_content(self, parent):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(side="left", fill="both", expand=True, padx=(0, 8))

        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        scroll = SlimScrollbar(wrap, command=canvas.yview)
        self.inner = tk.Frame(canvas, bg=BG)

        def inner_changed(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Rows added since the last pass (a new word, a shown list) get
            # their rounded corners too.
            if not getattr(self, "_round_pending", False):
                self._round_pending = True
                self.root.after(30, self._round_now)
        self.inner.bind("<Configure>", inner_changed)
        win = canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        def on_wheel(e):
            # An open drop-down list scrolls itself; moving the page under it
            # would leave the list floating away from its box.
            if ".popdown" in str(e.widget):
                return
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", on_wheel)
        # A closed drop-down changes its value on the mouse wheel (Tk's default).
        # Scrolling the page over a column of them silently changed settings, so
        # the wheel scrolls the page there too; pick a value by clicking.
        root = self.root
        root.bind_class("TCombobox", "<MouseWheel>",
                        lambda e: (canvas.yview_scroll(int(-e.delta / 120), "units"),
                                   "break")[1])
        self.canvas = canvas

        self.frames = {}
        self.groups = {}
        models = installed_ollama_models(self.values.get("OLLAMA_API_URL", ""))
        self._folds = {}
        for name, fields in SCHEMA:
            frame = tk.Frame(self.inner, bg=BG)
            holder = None
            # One "Advanced settings" fold per page, or per group of a page
            # (a group is shown and hidden as a block), at the end of it.
            folds = {}     # group -> (holder frame, body frame)
            n_adv = {}
            for f in fields:
                if f.key in ADVANCED_KEYS and f.kind != "heading":
                    n_adv[f.group] = n_adv.get(f.group, 0) + 1
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
                if f.key in ADVANCED_KEYS:
                    if f.group not in folds:
                        adv_holder = tk.Frame(target, bg=BG)   # packed after the loop
                        adv_body = tk.Frame(adv_holder, bg=BG)
                        exp = Expander(adv_holder, adv_body,
                                       "Advanced settings (%d)" % n_adv.get(f.group, 0),
                                       "Hide advanced settings")
                        exp.pack(anchor="w", padx=28)
                        folds[f.group] = (adv_holder, exp)
                    adv_holder, exp = folds[f.group]
                    target = exp.body
                    f.fold = exp
                if f.kind == "heading":
                    # A sub-heading, not a setting: never saved.
                    self._phone_heading(target, f.label, f.help)
                    continue
                self._build_field(target, f, models)
                self.fields[f.key] = f
            for adv_holder, _exp in folds.values():
                # Last in its page or group, before any panel built below.
                adv_holder.pack(fill="x", pady=(18, 0))
            if name == "Connections":
                self._build_phone_panel(frame)
            if name == "Words & Triggers":
                self._build_words_panel(frame)
            if name == "Transcription":
                self._build_model_picker(self.groups["src_local"])
            if name == "AI Editing (LLM)":
                self._build_llm_finder(frame)
                self._build_app_styles(frame)
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
                    self.btn_restart.pack(side="right", padx=(0, 8), after=self.btn_save)
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
        ("menu", "Right-click \"Transcribe\" on audio and video files",
         "In File Explorer: \"Transcribe to clipboard\", or \"Transcribe with Whisper "
         "Unchained...\" for the window with subtitles. A straight transcription, "
         "nothing edited. Windows 11: under \"Show more options\".",
         "install_context_menu.ps1"),
        ("login", "Start when I log in",
         "Starts the tray icon and hotkey when you sign in to Windows.",
         "install_listener_startup.ps1"),
    ]

    def _integration_on(self, key):
        if key == "menu":
            import winreg
            # The merged view: also sees an entry added for all users by older versions.
            for key in (r"SystemFileAssociations\.mp3\shell\WhisperUnchained.Clipboard",
                        r"*\shell\TranscribeToClipboard"):
                try:
                    winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key))
                    return True
                except OSError:
                    pass
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
        skip = (api_port(self.values), int(local_api.https_port(self.values)))
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
            # A preset from the list, or Custom...: then a modifier dropdown and a
            # "press a key" box appear underneath. The box itself is never typed
            # into, so a stray edit can't leave a half-written hotkey.
            presets = [(lbl, val) for lbl, val in field.labelled_choices]
            var = tk.StringVar(value=raw)          # the real hotkey string
            is_preset = any(val == raw for _l, val in presets)
            pick = tk.StringVar(value=next((l for l, v in presets if v == raw), CUSTOM_LABEL))
            widget = ttk.Combobox(row, textvariable=pick, font=F_BODY, state="readonly",
                                  values=[l for l, _v in presets] + [CUSTOM_LABEL], width=32)
            widget.pack(side="right", padx=(20, 0))
            custom = tk.Frame(parent, bg=BG)
            tk.Label(custom, text="Hold", font=F_LABEL, bg=BG, fg=TEXT).pack(side="left")
            mods, key = split_hotkey("" if is_preset else raw)
            mod_opts = list(HOTKEY_MODIFIERS)
            if mods and mods not in [v for _l, v in mod_opts]:
                mod_opts.append((" + ".join(m.title() for m in mods.split("+")), mods))
            mod_var = tk.StringVar(value=next(l for l, v in mod_opts if v == mods))
            mod_box = ttk.Combobox(custom, textvariable=mod_var, state="readonly", width=16,
                                   font=F_BODY, values=[l for l, _v in mod_opts])
            mod_box.pack(side="left", padx=(10, 10))
            tk.Label(custom, text="and press", font=F_LABEL, bg=BG, fg=TEXT).pack(side="left")
            capture = KeyCapture(custom, lambda k: compose())
            capture.pack(side="left", padx=(10, 0))
            capture.set(key)

            def compose(*_):
                m = next(v for l, v in mod_opts if l == mod_var.get())
                # No key yet: empty, so Save asks for one (never "ctrl+alt" alone).
                var.set("+".join(p for p in (m, capture.key) if p) if capture.key else "")

            def on_pick(*_):
                label = pick.get()
                if label == CUSTOM_LABEL:
                    custom.pack(fill="x", padx=28, pady=(10, 0), after=row)
                    compose()
                    if not capture.key:
                        capture.focus_set()
                else:
                    custom.pack_forget()
                    var.set(next(v for l, v in presets if l == label))
            mod_var.trace_add("write", compose)
            pick.trace_add("write", on_pick)
            if not is_preset:
                custom.pack(fill="x", padx=28, pady=(10, 0))
            field.hotkey_capture = capture
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
            # One line per item, under the label (a row list is too tall to sit
            # beside it). Saved comma-separated.
            var = None
            widget = RowList(left, [p.strip() for p in raw.split(",") if p.strip()],
                             self.mark_dirty, "Add a phrase", width=30)
            widget.pack(anchor="w", pady=(8, 0))
        elif field.kind == "secret":
            var = tk.StringVar(value=raw)
            box = SecretEntry(row, var, width=30)
            box.pack(side="right", padx=(20, 0))
            widget = box.entry
            var.trace_add("write", lambda *_: self.mark_dirty())
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
            if getattr(self, "_trig_expander", None) is not None:
                self._trig_expander.relabel()

        # Vocabulary
        self._phone_heading(
            parent, "Vocabulary",
            "Names, brands and jargon Whisper should spell correctly, one per box "
            "(e.g. Lesley, Ollama, Kubernetes). Keep it to the words it actually gets "
            "wrong: Whisper only reads a limited amount.")
        self.vocab_list = RowList(parent, self._words_cfg.get("vocabulary", []), changed,
                                  "Add a word", width=30)
        self.vocab_list.pack(anchor="w", fill="x", padx=28, pady=(8, 0))
        self.hint_count = tk.Label(parent, text="", font=F_HELP, bg=BG, fg=MUTED,
                                   anchor="w")
        self.hint_count.pack(anchor="w", padx=28, pady=(4, 0))

        # Name corrections
        self._phone_heading(
            parent, "Corrections and shortcuts",
            "When Whisper writes what's on the left, it types what's on the right "
            "instead. Fix a name it always gets wrong, or say a short phrase to type "
            "something long or hard to type: a link, your email, your number. Whole "
            "words, any capitalization; dictation only (a plain transcription is never "
            "changed). Name-like fixes also teach Whisper the spelling.")
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", padx=28, pady=(8, 0))
        tk.Label(head, text="When I say", font=F_HELP, bg=BG, fg=MUTED, width=24,
                 anchor="w").pack(side="left")
        tk.Label(head, text="Type this", font=F_HELP, bg=BG, fg=MUTED,
                 anchor="w").pack(side="left", padx=(30, 0))
        self.corr_box = tk.Frame(parent, bg=BG)
        self.corr_box.pack(fill="x", padx=28)
        self.corr_rows = []
        self._corr_on_change = changed
        for heard, write_as in self._words_cfg.get("name_corrections", {}).items():
            self._add_correction_row(heard, write_as, changed)
        # A new list starts with three empty rows showing an example each.
        if not self.corr_rows:
            for _ in CORRECTION_EXAMPLES:
                self._add_correction_row("", "", changed)
        add = tk.Label(parent, text="+ Add a correction or shortcut", font=F_LABEL, bg=BG,
                       fg=ACCENT, cursor="hand2")
        add.pack(anchor="w", padx=28, pady=(8, 0))
        add.bind("<Button-1>", lambda e: (self._add_correction_row("", "", changed),
                                          self.corr_rows[-1][1].focus_set()))

        # Trigger phrases
        self._phone_heading(
            parent, "Trigger phrases",
            "Say a trigger phrase while dictating, then tell the AI what you want. "
            "Works for dictation on this PC and from phones paired to it.")
        ex = tk.Text(parent, font=F_HELP, bg=SURFACE, fg=TEXT, relief="flat", wrap="word",
                     padx=14, pady=10, height=4, width=72, cursor="arrow",
                     highlightthickness=0, borderwidth=0)
        ex.tag_configure("work", foreground=EXAMPLE_WORK)
        ex.tag_configure("ask", foreground=EXAMPLE_ASK)
        ex.tag_configure("phrase", font=(_FONT, -14, "bold"))
        for text, part in TRIGGER_EXAMPLE:
            ex.insert("end", text, (part,) if part else ())
        ex.config(state="disabled")
        ex.pack(anchor="w", padx=28, pady=(8, 0))
        tk.Label(parent, text=TRIGGER_ACTIONS_HELP, font=F_HELP, bg=BG, fg=MUTED,
                 anchor="w", justify="left", wraplength=560).pack(anchor="w", padx=28,
                                                                  pady=(8, 0))
        # The phrase list is long (21 by default) and rarely edited: folded
        # behind a one-line summary until wanted.
        holder = tk.Frame(parent, bg=BG)
        holder.pack(fill="x", padx=28, pady=(10, 0))
        more = tk.Frame(holder, bg=BG)
        head = tk.Frame(more, bg=BG)
        head.pack(fill="x", pady=(8, 0))
        tk.Label(head, text="Phrase", font=F_HELP, bg=BG, fg=MUTED, width=24,
                 anchor="w").pack(side="left")
        tk.Label(head, text="What it does", font=F_HELP, bg=BG, fg=MUTED,
                 anchor="w").pack(side="left", padx=(8, 0))
        self.trig_box = tk.Frame(more, bg=BG)
        self.trig_box.pack(fill="x")
        self.trig_rows = []
        for r in words_config.triggers.rows(self._words_cfg):
            self._add_trigger_row(r["phrase"], r["action"], changed)
        add = tk.Label(more, text="+ Add a trigger phrase", font=F_LABEL, bg=BG,
                       fg=ACCENT, cursor="hand2")
        add.pack(anchor="w", pady=(8, 0))
        add.bind("<Button-1>", lambda e: (self._add_trigger_row("", "edit", changed),
                                          self.trig_rows[-1][3].focus_set()))
        reset = tk.Label(more, text="Restore original phrases", font=F_HELP,
                         bg=BG, fg=ACCENT, cursor="hand2")
        reset.pack(anchor="w", pady=(6, 0))
        self._trig_expander = Expander(holder, more, self._trigger_summary,
                                       "Hide the phrase list")
        self._trig_expander.pack(anchor="w")
        reset.bind("<Button-1>", lambda e: self._reset_triggers(changed))
        self._refresh_hint_count()

    def _add_correction_row(self, heard, write_as, on_change):
        row = tk.Frame(self.corr_box, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        entries = []
        first_entry = None
        example = CORRECTION_EXAMPLES[len(self.corr_rows) % len(CORRECTION_EXAMPLES)]
        for value, width, hint in ((heard, 24, example[0]), (write_as, 36, example[1])):
            var = tk.StringVar(row, value=value)
            e = tk.Entry(row, textvariable=var, font=F_BODY, bg=RAISED, fg=TEXT,
                         relief="flat", insertbackground=ACCENT, width=width,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, borderwidth=5)
            e.pack(side="left", padx=(0, 6))
            Placeholder(e, var, "e.g. " + hint)
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

    def _reload_correction_rows(self):
        """Redraw the correction rows from self._words_cfg (after a save merged
        in edits made on the phone)."""
        for row, _e, _vars in list(self.corr_rows):
            row.destroy()
        self.corr_rows = []
        for heard, write_as in self._words_cfg.get("name_corrections", {}).items():
            self._add_correction_row(heard, write_as, self._corr_on_change)

    # --- Styles for specific apps (AI Editing page) ------------------------------

    APP_STYLE_LABELS = (("cleanup", "Cleanup"), ("tone", "Tone"),
                        ("rewrite", "Rewriting"), ("shorten", "Conciseness"),
                        ("punctuation", "Punctuation"), ("emoji", "Emoji"))

    def _build_app_styles(self, parent):
        self._phone_heading(
            parent, "Styles for specific apps",
            "Dictate differently depending on where you're typing: casual in Slack, "
            "professional in Outlook, exactly as said in a terminal. Pick a program "
            "that's open, or type its file name (e.g. slack.exe). Choices left on "
            "\"%s\" follow the settings above; tone, rewriting and the rest "
            "only matter when cleanup runs. For the hotkey and the floating mic button "
            "on this PC, from the next dictation (no restart)." % app_styles.SAME)
        self.app_style_box = tk.Frame(parent, bg=BG)
        self.app_style_box.pack(fill="x", padx=28, pady=(4, 0))
        self.app_style_rows = []
        self._app_styles_dirty = False
        for rule in app_styles.load():
            self._add_app_style_row(rule)
        add = tk.Label(parent, text="+ Add an app", font=F_LABEL, bg=BG, fg=ACCENT,
                       cursor="hand2", takefocus=1)
        add.pack(anchor="w", padx=28, pady=(8, 0))
        for ev in ("<Button-1>", "<space>", "<Return>"):
            add.bind(ev, lambda e: (self._add_app_style_row({"app": ""}, focus=True),
                                    self._app_styles_changed(), "break")[2])

    def _app_styles_changed(self, *_):
        self._app_styles_dirty = True
        self.mark_dirty()

    def _add_app_style_row(self, rule, focus=False):
        card = tk.Frame(self.app_style_box, bg=SURFACE)
        card.pack(fill="x", pady=(8, 0))
        top = tk.Frame(card, bg=SURFACE)
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(top, text="App", font=F_HELP, bg=SURFACE, fg=MUTED).pack(side="left")
        app_var = tk.StringVar(card, value=rule.get("app", ""))
        app_box = ttk.Combobox(top, textvariable=app_var, width=26, font=F_BODY,
                               values=app_styles.running_apps(),
                               postcommand=lambda: app_box.config(values=app_styles.running_apps()))
        app_box.pack(side="left", padx=(10, 0))
        app_var.trace_add("write", self._app_styles_changed)
        record = {"card": card, "app": app_var, "opts": {}}

        def remove(_e=None):
            card.destroy()
            self.app_style_rows.remove(record)
            self._app_styles_changed()
            return "break"
        x = tk.Label(top, text="Remove", font=F_HELP, bg=SURFACE, fg=ACCENT, cursor="hand2",
                     takefocus=1)
        x.pack(side="right")
        for ev in ("<Button-1>", "<space>", "<Return>"):
            x.bind(ev, remove)

        grid = tk.Frame(card, bg=SURFACE)
        grid.pack(fill="x", padx=14, pady=(8, 12))
        for i, (key, label) in enumerate(self.APP_STYLE_LABELS):
            cell = tk.Frame(grid, bg=SURFACE)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 14), pady=(0, 6))
            tk.Label(cell, text=label, font=F_HELP, bg=SURFACE, fg=MUTED).pack(anchor="w")
            choices = [app_styles.SAME] + [lbl for _v, lbl in app_styles.OPTIONS[key]]
            current = dict(app_styles.OPTIONS[key]).get(rule.get(key, ""), app_styles.SAME)
            var = tk.StringVar(card, value=current)
            ttk.Combobox(cell, textvariable=var, values=choices, state="readonly",
                         width=17, font=F_BODY).pack(anchor="w", pady=(2, 0))
            var.trace_add("write", self._app_styles_changed)
            record["opts"][key] = var
        self.app_style_rows.append(record)
        if focus:
            app_box.focus_set()

    def collect_app_styles(self):
        """(rules, error) from the page."""
        rules, seen = [], set()
        for rec in self.app_style_rows:
            app = app_styles.normalize_app(rec["app"].get())
            if not app:
                return None, "Styles for specific apps: pick or type an app, or remove the row."
            if app in seen:
                return None, "Styles for specific apps: %s is listed twice." % app
            seen.add(app)
            rule = {"app": app}
            for key, var in rec["opts"].items():
                by_label = {lbl: v for v, lbl in app_styles.OPTIONS[key]}
                if var.get() in by_label:
                    rule[key] = by_label[var.get()]
            rules.append(rule)
        return rules, None

    def _trigger_summary(self):
        """'Show all 21 phrases (2 AI edit, 18 mean mode, 1 end of instruction)'."""
        labels = words_config.triggers.ACTION_LABELS
        counts = {}
        for _r, _p, a, _e in getattr(self, "trig_rows", []):
            counts[a.get()] = counts.get(a.get(), 0) + 1
        parts = ["%d %s" % (counts[labels[k]], labels[k] if k == "edit" else labels[k].lower())
                 for k in ("edit", "mean", "end") if counts.get(labels[k])]
        n = sum(counts.values())
        return "Show all %d phrase%s%s" % (n, "" if n == 1 else "s",
                                          " (%s)" % ", ".join(parts) if parts else "")

    def _add_trigger_row(self, phrase, action, on_change):
        labels = words_config.triggers.ACTION_LABELS
        row = tk.Frame(self.trig_box, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        pvar = tk.StringVar(row, value=phrase)
        e = _entry(row, pvar, 24)
        e.pack(side="left", padx=(0, 8))
        avar = tk.StringVar(row, value=labels.get(action, labels["edit"]))
        box = ttk.Combobox(row, textvariable=avar, state="readonly", width=18,
                           font=F_BODY, values=[labels[k] for k in ("edit", "mean", "end")])
        box.pack(side="left")
        pvar.trace_add("write", lambda *_: on_change())
        avar.trace_add("write", lambda *_: on_change())
        record = (row, pvar, avar, e)

        def remove(_e=None):
            row.destroy()
            self.trig_rows.remove(record)
            on_change()
        x = tk.Label(row, text="✕", font=F_BODY, bg=BG, fg=MUTED, cursor="hand2")
        x.pack(side="left", padx=(10, 0))
        Tip(x, "Remove")
        x.bind("<Button-1>", remove)
        self.trig_rows.append(record)

    def _reset_triggers(self, on_change):
        for row, *_ in list(self.trig_rows):
            row.destroy()
        self.trig_rows = []
        for r in words_config.triggers.DEFAULT_PHRASES:
            self._add_trigger_row(r["phrase"], r["action"], on_change)
        on_change()

    def collect_triggers(self):
        """(rows, error) from the trigger list."""
        by_label = {v: k for k, v in words_config.triggers.ACTION_LABELS.items()}
        rows = [{"phrase": p.get(), "action": by_label.get(a.get(), "edit")}
                for _r, p, a, _e in self.trig_rows]
        return words_config.triggers.validate(rows)

    def collect_words(self):
        """(vocabulary, corrections, error) from the page. Tk thread only."""
        vocab = words_config.clean_vocabulary(self.vocab_list.values())
        corr, err = words_config.clean_corrections(
            [(h.get(), w.get()) for _row, _e, (h, w) in self.corr_rows])
        return vocab, corr, err

    def _refresh_hint_count(self):
        vocab, corr, _err = self.collect_words()
        corr = corr or {}
        n = words_config.hint_chars(vocab, corr)
        budget = words_config.HINT_BUDGET_CHARS
        over = n > budget
        words = len(dict.fromkeys(vocab + words_config.shortcuts.spelling_hints(corr)))
        self.hint_count.config(
            text="%d words · %d of ~%d characters Whisper reads%s" % (
                words, n, budget,
                ": the words at the end will be ignored" if over else ""),
            fg=WARN if over else MUTED)

    # --- Model picker (Transcription section) --------------------------------

    def _model_root(self):
        return model_catalog.models_dir(
            {"WHISPER_MODELS_DIR": self.fields["WHISPER_MODELS_DIR"].var.get()})

    def _build_model_picker(self, parent):
        # The model folder: a setting most people never touch, so it's one line
        # under the list (with Open and Change) rather than a field of its own.
        folder = Field("WHISPER_MODELS_DIR", "Model folder", "text", default="models")
        folder.var = tk.StringVar(value=self.values.get("WHISPER_MODELS_DIR", "") or "models")
        folder.var.trace_add("write", lambda *_: self.mark_dirty())
        self.fields[folder.key] = folder
        self._phone_heading(
            parent, "Models",
            "Every Whisper model adds punctuation and capitals on its own, without "
            "you saying \"comma\" or \"period\"; bigger models do it more reliably. "
            ".en models are English-only and a little more accurate in English. "
            "Download a model, then Use it and Save & Restart API.")
        self._model_rows = {}
        self._model_frames = {}
        self._models_all = False
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
            self._model_frames[info.name] = row
        # Only the models that matter here are listed until asked for the rest.
        self._models_more = tk.Label(parent, text="", font=F_LABEL, bg=BG, fg=ACCENT,
                                     cursor="hand2", takefocus=1, highlightthickness=1,
                                     highlightbackground=BG, highlightcolor=TEXT)
        self._models_more.pack(anchor="w", padx=28, pady=(10, 0))
        for ev in ("<Button-1>", "<space>", "<Return>"):
            self._models_more.bind(ev, lambda e: (self._show_all_models(not self._models_all),
                                                  "break")[1])
        self.fields["WHISPER_MODEL_SIZE"].var.trace_add(
            "write", lambda *_: self._refresh_models())
        self.fields["WHISPER_MODELS_DIR"].var.trace_add(
            "write", lambda *_: self._refresh_models())
        line = tk.Frame(parent, bg=BG)
        line.pack(fill="x", padx=28, pady=(10, 0))
        tk.Label(line, text="Saved in", font=F_HELP, bg=BG, fg=MUTED).pack(side="left")
        self._folder_label = tk.Label(line, text="", font=F_HELP, bg=BG, fg=TEXT,
                                      anchor="w")
        self._folder_label.pack(side="left", padx=(6, 0))
        for text, cmd in (("Change...", self._change_model_folder),
                          ("Open", self._open_model_folder)):
            link = tk.Label(line, text=text, font=F_HELP, bg=BG, fg=ACCENT, cursor="hand2")
            link.pack(side="right", padx=(12, 0))
            link.bind("<Button-1>", lambda e, c=cmd: c())
        folder.widget = self._folder_label
        folder.var.trace_add("write", lambda *_: self._show_model_folder())
        self._show_model_folder()
        self._refresh_models()

    def _show_model_folder(self):
        path = os.path.normpath(self._model_root() or "")
        shown = path if len(path) <= 60 else "..." + path[-57:]
        self._folder_label.config(text=shown)

    def _open_model_folder(self):
        path = self._model_root()
        if path:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)

    def _change_model_folder(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self.root, title="Where to keep Whisper models",
                                         initialdir=self._model_root() or PROJECT_DIR)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        inside = os.path.normpath(PROJECT_DIR) + os.sep
        # Inside the app folder: store it relative, so moving the app keeps working.
        self.fields["WHISPER_MODELS_DIR"].var.set(
            os.path.relpath(chosen, PROJECT_DIR) if chosen.startswith(inside) else chosen)
        self.status.config(text="Models already downloaded stay in the old folder; "
                                "download again or move them there yourself.", fg=MUTED)

    def _highlight_models(self, on):
        if hasattr(self, "_models_box"):
            color = ACCENT if on else BG
            self._models_box.config(highlightbackground=color, highlightcolor=color)

    def _scroll_to_models(self):
        self._show_all_models(True)
        self.root.update_idletasks()
        top = self._models_box.winfo_rooty() - self.inner.winfo_rooty() - 60
        self.canvas.yview_moveto(max(0.0, top / max(1, self.inner.winfo_height())))
        # A short flash so the eye lands on the list after the jump.
        self._highlight_models(True)
        self.root.after(900, lambda: self._highlight_models(False))

    def _show_all_models(self, on):
        self._models_all = on
        self._refresh_models()

    def _refresh_models(self):
        if not hasattr(self, "_model_rows"):
            return
        root = self._model_root()
        active = self.fields["WHISPER_MODEL_SIZE"].var.get().strip()
        # Folded: the selected model, any downloaded ones, and the recommended.
        shown = [i.name for i in model_catalog.CATALOG
                 if self._models_all or i.name == active or i.name in MODELS_FEATURED
                 or model_catalog.is_downloaded(i, root)]
        for row in self._model_frames.values():
            row.pack_forget()
        for name in shown:
            self._model_frames[name].pack(fill="x", pady=(6, 0))
        hidden = len(model_catalog.CATALOG) - len(shown)
        if self._models_all:
            set_link_chevron(self._models_more, "Show fewer models", up=True)
        elif hidden:
            set_link_chevron(self._models_more, "Show all %d models (%d more)" % (
                len(model_catalog.CATALOG), hidden), up=False)
        else:
            self._models_more.config(text="", image="")
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

    def _live_int(self, key, saved):
        raw = self.fields[key].var.get().strip()
        return int(raw) if raw.isdigit() and 1024 <= int(raw) <= 65535 else int(saved)

    def _live_ports(self):
        """(https_on, https_port, plain_on, plain_port) as shown on the page
        (unsaved edits included). Plain is forced on when HTTPS is off."""
        https_on = bool(self.fields["SERVER_HTTPS"].var.get())
        plain_on = bool(self.fields["SERVER_PLAIN_HTTP"].var.get()) or not https_on
        return (https_on, self._live_int("WHISPER_HTTPS_PORT", local_api.https_port(self.values)),
                plain_on, self._live_int("WHISPER_API_PORT", local_api.port(self.values)))

    def _live_port(self):
        """The port phones pair to, as shown on the page."""
        https_on, https_port, _p, plain_port = self._live_ports()
        return https_port if https_on else plain_port

    def _go_to_field(self, section, key):
        """Jump to a setting on another page and put the cursor in it."""
        self.show_section(section)
        fold = getattr(self.fields[key], "fold", None)
        if fold is not None and not fold.opened:
            fold.toggle()
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

    def _build_phone_panel(self, page):
        # Shown top to bottom in the order people use them: get the app, pair
        # it, then other programs, the key, and the port settings last. Built in
        # a different order (pairing needs the port fields), so each part has
        # its own frame.
        app_f, pair_f, addr_f, key_f, ports_f = (tk.Frame(page, bg=BG) for _ in range(5))
        for f in (app_f, pair_f, addr_f, key_f, ports_f):
            f.pack(fill="x")

        def field(f, raw=None):
            if raw is not None:
                self.values.setdefault(f.key, raw)
            self._build_field(parent, f, None)
            self.fields[f.key] = f
            return f

        # --- Ports ---
        parent = ports_f
        self._phone_heading(
            parent, "Ports and encryption",
            "This PC's server listens on two ports. Phones use the encrypted one. "
            "Other programs on your network can use the unencrypted one, the way "
            "most self-hosted AI tools work. Changes apply after Save & Restart API.")
        field(Field(
            "SERVER_HTTPS", "Encrypted port (HTTPS), for phones", "toggle",
            "Uses a security certificate this PC makes for itself. The pairing code "
            "carries its fingerprint, so your phone trusts this PC and nothing "
            "pretending to be it. Phones need Whisper Unchained 1.3 or later, and "
            "must scan the pairing code again after this or its port changes.",
            default="false"))
        field(Field("WHISPER_HTTPS_PORT", "Encrypted port number", "port",
                    "Change it only if another program already uses it.",
                    default="5001"))
        field(Field(
            "SERVER_PLAIN_HTTP", "Unencrypted port, for other programs", "toggle",
            "For programs on your network that can't use this PC's own certificate. "
            "It only answers this PC, your home network and Tailscale (which encrypts "
            "on its own); requests from the internet are refused. On your home "
            "Wi-Fi, what's sent on this port can be read by other devices on it. "
            "Always on while the encrypted port is off.", default="true"),
            raw="true" if local_api.plain_enabled(self.values) else "false")
        field(Field("WHISPER_API_PORT", "Unencrypted port number", "port",
                    "5000 is the usual choice. Change it only if another program "
                    "already uses it.", default="5000"))
        fp = local_api.fingerprint()
        if fp:
            pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=28, pady=(14, 0))
            left = tk.Frame(row, bg=BG)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(left, text="Certificate fingerprint", font=F_LABEL, bg=BG,
                     fg=TEXT, anchor="w").pack(anchor="w")
            tk.Label(left, text="%s \u2026 %s" % (pretty[:8], pretty[-8:]), font=F_HELP,
                     bg=BG, fg=MUTED, anchor="w").pack(anchor="w", pady=(3, 0))
            tk.Label(left, text="Another PC using this one as its server needs it "
                                "(its Transcription page).", font=F_HELP, bg=BG,
                     fg=MUTED, anchor="w", justify="left", wraplength=360).pack(anchor="w")
            Button(row, "Copy", lambda: self._copy(pretty, "Certificate fingerprint copied.")
                   ).pack(side="right", padx=(20, 0))
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))

        # --- Addresses ---
        parent = addr_f
        self._phone_heading(
            parent, "Connect another program",
            "Answer the three questions and copy the address it builds. Changing "
            "the ports or the API key below updates it too.")
        self._finder = {}
        for key, label, options in self._finder_questions():
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=28, pady=(10, 0))
            tk.Label(row, text=label, font=F_LABEL, bg=BG, fg=TEXT, width=22,
                     anchor="w").pack(side="left")
            var = tk.StringVar(row, value=options[0][1])
            box = ttk.Combobox(row, textvariable=var, state="readonly", width=52,
                               font=F_BODY, values=[o[1] for o in options])
            box.pack(side="left")
            var.trace_add("write", lambda *_: self._refresh_addresses())
            self._finder[key] = (var, box, options)
        self._addr_box = tk.Frame(parent, bg=SURFACE)
        self._addr_box.pack(fill="x", padx=28, pady=(12, 0))
        self._addr_note = tk.Label(parent, text="", font=F_HELP, bg=BG, fg=WARN,
                                   anchor="w", justify="left", wraplength=560)
        self._addr_note.pack(anchor="w", padx=28, pady=(4, 0))
        for key in ("SERVER_HTTPS", "SERVER_PLAIN_HTTP", "WHISPER_HTTPS_PORT",
                    "WHISPER_API_PORT"):
            self.fields[key].var.trace_add("write", lambda *_: (self._refresh_addresses(),
                                                                self._refresh_qr()))

        # --- API key ---
        parent = key_f
        key_field = Field("WHISPER_API_KEY", "API key", "secret")
        self.fields[key_field.key] = key_field
        self._phone_heading(
            parent, "API key",
            "Like a password for this server. Your phone gets it by scanning the "
            "pairing code; other programs need it pasted in. Changes take effect "
            "after Save & Restart API.")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=28, pady=(10, 0))
        key_field.var = tk.StringVar(value=self.values.get("WHISPER_API_KEY", ""))
        box = SecretEntry(row, key_field.var, width=36)
        box.pack(side="left")
        key_field.widget = box.entry
        key_field.var.trace_add("write", lambda *_: (self.mark_dirty(), self._refresh_qr(),
                                                     self._refresh_addresses()))
        Button(row, "Generate", lambda: key_field.var.set(generate_api_key())).pack(
            side="left", padx=(10, 0))
        Button(row, "Copy", lambda: self._copy(key_field.var.get().strip(), "API key copied.",
                                               empty="No API key to copy.")).pack(
            side="left", padx=(6, 0))
        Button(row, "Remove", lambda: key_field.var.set("")).pack(
            side="left", padx=(6, 0))
        required = field(Field(
            "WHISPER_API_KEY_REQUIRED", "Require the API key", "toggle",
            "On: only devices and programs with the API key can use the server. Off: "
            "anything that can reach it can, for example other programs you want "
            "to use your Whisper with. The key stays saved either way, so devices "
            "that have it keep working when you switch back.",
            default="true"))
        required.var.trace_add("write", lambda *_: (self._refresh_qr(),
                                                    self._refresh_addresses()))
        self._refresh_addresses()

        # Pairing QR.
        parent = pair_f
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
        tk.Label(row, text=":", font=F_BODY, bg=BG, fg=MUTED).pack(side="left")
        self._pair_port = tk.Label(row, text="", font=F_BODY, bg=BG, fg=TEXT)
        self._pair_port.pack(side="left")
        self._refresh_addresses()

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

        # Downloads.
        parent = app_f
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

    def _copy(self, text, done, empty="Nothing to copy."):
        if not text:
            self.status.config(text=empty, fg=WARN)
            return
        try:
            import clipboard_win
            # Kept out of clipboard history, like dictations (keys are secrets).
            clipboard_win.set_text(text, private=True)
        except Exception:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        self.status.config(text=done, fg=ACCENT)

    # The "Connect another program" finder: three questions -> one address.
    FINDER_WHERE = (
        ("this_pc", "This PC"),
        ("home", "Another device on my home network"),
        ("tailscale", "A device on my Tailscale (at home or away)"),
    )
    FINDER_WHAT = (
        # key, label, path after the port, how to use it
        ("plain", "Exactly what was said (transcription)", "/v1",
         "Paste it as the program's OpenAI base URL (\"OpenAI-compatible\"); the "
         "program adds /audio/transcriptions itself. Any model name works. Nothing "
         "is corrected, cleaned up or logged."),
        ("dictate", "Dictation: my name corrections, trigger phrases and cleanup", "/dictate/v1",
         "Paste it as the program's OpenAI base URL. Text comes back the way your "
         "dictation does: name corrections, trigger phrases, and cleanup if it's on "
         "in AI Editing. Logged like your dictations."),
        ("sentences", "Exactly what was said, with a time for each sentence",
         "/transcribe_time_stamped",
         "Not the OpenAI format: the program sends the audio as a form upload in a "
         "field named \"file\" and gets JSON back: text, plus segments with start "
         "and end times in seconds."),
        ("words", "Exactly what was said, with a time for each word", "/transcribe_words",
         "Not the OpenAI format: the program sends the audio as a form upload in a "
         "field named \"file\" and gets JSON back with a start and end time for "
         "every word (what aMemory uses for calls)."),
    )

    def _finder_questions(self):
        conn = [("http", "Not encrypted (http): works in any program"),
                ("https", "Encrypted (https): the program must trust this PC's certificate")]
        return (("where", "The program runs on", list(self.FINDER_WHERE)),
                ("what", "It should get", [(k, l) for k, l, _p, _h in self.FINDER_WHAT]),
                ("conn", "Connection", conn))

    def _finder_choice(self, key):
        var, _box, options = self._finder[key]
        return next((k for k, label in options if label == var.get()), options[0][0])

    def _address_hosts(self):
        """{where: address}. The home network address is the one Windows actually
        routes through (not virtual adapters like WSL's)."""
        from pairing import primary_lan_ip, tailscale_ips
        if not hasattr(self, "_hosts_cache"):
            ts = tailscale_ips()
            self._hosts_cache = {"this_pc": "127.0.0.1", "home": primary_lan_ip(),
                                 "tailscale": ts[0] if ts else ""}
        return self._hosts_cache

    def finder_address(self):
        """(url or "", list of notes) for the finder's current answers."""
        https_on, https_port, plain_on, plain_port = self._live_ports()
        where, what, conn = (self._finder_choice(k) for k in ("where", "what", "conn"))
        host = self._address_hosts().get(where)
        path, how = next((p, h) for k, _l, p, h in self.FINDER_WHAT if k == what)
        notes = [how]
        if not host:
            return "", ["Tailscale isn't running on this PC, so there's no Tailscale "
                        "address. Install or start Tailscale, then open Settings again."]
        if conn == "https" and not https_on:
            return "", ["The encrypted port is off (Ports and encryption, below)."]
        if conn == "http" and not plain_on:
            return "", ["The unencrypted port is off (Ports and encryption, below)."]
        url = ("https://%s:%d%s" % (host, https_port, path) if conn == "https"
               else "http://%s:%d%s" % (host, plain_port, path))
        if conn == "https":
            notes.append("Encrypted. The program must trust this PC's own certificate: "
                         "give it api\\certs\\server.pem, or the fingerprint (below).")
        elif where == "this_pc":
            notes.append("Not encrypted, but it never leaves this PC.")
        elif where == "tailscale":
            notes.append("Not encrypted by this server, but Tailscale encrypts the "
                         "connection itself, so it's still private.")
        else:
            notes.append("Not encrypted: other devices on your home network could read "
                         "it. Fine for most homes; choose Encrypted if the program can.")
        key = self.fields["WHISPER_API_KEY"].var.get().strip()
        if not key or not self.fields["WHISPER_API_KEY_REQUIRED"].var.get():
            notes.append("API key: not required right now, so the program needs none.")
        else:
            notes.append("API key: required. Give the program the key below (Copy). "
                         "Programs that ask for an \"API key\" send it for you; others "
                         "need the header  Authorization: Bearer <key>.")
        return url, notes

    def _refresh_addresses(self):
        if not hasattr(self, "_addr_box") or not hasattr(self, "_finder"):
            return
        for w in self._addr_box.winfo_children():
            w.destroy()
        url, notes = self.finder_address()
        row = tk.Frame(self._addr_box, bg=SURFACE)
        row.pack(fill="x", padx=14, pady=(12, 0))
        copy = Button(row, "Copy", lambda: self._copy(url, "Copied %s" % url))
        copy.pack(side="right")
        copy.set_enabled(bool(url))
        tk.Label(row, text=url or "No address for this choice", font=F_BUTTON, bg=SURFACE,
                 fg=TEXT if url else MUTED, anchor="w").pack(side="left", fill="x")
        for n in notes:
            tk.Label(self._addr_box, text=n, font=F_HELP, bg=SURFACE, fg=MUTED, anchor="w",
                     justify="left", wraplength=600).pack(anchor="w", padx=14, pady=(6, 0))
        tk.Frame(self._addr_box, bg=SURFACE, height=12).pack()
        https_on, https_port, plain_on, plain_port = self._live_ports()
        saved = (local_api.https_enabled(self.values), int(local_api.https_port(self.values)),
                 local_api.plain_enabled(self.values), int(local_api.port(self.values)))
        self._addr_note.config(
            text="" if (https_on, https_port, plain_on, plain_port) == saved
            else "The ports changed: this address works after Save & Restart API.")
        if hasattr(self, "_pair_port"):
            self._pair_port.config(text="%d  (%s port)" % (
                self._live_port(), "encrypted" if https_on else "unencrypted"))

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
            https_live = bool(self.fields["SERVER_HTTPS"].var.get())
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
            if self._live_port() != int(local_api.pairing_port(self.values)):
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
        foot._no_round = True   # spans the window edge to edge
        foot.pack(fill="x", side="bottom")
        inner = tk.Frame(foot, bg=SURFACE)
        inner.pack(fill="x", padx=28, pady=14)

        self.status = tk.Label(inner, text="", font=F_BODY, bg=SURFACE, fg=MUTED,
                               anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

        # Windows order: the main action first, Close last (right-most).
        Button(inner, "Close", self.on_close, kind="ghost").pack(side="right")
        self.btn_save = Button(inner, "Save", self.save, kind="ghost")
        self.btn_save.pack(side="right", padx=(0, 8))
        self.btn_restart = Button(inner, "Save & Restart API", self.save_and_restart,
                                  kind="primary")
        self.btn_restart.pack(side="right", padx=(0, 8))
        self.set_saved_state(True)

    # --- behaviour ---------------------------------------------------------

    def show_section(self, name):
        self.current_section = name
        for f in self.frames.values():
            f.pack_forget()
        self.frames[name].pack(fill="both", expand=True)
        for n, (row, bar, lbl) in self.nav.items():
            active = n == name
            bg = RAISED if active else SURFACE
            bar.config(bg=ACCENT if active else bg)
            lbl.config(fg=TEXT if active else MUTED, bg=bg,
                       font=(_FONT_SB, -15) if active else F_TAB)
            row.config(bg=bg)
            if getattr(row, "_corners", None) is not None:
                row._corners.refresh()
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
                phrases = [p.replace(",", " ").strip() for p in f.widget.values()]
                updates[key] = ",".join(dict.fromkeys(p for p in phrases if p))
                continue
            raw = f.var.get()
            if f.labelled_choices:
                raw = next((val for lbl, val in f.labelled_choices if lbl == raw),
                           raw)
            value, err = f.coerce(raw)
            if err:
                return None, err
            updates[key] = value
        if updates.get("SERVER_HTTPS") == "true" and updates.get("SERVER_PLAIN_HTTP") == "true" \
                and updates.get("WHISPER_API_PORT") == updates.get("WHISPER_HTTPS_PORT"):
            return None, ("Connections: the encrypted and unencrypted ports need two "
                          "different numbers")
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
        if not err and words_dirty:
            trig, err = self.collect_triggers()
        styles_dirty = getattr(self, "_app_styles_dirty", False)
        if not err and styles_dirty:
            rules, err = self.collect_app_styles()
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
                words_config.save(vocab, corr, loaded_vocabulary=self._words_cfg.get("vocabulary", []),
                                  loaded_corrections=self._words_cfg.get("name_corrections", {}),
                                  triggers=trig)
                self._words_cfg = words_config.load()
                # Show what was saved, including edits the phone made meanwhile.
                self.vocab_list.set(self._words_cfg.get("vocabulary", []))
                self._reload_correction_rows()
                self._words_dirty = False
            except Exception as e:
                self.status.config(text="Could not save the word lists: %s" % e, fg=DANGER)
                return False
        if styles_dirty:
            try:
                app_styles.save(rules)
                self._app_styles_dirty = False
            except Exception as e:
                self.status.config(text="Could not save the app styles: %s" % e, fg=DANGER)
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
            # The firewall rule from install_service.ps1 follows the ports too.
            plain, https = api_port(self.values), int(local_api.https_port(self.values))
            open_ports = ([str(https)] if local_api.https_enabled(self.values) else []) + \
                ([str(plain)] if local_api.plain_enabled(self.values) else [])
            # Recreated rather than edited, so an install whose rule is missing
            # (or older) gets one. Same scope as install_service.ps1.
            rule = "'Whisper Unchained Server'"
            command = ("& '%s' set %s AppParameters '-m api.serve --host 0.0.0.0 "
                       "--port %d --https-port %d'; Get-NetFirewallRule -DisplayName %s "
                       "-ErrorAction SilentlyContinue | Remove-NetFirewallRule; "
                       "New-NetFirewallRule -DisplayName %s -Direction Inbound -Action Allow "
                       "-Protocol TCP -LocalPort %s -RemoteAddress LocalSubnet,'100.64.0.0/10' "
                       "-Profile Any | Out-Null; %s"
                       % (nssm, SERVICE_NAME, plain, https, rule, rule,
                          ",".join(open_ports), command))
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
