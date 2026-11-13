import keyboard
import sounddevice as sd
import numpy as np
import scipy.io.wavfile as wav
import winsound
import threading
import time
import os
import pyperclip
import ctypes
from ctypes import wintypes
import tkinter as tk
from dotenv import load_dotenv, dotenv_values
import sys
import traceback

# Load env file in the child too
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(ENV_FILE)

# Configuration
SAMPLE_RATE = 44100
CHANNELS = 1
MAX_DURATION_MINS = 10
# Recordings shorter than this are treated as accidental taps / near-silent
# blips and dropped before they ever hit the API. Whisper's decoder falls back
# to its language prior on silence and hallucinates end-of-clip filler (usually
# "Thank you."), so a fast double-tap of the hotkey would otherwise paste a
# phantom "thank you". Tune via .env if legitimate one-word utterances get cut.
MIN_RECORDING_DURATION_SEC = float(os.environ.get("MIN_RECORDING_DURATION_SEC", 0.4))
LISTENER_RESTART_INTERVAL_MINS = int(os.environ.get("LISTENER_RESTART_INTERVAL_MINS", 10))
WAV_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'temp_recording.wav')
ERROR_LOG_PATH = os.path.join(os.path.dirname(__file__), 'listener_errors.log')
# Where audio goes (this PC / another PC / cloud): see transcribe_client.py.

# Hotkey + mode from .env (HOTKEY, HOTKEY_MODE); see hotkey_config.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hotkey_config  # noqa: E402
import transcribe_client  # noqa: E402
import clipboard_win  # noqa: E402
import app_styles  # noqa: E402
HOTKEY, HOTKEY_MODE = hotkey_config.from_env(os.environ)
# Counts the characters the hotkey typed into the target app during a
# recording (backtick: one per press; F9: none). Clicks on the floating mic
# button type nothing, so they don't count.
press_tally = hotkey_config.PressTally(HOTKEY, HOTKEY_MODE)

# The floating mic button at the text cursor (caret_button.py); None if it failed.
caret_btn = None


class Target:
    """Where one recording is going, noted when it starts: the app in front
    (for its style, see app_styles.py), any highlighted text to edit by voice
    instead of dictating, and the spot itself (window, box, caret; see
    text_focus.Spot) so the text can go back there if you click away.

    Looked up on the focus worker thread (text_focus.worker(), which owns the
    UIA objects the spot holds) so the start beep isn't delayed; the
    recording is far longer than the lookup."""

    # A typing hotkey's character reaches the app a moment after the press:
    # note the caret after it, so going back puts the caret after it too.
    TYPED_CHAR_SETTLE = 0.08

    def __init__(self, source, typing_key):
        self.app, self.selection, self.spot = "", "", None
        # The style pill's choices for this recording (style_pill.StyleSession,
        # None: no pill), and whether the recording has stopped.
        self.style, self.stopped = None, False
        self._job = None
        settle = self.TYPED_CHAR_SETTLE if (source == "hotkey" and typing_key) else 0
        try:
            import text_focus
            self._job = text_focus.worker().submit(
                lambda _ins: self._look(source, typing_key, settle))
        except Exception:
            self._job = None

    def _look(self, source, typing_key, settle=0):
        import text_focus
        try:
            self.app = app_styles.foreground_app()
            # A typing hotkey (backtick) has already typed over the highlight
            # by now, so editing a selection needs the button or a key that
            # types nothing.
            if _env_on("EDIT_SELECTION", True) and (source == "button" or not typing_key):
                self.selection = text_focus.selection_to_edit()
        except Exception:
            pass
        if settle:
            time.sleep(settle)
        self.spot = text_focus.capture_spot()

    def wait(self, timeout=2.0):
        if self._job is not None:
            self._job.wait(timeout)
        return self

    def release(self):
        """Done with this recording: let go of the UIA objects the spot holds,
        on the thread that made them (queued after the lookup itself)."""
        try:
            import text_focus
            text_focus.worker().submit(lambda _ins: self.spot and self.spot.release())
        except Exception:
            pass


def _env():
    """This process's environment with .env on top (re-read each time)."""
    return dict(os.environ, **{k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})


