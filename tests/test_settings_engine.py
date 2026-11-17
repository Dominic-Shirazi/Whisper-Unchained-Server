"""The "Graphics card" card on the Transcription page of Settings: choosing the
speech engine (faster-whisper for NVIDIA, whisper.cpp for AMD, Intel or none),
its model, the Download button for its files, and what Save does with it.

Built on a temp copy of .env.example with temp folders for the engine and the
models. The downloader is a stand-in: nothing is downloaded, the real .env,
models folder and api/bin are never touched, and nothing is restarted."""
import os
import shutil
import sys
import threading
import time
import tkinter as tk

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import settings_gui as sg  # noqa: E402

# The Settings window is Windows-only (winreg, the Windows tray and hotkeys).
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Settings window")

ef = sg.engine_files
TURBO, BASE = "large-v3-turbo-q5_0", "base"
RADEON = "AMD Radeon RX 6600"
UHD = "Intel(R) UHD Graphics 630"


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    yield root
    root.destroy()


class Downloads:
    """Stands in for engine_files.fetch_missing: puts empty files where the
    real ones would go, after reporting some progress. `hold` makes it wait so
    a test can look at the page mid-download."""

    def __init__(self):
        self.calls, self.error, self.hold = [], None, None
        self.started = threading.Event()

    def fetch_missing(self, model, repo_root, models_dir=None, opener=None, progress=None,
                      cancelled=None):
        self.calls.append((model, repo_root, models_dir))
        todo = ef.missing(model, repo_root, models_dir)
        for spec, dest in todo:
            progress(spec, spec.size * 36 // 100 + 1, spec.size)
            self.started.set()
            if self.hold is not None:
                deadline = time.time() + 20
                while not self.hold.is_set() and time.time() < deadline:
                    if cancelled():
                        raise ef.Cancelled("Download cancelled.")
                    time.sleep(0.01)
            if self.error:
                raise ef.EngineFileError(self.error)
            if spec.kind == "zip":
                os.makedirs(dest, exist_ok=True)
                open(ef.engine_binary(repo_root), "wb").close()
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                open(dest, "wb").close()
        return [s for s, _d in todo]


@pytest.fixture
def make_app(tmp_path, monkeypatch, tk_root):
    """make(extra env lines, graphics names) -> the Settings app on a temp .env."""
    made = []
    downloads = Downloads()
    monkeypatch.setattr(sg.engine_files, "fetch_missing", downloads.fetch_missing)
    monkeypatch.setattr(sg, "ENGINE_ROOT", str(tmp_path))
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    monkeypatch.setattr(sg, "installed_ollama_models", lambda url: [])

    def make(extra="", graphics=(RADEON,)):
        env = tmp_path / ".env"
        text = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        text = "".join(l for l in text.splitlines(True) if not l.startswith("WHISPER_MODELS_DIR"))
        env.write_text(text + "\nWHISPER_MODELS_DIR=%s\n%s" % (tmp_path / "models", extra),
                       encoding="utf-8")
        monkeypatch.setattr(sg, "ENV_PATH", str(env))
        monkeypatch.setattr(sg.hardware, "windows_display_adapters", lambda: list(graphics))
        win = tk.Toplevel(tk_root)
        win.withdraw()
        made.append(win)
        app = sg.SettingsApp(win)
        app.downloads = downloads
        return app

    yield make
    for win in made:
        win.destroy()


def _texts(widget):
    out = []
    for c in widget.winfo_children():
        if isinstance(c, tk.Text):
            out.append(c.get("1.0", "end").strip())
        elif isinstance(c, tk.Label):
            out.append(str(c.cget("text")))
        out.extend(_texts(c))
    return out


def _wait(app, done, seconds=10):
    deadline = time.time() + seconds
    while not done() and time.time() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert done(), "timed out"


def _download(app):
    app._engine_download()
    _wait(app, lambda: app._engine_job is None)


def _put_files(tmp_path, model=TURBO):
    os.makedirs(os.path.dirname(ef.engine_binary(str(tmp_path))), exist_ok=True)
    open(ef.engine_binary(str(tmp_path)), "wb").close()
    os.makedirs(tmp_path / "models", exist_ok=True)
    for name in (ef.VAD.name, ef.MODELS[model].name):
        open(tmp_path / "models" / name, "wb").close()


def _env_lines(key):
    with open(sg.ENV_PATH, encoding="utf-8") as f:
        return [l.strip() for l in f if l.startswith(key + "=")]


# --- what the card says -----------------------------------------------------------

def test_card_offers_the_two_choices_in_plain_words(make_app):
    app = make_app()
    card = "\n".join(_texts(app._engine_card))
    assert "Graphics card" in card
    assert "NVIDIA (recommended when you have one)" in card
    assert "AMD or Intel graphics, or no graphics card" in card
    # Honest: this engine is new.
    assert "It is new and has only been tried on a few PCs so far." in card
    for spec in ef.MODELS.values():
        assert spec.note in card
    assert "large-v3-turbo  ·  574 MB" in card and "base  ·  148 MB" in card
    assert "—" not in card and "Vulkan" not in card and "signed" not in card.lower()
    # The card sits with this PC's own settings, above the model list.
    assert str(app._engine_card.winfo_parent()) == str(app.groups["src_local"])


def test_nvidia_is_the_default_and_shows_the_usual_settings(make_app):
    app = make_app()
    assert app._value("WHISPER_ENGINE") == "faster-whisper" and not app._engine_on()
    assert not app._engine_details.winfo_manager()                 # folded away
    assert app.subgroups["src_local:fw"].winfo_manager()           # model + device + list
    assert not app.dirty


def test_choosing_the_other_engine_swaps_the_settings_shown(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    assert app._engine_details.winfo_manager()
    assert not app.subgroups["src_local:fw"].winfo_manager()
    assert app.dirty
    assert not app._engine_fold.opened                             # "Where it runs" is folded
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_FW)
    assert not app._engine_details.winfo_manager()
    assert app.subgroups["src_local:fw"].winfo_manager()


def test_card_only_shows_when_this_pc_runs_the_server(make_app):
    app = make_app()
    app.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service (Groq, OpenAI, ...)")
    assert not app.groups["src_local"].winfo_manager()
    app.fields["TRANSCRIBE_SOURCE"].var.set("This PC (runs Whisper here)")
    assert app.groups["src_local"].winfo_manager()


def test_default_model_follows_the_graphics_found(make_app):
    assert make_app(graphics=(RADEON,))._value("WHISPER_CPP_MODEL") == TURBO
    assert make_app(graphics=(UHD,))._value("WHISPER_CPP_MODEL") == BASE
    assert make_app(graphics=())._value("WHISPER_CPP_MODEL") == BASE
    # What is saved wins over the guess.
    assert make_app("WHISPER_CPP_MODEL=large-v3-turbo-q5_0\n",
                    graphics=(UHD,))._value("WHISPER_CPP_MODEL") == TURBO


# --- the device lines (pure) ------------------------------------------------------

def test_device_text_before_the_first_start():
    assert sg.engine_device_text([RADEON], None) == "Found on this PC: %s." % RADEON
    assert sg.engine_device_text(["A", "B", "C"], {}) == "Found on this PC: A, B and C."
    assert sg.engine_device_text([], None) == "No graphics card found on this PC."


def test_device_text_says_what_automatic_chose():
    text = sg.engine_device_text([RADEON], {"device": "gpu", "name": RADEON + " (AMD)"})
    assert text.splitlines()[1] == "On its last start, Automatic used %s (AMD)." % RADEON
    text = sg.engine_device_text([UHD], {"device": "cpu", "found": [UHD],
                                         "gpu_seconds": 2.52, "cpu_seconds": 1.68})
    assert text.splitlines()[1] == (
        "On its last start, Automatic used the processor. It was faster than the "
        "graphics in a short test (1.7 s against 2.5 s).")
    text = sg.engine_device_text([UHD], {"device": "gpu", "name": UHD, "found": [UHD],
                                         "gpu_seconds": 0.9, "cpu_seconds": 1.68})
    assert "faster than the processor in a short test (0.9 s against 1.7 s)" in text
    text = sg.engine_device_text([], {"device": "cpu", "found": []})
    assert text.splitlines()[1] == ("On its last start, Automatic used the processor. "
                                    "The engine found no graphics it can use.")
    assert "—" not in text


def test_card_reads_what_the_engine_remembered(make_app, tmp_path):
    os.makedirs(tmp_path / "models")
    sg.whispercpp_engine.write_auto_cache(
        str(tmp_path / "models" / "whispercpp_auto.json"),
        last={"device": "cpu", "found": [UHD], "gpu_seconds": 50.7, "cpu_seconds": 36.3})
    app = make_app("WHISPER_ENGINE=whisper.cpp\nWHISPER_CPP_MODEL=large-v3-turbo-q5_0\n",
                   graphics=(UHD,))
    text = app._engine_device_label.cget("text")
    assert "Found on this PC: %s." % UHD in text
    assert "Automatic used the processor" in text and "36.3 s against 50.7 s" in text
    assert sg.ENGINE_SLOW_NOTE in text                  # the large model on the processor
    app.fields["WHISPER_CPP_MODEL"].var.set(BASE)
    assert sg.ENGINE_SLOW_NOTE not in app._engine_device_label.cget("text")


# --- download ---------------------------------------------------------------------

def test_missing_files_are_listed_with_a_download_button(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    assert app._engine_status.cget("text") == (
        "Still needed: the speech engine (30 MB), the silence filter (1 MB) and the "
        "speech model (574 MB).")
    assert app._engine_button.cget("text") == "Download" and app._engine_button._enabled
    app.fields["WHISPER_CPP_MODEL"].var.set(BASE)
    assert "the speech model (148 MB)" in app._engine_status.cget("text")


def test_download_shows_progress_then_enables_save(make_app, tmp_path):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    app.dirty = False
    app.set_saved_state(True)                   # as if nothing were unsaved
    app.downloads.hold = threading.Event()
    app._engine_download()
    _wait(app, lambda: app.downloads.started.is_set())
    _wait(app, lambda: "Downloading" in app._engine_status.cget("text"))
    assert app._engine_status.cget("text") == "Downloading the speech engine: 36% of 30 MB"
    assert not app._engine_button._enabled and app._engine_cancel.winfo_manager()
    app._engine_download()                      # a second press does nothing
    assert len(app.downloads.calls) == 1
    app.downloads.hold.set()
    _wait(app, lambda: app._engine_job is None)
    assert app.downloads.calls == [(TURBO, str(tmp_path), str(tmp_path / "models"))]
    assert app._engine_missing() == []
    assert app._engine_status.cget("text") == sg.ENGINE_READY
    assert not app._engine_button._enabled and "Downloaded" in app._engine_button.cget("text")
    assert not app._engine_cancel.winfo_manager()
    assert app.dirty and app.btn_save._enabled                      # Save is on now
    assert app.status.cget("text") == sg.ENGINE_DOWNLOADED


def test_cancel_stops_the_download(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    app.downloads.hold = threading.Event()
    app._engine_download()
    _wait(app, lambda: app.downloads.started.is_set())
    app._engine_cancel_download()
    _wait(app, lambda: app._engine_job is None)
    assert app._engine_status.cget("text") == "Download cancelled."
    assert app._engine_missing() and app._engine_button._enabled
    assert app._engine_button.cget("text") == "Download"


def test_a_failed_download_says_why(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    app.downloads.error = ("The downloaded copy of the speech engine is not the file that "
                           "was expected, so it was not used. Try again.")
    _download(app)
    assert app._engine_status.cget("text") == app.downloads.error
    assert str(app._engine_status.cget("fg")) == sg.DANGER
    assert app._engine_button._enabled                              # can try again


# --- save -------------------------------------------------------------------------

def test_save_is_refused_while_files_are_missing(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    assert app.save() is False
    assert app.status.cget("text") == (
        "This engine still needs the speech engine (30 MB), the silence filter (1 MB) and "
        "the speech model (574 MB). Press Download on the Transcription page first.")
    assert _env_lines("WHISPER_ENGINE") == []                       # nothing was written
    assert app.dirty


def test_save_and_restart_is_refused_the_same_way(make_app, monkeypatch):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    reached = []

    class NoWindows:                # the restart itself must never be reached
        def __getattr__(self, name):
            reached.append(name)
            raise AssertionError("the restart was attempted")

    monkeypatch.setattr(sg, "ctypes", NoWindows())
    app.save_and_restart()
    assert reached == [] and "Press Download" in app.status.cget("text")
    assert _env_lines("WHISPER_ENGINE") == []


def test_after_download_save_writes_the_three_settings(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    _download(app)
    assert app.save() is True
    assert _env_lines("WHISPER_ENGINE") == ["WHISPER_ENGINE=whisper.cpp"]
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=large-v3-turbo-q5_0"]
    assert _env_lines("WHISPER_CPP_DEVICE") == ["WHISPER_CPP_DEVICE=auto"]
    assert app.status.cget("text") == "Saved. Restart the API to apply."
    # What the server will check when it starts with these settings.
    env = sg.read_env()
    assert sg.whispercpp_engine.files_problem(env, sg.ENGINE_ROOT, env["WHISPER_MODELS_DIR"]) is None
    assert sg.whispercpp_engine.engine_name(env["WHISPER_ENGINE"]) == "whisper.cpp"


def test_processor_only_and_the_small_model_are_saved(make_app, tmp_path):
    _put_files(tmp_path, BASE)
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    app.fields["WHISPER_CPP_MODEL"].var.set(BASE)
    app.fields["WHISPER_CPP_DEVICE"].var.set("Processor only")
    assert app.save() is True
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=base"]
    assert _env_lines("WHISPER_CPP_DEVICE") == ["WHISPER_CPP_DEVICE=cpu"]


def test_saving_with_nvidia_adds_nothing_about_the_other_engine(make_app):
    app = make_app()
    app.fields["REMOVE_FILLERS"].var.set(True)
    app.mark_dirty()
    assert app.save() is True
    for key in ("WHISPER_ENGINE", "WHISPER_CPP_MODEL", "WHISPER_CPP_DEVICE"):
        assert _env_lines(key) == []


def test_switching_back_to_nvidia_is_saved_without_any_download(make_app, tmp_path):
    app = make_app("WHISPER_ENGINE=whisper.cpp\nWHISPER_CPP_MODEL=base\n"
                   "WHISPER_CPP_DEVICE=auto\n")
    assert app._engine_on() and app._engine_details.winfo_manager()
    assert app._engine_missing()                                    # nothing is downloaded
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_FW)
    assert app.save() is True
    assert _env_lines("WHISPER_ENGINE") == ["WHISPER_ENGINE=faster-whisper"]
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=base"]   # kept for later


def test_settings_made_by_hand_are_kept(make_app, tmp_path):
    own = tmp_path / "mine" / "custom-q8.bin"
    os.makedirs(own.parent)
    own.write_bytes(b"x")
    _put_files(tmp_path)
    app = make_app("WHISPER_ENGINE=whisper.cpp\nWHISPER_CPP_MODEL=%s\nWHISPER_CPP_DEVICE=1\n"
                   % own)
    card = "\n".join(_texts(app._engine_card))
    assert "Your own file: %s" % own in card
    assert app.fields["WHISPER_CPP_DEVICE"].var.get() == "Graphics device 1"
    assert app._engine_missing() == []                  # the engine and filter are there
    app.mark_dirty()
    assert app.save() is True
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=%s" % own]
    assert _env_lines("WHISPER_CPP_DEVICE") == ["WHISPER_CPP_DEVICE=1"]


def test_a_missing_file_of_your_own_is_refused_in_one_sentence(make_app, tmp_path):
    _put_files(tmp_path)
    gone = tmp_path / "mine" / "gone.bin"
    app = make_app("WHISPER_ENGINE=whisper.cpp\nWHISPER_CPP_MODEL=%s\n" % gone)
    app.mark_dirty()
    assert app.save() is False
    assert "The whisper.cpp model is missing" in app.status.cget("text")
    assert str(gone) in app.status.cget("text")


def test_no_warning_about_the_other_engines_model(make_app, tmp_path):
    """With whisper.cpp chosen, the faster-whisper model not being downloaded
    is no reason for a warning after saving."""
    _put_files(tmp_path)
    app = make_app("WHISPER_ENGINE=whisper.cpp\nWHISPER_CPP_MODEL=large-v3-turbo-q5_0\n")
    app.mark_dirty()
    assert app.save() is True
    assert app.status.cget("text") == "Saved. Restart the API to apply."
