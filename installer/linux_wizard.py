"""The setup wizard on Linux (step 2 of 2; scripts/setup_linux.sh runs step 1).

The same window and the same pages as Windows (installer/wizard.py, built on
the Settings window). Only the Install step's work differs, and it is all in
installer/linux_steps.py: systemd --user services instead of a Windows
service, no permission prompt, NVIDIA's libraries as part of the one download.
Sentences are the Windows ones except where the fact differs.

    .venv/bin/python installer/linux_wizard.py            normal run
    .venv/bin/python installer/linux_wizard.py --dry-run  saves settings and
                                            downloads, but starts no service
"""
import os
import sys
import tkinter as tk
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import linux_steps  # noqa: E402
import steps  # noqa: E402
import wizard  # noqa: E402  (creates .env on a fresh install, as on Windows)
import linux_setup_plan  # noqa: E402
from wizard import hardware, hotkey_config, sg, transcribe_client  # noqa: E402

SETUP_COMMAND = "scripts/setup_linux.sh"
# Where the fact differs from Windows: nothing asks for permission, and the
# service is yours, not the system's.
INSTALL_INTROS = dict(wizard.INSTALL_INTROS,
                      local="Ready to install. Nothing here needs your password.")
MENU_TITLE = "Add \"Transcribe\" to the Files right-click menu"
MENU_HELP = "Right-click an audio or video file > Scripts to get its text."
NO_BOOT_NOTE = ("Dictation starts when you log in. To have it start when the PC starts, "
                "before anyone logs in (for a phone to use it), run once in a terminal: "
                "sudo loginctl enable-linger %s")
DRIVER_PROBLEM = ("The graphics card couldn't be used (its driver is too old for it), so "
                  "dictation runs on the processor for now. Update the NVIDIA driver and "
                  "run setup again to use the card.")
MISSING_NOTE = ("A few small system pieces are not installed, so the tray icon, pasting or "
                "the microphone may not work yet. Install them with: sudo apt install %s")
# (the Windows words, what Linux does instead)
REWORDS = (
    ("It installs in its own window now; sign in when it asks, then",
     "Its install page opens in your browser; follow it, sign in, then"),
)
CARD_FULL_NOTE = ("The graphics card is nearly full with other programs, so dictation "
                  "runs on the processor for now. Close them and run setup again to use "
                  "the card.")


