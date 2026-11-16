"""The typed instruction (the floating mic button's right-click panel):
trigger suggestions and wording, snoozing the button, the client call and the
server address /dictate/v1/edit_text. No window is created."""
import os
import sys
import time

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only: floating mic button", allow_module_level=True)

import caret_button as cb
import prompt_panel as pp
import transcribe_client as tc

TICKET = {"id": "ticket", "name": "Bug ticket", "phrases": ["prompt ticket", "ticket this"],
          "prompt": "Rewrite the text as a bug ticket.", "enabled": True}
SHORTER = {"id": "shorter", "name": "Shorter", "phrases": ["prompt shorter"],
           "prompt": "Make the text shorter.", "enabled": True}
STANDUP = {"id": "standup", "name": "Stand-up update", "phrases": ["prompt stand up"],
           "prompt": "Rewrite as a stand-up update.", "enabled": True}
ALL = [TICKET, SHORTER, STANDUP]


def names(found):
    return [t["name"] for t in found]


# --- suggestions (like slash commands) ---------------------------------------

def test_typing_the_start_of_a_phrase_lists_matching_triggers():
    assert names(pp.suggest("pro", ALL)) == ["Bug ticket", "Shorter", "Stand-up update"]
    assert names(pp.suggest("prompt s", ALL)) == ["Shorter", "Stand-up update"]
    assert names(pp.suggest("Prompt   Sh", ALL)) == ["Shorter"]


def test_a_name_or_any_word_in_it_matches_too():
    assert names(pp.suggest("bug", ALL)) == ["Bug ticket"]
    assert names(pp.suggest("tick", ALL)) == ["Bug ticket"]       # phrase start comes first
    assert names(pp.suggest("up", ALL)) == ["Stand-up update"]    # a word inside
    assert names(pp.suggest("stand-up", ALL)) == ["Stand-up update"]


def test_a_slash_lists_everything_and_can_be_narrowed():
    assert names(pp.suggest("/", ALL)) == ["Bug ticket", "Shorter", "Stand-up update"]
    assert names(pp.suggest("/sh", ALL)) == ["Shorter"]
    assert len(pp.suggest("/", ALL * 5, limit=6)) == 6


def test_no_list_for_one_letter_an_ordinary_instruction_or_a_chosen_trigger():
    assert pp.suggest("", ALL) == [] and pp.suggest("p", ALL) == []
    assert pp.suggest("make it more formal", ALL) == []
    assert pp.suggest("prompt ticket", ALL) == []                 # the whole phrase: chosen
    assert pp.suggest("prompt ticket the login page", ALL) == []
    assert pp.suggest("first line\nprompt", ALL) == []
    assert pp.suggest("pro" + "x" * 50, ALL) == []
    assert pp.suggest("pro", []) == [] and pp.suggest("pro", None) == []


def test_accepting_a_suggestion_puts_its_first_phrase_in_the_box():
    assert pp.completion(TICKET) == "prompt ticket "
    assert pp.suggest(pp.completion(TICKET), ALL) == []


def test_chosen_finds_the_phrase_anywhere_in_what_was_typed():
    assert pp.chosen("prompt ticket the login page crashes", ALL) is TICKET
    assert pp.chosen("The login page crashes. Ticket this!", ALL) is TICKET
    assert pp.chosen("PROMPT  stand-up yesterday I shipped", ALL) is STANDUP
    assert pp.chosen("make it shorter", ALL) is None
    assert pp.chosen("prompt tickets", ALL) is None               # not the whole word


# --- wording and layout -----------------------------------------------------------

def test_labels():
    assert pp.include_label("see you tomorrow") == "Include the highlighted text (3 words)"
    assert pp.include_label("hello") == "Include the highlighted text (1 word)"
    assert pp.never_label("Notepad") == "Never in Notepad"
    assert pp.never_label("") == ""                               # no app known: no button
    long = pp.never_label("Microsoft Visual Studio Code Insiders")
    assert long.endswith("…") and len(long) <= len("Never in ") + 22
    assert pp.snooze_label(60) == "Snooze 1 hour" and pp.snooze_label(120) == "Snooze 2 hours"
    assert pp.snooze_label(15) == "Snooze 15 min"


