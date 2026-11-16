"""Hotkey strings and key tracking for the Linux listener (linux_listener.py).

.env holds the same HOTKEY strings as on Windows (hotkey_config.py: "f9",
"ctrl+alt+space", "right ctrl", "`"). This module turns one into a Combo and
decides, from the stream of key presses and releases X11 reports, when it
fires. Nothing here touches X: keys are plain names ("f9", "left ctrl", "a"),
so it runs (and is tested) on any OS.

Two things differ from Windows' `keyboard` library:
  * Modifiers must match exactly. Shift+F9 is not F9, which is what lets
    Shift+<hotkey> be the "edit the highlighted text" key (linux_listener.py).
  * X11 reports a held key as release+press pairs. KeyTracker waits a moment
    after a release before believing it, so a held key is one press.
"""
import threading

MODIFIERS = ("ctrl", "alt", "shift", "win")

_ALIASES = {
    "control": "ctrl", "windows": "win", "super": "win", "cmd": "win", "meta": "win",
    "option": "alt", "escape": "esc", "return": "enter", "break": "pause",
    "del": "delete", "ins": "insert", "pgup": "page up", "pgdn": "page down",
    "pageup": "page up", "pagedown": "page down", "scrolllock": "scroll lock",
    "capslock": "caps lock", "printscreen": "print screen", "spacebar": "space",
    "backtick": "`", "grave": "`",
}


def _token(t):
    """One part of a hotkey string -> (name, side). side is "left", "right" or
    None (either side; only modifiers have sides)."""
    t = " ".join(t.strip().lower().split())
    side = None
    for s in ("left", "right"):
        if t.startswith(s + " ") and _ALIASES.get(t[len(s) + 1:], t[len(s) + 1:]) in MODIFIERS:
            side, t = s, t[len(s) + 1:]
    return _ALIASES.get(t, t), side


def kind(name):
    """'left ctrl' -> 'ctrl'; a key that isn't a modifier -> None."""
    base = name.split(" ", 1)[1] if name.startswith(("left ", "right ")) else name
    return base if base in MODIFIERS else None


class Combo:
    """A parsed hotkey: the key that fires it (the last one named) and the
    modifiers that must be down with it, no more and no fewer."""

    def __init__(self, text):
        parts = [p for p in (text or "").split("+") if p.strip()]
        if (text or "").strip().endswith("++") or (text or "").strip() == "+":
            parts.append("+")
        if not parts:
            raise ValueError("empty hotkey")
        self.text = "+".join(p.strip().lower() for p in parts)
        tokens = [_token(p) for p in parts]
        for name, _side in tokens[:-1]:
            if name not in MODIFIERS:
                raise ValueError("'%s' can't be held with another key" % name)
        self.key, self.key_side = tokens[-1]
        self.mods = dict(tokens[:-1])          # kind -> side or None

    def _is_trigger(self, name):
        if self.key in MODIFIERS:
            return kind(name) == self.key and (
                self.key_side is None or name.startswith(self.key_side + " "))
        return name == self.key

    def fires(self, name, held):
        """Was the combo just completed by pressing `name`? `held` is every
        other key that is down."""
        if not self._is_trigger(name):
            return False
        down = {}
        for h in held:
            k = kind(h)
            if k and h != name:
                down.setdefault(k, set()).add(h.split(" ", 1)[0])
        if set(down) != set(self.mods):
            return False
        return all(side is None or side in down[k] for k, side in self.mods.items())

    def released_by(self, name):
        """Does letting go of `name` end a hold-to-talk recording?"""
        return self._is_trigger(name)


def parse(text):
    """Combo for a hotkey string, or raise ValueError (also for a key name X11
    has no key for; see linux_x11.KEYSYMS)."""
    return Combo(text)


def edit_hotkey(env, hotkey):
    """The key that starts "edit the highlighted text" (EDIT_HOTKEY in .env).
    Default Shift+<hotkey>; none for a hotkey that types a character or
    already uses Shift, or when EDIT_HOTKEY=off."""
    v = (env.get("EDIT_HOTKEY") or "").strip().lower()
    if v in ("off", "none", "false", "0"):
        return None
    if v:
        return v
    h = (hotkey or "").strip().lower()
    if len(h) == 1 or "shift" in h:
        return None
    return "shift+" + h


class KeyTracker:
    """Feeds raw press/release events, calls back on real ones.

    on_press(name, held) once per physical press (auto-repeat swallowed);
    on_release(name) once the key has really been let go."""

    RELEASE_WAIT = 0.04   # auto-repeat's release and press arrive together

    def __init__(self, on_press, on_release, timer=threading.Timer):
        self.on_press, self.on_release = on_press, on_release
        self._timer = timer
        self._held = {}        # name -> pending release timer or None
        self._lock = threading.Lock()

    def press(self, name):
        with self._lock:
            if name in self._held:
                pending = self._held[name]
                if pending is not None:
                    pending.cancel()
                    self._held[name] = None
                return                      # still down: auto-repeat
            self._held[name] = None
            held = set(self._held) - {name}
        self.on_press(name, held)

    def release(self, name):
        with self._lock:
            if name not in self._held:
                return
            if self._held[name] is not None:
                self._held[name].cancel()
            t = self._timer(self.RELEASE_WAIT, self._really_released, args=(name,))
            self._held[name] = t
        t.daemon = True
        t.start()

    def held(self):
        with self._lock:
            return set(self._held)

    def _really_released(self, name):
        with self._lock:
            if self._held.get(name) is None:
                return                      # pressed again meanwhile
            del self._held[name]
        self.on_release(name)
