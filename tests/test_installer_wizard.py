"""Setup wizard: navigation, recommendation, and a dry-run install against a
temp .env (the real .env is never written)."""
import os
import shutil
import sys
import time
import tkinter as tk

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
sys.path.insert(0, os.path.join(ROOT, "client"))

# The setup wizard installs the Windows service (Linux: scripts/install_linux.sh).
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only installer")


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def wiz(tmp_path, monkeypatch, tk_root):
    import settings_gui as sg
    import wizard
    import hardware
    env = tmp_path / ".env"
    shutil.copy(os.path.join(ROOT, ".env.example"), env)
    monkeypatch.setattr(sg, "ENV_PATH", str(env))
    # Word lists go to a temp copy too: no test may write the real file.
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    # A known PC: one 8 GB RTX card.
    monkeypatch.setattr(hardware, "detect_gpus",
                        lambda: [hardware.Gpu("Test RTX", 8192, 7000, 7.5)])
    # Tailscale present unless a test says otherwise (the PC running tests may lack it).
    monkeypatch.setattr(wizard.steps, "tailscale_installed", lambda: True)
    win = tk.Toplevel(tk_root)
    win.withdraw()
    app = wizard.WizardApp(win, dry_run=True)
    yield app, env, sg
    win.destroy()


def test_quick_path(wiz):
    app, _, _ = wiz
    seen = [app.current]
    while app.current != "Install":
        app.next()
        seen.append(app.current)
    assert seen == ["Welcome", "Your PC", "Install"]


def test_custom_path_shows_settings_pages(wiz):
    app, _, _ = wiz
    app.mode.set("custom")
    seen = [app.current]
    while app.current != "Install":
        app.next()
        seen.append(app.current)
    assert seen[2:-1] == ["Transcription", "Words", "AI Editing (LLM)",
                          "Hotkey", "Devices & Apps"]


def test_recommendation_applied(wiz):
    app, _, _ = wiz
    app.next()                      # Welcome -> Your PC
    app.next()                      # Your PC -> Install (applies it)
    assert app._value("WHISPER_MODEL_SIZE") == "large-v3-turbo"
    assert app.extra_env == {"WHISPER_COMPUTE_TYPE": "float16"}


def test_language_choice_changes_small_gpu_pick(wiz, monkeypatch):
    app, _, _ = wiz
    import hardware
    app.gpus = [hardware.Gpu("Small", 3000, 2500, 7.5)]
    app.english.set(False)
    app._refresh_rec()
    assert app.rec.model == "small"


def test_dry_run_install_writes_settings_only(wiz):
    app, env, sg = wiz
    app.next(); app.next()          # to Install
    app.next()                      # Install
    deadline = time.time() + 120
    while app.installing and time.time() < deadline:
        app.root.update()
        time.sleep(0.05)
    assert app.installed and not app.installing
    saved = sg.read_env()
    assert saved["WHISPER_MODEL_SIZE"] == "large-v3-turbo"
    assert saved["WHISPER_COMPUTE_TYPE"] == "float16"
    assert saved["API_VISIBLE"] == "false"
    marks = {k: m.cget("text") for k, (m, _d) in app.task_rows.items()}
    assert marks["save"] == "✓" and marks["service"] == "–"


def test_game_mode_offered_on_gpu_pcs_and_saved(wiz):
    app, env, sg = wiz
    app.fields["GAME_MODE"].var.set(True)          # the Install page's switch
    app.next(); app.next(); app.next()             # Welcome -> PC -> Install -> run
    deadline = time.time() + 120
    while app.installing and time.time() < deadline:
        app.root.update()
        time.sleep(0.05)
    assert sg.read_env()["GAME_MODE"] == "true"


def _run_install(app):
    deadline = time.time() + 120
    while app.installing and time.time() < deadline:
        app.root.update()
        time.sleep(0.05)


def test_remote_quick_path_includes_connection_page(wiz):
    app, _, _ = wiz
    app.src.set("remote")
    seen = [app.current]
    while app.current != "Install":
        app.next()
        seen.append(app.current)
    assert seen == ["Welcome", "Your PC", "Transcription", "Install"]
    assert not app.local_box.winfo_manager()           # no model recommendation
    assert not app.game_row.winfo_manager()            # no game toggle
    assert "Nothing heavy" in app.install_intro.cget("text")


