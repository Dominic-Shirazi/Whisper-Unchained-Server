"""Custom triggers: a spoken phrase that stands for a saved instruction
(api/custom_triggers.py), the shared library (trigger_library/, client/
trigger_library.py), how the server routes them, and the Settings screens.

No test here reaches the internet, a real LLM or the real processing_config.json."""
import io
import json
import os
import shutil
import subprocess
import sys
import tkinter as tk
import urllib.parse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "client"))

import custom_triggers as ct  # noqa: E402
import triggers as tr  # noqa: E402
import trigger_library as tl  # noqa: E402
import words_config as wc  # noqa: E402

TICKET = {"name": "Bug ticket", "phrases": ["prompt ticket", "ticket this"],
          "prompt": "Rewrite the text as a bug ticket."}
MARKDOWN = {"name": "Markdown", "phrases": ["output in markdown"],
            "prompt": "Rewrite the text as Markdown."}
BUILTIN = [r["phrase"] for r in tr.DEFAULT_PHRASES]


def clean(*items, **kw):
    out, err = ct.validate([dict(i) for i in items], **kw)
    assert err is None, err
    return out


# --- validation ------------------------------------------------------------------

def test_validate_cleans_and_gives_ids():
    out = clean(dict(TICKET, name="  Bug   ticket ", phrases=' "Prompt ticket!" \n\nticket this'),
                dict(MARKDOWN, name="Bug ticket", phrases=["output in markdown"]))
    assert out[0] == {"id": "bug-ticket", "name": "Bug ticket", "enabled": True,
                      "phrases": ["Prompt ticket", "ticket this"],
                      "prompt": "Rewrite the text as a bug ticket."}
    assert out[1]["id"] == "bug-ticket-2"          # same name: ids stay different


@pytest.mark.parametrize("change, words", [
    ({"name": ""}, "needs a name"),
    ({"name": "x" * 41}, "over 40 characters"),
    ({"phrases": []}, "at least one phrase"),
    ({"phrases": ["okay"]}, "too short"),
    ({"phrases": ["a, b"]}, "commas"),
    ({"phrases": ["p%d q" % i for i in range(7)]}, "6 phrases at most"),
    ({"prompt": "short"}, "say what the AI should do"),
    ({"prompt": "x" * 6001}, "over 6000 characters"),
])
def test_validate_says_what_is_wrong(change, words):
    out, err = ct.validate([dict(TICKET, **change)])
    assert out is None and words in err


def test_a_phrase_can_only_belong_to_one_trigger():
    out, err = ct.validate([TICKET, dict(MARKDOWN, phrases=["Ticket This"])])
    assert out is None and 'already used by the trigger "Bug ticket"' in err
    out, err = ct.validate([dict(TICKET, phrases=["prompt AI"])], builtin_phrases=BUILTIN)
    assert out is None and "built-in trigger phrase" in err


def test_one_distinctive_word_is_allowed_as_a_phrase():
    assert clean(dict(TICKET, phrases=["ticketize"]))[0]["phrases"] == ["ticketize"]


def test_temperature_is_clamped_and_optional():
    assert "temperature" not in clean(TICKET)[0]
    assert clean(dict(TICKET, temperature=9))[0]["temperature"] == ct.TEMP_MAX
    assert ct.temperature(clean(TICKET)[0]) == ct.TEMP_DEFAULT


def test_load_survives_a_hand_edited_file():
    assert ct.load({}) == [] and ct.load({"custom_triggers": "nope"}) == []
    assert ct.load({"custom_triggers": [{"name": "broken"}]}) == []
    assert ct.load({"custom_triggers": [TICKET]})[0]["id"] == "bug-ticket"


# --- matching --------------------------------------------------------------------

ITEMS = clean(TICKET, MARKDOWN)


@pytest.mark.parametrize("said, rest", [
    ("The login button is broken on mobile. Ticket this.", "The login button is broken on mobile."),
    ("Prompt ticket, the login button is broken", "the login button is broken"),
    ("PROMPT-TICKET the login is broken", "the login is broken"),
    ("The login is broken, ticket this, it only happens on mobile",
     "The login is broken it only happens on mobile"),
    ("The login is broken. Ticket this. It only happens on mobile.",
     "The login is broken. It only happens on mobile."),
    ("ticket this", ""),
])
def test_the_phrase_is_cut_out_and_the_rest_is_the_text(said, rest):
    item, got = ct.match(said, ITEMS)
    assert item["name"] == "Bug ticket" and got == rest


