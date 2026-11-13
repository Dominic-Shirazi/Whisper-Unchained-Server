"""Where is the text cursor, and is it somewhere we may offer dictation?

Used by the floating mic button (caret_button.py), by "voice-edit the
highlighted text" (`selection_to_edit`), and by the listener to go back to the
box a recording started in before pasting (`capture_spot` / `go_back`, at the
end of this file; they run on the single `worker()` thread).

Everything here fails soft: any COM / Win32 problem returns None (or ""),
never an exception, because it runs inside the dictation listener.

COM must be initialised in the thread that uses it, so make one
`FocusInspector` per thread (it calls CoInitializeEx itself).

Caret lookup order (first that works wins):
  1. Win32 caret: GetGUIThreadInfo -> rcCaret -> ClientToScreen
     (classic Edit / RichEdit controls, most Win32 apps).
  2. MSAA: AccessibleObjectFromWindow(hwnd, OBJID_CARET).accLocation
     (Chrome / Edge / Electron expose their caret this way).
  3. UIA: TextPattern2.GetCaretRange -> GetBoundingRectangles
     (WinUI / XAML apps, modern Notepad).
  4. The focused element's bounding rectangle (button goes at its right edge).
"""
import ctypes
import os
import queue
import re
import threading
import time
from ctypes import wintypes

# --- pure heuristics (unit-tested; no Windows calls) -----------------------

# Phrases that mark a login / username field wherever they appear in a label.
_LOGIN_PHRASES = re.compile(
    r"\b(user\s*name|user\s*id|userid|log\s*-?\s*in|log\s*-?\s*on|logon|"
    r"sign\s*-?\s*in|signin|account\s*name|login\s*name|"
    r"e-?mail\s*(address\s*)?(,|or|/)\s*(phone|user)|"
    r"(phone|user\s*name)\s*(number\s*)?(,|or|/)\s*e-?mail|"
    r"e-?mail\s*,\s*phone|passcode|password|passwd|one[-\s]?time\s*code)\b",
    re.I)

# Words that only count when they are (nearly) the whole label: "User",
# "Email address" -- but not "Search users" or "Email body".
_LOGIN_WHOLE = {
    "user", "username", "email", "e-mail", "email address", "e-mail address",
    "your email", "your email address", "enter your email", "account", "id",
    "phone or email", "email or phone", "login", "user id", "member id",
}

# Fragments that mark a login field inside an AutomationId / HTML id
# ("loginfmt", "login_field", "identifierId", "user_login", "ap_email").
_LOGIN_ID_PARTS = ("username", "userid", "user_id", "user-name", "login", "logon",
                   "signin", "sign_in", "sign-in", "identifier", "email", "passw",
                   "account_name", "accountname", "session_key", "otp")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("*", "").replace(":", "")).strip().lower()


def looks_like_login(name="", automation_id="", help_text="", placeholder=""):
    """True when a field's labels say it's a username / email / login box."""
    for label in (name, help_text, placeholder):
        n = _norm(label)
        if not n:
            continue
        if n in _LOGIN_WHOLE or _LOGIN_PHRASES.search(n):
            return True
    aid = (automation_id or "").lower()
    if aid and any(p in aid for p in _LOGIN_ID_PARTS):
        return True
    return False


# Win32 edit styles.
ES_READONLY = 0x0800
ES_PASSWORD = 0x0020
_EDIT_CLASSES = ("edit", "richedit", "richedit20a", "richedit20w", "richedit50w",
                 "richeditd2dpt", "tmemo", "tedit")

# UIA control types.
CT_EDIT = 50004
CT_DOCUMENT = 50030
CT_COMBOBOX = 50003
# Group, Custom, Pane: may be a contenteditable / custom rich-text editor.
CT_RICH_CANDIDATES = (50026, 50025, 50033)


def is_edit_class(cls):
    c = (cls or "").lower()
    return c in _EDIT_CLASSES or c.startswith("richedit") or c.startswith("windowsforms10.edit")


def decide(control_type=None, is_password=False, read_only=None, has_text=False,
           win32_class="", win32_style=0, has_win32_caret=False, login=False,
           enabled=True):
    """Should the mic button be offered for this focused element?

    Returns (ok, reason). Pure: every input is a plain value.
      read_only: ValuePattern.IsReadOnly, or None when there's no ValuePattern.
      has_text:  the element supports TextPattern.
    """
    if is_password:
        return False, "password"
    if is_edit_class(win32_class) and win32_style & ES_PASSWORD:
        return False, "password"
    if login:
        return False, "login"
    if not enabled:
        return False, "disabled"
    if is_edit_class(win32_class) and win32_style & ES_READONLY:
        return False, "read-only"
    if read_only is True:
        return False, "read-only"
    if control_type == CT_EDIT:
        return True, "edit"
    if control_type == CT_DOCUMENT:
        # A web page or PDF is a read-only Document; an editor is not.
        if read_only is False or has_win32_caret:
            return True, "document"
        return False, "document not editable"
    if read_only is False and has_text:
        return True, "editable text"  # contenteditable, custom editors
    if is_edit_class(win32_class) and has_win32_caret:
        return True, "win32 edit"
    return False, "not a text field"


