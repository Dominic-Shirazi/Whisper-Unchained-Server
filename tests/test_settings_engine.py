"""The Transcription page of Settings, as an everyday person meets it: one
statement about what this PC has and what dictation runs on, the accuracy in a
word, one download with one number, and everything technical (the choice of
speech engine, the model lists, addresses, folders) behind "Advanced".

Built on a temp copy of .env.example with temp folders for the engine and the
models. The downloaders are stand-ins: nothing is downloaded, the real .env,
models folder and api/bin are never touched, no browser is opened and nothing
is restarted."""
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
RTX = "NVIDIA GeForce RTX 3060"
CPP = "WHISPER_ENGINE=whisper.cpp\n"
# Words an everyday person should never have to read on the page's main view.
JARGON = ("engine", "Vulkan", "CUDA", "VAD", "VRAM", "whisper.cpp", "faster-whisper",
          "API", "compute", "GGML", "—")
# The room the page has at the default window size (1000 x 760, less the
# header and the footer), measured on the real window.
PAGE_ROOM = 568


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


def _put_usual_model(folder, name="large-v3-turbo"):
    """Make a model of the usual engine look downloaded."""
    snap = os.path.join(str(folder), sg.model_catalog.BY_NAME[name].cache_folder,
                        "snapshots", "sample")
    os.makedirs(snap)
    with open(os.path.join(snap, "model.bin"), "wb") as f:
        f.write(b"sample")
    open(os.path.join(snap, "config.json"), "w").close()


@pytest.fixture
def make_app(tmp_path, monkeypatch, tk_root):
    """make(extra env lines, graphics names, have=...) -> the Settings app on a
    temp .env. have: a model of the usual engine that is already downloaded
    (None = none is)."""
    made = []
    downloads = Downloads()
    monkeypatch.setattr(sg.engine_files, "fetch_missing", downloads.fetch_missing)

    def no_network(*a, **k):
        raise AssertionError("a test tried to download")
    monkeypatch.setattr(sg.engine_files, "open_url", no_network)
    monkeypatch.setattr(sg.model_catalog, "download", no_network)
    monkeypatch.setattr(sg.webbrowser, "open", no_network)
    monkeypatch.setattr(sg, "ENGINE_ROOT", str(tmp_path))
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    monkeypatch.setattr(sg, "installed_ollama_models", lambda url: [])

    def make(extra="", graphics=(RADEON,), have="large-v3-turbo"):
        env = tmp_path / ".env"
        text = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        text = "".join(l for l in text.splitlines(True) if not l.startswith("WHISPER_MODELS_DIR"))
        env.write_text(text + "\nWHISPER_MODELS_DIR=%s\n%s" % (tmp_path / "models", extra),
                       encoding="utf-8")
        if have and not os.path.isdir(tmp_path / "models" / sg.model_catalog.BY_NAME[
                have].cache_folder):
            _put_usual_model(tmp_path / "models", have)
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
    """Every piece of text under a widget that is on the page (packed)."""
    out = []
    for c in widget.winfo_children():
        if not c.winfo_manager():
            continue
        if isinstance(c, tk.Text):
            out.append(c.get("1.0", "end").strip())
        elif isinstance(c, tk.Label):
            out.append(str(c.cget("text")))
        out.extend(_texts(c))
    return out


def _main(app):
    return "\n".join(_texts(app._tx_main))


def _view(app, name):
    return "\n".join(_texts(app._tx_views[name]))


def _wait(app, done, seconds=10):
    deadline = time.time() + seconds
    while not done() and time.time() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert done(), "timed out"


def _download(app):
    app._card_download()
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


def _needs(app):
    """The download sentence when it is on the page, else None."""
    return app._dl_line.cget("text") if app._dl_line.winfo_manager() else None


# --- one statement: what was found, and what dictation runs on --------------------

def test_an_nvidia_card_is_named_and_used(make_app):
    app = make_app(graphics=(RTX, UHD))
    assert app._pc_statement.cget("text") == (
        "We found an NVIDIA GeForce RTX 3060. Dictation will run on it.")
    assert not app._engine_on() and not app._engine_pending()
    assert not app._pc_note.winfo_manager()             # nothing new to warn about
    assert _needs(app) is None                          # nothing to download
    assert not app._engine_button.winfo_manager()
    assert not app.dirty