def _begin_style(target, started):
    """Give the floating button this recording's style pill (style_pill.py):
    once the target app is known, start from its style or the AI Editing
    defaults. No pill for an edit of highlighted text or a cloud service.
    Runs on its own thread; never raises."""
    try:
        import style_pill
        target.wait()
        env = _env()
        src = transcribe_client.source(env)
        ok = src == "remote" or (src == "local" and bool(transcribe_client._dictate_url(env)))
        rule = app_styles.rule_for(target.app, app_styles.load())
        session = style_pill.session_for(rule, env, src, selection=bool(target.selection),
                                         styles_ok=ok, started=started)
        target.style = session
        if session is not None and target.stopped:
            session.close()       # stopped before we got here
        if caret_btn is not None and current_target is target and not target.stopped:
            caret_btn.begin_style(session)
    except Exception:
        _log("style pill: couldn't start:\n" + traceback.format_exc())


def _end_style(target):
    """The recording stopped: freeze its style choices, the pill goes away."""
    if target is None:
        return
    target.stopped = True
    try:
        if target.style is not None:
            target.style.close()
    except Exception:
        pass


def _env_on(key, default):
    v = (dotenv_values(ENV_FILE).get(key) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


current_target = None


def _button_state(state):
    """Tell the floating mic button what we're doing. Never raises."""
    try:
        if caret_btn is not None:
            caret_btn.set_state(state)
    except Exception:
        pass

# How long to let Chrome Remote Desktop push the local clipboard to the remote
# host after we nudge its window focus, before we send Ctrl+V. Tune via .env if
# your connection is slow.
CRD_SYNC_DELAY = float(os.environ.get("CRD_SYNC_DELAY_SECONDS", 0.6))
# Pause between the key events of the paste (Ctrl down / V / V up / Ctrl up).
PASTE_KEY_GAP = float(os.environ.get("PASTE_KEY_GAP_SECONDS", 0.04))

# --- Win32 plumbing for Chrome Remote Desktop clipboard sync -----------------
# CRD pushes the LOCAL clipboard to the REMOTE machine on a window focus-IN
# event, and only when the clipboard was written by a real, focused window doing
# a real copy (like Notepad) -- not by a headless API write (pyperclip) from a
# hidden owner window that's destroyed instantly.
#
# So we reproduce the manual "copy in Notepad, then click into CRD" workaround:
#   1. spawn a real (off-screen) EDIT control,
#   2. give it foreground focus, fill it, select all, issue a real copy,
#   3. destroy it -> focus falls back to the CRD window. THAT focus-in is what
#      makes CRD sync the fresh clipboard to the remote machine.
#   4. then paste.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_WS_POPUP = 0x80000000
_WS_VISIBLE = 0x10000000
_ES_MULTILINE = 0x0004
_ES_AUTOVSCROLL = 0x0040
_WS_EX_TOOLWINDOW = 0x00000080
_EM_SETSEL = 0x00B1
_WM_COPY = 0x0301

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.FindWindowW.restype = wintypes.HWND
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


def _set_foreground(hwnd):
    """Force `hwnd` to the foreground as reliably as Windows allows.

    AttachThreadInput defeats the cross-process foreground lock so the switch
    (Chrome <-> our window) actually happens. Returns True if `hwnd` ended up as
    the foreground window. Best-effort: failures are logged and swallowed.
    """
    if not hwnd:
        return False
    try:
        fg = _user32.GetForegroundWindow()
        fg_thread = _user32.GetWindowThreadProcessId(fg, None)
        cur_thread = _kernel32.GetCurrentThreadId()
        attached = fg_thread and fg_thread != cur_thread
        if attached:
            _user32.AttachThreadInput(cur_thread, fg_thread, True)
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(cur_thread, fg_thread, False)
        time.sleep(0.03)
        return _user32.GetForegroundWindow() == hwnd
    except Exception as e:
        print(f"[Listener] set_foreground failed (continuing): {e}")
        return False


def copy_via_real_window(text, target_hwnd, private=False):
    """Copy `text` to the clipboard from a real focused EDIT control, the way an
    app like Notepad does, then hand focus back to `target_hwnd`.

    The spawned window is real (off-screen so it never flashes), takes foreground,
    does a genuine WM_COPY (exactly what Ctrl+C triggers inside an edit control),
    then is destroyed so focus returns to the target -- generating the focus-in
    that Chrome Remote Desktop needs to sync the clipboard. Returns True on success.
    """
    edit_hwnd = None
    left_target = False
    try:
        hinst = _kernel32.GetModuleHandleW(None)
        style = _WS_POPUP | _WS_VISIBLE | _ES_MULTILINE | _ES_AUTOVSCROLL
        # Spawned far off-screen so the user never sees it.
        edit_hwnd = _user32.CreateWindowExW(
            _WS_EX_TOOLWINDOW, "EDIT", None, style,
            -32000, -32000, 10, 10, None, None, hinst, None,
        )
        if not edit_hwnd:
            return False

        _user32.SetWindowTextW(edit_hwnd, text)
        left_target = _set_foreground(edit_hwnd)   # real window takes focus (Chrome loses it)
        time.sleep(0.15)                            # let Chrome register the deactivation
        if private:
            # Same focused window, but our own clipboard write, so it can carry
            # the "don't keep in clipboard history" markers. Remote Desktop may
            # not sync this kind of copy.
            clipboard_win.set_text(text, private=True, owner=edit_hwnd)
        else:
            _user32.SendMessageW(edit_hwnd, _EM_SETSEL, 0, -1)   # select all
            _user32.SendMessageW(edit_hwnd, _WM_COPY, 0, 0)      # real edit-control copy
        time.sleep(0.05)
        return True
    except Exception as e:
        print(f"[Listener] real-window copy failed: {e}")
        return False
    finally:
        if edit_hwnd:
            _user32.DestroyWindow(edit_hwnd)        # close popup -> focus returns
        back = _set_foreground(target_hwnd)         # ...to the CRD window (focus-in)
        # Diagnostic: did the OS-level foreground bounce actually happen?
        # Both must be True for Chrome Remote Desktop to re-sync the clipboard.
        print(f"[Listener] focus bounce  left_target={left_target}  back_to_target={back}")

# Global State
recording_active = False
recording_data = []
stream = None
recording_start_time = 0
overlay = None
last_active_time = time.time()

class LoadingOverlay:
    """The small "Transcribing..." card in the middle of the screen, and the
    bigger "Couldn't get back to..." notice. Styled like Settings (theme.py:
    Noto Sans, dark surface, thin border, rounded corners, accent progress
    bar). It never takes focus (WS_EX_NOACTIVATE): the paste goes to your app.

    Thread-safe API (other threads): show_safe, hide_safe, update_label_safe,
    notice_safe. Everything else runs on the Tk thread."""

    BAR_W, BAR_H = 220, 4          # progress bar, 96-DPI pixels
    BAR_PERIOD = 1.3               # seconds for one sweep left -> right

    def __init__(self):
        import theme
        self.theme = theme
        self.root = tk.Tk()
        self.root.withdraw()  # Hide initially
        self.root.overrideredirect(True)  # Frameless
        self.root.attributes('-topmost', True)  # Always on top
        # Tk's pixels per point follow the screen DPI (the process is
        # DPI-aware, see __main__); our own pixel sizes follow the same scale.
        try:
            self.scale = max(1.0, self.root.winfo_fpixels('1i') / 96.0)
        except Exception:
            self.scale = 1.0
        t = theme
        # A 1-px border drawn by Tk; replaced by Windows 11's own rounded
        # border (same colour) once DWM accepts it, see _style_window.
        self.root.configure(bg=t.BORDER)
        self.card = tk.Frame(self.root, bg=t.SURFACE)
        self.card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.inner = tk.Frame(self.card, bg=t.SURFACE)
        self.inner.pack(padx=self._px(24), pady=self._px(16))

        self.label = tk.Label(self.inner, text="Transcribing...", bg=t.SURFACE, fg=t.TEXT,
                              font=(t.FONT, 12), justify=tk.CENTER,
                              wraplength=self._px(420))
        self.label.pack()
        # Second line: only for the notice.
        self.detail = tk.Label(self.inner, text="", bg=t.SURFACE, fg=t.TEXT,
                               font=(t.FONT, 12), justify=tk.CENTER,
                               wraplength=self._px(460))

        bw, bh = self._px(self.BAR_W), max(3, self._px(self.BAR_H))
        self.bar = tk.Canvas(self.inner, width=bw, height=bh, bg=t.SURFACE,
                             highlightthickness=0, bd=0)
        y = bh / 2
        self.bar.create_line(y, y, bw - y, y, fill=t.RAISED, width=bh, capstyle=tk.ROUND)
        self._seg = self.bar.create_line(y, y, y + 1, y, fill=t.ACCENT, width=bh,
                                         capstyle=tk.ROUND)
        self.bar.pack(pady=(self._px(10), 0))

        self._anim = None          # after() id of the bar animation
        self._anim_t0 = 0.0
        self._notice_job = None    # after() id that ends the notice
        self._hwnd = None
        self._rounded = False
        self.center_window()

    def _px(self, n):
        return int(round(n * self.scale))

    # -- window styling ------------------------------------------------------
    def _style_window(self):
        """Once, after Tk has made the window: never activate (so showing it
        can't take focus from the app being pasted into), keep it off the
        taskbar, and ask Windows 11 for rounded corners and a thin border."""
        if self._hwnd:
            return
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
        except Exception:
            return
        self._hwnd = hwnd
        try:
            GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x80
            ex = _user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE,
                                   ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass
        try:
            dwm = ctypes.windll.dwmapi
            DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, 2
            DWMWA_BORDER_COLOR = 34
            ok = dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                           ctypes.byref(ctypes.c_int(DWMWCP_ROUND)), 4) == 0
            c = self.theme.BORDER.lstrip("#")
            colorref = int(c[4:6], 16) << 16 | int(c[2:4], 16) << 8 | int(c[0:2], 16)
            ok = ok and dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_BORDER_COLOR,
                                                  ctypes.byref(ctypes.c_int(colorref)), 4) == 0
            if ok:
                # Windows draws the border now; drop Tk's square one.
                self._rounded = True
                self.root.configure(bg=self.theme.SURFACE)
                self.card.pack_configure(padx=0, pady=0)
        except Exception:
            pass

    def center_window(self):
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        self.root.geometry(f'+{x}+{y}')

    # -- progress bar --------------------------------------------------------
    def _start_bar(self):
        self._stop_bar()
        self._anim_t0 = time.monotonic()
        self._tick_bar()

    def _stop_bar(self):
        if self._anim is not None:
            try:
                self.root.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None

    def _tick_bar(self):
        """A short accent segment gliding back and forth along the track."""
        import math
        bw = int(self.bar.cget("width"))
        bh = int(self.bar.cget("height"))
        y = bh / 2
        seg = bw * 0.32
        u = ((time.monotonic() - self._anim_t0) / self.BAR_PERIOD) % 2.0
        u = u if u <= 1.0 else 2.0 - u                 # ping-pong 0..1..0
        p = (1 - math.cos(math.pi * u)) / 2            # ease in/out
        x0 = y + p * (bw - 2 * y - seg)
        self.bar.coords(self._seg, x0, y, x0 + seg, y)
        self._anim = self.root.after(16, self._tick_bar)

    # -- states --------------------------------------------------------------
    def _end_notice(self):
        if self._notice_job is not None:
            try:
                self.root.after_cancel(self._notice_job)
            except Exception:
                pass
            self._notice_job = None

    def _layout(self, title, detail=None, notice=False):
        t = self.theme
        self.label.config(text=title, font=(t.FONT_SB if notice else t.FONT, 15 if notice else 12))
        if detail:
            self.detail.config(text=detail)
            self.detail.pack(after=self.label, pady=(self._px(6), 0))
        else:
            self.detail.pack_forget()
        if notice:
            self.bar.pack_forget()
            self.inner.pack_configure(padx=self._px(32), pady=self._px(24))
        else:
            if not self.bar.winfo_ismapped():
                self.bar.pack(pady=(self._px(10), 0))
            self.inner.pack_configure(padx=self._px(24), pady=self._px(16))

    def _present(self):
        self._style_window()
        self.center_window()
        self.root.deiconify()
        self.root.attributes('-topmost', True)

    def show(self):
        # Reset the label every time we show, so a previous run's error message
        # ("No speech detected.", a long API error) never sticks around on the
        # next recording. Without this the label keeps the last text it was set
        # to, since successful runs never touch it.
        self._end_notice()
        self._layout("Transcribing...")
        self._present()
        self._start_bar()

    def hide(self):
        if self._notice_job is not None:
            return          # a notice is up: it hides itself when its time is up
        self._stop_bar()
        self.root.withdraw()

    def notice(self, title, detail="", seconds=6):
        """A bigger message that stays up for `seconds`, whatever else asks
        the overlay to hide in the meantime (a new recording replaces it)."""
        self._end_notice()
        self._stop_bar()
        self._layout(title, detail, notice=True)
        self._present()

        def done():
            self._notice_job = None
            self.hide()
        self._notice_job = self.root.after(int(seconds * 1000), done)

    def start(self):
        self.root.mainloop()

    # Thread-safe wrappers
    def show_safe(self):
        self.root.after(0, self.show)

    def hide_safe(self):
        self.root.after(0, self.hide)

    def notice_safe(self, title, detail="", seconds=6):
        self.root.after(0, lambda: self.notice(title, detail, seconds))

    def update_label(self, new_text):
        self.label.config(text=new_text)
        if self.root.state() != "withdrawn":
            self.center_window()   # a long error message widens the card

    def update_label_safe(self, new_text):
        self.root.after(0, lambda: self.update_label(new_text))