def test_the_box_starts_at_one_line_and_stops_growing():
    assert pp.lines_shown(0) == 1 and pp.lines_shown(1) == 1
    assert pp.lines_shown(3) == 3
    assert pp.lines_shown(40) == pp.MAX_LINES


def test_panel_opens_below_the_pointer_and_stays_on_the_screen():
    work = (0, 0, 1920, 1040)
    assert pp.place_panel((500, 300), (400, 130), work) == (476, 312)
    x, y = pp.place_panel((1900, 300), (400, 130), work)          # right edge
    assert x + 400 <= 1920 - 12
    x, y = pp.place_panel((500, 1000), (400, 130), work)          # no room below: above
    assert y + 130 <= 1000
    x, y = pp.place_panel((5, 5), (400, 130), work)               # top-left corner
    assert x >= 12 and y >= 12
    # A second monitor to the left (negative coordinates).
    x, y = pp.place_panel((-1900, 300), (400, 130), (-1920, 0, 0, 1040))
    assert -1920 <= x and x + 400 <= 0


# --- snoozing the mic button -------------------------------------------------------

def test_snooze_runs_for_an_hour_then_ends():
    now = 1_800_000_000
    raw = cb.snooze_value(now)
    assert raw == str(now + 3600)
    assert cb.snoozed_until(raw, now + 10) == now + 3600
    assert cb.snoozed_until(raw, now + 3599) == now + 3600
    assert cb.snoozed_until(raw, now + 3600) == 0                 # over
    assert cb.snoozed_until('"%s"' % raw, now) == now + 3600      # quoted in .env


def test_no_snooze_from_a_missing_cleared_or_broken_value():
    now = 1_800_000_000
    for raw in (None, "", "0", "soon", str(now - 5), str(now + 30 * 24 * 3600)):
        assert cb.snoozed_until(raw, now) == 0