def rect_ok(r, min_h=1):
    """A usable screen rectangle (l, t, r, b)."""
    return (r is not None and len(r) == 4 and r[2] >= r[0] and r[3] - r[1] >= min_h
            and abs(r[0]) < 100000 and abs(r[1]) < 100000)


def caret_inside(caret, elem, slack=8):
    """Does the caret rect sit inside the element rect (with a little slack)?
    A caret far outside the focused field is stale, so the caller ignores it."""
    if not rect_ok(elem):
        return True
    cx = (caret[0] + caret[2]) / 2
    cy = (caret[1] + caret[3]) / 2
    return (elem[0] - slack <= cx <= elem[2] + slack
            and elem[1] - slack <= cy <= elem[3] + slack)


# --- Win32 ---------------------------------------------------------------

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


if user32 is not None:
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long

GWL_STYLE = -16
OBJID_CARET = -8
GUI_CARETBLINKING = 0x1


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    try:
        user32.GetClassNameW(hwnd, buf, 256)
    except Exception:
        return ""
    return buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    except Exception:
        return 0
    return pid.value


def gui_thread_info(hwnd=None):
    """GUITHREADINFO of the thread owning `hwnd` (default: foreground)."""
    try:
        hwnd = hwnd or user32.GetForegroundWindow()
        if not hwnd:
            return None
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return None
        return info
    except Exception:
        return None


def win32_caret(info):
    """Screen rect of the classic Win32 caret, or None."""
    try:
        if not info or not info.hwndCaret:
            return None
        rc = info.rcCaret
        if rc.right == rc.left and rc.bottom == rc.top:
            return None
        pt = wintypes.POINT(rc.left, rc.top)
        user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
        r = (pt.x, pt.y, pt.x + max(1, rc.right - rc.left), pt.y + (rc.bottom - rc.top))
        return r if rect_ok(r) else None
    except Exception:
        return None


# --- COM (UIA + MSAA) ------------------------------------------------------

class FocusInfo:
    __slots__ = ("ok", "reason", "hwnd", "caret", "element", "caret_source",
                 "control_type", "name")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return "FocusInfo(%s)" % ", ".join("%s=%r" % (k, getattr(self, k)) for k in self.__slots__)