def beep_start():
    winsound.Beep(1000, 60)

def beep_stop():
    winsound.Beep(600, 100)

def callback(indata, frames, time_info, status):
    if recording_active:
        recording_data.append(indata.copy())

def start_recording():
    global recording_active, recording_data, stream, recording_start_time
    if recording_active:
        return
    
    print("[Listener] Starting recording...")
    beep_start()
    recording_data = []
    recording_start_time = time.time()

    # Open the mic BEFORE flipping recording_active. If the open raises, the
    # flag must stay False, otherwise idle_monitor thinks we're mid-recording
    # forever and never does its periodic self-restart.
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, callback=callback)
    stream.start()
    recording_active = True

    # Start timeout monitor
    threading.Thread(target=timeout_monitor, daemon=True).start()

def stop_recording(source="hotkey"):
    global recording_active, stream
    if not recording_active:
        return
    # How many hotkey characters to backspace before pasting, counted now
    # (a new recording may start before this one is pasted).
    # The stop press's own character (if any) lands wherever you are now,
    # which may not be where the recording started.
    stop_chars = press_tally.per_press if source == "hotkey" else 0
    cleanup = press_tally.stopped(source)
    target = current_target
    _end_style(target)

    print("[Listener] Stopping recording...")
    recording_active = False
    if stream:
        stream.stop()
        stream.close()
    
    _button_state("working")
    beep_stop()

    # Start processing in a separate thread so we don't block the hotkey/callback
    threading.Thread(target=save_and_transcribe, args=(cleanup, target, stop_chars),
                     daemon=True).start()

