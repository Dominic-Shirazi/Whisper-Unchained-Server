"""The Skills page of Settings ("Triggers" in the code): what it says and in
what order, the phrase editor behind "Choose your own phrase", the New skill
editor, and how the page reads and saves another PC's lists when this PC only
listens. Runs against temp copies of .env and the word file; the server PC is
faked."""
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

LOCAL = "This PC"
REMOTE = "Another PC of mine"
CLOUD = "A cloud service"

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


# --- The Skills page: wording and layout --------------------------------------------

def test_the_tab_says_skills_and_keeps_its_name_in_the_code(app):
    a, _ = app
    assert sg.SECTION_LABELS["Triggers"] == "Skills"
    assert a.nav["Triggers"][2].cget("text") == "Skills"
    # The same for AI Editing: "(LLM)" is only in the code's name for the page.
    assert sg.SECTION_LABELS["AI Editing (LLM)"] == "AI Editing"
    assert a.nav["AI Editing (LLM)"][2].cget("text") == "AI Editing"
    assert not any("LLM" in lbl.cget("text") for _r, _b, lbl in a.nav.values())
    assert a.nav["Words"][2].cget("text") == "Words"
    assert "Triggers" in a.frames and "Skills" not in a.frames


def test_page_opens_on_the_ask_card_then_your_skills(app):
    a, _ = app
    assert not sg.SECTION_INTROS.get("Triggers")
    assert "Triggers" not in getattr(a, "intro_labels", {})
    a.show_section("Triggers")
    shown = [t for t in _shown_texts(a.frames["Triggers"]) if t]     # (corner pieces have no text)
    assert shown[:4] == ["Ask the AI to change your text",
                         'Say "prompt AI", then what you want.',
                         '"Prompt AI, make this sound more formal."',
                         "Choose your own phrase"]
    assert shown[4] == "Your skills"
    assert sorted(shown[5:7]) == ["+ New skill", "Browse the library"]
    # One row per skill: name, what to say, Edit (and Share, remove).
    row = shown[7:]
    assert "Local one" in row and 'say "prompt local"' in row and "Edit" in row
    assert row.index("Local one") < row.index('say "prompt local"')
    # "+ New skill" sits left of "Browse the library", on the heading's row.
    own, browse = [w for w in a._trig_controls if isinstance(w, sg.Button)]
    a.root.deiconify()
    a.root.update()
    assert own.cget("text") == "+ New skill" and browse.cget("text") == "Browse the library"
    assert a._custom_heading.winfo_rootx() < own.winfo_rootx() < browse.winfo_rootx()
    assert abs(own.winfo_rooty() - browse.winfo_rooty()) < 3
    assert own.winfo_rooty() < a._custom_heading.winfo_rooty() + a._custom_heading.winfo_height()
    a.root.withdraw()
    # Nothing a user reads on the page says "trigger"; the long explanation
    # lives in the New skill editor now.
    everything = " ".join(_texts(a.frames["Triggers"]))
    assert "trigger" not in everything.lower()
    assert "A skill is your own phrase" not in everything
    assert "Example: a client emails" not in everything and "mean mode" not in everything
    assert "hand your words to the AI" not in everything
    assert "—" not in everything


def test_the_card_uses_the_first_phrase_the_user_really_has(app):
    a, _ = app
    a.trig_rows[0][1].set("hey computer")
    assert a._ask_line.cget("text") == 'Say "hey computer", then what you want.'
    assert a._ask_example.cget("text") == '"Hey computer, make this sound more formal."'
    # Only an "AI edit" phrase counts: "end prompt" is not a way to ask.
    a.trig_rows[0][1].set("")
    assert a._ask_line.cget("text") == sg.ASK_CARD_NO_PHRASE
    assert a._ask_example.cget("text") == ""
    a._reset_triggers(a._trig_changed)
    assert a._ask_line.cget("text") == 'Say "prompt AI", then what you want.'