class FocusInspector:
    """UI Automation + MSAA queries. Create and use in ONE thread."""

    def __init__(self):
        self._uia = None
        self._U = None
        self._acc = None
        self._failed = False
        self._tid = threading.get_ident()

    def _init(self):
        if self._uia is not None or self._failed:
            return self._uia is not None
        try:
            import comtypes
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            except OSError:
                pass  # already initialised in this thread (maybe as MTA): fine
            import comtypes.client as cc
            cc.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as U
            self._U = U
            try:
                # CUIAutomation8 (Windows 8+) lets us shorten the timeouts so a
                # hung app can't freeze the button thread (defaults 2 s / 20 s).
                uia2 = cc.CreateObject(U.CUIAutomation8, interface=U.IUIAutomation2)
                uia2.ConnectionTimeout = 1000
                uia2.TransactionTimeout = 1500
                self._uia = uia2
            except Exception:
                self._uia = cc.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
            try:
                cc.GetModule("oleacc.dll")
                from comtypes.gen import Accessibility
                self._acc = Accessibility
            except Exception:
                self._acc = None
            return True
        except Exception:
            self._failed = True
            return False

    # -- element queries --------------------------------------------------
    def focused_element(self):
        if not self._init():
            return None
        try:
            return self._uia.GetFocusedElement()
        except Exception:
            return None

    def _pattern(self, el, pid, iface):
        try:
            p = el.GetCurrentPattern(pid)
            if not p:
                return None
            return p.QueryInterface(iface)
        except Exception:
            return None

    def _rect(self, el):
        try:
            r = el.CurrentBoundingRectangle
            rr = (r.left, r.top, r.right, r.bottom)
            return rr if rect_ok(rr) else None
        except Exception:
            return None

    def _parent_has_password(self, el, levels=3):
        """Does a nearby container (form / group) hold a password field?"""
        U = self._U
        try:
            walker = self._uia.ControlViewWalker
            cond = self._uia.CreatePropertyCondition(U.UIA_IsPasswordPropertyId, True)
            if self._sibling_has_password(el, walker, cond):
                return True
            node = el
            for _ in range(levels):
                node = walker.GetParentElement(node)
                if not node:
                    return False
                ct = node.CurrentControlType
                # Stop before searching a whole page / window: too slow.
                if ct in (CT_DOCUMENT, 50032, 50033):  # Document, Window, Pane
                    return False
                found = node.FindFirst(U.TreeScope_Descendants, cond)
                if found:
                    return True
        except Exception:
            return False
        return False

    def _sibling_has_password(self, el, walker, cond, reach=2):
        """A password box among the next few siblings (or their
        direct children). Web forms often aren't exposed as a container, so
        the field's parent is the whole page; this stays cheap anyway."""
        U = self._U
        # Forward only: a username box comes before its password box.
        for step in (walker.GetNextSiblingElement,):
            node = el
            for _ in range(reach):
                try:
                    node = step(node)
                except Exception:
                    break
                if not node:
                    break
                try:
                    if node.CurrentIsPassword:
                        return True
                    if node.FindFirst(U.TreeScope_Children, cond):
                        return True
                except Exception:
                    continue
        return False

    def _caret_msaa(self, hwnd):
        if not self._acc or not hwnd:
            return None
        try:
            import comtypes
            oleacc = ctypes.oledll.oleacc
            ptr = ctypes.POINTER(self._acc.IAccessible)()
            oleacc.AccessibleObjectFromWindow(
                wintypes.HWND(hwnd), wintypes.DWORD(OBJID_CARET & 0xFFFFFFFF),
                ctypes.byref(self._acc.IAccessible._iid_), ctypes.byref(ptr))
            if not ptr:
                return None
            x, y, w, h = ptr.accLocation(comtypes.automation.VARIANT(0))
            r = (x, y, x + max(1, w), y + h)
            return r if (w or h) and rect_ok(r, min_h=2) else None
        except Exception:
            return None

    def _caret_uia(self, el):
        U = self._U
        tp2 = self._pattern(el, U.UIA_TextPattern2Id, U.IUIAutomationTextPattern2)
        if not tp2:
            return None
        try:
            active, rng = tp2.GetCaretRange()
            if not rng:
                return None
            rects = rng.GetBoundingRectangles()
            if not rects:
                # An empty range at the end of a line has no box; widen it.
                rng = rng.Clone()
                rng.ExpandToEnclosingUnit(U.TextUnit_Character)
                rects = rng.GetBoundingRectangles()
                if rects:
                    x, y, w, h = rects[0:4]
                    r = (int(x), int(y), int(x) + 1, int(y + h))
                    return r if rect_ok(r, min_h=2) else None
                return None
            x, y, w, h = rects[0:4]
            r = (int(x), int(y), int(x + max(1, w)), int(y + h))
            return r if rect_ok(r, min_h=2) else None
        except Exception:
            return None

    def inspect(self):
        """FocusInfo for the focused element, or None if nothing usable."""
        try:
            return self._inspect()
        except Exception:
            return None

    def _inspect(self):
        el = self.focused_element()
        info = gui_thread_info()
        if el is None:
            return None
        U = self._U

        def get(attr, default=None):
            try:
                return getattr(el, attr)
            except Exception:
                return default

        ct = get("CurrentControlType")
        name = get("CurrentName", "") or ""
        aid = get("CurrentAutomationId", "") or ""
        help_text = get("CurrentHelpText", "") or ""
        is_pw = bool(get("CurrentIsPassword", False))
        enabled = bool(get("CurrentIsEnabled", True))
        hwnd = get("CurrentNativeWindowHandle") or (info.hwndFocus if info else None)
        try:
            hwnd = int(hwnd) if hwnd else None
        except Exception:
            hwnd = None
        focus_hwnd = (info.hwndFocus if info else None) or hwnd
        cls = window_class(focus_hwnd) if focus_hwnd else ""
        style = 0
        if focus_hwnd and is_edit_class(cls):
            try:
                style = user32.GetWindowLongW(focus_hwnd, GWL_STYLE) & 0xFFFFFFFF
            except Exception:
                style = 0

        vp = self._pattern(el, U.UIA_ValuePatternId, U.IUIAutomationValuePattern)
        read_only = None
        if vp is not None:
            try:
                read_only = bool(vp.CurrentIsReadOnly)
            except Exception:
                read_only = None
        has_text = False
        try:
            has_text = bool(el.GetCurrentPropertyValue(U.UIA_IsTextPatternAvailablePropertyId))
        except Exception:
            pass

        wcaret = win32_caret(info)
        login = looks_like_login(name, aid, help_text)
        ok, reason = decide(ct, is_pw, read_only, has_text, cls, style,
                            wcaret is not None, login, enabled)
        if ok and ct == CT_EDIT and self._parent_has_password(el):
            ok, reason = False, "login form"
        elem = self._rect(el)
        result = FocusInfo(ok=ok, reason=reason, hwnd=focus_hwnd, element=elem,
                           control_type=ct, name=name, caret=None, caret_source=None)
        # A rich-text box that isn't an Edit (Chrome's contenteditable is a
        # Group with text but no ValuePattern): editable if it has a real caret.
        maybe_rich = (not ok and reason == "not a text field" and has_text
                      and ct in CT_RICH_CANDIDATES)
        if not ok and not maybe_rich:
            return result

        for source, fn in (("win32", lambda: wcaret),
                           ("msaa", lambda: self._caret_msaa(focus_hwnd)),
                           ("uia", lambda: self._caret_uia(el))):
            c = fn()
            if c and caret_inside(c, elem):
                result.caret, result.caret_source = c, source
                if maybe_rich:
                    if login or self._parent_has_password(el):
                        result.reason = "login form"
                        result.caret = None
                        return result
                    result.ok, result.reason = True, "editable text (caret)"
                return result
        if maybe_rich:
            return result
        if elem:
            result.caret, result.caret_source = elem, "element"
            return result
        return None

    def field_text(self):
        """All the text in the focused field (UIA), or None if unsupported."""
        try:
            el = self.focused_element()
            if el is None:
                return None
            U = self._U
            tp = self._pattern(el, U.UIA_TextPatternId, U.IUIAutomationTextPattern)
            if not tp:
                return None
            return tp.DocumentRange.GetText(200000)
        except Exception:
            return None

    def selected_text(self):
        """The focused element's highlighted text, or "" if none / unsupported."""
        try:
            el = self.focused_element()
            if el is None:
                return _win32_edit_selection()
            U = self._U
            tp = self._pattern(el, U.UIA_TextPatternId, U.IUIAutomationTextPattern)
            if not tp:
                return _win32_edit_selection()
            sel = tp.GetSelection()
            if not sel or sel.Length == 0:
                return ""
            parts = []
            for i in range(sel.Length):
                t = sel.GetElement(i).GetText(-1)
                if t:
                    parts.append(t)
            return "".join(parts)
        except Exception:
            return ""


