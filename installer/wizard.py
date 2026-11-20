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
import hotkey_config  # noqa: E402
import llm_detect  # noqa: E402
import model_catalog  # noqa: E402
import settings_gui as sg  # noqa: E402
import transcribe_client  # noqa: E402
from settings_gui import (ACCENT, BG, BORDER, DANGER, F_BODY, F_BUTTON, F_HELP,  # noqa: E402
                          F_LABEL, F_SECTION, F_TITLE, MUTED, RAISED, SURFACE, TEXT,
                          WARN, Button)

QUICK = ["Welcome", "Your PC", "Install", "Done"]
# No "Startup" page: its install-time choices (start at log-in, right-click
# menu, game mode) are the switches on the Install page, asked once.
CUSTOM_PAGES = ["Transcription", "Words", "AI Editing (LLM)", "Hotkey",
                "Devices & Apps"]
CUSTOM = ["Welcome", "Your PC"] + CUSTOM_PAGES + ["Install", "Done"]
INSTALL_INTROS = {
    "local": "Ready to install. Windows will ask for permission once, to set up the "
             "background part.",
    "remote": "Ready to install. Nothing heavy goes on this PC: just the hotkey and tray "
              "app, sending your dictation to your other PC.",
    "cloud": "Ready to install. Nothing heavy goes on this PC: just the hotkey and tray "
             "app, sending your dictation to the cloud service.",
}
STEP_LABELS = dict(sg.SECTION_LABELS)
# Your PC step: what its "Advanced" fold says, and its one switch.
PC_ADVANCED_NOTE = ("To choose the model or how dictation runs yourself, go back and pick "
                    "\"Walk me through the settings\". You can also change it later in "
                    "Settings.")
PC_NOT_RIGHT_ADVANCED = "To choose by hand instead, open Advanced at the bottom of this page."
USE_CARD_TITLE = "Use the graphics card"
USE_CARD_HELP = "Off: dictation runs on the processor, with a smaller model."
USE_QUICK_TITLE = "Use the quick way for processors"
USE_QUICK_HELP = "Off: the usual way, which takes a few seconds per sentence here."
DOWNLOAD_NOTHING = "Everything it needs is already on this PC."
# Shown beside a step while it works. Both waits are minutes, not seconds, on
# a slow PC: say so, or the window looks stuck.
SERVICE_WORKING = "setting up (this can take a few minutes)"
CHECK_WAITING = "starting up (this can take a few minutes)"
RESTART_NEEDED = ("Windows needs a restart before dictation works: a Microsoft "
                  "component it depends on was just installed. Restart this PC.")
NOT_STARTED_HELP = ("Restart this PC, then try dictating. If it still doesn't work, "
                    "run this setup again.")
# The Install step's list: (key, what the person reads).
INSTALL_TASKS = (("save", "Save your settings"),
                 ("download", "Download what dictation needs"),
                 ("service", "Set up the part that runs in the background"),
                 ("login", "Start when you log in"),
                 ("menu", "Right-click menu"),
                 ("tray", "Start the tray app"),
                 ("audio", "Check the microphone and speakers"),
                 ("check", "Check that dictation is ready"))
INSTALL_ROW_GAP = 6      # between the Install step's switches
AWAY = "Other devices"   # extra step: local install, no Tailscale on this PC
REACH = "Reaching it"    # extra step: transcription on another PC, no Tailscale here