def test_cloud_walkthrough_skips_server_pages(wiz):
    app, _, _ = wiz
    app.mode.set("custom")
    app.src.set("cloud")
    assert app.steps() == ["Welcome", "Your PC", "Transcription", "Words", "Triggers",
                           "Hotkey", "Install", "Done"]


def test_setup_has_a_words_step_and_leaves_triggers_to_settings(wiz):
    """Setup saves vocabulary and corrections only, so it doesn't offer the
    Triggers page (edits there would be lost); Settings has it afterwards."""
    app, _, sg = wiz
    import wizard
    assert "Words & Triggers" not in wizard.CUSTOM and "Words & Triggers" not in sg.LOCAL_ONLY_SECTIONS
    assert "Words" in wizard.CUSTOM and "Triggers" not in wizard.CUSTOM
    app.mode.set("custom")
    assert app.steps() == ["Welcome", "Your PC", "Transcription", "Words",
                           "AI Editing (LLM)", "Hotkey", "Devices & Apps", "Install", "Done"]
    app.src.set("remote")                    # the words live on the other PC
    assert app.steps() == ["Welcome", "Your PC", "Transcription", "Hotkey", "Install", "Done"]
    for name in ("Words", "Triggers"):       # both pages exist to be shown
        assert name in app.frames


def test_paste_connection_code_in_the_wizard(wiz, monkeypatch):
    app, env, sg = wiz
    import pairing
    import transcribe_client
    code = pairing.connection_code(pairing.pairing_payload(
        "100.1.2.3", "wr_sec_abcdefghijklmnop", "large-v3-turbo", "Den-PC", port=5001,
        cert_sha256="ab" * 32, alt_hosts=["192.168.0.2"]))
    monkeypatch.setattr(transcribe_client, "check_connection",
                        lambda env, **k: (env["REMOTE_SERVER_URL"].startswith("https://100."),
                                          "Connected."))
    app.src.set("remote")
    app.next(); app.next()                              # -> Your PC -> Transcription
    assert app.current == "Transcription"
    app.root.update_idletasks()
    button = app._action_buttons["_paste_code"]
    assert button.cget("text") == "Paste connection code"
    assert app.groups["src_remote"].winfo_manager()     # the button's group is on the page
    monkeypatch.setattr(app, "_clipboard_text", lambda: code)
    button.command()
    deadline = time.time() + 20
    while getattr(app, "_paste_busy", False) and time.time() < deadline:
        app.root.update()
        time.sleep(0.02)
    assert app.status.cget("text") == "Connected to Den-PC. Press Next."   # no Save button here
    assert app._value("REMOTE_SERVER_URL") == "https://100.1.2.3:5001/v1"
    assert "REMOTE_ALT_URLS" not in sg.read_env()       # nothing written before Install
    app.next(); app.next()                              # -> Install -> run
    _run_install(app)
    saved = sg.read_env()
    assert saved["TRANSCRIBE_SOURCE"] == "remote"
    assert saved["REMOTE_SERVER_URL"] == "https://100.1.2.3:5001/v1"
    assert saved["REMOTE_ALT_URLS"] == "https://192.168.0.2:5001/v1"
    assert saved["REMOTE_API_KEY"] == "wr_sec_abcdefghijklmnop"
    assert saved["REMOTE_CERT_SHA256"] == "ab" * 32


def test_remote_install_tests_connection_instead_of_service(wiz, monkeypatch):
    app, env, sg = wiz
    import transcribe_client
    monkeypatch.setattr(transcribe_client, "check_connection",
                        lambda env: (True, "Connected."))
    app.src.set("remote")
    app.next(); app.next()                              # -> Your PC -> Transcription
    app.fields["REMOTE_SERVER_URL"].var.set("http://100.1.2.3:5000/v1")
    app.next(); app.next()                              # -> Install -> run
    _run_install(app)
    saved = sg.read_env()
    assert saved["TRANSCRIBE_SOURCE"] == "remote"
    assert saved["REMOTE_SERVER_URL"] == "http://100.1.2.3:5000/v1"
    marks = {k: (m.cget("text"), d.cget("text")) for k, (m, d) in app.task_rows.items()}
    assert marks["download"][1] == "not needed here"
    assert marks["service"][1] == "not needed here"
    assert marks["check"] == ("✓", "Connected.")
    assert "WHISPER_COMPUTE_TYPE" not in saved or saved.get("WHISPER_COMPUTE_TYPE") != "float16"


