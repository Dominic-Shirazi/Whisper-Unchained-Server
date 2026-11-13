"""Free the GPU while you play: pause the transcription server when a game is
running full screen, resume it when you stop. Off unless GAME_MODE=true.

Detection (checked every few seconds by the tray app):
- Windows' own "busy / full-screen Direct3D app" state
  (SHQueryUserNotificationState), which covers exclusive and most borderless
  full-screen games, unless the program in front is a browser or video
  player (full-screen YouTube shouldn't pause dictation);
- or any program named in GAME_PROCESSES (for games that run windowed).

Pausing only after GAME_START_SECONDS of play and resuming after
GAME_END_SECONDS without it keeps a quick alt-tab from flapping the model.
A pause YOU made from the tray is never auto-resumed.
"""
import ctypes
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ctypes import wintypes

import local_api  # noqa: E402
_API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")
if _API_DIR not in sys.path:
    sys.path.append(_API_DIR)
import secrets_store  # noqa: E402  (api/secrets_store.py: keys may be stored encrypted)

GAME_START_SECONDS = 15
GAME_END_SECONDS = 10

# Full screen but not a game: never pause for these.
NOT_GAMES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
    "vlc.exe", "mpc-hc64.exe", "mpc-hc.exe", "mpv.exe", "potplayermini64.exe",
    "wmplayer.exe", "microsoft.media.player.exe", "video.ui.exe", "powerpnt.exe",
    "netflix.exe", "explorer.exe", "applicationframehost.exe",
}

QUNS_BUSY = 2                     # a full-screen app is running
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # exclusive full-screen Direct3D


def parse_list(raw):
    """'Game.exe, other' -> {'game.exe', 'other.exe'}"""
    names = (p.strip().strip('"').lower() for p in (raw or "").split(","))
    return {n if n.endswith(".exe") else n + ".exe" for n in names if n}


def notification_state():
    state = ctypes.c_int(0)
    try:
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    except Exception:
        return 0
    return state.value


def foreground_exe():
    """File name of the program whose window is in front, lowercase ('' if unknown)."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def running_exes():
    """Lowercase names of every running program (for GAME_PROCESSES)."""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=10, creationflags=0x08000000).stdout
        return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
    except Exception:
        return set()


def is_gaming(extra_games=frozenset(), state=None, front=None, running=None):
    """True if a game appears to be running. Arguments are injectable for tests."""
    if extra_games:
        procs = running if running is not None else running_exes()
        if procs & set(extra_games):
            return True
    st = notification_state() if state is None else state
    if st not in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN):
        return False
    exe = foreground_exe() if front is None else front
    return exe not in NOT_GAMES


class Tracker:
    """Turns 'is a game running right now?' samples into pause/resume actions,
    with the start/end delays. Pure logic, no I/O."""

    def __init__(self, start_after=GAME_START_SECONDS, end_after=GAME_END_SECONDS):
        self.start_after, self.end_after = start_after, end_after
        self.gaming_since = None
        self.clear_since = None

    def step(self, gaming, now, paused_reason):
        """paused_reason: what the server reports (None, 'user' or 'game').
        Returns 'pause', 'resume' or None."""
        if gaming:
            self.clear_since = None
            if self.gaming_since is None:  # 'is None': a start time of 0 is valid
                self.gaming_since = now
            if paused_reason is None and now - self.gaming_since >= self.start_after:
                return "pause"
        else:
            self.gaming_since = None
            if self.clear_since is None:
                self.clear_since = now
            if paused_reason == "game" and now - self.clear_since >= self.end_after:
                return "resume"
        return None


# --- talking to the server -------------------------------------------------------

def _request(env, path, payload=None, timeout=10):
    req = urllib.request.Request(
        local_api.base_url(env) + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    key = (secrets_store.reveal(env.get("WHISPER_API_KEY"), "WHISPER_API_KEY") or "").strip()
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout, context=local_api.ssl_context(env)) as r:
        return json.load(r)


def server_state(env):
    """(reachable, paused_reason) from /health."""
    try:
        h = _request(env, "/health", timeout=3)
        return True, (h.get("paused_reason") if h.get("status") == "paused" else None)
    except Exception:
        return False, None


def pause(env, reason):
    return _request(env, "/v1/pause", {"reason": reason}, timeout=60)


def resume(env):
    return _request(env, "/v1/resume", {}, timeout=60)
