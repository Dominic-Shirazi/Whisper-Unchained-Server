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
    assert seen[2:-1] == ["Transcription", "Words & Triggers", "AI Editing (LLM)",
                          "Hotkey", "Connections", "Startup"]


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
    assert app.steps() == ["Welcome", "Your PC", "Transcription", "Words & Triggers",
                           "Hotkey", "Startup", "Install", "Done"]


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