def test_failed_connection_is_reported(wiz, monkeypatch):
    app, _, _ = wiz
    import transcribe_client
    monkeypatch.setattr(transcribe_client, "check_connection",
                        lambda env: (False, "Connected, but the API key was rejected."))
    app.src.set("cloud")
    app.next(); app.next(); app.next(); app.next()
    _run_install(app)
    assert "key was rejected" in app.done_text.cget("text")


def test_changing_source_keeps_current_step_highlighted(wiz):
    app, _, _ = wiz
    app.next()                      # Your PC
    app.src.set("cloud")
    _row, bar, _lbl = app.nav["Your PC"]
    assert str(bar.cget("bg")) == sg_accent()


def sg_accent():
    import settings_gui
    return settings_gui.ACCENT


# --- "Other devices" (Tailscale) step ---------------------------------------

@pytest.fixture
def wiz_no_tailscale(tmp_path, monkeypatch, tk_root):
    import settings_gui as sg
    import wizard
    import hardware
    env = tmp_path / ".env"
    shutil.copy(os.path.join(ROOT, ".env.example"), env)
    monkeypatch.setattr(sg, "ENV_PATH", str(env))
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    monkeypatch.setattr(hardware, "detect_gpus",
                        lambda: [hardware.Gpu("Test RTX", 8192, 7000, 7.5)])
    monkeypatch.setattr(wizard.steps, "tailscale_installed", lambda: False)
    launched = []
    monkeypatch.setattr(wizard.steps, "install_tailscale",
                        lambda: launched.append(1) or "winget")
    win = tk.Toplevel(tk_root)
    win.withdraw()
    app = wizard.WizardApp(win, dry_run=False)
    yield app, launched
    win.destroy()


def test_away_step_appears_only_for_local_without_tailscale(wiz_no_tailscale):
    app, _ = wiz_no_tailscale
    assert app.steps() == ["Welcome", "Your PC", "Other devices", "Install", "Done"]
    app.mode.set("custom")
    assert app.steps()[-3:] == ["Other devices", "Install", "Done"]
    app.src.set("cloud")
    assert "Other devices" not in app.steps()


def test_choosing_tailscale_starts_its_installer_once(wiz_no_tailscale):
    app, launched = wiz_no_tailscale
    app.go("Other devices")
    app.next()
    assert app.current == "Install" and launched == [1]
    assert "Tailscale is installing" in app.status.cget("text")
    app.back()
    app.next()
    assert launched == [1]   # going back and forward doesn't start it again


@pytest.mark.parametrize("choice", ["covered", "home"])
def test_bypass_choices_install_nothing(wiz_no_tailscale, choice):
    app, launched = wiz_no_tailscale
    app.go("Other devices")
    app.away.set(choice)
    app.next()
    assert app.current == "Install" and launched == []


def test_install_tailscale_uses_winget_or_opens_the_page():
    import steps
    calls = []
    assert steps.install_tailscale(popen=lambda *a, **k: calls.append(a[0]),
                                   which=lambda n: r"C:\winget.exe") == "winget"
    assert calls[0][:4] == [r"C:\winget.exe", "install", "--id", "Tailscale.Tailscale"]
    opened = []
    assert steps.install_tailscale(open_page=opened.append, which=lambda n: None) == "browser"
    assert opened == [steps.TAILSCALE_DOWNLOAD]



def test_fresh_install_gets_a_required_key(wiz, monkeypatch, tk_root):
    _old, env, sg = wiz
    import wizard
    monkeypatch.setattr(wizard, "FRESH", True)
    win = tk.Toplevel(tk_root)
    win.withdraw()
    app = wizard.WizardApp(win, dry_run=True)
    try:
        key = app.fields["WHISPER_API_KEY"].var.get()
        assert key.startswith("wr_sec_") and len(key) > 30
        assert app.fields["WHISPER_API_KEY_REQUIRED"].var.get() is True
        app.next(); app.next()          # to Install
        app.next()                      # Install
        deadline = time.time() + 120
        while app.installing and time.time() < deadline:
            app.root.update()
            time.sleep(0.05)
        saved = sg.read_env()
        assert saved["WHISPER_API_KEY"] == key and saved["WHISPER_API_KEY_REQUIRED"] == "true"
    finally:
        win.destroy()