def timeout_monitor():
    while recording_active:
        elapsed_mins = (time.time() - recording_start_time) / 60
        if elapsed_mins >= MAX_DURATION_MINS:
            print("[Listener] Timeout reached. Stopping.")
            stop_recording(source="timeout")
            break
        time.sleep(1)

def idle_monitor():
    while True:
        time.sleep(10)
        if not recording_active:
            idle_mins = (time.time() - last_active_time) / 60.0
            if idle_mins >= LISTENER_RESTART_INTERVAL_MINS:
                print(f"[Listener] Idle for {LISTENER_RESTART_INTERVAL_MINS} minutes. Exiting to allow watchdog to restart.")
                os._exit(0)

def save_and_transcribe(cleanup=0, target=None, stop_chars=0):
    try:
        _save_and_transcribe(cleanup, target, stop_chars)
    finally:
        if target is not None:
            target.release()
        # Done (pasted, nothing heard, too short, or failed): the button goes
        # away -- unless a new recording has already started.
        if not recording_active:
            _button_state("idle")


def _save_and_transcribe(cleanup, target=None, stop_chars=0):
    global recording_data

    if not recording_data:
        print("[Listener] No data recorded.")
        return

    # Drop accidental taps / near-silent blips before showing the overlay or
    # touching the API. Measured from the actual captured samples, not wall
    # clock, so it reflects real audio length. See MIN_RECORDING_DURATION_SEC.
    audio = np.concatenate(recording_data, axis=0)
    duration_sec = len(audio) / SAMPLE_RATE
    if duration_sec < MIN_RECORDING_DURATION_SEC:
        print(f"[Listener] Recording too short ({duration_sec:.2f}s < {MIN_RECORDING_DURATION_SEC}s). Skipping.")
        return

    # Show overlay
    if overlay:
        overlay.show_safe()

    try:
        print("[Listener] Saving WAV...")
        os.makedirs(os.path.dirname(WAV_OUTPUT_PATH), exist_ok=True)
        wav.write(WAV_OUTPUT_PATH, SAMPLE_RATE, np.int16(audio * 32767))

        transcribe_and_paste(cleanup, target, stop_chars)
    finally:
        # Hide overlay
        if overlay:
            overlay.hide_safe()

