"""/dictate/v1/process_text: text another device transcribed itself gets the rest
of the dictation pipeline here (name corrections, spoken phrases and skills,
AI cleanup). No audio and no speech engine. The AI is faked."""
import pytest

TICKET = {"id": "ticket", "name": "Bug ticket", "phrases": ["prompt ticket"],
          "prompt": "Rewrite the text as a bug ticket.", "enabled": True}
URL = "/dictate/v1/process_text"


@pytest.fixture
def server(monkeypatch):
    from fastapi.testclient import TestClient
    import api.whisper_api as wa
    sent, logged = [], []

    def fake(system, user, fallback, temperature=0.2):
        sent.append({"system": system, "user": user, "fallback": fallback})
        return "RESULT", True
    monkeypatch.setattr(wa, "_call_edit_llm", fake)
    monkeypatch.setattr(wa, "_fastlog", lambda *a, **k: logged.append(" ".join(map(str, a))))
    monkeypatch.setattr(wa, "_log_transcript",
                        lambda *a, **k: pytest.fail("typed text must not reach the transcript log"))
    monkeypatch.setattr(wa, "_archive_audio",
                        lambda *a, **k: pytest.fail("there is no audio to keep"))
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", False)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "")
    monkeypatch.setattr(wa, "_reload_config_if_changed", lambda: None)
    monkeypatch.setattr(wa, "CLEANUP_ALWAYS", False)
    monkeypatch.setattr(wa, "REMOVE_FILLERS", False)
    monkeypatch.setattr(wa, "LOG_TRANSCRIPTS", False)
    monkeypatch.setattr(wa, "model", None)          # the speech engine is never needed
    monkeypatch.setitem(wa.PAUSE, "paused", False)
    monkeypatch.setitem(wa.CONFIG, "name_corrections", {"leslie": "Lesley"})
    monkeypatch.setitem(wa.CONFIG, "vocabulary", [])
    monkeypatch.setitem(wa.CONFIG, "trigger_phrases", list(wa.triggers.DEFAULT_PHRASES))
    monkeypatch.setitem(wa.CONFIG, "custom_triggers", [dict(TICKET)])
    return wa, sent, logged, TestClient(wa.app)


def test_plain_text_gets_the_word_list_and_no_ai_when_cleanup_is_off(server):
    wa, sent, logged, client = server
    r = client.post(URL, json={"text": "tell leslie i am late"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "tell Lesley i am late", "changed": True}
    assert sent == []
    r = client.post(URL, json={"text": "nothing to fix here"})
    assert r.json() == {"text": "nothing to fix here", "changed": False}
    # Only lengths reach the log, never the words.
    assert logged and not any("leslie" in line.lower() or "nothing to fix" in line for line in logged)


def test_cleanup_runs_when_asked_or_when_this_pc_always_cleans(server, monkeypatch):
    wa, sent, _logged, client = server
    r = client.post(URL, json={"text": "um so the meeting moved", "cleanup": "on",
                               "cleanup_tone": "professional"})
    assert r.json() == {"text": "RESULT", "changed": True}
    assert "the meeting moved" in sent[0]["user"]
    monkeypatch.setattr(wa, "CLEANUP_ALWAYS", True)
    assert client.post(URL, json={"text": "again please"}).json()["text"] == "RESULT"
    # The phone's own switch, off, wins over this PC's default.
    r = client.post(URL, json={"text": "leave me alone", "cleanup": "off"})
    assert r.json() == {"text": "leave me alone", "changed": False} and len(sent) == 2
    assert client.post(URL, json={"text": "clean this", "processing": "clean",
                                  "cleanup": "off"}).json()["text"] == "RESULT"


def test_a_spoken_catchphrase_or_a_chosen_skill_runs_instead_of_cleanup(server, monkeypatch):
    wa, sent, _logged, client = server
    monkeypatch.setattr(wa, "CLEANUP_ALWAYS", True)
    r = client.post(URL, json={"text": "the login page crashes prompt ticket"})
    assert r.json()["text"] == "RESULT" and len(sent) == 1
    assert sent[0]["system"].startswith("Rewrite the text as a bug ticket.")
    assert "the login page crashes" in sent[0]["user"]
    # Chosen from the list, its catchphrase not said.
    r = client.post(URL, json={"text": "the pay button does nothing", "trigger": "ticket"})
    assert r.json()["text"] == "RESULT" and len(sent) == 2
    assert sent[1]["system"].startswith("Rewrite the text as a bug ticket.")
    # An unknown skill id is ignored: an ordinary cleanup.
    client.post(URL, json={"text": "just words", "trigger": "no-such-skill"})
    assert len(sent) == 3 and not sent[2]["system"].startswith("Rewrite the text as a bug ticket.")


def test_the_text_comes_back_as_sent_when_the_ai_fails(server, monkeypatch):
    wa, _sent, _logged, client = server
    monkeypatch.setattr(wa, "_call_edit_llm",
                        lambda s, u, fallback, temperature=0.2: (fallback, False))
    r = client.post(URL, json={"text": "keep every word", "cleanup": "on"})
    assert r.json() == {"text": "keep every word", "changed": False}


@pytest.mark.parametrize("body, status, code", [
    ({}, 400, "missing_text"),
    ({"text": "   "}, 400, "missing_text"),
    ({"text": "x" * 20001}, 413, "text_too_long"),
    ({"text": "x", "processing": "raw"}, 400, "invalid_processing"),
    ({"text": "x", "cleanup": "maybe"}, 400, "invalid_cleanup_option"),
    ({"text": "x", "cleanup_tone": "shouty"}, 400, "invalid_cleanup_option"),
])
def test_bad_requests_are_refused(server, body, status, code):
    _wa, sent, _logged, client = server
    r = client.post(URL, json=body)
    assert r.status_code == status and r.json()["error"]["code"] == code and sent == []


def test_paused_and_key_rules_are_the_same_as_everything_else(server, monkeypatch):
    wa, _sent, _logged, client = server
    monkeypatch.setitem(wa.PAUSE, "paused", True)
    r = client.post(URL, json={"text": "hello"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "server_paused"
    monkeypatch.setitem(wa.PAUSE, "paused", False)
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", True)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "secret")
    assert client.post(URL, json={"text": "hello"}).status_code == 401
    assert client.post(URL, json={"text": "hello"},
                       headers={"Authorization": "Bearer secret"}).status_code == 200


def test_asking_the_ai_by_voice_logs_the_words_only_with_the_transcript_log_on(server, monkeypatch):
    wa, sent, logged, client = server
    r = client.post(URL, json={"text": "the secret plan is ready prompt AI make this formal"})
    assert r.json()["text"] == "RESULT" and len(sent) == 1
    assert any("EDIT trigger fired" in line for line in logged)
    assert not any("secret plan" in line or "make this formal" in line for line in logged)
    # The user turned the transcript log on: the diagnostic line may hold the words.
    monkeypatch.setattr(wa, "LOG_TRANSCRIPTS", True)
    client.post(URL, json={"text": "the secret plan is ready prompt AI make this formal"})
    assert any("secret plan" in line and "make this formal" in line for line in logged)
