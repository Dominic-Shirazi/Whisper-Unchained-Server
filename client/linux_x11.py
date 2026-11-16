"""X11 plumbing for the Linux listener: hear every key (XRecord), find the
window in front and its program, bring a window back, press keys (XTEST).

Only python-xlib; no xdotool. Works on an X11 session. On Wayland none of
this is allowed to programs: linux_listener.py falls back to a desktop
keyboard shortcut and wtype/ydotool there.
"""
import os
import threading
import time

from Xlib import X, XK, display, error
from Xlib.ext import record, xtest
from Xlib.protocol import event, rq

# Hotkey names (linux_hotkeys.py) -> X keysym names. Single characters and
# f1..f24 are handled in keysym_for().
KEYSYMS = {
    "space": "space", "enter": "Return", "tab": "Tab", "esc": "Escape",
    "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "page up": "Page_Up", "page down": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "pause": "Pause", "scroll lock": "Scroll_Lock", "caps lock": "Caps_Lock",
    "print screen": "Print", "menu": "Menu", "num lock": "Num_Lock",
    "left ctrl": "Control_L", "right ctrl": "Control_R",
    "left shift": "Shift_L", "right shift": "Shift_R",
    "left alt": "Alt_L", "right alt": "Alt_R",
    "left win": "Super_L", "right win": "Super_R",
    "ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "win": "Super_L",
}
# The same key under another keysym (AltGr, Meta) -> the name we use.
_EXTRA_NAMES = {"ISO_Level3_Shift": "right alt", "Meta_L": "left alt", "Meta_R": "right alt"}


def keysym_for(name):
    """X keysym number for a hotkey name, or 0."""
    if name in KEYSYMS:
        return XK.string_to_keysym(KEYSYMS[name])
    if len(name) > 1 and name[0] == "f" and name[1:].isdigit():
        return XK.string_to_keysym("F" + name[1:])
    if len(name) == 1 and " " < name <= "~":
        return ord(name)          # Latin-1 keysyms are their character codes
    return 0


_NAMES = None


def name_of(keysym):
    """Hotkey name for a keysym ('f9', 'left ctrl', '`', 'a'), or None."""
    global _NAMES
    if _NAMES is None:
        names = {}
        for name, sym in KEYSYMS.items():
            if name not in ("ctrl", "shift", "alt", "win"):
                names[XK.string_to_keysym(sym)] = name
        for n in range(1, 25):
            names[XK.string_to_keysym("F%d" % n)] = "f%d" % n
        for sym, name in _EXTRA_NAMES.items():
            names[XK.string_to_keysym(sym)] = name
        _NAMES = names
    if keysym in _NAMES:
        return _NAMES[keysym]
    if 0x20 < keysym <= 0x7e:
        return chr(keysym).lower()
    return None


class KeyWatcher(threading.Thread):
    """Calls on_press(name) / on_release(name) for every key on the keyboard,
    whatever window has focus. Keys still reach their app (nothing is
    swallowed), as on Windows."""

    def __init__(self, on_press, on_release):
        super().__init__(name="x11-keys", daemon=True)
        self.on_press, self.on_release = on_press, on_release
        self._local = display.Display()      # keycode lookups
        self._rec = display.Display()        # blocked in the record loop
        if not self._rec.has_extension("RECORD"):
            raise RuntimeError("This X server has no RECORD extension.")
        self.failed = None

    def run(self):
        try:
            ctx = self._rec.record_create_context(0, [record.AllClients], [{
                "core_requests": (0, 0), "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.KeyPress, X.KeyRelease),
                "errors": (0, 0), "client_started": False, "client_died": False}])
            self._rec.record_enable_context(ctx, self._events)
        except Exception as e:           # X went away: the caller exits
            self.failed = e

    def _events(self, reply):
        if reply.category != record.FromServer or reply.client_swapped:
            return
        data = reply.data
        while data and len(data) >= 32:
            ev, data = rq.EventField(None).parse_binary_value(data, self._rec.display, None, None)
            if ev.type not in (X.KeyPress, X.KeyRelease):
                continue
            name = name_of(self._local.keycode_to_keysym(ev.detail, 0))
            if name:
                (self.on_press if ev.type == X.KeyPress else self.on_release)(name)