def test_an_amd_card_is_named_and_gets_the_one_download(make_app):
    app = make_app()
    assert app._pc_statement.cget("text") == (
        "We found an AMD Radeon RX 6600. Dictation will run on it.")
    # Honest: this is new.
    assert app._pc_note.cget("text") == sg.hardware.NEW_CARD_NOTE
    assert "only been tried on a few PCs" in _main(app)
    assert _needs(app) == "This PC needs a one-time download of about 600 MB."
    assert app._engine_button.cget("text") == "Download" and app._engine_button._enabled
    assert app._engine_button.winfo_manager()
    # Nothing was chosen for the person behind their back: the saved settings
    # stay as they are until the download is there.
    assert app._engine_pending() and app._value("WHISPER_ENGINE") == sg.ENGINE_FW
    assert not app.dirty


def test_no_graphics_card_runs_on_the_processor(make_app):
    app = make_app("WHISPER_MODEL_SIZE=small.en\nWHISPER_DEVICE=cpu\n", graphics=(),
                   have="small.en")
    assert app._pc_statement.cget("text") == (
        "No graphics card was found. Dictation will run on the processor, with a "
        "smaller model.")
    assert app._accuracy.cget("text") == "Accuracy: good"
    assert _needs(app) is None and not app._engine_pending()


def test_built_in_graphics_are_named_but_not_used(make_app):
    app = make_app("WHISPER_MODEL_SIZE=small.en\n", graphics=(UHD,), have="small.en")
    assert app._pc_statement.cget("text") == (
        "No separate graphics card was found, only Intel UHD Graphics 630, which is "
        "built into the processor. Dictation will run on the processor, with a "
        "smaller model.")
    assert not app._engine_pending()                    # nothing is offered for it


def test_nvidia_is_never_shown_on_a_pc_without_one(make_app):
    for graphics in ((RADEON,), (UHD,), ()):
        app = make_app(graphics=graphics)
        app._not_right.toggle()                         # even with the note unfolded
        shown = _main(app).replace(sg.hardware.NOT_RIGHT_NOTE, "")
        assert "NVIDIA" not in shown and "nvidia" not in shown.lower()


def test_a_card_that_is_set_aside_is_said_plainly(make_app):
    app = make_app("WHISPER_DEVICE=cpu\n", graphics=(RTX,))
    assert app._pc_statement.cget("text") == (
        "We found an NVIDIA GeForce RTX 3060, but dictation is set to run on the "
        "processor.")
    # Set to a graphics card that is not there: say so, and where to change it.
    app = make_app("WHISPER_DEVICE=cuda\n", graphics=())
    assert app._pc_statement.cget("text") == sg.NO_CARD_BUT_SET
    assert "Advanced" in sg.NO_CARD_BUT_SET


def test_the_main_view_has_no_jargon_and_no_choice_of_engine(make_app):
    for graphics, extra in (((RTX,), ""), ((RADEON,), ""), ((), ""), ((RADEON,), CPP)):
        app = make_app(extra, graphics=graphics)
        shown = _main(app)
        for word in JARGON:
            assert word not in shown, (word, graphics)
        assert "signed" not in shown.lower()
        for _value, title, text in sg.ENGINE_CHOICES:       # the two radio buttons
            assert title not in shown and text not in shown
        # The parts of the download are not listed: one sentence, one number.
        for part in ("speech engine", "silence filter", "speech model"):
            assert part not in shown
    for text in (sg.hardware.NOT_RIGHT_NOTE, sg.hardware.NEW_CARD_NOTE, sg.DOWNLOAD_NEEDED,
                 sg.MODELS_HELP, sg.NO_CARD_BUT_SET):
        assert "—" not in text and "signed" not in text.lower()


def test_the_card_only_shows_when_this_pc_does_the_work(make_app):
    app = make_app()
    assert str(app._engine_card.winfo_parent()) == str(app.groups["src_local"])
    app.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service")
    assert not app.groups["src_local"].winfo_manager()
    app.fields["TRANSCRIBE_SOURCE"].var.set("This PC")
    assert app.groups["src_local"].winfo_manager()


# --- "Not what this PC has?" ------------------------------------------------------

def test_not_right_unfolds_the_likely_cause_and_the_fix(make_app):
    app = make_app()
    link = app._not_right
    assert link.cget("text") == "Not what this PC has?" and not link.opened
    assert sg.hardware.NOT_RIGHT_NOTE not in _main(app)
    link.toggle()
    assert link.opened
    shown = _main(app)
    note = sg.hardware.NOT_RIGHT_NOTE
    assert note in shown
    assert "driver" in note and "missing or out of date" in note
    assert "NVIDIA, AMD or Intel" in note and "restart" in note
    assert "open this page again" in note
    assert sg.NOT_RIGHT_ADVANCED in shown and "Advanced" in sg.NOT_RIGHT_ADVANCED
    link.toggle()
    assert sg.hardware.NOT_RIGHT_NOTE not in _main(app)


