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

# The Settings window is Windows-only (winreg, the Windows tray and hotkeys).
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Settings window")


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
    # Word lists go to a temp copy too: no test may write the real file.
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
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


def test_pairing_port_follows_connections_page(app):
    a, _ = app
    a.fields["SERVER_HTTPS"].var.set(True)
    a.fields["WHISPER_HTTPS_PORT"].var.set("5055")
    assert a._live_port() == 5055                  # phones pair to the HTTPS port
    a._set_qr_mode("pair")
    assert "NEW port" in a.qr_note.cget("text")
    a.fields["WHISPER_HTTPS_PORT"].var.set("80")   # invalid -> saved port
    assert a._live_port() == int(sg.local_api.https_port(a.values))
    a.fields["SERVER_HTTPS"].var.set(False)
    a.fields["WHISPER_API_PORT"].var.set("5056")
    assert a._live_port() == 5056                  # no HTTPS: the plain port


def _address_texts(a):
    out = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, sg.tk.Label):
                out.append(c.cget("text"))
            walk(c)
    walk(a._addr_box)
    return out


def _pick(a, key, choice):
    var, _box, options = a._finder[key]
    var.set(next(label for k, label in options if k == choice))


def test_connect_another_program_builds_one_address(app):
    a, _ = app
    a._hosts_cache = {"this_pc": "127.0.0.1", "home": "192.168.0.2", "tailscale": "100.1.2.3"}
    a.fields["SERVER_HTTPS"].var.set(True)
    a.fields["SERVER_PLAIN_HTTP"].var.set(True)
    _pick(a, "where", "home"); _pick(a, "what", "plain"); _pick(a, "conn", "http")
    assert a.finder_address()[0] == "http://192.168.0.2:5000/v1"
    _pick(a, "what", "dictate")
    assert a.finder_address()[0] == "http://192.168.0.2:5000/dictate/v1"
    _pick(a, "where", "tailscale"); _pick(a, "what", "words"); _pick(a, "conn", "https")
    url, notes = a.finder_address()
    assert url == "https://100.1.2.3:5001/transcribe_words"
    assert any("certificate" in n for n in notes)
    _pick(a, "what", "sentences"); _pick(a, "where", "this_pc"); _pick(a, "conn", "http")
    assert a.finder_address()[0] == "http://127.0.0.1:5000/transcribe_time_stamped"
    # Live: ports, port switches and the key change the result.
    a.fields["WHISPER_API_PORT"].var.set("5055")
    assert a.finder_address()[0] == "http://127.0.0.1:5055/transcribe_time_stamped"
    a.fields["SERVER_PLAIN_HTTP"].var.set(False)
    url, notes = a.finder_address()
    assert url == "" and "unencrypted port is off" in notes[0]
    a.fields["SERVER_PLAIN_HTTP"].var.set(True)
    a.fields["WHISPER_API_KEY"].var.set("wr_sec_abcdefghijklmnop")
    a.fields["WHISPER_API_KEY_REQUIRED"].var.set(True)
    assert any(n.startswith("API key: required") for n in a.finder_address()[1])
    a.fields["WHISPER_API_KEY_REQUIRED"].var.set(False)
    assert any(n.startswith("API key: not required") for n in a.finder_address()[1])
    a._hosts_cache["tailscale"] = ""
    _pick(a, "where", "tailscale")
    assert a.finder_address()[0] == "" and "Tailscale isn't running" in a.finder_address()[1][0]


def test_connections_page_order(app):
    a, _ = app
    frame = a.frames["Connections"]
    a.show_section("Connections")
    a.root.update_idletasks()
    names = ("Get the app", "Pair your phone", "Connect another program", "API key",
             "Ports and encryption")
    heads = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, sg.tk.Label) and c.cget("text") in names:
                heads.append((c.winfo_rooty(), c.cget("text")))
            walk(c)
    walk(frame)
    order = [t for _y, t in sorted(heads)]
    assert order.index("Get the app") < order.index("Pair your phone") <         order.index("Connect another program") < order.index("API key") <         order.index("Ports and encryption")


def test_same_number_for_both_ports_refused(app):
    a, _ = app
    a.fields["SERVER_HTTPS"].var.set(True)
    a.fields["SERVER_PLAIN_HTTP"].var.set(True)
    a.fields["WHISPER_HTTPS_PORT"].var.set("5000")
    a.fields["WHISPER_API_PORT"].var.set("5000")
    assert a.save() is False and "different" in a.status.cget("text")


