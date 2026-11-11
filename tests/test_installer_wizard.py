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
                          "Hotkey", "Phone", "Startup"]


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
