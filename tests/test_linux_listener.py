"""The Linux listener's own logic: hotkey strings, key tracking, which
recorder, which paste keys. No X server, microphone or clipboard needed, so
these run on Windows too (the modules that talk to X are imported lazily)."""
import wave

import pytest

import linux_hotkeys as hk
import linux_listener as ll


# --- hotkey strings ---------------------------------------------------------------

def test_single_key_fires_alone_only():
    c = hk.parse("f9")
    assert c.fires("f9", set())
    assert not c.fires("f9", {"left shift"})       # Shift+F9 is the edit key
    assert not c.fires("f8", set())


def test_combo_needs_exactly_its_modifiers():
    c = hk.parse("ctrl+alt+space")
    assert c.fires("space", {"left ctrl", "right alt"})
    assert not c.fires("space", {"left ctrl"})
    assert not c.fires("space", {"left ctrl", "left alt", "left shift"})
    assert not c.fires("left ctrl", {"left alt", "space"})   # the last key fires it


def test_right_ctrl_alone():
    c = hk.parse("right ctrl")
    assert c.fires("right ctrl", set())
    assert not c.fires("left ctrl", set())
    assert not c.fires("right ctrl", {"left shift"})
    assert c.released_by("right ctrl") and not c.released_by("left ctrl")


def test_sided_modifier_in_a_combo():
    c = hk.parse("right alt+f8")
    assert c.fires("f8", {"right alt"})
    assert not c.fires("f8", {"left alt"})


def test_aliases_and_spacing():
    assert hk.parse("Control + Shift + Space").text == "control+shift+space"
    assert hk.parse("windows+h").fires("h", {"left win"})
    assert hk.parse("scroll lock").key == "scroll lock"
    assert hk.parse("break").key == "pause"


def test_every_windows_preset_parses_and_has_an_x_key():
    xlib = pytest.importorskip("Xlib")  # noqa: F841
    import hotkey_config
    import linux_x11
    for text, _label in hotkey_config.PRESETS:
        c = hk.parse(text)
        name = c.key if c.key not in hk.MODIFIERS else (c.key_side or "left") + " " + c.key
        assert linux_x11.keysym_for(name), text


def test_bad_hotkeys_rejected():
    for bad in ("", "f9+ctrl", "a+b"):
        with pytest.raises(ValueError):
            hk.parse(bad)


def test_edit_hotkey_default_and_overrides():
    assert hk.edit_hotkey({}, "f9") == "shift+f9"
    assert hk.edit_hotkey({}, "ctrl+alt+space") == "shift+ctrl+alt+space"
    assert hk.edit_hotkey({}, "`") is None                  # types a character
    assert hk.edit_hotkey({}, "ctrl+shift+space") is None   # Shift already used
    assert hk.edit_hotkey({"EDIT_HOTKEY": "off"}, "f9") is None
    assert hk.edit_hotkey({"EDIT_HOTKEY": "F8"}, "f9") == "f8"


# --- key tracking -----------------------------------------------------------------

class FakeTimer:
    """threading.Timer that fires only when the test says so."""
    made = []

    def __init__(self, _wait, fn, args=()):
        self.fn, self.args, self.cancelled = fn, args, False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args)


@pytest.fixture
def tracker():
    FakeTimer.made = []
    events = []
    t = hk.KeyTracker(lambda n, held: events.append(("down", n, frozenset(held))),
                      lambda n: events.append(("up", n)), timer=FakeTimer)
    return t, events


def test_auto_repeat_is_one_press(tracker):
    t, events = tracker
    t.press("f9")
    for _ in range(5):                 # X11: a held key is release+press pairs
        t.release("f9")
        t.press("f9")
    for timer in FakeTimer.made:
        timer.fire()
    assert events == [("down", "f9", frozenset())]
    t.release("f9")
    FakeTimer.made[-1].fire()
    assert events[-1] == ("up", "f9")


