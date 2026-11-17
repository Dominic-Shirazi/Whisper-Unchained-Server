"""The Triggers page of Settings: what it says and in what order, and how it
reads and saves another PC's triggers when this PC only listens. Runs against
temp copies of .env and the word file; the server PC is faked."""
import json
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

REAL_IN_BACKGROUND = sg.SettingsApp._in_background      # the fixtures replace it

LOCAL = "This PC (runs Whisper here)"
REMOTE = "Another PC that runs the server"
CLOUD = "A cloud service (Groq, OpenAI, ...)"

SERVER_PHRASES = [{"phrase": "hey Jarvis", "action": "edit"},
                  {"phrase": "that is all", "action": "end"}]
SERVER_CUSTOM = [
    {"id": "markdown", "name": "Markdown", "phrases": ["output in markdown"],
     "prompt": "Rewrite the text as clean Markdown.", "enabled": True},
    {"id": "ticket", "name": "Bug ticket", "phrases": ["prompt ticket"],
     "prompt": "Rewrite the text as a bug ticket.", "enabled": False,
     "library_id": "ticket", "library_version": 1, "author": "Whisper Unchained"},
]


class Server:
    """Stands in for the server PC: what it holds, and what was asked of it."""

    def __init__(self):
        self.phrases = [dict(r) for r in SERVER_PHRASES]
        self.custom = [dict(t) for t in SERVER_CUSTOM]
        self.fetch_error = None
        self.save_error = None
        self.fetched = []       # the env of each fetch
        self.saved = []         # (env, phrases, custom, loaded_custom)

    def fetch(self, env, get=None):
        self.fetched.append(dict(env))
        if self.fetch_error:
            raise self.fetch_error
        return [dict(r) for r in self.phrases], [dict(t) for t in self.custom]

    def save(self, env, phrases, custom, loaded_custom=(), post=None):
        self.saved.append((dict(env), [dict(r) for r in phrases], [dict(t) for t in custom],
                           [dict(t) for t in loaded_custom]))
        if self.save_error:
            raise self.save_error
        self.phrases = [dict(r) for r in phrases]
        self.custom = [dict(t) for t in custom]


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
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(sg.transcribe_client, "server_triggers", s.fetch)
    monkeypatch.setattr(sg.transcribe_client, "save_server_triggers", s.save)
    # No worker thread in tests: the answer is applied at once.
    monkeypatch.setattr(sg.SettingsApp, "_in_background",
                        lambda self, work, done: done(work()))
    return s


@pytest.fixture
def app(tmp_path, monkeypatch, tk_root, server):
    env = tmp_path / ".env"
    src = os.path.join(ROOT, ".env")
    if not os.path.exists(src):
        pytest.skip("no .env on this machine")
    shutil.copy(src, env)
    # Always start from "this PC", whatever the machine running the tests uses.
    text = "".join(l for l in env.read_text(encoding="utf-8").splitlines(True)
                   if not l.startswith(("TRANSCRIBE_SOURCE", "REMOTE_")))
    env.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sg, "ENV_PATH", str(env))
    words = tmp_path / "processing_config.json"
    shutil.copy(sg.words_config.EXAMPLE_PATH, words)
    data = json.loads(words.read_text(encoding="utf-8"))
    data["trigger_phrases"] = [{"phrase": "prompt AI", "action": "edit"},
                               {"phrase": "end prompt", "action": "end"}]
    data["custom_triggers"] = [{"id": "local-one", "name": "Local one",
                                "phrases": ["prompt local"], "prompt": "Do the local thing.",
                                "enabled": True}]
    words.write_text(json.dumps(data, indent=2), encoding="utf-8")
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    root = tk.Toplevel(tk_root)
    root.withdraw()
    a = sg.SettingsApp(root)
    yield a, words
    root.destroy()


def _texts(widget):
    """Every piece of text under a widget, in the order it was built."""
    out = []
    for c in widget.winfo_children():
        if isinstance(c, tk.Text):
            out.append(c.get("1.0", "end").strip())
        elif isinstance(c, tk.Label):
            out.append(str(c.cget("text")))
        out.extend(_texts(c))
    return out


def _shown_texts(widget):
    out = []
    for c in widget.winfo_children():
        if not c.winfo_manager():
            continue
        if isinstance(c, tk.Label):
            out.append(str(c.cget("text")))
        out.extend(_shown_texts(c))
    return out


def _click(a, widget):
    """A real mouse click (the window has to be on screen to get one)."""
    a.root.deiconify()
    a.root.update()
    widget.event_generate("<Button-1>")
    a.root.update()
    a.root.withdraw()