def test_server_window_hidden_by_default(app, tmp_path, monkeypatch):
    f = next(f for _, fs in sg.SCHEMA for f in fs if f.key == "API_VISIBLE")
    assert f.default == "false"


def test_words_page_saves_lists_and_keeps_triggers(app):
    a, _ = app
    before = sg.words_config.load()
    a.vocab_list.set(["Kubernetes", "ollama", "Ollama", ""])
    a._words_dirty = True
    a._add_correction_row("Kuber Netties", "Kubernetes", lambda *_: None)
    assert a.save() is True
    saved = sg.words_config.load()
    assert saved["vocabulary"] == ["Kubernetes", "ollama"]          # de-duplicated
    assert saved["name_corrections"]["Kuber Netties"] == "Kubernetes"
    assert saved["name_corrections"]["Leslie"] == "Lesley"              # existing kept
    assert saved["trigger_phrases"] == before["trigger_phrases"]  # untouched
    assert saved["_comment_vocabulary"] == before["_comment_vocabulary"]


def test_half_filled_correction_blocks_save(app):
    a, env = app
    before = env.read_text(encoding="utf-8")
    a._add_correction_row("Emma", "", lambda *_: None)
    a._words_dirty = True
    assert a.save() is False
    assert "both sides" in a.status.cget("text")
    assert env.read_text(encoding="utf-8") == before


def test_hint_counter_warns_when_over_budget(app):
    a, _ = app
    a.vocab_list.set(["Word%03d" % i for i in range(120)])
    a._refresh_hint_count()
    assert "ignored" in a.hint_count.cget("text")


def test_duplicate_correction_refused(app):
    a, _ = app
    a._add_correction_row("leslie", "Lesley", lambda *_: None)   # already in the list
    a._words_dirty = True
    assert a.save() is False and "twice" in a.status.cget("text")


def test_cleanup_settings_save_and_show_for_both_backends(app):
    a, _ = app
    for backend in ("Ollama (this PC or LAN)", "OpenAI-compatible server or cloud"):
        a.fields["EDIT_BACKEND"].var.set(backend)
        assert a.fields["CLEANUP_ALWAYS"].group is None      # never hidden
    a.fields["CLEANUP_REWRITE"].var.set("Fix grammar")
    a.fields["CLEANUP_ALWAYS"].var.set(True)
    assert a.save() is True
    saved = sg.read_env()
    assert saved["CLEANUP_REWRITE"] == "fix" and saved["CLEANUP_ALWAYS"] == "true"


def test_shared_switch_redraws_when_value_changes_elsewhere(app):
    a, _ = app
    var = a.fields["GAME_MODE"].var
    twin = sg.Toggle(a.root, var)          # e.g. the wizard's Install-page switch
    var.set(True)
    for _ in range(40):                    # let the slide animation finish
        a.root.update()
        a.root.after(15)
    assert twin._pos == 1.0
    twin.destroy()
    var.set(False)                         # a destroyed switch must not error


def _visible_sections(a):
    return [n for n, _ in sg.SCHEMA if a.nav[n][0].winfo_manager()]


def test_source_switch_shows_only_matching_settings(app):
    a, _ = app
    groups = a.groups
    a.fields["TRANSCRIBE_SOURCE"].var.set("This PC (runs Whisper here)")
    assert groups["src_local"].winfo_manager() and not groups["src_cloud"].winfo_manager()
    assert "AI Editing (LLM)" in _visible_sections(a)

    a.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service (Groq, OpenAI, ...)")
    assert groups["src_cloud"].winfo_manager()
    assert not groups["src_local"].winfo_manager()            # CUDA + model list hidden
    assert not groups["src_local_startup"].winfo_manager()    # server startup hidden
    vis = _visible_sections(a)
    assert "AI Editing (LLM)" not in vis and "Connections" not in vis and "Words & Triggers" in vis
    assert a._value("CLOUD_BASE_URL") == "https://api.groq.com/openai/v1"   # preset filled

    a.fields["TRANSCRIBE_SOURCE"].var.set("Another PC running Whisper Unchained Server")
    assert groups["src_remote"].winfo_manager()
    assert "Words & Triggers" not in _visible_sections(a)


