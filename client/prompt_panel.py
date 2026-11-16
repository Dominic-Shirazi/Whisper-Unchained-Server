"""The floating mic button's right-click panel: type an instruction for the AI.

Right-click the floating mic button and a small panel opens at the pointer:

    +------------------------------------------+
    |   > Bug ticket        prompt ticket      |   matching custom triggers,
    |     Shorter           prompt shorter     |   ABOVE the box (it grows upwards)
    | ( Type an instruction for the AI       ) |   rounded box, one line,
    | [x] Include the highlighted text   (z) (/)|  grows as you type
    +------------------------------------------+

Everything that comes and goes (the trigger list, the one-time hint) sits
above the box and the panel grows upwards, so the box you are typing in
never moves. The two small pictures bottom right are for the button itself:
snooze it for an hour, or never show it in this app. Each says what it does
in a hover hint that appears below the panel (its own little window, so
nothing in the panel shifts).

It is the typed twin of speaking an instruction ("prompt AI ...", or
highlight-and-speak): for an airport or an open office, where you'd rather
not talk to your laptop. Enter sends, Shift+Enter starts a new line, Esc or a
click elsewhere closes. The answer goes where the text cursor was: over the
highlight when "Include the highlighted text" is ticked, after it when not.

As you type, custom triggers whose name or phrase matches are listed (like
slash commands); Up/Down and Enter or Tab put the trigger's phrase in the
box, and the server applies its saved instruction exactly as if the phrase
had been spoken.

The panel is a Tk window in the listener's own process, so the floating mic
button never appears for this box (it skips its own process's windows).

The pure parts (matching, wording) are unit-tested; the window is driven by
background_listener.py on the Tk thread.
"""
import ctypes
import re
import time
from ctypes import wintypes

BASE_WIDTH = 380          # panel width at 100% scaling (96 DPI)
MAX_LINES = 8             # the box grows to this many lines, then scrolls
SUGGEST_MAX = 6
SUGGEST_QUERY_MAX = 40    # longer than this is an instruction, not a search
INSTRUCTION_MAX = 4000    # the server's limit (INSTRUCTION_MAX_CHARS)
HINT_KEY = "PROMPT_BOX_HINT"   # .env: "off" once the hint has been closed

PLACEHOLDER = "Type an instruction for the AI"
HINT_TEXT = ("Ticked: the AI changes the highlighted text. "
             "Not ticked: its answer goes in after it.")
TIP_DELAY_MS = 350


# --- pure logic (unit-tested) ---------------------------------------------

def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _words(s):
    return re.findall(r"[^\W_]+", _norm(s))


def chosen(text, triggers):
    """The trigger whose phrase `text` already contains, or None."""
    t = " ".join(_words(text))
    best = None
    for trig in triggers or ():
        for phrase in trig.get("phrases") or ():
            p = " ".join(_words(phrase))
            if p and re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", t):
                if best is None or len(p) > best[0]:
                    best = (len(p), trig)
    return best[1] if best else None


def suggest(text, triggers, limit=SUGGEST_MAX):
    """Custom triggers to offer for what has been typed so far.

    "/" alone lists them all. Otherwise at least two letters, matched against
    the start of a trigger's phrase or name first, then the start of any word
    in them. Nothing once the text holds a whole phrase (it's chosen), or is
    long or multi-line (it's an instruction, not a search)."""
    raw = text or ""
    if "\n" in raw.strip():
        return []
    slash = raw.lstrip().startswith("/")
    q = _norm(raw.lstrip().lstrip("/"))
    items = [t for t in (triggers or ()) if t.get("phrases")]
    if not q:
        return items[:limit] if slash else []
    if len(q) < 2 or len(q) > SUGGEST_QUERY_MAX or chosen(q, items) is not None:
        return []
    qw = " ".join(_words(q))
    if not qw:
        return []
    ranked = []
    for n, trig in enumerate(items):
        name = " ".join(_words(trig.get("name")))
        phrases = [" ".join(_words(p)) for p in trig["phrases"]]
        if any(p.startswith(qw) for p in phrases):
            rank = 0
        elif name.startswith(qw):
            rank = 1
        elif any((" " + s).find(" " + qw) >= 0 for s in phrases + [name]):
            rank = 2
        else:
            continue
        ranked.append((rank, n, trig))
    ranked.sort(key=lambda r: r[:2])
    return [r[2] for r in ranked[:limit]]