def test_fresh_install_turns_cleanup_on_when_an_ai_server_is_found(wiz, monkeypatch, tk_root):
    _old, env, sg = wiz
    import wizard
    import llm_detect
    monkeypatch.setattr(wizard, "FRESH", True)
    found = llm_detect.Found("Ollama", "127.0.0.1", 11434, "http://127.0.0.1:11434/v1",
                             ["qwen2.5:7b-instruct-q4_K_M"])
    monkeypatch.setattr(llm_detect, "detect", lambda *a, **k: [found])
    win = tk.Toplevel(tk_root)
    win.withdraw()
    app = wizard.WizardApp(win, dry_run=True)
    try:
        deadline = time.time() + 10
        while not app.ai_note and time.time() < deadline:
            app.root.update()
            time.sleep(0.05)
        assert "light AI cleanup" in app.ai_note, app.ai_note
        assert app.fields["CLEANUP_ALWAYS"].var.get() is True
    finally:
        win.destroy()


def test_fresh_install_leaves_cleanup_off_without_an_ai_server(wiz, monkeypatch, tk_root):
    _old, env, sg = wiz
    import wizard
    monkeypatch.setattr(wizard, "FRESH", True)
    win = tk.Toplevel(tk_root)
    win.withdraw()
    app = wizard.WizardApp(win, dry_run=True)
    try:
        deadline = time.time() + 10
        while not app.ai_note and time.time() < deadline:
            app.root.update()
            time.sleep(0.05)
        assert app.fields["CLEANUP_ALWAYS"].var.get() is False
    finally:
        win.destroy()


# --- a PC that uses another PC's server needs Tailscale too ---------------------

def test_reach_step_appears_for_another_pc_without_tailscale(wiz_no_tailscale):
    app, launched = wiz_no_tailscale
    app.src.set("remote")
    assert app.steps() == ["Welcome", "Your PC", "Transcription", "Reaching it", "Install", "Done"]
    app.mode.set("custom")
    assert app.steps()[-3:] == ["Reaching it", "Install", "Done"]
    assert "Other devices" not in app.steps()
    app.go("Reaching it")
    app.next()
    assert app.current == "Install" and launched == [1]      # Tailscale's installer started
    app.src.set("cloud")
    assert "Reaching it" not in app.steps()


def test_no_reach_step_when_tailscale_is_already_here(wiz):
    app, _, _ = wiz
    app.src.set("remote")
    assert "Reaching it" not in app.steps()


# --- the microphone and speakers are checked -------------------------------------

class _Audio:
    """Stands in for sounddevice."""
    def __init__(self, mic=True, out=True, silent=False, broken=False):
        self.mic, self.out, self.silent, self.broken = mic, out, silent, broken

    def query_devices(self, kind=None):
        if not (self.mic if kind == "input" else self.out):
            raise RuntimeError("no default %s device" % kind)
        return {"name": "x"}

    def rec(self, frames, samplerate, channels, dtype):
        if self.broken:
            raise RuntimeError("can't open")
        import numpy as np
        return np.zeros((frames, 1), dtype="int16") if self.silent else np.ones((frames, 1), dtype="int16")

    def wait(self):
        pass


def test_check_audio_finds_what_is_switched_off():
    from installer import steps
    assert steps.check_audio(_Audio()) == (True, True, [])
    mic, out, found = steps.check_audio(_Audio(mic=False))
    assert (mic, out) == (False, True) and "No microphone is switched on" in found[0]
    mic, out, found = steps.check_audio(_Audio(out=False))
    assert (mic, out) == (True, False) and "speakers or headphones" in found[0]
    mic, out, found = steps.check_audio(_Audio(silent=True))
    assert not mic and "only silence" in found[0] and "Privacy" in found[0]
    mic, out, found = steps.check_audio(_Audio(broken=True))
    assert not mic and "couldn't be opened" in found[0]
    assert len(steps.check_audio(_Audio(mic=False, out=False))[2]) == 2
    opened = []
    steps.open_sound_settings(opened.append)
    assert opened == ["ms-settings:sound"]


# --- no NVIDIA card, but AMD or Intel graphics: the second speech engine ----------