def _phrases(a):
    return [p.get() for _r, p, _a, _e in a.trig_rows]


def _names(a):
    return [t["name"] for t in a._custom]


def _visible_sections(a):
    return [n for n, _ in sg.SCHEMA if a.nav[n][0].winfo_manager()]


def _go_remote(a, url="https://192.168.1.50:5001/v1"):
    a.fields["TRANSCRIBE_SOURCE"].var.set(REMOTE)
    a.fields["REMOTE_SERVER_URL"].var.set(url)
    a.fields["REMOTE_API_KEY"].var.set("wr_sec_exampleexampleexample123")
    a.show_section("Triggers")


# --- Change A: wording and order ------------------------------------------------

def test_page_has_no_intro_and_opens_on_trigger_phrases(app):
    a, _ = app
    assert not sg.SECTION_INTROS.get("Triggers")
    assert "Triggers" not in getattr(a, "intro_labels", {})
    a.show_section("Triggers")
    shown = _shown_texts(a.frames["Triggers"])
    assert shown[0] == "Trigger phrases"
    assert shown[1] == "Say one of these while dictating, then tell the AI what you want."
    assert shown[2] == "Custom triggers ↓"
    everything = " ".join(_texts(a.frames["Triggers"]))
    assert "hand your words to the AI" not in everything
    assert "phones paired" not in everything and "on this PC and on phones" not in everything
    assert "—" not in everything


def test_custom_triggers_are_explained_with_an_example(app):
    a, _ = app
    texts = _texts(a.frames["Triggers"])
    assert texts.count("Custom triggers") == 1          # the heading (the link has an arrow)
    order = [next(i for i, t in enumerate(texts) if want in t) for want in (
        "Trigger phrases", "prompt AI", "What each kind of phrase does", "Show all 2 phrases",
        "A custom trigger is your own phrase for an instruction you've saved.",
        "Example: a client emails you their details",
        'Add "Client intake sheet" from the library',
        "Say the phrase anywhere while dictating",
        "Or highlight text first",
        "It uses the AI set up in AI Editing. A stronger AI can do harder jobs",
        "Browse the library",
        "Looking for mean mode? It's in the library, under Fun.")]
    assert order == sorted(order)
    heading = texts.index("Custom triggers")
    assert order[3] < heading < order[4]
    assert texts[-1] == "Looking for mean mode? It's in the library, under Fun."
    example = a._custom_example
    assert "say prompt intake. It comes back as your intake sheet" in example.get("1.0", "end")
    assert '"unknown" beside anything they didn\'t say.' in example.get("1.0", "end")
    start, end = example.tag_ranges("phrase")
    assert example.get(start, end) == "prompt intake"           # the phrase is the bold part
    assert str(example.cget("bg")) == sg.SURFACE


def test_intake_link_opens_the_library_at_that_entry(app, monkeypatch):
    a, _ = app
    import trigger_dialogs
    opened = []
    monkeypatch.setattr(trigger_dialogs, "browse_library",
                        lambda parent, installed, on_add, load=None, focus=None:
                        opened.append(focus))
    link = next(w for w in a._trig_controls
                if isinstance(w, tk.Label) and "Client intake sheet" in str(w.cget("text")))
    a.show_section("Triggers")
    _click(a, link)
    assert opened == ["intake"]
    with open(os.path.join(ROOT, "trigger_library", "library.json"), encoding="utf-8") as f:
        assert '"id": "intake"' in f.read()


def test_library_browser_brings_the_asked_entry_into_view(tk_root):
    import trigger_dialogs as td
    entries = [{"id": "e%d" % n, "name": "Entry %d" % n, "description": "d", "category": "Cat",
                "phrases": ["prompt e%d" % n], "prompt": "Do thing %d." % n, "model": "any",
                "version": 1, "author": "a"} for n in range(30)]
    d = td.LibraryBrowser(tk_root, lambda: [], lambda e: None,
                          load=lambda: {"entries": entries, "updated": "", "online": True},
                          focus="e20")
    d.library = d._load()
    d.top.deiconify()
    d.top.update()
    d.loaded()
    d.top.update()
    assert d.canvas.yview()[0] > 0.3 and "Entry 20" in d.status.cget("text")
    assert d.show_entry("nope") is False
    d.close()