# --- Advanced: the views behind the main one ---------------------------------------

def test_advanced_views_replace_the_page_and_lead_back(make_app):
    app = make_app()
    assert app._tx_view() is None and app._tx_main.winfo_manager()
    for name in ("models", "adv_local"):
        app._show_tx_view(name)
        assert app._tx_view() == name and not app._tx_main.winfo_manager()
        assert sg.TX_BACK in _view(app, name)
        app._show_tx_main()
        assert app._tx_view() is None and app._tx_main.winfo_manager()
    # Coming back to the page, or changing where dictation runs, shows the main view.
    app._show_tx_view("adv_local")
    app.show_section("Words")
    app.show_section("Transcription")
    assert app._tx_view() is None
    app._show_tx_view("adv_local")
    app.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service")
    assert app._tx_view() is None and app._tx_main.winfo_manager()


def test_choosing_by_hand_is_behind_advanced(make_app):
    app = make_app(graphics=(RTX,))
    app._show_tx_view("adv_local")
    shown = _view(app, "adv_local")
    assert "Advanced" in shown and sg.ENGINE_HEADING in shown
    for _value, title, text in sg.ENGINE_CHOICES:
        assert title in shown and text in shown
    assert "It is new and has only been tried on a few PCs so far." in shown
    assert "Runs on" in shown and "Model folder" in shown and "Saved in" in shown
    assert "Vulkan" not in shown and "—" not in shown and "signed" not in shown.lower()
    # The usual engine's own setting shows; the second engine's does not.
    assert app._adv_fw.winfo_manager() and not app._adv_cpp.winfo_manager()
    assert str(app.fields["WHISPER_DEVICE"].widget).startswith(str(app._adv_fw))
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    assert app._adv_cpp.winfo_manager() and not app._adv_fw.winfo_manager()
    assert sg.ENGINE_AUTO_HELP in _view(app, "adv_local")
    assert app.dirty


def test_the_parts_of_the_download_are_only_listed_under_advanced(make_app):
    app = make_app()
    parts = ("The download is made of the speech engine (30 MB), the silence filter "
             "(1 MB) and the speech model (574 MB).")
    assert parts not in _main(app)
    app._show_tx_view("adv_local")
    assert parts in _view(app, "adv_local")


def test_a_choice_made_by_hand_ends_the_offer(make_app):
    app = make_app()
    assert app._engine_pending()
    app._engine_picked(sg.ENGINE_FW)            # a click on "The usual way"
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_FW)
    assert not app._engine_pending()
    assert app._pc_statement.cget("text") == (
        "We found an AMD Radeon RX 6600, but dictation is set to run on the processor.")
    assert _needs(app) is None and not app._pc_note.winfo_manager()
    assert app._fw_models.winfo_manager() and not app._cpp_models.winfo_manager()


# --- the model: named by its accuracy, chosen behind a link ------------------------

def test_the_accuracy_is_one_plain_line_with_a_link_to_the_models(make_app):
    app = make_app(graphics=(RTX,))
    assert app._accuracy.cget("text") == "Accuracy: best"
    assert app._model_link.cget("text") == "Advanced: choose a different model"
    assert "large-v3-turbo" not in _main(app)           # no model names by default
    app._show_tx_view("models")             # what the link does
    assert app._tx_view() == "models"
    assert "Choose a model" in _view(app, "models")


def test_default_model_follows_the_graphics_found(make_app):
    assert make_app(graphics=(RADEON,))._value("WHISPER_CPP_MODEL") == TURBO
    assert make_app(graphics=(UHD,))._value("WHISPER_CPP_MODEL") == BASE
    assert make_app(graphics=())._value("WHISPER_CPP_MODEL") == BASE
    # What is saved wins over the guess.
    assert make_app("WHISPER_CPP_MODEL=large-v3-turbo-q5_0\n",
                    graphics=(UHD,))._value("WHISPER_CPP_MODEL") == TURBO