def transcribe_and_paste(cleanup=0, target=None, stop_chars=0):
    print("[Listener] Sending to API...")
    try:
        # This PC, another PC, or a cloud service (TRANSCRIBE_SOURCE).
        # .env is re-read each time, so switching the source in Settings takes
        # effect on the next dictation, no listener restart needed.
        env = dict(os.environ, **{k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})
        target = target.wait() if target is not None else None
        if target is not None and target.selection:
            # Highlighted text: what was said is the instruction, and the
            # editor's version replaces the highlight (still selected).
            if overlay:
                overlay.update_label_safe("Editing the highlighted text...")
            text = transcribe_client.edit_selection(WAV_OUTPUT_PATH, env, target.selection)
        else:
            # The app's own style, if Settings has one for it -- or what was
            # picked on the style pill for this one dictation.
            rule = app_styles.rule_for(target.app, app_styles.load()) if target else None
            style = app_styles.request_fields(rule)
            session = getattr(target, "style", None) if target is not None else None
            if session is not None and session.modified:
                style = session.request_fields(style)
                _log(f"style pill: {session.describe()}")
            text = transcribe_client.transcribe(WAV_OUTPUT_PATH, env, timeout=60, style=style)
        
        print(f"[Listener] Transcribed: {text}")

        if text:
            deliver(text, cleanup, stop_chars, target, env)
        else:
            if overlay:
                overlay.update_label_safe("No speech detected.")
                time.sleep(2)
            
    except Exception as e:
        error_msg = f"API Error: {str(e)}"
        print(f"[Listener] {error_msg}")
        if overlay:
            overlay.update_label_safe(error_msg)
            time.sleep(3)

