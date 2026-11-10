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


def test_backend_choice_shows_only_its_fields(app):
    a, _ = app
    def shown(key):
        return a.fields[key].widget.winfo_toplevel() and bool(
            a.groups[a.fields[key].group].winfo_manager())
    a.fields["EDIT_BACKEND"].var.set("OpenAI-compatible server or cloud")
    assert shown("EDIT_LLM_BASE_URL") and shown("EDIT_LLM_API_KEY")
    assert not shown("OLLAMA_EDIT_API_URL") and not shown("OLLAMA_EDIT_KEEP_ALIVE")
    a.fields["EDIT_BACKEND"].var.set("Ollama (this PC or LAN)")
    assert shown("OLLAMA_MODEL") and not shown("EDIT_LLM_MODEL")
    assert "7.5" not in a.fields["OLLAMA_EDIT_KEEP_ALIVE"].help_label.cget("text")


def test_hidden_backend_values_still_saved(app):
    a, _ = app
    before = sg.read_env().get("EDIT_LLM_BASE_URL", "")
    a.fields["EDIT_BACKEND"].var.set("Ollama (this PC or LAN)")
    assert a.save() is True
    saved = sg.read_env()
    assert saved["EDIT_BACKEND"] == "ollama"
    assert saved.get("EDIT_LLM_BASE_URL", "") == before  # kept, just unused


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


def test_model_chosen_only_from_list(app):
    a, _ = app
    f = a.fields["WHISPER_MODEL_SIZE"]
    assert f.widget.winfo_class() == "Frame"  # a display + link, not a dropdown
    a._use_model(sg.model_catalog.BY_NAME["small.en"])
    assert f.var.get() == "small.en"
    assert a.save() is True and sg.read_env()["WHISPER_MODEL_SIZE"] == "small.en"


def test_model_row_hover_highlights_list(app):
    a, _ = app
    a._highlight_models(True)
    assert str(a._models_box.cget("highlightbackground")) == sg.ACCENT
    a._highlight_models(False)
    assert str(a._models_box.cget("highlightbackground")) == sg.BG


def test_every_section_explains_itself(app):
    for name, _fields in sg.SCHEMA:
        assert sg.SECTION_INTROS.get(name), name


def test_subheadings_are_not_settings(app):
    a, env = app
    assert "_h_server" not in a.fields and "_h_listener" not in a.fields
    assert a.save() is True
    assert "_h_" not in env.read_text(encoding="utf-8")


def test_scheduled_restart_note_matches_this_pc(app, monkeypatch):
    a, _ = app
    for installed in (False, True):
        monkeypatch.setattr(sg, "scheduled_restart_installed", lambda: installed)
        a._refresh_restart_help()
        text = a.fields["RESTART_INTERVAL_HOURS"].help_label.cget("text")
        assert text.startswith("NOT ACTIVE") is (not installed)


def test_phone_port_mirrors_startup_and_feeds_qr(app):
    a, _ = app
    a.fields["WHISPER_API_PORT"].var.set("5055")
    assert a._live_port() == 5055
    a._set_qr_mode("pair")
    assert "NEW port" in a.qr_note.cget("text")
    a.fields["WHISPER_API_PORT"].var.set("80")       # invalid -> saved port
    assert a._live_port() == sg.api_port(a.values)


def test_port_link_jumps_to_startup(app):
    a, _ = app
    a._go_to_field("Startup", "WHISPER_API_PORT")
    assert a.frames["Startup"].winfo_manager() == "pack"
    assert a.frames["Phone"].winfo_manager() == ""


def test_server_window_hidden_by_default(app, tmp_path, monkeypatch):
    f = next(f for _, fs in sg.SCHEMA for f in fs if f.key == "API_VISIBLE")
    assert f.default == "false"