def test_the_second_engines_models_are_listed_with_size_speed_accuracy_and_why(make_app):
    app = make_app(CPP)
    app._show_tx_view("models")
    assert app._cpp_models.winfo_manager() and not app._fw_models.winfo_manager()
    shown = _view(app, "models")
    assert len(ef.MODELS) == 10
    for spec in ef.MODELS.values():
        assert spec.title in shown
        assert "Download: %s  ·  Accuracy: %s" % (spec.size_text, spec.accuracy) in shown
        assert "Speed: " + spec.speed in shown and spec.note in shown
    assert "large-v3-turbo (smaller file)   • selected" in shown
    assert "—" not in shown and "Vulkan" not in shown


def test_speed_lines_only_state_what_was_measured():
    measured = {"base", "large-v3-turbo-q5_0"}
    for key, spec in ef.MODELS.items():
        assert spec.accuracy in ("basic", "good", "very good", "best")
        assert spec.note and spec.speed
        if key in measured:
            assert "measured" in spec.speed and "13 second clip" in spec.speed
        else:
            assert spec.speed.startswith("Not timed yet.")
            assert not any(ch.isdigit() for ch in spec.speed.replace("v3", ""))
    assert "1 to 2 seconds" in ef.MODELS["base"].speed and "0.13" in ef.MODELS["base"].speed
    assert "0.3 to 0.6 seconds" in ef.MODELS[TURBO].speed
    assert "36 seconds" in ef.MODELS[TURBO].speed
    for key in ("tiny.en", "base.en", "small.en", "medium.en"):
        assert "English only" in ef.MODELS[key].note


def test_the_usual_engines_models_are_listed_the_same_way(make_app):
    app = make_app(graphics=(RTX,))
    app._show_tx_view("models")
    assert app._fw_models.winfo_manager() and not app._cpp_models.winfo_manager()
    app._show_all_models(True)
    shown = _view(app, "models")
    for info in sg.model_catalog.CATALOG:
        assert info.name in shown and "Speed: " + info.speed in shown and info.why in shown
        assert "Accuracy: %s" % info.accuracy in shown
    assert "Download: 484 MB  ·  Accuracy: good" in shown
    assert "Download: 1.6 GB  ·  Accuracy: best" in shown
    app._show_all_models(False)
    shown = _view(app, "models")
    for name in sg.MODELS_FEATURED:
        assert name in shown
    assert "tiny.en" not in shown and "more)" in app._models_more.cget("text")


def test_using_another_model_changes_the_accuracy_and_the_download(make_app):
    app = make_app(CPP)
    app._cpp_rows["base"][2]._click()               # Use
    assert app._value("WHISPER_CPP_MODEL") == BASE and app.dirty
    assert app._accuracy.cget("text") == "Accuracy: basic"
    assert _needs(app) == "This PC needs a one-time download of about 180 MB."
    # The usual engine: a model that is not here yet is one download too.
    app = make_app(graphics=(RTX,))
    app._use_model(sg.model_catalog.BY_NAME["small.en"])
    assert app._accuracy.cget("text") == "Accuracy: good"
    assert _needs(app) == "This PC needs a one-time download of about 500 MB."


def test_download_on_a_models_row_fetches_that_model(make_app, tmp_path):
    app = make_app(CPP)
    app._cpp_rows["base"][1]._click()               # Download
    _wait(app, lambda: app._engine_job is None)
    assert app.downloads.calls == [(BASE, str(tmp_path), str(tmp_path / "models"))]
    assert app._value("WHISPER_CPP_MODEL") == BASE and app._engine_missing() == []
    name, dl, use, _title = app._cpp_rows["base"]
    assert "Downloaded" in dl.cget("text") and not dl._enabled
    assert "selected" in name.cget("text") and not use._enabled


# --- the lines about the second engine's last start (pure) -------------------------

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


def test_advanced_reads_what_the_engine_remembered(make_app, tmp_path):
    os.makedirs(tmp_path / "models")
    sg.whispercpp_engine.write_auto_cache(
        str(tmp_path / "models" / "whispercpp_auto.json"),
        last={"device": "cpu", "found": [UHD], "gpu_seconds": 50.7, "cpu_seconds": 36.3})
    app = make_app(CPP + "WHISPER_CPP_MODEL=large-v3-turbo-q5_0\n", graphics=(UHD,),
                   have=None)
    text = app._engine_device_label.cget("text")
    assert "Found on this PC: %s." % UHD in text
    assert "Automatic used the processor" in text and "36.3 s against 50.7 s" in text
    assert sg.ENGINE_SLOW_NOTE in text                  # the large model on the processor
    assert text not in _main(app)                       # Advanced only
    app.fields["WHISPER_CPP_MODEL"].var.set(BASE)
    assert sg.ENGINE_SLOW_NOTE not in app._engine_device_label.cget("text")