def _log(msg):
    """One line in the listener log. Never dictated text: lengths and
    program names only."""
    print(f"[Listener] {msg}")
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _paste_here(text, cleanup, private):
    """Paste into whatever has focus now (the original, unchanged path)."""
    # Remember the window we're typing into, before we touch focus.
    target_hwnd = _user32.GetForegroundWindow()

    # Remove the characters the hotkey typed (one per backtick press;
    # none for F9 / Ctrl+Alt+Space or mic-button clicks), while the
    # target window has focus.
    _remove_hotkey_chars(cleanup)

    # Copy from a real off-screen window and bounce focus back to the
    # target, so Chrome Remote Desktop sees a real copy + focus-in and
    # syncs the clipboard to the remote machine. Falls back to a plain
    # clipboard write if the real-window copy fails.
    _copy(text, target_hwnd, private)

    # Give CRD time to push the clipboard across before we paste.
    time.sleep(CRD_SYNC_DELAY)

    _paste()
    print("[Listener] Pasted.")


def _copy(text, target_hwnd, private):
    """Text onto the clipboard (the Remote Desktop-friendly way), focus back
    on target_hwnd. With CLIPBOARD_PRIVATE on it's still written, just marked
    "keep out of clipboard history"."""
    if not copy_via_real_window(text, target_hwnd, private=private):
        if private:
            clipboard_win.set_text(text, private=True)
        else:
            pyperclip.copy(text)
        _set_foreground(target_hwnd)


def deliver(text, cleanup, stop_chars, target, env):
    """Paste `text` where the recording started.

    Still in that window and box (the usual case): exactly the old path.
    Clicked away since: go back there first (window to the front, the box
    focused, the caret or highlight re-selected) and paste only once that's
    verified. If it can't get back, paste nowhere: the text stays on the
    clipboard and a notice says so."""
    private = clipboard_win.wanted(env)
    spot = getattr(target, "spot", None) if target is not None else None
    if spot is None or not spot.hwnd:
        _paste_here(text, cleanup, private)
        return
    import text_focus
    w = text_focus.worker()
    try:
        same = w.run(lambda ins: text_focus.unchanged(spot, text_focus.LiveOps(ins)), timeout=3)
    except Exception as e:
        _log(f"return: couldn't check focus ({e.__class__.__name__}); pasting where you are")
        same = True
    if same:
        _paste_here(text, cleanup, private)
        return
    if not spot.editable:
        # Started somewhere that isn't a text box (desktop, a page, a
        # button): nothing to go back to, so paste where you are now.
        _log(f"return: started outside a text box ({spot.exe or '?'}); pasting where you are")
        _paste_here(text, cleanup, private)
        return
    need_sel = bool(target.selection)
    _deliver_back(text, cleanup, stop_chars, spot, need_sel, private, w)


def _remove_typed(w, count, where):
    """Backspace `count` hotkey characters at the caret -- only after checking
    that's what is right before it (better a stray ` than a deleted letter)."""
    if count <= 0:
        return
    import text_focus
    try:
        before = w.run(lambda ins: text_focus.LiveOps(ins).text_before_caret(count), timeout=2)
    except Exception:
        before = None
    if before is not None and before[-count:] == HOTKEY * count:
        _remove_hotkey_chars(count)
        _log(f"return: removed {count} hotkey character(s) {where}")
    else:
        _log(f"return: left {count} hotkey character(s) {where} (couldn't confirm them)")