def test_no_match_inside_other_words_or_when_switched_off():
    assert ct.match("I bought a ticket thistle", ITEMS) is None
    assert ct.match("just a normal sentence", ITEMS) is None
    off = clean(dict(TICKET, enabled=False))
    assert ct.match("ticket this please", off) is None


def test_the_first_phrase_said_wins():
    item, _rest = ct.match("notes. output in markdown and also ticket this", ITEMS)
    assert item["name"] == "Markdown"


def test_messages():
    item = ITEMS[0]
    system = ct.system_prompt(item, ["Lesley", " ", "Ollama"])
    assert system.startswith("Rewrite the text as a bug ticket.")
    assert "Lesley, Ollama" in system and system.endswith(ct.RULES)
    assert "Spell these" not in ct.system_prompt(item)
    assert ct.user_message(" hello ") == "<text>\nhello\n</text>"
    assert ct.user_message("hello", "make it short").endswith(
        "</text>\n\n%s make it short" % ct.EXTRA_LABEL)


# --- the library files -------------------------------------------------------------

def _library():
    with open(os.path.join(ROOT, "trigger_library", "library.json"), encoding="utf-8") as f:
        return json.load(f)


def test_library_json_is_built_from_the_trigger_files():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "trigger_library", "build.py"),
                        "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_library_trigger_is_valid_and_can_be_added_next_to_the_built_ins():
    entries, err = ct.validate_library(_library())
    assert err is None and len(entries) >= 20
    installed, err = ct.validate([ct.from_library(e) for e in entries], builtin_phrases=BUILTIN)
    assert err is None, err                       # no phrase clashes anywhere in the library
    assert all(i["library_id"] == e["id"] and i["library_version"] == e["version"]
               for i, e in zip(installed, entries))


def test_library_phrases_never_fire_the_built_in_triggers_or_each_other():
    entries, _ = ct.validate_library(_library())
    pats = tr.patterns({"trigger_phrases": tr.DEFAULT_PHRASES})
    import re
    builtin = re.compile(r"\b(" + "|".join(pats["edit"] + pats["mean"]) + r")\b", re.I)
    items = [ct.from_library(e) for e in entries]
    for e in entries:
        for phrase in e["phrases"]:
            said = "some words here. %s." % phrase
            assert not builtin.search(said), phrase
            assert ct.match(said, items)[0]["library_id"] == e["id"], phrase


@pytest.mark.parametrize("change, words", [
    ({"id": "Bad Id"}, "lowercase words"),
    ({"description": ""}, "description"),
    ({"model": "huge"}, '"any" or "strong"'),
    ({"version": 0}, "version"),
])
def test_validate_library_rejects_a_bad_entry(change, words):
    entry = dict(dict(TICKET, id="ticket", description="d", model="any", version=1), **change)
    out, err = ct.validate_library({"triggers": [entry]})
    assert out is None and words in err


def test_validate_library_rejects_duplicate_ids_and_non_lists():
    entry = dict(TICKET, id="ticket", description="d", model="any", version=1)
    other = dict(MARKDOWN, id="ticket", description="d", model="any", version=1)
    assert "used twice" in ct.validate_library({"triggers": [entry, other]})[1]
    assert ct.validate_library({"triggers": "x"})[0] is None
    assert ct.validate_library([])[0] is None


# --- downloading the library ---------------------------------------------------------

class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_load_uses_the_online_library_and_sends_nothing_personal():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return _Reply(json.dumps(_library()).encode("utf-8"))
    lib = tl.load(opener=opener)
    assert lib["online"] and not lib["note"] and len(lib["entries"]) >= 20
    req = seen[0]
    assert req.full_url == tl.LIBRARY_URL and req.get_method() == "GET" and req.data is None
    assert dict(req.header_items()) == {"User-agent": "WhisperUnchained"}


@pytest.mark.parametrize("reply", [b"not json", b'{"triggers": [{"name": "x"}]}',
                                   b" " * (ct.LIBRARY_MAX_BYTES + 5)])
def test_load_falls_back_to_the_bundled_copy(reply):
    def opener(req, timeout):
        if reply is None:
            raise OSError("offline")
        return _Reply(reply)
    lib = tl.load(opener=opener)
    assert not lib["online"] and len(lib["entries"]) >= 20 and "came with" in lib["note"]


