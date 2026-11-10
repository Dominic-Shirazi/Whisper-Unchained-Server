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
import model_catalog
import threading
import tkinter as tk
from tkinter import ttk

# Crisper text on high-DPI displays. Must run before any widget exists, and is
# purely cosmetic, so a failure here is never worth crashing over.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
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
MUTED    = "#868E9C"   # help text, secondary labels
ACCENT   = "#2FB79A"
ACCENT_H = "#3ACFAF"
DANGER   = "#D9694A"
WARN     = "#D9A441"

F_BODY    = ("Segoe UI", 10)
F_TITLE   = ("Segoe UI Semibold", 16)
F_LABEL   = ("Segoe UI", 10)
F_HELP    = ("Segoe UI", 8)
F_BUTTON  = ("Segoe UI Semibold", 10)

WHISPER_SIZES = [
    "tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium",
    "medium.en", "large-v1", "large-v2", "large-v3", "large-v3-turbo",
    "distil-large-v2", "distil-large-v3", "distil-medium.en", "distil-small.en",
]

# keep_alive is the one setting whose raw value is genuinely cryptic (-1 does
# not read as "forever" to anyone), so it gets spelled out. Values are sent to
# Ollama verbatim; -1 must stay a bare number, not a duration string.
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
                 labelled_choices=None, default=""):
        self.key = key
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
        elif self.kind in ("text", "choice", "pick", "hotkey") and not raw:
            return None, "%s: cannot be empty" % self.label
        return raw, None


CUSTOM_LABEL = "Custom..."