def _make_wizard(tmp_path, monkeypatch, tk_root, nvidia, other):
    """A wizard on a temp .env whose models and engine folders are temp too,
    on a PC with the given NVIDIA cards and other graphics. Any real download
    fails the test."""
    import settings_gui as sg
    import wizard
    import hardware
    env = tmp_path / ".env"
    text = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    text = "".join(l for l in text.splitlines(True) if not l.startswith("WHISPER_MODELS_DIR"))
    env.write_text(text + "\nWHISPER_MODELS_DIR=%s\n" % (tmp_path / "models"), encoding="utf-8")
    monkeypatch.setattr(sg, "ENV_PATH", str(env))
    monkeypatch.setattr(sg, "ENGINE_ROOT", str(tmp_path))
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    monkeypatch.setattr(hardware, "detect_gpus", lambda: list(nvidia))
    monkeypatch.setattr(hardware, "detect_other_graphics", lambda *a, **k: list(other))
    monkeypatch.setattr(hardware, "windows_display_adapters",
                        lambda: [g.name for g in nvidia] + list(other))
    monkeypatch.setattr(wizard.steps, "tailscale_installed", lambda: True)

    def no_network(*a, **k):
        raise AssertionError("a test tried to download")
    monkeypatch.setattr(sg.engine_files, "open_url", no_network)
    # The usual engine's model is never fetched either: a stand-in says it was.
    monkeypatch.setattr(sg.model_catalog, "download", lambda info, root: "downloaded")
    win = tk.Toplevel(tk_root)
    win.withdraw()
    app = wizard.WizardApp(win, dry_run=True)
    return app, win, sg


@pytest.fixture
def wiz_radeon(tmp_path, monkeypatch, tk_root):
    app, win, sg = _make_wizard(tmp_path, monkeypatch, tk_root, [], ["AMD Radeon RX 6600"])
    yield app, sg
    win.destroy()


@pytest.fixture
def wiz_built_in(tmp_path, monkeypatch, tk_root):
    app, win, sg = _make_wizard(tmp_path, monkeypatch, tk_root, [],
                                ["Intel(R) UHD Graphics 630"])
    yield app, sg
    win.destroy()


def _run_install(app):
    while app.current != "Install":
        app.next()
    app.next()
    deadline = time.time() + 120
    while app.installing and time.time() < deadline:
        app.root.update()
        time.sleep(0.05)
    assert app.installed and not app.installing


def _marks(app):
    return {k: (m.cget("text"), d.cget("text")) for k, (m, d) in app.task_rows.items()}


def _page(app, name):
    """Every piece of text that is on a wizard page (packed)."""
    out = []

    def walk(widget):
        for c in widget.winfo_children():
            if not c.winfo_manager():
                continue
            if isinstance(c, tk.Label):
                out.append(str(c.cget("text")))
            walk(c)
    walk(app.frames[name])
    return "\n".join(out)


RTX = "NVIDIA GeForce RTX 3060"


@pytest.fixture
def wiz_nvidia(tmp_path, monkeypatch, tk_root):
    import hardware
    app, win, sg = _make_wizard(tmp_path, monkeypatch, tk_root,
                                [hardware.Gpu(RTX, 12288, 11000, 8.6)],
                                ["Intel(R) UHD Graphics 770"])
    yield app, sg
    win.destroy()


def test_your_pc_says_what_was_found_on_an_nvidia_pc(wiz_nvidia):
    app, sg = wiz_nvidia
    assert app.rec_title.cget("text") == (
        "We found an NVIDIA GeForce RTX 3060. Dictation will run on it.")
    assert app.rec_text.cget("text") == "Accuracy: best. Setup will download about 1.6 GB."
    assert not app.rec_note.winfo_manager()
    assert app.offer is None and app.engine_offer() is None
    _run_install(app)
    assert "WHISPER_ENGINE" not in sg.read_env()
    assert sg.read_env()["WHISPER_MODEL_SIZE"] == "large-v3-turbo"