def test_cloud_preset_switch_keeps_custom_values(app):
    a, _ = app
    a.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service (Groq, OpenAI, ...)")
    a.fields["CLOUD_PROVIDER"].var.set("OpenAI")
    assert a._value("CLOUD_BASE_URL") == "https://api.openai.com/v1"
    assert a._value("CLOUD_MODEL") == "whisper-1"
    a.fields["CLOUD_BASE_URL"].var.set("https://my.server/v1")
    a.fields["CLOUD_PROVIDER"].var.set("Groq")
    assert a._value("CLOUD_BASE_URL") == "https://my.server/v1"   # typed value kept


def test_source_saved_and_backend_groups_unaffected(app):
    a, _ = app
    a.fields["TRANSCRIBE_SOURCE"].var.set("Another PC running Whisper Unchained Server")
    a.fields["REMOTE_SERVER_URL"].var.set("http://100.1.2.3:5000/v1")
    a.fields["EDIT_BACKEND"].var.set("Ollama (this PC or LAN)")      # backend switcher...
    assert a.groups["src_remote"].winfo_manager()                     # ...doesn't hide it
    assert a.save() is True
    saved = sg.read_env()
    assert saved["TRANSCRIBE_SOURCE"] == "remote"
    assert saved["REMOTE_SERVER_URL"] == "http://100.1.2.3:5000/v1"


def test_source_changes_intro_filler_switch_and_footer(app):
    a, _ = app
    a.fields["TRANSCRIBE_SOURCE"].var.set("Another PC running Whisper Unchained Server")
    assert "another PC" in a.intro_labels["Transcription"].cget("text")
    assert not a.fields["REMOVE_FILLERS"].rows[0].winfo_manager()   # that PC's setting
    assert not a.btn_restart.winfo_manager()                        # nothing to restart here
    a.fields["TRANSCRIBE_SOURCE"].var.set("A cloud service (Groq, OpenAI, ...)")
    assert a.fields["REMOVE_FILLERS"].rows[0].winfo_manager()       # runs on this PC
    a.fields["TRANSCRIBE_SOURCE"].var.set("This PC (runs Whisper here)")
    assert a.btn_restart.winfo_manager()
    assert "runs on this PC" in a.intro_labels["Transcription"].cget("text")


# --- Startup > Windows integration buttons -----------------------------------

def test_integration_buttons_run_the_scripts_without_admin(app, monkeypatch):
    app = app[0]
    state = {"menu": False, "login": True}
    calls = []
    monkeypatch.setattr(app, "_integration_on", lambda k: state[k])

    class R:
        returncode = 0

    def fake_run(cmd, **kw):
        calls.append(cmd)
        key = "menu" if "install_context_menu.ps1" in cmd[5] else "login"
        state[key] = "-Remove" not in cmd
        return R()
    monkeypatch.setattr(sg.subprocess, "run", fake_run)
    app._refresh_integration("menu")
    assert app._integration_rows["menu"][0].cget("text") == "Add"
    app._toggle_integration("menu")
    assert calls[-1][-1] == "-NoPause" and "runas" not in " ".join(calls[-1])
    assert app._integration_rows["menu"][0].cget("text") == "Remove"
    app._toggle_integration("login")
    assert calls[-1][-1] == "-Remove" and state["login"] is False


def test_delete_my_history_removes_logs_and_clips(app, monkeypatch, tmp_path):
    app = app[0]
    api = tmp_path / "api"
    (api / "audio_corpus" / "keep").mkdir(parents=True)
    (api / "transcripts.log").write_text("hello", encoding="utf-8")
    (api / "audio_corpus" / "a.wav").write_bytes(b"x" * 10)
    (api / "audio_corpus" / "keep" / "b.wav").write_bytes(b"x" * 10)
    monkeypatch.setattr(sg, "PROJECT_DIR", str(tmp_path))
    assert app._history_summary() == (2, 25)
    app.delete_history(confirm=lambda: False)
    assert (api / "transcripts.log").exists()
    app.delete_history(confirm=lambda: True)
    assert app._history_summary() == (0, 0) and not (api / "transcripts.log").exists()


def test_trigger_phrases_rename_add_remove_and_save(app):
    a, _ = app
    labels = sg.words_config.triggers.ACTION_LABELS
    first = next(r for r in a.trig_rows if r[1].get() == "prompt AI")
    first[1].set("Jarvis")                                   # rename
    a._add_trigger_row("hey editor", "edit", a._corr_on_change)
    roast = next(r for r in a.trig_rows if r[1].get() == "prompt roast")
    roast[2].set(labels["edit"])                             # change what it does
    assert a.save() is True
    rows = sg.words_config.load()["trigger_phrases"]
    phrases = {r["phrase"]: r["action"] for r in rows}
    assert phrases["Jarvis"] == "edit" and "prompt AI" not in phrases
    assert phrases["hey editor"] == "edit" and phrases["prompt roast"] == "edit"


