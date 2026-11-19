"""Two dialogs for skills (Settings > Skills). A skill is a "custom trigger"
in the code, the word file and the server's API; only the wording changed.

* TriggerEditor: write or change one skill (name, the phrases to say, what
  the AI should do). A new one opens with a short explanation and an example.
* LibraryBrowser: the shared library, with an Add button per skill.

What a skill is and how it runs: api/custom_triggers.py. Where the library
comes from: client/trigger_library.py.

Both follow the Settings look (settings_gui's palette, fonts and buttons) and
never show state by colour alone: "Added" is a word and a tick, "Needs a strong
model" is written out.
"""
import threading

import trigger_library
import custom_triggers  # api/custom_triggers.py (trigger_library puts api/ on sys.path)

MODEL_NOTES = {"any": "Works with small local models",
               "strong": "Needs a strong model"}
SMALL_ONLY = "Small local models only"
OPEN, FOLDED = "\u25be", "\u25b8"      # the arrows before a heading or a skill's name
FILTER_ROW_WIDTH = 660
EDITOR_NAME_HINT = "e.g. Bug ticket"
EDITOR_PHRASE_HELP = ("One phrase per line. Pick words you wouldn't say by accident, "
                      "such as \"prompt ticket\" or \"skill ticket\".")
EDITOR_EXAMPLE_SHOW, EDITOR_EXAMPLE_HIDE = "See an example", "Hide the example"
EDITOR_LEAVE_WARNING = ("That closes this window without saving what you typed. "
                        "Click the link again to go to the library.")
EDITOR_PROMPT_HELP = ("Write it the way you'd explain the job to a person. It is applied to "
                      "what you dictated, or to the text you highlighted first.")
LIBRARY_HELP = ("Skills other people have shared. Add one, then change its phrase or "
                "instruction to suit you.\n"
                "• A skill is only instructions for your AI: read them before adding")


def _sg():
    import settings_gui
    return settings_gui


def _text_box(sg, tk, parent, height, width=64):
    """A multi-line box in the Settings style, with its own slim scrollbar."""
    frame = tk.Frame(parent, bg=sg.RAISED, highlightthickness=1,
                     highlightbackground=sg.BORDER, highlightcolor=sg.ACCENT)
    box = tk.Text(frame, font=sg.F_BODY, bg=sg.RAISED, fg=sg.TEXT, relief="flat",
                  insertbackground=sg.ACCENT, wrap="word", height=height, width=width,
                  borderwidth=6, highlightthickness=0, undo=True)
    scroll = sg.SlimScrollbar(frame, box.yview)
    scroll.configure(height=1)      # a canvas asks for 267 px by default; the text decides
    box.configure(yscrollcommand=scroll.set)
    box.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    box.bind("<FocusIn>", lambda e: frame.config(highlightbackground=sg.ACCENT), add="+")
    box.bind("<FocusOut>", lambda e: frame.config(highlightbackground=sg.BORDER), add="+")
    # Tab moves to the next box (it would otherwise type a tab into the text).
    box.bind("<Tab>", lambda e: (e.widget.tk_focusNext().focus_set(), "break")[1])
    box.bind("<Shift-Tab>", lambda e: (e.widget.tk_focusPrev().focus_set(), "break")[1])
    # The wheel scrolls this box, not the Settings page behind the dialog.
    box.bind("<MouseWheel>", lambda e: (box.yview_scroll(int(-e.delta / 120), "units"), "break")[1])
    return frame, box