# --- one download, one number ------------------------------------------------------

def test_the_download_is_one_sentence_with_one_size(make_app):
    app = make_app(CPP)
    assert _needs(app) == "This PC needs a one-time download of about 600 MB."
    assert _needs(app).count("MB") == 1
    app.fields["WHISPER_CPP_MODEL"].var.set(BASE)
    assert _needs(app) == "This PC needs a one-time download of about 180 MB."
    assert ef.about_size_text(605007387) == "600 MB"
    assert ef.about_size_text(1620 * 10 ** 6) == "1.6 GB"
    assert ef.about_size_text(885098) == "1 MB"


def test_progress_is_one_bar_and_one_percentage_for_the_whole_lot(make_app, tmp_path):
    app = make_app()                            # the AMD card, nothing chosen yet
    app.dirty = False
    app.set_saved_state(True)                   # as if nothing were unsaved
    app.downloads.hold = threading.Event()
    app._card_download()
    _wait(app, lambda: app.downloads.started.is_set())
    _wait(app, lambda: app._dl_percent.cget("text") != "0%")
    # 36% of the first and smallest of three files is 1% of everything.
    assert app._dl_percent.cget("text") == "1%" and app._dl_bar.percent == 1
    assert app._dl_bar.winfo_manager() and app._dl_percent.winfo_manager()
    assert app._engine_button.cget("text") == "Downloading" and not app._engine_button._enabled
    assert app._engine_cancel.winfo_manager()
    shown = _main(app)
    for part in ("speech engine", "silence filter", "speech model"):
        assert part not in shown
    app._card_download()                        # a second press does nothing
    assert len(app.downloads.calls) == 1
    app.downloads.hold.set()
    _wait(app, lambda: app._engine_job is None)
    assert app.downloads.calls == [(TURBO, str(tmp_path), str(tmp_path / "models"))]
    assert app._engine_missing() == []
    # Everything is here: no sentence, no button, no bar.
    assert _needs(app) is None and not app._engine_button.winfo_manager()
    assert not app._dl_bar.winfo_manager() and not app._engine_cancel.winfo_manager()
    # The card that was found is used now, once this is saved.
    assert app._engine_on() and not app._engine_pending()
    assert app.dirty and app.btn_save._enabled
    assert app.status.cget("text") == sg.ENGINE_DOWNLOADED