def test_load_offline_with_no_bundled_copy(tmp_path):
    def opener(req, timeout):
        raise OSError("offline")
    lib = tl.load(opener=opener, path=str(tmp_path / "missing.json"))
    assert lib["entries"] == [] and "internet" in lib["note"]


def test_installed_state_and_phrase_clashes():
    entries, _ = ct.validate_library(_library())
    entry = entries[0]
    assert tl.installed_state(entry, []) == "new"
    mine = ct.from_library(entry)
    assert tl.installed_state(entry, [mine]) == "added"
    assert tl.installed_state(dict(entry, version=entry["version"] + 1), [mine]) == "update"
    assert tl.phrase_clashes(entry, [mine]) == []                # its own phrases don't clash
    other = clean(dict(TICKET, phrases=[entry["phrases"][0].upper()]))
    assert tl.phrase_clashes(entry, other) == [entry["phrases"][0]]
    assert tl.phrase_clashes(entry, [], builtin_phrases=[entry["phrases"][0]]) == [entry["phrases"][0]]


def test_share_link_carries_only_the_trigger():
    item = clean(dict(TICKET, library_id="x", author="Someone", temperature=0.8))[0]
    url, fits = tl.share_link(item)
    assert fits and url.startswith(tl.SHARE_URL + "?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["title"] == ["New trigger: Bug ticket"] and q["labels"] == ["trigger"]
    shared = json.loads(q["body"][0].split("```json\n")[1].split("\n```")[0])
    assert shared == {"name": "Bug ticket", "description": "", "category": "", "model": "any",
                      "phrases": ["prompt ticket", "ticket this"], "temperature": 0.8,
                      "prompt": "Rewrite the text as a bug ticket."}


def test_share_link_for_a_long_trigger_asks_for_a_paste():
    item = clean(dict(TICKET, prompt="Do this carefully. " * 300))[0]
    url, fits = tl.share_link(item)
    assert not fits and len(url) < tl.SHARE_URL_MAX and "clipboard" in urllib.parse.unquote_plus(url)


# --- saving ------------------------------------------------------------------------

def test_save_writes_custom_triggers_and_keeps_everything_else(tmp_path):
    path = tmp_path / "processing_config.json"
    path.write_text(json.dumps({"vocabulary": ["Ollama"], "name_corrections": {"a b": "c"},
                                "insults_file_path": "insults.md",
                                "custom_triggers": [dict(MARKDOWN)]}), encoding="utf-8")
    wc.save(["Ollama"], {"a b": "c"}, path=str(path))            # custom not passed: untouched
    assert json.loads(path.read_text(encoding="utf-8"))["custom_triggers"] == [MARKDOWN]
    wc.save(["Ollama"], {"a b": "c"}, path=str(path), custom=clean(TICKET))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [t["name"] for t in data["custom_triggers"]] == ["Bug ticket"]
    assert data["insults_file_path"] == "insults.md" and data["vocabulary"] == ["Ollama"]
    assert ct.load(data) == clean(TICKET)


# --- the server ----------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    import api.whisper_api as wa
    sent = []

    def fake(system, user, fallback, temperature=0.2):
        sent.append({"system": system, "user": user, "temperature": temperature})
        return "RESULT", True
    monkeypatch.setattr(wa, "_call_edit_llm", fake)
    monkeypatch.setattr(wa, "_fastlog", lambda *a, **k: None)
    monkeypatch.setitem(wa.CONFIG, "name_corrections", {})
    monkeypatch.setitem(wa.CONFIG, "vocabulary", ["Ollama"])
    monkeypatch.setitem(wa.CONFIG, "trigger_phrases", list(tr.DEFAULT_PHRASES))
    monkeypatch.setitem(wa.CONFIG, "custom_triggers",
                        [dict(TICKET, temperature=0.8), dict(MARKDOWN)])
    return wa, sent


def test_server_runs_the_saved_instruction_on_the_rest_of_the_dictation(server):
    wa, sent = server
    assert wa.process_transcribed_text("The login is broken. Ticket this.") == "RESULT"
    assert sent[0]["user"] == "<text>\nThe login is broken.\n</text>"
    assert sent[0]["system"].startswith("Rewrite the text as a bug ticket.")
    assert "Ollama" in sent[0]["system"] and sent[0]["temperature"] == 0.8
    assert wa._trigger_fired("The login is broken. Ticket this.")      # so cleanup is skipped
    assert not wa._trigger_fired("The login is broken.")


def test_server_only_the_phrase_said_pastes_nothing(server):
    wa, sent = server
    assert wa.process_transcribed_text("Ticket this.") == "" and sent == []


def test_server_gives_the_dictation_back_without_the_phrase_when_the_ai_fails(server, monkeypatch):
    wa, _sent = server
    monkeypatch.setattr(wa, "_call_edit_llm", lambda s, u, fallback, temperature=0.2: (fallback, False))
    assert wa.process_transcribed_text("The login is broken. Ticket this.") == "The login is broken."


def test_server_built_in_phrase_said_first_decides(server, monkeypatch):
    wa, sent = server
    assert wa.process_transcribed_text("my notes prompt AI output in markdown please") == "RESULT"
    assert sent[-1]["user"].startswith("Editing instruction:")          # the "prompt AI" editor
    assert wa.process_transcribed_text("my notes. output in markdown. prompt AI") == "RESULT"
    assert sent[-1]["user"].startswith("<text>")                        # the custom trigger


def test_server_tidies_what_the_model_wraps_around_the_answer(server, monkeypatch):
    wa, _sent = server
    monkeypatch.setattr(wa, "_call_edit_llm", lambda s, u, fallback, temperature=0.2: (
        "<think>hmm</think>Here is the cleaned text: Title: Login broken", True))
    assert wa.process_transcribed_text("Login broken. Ticket this.") == "Title: Login broken"


def test_server_highlighted_text_plus_the_phrase(server, monkeypatch):
    wa, sent = server
    from fastapi.testclient import TestClient
    import test_v1_api as v1
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", False)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "")
    monkeypatch.setattr(wa, "DENOISE_ENABLED", False)
    monkeypatch.setattr(wa, "_run_pipeline_serialized",
                        lambda *a, **k: {"text": "Ticket this, and keep it short"})
    r = TestClient(wa.app).post(
        "/dictate/v1/edit_selection", data={"selection": "the login page crashes"},
        files={"file": ("a.wav", v1.WAV if hasattr(v1, "WAV") else b"RIFF0000WAVE", "audio/wav")})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "RESULT", "instruction": "Ticket this, and keep it short",
                        "edited": True}
    assert sent[0]["user"] == ("<text>\nthe login page crashes\n</text>\n\n%s and keep it short"
                               % ct.EXTRA_LABEL)


