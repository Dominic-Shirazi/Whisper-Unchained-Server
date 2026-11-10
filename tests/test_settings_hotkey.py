"""Drive the real Settings window against a temp .env: the Hotkey section saves,
refuses hold-to-talk on the backtick, and an .env without HOTKEY lines still saves."""
import os
import shutil
import sys
import tkinter as tk

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import settings_gui as sg  # noqa: E402


@pytest.fixture(scope="module")
def tk_root():
    # ONE Tk interpreter for the module: creating and destroying several in a row
    # intermittently fails on Windows ("Can't find a usable init.tcl"). Each test
    # builds its window in a fresh Toplevel instead.
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tmp_path, monkeypatch, tk_root):
    env = tmp_path / ".env"
    # A temp COPY of the live .env (the example leaves required URLs commented
    # out, which the window rightly refuses to save). The real file is never written.
    src = os.path.join(ROOT, ".env")
    if not os.path.exists(src):
        pytest.skip("no .env on this machine")
    shutil.copy(src, env)
    # Mimic an older .env from before the Hotkey section existed.
    text = "".join(l for l in env.read_text(encoding="utf-8").splitlines(True)
                   if not l.startswith(("HOTKEY", "WHISPER_API_KEY")))
    env.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sg, "ENV_PATH", str(env))
    root = tk.Toplevel(tk_root)
    root.withdraw()
    a = sg.SettingsApp(root)
    yield a, env
    root.destroy()


def _set_mode(a, label):
    a.fields["HOTKEY_MODE"].var.set(label)


def test_old_env_without_hotkey_lines_still_saves(app):
    a, env = app
    assert a._value("HOTKEY") == "`"
    assert a.save() is True
    saved = sg.read_env()
    assert saved["HOTKEY"] == "`" and saved["HOTKEY_MODE"] == "toggle"


def test_f9_hold_saves(app):
    a, _ = app
    a.fields["HOTKEY"].var.set("f9")
    _set_mode(a, "Hold to talk")
    assert a.save() is True
    saved = sg.read_env()
    assert saved["HOTKEY"] == "f9" and saved["HOTKEY_MODE"] == "hold"
    assert "Restart Listener" in a.status.cget("text")


def test_backtick_hold_refused_and_nothing_written(app):
    a, env = app
    before = env.read_text(encoding="utf-8")
    _set_mode(a, "Hold to talk")
    assert a.save() is False
    assert "Hold to talk" in a.status.cget("text")
    assert env.read_text(encoding="utf-8") == before


def test_unknown_key_refused(app):
    a, _ = app
    a.fields["HOTKEY"].var.set("ctrl+notakey")
    assert a.save() is False


def test_conflict_saves_with_warning(app):
    a, _ = app
    a.fields["HOTKEY"].var.set("alt+space")
    assert a.save() is True
    assert "window menu" in a.status.cget("text")


def test_hotkey_list_is_hotkeys_not_ollama_models(app):
    a, _ = app
    values = list(a.fields["HOTKEY"].widget.cget("values"))
    assert values[-1] == sg.CUSTOM_LABEL
    assert any("F9" in v for v in values)
    assert not any(":" in v for v in values)  # Ollama names look like name:tag


def test_preset_label_saves_its_key(app):
    a, _ = app
    label = next(l for k, l in sg.hotkey_config.PRESETS if k == "ctrl+alt+space")
    a.fields["HOTKEY"].var.set(label)
    assert a.save() is True
    assert sg.read_env()["HOTKEY"] == "ctrl+alt+space"


def test_edit_backend_inferred_from_old_env(app):
    a, env = app
    has_url = bool(sg.read_env().get("EDIT_LLM_BASE_URL"))
    assert a._value("EDIT_BACKEND") == ("openai" if has_url else "ollama")


def test_inactive_backend_fields_say_so(app):
    a, _ = app
    a.fields["EDIT_BACKEND"].var.set("OpenAI-compatible server or cloud")
    assert a.fields["OLLAMA_EDIT_KEEP_ALIVE"].help_label.cget("text").startswith("NOT ACTIVE")
    assert not a.fields["EDIT_LLM_MODEL"].help_label.cget("text").startswith("NOT ACTIVE")
    a.fields["EDIT_BACKEND"].var.set("Ollama (this PC or LAN)")
    assert a.fields["EDIT_LLM_MODEL"].help_label.cget("text").startswith("NOT ACTIVE")
    assert "7.5" not in a.fields["OLLAMA_EDIT_KEEP_ALIVE"].help_label.cget("text")


def test_vram_note_follows_model(app, monkeypatch):
    a, _ = app
    a._ollama_cache = {}
    monkeypatch.setattr(sg, "installed_ollama_sizes",
                        lambda base: {"small:1b": 1.0e9, "big:30b": 18.0e9})
    a.fields["EDIT_BACKEND"].var.set("Ollama (this PC or LAN)")
    a.fields["OLLAMA_MODEL"].var.set("small:1b")
    assert "3.0 GB" in a.fields["OLLAMA_EDIT_KEEP_ALIVE"].help_label.cget("text")
    a.fields["OLLAMA_MODEL"].var.set("big:30b")
    assert "20.0 GB" in a.fields["OLLAMA_EDIT_KEEP_ALIVE"].help_label.cget("text")
    a.fields["OLLAMA_MODEL"].var.set("gone:7b")
    assert "isn't installed" in a.fields["OLLAMA_MODEL"].help_label.cget("text")


def test_missing_roast_model_flagged(app, monkeypatch):
    a, _ = app
    a._ollama_cache = {}
    monkeypatch.setattr(sg, "installed_ollama_sizes", lambda base: {"other:7b": 1})
    a.fields["OLLAMA_ROAST_MODEL"].var.set("removed/model:latest")
    assert "isn't installed" in a.fields["OLLAMA_ROAST_MODEL"].help_label.cget("text")


def test_port_validation(app):
    a, _ = app
    a.fields["WHISPER_API_PORT"].var.set("80")
    assert a.save() is False
    a.fields["WHISPER_API_PORT"].var.set("5055")
    assert a.save() is True and sg.read_env()["WHISPER_API_PORT"] == "5055"