def _place(sg, tk, top, parent, minsize):
    """Centre over the parent window and make the dialog modal."""
    top.update_idletasks()
    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
    try:
        p = parent.winfo_toplevel()
        x = p.winfo_rootx() + (p.winfo_width() - w) // 2
        y = p.winfo_rooty() + max(0, (p.winfo_height() - h) // 3)
    except tk.TclError:
        x, y = 200, 120
    top.geometry("+%d+%d" % (max(0, x), max(0, y)))
    top.minsize(*minsize)
    top.deiconify()
    sg.dark_title_bar(top)
    try:
        top.grab_set()
    except tk.TclError:
        pass


class TriggerEditor:
    """Write or change one skill. `check(item)` returns an error to show (the
    phrase is already used, for example) or None; result is the skill as a
    dict, or None if cancelled. `open_library` is True when a new skill was
    abandoned for the example's "Add ... from the library" link."""

    def __init__(self, parent, item=None, check=None):
        import tkinter as tk
        sg = self.sg = _sg()
        self.tk = tk
        self.item = dict(item or {})
        self.check = check
        self.result = None
        self.open_library = False
        new = not item

        top = self.top = tk.Toplevel(parent)
        top.withdraw()
        top.title("New skill" if new else "Edit skill")
        top.configure(bg=sg.BG)
        try:
            top.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass
        sg.set_window_icon(top)

        # The boxes on the left; a new skill's example opens beside them (a
        # taller window wouldn't fit a laptop screen).
        outer = tk.Frame(top, bg=sg.BG)
        outer.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        body = tk.Frame(outer, bg=sg.BG)
        body.pack(side="left", fill="both", expand=True)
        self.example = tk.Frame(outer, bg=sg.BG)        # packed by toggle_example

        def heading(text, help_text=""):
            tk.Label(body, text=text, font=sg.F_SECTION, bg=sg.BG, fg=sg.TEXT,
                     anchor="w").pack(fill="x", pady=(14, 0))
            if help_text:
                tk.Label(body, text=help_text, font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED,
                         anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(2, 0))

        if new:
            self._build_help(body)
        tk.Label(body, text="Name", font=sg.F_SECTION, bg=sg.BG, fg=sg.TEXT,
                 anchor="w").pack(fill="x", pady=(14 if new else 0, 0))
        self.name = tk.StringVar(value=self.item.get("name", ""))
        self.name_entry = sg._entry(body, self.name, 40)
        self.name_entry.pack(anchor="w", pady=(6, 0))
        sg.Placeholder(self.name_entry, self.name, EDITOR_NAME_HINT)

        heading("Say this", EDITOR_PHRASE_HELP)
        frame, self.phrases = _text_box(sg, tk, body, height=3, width=40)
        frame.pack(anchor="w", pady=(6, 0))
        self.phrases.insert("1.0", "\n".join(self.item.get("phrases", [])))

        heading("What the AI should do", EDITOR_PROMPT_HELP)
        frame, self.prompt = _text_box(sg, tk, body, height=10 if new else 13, width=70)
        frame.pack(fill="both", expand=True, pady=(6, 0))
        self.prompt.insert("1.0", self.item.get("prompt", ""))

        foot = tk.Frame(top, bg=sg.SURFACE)
        foot.pack(fill="x", pady=(18, 0))
        self.status = tk.Label(foot, font=sg.F_HELP, bg=sg.SURFACE, fg=sg.MUTED, anchor="w",
                               justify="left", wraplength=380)
        self.status.pack(side="left", padx=24)
        sg.Button(foot, "Cancel", self.cancel).pack(side="right", padx=(6, 22), pady=14)
        sg.Button(foot, "Save skill" if new else "Done", self.ok,
                  kind="primary").pack(side="right", pady=14)
        top.bind("<Escape>", lambda e: self.cancel())
        top.protocol("WM_DELETE_WINDOW", self.cancel)

    def _build_help(self, body):
        """What a skill is, on top of a new one: three lines, and a worked
        example that "See an example" opens beside the boxes (the text is
        settings_gui's)."""
        sg, tk = self.sg, self.tk
        sg.HelpText(body, sg.CUSTOM_TRIGGERS_HELP, wraplength=600).pack(anchor="w")
        self.example_link = tk.Label(body, font=sg.F_HELP, bg=sg.BG, fg=sg.ACCENT,
                                     cursor="hand2", takefocus=1, anchor="w")
        self.example_link.pack(anchor="w", pady=(6, 0))
        box = self.example
        tk.Label(box, text="An example", font=sg.F_SECTION, bg=sg.BG, fg=sg.TEXT,
                 anchor="w").pack(anchor="w")
        self.example_text = sg.example_box(box, sg.CUSTOM_TRIGGER_EXAMPLE, padx=0, width=50)
        self.library_link = tk.Label(box, text=sg.CUSTOM_TRIGGER_EXAMPLE_LINK, font=sg.F_HELP,
                                     bg=sg.BG, fg=sg.ACCENT, cursor="hand2", anchor="w")
        self.library_link.pack(anchor="w", pady=(6, 0))
        self.library_link.bind("<Button-1>", lambda e: self.go_to_library())
        tk.Label(box, text="How skills work", font=sg.F_SECTION, bg=sg.BG, fg=sg.TEXT,
                 anchor="w").pack(anchor="w", pady=(18, 0))
        sg.HelpText(box, sg.CUSTOM_TRIGGERS_BULLETS, wraplength=350).pack(
            anchor="w", pady=(4, 0))
        tk.Label(box, text=sg.CUSTOM_TRIGGERS_FOOT, font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED,
                 anchor="w", justify="left", wraplength=370).pack(anchor="w", pady=(12, 0))
        for ev in ("<Button-1>", "<space>", "<Return>"):
            self.example_link.bind(ev, lambda e: (self.toggle_example(), "break")[1])
        sg.set_link_chevron(self.example_link, EDITOR_EXAMPLE_SHOW, up=False)

    def toggle_example(self):
        """Show or hide the example, beside the boxes: the window gets wider,
        not taller."""
        sg = self.sg
        if self.example.winfo_manager():
            self.example.pack_forget()
            sg.set_link_chevron(self.example_link, EDITOR_EXAMPLE_SHOW, up=False)
        else:
            self.example.pack(side="right", fill="y", padx=(28, 0))
            sg.set_link_chevron(self.example_link, EDITOR_EXAMPLE_HIDE, up=True)

    def typed_anything(self):
        return bool(self.name.get().strip() or self.phrases.get("1.0", "end").strip()
                    or self.prompt.get("1.0", "end").strip())

    def go_to_library(self):
        """The example's link: close this window and open the library at that
        entry. Asks for a second click when something has been typed."""
        if self.typed_anything() and not getattr(self, "_leave_warned", False):
            self._leave_warned = True
            self.status.config(text=EDITOR_LEAVE_WARNING, fg=self.sg.WARN)
            return
        self.open_library = True
        self.cancel()

    def collect(self):
        """(item, error) from the boxes."""
        item = dict(self.item)
        item.update(name=self.name.get(),
                    phrases=self.phrases.get("1.0", "end").splitlines(),
                    prompt=self.prompt.get("1.0", "end").strip())
        item.setdefault("enabled", True)
        clean, err = custom_triggers.validate([item])
        if err:
            return None, self.sg.skill_wording(err)
        if not clean:
            return None, "Skills: every skill needs a name."
        clean = clean[0]
        # Changing the instruction makes it the user's own: the library's
        # "update available" no longer applies to it.
        if self.item.get("library_id") and clean["prompt"] != self.item.get("prompt", "").strip():
            clean.pop("library_id", None)
            clean.pop("library_version", None)
        if self.check:
            err = self.check(clean)
            if err:
                return None, err
        return clean, None

    def ok(self):
        item, err = self.collect()
        if err:
            self.status.config(text=err, fg=self.sg.DANGER)
            return
        self.result = item
        self.top.destroy()

    def cancel(self):
        self.result = None
        self.top.destroy()

    def show(self, parent):
        _place(self.sg, self.tk, self.top, parent, (560, 520))
        self.name_entry.focus_set()


def edit_trigger(parent, item=None, check=None, on_library=None):
    """Open the editor over `parent` and wait. Returns the skill or None.
    `on_library()` is called after it closes when a new skill was abandoned
    for the example's library link."""
    dlg = TriggerEditor(parent, item, check)
    dlg.show(parent)
    parent.wait_window(dlg.top)
    if dlg.open_library and on_library:
        on_library()
    return dlg.result


class LibraryBrowser:
    """The shared library. `installed()` returns the skills the user has now;
    `on_add(entry)` adds one and returns an error to show or None. `load`
    replaces trigger_library.load (tests). `focus`: the id of an entry to
    bring to the top of the list once it has loaded."""

    def __init__(self, parent, installed, on_add, load=None, focus=None):
        import tkinter as tk
        sg = self.sg = _sg()
        self.tk = tk
        self.installed = installed
        self.on_add = on_add
        self._load = load or trigger_library.load
        self.library = None          # set by the loading thread
        self.entries = []
        self.cards = {}              # id -> {"button", "state"}
        self.closed = False
        self.focus_id = focus
        self.open_ids = set()        # entries opened to show their details
        self.folded = set()          # categories folded shut
        self.small_only = False      # the "Small local models only" filter
        self.category = None         # None = every category

        top = self.top = tk.Toplevel(parent)
        top.withdraw()
        top.title("Skill library")
        top.configure(bg=sg.BG)
        try:
            top.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass
        sg.set_window_icon(top)

        body = tk.Frame(top, bg=sg.BG)
        body.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        tk.Label(body, text="Skill library", font=sg.F_SECTION, bg=sg.BG, fg=sg.TEXT,
                 anchor="w").pack(fill="x")
        intro = sg.HelpText(body, LIBRARY_HELP, wraplength=640)
        intro.pack(anchor="w", pady=(4, 0))
        # Narrowed by hand, the window wraps the intro and the filters to fit.
        self.row_width = FILTER_ROW_WIDTH
        body.bind("<Configure>", lambda e: self._fit_width(e.width, intro), add="+")

        self.query = tk.StringVar()
        sbox = tk.Frame(body, bg=sg.RAISED, highlightthickness=1,
                        highlightbackground=sg.BORDER, highlightcolor=sg.ACCENT)
        sbox.pack(fill="x", pady=(14, 8))
        self.search = tk.Entry(sbox, textvariable=self.query, font=sg.F_BODY, bg=sg.RAISED,
                               fg=sg.TEXT, relief="flat", insertbackground=sg.ACCENT,
                               borderwidth=6, highlightthickness=0)
        self.search.pack(fill="x")
        sg.Placeholder(self.search, self.query, "Search, e.g. email, markdown, translate")
        self.search.bind("<FocusIn>", lambda e: sbox.config(highlightbackground=sg.ACCENT), add="+")
        self.search.bind("<FocusOut>", lambda e: sbox.config(highlightbackground=sg.BORDER), add="+")
        self.query.trace_add("write", lambda *_: self.render())
        self.filters = tk.Frame(body, bg=sg.BG)
        self.filters.pack(fill="x", pady=(0, 8))

        card = tk.Frame(body, bg=sg.SURFACE)
        card.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(card, bg=sg.SURFACE, highlightthickness=0, bd=0,
                                width=680, height=440)
        self.scroll = sg.SlimScrollbar(card, self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        self.scroll.pack(side="right", fill="y", pady=4)
        self.inner = tk.Frame(self.canvas, bg=sg.SURFACE)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._win, width=e.width))
        top.bind("<MouseWheel>", self._wheel)

        foot = tk.Frame(top, bg=sg.SURFACE)
        foot.pack(fill="x", pady=(18, 0))
        self.status = tk.Label(foot, font=sg.F_HELP, bg=sg.SURFACE, fg=sg.MUTED, anchor="w",
                               justify="left", wraplength=420, text="Loading the library...")
        self.status.pack(side="left", padx=24)
        sg.Button(foot, "Close", self.close, kind="primary").pack(
            side="right", padx=(6, 22), pady=14)
        web = tk.Label(foot, text="Open on the web", font=sg.F_HELP, bg=sg.SURFACE,
                       fg=sg.ACCENT, cursor="hand2")
        web.pack(side="right", padx=(0, 14))
        web.bind("<Button-1>", lambda e: self.open_web())
        top.bind("<Escape>", lambda e: self.close())
        top.protocol("WM_DELETE_WINDOW", self.close)

    # --- loading ---------------------------------------------------------------

    def start_loading(self):
        def work():
            try:
                self.library = self._load()
            except Exception:
                self.library = {"entries": [], "updated": "", "online": False,
                                "note": "Couldn't open the library."}
        threading.Thread(target=work, daemon=True).start()
        self._poll()

    def _poll(self):
        if self.closed:
            return
        if self.library is None:
            self.top.after(80, self._poll)
            return
        self.loaded()

    def loaded(self):
        lib = self.library
        self.entries = lib["entries"]
        if lib.get("note"):
            self.status.config(text=lib["note"], fg=self.sg.WARN)
        else:
            self.status.config(
                text="%d skills%s" % (len(self.entries),
                                      ", updated %s" % lib["updated"] if lib.get("updated") else ""),
                fg=self.sg.MUTED)
        self.render()
        self.show_entry(self.focus_id)

    def show_entry(self, entry_id):
        """Scroll one entry to the top of the list and name it in the footer.
        False when the library doesn't have it."""
        card = self.cards.get(entry_id)
        if not card:
            return False
        if entry_id not in self.open_ids:
            card["toggle"]()
        self.top.update_idletasks()
        height = max(1, self.inner.winfo_reqheight())
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), height))
        self.canvas.yview_moveto(max(0, card["row"].winfo_y() - 8) / height)
        if card.get("state") != "added":
            self.status.config(text='"%s" is at the top. Press Add to use it.' % card["name"],
                               fg=self.sg.ACCENT)
        return True

    # --- drawing ---------------------------------------------------------------

    def matches(self, entry):
        if self.small_only and entry["model"] != "any":
            return False
        if self.category and entry["category"] != self.category:
            return False
        q = self.query.get().strip().lower()
        if not q:
            return True
        hay = " ".join([entry["name"], entry["description"], entry["category"],
                        " ".join(entry["phrases"])]).lower()
        return all(word in hay for word in q.split())

    def categories(self):
        seen = []
        for e in self.entries:
            if e["category"] not in seen:
                seen.append(e["category"])
        return seen

    def set_category(self, category):
        self.category = category
        self.render()

    def toggle_small_only(self):
        self.small_only = not self.small_only
        self.render()

    def toggle_category(self, category):
        self.folded.symmetric_difference_update({category})
        self.render(keep_scroll=True)

    def _chip(self, parent, text, on, command):
        sg, tk = self.sg, self.tk
        chip = tk.Label(parent, text=text, font=sg.F_HELP, cursor="hand2", padx=10, pady=3,
                        bg=sg.ACCENT if on else sg.RAISED, fg=sg.BG if on else sg.MUTED)
        chip.bind("<Button-1>", lambda e: command())
        chip.pack(side="left", padx=(0, 6))
        return chip

    def _draw_filters(self):
        import tkinter.font as tkfont
        sg, tk = self.sg, self.tk
        for w in self.filters.winfo_children():
            w.destroy()
        self.chips = []
        if not self.entries:
            return
        wanted = [(SMALL_ONLY, self.small_only, self.toggle_small_only),
                  ("All", self.category is None, lambda: self.set_category(None))]
        wanted += [(c, self.category == c, lambda c=c: self.set_category(c))
                   for c in self.categories()]
        measure = tkfont.Font(font=sg.F_HELP).measure
        line, used = None, 0
        for text, on, command in wanted:   # a new line of filters when this one is full
            width = measure(text) + 26
            if line is None or used + width > self.row_width:
                line = tk.Frame(self.filters, bg=sg.BG)
                line.pack(fill="x", pady=(0, 6))
                used = 0
            self.chips.append(self._chip(line, text, on, command))
            used += width

    def _fit_width(self, width, intro):
        """The window's width changed: never wider than the usual layout."""
        row = min(FILTER_ROW_WIDTH, width)
        if width <= 1 or row == self.row_width:
            return
        self.row_width = row
        intro.configure(wraplength=row - 20)
        self._draw_filters()

    def render(self, keep_scroll=False):
        sg, tk = self.sg, self.tk
        at = self.canvas.yview()[0]
        for w in self.inner.winfo_children():
            w.destroy()
        self.cards = {}
        self.headings = {}
        self._draw_filters()
        shown = [e for e in self.entries if self.matches(e)]
        if self.library is not None and not shown:
            tk.Label(self.inner, text="Nothing matches." if self.entries else "The library is empty.",
                     font=sg.F_BODY, bg=sg.SURFACE, fg=sg.MUTED).pack(anchor="w", padx=16, pady=16)
        category = None
        for entry in shown:
            if entry["category"] != category:
                category = entry["category"]
                count = sum(1 for e in shown if e["category"] == category)
                head = tk.Label(self.inner, font=sg.F_SECTION, bg=sg.SURFACE, fg=sg.TEXT,
                                anchor="w", cursor="hand2",
                                text="%s %s (%d)" % (FOLDED if category in self.folded else OPEN,
                                                     category, count))
                head.pack(fill="x", padx=16, pady=(16, 4))
                head.bind("<Button-1>", lambda e, c=category: self.toggle_category(c))
                self.headings[category] = head
            if category not in self.folded:
                self._card(entry)
        if keep_scroll:
            self.top.update_idletasks()
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.yview_moveto(at)
        else:
            self.canvas.yview_moveto(0)

    def _card(self, entry):
        """One skill: a single line (name, model note) that opens to show the rest."""
        sg, tk = self.sg, self.tk
        row = tk.Frame(self.inner, bg=sg.SURFACE)
        row.pack(fill="x", padx=16)
        line = tk.Frame(row, bg=sg.SURFACE, cursor="hand2")
        line.pack(fill="x", pady=(5, 5))
        title = tk.Label(line, font=sg.F_TAB, bg=sg.SURFACE, fg=sg.TEXT, anchor="w",
                         cursor="hand2")
        title.pack(side="left")
        note = tk.Label(line, text=MODEL_NOTES[entry["model"]], font=sg.F_HELP, bg=sg.SURFACE,
                        fg=sg.FAINT, anchor="e", cursor="hand2")
        note.pack(side="right", padx=(12, 4))

        details = tk.Frame(row, bg=sg.SURFACE)
        right = tk.Frame(details, bg=sg.SURFACE)
        right.pack(side="right", anchor="n", padx=(12, 4))
        left = tk.Frame(details, bg=sg.SURFACE)
        left.pack(side="left", fill="x", expand=True, padx=(18, 0))
        tk.Label(left, text=entry["description"], font=sg.F_HELP, bg=sg.SURFACE, fg=sg.MUTED,
                 anchor="w", justify="left", wraplength=450).pack(fill="x")
        say = "Say: " + " or ".join('"%s"' % p for p in entry["phrases"])
        tk.Label(left, text=say, font=sg.F_HELP, bg=sg.SURFACE, fg=sg.FAINT, anchor="w",
                 justify="left", wraplength=450).pack(fill="x")
        prompt = tk.Label(left, text=entry["prompt"], font=sg.F_HELP, bg=sg.RAISED, fg=sg.TEXT,
                          anchor="w", justify="left", wraplength=430, padx=10, pady=8)
        link = tk.Label(left, text="Show the instruction", font=sg.F_HELP, bg=sg.SURFACE,
                        fg=sg.ACCENT, cursor="hand2", anchor="w")
        link.pack(anchor="w", pady=(2, 0))

        def toggle_prompt(_e=None):
            if prompt.winfo_manager():
                prompt.pack_forget()
                link.config(text="Show the instruction")
            else:
                prompt.pack(fill="x", pady=(4, 0))
                link.config(text="Hide the instruction")
        link.bind("<Button-1>", toggle_prompt)

        def toggle(_e=None):
            if details.winfo_manager():
                details.pack_forget()
                self.open_ids.discard(entry["id"])
            else:
                details.pack(fill="x", pady=(0, 8))
                self.open_ids.add(entry["id"])
            self._show_state(entry)
        for w in (line, title, note):
            w.bind("<Button-1>", toggle)

        button = sg.Button(right, "Add", lambda e=entry: self.add(e))
        button.pack()
        self.cards[entry["id"]] = {"button": button, "toggle": toggle, "row": row,
                                   "name": entry["name"], "title": title, "details": details,
                                   "toggle_prompt": toggle_prompt}
        if entry["id"] in self.open_ids:
            details.pack(fill="x", pady=(0, 8))
        self._show_state(entry)
        tk.Frame(self.inner, bg=sg.BORDER, height=1).pack(fill="x", padx=16)

    def _show_state(self, entry):
        state = trigger_library.installed_state(entry, self.installed())
        card = self.cards.get(entry["id"])
        if not card:
            return
        card["state"] = state
        card["title"].config(text="%s %s%s" % (
            OPEN if entry["id"] in self.open_ids else FOLDED, entry["name"],
            "  \u2713" if state != "new" else ""))
        text = {"new": "Add", "added": "✓ Added", "update": "Update"}[state]
        card["button"].config(text=text)
        card["button"].set_enabled(state != "added")

    def add(self, entry):
        err = self.on_add(entry)
        if err:
            self.status.config(text=err, fg=self.sg.DANGER)
            return
        self._show_state(entry)
        self.status.config(text='Added "%s". Press Save in Settings to keep it.' % entry["name"],
                           fg=self.sg.ACCENT)

    # --- the rest --------------------------------------------------------------

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")
        return "break"

    def open_web(self):
        import webbrowser
        webbrowser.open(trigger_library.BROWSE_URL)

    def close(self):
        self.closed = True
        self.top.destroy()

    def show(self, parent):
        _place(self.sg, self.tk, self.top, parent, (620, 480))
        self.search.focus_set()
        self.start_loading()


def browse_library(parent, installed, on_add, load=None, focus=None):
    dlg = LibraryBrowser(parent, installed, on_add, load, focus=focus)
    dlg.show(parent)
    parent.wait_window(dlg.top)