def _deliver_back(text, cleanup, stop_chars, spot, need_sel, private, w):
    import text_focus
    app = spot.exe or "?"
    _log(f"return: focus moved away from {spot.describe()}; going back "
         f"({'edit' if need_sel else 'dictation'}, {len(text)} chars)")
    # The stop press typed its character where you are now: tidy that first.
    _remove_typed(w, stop_chars, "where you were")
    try:
        out = w.run(lambda ins: text_focus.go_back(spot, text_focus.LiveOps(ins), need_sel),
                    timeout=10)
    except Exception as e:
        out = text_focus.Outcome(False, f"error {e.__class__.__name__}")
    _log(f"return: {app} -> {'ok' if out.ok else 'FAILED'} ({out.reason}; {', '.join(out.steps)})")
    if out.ok:
        # The start press's character, if any, is in the original box.
        _remove_typed(w, cleanup - stop_chars, f"in {app}")
        _copy(text, spot.hwnd, private)
        time.sleep(CRD_SYNC_DELAY)
        try:
            ready = w.run(lambda ins: text_focus.ready_to_paste(spot, text_focus.LiveOps(ins),
                                                                need_sel), timeout=6)
        except Exception as e:
            ready = text_focus.Outcome(False, f"error {e.__class__.__name__}")
        if ready.ok:
            _paste()
            _log(f"return: pasted in {app}" + (f" ({', '.join(ready.steps)})" if ready.steps else ""))
            return
        _log(f"return: not pasting, {ready.reason}; text is on the clipboard")
        out = ready
    else:
        # The only copy of the text: onto the clipboard, focus left where it is.
        try:
            _copy(text, _user32.GetForegroundWindow(), private)
        except Exception:
            _log("return: clipboard write failed:\n" + traceback.format_exc())
    _couldnt_go_back(spot, need_sel, out)


def couldnt_go_back_message(app_name, need_sel, reason=""):
    """(title, body) of the notice. Pure."""
    where = app_name or "the window you were in"
    if need_sel and reason in ("highlight not re-selected", "highlight lost"):
        title = f"Couldn't re-select your highlighted text in {where}."
        body = "The edited text is on the clipboard: press Ctrl+V to paste it."
    else:
        title = f"Couldn't get back to {where}."
        body = "Your text is on the clipboard: press Ctrl+V to paste it."
    return title, body


def _couldnt_go_back(spot, need_sel, out):
    title, body = couldnt_go_back_message(spot.app_name, need_sel, out.reason)
    if overlay:
        overlay.notice_safe(title, body, seconds=NOTICE_SECONDS)


NOTICE_SECONDS = 6


def _paste():
    """Ctrl+V the way a person presses it: Ctrl down, a beat, V, a beat, Ctrl up.
    keyboard.send('ctrl+v') fires all four key events within a millisecond, and
    Chrome Remote Desktop can forward the V before the remote side has Ctrl
    down, so the remote app types a plain "v". The gaps cost ~0.1 s."""
    gap = PASTE_KEY_GAP
    keyboard.press('ctrl')
    try:
        time.sleep(gap)
        keyboard.press('v')
        time.sleep(gap)
        keyboard.release('v')
        time.sleep(gap)
    finally:
        keyboard.release('ctrl')


def _remove_hotkey_chars(count):
    if count > 0:
        time.sleep(0.1)
        keyboard.send(", ".join(["backspace"] * count))
        time.sleep(0.1)


def _hold_start():
    # Key auto-repeat fires this again and again while held: start once only.
    if not recording_active:
        toggle_recording()


def _hold_stop(_event=None):
    if recording_active:
        toggle_recording()


def register_hotkey():
    """Register the configured hotkey; fall back to backtick if it's invalid,
    so a bad .env edit can never leave dictation with no hotkey at all."""
    global HOTKEY, HOTKEY_MODE, press_tally
    try:
        if HOTKEY_MODE == "hold":
            keyboard.add_hotkey(HOTKEY, _hold_start)
            keyboard.on_release_key(hotkey_config.release_key(HOTKEY), _hold_stop)
        else:
            keyboard.add_hotkey(HOTKEY, toggle_recording)
        return
    except Exception as e:
        print(f"[Listener] Hotkey {HOTKEY!r} rejected ({e}); falling back to backtick.")
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Hotkey {HOTKEY!r} ({HOTKEY_MODE}) rejected: {e}; using backtick\n")
        except Exception:
            pass
    # Hold mode can fail halfway (hotkey added, release hook not): clear
    # everything so the fallback is the ONLY hotkey. Nothing else is hooked yet.
    keyboard.unhook_all()
    HOTKEY, HOTKEY_MODE = hotkey_config.DEFAULT_HOTKEY, "toggle"
    press_tally = hotkey_config.PressTally(HOTKEY, HOTKEY_MODE)
    keyboard.add_hotkey(HOTKEY, toggle_recording)


