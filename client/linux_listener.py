"""The hotkey listener on Linux: press the hotkey, talk, press it again, and
the text is pasted where you were typing. The Linux counterpart of
background_listener.py (Windows), sharing its settings (.env), its hotkey
strings (hotkey_config.py), per-app styles (app_styles.py) and the path the
audio takes (transcribe_client.py: this PC, another PC, or a cloud service).

    python -m client.linux_listener               run it
    python -m client.linux_listener --check       what it found on this machine
    python -m client.linux_listener --toggle      start/stop the running one
    python -m client.linux_listener --toggle-edit same, editing the highlight

X11 (linux_x11.py): a global hotkey, the app in front (per-app styles, and
Ctrl+Shift+V in terminals), going back to the window you started in.
Wayland: programs can't see keys or windows, so bind a desktop keyboard
shortcut to `--toggle`; pasting needs wtype or ydotool, else the text is left
on the clipboard.

What differs from Windows is logged in docs/PLATFORM_DIFFERENCES.md. The main
one: editing highlighted text has its own key (Shift+<hotkey>, EDIT_HOTKEY in
.env), because X11's "highlighted text" outlives the highlight and the plain
hotkey must never mistake an old one for an instruction to edit.

Never logs dictated text: lengths and program names only.
"""
import math
import os
import queue
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave

from dotenv import dotenv_values, load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(os.path.dirname(HERE), ".env")
load_dotenv(ENV_FILE)
sys.path.insert(0, HERE)
import app_styles  # noqa: E402
import hotkey_config  # noqa: E402
import linux_hotkeys  # noqa: E402
import transcribe_client  # noqa: E402

APP_NAME = "Whisper Unchained"
SLUG = "whisper-unchained"
SAMPLE_RATE = 16000            # what Whisper resamples to anyway
MAX_DURATION_MINS = 10
MIN_RECORDING_DURATION_SEC = float(os.environ.get("MIN_RECORDING_DURATION_SEC", 0.4))
PASTE_KEY_GAP = float(os.environ.get("PASTE_KEY_GAP_SECONDS", 0.04))
ERROR_LOG_PATH = os.path.join(HERE, "listener_errors.log")

# Programs that paste with Ctrl+Shift+V (add more with TERMINAL_APPS in .env).
TERMINALS = {
    "gnome-terminal-server", "gnome-terminal", "ptyxis", "kgx", "konsole", "kitty",
    "alacritty", "wezterm", "wezterm-gui", "foot", "tilix", "terminator",
    "xfce4-terminal", "mate-terminal", "lxterminal", "qterminal", "terminology",
    "ghostty", "cool-retro-term", "guake", "yakuake", "tilda", "blackbox", "rio",
}


