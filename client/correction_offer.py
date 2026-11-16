"""The small card that offers to remember a correction (correction_learner.py).

        Corey  →  Cory
        Always make this correction?
                      [Always]  [Not now]  [Never for this]

It sits at the bottom right of the screen you're working on and never takes
the keyboard (you can go on typing). It closes by itself after a few seconds,
which counts as "Not now". Runs on the listener's Tk thread.
"""
import ctypes
from ctypes import wintypes

import correction_learner as cl

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


class CorrectionOffer:
    def __init__(self, root, scale=1.0, theme=None):
        import tkinter as tk
        from tkinter import font as tkfont
        if theme is None:
            import theme
        self.tk, self.t, self.root = tk, theme, root
        self.scale = max(1.0, float(scale))
        self.top = None
        self._job = None
        self._big = tkfont.Font(root=root, family=theme.FONT_SB, size=12)
        self._text = tkfont.Font(root=root, family=theme.FONT, size=10)
        self._small = tkfont.Font(root=root, family=theme.FONT, size=9)

    def _px(self, n):
        return int(round(n * self.scale))

    def close(self):
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self.top is not None:
            try:
                self.top.destroy()
            except Exception:
                pass
            self.top = None

    def show(self, pair, on_answer, work=None, seconds=cl.OFFER_SECONDS):
        """pair: (heard, corrected). on_answer("always" | "later" | "never")
        is called once; the timeout answers "later"."""
        self.close()
        tk, t = self.tk, self.t
        top = self.top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=t.BORDER)
        card = tk.Frame(top, bg=t.SURFACE)
        card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        body = tk.Frame(card, bg=t.SURFACE)
        body.pack(padx=self._px(16), pady=self._px(12))
        tk.Label(body, text=cl.offer_line(pair), bg=t.SURFACE, fg=t.TEXT, font=self._big,
                 anchor="w", justify="left", wraplength=self._px(380)).pack(fill=tk.X)
        tk.Label(body, text=cl.OFFER_QUESTION, bg=t.SURFACE, fg=t.MUTED, font=self._text,
                 anchor="w").pack(fill=tk.X, pady=(self._px(2), self._px(10)))
        bar = tk.Frame(body, bg=t.SURFACE)
        bar.pack(fill=tk.X)
        answered = {"done": False}

        def answer(which):
            if answered["done"]:
                return
            answered["done"] = True
            self.close()
            try:
                on_answer(which)
            except Exception:
                pass

        always, later, never = cl.OFFER_BUTTONS
        for words, which, strong in ((never, "never", False), (later, "later", False),
                                     (always, "always", True)):
            fg, bg, hov = ((t.ON_ACCENT, t.ACCENT, t.ACCENT_H) if strong
                           else (t.TEXT, t.RAISED, t.HOVER))
            b = tk.Label(bar, text=words, bg=bg, fg=fg, font=self._small, cursor="hand2",
                         padx=self._px(12), pady=self._px(5))
            b.bind("<Enter>", lambda _e, w=b, c=hov: w.configure(bg=c))
            b.bind("<Leave>", lambda _e, w=b, c=bg: w.configure(bg=c))
            b.bind("<ButtonRelease-1>", lambda _e, w=which: answer(w))
            b.pack(side=tk.RIGHT, padx=(self._px(6), 0))

        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        if work is None:
            work = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        m = self._px(24)
        top.geometry("%dx%d+%d+%d" % (w, h, work[2] - w - m, work[3] - h - m))
        self._no_activate(top)
        top.deiconify()
        self._job = self.root.after(int(seconds * 1000), lambda: answer("later"))
        return self

    def _no_activate(self, top):
        """Shown without taking the keyboard, and clicks don't take it either."""
        if _user32 is None:
            return
        try:
            hwnd = _user32.GetParent(wintypes.HWND(top.winfo_id())) or top.winfo_id()
            ex = _user32.GetWindowLongW(wintypes.HWND(hwnd), -20)
            _user32.SetWindowLongW(wintypes.HWND(hwnd), -20, ex | 0x08000000 | 0x80)
        except Exception:
            pass