def test_overall_progress_counts_every_file():
    todo = [(ef.ENGINE, ""), (ef.VAD, ""), (ef.MODELS[TURBO], "")]
    overall = ef.Overall(todo)
    assert overall.update(ef.ENGINE, 0) == 0
    assert overall.update(ef.ENGINE, ef.ENGINE.size) == 4
    assert overall.update(ef.MODELS[TURBO], ef.MODELS[TURBO].size // 2) == 52
    assert overall.update(ef.MODELS[TURBO], ef.MODELS[TURBO].size) == 99     # never 100 early
    assert ef.total_size(todo) == ef.ENGINE.size + ef.VAD.size + ef.MODELS[TURBO].size


def test_cancel_stops_the_download(make_app):
    app = make_app()
    app.downloads.hold = threading.Event()
    app._card_download()
    _wait(app, lambda: app.downloads.started.is_set())
    app._engine_cancel_download()
    _wait(app, lambda: app._engine_job is None)
    assert app._engine_status.cget("text") == "Download cancelled."
    assert app._engine_status.winfo_manager()
    assert app._engine_missing() and app._engine_button._enabled
    assert app._engine_button.cget("text") == "Download"
    assert not app._engine_on()                 # nothing changed


def test_a_failed_download_says_why(make_app):
    app = make_app()
    app.downloads.error = ("The downloaded copy of the speech engine is not the file that "
                           "was expected, so it was not used. Try again.")
    _download(app)
    assert app._engine_status.cget("text") == app.downloads.error
    assert str(app._engine_status.cget("fg")) == sg.DANGER
    assert app._engine_button._enabled and not app._engine_on()     # can try again


def test_the_usual_engines_model_is_the_same_one_download(make_app, monkeypatch, tmp_path):
    app = make_app(graphics=(RTX,), have=None)
    assert _needs(app) == "This PC needs a one-time download of about 1.6 GB."
    fetched = []

    def fake(info, root):
        fetched.append((info.name, root))
        _put_usual_model(root, info.name)
        return "downloaded"
    monkeypatch.setattr(sg.model_catalog, "download", fake)
    app._card_download()
    _wait(app, lambda: app._downloading is None)
    assert fetched == [("large-v3-turbo", str(tmp_path / "models"))]
    assert _needs(app) is None and not app._engine_button.winfo_manager()
    assert not app._dl_bar.winfo_manager()


def test_files_that_are_already_here_only_need_switching_on(make_app, tmp_path):
    _put_files(tmp_path)
    app = make_app()
    assert app._engine_pending()
    assert _needs(app) == sg.PENDING_READY
    assert app._engine_button.cget("text") == "Use it"
    app._card_download()
    assert app.downloads.calls == [] and app._engine_on() and app.dirty
    assert app.status.cget("text") == sg.ENGINE_USE_IT
    assert _needs(app) is None


# --- save -------------------------------------------------------------------------

def test_an_offer_never_gets_in_the_way_of_saving(make_app):
    """The AMD card was found and its download is on offer: other settings
    still save, and nothing about the second engine is written."""
    app = make_app()
    app.fields["REMOVE_FILLERS"].var.set(True)
    app.mark_dirty()
    assert app.save() is True
    for key in ("WHISPER_ENGINE", "WHISPER_CPP_MODEL", "WHISPER_CPP_DEVICE"):
        assert _env_lines(key) == []


def test_save_is_refused_while_a_chosen_engines_files_are_missing(make_app):
    app = make_app()
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_CPP)
    assert app.save() is False
    assert app.status.cget("text") == (
        "This PC still needs a one-time download of about 600 MB. Press Download on "
        "the Transcription page first.")
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
    app = make_app(CPP)
    app.fields["WHISPER_CPP_MODEL"].var.set(BASE)
    app.fields["WHISPER_CPP_DEVICE"].var.set("Processor only")
    assert app._pc_statement.cget("text") == (
        "We found an AMD Radeon RX 6600, but dictation is set to run on the processor.")
    assert app.save() is True
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=base"]
    assert _env_lines("WHISPER_CPP_DEVICE") == ["WHISPER_CPP_DEVICE=cpu"]


def test_saving_with_nvidia_adds_nothing_about_the_other_engine(make_app):
    app = make_app(graphics=(RTX,))
    app.fields["REMOVE_FILLERS"].var.set(True)
    app.mark_dirty()
    assert app.save() is True
    for key in ("WHISPER_ENGINE", "WHISPER_CPP_MODEL", "WHISPER_CPP_DEVICE"):
        assert _env_lines(key) == []


def test_switching_back_is_saved_without_any_download(make_app, tmp_path):
    app = make_app(CPP + "WHISPER_CPP_MODEL=base\nWHISPER_CPP_DEVICE=auto\n")
    assert app._engine_on() and app._adv_cpp.winfo_manager()
    assert app._engine_missing()                                    # nothing is downloaded
    app.fields["WHISPER_ENGINE"].var.set(sg.ENGINE_FW)
    assert not app._engine_pending()        # it was chosen once: no offer on top
    assert app.save() is True
    assert _env_lines("WHISPER_ENGINE") == ["WHISPER_ENGINE=faster-whisper"]
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=base"]   # kept for later


def test_settings_made_by_hand_are_kept(make_app, tmp_path):
    own = tmp_path / "mine" / "custom-q8.bin"
    os.makedirs(own.parent)
    own.write_bytes(b"x")
    _put_files(tmp_path)
    app = make_app(CPP + "WHISPER_CPP_MODEL=%s\nWHISPER_CPP_DEVICE=1\n" % own)
    assert app._accuracy.cget("text") == sg.ACCURACY_OWN
    app._show_tx_view("models")
    assert "Your own file: %s   • selected" % own in _view(app, "models")
    assert app.fields["WHISPER_CPP_DEVICE"].var.get() == "Graphics device 1"
    assert app._engine_missing() == []                  # the engine and filter are there
    app.mark_dirty()
    assert app.save() is True
    assert _env_lines("WHISPER_CPP_MODEL") == ["WHISPER_CPP_MODEL=%s" % own]
    assert _env_lines("WHISPER_CPP_DEVICE") == ["WHISPER_CPP_DEVICE=1"]