def test_custom_triggers_link_scrolls_down_to_them(app):
    a, _ = app
    a.root.geometry("1000x700")
    a.root.deiconify()
    a.show_section("Triggers")
    a.root.update()
    assert a.canvas.yview()[0] == 0.0
    a._scroll_to(a._custom_heading)
    a.root.update()
    assert a.canvas.yview()[0] > 0.0
    a.root.withdraw()


# --- Change B: a PC that only listens ----------------------------------------------

def test_local_source_shows_this_pcs_lists_and_asks_nobody(app, server):
    a, _ = app
    a.show_section("Triggers")
    assert _phrases(a) == ["prompt AI", "end prompt"] and _names(a) == ["Local one"]
    assert not a._remote_banner.winfo_manager()
    assert server.fetched == []


def test_remote_source_shows_triggers_and_hides_words(app, server):
    a, _ = app
    a.fields["TRANSCRIBE_SOURCE"].var.set(REMOTE)
    vis = _visible_sections(a)
    assert "Triggers" in vis and "Words" not in vis
    assert a._remote_banner.winfo_manager()
    assert server.fetched == []                     # nothing asked until the page is looked at
    a.fields["TRANSCRIBE_SOURCE"].var.set(CLOUD)    # cloud: both pages, this PC's lists
    vis = _visible_sections(a)
    assert "Triggers" in vis and "Words" in vis and not a._remote_banner.winfo_manager()


def test_fetched_lists_replace_the_local_ones(app, server):
    a, _ = app
    _go_remote(a)
    assert _phrases(a) == ["hey Jarvis", "that is all"]
    assert _names(a) == ["Markdown", "Bug ticket"]
    assert [t["enabled"] for t in a._custom] == [True, False]       # switched-off ones too
    assert a._trig_editable and a._remote_state == "loaded"
    # The banner is the first thing on the page, above the heading.
    shown = _shown_texts(a.frames["Triggers"])
    assert shown[0] == "These are saved on the other PC and apply to everything that uses it."
    assert shown[1] == "Reload" and shown[2] == "Trigger phrases"
    # Asked with what the form holds now, before any Save.
    env = server.fetched[-1]
    assert env["REMOTE_SERVER_URL"] == "https://192.168.1.50:5001/v1"
    assert env["REMOTE_API_KEY"] == "wr_sec_exampleexampleexample123"
    assert env["TRANSCRIBE_SOURCE"] == "remote"
    assert a._remote_trig_dirty is False and not a._words_dirty
    # Looking at the page again doesn't ask again; Reload does.
    n = len(server.fetched)
    a.show_section("Transcription")
    a.show_section("Triggers")
    assert len(server.fetched) == n
    server.phrases = [{"phrase": "hey computer", "action": "edit"}]
    a._load_remote_triggers()
    assert len(server.fetched) == n + 1 and _phrases(a) == ["hey computer"]
    # A new connection (a pasted code) is a different PC: asked again.
    a.show_section("Transcription")
    a.fields["REMOTE_SERVER_URL"].var.set("https://192.168.1.60:5001/v1")
    a.show_section("Triggers")
    assert server.fetched[-1]["REMOTE_SERVER_URL"] == "https://192.168.1.60:5001/v1"


def test_loading_shows_in_the_banner_and_locks_the_page(app, server, monkeypatch):
    a, _ = app
    waiting = []
    monkeypatch.setattr(sg.SettingsApp, "_in_background",
                        lambda self, work, done: waiting.append((work, done)))
    _go_remote(a)
    assert a._remote_note.cget("text") == "Loading from the other PC..."
    assert not a._trig_editable and _phrases(a) == [] and a._custom == []
    work, done = waiting.pop()
    done(work())
    assert a._trig_editable and _phrases(a) == ["hey Jarvis", "that is all"]
    assert "saved on the other PC" in a._remote_note.cget("text")