def _send_timeout(hwnd, msg, wparam, lparam, timeout_ms=500):
    """SendMessageTimeout -> the result value, or None if it failed / hung."""
    send = user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    res = ctypes.c_size_t()
    if not send(hwnd, msg, wparam, lparam, 0x2, timeout_ms, ctypes.byref(res)):  # ABORTIFHUNG
        return None
    return res.value


def edit_getsel(hwnd):
    """(start, end) of the selection in a classic Edit control, or None.
    EM_GETSEL with pointers doesn't marshal across processes, so this uses the
    return value (low word start, high word end): only up to 64K characters."""
    try:
        n = _send_timeout(hwnd, 0x000E, 0, 0)          # WM_GETTEXTLENGTH
        if n is None or n > 0xFFFF:
            return None
        v = _send_timeout(hwnd, 0x00B0, 0, 0)          # EM_GETSEL
        if v is None:
            return None
        return v & 0xFFFF, (v >> 16) & 0xFFFF
    except Exception:
        return None


def edit_text(hwnd):
    """All the text of a classic Edit control, or None."""
    try:
        n = _send_timeout(hwnd, 0x000E, 0, 0)
        if n is None or n > 0xFFFF:
            return None
        buf = ctypes.create_unicode_buffer(n + 1)
        if _send_timeout(hwnd, 0x000D, n + 1, ctypes.addressof(buf)) is None:  # WM_GETTEXT
            return None
        return buf.value
    except Exception:
        return None


def _win32_edit_selection():
    """Selection in a classic Win32 "Edit" control (no UIA TextPattern there).
    Only the plain Edit class: RichEdit counts line breaks differently."""
    try:
        info = gui_thread_info()
        hwnd = info.hwndFocus if info else None
        if not hwnd or window_class(hwnd).lower() != "edit":
            return ""
        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        if style & ES_PASSWORD:
            return ""
        sel = edit_getsel(hwnd)
        if not sel or sel[1] <= sel[0]:
            return ""
        text = edit_text(hwnd)
        return text[sel[0]:sel[1]] if text is not None else ""
    except Exception:
        return ""


_local = threading.local()


def _inspector():
    ins = getattr(_local, "inspector", None)
    if ins is None:
        ins = _local.inspector = FocusInspector()
    return ins


def inspect():
    """FocusInfo for the focused element, using this thread's inspector."""
    return _inspector().inspect()


def selected_text():
    """Highlighted text in the focused field ("" if none). Any thread."""
    return _inspector().selected_text()


def replaces_whole_box(selection, field_text):
    """True when a short one-line box is entirely highlighted, as a browser's
    address bar or a search box is after one click: there, dictating means
    "type over it", not "edit it by voice"."""
    if field_text is None:
        return False
    whole = field_text.strip()
    return bool(whole) and selection.strip() == whole and "\n" not in whole and len(whole) <= 300


def selection_to_edit():
    """The highlighted text to voice-edit, or "" when there's none or this
    isn't a place to edit it (password, sign-in or read-only box, or a short
    box that's fully highlighted). Any thread; never raises."""
    try:
        ins = _inspector()
        info = ins.inspect()
        if info is None or not info.ok:
            return ""
        sel = ins.selected_text()
        if not sel.strip() or replaces_whole_box(sel, ins.field_text()):
            return ""
        return sel
    except Exception:
        return ""


# --- Going back to where a recording started -------------------------------
#
# The listener notes the spot when a recording starts (window, focused
# control, caret or highlight). If you've clicked somewhere else by the time
# the text is ready, it puts you back there before pasting -- or, if it can't,
# pastes nowhere and says the text is on the clipboard.
#
# The UIA element and text range noted at the start are COM objects owned by
# the thread that made them, so noting AND restoring both run on ONE worker
# thread (`worker()`), which owns its own FocusInspector.
#
# The decisions (`unchanged`, `go_back`, `ready_to_paste`) are plain functions
# over an `ops` object: LiveOps does the real Win32 / UIA work; tests pass fakes.