def test_a_missing_file_of_your_own_is_refused_in_one_sentence(make_app, tmp_path):
    _put_files(tmp_path)
    gone = tmp_path / "mine" / "gone.bin"
    app = make_app(CPP + "WHISPER_CPP_MODEL=%s\n" % gone)
    app.mark_dirty()
    assert app.save() is False
    assert "The whisper.cpp model is missing" in app.status.cget("text")
    assert str(gone) in app.status.cget("text")


def test_no_warning_about_the_other_engines_model(make_app, tmp_path):
    """With whisper.cpp chosen, the faster-whisper model not being downloaded
    is no reason for a warning after saving."""
    _put_files(tmp_path)
    app = make_app(CPP + "WHISPER_CPP_MODEL=large-v3-turbo-q5_0\n", have=None)
    app.mark_dirty()
    assert app.save() is True
    assert app.status.cget("text") == "Saved. Restart the API to apply."


# --- a cloud service: why, price, privacy, link -------------------------------------

def test_cloud_services_match_the_phone_and_old_choices_still_work():
    tc = sg.transcribe_client
    assert list(tc.CLOUD_PRESETS) == ["groq", "together", "openai", "openrouter", "custom"]
    # The keys people already have saved, with the same address and model.
    assert tc.CLOUD_PRESETS["groq"] == ("Groq", "https://api.groq.com/openai/v1",
                                        "whisper-large-v3-turbo")
    assert tc.CLOUD_PRESETS["openai"] == ("OpenAI", "https://api.openai.com/v1", "whisper-1")
    assert tc.CLOUD_PRESETS["custom"][1:] == ("", "")
    assert tc.CLOUD_PRESETS["together"][1:] == ("https://api.together.xyz/v1",
                                                "whisper-large-v3")
    assert tc.CLOUD_PRESETS["openrouter"][1:] == ("https://openrouter.ai/api/v1",
                                                  "openai/whisper-large-v3")
    assert set(tc.CLOUD_INFO) == set(tc.CLOUD_PRESETS)


def test_cloud_prices_carry_one_date_that_is_easy_to_update(monkeypatch):
    tc = sg.transcribe_client
    assert tc.CLOUD_PRICES_AS_OF == "November 2026"
    # The figures are the phone app's (its ProviderPreset list).
    for key, figures in (("groq", ("$0.04 per hour",)),
                         ("together", ("$0.0015 per minute", "$0.09 per hour")),
                         ("openai", ("$0.006 per minute", "$0.36 per hour"))):
        price = tc.cloud_info(key)["price"]
        assert "(as of November 2026)" in price
        for figure in figures:
            assert figure in price
        assert tc.cloud_info(key)["url"].startswith("https://")
    for key in tc.CLOUD_INFO:
        info = tc.cloud_info(key)
        assert info["why"] and info["price"] and info["privacy"]
        assert "November" not in tc.CLOUD_INFO[key]["price"]        # only the constant
        assert "—" not in " ".join(info.values()) and "{" not in info["price"]
    assert tc.cloud_info("something else") == tc.cloud_info("custom")
    monkeypatch.setattr(tc, "CLOUD_PRICES_AS_OF", "March 2027")
    assert "(as of March 2027)" in tc.cloud_info("groq")["price"]


def test_the_cloud_view_shows_the_chosen_services_reasons(make_app):
    tc = sg.transcribe_client
    app = make_app("TRANSCRIBE_SOURCE=cloud\n", graphics=(RTX,))
    assert app.fields["CLOUD_PROVIDER"].widget.cget("values") == (
        "Groq", "Together AI", "OpenAI", "OpenRouter", "Another service")
    for label, key in (("Groq", "groq"), ("Together AI", "together"), ("OpenAI", "openai"),
                       ("OpenRouter", "openrouter")):
        app.fields["CLOUD_PROVIDER"].var.set(label)
        info, shown = tc.cloud_info(key), _main(app)
        assert info["why"] in shown
        assert "Price: " + info["price"] in shown and "Privacy: " + info["privacy"] in shown
        assert app._cloud_link.winfo_manager() and sg.CLOUD_PRICE_LINK in shown
    app.fields["CLOUD_PROVIDER"].var.set("Another service")
    assert not app._cloud_link.winfo_manager()              # no price page to open
    assert sg.CLOUD_CUSTOM_NOTE in _main(app)


def test_the_price_link_opens_that_services_pricing_page(make_app, monkeypatch):
    app = make_app("TRANSCRIBE_SOURCE=cloud\n", graphics=(RTX,))
    opened = []
    monkeypatch.setattr(sg.webbrowser, "open", opened.append)
    app.fields["CLOUD_PROVIDER"].var.set("Together AI")
    app._open_cloud_prices()
    assert opened == ["https://www.together.ai/pricing"]
    assert app._value("CLOUD_BASE_URL") == "https://api.together.xyz/v1"    # preset filled
    assert app._value("CLOUD_MODEL") == "whisper-large-v3"


