"""Floating mic button at the text cursor (like the phone app's floating mic).

When you click or tab into a text field anywhere in Windows, a small round mic
button pops up beside the text caret. Click it to start dictating (it turns
red with a stop square), click again to stop; the listener pastes the result
just like the hotkey. Right-click for "Hide for now" / "Turn off".

States are shown by SHAPE as well as colour (Dom is red/green colour blind):
  idle      teal circle, white microphone
  recording red circle, white stop square
  working   grey circle, three dots

Runs in its own thread with its own Win32 layered window and message loop
(ctypes, not Tk). The window is WS_EX_NOACTIVATE and answers WM_MOUSEACTIVATE
with MA_NOACTIVATE, so clicking it never takes focus from the text field.

Trigger detection (cheap when idle):
  * SetWinEventHook(EVENT_OBJECT_FOCUS), out of context: keyboard focus moved.
  * A 50 ms timer watches the left mouse button (GetAsyncKeyState); on release
    the focused element is checked and the button shown if the click landed
    inside an editable field. UI Automation is only queried after one of
    those two events, never on a blind poll.
  * While the button is up, GetLastInputInfo + an unmoved pointer means the
    user is typing -> hide.

.env: FLOATING_MIC_BUTTON (default true). Re-read when the file's mtime moves.
"""
import ctypes
import os
import threading
import time
import traceback
from ctypes import wintypes

ENV_KEY = "FLOATING_MIC_BUTTON"
ENV_DEFAULT = True

# Look
BASE_SIZE = 36            # circle diameter at 100% scaling (96 DPI)
SUPERSAMPLE = 4
COLORS = {
    "idle": (0x2F, 0xB7, 0x9A),
    "recording": (0xD9, 0x36, 0x3E),
    "working": (0x5A, 0x60, 0x6B),
}

# Timing (seconds)
FULL_SECS = 2.0           # fully visible
FADED_SECS = 3.0          # then see-through for this long, then hidden
FADED_ALPHA = 0.35
RAMP_SECS = 0.25          # length of the fade from full to faded
TYPING_GRACE = 0.35       # input this soon after showing is the click/Tab itself
TYPING_HIDE_DELAY = 0.30  # a hotkey press may be about to start a recording
TICK_MS = 50
DISABLED_TICK_MS = 1000
ENV_POLL_SECS = 2.0


# --- pure logic (unit-tested) ---------------------------------------------

def place(anchor, d, work, gap, at_element=False):
    """Top-left of a d x d button next to `anchor` (l, t, r, b), kept inside
    `work` (the monitor work area).

    Caret: just right of it, vertically centred on the line; flipped to the
    left side when there's no room on the right.
    Element fallback (no caret found): just inside the field's right edge.
    """
    l, t, r, b = anchor
    wl, wt, wr, wb = work
    if at_element:
        x = r - d - gap
        h = b - t
        y = (t + b) / 2 - d / 2 if h < d * 2.5 else t + gap
    else:
        x = r + gap
        if x + d > wr:
            x = l - gap - d
        y = (t + b) / 2 - d / 2
    x = max(wl, min(x, wr - d))
    y = max(wt, min(y, wb - d))
    return int(round(x)), int(round(y))


class FadeTimeline:
    """When is the button fully visible, see-through, or hidden.

    Time is passed in (seconds, monotonic), so tests can drive it.
    alpha(now) -> 1.0 full, FADED_ALPHA see-through, 0.0 hidden.
    """

    def __init__(self, full=FULL_SECS, faded=FADED_SECS, faded_alpha=FADED_ALPHA,
                 ramp=RAMP_SECS):
        self.full, self.faded, self.faded_alpha, self.ramp = full, faded, faded_alpha, ramp
        self.shown_at = None
        self.hover = False
        self.pinned = False

    @property
    def visible(self):
        return self.shown_at is not None

    def show(self, now):
        self.shown_at = now

    def hide(self):
        self.shown_at = None
        self.hover = False

    def set_hover(self, hover, now):
        self.hover = hover
        if self.visible:
            self.shown_at = now   # leaving restarts the 2 s + 3 s countdown

    def pin(self, pinned, now):
        self.pinned = pinned
        if self.visible and not pinned:
            self.shown_at = now

    def alpha(self, now):
        if self.shown_at is None:
            return 0.0
        if self.pinned or self.hover:
            return 1.0
        t = now - self.shown_at
        if t < self.full:
            return 1.0
        if t < self.full + self.ramp:
            k = (t - self.full) / self.ramp
            return 1.0 - (1.0 - self.faded_alpha) * k
        if t < self.full + self.faded:
            return self.faded_alpha
        self.shown_at = None
        return 0.0