GA_ROOT = 2
SW_RESTORE = 9
EM_SETSEL, EM_SCROLLCARET = 0x00B1, 0x00B7
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# inspect() reasons that still mean "a box you type into" (the mic button
# isn't offered there, but a dictation that started there goes back there).
EDITABLE_REASONS = ("password", "login", "login form")


class Spot:
    """Where a recording started. `element` / `range` are COM objects that only
    the worker thread may touch; everything else is plain data. Holds no text
    (only the highlight's length)."""

    def __init__(self, **kw):
        self.hwnd = None          # top-level foreground window
        self.pid = 0
        self.exe = ""             # 'notepad.exe' (for logs)
        self.app_name = ""        # 'Notepad' (for the notice)
        self.focus_hwnd = None    # Win32 focus (GetGUIThreadInfo)
        self.focus_class = ""
        self.editable = False     # started in a text box: worth going back to
        self.win32_sel = None     # (start, end) in a classic Edit control
        self.element = None       # UIA focused element
        self.range = None         # UIA selection / caret range (a Clone)
        self.sel_len = 0          # characters highlighted (0 = just a caret)
        self.caret_pt = None      # screen point of the caret (last-resort click)
        for k, v in kw.items():
            setattr(self, k, v)

    def can_restore_caret(self):
        return self.win32_sel is not None or self.range is not None

    def release(self):
        """Drop the COM objects (call on the worker thread that made them)."""
        self.element = self.range = None

    def precise_hwnd(self):
        """The focus window alone pins down the box (a classic Edit control),
        so a UIA element isn't needed to tell it's the same one."""
        return bool(self.focus_hwnd and self.focus_hwnd != self.hwnd
                    and is_edit_class(self.focus_class))

    def describe(self):
        """For the log: no text, no window title."""
        caret = ("edit-sel" if self.win32_sel is not None else
                 "uia-range" if self.range is not None else
                 "point" if self.caret_pt else "none")
        return "%s focus=%s caret=%s sel_len=%d editable=%s" % (
            self.exe or "?", self.focus_class or "?", caret, self.sel_len, self.editable)


class Outcome:
    __slots__ = ("ok", "reason", "steps")

    def __init__(self, ok, reason, steps=None):
        self.ok, self.reason, self.steps = ok, reason, list(steps or [])

    def __repr__(self):
        return "Outcome(%r, %r, %r)" % (self.ok, self.reason, self.steps)


def unchanged(spot, ops):
    """Still in the window and box the recording started in? Then the paste
    goes ahead exactly as before. A focus we can't compare counts as
    unchanged: only a positively different box triggers going back."""
    return ops.foreground() == spot.hwnd and ops.focus_state(spot) != "other"


def go_back(spot, ops, need_selection=False):
    """Put the original window, box and caret (or highlight) back.

    need_selection: a voice-edit of highlighted text; the paste replaces the
    selection, so the exact highlight must be back or we don't paste.
    Returns Outcome(ok, reason, steps). Never pastes anything itself."""
    steps = []
    if not ops.is_window(spot.hwnd) or ops.window_pid(spot.hwnd) != spot.pid:
        return Outcome(False, "window closed", steps)
    if ops.is_minimized(spot.hwnd):
        ops.restore(spot.hwnd)
        steps.append("unminimized")
    how = ops.bring_to_front(spot.hwnd)
    if not how:
        return Outcome(False, "foreground refused", steps)
    steps.append("front:" + how)

    focused = ops.focus_state(spot) == "same"
    if not focused:
        ops.set_focus(spot)
        focused = ops.focus_state(spot) == "same"
        steps.append("focus:set" if focused else "focus:failed")

    caret = exact_sel = False
    if focused and spot.can_restore_caret():
        caret = exact_sel = ops.restore_caret(spot)
        steps.append("caret:exact" if caret else "caret:failed")

    # Last resort: click where the caret was (only if that spot is still the
    # same box, in this window). A click can't recreate a highlight, but it
    # can give focus back, after which the highlight is re-selected.
    if spot.caret_pt and (not focused or (not caret and spot.sel_len == 0)):
        if ops.click_caret(spot):
            steps.append("clicked")
            focused = ops.focus_state(spot) == "same"
            if focused and spot.sel_len and spot.can_restore_caret():
                caret = exact_sel = ops.restore_caret(spot)
                steps.append("caret:exact" if caret else "caret:failed")
            elif focused and not spot.sel_len:
                caret = True
                steps.append("caret:clicked")
        else:
            steps.append("click:refused")

    if ops.foreground() != spot.hwnd:
        return Outcome(False, "lost foreground", steps)
    if not focused:
        return Outcome(False, "box not focused", steps)
    if need_selection and not exact_sel:
        return Outcome(False, "highlight not re-selected", steps)
    return Outcome(True, "exact" if caret else "box (caret not restored)", steps)


def ready_to_paste(spot, ops, need_selection=False):
    """Last check just before Ctrl+V (after the clipboard focus bounce)."""
    steps = []
    if ops.foreground() != spot.hwnd:
        how = ops.bring_to_front(spot.hwnd)
        if not how:
            return Outcome(False, "lost foreground", steps)
        steps.append("front:" + how)
    if ops.focus_state(spot) != "same":
        ops.set_focus(spot)
        if ops.focus_state(spot) != "same":
            return Outcome(False, "lost focus", steps)
        steps.append("focus:set")
    if need_selection and not ops.selection_exact(spot):
        if not ops.restore_caret(spot):
            return Outcome(False, "highlight lost", steps)
        steps.append("caret:exact")
    return Outcome(True, "ready", steps)