def test_your_pc_says_what_was_found_on_an_amd_pc(wiz_radeon):
    app, sg = wiz_radeon
    assert app.offer is not None and app.engine_offer() is app.offer
    assert app.rec_title.cget("text") == (
        "We found an AMD Radeon RX 6600. Dictation will run on it.")
    # Honest: this is new.
    assert app.rec_note.cget("text") == sg.hardware.NEW_CARD_NOTE
    assert app.rec_note.winfo_manager() and "only been tried on a few PCs" in _page(app, "Your PC")
    # One number for everything setup will fetch.
    assert app.rec_text.cget("text") == "Accuracy: best. Setup will download about 600 MB."
    app.next()                      # Welcome -> Your PC
    app.next()                      # Your PC -> Install (applies it)
    assert app._value("WHISPER_ENGINE") == "whisper.cpp"
    assert app._value("WHISPER_CPP_MODEL") == "large-v3-turbo-q5_0"
    assert app._value("WHISPER_CPP_DEVICE") == "auto"
    assert app.fields["WHISPER_CPP_DEVICE"].var.get() == "Automatic (recommended)"


def test_your_pc_says_what_was_found_with_no_graphics_card(tmp_path, monkeypatch, tk_root):
    app, win, sg = _make_wizard(tmp_path, monkeypatch, tk_root, [], [])
    try:
        assert app.rec_title.cget("text") == (
            "No graphics card was found. Dictation will run on the processor, with a "
            "smaller model.")
        assert app.rec_text.cget("text").startswith(
            "Accuracy: good. Setup will download about 500 MB.")
        _run_install(app)
        saved = sg.read_env()
        assert "WHISPER_ENGINE" not in saved
        assert saved["WHISPER_MODEL_SIZE"] == "small.en" and saved["WHISPER_DEVICE"] == "cpu"
    finally:
        win.destroy()


def test_your_pc_has_no_jargon_and_never_shows_nvidia_without_one(wiz_radeon, wiz_nvidia):
    for app, _sg in (wiz_radeon, wiz_nvidia):
        app.next()                  # Welcome -> Your PC
        shown = _page(app, "Your PC")
        for word in ("engine", "Vulkan", "CUDA", "VAD", "VRAM", "whisper.cpp", "Whisper runs",
                     "faster-whisper", "API", "video memory", "Tailscale", "large-v3", "—"):
            assert word not in shown, word
        assert "signed" not in shown.lower()
    app, _sg = wiz_radeon
    assert "NVIDIA" not in _page(app, "Your PC")


def test_not_what_this_pc_has_unfolds_the_cause_and_the_fix(wiz_radeon):
    app, sg = wiz_radeon
    app.next()
    note = sg.hardware.NOT_RIGHT_NOTE
    assert app.not_right.cget("text") == "Not what this PC has?" and not app.not_right.opened
    assert note not in _page(app, "Your PC")
    app.not_right.toggle()
    shown = _page(app, "Your PC")
    assert note in shown and "Advanced" in shown.replace(note, "")
    assert "driver" in note and "NVIDIA, AMD or Intel" in note


def test_dry_run_saves_the_engine_choice_and_downloads_nothing(wiz_radeon):
    app, sg = wiz_radeon
    _run_install(app)
    saved = sg.read_env()
    assert saved["WHISPER_ENGINE"] == "whisper.cpp"
    assert saved["WHISPER_CPP_MODEL"] == "large-v3-turbo-q5_0"
    assert saved["WHISPER_CPP_DEVICE"] == "auto"
    marks = _marks(app)
    assert marks["save"] == ("✓", "")               # no file names in the list
    assert marks["download"] == ("–", "test run")
    assert sg.engine_files.missing("large-v3-turbo-q5_0", sg.ENGINE_ROOT,
                                   saved["WHISPER_MODELS_DIR"])       # still nothing there
    assert not os.path.exists(os.path.join(sg.ENGINE_ROOT, "api"))


def test_built_in_graphics_keep_the_usual_engine(wiz_built_in):
    app, sg = wiz_built_in
    assert app.engine_offer() is None
    assert app.rec_title.cget("text") == (
        "No separate graphics card was found, only Intel UHD Graphics 630, which is "
        "built into the processor. Dictation will run on the processor, with a "
        "smaller model.")
    assert app.rec_text.cget("text").startswith("Accuracy: good.")
    _run_install(app)
    saved = sg.read_env()
    assert "WHISPER_ENGINE" not in saved
    assert saved["WHISPER_MODEL_SIZE"] == "small.en" and saved["WHISPER_DEVICE"] == "cpu"


