"""Where is the text cursor, and is it somewhere we may offer dictation?

Used by the floating mic button (caret_button.py) and, later, by "voice-edit
the highlighted text" (`selected_text`).

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
import re
import threading
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
        send = user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        start, end = wintypes.DWORD(), wintypes.DWORD()
        res = ctypes.c_size_t()
        # EM_GETSEL with pointers doesn't marshal across processes; use the
        # return value (low word start, high word end; fine up to 64K chars).
        if not send(hwnd, 0x00B0, 0, 0, 0x2, 500, ctypes.byref(res)):
            return ""
        start, end = res.value & 0xFFFF, (res.value >> 16) & 0xFFFF
        if end <= start:
            return ""
        if not send(hwnd, 0x000E, 0, 0, 0x2, 500, ctypes.byref(res)):  # WM_GETTEXTLENGTH
            return ""
        n = res.value
        if n > 0xFFFF:  # EM_GETSEL return value only holds 16-bit offsets
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        if not send(hwnd, 0x000D, n + 1, ctypes.addressof(buf), 0x2, 500, ctypes.byref(res)):
            return ""
        return buf.value[start:end]
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