def test_change_what_you_say_opens_the_phrase_editor_and_back(app):
    a, _ = app
    a.show_section("Triggers")
    button = a._ask_button
    assert button.cget("text") == "Choose your own phrase"
    _click(a, button)
    assert a._phrases_view.winfo_manager() and not a._skills_main.winfo_manager()
    shown = _shown_texts(a.frames["Triggers"])
    assert shown[0] == "‹ Back to Skills"
    assert shown[1] == "What you say to ask the AI"
    assert shown[2] == "Say one of these while dictating, then tell the AI what you want."
    texts = _texts(a._phrases_view)
    order = [next(i for i, t in enumerate(texts) if want in t) for want in (
        "What you say to ask the AI", "message", "phrase", "directions",
        "Result:", "End of instruction",
        "Phrase", "+ Add a phrase", "Restore original phrases",
        "2 phrases (1 AI edit, 1 end of instruction)")]
    assert order == sorted(order)
    assert "Your skills" not in shown
    # The rows are the same ones Save reads.
    assert all(str(r[0]).startswith(str(a._phrases_view) + ".") for r in a.trig_rows)
    back = next(w for w in a._phrases_view.winfo_children()
                if isinstance(w, tk.Label) and "Back to Skills" in str(w.cget("text")))
    _click(a, back)
    assert a._skills_main.winfo_manager() and not a._phrases_view.winfo_manager()
    # Coming back to the page from another one starts on the skills again.
    a._show_phrases_view()
    a.show_section("Words")
    a.show_section("Triggers")
    assert a._skills_main.winfo_manager() and not a._phrases_view.winfo_manager()


def test_phrase_editor_still_edits_validates_and_saves(app):
    a, _ = app
    a.show_section("Triggers")
    a._show_phrases_view()
    add = next(w for w in a._trig_controls
               if isinstance(w, tk.Label) and w.cget("text") == "+ Add a phrase")
    _click(a, add)
    assert len(a.trig_rows) == 3
    a.trig_rows[2][1].set("a,b")
    assert a.save() is False
    assert a.status.cget("text") == 'Phrase "a,b" can\'t contain commas or line breaks.'
    a.trig_rows[2][1].set("hey computer")
    assert a._phrase_count.cget("text") == "3 phrases (2 AI edit, 1 end of instruction)"
    assert a.save() is True, a.status.cget("text")
    assert [r["phrase"] for r in sg.words_config.load()["trigger_phrases"]] == [
        "prompt AI", "end prompt", "hey computer"]


def test_the_card_heading_and_first_rows_fit_without_scrolling(app):
    a, _ = app
    for n in range(5):
        a._custom.append({"id": "s%d" % n, "name": "Skill number %d" % n,
                          "phrases": ["skill number %d" % n], "prompt": "Do thing %d." % n,
                          "enabled": True})
    a._render_custom_triggers()
    a.root.geometry("1000x760")             # the window's default size
    a.root.deiconify()
    a.show_section("Triggers")
    a.root.update()
    rows = a.custom_box.winfo_children()
    assert len(rows) == 6
    bottom = a.canvas.winfo_rooty() + a.canvas.winfo_height()
    assert a.canvas.yview()[0] == 0.0
    assert rows[-1].winfo_rooty() + rows[-1].winfo_height() <= bottom
    assert a._ask_line.winfo_rooty() >= a.canvas.winfo_rooty()
    a.root.withdraw()


# --- The pure helpers behind the page ---------------------------------------------------

def test_first_edit_phrase_and_the_cards_sentences():
    rows = [{"phrase": "end prompt", "action": "end"}, {"phrase": "  ", "action": "edit"},
            {"phrase": " hey   Whisper ", "action": "edit"}, {"phrase": "prompt AI", "action": "edit"}]
    assert sg.first_edit_phrase(rows) == "hey Whisper"
    assert sg.first_edit_phrase([("end prompt", "end"), ("prompt AI", "edit")]) == "prompt AI"
    assert sg.first_edit_phrase([{"phrase": "end prompt", "action": "end"}]) == ""
    assert sg.first_edit_phrase([]) == "" and sg.first_edit_phrase(None) == ""
    assert sg.ask_ai_lines("prompt AI") == ('Say "prompt AI", then what you want.',
                                            '"Prompt AI, make this sound more formal."')
    assert sg.ask_ai_lines("hey Whisper")[1] == '"Hey Whisper, make this sound more formal."'
    assert sg.ask_ai_lines("") == (sg.ASK_CARD_NO_PHRASE, "")
    assert sg.ask_ai_lines(None) == (sg.ASK_CARD_NO_PHRASE, "")


def test_skill_summary_names_the_first_phrase():
    assert sg.skill_summary({"phrases": ["prompt intake"]}) == 'say "prompt intake"'
    assert sg.skill_summary({"phrases": ["skill formal email", "prompt email", "x y"]}) == \
        'say "skill formal email" (+2 more)'
    assert sg.skill_summary({"phrases": []}) == "" and sg.skill_summary({}) == ""