def test_the_card_can_be_switched_off_under_advanced(wiz_radeon):
    app, sg = wiz_radeon
    app.next()
    # Folded: the default view asks nothing.
    assert not app.pc_advanced.opened
    import wizard
    assert wizard.USE_CARD_TITLE not in _page(app, "Your PC")
    app.pc_advanced.toggle()
    shown = _page(app, "Your PC")
    assert wizard.USE_CARD_TITLE in shown and wizard.PC_ADVANCED_NOTE in shown
    app.use_offer.set(False)
    assert app.engine_offer() is None
    assert app.rec_title.cget("text") == (
        "We found an AMD Radeon RX 6600, but dictation is set to run on the processor.")
    assert app.rec_text.cget("text").startswith(
        "Accuracy: good. Setup will download about 500 MB.")
    assert not app.rec_note.winfo_manager()
    _run_install(app)
    saved = sg.read_env()
    assert "WHISPER_ENGINE" not in saved            # the usual engine: nothing to write
    assert saved["WHISPER_MODEL_SIZE"] == "small.en" and saved["WHISPER_DEVICE"] == "cpu"
    # The Transcription page does not offer the card again behind the person's back.
    assert not app._engine_pending()


def test_the_install_step_has_one_download_row(wiz_radeon, wiz_nvidia):
    for app, _sg in (wiz_radeon, wiz_nvidia):
        assert list(app.task_rows) == ["save", "download", "service", "login", "menu",
                                       "tray", "audio", "check"]
        shown = _page(app, "Install")
        assert "Download what dictation needs" in shown
        for part in ("speech engine", "speech model", "silence filter"):
            assert part not in shown
        assert not app.install_bar.winfo_manager()      # only while it downloads


def test_the_walk_through_page_says_what_setup_will_download(wiz_radeon):
    """The Transcription page inside setup has no Download button of its own:
    the Install step does it."""
    app, sg = wiz_radeon
    app.mode.set("custom")
    app.next(); app.next()                  # -> Your PC -> Transcription
    assert app.current == "Transcription"
    assert app._engine_card.winfo_manager()
    assert app._pc_statement.cget("text") == (
        "We found an AMD Radeon RX 6600. Dictation will run on it.")
    assert app._dl_line.cget("text") == "Setup will download about 600 MB."
    assert not app._engine_button.winfo_manager()


