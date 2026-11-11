"""Whisper Unchained Server setup wizard (step 2 of 2; Setup.bat runs step 1).

Built on the Settings window (client/settings_gui.py), so the "walk me
through it" pages ARE the Settings pages and both write the same .env.

Steps:  Welcome -> Your PC -> [Settings pages, if customizing] -> Install -> Done

    pythonw installer\\wizard.py            normal run
    python  installer\\wizard.py --dry-run  saves settings + downloads the model,
                                            but skips the service, login start,
                                            right-click menu and tray (for tests)
"""
import os
import queue
import sys
import threading
import tkinter as tk
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, HERE)

import steps  # noqa: E402

FRESH = steps.ensure_env(ROOT)  # must exist before settings_gui reads it

import hardware  # noqa: E402
import llm_detect  # noqa: E402
import model_catalog  # noqa: E402
import settings_gui as sg  # noqa: E402
import transcribe_client  # noqa: E402
from settings_gui import (ACCENT, BG, BORDER, DANGER, F_BODY, F_BUTTON, F_HELP,  # noqa: E402
                          F_LABEL, F_SECTION, F_TITLE, MUTED, RAISED, SURFACE, TEXT,
                          WARN, Button)

QUICK = ["Welcome", "Your PC", "Install", "Done"]
CUSTOM_PAGES = ["Transcription", "Words & Triggers", "AI Editing (LLM)", "Hotkey",
                "Phone", "Startup"]
CUSTOM = ["Welcome", "Your PC"] + CUSTOM_PAGES + ["Install", "Done"]
INSTALL_INTROS = {
    "local": "Ready to install. Windows will ask once for permission, to set up the "
             "background server.",
    "remote": "Ready to install. Nothing heavy goes on this PC: just the hotkey and tray "
              "app, sending your dictation to your other PC.",
    "cloud": "Ready to install. Nothing heavy goes on this PC: just the hotkey and tray "
             "app, sending your dictation to the cloud service.",
}
STEP_LABELS = {"AI Editing (LLM)": "AI Editing", "Words & Triggers": "Words"}
AWAY = "Other devices"   # extra step: local install, no Tailscale on this PC