def test_press_reports_what_else_is_held(tracker):
    t, events = tracker
    t.press("left shift")
    t.press("f9")
    assert events[-1] == ("down", "f9", frozenset({"left shift"}))
    assert t.held() == {"left shift", "f9"}


def test_release_of_a_key_never_pressed_is_ignored(tracker):
    t, events = tracker
    t.release("f9")
    assert events == [] and FakeTimer.made == []


# --- recorder, clipboard, paste keys --------------------------------------------------

def test_recorder_prefers_pipewire_then_pulse_then_alsa():
    have = lambda *names: (lambda n: "/usr/bin/" + n if n in names else None)  # noqa: E731
    assert ll.recorder_command("a.wav", which=have("pw-record", "arecord"))[0] == "pw-record"
    assert ll.recorder_command("a.wav", which=have("parecord", "arecord"))[0] == "parecord"
    assert ll.recorder_command("a.wav", which=have("arecord"))[0] == "arecord"
    assert ll.recorder_command("a.wav", which=have()) is None


def test_recorder_uses_the_chosen_microphone():
    have = lambda *names: (lambda n: n if n in names else None)  # noqa: E731
    assert ll.recorder_command("a.wav", "usb", which=have("pw-record"))[-3:] == ["--target", "usb", "a.wav"]
    assert "--device=usb" in ll.recorder_command("a.wav", "usb", which=have("parecord"))
    assert ll.recorder_command("a.wav", "hw:1", which=have("arecord"))[-3:] == ["-D", "hw:1", "a.wav"]


def test_clipboard_tool_by_session():
    have = lambda *names: (lambda n: n if n in names else None)  # noqa: E731
    assert ll.clipboard_tool("wayland", which=have("wl-copy", "xclip")) == "wl-clipboard"
    assert ll.clipboard_tool("x11", which=have("wl-copy", "xclip")) == "xclip"
    assert ll.clipboard_tool("wayland", which=have("xsel")) == "xsel"
    assert ll.clipboard_tool("x11", which=have()) is None


def test_paste_keys():
    assert ll.paste_keys({}, "firefox", "firefox") == ["ctrl", "v"]
    assert ll.paste_keys({}, "gnome-terminal-server", "gnome-terminal-server") == ["ctrl", "shift", "v"]
    assert ll.paste_keys({}, "python3.12", "xfce4-terminal") == ["ctrl", "shift", "v"]
    assert ll.paste_keys({}, "", "") == ["ctrl", "v"]
    assert ll.paste_keys({"TERMINAL_APPS": "st, urxvt"}, "st", "") == ["ctrl", "shift", "v"]
    assert ll.paste_keys({"PASTE_KEYS": "shift+insert"}, "firefox", "") == ["shift", "insert"]


def test_session_type():
    assert ll.session_type({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}) == "x11"
    assert ll.session_type({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}) == "wayland"
    assert ll.session_type({"WAYLAND_DISPLAY": "wayland-0"}) == "wayland"
    assert ll.session_type({"DISPLAY": ":0"}) == "x11"      # e.g. started by systemd
    assert ll.session_type({}) == ""


def test_wav_seconds(tmp_path):
    p = str(tmp_path / "a.wav")
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 8000)
    assert ll.wav_seconds(p) == pytest.approx(0.5)
    assert ll.wav_seconds(str(tmp_path / "missing.wav")) == 0.0


def test_wav_seconds_with_an_unfinished_header(tmp_path):
    """A recorder killed mid-write leaves the frame count at 0."""
    p = str(tmp_path / "a.wav")
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000)
    with open(p, "r+b") as f:
        f.seek(40)
        f.write(b"\0\0\0\0")
    assert ll.wav_seconds(p) == pytest.approx(1.0, abs=0.01)


def test_pretty_key():
    assert ll.pretty_key("ctrl+alt+space") == "Ctrl+Alt+Space"
    assert ll.pretty_key("`") == "`"


# --- delivering the text ------------------------------------------------------------