def _reset_after_hotkey_error():
    """Put recording state back to idle after a failed start/stop."""
    global recording_active, stream
    recording_active = False
    press_tally.presses = 0
    _button_state("idle")
    if current_target is not None:
        _end_style(current_target)
        current_target.release()
    if stream:
        try:
            stream.close()
        except Exception:
            pass
        stream = None


_toggle_lock = threading.Lock()


def toggle_recording(source="hotkey"):
    """Start/stop from the hotkey ("hotkey") or the floating mic button
    ("button"). Both can fire at once from different threads; one at a time."""
    with _toggle_lock:
        _toggle_recording(source)


def _toggle_recording(source):
    # This runs on the `keyboard` library's event-processing thread, and that
    # library does NOT catch exceptions from non-suppressing hotkeys: a single
    # raise (e.g. PortAudioError when the mic is unplugged/asleep) kills the
    # thread, and every later backtick press is silently queued forever while
    # the process stays alive -- so the watchdog never notices. Never let
    # anything escape from here.
    global last_active_time, current_target
    last_active_time = time.time()
    try:
        if recording_active:
            stop_recording(source)
        else:
            press_tally.started(source)
            current_target = Target(source, typing_key=press_tally.per_press > 0)
            # Before the beep + mic open, so a visible button turns red at
            # once (and isn't hidden as "the user is typing" by the hotkey).
            if caret_btn is not None:
                caret_btn.begin_style(None)   # the last recording's pill is gone
            _button_state("recording")
            start_recording()
            threading.Thread(target=_begin_style, args=(current_target, time.monotonic()),
                             name="style-pill", daemon=True).start()
    except Exception:
        err = traceback.format_exc()
        print(f"[Listener] Hotkey handler failed, hotkey stays alive:\n{err}")
        # pythonw discards stdout, so keep a file trail of what went wrong.
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{err}\n")
        except Exception:
            pass
        _reset_after_hotkey_error()
        try:
            winsound.Beep(300, 300)  # low buzz = "mic failed, press again"
        except Exception:
            pass

def mic_button_clicked():
    """The floating mic button was clicked (runs on a worker thread, so the
    beeps don't block the button's message loop)."""
    toggle_recording(source="button")


def start_caret_button():
    """Start the floating mic button thread. It reads FLOATING_MIC_BUTTON from
    .env itself (default on) and follows changes without a restart, so it is
    always started; when off it just idles. A failure never stops dictation."""
    global caret_btn
    try:
        import caret_button
        caret_btn = caret_button.CaretButton(
            on_click=mic_button_clicked, env_path=ENV_FILE, log_path=ERROR_LOG_PATH).start()
    except Exception:
        caret_btn = None
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Floating mic button failed to start:\n{traceback.format_exc()}\n")
        except Exception:
            pass


def _dpi_aware():
    """Crisp text in the overlay on high-DPI screens, as in Settings. Before
    any window exists. The floating mic button and the focus worker set their
    own per-monitor awareness per thread, so this doesn't change them; the
    keyboard hook doesn't care."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system aware
    except Exception:
        pass


if __name__ == "__main__":
    _dpi_aware()

    # Startup sound (Rising tone) — only on the first launch. The watchdog sets
    # WF_STARTUP_BEEP=0 on its periodic restarts so those stay silent.
    if os.environ.get("WF_STARTUP_BEEP", "1") == "1":
        winsound.Beep(500, 100)
        winsound.Beep(800, 100)

    # Initialize overlay
    overlay = LoadingOverlay()
    
    # Hotkey
    register_hotkey()
    action = "Hold" if HOTKEY_MODE == "hold" else "Press"
    print(f"[Listener] {action} {HOTKEY!r} to record (Max {MAX_DURATION_MINS} mins).")
    
    # Floating mic button at the text cursor (FLOATING_MIC_BUTTON in .env).
    start_caret_button()

    # Start robust idle monitor
    threading.Thread(target=idle_monitor, daemon=True).start()
    
    # Start GUI loop (replaces keyboard.wait)
    print("[Listener] Running...")
    overlay.start()