def runtime_dir():
    """A private folder for the recording, the beeps and the pid file. In
    memory (XDG_RUNTIME_DIR) where there is one, so audio never hits the disk."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = (os.path.join(base, SLUG) if base
            else os.path.join(tempfile.gettempdir(), "%s-%d" % (SLUG, os.getuid())))
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _env():
    """This process's environment with .env on top (re-read each time, so a
    changed setting applies to the next dictation)."""
    return dict(os.environ, **{k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})


def _on(env, key, default):
    v = (env.get(key) or "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def _log(msg):
    print("[Listener] %s" % msg, flush=True)
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("--- %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def session_type(env=os.environ):
    """'x11', 'wayland' or '' (no desktop at all)."""
    t = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if t == "wayland" or (not t and env.get("WAYLAND_DISPLAY")):
        return "wayland"
    return "x11" if env.get("DISPLAY") else ""


# --- Microphone -----------------------------------------------------------------

def recorder_command(path, mic="", which=shutil.which):
    """The command that records the microphone to `path` (16 kHz mono WAV)
    until interrupted: PipeWire's, PulseAudio's or ALSA's own recorder,
    whichever is installed. `mic` (MIC_DEVICE in .env) names a source other
    than the default one. None: no recorder on this machine."""
    rate = str(SAMPLE_RATE)
    if which("pw-record"):
        return ["pw-record", "--rate", rate, "--channels", "1", "--format", "s16"] + \
               (["--target", mic] if mic else []) + [path]
    if which("parecord"):
        return ["parecord", "--rate=" + rate, "--channels=1", "--format=s16le",
                "--file-format=wav"] + (["--device=" + mic] if mic else []) + [path]
    if which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", rate, "-c", "1"] + \
               (["-D", mic] if mic else []) + [path]
    return None


def wav_seconds(path):
    """Length of a recording. A recorder stopped mid-write can leave a header
    that says 0, so fall back to the file size."""
    try:
        with wave.open(path, "rb") as w:
            n, rate, width, ch = w.getnframes(), w.getframerate(), w.getsampwidth(), w.getnchannels()
        size = os.path.getsize(path) - 44
        if rate and (n <= 0 or n * width * ch > size + 4096):
            n = size // max(1, width * ch)
        return n / float(rate) if rate else 0.0
    except (OSError, wave.Error, EOFError):
        try:
            return max(0, os.path.getsize(path) - 44) / (SAMPLE_RATE * 2.0)
        except OSError:
            return 0.0


class Recorder:
    def __init__(self):
        self.proc = None
        self.path = os.path.join(runtime_dir(), "recording.wav")

    def start(self, mic=""):
        cmd = recorder_command(self.path, mic)
        if cmd is None:
            raise RuntimeError("No recorder found: install pipewire (pw-record), "
                               "pulseaudio-utils (parecord) or alsa-utils (arecord).")
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        time.sleep(0.12)
        if self.proc.poll() is not None:        # no microphone, bad MIC_DEVICE...
            err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            self.proc = None
            raise RuntimeError("The microphone didn't open: %s" % (err.splitlines()[-1] if err else cmd[0]))

    def stop(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.send_signal(signal.SIGINT)        # lets it finish the WAV header
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


# --- Beeps ----------------------------------------------------------------------

class Beeps:
    """The same tones as Windows: rising at start, 1000 Hz on, 600 Hz off, a
    low buzz when the microphone fails. BEEPS=false in .env turns them off."""

    TONES = {"start": [(1000, 60)], "stop": [(600, 100)], "fail": [(300, 300)],
             "hello": [(500, 100), (800, 100)]}

    def __init__(self):
        self.player = next((p for p in ("pw-play", "paplay", "aplay") if shutil.which(p)), None)
        self.files = {}
        if self.player:
            for name, tones in self.TONES.items():
                self.files[name] = self._write(name, tones)

    @staticmethod
    def _write(name, tones, rate=44100, volume=0.25):
        path = os.path.join(runtime_dir(), "beep-%s.wav" % name)
        frames = bytearray()
        for freq, ms in tones:
            n = int(rate * ms / 1000)
            for i in range(n):
                fade = min(1.0, i / 200.0, (n - i) / 200.0)      # no clicks
                frames += struct.pack("<h", int(32767 * volume * fade *
                                                math.sin(2 * math.pi * freq * i / rate)))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        return path

    def play(self, name):
        if not self.player or name not in self.files or not _on(_env(), "BEEPS", True):
            return
        try:
            cmd = [self.player] + (["-q"] if self.player == "aplay" else []) + [self.files[name]]
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass


# --- Clipboard ------------------------------------------------------------------

def _run(cmd, text=None, timeout=3):
    """Run a helper; (ok, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, input=text, text=True, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return p.returncode == 0, p.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return False, ""


def clipboard_tool(session, which=shutil.which):
    """'wl-clipboard', 'xclip', 'xsel' or None."""
    if session == "wayland" and which("wl-copy"):
        return "wl-clipboard"
    return next((t for t in ("xclip", "xsel") if which(t)), None)


def set_clipboard(text, tool):
    """Text onto the clipboard. The helper stays behind to serve it (that's
    how X11 and Wayland clipboards work), so its output goes nowhere."""
    cmd = {"wl-clipboard": ["wl-copy"], "xclip": ["xclip", "-selection", "clipboard"],
           "xsel": ["xsel", "--clipboard", "--input"]}.get(tool)
    if not cmd:
        return False
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        p.communicate(text.encode("utf-8"), timeout=3)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def highlighted_text(tool):
    """The text highlighted right now (the PRIMARY selection), or ''."""
    cmd = {"wl-clipboard": ["wl-paste", "--primary", "--no-newline"],
           "xclip": ["xclip", "-o", "-selection", "primary"],
           "xsel": ["xsel", "--primary", "--output"]}.get(tool)
    ok, out = _run(cmd, timeout=1.5) if cmd else (False, "")
    return out if ok else ""


def paste_keys(env, exe, cls):
    """The keys that paste in the app in front: Ctrl+V, Ctrl+Shift+V in a
    terminal, or whatever PASTE_KEYS in .env says for everything."""
    forced = (env.get("PASTE_KEYS") or "").strip().lower()
    if forced:
        return [k.strip() for k in forced.split("+") if k.strip()]
    extra = {a.strip().lower() for a in (env.get("TERMINAL_APPS") or "").split(",") if a.strip()}
    names = {(exe or "").lower(), (cls or "").lower()} - {""}
    if names & (TERMINALS | extra) or any("terminal" in n for n in names):
        return ["ctrl", "shift", "v"]
    return ["ctrl", "v"]


# --- The card on screen -----------------------------------------------------------

class Overlay:
    """A small card at the bottom of the screen: "Recording" while the
    microphone is open, "Transcribing..." while the server works, and short
    messages. It never takes the keyboard focus (an override-redirect window),
    so the paste still goes to your app. Call from any thread."""

    def __init__(self):
        import tkinter as tk
        import tkinter.font as tkfont
        import theme
        self.tk, self.t = tk, theme
        self.root = tk.Tk(className=SLUG)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        fams = set(tkfont.families(self.root))
        font = next((f for f in ("Noto Sans", "Ubuntu", "Cantarell", "DejaVu Sans") if f in fams),
                    "TkDefaultFont")
        self.root.configure(bg=theme.BORDER)
        card = tk.Frame(self.root, bg=theme.SURFACE)
        card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        row = tk.Frame(card, bg=theme.SURFACE)
        row.pack(padx=18, pady=10)
        self.dot = tk.Label(row, text="●", bg=theme.SURFACE, fg=theme.DANGER, font=(font, 12))
        self.label = tk.Label(row, text="", bg=theme.SURFACE, fg=theme.TEXT, font=(font, 12),
                              justify=tk.CENTER, wraplength=520)
        self.label.pack(side=tk.RIGHT)
        self._q = queue.Queue()
        self._hide_job = None
        self.on_tick = None
        self.root.after(50, self._pump)

    def _pump(self):
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        if self.on_tick:
            self.on_tick()
        self.root.after(50, self._pump)

    def _show(self, text, dot, seconds):
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        self.label.config(text=text)
        if dot:
            self.dot.pack(side=self.tk.LEFT, padx=(0, 8))
        else:
            self.dot.pack_forget()
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = self.root.winfo_screenheight() - h - 96
        self.root.geometry("+%d+%d" % (x, y))
        self.root.deiconify()
        self.root.lift()
        if seconds:
            self._hide_job = self.root.after(int(seconds * 1000), self._hide)

    def _hide(self):
        self._hide_job = None
        self.root.withdraw()

    def show(self, text, dot=False, seconds=0):
        self._q.put(lambda: self._show(text, dot, seconds))

    def hide(self):
        """Take the card away, unless it's showing a timed message."""
        self._q.put(lambda: self._hide_job is None and self._hide())

    def run(self):
        self.root.mainloop()


class NoOverlay:
    """No Tk (python3-tk missing): messages become desktop notifications."""
    on_tick = None

    def show(self, text, dot=False, seconds=0):
        if seconds and shutil.which("notify-send"):
            _run(["notify-send", "-a", APP_NAME, "-t", str(int(seconds * 1000)), APP_NAME, text])

    def hide(self):
        pass

    def run(self):
        while True:
            time.sleep(0.05)
            if self.on_tick:
                self.on_tick()


# --- The listener -------------------------------------------------------------------

class Target:
    """Where one recording is going, noted when it starts."""

    def __init__(self, wid=0, exe="", cls="", selection=""):
        self.wid, self.exe, self.cls, self.selection = wid, exe, cls, selection


def pretty_key(hotkey):
    return "+".join(p.strip().capitalize() if len(p.strip()) > 1 else p.strip()
                    for p in hotkey.split("+"))


class Listener:
    def __init__(self, overlay, desk=None, session="x11"):
        self.overlay, self.desk, self.session = overlay, desk, session
        self.clip = clipboard_tool(session)
        self.recorder = Recorder()
        self.beeps = Beeps()
        self.hotkey, self.mode = hotkey_config.from_env(os.environ)
        self.tally = hotkey_config.PressTally(self.hotkey, self.mode)
        self.recording = False
        self.started_at = 0.0
        self.target = None
        self._lock = threading.Lock()
        self._retrying = False

    # -- start / stop ------------------------------------------------------------
    def toggle(self, source="hotkey", edit=False):
        """Start or stop, from the hotkey, a signal (`--toggle`) or the
        timeout. One at a time; never raises (the key thread must survive)."""
        with self._lock:
            try:
                if self.recording:
                    self._stop(source)
                else:
                    self._start(source, edit)
            except Exception as e:
                _log("start/stop failed:\n" + traceback.format_exc())
                self.recording = False
                self.tally.presses = 0
                self.recorder.stop()
                self.beeps.play("fail")
                self.overlay.show(str(e) if isinstance(e, RuntimeError)
                                  else "Something went wrong (see listener_errors.log).", seconds=4)

    def _look(self, edit):
        wid = self.desk.active_window() if self.desk else 0
        exe, cls = self.desk.program(wid) if self.desk else ("", "")
        return Target(wid, exe, cls, highlighted_text(self.clip) if edit else "")

    def _start(self, source, edit):
        env = _env()
        target = self._look(edit and _on(env, "EDIT_SELECTION", True))
        if edit and not target.selection.strip():
            self.beeps.play("fail")
            self.overlay.show("Highlight some text first, then press %s."
                              % pretty_key(linux_hotkeys.edit_hotkey(env, self.hotkey) or self.hotkey),
                              seconds=4)
            return
        self.tally.started(source)
        self.recorder.start((env.get("MIC_DEVICE") or "").strip())
        self.beeps.play("start")
        self.target, self.recording, self.started_at = target, True, time.time()
        if _on(env, "RECORDING_INDICATOR", True):
            how = "let go" if self.mode == "hold" else "%s" % pretty_key(self.hotkey)
            what = "Say how to change the highlighted text" if target.selection else "Recording"
            self.overlay.show("%s  ·  %s to stop" % (what, how), dot=True)
        threading.Thread(target=self._timeout, args=(self.started_at,), daemon=True).start()

    def _stop(self, source):
        # The stop press's own character (a typing hotkey) lands wherever you
        # are now, which may not be where the recording started.
        stop_chars = self.tally.per_press if source == "hotkey" else 0
        cleanup = self.tally.stopped(source)
        self.recording = False
        self.recorder.stop()
        self.beeps.play("stop")
        threading.Thread(target=self._finish, args=(self.target, cleanup, stop_chars),
                         daemon=True).start()

    def _timeout(self, started):
        while self.recording and self.started_at == started:
            if time.time() - started >= MAX_DURATION_MINS * 60:
                _log("recording hit %d minutes: stopping" % MAX_DURATION_MINS)
                self.toggle("timeout")
                return
            time.sleep(1)

    # -- after the recording -----------------------------------------------------
    def _finish(self, target, cleanup, stop_chars):
        path = self.recorder.path
        try:
            seconds = wav_seconds(path)
            if seconds < MIN_RECORDING_DURATION_SEC:
                _log("recording too short (%.2fs): skipped" % seconds)
                return
            env = _env()
            self.overlay.show("Editing the highlighted text..." if target.selection
                              else "Transcribing...")
            try:
                if target.selection:
                    text = transcribe_client.edit_selection(path, env, target.selection)
                else:
                    rule = app_styles.rule_for(target.exe, app_styles.load())
                    text = transcribe_client.transcribe(path, env, timeout=60,
                                                        style=app_styles.request_fields(rule))
            except getattr(transcribe_client, "Unreachable", ()) as e:
                if target.selection:     # an instruction about a highlight makes no sense later
                    self.overlay.show(str(e), seconds=4)
                else:
                    self._keep(path, e)
                return
            except Exception as e:
                _log("transcription failed: %s" % e.__class__.__name__)
                self.overlay.show(str(e) if isinstance(e, transcribe_client.TranscribeError)
                                  else "Error: %s" % e, seconds=4)
                return
            _log("%s: %.1fs of audio -> %d characters (%s)"
                 % ("edit" if target.selection else "dictation", seconds, len(text),
                    target.exe or "?"))
            if not text:
                self.overlay.show("No speech detected.", seconds=2)
                return
            self.deliver(text, cleanup, stop_chars, target, env)
        except Exception:
            _log("after recording:\n" + traceback.format_exc())
        finally:
            try:
                os.remove(path)          # the audio isn't kept
            except OSError:
                pass
            if not self.recording:
                self.overlay.hide()

    # -- a dictation the server couldn't take is kept and tried again ------------
    def _keep(self, path, error):
        """The server didn't answer: keep the recording (kept_recordings.py,
        as on Windows), say so, start retrying."""
        import kept_recordings
        kept = kept_recordings.keep(path)
        _log("couldn't send the dictation (%s); %s"
             % (error.__class__.__name__, "kept it" if kept else "couldn't keep it"))
        self.overlay.show("%s\n%s" % kept_recordings.kept_message(error, bool(kept)), seconds=8)
        if kept:
            self.start_retrying()

    def start_retrying(self):
        """One background thread that sends kept recordings once the server is back."""
        import kept_recordings
        with self._lock:
            if self._retrying or not kept_recordings.pending():
                return
            self._retrying = True
        threading.Thread(target=self._retry_kept, name="kept-recordings", daemon=True).start()

    def retry_once(self, paths, transcribe, drop):
        """Send each kept recording (oldest first). Returns (texts, still
        waiting). Stops at the first one the server can't take yet; one that
        fails any other way is dropped (it would fail the same way every time)."""
        texts = []
        for n, path in enumerate(paths):
            try:
                text = transcribe(path)
            except transcribe_client.Unreachable:
                return texts, len(paths) - n
            except Exception as e:
                _log("kept recording dropped: %s" % e.__class__.__name__)
                drop(path)
                continue
            drop(path)
            if text:
                texts.append(text)
        return texts, 0

    def _retry_kept(self):
        import kept_recordings
        try:
            while True:
                time.sleep(kept_recordings.RETRY_SECONDS)
                if self.recording:
                    continue
                env = _env()
                texts, waiting = self.retry_once(
                    kept_recordings.pending(),
                    lambda p: transcribe_client.transcribe(p, env, timeout=60),
                    kept_recordings.drop)
                if texts:
                    # You have moved on by now: the clipboard, never a paste.
                    _log("kept recordings transcribed: %d" % len(texts))
                    set_clipboard(kept_recordings.join_texts(texts), self.clip)
                    self.overlay.show("%s\n%s" % kept_recordings.ready_message(len(texts)),
                                      seconds=8)
                if not waiting and not kept_recordings.pending():
                    break
        except Exception:
            _log("kept recordings: retry stopped:\n" + traceback.format_exc())
        finally:
            self._retrying = False

    def deliver(self, text, cleanup, stop_chars, target, env):
        """Paste `text` where the recording started. Clicked away since: bring
        that window back first (X11 puts the focus back in the box it had).
        If that fails, paste nowhere: the text stays on the clipboard."""
        if not set_clipboard(text, self.clip):
            _log("no clipboard tool (install xclip, or wl-clipboard on Wayland)")
            self.overlay.show("Can't reach the clipboard: install xclip (or wl-clipboard).",
                              seconds=6)
            return
        if self.desk is None:
            self._deliver_wayland(env)
            return
        exe, cls = target.exe, target.cls
        now = self.desk.active_window()
        if target.wid and now != target.wid:
            _log("focus moved away from %s: going back (%d characters)" % (exe or "?", len(text)))
            # The stop press typed its character where you are now.
            self._backspace(stop_chars)
            cleanup -= stop_chars
            if not self.desk.activate(target.wid):
                _log("couldn't get back to %s: text left on the clipboard" % (exe or "?"))
                self.overlay.show("Couldn't get back to %s.\nYour text is on the clipboard: "
                                  "press Ctrl+V to paste it." % (exe or "the window you were in"),
                                  seconds=6)
                return
            time.sleep(0.15)             # the window settles its focus
        elif not target.wid:
            exe, cls = self.desk.program(now)
        if self.tracker_held_modifiers():
            self.desk.release_modifiers()
        self._backspace(cleanup)
        time.sleep(0.08)                 # the clipboard helper is serving by now
        self.desk.send(paste_keys(env, exe, cls), gap=PASTE_KEY_GAP)
        _log("pasted in %s" % (exe or "?"))

    def _backspace(self, count):
        if count > 0:
            time.sleep(0.1)
            self.desk.send(["backspace"], gap=0.01, times=count)
            time.sleep(0.1)

    tracker_held_modifiers = staticmethod(lambda: False)   # set by run() on X11

    def _deliver_wayland(self, env):
        keys = paste_keys(env, "", "")
        if shutil.which("wtype"):
            mods = [a for k in keys[:-1] for a in ("-M", k)]
            ok, _ = _run(["wtype"] + mods + ["-k", keys[-1]] +
                         [a for k in reversed(keys[:-1]) for a in ("-m", k)])
        elif shutil.which("ydotool"):
            codes = {"ctrl": 29, "shift": 42, "alt": 56, "v": 47}
            seq = [codes[k] for k in keys if k in codes]
            ok, _ = _run(["ydotool", "key"] + ["%d:1" % c for c in seq] +
                         ["%d:0" % c for c in reversed(seq)])
        else:
            ok = False
        if ok:
            _log("pasted (wayland)")
        else:
            self.overlay.show("Your text is on the clipboard: press Ctrl+V to paste it.", seconds=5)


# --- Running it ---------------------------------------------------------------------

def pid_path():
    return os.path.join(runtime_dir(), "listener.pid")


def signal_running(sig):
    """Send `sig` to the running listener; False if there isn't one."""
    try:
        with open(pid_path()) as f:
            pid = int(f.read().strip())
        os.kill(pid, sig)
        return True
    except (OSError, ValueError):
        return False


def check():
    """Print what the listener would use on this machine. 0: ready."""
    env = _env()
    session = session_type()
    hotkey, mode = hotkey_config.from_env(env)
    edit = linux_hotkeys.edit_hotkey(env, hotkey)
    rec = recorder_command("x.wav")
    clip = clipboard_tool(session)
    problems = []
    print("%s listener on Linux" % APP_NAME)
    print("  session      %s" % (session or "none (no DISPLAY): can't run here"))
    if session == "x11":
        try:
            import linux_x11
            linux_x11.Desktop().active_window()
            print("  hotkey       %s (%s)%s" % (pretty_key(hotkey), mode,
                                               ", edit highlighted text: " + pretty_key(edit) if edit else ""))
        except Exception as e:
            problems.append("can't talk to the X server (%s); is python-xlib installed?" % e.__class__.__name__)
    elif session == "wayland":
        print("  hotkey       none on Wayland: bind a keyboard shortcut in your desktop's")
        print("               settings to:  %s -m client.linux_listener --toggle" % sys.executable)
        tool = next((t for t in ("wtype", "ydotool") if shutil.which(t)), None)
        print("  pasting      %s" % (tool or "no wtype/ydotool: text is left on the clipboard"))
    else:
        problems.append("no desktop session")
    print("  microphone   %s" % (rec[0] if rec else "-"))
    if not rec:
        problems.append("no recorder: install pipewire, pulseaudio-utils or alsa-utils")
    print("  clipboard    %s" % (clip or "-"))
    if not clip:
        problems.append("no clipboard tool: install xclip (X11) or wl-clipboard (Wayland)")
    try:
        import tkinter  # noqa: F401
        print("  on-screen    Tk card")
    except ImportError:
        print("  on-screen    desktop notifications (install python3-tk for the card)")
    src = transcribe_client.source(env)
    ok, msg = transcribe_client.check_connection(env)
    print("  transcribes  %s: %s" % ({"local": "this PC's server", "remote": "another PC",
                                     "cloud": "a cloud service"}[src], msg))
    if not ok:
        problems.append("transcription source: " + msg)
    for p in problems:
        print("  PROBLEM      " + p)
    return 1 if problems else 0


def run():
    session = session_type()
    if not session:
        print("No desktop session (DISPLAY is not set): the listener needs one.", file=sys.stderr)
        return 1
    import fcntl          # Linux only; the rest of this file imports anywhere (tests)
    lock = open(pid_path(), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("The listener is already running.", file=sys.stderr)
        return 0
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    try:
        overlay = Overlay()
    except Exception as e:
        _log("no on-screen card (%s): using notifications" % e.__class__.__name__)
        overlay = NoOverlay()

    desk = watcher = None
    if session == "x11":
        import linux_x11
        desk = linux_x11.Desktop()
    listener = Listener(overlay, desk, session)

    # `--toggle` / `--toggle-edit` from a desktop keyboard shortcut. The flag is
    # picked up on the card's 50 ms tick (a signal handler mustn't do the work).
    asked = []
    signal.signal(signal.SIGUSR1, lambda *_: asked.append(False))
    signal.signal(signal.SIGUSR2, lambda *_: asked.append(True))

    def quit_(*_):
        listener.recorder.stop()
        os._exit(0)
    signal.signal(signal.SIGTERM, quit_)
    signal.signal(signal.SIGINT, quit_)

    def tick():
        while asked:
            edit = asked.pop(0)
            threading.Thread(target=listener.toggle, args=("signal", edit), daemon=True).start()
        if watcher is not None and not watcher.is_alive():
            _log("lost the X server (%s): exiting" % watcher.failed)
            quit_()
    overlay.on_tick = tick

    if session == "x11":
        try:
            combo = linux_hotkeys.parse(listener.hotkey)
            if not linux_x11.keysym_for(combo.key if combo.key not in linux_hotkeys.MODIFIERS
                                        else "left " + combo.key):
                raise ValueError("no such key")
        except ValueError as e:
            _log("hotkey %r rejected (%s): using %s" % (listener.hotkey, e, hotkey_config.DEFAULT_HOTKEY))
            listener.hotkey, listener.mode = hotkey_config.DEFAULT_HOTKEY, "toggle"
            listener.tally = hotkey_config.PressTally(listener.hotkey, listener.mode)
            combo = linux_hotkeys.parse(listener.hotkey)
        edit_text = linux_hotkeys.edit_hotkey(_env(), listener.hotkey)
        try:
            edit_combo = linux_hotkeys.parse(edit_text) if edit_text else None
        except ValueError:
            edit_combo = None
        hold = listener.mode == "hold"

        def on_press(name, held):
            edit = bool(edit_combo and edit_combo.fires(name, held))
            if not edit and not combo.fires(name, held):
                return
            if hold and listener.recording:
                return
            if listener.recording and edit:
                edit = False                 # either key stops a recording
            threading.Thread(target=listener.toggle, args=("hotkey", edit), daemon=True).start()

        def on_release(name):
            if hold and listener.recording and combo.released_by(name):
                threading.Thread(target=listener.toggle, args=("hotkey",), daemon=True).start()

        tracker = linux_hotkeys.KeyTracker(on_press, on_release)
        listener.tracker_held_modifiers = lambda: any(linux_hotkeys.kind(k) for k in tracker.held())
        watcher = linux_x11.KeyWatcher(tracker.press, tracker.release)
        watcher.start()
        print("[Listener] %s %s to record%s." % (
            "Hold" if hold else "Press", pretty_key(listener.hotkey),
            "; %s to edit highlighted text" % pretty_key(edit_text) if edit_combo else ""), flush=True)
    else:
        print("[Listener] Wayland: no global hotkey. Bind a keyboard shortcut to:\n"
              "  %s -m client.linux_listener --toggle" % sys.executable, flush=True)

    if os.environ.get("WF_STARTUP_BEEP", "1") == "1":
        listener.beeps.play("hello")
    if hasattr(transcribe_client, "Unreachable"):
        listener.start_retrying()        # recordings kept before a restart
    overlay.run()
    return 0


def main(argv=None):
    args = (sys.argv[1:] if argv is None else argv)
    if "--check" in args:
        return check()
    if "--toggle" in args or "--toggle-edit" in args:
        sig = signal.SIGUSR2 if "--toggle-edit" in args else signal.SIGUSR1
        if signal_running(sig):
            return 0
        print("The listener isn't running.", file=sys.stderr)
        return 1
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