class FakeDesk:
    def __init__(self, active, back=True):
        self.active, self.back, self.sent = active, back, []

    def active_window(self):
        return self.active

    def program(self, wid):
        return ("gnome-terminal-server", "") if wid == 7 else ("firefox", "firefox")

    def activate(self, wid):
        if self.back:
            self.active = wid
        return self.back

    def send(self, names, gap=0, times=1):
        self.sent.append((self.active, tuple(names), times))

    def release_modifiers(self):
        self.sent.append("release")


class FakeOverlay:
    def __init__(self):
        self.shown = []

    def show(self, text, dot=False, seconds=0):
        self.shown.append(text)

    def hide(self):
        pass


@pytest.fixture
def listener(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ll, "set_clipboard", lambda text, tool: True)
    monkeypatch.setattr(ll.time, "sleep", lambda s: None)
    monkeypatch.setattr(ll, "_log", lambda msg: None)
    monkeypatch.setattr(ll.shutil, "which", lambda n: None)     # no beeps, no tools

    def make(active, back=True):
        return ll.Listener(FakeOverlay(), FakeDesk(active, back), "x11")
    return make


def test_paste_where_it_started(listener):
    lis = listener(active=5)
    lis.deliver("hi", 0, 0, ll.Target(5, "firefox", "firefox"), {})
    assert lis.desk.sent == [(5, ("ctrl", "v"), 1)]


def test_typing_hotkey_characters_are_removed_first(listener):
    lis = listener(active=5)
    lis.deliver("hi", 2, 1, ll.Target(5, "firefox", "firefox"), {})
    assert lis.desk.sent == [(5, ("backspace",), 2), (5, ("ctrl", "v"), 1)]


def test_clicked_away_goes_back_before_pasting(listener):
    lis = listener(active=9)
    lis.deliver("hi", 2, 1, ll.Target(7, "gnome-terminal-server", ""), {})
    # the stop press's character where you were, the start press's back home
    assert lis.desk.sent == [(9, ("backspace",), 1), (7, ("backspace",), 1),
                             (7, ("ctrl", "shift", "v"), 1)]


def test_cant_get_back_pastes_nowhere(listener):
    lis = listener(active=9, back=False)
    lis.deliver("hi", 0, 0, ll.Target(7, "gnome-terminal-server", ""), {})
    assert lis.desk.sent == []
    assert "clipboard" in lis.overlay.shown[-1]


def test_started_on_the_desktop_pastes_where_you_are(listener):
    lis = listener(active=7)
    lis.deliver("hi", 0, 0, ll.Target(0), {})
    assert lis.desk.sent == [(7, ("ctrl", "shift", "v"), 1)]


def test_held_modifiers_are_let_go_before_the_paste(listener):
    lis = listener(active=5)
    lis.tracker_held_modifiers = lambda: True
    lis.deliver("hi", 0, 0, ll.Target(5, "firefox", "firefox"), {})
    assert lis.desk.sent == ["release", (5, ("ctrl", "v"), 1)]


def test_no_clipboard_tool_says_so(listener, monkeypatch):
    monkeypatch.setattr(ll, "set_clipboard", lambda text, tool: False)
    lis = listener(active=5)
    lis.deliver("hi", 0, 0, ll.Target(5), {})
    assert lis.desk.sent == [] and "xclip" in lis.overlay.shown[-1]


# --- recordings kept while the server is away ---------------------------------------

def test_unreachable_server_keeps_the_recording(listener, monkeypatch, tmp_path):
    import kept_recordings
    folder = tmp_path / "kept"
    monkeypatch.setattr(kept_recordings, "FOLDER", str(folder))
    monkeypatch.setattr(kept_recordings.keep, "__defaults__", (str(folder), None))
    wav = tmp_path / "recording.wav"
    wav.write_bytes(b"RIFF")
    lis = listener(active=5)
    lis.start_retrying = lambda: None
    lis._keep(str(wav), ll.transcribe_client.Unreachable("The server PC is off."))
    assert len(list(folder.glob("*.wav"))) == 1
    assert "kept" in lis.overlay.shown[-1]