def test_snooze_is_read_from_env_and_switches_the_button_off(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FLOATING_MIC_BUTTON=true\n%s=%s\n" % (cb.SNOOZE_KEY, cb.snooze_value(time.time())))
    assert cb.snoozed_until(cb.read_config(str(env))["snooze"], time.time()) > time.time()
    btn = cb.CaretButton(on_click=lambda: None, env_path=str(env))
    assert btn._read_enabled() is False                           # snoozed = off for now
    env.write_text("FLOATING_MIC_BUTTON=true\n%s=0\n" % cb.SNOOZE_KEY)
    btn.refresh()
    assert btn._read_enabled() is True                            # woken from the tray
    env.write_text("FLOATING_MIC_BUTTON=false\n")
    btn.refresh()
    assert btn._read_enabled() is False


def test_clock_text():
    at = time.mktime((2026, 11, 16, 15, 5, 0, 0, 0, -1))
    assert cb.clock_text(at) == "3:05 PM"
    assert cb.clock_text(time.mktime((2026, 11, 16, 0, 40, 0, 0, 0, -1))) == "12:40 AM"


def test_right_click_goes_to_the_listener_only_while_idle(monkeypatch):
    seen, hidden = [], []
    btn = cb.CaretButton(on_click=lambda: None, enabled=True, on_menu=seen.append)
    monkeypatch.setattr(btn, "_hide", lambda: hidden.append(1))
    monkeypatch.setattr(btn, "_cancel_press", lambda: None)
    monkeypatch.setattr(cb.user32, "GetForegroundWindow", lambda: 0)
    monkeypatch.setattr(cb.user32, "CreatePopupMenu",
                        lambda: pytest.fail("the plain menu must not open"))
    btn._context_menu()
    assert seen == [{"hwnd": 0, "app": "", "label": ""}] and hidden == [1]
    btn._state = "recording"
    btn._context_menu()
    assert len(seen) == 1                                         # mid-recording: nothing


# --- the client call ------------------------------------------------------------

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def __call__(self, url, **kw):
        self.calls.append(dict(kw, url=url))
        return self.resp


def test_edit_text_posts_the_instruction_and_highlight_to_this_pc():
    post = FakeHttp(Resp(body={"text": "See you tomorrow.", "edited": True}))
    out = tc.edit_text({"WHISPER_API_PORT": "5055", "WHISPER_API_KEY": "k"},
                       "fix the spelling", "see you tmrw", post=post)
    assert out == ("See you tomorrow.", True)
    call = post.calls[0]
    assert call["url"] == "http://127.0.0.1:5055/dictate/v1/edit_text"
    assert call["json"] == {"instruction": "fix the spelling", "selection": "see you tmrw"}
    assert call["headers"] == {"Authorization": "Bearer k"}


def test_edit_text_on_another_pc_and_when_the_ai_gave_nothing():
    post = FakeHttp(Resp(body={"text": "", "edited": False}))
    out = tc.edit_text({"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://10.0.0.2:5000/v1/"},
                       "write a haiku", post=post)
    assert out == ("", False)
    assert post.calls[0]["url"] == "http://10.0.0.2:5000/dictate/v1/edit_text"
    assert post.calls[0]["json"]["selection"] == ""


def test_edit_text_says_so_for_a_cloud_service_or_an_old_server():
    with pytest.raises(tc.TranscribeError, match="cloud"):
        tc.edit_text({"TRANSCRIBE_SOURCE": "cloud"}, "x", post=FakeHttp(Resp()))
    with pytest.raises(tc.TranscribeError, match="too old"):
        tc.edit_text({}, "x", post=FakeHttp(Resp(status=404)))


def test_custom_triggers_for_the_panel_come_from_the_word_file_or_the_other_pc(tmp_path, monkeypatch):
    import json
    import words_config
    path = tmp_path / "processing_config.json"
    path.write_text(json.dumps({"custom_triggers": [TICKET, dict(SHORTER, enabled=False)]}))
    monkeypatch.setattr(words_config, "CONFIG_PATH", str(path))
    assert names(tc.custom_triggers({})) == ["Bug ticket"]        # switched-off ones left out
    get = FakeHttp(Resp(body={"custom_triggers": [STANDUP]}))
    remote = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://10.0.0.2:5000/v1",
              "REMOTE_API_KEY": "rk"}
    assert names(tc.custom_triggers(remote, get=get)) == ["Stand-up update"]
    assert get.calls[0]["url"] == "http://10.0.0.2:5000/v1/custom_triggers"
    assert tc.custom_triggers(remote, get=FakeHttp(Resp(status=401))) == []
    (tmp_path / "broken.json").write_text("{not json")
    monkeypatch.setattr(words_config, "CONFIG_PATH", str(tmp_path / "broken.json"))
    assert tc.custom_triggers({}) == []                           # never raises


# --- the listener's words ----------------------------------------------------------

def test_failure_notice_says_what_happened_to_the_text():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "client", "background_listener.py"), encoding="utf-8").read()
    ns = {}
    start = src.index("def after_highlight")
    exec(src[start:src.index("\ndef _typed_instruction", start)], ns)
    assert ns["after_highlight"]("I agree.", "see you tmrw.") == " I agree."
    assert ns["after_highlight"]("I agree.", "see you tmrw.\n") == "I agree."
    assert ns["after_highlight"]("\nI agree.", "see you tmrw.") == "\nI agree."
    assert ns["after_highlight"]("I agree.", "") == "I agree."
    title, body = ns["typed_failure_message"](True)
    assert "didn't answer" in title and "wasn't changed" in body and "right-click" in body
    assert "Nothing was typed" in ns["typed_failure_message"](False)[1]


# --- the server address ------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    from fastapi.testclient import TestClient
    import api.whisper_api as wa
    import triggers as tr
    sent = []

    def fake(system, user, fallback, temperature=0.2):
        sent.append({"system": system, "user": user, "fallback": fallback})
        return "RESULT", True
    monkeypatch.setattr(wa, "_call_edit_llm", fake)
    monkeypatch.setattr(wa, "_fastlog", lambda *a, **k: None)
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", False)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "")
    monkeypatch.setattr(wa, "_reload_config_if_changed", lambda: None)
    monkeypatch.setitem(wa.PAUSE, "paused", False)
    monkeypatch.setitem(wa.CONFIG, "name_corrections", {})
    monkeypatch.setitem(wa.CONFIG, "vocabulary", [])
    monkeypatch.setitem(wa.CONFIG, "trigger_phrases", list(tr.DEFAULT_PHRASES))
    monkeypatch.setitem(wa.CONFIG, "custom_triggers", [dict(TICKET)])
    return wa, sent, TestClient(wa.app)