@pytest.mark.parametrize("error", [
    sg.transcribe_client.TranscribeError("Couldn't reach the other PC (ConnectTimeout)."),
    sg.transcribe_client.TooOld("The app on the server PC is too old for this. "
                                "Update it there first."),
])
def test_fetch_failure_shows_the_sentence_and_disables_editing(app, server, error, monkeypatch):
    a, words = app
    before = words.read_text(encoding="utf-8")
    server.fetch_error = error
    _go_remote(a)
    assert a._remote_note.cget("text") == str(error)
    assert str(a._remote_note.cget("fg")) == sg.DANGER
    assert a._remote_state == "failed" and not a._trig_editable
    assert _phrases(a) == [] and a._custom == []          # not this PC's lists by mistake
    assert not [w for w in a.custom_box.winfo_children()]
    buttons = [w for w in a._trig_controls if isinstance(w, sg.Button)]
    assert len(buttons) == 2 and not any(b._enabled for b in buttons)
    # The add links do nothing while it is like this.
    a._trig_expander.toggle()                              # open the folded phrase list
    opened = []
    monkeypatch.setattr(sg.SettingsApp, "_browse_trigger_library",
                        lambda self, focus=None: opened.append(focus))
    links = [w for w in a._trig_controls if not isinstance(w, sg.Button)]
    assert len(links) == 3
    for link in links:
        _click(a, link)
    for b in buttons:
        _click(a, b)
    assert _phrases(a) == [] and a._remote_trig_dirty is False and opened == []
    # Nothing to save there, and this PC's file is left alone.
    assert a.save() is True and server.saved == []
    assert words.read_text(encoding="utf-8") == before
    # Reload still works.
    server.fetch_error = None
    a._load_remote_triggers()
    assert a._trig_editable and _phrases(a) == ["hey Jarvis", "that is all"]
    assert all(b._enabled for b in buttons)
    _click(a, links[0])                                    # "+ Add a trigger phrase" again
    assert len(a.trig_rows) == 3
    _click(a, links[2])
    assert opened == ["intake"]


def test_save_goes_to_the_other_pc_and_not_to_this_pcs_file(app, server, monkeypatch):
    a, words = app
    before = words.read_text(encoding="utf-8")
    _go_remote(a)
    local_saves = []
    real_save = sg.words_config.save
    monkeypatch.setattr(sg.words_config, "save",
                        lambda *args, **kw: local_saves.append(1) or real_save(*args, **kw))
    a.trig_rows[0][1].set("hey computer")
    a._remove_custom_trigger(a._custom[0])
    a._custom[0]["enabled"] = True
    a._custom_changed()
    assert a._remote_trig_dirty and not a._words_dirty and a.dirty
    assert a.save() is True, a.status.cget("text")
    assert len(server.saved) == 1
    env, phrases, custom, loaded = server.saved[0]
    assert env["REMOTE_SERVER_URL"] == "https://192.168.1.50:5001/v1"
    assert phrases == [{"phrase": "hey computer", "action": "edit"},
                       {"phrase": "that is all", "action": "end"}]
    assert [(t["id"], t["enabled"]) for t in custom] == [("ticket", True)]
    assert custom[0]["library_id"] == "ticket"
    assert loaded == SERVER_CUSTOM                       # as fetched, so "markdown" is removed
    assert local_saves == [] and words.read_text(encoding="utf-8") == before
    assert sg.read_env()["TRANSCRIBE_SOURCE"] == "remote"   # .env still written in the same Save
    assert a._remote_trig_dirty is False and a.dirty is False
    assert _phrases(a) == ["hey computer", "that is all"] and _names(a) == ["Bug ticket"]
    # A second Save with nothing changed sends nothing.
    assert a.save() is True and len(server.saved) == 1
    # The next save removes against the list as it is there now.
    a._remove_custom_trigger(a._custom[0])
    assert a.save() is True
    assert [t["id"] for t in server.saved[-1][3]] == ["ticket"] and server.saved[-1][2] == []


def test_save_shows_what_the_other_pc_made_of_it(app, server, monkeypatch):
    a, _ = app
    _go_remote(a)

    def save(env, phrases, custom, loaded_custom=(), post=None):
        server.phrases = [{"phrase": "Hey Computer", "action": "edit"}]     # its own spelling
    monkeypatch.setattr(sg.transcribe_client, "save_server_triggers", save)
    a.trig_rows[0][1].set("hey computer")
    assert a.save() is True
    assert _phrases(a) == ["Hey Computer"] and a._remote_trig_dirty is False


def test_bad_remote_phrase_blocks_the_save(app, server):
    a, _ = app
    _go_remote(a)
    a.trig_rows[0][1].set("a,b")
    assert a.save() is False and "commas" in a.status.cget("text")
    assert server.saved == [] and a._remote_trig_dirty


def test_refused_save_keeps_the_edits(app, server):
    a, words = app
    before = words.read_text(encoding="utf-8")
    _go_remote(a)
    server.save_error = sg.transcribe_client.TranscribeError("Keep at least one phrase")
    a.trig_rows[0][1].set("hey computer")
    assert a.save() is False
    assert a.status.cget("text") == "Keep at least one phrase"
    assert str(a.status.cget("fg")) == sg.DANGER
    assert _phrases(a) == ["hey computer", "that is all"]
    assert a._remote_trig_dirty and a.dirty
    assert words.read_text(encoding="utf-8") == before
    server.save_error = None
    assert a.save() is True and server.phrases[0]["phrase"] == "hey computer"