def lighten(rgb, k=0.12):
    return tuple(int(round(c + (255 - c) * k)) for c in rgb)


def darken(rgb, k=0.22):
    return tuple(int(round(c * (1 - k))) for c in rgb)


_ICON_FONTS = (r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf")
_MIC_GLYPH = "\uE720"


def _icon_font(px):
    from PIL import ImageFont
    for path in _ICON_FONTS:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, px)
        except Exception:
            continue
    return None


def margin_for(d):
    return max(3, int(round(d * 0.2)))


def render(state, hover, d):
    """RGBA Pillow image: a d-px circle (plus shadow margin) for `state`."""
    from PIL import Image, ImageDraw, ImageFilter
    s = SUPERSAMPLE
    m = margin_for(d)
    W = (d + 2 * m) * s
    D = d * s
    o = m * s  # circle top-left inside the canvas
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # Soft shadow, slightly below the circle.
    sh = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((o, o + s * 1.5, o + D, o + D + s * 1.5), fill=(0, 0, 0, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(s * max(1.5, d / 16)))
    img.alpha_composite(sh)

    base = COLORS.get(state, COLORS["idle"])
    fill = lighten(base) if hover else base
    dr = ImageDraw.Draw(img)
    dr.ellipse((o, o, o + D - 1, o + D - 1), fill=fill + (255,),
               outline=darken(base) + (255,), width=s)

    cx = cy = o + D / 2
    white = (255, 255, 255, 255)
    if state == "recording":
        half = D * 0.17
        dr.rounded_rectangle((cx - half, cy - half, cx + half, cy + half),
                             radius=D * 0.05, fill=white)
    elif state == "working":
        r = D * 0.065
        for dx in (-D * 0.2, 0, D * 0.2):
            dr.ellipse((cx + dx - r, cy - r, cx + dx + r, cy + r), fill=white)
    else:
        font = _icon_font(int(D * 0.48))
        if font is not None:
            box = dr.textbbox((0, 0), _MIC_GLYPH, font=font)
            gw, gh = box[2] - box[0], box[3] - box[1]
            dr.text((cx - gw / 2 - box[0], cy - gh / 2 - box[1]), _MIC_GLYPH,
                    font=font, fill=white)
        else:
            _draw_mic_shapes(dr, cx, cy, D, white)
    return img.resize((d + 2 * m, d + 2 * m), Image.LANCZOS)


def _draw_mic_shapes(dr, cx, cy, D, color):
    """A microphone from plain shapes (when no icon font is installed)."""
    w = D * 0.16
    top, bot = cy - D * 0.24, cy + D * 0.05
    dr.rounded_rectangle((cx - w, top, cx + w, bot), radius=w, fill=color)
    lw = max(2, int(D * 0.045))
    r = D * 0.25
    dr.arc((cx - r, cy - D * 0.2 - r * 0.2, cx + r, cy + D * 0.13), 20, 160, fill=color, width=lw)
    dr.line((cx, cy + D * 0.13, cx, cy + D * 0.24), fill=color, width=lw)
    dr.line((cx - D * 0.1, cy + D * 0.24, cx + D * 0.1, cy + D * 0.24), fill=color, width=lw)


def premultiplied_bgra(img):
    """Pillow RGBA -> bytes in the premultiplied BGRA that UpdateLayeredWindow wants."""
    import numpy as np
    a = np.asarray(img, dtype=np.uint16)
    alpha = a[..., 3:4]
    rgb = (a[..., :3] * alpha + 127) // 255
    out = np.empty(a.shape, dtype=np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = a[..., 3]
    return out.tobytes()


# --- Win32 ---------------------------------------------------------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                ("lPrivate", wintypes.DWORD)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                       ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSEACTIVATE = 0x0021
WM_MOUSELEAVE = 0x02A3
WM_NULL = 0x0000
WM_SETCURSOR = 0x0020
MA_NOACTIVATE = 3
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1
EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
VK_LBUTTON = 0x01
TME_LEAVE = 0x2
MF_STRING = 0x0
MF_SEPARATOR = 0x800
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2
IDC_HAND = 32649
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MENU_HIDE = 1
MENU_TURN_OFF = 2

_CLASS_NAME = "WhisperUnchainedCaretMic"


def _log_error(log_path, what):
    try:
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("--- %s\n[caret button] %s\n%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), what, traceback.format_exc()))
    except Exception:
        pass


def _process_image(pid):
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _force_foreground(hwnd):
    """SetForegroundWindow that works from a background thread: attach to the
    current foreground thread's input queue for the moment of the switch."""
    try:
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_tid, False)
    except Exception:
        pass


class CaretButton:
    """The floating mic button. `on_click()` is called on a worker thread.

    Thread-safe from outside: set_state("idle" | "recording" | "working"),
    stop(). Everything else runs on the button's own thread.
    """

    def __init__(self, on_click, env_path=None, log_path=None, enabled=None, log=None):
        self.on_click = on_click
        self.env_path = env_path
        self.log_path = log_path
        self._forced_enabled = enabled   # tests/demo: ignore .env
        self._log = log or (lambda msg: None)
        self.hwnd = None
        self.thread = None
        self._state = "idle"             # requested by the listener
        self._seen_state = "idle"        # last state the tick acted on
        self._drawn = None               # (state, hover, size, alpha) last painted
        self.timeline = FadeTimeline()
        self._size = BASE_SIZE
        self._pos = (0, 0)
        self._enabled = True
        self._pending = None             # (due, reason, click_pt)
        self._pending_hide = None
        self._lbutton = False
        self._down_pt = None
        self._down_own = False
        self._last_input = 0
        self._input_cursor = None
        self._shown_mono = 0.0
        self._next_env_poll = 0.0
        self._next_topmost = 0.0
        self._hook = None
        self._owner = None
        self._menu_open = False
        self._focus_key = None
        self._fg = None
        self._timer_ms = None
        self._mem_dc = None
        self._bmp = None
        self._old_bmp = None
        self._bmp_key = None
        self._inspector = None
        self._own_pid = os.getpid()
        self._ready = threading.Event()
        # Keep ctypes callbacks alive for the window's lifetime.
        self._wndproc = WNDPROC(self._wnd_proc)
        self._eventproc = WINEVENTPROC(self._on_win_event)

    # -- public, any thread ---------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._run, name="caret-button", daemon=True)
        self.thread.start()
        self._ready.wait(5)
        return self

    def set_state(self, state):
        """Called by the listener: "recording", "working" or "idle" (done)."""
        self._state = state
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_TIMER, 1, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # -- thread ----------------------------------------------------------
    def _run(self):
        try:
            try:
                user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            except Exception:
                pass
            import text_focus
            self._inspector = text_focus.FocusInspector()
            self._create_window()
        except Exception:
            _log_error(self.log_path, "start failed")
            self._ready.set()
            return
        self._ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        wc.lpszClassName = _CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))  # fails harmlessly if registered
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, _CLASS_NAME, "Whisper Unchained mic", WS_POPUP,
                                           0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        self._mem_dc = gdi32.CreateCompatibleDC(None)
        self._apply_enabled(self._read_enabled(), force=True)

    def _menu_owner(self):
        if not self._owner:
            hinst = kernel32.GetModuleHandleW(None)
            self._owner = user32.CreateWindowExW(WS_EX_TOOLWINDOW, _CLASS_NAME, "", WS_POPUP,
                                                 -32000, -32000, 1, 1, None, None, hinst, None)
        return self._owner

    # -- enable / disable -----------------------------------------------
    def _read_enabled(self):
        if self._forced_enabled is not None:
            return self._forced_enabled
        if not self.env_path:
            return ENV_DEFAULT
        if not hasattr(self, "_watcher"):
            import env_file
            self._watcher = env_file.Watcher(self.env_path, ENV_KEY, ENV_DEFAULT)
        return self._watcher.poll()

    def _apply_enabled(self, enabled, force=False):
        if enabled == self._enabled and not force:
            return
        self._enabled = enabled
        if enabled:
            if not self._hook:
                self._hook = user32.SetWinEventHook(
                    EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, None, self._eventproc, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            self._set_timer(TICK_MS)
        else:
            if self._hook:
                user32.UnhookWinEvent(self._hook)
                self._hook = None
            self._hide()
            self._set_timer(DISABLED_TICK_MS)
        self._log("enabled=%s" % enabled)

    def _set_timer(self, ms):
        if ms != self._timer_ms:
            user32.SetTimer(self.hwnd, 1, ms, None)
            self._timer_ms = ms

    # -- window procedure -----------------------------------------------
    def _wnd_proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_MOUSEMOVE:
                if not self.timeline.hover:
                    self.timeline.set_hover(True, time.monotonic())
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self._paint()
                return 0
            if msg == WM_MOUSELEAVE:
                self.timeline.set_hover(False, time.monotonic())
                self._paint()
                return 0
            if msg == WM_LBUTTONDOWN:
                return 0
            if msg == WM_LBUTTONUP:
                self._clicked()
                return 0
            if msg == WM_RBUTTONUP:
                self._context_menu()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                if self._hook:
                    user32.UnhookWinEvent(self._hook)
                    self._hook = None
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            _log_error(self.log_path, "window message %#x" % msg)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _on_win_event(self, hook, event, hwnd, id_object, id_child, thread, ms):
        # Called from our message loop. Defer the (slower) UIA query to the
        # timer so a burst of focus events costs one lookup.
        try:
            if not self._enabled or not hwnd:
                return
            self._pending = (time.monotonic() + 0.06, "focus", None)
        except Exception:
            pass

    # -- clicks ----------------------------------------------------------
    def _clicked(self):
        if self._state == "working":
            return
        now = time.monotonic()
        if self._state == "idle":
            # Show the red stop square right away; the listener confirms.
            self._state = "recording"
        self.timeline.show(now)
        self.timeline.pin(True, now)
        self._paint()

        def run():
            try:
                self.on_click()
            except Exception:
                _log_error(self.log_path, "click handler")
        threading.Thread(target=run, name="caret-button-click", daemon=True).start()

    def _context_menu(self):
        """Right-click menu. A popup menu only works (and closes when you click
        elsewhere) if its owner is the foreground window, and our button can't
        be foreground (WS_EX_NOACTIVATE). So a hidden, ordinary owner window
        borrows the foreground for the menu, then hands it straight back to
        the window you were typing in."""
        cmd = 0
        menu = user32.CreatePopupMenu()
        prev = user32.GetForegroundWindow()
        self._menu_open = True
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_HIDE, "Hide for now")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_TURN_OFF, "Turn off the floating mic button")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            owner = self._menu_owner()
            _force_foreground(owner)
            cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                        pt.x, pt.y, 0, owner, None)
            user32.PostMessageW(owner, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
            if prev:
                _force_foreground(prev)
            self._menu_open = False
            self._pending = None
        if cmd == MENU_HIDE:
            if self._state == "idle":
                self._hide()
        elif cmd == MENU_TURN_OFF:
            if self.env_path:
                try:
                    import env_file
                    env_file.set_key(self.env_path, ENV_KEY, "false")
                except Exception:
                    _log_error(self.log_path, "writing .env")
            self._forced_enabled = None if self._forced_enabled is None else False
            self._apply_enabled(False)

    # -- the 50 ms tick ----------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        if now >= self._next_env_poll:
            self._next_env_poll = now + ENV_POLL_SECS
            self._apply_enabled(self._read_enabled())
        if not self._enabled or self._menu_open:
            return

        fg = user32.GetForegroundWindow()
        if fg != self._fg:
            self._fg = fg
            self._focus_key = None   # back in a window: its field may show again
        self._follow_state(now)
        self._watch_mouse(now)
        self._watch_typing(now)

        if self._pending and now >= self._pending[0]:
            _, reason, pt = self._pending
            self._pending = None
            self._check(reason, pt)
        if self.timeline.visible and now >= self._next_topmost:
            # Another always-on-top window (activated by the click that showed
            # us) can land above the button; keep it on top while it's up.
            self._next_topmost = now + 0.5
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if self._pending_hide and now >= self._pending_hide:
            self._pending_hide = None
            if self._state == "idle":
                self._hide()
        self._paint()

    def _follow_state(self, now):
        state = self._state
        prev, self._seen_state = self._seen_state, state
        if state == prev:
            return
        if state == "idle":
            # Recording / transcription finished: done -> hide.
            self.timeline.pin(False, now)
            self._hide()
        else:
            self._pending_hide = None
            if self.timeline.visible:
                self.timeline.pin(True, now)

    def _watch_mouse(self, now):
        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if down and not self._lbutton:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            self._down_pt = (pt.x, pt.y)
            hit = user32.WindowFromPoint(pt)
            self._down_own = bool(hit) and hit == self.hwnd
        elif not down and self._lbutton:
            if not self._down_own and self._down_pt:
                self._pending = (now + 0.08, "click", self._down_pt)
        self._lbutton = down

    def _watch_typing(self, now):
        lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return
        changed = lii.dwTime != self._last_input
        self._last_input = lii.dwTime
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cursor = (pt.x, pt.y)
        moved = cursor != self._input_cursor
        self._input_cursor = cursor
        if (changed and not moved and not self._lbutton and self.timeline.visible
                and self._state == "idle" and not self.timeline.hover
                and now - self._shown_mono > TYPING_GRACE and not self._pending_hide):
            # Keyboard input while the button is up: the user is typing.
            # Wait a moment: it may be the hotkey starting a recording.
            self._pending_hide = now + TYPING_HIDE_DELAY

    # -- show / hide ---------------------------------------------------------
    def _own_window(self, hwnd):
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == self._own_pid:
            return True
        # Settings / History / file windows: Tk windows run by our Python.
        import text_focus
        cls = text_focus.window_class(hwnd)
        if cls.startswith("Tk"):
            exe = os.path.basename(_process_image(pid.value)).lower()
            return exe.startswith("python")
        return False

    def _check(self, reason, click_pt):
        if self._state == "working":
            return
        fg = user32.GetForegroundWindow()
        if not fg or self._own_window(fg):
            if self._state == "idle":
                self._hide()
            return
        info = self._inspector.inspect()
        self._log("check %s: %r" % (reason, info))
        if info is None or not info.ok or not info.caret:
            self._focus_key = None
            if self._state == "idle":
                self._hide()
            return
        key = (info.hwnd, info.element, info.control_type, info.name)
        if reason == "focus" and key == self._focus_key:
            return   # a repeat focus event for the field we already handled
        self._focus_key = key
        if reason == "click" and click_pt and info.element:
            l, t, r, b = info.element
            x, y = click_pt
            if not (l - 4 <= x <= r + 4 and t - 4 <= y <= b + 4):
                return   # clicked elsewhere (toolbar, scrollbar...): leave it
        self._show_at(info.caret, info.caret_source == "element")

    def _show_at(self, anchor, at_element):
        cx = (anchor[0] + anchor[2]) // 2
        cy = (anchor[1] + anchor[3]) // 2
        work, dpi = self._monitor(cx, cy)
        self._size = max(24, int(round(BASE_SIZE * dpi / 96)))
        d = self._size
        x, y = place(anchor, d, work, gap=max(4, d // 5), at_element=at_element)
        m = margin_for(d)
        self._pos = (x - m, y - m)
        now = time.monotonic()
        self._shown_mono = now
        self._pending_hide = None
        self.timeline.show(now)
        if self._state != "idle":
            self.timeline.pin(True, now)
        self._drawn = None  # force repaint at the new place
        self._paint()

    def _hide(self):
        self.timeline.hide()
        self._pending_hide = None
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_HIDE)
        self._drawn = None

    def _monitor(self, x, y):
        work, dpi = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)), 96
        try:
            hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                w = mi.rcWork
                work = (w.left, w.top, w.right, w.bottom)
            dx, dy = wintypes.UINT(), wintypes.UINT()
            if ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                dpi = dx.value or 96
        except Exception:
            pass
        return work, dpi

    # -- drawing ---------------------------------------------------------------
    def _paint(self):
        now = time.monotonic()
        alpha = self.timeline.alpha(now)
        if alpha <= 0:
            if self._drawn is not None:
                self._hide()
            return
        state = self._state if self._state in COLORS else "idle"
        key = (state, self.timeline.hover, self._size, int(alpha * 255), self._pos)
        if key == self._drawn:
            return
        first = self._drawn is None
        self._update_layered(state, self.timeline.hover, int(alpha * 255))
        if first:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self._pos[0], self._pos[1], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._drawn = key

    def _update_layered(self, state, hover, alpha):
        bkey = (state, hover, self._size)
        img_size = self._size + 2 * margin_for(self._size)
        if bkey != self._bmp_key:
            img = render(state, hover, self._size)
            data = premultiplied_bgra(img)
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = img_size
            bmi.biHeight = -img_size  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bits = ctypes.c_void_p()
            bmp = gdi32.CreateDIBSection(self._mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not bmp:
                return
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(self._mem_dc, bmp)
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
            else:
                self._old_bmp = old
            self._bmp = bmp
            self._bmp_key = bkey
        screen = user32.GetDC(None)
        try:
            blend = BLENDFUNCTION(0, 0, max(1, min(255, alpha)), AC_SRC_ALPHA)
            pos = wintypes.POINT(*self._pos)
            size = wintypes.SIZE(img_size, img_size)
            src = wintypes.POINT(0, 0)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pos), ctypes.byref(size),
                                       self._mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)