def completion(trigger):
    """What accepting a suggestion puts in the box: the phrase, ready for more."""
    return (trigger.get("phrases") or [""])[0].strip() + " "


def word_count(text):
    return len(re.findall(r"\S+", text or ""))


def include_label(selection):
    n = word_count(selection)
    return "Include the highlighted text (%d word%s)" % (n, "" if n == 1 else "s")


def never_label(app_label, limit=28):
    """The "never for this app" picture's hover hint; long program names are cut."""
    name = (app_label or "").strip()
    if not name:
        return ""
    if len(name) > limit:
        name = name[:limit - 1].rstrip() + "…"
    return "Never show this button in %s" % name


def snooze_label(minutes):
    """The snooze picture's hover hint."""
    if minutes % 60 == 0:
        h = minutes // 60
        return "Snooze this button for %d hour%s" % (h, "" if h == 1 else "s")
    return "Snooze this button for %d minutes" % minutes


def lines_shown(display_lines, limit=MAX_LINES):
    return max(1, min(limit, int(display_lines or 1)))


def keep_box_still(box_y, box_offset, height, work, gap=8):
    """Where the panel's top goes so the text box stays at screen row `box_y`
    while things above it come and go. box_offset: the box's distance from the
    panel's top; height: the panel's. Pushed back inside `work` if it has to
    be. Returns (panel top, the box's row after that)."""
    _wl, wt, _wr, wb = work
    y = box_y - box_offset
    y = min(y, wb - gap - height)
    y = max(y, wt + gap)
    return int(y), int(y + box_offset)


def tip_place(target, size, panel_bottom, work, gap=6):
    """Top-left of a hover hint of `size` for the picture at `target`
    (l, t, r, b): centred under it, below the panel's bottom edge."""
    w, h = size
    wl, _wt, wr, wb = work
    x = (target[0] + target[2]) / 2 - w / 2
    x = min(max(x, wl + gap), max(wl + gap, wr - w - gap))
    y = panel_bottom + gap
    if y + h > wb:
        y = target[1] - gap - h        # no room below the screen edge: above the picture
    return int(x), int(y)


def place_panel(pointer, size, work, gap=12):
    """Top-left for a panel of `size` opened at `pointer`, inside `work`:
    below and a little left of the pointer; above it when there's no room."""
    px, py = pointer
    w, h = size
    wl, wt, wr, wb = work
    x = min(max(px - 24, wl + gap), max(wl + gap, wr - w - gap))
    y = py + gap
    if y + h > wb - gap:
        y = py - gap - h
    y = min(max(y, wt + gap), max(wt + gap, wb - h - gap))
    return int(x), int(y)


# --- Win32 bits (Tk thread) -------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32.MonitorFromPoint.restype = wintypes.HMONITOR
_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def work_area(x, y):
    """(l, t, r, b) of the work area of the monitor at a screen point, or None."""
    try:
        mon = _user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if mon and _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def set_foreground(hwnd):
    """Bring `hwnd` to the front from a background process (attach to the
    foreground thread's input queue for the moment of the switch)."""
    if not hwnd:
        return False
    try:
        fg = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = _kernel32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != me and _user32.AttachThreadInput(me, fg_tid, True)
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(me, fg_tid, False)
        return _user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