def test_clash_checks_use_the_fetched_phrases(app, server):
    a, _ = app
    _go_remote(a)
    item = {"id": "x", "name": "Other", "phrases": ["hey Jarvis"],
            "prompt": "Do something useful with it.", "enabled": True}
    assert "built-in" in a._check_custom(item)                  # the other PC's phrase
    item["phrases"] = ["prompt AI"]                             # only this PC's file has it
    assert a._check_custom(item) is None
    entry = {"id": "lib", "name": "Lib", "phrases": ["that is all"], "prompt": "p" * 20,
             "description": "d", "model": "any", "version": 1, "author": "a", "category": "c"}
    assert "already a phrase" in a._add_library_trigger(entry)
    entry["phrases"] = ["prompt lib"]
    assert a._add_library_trigger(entry) is None
    assert a._remote_trig_dirty and not a._words_dirty and _names(a)[-1] == "Lib"


def test_switching_back_to_this_pc_shows_the_local_lists(app, server):
    a, words = app
    before = words.read_text(encoding="utf-8")
    _go_remote(a)
    a.fields["TRANSCRIBE_SOURCE"].var.set(LOCAL)
    assert _phrases(a) == ["prompt AI", "end prompt"] and _names(a) == ["Local one"]
    assert not a._remote_banner.winfo_manager() and a._trig_editable
    assert "discarded" not in a.status.cget("text")
    assert "Words" in _visible_sections(a)
    # With unsaved edits for the other PC: dropped, and said so.
    _go_remote(a)
    a.trig_rows[0][1].set("hey computer")
    a.fields["TRANSCRIBE_SOURCE"].var.set(LOCAL)
    assert a.status.cget("text") == "Unsaved trigger changes for the other PC were discarded."
    assert _phrases(a) == ["prompt AI", "end prompt"]
    assert a.save() is True and server.saved == []
    assert words.read_text(encoding="utf-8") == before
    # And local editing saves to this PC's file, as ever.
    a.trig_rows[0][1].set("hey local")
    assert a._words_dirty and not a._remote_trig_dirty
    assert a.save() is True and server.saved == []
    assert sg.words_config.load()["trigger_phrases"][0]["phrase"] == "hey local"


def test_switching_back_after_a_failed_fetch_unlocks_the_page(app, server):
    a, _ = app
    server.fetch_error = sg.transcribe_client.TranscribeError("Couldn't reach the other PC.")
    _go_remote(a)
    assert not a._trig_editable
    a.fields["TRANSCRIBE_SOURCE"].var.set(LOCAL)
    assert a._trig_editable and _phrases(a) == ["prompt AI", "end prompt"]


def test_a_late_answer_after_switching_back_is_ignored(app, server, monkeypatch):
    a, _ = app
    waiting = []
    monkeypatch.setattr(sg.SettingsApp, "_in_background",
                        lambda self, work, done: waiting.append((work, done)))
    _go_remote(a)
    a.fields["TRANSCRIBE_SOURCE"].var.set(LOCAL)
    work, done = waiting.pop()
    done(work())
    assert _phrases(a) == ["prompt AI", "end prompt"] and a._trig_origin == "local"


def test_words_edited_before_switching_never_carry_the_other_pcs_triggers(app, server):
    a, _ = app
    a.vocab_list.set(["Kubernetes"])
    a._words_changed()
    _go_remote(a)
    a.trig_rows[0][1].set("hey computer")
    assert a.save() is True
    saved = sg.words_config.load()
    assert saved["vocabulary"] == ["Kubernetes"]
    assert [r["phrase"] for r in saved["trigger_phrases"]] == ["prompt AI", "end prompt"]
    assert [t["name"] for t in saved["custom_triggers"]] == ["Local one"]
    assert server.phrases[0]["phrase"] == "hey computer"


def test_background_work_is_applied_on_the_tk_thread(app):
    a, _ = app
    import threading
    import time
    seen = []
    REAL_IN_BACKGROUND(a, lambda: threading.current_thread().name,
                       lambda worker: seen.append((worker, threading.current_thread().name)))
    for _ in range(100):
        a.root.update()
        if seen:
            break
        time.sleep(0.02)
    assert seen and seen[0][0] != seen[0][1] and seen[0][1] == threading.current_thread().name