# --- the Settings screens ---------------------------------------------------------------

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Settings window")


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
def app(tmp_path, monkeypatch, tk_root):
    import settings_gui as sg
    src = os.path.join(ROOT, ".env")
    if not os.path.exists(src):
        pytest.skip("no .env on this machine")
    env = tmp_path / ".env"
    shutil.copy(src, env)                          # a temp copy; the real one is never written
    monkeypatch.setattr(sg, "ENV_PATH", str(env))
    words = tmp_path / "processing_config.json"
    data = json.load(open(sg.words_config.EXAMPLE_PATH, encoding="utf-8"))
    data["custom_triggers"] = [dict(MARKDOWN)]
    words.write_text(json.dumps(data, indent=2), encoding="utf-8")
    monkeypatch.setattr(sg.words_config, "CONFIG_PATH", str(words))
    opened = []
    monkeypatch.setattr(sg.webbrowser, "open", lambda url: opened.append(url) or True)
    root = tk.Toplevel(tk_root)
    root.withdraw()
    a = sg.SettingsApp(root)
    yield a, words, opened
    root.destroy()


@WINDOWS
def test_settings_lists_saved_triggers_and_saves_changes(app):
    a, words, _opened = app
    assert [t["name"] for t in a._custom] == ["Markdown"]
    entries, _ = ct.validate_library(_library())
    ticket = next(e for e in entries if e["id"] == "ticket")
    assert a._add_library_trigger(ticket) is None
    assert a._words_dirty and [t["name"] for t in a._custom] == ["Markdown", "Bug ticket"]
    a._custom[0]["enabled"] = False
    assert a.save() is True, a.status.cget("text")
    saved = ct.load(json.loads(words.read_text(encoding="utf-8")))
    assert [(t["name"], t["enabled"]) for t in saved] == [("Markdown", False), ("Bug ticket", True)]
    assert saved[1]["library_id"] == "ticket" and saved[1]["library_version"] == ticket["version"]