def test_a_saved_cloud_choice_is_still_selected(make_app):
    app = make_app("TRANSCRIBE_SOURCE=cloud\nCLOUD_PROVIDER=openai\n"
                   "CLOUD_BASE_URL=https://api.openai.com/v1\nCLOUD_MODEL=whisper-1\n",
                   graphics=(RTX,))
    assert app.fields["CLOUD_PROVIDER"].var.get() == "OpenAI"
    assert sg.transcribe_client.cloud_info("openai")["why"] in _main(app)
    app.mark_dirty()
    assert app.save() is True and _env_lines("CLOUD_PROVIDER")[-1] == "CLOUD_PROVIDER=openai"


def test_addresses_and_model_names_are_behind_advanced(make_app):
    app = make_app("TRANSCRIBE_SOURCE=cloud\n", graphics=(RTX,))
    shown = _main(app)
    for label in ("Service address", "https://", "whisper-large-v3-turbo"):
        assert label not in shown
    for key in ("CLOUD_BASE_URL", "CLOUD_MODEL"):
        assert str(app.fields[key].widget).startswith(str(app._tx_views["adv_cloud"]))
    assert str(app.fields["CLOUD_API_KEY"].widget).startswith(str(app.groups["src_cloud"]))
    app._show_tx_view("adv_cloud")
    assert "Service address" in _view(app, "adv_cloud")
    # Another PC: the connection code stays, the address moves.
    app.fields["TRANSCRIBE_SOURCE"].var.set("Another PC of mine")
    shown = _main(app)
    assert "Connection code" in shown and sg.REMOTE_NOT_READY in shown
    for label in ("Server address", "Certificate fingerprint", "Server API key"):
        assert label not in shown
    for key in ("REMOTE_SERVER_URL", "REMOTE_API_KEY", "REMOTE_CERT_SHA256"):
        assert str(app.fields[key].widget).startswith(str(app._tx_views["adv_remote"]))
    app.fields["REMOTE_SERVER_URL"].var.set("https://100.1.2.3:5001/v1")
    assert sg.REMOTE_READY in _main(app) and "100.1.2.3" not in _main(app)


def test_the_fallback_switch_is_folded_and_not_offered_with_a_cloud_service(make_app):
    app = make_app(graphics=(RTX,))
    f = app.fields["TRANSCRIBE_FALLBACK_CLOUD"]
    assert not f.fold.opened and f.label not in _main(app)
    assert app._tx_fold.winfo_manager()
    app.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service")
    assert not app._tx_fold.winfo_manager()
    app.fields["TRANSCRIBE_SOURCE"].var.set("This PC")
    assert app._tx_fold.winfo_manager()
    f.var.set(True)
    app.mark_dirty()
    assert app.save() is True and _env_lines("TRANSCRIBE_FALLBACK_CLOUD")[-1].endswith("=true")


# --- fit ----------------------------------------------------------------------------

@pytest.mark.parametrize("extra, graphics, have", [
    ("", (RTX, UHD), "large-v3-turbo"),                 # NVIDIA found
    ("", (RADEON,), "large-v3-turbo"),                  # AMD found, a download needed
    (CPP, (RADEON,), None),                             # the same, chosen and saved
    ("", (RTX,), None),                                 # NVIDIA, its model not here yet
    ("WHISPER_MODEL_SIZE=small.en\n", (), "small.en"),  # no graphics card
    ("TRANSCRIBE_SOURCE=cloud\n", (RTX,), "large-v3-turbo"),
    ("TRANSCRIBE_SOURCE=remote\n", (RTX,), "large-v3-turbo"),
])
def test_the_main_view_fits_the_default_window(make_app, extra, graphics, have):
    app = make_app(extra, graphics=graphics, have=have)
    app.show_section("Transcription")
    app.root.update_idletasks()
    assert app.frames["Transcription"].winfo_reqheight() <= PAGE_ROOM
    if app._engine_button.winfo_manager():
        # The Download button is above the fold too.
        bottom = (app._engine_button.winfo_rooty() + app._engine_button.winfo_height()
                  - app.frames["Transcription"].winfo_rooty())
        assert bottom <= PAGE_ROOM
