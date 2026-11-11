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
import model_catalog  # noqa: E402
import settings_gui as sg  # noqa: E402
from settings_gui import (ACCENT, BG, BORDER, DANGER, F_BODY, F_BUTTON, F_HELP,  # noqa: E402
                          F_LABEL, F_SECTION, F_TITLE, MUTED, RAISED, SURFACE, TEXT,
                          WARN, Button)

QUICK = ["Welcome", "Your PC", "Install", "Done"]
CUSTOM_PAGES = ["Transcription", "Words & Triggers", "AI Editing (LLM)", "Hotkey",
                "Phone", "Startup"]
CUSTOM = ["Welcome", "Your PC"] + CUSTOM_PAGES + ["Install", "Done"]
STEP_LABELS = {"AI Editing (LLM)": "AI Editing", "Words & Triggers": "Words"}


class WizardApp(sg.SettingsApp):
    def __init__(self, root, dry_run=False):
        self.dry_run = dry_run
        self.fresh = FRESH
        self.mode = tk.StringVar(root, value="quick")
        self.english = tk.BooleanVar(root, value=True)
        self.gpus = hardware.detect_gpus()
        self.extra_env = {}
        self.installing = False
        self.installed = False
        self.events = queue.Queue()
        super().__init__(root)
        root.title("Whisper Unchained Setup")
        root.geometry("900x700")
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

    def steps(self):
        return CUSTOM if self.mode.get() == "custom" else QUICK

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
        self._value_set("WHISPER_MODEL_SIZE", self.rec.model)
        self._value_set("WHISPER_DEVICE", self.rec.device)
        self.extra_env["WHISPER_COMPUTE_TYPE"] = self.rec.compute_type

    def _value_set(self, key, value):
        f = self.fields.get(key)
        if f is not None and f.var is not None:
            f.var.set(value)

    def _page_install(self, f):
        self._heading(f, "Install",
                      "Ready to install. Windows will ask once for permission, to set up "
                      "the background server.")
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
        if name == "Install":
            if self.installed:
                return self.go("Done")
            return self.start_install()
        if name == "Done":
            return self.root.destroy()
        if name in order and order.index(name) + 1 < len(order):
            self.go(order[order.index(name) + 1])
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
        opts = {"login": self.opt_login.get(), "menu": self.opt_menu.get()}
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