class LinuxWizardApp(wizard.WizardApp):

    def __init__(self, root, dry_run=False):
        # Module-level words the Windows class reads when it builds its pages.
        wizard.INSTALL_INTROS = INSTALL_INTROS
        super().__init__(root, dry_run=dry_run)
        self.has_tailscale = linux_steps.tailscale_installed()
        self._render_steps()

    # --- Your PC: the one figure also covers NVIDIA's libraries ----------------

    def _needs_nvidia_libs(self):
        return (not self.engine_offer() and getattr(self, "rec", None) is not None
                and self.rec.device == "cuda" and not linux_steps.nvidia_libs_installed(ROOT))

    def _pc_download(self):
        return super()._pc_download() + (
            linux_steps.NVIDIA_BYTES if self._needs_nvidia_libs() else 0)

    def _refresh_rec(self):
        super()._refresh_rec()
        # Linux only: an NVIDIA card can be in the PC with no driver for it.
        if self.gpus or self.engine_offer():
            return
        card, _built_in = hardware.pick_cards(hardware.display_adapters())
        if "nvidia" in card.lower():
            # The quick engine is made for the processor: no "lighter version".
            self.rec_title.config(text=linux_setup_plan.NO_DRIVER % (
                hardware.with_article(card),
                "" if self.processor_offer() else ", in a lighter version"))

    def _build_content(self, parent):
        super()._build_content(parent)
        for frame in self.frames.values():
            _reword(frame)

    # --- Install page: the same switches, in Linux terms ------------------------

    def _page_install(self, f):
        super()._page_install(f)
        for child in self.menu_row.winfo_children():
            if isinstance(child, tk.Label):
                text = child.cget("text")
                if text.startswith("Add "):
                    child.config(text=MENU_TITLE)
                elif text.startswith("Right-click"):
                    child.config(text=MENU_HELP)

    def _page_done(self, f):
        super()._page_done(f)
        # The Windows buttons open things with os.startfile.
        for row in f.winfo_children():
            for child in row.winfo_children() if isinstance(row, tk.Frame) else ():
                if isinstance(child, sg.Button) and child.cget("text") == \
                        "Open the install log folder":
                    child.command = lambda: linux_steps.open_folder(steps.log_path(""))
        self.sound_button.command = linux_steps.open_sound_settings

    def next(self):
        if (self.current in (wizard.AWAY, wizard.REACH) and self.away.get() == "tailscale"
                and not self.tailscale_started and not self.dry_run):
            # Tailscale's Linux install wants a terminal and a password: its
            # own page says how, and setup carries on meanwhile.
            self.tailscale_started = linux_steps.install_tailscale()
            order = self.steps()
            self.go(order[order.index(self.current) + 1])
            self.status.config(text="Tailscale's install page is open in your browser.",
                               fg=wizard.MUTED)
            return
        return super().next()

    # --- install -----------------------------------------------------------------

    def _install_worker(self, updates, opts, words=None):
        def emit(key, state, detail=""):
            """One line of the Install step, also kept in installer/logs/wizard.log
            (as on Windows), so a failed install can be read afterwards."""
            self.events.put(("task", key, state, detail))
            if state != "running" or detail:
                try:
                    steps.write_log("step %s: %s%s" % (key, state,
                                                       " (%s)" % detail if detail else ""))
                except Exception:
                    pass
        problems = []
        try:
            steps.write_log("---- install started (Linux) ----")
            steps.write_log("graphics: %s" % ", ".join(hardware.display_adapters()))
            steps.write_log("where dictation runs: %s" % opts.get("source"))
        except Exception:
            pass
        try:
            emit("save", "running")
            sg.write_env(dict(updates, **self.extra_env))
            if words is not None:
                sg.words_config.save(*words)
            self.values = sg.read_env()
            emit("save", "ok")

            if opts["source"] != "local":
                return self._install_client_only(updates, opts, emit, problems)

            engine = self.values.get("WHISPER_ENGINE")
            if engine == sg.ENGINE_CPP:
                self._download_engine_files(emit)
            elif engine == sg.ENGINE_PK:
                self._download_engine_files(emit, sg.parakeet_files.missing(
                    self.values.get("PARAKEET_MODEL", ""), sg.ENGINE_ROOT,
                    sg.model_catalog.models_dir(self.values)))
            else:
                self._prepare_card(emit, problems)
                self._download_usual_model(emit)

            if self.dry_run:
                for k in ("service", "login", "menu", "tray", "check"):
                    emit(k, "skip", "test run")
                return self.events.put(("finished", True, []))

            emit("service", "running", "a few seconds")
            ok, detail = linux_steps.install_server(ROOT, sg.api_port(self.values))
            background_failed = not ok
            if ok:
                emit("service", "ok")
                if not linux_steps.lingering():
                    self.boot_note = NO_BOOT_NOTE % (os.environ.get("USER") or "$USER")
            else:
                emit("service", "fail", detail or "could not be set up")
                problems.append("The part that runs in the background could not be set "
                                "up%s." % (": " + detail if detail else ""))

            self._desktop_steps(opts, emit, problems)
            self._check_audio(emit, problems)
            if background_failed:
                emit("check", "skip", "not set up")
            else:
                self._wait_until_ready(emit, problems)
            self.events.put(("finished", not problems, problems))
        except Exception as e:
            with open(steps.log_path("wizard.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.events.put(("finished", False, [str(e)]))

    def _prepare_card(self, emit, problems):
        """NVIDIA only: fetch its libraries (the first part of the one
        download), pick the card with room, and make sure the engine can
        really use it. Falls back to the processor with a plain reason."""
        if self.values.get("WHISPER_DEVICE", "cpu") == "cpu":
            return
        changes = {}
        if not linux_steps.nvidia_libs_installed(ROOT):
            if self.dry_run:
                emit("download", "skip", "test run")
            else:
                about = sg.engine_files.about_size_text(linux_steps.NVIDIA_BYTES)
                emit("download", "running", "the graphics card's libraries, about %s" % about)
                self.events.put(("cancellable", True))
                try:
                    ok, detail = linux_steps.install_nvidia_libs(
                        ROOT, cancelled=self.cancel_download.is_set,
                        progress=lambda done, total: self.events.put(
                            ("progress", min(99, 100 * done // max(1, total)))))
                finally:
                    self.events.put(("cancellable", False))
                    self.events.put(("progress", None))
                if detail == "cancelled":
                    emit("download", "fail", "cancelled")
                    raise RuntimeError("The download was cancelled. Run %s again to carry "
                                       "on." % SETUP_COMMAND)
                if not ok:
                    emit("download", "fail", "did not download")
                    raise RuntimeError("The download failed: %s" % (detail or "no detail"))
        picked = linux_steps.pick_gpu()
        if not self.dry_run:
            changes = linux_steps.gpu_settings(
                picked, self.values.get("WHISPER_COMPUTE_TYPE") or "float16")
            if changes.get("WHISPER_DEVICE") == "cpu":
                problems.append(CARD_FULL_NOTE)
            elif not linux_steps.cuda_works(ROOT, picked[0] if picked else None):
                changes = {"WHISPER_DEVICE": "cpu", "WHISPER_COMPUTE_TYPE": "int8",
                           "CUDA_VISIBLE_DEVICES": ""}
                problems.append(DRIVER_PROBLEM)
            if changes.get("WHISPER_DEVICE") == "cpu":
                # The large model is very slow on a processor: the lighter one.
                small = "small.en" if self.english.get() else "small"
                changes["WHISPER_MODEL_SIZE"] = small
        if changes:
            sg.write_env(changes)
            self.values = sg.read_env()

    def _desktop_steps(self, opts, emit, problems):
        """Start at login, the Files menu, and the tray (one service does the
        first and the last)."""
        emit("login", "running")
        emit("tray", "running")
        ok, detail = linux_steps.install_desktop(ROOT, at_login=opts["login"])
        emit("login", ("ok" if ok else "fail") if opts["login"] else "skip",
             "" if opts["login"] else "not chosen")
        emit("tray", "ok" if ok else "fail", "" if ok else (detail or "did not start"))
        missing = linux_steps.desktop_missing()
        if missing:
            problems.append(MISSING_NOTE % " ".join(missing))
        if not ok:
            problems.append("The tray app did not start.")
        if not opts["menu"]:
            return emit("menu", "skip", "not chosen")
        ok, detail = linux_steps.install_menu(ROOT)
        if ok is None:
            emit("menu", "skip", detail)
        else:
            emit("menu", "ok" if ok else "fail", "" if ok else detail)
            if not ok:
                problems.append("The right-click menu could not be added: %s" % detail)

    def _wait_until_ready(self, emit, problems):
        emit("check", "running", linux_steps.waiting_text(0))
        own_https = sg.local_api.uses_https(self.values)
        port = int(sg.local_api.https_port(self.values)) if own_https \
            else sg.api_port(self.values)
        ok, detail, said = linux_steps.wait_for_server(
            port, own_https, steps.wait_for_server,
            progress=lambda s: emit("check", "running", linux_steps.waiting_text(s)))
        emit("check", "ok" if ok else "fail", detail)
        if not ok:
            text = "Dictation didn't start: %s." % detail
            if said:
                text += " The last things it said:\n      " + "\n      ".join(said)
            problems.append(text)

    def _check_audio(self, emit, problems):
        if self.dry_run:
            emit("audio", "skip", "test run")
            return
        emit("audio", "running")
        if not linux_steps.recorder() and any("sudo apt install" in p for p in problems):
            # No recording program: already said, with the other missing pieces.
            return emit("audio", "skip", "couldn't be checked yet")
        _mic, _speakers, found = linux_steps.check_audio()
        self.audio_problem = bool(found)
        if found:
            emit("audio", "fail", "needs your attention")
            problems.extend(found)
        else:
            emit("audio", "ok")

    def _install_client_only(self, updates, opts, emit, problems):
        """Another PC / cloud: no model, no server here. Set up the tray and
        check the connection instead."""
        emit("download", "skip", "not needed here")
        emit("service", "skip", "not needed here")
        if self.dry_run:
            for k in ("login", "menu", "tray"):
                emit(k, "skip", "test run")
        else:
            self._desktop_steps(opts, emit, problems)
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
        super()._finished(ok, problems)
        # The same words, minus the Windows-only names.
        text = self.done_text.cget("text")
        text = text.replace("The waveform icon in the tray (by the clock)",
                            "The waveform icon in the top bar")
        text = text.replace("(its icon is by the clock)", "(tailscale up, in a terminal)")
        text = text.replace("Setup.bat", SETUP_COMMAND)
        hotkey = self.values.get("HOTKEY") or hotkey_config.DEFAULT_HOTKEY
        if ok and "Press %s" % hotkey in text:
            import linux_listener
            text = text.replace("Press %s" % hotkey, "Press %s"
                                % linux_listener.pretty_key(hotkey), 1)
        note = getattr(self, "boot_note", "")
        if note and self.source() == "local":
            text += "\n\n" + note
        self.done_text.config(text=text)


def _reword(widget):
    """Swap the few sentences that state a Windows fact for the Linux one."""
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            text = child.cget("text")
            for old, new in REWORDS:
                if old in text:
                    child.config(text=text.replace(old, new))
        _reword(child)


def main():
    dry = "--dry-run" in sys.argv
    root = tk.Tk(className="whisper-unchained")
    root.withdraw()            # built hidden, shown once ready: no white flash
    root.configure(bg=sg.BG)
    try:
        app = LinuxWizardApp(root, dry_run=dry)
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