def test_everything_downloads_as_one_row_with_one_percentage(wiz_radeon, monkeypatch):
    """A real install (not a test run) fetches the three files behind one row
    and one percentage. The downloader is a stand-in."""
    app, sg = wiz_radeon
    app.dry_run = False
    app.values = {"WHISPER_ENGINE": "whisper.cpp", "WHISPER_CPP_MODEL": "base",
                  "WHISPER_MODELS_DIR": os.path.join(sg.ENGINE_ROOT, "models")}
    fetched = []

    def fake_fetch(spec, dest, opener=None, progress=None, cancelled=None):
        fetched.append((spec.key, dest))
        progress(spec, spec.size // 2, spec.size)
        progress(spec, spec.size, spec.size)

    monkeypatch.setattr(sg.engine_files, "fetch", fake_fetch)
    events = []
    app._download_engine_files(lambda *a: events.append(a))
    assert [k for k, _d in fetched] == ["engine", "vad", "model"]
    assert fetched[0][1] == os.path.join(sg.ENGINE_ROOT, "api", "bin", "whispercpp")
    assert fetched[2][1] == os.path.join(sg.ENGINE_ROOT, "models", "ggml-base.bin")
    assert {e[0] for e in events} == {"download"}           # one row, whatever the file
    assert events[0] == ("download", "running", "starting...")
    assert events[-1] == ("download", "ok")
    texts = [e[2] for e in events[1:-1]]
    # 30 MB + 1 MB + 148 MB: half of the first file is 8% of it all.
    assert texts[0] == "8% of about 180 MB" and texts[-1] == "99% of about 180 MB"
    assert all(t.endswith("of about 180 MB") for t in texts)
    percents = [int(t.split("%")[0]) for t in texts]
    assert percents == sorted(percents)                      # it only ever goes up
    # Back turned into "Cancel download" for the whole download, once; the bar
    # followed the same number and was taken away at the end.
    flags = []
    while not app.events.empty():
        flags.append(app.events.get_nowait())
    assert flags[0] == ("cancellable", True) and flags[-2:] == [
        ("cancellable", False), ("progress", None)]
    assert [f for f in flags if f[0] == "cancellable"] == [("cancellable", True),
                                                          ("cancellable", False)]
    assert [f[1] for f in flags[1:-2]] == percents
    app._install_progress(36)
    assert app.install_bar.winfo_manager() and app.install_bar.percent == 36
    app._install_progress(None)
    assert not app.install_bar.winfo_manager()


def test_the_usual_model_downloads_as_the_same_one_row(wiz_nvidia, monkeypatch):
    app, sg = wiz_nvidia
    import model_catalog
    models = os.path.join(sg.ENGINE_ROOT, "models")
    app.values = {"WHISPER_MODEL_SIZE": "small.en", "WHISPER_MODELS_DIR": models}
    fetched = []

    def fake_download(info, root):
        fetched.append((info.name, root))
        return "downloaded"
    monkeypatch.setattr(model_catalog, "download", fake_download)
    events = []
    app._download_usual_model(lambda *a: events.append(a))
    assert fetched == [("small.en", models)]
    assert events[0] == ("download", "running", "starting...")
    assert events[-1] == ("download", "ok") and {e[0] for e in events} == {"download"}

    def failing(info, root):
        raise OSError("no connection")
    monkeypatch.setattr(model_catalog, "download", failing)
    events = []
    with pytest.raises(RuntimeError, match="The download failed"):
        app._download_usual_model(lambda *a: events.append(a))
    assert events[-1] == ("download", "fail", "did not download")
    # A model of the person's own: nothing to fetch.
    app.values = {"WHISPER_MODEL_SIZE": "my-own-model", "WHISPER_MODELS_DIR": models}
    events = []
    app._download_usual_model(lambda *a: events.append(a))
    assert events == [("download", "ok", "nothing to download")]


def test_files_already_here_are_not_downloaded_again(wiz_radeon, monkeypatch):
    app, sg = wiz_radeon
    app.dry_run = False
    root = sg.ENGINE_ROOT
    os.makedirs(os.path.dirname(sg.engine_files.engine_binary(root)))
    open(sg.engine_files.engine_binary(root), "wb").close()
    os.makedirs(os.path.join(root, "models"))
    for name in ("ggml-base.bin", sg.engine_files.VAD.name):
        open(os.path.join(root, "models", name), "wb").close()
    app.values = {"WHISPER_ENGINE": "whisper.cpp", "WHISPER_CPP_MODEL": "base",
                  "WHISPER_MODELS_DIR": os.path.join(root, "models")}

    def no_fetch(*a, **k):
        raise AssertionError("nothing is missing")
    monkeypatch.setattr(sg.engine_files, "fetch", no_fetch)
    events = []
    app._download_engine_files(lambda *a: events.append(a))
    assert events == [("download", "ok", "already on this PC")]


def test_cancelling_or_a_bad_file_stops_the_install_with_a_plain_sentence(wiz_radeon,
                                                                          monkeypatch):
    app, sg = wiz_radeon
    app.dry_run = False
    app.values = {"WHISPER_ENGINE": "whisper.cpp", "WHISPER_CPP_MODEL": "base",
                  "WHISPER_MODELS_DIR": os.path.join(sg.ENGINE_ROOT, "models")}

    def cancelled_fetch(spec, dest, opener=None, progress=None, cancelled=None):
        assert cancelled() is True
        raise sg.engine_files.Cancelled("Download cancelled.")

    monkeypatch.setattr(sg.engine_files, "fetch", cancelled_fetch)
    app.installing = True
    app.back()                                   # the "Cancel download" button
    app.installing = False
    assert app.cancel_download.is_set()
    events = []
    with pytest.raises(RuntimeError, match="The download was cancelled"):
        app._download_engine_files(lambda *a: events.append(a))
    assert events[-1] == ("download", "fail", "cancelled")

    def bad_fetch(spec, dest, opener=None, progress=None, cancelled=None):
        raise sg.engine_files.EngineFileError(
            "The downloaded copy of the speech engine is not the file that was expected, "
            "so it was not used. Try again.")

    monkeypatch.setattr(sg.engine_files, "fetch", bad_fetch)
    events = []
    with pytest.raises(sg.engine_files.EngineFileError, match="was not used"):
        app._download_engine_files(lambda *a: events.append(a))
    assert events[-1] == ("download", "fail", "did not download")