@WINDOWS
def test_settings_refuses_a_phrase_that_is_already_taken(app):
    a, words, _opened = app
    before = words.read_text(encoding="utf-8")
    clash = {"id": "md2", "name": "Other", "phrases": ["output in markdown"], "prompt": "x" * 20,
             "description": "d", "model": "any", "version": 1, "author": "a", "category": "c"}
    assert "already a phrase of another trigger" in a._add_library_trigger(clash)
    assert "built-in" in a._check_custom(clean(dict(TICKET, phrases=["prompt AI"]))[0])
    # A built-in phrase typed on the page after the trigger was added: Save says so.
    a._custom.append(clean(dict(TICKET, phrases=["jarvis please"]))[0])
    a._add_trigger_row("jarvis please", "edit", a._custom_changed)
    a._custom_changed()
    assert a.save() is False and "jarvis please" in a.status.cget("text")
    assert words.read_text(encoding="utf-8") == before


@WINDOWS
def test_settings_updating_a_library_trigger_replaces_it_and_keeps_it_switched_off(app):
    a, _words, _opened = app
    entries, _ = ct.validate_library(_library())
    ticket = next(e for e in entries if e["id"] == "ticket")
    a._add_library_trigger(ticket)
    a._custom[-1]["enabled"] = False
    newer = dict(ticket, version=ticket["version"] + 1, prompt=ticket["prompt"] + " More.")
    assert a._add_library_trigger(newer) is None
    assert len(a._custom) == 2 and a._custom[-1]["prompt"].endswith("More.")
    assert a._custom[-1]["enabled"] is False


@WINDOWS
def test_settings_share_opens_the_form_and_remove_works(app):
    a, _words, opened = app
    a._share_custom_trigger(a._custom[0])
    assert opened and opened[0].startswith(tl.SHARE_URL) and "Submit" in a.status.cget("text")
    a._remove_custom_trigger(a._custom[0])
    assert a._custom == [] and a._words_dirty


@WINDOWS
def test_editor_dialog_checks_and_returns_the_trigger(tk_root):
    import trigger_dialogs as td
    d = td.TriggerEditor(tk_root, None, check=lambda item: "taken" if "taken" in item["phrases"][0] else None)
    d.top.withdraw()
    d.ok()
    assert d.result is None and "needs a name" in d.status.cget("text")
    d.name.set("Bug ticket")
    d.phrases.insert("1.0", "taken phrase\n")
    d.prompt.insert("1.0", "Rewrite the text as a bug ticket.")
    d.ok()
    assert d.result is None and d.status.cget("text") == "taken"
    d.phrases.delete("1.0", "end")
    d.phrases.insert("1.0", "prompt ticket\n\nticket this\n")
    d.ok()
    assert d.result["phrases"] == ["prompt ticket", "ticket this"] and d.result["enabled"] is True


@WINDOWS
def test_editing_the_instruction_of_a_library_trigger_makes_it_your_own(tk_root):
    import trigger_dialogs as td
    entries, _ = ct.validate_library(_library())
    mine = ct.from_library(entries[0])
    d = td.TriggerEditor(tk_root, mine)
    d.top.withdraw()
    item, err = d.collect()
    assert err is None and item["library_id"] == entries[0]["id"]      # untouched: still linked
    d.prompt.insert("end", " And sign it Dom.")
    item, err = d.collect()
    assert err is None and "library_id" not in item and "library_version" not in item
    d.cancel()


@WINDOWS
def test_library_dialog_lists_filters_and_adds(tk_root):
    import trigger_dialogs as td
    entries, _ = ct.validate_library(_library())
    mine = []

    def on_add(entry):
        if entry["id"] == "commit":
            return "No thanks."
        mine.append(ct.from_library(entry))
        return None
    d = td.LibraryBrowser(tk_root, lambda: mine, on_add, load=lambda: {
        "entries": entries, "updated": "2026-11-16", "online": True, "note": ""})
    d.top.withdraw()
    d.start_loading()
    for _ in range(200):
        tk_root.update()
        if d.entries:
            break
    assert len(d.cards) == len(entries) and "updated 2026-11-16" in d.status.cget("text")
    d.add(next(e for e in entries if e["id"] == "ticket"))
    assert d.cards["ticket"]["state"] == "added" and d.cards["ticket"]["button"].cget("text") == "✓ Added"
    d.add(next(e for e in entries if e["id"] == "commit"))
    assert d.status.cget("text") == "No thanks." and d.cards["commit"]["state"] == "new"
    d.query.set("spanish")
    assert list(d.cards) == ["spanish"]
    d.query.set("zzzz")
    assert d.cards == {}
    d.close()