@pytest.mark.parametrize("server_says, shown", [
    ("Custom triggers: every trigger needs a name.", "Skills: every skill needs a name."),
    ('Custom trigger "Bug ticket": the phrase "prompt AI" is already used by a built-in '
     'trigger phrase.',
     'Skill "Bug ticket": the phrase "prompt AI" is already used by one of your phrases '
     'for asking the AI.'),
    ('Custom trigger "A": the phrase "x y" is already used by the trigger "B".',
     'Skill "A": the phrase "x y" is already used by the skill "B".'),
    ('Trigger phrase "a,b" can\'t contain commas or line breaks.',
     'Phrase "a,b" can\'t contain commas or line breaks.'),
    # The user's own words, in quotes, are never touched.
    ('Custom trigger "Trigger happy": the phrase "pull the trigger" is too short.',
     'Skill "Trigger happy": the phrase "pull the trigger" is too short.'),
    ("Keep at least one phrase", "Keep at least one phrase"),
])
def test_server_messages_are_shown_in_todays_words(server_says, shown):
    assert sg.skill_wording(server_says) == shown


def test_skill_wording_leaves_nothing_alone():
    assert sg.skill_wording(None) is None and sg.skill_wording("") == ""


def test_the_real_checks_never_say_trigger_in_settings(app):
    a, _ = app
    bad = {"id": "x", "name": "Other", "phrases": ["prompt AI"],
           "prompt": "Do something useful with it.", "enabled": True}
    err = a._check_custom(bad)
    assert err and "trigger" not in err.lower() and "of your phrases for asking the AI" in err
    a._custom.append(dict(bad, phrases=["prompt local"]))
    _clean, err = a.collect_custom_triggers()
    assert err and "trigger" not in err.lower() and 'the skill "Local one"' in err


# --- The New skill editor carries the explanation ----------------------------------------

def test_new_skill_editor_explains_skills_with_an_example(tk_root):
    import trigger_dialogs as td
    d = td.TriggerEditor(tk_root, None)
    try:
        assert d.top.title() == "New skill"
        texts = _texts(d.top)
        order = [next(i for i, t in enumerate(texts) if want in t) for want in (
            "A skill is your own phrase for an instruction you've saved.",
            'The phrase can be anything you like: "prompt intake", "skill intake", or your own',
            "See an example", "Name", "Say this", "What the AI should do", "Save skill")]
        assert order == sorted(order)
        assert not d.example.winfo_manager()                # closed until asked for
        d.top.update_idletasks()
        width, height = d.top.winfo_reqwidth(), d.top.winfo_reqheight()
        d.toggle_example()
        assert d.example.winfo_manager() and d.example_link.cget("text") == "Hide the example"
        d.top.update_idletasks()                            # beside the boxes: wider, not taller
        assert d.top.winfo_reqwidth() > width + 300 and d.top.winfo_reqheight() == height
        inside = _texts(d.example)
        order = [next(i for i, t in enumerate(inside) if want in t) for want in (
            "Example: a client emails you their details",
            'Add "Client intake sheet" from the library',
            "Say a skill's phrase anywhere while dictating",
            "Or highlight text first",
            'one skill can be "prompt intake", another "skill formal email"',
            "A skill uses the AI set up in AI Editing. A stronger AI can do harder jobs",
            "Looking for mean mode? It's a skill in the library, under Fun.")]
        assert order == sorted(order)
        example = d.example_text
        assert "say prompt intake. It comes back as your intake sheet" in example.get("1.0", "end")
        start, end = example.tag_ranges("phrase")
        assert example.get(start, end) == "prompt intake"   # the phrase is the bold part
        assert str(example.cget("bg")) == sg.SURFACE
        assert "trigger" not in " ".join(texts + inside).lower()
        d.toggle_example()
        assert not d.example.winfo_manager() and d.example_link.cget("text") == "See an example"
    finally:
        d.top.destroy()


def test_editing_a_skill_has_no_explanation(tk_root):
    import trigger_dialogs as td
    d = td.TriggerEditor(tk_root, {"id": "x", "name": "Bug ticket", "phrases": ["prompt ticket"],
                                   "prompt": "Rewrite the text as a bug ticket.", "enabled": True})
    try:
        assert d.top.title() == "Edit skill" and not hasattr(d, "example_link")
        texts = _texts(d.top)
        assert "Done" in texts and not any("A skill is your own phrase" in t for t in texts)
    finally:
        d.top.destroy()