# -- noting the spot ----------------------------------------------------------

def _process_image(pid):
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
        finally:
            k.CloseHandle(h)
    except Exception:
        pass
    return ""


def _file_description(path):
    """The FileDescription in an exe's version info ('Google Chrome'), or ''."""
    try:
        ver = ctypes.windll.version
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        langs = []
        if (ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n))
                and n.value >= 4):
            arr = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))
            langs.append("%04x%04x" % (arr[0], arr[1]))
        for lang in langs + ["040904b0", "040904e4", "000004b0"]:
            q = "\\StringFileInfo\\%s\\FileDescription" % lang
            if ver.VerQueryValueW(buf, q, ctypes.byref(ptr), ctypes.byref(n)) and n.value:
                return ctypes.wstring_at(ptr, n.value).split(chr(0))[0].strip()
    except Exception:
        pass
    return ""


def _window_title(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value.strip()
    except Exception:
        return ""


def app_display_name(exe_path, description="", title=""):
    """What to call an app in a notice: its file description ('Google
    Chrome') when short, the window title for a Store app's frame, else the
    file name ('Notepad'). Pure."""
    base = os.path.basename(exe_path or "")
    d = (description or "").strip()
    if base.lower() == "applicationframehost.exe" and title:
        return title[:40]
    if d and len(d) <= 32:
        return d
    stem = os.path.splitext(base)[0]
    if stem:
        return stem[:1].upper() + stem[1:]
    return (title or "")[:40]


def capture_spot(ins=None):
    """Note where the user is typing right now. Call on the worker thread
    (worker().submit(...)); never raises (None if nothing usable)."""
    try:
        return _capture_spot(ins or _inspector())
    except Exception:
        return None


def _capture_spot(ins):
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    s = Spot(hwnd=int(fg), pid=window_pid(fg))
    if s.pid == os.getpid():
        return s      # one of our own windows: nothing to go back to
    path = _process_image(s.pid)
    s.exe = os.path.basename(path).lower()
    s.app_name = app_display_name(path, _file_description(path) if path else "", _window_title(fg))
    info = gui_thread_info(fg)
    if info and info.hwndFocus:
        s.focus_hwnd = int(info.hwndFocus)
        s.focus_class = window_class(s.focus_hwnd)

    fi = ins.inspect()
    el = ins.focused_element()
    if el is not None:
        try:
            if el.CurrentProcessId != s.pid:
                el = None      # UIA focus isn't in the foreground app
        except Exception:
            el = None
    s.element = el
    s.editable = bool((fi is not None and (fi.ok or fi.reason in EDITABLE_REASONS))
                      or is_edit_class(s.focus_class))
    if fi is not None and fi.caret and fi.caret_source != "element":
        l, t, r, b = fi.caret
        s.caret_pt = (int(l), int((t + b) // 2))

    if s.focus_hwnd and s.focus_class.lower() == "edit":
        s.win32_sel = edit_getsel(s.focus_hwnd)
        if s.win32_sel is not None:
            s.sel_len = abs(s.win32_sel[1] - s.win32_sel[0])
    if s.win32_sel is None and el is not None:
        try:
            U = ins._U
            tp = ins._pattern(el, U.UIA_TextPatternId, U.IUIAutomationTextPattern)
            sel = tp.GetSelection() if tp else None
            if sel and sel.Length >= 1:
                rng = sel.GetElement(0).Clone()
                s.range = rng
                s.sel_len = len(rng.GetText(-1) or "")
        except Exception:
            s.range = None
    return s


# -- the real Win32 / UIA steps (worker thread only) -------------------------

ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTU(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTU)]


def _send_inputs(items):
    arr = (_INPUT * len(items))(*items)
    return user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT))


def _key(vk, up=False):
    i = _INPUT(type=1)
    i.u.ki = _KEYBDINPUT(vk, 0, 0x2 if up else 0, 0, 0)
    return i


def _mouse(flags):
    i = _INPUT(type=0)
    i.u.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
    return i


if user32 is not None:
    for _name, _args, _res in (
            ("IsWindow", [wintypes.HWND], wintypes.BOOL),
            ("IsIconic", [wintypes.HWND], wintypes.BOOL),
            ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            ("BringWindowToTop", [wintypes.HWND], wintypes.BOOL),
            ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
            ("SwitchToThisWindow", [wintypes.HWND, wintypes.BOOL], None),
            ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
            ("SetFocus", [wintypes.HWND], wintypes.HWND),
            ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            ("WindowFromPoint", [wintypes.POINT], wintypes.HWND),
            ("SetCursorPos", [ctypes.c_int, ctypes.c_int], wintypes.BOOL),
            ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            ("AllowSetForegroundWindow", [wintypes.DWORD], wintypes.BOOL),
            ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("SendInput", [wintypes.UINT, ctypes.c_void_p, ctypes.c_int], wintypes.UINT)):
        try:
            _f = getattr(user32, _name)
            _f.argtypes = _args
            _f.restype = _res
        except AttributeError:
            pass