@WINDOWS
def test_library_dialog_says_when_it_is_showing_the_bundled_copy(tk_root):
    import trigger_dialogs as td
    d = td.LibraryBrowser(tk_root, lambda: [], lambda e: None, load=lambda: {
        "entries": [], "updated": "", "online": False, "note": "Couldn't reach the online library."})
    d.top.withdraw()
    d.start_loading()
    for _ in range(200):
        tk_root.update()
        if d.library is not None and "reach" in d.status.cget("text"):
            break
    assert "Couldn't reach" in d.status.cget("text")
    d.close()


# --- editing from the phone: /v1/custom_triggers ------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    """The server pointed at a temp word file (never the real one)."""
    import api.whisper_api as wa
    from fastapi.testclient import TestClient
    p = tmp_path / "processing_config.json"
    p.write_text(json.dumps({"vocabulary": ["Ollama"], "name_corrections": {"Leslie": "Lesley"},
                             "_comment": "keep", "trigger_phrases": list(tr.DEFAULT_PHRASES),
                             "custom_triggers": [dict(MARKDOWN, library_id="markdown",
                                                      library_version=1)]}), encoding="utf-8")
    monkeypatch.setattr(wa, "_CONFIG_PATH", str(p))
    monkeypatch.setattr(wa, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(wa, "_config_seen_mtime", None)
    monkeypatch.setattr(wa, "CONFIG", wa.CONFIG)
    monkeypatch.setattr(wa, "HOTWORDS", wa.HOTWORDS)
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", False)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "")
    return TestClient(wa.app), p, wa


URL = "/v1/custom_triggers"


def test_api_lists_triggers_and_limits(api):
    client, _p, _wa = api
    body = client.get(URL).json()
    assert [t["id"] for t in body["custom_triggers"]] == ["markdown"]
    assert body["limits"] == {"triggers": 100, "name": 40, "phrases": 6, "phrase": 60,
                              "prompt_min": 10, "prompt": 6000}


def test_api_add_edit_switch_off_and_remove(api):
    client, p, wa = api
    r = client.post(URL, json={"set": [TICKET]})
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["custom_triggers"]] == ["markdown", "bug-ticket"]
    assert wa._trigger_fired("it broke. ticket this")          # live at once, no restart
    # Change the instruction of the library one: it becomes the user's own.
    r = client.post(URL, json={"set": [{"id": "markdown", "prompt": "Markdown, with a title line.",
                                        "enabled": False}]})
    md = r.json()["custom_triggers"][0]
    assert r.json()["replaced"] == ["markdown"] and md["enabled"] is False
    assert md["prompt"].endswith("title line.") and md["phrases"] == ["output in markdown"]
    assert "library_id" not in md
    r = client.post(URL, json={"remove": ["bug-ticket", "nope"]})
    assert r.json()["removed"] == ["bug-ticket"] and len(r.json()["custom_triggers"]) == 1
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["_comment"] == "keep" and data["vocabulary"] == ["Ollama"]
    assert os.listdir(p.parent / "word_list_backups")           # replaced/removed: backed up first


@pytest.mark.parametrize("body, words", [
    ({}, "Send 1 to 20"),
    ({"set": [dict(TICKET, phrases=["prompt AI"])]}, "built-in"),
    ({"set": [dict(TICKET, phrases=["output in markdown"])]}, "already used"),
    ({"set": [dict(TICKET, prompt="")]}, "say what the AI should do"),
])
def test_api_refuses_bad_edits_and_changes_nothing(api, body, words):
    client, p, _wa = api
    before = p.read_text(encoding="utf-8")
    r = client.post(URL, json=body)
    assert r.status_code == 400 and words in r.json()["error"]["message"]
    assert p.read_text(encoding="utf-8") == before


def test_api_needs_the_key_when_auth_is_on(api, monkeypatch):
    client, _p, wa = api
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", True)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "wr_sec_test_key_123")
    assert client.get(URL).status_code == 401
    assert client.post(URL, json={"set": [TICKET]}).status_code == 401