def test_bad_trigger_phrase_blocks_save(app):
    a, env = app
    before = env.read_text(encoding="utf-8")
    a._add_trigger_row("x", "edit", a._corr_on_change)
    a._words_dirty = True
    assert a.save() is False and "too short" in a.status.cget("text")
    assert env.read_text(encoding="utf-8") == before


def test_reset_triggers_restores_defaults(app):
    a, _ = app
    for r in list(a.trig_rows):
        r[0].destroy()
    a.trig_rows.clear()
    a._reset_triggers(lambda *_: None)
    assert [r[1].get() for r in a.trig_rows] ==         [d["phrase"] for d in sg.words_config.triggers.DEFAULT_PHRASES]


def test_spoken_tags_are_rows(app):
    a, _ = app
    f = a.fields["CORPUS_TAG_TRIGGERS"]
    f.widget.set(["loud in here", "  ", "tag, this"])
    updates, err = a.collect()
    assert err is None and updates["CORPUS_TAG_TRIGGERS"] == "loud in here,tag  this"


def test_custom_hotkey_from_modifier_and_key(app):
    a, _ = app
    f = a.fields["HOTKEY"]
    cap = f.hotkey_capture
    combo = f.widget
    combo.set(sg.CUSTOM_LABEL)
    combo.event_generate("<<ComboboxSelected>>")
    a.root.update()
    combo.setvar(str(combo.cget("textvariable")), sg.CUSTOM_LABEL)
    assert f.var.get() == "" or cap.key                     # no key yet -> empty
    cap.set("d")
    cap.on_key("d")
    mod_box = [w for w in cap.master.winfo_children() if isinstance(w, sg.ttk.Combobox)][0]
    mod_box.set("Ctrl + Alt")
    mod_box.setvar(str(mod_box.cget("textvariable")), "Ctrl + Alt")
    assert f.var.get() == "ctrl+alt+d"
    combo.setvar(str(combo.cget("textvariable")), "F9")
    assert f.var.get() == "f9"


def test_key_names_from_tk():
    assert sg.key_from_event("grave", "`") == "`"
    assert sg.key_from_event("F10") == "f10"
    assert sg.key_from_event("D", "D") == "d"
    assert sg.key_from_event("Prior") == "page up"
    assert sg.key_from_event("Control_L") == ""        # a modifier alone is ignored
    assert sg.key_from_event("Control_R") == "right ctrl"
    assert sg.split_hotkey("ctrl+alt+d") == ("ctrl+alt", "d")
    assert sg.split_hotkey("right ctrl") == ("", "right ctrl")


def test_row_list_enter_inserts_below(tk_root):
    changes = []
    rl = sg.RowList(tk_root, ["a", "c"], lambda: changes.append(1), "Add")
    try:
        rl.add("b", after=rl.rows[0])
        assert rl.values() == ["a", "b", "c"]
        rl.remove(rl.rows[0])
        assert rl.values() == ["b", "c"] and changes
    finally:
        rl.destroy()


def test_shortcut_rows_examples_are_never_saved(app):
    a, _ = app
    a._add_correction_row("", "", a._corr_on_change)          # empty: shows an example
    a._add_correction_row("here's my address", "123 Main St, Portland, OR", a._corr_on_change)
    a._words_dirty = True
    assert a.save() is True
    saved = sg.words_config.load()["name_corrections"]
    assert saved["here's my address"] == "123 Main St, Portland, OR"
    assert not any(k.startswith("e.g.") or v.startswith("e.g.") for k, v in saved.items())
    assert "" not in saved


def test_mouse_wheel_over_a_dropdown_never_changes_it(app):
    a, _ = app
    a.show_section("Words & Triggers")
    a.root.update()
    row = a.trig_rows[0]
    combo = [w for w in row[0].winfo_children() if isinstance(w, sg.ttk.Combobox)][0]
    before = row[2].get()
    for d in (-120, -120, 120):
        combo.event_generate("<MouseWheel>", delta=d)
        a.root.update()
    assert row[2].get() == before
