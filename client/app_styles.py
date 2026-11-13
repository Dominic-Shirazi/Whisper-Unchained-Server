"""Cleanup styles for specific apps: dictate into Slack casually, into Outlook
professionally, into a terminal exactly as said.

Rules live in client/app_styles.json (this PC only, git-ignored):

    [{"app": "slack.exe", "cleanup": "on", "tone": "casual", "emoji": "on"},
     {"app": "WindowsTerminal.exe", "cleanup": "off"}]

Every key but "app" is optional; a missing or empty one means "same as the
AI Editing page". The listener looks up the app it is about to paste into and
sends the matching options with that one dictation (the server's per-request
cleanup options, contract 01), so nothing on the server changes.
"""
import ctypes
import json
import os
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "app_styles.json")

# The style options the server accepts per request, with their values
# (api/cleanup_prompt.json) and the words Settings shows for them.
OPTIONS = {
    "cleanup": [("on", "Clean up"), ("off", "Exactly as said")],
    "punctuation": [("standard", "Standard"), ("light", "Light"), ("minimal", "Minimal")],
    "rewrite": [("keep", "Keep my words"), ("fix", "Fix grammar"), ("polish", "Polish")],
    "tone": [("neutral", "My own tone"), ("professional", "Professional"),
             ("casual", "Casual"), ("friendly", "Friendly")],
    "shorten": [("off", "Full length"), ("light", "Shorter"), ("strong", "Shortest")],
    "emoji": [("off", "No emoji"), ("on", "Allow an emoji")],
}
KEYS = tuple(OPTIONS)
SAME = "Same as default"   # Settings' label for "no override"


def normalize_app(name):
    """'C:\\...\\Slack.exe' or 'Slack' -> 'slack.exe' (how rules are matched)."""
    name = os.path.basename((name or "").strip().strip('"')).lower()
    if name and not name.endswith(".exe"):
        name += ".exe"
    return name


def clean(rules):
    """Valid rules only: a known app name, known values, one rule per app."""
    out, seen = [], set()
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        app = normalize_app(r.get("app"))
        if not app or app in seen or app == ".exe":
            continue
        rule = {"app": app}
        for k in KEYS:
            v = str(r.get(k) or "").strip().lower()
            if v in dict(OPTIONS[k]):
                rule[k] = v
        seen.add(app)
        out.append(rule)
    return out


def load(path=PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return clean(json.load(f))
    except (OSError, ValueError):
        return []


def save(rules, path=PATH):
    rules = clean(rules)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)
    os.replace(tmp, path)
    return rules


def rule_for(app, rules):
    app = normalize_app(app)
    return next((r for r in rules if r["app"] == app), None) if app else None


def request_fields(rule):
    """The form fields for one dictation: {'cleanup': 'on', 'cleanup_tone': ...}.
    Empty when there's no rule (the server's own settings apply)."""
    if not rule:
        return {}
    fields = {}
    if rule.get("cleanup"):
        fields["cleanup"] = rule["cleanup"]
    for k in KEYS[1:]:
        if rule.get(k):
            fields["cleanup_" + k] = rule[k]
    return fields


# --- Which app is in front ----------------------------------------------------

def app_of_window(hwnd):
    """The program file name (e.g. 'slack.exe') that owns a window, or ''."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return normalize_app(buf.value)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def foreground_app():
    try:
        return app_of_window(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return ""


def running_apps():
    """Program names with a visible, titled window right now (for the Settings
    picker), sorted, without this app's own Python."""
    found = set()
    try:
        user32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _l):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                name = app_of_window(hwnd)
                if name and name not in ("python.exe", "pythonw.exe", "explorer.exe",
                                         "applicationframehost.exe", "textinputhost.exe"):
                    found.add(name)
            return True
        user32.EnumWindows(proc(cb), 0)
    except Exception:
        pass
    return sorted(found)