def _h(x):
    return int(x) if x else 0


class LiveOps:
    """The real steps behind go_back / ready_to_paste. Worker thread only."""

    def __init__(self, ins):
        self.ins = ins

    # -- windows -------------------------------------------------------------
    def foreground(self):
        return _h(user32.GetForegroundWindow())

    def is_window(self, hwnd):
        return bool(hwnd and user32.IsWindow(hwnd))

    def window_pid(self, hwnd):
        return window_pid(hwnd)

    def is_minimized(self, hwnd):
        return bool(user32.IsIconic(hwnd))

    def restore(self, hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)    # let the restore animation hand back a real window

    def _wait_front(self, hwnd, secs=0.3):
        end = time.monotonic() + secs
        while True:
            if self.foreground() == hwnd:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)

    def _front_attach(self, hwnd):
        """SetForegroundWindow while sharing input with the current foreground
        thread (and the target's), which lifts the foreground lock."""
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        fg = user32.GetForegroundWindow()
        tids = {user32.GetWindowThreadProcessId(fg, None) if fg else 0,
                user32.GetWindowThreadProcessId(hwnd, None)} - {0, me}
        attached = [t for t in tids if user32.AttachThreadInput(me, t, True)]
        try:
            user32.AllowSetForegroundWindow(0xFFFFFFFF)   # ASFW_ANY
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            for t in attached:
                user32.AttachThreadInput(me, t, False)

    def _front_alt(self, hwnd):
        """The Alt-key trick: Windows lets SetForegroundWindow through while
        Alt is down (it's how a menu takes focus)."""
        _send_inputs([_key(0x12)])               # VK_MENU down
        try:
            user32.SetForegroundWindow(hwnd)
        finally:
            _send_inputs([_key(0x12, up=True)])

    def _front_switch(self, hwnd):
        user32.SwitchToThisWindow(hwnd, True)

    def bring_to_front(self, hwnd):
        """'' if refused, else how it got there."""
        if self.foreground() == hwnd:
            return "already"
        for how, fn in (("attach", self._front_attach), ("alt", self._front_alt),
                        ("switch", self._front_switch)):
            try:
                fn(hwnd)
            except Exception:
                pass
            if self._wait_front(hwnd):
                return how
        return ""

    # -- focus ---------------------------------------------------------------
    def focus_state(self, spot):
        """'same' (the box we started in has focus), 'other' (something else
        has), or 'unknown' (same window, but UIA can't say which box)."""
        info = gui_thread_info(spot.hwnd)
        cur_hwnd = _h(info.hwndFocus) if info else 0
        if spot.focus_hwnd and cur_hwnd != spot.focus_hwnd:
            return "other"
        if spot.element is None:
            return "same"
        try:
            spot.element.CurrentProcessId           # still alive?
        except Exception:
            return "same" if spot.precise_hwnd() else "unknown"
        cur = self.ins.focused_element()
        if cur is None:
            return "same" if spot.precise_hwnd() else "unknown"
        try:
            return "same" if self.ins._uia.CompareElements(cur, spot.element) else "other"
        except Exception:
            return "same" if spot.precise_hwnd() else "unknown"

    def set_focus(self, spot):
        info = gui_thread_info(spot.hwnd)
        cur_hwnd = _h(info.hwndFocus) if info else 0
        if spot.focus_hwnd and cur_hwnd != spot.focus_hwnd and user32.IsWindow(spot.focus_hwnd):
            me = ctypes.windll.kernel32.GetCurrentThreadId()
            tid = user32.GetWindowThreadProcessId(spot.focus_hwnd, None)
            if tid and tid != me and user32.AttachThreadInput(me, tid, True):
                try:
                    user32.SetFocus(spot.focus_hwnd)
                finally:
                    user32.AttachThreadInput(me, tid, False)
        if spot.element is not None and self.focus_state(spot) != "same":
            try:
                spot.element.SetFocus()
            except Exception:
                pass
        time.sleep(0.05)

    # -- caret / highlight -----------------------------------------------------
    def _current_range(self):
        """(first selection range, U) of the focused element, or (None, U)."""
        U = self.ins._U
        el = self.ins.focused_element()
        if el is None:
            return None, U
        tp = self.ins._pattern(el, U.UIA_TextPatternId, U.IUIAutomationTextPattern)
        if not tp:
            return None, U
        sel = tp.GetSelection()
        if not sel or sel.Length < 1:
            return None, U
        return sel.GetElement(0), U

    def selection_exact(self, spot):
        try:
            if spot.win32_sel is not None:
                cur = edit_getsel(spot.focus_hwnd)
                return cur is not None and sorted(cur) == sorted(spot.win32_sel)
            if spot.range is not None:
                cur, U = self._current_range()
                if cur is None:
                    return False
                s0, e0 = U.TextPatternRangeEndpoint_Start, U.TextPatternRangeEndpoint_End
                return (cur.CompareEndpoints(s0, spot.range, s0) == 0
                        and cur.CompareEndpoints(e0, spot.range, e0) == 0)
        except Exception:
            pass
        return False

    def restore_caret(self, spot):
        """Re-select the noted caret / highlight; True only if verified."""
        try:
            if spot.win32_sel is not None:
                if not user32.IsWindow(spot.focus_hwnd):
                    return False
                a, b = spot.win32_sel
                _send_timeout(spot.focus_hwnd, EM_SETSEL, a, b)
                _send_timeout(spot.focus_hwnd, EM_SCROLLCARET, 0, 0)
            elif spot.range is not None:
                spot.range.Select()
                time.sleep(0.03)
            else:
                return False
        except Exception:
            return False
        return self.selection_exact(spot)

    def click_caret(self, spot):
        """Click where the caret was -- only if that point is still inside the
        same box of the same window. Puts the mouse pointer back after."""
        try:
            x, y = spot.caret_pt
            pt = wintypes.POINT(x, y)
            hit = user32.WindowFromPoint(pt)
            if not hit or _h(user32.GetAncestor(hit, GA_ROOT)) != spot.hwnd:
                return False
            if spot.element is not None:
                r = spot.element.CurrentBoundingRectangle
                if not (r.left <= x <= r.right and r.top <= y <= r.bottom):
                    return False
                node = self.ins._uia.ElementFromPoint(pt)
                walker = self.ins._uia.ControlViewWalker
                for _ in range(6):
                    if not node:
                        return False
                    if self.ins._uia.CompareElements(node, spot.element):
                        break
                    node = walker.GetParentElement(node)
                else:
                    return False
            elif not spot.precise_hwnd() or _h(hit) != spot.focus_hwnd:
                return False
            old = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(old))
            swapped = bool(user32.GetSystemMetrics(23))      # SM_SWAPBUTTON
            down, up = (0x8, 0x10) if swapped else (0x2, 0x4)
            user32.SetCursorPos(x, y)
            try:
                _send_inputs([_mouse(down), _mouse(up)])
                time.sleep(0.05)
            finally:
                user32.SetCursorPos(old.x, old.y)
            time.sleep(0.08)
            return True
        except Exception:
            return False

    def text_before_caret(self, n):
        """The n characters just before the caret in the focused box (no
        highlight), or None if that can't be read."""
        try:
            info = gui_thread_info()
            h = _h(info.hwndFocus) if info else 0
            if h and window_class(h).lower() == "edit":
                sel = edit_getsel(h)
                text = edit_text(h)
                if not sel or text is None or sel[0] != sel[1] or sel[0] < n:
                    return None
                return text[sel[0] - n:sel[0]]
            cur, U = self._current_range()
            if cur is None:
                return None
            s0, e0 = U.TextPatternRangeEndpoint_Start, U.TextPatternRangeEndpoint_End
            if cur.CompareEndpoints(s0, cur, e0) != 0:
                return None        # something is highlighted
            r = cur.Clone()
            if r.MoveEndpointByUnit(s0, U.TextUnit_Character, -n) != -n:
                return None
            return r.GetText(n + 4)
        except Exception:
            return None