URL = "/dictate/v1/edit_text"


def test_server_typed_instruction_changes_the_highlight(server):
    wa, sent, client = server
    r = client.post(URL, json={"instruction": "make it formal", "selection": "see you tmrw"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "RESULT", "instruction": "make it formal", "edited": True}
    assert "make it formal" in sent[0]["user"] and "see you tmrw" in sent[0]["user"]
    assert sent[0]["fallback"] == "see you tmrw"


def test_server_typed_instruction_alone_writes_new_text(server):
    wa, sent, client = server
    r = client.post(URL, json={"instruction": "write a polite no to a meeting"})
    assert r.json() == {"text": "RESULT", "instruction": "write a polite no to a meeting",
                        "edited": True}
    assert sent[0]["user"] == "write a polite no to a meeting"


def test_server_typed_trigger_phrase_uses_the_saved_instruction(server):
    wa, sent, client = server
    # With a highlight: the trigger works on it, the rest is a one-off extra.
    r = client.post(URL, json={"instruction": "prompt ticket keep it short",
                               "selection": "the login page crashes"})
    assert r.json()["text"] == "RESULT" and r.json()["edited"] is True
    assert sent[0]["system"].startswith("Rewrite the text as a bug ticket.")
    assert "<text>\nthe login page crashes\n</text>" in sent[0]["user"]
    assert "keep it short" in sent[0]["user"]
    # Without one: what was typed after the phrase is the text.
    r = client.post(URL, json={"instruction": "prompt ticket the login page crashes"})
    assert r.json()["edited"] is True
    assert sent[1]["user"] == "<text>\nthe login page crashes\n</text>"
    # Only the phrase and nothing to work on: nothing happens.
    r = client.post(URL, json={"instruction": "prompt ticket"})
    assert r.json() == {"text": "", "instruction": "prompt ticket", "edited": False}
    assert len(sent) == 2


def test_server_gives_the_highlight_back_or_nothing_when_the_ai_fails(server, monkeypatch):
    wa, _sent, client = server
    monkeypatch.setattr(wa, "_call_edit_llm", lambda s, u, fallback, temperature=0.2: (fallback, False))
    r = client.post(URL, json={"instruction": "make it formal", "selection": "keep me"})
    assert r.json()["text"] == "keep me" and r.json()["edited"] is False
    r = client.post(URL, json={"instruction": "write a haiku"})
    assert r.json()["text"] == "" and r.json()["edited"] is False
    r = client.post(URL, json={"instruction": "prompt ticket the login page crashes"})
    assert r.json()["text"] == "" and r.json()["edited"] is False


def test_server_refuses_an_empty_or_oversized_typed_instruction(server):
    wa, sent, client = server
    assert client.post(URL, json={"instruction": "  "}).json()["error"]["code"] == "missing_instruction"
    r = client.post(URL, json={"instruction": "x" * (wa.INSTRUCTION_MAX_CHARS + 1)})
    assert r.status_code == 413 and r.json()["error"]["code"] == "instruction_too_long"
    r = client.post(URL, json={"instruction": "x", "selection": "y" * (wa.SELECTION_MAX_CHARS + 1)})
    assert r.status_code == 413
    assert sent == []
    assert pp.INSTRUCTION_MAX == wa.INSTRUCTION_MAX_CHARS      # the box stops where the server does


def test_server_typed_instruction_waits_while_paused_and_needs_the_key(server, monkeypatch):
    wa, sent, client = server
    monkeypatch.setitem(wa.PAUSE, "paused", True)
    assert client.post(URL, json={"instruction": "x"}).status_code == 503
    monkeypatch.setitem(wa.PAUSE, "paused", False)
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", True)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "wr_sec_test_key_123")
    assert client.post(URL, json={"instruction": "x"}).status_code == 401
    assert sent == []