class Desktop:
    """The window in front, its program, and typing into it. One connection,
    one caller at a time."""

    def __init__(self):
        self.d = display.Display()
        self.root = self.d.screen().root
        self._lock = threading.RLock()
        self._atom = {}

    def _a(self, name):
        if name not in self._atom:
            self._atom[name] = self.d.intern_atom(name)
        return self._atom[name]

    def _prop(self, win, name):
        try:
            p = win.get_full_property(self._a(name), X.AnyPropertyType)
            return p.value if p is not None else None
        except (error.XError, error.ConnectionClosedError):
            return None

    def active_window(self):
        """Id of the window with keyboard focus (0: none, e.g. the desktop)."""
        with self._lock:
            v = self._prop(self.root, "_NET_ACTIVE_WINDOW")
            return int(v[0]) if v is not None and len(v) else 0

    def program(self, wid):
        """(program file name, window class) of a window: ('firefox',
        'firefox'), ('gnome-terminal-server', 'gnome-terminal-server')."""
        if not wid:
            return "", ""
        with self._lock:
            win = self.d.create_resource_object("window", wid)
            exe = cls = ""
            pid = self._prop(win, "_NET_WM_PID")
            if pid is not None and len(pid):
                try:
                    exe = os.path.basename(os.readlink("/proc/%d/exe" % int(pid[0])))
                except OSError:
                    pass
            try:
                c = win.get_wm_class()
                cls = (c[1] or c[0] or "").lower() if c else ""
            except (error.XError, error.ConnectionClosedError):
                pass
            return exe, cls

    def title(self, wid):
        with self._lock:
            v = self._prop(self.d.create_resource_object("window", wid), "_NET_WM_NAME")
            try:
                return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
            except Exception:
                return ""

    def activate(self, wid, timeout=1.5):
        """Bring a window back to the front; True once it has the focus."""
        with self._lock:
            win = self.d.create_resource_object("window", wid)
            ev = event.ClientMessage(window=win, client_type=self._a("_NET_ACTIVE_WINDOW"),
                                     data=(32, [2, X.CurrentTime, 0, 0, 0]))
            mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
            try:
                self.root.send_event(ev, event_mask=mask)
                self.d.sync()
            except (error.XError, error.ConnectionClosedError):
                return False
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.active_window() == wid:
                return True
            time.sleep(0.03)
        return False

    def _keycode(self, name):
        sym = keysym_for(name)
        code = self.d.keysym_to_keycode(sym) if sym else 0
        if not code:
            raise ValueError("no key for %r on this keyboard" % name)
        return code

    def send(self, names, gap=0.04, times=1):
        """Press a combination like a person: ['ctrl', 'v'] is Ctrl down, V,
        Ctrl up, with a beat between (remote desktops drop faster ones)."""
        codes = [self._keycode(n) for n in names]
        with self._lock:
            for _ in range(times):
                for c in codes[:-1]:
                    xtest.fake_input(self.d, X.KeyPress, c)
                    self.d.sync()
                    time.sleep(gap)
                xtest.fake_input(self.d, X.KeyPress, codes[-1])
                xtest.fake_input(self.d, X.KeyRelease, codes[-1])
                self.d.sync()
                time.sleep(gap)
                for c in reversed(codes[:-1]):
                    xtest.fake_input(self.d, X.KeyRelease, c)
                self.d.sync()

    def release_modifiers(self):
        """Let go of any modifier still held from the hotkey (Shift+F9), so
        the paste isn't Ctrl+Shift+V by accident."""
        with self._lock:
            for name in ("left shift", "right shift", "left ctrl", "right ctrl",
                         "left alt", "right alt", "left win", "right win"):
                try:
                    xtest.fake_input(self.d, X.KeyRelease, self._keycode(name))
                except ValueError:
                    pass
            self.d.sync()
