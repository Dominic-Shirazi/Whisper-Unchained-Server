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
    assert marks["model"][1] == "not needed here" and marks["service"][1] == "not needed here"
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


def test_other_graphics_are_offered_the_second_engine(wiz_radeon):
    app, sg = wiz_radeon
    assert app.offer is not None and app.engine_offer() is app.offer
    assert app.rec_title.cget("text") == "Recommended: large-v3-turbo on your AMD Radeon RX 6600"
    text = app.rec_text.cget("text")
    assert "AMD Radeon RX 6600" in text and "only been tried on a few PCs" in text
    assert text.count(". ") + 1 == 2 and "—" not in text          # two sentences
    app.next()                      # Welcome -> Your PC
    app.next()                      # Your PC -> Install (applies it)
    assert app._value("WHISPER_ENGINE") == "whisper.cpp"
    assert app._value("WHISPER_CPP_MODEL") == "large-v3-turbo-q5_0"
    assert app._value("WHISPER_CPP_DEVICE") == "auto"
    assert app.fields["WHISPER_CPP_DEVICE"].var.get() == "Automatic (recommended)"
    # The Install page lists the three downloads.
    for key in ("engine", "model", "vad"):
        assert app.task_frames[key].winfo_manager()


def test_dry_run_saves_the_engine_choice_and_downloads_nothing(wiz_radeon):
    app, sg = wiz_radeon
    _run_install(app)
    saved = sg.read_env()
    assert saved["WHISPER_ENGINE"] == "whisper.cpp"
    assert saved["WHISPER_CPP_MODEL"] == "large-v3-turbo-q5_0"
    assert saved["WHISPER_CPP_DEVICE"] == "auto"
    marks = _marks(app)
    assert marks["save"][0] == "✓"
    for key in ("engine", "model", "vad"):
        assert marks[key] == ("–", "test run")
    assert sg.engine_files.missing("large-v3-turbo-q5_0", sg.ENGINE_ROOT,
                                   saved["WHISPER_MODELS_DIR"])       # still nothing there
    assert not os.path.exists(os.path.join(sg.ENGINE_ROOT, "api"))


def test_built_in_graphics_keep_the_usual_engine(wiz_built_in):
    app, sg = wiz_built_in
    assert app.engine_offer() is None
    assert app.rec_title.cget("text") == "Recommended: small.en on your processor"
    _run_install(app)
    saved = sg.read_env()
    assert "WHISPER_ENGINE" not in saved
    assert saved["WHISPER_MODEL_SIZE"] == "small.en" and saved["WHISPER_DEVICE"] == "cpu"


def test_the_offer_can_be_switched_off(wiz_radeon):
    app, sg = wiz_radeon
    app.use_offer.set(False)
    assert app.engine_offer() is None
    assert app.rec_title.cget("text") == "Recommended: small.en on your processor"
    _run_install(app)
    saved = sg.read_env()
    assert "WHISPER_ENGINE" not in saved            # the usual engine: nothing to write
    assert saved["WHISPER_MODEL_SIZE"] == "small.en" and saved["WHISPER_DEVICE"] == "cpu"
    for key in ("engine", "vad"):
        assert not app.task_frames[key].winfo_manager()


def test_with_an_nvidia_card_setup_does_not_offer_it(tmp_path, monkeypatch, tk_root):
    import hardware
    app, win, sg = _make_wizard(tmp_path, monkeypatch, tk_root,
                                [hardware.Gpu("Test RTX", 8192, 7000, 7.5)],
                                ["Intel(R) UHD Graphics 630"])
    try:
        assert app.offer is None and app.engine_offer() is None
        assert app.rec_title.cget("text") == "Recommended: large-v3-turbo on your graphics card"
        assert not app._engine_card.winfo_manager()      # Settings only
        _run_install(app)
        assert "WHISPER_ENGINE" not in sg.read_env()
        for key in ("engine", "vad"):
            assert not app.task_frames[key].winfo_manager()
    finally:
        win.destroy()


def test_engine_files_download_as_three_progress_rows(wiz_radeon, monkeypatch):
    """A real install (not a test run) fetches the three files, each with its
    own row and percentage. The downloader is a stand-in."""
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
    assert [k for k, _d in fetched] == ["engine", "model", "vad"]
    assert fetched[0][1] == os.path.join(sg.ENGINE_ROOT, "api", "bin", "whispercpp")
    assert fetched[1][1] == os.path.join(sg.ENGINE_ROOT, "models", "ggml-base.bin")
    assert ("model", "running", "49% of 148 MB") in events
    assert ("model", "running", "99% of 148 MB") in events
    assert [e for e in events if e[1] == "ok"] == [
        ("engine", "ok", "30 MB"), ("model", "ok", "148 MB"), ("vad", "ok", "1 MB")]
    # Back turned into "Cancel download" while each file came down, and back again.
    flags = []
    while not app.events.empty():
        flags.append(app.events.get_nowait())
    assert flags == [("cancellable", True), ("cancellable", False)] * 3


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
    assert events == [(k, "ok", "already downloaded") for k in ("engine", "model", "vad")]


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
    assert events[-1] == ("engine", "fail", "cancelled")

    def bad_fetch(spec, dest, opener=None, progress=None, cancelled=None):
        raise sg.engine_files.EngineFileError(
            "The downloaded copy of the speech engine is not the file that was expected, "
            "so it was not used. Try again.")

    monkeypatch.setattr(sg.engine_files, "fetch", bad_fetch)
    events = []
    with pytest.raises(sg.engine_files.EngineFileError, match="was not used"):
        app._download_engine_files(lambda *a: events.append(a))
    assert events[-1] == ("engine", "fail", "did not download")
