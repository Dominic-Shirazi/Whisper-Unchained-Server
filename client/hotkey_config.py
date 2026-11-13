"""Hotkey settings shared by the listener and the Settings window.

.env keys:
  HOTKEY       the `keyboard` library's hotkey string. Default "`" (backtick).
  HOTKEY_MODE  "toggle" (tap to start, tap to stop) or "hold" (push-to-talk).

A hotkey that is a single printable character (backtick) also TYPES that
character into the focused app, since the listener doesn't suppress it (the
`keyboard` library's suppress mode is unreliable on Windows). The listener
backspaces those away before pasting. Keys that type nothing (F-keys, Pause,
combos with Ctrl/Alt) need no cleanup, so they are the better choice.
"""

DEFAULT_HOTKEY = "`"
MODES = ("toggle", "hold")

# (hotkey string, label shown in Settings)
PRESETS = [
    ("`", "` (backtick)"),
    ("f9", "F9"),
    ("f8", "F8"),
    ("pause", "Pause / Break"),
    ("scroll lock", "Scroll Lock"),
    ("ctrl+alt+space", "Ctrl + Alt + Space"),
    ("ctrl+shift+space", "Ctrl + Shift + Space"),
    ("right ctrl", "Right Ctrl (alone)"),
]

# Combos other software already owns. Allowed, but the Settings window warns.
CONFLICTS = {
    "alt+space": "opens the window menu (and PowerToys Run)",
    "win+h": "is Windows' own voice typing",
    "ctrl+space": "switches input language / triggers autocomplete in many apps",
    "f1": "opens help in most apps",
    "f5": "refreshes browsers",
    "f11": "toggles full screen",
    "f12": "opens browser dev tools",
}


def normalize(hotkey):
    return "+".join(p.strip().lower() for p in (hotkey or "").split("+") if p.strip())


def types_a_character(hotkey):
    """True when pressing the hotkey also types into the focused app."""
    h = (hotkey or "").strip()
    return len(h) == 1 and h.isprintable() and not h.isspace()


def cleanup_backspaces(hotkey, mode):
    """Characters the hotkey left behind by the time we paste.
    toggle: one per press (start + stop) = 2. hold never uses a typing key."""
    if mode == "toggle" and types_a_character(hotkey):
        return 2
    return 0


class PressTally:
    """Counts the hotkey presses that actually happened during one recording,
    so the listener backspaces exactly the characters the hotkey typed.

    A recording can be started or stopped by the hotkey OR by the floating mic
    button (or the 10-minute timeout). Only hotkey presses type a character,
    one per press, and only for a typing key in toggle mode:
      hotkey start + hotkey stop  -> 2 backspaces (backtick)
      button start + hotkey stop  -> 1
      hotkey start + button stop  -> 1
      button start + button stop  -> 0
    """

    def __init__(self, hotkey, mode):
        self.per_press = 1 if (mode == "toggle" and types_a_character(hotkey)) else 0
        self.presses = 0

    def started(self, source):
        self.presses = 1 if source == "hotkey" else 0

    def stopped(self, source):
        """Recording ended; return how many characters to backspace."""
        if source == "hotkey":
            self.presses += 1
        n = self.presses * self.per_press
        self.presses = 0
        return n


def release_key(hotkey):
    """The key whose release ends a hold-to-talk recording (the last one)."""
    return normalize(hotkey).split("+")[-1]


def validate(hotkey, mode, parse=None):
    """Return (error, warning). error blocks saving; warning is advice.
    `parse` is keyboard.parse_hotkey, passed in so tests don't need hooks."""
    h = (hotkey or "").strip()
    if not h:
        return "Hotkey: cannot be empty", None
    if mode not in MODES:
        return "Hotkey mode: must be toggle or hold", None
    if parse is not None:
        try:
            parse(h)
        except Exception:
            return "Hotkey: '%s' isn't a key name the listener understands" % h, None
    if mode == "hold" and types_a_character(h):
        return ("Hold to talk needs a key that types nothing (F9, Pause, "
                "Ctrl+Alt+Space...): holding '%s' would repeat it into your text" % h), None
    why = CONFLICTS.get(normalize(h))
    if why:
        return None, "%s %s." % (h, why)
    return None, None


def from_env(env):
    """(hotkey, mode) from .env values, falling back to safe defaults."""
    hotkey = (env.get("HOTKEY") or "").strip() or DEFAULT_HOTKEY
    mode = (env.get("HOTKEY_MODE") or "toggle").strip().lower()
    if mode not in MODES:
        mode = "toggle"
    if mode == "hold" and types_a_character(hotkey):
        mode = "toggle"  # never auto-repeat a character into the user's text
    return hotkey, mode