class WizardApp(sg.SettingsApp):
    PASTE_NEXT = "Press Next"   # after "Paste connection code": no Save button here
    DOWNLOAD_IN_PLACE = False   # the Install step downloads; the pages only say how much

    def __init__(self, root, dry_run=False):
        self.dry_run = dry_run
        self.fresh = FRESH
        self.mode = tk.StringVar(root, value="quick")
        self.english = tk.BooleanVar(root, value=True)
        self.gpus = hardware.detect_gpus()
        # No NVIDIA card but AMD or Intel graphics: offer the second speech
        # engine (whisper.cpp), which can run on them. None = not offered.
        self.other_graphics = hardware.detect_other_graphics()
        self.offer = hardware.offer_engine(self.gpus, self.other_graphics)
        self.use_offer = tk.BooleanVar(root, value=True)
        # Dictation on the processor: offer the third engine (Parakeet), which
        # is quick there. Only where this install has it, and never on a PC
        # that gets the offer above.
        self.pk_ok = sg.parakeet_files.runtime_installed() and not self.offer
        self.use_quick = tk.BooleanVar(root, value=True)
        self.cancel_download = threading.Event()
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
        # New installs get an API key, required by default (Settings > Devices & Apps can
        # switch it off). The phone gets it from the pairing code; local programs
        # read it from .env.
        key = self.fields.get("WHISPER_API_KEY")
        if self.fresh and key is not None and not key.var.get().strip():
            key.var.set(sg.generate_api_key())
        # "Where transcription runs" is the Your PC page's question; don't ask
        # it again at the top of the Transcription page.
        src = self.fields.get("TRANSCRIBE_SOURCE")
        if src is not None and src.widget is not None:
            src.widget.master.pack_forget()
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
            order = ["Welcome", "Your PC", "Transcription", "Install", "Done"]
        else:
            pages = ["Transcription"] + (list(sg.WORDS_SECTIONS) if self.source() == "cloud" else [])
            order = ["Welcome", "Your PC"] + pages + ["Hotkey", "Install", "Done"]
        if self.source() == "remote" and not self.has_tailscale:
            # This PC has to find the other one: away from home that takes
            # Tailscale here too, which is easy to miss.
            order.insert(order.index("Install"), REACH)
        return order

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
                            (AWAY, self._page_away), (REACH, self._page_reach),
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

    def _choice_card(self, parent, var, value, title, text, style="tall"):
        """A big clickable option: radio dot + title + one line of explanation.

        style: "tall" = the explanation under the title (the usual card);
               "line" = a shorter card, the explanation on the title's own line;
               "pair" = a shorter card that the caller places itself, to put
                        two choices side by side. Returns the card."""
        tall = style == "tall"
        card = tk.Frame(parent, bg=RAISED, cursor="hand2", highlightthickness=2,
                        highlightbackground=RAISED)
        if style != "pair":
            card.pack(fill="x", padx=28, pady=(12 if tall else 6, 0))
        dot = tk.Label(card, text="", font=F_SECTION if tall else F_BUTTON, bg=RAISED,
                       fg=ACCENT, width=2)
        dot.pack(side="left", padx=(12 if tall else 8, 0), pady=14 if tall else 0)
        body = tk.Frame(card, bg=RAISED)
        body.pack(side="left", fill="x", expand=True, padx=8 if tall else 4,
                  pady=10 if tall else 7)
        name = tk.Label(body, text=title, font=F_BUTTON, bg=RAISED, fg=TEXT, anchor="w")
        note = tk.Label(body, text=text, font=F_HELP, bg=RAISED, fg=MUTED, anchor="w",
                        justify="left", wraplength=480 if style != "pair" else 290)
        if style == "line":
            name.pack(side="left")
            note.pack(side="left", padx=(12, 0))
        else:
            name.pack(anchor="w")
            note.pack(anchor="w", pady=(2 if tall else 0, 0))

        def refresh(*_):
            on = var.get() == value
            dot.config(text="●" if on else "○")
            card.config(highlightbackground=ACCENT if on else RAISED)
        for w in (card, dot, body, *body.winfo_children()):
            w.bind("<Button-1>", lambda e: var.set(value))
        var.trace_add("write", refresh)
        refresh()
        return card

    def _page_welcome(self, f):
        if self.fresh:
            self._heading(f, "Welcome",
                          "This sets up dictation on this PC: a hotkey that types what "
                          "you say into any app, and (if you like) your Android phone as "
                          "a microphone. Nothing you say leaves your PC.")
        else:
            self._heading(f, "Welcome back",
                          "Whisper Unchained is already set up here. Running setup again "
                          "keeps your current settings and repairs or updates the install.")
        self._choice_card(f, self.mode, "quick", "Use recommended settings",
                          "Fastest. Picks what works best on this PC and sensible "
                          "choices for everything else. You can change any of it later in "
                          "Settings (tray icon > Settings...).")
        self._choice_card(f, self.mode, "custom", "Walk me through the settings",
                          "Shows each settings page (transcription, words, AI editing, "
                          "hotkey, phone) before installing.")
        self.mode.trace_add("write", lambda *_: self._render_steps() or self._mark_step())

    def _page_pc(self, f):
        self._heading(f, "Your PC")
        tk.Label(f, text="Where should dictation run?", font=F_BUTTON, bg=BG,
                 fg=TEXT, anchor="w").pack(anchor="w", padx=28, pady=(8, 0))
        current = sg.read_env().get("TRANSCRIBE_SOURCE", "") if not self.fresh else ""
        self.src = tk.StringVar(f, value=current if current in transcribe_client.SOURCES
                                else "local")
        # One line each, so the whole step shows without scrolling.
        self._choice_card(f, self.src, "local", "On this PC",
                          "Private and free. Nothing you say leaves this PC.", style="line")
        self._choice_card(f, self.src, "remote", "On another PC of mine",
                          "Use a PC that already runs Whisper Unchained Server.",
                          style="line")
        self._choice_card(f, self.src, "cloud", "A cloud service",
                          "An online service does it for a small fee. The audio "
                          "leaves this PC.", style="line")
        self.src.trace_add("write", lambda *_: self._source_changed())
        self.local_box = tk.Frame(f, bg=BG)
        self.local_box.pack(fill="x")
        f = self.local_box  # the rest of this page only applies to "On this PC"

        # One statement: what was found and what dictation will run on. Setup
        # picks the engine and the model from it; nothing to choose here.
        self.rec_box = tk.Frame(f, bg=SURFACE)
        self.rec_box.pack(fill="x", padx=28, pady=(12, 0))
        self.rec_title = tk.Label(self.rec_box, font=F_BODY, bg=SURFACE, fg=TEXT,
                                  anchor="w", justify="left", wraplength=650)
        self.rec_title.pack(anchor="w", padx=14, pady=(10, 0))
        self.rec_note = tk.Label(self.rec_box, font=F_HELP, bg=SURFACE, fg=MUTED,
                                 anchor="w", justify="left", wraplength=650)
        self.rec_text = tk.Label(self.rec_box, font=F_HELP, bg=SURFACE, fg=TEXT,
                                 anchor="w", justify="left", wraplength=650)
        self.rec_text.pack(anchor="w", padx=14, pady=(4, 0))
        fold = tk.Frame(self.rec_box, bg=SURFACE)
        fold.pack(fill="x", pady=(2, 8))
        note = tk.Frame(fold, bg=SURFACE)
        tk.Label(note, text=hardware.NOT_RIGHT_NOTE + "\n" + PC_NOT_RIGHT_ADVANCED,
                 font=F_HELP, bg=SURFACE, fg=MUTED, anchor="w", justify="left",
                 wraplength=630).pack(anchor="w", padx=18, pady=(4, 0))
        self.not_right = sg.Expander(fold, note, hardware.NOT_RIGHT_LINK,
                                     hardware.NOT_RIGHT_LINK)
        self.not_right.config(font=F_HELP)
        self.not_right.pack(anchor="w", padx=12)

        tk.Label(f, text="Which languages will you dictate in?", font=F_BUTTON, bg=BG,
                 fg=TEXT, anchor="w").pack(anchor="w", padx=28, pady=(10, 0))
        lang = self.lang = tk.StringVar(f, value="en")
        # The two choices side by side, each half the row.
        row = self.lang_row = tk.Frame(f, bg=BG)
        row.pack(fill="x", padx=28, pady=(8, 0))
        for col, (value, title, text) in enumerate((
                ("en", "English only", "A little more accurate in English."),
                ("multi", "English and other languages",
                 "About 100 languages are understood."))):
            row.columnconfigure(col, weight=1, uniform="lang")
            self._choice_card(row, lang, value, title, text, style="pair").grid(
                row=0, column=col, sticky="nsew", padx=(0, 5) if col == 0 else (5, 0))
        lang.trace_add("write", lambda *_: (self.english.set(lang.get() == "en"),
                                            self._refresh_rec()))

        # Advanced: the one switch (only when another way exists on this PC)
        # and where the rest can be chosen by hand.
        holder = tk.Frame(f, bg=BG)
        holder.pack(fill="x", pady=(8, 0))
        more = tk.Frame(holder, bg=BG)
        if self.offer:
            # Off = the usual engine, on the processor.
            row = tk.Frame(more, bg=BG)
            row.pack(fill="x", padx=28, pady=(10, 0))
            sg.Toggle(row, self.use_offer).pack(side="right")
            tk.Label(row, text=USE_CARD_TITLE, font=F_LABEL, bg=BG, fg=TEXT,
                     anchor="w").pack(anchor="w")
            tk.Label(row, text=USE_CARD_HELP, font=F_HELP, bg=BG, fg=MUTED,
                     anchor="w").pack(anchor="w")
            self.use_offer.trace_add("write", lambda *_: self._refresh_rec())
        # The same switch for the quick engine, on a PC whose dictation runs
        # on the processor (shown and hidden by _refresh_rec).
        row = self.quick_row = tk.Frame(more, bg=BG)
        sg.Toggle(row, self.use_quick).pack(side="right")
        tk.Label(row, text=USE_QUICK_TITLE, font=F_LABEL, bg=BG, fg=TEXT,
                 anchor="w").pack(anchor="w")
        tk.Label(row, text=USE_QUICK_HELP, font=F_HELP, bg=BG, fg=MUTED,
                 anchor="w").pack(anchor="w")
        self.use_quick.trace_add("write", lambda *_: self._refresh_rec())
        self.quick_note = tk.Label(more, text=PC_ADVANCED_NOTE, font=F_HELP, bg=BG, fg=MUTED,
                                   anchor="w", justify="left", wraplength=560)
        self.quick_note.pack(anchor="w", padx=28, pady=(10, 0))
        self.pc_advanced = sg.Expander(holder, more, "Advanced", "Hide advanced")
        self.pc_advanced.pack(anchor="w", padx=28)
        self._refresh_rec()

    def _page_away(self, f):
        self._heading(f, "Will other devices use this PC?",
                      "Your phone, a laptop or another PC can use this PC's transcription "
                      "and AI editing. On the same Wi-Fi or home network that works as is. "
                      "From anywhere else (mobile data, work, travel), they have to find "
                      "this PC over the internet. The easy way is Tailscale, a free, "
                      "private network between your own devices. Nothing is opened to "
                      "the rest of the internet.")
        self._choice_card(f, self.away, "tailscale", "Install Tailscale (recommended)",
                          "Free for personal use. It installs in its own window now; sign "
                          "in when it asks, then install Tailscale on your phone or other "
                          "PCs with the same account.")
        self._choice_card(f, self.away, "covered", "I have this covered",
                          "I already set up my own way in, such as a VPN.")
        self._choice_card(f, self.away, "home", "Just this PC, or only on my home network",
                          "Nothing to install. You can add Tailscale any time later.")

    def _page_reach(self, f):
        self._heading(f, "How will this PC reach the other one?",
                      "This PC sends your voice to the PC that runs Whisper Unchained "
                      "Server. On the same Wi-Fi or home network that works as is. Anywhere "
                      "else (another house, work, travel) the two have to find each other "
                      "over the internet, and the usual way is Tailscale: a free, private "
                      "network between your own devices. Tailscale isn't installed on this "
                      "PC yet. If the address you entered for the other PC starts with 100. "
                      "or ends in .ts.net, it is a Tailscale address and this PC needs "
                      "Tailscale to use it.")
        self._choice_card(f, self.away, "tailscale", "Install Tailscale (recommended)",
                          "Free for personal use. It installs in its own window now; sign "
                          "in with the same account as on the other PC.")
        self._choice_card(f, self.away, "home", "Both PCs are on the same network",
                          "Nothing to install. You can add Tailscale any time later.")
        self._choice_card(f, self.away, "covered", "I have this covered",
                          "I already set up my own way in, such as a VPN.")

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
            self.ai_note = ("No AI program was found on this PC, so asking the AI to change "
                            "your text won't work until you add one (Ollama and LM Studio "
                            "are free). Settings > AI Editing can find it later, also on "
                            "another PC.")
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
                self.port_note = ("Another program already uses connection number %d, "
                                  "so dictation will use %d instead." % (port, new))
        self._refresh_install_notes()

    def _refresh_install_notes(self):
        if not hasattr(self, "install_notes"):
            return
        notes = ([n for n in (self.ai_note, self.port_note) if n]
                 if self.source() == "local" else [])
        self.install_notes.config(text="\n".join(notes))

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
                    self.game_row.pack(fill="x", padx=28, pady=(INSTALL_ROW_GAP, 0),
                                       after=self.menu_row)
            else:
                self.game_row.pack_forget()
        if hasattr(self, "install_intro"):
            self.install_intro.config(text=INSTALL_INTROS[src])
        self._render_steps()
        if getattr(self, "current", None):
            self.show_section(self.current)  # rebuilt step list: re-highlight this page
        self._mark_step()

    def engine_offer(self):
        """The whisper.cpp offer when it applies and has not been switched off."""
        return self.offer if self.offer and self.use_offer.get() else None

    def quick_possible(self):
        """The Parakeet offer for what this PC and language would get, whether
        or not it is switched on (None = does not apply)."""
        if not self.pk_ok or not hasattr(self, "rec"):
            return None
        return hardware.offer_processor(self.rec, self.english.get())

    def processor_offer(self):
        """The Parakeet offer when it applies and has not been switched off."""
        return self.quick_possible() if self.use_quick.get() else None

    def _pc_download(self):
        """Bytes setup still has to download for what it will install."""
        files = sg.engine_files
        offer = self.engine_offer()
        if offer:
            return files.total_size(files.missing(offer.model, sg.ENGINE_ROOT,
                                                  self._model_root()))
        quick = self.processor_offer()
        if quick:
            return files.total_size(sg.parakeet_files.missing(
                quick.model, sg.ENGINE_ROOT, self._model_root()))
        info = model_catalog.BY_NAME.get(self.rec.model)
        root = self._model_root()
        if info and root and not model_catalog.is_downloaded(info, root):
            return info.size_mb * 10 ** 6
        return 0

    def _refresh_rec(self):
        """The statement on the Your PC step: what was found, what dictation
        will run on, how accurate that is and how much setup downloads."""
        self.rec = hardware.recommend(self.gpus, english_only=self.english.get())
        offer = self.engine_offer()
        quick = self.processor_offer()
        other_card, built_in = hardware.pick_cards(self.other_graphics)
        note = ""
        # The quick engine's switch is only there while it could apply.
        self.quick_row.pack_forget()
        if self.quick_possible():
            self.quick_row.pack(fill="x", padx=28, pady=(10, 0), before=self.quick_note)
        if offer:
            text = hardware.found_text(other_card, built_in)
            note = hardware.NEW_CARD_NOTE
            accuracy = sg.engine_files.MODELS[offer.model].accuracy
        elif quick:
            card = hardware.nvidia_name(self.rec.gpu.name) if self.rec.gpu else other_card
            text = hardware.found_text(card, built_in, False, False, problem=self.rec.problem)
            accuracy = sg.parakeet_files.MODELS[quick.model].accuracy
        else:
            card = hardware.nvidia_name(self.rec.gpu.name) if self.rec.gpu else other_card
            text = hardware.found_text(card, built_in, self.rec.device == "cuda",
                                       problem=self.rec.problem)
            accuracy = model_catalog.BY_NAME[self.rec.model].accuracy
        self.rec_title.config(text=text)
        self.rec_note.config(text=note)
        self.rec_note.pack_forget()
        if note:
            self.rec_note.pack(anchor="w", padx=14, pady=(2, 0), after=self.rec_title)
        size = self._pc_download()
        line = "Accuracy: %s. %s" % (accuracy, (
            sg.DOWNLOAD_BY_SETUP % sg.engine_files.about_size_text(size)) if size
            else DOWNLOAD_NOTHING)
        if not self.fresh and not offer and not quick:
            current = self.values.get("WHISPER_MODEL_SIZE", "")
            if current and current != self.rec.model:
                line += ("\nYou made a different choice before, and setup will switch "
                         "to this one. To keep yours, go back and choose \"Walk me "
                         "through the settings\".")
        self.rec_text.config(text=line)

    def _apply_recommendation(self):
        """Put the recommended model/device into the settings fields."""
        if self.source() != "local":
            return  # no Whisper on this PC: nothing to size
        self._value_set("WHISPER_MODEL_SIZE", self.rec.model)
        self._value_set("WHISPER_DEVICE", self.rec.device)
        self.extra_env["WHISPER_COMPUTE_TYPE"] = self.rec.compute_type
        offer = self.engine_offer()
        if offer:
            for key, value in offer.env().items():
                self._value_set(key, value)
        elif self.offer:     # offered and switched off: the usual engine
            self._offer_declined = True     # the Transcription page does not offer it again
            self._value_set("WHISPER_ENGINE", sg.ENGINE_FW)
        elif self.processor_offer():
            for key, value in self.processor_offer().env().items():
                self._value_set(key, value)
        elif self.pk_ok:     # switched off, or it does not apply: the usual engine
            self._value_set("WHISPER_ENGINE", sg.ENGINE_FW)

    def _value_set(self, key, value):
        f = self.fields.get(key)
        if f is not None and f.var is not None:
            # A drop-down shows a label: accept the plain value for it too.
            f.var.set(next((l for l, v in f.labelled_choices if v == value), value))

    def _page_install(self, f):
        self._heading(f, "Install")
        self.install_intro = tk.Label(f, text=INSTALL_INTROS["local"], font=F_BODY, bg=BG,
                                      fg=MUTED, anchor="w", justify="left", wraplength=660)
        self.install_intro.pack(anchor="w", padx=28, pady=(4, 0))
        self.install_notes = tk.Label(f, text="", font=F_HELP, bg=BG, fg=TEXT, anchor="w",
                                      justify="left", wraplength=660)
        self.install_notes.pack(anchor="w", padx=28, pady=(4, 0))
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
            options.append((game.var, sg.GAME_MODE_TITLE,
                            "Pauses dictation while a game runs full screen; resumes after."))
        for var, title, text in options:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", padx=28, pady=(INSTALL_ROW_GAP, 0))
            if var is self.opt_menu:
                self.menu_row = row
            if game is not None and var is game.var:
                self.game_row = row  # hidden when transcription isn't on this PC
            sg.Toggle(row, var).pack(side="right")
            tk.Label(row, text=title, font=F_LABEL, bg=BG, fg=TEXT,
                     anchor="w").pack(anchor="w")
            tk.Label(row, text=text, font=F_HELP, bg=BG, fg=MUTED,
                     anchor="w").pack(anchor="w")
        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=28, pady=(10, 2))
        self.task_rows = {}
        self.task_frames = {}
        for key, label in INSTALL_TASKS:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", padx=28, pady=(1, 0))
            mark = tk.Label(row, text="○", font=F_HELP, bg=BG, fg=MUTED, width=2, pady=0)
            mark.pack(side="left")
            tk.Label(row, text=label, font=F_HELP, bg=BG, fg=TEXT, pady=0).pack(side="left")
            detail = tk.Label(row, text="", font=F_HELP, bg=BG, fg=MUTED, pady=0)
            detail.pack(side="left", padx=(10, 0))
            self.task_rows[key] = (mark, detail)
            self.task_frames[key] = row
        # One bar for the whole download, beside its row while it runs.
        self.install_bar = sg.ProgressBar(self.task_frames["download"], width=160)

    def _install_progress(self, percent):
        """The download row's bar: 0 to 100, or None to take it away."""
        if percent is None:
            self.install_bar.pack_forget()
            return
        self.install_bar.set(percent)
        if not self.install_bar.winfo_manager():
            self.install_bar.pack(side="left", padx=(12, 0))

    def _page_done(self, f):
        self.done_heading = tk.Label(f, text="You're all set", font=F_SECTION, bg=BG,
                                     fg=TEXT, anchor="w")
        self.done_heading.pack(anchor="w", padx=28, pady=(8, 0))
        self.done_text = tk.Label(f, text="", font=F_BODY, bg=BG, fg=TEXT, anchor="w",
                                  justify="left", wraplength=560)
        self.done_text.pack(anchor="w", padx=28, pady=(8, 0))
        row = tk.Frame(f, bg=BG)
        row.pack(fill="x", padx=28, pady=(18, 0))
        Button(row, "Pair my phone", lambda: self.show_section("Devices & Apps"),
               kind="ghost").pack(side="left")
        Button(row, "Open the install log folder", lambda: os.startfile(steps.LOG_DIR)
               if os.path.isdir(steps.LOG_DIR) else None, kind="ghost").pack(
            side="left", padx=(10, 0))
        # Shown only when the microphone or speakers check found something.
        self.sound_button = Button(row, "Open sound settings", steps.open_sound_settings,
                                   kind="ghost")

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
        if name in (AWAY, REACH) and self.away.get() == "tailscale" and not self.tailscale_started:
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
        if self.installing:      # the button reads "Cancel download" just now
            self.cancel_download.set()
            self.status.config(text="Stopping the download...", fg=WARN)
            return
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
                elif ev[0] == "progress":
                    self._install_progress(ev[1])
                elif ev[0] == "cancellable":
                    # While a large file downloads, Back becomes the way out.
                    self.btn_back.config(text="Cancel download" if ev[1] else "Back")
                    self.btn_back.set_enabled(bool(ev[1]))
                elif ev[0] == "finished":
                    return self._finished(*ev[1:])
        except queue.Empty:
            pass
        self.root.after(200, self._pump)

    def _install_worker(self, updates, opts, words=None):
        last = {}

        def emit(key, state, detail=""):
            self.events.put(("task", key, state, detail))
            # The log gets each change of state, not every percentage.
            if last.get(key) != state or state != "running":
                steps.write_log("step %s: %s%s" % (
                    key, state, " (%s)" % detail if detail else ""))
            last[key] = state
        problems = []
        steps.write_log("---- install started ----")
        for fact in steps.system_facts(hardware.windows_display_adapters):
            steps.write_log(fact)
        steps.write_log("where dictation runs: %s" % opts.get("source"))
        try:
            emit("save", "running")
            sg.write_env(dict(updates, **self.extra_env))
            if words is not None:
                sg.words_config.save(*words)
            self.values = sg.read_env()
            emit("save", "ok")

            if opts["source"] != "local":
                return self._install_client_only(updates, opts, emit, problems)

            if self.values.get("WHISPER_ENGINE") == sg.ENGINE_CPP:
                self._download_engine_files(emit)
            elif self.values.get("WHISPER_ENGINE") == sg.ENGINE_PK:
                self._download_engine_files(emit, sg.parakeet_files.missing(
                    self.values.get("PARAKEET_MODEL", ""), sg.ENGINE_ROOT,
                    model_catalog.models_dir(self.values)))
            else:
                self._download_usual_model(emit)

            if self.dry_run:
                for k in ("service", "login", "menu", "tray", "check"):
                    emit(k, "skip", "test run")
                return self.events.put(("finished", True, []))

            background_failed = False
            emit("service", "running", "approve the Windows prompt")
            ok, log = steps.run_elevated_ps(
                "install_service.ps1", "-NoPause", "-NoScheduledRestart",
                log_name="service.log",
                started=lambda: emit("service", "running", SERVICE_WORKING))
            if ok == steps.ELEVATION_CANCELLED:
                emit("service", "fail", "permission was declined")
                background_failed = True
                problems.append("The part that runs in the background wasn't set up "
                                "because the Windows permission prompt was declined. "
                                "Run Setup.bat again to retry.")
            elif not ok:
                emit("service", "fail", "see installer/logs/service.log")
                background_failed = True
                problems.append("The part that runs in the background could not be "
                                "set up.")
            else:
                emit("service", "ok")
                if steps.log_says(log, "RESTART NEEDED"):
                    problems.append(RESTART_NEEDED)

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

            self._check_audio(emit, problems)
            if background_failed:
                emit("check", "skip", "not set up")
            else:
                emit("check", "running", CHECK_WAITING)
                # The same port this PC's own tray uses: plain when that port
                # is on, else the HTTPS one.
                own_https = sg.local_api.uses_https(self.values)
                ok, detail = steps.wait_for_server(
                    int(sg.local_api.https_port(self.values)) if own_https
                    else sg.api_port(self.values), https=own_https)
                emit("check", "ok" if ok else "fail", detail)
                if not ok:
                    problems.append("Dictation didn't start: %s" % detail)
                    emit("check", "running", "finding out why...")
                    steps.write_log(steps.diagnose_start())
                    emit("check", "fail", detail)
                    if detail == steps.NOT_STARTED:
                        problems.append(NOT_STARTED_HELP)
            for p in problems:
                steps.write_log("problem: %s" % p)
            steps.write_log("---- install finished %s ----"
                            % ("with problems" if problems else "ok"))
            self.events.put(("finished", not problems, problems))
        except Exception as e:
            with open(steps.log_path("wizard.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.events.put(("finished", False, [str(e)]))

    def _download_usual_model(self, emit):
        """The usual engine's model, as the one download row: one percentage
        and one bar. A model that is already here is not fetched again."""
        info = model_catalog.BY_NAME.get(self.values.get("WHISPER_MODEL_SIZE", ""))
        root_dir = model_catalog.models_dir(self.values)
        if not info or not root_dir:
            return emit("download", "ok", "nothing to download")
        if model_catalog.is_downloaded(info, root_dir):
            return emit("download", "ok", "already on this PC")
        about = sg.engine_files.about_size_text(info.size_mb * 10 ** 6)
        emit("download", "running", "starting...")
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
        try:
            while not done.wait(0.5):
                size = max(model_catalog.folder_size(base),
                           model_catalog.folder_size(base + ".partial"))
                pct = min(99, 100 * size // (info.size_mb * 2 ** 20))
                emit("download", "running", "%d%% of about %s" % (pct, about))
                self.events.put(("progress", pct))
        finally:
            self.events.put(("progress", None))
        if "err" in result:
            emit("download", "fail", "did not download")
            raise RuntimeError("The download failed: %s" % result["err"])
        emit("download", "ok")

    def _download_engine_files(self, emit, todo=None):
        """Everything the second engine (whisper.cpp) needs, or the files
        listed in todo (the third engine's model), as the one download row:
        one percentage and one bar for all of it. Each file is checked against
        its known fingerprint before it is used (see api/engine_files.py). A
        test run downloads nothing."""
        files = sg.engine_files
        if todo is None:
            todo = files.missing(self.values.get("WHISPER_CPP_MODEL", ""), sg.ENGINE_ROOT,
                                 model_catalog.models_dir(self.values))
        if not todo:
            return emit("download", "ok", "already on this PC")
        if self.dry_run:
            return emit("download", "skip", "test run")
        about = files.about_size_text(files.total_size(todo))
        overall = files.Overall(todo)
        shown = [-1]

        def progress(spec, done, total):
            pct = overall.update(spec, done)
            if pct != shown[0]:
                shown[0] = pct
                emit("download", "running", "%d%% of about %s" % (pct, about))
                self.events.put(("progress", pct))
        emit("download", "running", "starting...")
        self.events.put(("cancellable", True))
        try:
            for spec, dest in todo:
                files.fetch(spec, dest, progress=progress,
                            cancelled=self.cancel_download.is_set)
        except files.Cancelled:
            emit("download", "fail", "cancelled")
            raise RuntimeError("The download was cancelled. Run Setup.bat again to "
                               "carry on.")
        except Exception:
            emit("download", "fail", "did not download")
            raise
        finally:
            self.events.put(("cancellable", False))
            self.events.put(("progress", None))
        emit("download", "ok")

    def _check_audio(self, emit, problems):
        """A microphone that is switched off in Windows is the commonest reason
        a fresh install "does nothing": say so now, with where to fix it."""
        if self.dry_run:
            emit("audio", "skip", "test run")
            return
        emit("audio", "running")
        mic_ok, speakers_ok, found = steps.check_audio()
        self.audio_problem = bool(found)
        if found:
            emit("audio", "fail", "needs your attention")
            problems.extend(found)
        else:
            emit("audio", "ok")

    def _install_client_only(self, updates, opts, emit, problems):
        """Another PC / cloud: no model, no service. Set up the tray and check
        the connection instead."""
        emit("download", "skip", "not needed here")
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
        self._check_audio(emit, problems)
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
        hotkey = self.values.get("HOTKEY") or hotkey_config.DEFAULT_HOTKEY
        mode = self.values.get("HOTKEY_MODE", "toggle")
        how = ("Hold %s, speak, and let go" if mode == "hold"
               else "Press %s, speak, and press it again") % hotkey
        if ok:
            self.done_heading.config(text="You're all set")
            text = ("%s: the text appears wherever your cursor is.\n\n"
                    "The waveform icon in the tray (by the clock) has Settings and "
                    "pause/restart options. To use your phone as a microphone, "
                    "pair it now." % how)
            if self.tailscale_started and self.source() == "remote":
                text += ("\n\nTailscale: sign in (its icon is by the clock) with the same "
                         "account as on the other PC. Until you do, this PC can only reach "
                         "it on the same network.")
            elif self.tailscale_started:
                text += ("\n\nOther devices: sign in to Tailscale (its icon is by the "
                         "clock), install Tailscale on your phone or other PCs with the "
                         "same account, then pair your phone, so the pairing code uses "
                         "the Tailscale address.")
            if self.dry_run:
                text = "Test run finished: settings saved, downloads ready.\n\n" + text
            self.status.config(text="Install finished.", fg=ACCENT)
        else:
            self.done_heading.config(text="Almost there")
            text = ("Some steps didn't finish:\n  • " + "\n  • ".join(problems) +
                    "\n\nYour settings are saved. Fix the issue and run Setup.bat again; "
                    "it picks up where it left off. The logs are in installer/logs.")
            self.status.config(text="Install finished with problems.", fg=DANGER)
        self.done_text.config(text=text)
        if getattr(self, "audio_problem", False):
            self.sound_button.pack(side="left", padx=(10, 0))
        self.go("Install")


def main():
    dry = "--dry-run" in sys.argv
    root = tk.Tk()
    root.withdraw()            # built hidden, shown once ready: no white flash
    root.configure(bg=sg.BG)
    try:
        app = WizardApp(root, dry_run=dry)
        sg.show_when_ready(root)
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