# --- the window -------------------------------------------------------------

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class PromptPanel:
    """One panel per listener, re-used for every right-click. Everything here
    runs on the Tk thread (the listener's overlay root).

    on_submit(text, include)  Enter with something typed (panel already closed)
    on_snooze()               the Snooze button
    on_never()                the "Never in <app>" button
    on_hint_closed()          the hint's X: don't show it again
    on_closed(draft)          closed without sending; `draft` is what was typed
    """

    def __init__(self, root, scale=1.0, theme=None):
        import tkinter as tk
        from tkinter import font as tkfont
        if theme is None:
            import theme
        self.tk, self.t, self.root = tk, theme, root
        self.scale = max(1.0, float(scale))
        self.top = None
        self._photos = {}
        self._font = tkfont.Font(root=root, family=theme.FONT, size=11)
        self._small = tkfont.Font(root=root, family=theme.FONT, size=9)
        self._semi = tkfont.Font(root=root, family=theme.FONT_SB, size=10)
        self._cb = {}
        self._triggers = []
        self._suggestions = []
        self._picked = 0
        self._dismissed_for = None     # text the list was Esc-dismissed for
        self._include = False
        self._selection = ""
        self._prev = None
        self._opened = 0.0
        self._lines = 1

    def _px(self, n):
        return int(round(n * self.scale))

    @property
    def is_open(self):
        return self.top is not None

    # -- pictures (rounded shapes, drawn 4x and scaled down) -------------------
    def _rounded(self, w, h, radius, fill, behind, outline=None, width=1.0):
        key = ("rr", w, h, radius, fill, behind, outline, width)
        if key not in self._photos:
            from PIL import Image, ImageDraw, ImageTk
            k = 4
            img = Image.new("RGB", (w * k, h * k), _rgb(behind))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=radius * k, fill=_rgb(fill),
                                outline=_rgb(outline) if outline else None,
                                width=max(1, int(round(width * k))))
            self._photos[key] = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS),
                                                   master=self.root)
        return self._photos[key]

    def _check(self, checked, behind):
        """A checkbox that reads without colour: ticked is a filled square
        with a check mark, unticked an empty outlined square."""
        size = self._px(16)
        key = ("check", checked, behind, size)
        if key not in self._photos:
            from PIL import Image, ImageDraw, ImageTk
            k = 8
            S = size * k
            img = Image.new("RGB", (S, S), _rgb(behind))
            d = ImageDraw.Draw(img)
            pad = k
            box = [pad, pad, S - pad - 1, S - pad - 1]
            if checked:
                d.rounded_rectangle(box, radius=4 * k, fill=_rgb(self.t.TEXT))
                d.line([(S * 0.28, S * 0.52), (S * 0.44, S * 0.68), (S * 0.73, S * 0.34)],
                       fill=_rgb(self.t.SURFACE), width=int(2.2 * k), joint="curve")
            else:
                d.rounded_rectangle(box, radius=4 * k, outline=_rgb("#9C9FA5"),
                                    width=int(1.5 * k))
            self._photos[key] = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS),
                                                   master=self.root)
        return self._photos[key]

    def _icon(self, kind, hover):
        """The two pictures: "snooze" (a crescent moon with a z) and "never"
        (a circle with a line through it), on a rounded tile that lightens
        under the pointer."""
        size = self._px(26)
        key = ("icon", kind, hover, size)
        if key not in self._photos:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            k = 8
            S = size * k
            t = self.t
            behind = _rgb(t.HOVER if hover else t.SURFACE)
            ink = _rgb(t.TEXT if hover else t.MUTED)
            img = Image.new("RGB", (S, S), _rgb(t.SURFACE))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([0, 0, S - 1, S - 1], radius=7 * k, fill=behind)
            lw = int(1.7 * k)
            if kind == "never":
                m = S * 0.25
                d.ellipse([m, m, S - m, S - m], outline=ink, width=lw)
                r = (S / 2 - m) * 0.707
                d.line([(S / 2 - r, S / 2 - r), (S / 2 + r, S / 2 + r)], fill=ink, width=lw)
            else:
                # Crescent: a disc with a bite taken out of its upper right.
                cx, cy, r = S * 0.44, S * 0.56, S * 0.25
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ink)
                bx, by, br = cx + r * 0.50, cy - r * 0.42, r * 0.92
                d.ellipse([bx - br, by - br, bx + br, by + br], fill=behind)
                try:
                    f = ImageFont.truetype(t.FONT_FILE_SB, int(S * 0.34))
                    d.text((S * 0.70, S * 0.30), "z", font=f, fill=ink, anchor="mm")
                except Exception:
                    pass
            self._photos[key] = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS),
                                                   master=self.root)
        return self._photos[key]

    # -- open / close --------------------------------------------------------
    def open(self, app_label="", selection="", triggers=(), show_hint=True, draft="",
             prev_hwnd=None, snooze_minutes=60, on_submit=None, on_snooze=None,
             on_never=None, on_hint_closed=None, on_closed=None, at=None):
        self.close(restore=False, notify=False)
        tk, t = self.tk, self.t
        self._cb = {"submit": on_submit, "snooze": on_snooze, "never": on_never,
                    "hint": on_hint_closed, "closed": on_closed}
        self._triggers = list(triggers or ())
        self._selection = selection or ""
        self._include = bool(self._selection)
        self._prev = prev_hwnd
        self._suggestions, self._picked, self._dismissed_for = [], 0, None
        self._lines = 1

        top = self.top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=t.BORDER)
        self._card = tk.Frame(top, bg=t.SURFACE)
        self._card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        pad = self._px(6)
        body = self._body = tk.Frame(self._card, bg=t.SURFACE)
        body.pack(fill=tk.BOTH, expand=True, padx=pad, pady=pad)
        self._w = self._px(BASE_WIDTH)

        # The rounded box: a picture on a canvas, the text widget on top of it.
        self._ipad = (self._px(10), self._px(6))
        self._box = tk.Canvas(body, width=self._w, height=10, bg=t.SURFACE,
                              highlightthickness=0, bd=0)
        self._box.pack()
        self._box_img = self._box.create_image(0, 0, anchor="nw")
        self.text = tk.Text(self._box, height=1, wrap="word", bg=t.RAISED, fg=t.TEXT,
                            insertbackground=t.TEXT, selectbackground=t.BORDER,
                            selectforeground=t.TEXT, relief="flat", bd=0,
                            highlightthickness=0, font=self._font, undo=True,
                            padx=0, pady=0)
        self._text_win = self._box.create_window(self._ipad[0], self._ipad[1], anchor="nw",
                                                 window=self.text,
                                                 width=self._w - 2 * self._ipad[0])
        self._ghost = tk.Label(self.text, text=PLACEHOLDER, bg=t.RAISED, fg=t.FAINT,
                               font=self._font, bd=0, padx=0, pady=0, cursor="xterm")
        self._ghost.bind("<Button-1>", lambda _e: self.text.focus_set())

        # Above the box, packed only while there is something to show: the
        # one-time hint, the matching triggers, "Trigger: Bug ticket".
        self._list = tk.Frame(body, bg=t.SURFACE)
        self._status = tk.Label(body, text="", bg=t.SURFACE, fg=t.MUTED, font=self._small,
                                anchor="w", justify="left")
        self._hint_row = None
        if self._selection and show_hint:
            hint = self._hint_row = tk.Frame(body, bg=t.SURFACE)
            x = tk.Label(hint, text="✕", bg=t.SURFACE, fg=t.MUTED, font=self._small,
                         cursor="hand2", padx=self._px(4))
            x.pack(side=tk.RIGHT, anchor="n")
            x.bind("<Button-1>", lambda _e: self._close_hint())
            x.bind("<Enter>", lambda _e: x.configure(fg=t.TEXT))
            x.bind("<Leave>", lambda _e: x.configure(fg=t.MUTED))
            tk.Label(hint, text=HINT_TEXT, bg=t.SURFACE, fg=t.MUTED, font=self._small,
                     justify="left", anchor="w",
                     wraplength=self._w - self._px(28)).pack(side=tk.LEFT, fill=tk.X)
            hint.pack(fill=tk.X, pady=(0, self._px(5)), before=self._box)

        # Under the box, one row: the tick box (only with a highlight) on the
        # left, the button's own two pictures on the right.
        foot = self._foot = tk.Frame(body, bg=t.SURFACE)
        foot.pack(fill=tk.X, pady=(self._px(5), 0))
        self._sel_box = None
        if self._selection:
            row = tk.Frame(foot, bg=t.SURFACE, cursor="hand2")
            self._sel_box = tk.Label(row, image=self._check(True, t.SURFACE), bg=t.SURFACE, bd=0)
            self._sel_box.pack(side=tk.LEFT)
            lab = tk.Label(row, text=include_label(self._selection), bg=t.SURFACE, fg=t.TEXT,
                           font=self._small, bd=0)
            lab.pack(side=tk.LEFT, padx=(self._px(6), 0))
            for wdg in (row, self._sel_box, lab):
                wdg.bind("<Button-1>", lambda _e: self._toggle_include())
            row.pack(side=tk.LEFT, padx=(self._px(2), 0))
        never = never_label(app_label)
        if never:
            self._icon_button(foot, "never", never, lambda: self._act("never")).pack(side=tk.RIGHT)
        self._icon_button(foot, "snooze", snooze_label(snooze_minutes),
                          lambda: self._act("snooze")).pack(
            side=tk.RIGHT, padx=(0, self._px(2) if never else 0))
        self._tip = self._tip_job = None

        tx = self.text
        tx.bind("<Return>", self._on_return)
        tx.bind("<KP_Enter>", self._on_return)
        tx.bind("<Shift-Return>", lambda _e: None)        # a new line, Tk's default
        tx.bind("<Escape>", self._on_escape)
        tx.bind("<Up>", lambda e: self._move(-1))
        tx.bind("<Down>", lambda e: self._move(1))
        tx.bind("<Tab>", self._on_tab)
        tx.bind("<<Modified>>", self._on_modified)
        top.bind("<FocusOut>", lambda _e: self._focus_check())

        if draft:
            tx.insert("1.0", draft[:INSTRUCTION_MAX])
        tx.edit_modified(False)
        self._resize_box()
        self._refresh()

        top.update_idletasks()
        size = (top.winfo_reqwidth(), top.winfo_reqheight())
        pointer = at or self.root.winfo_pointerxy()
        work = work_area(*pointer) or (0, 0, self.root.winfo_screenwidth(),
                                       self.root.winfo_screenheight())
        self._work = work
        x, y = place_panel(pointer, size, work, gap=self._px(12))
        self._x = x
        top.geometry("%dx%d+%d+%d" % (size[0], size[1], x, y))
        top.deiconify()
        self._style_window()
        top.update_idletasks()
        self._box_y = y + self._box_offset(top.winfo_reqheight())
        self._placed = True
        self._reposition()
        self._opened = time.monotonic()
        self._take_focus()
        return self

    def _box_offset(self, height):
        """The text box's distance from the top edge of a panel `height` tall.
        Worked out from what sits under the box (asked-for sizes), because
        the widgets' own positions are only right after the window has been
        resized, and we need the answer before that."""
        edge = int(str(self._card.pack_info().get("pady", 0)).split()[0])
        below = (self._box.winfo_reqheight() + self._px(5) + self._foot.winfo_reqheight()
                 + self._px(6) + edge)
        return max(0, height - below)

    def _icon_button(self, parent, kind, hint, command):
        tk, t = self.tk, self.t
        b = tk.Label(parent, image=self._icon(kind, False), bg=t.SURFACE, bd=0, cursor="hand2")

        def enter(_e):
            b.configure(image=self._icon(kind, True))
            self._tip_later(b, hint)

        def leave(_e):
            b.configure(image=self._icon(kind, False))
            self._hide_tip()
        b.bind("<Enter>", enter)
        b.bind("<Leave>", leave)
        b.bind("<ButtonRelease-1>", lambda _e: command())
        return b

    # -- hover hints: a little window of their own, below the panel ------------
    def _tip_later(self, widget, text):
        self._hide_tip()
        if self.top is not None:
            self._tip_job = self.top.after(TIP_DELAY_MS, lambda: self._show_tip(widget, text))

    def _show_tip(self, widget, text):
        tk, t, top = self.tk, self.t, self.top
        self._tip_job = None
        if top is None:
            return
        tip = self._tip = tk.Toplevel(top)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=t.BORDER)
        tk.Label(tip, text=text, bg=t.RAISED, fg=t.TEXT, font=self._small,
                 padx=self._px(8), pady=self._px(3)).pack(padx=1, pady=1)
        tip.update_idletasks()
        try:
            # Never takes the keyboard: the box keeps it (and the panel stays open).
            hwnd = int(tip.wm_frame(), 16)
            ex = _user32.GetWindowLongW(wintypes.HWND(hwnd), -20)
            _user32.SetWindowLongW(wintypes.HWND(hwnd), -20, ex | 0x08000000 | 0x80)
        except Exception:
            pass
        l, tp = widget.winfo_rootx(), widget.winfo_rooty()
        x, y = tip_place((l, tp, l + widget.winfo_width(), tp + widget.winfo_height()),
                         (tip.winfo_reqwidth(), tip.winfo_reqheight()),
                         top.winfo_rooty() + top.winfo_height(), self._work, gap=self._px(6))
        tip.geometry("+%d+%d" % (x, y))
        tip.deiconify()

    def _hide_tip(self):
        if self._tip_job is not None and self.top is not None:
            try:
                self.top.after_cancel(self._tip_job)
            except Exception:
                pass
        self._tip_job = None
        tip, self._tip = self._tip, None
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass

    def _style_window(self):
        """Windows 11's rounded corners and thin border, as on the overlay."""
        try:
            self.top.update_idletasks()
            hwnd = self._hwnd = int(self.top.wm_frame(), 16)
            dwm = ctypes.windll.dwmapi
            ok = dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33,
                                           ctypes.byref(ctypes.c_int(2)), 4) == 0
            c = self.t.BORDER.lstrip("#")
            colorref = int(c[4:6], 16) << 16 | int(c[2:4], 16) << 8 | int(c[0:2], 16)
            ok = ok and dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34,
                                                  ctypes.byref(ctypes.c_int(colorref)), 4) == 0
            if ok:
                self.top.configure(bg=self.t.SURFACE)
                self._card.pack_configure(padx=0, pady=0)
        except Exception:
            self._hwnd = None

    def _take_focus(self, tries=4):
        if self.top is None:
            return
        try:
            set_foreground(getattr(self, "_hwnd", None))
            self.top.lift()
            self.text.focus_force()
            if self.top.focus_displayof() is None and tries > 0:
                self.top.after(60, lambda: self._take_focus(tries - 1))
        except Exception:
            pass

    def close(self, restore=True, notify=True):
        """Close. restore: give the keyboard back to the window you were in
        (not after a click elsewhere: that window already has it)."""
        if self.top is not None:
            self._hide_tip()
        top, self.top = self.top, None
        self._placed = False
        if top is None:
            return
        draft = ""
        try:
            draft = self.text.get("1.0", "end-1c").strip()
        except Exception:
            pass
        try:
            top.destroy()
        except Exception:
            pass
        if restore and self._prev and _user32.IsWindow(self._prev):
            set_foreground(self._prev)
        cb = self._cb.get("closed")
        if notify and cb:
            cb(draft)

    def set_triggers(self, triggers):
        """The trigger list arrived late (another PC's): use it from now on."""
        self._triggers = list(triggers or ())
        if self.top is not None:
            self._refresh()

    def _focus_check(self):
        """Clicked somewhere else: close, leaving the focus where it went."""
        top = self.top
        if top is None or time.monotonic() - self._opened < 0.4:
            return

        def check():
            if self.top is top and top.focus_displayof() is None:
                self.close(restore=False)
        top.after(120, check)

    # -- the box -------------------------------------------------------------
    def value(self):
        return self.text.get("1.0", "end-1c")

    def _resize_box(self):
        tx = self.text
        try:
            tx.update_idletasks()
            n = tx.count("1.0", "end-1c", "displaylines")
            n = n[0] if isinstance(n, tuple) else (n or 0)
        except Exception:
            n = self.value().count("\n")
        lines = lines_shown(n + 1)
        if lines != self._lines or not self._box.itemcget(self._box_img, "image"):
            self._lines = lines
            tx.configure(height=lines)
            h = self._font.metrics("linespace") * lines + 2 * self._ipad[1]
            self._box.configure(height=h)
            self._box.itemconfigure(self._box_img, image=self._rounded(
                self._w, h, self._px(10), self.t.RAISED, self.t.SURFACE,
                outline=self.t.BORDER, width=1.0))
            self._reposition()
        tx.see("insert")

    def _reposition(self):
        """Size and place the panel after anything in it changed, in one step:
        the text box stays on the same screen row (lists and hints open above
        it), unless that would push the panel off the screen."""
        top = self.top
        if top is None or not getattr(self, "_placed", False):
            return
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        y, self._box_y = keep_box_still(self._box_y, self._box_offset(h), h, self._work,
                                        gap=self._px(8))
        top.geometry("%dx%d+%d+%d" % (w, h, self._x, y))

    def _on_modified(self, _e=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if len(self.value()) > INSTRUCTION_MAX:
            self.text.delete("1.0 + %d chars" % INSTRUCTION_MAX, "end-1c")
        self._resize_box()
        self._refresh()

    def _refresh(self):
        """Placeholder, the trigger list and the "Trigger: ..." line."""
        tk, t = self.tk, self.t
        value = self.value()
        if value:
            self._ghost.place_forget()
        else:
            self._ghost.place(x=0, y=0)
        found = [] if value == self._dismissed_for else suggest(value, self._triggers)
        if found != self._suggestions:
            self._suggestions, self._picked = found, 0
        for child in self._list.winfo_children():
            child.destroy()
        if found:
            for n, trig in enumerate(found):
                on = n == self._picked
                bg = t.RAISED if on else t.SURFACE
                row = tk.Frame(self._list, bg=bg, cursor="hand2")
                # The picked row is marked with an arrow as well as a lighter band.
                mark = tk.Label(row, text="▸" if on else " ", bg=bg, fg=t.TEXT,
                                font=self._semi, width=2)
                mark.pack(side=tk.LEFT)
                name = tk.Label(row, text=trig.get("name") or trig["phrases"][0], bg=bg,
                                fg=t.TEXT, font=self._semi, anchor="w")
                name.pack(side=tk.LEFT, pady=self._px(2))
                phrase = tk.Label(row, text=trig["phrases"][0], bg=bg, fg=t.MUTED,
                                  font=self._small, anchor="e")
                phrase.pack(side=tk.RIGHT, padx=(self._px(8), self._px(10)))
                for wdg in (row, mark, name, phrase):
                    wdg.bind("<Button-1>", lambda _e, i=n: self._accept(i))
                row.pack(fill=tk.X)
            self._list.pack(fill=tk.X, pady=(0, self._px(5)), before=self._box)
        else:
            self._list.pack_forget()
        hit = chosen(value, self._triggers)
        if hit is not None:
            self._status.configure(text="Trigger: %s" % (hit.get("name") or ""))
            self._status.pack(fill=tk.X, padx=self._px(2), pady=(0, self._px(4)),
                              before=self._box)
        else:
            self._status.pack_forget()
        self._reposition()

    def _move(self, step):
        if not self._suggestions:
            return None        # the arrow moves the text cursor as usual
        self._picked = (self._picked + step) % len(self._suggestions)
        self._refresh()
        return "break"

    def _accept(self, index=None):
        if not self._suggestions:
            return
        trig = self._suggestions[self._picked if index is None else index]
        self.text.delete("1.0", "end")
        self.text.insert("1.0", completion(trig))
        self.text.mark_set("insert", "end-1c")
        self.text.focus_set()
        self._on_modified()

    def _on_tab(self, _e=None):
        if self._suggestions:
            self._accept()
        return "break"

    def _on_escape(self, _e=None):
        if self._suggestions:
            self._dismissed_for = self.value()   # hide the list until the text changes
            self._refresh()
        else:
            self.close(restore=True)
        return "break"

    def _on_return(self, e=None):
        if e is not None and (e.state & 0x1):
            return None        # Shift+Enter: a new line
        if self._suggestions:
            self._accept()
            return "break"
        self.submit()
        return "break"

    def submit(self):
        value = self.value().strip()
        if not value:
            return
        cb, include = self._cb.get("submit"), bool(self._selection and self._include)
        self.close(restore=True, notify=False)
        if cb:
            cb(value, include)

    def _toggle_include(self):
        if self._sel_box is None:
            return
        self._include = not self._include
        self._sel_box.configure(image=self._check(self._include, self.t.SURFACE))
        self.text.focus_set()

    def _close_hint(self):
        if self._hint_row is not None:
            self._hint_row.destroy()
            self._hint_row = None
            self._reposition()
        cb = self._cb.get("hint")
        if cb:
            cb()
        self.text.focus_set()

    def _act(self, which):
        cb = self._cb.get(which)
        self.close(restore=True)
        if cb:
            cb()


# --- try it: python client/prompt_panel.py [--selection] -------------------------

def _demo():
    import sys
    import tkinter as tk
    import theme
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    triggers = [
        {"name": "Bug ticket", "phrases": ["prompt ticket", "ticket this"]},
        {"name": "Shorter", "phrases": ["prompt shorter"]},
        {"name": "Spanish", "phrases": ["prompt Spanish"]},
    ]
    panel = PromptPanel(root, scale, theme)
    sel = "see you tmrw at the usual place" if "--selection" in sys.argv else ""
    panel.open(app_label="Notepad", selection=sel, triggers=triggers,
               on_submit=lambda text, inc: (print("SUBMIT", inc, repr(text)), root.destroy()),
               on_snooze=lambda: (print("SNOOZE"), root.destroy()),
               on_never=lambda: (print("NEVER"), root.destroy()),
               on_hint_closed=lambda: print("HINT CLOSED"),
               on_closed=lambda draft: (print("CLOSED", repr(draft)), root.after(50, root.destroy)))
    root.mainloop()


if __name__ == "__main__":
    _demo()