# -- the worker thread --------------------------------------------------------

class _Job:
    def __init__(self, fn):
        self.fn, self.result, self.error = fn, None, None
        self.done = threading.Event()

    def wait(self, timeout=None):
        """The result, or None if it timed out or failed."""
        self.done.wait(timeout)
        return self.result if self.done.is_set() and self.error is None else None


class FocusWorker:
    """One thread that owns a FocusInspector (its COM apartment) and every
    UIA object it hands out. Per-monitor DPI aware, so caret rectangles,
    WindowFromPoint and SetCursorPos all use the same physical pixels."""

    def __init__(self):
        self._q = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self.ins = None

    def _ensure(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="focus-worker",
                                                daemon=True)
                self._thread.start()

    def _loop(self):
        try:
            user32.SetThreadDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        except Exception:
            pass
        try:
            # A message queue, so AttachThreadInput accepts this thread.
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        except Exception:
            pass
        self.ins = _local.inspector = FocusInspector()
        while True:
            job = self._q.get()
            try:
                job.result = job.fn(self.ins)
            except BaseException as e:   # noqa: BLE001 -- reported via job.error
                job.error = e
            finally:
                job.done.set()

    def submit(self, fn):
        """Run fn(inspector) on the worker; returns a job (job.wait(timeout))."""
        job = _Job(fn)
        if self._thread is not None and threading.current_thread() is self._thread:
            try:
                job.result = fn(self.ins)
            except BaseException as e:   # noqa: BLE001
                job.error = e
            job.done.set()
            return job
        self._ensure()
        self._q.put(job)
        return job

    def run(self, fn, timeout=5.0):
        """fn(inspector) on the worker; its result. Raises TimeoutError if it
        took too long, or fn's own exception."""
        job = self.submit(fn)
        if not job.done.wait(timeout):
            raise TimeoutError("focus worker busy")
        if job.error is not None:
            raise job.error
        return job.result


_worker = None
_worker_lock = threading.Lock()


def worker():
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = FocusWorker()
        return _worker