class WizardApp(sg.SettingsApp):
    def __init__(self, root, dry_run=False):
        self.dry_run = dry_run
        self.fresh = FRESH
        self.mode = tk.StringVar(root, value="quick")
        self.english = tk.BooleanVar(root, value=True)
        self.gpus = hardware.detect_gpus()
        self.has_tailscale = steps.tailscale_installed()
        self.away = tk.StringVar(root, value="tailscale")
        self.tailscale_started = None   # "winget" / "browser" once launched
        self.ai_note = ""     # what AI editing will use (from the server search)
        self.port_note = ""   # set if the usual port was taken
        self.extra_env = {}
        self.installing = False
        self.installed = False
        self.events = queue.Queue()
        super().__init__(root)
        # New installs get an API key, required by default (Settings > Phone can
        # switch it off). The phone gets it from the pairing code; local programs
        # read it from .env.
        key = self.fields.get("WHISPER_API_KEY")
        if self.fresh and key is not None and not key.var.get().strip():
            key.var.set(sg.generate_api_key())
        root.title("Whisper Unchained Setup")
        root.geometry("1000x780")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.go("Welcome")

    # --- frame: header, step list, footer -------------------------------------

    def _build_header(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=28, pady=(24, 14))
        tk.Label(head, text="Set up Whisper Unchained", font=F_TITLE, bg=BG,
                 fg=TEXT).pack(anchor="w")
        tk.Label(head, text="Private dictation that runs on your own PC.",
                 font=F_HELP, bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 0))

    def _build_sidebar(self, parent):
        self.sidebar = tk.Frame(parent, bg=SURFACE, width=180)
        self.sidebar.pack(side="left", fill="y", padx=(28, 0))
        self.sidebar.pack_propagate(False)
        self.nav = {}
        self._render_steps()

    def source(self):
        return self.src.get() if hasattr(self, "src") else "local"

    def steps(self):
        """The page order depends on the mode AND where transcription runs."""
        if self.source() == "local":
            order = list(CUSTOM if self.mode.get() == "custom" else QUICK)
            if not self.has_tailscale:
                order.insert(order.index("Install"), AWAY)
            return order
        # Another PC / cloud: the connection details are needed even on the
        # quick path; server-only pages (AI Editing, Phone) don't apply.
        if self.mode.get() != "custom":
            return ["Welcome", "Your PC", "Transcription", "Install", "Done"]
        pages = ["Transcription"] + (["Words & Triggers"] if self.source() == "cloud" else [])
        return ["Welcome", "Your PC"] + pages + ["Hotkey", "Startup", "Install", "Done"]

    def _render_steps(self):
        """The step list on the left: a map of where you are, not clickable."""
        for w in self.sidebar.winfo_children():
            w.destroy()
        self.nav = {}
        for i, name in enumerate(self.steps(), 1):
            row = tk.Frame(self.sidebar, bg=SURFACE)
            row.pack(fill="x")
            bar = tk.Frame(row, bg=SURFACE, width=3)
            bar.pack(side="left", fill="y")
            lbl = tk.Label(row, text="%d. %s" % (i, STEP_LABELS.get(name, name)),
                           font=F_LABEL, bg=SURFACE, fg=MUTED, anchor="w",
                           padx=14, pady=11)
            lbl.pack(side="left", fill="x", expand=True)
            self.nav[name] = (row, bar, lbl)

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=SURFACE)
        foot.pack(fill="x", side="bottom")
        inner = tk.Frame(foot, bg=SURFACE)
        inner.pack(fill="x", padx=28, pady=14)
        self.status = tk.Label(inner, text="", font=F_BODY, bg=SURFACE, fg=MUTED,
                               anchor="w", justify="left", wraplength=420)
        self.status.pack(side="left", fill="x", expand=True)
        self.btn_next = Button(inner, "Next", self.next, kind="primary")
        self.btn_next.pack(side="right")
        self.btn_back = Button(inner, "Back", self.back, kind="ghost")
        self.btn_back.pack(side="right", padx=(0, 10))
        # SettingsApp's save() reports through these; the wizard has no Save button.
        self.btn_save = Button(inner, "", lambda: None)
        self.btn_restart = self.btn_save

    def set_saved_state(self, saved):
        pass  # no "Unsaved changes" nagging mid-wizard: Install saves everything

    def on_close(self):
        if self.installing:
            self.status.config(text="Please wait for the install to finish.", fg=WARN)
            return
        self.root.destroy()

    # --- pages that only the wizard has ----------------------------------------

    def _build_content(self, parent):
        super()._build_content(parent)
        for name, build in (("Welcome", self._page_welcome), ("Your PC", self._page_pc),
                            (AWAY, self._page_away),
                            ("Install", self._page_install), ("Done", self._page_done)):
            frame = tk.Frame(self.inner, bg=BG)
            build(frame)
            self.frames[name] = frame

    def _heading(self, parent, title, text=""):
        tk.Label(parent, text=title, font=F_SECTION, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w", padx=28, pady=(8, 0))
        if text:
            tk.Label(parent, text=text, font=F_BODY, bg=BG, fg=MUTED, anchor="w",
                     justify="left", wraplength=560).pack(anchor="w", padx=28, pady=(6, 0))

    def _choice_card(self, parent, var, value, title, text):
        """A big clickable option: radio dot + title + one line of explanation."""
        card = tk.Frame(parent, bg=RAISED, cursor="hand2", highlightthickness=2,
                        highlightbackground=RAISED)
        card.pack(fill="x", padx=28, pady=(12, 0))
        dot = tk.Label(card, text="", font=F_SECTION, bg=RAISED, fg=ACCENT, width=2)
        dot.pack(side="left", padx=(12, 0), pady=14)
        body = tk.Frame(card, bg=RAISED)
        body.pack(side="left", fill="x", expand=True, padx=8, pady=10)
        tk.Label(body, text=title, font=F_BUTTON, bg=RAISED, fg=TEXT,
                 anchor="w").pack(anchor="w")
        tk.Label(body, text=text, font=F_HELP, bg=RAISED, fg=MUTED, anchor="w",
                 justify="left", wraplength=480).pack(anchor="w", pady=(2, 0))

        def refresh(*_):
            on = var.get() == value
            dot.config(text="●" if on else "○")
            card.config(highlightbackground=ACCENT if on else RAISED)
        for w in (card, dot, body, *body.winfo_children()):
            w.bind("<Button-1>", lambda e: var.set(value))
        var.trace_add("write", refresh)
        refresh()

    def _page_welcome(self, f):
        if self.fresh:
            self._heading(f, "Welcome",
                          "This sets up a speech-to-text server on this PC, a hotkey that "
                          "types what you say into any app, and (optionally) your Android "
                          "phone as a microphone. Nothing you say leaves your PC.")
        else:
            self._heading(f, "Welcome back",
                          "Whisper Unchained is already set up here. Running setup again "
                          "keeps your current settings and repairs or updates the install.")
        self._choice_card(f, self.mode, "quick", "Use recommended settings",
                          "Fastest. Picks the best model for your graphics card and sensible "
                          "defaults for everything else. You can change any of it later in "
                          "Settings (tray icon > Settings...).")
        self._choice_card(f, self.mode, "custom", "Walk me through the settings",
                          "Shows each settings page (model, AI editing, hotkey, phone, "
                          "startup) before installing.")
        self.mode.trace_add("write", lambda *_: self._render_steps() or self._mark_step())

    def _page_pc(self, f):
        self._heading(f, "Your PC")
        tk.Label(f, text="Where should transcription run?", font=F_BUTTON, bg=BG,
                 fg=TEXT, anchor="w").pack(anchor="w", padx=28, pady=(10, 0))
        current = sg.read_env().get("TRANSCRIBE_SOURCE", "") if not self.fresh else ""
        self.src = tk.StringVar(f, value=current if current in transcribe_client.SOURCES
                                else "local")
        self._choice_card(f, self.src, "local", "On this PC",
                          "Private and free: Whisper runs here. Best with an NVIDIA "
                          "graphics card." + ("" if self.gpus else " (None found: it will "
                                              "use the processor, which is slower.)"))
        self._choice_card(f, self.src, "remote", "On another PC of mine",
                          "Send dictation to a PC already running Whisper Unchained "
                          "Server (e.g. over Tailscale). Nothing heavy is installed here.")
        self._choice_card(f, self.src, "cloud", "A cloud service",
                          "Groq, OpenAI or similar, with your own API key. No graphics "
                          "card needed, but the audio leaves this PC.")
        self.src.trace_add("write", lambda *_: self._source_changed())
        self.local_box = tk.Frame(f, bg=BG)
        self.local_box.pack(fill="x")
        f = self.local_box  # the rest of this page only applies to "On this PC"
        if self.gpus:
            gpu_text = "\n".join("%s  ·  %.0f GB video memory" % (g.name, g.total_mb / 1024)
                                 for g in self.gpus)
        else:
            gpu_text = "No NVIDIA graphics card found."
        tk.Label(f, text=gpu_text, font=F_BODY, bg=BG, fg=TEXT, anchor="w",
                 justify="left").pack(anchor="w", padx=28, pady=(8, 0))

        tk.Label(f, text="Which languages will you dictate in?", font=F_BUTTON, bg=BG,
                 fg=TEXT, anchor="w").pack(anchor="w", padx=28, pady=(18, 0))
        lang = tk.StringVar(f, value="en")
        self._choice_card(f, lang, "en", "English only",
                          "English-only models are a little more accurate in English.")
        self._choice_card(f, lang, "multi", "English and other languages",
                          "Whisper understands about 100 languages.")
        lang.trace_add("write", lambda *_: (self.english.set(lang.get() == "en"),
                                            self._refresh_rec()))

        self.rec_box = tk.Frame(f, bg=SURFACE)
        self.rec_box.pack(fill="x", padx=28, pady=(18, 0))
        self.rec_title = tk.Label(self.rec_box, font=F_BUTTON, bg=SURFACE, fg=ACCENT,
                                  anchor="w")
        self.rec_title.pack(anchor="w", padx=14, pady=(10, 0))
        self.rec_text = tk.Label(self.rec_box, font=F_HELP, bg=SURFACE, fg=TEXT,
                                 anchor="w", justify="left", wraplength=540)
        self.rec_text.pack(anchor="w", padx=14, pady=(2, 10))
        self._refresh_rec()

    def _page_away(self, f):
        self._heading(f, "Will other devices use this PC?",
                      "Your phone, a laptop or another PC can use this PC's transcription "
                      "and AI editing. On the same Wi-Fi or home network that works as is. "
                      "From anywhere else (mobile data, work, travel), they have to find "
                      "this PC over the internet: unless your Internet provider gives you "
                      "a static IP address and you've set up port forwarding, you'll most "
                      "likely need Tailscale, a free, private network between your own "
                      "devices. Nothing is opened to the rest of the internet.")
        self._choice_card(f, self.away, "tailscale", "Install Tailscale (recommended)",
                          "Free for personal use. It installs in its own window now; sign "
                          "in when it asks, then install Tailscale on your phone or other "
                          "PCs with the same account.")
        self._choice_card(f, self.away, "covered", "I have this covered",
                          "A static IP with port forwarding, my own VPN, or another way in.")
        self._choice_card(f, self.away, "home", "Just this PC, or only on my home network",
                          "Nothing to install. You can add Tailscale any time later.")

    def on_llm_found(self, found):
        """On a fresh install, use the best AI server found on this PC for AI
        editing (a repair keeps the user's own choice). Custom-path users see
        the same search on the AI Editing page and can pick another."""
        local = [f for f in found if f.host in ("127.0.0.1", "localhost")
                 and f.models and not f.needs_key]
        if not self.fresh:
            return
        if local:
            ranked = sorted(local, key=lambda f: 0 if llm_detect.model_note(
                llm_detect.best_model(f.models)) else 1)
            pick = ranked[0]
            model = llm_detect.best_model(pick.models)
            self.use_llm_server(pick, model)
            # An AI model is available, so clean up every dictation by default
            # (fillers, "no wait" fixes, punctuation); Settings > AI Editing turns it off.
            cleanup = self.fields.get("CLEANUP_ALWAYS")
            if cleanup is not None and cleanup.var is not None:
                cleanup.var.set(True)
            self.ai_note = ("AI editing will use %s on this PC (%s), and every dictation gets "
                            "a light AI cleanup. Change either in Settings > AI Editing."
                            % (pick.name, model))
        else:
            self.ai_note = ("No AI server found on this PC, so \"prompt AI\" voice commands "
                            "won't work until you add one (Ollama and LM Studio are free). "
                            "Settings > AI Editing can find it later, also on another PC.")
        self._refresh_install_notes()

    def _check_port(self):
        """The transcription port must be free (or already ours, on a repair)."""
        f = self.fields.get("WHISPER_API_PORT")
        if f is None or f.var is None:
            return
        try:
            port = int(f.var.get().strip() or 5000)
        except ValueError:
            return
        self.port_note = ""
        if llm_detect.port_status(port) == "taken":
            new = llm_detect.suggest_port(port)
            if new != port:
                f.var.set(str(new))
                self.port_note = ("Port %d is used by another program, so the server will "
                                  "use port %d." % (port, new))
        self._refresh_install_notes()

    def _refresh_install_notes(self):
        if not hasattr(self, "install_notes"):
            return
        notes = ([n for n in (self.ai_note, self.port_note) if n]
                 if self.source() == "local" else [])
        self.install_notes.config(text="\n\n".join(notes))

    def _source_changed(self):
        """Mirror the choice into the Transcription page's field and re-plan the steps."""
        src = self.source()
        label = next(l for l, v in sg.SOURCE_CHOICES if v == src)
        self._value_set("TRANSCRIBE_SOURCE", label)
        if src == "local":
            if not self.local_box.winfo_manager():
                self.local_box.pack(fill="x")
        else:
            self.local_box.pack_forget()
        if hasattr(self, "game_row"):
            if src == "local":
                if not self.game_row.winfo_manager():
                    self.game_row.pack(fill="x", padx=28, pady=(14, 0), after=self.menu_row)
            else:
                self.game_row.pack_forget()
        if hasattr(self, "install_intro"):
            self.install_intro.config(text=INSTALL_INTROS[src])
        self._render_steps()
        if getattr(self, "current", None):
            self.show_section(self.current)  # rebuilt step list: re-highlight this page
        self._mark_step()

    def _refresh_rec(self):
        self.rec = hardware.recommend(self.gpus, english_only=self.english.get())
        where = "graphics card" if self.rec.device == "cuda" else "processor"
        self.rec_title.config(text="Recommended: %s on your %s" % (self.rec.model, where))
        note = self.rec.reason
        if not self.fresh:
            current = self.values.get("WHISPER_MODEL_SIZE", "")
            if current and current != self.rec.model:
                note += ("\n\nYou currently use %s. Setup will switch to the "
                         "recommendation; go back and choose \"Walk me through the "
                         "settings\" to keep yours." % current)
        self.rec_text.config(text=note)

    def _apply_recommendation(self):
        """Put the recommended model/device into the settings fields."""
        if self.source() != "local":
            return  # no Whisper on this PC: nothing to size
        self._value_set("WHISPER_MODEL_SIZE", self.rec.model)
        self._value_set("WHISPER_DEVICE", self.rec.device)
        self.extra_env["WHISPER_COMPUTE_TYPE"] = self.rec.compute_type

    def _value_set(self, key, value):
        f = self.fields.get(key)
        if f is not None and f.var is not None:
            f.var.set(value)

    def _page_install(self, f):
        self._heading(f, "Install")
        self.install_intro = tk.Label(f, text=INSTALL_INTROS["local"], font=F_BODY, bg=BG,
                                      fg=MUTED, anchor="w", justify="left", wraplength=560)
        self.install_intro.pack(anchor="w", padx=28, pady=(6, 0))
        self.install_notes = tk.Label(f, text="", font=F_HELP, bg=BG, fg=TEXT, anchor="w",
                                      justify="left", wraplength=560)
        self.install_notes.pack(anchor="w", padx=28, pady=(8, 0))
        self.opt_login = tk.BooleanVar(f, value=True)
        self.opt_menu = tk.BooleanVar(f, value=True)
        options = [
            (self.opt_login, "Start when I log in",
             "Starts the tray icon and hotkey automatically."),
            (self.opt_menu, "Add \"Transcribe to Clipboard\" to the right-click menu",
             "Right-click any audio file to get its text."),
        ]
        # Only worth asking on a PC with a graphics card to free. This is the
        # Startup page's own GAME_MODE field, so the two can never disagree.
        game = self.fields.get("GAME_MODE")
        if self.gpus and game is not None and game.var is not None:
            options.append((game.var, "Free GPU memory while gaming",
                            "Pauses dictation while a game runs full screen; resumes after."))
        for var, title, text in options:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", padx=28, pady=(14, 0))
            if var is self.opt_menu:
                self.menu_row = row
            if game is not None and var is game.var:
                self.game_row = row  # hidden when transcription isn't on this PC
            sg.Toggle(row, var).pack(side="right")
            tk.Label(row, text=title, font=F_LABEL, bg=BG, fg=TEXT,
                     anchor="w").pack(anchor="w")
            tk.Label(row, text=text, font=F_HELP, bg=BG, fg=MUTED,
                     anchor="w").pack(anchor="w")
        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(18, 6))
        self.task_rows = {}
        for key, label in (("save", "Save your settings"),
                           ("model", "Download the speech model"),
                           ("service", "Install the background server"),
                           ("login", "Start when you log in"),
                           ("menu", "Right-click menu"),
                           ("tray", "Start the tray app"),
                           ("check", "Check the server answers")):
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", padx=28, pady=(6, 0))
            mark = tk.Label(row, text="○", font=F_BODY, bg=BG, fg=MUTED, width=2)
            mark.pack(side="left")
            tk.Label(row, text=label, font=F_BODY, bg=BG, fg=TEXT).pack(side="left")
            detail = tk.Label(row, text="", font=F_HELP, bg=BG, fg=MUTED)
            detail.pack(side="left", padx=(10, 0))
            self.task_rows[key] = (mark, detail)

    def _page_done(self, f):
        self.done_heading = tk.Label(f, text="You're all set", font=F_SECTION, bg=BG,
                                     fg=TEXT, anchor="w")
        self.done_heading.pack(anchor="w", padx=28, pady=(8, 0))
        self.done_text = tk.Label(f, text="", font=F_BODY, bg=BG, fg=TEXT, anchor="w",
                                  justify="left", wraplength=560)
        self.done_text.pack(anchor="w", padx=28, pady=(8, 0))
        row = tk.Frame(f, bg=BG)
        row.pack(fill="x", padx=28, pady=(18, 0))
        Button(row, "Pair my phone", lambda: self.show_section("Phone"),
               kind="ghost").pack(side="left")
        Button(row, "Open the install log folder", lambda: os.startfile(steps.LOG_DIR)
               if os.path.isdir(steps.LOG_DIR) else None, kind="ghost").pack(
            side="left", padx=(10, 0))

    # --- navigation -------------------------------------------------------------

    def go(self, name):
        self.current = name
        self.show_section(name)
        self._mark_step()
        order = self.steps()
        i = order.index(name) if name in order else 0
        self.btn_back.set_enabled(0 < i and name != "Done" and not self.installing)
        nxt = {"Install": "Next" if self.installed else "Install",
               "Done": "Finish"}.get(name, "Next")
        self.btn_next.config(text=nxt)
        self.btn_next.set_enabled(not self.installing)
        if name != "Install":
            self.status.config(text="", fg=MUTED)
        elif not self.installed and not self.installing and self.source() == "local":
            self._check_port()
        self._refresh_install_notes()

    def _mark_step(self):
        # Steps already passed show a check; show_section highlights the current one.
        order = self.steps()
        here = order.index(self.current) if getattr(self, "current", None) in order else 0
        for i, name in enumerate(order):
            if name in self.nav and i < here:
                self.nav[name][2].config(fg=TEXT)

    def next(self):
        order = self.steps()
        name = self.current
        if name == "Your PC":
            self._apply_recommendation()
        launched = None
        if name == AWAY and self.away.get() == "tailscale" and not self.tailscale_started:
            # Not waited for: it has its own window and the install goes on meanwhile.
            self.tailscale_started = launched = (
                "dry-run" if self.dry_run else steps.install_tailscale())
        if name == "Install":
            if self.installed:
                return self.go("Done")
            return self.start_install()
        if name == "Done":
            return self.root.destroy()
        if name in order and order.index(name) + 1 < len(order):
            self.go(order[order.index(name) + 1])
            if launched == "winget":
                self.status.config(text="Tailscale is installing in its own window. "
                                        "Sign in when it asks.", fg=MUTED)
            elif launched == "browser":
                self.status.config(text="Tailscale's download page is open in your "
                                        "browser.", fg=MUTED)
        else:  # e.g. the Phone page opened from Done
            self.go("Done")

    def back(self):
        order = self.steps()
        if self.current in order and order.index(self.current) > 0:
            self.go(order[order.index(self.current) - 1])

    # --- install --------------------------------------------------------------

    def _task(self, key, state, detail=""):
        """state: running | ok | skip | fail. Called on the Tk thread only."""
        mark, label = self.task_rows[key]
        sym, color = {"running": ("◔", WARN), "ok": ("✓", ACCENT),
                      "skip": ("–", MUTED), "fail": ("✗", DANGER)}[state]
        mark.config(text=sym, fg=color)
        label.config(text=detail, fg=DANGER if state == "fail" else MUTED)

    def start_install(self):
        updates, err = self.collect()
        words = None
        if not err and self._words_dirty:
            vocab, corr, err = self.collect_words()
            words = (vocab, corr)
        if err:
            self.status.config(text=err, fg=DANGER)
            return
        self.installing = True
        self.go("Install")
        self.status.config(text="Installing...", fg=WARN)
        opts = {"login": self.opt_login.get(), "menu": self.opt_menu.get(),
                "source": self.source()}
        threading.Thread(target=self._install_worker, args=(updates, opts, words),
                         daemon=True).start()
        self._pump()

    def _pump(self):
        """Apply progress events from the worker thread (Tk isn't thread-safe)."""
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "task":
                    self._task(*ev[1:])
                elif ev[0] == "finished":
                    return self._finished(*ev[1:])
        except queue.Empty:
            pass
        self.root.after(200, self._pump)

    def _install_worker(self, updates, opts, words=None):
        emit = lambda *a: self.events.put(("task",) + a)  # noqa: E731
        problems = []
        try:
            emit("save", "running")
            sg.write_env(dict(updates, **self.extra_env))
            if words is not None:
                sg.words_config.save(*words)
            self.values = sg.read_env()
            emit("save", "ok", ".env")

            if opts["source"] != "local":
                return self._install_client_only(updates, opts, emit, problems)

            emit("model", "running", "starting...")
            info = model_catalog.BY_NAME.get(self.values.get("WHISPER_MODEL_SIZE", ""))
            root_dir = model_catalog.models_dir(self.values)
            if info and root_dir and not model_catalog.is_downloaded(info, root_dir):
                done = threading.Event()
                result = {}

                def dl():
                    try:
                        result["how"] = model_catalog.download(info, root_dir)
                    except Exception as e:
                        result["err"] = str(e)
                    done.set()
                threading.Thread(target=dl, daemon=True).start()
                base = os.path.join(root_dir, info.cache_folder)
                while not done.wait(0.5):
                    size = max(model_catalog.folder_size(base),
                               model_catalog.folder_size(base + ".partial"))
                    emit("model", "running", "%d%% of %s" % (
                        min(99, 100 * size // (info.size_mb * 2 ** 20)), info.name))
                if "err" in result:
                    raise RuntimeError("Model download failed: %s" % result["err"])
                emit("model", "ok", info.name)
            else:
                emit("model", "ok", "already downloaded" if info else "skipped")

            if self.dry_run:
                for k in ("service", "login", "menu", "tray", "check"):
                    emit(k, "skip", "test run")
                return self.events.put(("finished", True, []))

            emit("service", "running", "approve the Windows prompt")
            ok, log = steps.run_elevated_ps("install_service.ps1", "-NoPause",
                                            "-NoScheduledRestart", log_name="service.log")
            if ok == steps.ELEVATION_CANCELLED:
                emit("service", "fail", "permission was declined")
                problems.append("The background server wasn't installed because the "
                                "Windows permission prompt was declined. Run Setup.bat "
                                "again to retry.")
            elif not ok:
                emit("service", "fail", "see installer/logs/service.log")
                problems.append("The background server failed to install.")
            else:
                emit("service", "ok")

            for key, script, wanted in (("login", "install_listener_startup.ps1", opts["login"]),
                                        ("menu", "install_context_menu.ps1", opts["menu"])):
                if not wanted:
                    emit(key, "skip", "not chosen")
                    continue
                emit(key, "running")
                ok, log = steps.run_ps(script, "-NoPause")
                emit(key, "ok" if ok else "fail", "" if ok else "see installer/logs")
                if not ok:
                    problems.append("%s failed." % script)

            emit("tray", "running")
            steps.start_tray(ROOT)
            emit("tray", "ok")

            if any("server" in p for p in problems):
                emit("check", "skip", "server not installed")
            else:
                emit("check", "running", "loading the model (up to a minute)")
                ok, detail = steps.wait_for_server(sg.api_port(self.values))
                emit("check", "ok" if ok else "fail", detail)
                if not ok:
                    problems.append("The server didn't answer: %s" % detail)
            self.events.put(("finished", not problems, problems))
        except Exception as e:
            with open(steps.log_path("wizard.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.events.put(("finished", False, [str(e)]))

    def _install_client_only(self, updates, opts, emit, problems):
        """Another PC / cloud: no model, no service. Set up the tray and check
        the connection instead."""
        emit("model", "skip", "not needed here")
        emit("service", "skip", "not needed here")
        if self.dry_run:
            for k in ("login", "menu", "tray"):
                emit(k, "skip", "test run")
        else:
            for key, script, wanted in (("login", "install_listener_startup.ps1", opts["login"]),
                                        ("menu", "install_context_menu.ps1", opts["menu"])):
                if not wanted:
                    emit(key, "skip", "not chosen")
                    continue
                emit(key, "running")
                ok, _log = steps.run_ps(script, "-NoPause")
                emit(key, "ok" if ok else "fail", "" if ok else "see installer/logs")
                if not ok:
                    problems.append("%s failed." % script)
            emit("tray", "running")
            steps.start_tray(ROOT)
            emit("tray", "ok")
        emit("check", "running", "testing the connection")
        ok, detail = transcribe_client.check_connection(self.values)
        emit("check", "ok" if ok else "fail", detail)
        if not ok:
            where = "the other PC" if opts["source"] == "remote" else "the cloud service"
            problems.append("Couldn't use %s: %s Check the address and key in Settings > "
                            "Transcription." % (where, detail))
        self.events.put(("finished", not problems, problems))

    def _finished(self, ok, problems):
        self.installing = False
        self.installed = True
        hotkey = self.values.get("HOTKEY") or "`"
        mode = self.values.get("HOTKEY_MODE", "toggle")
        how = ("Hold %s, speak, and let go" if mode == "hold"
               else "Press %s, speak, and press it again") % hotkey
        if ok:
            self.done_heading.config(text="You're all set")
            text = ("%s: the text appears wherever your cursor is.\n\n"
                    "The waveform icon in the tray (by the clock) has Settings and "
                    "pause/restart options. To use your phone as a microphone, "
                    "pair it now." % how)
            if self.tailscale_started:
                text += ("\n\nOther devices: sign in to Tailscale (its icon is by the "
                         "clock), install Tailscale on your phone or other PCs with the "
                         "same account, then pair your phone, so the pairing code uses "
                         "the Tailscale address.")
            if self.dry_run:
                text = "Test run finished: settings saved and model ready.\n\n" + text
            self.status.config(text="Install finished.", fg=ACCENT)
        else:
            self.done_heading.config(text="Almost there")
            text = ("Some steps didn't finish:\n  • " + "\n  • ".join(problems) +
                    "\n\nYour settings are saved. Fix the issue and run Setup.bat again; "
                    "it picks up where it left off. The logs are in installer/logs.")
            self.status.config(text="Install finished with problems.", fg=DANGER)
        self.done_text.config(text=text)
        self.go("Install")


def main():
    dry = "--dry-run" in sys.argv
    root = tk.Tk()
    try:
        app = WizardApp(root, dry_run=dry)
    except Exception:
        with open(steps.log_path("wizard.log"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()
    return app


if __name__ == "__main__":
    main()
