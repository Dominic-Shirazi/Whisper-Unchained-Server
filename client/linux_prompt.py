"""Type an instruction for the AI instead of saying it (Linux).

    python -m client.linux_prompt

A small box: type "make this friendlier" (or start typing a skill's
phrase and pick it), press Enter. With text highlighted in the app you came
from, the AI's version replaces it; without, the answer is pasted where you
were. The Linux counterpart of the typed box on Windows' floating mic button
(prompt_panel.py); opened from the tray menu or PROMPT_HOTKEY in .env.

X11's "highlighted text" is the last thing highlighted anywhere, which may be
old, so the box shows what it would change and lets you untick it.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import linux_listener as ll  # noqa: E402
import transcribe_client  # noqa: E402

SUGGEST_MAX = 6
PREVIEW_CHARS = 90


# --- matching skills to       what's typed -----------------------------------------
# prompt_panel.py's own functions where that module imports (it needs Windows
# today); otherwise these, which follow the same rules more simply.

def _words(s):
    return "".join(c.lower() if c.isalnum() else " " for c in (s or "")).split()


def suggest(text, triggers, limit=SUGGEST_MAX):
    """Custom triggers to offer for what has been typed: "/" lists them all,
    otherwise two letters or more matched against the start of a phrase or
    name, then the start of any word in them."""
    raw = (text or "").strip()
    items = [t for t in (triggers or ()) if t.get("phrases")]
    if "\n" in raw:
        return []
    if raw.startswith("/") and not raw.lstrip("/").strip():
        return items[:limit]
    q = " ".join(_words(raw.lstrip("/")))
    if len(q) < 2 or len(q) > 40:
        return []
    ranked = []
    for n, trig in enumerate(items):
        name = " ".join(_words(trig.get("name")))
        phrases = [" ".join(_words(p)) for p in trig["phrases"]]
        if q in phrases:
            return []                    # a whole phrase is typed: it's chosen
        if any(p.startswith(q) for p in phrases):
            rank = 0
        elif name.startswith(q):
            rank = 1
        elif any((" " + s).find(" " + q) >= 0 for s in phrases + [name]):
            rank = 2
        else:
            continue
        ranked.append((rank, n, trig))
    return [t for _r, _n, t in sorted(ranked, key=lambda x: x[:2])[:limit]]


def completion(trigger):
    """What picking a suggestion puts in the box: the trigger's first phrase
    and a space, ready for more words."""
    return (trigger.get("phrases") or [""])[0] + " "


try:
    from prompt_panel import completion, suggest  # noqa: E402,F811
except Exception:
    pass


def preview(selection, limit=PREVIEW_CHARS):
    """One line of the highlighted text, for the tick box."""
    flat = " ".join((selection or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


# --- the box ------------------------------------------------------------------------

class PromptBox:
    def __init__(self, desk, target, env, edit=transcribe_client.edit_text):
        import tkinter as tk
        import tkinter.font as tkfont
        import theme as t
        self.tk, self.t = tk, t
        self.desk, self.target, self.env, self.edit = desk, target, env, edit
        self.clip = ll.clipboard_tool(ll.session_type())
        self.triggers = transcribe_client.custom_triggers(env)
        self.busy = False

        root = self.root = tk.Tk(className=ll.SLUG)
        root.title("%s: tell the AI what to do" % ll.APP_NAME)
        root.configure(bg=t.BG)
        root.resizable(False, False)
        fams = set(tkfont.families(root))
        font = next((f for f in ("Noto Sans", "Ubuntu", "Cantarell", "DejaVu Sans") if f in fams),
                    "TkDefaultFont")
        pad = tk.Frame(root, bg=t.BG)
        pad.pack(padx=18, pady=14)
        tk.Label(pad, text="Tell the AI what to do", bg=t.BG, fg=t.TEXT,
                 font=(font, 13, "bold")).pack(anchor="w")
        self.use_sel = tk.BooleanVar(root, value=bool(target.selection.strip()))
        if target.selection.strip():
            tk.Checkbutton(pad, text="Change the highlighted text:  “%s”"
                           % preview(target.selection), variable=self.use_sel,
                           bg=t.BG, fg=t.MUTED, selectcolor=t.RAISED, activebackground=t.BG,
                           activeforeground=t.TEXT, font=(font, 10), anchor="w",
                           wraplength=520, justify="left", highlightthickness=0, bd=0
                           ).pack(anchor="w", pady=(6, 0))
        # exportselection off on both: a Tk widget that selects something takes X's
        # PRIMARY selection, and the app we came from then drops its highlight.
        self.entry = tk.Entry(pad, width=58, exportselection=False, bg=t.RAISED, fg=t.TEXT, insertbackground=t.TEXT,
                              relief="flat", font=(font, 12), highlightthickness=1,
                              highlightbackground=t.BORDER, highlightcolor=t.ACCENT)
        self.entry.pack(fill="x", pady=(10, 0), ipady=6)
        self.list = tk.Listbox(pad, height=0, exportselection=False, bg=t.SURFACE, fg=t.TEXT, relief="flat",
                               font=(font, 11), highlightthickness=0, activestyle="none",
                               selectbackground=t.ACCENT, selectforeground=t.ON_ACCENT)
        self.shown = []
        hint = "Enter to send, Esc to close"
        if self.triggers:
            hint += ". Type / for your %d skill%s" % (
                len(self.triggers), "" if len(self.triggers) == 1 else "s")
        self.status = tk.Label(pad, text=hint, bg=t.BG, fg=t.FAINT, font=(font, 10),
                               wraplength=520, justify="left")
        self.status.pack(anchor="w", pady=(8, 0))

        self.entry.bind("<KeyRelease>", self._typed)
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Down>", lambda e: self._move(1))
        self.entry.bind("<Up>", lambda e: self._move(-1))
        self.list.bind("<ButtonRelease-1>", lambda e: self._pick())
        root.bind("<Escape>", lambda e: root.destroy())
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry("+%d+%d" % ((root.winfo_screenwidth() - w) // 2,
                                  root.winfo_screenheight() // 3))
        root.after(50, self.entry.focus_force)

    # -- suggestions -----------------------------------------------------------------
    def _typed(self, event=None):
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape"):
            return
        self.shown = suggest(self.entry.get(), self.triggers)
        self.list.delete(0, "end")
        for trig in self.shown:
            self.list.insert("end", "  %s   ·   “%s”" % (trig.get("name", ""), completion(trig).strip()))
        if self.shown:
            self.list.config(height=len(self.shown))
            self.list.pack(fill="x", pady=(4, 0), before=self.status)
        else:
            self.list.pack_forget()

    def _move(self, step):
        if not self.shown:
            return "break"
        cur = self.list.curselection()
        i = (cur[0] + step if cur else (0 if step > 0 else len(self.shown) - 1)) % len(self.shown)
        self.list.selection_clear(0, "end")
        self.list.selection_set(i)
        return "break"

    def _pick(self):
        cur = self.list.curselection()
        if not cur or cur[0] >= len(self.shown):
            return False
        self.entry.delete(0, "end")
        self.entry.insert(0, completion(self.shown[cur[0]]))
        self.shown = []
        self.list.delete(0, "end")
        self.list.pack_forget()
        self.entry.focus_set()
        return True

    # -- sending ---------------------------------------------------------------------
    def _enter(self, _event=None):
        if self.busy or self._pick():
            return
        instruction = self.entry.get().strip()
        if not instruction:
            return
        selection = self.target.selection if self.use_sel.get() else ""
        self.busy = True
        self.entry.config(state="disabled")
        self.status.config(text="Working...", fg=self.t.MUTED)
        threading.Thread(target=self._work, args=(instruction, selection), daemon=True).start()

    def _work(self, instruction, selection):
        try:
            text, ok = self.edit(self.env, instruction, selection)
            error = None if ok and text else "The AI didn't answer. Is one set up in Settings > AI Editing?"
        except Exception as e:
            text, error = "", (str(e) if isinstance(e, transcribe_client.TranscribeError)
                               else "Error: %s" % e.__class__.__name__)
        self.root.after(0, lambda: self._done(text, error))

    def _done(self, text, error):
        if error:
            self.busy = False
            self.entry.config(state="normal")
            self.status.config(text=error, fg=self.t.DANGER)
            return
        self.root.withdraw()
        self.root.update()
        outcome = deliver(text, self.desk, self.target, self.env, self.clip)
        ll._log("typed instruction: %d characters -> %s (%s)"
                % (len(text), outcome, self.target.exe or "?"))
        if outcome == "pasted":
            self.root.destroy()
            return
        self.status.config(text="It's on the clipboard: press Ctrl+V where you want it."
                           if outcome == "clipboard" else
                           "Can't reach the clipboard: install xclip (or wl-clipboard).",
                           fg=self.t.WARN)
        self.root.deiconify()
        self.root.after(4000, self.root.destroy)


def deliver(text, desk, target, env, clip, sleep=time.sleep):
    """Put the answer where the box was opened from. 'pasted', 'clipboard'
    (couldn't get back there: nothing pasted) or 'failed' (no clipboard)."""
    if not ll.set_clipboard(text, clip):
        return "failed"
    if desk is None or not target.wid or not desk.activate(target.wid):
        return "clipboard"
    sleep(0.2)                           # the window settles its focus
    desk.send(ll.paste_keys(env, target.exe, target.cls), gap=ll.PASTE_KEY_GAP)
    return "pasted"


def main():
    session = ll.session_type()
    if not session:
        print("No desktop session (DISPLAY is not set).", file=sys.stderr)
        return 1
    desk = None
    target = ll.Target()
    clip = ll.clipboard_tool(session)
    if session == "x11":
        import linux_x11
        desk = linux_x11.Desktop()
        time.sleep(0.25)                 # a tray menu is still closing: let the app get its focus back
        wid = desk.active_window()
        exe, cls = desk.program(wid)
        target = ll.Target(wid, exe, cls)
    target.selection = ll.highlighted_text(clip)
    PromptBox(desk, target, ll._env()).root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
