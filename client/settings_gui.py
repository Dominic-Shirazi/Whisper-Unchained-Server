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
import tempfile

import console_guard  # noqa: E402,F401  (no console under pythonw: see the module)
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
import secrets_store  # api/secrets_store.py (words_config puts api/ on sys.path)
import engine_files      # api/engine_files.py: the whisper.cpp engine's downloads
import whispercpp_engine  # api/whispercpp_engine.py: only its file and name checks
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "installer"))
import hardware          # installer/hardware.py: which graphics this PC has
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
SERVICE_NAME = "WhisperUnchained"   # see api/service_name.py (old PCs: FasterWhisperAPI)

# --- Palette and fonts: client/theme.py (shared with every window) ----------
import theme  # noqa: E402
from theme import (BG, SURFACE, RAISED, HOVER, BORDER, TEXT, MUTED, FAINT,  # noqa: E402,F401
                   ACCENT, ACCENT_H, ON_ACCENT, DANGER, WARN)
_FONT, _FONT_SB, _FONT_DISPLAY = theme.FONT, theme.FONT_SB, theme.FONT_DISPLAY
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
    ("This PC", "local"),
    ("Another PC of mine", "remote"),
    ("A cloud service", "cloud"),
]
CLOUD_PROVIDER_CHOICES = [(label, key) for key, (label, _u, _m)
                          in transcribe_client.CLOUD_PRESETS.items()]
SOURCE_INTROS = {
    "local": "What you say is turned into text on this PC. Nothing you say leaves it.",
    "remote": "What you say is sent to another PC of yours, which turns it into text.\n"
              "• That PC uses its own word lists and skills",
    "cloud": "What you say is sent to an online service, which turns it into text. "
             "The audio leaves this PC.\n"
             "• Your word list still applies; skills and AI clean-up do not work "
             "this way yet",
}
# Pages that only matter when this PC runs the server itself.
LOCAL_ONLY_SECTIONS = {"AI Editing (LLM)", "Audio Corpus", "Denoise", "Devices & Apps"}
# The word lists and trigger phrases live on the PC that transcribes. When that
# is another PC, "Words" is hidden and "Triggers" reads and saves through it.
WORDS_SECTIONS = ("Words", "Triggers")
REMOTE_HIDDEN_SECTIONS = ("Words",)

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
                 labelled_choices=None, default="", group=None, emphasis="", view=None,
                 compact=False, tip=""):
        self.key = key
        # More detail, shown when the pointer rests on the label.
        self.tip = tip
        # A shorter row: a wide help line and no rule under it, for a page
        # that has to fit the window without scrolling.
        self.compact = compact
        # Built into one of the Transcription page's "Advanced" views instead
        # of the page itself (see _view_target).
        self.view = view
        # A short italic line under the help text, for the one fact that matters.
        self.emphasis = emphasis
        # Fields sharing a group are shown or hidden together (see _show_group).
        self.group = group
        # Shown when .env has no line for this key yet (newer settings).
        self.default = default
        self.label = label
        # toggle | choice | pick | hotkey | text | otext (optional text) |
        # secret (optional, masked) | int | port | float | list |
        # apps (programs picked from a list, saved comma-separated)
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

# --- The "This PC" card (Transcription page) --------------------------------------
# One statement about what was found and what dictation runs on. Which speech
# engine that means (faster-whisper or whisper.cpp) is worked out from it;
# choosing by hand is behind "Advanced".
ENGINE_FW, ENGINE_CPP = "faster-whisper", "whisper.cpp"
ENGINE_CARD_TITLE = "This PC"
ENGINE_HEADING = "How dictation runs"
ENGINE_CHOICES = [
    (ENGINE_FW, "The usual way",
     "For NVIDIA graphics cards. Without one it uses the processor."),
    (ENGINE_CPP, "The second way",
     "Can also use AMD and Intel graphics cards. "
     "It is new and has only been tried on a few PCs so far."),
]
ENGINE_DEVICE_CHOICES = [("Automatic (recommended)", "auto"), ("Processor only", "cpu")]
ENGINE_AUTO_HELP = ("Automatic uses a separate graphics card when there is one. With "
                    "built-in graphics only, it times the graphics and the processor on "
                    "the first start and keeps the faster.")
ENGINE_SLOW_NOTE = ("On the processor the large model is very slow. The base model is "
                    "the better choice on this PC.")
# What the footer's two buttons are called, wherever a page points at them.
SAVE_RESTART = "Save and restart"
SAVE_RESTART_TIP = ("Saves your changes and restarts dictation so they take effect.\n"
                    "Windows asks for permission first. Dictation stops for up to\n"
                    "a minute while it restarts.")
SAVE_TIP = ("Saves your changes without restarting.\n"
            "Some changes only take effect after a restart.")
SAVED_RESTART = "Saved. Press %s to start using the changes." % SAVE_RESTART
SAVED_PLAIN = "Saved."
RESTART_ASKING = "Saved. Windows will ask for permission. Say Yes to restart dictation."
ENGINE_DOWNLOADED = "Downloaded. Press %s to use it." % SAVE_RESTART
ENGINE_USE_IT = "Press %s to use it." % SAVE_RESTART
ENGINE_SAVE_REFUSED = ("This PC still needs a one-time download of about %s. Press "
                       "Download on the Transcription page first.")
DOWNLOAD_NEEDED = "This PC needs a one-time download of about %s."
DOWNLOAD_BY_SETUP = "Setup will download about %s."
DOWNLOAD_PARTS = "The download is made of %s."
PENDING_READY = "Everything it needs is already on this PC."
NO_CARD_BUT_SET = ("No graphics card was found, but dictation is set to run on one. "
                   "It will not start until that is changed under Advanced.")
NOT_RIGHT_ADVANCED = "To choose by hand instead, press Advanced."
ACCURACY_OWN = "Accuracy: depends on your own model"
MODEL_HEADING = "Model"
MODEL_LINK = "Choose a different model"
MODELS_HELP = ("Bigger models are more accurate and need a stronger PC.\n"
               "• All of them add punctuation and capitals on their own\n"
               "• Press Use on the one you want, then %s" % SAVE_RESTART)
# The page has ONE "Advanced" button. It opens the view for where dictation
# runs (adv_local, adv_remote or adv_cloud); the model list is one step
# further in, and leads back to that view.
TX_ADVANCED = "Advanced"
TX_BACK_ADVANCED = "‹ Back to Advanced"
# The views behind "Advanced" on the Transcription page: (name, heading, help).
TX_BACK = "\u2039 Back to Transcription"
TX_VIEWS = (
    ("models", "Choose a model", MODELS_HELP),
    ("adv_local", "Advanced", "Most people never need to change these."),
    ("adv_remote", "Advanced",
     "A connection code fills these in. Type them only if you have no code."),
    ("adv_cloud", "Advanced", "Choosing a service fills these in."),
)
REMOTE_READY = "\u2713 The other PC is set up."
REMOTE_NOT_READY = "Not set up yet."
CLOUD_PRICE_LINK = "See its prices"
CLOUD_CUSTOM_NOTE = "Enter its address and model under Advanced."
# The project folder the engine's files live under (tests point it elsewhere).
ENGINE_ROOT = PROJECT_DIR


def _name_list(names):
    names = [n for n in names if n]
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else ""


def engine_device_text(found, last):
    """Plain lines for the card: the graphics hardware on this PC and, once
    the engine has started at least once, what Automatic chose then.

    found: names of the graphics hardware (may be empty).
    last:  the "last" record the engine keeps next to the models (see
           whispercpp_engine.write_auto_cache), or None before its first start."""
    first = ("Found on this PC: %s." % _name_list(found)) if found \
        else "No graphics card found on this PC."
    if not isinstance(last, dict) or last.get("device") not in ("gpu", "cpu"):
        return first
    if last["device"] == "gpu":
        second = "On its last start, Automatic used %s." % (last.get("name") or "the graphics")
        if last.get("gpu_seconds") is not None and last.get("cpu_seconds") is not None:
            second += (" It was faster than the processor in a short test (%.1f s "
                       "against %.1f s)." % (last["gpu_seconds"], last["cpu_seconds"]))
    else:
        second = "On its last start, Automatic used the processor."
        if last.get("gpu_seconds") is not None and last.get("cpu_seconds") is not None:
            second += (" It was faster than the graphics in a short test (%.1f s "
                       "against %.1f s)." % (last["cpu_seconds"], last["gpu_seconds"]))
        elif not last.get("found"):
            second += " The engine found no graphics it can use."
    return first + "\n" + second


def detected_graphics():
    """Names of the real graphics hardware in this PC (NVIDIA included)."""
    return hardware.real_graphics(hardware.display_adapters())

# Settings most people never change: folded under "Advanced settings" at the
# end of their page (same detail, less to read past).
ADVANCED_KEYS = {
    "KEEP_AUDIO_MAX", "KEEP_AUDIO_HARD_MAX", "KEEP_AUDIO_PIN_BELOW",
    "DENOISE_ATTEN_LIMIT_DB",
    "RESTART_INTERVAL_HOURS", "API_VISIBLE",
    "LISTENER_RESTART_INTERVAL_MINS", "LISTEN_DELAY_SECONDS",
    # AI Editing: which AI program is used and how to reach it. "Find an AI
    # program" at the top of that page fills these in.
    "EDIT_BACKEND", "OLLAMA_EDIT_API_URL", "OLLAMA_MODEL", "OLLAMA_EDIT_KEEP_ALIVE",
    "EDIT_LLM_BASE_URL", "EDIT_LLM_MODEL", "EDIT_LLM_API_KEY", "EDIT_LLM_NO_THINK",
}
GAME_MODE_TITLE = "Free up the graphics card while gaming"
# "Offer to remember my corrections" is on the Words page. When another PC
# does the work, Words is hidden (the lists live there), so the same switch
# also shows on the Transcription page then.
LEARN_HELP = ("Fix a word right after dictating and a small card asks whether to "
              "always make that correction.\n"
              "• Nothing is saved unless you press Always\n"
              "• %s\n"
              "• Password and sign-in boxes are never read")
LEARN_SAVED_HERE = "Saved ones are in the list below, where you can change or remove them"
LEARN_SAVED_REMOTE = ("Saved ones go to the other PC's Words page, where you can "
                      "change or remove them")
# AI Editing: the box that looks for an AI program and fills in the rest.
AI_FINDER_TITLE = "Find an AI program"
AI_FINDER_HELP = ("Looks for AI programs already running on this PC.\n"
                  "• Knows Ollama, LM Studio, llama.cpp, vLLM, Unsloth Studio, "
                  "Jan and KoboldCpp\n"
                  "• On another PC? Type that PC's address in the box first")
AI_FINDER_BUTTON = "Look for AI programs"
AI_FINDER_ENTRY_TIP = "Add :port after the address for an unusual port."
ADVANCED_FOLD = "Advanced settings"
ADVANCED_FOLD_HIDE = "Hide advanced settings"
# Devices & Apps: the second card, and the page's one Advanced fold (any other
# program, the switches that change its address, port numbers, the key).
PHONE_PC_CARD = "Another PC"
PHONE_PC_HELP = ("For another PC of yours that has Whisper Unchained. It can then use "
                 "this PC for dictation.")
PHONE_PC_STEPS = ("On the other PC: Settings > Transcription > Another PC of mine > Paste "
                  "connection code. The code contains your key, so treat it like a "
                  "password.\n"
                  "• Connecting a different program? Open Advanced, below")
PHONE_ADVANCED = "Advanced: other programs, the key and security"

# Models always listed under "Choose a model" (the rest fold away).
MODELS_FEATURED = ("large-v3-turbo", "small.en", "distil-large-v3.5")

# Light-gray italic examples shown in empty "Corrections and shortcuts" rows
# (only shown, never saved: a new install starts with no corrections).
CORRECTION_EXAMPLES = (
    ("Leslie", "Lesley"),
    ("here's my link", "here's my link: cyrus-studio.com"),
    ("my email address", "name@example.com"),
)

# The example, in pieces: (text, part). "work" = what to work on (blue),
# "phrase" = the trigger phrase (bold), "ask" = what to do with it (amber).
# Blue and amber stay apart for red/green colour blindness; the words say it too.
EXAMPLE_WORK, EXAMPLE_ASK = "#6CB4FF", "#F0B44C"
# One sentence in three parts, each with its name under it in the same colour:
# (what you say, what that part is, part).
TRIGGER_EXAMPLE_PARTS = (
    ("hey man, can you get me the report by friday", "message", "work"),
    ("prompt AI", "phrase", "phrase"),
    ("make it polite", "directions", "ask"),
)
TRIGGER_EXAMPLE_RESULT = "Hi! Could you please send me the report by Friday? Thank you."
TRIGGER_PHRASES_HELP = "Say one of these while dictating, then tell the AI what you want."
# What the user reads. The page is still "Triggers" in the code (SCHEMA,
# show_section, --section), and a skill is still a "custom trigger" in the
# word file and the server's API.
SECTION_LABELS = {"Triggers": "Skills", "AI Editing (LLM)": "AI Editing"}
ASK_CARD_TITLE = "Ask the AI to change your text"
ASK_CARD_BUTTON = "Choose your own phrase"
ASK_CARD_NO_PHRASE = "You have no phrase for asking the AI yet. Add one to ask by voice."
PHRASES_VIEW_TITLE = "What you say to ask the AI"
PHRASES_VIEW_BACK = "‹ Back to Skills"
SKILLS_HEADING = "Your skills"
NEW_SKILL_BUTTON = "+ New skill"
# In the "New skill" editor (trigger_dialogs.TriggerEditor), not on the page.
CUSTOM_TRIGGERS_HELP = (
    "A skill is your own phrase for an instruction you've saved. Say the "
    "phrase and the AI follows that instruction, so you never explain it twice.\n"
    "• The phrase can be anything you like: \"prompt intake\", \"skill intake\", "
    "or your own words")
# The example, in pieces: "phrase" = what to say (bold).
CUSTOM_TRIGGER_EXAMPLE = (
    ("Example: a client emails you their details in one long paragraph. Paste it "
     "into your notes, highlight it, click the mic button and say ", None),
    ("prompt intake", "phrase"),
    (". It comes back as your intake sheet: name, phone, email, address and date "
     "of birth for each person, in the same order every time, with \"unknown\" "
     "beside anything they didn't say.", None),
)
if sys.platform != "win32":
    import linux_settings
    CUSTOM_TRIGGER_EXAMPLE = linux_settings.trigger_example(CUSTOM_TRIGGER_EXAMPLE)
    SAVE_RESTART_TIP = linux_settings.SAVE_RESTART_TIP
    ADVANCED_KEYS |= linux_settings.ADVANCED_KEYS
# The library entry the example is about (trigger_library/library.json).
CUSTOM_TRIGGER_EXAMPLE_ID = "intake"
CUSTOM_TRIGGER_EXAMPLE_LINK = "Add \"Client intake sheet\" from the library"
CUSTOM_TRIGGERS_BULLETS = (
    "• Say a skill's phrase anywhere while dictating: everything else you said "
    "is what it works on\n"
    "• Or highlight text first, then say just the phrase\n"
    "• Mix them as you like: one skill can be \"prompt intake\", another "
    "\"skill formal email\"\n"
    "• A skill uses the AI set up in AI Editing. A stronger AI can do harder jobs")
CUSTOM_TRIGGERS_FOOT = "Looking for mean mode? It's a skill in the library, under Fun."
# The Triggers page when transcription runs on another PC (see _refresh_source).
REMOTE_TRIGGERS_NOTE = "These are saved on the other PC and apply to everything that uses it."
REMOTE_TRIGGERS_LOADING = "Loading from the other PC..."
REMOTE_TRIGGERS_SAVING = "Saving on the other PC..."
REMOTE_TRIGGERS_DISCARDED = "Unsaved skill changes for the other PC were discarded."
CUSTOM_TRIGGERS_EMPTY = ("None yet. Start with one from the library, or make your own.")


def first_edit_phrase(rows):
    """The first phrase that asks the AI for an edit, from {"phrase", "action"}
    rows or (phrase, action) pairs; "" when there is none."""
    for row in rows or ():
        if isinstance(row, dict):
            phrase, action = row.get("phrase"), row.get("action", "edit")
        else:
            phrase, action = row
        phrase = " ".join(str(phrase or "").split())
        if phrase and action == "edit":
            return phrase
    return ""


def ask_ai_lines(phrase):
    """(sentence, example) for the "Ask the AI to change your text" card, built
    from the user's own first phrase. The example is "" when there is none."""
    phrase = " ".join(str(phrase or "").split())
    if not phrase:
        return ASK_CARD_NO_PHRASE, ""
    return ('Say "%s", then what you want.' % phrase,
            '"%s, make this sound more formal."' % (phrase[:1].upper() + phrase[1:]))


def skill_summary(item):
    """What a skill's row says under its name: say "<first phrase>"."""
    phrases = [p for p in (item.get("phrases") or []) if str(p).strip()]
    if not phrases:
        return ""
    more = len(phrases) - 1
    return 'say "%s"%s' % (phrases[0], " (+%d more)" % more if more else "")


# The server's own messages (api/triggers.py, api/custom_triggers.py) still say
# "trigger": other programs read them too. Settings shows them in today's words.
_SKILL_WORDS = (
    ("a built-in trigger phrase", "one of your phrases for asking the AI"),
    ("Custom triggers", "Skills"), ("custom triggers", "skills"),
    ("Custom trigger", "Skill"), ("custom trigger", "skill"),
    ("Trigger phrases", "Phrases"), ("trigger phrases", "phrases"),
    ("Trigger phrase", "Phrase"), ("trigger phrase", "phrase"),
    ("Triggers", "Skills"), ("triggers", "skills"),
    ("Trigger", "Skill"), ("trigger", "skill"),
)


def skill_wording(message):
    """A message from the server or its checks, with "trigger" said as "skill".
    Anything in double quotes (the user's own names and phrases) is left alone."""
    if not message:
        return message
    parts = re.split(r'("[^"]*")', str(message))
    for i in range(0, len(parts), 2):
        for old, new in _SKILL_WORDS:
            parts[i] = parts[i].replace(old, new)
    return "".join(parts)

TRIGGER_ACTIONS_HELP = (
    "An \"End of instruction\" phrase is optional: say it when your directions are done. "
    "Capitals and commas don't matter.")


