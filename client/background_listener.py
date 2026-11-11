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
from tkinter import ttk
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
HOTKEY, HOTKEY_MODE = hotkey_config.from_env(os.environ)
# Characters the hotkey typed into the target app (backtick: 2, F9: 0).
HOTKEY_CLEANUP = hotkey_config.cleanup_backspaces(HOTKEY, HOTKEY_MODE)

# How long to let Chrome Remote Desktop push the local clipboard to the remote
# host after we nudge its window focus, before we send Ctrl+V. Tune via .env if
# your connection is slow.
CRD_SYNC_DELAY = float(os.environ.get("CRD_SYNC_DELAY_SECONDS", 0.6))

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


def copy_via_real_window(text, target_hwnd):
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
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # Hide initially
        self.root.overrideredirect(True)  # Frameless
        self.root.attributes('-topmost', True)  # Always on top
        self.root.attributes('-alpha', 0.9)  # Slight transparency
        
        # Style
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", background="#333", foreground="white", font=("Segoe UI", 12))
        style.configure("TFrame", background="#333")
        
        # Layout
        self.frame = ttk.Frame(self.root, padding=20)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        self.label = ttk.Label(self.frame, text="Transcribing...")
        self.label.pack(pady=(0, 10))
        
        self.progress = ttk.Progressbar(self.frame, mode='indeterminate', length=200)
        self.progress.pack()
        
        # Center the window
        self.center_window()

    def center_window(self):
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        self.root.geometry(f'+{x}+{y}')

    def show(self):
        # Reset the label every time we show, so a previous run's error message
        # ("No speech detected.", a long API error) never sticks around on the
        # next recording. Without this the label keeps the last text it was set
        # to, since successful runs never touch it.
        self.label.config(text="Transcribing...")
        self.root.deiconify()
        self.progress.start(10)
        self.center_window()

    def hide(self):
        self.progress.stop()
        self.root.withdraw()

    def start(self):
        self.root.mainloop()

    # Thread-safe wrappers
    def show_safe(self):
        self.root.after(0, self.show)

    def hide_safe(self):
        self.root.after(0, self.hide)

    def update_label(self, new_text):
        self.label.config(text=new_text)

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

def stop_recording():
    global recording_active, stream
    if not recording_active:
        return

    print("[Listener] Stopping recording...")
    recording_active = False
    if stream:
        stream.stop()
        stream.close()
    
    beep_stop()
    
    # Start processing in a separate thread so we don't block the hotkey/callback
    threading.Thread(target=save_and_transcribe, daemon=True).start()

def timeout_monitor():
    while recording_active:
        elapsed_mins = (time.time() - recording_start_time) / 60
        if elapsed_mins >= MAX_DURATION_MINS:
            print("[Listener] Timeout reached. Stopping.")
            stop_recording()
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

def save_and_transcribe():
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

        transcribe_and_paste()
    finally:
        # Hide overlay
        if overlay:
            overlay.hide_safe()

def transcribe_and_paste():
    print("[Listener] Sending to API...")
    try:
        # This PC, another PC, or a cloud service (TRANSCRIBE_SOURCE).
        # .env is re-read each time, so switching the source in Settings takes
        # effect on the next dictation, no listener restart needed.
        env = dict(os.environ, **{k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})
        text = transcribe_client.transcribe(WAV_OUTPUT_PATH, env, timeout=60)
        
        print(f"[Listener] Transcribed: {text}")
        
        if text:
            # Remember the window we're typing into, before we touch focus.
            target_hwnd = _user32.GetForegroundWindow()

            # Remove the characters the hotkey typed (two backticks; none
            # for F9 / Ctrl+Alt+Space), while the target window has focus.
            _remove_hotkey_chars()

            # Copy from a real off-screen window and bounce focus back to the
            # target, so Chrome Remote Desktop sees a real copy + focus-in and
            # syncs the clipboard to the remote machine. Falls back to a plain
            # clipboard write if the real-window copy fails.
            if not copy_via_real_window(text, target_hwnd):
                pyperclip.copy(text)
                _set_foreground(target_hwnd)

            # Give CRD time to push the clipboard across before we paste.
            time.sleep(CRD_SYNC_DELAY)

            keyboard.send('ctrl+v')
            print("[Listener] Pasted.")
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

def _remove_hotkey_chars():
    if HOTKEY_CLEANUP:
        time.sleep(0.1)
        keyboard.send(", ".join(["backspace"] * HOTKEY_CLEANUP))
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
    global HOTKEY, HOTKEY_MODE, HOTKEY_CLEANUP
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
    HOTKEY_CLEANUP = hotkey_config.cleanup_backspaces(HOTKEY, HOTKEY_MODE)
    keyboard.add_hotkey(HOTKEY, toggle_recording)


def _reset_after_hotkey_error():
    """Put recording state back to idle after a failed start/stop."""
    global recording_active, stream
    recording_active = False
    if stream:
        try:
            stream.close()
        except Exception:
            pass
        stream = None


def toggle_recording():
    # This runs on the `keyboard` library's event-processing thread, and that
    # library does NOT catch exceptions from non-suppressing hotkeys: a single
    # raise (e.g. PortAudioError when the mic is unplugged/asleep) kills the
    # thread, and every later backtick press is silently queued forever while
    # the process stays alive -- so the watchdog never notices. Never let
    # anything escape from here.
    global last_active_time
    last_active_time = time.time()
    try:
        if recording_active:
            stop_recording()
        else:
            start_recording()
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

if __name__ == "__main__":
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
    
    # Start robust idle monitor
    threading.Thread(target=idle_monitor, daemon=True).start()
    
    # Start GUI loop (replaces keyboard.wait)
    print("[Listener] Running...")
    overlay.start()