SCHEMA = [
    ("Transcription", [
        Field("WHISPER_MODEL_SIZE", "Whisper model", "choice",
              "Bigger models are more accurate. On a GPU even the large ones stay "
              "fast (turbo: ~0.7 s for a 16 s clip); on CPU, stick to small or "
              "smaller. Download from the list below first, so switching is a "
              "quick restart instead of minutes offline.",
              choices=WHISPER_SIZES),
        Field("WHISPER_DEVICE", "Compute device", "choice",
              "cuda uses the GPU. Falls back to CPU if unavailable.",
              choices=["cuda", "cpu", "auto"]),
        Field("WHISPER_MODELS_DIR", "Model folder", "text",
              "Where the service loads models from (relative = inside this "
              "project). Shared with this window so it can show and download models.",
              default="models"),
    ]),
    # The LLM that runs spoken edit commands ("prompt AI, make this formal").
    # Help text on these fields is rewritten live by _refresh_ai_help().
    ("AI Editing (LLM)", [
        Field("EDIT_BACKEND", "Edit backend", "choice",
              "Which kind of server runs spoken edit commands.",
              labelled_choices=EDIT_BACKEND_CHOICES, default="ollama"),
        Field("OLLAMA_EDIT_API_URL", "Ollama endpoint", "otext",
              "Ollama on this PC or your LAN. Use an IP, not a hostname: the API "
              "runs as a LocalSystem service and cannot resolve MagicDNS names."),
        Field("OLLAMA_MODEL", "Ollama edit model", "pick", "x"),
        Field("OLLAMA_EDIT_KEEP_ALIVE", "Keep edit model in VRAM", "pick", "x",
              labelled_choices=KEEP_ALIVE_CHOICES),
        Field("EDIT_LLM_BASE_URL", "Server base URL", "otext",
              "Any OpenAI-compatible server, ending in /v1: llama.cpp or LM Studio "
              "on your LAN, or a cloud service (OpenRouter, OpenAI, Groq...)."),
        Field("EDIT_LLM_MODEL", "Server model", "otext",
              "The model name that server expects."),
        Field("EDIT_LLM_API_KEY", "Server API key", "secret",
              "Needed for cloud services and for servers started with --api-key. "
              "Leave empty for an open local server."),
        Field("EDIT_LLM_NO_THINK", "Skip reasoning", "toggle",
              "For reasoning models: answer directly instead of thinking first "
              "(several seconds faster per edit)."),
    ]),
    ("Mean Mode", [
        Field("OLLAMA_API_URL", "Mean-mode Ollama endpoint", "text",
              "Roast / TikTok / disses always run on Ollama, whatever AI Editing "
              "uses."),
        Field("OLLAMA_ROAST_MODEL", "Mean-mode model", "pick", "x"),
    ]),
    ("Audio Corpus", [
        Field("KEEP_AUDIO", "Keep source clips", "toggle",
              "Archives the original audio so model A/B tests can replay real "
              "dictation instead of synthetic audio."),
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
    ("Service", [
        Field("WHISPER_API_PORT", "API port", "port",
              "5000 is a common choice for local APIs. Change it only if another "
              "program already uses 5000. Save & Restart API applies it (and "
              "updates the service). Re-pair your phone afterwards.",
              default="5000"),
        Field("API_VISIBLE", "Show API console window", "toggle",
              "Off runs the API silently as a background service."),
        Field("RESTART_INTERVAL_HOURS", "Auto-restart interval (hours)", "int",
              "NOT ACTIVE: the scheduled task that reads this was never installed on "
              "this machine, so nothing currently restarts the API on its own."),
        Field("LISTENER_RESTART_INTERVAL_MINS", "Listener idle restart (mins)", "int",
              "Idle minutes before the hotkey listener recycles itself."),
        Field("LISTEN_DELAY_SECONDS", "Listener startup delay (secs)", "int",
              "How long the listener waits for the API on boot."),
    ]),
    ("Hotkey", [
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

# --- Phone pairing ---------------------------------------------------------
# Contract: WhisperRemote_for_Android contracts/01-transcription-api.md §7.

PAIRING_VERSION = 1
APP_REPO_URL = "https://github.com/Dominic-Shirazi/WhisperRemote_for_Android"
APP_RELEASES_URL = APP_REPO_URL + "/releases"
SERVER_REPO_URL = "https://github.com/Dominic-Shirazi/Open_Whisper_Flow"
# No listing yet. Set this once the app is live on Google Play.
PLAY_STORE_URL = ""


def generate_api_key():
    """Installer-format key: wr_sec_ + 32 url-safe random characters."""
    return "wr_sec_" + secrets.token_urlsafe(24)


def pairing_payload(host, api_key, model, name, port=5000):
    """The JSON the phone scans. api_key is OMITTED (not empty) with auth off."""
    payload = {"v": PAIRING_VERSION,
               "base_url": "http://%s:%d/v1" % (host, port),
               "model": model, "name": name}
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
        self._draw()

    def _click(self, _event=None):
        self.var.set(not self.var.get())
        self._animate()
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

        root.title("Faster-Whisper Settings")
        root.configure(bg=BG)
        root.geometry("900x680")
        root.minsize(780, 580)
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
        head.pack(fill="x", padx=28, pady=(24, 14))
        tk.Label(head, text="Settings", font=F_TITLE, bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(head, text=ENV_PATH, font=F_HELP, bg=BG, fg=MUTED).pack(
            anchor="w", pady=(2, 0))

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
            lbl = tk.Label(row, text=name, font=F_LABEL, bg=SURFACE, fg=MUTED,
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
        models = installed_ollama_models(self.values.get("OLLAMA_API_URL", ""))
        for name, fields in SCHEMA:
            frame = tk.Frame(self.inner, bg=BG)
            for f in fields:
                self._build_field(frame, f, models)
                self.fields[f.key] = f
            if name == "Phone":
                self._build_phone_panel(frame)
            if name == "Transcription":
                self._build_model_picker(frame)
            self.frames[name] = frame
        self._setup_ai_help()

    # --- AI Editing / Mean Mode: help that follows the current choices --------

    def _setup_ai_help(self):
        self._ollama_cache = {}
        keys = ("EDIT_BACKEND", "OLLAMA_EDIT_API_URL", "OLLAMA_MODEL",
                "OLLAMA_EDIT_KEEP_ALIVE", "OLLAMA_API_URL", "OLLAMA_ROAST_MODEL",
                "EDIT_LLM_BASE_URL")
        for k in keys:
            f = self.fields.get(k)
            if f and f.var is not None:
                f.var.trace_add("write", lambda *_: self._refresh_ai_help())
        self._refresh_ai_help()

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
        off = "NOT ACTIVE: edits go to the %s. " % (
            "OpenAI-compatible server below" if backend == "openai" else "Ollama endpoint above")

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
        self._set_help("OLLAMA_MODEL", (off + base) if backend == "openai" else base)

        sizes = self._ollama_sizes(url) or {}
        if model in sizes:
            gb = sizes[model] / 1e9
            cost = ("Staying loaded costs about %.1f GB of VRAM for %s (%.1f GB of "
                    "weights plus 1-3 GB working memory), but cuts a cold edit from "
                    "~5 s to under 1 s." % (gb + 2, model, gb))
        else:
            cost = ("Staying loaded costs roughly the model's file size plus 1-3 GB "
                    "of VRAM, but cuts a cold edit from ~5 s to under 1 s.")
        keep = "How long Ollama holds the edit model after a request. " + cost
        self._set_help("OLLAMA_EDIT_KEEP_ALIVE",
                       (off + "That server manages its own memory.")
                       if backend == "openai" else keep)
        ollama_url_help = ("Ollama on this PC or your LAN. Use an IP, not a hostname: "
                           "the API runs as a LocalSystem service and cannot resolve "
                           "MagicDNS names.")
        self._set_help("OLLAMA_EDIT_API_URL",
                       off + ollama_url_help if backend == "openai" else ollama_url_help)

        # OpenAI-compatible fields.
        oa = {
            "EDIT_LLM_BASE_URL": ("Any OpenAI-compatible server, ending in /v1: "
                                  "llama.cpp or LM Studio on your LAN, or a cloud "
                                  "service (OpenRouter, OpenAI, Groq...)."),
            "EDIT_LLM_MODEL": "The model name that server expects.",
            "EDIT_LLM_API_KEY": ("Needed for cloud services and for servers started "
                                 "with --api-key. Leave empty for an open local server."),
            "EDIT_LLM_NO_THINK": ("For reasoning models: answer directly instead of "
                                  "thinking first (several seconds faster per edit)."),
        }
        for k, text in oa.items():
            self._set_help(k, off + text if backend == "ollama" else text)
        if backend == "openai" and not self._value("EDIT_LLM_BASE_URL"):
            self._set_help("EDIT_LLM_BASE_URL",
                           "⚠ Needed: with no URL, edits fall back to Ollama.")

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
            # NOT ACTIVE / dead-config warnings earn the amber; ordinary help does not.
            if field.help.startswith("NOT ACTIVE"):
                helper.config(fg=WARN)

        raw = self.values.get(field.key, "") or field.default

        if field.kind == "toggle":
            var = tk.BooleanVar(value=raw.strip().lower() in ("1", "true", "yes"))
            widget = Toggle(row, var, command=self.mark_dirty)
            widget.pack(side="right", padx=(20, 0))
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
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))

    # --- Phone section -------------------------------------------------------

    # --- Model picker (Transcription section) --------------------------------

    def _model_root(self):
        return model_catalog.models_dir(
            {"WHISPER_MODELS_DIR": self.fields["WHISPER_MODELS_DIR"].var.get()})

    def _build_model_picker(self, parent):
        self._phone_heading(
            parent, "Models",
            "Every Whisper model adds punctuation and capitals on its own. "
            ".en models are English-only and a little more accurate in English. "
            "Download a model, then Use it and Save & Restart API.")
        self._model_rows = {}
        self._downloading = None
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x", padx=28, pady=(6, 0))
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

    def _phone_heading(self, parent, title, help_text=""):
        tk.Label(parent, text=title, font=F_BUTTON, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=28, pady=(18, 0))
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
            parent, "API key (optional)",
            "Leave empty on your own Tailscale network: only your devices can reach "
            "the server, and traffic is already encrypted. Set one if the server is "
            "reachable from a shared network. Takes effect after Save & Restart API.")
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
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(16, 0))

        # Pairing QR.
        self._phone_heading(
            parent, "Pair your phone",
            "Open WhisperRemote on your phone and scan this code to fill in the "
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
        tk.Label(row, text=":%d" % api_port(self.values), font=F_BODY, bg=BG,
                 fg=MUTED).pack(side="left")

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
            data = pairing_payload(
                self.phone_host.get().strip() or "127.0.0.1", key,
                self.values.get("WHISPER_MODEL_SIZE", ""), socket.gethostname(),
                port=api_port(self.values))
            note = "Scan in WhisperRemote: Settings > Scan pairing code."
            if key != self.values.get("WHISPER_API_KEY", ""):
                note += (" This code has your NEW key: Save & Restart API before "
                         "the phone can use it.")
            elif not key:
                note += " No API key set, so the code holds only the address."
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
        if err:
            self.status.config(text=err, fg=DANGER)
            return False
        before = dict(self.values)
        try:
            write_env(updates)
        except Exception as e:
            self.status.config(text="Could not write .env: %s" % e, fg=DANGER)
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
            command = ("& '%s' set %s AppParameters '-m uvicorn api.whisper_api:app "
                       "--host 0.0.0.0 --port %d'; %s"
                       % (nssm, SERVICE_NAME, api_port(self.values), command))
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
