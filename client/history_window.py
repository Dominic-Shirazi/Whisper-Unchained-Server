"""The History window (tray menu > History...): search past dictations on this
PC and copy one again, or play its recording when the clip was kept.

Reads api/transcripts.log (history.py); never changes it.
Run: pythonw client/history_window.py
"""
import os
import sys
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history  # noqa: E402
import settings_gui as sg  # noqa: E402

SHOW_MAX = 3000   # rows in the list; searching narrows it
TRUE = ("1", "true", "yes", "on")


def row_label(e):
    try:
        when = time.strftime("%b %d  %H:%M", time.strptime(e.when, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        when = e.when
    text = " ".join(e.text.split())
    return "%s    %s" % (when, text[:110] + ("..." if len(text) > 110 else ""))


class HistoryWindow:
    def __init__(self, root, env, entries):
        self.root, self.env = root, env
        self.all = entries
        self.shown = []

        root.title("History")
        root.configure(bg=sg.BG)
        root.geometry("900x640")
        root.minsize(600, 420)
        sg.set_window_icon(root)
        sg.dark_title_bar(root)
        sg.style_scrollbar(ttk.Style())

        pad = dict(padx=24)
        tk.Label(root, text="History", font=sg.F_TITLE, bg=sg.BG,
                 fg=sg.TEXT).pack(anchor="w", pady=(20, 2), **pad)
        note = "Your past dictations on this PC, newest first. Search, then copy one again."
        if (env.get("LOG_TRANSCRIPTS") or "").strip().lower() not in TRUE:
            note += ("\nThe transcript log is off, so new dictations aren't added. Turn on "
                     "\"Keep a log of transcripts\" in Settings > Audio Corpus.")
        tk.Label(root, text=note, font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED, justify="left",
                 wraplength=840).pack(anchor="w", **pad)

        tk.Label(root, text="Search", font=sg.F_LABEL, bg=sg.BG, fg=sg.TEXT,
                 anchor="w").pack(fill="x", pady=(14, 0), **pad)
        self.query = tk.StringVar(root)
        search = tk.Entry(root, textvariable=self.query, font=sg.F_BODY, bg=sg.RAISED,
                          fg=sg.TEXT, relief="flat", insertbackground=sg.ACCENT,
                          highlightthickness=1, highlightbackground=sg.BORDER,
                          highlightcolor=sg.ACCENT, borderwidth=6)
        search.pack(fill="x", pady=(4, 4), **pad)
        self.count = tk.Label(root, text="", font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED, anchor="w")
        self.count.pack(fill="x", **pad)

        # Bottom part first, so a short window shrinks the list, not the buttons.
        bar = tk.Frame(root, bg=sg.BG)
        bar.pack(side="bottom", fill="x", pady=(0, 18), **pad)
        self.copy_btn = sg.Button(bar, "Copy", self.copy, kind="primary")
        self.copy_raw_btn = sg.Button(bar, "Copy what Whisper heard", self.copy_raw)
        self.play_btn = sg.Button(bar, "Play recording", self.play)
        for b in (self.copy_btn, self.copy_raw_btn, self.play_btn):
            b.pack(side="left", padx=(0, 8))
        self.status = tk.Label(bar, text="", font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED)
        self.status.pack(side="left", padx=(8, 0))

        detail_box = tk.Frame(root, bg=sg.BORDER, padx=1, pady=1)
        detail_box.pack(side="bottom", fill="x", pady=(8, 10), **pad)
        self.detail = tk.Text(detail_box, height=7, wrap="word", font=sg.F_BODY, bg=sg.RAISED,
                              fg=sg.TEXT, relief="flat", padx=12, pady=10,
                              insertbackground=sg.ACCENT)
        self.detail.pack(fill="x")

        list_box = tk.Frame(root, bg=sg.RAISED, highlightthickness=1,
                            highlightbackground=sg.BORDER, padx=8, pady=6)
        list_box.pack(fill="both", expand=True, pady=(6, 0), **pad)
        self.list = tk.Listbox(list_box, font=sg.F_HELP, bg=sg.RAISED, fg=sg.TEXT,
                               selectbackground=sg.ACCENT, selectforeground=sg.ON_ACCENT,
                               activestyle="none", relief="flat", highlightthickness=0,
                               borderwidth=0)
        scroll = ttk.Scrollbar(list_box, orient="vertical", command=self.list.yview,
                               style="Vertical.TScrollbar")
        self.list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list.pack(side="left", fill="both", expand=True)
        self.list.bind("<<ListboxSelect>>", lambda e: self.show_selected())
        self.list.bind("<Double-Button-1>", lambda e: self.copy())

        self._pending = None
        self.query.trace_add("write", lambda *a: self._schedule_filter())
        self.filter()
        search.focus_set()

    def _schedule_filter(self):
        if self._pending:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(200, self.filter)

    def filter(self):
        self._pending = None
        found = history.search(self.all, self.query.get())
        self.shown = found[:SHOW_MAX]
        self.list.delete(0, "end")
        for e in self.shown:
            self.list.insert("end", row_label(e))
        if not self.all:
            self.count.config(text="No dictations logged yet.")
        elif len(found) > len(self.shown):
            self.count.config(text="%d found, showing the newest %d. Search to narrow it down."
                              % (len(found), len(self.shown)))
        else:
            self.count.config(text="%d found." % len(found))
        if self.shown:
            self.list.selection_set(0)
        self.show_selected()

    def selected(self):
        sel = self.list.curselection()
        return self.shown[sel[0]] if sel else None

    def show_selected(self):
        e = self.selected()
        self.detail.delete("1.0", "end")
        if e is None:
            for b in (self.copy_btn, self.copy_raw_btn, self.play_btn):
                b.set_enabled(False)
            return
        self.detail.insert("1.0", e.text)
        if e.final is not None and e.final != e.raw:
            self.detail.insert("end", "\n\nWhat Whisper heard:\n" + e.raw)
        self.copy_btn.set_enabled(True)
        self.copy_raw_btn.set_enabled(e.final is not None and e.final != e.raw)
        self.play_btn.set_enabled(bool(e.audio_path()))
        self.status.config(text="")

    def _to_clipboard(self, text):
        try:
            import clipboard_win
            if clipboard_win.wanted(self.env):
                clipboard_win.set_text(text, private=True)
                return
        except Exception:
            pass
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def copy(self):
        e = self.selected()
        if e:
            self._to_clipboard(e.text)
            self.status.config(text="Copied.")

    def copy_raw(self):
        e = self.selected()
        if e:
            self._to_clipboard(e.raw)
            self.status.config(text="Copied what Whisper heard.")

    def play(self):
        e = self.selected()
        path = e.audio_path() if e else ""
        if path:
            try:
                os.startfile(path)   # the user's default player
            except OSError as err:
                self.status.config(text="Couldn't play it: %s" % err)


def main():
    env = dict(os.environ)
    env.update(sg.read_env())
    root = tk.Tk()
    HistoryWindow(root, env, history.load())
    root.after_idle(lambda: sg.round_widgets(root))   # Windows 11 corners
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
