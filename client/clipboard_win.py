"""Put text on the Windows clipboard, optionally marked "don't keep in history".

Windows clipboard history (Win+V) and cloud clipboard skip a copy that carries
these registered formats; password managers use the same ones:
  ExcludeClipboardContentFromMonitorProcessing  (presence = skip it)
  CanIncludeInClipboardHistory = 0
  CanUploadToCloudClipboard    = 0
Used when Settings > Hotkey > "Keep dictations out of clipboard history" is on.
"""
import ctypes
import time
from ctypes import wintypes

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PRIVATE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing",
                   "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")

_u = ctypes.windll.user32
_k = ctypes.windll.kernel32
_u.OpenClipboard.argtypes = [wintypes.HWND]
_u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_u.SetClipboardData.restype = wintypes.HANDLE
_u.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_u.RegisterClipboardFormatW.restype = wintypes.UINT
_u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_u.CreateWindowExW.restype = wintypes.HWND
_u.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_u.DestroyWindow.argtypes = [wintypes.HWND]
_k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_k.GlobalAlloc.restype = wintypes.HGLOBAL
_k.GlobalLock.argtypes = [wintypes.HGLOBAL]
_k.GlobalLock.restype = ctypes.c_void_p
_k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


def _global(data: bytes):
    h = _k.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise OSError("GlobalAlloc failed")
    p = _k.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    _k.GlobalUnlock(h)
    return h


def _open(owner, tries=20):
    for _ in range(tries):
        if _u.OpenClipboard(owner):
            return
        time.sleep(0.02)   # another app has it open for a moment
    raise OSError("The clipboard is busy.")


def set_text(text, private=False, owner=None):
    """Replace the clipboard with `text`. `owner` is the window that will own
    the clipboard; without one, a hidden window is made for the moment (with
    no owner Windows can refuse the data)."""
    temp = None
    if owner is None:
        temp = owner = _u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                          None, None, None, None)
    try:
        _open(owner)
        try:
            _u.EmptyClipboard()
            # Ownership of the memory passes to the clipboard once it's set.
            _u.SetClipboardData(CF_UNICODETEXT, _global((text + "\0").encode("utf-16-le")))
            if private:
                zero = (0).to_bytes(4, "little")
                for name in PRIVATE_FORMATS:
                    _u.SetClipboardData(_u.RegisterClipboardFormatW(name), _global(zero))
        finally:
            _u.CloseClipboard()
    finally:
        if temp:
            _u.DestroyWindow(temp)


def is_private_now():
    """True if what's on the clipboard carries the 'skip history' marker."""
    return bool(_u.IsClipboardFormatAvailable(
        _u.RegisterClipboardFormatW(PRIVATE_FORMATS[0])))


def wanted(env):
    return (env.get("CLIPBOARD_PRIVATE") or "").strip().lower() in ("1", "true", "yes", "on")