SCHEMA = [
    ("Transcription", [
        Field("TRANSCRIBE_SOURCE", "Where transcription runs", "choice",
              labelled_choices=SOURCE_CHOICES, default="local", compact=True),
        # --- This PC ---
        # The "This PC" card (see _build_engine_card): what was found, what
        # dictation runs on, and the one download when something is missing.
        Field("WHISPER_ENGINE", ENGINE_CARD_TITLE, "engine", group="src_local",
              default=ENGINE_FW),
        # No row of its own: the card names its accuracy, and the list behind
        # "Choose a different model" under Advanced is the one place to choose.
        Field("WHISPER_MODEL_SIZE", "Model", "model", group="src_local"),
        Field("WHISPER_DEVICE", "Runs on", "choice",
              "What does the work.\n"
              "• Automatic: the graphics card when it works, otherwise the processor\n"
              "• Graphics card: much faster; needs an NVIDIA card",
              labelled_choices=[("Automatic", "auto"),
                                ("Graphics card (NVIDIA)", "cuda"),
                                ("Processor (CPU)", "cpu")], group="src_local",
              view="adv_local_fw"),
        # --- Another PC ---
        # A button, not a setting (see _build_action_row): never saved.
        Field("_paste_code", "Connection code", "button",
              "Copied from the other PC's Settings > Devices & Apps. "
              "It fills in everything this PC needs.", group="src_remote"),
        Field("REMOTE_SERVER_URL", "Server address", "otext",
              "The other PC's Whisper Unchained Server, ending in /v1.\n"
              "• Copy it from that PC's Settings > Devices & Apps\n"
              "• e.g. https://100.x.y.z:5001/v1 (its Tailscale address)\n"
              "• That PC applies its own vocabulary, skills and cleanup",
              group="src_remote", view="adv_remote"),
        Field("REMOTE_API_KEY", "Server API key", "secret",
              "Only if that server has a key (its Settings > Devices & Apps page).",
              group="src_remote", view="adv_remote"),
        Field("REMOTE_CERT_SHA256", "Certificate fingerprint", "otext",
              "Only for an encrypted (https://) address.\n"
              "• Copy it from that server's Settings > Devices & Apps\n"
              "• This PC then trusts exactly that server",
              group="src_remote", view="adv_remote"),
        # --- Cloud ---
        Field("CLOUD_PROVIDER", "Service", "choice",
              labelled_choices=CLOUD_PROVIDER_CHOICES, default="groq", group="src_cloud",
              compact=True),
        Field("CLOUD_BASE_URL", "Service address", "otext",
              "Ends in /v1 (or /openai/v1 for Groq).", group="src_cloud",
              view="adv_cloud"),
        Field("CLOUD_API_KEY", "Your key", "secret",
              "You get it from the service when you sign up. It stays on this PC.",
              group="src_cloud", compact=True),
        Field("CLOUD_MODEL", "Model", "otext",
              "The name the service gives its speech model.\n"
              "• Your vocabulary is sent to it as hints\n"
              "• Name corrections run on this PC", group="src_cloud", view="adv_cloud"),
        Field("TRANSCRIBE_FALLBACK_CLOUD", "If the server can't be reached, use my cloud service",
              "toggle",
              "Off: the recording is kept and transcribed when the server is back.\n"
              "On: that one dictation goes to the cloud service instead.\n"
              "• Needs a cloud service filled in: choose \"A cloud service\" on the "
              "Transcription page once, enter its key, then switch back\n"
              "• The audio leaves this PC when this happens",
              default="false", view="adv_common"),
    ]),
    # The LLM that runs spoken edit commands ("prompt AI, make this formal").
    # Help text on these fields is rewritten live by _refresh_ai_help().
    # Custom panels (see _build_words_panel and _build_triggers_panel): both
    # saved to api/processing_config.json.
    # The two switches are placed by _build_words_panel (view="words_..."):
    # the first with the corrections it adds to, the second at the end.
    ("Words", [
        Field("LEARN_CORRECTIONS", "Offer to remember my corrections", "toggle",
              LEARN_HELP % LEARN_SAVED_HERE, default="true", view="words_learn"),
        Field("REMOVE_FILLERS", "Remove filler sounds", "toggle",
              "Deletes standalone um, umm, uh, uhh and hmm.\n"
              "• Instant, no AI\n"
              "• Never \"like\" or \"you know\": usually real words\n"
              "• Every removal is logged; nothing vanishes silently\n"
              "• Not needed with AI Editing > \"Clean up every dictation\" "
              "on, but still helps if the AI can't be reached",
              default="false", view="words_fillers"),
    ]),
    ("Triggers", []),
    ("AI Editing (LLM)", [
        Field("EDIT_BACKEND", "Edit backend", "choice",
              "Which kind of server runs spoken edit commands.\n"
              "• Only the settings for your pick are shown",
              labelled_choices=EDIT_BACKEND_CHOICES, default="ollama"),
        Field("OLLAMA_EDIT_API_URL", "Ollama endpoint", "otext",
              "Ollama on this PC or your LAN.\n"
              "• Use an IP address, not a hostname\n"
              "• The service can't resolve MagicDNS names", group='ollama'),
        Field("OLLAMA_MODEL", "Ollama edit model", "pick", "x", group='ollama'),
        Field("OLLAMA_EDIT_KEEP_ALIVE", "Keep edit model in VRAM", "pick", "x",
              labelled_choices=KEEP_ALIVE_CHOICES, group='ollama'),
        Field("EDIT_LLM_BASE_URL", "Server base URL", "otext",
              "Any OpenAI-compatible server, ending in /v1.\n"
              "• llama.cpp or LM Studio on your LAN\n"
              "• Or a cloud service (OpenRouter, OpenAI, Groq...)", group='openai'),
        Field("EDIT_LLM_MODEL", "Server model", "otext",
              "The model name that server expects.", group='openai'),
        Field("EDIT_LLM_API_KEY", "Server API key", "secret",
              "Needed for cloud services.\n"
              "• And for servers started with --api-key\n"
              "• Leave empty for an open local server", group='openai'),
        Field("EDIT_LLM_NO_THINK", "Skip thinking (reasoning)", "toggle",
              "For reasoning models: answer directly, no thinking first.\n"
              "• Ignored by models that don't think",
              group='openai', emphasis="Much faster per edit."),
        Field("_h_cleanup", "Dictation cleanup", "heading",
              "Optional: the AI tidies every dictation, without being asked.\n"
              "• Fillers, \"no wait, I mean\" corrections, spoken \"new paragraph\"\n"
              "• Your phone can turn it on for itself separately"),
        Field("CLEANUP_ALWAYS", "Clean up every dictation", "toggle",
              "Uses the AI chosen on this page.\n"
              "• Adds about a second per dictation\n"
              "• Removes um/uh too (Words > \"Remove filler sounds\" "
              "is the no-AI version)\n"
              "• Skipped after \"prompt AI\" (that goes to the editor)\n"
              "• Editor unreachable: you get your dictation uncleaned",
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
              "Used with Fix grammar or Polish.\n"
              "• Keep my words keeps your tone (changing it means rewording)\n"
              "• Want it longer? Say \"prompt AI, make this longer\"",
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
    ("Hotkey", [
        Field("HOTKEY", "Dictation hotkey", "hotkey",
              "Pick a preset, or Custom... for your own key combo.\n"
              "• Best: keys that type nothing (F9, Pause, Ctrl+Alt+Space)\n"
              "• The backtick types a ` that must be deleted before pasting\n"
              "• Applies after Restart Listener in the tray",
              # PRESETS are (key, label); the dropdown wants (label, value).
              labelled_choices=[(lbl, key) for key, lbl in hotkey_config.PRESETS],
              default=hotkey_config.DEFAULT_HOTKEY),
        Field("HOTKEY_MODE", "Mode", "choice",
              "Hold to talk records while the key is held down.\n"
              "• Needs a key that types nothing (not the backtick)",
              labelled_choices=[("Tap to start, tap to stop", "toggle"),
                                ("Hold to talk", "hold")],
              default="toggle"),
        Field("FLOATING_MIC_BUTTON", "Floating mic button", "toggle",
              "The app's logo pops up beside the text cursor.\n"
              "• Click to start, click again to stop: one-handed\n"
              "• Drag it anywhere; it remembers the spot\n"
              "• A line runs around it; it hides when the loop closes\n"
              "• Clicked away while recording? It goes back to your box to paste\n"
              "• Never shows in password or sign-in boxes\n"
              "• Right-click: hide it in one app, or turn it off\n"
              "• Applies within a few seconds", default="true"),
        Field("FLOATING_MIC_HIDE_APPS", "Never show the mic button in", "apps",
              "Programs where the floating mic button stays hidden.\n"
              "• Or right-click the button: Hide in this app"),
        Field("EDIT_SELECTION", "Edit highlighted text by voice", "toggle",
              "Highlight text, start dictating, and say what to do.\n"
              "• e.g. \"make this more polite\", \"translate to Spanish\"\n"
              "• The AI's version replaces the highlight\n"
              "• Start with the mic button or a key that types nothing (F9); "
              "the backtick types over the highlight\n"
              "• Uses the editor on the AI Editing page",
              default="true"),
        Field("CLIPBOARD_PRIVATE", "Keep dictations out of clipboard history", "toggle",
              "Text reaches your app through the clipboard.\n"
              "• On: clipboard history (Win+V) and cloud clipboard skip "
              "dictations, like password managers do\n"
              "• May stop pasting into Chrome Remote Desktop\n"
              "• Applies to the next dictation", default="false"),
    ]),
    ("Audio Corpus", [
        Field("LOG_TRANSCRIPTS", "Keep a log of transcripts", "toggle",
              "Saves the text of every dictation to api/transcripts.log.\n"
              "• For tuning skills and prompts on your real speech",
              default="false"),
        Field("KEEP_AUDIO", "Keep source clips", "toggle",
              "Archives the original audio.\n"
              "• Lets tests replay your real dictation instead of made-up "
              "audio", default="false"),
        Field("KEEP_AUDIO_MAX", "Rolling clip limit", "int",
              "Ordinary clips kept before the oldest is pruned. keep/ is never pruned."),
        Field("KEEP_AUDIO_HARD_MAX", "Hard-clip limit", "int",
              "Cap on auto-collected low-confidence clips in hard/."),
        Field("KEEP_AUDIO_PIN_BELOW", "Hard-clip threshold", "float",
              "Clips with confidence at or below this are filed as hard.\n"
              "• Length-adjusted; -0.25 is about the worst 1 clip in 20"),
        Field("CORPUS_TAG_TRIGGERS", "Spoken tags", "list",
              "Say one while dictating to keep that clip for good.\n"
              "• Filed in keep/, named with the phrase\n"
              "• One phrase per line"),
    ]),
    ("Denoise", [
        Field("DENOISE_ENABLED", "Dual-pass denoise", "toggle",
              "Transcribes each clip twice: raw and denoised.\n"
              "• Keeps whichever pass Whisper was more sure of\n"
              "• Roughly doubles transcription time"),
        Field("DENOISE_ATTEN_LIMIT_DB", "Attenuation limit (dB)", "int",
              "100 is full strength. Lower mixes some original signal back in."),
    ]),
    ("Startup", [
        Field("_h_server", "Dictation in the background", "heading",
              "The background program that turns what you say into text.",
              group="src_local_startup"),
        Field("GAME_MODE", GAME_MODE_TITLE, "toggle",
              "A game full screen for 15 s pauses dictation and frees the "
              "graphics card.\n"
              "• Comes back 10 s after you stop playing\n"
              "• Full-screen video (browser or player) doesn't count\n"
              "• Takes effect within seconds, no restart",
              default="false", group="src_local_startup"),
        Field("GAME_PROCESSES", "Also pause for these programs", "apps",
              "Optional, for games that run in a window.\n"
              "• Dictation pauses while any of these is running",
              group="src_local_startup"),
        Field("RESTART_INTERVAL_HOURS", "Scheduled restart (hours)", "int",
              "x", group="src_local_startup"),  # help set live by _refresh_restart_help()
        Field("API_VISIBLE", "LAUNCHER.bat: show server window", "toggle",
              "Only if you start things with scripts/LAUNCHER.bat.\n"
              "• On: the server runs in a visible console window\n"
              "• Off: LAUNCHER starts the background service instead",
              default="false", group="src_local_startup"),
        Field("_h_look", "Appearance", "heading", "How these windows look."),
        Field("UI_ACCENT", "Accent colour", "choice",
              "The colour of switches, links, the main button and the mic "
              "button's countdown. Applies the next time a window opens.",
              labelled_choices=theme.ACCENT_LABELS, default=theme.DEFAULT_ACCENT),
        Field("_h_listener", "Hotkey listener", "heading",
              "The small program behind the tray icon.\n"
              "• Records when you press the hotkey, then pastes the text"),
        Field("LISTENER_RESTART_INTERVAL_MINS", "Refresh after idle (mins)", "int",
              "Quietly restarts the listener after this many idle minutes.\n"
              "• Keeps the hotkey from going dead after long idle periods"),
        Field("LISTEN_DELAY_SECONDS", "LAUNCHER.bat: wait before listener (secs)", "int",
              "Only used by scripts/LAUNCHER.bat.\n"
              "• How long it waits for the model to load before starting "
              "the listener"),
    ]),
    # Custom panel (see _build_phone_panel): ports, addresses, API key,
    # pairing QR, app downloads.
    ("Devices & Apps", []),
]

if sys.platform != "win32":
    import linux_settings
    SCHEMA = linux_settings.adapt(SCHEMA, Field)

SECTION_INTROS = {
    "Transcription": SOURCE_INTROS["local"],
    "Words": "Names and terms Whisper should spell your way, and words or "
             "phrases it should swap for something else.\n"
             "• Used for every dictation, from this PC and your phone",
    # No intro: the page opens on its first heading, which says it all.
    "Triggers": "",
    "AI Editing (LLM)": "Optional: an AI rewrites your dictation when you ask.\n"
                        "• Say \"prompt AI\" plus an instruction, e.g. \"make this "
                        "more formal\"\n"
                        "• Plain dictation never uses it",
    "Audio Corpus": "Optional, and off unless you turn it on.\n"
                    "• Keeps transcripts and recordings on this PC\n"
                    "• Useful for checking how well dictation hears your real voice\n"
                    "• Nothing is uploaded",
    "Denoise": "Optional: cleans background noise before transcribing.\n"
               "• Most microphones don't need it",
    "Startup": "How dictation starts with Windows and keeps running well.\n"
               "• Most people never need to change these",
    "Hotkey": "How you start dictating on this PC.\n"
              "• The hotkey, and the mic button at the text cursor",
    "Devices & Apps": "How your phone and your other PCs use the dictation on this PC.\n"
                      "• One box for your phone, one for another PC",
}
if sys.platform != "win32":
    SECTION_INTROS.update(linux_settings.INTROS)


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
    # API keys may be stored encrypted (dpapi:...): everything here sees them plain.
    return secrets_store.reveal_env(values)


def write_env(updates):
    """Replace values in place, preserving every comment, blank line and ordering.

    Only lines that are already live assignments are rewritten -- a commented
    `# OLLAMA_EDIT_API_URL=...` fallback stays commented, because those are kept
    on purpose as one-line revert switches. Written via a temp file + os.replace
    so an interrupted save cannot leave a half-written .env behind.
    """
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # API keys are written encrypted on Windows (api/secrets_store.py). A key
    # that's already encrypted and unchanged keeps its exact line, so saving
    # doesn't rewrite it; a plaintext one is encrypted now (the migration).
    updates = dict(updates)
    current = {}
    for line in lines:
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$", line)
        if m:
            current[m.group(1)] = m.group(2).strip()
    for key in secrets_store.SECRET_KEYS:
        if key in updates and updates[key]:
            old = current.get(key, "")
            if secrets_store.is_protected(old) and secrets_store.reveal(old, key) == updates[key]:
                updates[key] = old
            else:
                updates[key] = secrets_store.protect(updates[key])
        elif key in updates and secrets_store.unreadable(current.get(key, "")):
            # Encrypted on another PC, so the page showed it empty. Keep it
            # rather than quietly drop it: the server stays locked (not open)
            # until a new key is typed in.
            updates[key] = current[key]

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
        from PIL import ImageFilter
        img = Image.open(LOGO_PNG).convert("RGBA").resize((px, px), Image.LANCZOS)
        # Shrinking 512 px to ~40 softens the thin waveform bars; a light
        # unsharp mask brings their edges back.
        img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=80, threshold=2))
        return ImageTk.PhotoImage(img, master=root)
    except Exception:
        return None


def set_window_icon(root):
    """Title bar + taskbar icon: the app's waveform logo (shared with Android)."""
    try:
        root.iconbitmap(default=ICON_ICO)
    except Exception:
        pass


def show_when_ready(root):
    """Show a window built while withdrawn (see main()), fully drawn, so it
    doesn't flash as a small white box and then grow into the real window."""
    try:
        round_widgets(root)
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.focus_force()
    except tk.TclError:
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
    style.map("Vertical.TScrollbar", background=[("active", "#45474D")])
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
    """True if install_service.ps1's restart task exists (new or old name)."""
    for task in ("Restart Whisper Unchained", "Restart FasterWhisperAPI"):
        try:
            r = subprocess.run(["schtasks", "/query", "/tn", task],
                               capture_output=True, timeout=3,
                               creationflags=0x08000000)  # no console window
            if r.returncode == 0:
                return True
        except Exception:
            pass
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
        color = "#8A8D93" if self._hover else "#55585E"
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


def close_open_dropdowns(widget):
    """Close any open ttk drop-down list under widget's root. Returns True if
    one was open. Scrolling the page under an open list would leave the list
    floating away from its box."""
    tkapp = widget.tk
    closed = False
    try:
        pops = tkapp.splitlist(tkapp.eval("info commands *.popdown"))
    except tk.TclError:
        return False
    for pd in pops:
        try:
            if int(tkapp.call("winfo", "ismapped", pd)):
                tkapp.call("ttk::combobox::Unpost", pd.rsplit(".popdown", 1)[0])
                closed = True
        except tk.TclError:
            pass
    return closed


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
        edge = _hex_rgb(self._blend("#9C9FA5", ACCENT, pos))
        d.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=h * k // 2, fill=fill,
                            outline=edge, width=k)
        knob = (14 if hover else 12) * k
        cx = x0 + h * k / 2 + pos * (tw - h) * k
        cy = y0 + h * k / 2
        color = _hex_rgb(self._blend("#C9CBCF", ON_ACCENT, pos))
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
            "primary": (ACCENT, ON_ACCENT, ACCENT_H),
            "ghost":   (RAISED, TEXT, HOVER),
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
        self.config(fg=self.fg if on else FAINT,
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


class ProgressBar(tk.Frame):
    """A thin bar that fills from the left: one number for a whole download."""

    def __init__(self, parent, width=200):
        super().__init__(parent, bg=RAISED, width=width, height=8)
        self._no_round = True
        self.pack_propagate(False)
        self._fill = tk.Frame(self, bg=ACCENT)
        self._fill._no_round = True
        self.percent = 0

    def set(self, percent):
        self.percent = max(0, min(100, int(percent)))
        self._fill.place(x=0, y=0, relheight=1, relwidth=self.percent / 100.0)


class Tip:
    """A small hover note, for icon-only controls (words, never colour alone)."""

    def __init__(self, widget, text, above=False):
        """above: show the note over the widget and ending at its right edge
        (for the buttons at the bottom right of a window, where a note under
        them would fall off the screen)."""
        self.widget, self.text, self.win, self.above = widget, text, None, above
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Button-1>", self.hide, add="+")

    def show(self, _e=None):
        self.hide()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        label = tk.Label(self.win, text=self.text() if callable(self.text) else self.text,
                         font=F_HELP, bg=SURFACE, fg=TEXT, padx=8, pady=4, justify="left",
                         highlightthickness=1, highlightbackground=BORDER)
        label._no_round = True      # a plain note: no corner pieces (round_widgets)
        label.pack()
        if self.above:
            label.update_idletasks()
            x = (self.widget.winfo_rootx() + self.widget.winfo_width()
                 - label.winfo_reqwidth())
            y = self.widget.winfo_rooty() - label.winfo_reqheight() - 6
        self.win.wm_geometry("+%d+%d" % (x, y))

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
        self.label = tk.Label(entry, text=text, font=(_FONT, -15, "italic"),
                              bg=entry.cget("bg"), fg=FAINT, cursor="xterm")
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


class HelpText(tk.Frame):
    """Help text that reads at a glance: a lead line, then "• " bullet lines
    with a hanging indent (a wrapped bullet continues under its text, not
    under the dot). Acts like a Label for config(text=, fg=) and cget("text")."""

    BULLET = "• "

    def __init__(self, parent, text="", font=None, bg=None, fg=None, wraplength=360):
        bg = bg or BG
        super().__init__(parent, bg=bg)
        self._opts = {"text": text, "font": font or F_HELP, "bg": bg,
                      "fg": fg or MUTED, "wraplength": wraplength}
        self._render()

    def _render(self):
        for w in self.winfo_children():
            w.destroy()
        o = self._opts
        for line in o["text"].split("\n"):
            if line.startswith(self.BULLET):
                row = tk.Frame(self, bg=o["bg"])
                row.pack(anchor="w", fill="x", pady=(1, 0))
                tk.Label(row, text="•", font=o["font"], bg=o["bg"], fg=o["fg"]).pack(
                    side="left", anchor="n", padx=(2, 6))
                tk.Label(row, text=line[len(self.BULLET):], font=o["font"], bg=o["bg"],
                         fg=o["fg"], anchor="w", justify="left",
                         wraplength=max(60, o["wraplength"] - 18)).pack(side="left", anchor="w")
            else:
                tk.Label(self, text=line, font=o["font"], bg=o["bg"], fg=o["fg"],
                         anchor="w", justify="left",
                         wraplength=o["wraplength"]).pack(anchor="w")

    def configure(self, cnf=None, **kw):
        kw = dict(cnf or {}, **kw)
        for alias, name in (("foreground", "fg"), ("background", "bg")):
            if alias in kw:
                kw[name] = kw.pop(alias)
        mine = {k: kw.pop(k) for k in list(kw) if k in self._opts}
        if "bg" in mine:
            kw["bg"] = mine["bg"]
        if kw:
            super().configure(**kw)
        if mine:
            self._opts.update(mine)
            self._render()

    config = configure

    def cget(self, key):
        key = {"foreground": "fg", "background": "bg"}.get(key, key)
        return self._opts[key] if key in self._opts else super().cget(key)


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


def example_box(parent, pieces, height=None, padx=28, width=72):
    """A worked example on a panel. `pieces` are (text, part): "work" and
    "ask" are coloured, "phrase" is bold. Without a height the box grows to
    fit its text. Also used by the "New skill" editor (trigger_dialogs)."""
    ex = tk.Text(parent, font=F_HELP, bg=SURFACE, fg=TEXT, relief="flat", wrap="word",
                 padx=14, pady=10, height=height or 5, width=width, cursor="arrow",
                 highlightthickness=0, borderwidth=0)
    ex.tag_configure("work", foreground=EXAMPLE_WORK)
    ex.tag_configure("ask", foreground=EXAMPLE_ASK)
    ex.tag_configure("phrase", font=(_FONT, -14, "bold"))
    for text, part in pieces:
        ex.insert("end", text, (part,) if part else ())
    ex.config(state="disabled")
    ex.pack(anchor="w", padx=padx, pady=(8, 0))
    if height is None:
        def fit(_e=None):
            try:
                lines = ex.count("1.0", "end", "displaylines")
            except tk.TclError:
                return
            if isinstance(lines, (tuple, list)):
                lines = lines[0] if lines else None
            if lines and int(lines) != int(ex.cget("height")):
                ex.config(height=int(lines))
        ex.bind("<Configure>", fit)
    return ex


def labelled_example(parent, parts, result, padx=28):
    """"You say" on one line in coloured parts, each part's name centred under
    it in the same colour, then the result."""
    box = tk.Frame(parent, bg=SURFACE)
    box.pack(anchor="w", padx=padx, pady=(8, 0))
    line = tk.Frame(box, bg=SURFACE)
    line.pack(anchor="w", padx=14, pady=(10, 0))
    colours = {"work": EXAMPLE_WORK, "ask": EXAMPLE_ASK, "phrase": TEXT}
    tk.Label(line, text="You say:  \"", font=F_HELP, bg=SURFACE, fg=TEXT, padx=0, bd=0).grid(
        row=0, column=0, sticky="e")
    # One sentence: the same font throughout, the spaces between the parts
    # are real spaces, and no padding that would open extra gaps.
    for n, (said, name, part) in enumerate(parts, start=1):
        fg = colours.get(part, TEXT)
        gap = " " if n < len(parts) else ""
        tk.Label(line, text=said + gap, font=F_HELP, bg=SURFACE, fg=fg, padx=0, bd=0,
                 highlightthickness=0).grid(row=0, column=n)
        tk.Label(line, text=name, font=F_HELP, bg=SURFACE, fg=fg, padx=0, bd=0).grid(
            row=1, column=n)
    tk.Label(line, text="\"", font=F_HELP, bg=SURFACE, fg=TEXT, padx=0, bd=0).grid(
        row=0, column=len(parts) + 1, sticky="w")
    tk.Label(box, text="Result:  \"%s\"" % result, font=F_HELP, bg=SURFACE, fg=TEXT,
             anchor="w").pack(anchor="w", padx=14, pady=(10, 10))
    return box


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
        self.root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)
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
        # One header block: the logo, the app name beside it and "Settings"
        # under the name (the title bar names the window too, so the page
        # doesn't repeat it as a second big title).
        brand = tk.Frame(head, bg=BG)
        brand.pack(anchor="w", pady=(6, 4))
        self._logo = load_logo(self.root, 40)   # keep a reference or Tk drops it
        if self._logo:
            tk.Label(brand, image=self._logo, bg=BG).pack(side="left", padx=(0, 12))
        names = tk.Frame(brand, bg=BG)
        names.pack(side="left")
        tk.Label(names, text="Whisper Unchained", font=F_TITLE, bg=BG,
                 fg=TEXT).pack(anchor="w")
        tk.Label(names, text="Settings", font=F_SECTION, bg=BG,
                 fg=MUTED).pack(anchor="w")

    def _build_sidebar(self, parent):
        self.sidebar = tk.Frame(parent, bg=SURFACE, width=180)
        self.sidebar.pack(side="left", fill="y", padx=(28, 0))
        self.sidebar.pack_propagate(False)
        self.nav = {}
        # Taller than the card's corner pieces, which would otherwise sit on
        # top of the first item's backplate and clip it.
        tk.Frame(self.sidebar, bg=SURFACE, height=R_CARD + 4).pack(fill="x")
        for name, _ in SCHEMA:
            # Windows 11 navigation: the selected page gets a rounded backplate
            # and a short accent pill on its left edge (plus brighter text, so
            # it never relies on colour alone).
            row = tk.Frame(self.sidebar, bg=SURFACE, cursor="hand2", takefocus=1,
                           highlightthickness=0)
            row.pack(fill="x", padx=6, pady=1)
            bar = tk.Frame(row, bg=SURFACE, width=3, height=16)
            bar.pack(side="left", padx=(0, 0))
            lbl = tk.Label(row, text=SECTION_LABELS.get(name, name), font=F_TAB,
                           bg=SURFACE, fg=MUTED,
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
        color = "#232427" if on else SURFACE
        row.config(bg=color)
        lbl.config(bg=color)
        if getattr(row, "_corners", None) is not None:
            row._corners.refresh()

    def _round_now(self):
        self._round_pending = False
        try:
            round_widgets(self.root)
        except tk.TclError:
            pass    # the window closed first

    def _round_soon(self):
        """Round the corners of widgets added since the last pass (a rebuilt
        box that kept its size doesn't trigger the page's own pass)."""
        if not getattr(self, "_round_pending", False):
            self._round_pending = True
            try:
                self.root.after(30, self._round_now)
            except tk.TclError:
                self._round_pending = False

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
            if close_open_dropdowns(self.root):
                return      # this notch just closes the list, as Windows does
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", on_wheel)
        # A closed drop-down changes its value on the mouse wheel (Tk's default).
        # Scrolling the page over a column of them silently changed settings, so
        # the wheel scrolls the page there too; pick a value by clicking.
        root = self.root
        root.bind_class("TCombobox", "<MouseWheel>",
                        lambda e: (on_wheel(e), "break")[1])
        self.canvas = canvas

        self.frames = {}
        self.groups = {}
        models = installed_ollama_models(self.values.get("OLLAMA_API_URL", ""))
        self._page_folds = {}      # page name -> its "Advanced settings" fold
        self._slots = {}           # view name -> a frame a panel places itself
        self._shared_vars = {}     # setting -> its value, when two switches share it
        for name, fields in SCHEMA:
            page = frame = tk.Frame(self.inner, bg=BG)
            if name == "Transcription":
                # A main view, plus the ones behind "Advanced" (_build_tx_views).
                frame = tk.Frame(page, bg=BG)
                frame.pack(fill="x")
                self._build_tx_views(page, frame)
            self._building = frame
            holder = None
            # ONE "Advanced settings" fold per page, at the very end of it.
            # Settings of a group (shown and hidden as a block) stay a block
            # inside the fold: under the group's own name when the whole group
            # is advanced, else under "<group>_adv" beside the group itself.
            fold = None
            advanced = [f for f in fields if f.key in ADVANCED_KEYS and f.kind != "heading"]
            plain_groups = {f.group for f in fields
                            if f.group and f.key not in ADVANCED_KEYS}
            fold_text = ADVANCED_FOLD
            if advanced and not any(f.group for f in advanced):
                fold_text += " (%d)" % len(advanced)    # all of them always show
            intro = SECTION_INTROS.get(name)
            if intro:
                lbl = HelpText(frame, intro, font=F_BODY,
                               wraplength=660 if name == "Transcription" else 560)
                lbl.pack(anchor="w", padx=28, pady=(4, 2))
                self.intro_labels = getattr(self, "intro_labels", {})
                self.intro_labels[name] = lbl
            for f in fields:
                target = frame
                is_advanced = f in advanced
                if f.group and not is_advanced:
                    if holder is None:
                        holder = tk.Frame(frame, bg=BG)
                        holder.pack(fill="x")
                    if f.group not in self.groups:
                        self.groups[f.group] = tk.Frame(holder, bg=BG)
                    target = self.groups[f.group]
                if f.view:
                    target = self._view_target(f.view)
                if f.kind == "engine":
                    self._build_engine_card(target, f)      # a card, not a row
                    continue
                if f.kind == "model":
                    # No row: see the schema. The value is kept and saved.
                    f.var = tk.StringVar(value=self.values.get(f.key, "") or f.default)
                    f.var.trace_add("write", lambda *_: self.mark_dirty())
                    self.fields[f.key] = f
                    continue
                if is_advanced:
                    if fold is None:
                        adv_holder = tk.Frame(frame, bg=BG)   # packed last on the page
                        fold = Expander(adv_holder, tk.Frame(adv_holder, bg=BG),
                                        fold_text, ADVANCED_FOLD_HIDE)
                        fold.pack(anchor="w", padx=28)
                        fold.holder = adv_holder
                    target = fold.body
                    if f.group:
                        block = f.group + "_adv" if f.group in plain_groups else f.group
                        if block not in self.groups:
                            self.groups[block] = tk.Frame(fold.body, bg=BG)
                        target = self.groups[block]
                    f.fold = fold
                if f.kind == "heading":
                    # A sub-heading, not a setting: never saved.
                    self._phone_heading(target, f.label, f.help)
                    continue
                if f.kind == "button":
                    self._build_action_row(target, f)
                    continue
                if f.compact:
                    self._build_field(target, f, models, wrap=520, sep=False)
                else:
                    self._build_field(target, f, models)
                self.fields[f.key] = f
                if f.key == "CLOUD_PROVIDER":
                    self._build_cloud_info(target)
            if fold is not None:
                self._page_folds[name] = fold
            if name == "Transcription":
                # Only while another PC does the work: see LEARN_HELP.
                self._learn_twin = tk.Frame(frame, bg=BG)
                self._build_switch_twin(self._learn_twin, "LEARN_CORRECTIONS",
                                        LEARN_HELP % LEARN_SAVED_REMOTE)
                # The page's one way into everything technical.
                row = self._tx_advanced_row = tk.Frame(frame, bg=BG)
                row.pack(fill="x", padx=28, pady=(14, 0))
                self._tx_advanced = Button(row, TX_ADVANCED, self._open_tx_advanced)
                self._tx_advanced.pack(side="left")
            if name == "Devices & Apps":
                self._build_phone_panel(frame)
            if name == "Words":
                self._build_words_panel(frame)
            if name == "Triggers":
                self._build_triggers_panel(frame)
            if name == "Transcription":
                self._build_model_picker(self._fw_models)
                self.fields["WHISPER_DEVICE"].var.trace_add(
                    "write", lambda *_: self._refresh_engine())
                self.fields["REMOTE_SERVER_URL"].var.trace_add(
                    "write", lambda *_: self._refresh_remote_state())
                self._refresh_remote_state()
                self._refresh_engine()
                self._build_alt_urls_field()
            if name == "AI Editing (LLM)":
                self._build_llm_finder(frame)
                self._build_app_styles(frame)
            if name == "Startup":
                self._build_integration_panel(frame)
            if name == "Audio Corpus":
                self._build_history_panel(frame)
                self._build_source_extras()
            if fold is not None:
                # After the page's own panels: Advanced is the last thing on it.
                fold.holder.pack(fill="x", pady=(18, 0))
            self.frames[name] = page
        self._setup_ai_help()
        self._refresh_restart_help()
        self.fields["TRANSCRIBE_SOURCE"].var.trace_add("write", lambda *_: self._refresh_source())
        self.fields["CLOUD_PROVIDER"].var.trace_add("write", lambda *_: self._apply_cloud_preset())
        self._refresh_source()

    # --- Transcription source ------------------------------------------------------

    def _build_source_extras(self):
        """Under the remote and cloud settings: a 'Test connection' button.
        (The address and the other technical settings are behind the page's
        one Advanced button.)"""
        self._conn_labels = {}
        for group in ("src_remote", "src_cloud"):
            row = tk.Frame(self.groups[group], bg=BG)
            row.pack(fill="x", padx=28, pady=(10, 0))
            Button(row, "Test connection", self._test_connection).pack(side="left")
            lbl = tk.Label(row, text="", font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                           justify="left", wraplength=480)
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

    # --- Connection code (pasted from the server PC's Devices & Apps page) ------

    # Where the user goes next; the setup wizard has no Save button.
    PASTE_NEXT = "Press Save"

    def _build_action_row(self, parent, field):
        """A row with a button instead of a setting (kind "button"). Nothing
        here is saved; the button fills in the fields under it."""
        commands = {"_paste_code": ("Paste connection code", self.paste_connection_code)}
        text, command = commands[field.key]
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=28, pady=(16, 0))
        left = tk.Frame(row, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=field.label, font=F_LABEL, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w")
        HelpText(left, field.help, wraplength=360).pack(anchor="w", pady=(3, 0))
        if field.key == "_paste_code":
            # Whether the other PC is filled in, without showing its address.
            self._remote_ready = tk.Label(left, text="", font=F_HELP, bg=BG, fg=MUTED,
                                          anchor="w")
            self._remote_ready.pack(anchor="w", pady=(4, 0))
        button = Button(row, text, command, kind="primary")
        button.pack(side="right", padx=(20, 0))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))
        self._action_buttons = getattr(self, "_action_buttons", {})
        self._action_buttons[field.key] = button

    def _build_alt_urls_field(self):
        """The server PC's other addresses (from a connection code). Not shown:
        kept as it was read, replaced only by pasting a code, written on Save."""
        from pairing import ALT_URLS_KEY
        f = Field(ALT_URLS_KEY, "Other server addresses", "otext")
        f.var = tk.StringVar(self.root, value=self.values.get(ALT_URLS_KEY, ""))
        f.var.trace_add("write", lambda *_: self.mark_dirty())
        self.fields[f.key] = f

    def _clipboard_text(self):
        try:
            return self.root.clipboard_get()
        except tk.TclError:      # empty, or something that isn't text
            return ""

    def _code_reachable(self, parsed, url):
        """Does the server PC answer at this one address, with the code's key
        and certificate? Runs off the Tk thread."""
        ok, _msg = transcribe_client.check_connection(
            {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": url,
             "REMOTE_API_KEY": parsed["api_key"],
             "REMOTE_CERT_SHA256": parsed["cert_sha256"]}, timeout=4)
        return ok

    def paste_connection_code(self):
        """Fill the "Another PC" settings from the code on the clipboard. The
        address that answers right now goes first. Nothing is saved here."""
        import pairing
        import queue
        if getattr(self, "_paste_busy", False):
            return
        text = self._clipboard_text()
        if not text.strip():
            self.status.config(text="Nothing to paste. Copy the connection code on the "
                                    "other PC first.", fg=DANGER)
            return
        try:
            parsed = pairing.parse_connection_code(text)
        except ValueError as e:
            self.status.config(text=str(e), fg=DANGER)
            return
        self._paste_busy = True
        button = getattr(self, "_action_buttons", {}).get("_paste_code")
        if button is not None:
            button.set_enabled(False)
        self.status.config(text="Checking the connection...", fg=MUTED)
        done = queue.Queue()

        def work():
            answered = []

            def reachable(url):
                ok = self._code_reachable(parsed, url)
                if ok:
                    answered.append(url)
                return ok
            try:
                settings = pairing.remote_settings(parsed, reachable=reachable)
            except Exception:
                settings = pairing.remote_settings(parsed)
            done.put((settings, bool(answered)))

        def poll():
            # Tk isn't thread-safe: the worker only fills the queue, and the
            # result is applied here, on the Tk thread.
            try:
                settings, answered = done.get_nowait()
            except queue.Empty:
                try:
                    self.root.after(100, poll)
                except tk.TclError:
                    pass                    # the window was closed meanwhile
                return
            self._apply_connection_code(parsed, settings, answered)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, poll)

    def _apply_connection_code(self, parsed, settings, answered):
        self._paste_busy = False
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        button = getattr(self, "_action_buttons", {}).get("_paste_code")
        if button is not None:
            button.set_enabled(True)
        from pairing import ALT_URLS_KEY
        for key in ("REMOTE_SERVER_URL", "REMOTE_API_KEY", "REMOTE_CERT_SHA256", ALT_URLS_KEY):
            self.fields[key].var.set(settings[key])
        if self._value("TRANSCRIBE_SOURCE") != "remote":
            self.fields["TRANSCRIBE_SOURCE"].var.set(
                next(label for label, value in SOURCE_CHOICES if value == "remote"))
        name = parsed["name"] or "the other PC"
        if answered:
            self.status.config(text="Connected to %s. %s." % (name, self.PASTE_NEXT), fg=ACCENT)
        else:
            self.status.config(
                text="Filled in from the code, but %s didn't answer just now. %s and try "
                     "again when it is on." % (name, self.PASTE_NEXT), fg=WARN)

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
        self._refresh_cloud_info()

    def _build_cloud_info(self, parent):
        """Under the service picker: why you would choose that service, what
        it costs, what happens to your audio, and a link to its prices."""
        box = tk.Frame(parent, bg=SURFACE)
        box.pack(fill="x", padx=28, pady=(8, 0))
        top = tk.Frame(box, bg=SURFACE)
        top.pack(fill="x", padx=14, pady=(10, 0))
        self._cloud_lines = {}
        for key in ("why", "price", "privacy"):
            first = key == "why"
            lbl = tk.Label(top if first else box, text="", font=F_HELP, bg=SURFACE,
                           fg=TEXT if first else MUTED, anchor="w", justify="left",
                           wraplength=540 if first else 650)
            if first:
                lbl.pack(side="left")
            else:
                lbl.pack(anchor="w", padx=14, pady=(4, 0))
            self._cloud_lines[key] = lbl
        self._cloud_end = tk.Frame(box, bg=SURFACE, height=10)
        self._cloud_end.pack(fill="x")
        self._cloud_link = self._link(top, CLOUD_PRICE_LINK, self._open_cloud_prices,
                                      font=F_HELP)
        self._cloud_custom = tk.Label(box, text=CLOUD_CUSTOM_NOTE, font=F_HELP, bg=SURFACE,
                                      fg=TEXT, anchor="w")
        self._refresh_cloud_info()

    def _cloud_key(self):
        return self._value("CLOUD_PROVIDER") or "groq"

    def _refresh_cloud_info(self):
        if not hasattr(self, "_cloud_lines"):
            return
        key = self._cloud_key()
        info = transcribe_client.cloud_info(key)
        self._cloud_lines["why"].config(text=info["why"])
        self._cloud_lines["price"].config(text="Price: " + info["price"])
        self._cloud_lines["privacy"].config(text="Privacy: " + info["privacy"])
        self._cloud_link.pack_forget()
        if info["url"]:
            self._cloud_link.pack(side="right", anchor="n")
        self._cloud_custom.pack_forget()
        if key == "custom":
            self._cloud_custom.pack(anchor="w", padx=14, pady=(4, 0), before=self._cloud_end)

    def _open_cloud_prices(self):
        url = transcribe_client.cloud_info(self._cloud_key())["url"]
        if url:
            webbrowser.open(url)

    def _refresh_remote_state(self):
        if hasattr(self, "_remote_ready"):
            ready = bool(self._value("REMOTE_SERVER_URL"))
            self._remote_ready.config(text=REMOTE_READY if ready else REMOTE_NOT_READY,
                                      fg=ACCENT if ready else MUTED)

    def _refresh_source(self):
        src = self._value("TRANSCRIBE_SOURCE") or "local"
        self._show_tx_main(scroll=False)    # an Advanced view belongs to one choice
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
            hidden.update(REMOTE_HIDDEN_SECTIONS)  # the words live on that PC
        self._hidden_sections = hidden
        for name, _f in SCHEMA:
            if name in self.nav:
                self.nav[name][0].pack_forget()
        for name, _f in SCHEMA:
            if name in self.nav and name not in hidden:
                self.nav[name][0].pack(fill="x")
        if getattr(self, "current_section", None) in hidden:
            self.show_section("Transcription")
        self._sync_triggers_source(src)
        if src == "cloud" and not self._value("CLOUD_BASE_URL"):
            self._apply_cloud_preset()
        # The page intro and the footer button follow the choice. (The filler
        # switch is on the Words page, which is hidden when another PC does
        # the work: that PC's own setting applies.)
        if "Transcription" in getattr(self, "intro_labels", {}):
            self.intro_labels["Transcription"].config(text=SOURCE_INTROS[src])
        twin = getattr(self, "_learn_twin", None)
        if twin is not None:
            if src != "remote":
                twin.pack_forget()
            elif not twin.winfo_manager():
                twin.pack(fill="x", before=self._tx_advanced_row)
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
            text = ("Restarts the server every this many hours to clear GPU memory.\n"
                    "• A change applies when scripts/install_service.ps1 runs again")
        else:
            text = ("NOT ACTIVE: no scheduled restart is set up on this PC.\n"
                    "• The server just keeps running, which is fine\n"
                    "• To set one up, run scripts/install_service.ps1 as Administrator")
        self._set_help("RESTART_INTERVAL_HOURS", text)

    # --- AI Editing: help that follows the current choices ---------------------

    def _setup_ai_help(self):
        self._ollama_cache = {}
        keys = ("EDIT_BACKEND", "OLLAMA_EDIT_API_URL", "OLLAMA_MODEL",
                "OLLAMA_EDIT_KEEP_ALIVE", "EDIT_LLM_BASE_URL")
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
                            "Everything saved above stays on this PC.\n"
                            "• Delete it any time\n"
                            "• The switches above decide what's saved from now on")
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
         "Adds to File Explorer's right-click menu:\n"
         "• \"Transcribe to clipboard\": straight transcription, nothing edited\n"
         "• \"Transcribe with Whisper Unchained...\": window with subtitles\n"
         "• Windows 11: under \"Show more options\"",
         "install_context_menu.ps1"),
        ("login", "Start when I log in",
         "Starts the tray icon and hotkey when you sign in to Windows.",
         "install_listener_startup.ps1"),
    ]

    def _integration_on(self, key):
        if sys.platform != "win32":
            return linux_settings.integration_on(key)
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
        folder = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu"
                              r"\Programs\Startup")
        return any(os.path.exists(os.path.join(folder, n))
                   for n in ("Whisper Unchained.lnk", "FasterWhisperListener.lnk"))

    def _build_integration_panel(self, frame):
        linux = sys.platform != "win32"
        self._phone_heading(frame, "Desktop integration" if linux else "Windows integration")
        self._integration_rows = {}
        for key, title, text, _script in (linux_settings.INTEGRATIONS if linux
                                          else self.INTEGRATIONS):
            row = tk.Frame(frame, bg=BG)
            row.pack(fill="x", padx=28, pady=(10, 0))
            btn = Button(row, "", lambda k=key: self._toggle_integration(k))
            btn.pack(side="right")
            tk.Label(row, text=title, font=F_LABEL, bg=BG, fg=TEXT, anchor="w").pack(anchor="w")
            status = HelpText(row, text, wraplength=420)
            status.pack(anchor="w")
            self._integration_rows[key] = (btn, status, text)
            self._refresh_integration(key)
        # Only in a copy the .exe installed (a developer copy has no file list).
        if os.path.exists(os.path.join(PROJECT_DIR, "installer", "installed_files.txt")):
            row = tk.Frame(frame, bg=BG)
            row.pack(fill="x", padx=28, pady=(10, 0))
            Button(row, "Uninstall...", self._start_uninstall).pack(side="right")
            tk.Label(row, text="Uninstall Whisper Unchained", font=F_LABEL, bg=BG, fg=TEXT,
                     anchor="w").pack(anchor="w")
            HelpText(row, "Same as Windows Settings > Apps > Uninstall. Asks first, and "
                     "keeps your settings, words and transcripts unless you say otherwise.",
                     wraplength=420).pack(anchor="w")

    def _start_uninstall(self):
        """installer/uninstall_app.py on this app's own Python. It asks, then
        closes this window along with the tray."""
        pythonw = os.path.join(PROJECT_DIR, ".venv", "Scripts", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        subprocess.Popen([pythonw, os.path.join(PROJECT_DIR, "installer", "uninstall_app.py")],
                         cwd=tempfile.gettempdir(), creationflags=0x08000000)

    def _refresh_integration(self, key, note=""):
        btn, status, text = self._integration_rows[key]
        on = self._integration_on(key)
        btn.config(text="Remove" if on else "Add")
        status.config(text=(note or text) + ("  (on)" if on and not note else ""))

    def _toggle_integration(self, key):
        if sys.platform != "win32":
            ok = linux_settings.toggle_integration(key, self._integration_on(key))
            self._refresh_integration(key, "" if ok else "That didn't work.")
            return
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
        """A box at the top of AI Editing: looks for AI programs (LLM servers)
        already running on this PC, or on another PC the user names, and fills
        in the fields under "Advanced settings"."""
        box = tk.Frame(frame, bg=SURFACE)
        intro = getattr(self, "intro_labels", {}).get("AI Editing (LLM)")
        if intro is not None:
            box.pack(fill="x", padx=28, pady=(10, 6), after=intro)
        else:
            box.pack(fill="x", padx=28, pady=(10, 6), before=frame.winfo_children()[0])
        tk.Label(box, text=AI_FINDER_TITLE, font=F_BUTTON, bg=SURFACE, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=14, pady=(10, 0))
        HelpText(box, AI_FINDER_HELP,
                 bg=SURFACE, wraplength=540).pack(anchor="w", padx=14, pady=(2, 0))
        row = tk.Frame(box, bg=SURFACE)
        row.pack(fill="x", padx=14, pady=(8, 0))
        self.llm_other = tk.StringVar(box)
        entry = tk.Entry(row, textvariable=self.llm_other, font=F_BODY, bg=RAISED, fg=TEXT,
                         insertbackground=ACCENT, relief="flat", width=28)
        entry.pack(side="left", ipady=6)
        entry.bind("<Return>", lambda _e: self.find_llm_servers())
        Tip(entry, AI_FINDER_ENTRY_TIP)
        Button(row, AI_FINDER_BUTTON, self.find_llm_servers).pack(side="left", padx=(10, 0))
        self.llm_results = tk.Frame(box, bg=SURFACE)
        self.llm_results.pack(fill="x", padx=14, pady=(6, 10))
        # Which models to look for is a technical choice: first thing under
        # the page's "Advanced settings", above the model fields themselves.
        tips = "Suggested models:\n" + "\n".join(
            "• %s: %s" % (m, note) for m, note in llm_detect.SUGGESTED_MODELS)
        fold = self._page_folds.get("AI Editing (LLM)")
        if fold is not None and fold.body.winfo_children():
            HelpText(fold.body, tips, wraplength=560).pack(
                anchor="w", padx=28, pady=(12, 0), before=fold.body.winfo_children()[0])
        else:
            HelpText(box, tips, bg=SURFACE, wraplength=540).pack(
                anchor="w", padx=14, pady=(0, 10))
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
            self._llm_message("No AI program was found on %s. Ollama (ollama.com) and "
                              "LM Studio (lmstudio.ai) are free and easy to start with."
                              % where)
        else:
            self._llm_message("")
            for f in found:
                line = tk.Frame(self.llm_results, bg=SURFACE)
                line.pack(fill="x", pady=1)
                model = llm_detect.best_model(f.models)
                text = self._found_line(f, found) + (
                    "  ·  would use %s" % model if model else "")
                tk.Label(line, text=text, font=F_HELP, bg=SURFACE, fg=TEXT,
                         anchor="w").pack(side="left")
                use = tk.Label(line, text="  Use this", font=F_LABEL, bg=SURFACE,
                               fg=ACCENT, cursor="hand2")
                use.pack(side="left")
                use.bind("<Button-1>", lambda _e, f=f: self.use_llm_server(f))
        self.on_llm_found(found)

    @staticmethod
    def _found_line(f, found):
        """One AI program that was found, in plain words: its name and where
        it is. Its number is added only to tell two of the same apart."""
        where = "this PC" if f.host in ("127.0.0.1", "localhost") else f.host
        text = "%s on %s" % (f.name, where)
        if sum(1 for o in found if (o.name, o.host) == (f.name, f.host)) > 1:
            text += " (%d)" % f.port
        if f.needs_key:
            text += ": needs a key"
        elif not f.models:
            text += ": nothing loaded in it yet"
        return text

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
            note += " It needs a key: enter it under Advanced settings, below."
        elif not model:
            note += (" Nothing is loaded in it yet: load an AI in it, then choose it "
                     "under Advanced settings, below.")
        self._llm_message(note, ACCENT)

    def _show_group(self, name):
        """Show one backend's fields and hide the others. Hidden fields keep
        their values and are still saved, so switching back loses nothing."""
        for g, frame in self.groups.items():
            if g.startswith("src_"):
                continue  # transcription-source groups: see _refresh_source
            if g in (name, name + "_adv"):
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
            return "\n• Couldn't reach Ollama at that endpoint to check the model"
        if model and model not in sizes:
            return None  # caller words the "not installed" warning
        return ""

    def _refresh_ai_help(self):
        if "EDIT_BACKEND" not in self.fields or not hasattr(self, "_ollama_cache"):
            return
        backend = self._value("EDIT_BACKEND")
        self._show_group("openai" if backend == "openai" else "ollama")

        # Ollama edit fields.
        url = self._value("OLLAMA_EDIT_API_URL")
        model = self._value("OLLAMA_MODEL")
        note = self._model_note(url, model)
        base = "Model used to clean up and edit dictation."
        if note is None:
            base = ("⚠ '%s' isn't installed on that Ollama, so edits will fail.\n"
                    "• Pick an installed one, or run: ollama pull %s" % (model, model))
        else:
            base += note
        self._set_help("OLLAMA_MODEL", base)

        sizes = self._ollama_sizes(url) or {}
        if model in sizes:
            gb = sizes[model] / 1e9
            cost = ("• Staying loaded costs about %.1f GB of VRAM for %s "
                    "(%.1f GB of weights plus 1-3 GB working memory)\n"
                    "• But cuts a cold edit from ~5 s to under 1 s" % (gb + 2, model, gb))
        else:
            cost = ("• Staying loaded costs about the model's file size plus 1-3 GB "
                    "of VRAM\n"
                    "• But cuts a cold edit from ~5 s to under 1 s")
        self._set_help("OLLAMA_EDIT_KEEP_ALIVE",
                       "How long Ollama holds the edit model after a request.\n" + cost)

        url_help = ("Any OpenAI-compatible server, ending in /v1.\n"
                    "• llama.cpp or LM Studio on your LAN\n"
                    "• Or a cloud service (OpenRouter, OpenAI, Groq...)")
        if backend == "openai" and not self._value("EDIT_LLM_BASE_URL"):
            url_help = "⚠ Needed: with no URL, edits fall back to Ollama.\n" + url_help
        self._set_help("EDIT_LLM_BASE_URL", url_help)

    def _build_switch_twin(self, parent, key, help_text):
        """A second switch for a setting that is built on another page (two
        switches can share one setting). Until that page is built, the
        setting's value is read from the file here."""
        field = next(f for _n, fields in SCHEMA for f in fields if f.key == key)
        raw = self.values.get(key, "") or field.default
        # Kept on this window (the schema's Field objects outlive it), for
        # _build_field to pick up when it builds the setting's own row.
        var = self._shared_vars[key] = tk.BooleanVar(
            self.root, value=raw.strip().lower() in ("1", "true", "yes"))
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=28, pady=(12, 0))
        left = tk.Frame(row, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=field.label, font=F_LABEL, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w")
        HelpText(left, help_text, wraplength=600).pack(anchor="w", pady=(3, 0))
        Toggle(row, var, command=self.mark_dirty).pack(side="right", padx=(20, 0))

    def _build_field(self, parent, field, models, padx=28, wrap=360, sep=True):
        """padx / wrap / sep: for a field inside a card (narrower margins, a
        wider help line, no rule under it)."""
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=padx, pady=(16 if sep else 12, 0))

        left = tk.Frame(row, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        name = tk.Label(left, text=field.label, font=F_LABEL, bg=BG, fg=TEXT, anchor="w")
        name.pack(anchor="w")
        if field.tip:
            Tip(name, field.tip)
        field.help_label = None
        if field.help:
            helper = HelpText(left, field.help, wraplength=wrap)
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
            # A twin built earlier (see _build_switch_twin) already has the value.
            var = self._shared_vars.get(field.key) or tk.BooleanVar(
                value=raw.strip().lower() in ("1", "true", "yes"))
            widget = Toggle(row, var, command=self.mark_dirty)
            widget.pack(side="right", padx=(20, 0))
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
                # A fixed list is pick-only; an AI model name can be typed.
                state = "normal" if field.kind == "pick" else "readonly"
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
        elif field.kind == "apps":
            import app_picker
            var = tk.StringVar(value=app_picker.format_names(app_picker.parse_names(raw)))
            box = tk.Frame(row, bg=BG)
            box.pack(side="right", padx=(20, 0))

            def choose(f=field, v=var):
                picked = app_picker.pick_apps(self.root, f.label, v.get(), help_text=f.help)
                if picked is not None:
                    v.set(app_picker.format_names(picked))
            widget = Button(box, "Choose programs...", choose)
            widget.pack(side="right")
            summary = tk.Label(box, font=F_HELP, bg=BG, fg=MUTED, anchor="e",
                               justify="right", wraplength=220)
            summary.pack(side="right", padx=(0, 12))

            def show(*_, v=var, lbl=summary):
                names = app_picker.parse_names(v.get())
                lbl.config(text=("None" if not names else
                                 ", ".join(n[:-4] if n.endswith(".exe") else n
                                           for n in names[:4])
                                 + (" and %d more" % (len(names) - 4) if len(names) > 4 else "")))
            show()
            var.trace_add("write", show)
            var.trace_add("write", lambda *_: self.mark_dirty())
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
        rule = tk.Frame(parent, bg=BORDER, height=1)
        if sep:
            rule.pack(fill="x", padx=padx, pady=(16, 0))
        field.rows = (row, rule)  # so a single field can be hidden (see _refresh_source)

    # --- Phone section -------------------------------------------------------

    # --- Words and Triggers (both saved to api/processing_config.json) -------

    def _build_words_panel(self, parent):
        self._words_cfg = words_config.load()
        self._words_dirty = False

        def changed(*_):
            self._words_dirty = True
            self.mark_dirty()
            self._refresh_hint_count()
            self._refresh_ask_card()

        self._words_changed = changed
        # Vocabulary
        self._phone_heading(
            parent, "Vocabulary",
            "Names, brands and jargon Whisper should spell right.\n"
            "• One per box, e.g. Lesley, Ollama, Kubernetes\n"
            "• Only words it actually gets wrong: Whisper reads a limited amount")
        self.vocab_list = RowList(parent, self._words_cfg.get("vocabulary", []), changed,
                                  "Add a word", width=30)
        self.vocab_list.pack(anchor="w", fill="x", padx=28, pady=(8, 0))
        self.hint_count = tk.Label(parent, text="", font=F_HELP, bg=BG, fg=MUTED,
                                   anchor="w")
        self.hint_count.pack(anchor="w", padx=28, pady=(4, 0))

        # Name corrections
        self._phone_heading(
            parent, "Corrections and shortcuts",
            "Whisper writes the left side? It types the right side instead.\n"
            "• Fix a name it always gets wrong\n"
            "• Or a short phrase for something long: a link, email, number\n"
            "• Whole words, any capitalization; dictation only (plain "
            "transcription is never changed)\n"
            "• Name-like fixes also teach Whisper the spelling")
        if "words_learn" in self._slots:    # "Offer to remember my corrections"
            self._slots["words_learn"].pack(fill="x")
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
        if "words_fillers" in self._slots:  # "Remove filler sounds"
            self._phone_heading(parent, "Filler sounds")
            self._slots["words_fillers"].pack(fill="x")

        self._refresh_hint_count()

    def _build_triggers_panel(self, parent):
        """The Skills page (still "Triggers" in the code). One page, two views:
        the main one (asking the AI, then your skills) and the phrase editor
        behind "Choose your own phrase". Built after the Words page, whose lists
        it is saved with."""
        words_changed = self._words_changed
        # Whose lists the page holds: this PC's word file ("local"), or the
        # server PC's, read and saved over its API ("remote": see
        # _sync_triggers_source). Remote edits never touch this PC's file.
        self._trig_origin = "local"
        self._trig_editable = True
        self._trig_controls = []        # links and buttons switched off with the page
        self._remote_state = None       # None | "loading" | "loaded" | "failed"
        self._remote_sig = None         # the connection the lists were asked from
        self._remote_loaded_custom = []
        self._remote_trig_dirty = False
        self._remote_fetch_id = 0

        def changed(*_):
            if self._trig_origin != "remote":
                words_changed()
                return
            self._remote_trig_dirty = True
            self.mark_dirty()
            self._refresh_ask_card()

        self._trig_changed = changed
        # Shown above whichever view is up while the lists are another PC's.
        self._remote_banner = tk.Frame(parent, bg=BG)
        self._remote_note = tk.Label(self._remote_banner, text=REMOTE_TRIGGERS_NOTE,
                                     font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                                     justify="left", wraplength=470)
        self._remote_note.pack(side="left")
        self._link(self._remote_banner, "Reload", self._load_remote_triggers,
                   font=F_HELP).pack(side="left", anchor="n", padx=(12, 0))
        main = self._skills_main = tk.Frame(parent, bg=BG)
        main.pack(fill="x")
        view = self._phrases_view = tk.Frame(parent, bg=BG)   # see _show_phrases_view

        # --- The main view: the card, then the skills ---
        card = self._card(main, ASK_CARD_TITLE)
        self._ask_line = tk.Label(card, font=F_BODY, bg=BG, fg=TEXT, anchor="w",
                                  justify="left", wraplength=560)
        self._ask_line.pack(anchor="w", padx=self.CARD_PAD, pady=(6, 0))
        self._ask_example = tk.Label(card, font=F_HELP_I, bg=BG, fg=MUTED, anchor="w",
                                     justify="left", wraplength=560)
        self._ask_example.pack(anchor="w", padx=self.CARD_PAD, pady=(2, 0))
        self._ask_button = Button(card, ASK_CARD_BUTTON, self._show_phrases_view)
        self._ask_button.pack(anchor="w", padx=self.CARD_PAD - 2, pady=(12, 0))

        # --- The phrase editor: its own view, one click away ---
        self._link(view, PHRASES_VIEW_BACK, self._show_skills_view).pack(
            anchor="w", padx=28, pady=(18, 0))
        self._trig_heading = self._phone_heading(view, PHRASES_VIEW_TITLE, top=10)
        tk.Label(view, text=TRIGGER_PHRASES_HELP, font=F_HELP, bg=BG, fg=MUTED,
                 anchor="w").pack(anchor="w", padx=28, pady=(3, 0))
        self._trig_example = labelled_example(view, TRIGGER_EXAMPLE_PARTS,
                                              TRIGGER_EXAMPLE_RESULT)
        HelpText(view, TRIGGER_ACTIONS_HELP, wraplength=560).pack(
            anchor="w", padx=28, pady=(8, 0))
        more = tk.Frame(view, bg=BG)
        more.pack(fill="x", padx=28, pady=(10, 0))
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
        self._link(more, "+ Add a phrase",
                   lambda: (self._add_trigger_row("", "edit", changed),
                            self.trig_rows[-1][3].focus_set()),
                   guarded=True).pack(anchor="w", pady=(8, 0))
        self._link(more, "Restore original phrases", lambda: self._reset_triggers(changed),
                   font=F_HELP, guarded=True).pack(anchor="w", pady=(6, 0))
        self._phrase_count = tk.Label(more, text="", font=F_HELP, bg=BG, fg=FAINT, anchor="w")
        self._phrase_count.pack(anchor="w", pady=(10, 0))
        self._build_custom_triggers(main, changed)
        self._refresh_ask_card()

    def _trig_view(self):
        """The view of the Skills page that is up now."""
        return self._phrases_view if self._phrases_view.winfo_manager() else self._skills_main

    def _show_phrases_view(self):
        """"Choose your own phrase": the phrase editor in place of the skills."""
        self._skills_main.pack_forget()
        self._phrases_view.pack(fill="x")
        self.canvas.yview_moveto(0)
        self._round_soon()

    def _show_skills_view(self):
        """Back to the card and the skills (also when the page is opened)."""
        if not hasattr(self, "_phrases_view"):
            return
        self._phrases_view.pack_forget()
        if not self._skills_main.winfo_manager():
            self._skills_main.pack(fill="x")
        self.canvas.yview_moveto(0)

    def _refresh_ask_card(self):
        """The card's sentence and example, from the first "AI edit" phrase as
        the phrase editor holds it now."""
        if not hasattr(self, "_ask_line"):
            return
        by_label = {v: k for k, v in words_config.triggers.ACTION_LABELS.items()}
        phrase = first_edit_phrase([(p.get(), by_label.get(a.get(), "edit"))
                                    for _r, p, a, _e in self.trig_rows])
        if not phrase and not self._trig_editable:
            sentence = example = ""     # another PC's, still loading or unreachable
        else:
            sentence, example = ask_ai_lines(phrase)
        self._ask_line.config(text=sentence)
        self._ask_example.config(text=example)
        self._phrase_count.config(text=self._trigger_summary())

    def _link(self, parent, text, command, font=F_LABEL, guarded=False):
        """A label that reads and clicks as a link. A `guarded` one edits the
        Triggers page, and goes quiet while that page can't be edited."""
        link = tk.Label(parent, text=text, font=font, bg=_bg_of(parent), fg=ACCENT,
                        cursor="hand2")

        def click(_e=None):
            if not guarded or self._trig_editable:
                command()
            return "break"
        link.bind("<Button-1>", click)
        if guarded:
            self._trig_controls.append(link)
        return link

    def _example_box(self, parent, pieces, height=None):
        return example_box(parent, pieces, height)

    # --- Skills: a phrase that stands for a saved instruction --------------------
    # ("custom triggers" in the word file, the server's API and the code.)

    def _build_custom_triggers(self, parent, changed):
        self._custom = words_config.custom_triggers.load(self._words_cfg)
        self._custom_changed = changed
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=28, pady=(22, 0))
        self._custom_heading = tk.Label(bar, text=SKILLS_HEADING, font=F_SECTION, bg=BG,
                                        fg=TEXT, anchor="w")
        self._custom_heading.pack(side="left")
        # Packed from the right: reads "+ New skill", then "Browse the library".
        browse = Button(bar, "Browse the library", self._browse_trigger_library)
        browse.pack(side="right")
        own = Button(bar, NEW_SKILL_BUTTON, self._new_custom_trigger, kind="primary")
        own.pack(side="right", padx=(0, 8))
        self._trig_controls += [own, browse]
        self.custom_box = tk.Frame(parent, bg=BG)
        self.custom_box.pack(fill="x", padx=28, pady=(8, 0))
        self._render_custom_triggers()

    # --- The Triggers page for a PC that only listens -----------------------------
    # The lists then belong to the server PC: fetched and saved over its API
    # (transcribe_client.server_triggers / save_server_triggers), never from or
    # to this PC's word file.

    def _remote_env(self):
        """The saved settings with the "Another PC" boxes as they read now, so
        this works right after pasting a connection code, before Save."""
        from pairing import ALT_URLS_KEY
        env = dict(self.values)
        for key in ("REMOTE_SERVER_URL", "REMOTE_API_KEY", "REMOTE_CERT_SHA256", ALT_URLS_KEY):
            f = self.fields.get(key)
            if f is not None:
                env[key] = f.var.get().strip()
        env["TRANSCRIBE_SOURCE"] = "remote"
        return env

    def _remote_signature(self):
        from pairing import ALT_URLS_KEY
        env = self._remote_env()
        return tuple(env.get(k, "") for k in ("REMOTE_SERVER_URL", "REMOTE_API_KEY",
                                              "REMOTE_CERT_SHA256", ALT_URLS_KEY))

    def _in_background(self, work, done):
        """Run work() off the Tk thread, then done(its result) on the Tk thread.
        work() must not touch Tk and must not raise."""
        import queue
        box = queue.Queue()

        def poll():
            try:
                result = box.get_nowait()
            except queue.Empty:
                try:
                    self.root.after(100, poll)
                except tk.TclError:
                    pass                    # the window was closed meanwhile
                return
            try:
                if not self.root.winfo_exists():
                    return
            except tk.TclError:
                return
            done(result)
        threading.Thread(target=lambda: box.put(work()), daemon=True).start()
        self.root.after(100, poll)

    def _sync_triggers_source(self, src):
        """Follow "Where transcription runs": another PC's lists get the banner
        and are fetched when the page is looked at; otherwise this PC's lists."""
        if not hasattr(self, "_remote_banner"):
            return
        if src == "remote":
            if not self._remote_banner.winfo_manager():
                self._remote_banner.pack(fill="x", padx=28, pady=(18, 0),
                                         before=self._trig_view())
            if getattr(self, "current_section", None) == "Triggers":
                self._triggers_page_shown()
            return
        self._remote_banner.pack_forget()
        self._remote_fetch_id += 1          # an answer still on its way is ignored
        if self._trig_origin == "remote":
            if self._remote_trig_dirty:
                self.status.config(text=REMOTE_TRIGGERS_DISCARDED, fg=WARN)
            self._show_local_triggers()

    def _triggers_page_shown(self):
        """Fetch the server PC's lists the first time they're looked at, after
        the connection changed, and again after a failure."""
        if not hasattr(self, "_remote_banner") or \
                (self._value("TRANSCRIBE_SOURCE") or "local") != "remote":
            return
        if self._remote_state in ("loading", "loaded") and \
                self._remote_sig == self._remote_signature():
            return
        self._load_remote_triggers()

    def _set_triggers_editable(self, on):
        self._trig_editable = on
        for w in self._trig_controls:
            if isinstance(w, Button):
                w.set_enabled(on)
            else:
                w.config(fg=ACCENT if on else FAINT, cursor="hand2" if on else "arrow")
        self._refresh_ask_card()

    def _fill_triggers(self, phrases, custom, origin):
        """Replace both lists on the page. Marks nothing as changed."""
        self._trig_origin = origin
        for row, *_ in list(self.trig_rows):
            row.destroy()
        self.trig_rows = []
        for r in phrases:
            if isinstance(r, dict) and r.get("action") not in words_config.triggers.RETIRED_ACTIONS:
                self._add_trigger_row(str(r.get("phrase") or ""), r.get("action", "edit"),
                                      self._trig_changed)
        clean, _err = words_config.custom_triggers.validate(custom)
        if clean is None:
            # Something this app wouldn't accept: shown as it is rather than
            # dropped (a dropped trigger would be removed from that PC on Save).
            clean = [dict(t) for t in custom if isinstance(t, dict) and t.get("name")
                     and isinstance(t.get("phrases"), list) and t["phrases"]]
        self._custom = clean
        self._render_custom_triggers()
        self._refresh_ask_card()
        self._round_soon()

    def _show_local_triggers(self):
        """Back to this PC's own lists, as its word file has them."""
        cfg = words_config.load()
        self._remote_state = self._remote_sig = None
        self._remote_loaded_custom = []
        self._remote_trig_dirty = False
        self._set_triggers_editable(True)
        self._fill_triggers(words_config.triggers.rows(cfg),
                            words_config.custom_triggers.load(cfg), "local")

    def _load_remote_triggers(self, quiet=False, saved=None):
        """Fetch the server PC's lists and show them in place of what's there.
        `quiet` (after a save): the page stays as it is unless that PC answers
        with something other than `saved`, the (phrases, custom) just sent."""
        if (self._value("TRANSCRIBE_SOURCE") or "local") != "remote":
            return
        if self._remote_state == "loading" and not quiet and                 self._remote_sig == self._remote_signature():
            return                          # that very answer is on its way
        env = self._remote_env()
        self._remote_fetch_id += 1
        fetch_id = self._remote_fetch_id
        self._remote_sig = self._remote_signature()
        if not quiet:
            if self._trig_origin == "remote" and self._remote_trig_dirty:
                self.status.config(text=REMOTE_TRIGGERS_DISCARDED, fg=WARN)
            self._remote_state = "loading"
            self._remote_loaded_custom = []
            self._remote_trig_dirty = False
            self._set_triggers_editable(False)
            self._fill_triggers([], [], "remote")
            self._remote_note.config(text=REMOTE_TRIGGERS_LOADING, fg=MUTED)

        def work():
            try:
                return transcribe_client.server_triggers(env), None
            except transcribe_client.TranscribeError as e:
                return None, skill_wording(str(e))
            except Exception as e:
                return None, "Couldn't read the other PC's skills (%s)." % e.__class__.__name__

        def done(result):
            if fetch_id != self._remote_fetch_id:
                return                      # asked again, or switched away, meanwhile
            lists, err = result
            if quiet:
                if err or self._remote_trig_dirty:
                    return                  # keep the page; edits made since stay
                self._remote_loaded_custom = [dict(t) for t in lists[1] if isinstance(t, dict)]
                if lists != saved:
                    self._fill_triggers(lists[0], lists[1], "remote")
                return
            if err:
                self._remote_state = "failed"
                self._remote_note.config(text=err, fg=DANGER)
                return
            phrases, custom = lists
            self._remote_state = "loaded"
            self._remote_loaded_custom = [dict(t) for t in custom if isinstance(t, dict)]
            self._set_triggers_editable(True)
            self._fill_triggers(phrases, custom, "remote")
            self._remote_note.config(text=REMOTE_TRIGGERS_NOTE, fg=MUTED)
        self._in_background(work, done)

    def _render_custom_triggers(self):
        for w in self.custom_box.winfo_children():
            w.destroy()
        if not self._custom:
            self.custom_box.config(height=1)    # an emptied frame keeps its old size
            if self._trig_editable:     # not while another PC's list is missing
                tk.Label(self.custom_box, text=CUSTOM_TRIGGERS_EMPTY, font=F_HELP_I, bg=BG,
                         fg=FAINT, anchor="w").pack(anchor="w")
            return
        for item in self._custom:
            # One line each: switch, name, what to say, then Edit / Share / remove.
            row = tk.Frame(self.custom_box, bg=BG)
            row.pack(fill="x", pady=(6, 0))
            var = tk.BooleanVar(row, value=item.get("enabled", True))

            def flipped(item=item, var=var):
                item["enabled"] = bool(var.get())
                self._custom_changed()
            Toggle(row, var, command=flipped).pack(side="left", padx=(0, 12))
            # The right side first, so a long name can't push it off the row.
            x = tk.Label(row, text="✕", font=F_BODY, bg=BG, fg=MUTED, cursor="hand2")
            x.pack(side="right", padx=(10, 0))
            Tip(x, "Remove")
            x.bind("<Button-1>", lambda e, i=item: self._remove_custom_trigger(i))
            for label, action in (("Share", self._share_custom_trigger),
                                  ("Edit", self._edit_custom_trigger)):
                link = tk.Label(row, text=label, font=F_LABEL, bg=BG, fg=ACCENT,
                                cursor="hand2")
                link.pack(side="right", padx=(12, 0))
                link.bind("<Button-1>", lambda e, a=action, i=item: a(i))
            tk.Label(row, text=item["name"], font=F_TAB, bg=BG, fg=TEXT,
                     anchor="w").pack(side="left")
            tk.Label(row, text=skill_summary(item), font=F_HELP, bg=BG, fg=MUTED,
                     anchor="w").pack(side="left", padx=(14, 0))

    def _builtin_phrases(self):
        """The "prompt AI" and end phrases as the page shows them now."""
        return [p.get().strip() for _r, p, _a, _e in getattr(self, "trig_rows", [])
                if p.get().strip()]

    def _check_custom(self, candidate, replacing=None):
        """An error if `candidate` can't sit next to the other skills, else None."""
        others = [t for t in self._custom if t is not replacing]
        _clean, err = words_config.custom_triggers.validate(
            others + [candidate], builtin_phrases=self._builtin_phrases())
        return skill_wording(err)

    def _new_custom_trigger(self):
        import trigger_dialogs
        item = trigger_dialogs.edit_trigger(
            self.root, None, self._check_custom,
            on_library=lambda: self._browse_trigger_library(CUSTOM_TRIGGER_EXAMPLE_ID))
        if item:
            self._custom.append(item)
            self._render_custom_triggers()
            self._custom_changed()

    def _edit_custom_trigger(self, item):
        import trigger_dialogs
        edited = trigger_dialogs.edit_trigger(
            self.root, item, lambda c: self._check_custom(c, replacing=item))
        if edited:
            self._custom[self._custom.index(item)] = edited
            self._render_custom_triggers()
            self._custom_changed()

    def _remove_custom_trigger(self, item):
        self._custom.remove(item)
        self._render_custom_triggers()
        self._custom_changed()

    def _add_library_trigger(self, entry):
        """Add (or update) a skill from the library. Returns an error or None."""
        import trigger_library
        clash = trigger_library.phrase_clashes(entry, self._custom, self._builtin_phrases())
        if clash:
            return ('"%s" is already a phrase of another skill. Change that one first, '
                    'then add this.' % clash[0])
        new = words_config.custom_triggers.from_library(entry)
        old = next((t for t in self._custom if t.get("library_id") == entry["id"]), None)
        err = self._check_custom(new, replacing=old)
        if err:
            return err
        if old is not None:
            new["enabled"] = old.get("enabled", True)
            self._custom[self._custom.index(old)] = new
        else:
            self._custom.append(new)
        self._render_custom_triggers()
        self._custom_changed()
        return None

    def _browse_trigger_library(self, focus=None):
        """Open the library. `focus`: the id of an entry to bring into view."""
        import trigger_dialogs
        trigger_dialogs.browse_library(self.root, lambda: self._custom,
                                       self._add_library_trigger, focus=focus)

    def _share_custom_trigger(self, item):
        """Open the pre-filled share form in the browser. Nothing is published
        until the user presses Submit there."""
        import trigger_library
        url, fits = trigger_library.share_link(item)
        if not fits:
            self.root.clipboard_clear()
            self.root.clipboard_append(trigger_library.share_json(item))
        try:
            webbrowser.open(url)
        except Exception:
            self.status.config(text="Couldn't open the browser.", fg=WARN)
            return
        self.status.config(
            text=("Share form opened in your browser. Nothing is published until you "
                  "press Submit there." if fits else
                  "Share form opened. The skill is on your clipboard: paste it into "
                  "the form."), fg=ACCENT)

    def collect_custom_triggers(self):
        """(list, error): the skills, checked against the built-in phrases."""
        clean, err = words_config.custom_triggers.validate(
            getattr(self, "_custom", []), builtin_phrases=self._builtin_phrases())
        return clean, skill_wording(err)

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
            Placeholder(e, var, hint)
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
            "Dictate differently depending on where you're typing.\n"
            "• e.g. casual in Slack, professional in Outlook, as said in a terminal\n"
            "• Pick an open program, or type its file name (slack.exe)\n"
            "• Choices left on \"%s\" follow the settings above\n"
            "• Tone, rewriting and the rest only matter when cleanup runs\n"
            "• For the hotkey and mic button on this PC, from the next "
            "dictation (no restart)" % app_styles.SAME)
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
        """'4 phrases (3 AI edit, 1 end of instruction)', under the phrase list."""
        labels = words_config.triggers.ACTION_LABELS
        counts = {}
        for _r, _p, a, _e in getattr(self, "trig_rows", []):
            counts[a.get()] = counts.get(a.get(), 0) + 1
        parts = ["%d %s" % (counts[labels[k]], labels[k] if k == "edit" else labels[k].lower())
                 for k in ("edit", "end") if counts.get(labels[k])]
        n = sum(counts.values())
        if not n and not getattr(self, "_trig_editable", True):
            return "Phrase list not loaded"     # another PC's, still loading or unreachable
        return "%d phrase%s%s" % (n, "" if n == 1 else "s",
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
                           font=F_BODY, values=[labels[k] for k in ("edit", "end")])
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
        clean, err = words_config.triggers.validate(rows)
        return clean, skill_wording(err)

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

    # --- The Transcription page's views -------------------------------------------
    # One main view with ONE "Advanced" button. Behind it is the Advanced view
    # for where dictation runs; it replaces the main view and has a Back link
    # (as "Choose your own phrase" does on Skills). The model list is one step
    # further in and leads back to Advanced.

    def _build_tx_views(self, page, main):
        self._tx_main = main
        self._tx_views, self._tx_bodies = {}, {}
        for name, title, help_text in TX_VIEWS:
            view = tk.Frame(page, bg=BG)
            if name == "models":
                back = self._link(view, TX_BACK_ADVANCED, self._open_tx_advanced)
            else:
                back = self._link(view, TX_BACK, self._show_tx_main)
            back.pack(anchor="w", padx=28, pady=(18, 0))
            self._phone_heading(view, title, help_text, top=10)
            body = tk.Frame(view, bg=BG)
            body.pack(fill="x")
            self._tx_views[name], self._tx_bodies[name] = view, body
        # Shown under the Advanced view for this PC and for another PC (a
        # widget has one parent, and both views need what is in here).
        self._tx_common = tk.Frame(page, bg=BG)

    # The Advanced views that end with the shared block (_tx_common). Not the
    # cloud one: "use my cloud service instead" means nothing there.
    TX_COMMON_VIEWS = ("adv_local", "adv_remote")

    def _view_target(self, view):
        """Where a schema field with view=... is built."""
        if view == "adv_local_fw":
            return self._adv_fw
        if view == "adv_common":
            return self._tx_common
        if view.startswith("words_"):
            # A frame on the page being built; _build_words_panel packs it.
            if view not in self._slots:
                self._slots[view] = tk.Frame(self._building, bg=BG)
            return self._slots[view]
        return self._tx_bodies[view]

    def _open_tx_advanced(self):
        """The page's one Advanced button: the view for where dictation runs."""
        self._show_tx_view("adv_" + (self._value("TRANSCRIBE_SOURCE") or "local"))

    def _tx_view(self):
        """The name of the Advanced view that is up now, or None (the main view)."""
        return next((n for n, v in getattr(self, "_tx_views", {}).items()
                     if v.winfo_manager()), None)

    def _show_tx_view(self, name):
        self._tx_main.pack_forget()
        self._tx_common.pack_forget()
        for n, view in self._tx_views.items():
            if n == name:
                view.pack(fill="x")
            else:
                view.pack_forget()
        if name in self.TX_COMMON_VIEWS:
            self._tx_common.pack(fill="x")      # after the view just packed
        self.canvas.yview_moveto(0)
        self._round_soon()

    def _show_tx_main(self, scroll=True):
        if not hasattr(self, "_tx_views"):
            return
        self._tx_common.pack_forget()
        for view in self._tx_views.values():
            view.pack_forget()
        if not self._tx_main.winfo_manager():
            self._tx_main.pack(fill="x")
        if scroll:
            self.canvas.yview_moveto(0)

    # --- This PC: what was found, and what dictation runs on ----------------------

    # Settings downloads what is missing itself; the setup wizard does it on
    # its Install step and only says how much.
    DOWNLOAD_IN_PLACE = True

    def _radio_row(self, parent, var, value, title, text="", top=10, padx=None,
                   command=None):
        """One choice of several: a dot, a title and a line of explanation.
        Click anywhere on it, or press Space on it. command: called on a
        click, before the choice is set."""
        row = tk.Frame(parent, bg=BG, cursor="hand2", takefocus=1, highlightthickness=1,
                       highlightbackground=BG, highlightcolor=TEXT)
        row.pack(fill="x", padx=self.CARD_PAD - 4 if padx is None else padx, pady=(top, 0))
        dot = tk.Label(row, text="", font=F_BUTTON, bg=BG, fg=ACCENT, width=2)
        dot.pack(side="left", anchor="n")
        body = tk.Frame(row, bg=BG)
        body.pack(side="left", fill="x", expand=True)
        name = tk.Label(body, text=title, font=F_LABEL, bg=BG, fg=TEXT, anchor="w")
        name.pack(anchor="w")
        widgets = [row, dot, body, name]
        if text:
            help_label = tk.Label(body, text=text, font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                                  justify="left", wraplength=540)
            help_label.pack(anchor="w")
            widgets.append(help_label)

        def refresh(*_):
            dot.config(text="●" if var.get() == value else "○")

        def pick(_e=None):
            if command is not None:
                command(value)
            var.set(value)
            return "break"
        for w in widgets:
            w.bind("<Button-1>", pick)
        for ev in ("<space>", "<Return>"):
            row.bind(ev, pick)
        var.trace_add("write", refresh)
        refresh()
        return row

    def _build_engine_card(self, parent, field):
        """The "This PC" card: one statement about the graphics that were
        found and what dictation runs on, the accuracy in a word, and (only
        while something is missing) one download with one progress bar.

        Behind its links: the model lists ("Choose a model") and the choice
        of engine, where it runs and the model folder ("Advanced")."""
        pad = self.CARD_PAD
        saved = self.values
        # The second engine's download exists for Windows and 64-bit Linux.
        # Elsewhere the card only names the accuracy and offers the model list.
        self._cpp_ok = engine_files.available()
        self._graphics = detected_graphics() if self._cpp_ok else []
        card, _built_in = hardware.pick_cards(self._graphics)
        # A separate AMD or Intel card and no NVIDIA one: the second engine is
        # the one that can use it.
        self._suggest_cpp = bool(card) and "nvidia" not in card.lower()
        self._offer_declined = False
        self._engine_job = None
        self._downloading = None
        field.var = tk.StringVar(value=whispercpp_engine.engine_name(saved.get(field.key)))
        model_value = (saved.get("WHISPER_CPP_MODEL") or "").strip() or (
            engine_files.DEFAULT_MODEL if card else engine_files.CPU_MODEL)
        model = Field("WHISPER_CPP_MODEL", "Speech model", "otext")
        model.var = tk.StringVar(value=engine_files.model_key(model_value) or model_value)
        device = Field("WHISPER_CPP_DEVICE", "Runs on", "otext")
        choices = list(ENGINE_DEVICE_CHOICES)
        dev_value = whispercpp_engine.normalize_device(saved.get("WHISPER_CPP_DEVICE"))
        if dev_value not in ("auto", "cpu"):   # set by hand: keep it selectable
            choices.append(("Graphics device %s" % dev_value, dev_value))
        device.labelled_choices = choices
        device.var = tk.StringVar(value=next(l for l, v in choices if v == dev_value))
        self._engine_var, self._cpp_model_var, self._cpp_device = field.var, model.var, device
        if self._cpp_ok:
            for f in (field, model, device):
                self.fields[f.key] = f

        # --- the card itself ---
        # No heading: the choice right above it already says "This PC".
        outer = self._engine_card = tk.Frame(parent, bg=BG, highlightthickness=1,
                                             highlightbackground=BORDER,
                                             highlightcolor=BORDER)
        outer.pack(fill="x", padx=28, pady=(14, 0))
        body = tk.Frame(outer, bg=BG)
        body.pack(fill="x")
        tk.Frame(outer, bg=BG, height=14).pack(fill="x")
        self._pc_statement = tk.Label(body, text="", font=F_BODY, bg=BG, fg=TEXT, anchor="w",
                                      justify="left", wraplength=580)
        self._pc_note = tk.Label(body, text="", font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                                 justify="left", wraplength=580)
        self._not_right_box = tk.Frame(body, bg=BG)
        note = tk.Frame(self._not_right_box, bg=BG)
        tk.Label(note, text=hardware.NOT_RIGHT_NOTE + "\n" + NOT_RIGHT_ADVANCED, font=F_HELP,
                 bg=BG, fg=MUTED, anchor="w", justify="left", wraplength=560).pack(
            anchor="w", padx=pad + 4, pady=(4, 0))
        self._not_right = Expander(self._not_right_box, note, hardware.NOT_RIGHT_LINK,
                                   hardware.NOT_RIGHT_LINK)
        self._not_right.config(font=F_HELP)
        self._not_right.pack(anchor="w", padx=pad - 2)
        if self._cpp_ok:
            self._pc_statement.pack(anchor="w", padx=pad, pady=(14, 0))
            self._not_right_box.pack(fill="x", pady=(4, 0))

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", padx=pad, pady=(10 if self._cpp_ok else 14, 0))
        self._accuracy = tk.Label(row, text="", font=F_BODY, bg=BG, fg=TEXT, anchor="w")
        self._accuracy.pack(side="left")

        # One download: a sentence with one size, one button, one bar. The
        # sentence and the Download button show only while something is missing.
        self._dl_line = tk.Label(body, text="", font=F_BODY, bg=BG, fg=TEXT, anchor="w",
                                 justify="left", wraplength=580)
        last = self._card_last_row = tk.Frame(body, bg=BG)
        last.pack(fill="x", padx=pad - 2, pady=(12, 0))
        self._engine_button = Button(last, "Download", self._card_download, kind="primary")
        self._engine_cancel = Button(last, "Cancel", self._engine_cancel_download)
        self._dl_bar = ProgressBar(last)
        self._dl_percent = tk.Label(last, text="", font=F_HELP, bg=BG, fg=TEXT)
        self._engine_status = tk.Label(body, text="", font=F_HELP, bg=BG, fg=MUTED,
                                       anchor="w", justify="left", wraplength=580)

        # --- "Choose a model": one list per engine, only one of them shown ---
        lists = self._tx_bodies["models"]
        self._fw_models = tk.Frame(lists, bg=BG)       # filled by _build_model_picker
        self._cpp_models = tk.Frame(lists, bg=BG)
        box = tk.Frame(self._cpp_models, bg=BG)
        box.pack(fill="x", padx=26, pady=(6, 0))
        self._cpp_rows = {}
        offered = [(key, spec.title, spec) for key, spec in engine_files.MODELS.items()]
        if model.var.get() not in engine_files.MODELS:   # a file of the person's own
            offered.append((model.var.get(), "Your own file: %s" % model.var.get(), None))
        for key, title, spec in offered:
            lines = [] if spec is None else [
                "Download: %s  ·  Accuracy: %s" % (spec.size_text, spec.accuracy),
                "Speed: " + spec.speed, spec.note]
            _row, name, dl, use = self._model_row(
                box, title, lines, lambda k=key: self._cpp_model_var.set(k),
                lambda k=key: self._download_cpp_model(k))
            if spec is None:
                dl.pack_forget()
            self._cpp_rows[key] = (name, dl, use, title)

        # --- "Advanced" ---
        adv = self._tx_bodies["adv_local"]
        # The model: which one is in use, and the way to the list of them.
        tk.Label(adv, text=MODEL_HEADING, font=F_BUTTON, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=28, pady=(16, 0))
        line = tk.Frame(adv, bg=BG)
        line.pack(fill="x", padx=28, pady=(6, 0))
        self._model_link = Button(line, MODEL_LINK, lambda: self._show_tx_view("models"))
        self._model_link.pack(side="right")
        self._model_now = tk.Label(line, text="", font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                                   justify="left", wraplength=400)
        self._model_now.pack(side="left")
        tk.Frame(adv, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))
        if self._cpp_ok:
            tk.Label(adv, text=ENGINE_HEADING, font=F_BUTTON, bg=BG, fg=TEXT,
                     anchor="w").pack(anchor="w", padx=28, pady=(16, 0))
            for value, title, text in ENGINE_CHOICES:
                self._radio_row(adv, field.var, value, title, text, top=6, padx=24,
                                command=self._engine_picked)
        blocks = tk.Frame(adv, bg=BG)
        blocks.pack(fill="x")
        self._adv_fw = tk.Frame(blocks, bg=BG)     # the usual engine: WHISPER_DEVICE
        more = self._adv_cpp = tk.Frame(blocks, bg=BG)
        line = tk.Frame(more, bg=BG)
        line.pack(fill="x", padx=28, pady=(16, 0))
        tk.Label(line, text="Runs on", font=F_LABEL, bg=BG, fg=TEXT).pack(side="left")
        device.widget = ttk.Combobox(line, textvariable=device.var, font=F_BODY,
                                     state="readonly", values=[l for l, _v in choices],
                                     width=24)
        device.widget.pack(side="left", padx=(12, 0))
        HelpText(more, ENGINE_AUTO_HELP, wraplength=560).pack(
            anchor="w", padx=28, pady=(6, 0))
        # What this PC has and, after a first start, what Automatic chose.
        self._engine_device_label = HelpText(more, "", wraplength=560)
        self._engine_device_label.pack(anchor="w", padx=28, pady=(10, 0))
        tk.Frame(more, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))
        # The parts of the download, for anyone who wants them.
        self._dl_parts = tk.Label(adv, text="", font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                                  justify="left", wraplength=560)
        self._adv_folder = tk.Frame(adv, bg=BG)    # the model folder (_build_model_picker)
        self._adv_folder.pack(fill="x")

        for f in (field, model, device):
            f.var.trace_add("write", lambda *_: (self.mark_dirty(), self._refresh_engine()))

    def _model_row(self, box, title, lines, on_use, on_download):
        """One model in a list: its name, a few plain lines, Download and Use.
        Returns (row, name label, Download button, Use button)."""
        # Rows sit on SURFACE so the RAISED ghost buttons read as buttons.
        row = tk.Frame(box, bg=SURFACE)
        row.pack(fill="x", pady=(6, 0))
        # Pack the buttons FIRST: Tk gives earlier-packed widgets their
        # space, so long notes wrap instead of pushing buttons off-screen.
        right = tk.Frame(row, bg=SURFACE)
        right.pack(side="right", padx=12)
        use = Button(right, "Use", on_use)
        use.config(width=7, padx=8)
        use.pack(side="right", padx=(6, 0))
        dl = Button(right, "Download", on_download)
        dl.config(width=14, padx=8)
        dl.pack(side="right")
        left = tk.Frame(row, bg=SURFACE)
        left.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        name = tk.Label(left, text=title, font=F_BUTTON, bg=SURFACE, fg=TEXT, anchor="w")
        name.pack(anchor="w")
        for i, line in enumerate(l for l in lines if l):
            tk.Label(left, text=line, font=F_HELP, bg=SURFACE, fg=TEXT if i == 0 else MUTED,
                     anchor="w", justify="left", wraplength=430).pack(anchor="w")
        return row, name, dl, use

    def _engine_on(self):
        """True when the whisper.cpp engine is the one chosen on the page."""
        return getattr(self, "_cpp_ok", False) and self._engine_var.get() == ENGINE_CPP

    def _engine_pending(self):
        """True when this PC has a separate AMD or Intel card (and no NVIDIA
        one) but nothing was ever chosen for it: the card offers the one
        download that puts dictation on it, and switches when it is there.
        Until then the saved settings stay exactly as they are."""
        return (getattr(self, "_cpp_ok", False) and self._suggest_cpp
                and not self._offer_declined and not self._engine_on()
                and "WHISPER_ENGINE" not in self.values)

    def _engine_picked(self, _value):
        """A choice made by hand under Advanced: the offer above is over."""
        self._offer_declined = True

    def _engine_missing(self):
        """What the second engine and its model still need: [(file, where it goes)]."""
        return engine_files.missing(self._cpp_model_var.get().strip(), ENGINE_ROOT,
                                    self._model_root())

    def _engine_last_choice(self):
        """What the engine remembered about its last start, or None."""
        folder = engine_files.models_folder(self._model_root(), ENGINE_ROOT)
        return whispercpp_engine.read_auto_cache(
            os.path.join(folder, whispercpp_engine.AUTO_CACHE_NAME)).get("last")

    def _accuracy_word(self):
        """The chosen model's accuracy in a word, or None for a model of the
        person's own."""
        if self._engine_on() or self._engine_pending():
            spec = engine_files.MODELS.get(self._cpp_model_var.get().strip())
            return spec.accuracy if spec else None
        info = model_catalog.BY_NAME.get(self.fields["WHISPER_MODEL_SIZE"].var.get().strip())
        return info.accuracy if info else None

    def _pc_text(self):
        """(the statement about this PC, a muted line under it or "")."""
        card, built_in = hardware.pick_cards(self._graphics)
        smaller = self._accuracy_word() in ("basic", "good")
        if self._engine_on() or self._engine_pending():
            on_card = bool(card) and (self._engine_pending()
                                      or self._value("WHISPER_CPP_DEVICE") != "cpu")
            new = on_card and "nvidia" not in card.lower()
            return (hardware.found_text(card, built_in, on_card, smaller),
                    hardware.NEW_CARD_NOTE if new else "")
        nvidia = card if "nvidia" in card.lower() else ""
        device = self._value("WHISPER_DEVICE")
        if nvidia:
            return hardware.found_text(nvidia, built_in, device != "cpu", smaller), ""
        if device == "cuda":
            return NO_CARD_BUT_SET, ""
        return hardware.found_text(card, built_in, False, smaller), ""

    def _download_needed(self):
        """(bytes, "cpp" | "fw") still to download for what the page shows, or
        (0, None) when everything is on this PC."""
        if self._engine_on() or self._engine_pending():
            todo = self._engine_missing()
            return (engine_files.total_size(todo), "cpp") if todo else (0, None)
        info = model_catalog.BY_NAME.get(self.fields["WHISPER_MODEL_SIZE"].var.get().strip())
        root = self._model_root()
        if info and root and not model_catalog.is_downloaded(info, root):
            return info.size_mb * 10 ** 6, "fw"     # the catalogue's sizes are round MB
        return 0, None

    def _refresh_engine(self):
        """Bring the card and its views up to date with what is chosen."""
        if not hasattr(self, "_pc_statement") or "WHISPER_MODELS_DIR" not in self.fields:
            return
        cpp = self._engine_on()
        second = cpp or self._engine_pending()
        for block, on in ((self._fw_models, not second), (self._cpp_models, second),
                          (self._adv_fw, not cpp), (self._adv_cpp, cpp)):
            if not on:
                block.pack_forget()
            elif not block.winfo_manager():
                block.pack(fill="x")
        if self._cpp_ok:
            text, note = self._pc_text()
            self._pc_statement.config(text=text)
            self._pc_note.config(text=note)
            self._pc_note.pack_forget()
            if note:
                self._pc_note.pack(anchor="w", padx=self.CARD_PAD, pady=(2, 0),
                                   after=self._pc_statement)
        word = self._accuracy_word()
        self._accuracy.config(text="Accuracy: %s" % word if word else ACCURACY_OWN)
        if second:
            key = self._cpp_model_var.get().strip()
            spec = engine_files.MODELS.get(key)
            using = spec.title if spec else key
        else:
            using = self.fields["WHISPER_MODEL_SIZE"].var.get().strip()
        self._model_now.config(text="In use: %s" % using)
        if cpp:
            last = self._engine_last_choice()
            text = engine_device_text(self._graphics, last)
            if isinstance(last, dict) and last.get("device") == "cpu" \
                    and self._value("WHISPER_CPP_MODEL") == engine_files.DEFAULT_MODEL:
                text += "\n" + ENGINE_SLOW_NOTE
            self._engine_device_label.config(text=text)
        self._refresh_cpp_models()
        self._refresh_download()

    def _refresh_cpp_models(self):
        """The second engine's list: which model is chosen, which are here."""
        folder = engine_files.models_folder(self._model_root(), ENGINE_ROOT)
        active = self._cpp_model_var.get().strip()
        job = self._engine_job
        for key, (name, dl, use, title) in self._cpp_rows.items():
            spec = engine_files.MODELS.get(key)
            selected = key == active
            name.config(text=title + ("   • selected" if selected else ""),
                        fg=ACCENT if selected else TEXT)
            use.config(text="Selected" if selected else "Use")
            use.set_enabled(not selected and job is None)
            if spec is None:
                continue
            if job is not None and selected:
                dl.set_enabled(False)
                dl.config(text="Downloading %d%%" % (job["percent"] or 0), fg=WARN)
                continue
            have = os.path.isfile(os.path.join(folder, spec.name))
            # One control carries the state: "Download", or a disabled "Downloaded".
            dl.config(text="✓ Downloaded" if have else "Download")
            dl.set_enabled(not have and job is None and self._downloading is None)
            if have:
                dl.config(fg=ACCENT)

    def _refresh_download(self):
        """The one download: its sentence and button while something is
        missing, nothing at all once everything is on this PC."""
        if self._engine_job is not None or self._downloading is not None:
            return      # the progress poller owns the button and the bar
        size, kind = self._download_needed()
        self._show_progress(None)
        self._set_download_status("")
        ready = not size and self._engine_pending()     # here already, never switched on
        if size:
            sentence = DOWNLOAD_NEEDED if self.DOWNLOAD_IN_PLACE else DOWNLOAD_BY_SETUP
            self._dl_line.config(text=sentence % engine_files.about_size_text(size))
        elif ready:
            self._dl_line.config(text=PENDING_READY)
        if size or ready:
            self._dl_line.pack(anchor="w", padx=self.CARD_PAD, pady=(12, 0),
                               before=self._card_last_row)
        else:
            self._dl_line.pack_forget()
        if (size or ready) and self.DOWNLOAD_IN_PLACE:
            self._engine_button.config(text="Download" if size else "Use it")
            self._engine_button.set_enabled(True)
            if not self._engine_button.winfo_manager():
                self._engine_button.pack(side="left")
        else:
            self._engine_button.pack_forget()
        todo = self._engine_missing() if kind == "cpp" else []
        self._dl_parts.pack_forget()
        if todo:
            self._dl_parts.config(text=DOWNLOAD_PARTS % engine_files.missing_text(todo))
            self._dl_parts.pack(anchor="w", padx=28, pady=(16, 0), before=self._adv_folder)

    def _show_progress(self, percent, can_cancel=False):
        """One bar and one percentage for the whole download; None hides them."""
        for w in (self._engine_cancel, self._dl_bar, self._dl_percent):
            w.pack_forget()
        if percent is None:
            return
        self._engine_button.config(text="Downloading")
        self._engine_button.set_enabled(False)
        if not self._engine_button.winfo_manager():
            self._engine_button.pack(side="left")
        if can_cancel:
            self._engine_cancel.pack(side="left", padx=(8, 0))
        self._dl_bar.set(percent)
        self._dl_bar.pack(side="left", padx=(14, 0))
        self._dl_percent.config(text="%d%%" % percent)
        self._dl_percent.pack(side="left", padx=(10, 0))

    def _set_download_status(self, text, color=MUTED):
        """A line under the card's button: a cancelled or failed download."""
        self._engine_status.config(text=text, fg=color)
        self._engine_status.pack_forget()
        if text:
            self._engine_status.pack(anchor="w", padx=self.CARD_PAD, pady=(8, 0),
                                     after=self._card_last_row)

    def _card_download(self):
        """The card's one button: fetch whatever is still missing."""
        size, kind = self._download_needed()
        if kind == "cpp":
            self._engine_download()
        elif kind == "fw":
            self._download_model(model_catalog.BY_NAME[
                self.fields["WHISPER_MODEL_SIZE"].var.get().strip()])
        elif self._engine_pending():        # "Use it": the files are here already
            self._engine_var.set(ENGINE_CPP)
            self.status.config(text=ENGINE_USE_IT, fg=ACCENT)

    def _download_cpp_model(self, key):
        """Download on a row of the second engine's list: choose that model
        and fetch it, with anything else the engine still needs."""
        if self._engine_job is None and self._downloading is None:
            self._cpp_model_var.set(key)
            self._engine_download()

    def _engine_download(self):
        """Fetch whatever the second engine and its model still need, on a
        worker thread. The worker only writes into the job record;
        _engine_poll (on the Tk thread) shows it as one percentage."""
        todo = self._engine_missing()
        if self._engine_job is not None or self._downloading is not None or not todo:
            return
        overall = engine_files.Overall(todo)
        job = {"cancel": threading.Event(), "percent": 0, "result": None,
               "pending": self._engine_pending()}
        model, models_dir = self._cpp_model_var.get().strip(), self._model_root()

        def work():
            try:
                engine_files.fetch_missing(
                    model, ENGINE_ROOT, models_dir,
                    progress=lambda spec, done, total: job.update(
                        percent=overall.update(spec, done)),
                    cancelled=job["cancel"].is_set)
                job["result"] = ("ok", "")
            except engine_files.Cancelled:
                job["result"] = ("cancelled", "")
            except Exception as e:
                job["result"] = ("error", str(e))
        self._engine_job = job
        self._set_download_status("")
        self._show_progress(0, can_cancel=True)
        self._refresh_cpp_models()
        threading.Thread(target=work, daemon=True).start()
        self._engine_poll()

    def _engine_cancel_download(self):
        if self._engine_job is not None:
            self._engine_job["cancel"].set()
            self._set_download_status("Stopping...", WARN)

    def _engine_poll(self):
        job = self._engine_job
        if job is None:
            return
        if job["result"] is None:
            if not job["cancel"].is_set():
                self._show_progress(job["percent"], can_cancel=True)
                self._refresh_cpp_models()
            self.root.after(300, self._engine_poll)
            return
        kind, detail = job["result"]
        self._engine_job = None
        if kind == "ok" and job["pending"]:
            # It was fetched for the card that was found: use that card now.
            self._engine_var.set(ENGINE_CPP)
        self._refresh_engine()
        if kind == "ok":
            # Something to save now: the choice can go into effect.
            self.mark_dirty()
            self.status.config(text=ENGINE_DOWNLOADED, fg=ACCENT)
        elif kind == "cancelled":
            self._set_download_status("Download cancelled.")
        else:
            self._set_download_status(detail, DANGER)

    def _engine_save_problem(self, updates):
        """Why these settings can't be saved for the whisper.cpp engine (one
        plain sentence), or None. Its files have to be on this PC first:
        otherwise the next restart would leave dictation with no engine."""
        if updates.get("WHISPER_ENGINE") != ENGINE_CPP \
                or updates.get("TRANSCRIBE_SOURCE", "local") != "local":
            return None
        todo = self._engine_missing()
        if todo:
            return ENGINE_SAVE_REFUSED % engine_files.about_size_text(
                engine_files.total_size(todo))
        return whispercpp_engine.files_problem(
            dict(self.values, **updates), ENGINE_ROOT,
            engine_files.models_folder(self._model_root(), ENGINE_ROOT))

    def _build_model_picker(self, parent):
        """The usual engine's models, for the "Choose a model" view, and the
        model folder line, for "Advanced"."""
        # The model folder: a setting most people never touch, so it's one line
        # (with Open and Change) rather than a field of its own.
        folder = Field("WHISPER_MODELS_DIR", "Model folder", "text", default="models")
        folder.var = tk.StringVar(value=self.values.get("WHISPER_MODELS_DIR", "") or "models")
        folder.var.trace_add("write", lambda *_: self.mark_dirty())
        self.fields[folder.key] = folder
        self._model_rows = {}
        self._model_frames = {}
        self._models_all = False
        self._downloading = None
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x", padx=26, pady=(6, 0))
        self._models_box = box
        for info in model_catalog.CATALOG:
            size = ("%.1f GB" % (info.size_mb / 1024) if info.size_mb >= 1000
                    else "%d MB" % info.size_mb)
            row, title, dl, use = self._model_row(
                box, info.name,
                ["Download: %s  ·  Accuracy: %s" % (size, info.accuracy),
                 "Speed: " + info.speed, info.why,
                 "Graphics memory: %s" % info.vram],
                lambda i=info: self._use_model(i), lambda i=info: self._download_model(i))
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
        holder = self._adv_folder
        tk.Label(holder, text="Model folder", font=F_BUTTON, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=28, pady=(16, 0))
        line = tk.Frame(holder, bg=BG)
        line.pack(fill="x", padx=28, pady=(4, 0))
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
            if sys.platform != "win32":
                linux_settings.open_path(path)
                return
            os.startfile(path)

    def _change_model_folder(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self.root, title="Where to keep the models",
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
            busy = self._downloading is not None or self._engine_job is not None
            selected = info.name == active
            title.config(text=info.name + ("   • selected" if selected else ""),
                         fg=ACCENT if selected else TEXT)
            use.config(text="Selected" if selected else "Use")
            use.set_enabled(not selected)
            if self._downloading == info.name:
                continue  # the progress poller owns this row's button text
            # One control carries the state: "Download", or a disabled "Downloaded".
            dl.config(text="✓ Downloaded" if have else "Download")
            dl.set_enabled(not have and not busy and bool(root))
            if have:
                dl.config(fg=ACCENT)
        self._refresh_engine()      # the card's accuracy and its download follow

    def _use_model(self, info):
        self.fields["WHISPER_MODEL_SIZE"].var.set(info.name)
        if not model_catalog.is_downloaded(info, self._model_root()):
            self.status.config(
                text="%s isn't downloaded: restarting will fetch it first and "
                     "dictation will be offline for a few minutes." % info.name, fg=WARN)

    def _download_model(self, info):
        root = self._model_root()
        if not root or self._downloading or self._engine_job is not None:
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
            self._set_download_status("")
            self._show_progress(pct)        # the same number on the card
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

    def _phone_heading(self, parent, title, help_text="", padx=28, font=F_SECTION, top=20):
        heading = tk.Label(parent, text=title, font=font, bg=BG, fg=TEXT, anchor="w")
        heading.pack(anchor="w", padx=padx, pady=(top, 0))
        if help_text:
            HelpText(parent, help_text, wraplength=560).pack(
                anchor="w", padx=padx, pady=(3, 0))
        return heading

    CARD_PAD = 18   # space between a card's outline and what's in it

    def _card(self, parent, title):
        """A bordered box with its own bold heading: one job per box, so two
        things on a page can't be read as one. Returns the frame to fill."""
        card = tk.Frame(parent, bg=BG, highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=BORDER)
        card.pack(fill="x", padx=28, pady=(18, 0))
        self._phone_heading(card, title, padx=self.CARD_PAD, top=14)
        body = tk.Frame(card, bg=BG)
        body.pack(fill="x")
        tk.Frame(card, bg=BG, height=self.CARD_PAD).pack(fill="x")
        return body

    def _build_phone_panel(self, page):
        # Two boxes, one job each: "Your phone" and "Another PC". Everything
        # technical is folded under the page's one Advanced: connecting any
        # other program (with the switches that change its address), then
        # the port numbers, the certificate and the key itself. Built in a
        # different order than shown (the address finder and the pairing code
        # need the port and key fields first), so each part has its own frame.
        pad = self.CARD_PAD
        phone_card = self._card(page, "Your phone")
        other_card = self._card(page, PHONE_PC_CARD)
        pair_f, app_f = tk.Frame(phone_card, bg=BG), tk.Frame(phone_card, bg=BG)
        pc_f = tk.Frame(other_card, bg=BG)
        for f in (pair_f, app_f, pc_f):
            f.pack(fill="x")
        adv_holder = tk.Frame(page, bg=BG)
        adv_holder.pack(fill="x", pady=(18, 0))
        adv_f = tk.Frame(adv_holder, bg=BG)
        self._phone_advanced = Expander(adv_holder, adv_f, PHONE_ADVANCED, "Hide advanced")
        self._phone_advanced.pack(anchor="w", padx=28)
        program_card = self._card(adv_f, "Any other program")
        addr_f, switch_f = tk.Frame(program_card, bg=BG), tk.Frame(program_card, bg=BG)
        for f in (addr_f, switch_f):
            f.pack(fill="x")
        ports_f, key_f = tk.Frame(adv_f, bg=BG), tk.Frame(adv_f, bg=BG)
        ports_f.pack(fill="x")
        key_f.pack(fill="x")

        def field(f, raw=None, **how):
            if raw is not None:
                self.values.setdefault(f.key, raw)
            self._build_field(parent, f, None, **how)
            self.fields[f.key] = f
            return f

        # --- The switches that change the address (under the result box) ---
        parent = switch_f
        compact = {"padx": pad, "wrap": 470, "sep": False}
        tk.Label(parent, text="These switches change the address above", font=F_BUTTON,
                 bg=BG, fg=TEXT, anchor="w").pack(anchor="w", padx=pad, pady=(16, 0))
        required = field(Field(
            "WHISPER_API_KEY_REQUIRED", "Require the API key", "toggle",
            "On: only devices and programs with the API key. Off: anything that "
            "can reach this PC. The key stays saved either way.",
            default="true"), **compact)
        https = field(Field(
            "SERVER_HTTPS", "Encrypted port (HTTPS), for phones", "toggle",
            "The pairing code carries this PC's fingerprint, so your phone trusts "
            "this PC and nothing pretending to be it. Scan the code again after "
            "changing this.",
            default="false"), **compact)
        plain = field(Field(
            "SERVER_PLAIN_HTTP", "Unencrypted port, for other programs", "toggle",
            "Only answers this PC, your home network and Tailscale. Always on "
            "while the encrypted port is off.", default="true"),
            raw="true" if local_api.plain_enabled(self.values) else "false", **compact)
        for switch in (required, https, plain):
            switch.fold = self._phone_advanced      # see _go_to_field
        HelpText(parent, HelpText.BULLET + "Changes apply after " + SAVE_RESTART,
                 wraplength=560).pack(anchor="w", padx=pad, pady=(12, 0))

        # --- Advanced: port numbers, certificate ---
        parent = ports_f
        self._phone_heading(
            parent, "Ports",
            "This PC's server listens on two ports: encrypted for phones, "
            "unencrypted for other programs.\n"
            "• The encrypted port uses a security certificate this PC makes for "
            "itself; phones need Whisper Unchained 1.3 or later\n"
            "• The unencrypted port refuses requests from the internet; on home "
            "Wi-Fi, other devices on it can read what's sent\n"
            "• Scan the pairing code again after changing the encrypted port number\n"
            "• Changes apply after " + SAVE_RESTART, font=F_BUTTON)
        adv = field(Field("WHISPER_HTTPS_PORT", "Encrypted port number", "port",
                          "Change it only if another program already uses it.",
                          default="5001"))
        adv.fold = self._phone_advanced
        adv = field(Field("WHISPER_API_PORT", "Unencrypted port number", "port",
                          "5000 is the usual choice.\n"
                          "• Change it only if another program already uses it",
                          default="5000"))
        adv.fold = self._phone_advanced
        fp = local_api.fingerprint()
        if fp:
            pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=28, pady=(14, 0))
            left = tk.Frame(row, bg=BG)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(left, text="Certificate fingerprint", font=F_LABEL, bg=BG,
                     fg=TEXT, anchor="w").pack(anchor="w")
            tk.Label(left, text="%s … %s" % (pretty[:8], pretty[-8:]), font=F_HELP,
                     bg=BG, fg=MUTED, anchor="w").pack(anchor="w", pady=(3, 0))
            tk.Label(left, text="For a program that uses the encrypted port. Another PC "
                                "running Whisper Unchained gets it from the connection "
                                "code.", font=F_HELP, bg=BG,
                     fg=MUTED, anchor="w", justify="left", wraplength=360).pack(anchor="w")
            Button(row, "Copy", lambda: self._copy(pretty, "Certificate fingerprint copied.")
                   ).pack(side="right", padx=(20, 0))
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))

        # --- Another PC ---
        parent = pc_f
        HelpText(parent, PHONE_PC_HELP, wraplength=650).pack(
            anchor="w", padx=pad, pady=(3, 0))
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=pad, pady=(10, 0))
        Button(row, "Copy connection code", self.copy_connection_code,
               kind="primary").pack(side="left")
        HelpText(parent, PHONE_PC_STEPS, wraplength=650).pack(
            anchor="w", padx=pad, pady=(8, 0))

        # --- Advanced: any other program ---
        parent = addr_f
        HelpText(parent, "For any program that can use an OpenAI-style speech address.\n"
                         "• Answer three questions to get the address",
                 wraplength=600).pack(anchor="w", padx=pad, pady=(3, 0))
        self._finder = {}
        for key, label, options in self._finder_questions():
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=pad, pady=(10, 0))
            tk.Label(row, text=label, font=F_LABEL, bg=BG, fg=TEXT, width=24,
                     anchor="w").pack(side="left")
            var = tk.StringVar(row, value=options[0][1])
            box = ttk.Combobox(row, textvariable=var, state="readonly", width=52,
                               font=F_BODY, values=[o[1] for o in options])
            box.pack(side="left")
            var.trace_add("write", lambda *_: self._refresh_addresses())
            self._finder[key] = (var, box, options)
        # The result: its own raised box, so it reads as the thing to copy.
        self._addr_box = tk.Frame(parent, bg=RAISED, highlightthickness=1,
                                  highlightbackground=BORDER, highlightcolor=BORDER)
        self._addr_box.pack(fill="x", padx=pad, pady=(14, 0))
        self._addr_inner = tk.Frame(self._addr_box, bg=RAISED)
        self._addr_inner.pack(fill="x")
        for key in ("SERVER_HTTPS", "SERVER_PLAIN_HTTP", "WHISPER_HTTPS_PORT",
                    "WHISPER_API_PORT"):
            self.fields[key].var.trace_add("write", lambda *_: (self._refresh_addresses(),
                                                                self._refresh_qr()))

        # --- Advanced: the API key itself ---
        parent = key_f
        key_field = Field("WHISPER_API_KEY", "API key", "secret")
        key_field.fold = self._phone_advanced
        self.fields[key_field.key] = key_field
        self._phone_heading(
            parent, "API key",
            "Like a password for this server.\n"
            "• Phones get it by scanning the pairing code, other PCs from the "
            "connection code; other programs need it pasted in\n"
            "• Changes apply after " + SAVE_RESTART + (
                "\n• Saved encrypted: a copy, backup or screenshot of the "
                "settings file won't give it away (programs on this PC can "
                "still read it)" if secrets_store.available() else ""), font=F_BUTTON)
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
        Button(row, "Copy", self._copy_api_key).pack(side="left", padx=(6, 0))
        Button(row, "Remove", lambda: key_field.var.set("")).pack(
            side="left", padx=(6, 0))
        required.var.trace_add("write", lambda *_: (self._refresh_qr(),
                                                    self._refresh_addresses()))
        self._refresh_addresses()

        # --- Your phone: pairing QR ---
        parent = pair_f
        HelpText(parent, "Scan this code with Whisper Unchained on your phone.\n"
                         "• Fills in everything the phone needs\n"
                         "• It contains your key, so it stays hidden until you ask",
                 wraplength=560).pack(anchor="w", padx=pad, pady=(3, 0))
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=pad, pady=(10, 0))
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
        # Which kind of address the code will carry, in words: a home address
        # works only on the home Wi-Fi, which is easy to miss until you leave.
        host_note = tk.Label(parent, text="", font=F_HELP, bg=BG, fg=MUTED, anchor="w",
                             justify="left", wraplength=560)
        host_note.pack(anchor="w", padx=pad, pady=(4, 0))

        def describe(*_):
            from pairing import is_tailscale_ip
            h = self.phone_host.get().strip()
            if is_tailscale_ip(h):
                host_note.config(text="Tailscale address: works at home and anywhere else "
                                      "(Tailscale on in the phone).", fg=MUTED)
            elif any(is_tailscale_ip(x) for x in hosts):
                host_note.config(text="Home network address: the phone only reaches this PC "
                                      "on the same Wi-Fi. Pick the 100.x address to use it "
                                      "anywhere.", fg=WARN)
            else:
                host_note.config(text="Home network address: works on the same Wi-Fi only. "
                                      "Install Tailscale to dictate from anywhere.", fg=MUTED)
        self.phone_host.trace_add("write", describe)
        describe()
        self._refresh_addresses()

        btns = tk.Frame(parent, bg=BG)
        btns.pack(fill="x", padx=pad, pady=(10, 0))
        self._qr_mode = None  # None | "pair" | "download"
        self._qr_pair_btn = Button(btns, "Show pairing QR",
                                   lambda: self._set_qr_mode("pair"), kind="primary")
        self._qr_pair_btn.pack(side="left")
        self._qr_dl_btn = Button(btns, "Show download QR",
                                 lambda: self._set_qr_mode("download"))
        self._qr_dl_btn.pack(side="left", padx=(10, 0))

        self.qr_canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0,
                                   width=0, height=0)
        self.qr_canvas.pack(anchor="w", padx=pad, pady=(12, 0))
        self.qr_note = HelpText(parent, "", wraplength=560)
        self.qr_note.pack(anchor="w", padx=pad, pady=(4, 0))

        # --- Your phone: downloads ---
        parent = app_f
        self._phone_heading(parent, "Get the app", padx=pad, font=F_BUTTON, top=6)
        links = [
            ("Google Play", PLAY_STORE_URL, "coming soon"),
            ("Android APK (GitHub releases)", APP_RELEASES_URL,
             "includes the extras the Play version leaves out"),
            ("App source code", APP_REPO_URL, ""),
            ("Whisper Unchained Server source code", SERVER_REPO_URL, ""),
        ]
        for label, url, note in links:
            line = tk.Frame(parent, bg=BG)
            line.pack(fill="x", padx=pad, pady=(8, 0))
            link = tk.Label(line, text=label, font=F_LABEL, bg=BG,
                            fg=ACCENT if url else MUTED,
                            cursor="hand2" if url else "arrow")
            link.pack(side="left")
            if url:
                link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
            if note:
                tk.Label(line, text="  " + note, font=F_HELP, bg=BG,
                         fg=MUTED).pack(side="left")

    def _copy_api_key(self):
        self._copy(self.fields["WHISPER_API_KEY"].var.get().strip(), "API key copied.",
                   empty="No API key to copy.")

    def pairing_data(self):
        """(payload, other addresses): what the pairing QR and the connection
        code both carry, from the page as it stands (unsaved edits included).
        One function, so the two can never disagree."""
        import pairing as _pairing
        key = self.fields["WHISPER_API_KEY"].var.get().strip()
        https_live = bool(self.fields["SERVER_HTTPS"].var.get())
        chosen = self.phone_host.get().strip() or "127.0.0.1"
        others = _pairing.other_hosts(chosen)
        data = pairing_payload(
            chosen, key,
            self.values.get("WHISPER_MODEL_SIZE", ""), socket.gethostname(),
            port=self._live_port(),
            cert_sha256=local_api.fingerprint() if https_live else "",
            alt_hosts=others)
        return data, others

    def copy_connection_code(self):
        """The pairing payload as one line, for a PC with no camera. Never
        shown on screen: it holds the API key."""
        import pairing as _pairing
        data, _others = self.pairing_data()
        self._copy(_pairing.connection_code(data), "Connection code copied.")

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

    # The "Any other program" finder: three questions -> one address.
    FINDER_WHERE = (
        ("this_pc", "This PC"),
        ("home", "Another device on my home network"),
        ("tailscale", "A device on my Tailscale (at home or away)"),
    )
    FINDER_WHAT = (
        # key, label, path after the port, how to use it
        ("plain", "Exactly what was said (transcription)", "/v1",
         "Paste it as the program's OpenAI base URL (\"OpenAI-compatible\"); it "
         "adds /audio/transcriptions itself\n"
         "• Any model name works; nothing is corrected, cleaned up or logged"),
        ("dictate", "Dictation: my name corrections, skills and cleanup", "/dictate/v1",
         "Paste it as the program's OpenAI base URL\n"
         "• Text comes back like your dictation: name corrections, requests "
         "to the AI, skills, and cleanup if it's on in AI Editing\n"
         "• Logged like your dictations"),
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
        return (("where", "The other program runs on", list(self.FINDER_WHERE)),
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
            return "", ["The encrypted port is off. Its switch is just below."]
        if conn == "http" and not plain_on:
            return "", ["The unencrypted port is off. Its switch is just below."]
        url = ("https://%s:%d%s" % (host, https_port, path) if conn == "https"
               else "http://%s:%d%s" % (host, plain_port, path))
        if conn == "https":
            notes.append("Encrypted. The program must trust this PC's own certificate: "
                         "give it api\\certs\\server.pem, or the fingerprint (under "
                         "Advanced, below).")
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
            notes.append("API key: required. Give the program the key above (Copy). "
                         "Programs that ask for an \"API key\" send it for you; others "
                         "need the header  Authorization: Bearer <key>.")
        return url, notes

    def _refresh_addresses(self):
        if not hasattr(self, "_addr_box") or not hasattr(self, "_finder"):
            return
        # The outline's rounded corners are children of _addr_box: only what's
        # inside it is rebuilt.
        box = self._addr_inner
        for w in box.winfo_children():
            w.destroy()
        url, notes = self.finder_address()
        tk.Label(box, text="Copy these into the other program", font=F_BUTTON, bg=RAISED,
                 fg=TEXT, anchor="w").pack(anchor="w", padx=14, pady=(12, 0))

        def line(name, value, live, command):
            row = tk.Frame(box, bg=RAISED)
            row.pack(fill="x", padx=14, pady=(8, 0))
            # Ghost buttons are the box's own colour: these get an outline.
            copy = Button(row, "Copy", command)
            copy.pack(side="right")
            copy.config(highlightbackground=BORDER)
            copy.set_enabled(live)
            tk.Label(row, text=name, font=F_HELP, bg=RAISED, fg=MUTED, width=8,
                     anchor="w").pack(side="left")
            tk.Label(row, text=value, font=F_BUTTON, bg=RAISED,
                     fg=TEXT if live else MUTED, anchor="w").pack(side="left", fill="x")
        line("Address", url or "No address for this choice", bool(url),
             lambda: self._copy(url, "Copied %s" % url))
        key = self.fields["WHISPER_API_KEY"].var.get().strip()
        needs_key = bool(key) and bool(self.fields["WHISPER_API_KEY_REQUIRED"].var.get())
        # Never shown in full here: Advanced (below) has the key and its eye.
        line("API key", "•" * 16 if needs_key else "Not needed right now",
             needs_key, self._copy_api_key)
        for n in notes:
            HelpText(box, HelpText.BULLET + n, bg=RAISED, wraplength=600).pack(
                anchor="w", padx=14, pady=(6, 0))
        https_on, https_port, plain_on, plain_port = self._live_ports()
        saved = (local_api.https_enabled(self.values), int(local_api.https_port(self.values)),
                 local_api.plain_enabled(self.values), int(local_api.port(self.values)))
        if (https_on, https_port, plain_on, plain_port) != saved:
            tk.Label(box, text="The ports changed: this address works after %s."
                               % SAVE_RESTART, font=F_HELP, bg=RAISED, fg=WARN, anchor="w",
                     justify="left", wraplength=600).pack(anchor="w", padx=14, pady=(6, 0))
        tk.Frame(box, bg=RAISED, height=12).pack()
        self._round_soon()
        if hasattr(self, "_pair_port"):
            self._pair_port.config(text="%d  (%s)" % (
                self._live_port(), "encrypted" if https_on else "not encrypted"))

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
            import pairing as _pairing
            data, others = self.pairing_data()
            note = "Scan in Whisper Unchained: Settings > Scan pairing code."
            also = _pairing.describe_other_hosts(others)
            if also:
                note += "\n• " + also
            if https_live and (not local_api.fingerprint()
                               or not local_api.https_enabled(self.values)):
                note += ("\n• The encrypted connection is on but not running yet: press "
                         "%s, then show this code again" % SAVE_RESTART)
            if self._live_port() != int(local_api.pairing_port(self.values)):
                note += ("\n• This code has your NEW port: press %s before scanning"
                         % SAVE_RESTART)
            if key != self.values.get("WHISPER_API_KEY", ""):
                note += ("\n• This code has your NEW key: press %s before the phone "
                         "can use it" % SAVE_RESTART)
            elif not key:
                note += "\n• No key is set, so the code holds only the address"
            elif not self.fields["WHISPER_API_KEY_REQUIRED"].var.get():
                note += ("\n• The key isn't required right now; the phone keeps "
                         "it for when you switch it back on")
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
                               anchor="w", justify="left")

        # Windows order: the main action first, Close last (right-most).
        Button(inner, "Close", self.on_close, kind="ghost").pack(side="right")
        self.btn_save = Button(inner, "Save", self.save, kind="ghost")
        self.btn_save.pack(side="right", padx=(0, 8))
        self.btn_restart = Button(inner, SAVE_RESTART, self.save_and_restart,
                                  kind="primary")
        self.btn_restart.pack(side="right", padx=(0, 8))
        # What each of the two does, in a sentence, when the pointer rests on it.
        Tip(self.btn_save, SAVE_TIP, above=True)
        Tip(self.btn_restart, SAVE_RESTART_TIP, above=True)
        # Packed after the buttons, so a long message wraps onto a second line
        # in the room that's left instead of squeezing them.
        self.status.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.status.bind("<Configure>", lambda e: self.status.config(
            wraplength=max(200, e.width - 4)))
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
        if name == "Transcription":
            self._show_tx_main()
        if name == "Triggers":
            self._show_skills_view()
            self._triggers_page_shown()

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
        # Never add an empty "other addresses" line to a .env that has none.
        from pairing import ALT_URLS_KEY
        if not updates.get(ALT_URLS_KEY) and ALT_URLS_KEY not in self.values:
            updates.pop(ALT_URLS_KEY, None)
        # The same for the second speech engine: a .env that never chose it
        # gets no lines about it.
        if updates.get("WHISPER_ENGINE") == ENGINE_FW:
            for key in ("WHISPER_ENGINE", "WHISPER_CPP_MODEL", "WHISPER_CPP_DEVICE"):
                if key not in self.values:
                    updates.pop(key, None)
        if updates.get("SERVER_HTTPS") == "true" and updates.get("SERVER_PLAIN_HTTP") == "true" \
                and updates.get("WHISPER_API_PORT") == updates.get("WHISPER_HTTPS_PORT"):
            return None, ("Devices & Apps: the encrypted and unencrypted ports need two "
                          "different numbers")
        if "HOTKEY" in updates:
            try:
                if sys.platform != "win32":
                    parse = linux_settings.parse_hotkey
                else:
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
        if not err:
            err = self._engine_save_problem(updates)
        words_dirty = getattr(self, "_words_dirty", False)
        # The Triggers page may hold another PC's lists: those are saved there,
        # and only when they were edited. This PC's word file then keeps its own.
        remote_trig = getattr(self, "_trig_origin", "local") == "remote"
        remote_dirty = (remote_trig and self._remote_trig_dirty
                        and self._remote_state == "loaded")
        trig = custom = None
        if not err and words_dirty:
            vocab, corr, err = self.collect_words()
        if not err and (remote_dirty or (words_dirty and not remote_trig)):
            trig, err = self.collect_triggers()
        if not err and (remote_dirty or (words_dirty and not remote_trig)):
            custom, err = self.collect_custom_triggers()
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
            self.status.config(text="Could not save your settings: %s" % e, fg=DANGER)
            return False
        if words_dirty:
            try:
                words_config.save(vocab, corr, loaded_vocabulary=self._words_cfg.get("vocabulary", []),
                                  loaded_corrections=self._words_cfg.get("name_corrections", {}),
                                  triggers=None if remote_trig else trig,
                                  custom=None if remote_trig else custom)
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
        if remote_dirty:
            # Done here, not on a worker thread: Save answers yes or no, and
            # Save and restart waits on that answer.
            self.status.config(text=REMOTE_TRIGGERS_SAVING, fg=MUTED)
            self.root.update_idletasks()
            try:
                transcribe_client.save_server_triggers(
                    self._remote_env(), trig, custom,
                    loaded_custom=self._remote_loaded_custom)
            except Exception as e:
                # Refused or unreachable: the edits stay on the page, unsaved.
                self.values = read_env()
                self.status.config(
                    text=skill_wording(str(e)) if isinstance(e, transcribe_client.TranscribeError)
                    else "Couldn't save on the other PC (%s)." % e.__class__.__name__,
                    fg=DANGER)
                return False
            self._remote_loaded_custom = [dict(t) for t in custom]
            self._remote_trig_dirty = False
            self._load_remote_triggers(quiet=True, saved=(trig, custom))
        self.values = read_env()
        self.dirty = False
        self.set_saved_state(True)
        hotkey_changed = any(before.get(k, "") != self.values.get(k, "")
                             for k in ("HOTKEY", "HOTKEY_MODE"))
        # Only a PC that does the work itself has anything to restart.
        local = transcribe_client.source(self.values) == "local"
        msg = SAVED_RESTART if local else SAVED_PLAIN
        if hotkey_changed:
            msg = "Saved. Use Restart Listener in the tray to apply the new hotkey."
        warning = getattr(self, "_hotkey_warning", None)
        selected = model_catalog.BY_NAME.get(self.values.get("WHISPER_MODEL_SIZE", ""))
        if self.values.get("WHISPER_ENGINE") == ENGINE_CPP:
            selected = None     # that model belongs to the other engine
        if selected and not model_catalog.is_downloaded(
                selected, model_catalog.models_dir(self.values)):
            warning = ("%s isn't on this PC yet: the next restart downloads it first, "
                       "and dictation is off for a few minutes while it does."
                       % selected.name)
        if warning:
            self.status.config(text="Saved, but note: " + warning, fg=WARN)
        else:
            self.status.config(text=msg, fg=ACCENT)
        return True

    def save_and_restart(self):
        if not self.save():
            return
        if sys.platform != "win32":
            ok, msg = linux_settings.restart_server()
            self.status.config(text=msg, fg=ACCENT if ok else WARN)
            return
        # Same elevated path the tray menu uses: the service ACL grants
        # Interactive Users read-only rights, so stopping it needs a UAC prompt.
        # The port lives in the service's own command line (NSSM AppParameters),
        # not just .env, so rewrite it in the same elevated step. Idempotent.
        import service_name
        svc = service_name.installed(refresh=True)
        command = "Restart-Service -Name %s -Force" % svc
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
                       % (nssm, svc, plain, https, rule, rule,
                          ",".join(open_ports), command))
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe",
                "-WindowStyle Hidden -Command \"%s\"" % command, None, 0)
            self.status.config(
                text=RESTART_ASKING, fg=ACCENT)
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
    root.withdraw()            # built hidden, shown once ready: no white flash
    root.configure(bg=BG)
    app = SettingsApp(root)

    # Deep-link straight to a section: `settings_gui.py --section "Voice AI"`.
    # Also what the screenshot tests drive, so every panel can be checked
    # without a human clicking through them.
    if "--section" in sys.argv:
        name = sys.argv[sys.argv.index("--section") + 1]
        match = next((n for n, _ in SCHEMA if name.lower() in
                      (n.lower(), SECTION_LABELS.get(n, n).lower())), None)
        if match:
            app.show_section(match)

    # Auto-close hook, used only by the smoke test so the GUI can be validated
    # without a human clicking anything.
    show_when_ready(root)
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