def test_new_skill_editor_says_skill_when_it_refuses(tk_root):
    import trigger_dialogs as td
    d = td.TriggerEditor(tk_root, None)
    try:
        d.name.set("Bug ticket")
        d.phrases.insert("1.0", "a,b")
        d.prompt.insert("1.0", "Rewrite the text as a bug ticket.")
        item, err = d.collect()
        assert item is None and err.startswith('Skill "Bug ticket": ') and "trigger" not in err.lower()
        d.name.set("")
        assert d.collect() == (None, "Skills: every skill needs a name.")
    finally:
        d.top.destroy()


def test_examples_library_link_leaves_the_editor_only_when_nothing_is_lost(tk_root):
    import trigger_dialogs as td
    d = td.TriggerEditor(tk_root, None)
    d.go_to_library()                               # nothing typed: straight there
    assert d.open_library is True and d.result is None and not d.top.winfo_exists()
    d = td.TriggerEditor(tk_root, None)
    d.name.set("Half written")
    d.go_to_library()
    assert d.open_library is False and d.top.winfo_exists()
    assert d.status.cget("text") == td.EDITOR_LEAVE_WARNING
    d.go_to_library()                               # said twice: go
    assert d.open_library is True and not d.top.winfo_exists()


def test_new_skill_opens_the_library_at_the_intake_entry_when_asked(app, monkeypatch):
    a, _ = app
    import trigger_dialogs
    opened, asked = [], []

    def fake_edit(parent, item=None, check=None, on_library=None):
        asked.append((item, check is not None))
        on_library()
        return None
    monkeypatch.setattr(trigger_dialogs, "edit_trigger", fake_edit)
    monkeypatch.setattr(trigger_dialogs, "browse_library",
                        lambda parent, installed, on_add, load=None, focus=None:
                        opened.append(focus))
    a._new_custom_trigger()
    assert asked == [(None, True)] and opened == ["intake"]
    assert _names(a) == ["Local one"] and not a.dirty
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
    assert d.top.title() == "Skill library"
    d.library = d._load()
    d.top.deiconify()
    d.top.update()
    d.loaded()
    d.top.update()
    assert d.canvas.yview()[0] > 0.3 and "Entry 20" in d.status.cget("text")
    assert d.show_entry("nope") is False
    assert "trigger" not in " ".join(_texts(d.top)).lower()
    d.close()


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
    assert shown[1] == "Reload" and shown[2] == "Ask the AI to change your text"
    assert 'Say "hey Jarvis", then what you want.' in shown      # that PC's phrase
    a._show_phrases_view()                          # the banner stays above the phrase editor
    shown = _shown_texts(a.frames["Triggers"])
    assert shown[1] == "Reload" and shown[2] == "‹ Back to Skills"
    a._show_skills_view()
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
    assert a._ask_line.cget("text") == ""           # not "you have no phrase": not known yet
    assert a._phrase_count.cget("text") == "Phrase list not loaded"
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
    # The buttons and the add links do nothing while it is like this.
    # (The two windows are faked: a real one would wait for a person.)
    import trigger_dialogs
    opened = []
    monkeypatch.setattr(trigger_dialogs, "browse_library",
                        lambda parent, installed, on_add, load=None, focus=None:
                        opened.append(("library", focus)))
    monkeypatch.setattr(trigger_dialogs, "edit_trigger",
                        lambda parent, item=None, check=None, on_library=None:
                        opened.append("new"))
    for b in buttons:
        _click(a, b)
    a._show_phrases_view()                                 # still reachable, and empty
    links = [w for w in a._trig_controls if not isinstance(w, sg.Button)]
    assert [w.cget("text") for w in links] == ["+ Add a phrase", "Restore original phrases"]
    for link in links:
        _click(a, link)
    assert _phrases(a) == [] and a._remote_trig_dirty is False and opened == []
    # Nothing to save there, and this PC's file is left alone.
    assert a.save() is True and server.saved == []
    assert words.read_text(encoding="utf-8") == before
    # Reload still works.
    server.fetch_error = None
    a._load_remote_triggers()
    assert a._trig_editable and _phrases(a) == ["hey Jarvis", "that is all"]
    assert all(b._enabled for b in buttons)
    _click(a, links[0])                                    # "+ Add a phrase" again
    assert len(a.trig_rows) == 3
    a._show_skills_view()
    for b in buttons:
        _click(a, b)
    assert opened == ["new", ("library", None)]


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
    assert "of your phrases for asking the AI" in a._check_custom(item)     # the other PC's phrase
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
    assert a.status.cget("text") == "Unsaved skill changes for the other PC were discarded."
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
