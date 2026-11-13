"""Per-app cleanup styles: the rules file and what one dictation sends."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import app_styles  # noqa: E402
import transcribe_client as tc  # noqa: E402
from test_transcribe_client import FakePost, Resp  # noqa: E402


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFF....WAVE")
    return str(p)


def test_app_names_match_however_they_are_written():
    for raw in ("Slack.exe", "slack", r"C:\Users\x\AppData\Local\slack\slack.exe", ' "SLACK.EXE" '):
        assert app_styles.normalize_app(raw) == "slack.exe"
    assert app_styles.normalize_app("") == ""


def test_clean_drops_bad_values_and_duplicates():
    rules = app_styles.clean([
        {"app": "Slack", "tone": "Casual", "emoji": "on", "rewrite": "bogus"},
        {"app": "slack.exe", "tone": "professional"},      # duplicate: first wins
        {"app": "", "tone": "casual"},                     # no app
        {"app": "code.exe", "cleanup": "off"},
        "not a rule",
    ])
    assert rules == [{"app": "slack.exe", "tone": "casual", "emoji": "on"},
                     {"app": "code.exe", "cleanup": "off"}]


def test_save_and_load_round_trip(tmp_path):
    path = str(tmp_path / "app_styles.json")
    app_styles.save([{"app": "Outlook", "cleanup": "on", "tone": "professional"}], path)
    assert app_styles.load(path) == [{"app": "outlook.exe", "cleanup": "on",
                                      "tone": "professional"}]
    assert json.load(open(path, encoding="utf-8"))[0]["app"] == "outlook.exe"


def test_missing_or_broken_file_means_no_rules(tmp_path):
    assert app_styles.load(str(tmp_path / "nope.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert app_styles.load(str(bad)) == []


def test_request_fields():
    rules = app_styles.clean([{"app": "slack.exe", "cleanup": "on", "tone": "casual",
                               "shorten": "light"}])
    rule = app_styles.rule_for("Slack.exe", rules)
    assert app_styles.request_fields(rule) == {"cleanup": "on", "cleanup_tone": "casual",
                                               "cleanup_shorten": "light"}
    assert app_styles.rule_for("notepad.exe", rules) is None
    assert app_styles.request_fields(None) == {}


def test_without_a_style_the_hotkey_path_is_unchanged(wav):
    post = FakePost(Resp(body={"text": "hi"}))
    assert tc.transcribe(wav, {}, post=post, style={}) == "hi"
    assert post.calls[0]["url"].endswith("/transcribe")


def test_a_style_goes_to_the_dictation_address(wav):
    post = FakePost(Resp(body={"text": "hey!"}))
    assert tc.transcribe(wav, {"WHISPER_API_PORT": "5055"}, post=post,
                         style={"cleanup": "on", "cleanup_tone": "casual"}) == "hey!"
    call = post.calls[0]
    assert call["url"] == "http://127.0.0.1:5055/dictate/v1/audio/transcriptions"
    assert call["data"] == {"response_format": "json", "processing": "full",
                            "cleanup": "on", "cleanup_tone": "casual"}


def test_an_older_server_falls_back_to_plain_dictation(wav):
    class TwoStep(FakePost):
        def __call__(self, url, **kw):
            super().__call__(url, **kw)
            return Resp(404) if "/dictate/" in url else Resp(body={"text": "ok"})
    post = TwoStep(None)
    assert tc.transcribe(wav, {}, post=post, style={"cleanup": "off"}) == "ok"
    assert [c["url"].rsplit("/", 1)[-1] for c in post.calls] == ["transcriptions", "transcribe"]


def test_a_server_error_with_a_style_is_readable(wav):
    post = FakePost(Resp(503, {"error": {"message": "Dictation is paused on the PC."}}))
    with pytest.raises(tc.TranscribeError, match="paused"):
        tc.transcribe(wav, {}, post=post, style={"cleanup": "on"})


def test_remote_sends_the_style_too(wav):
    post = FakePost(Resp(body={"text": "x"}))
    tc.transcribe(wav, {"TRANSCRIBE_SOURCE": "remote",
                        "REMOTE_SERVER_URL": "http://10.0.0.2:5000/v1"},
                  post=post, style={"cleanup_tone": "friendly"})
    assert post.calls[0]["data"]["cleanup_tone"] == "friendly"
    assert post.calls[0]["data"]["processing"] == "full"


# --- Voice-edit highlighted text (client side) ---------------------------------

def test_edit_selection_posts_highlight_to_this_pc(wav):
    post = FakePost(Resp(body={"text": "Thanks so much!", "edited": True}))
    out = tc.edit_selection(wav, {"WHISPER_API_PORT": "5055"}, "thx", post=post)
    assert out == "Thanks so much!"
    assert post.calls[0]["url"] == "http://127.0.0.1:5055/dictate/v1/edit_selection"
    assert post.calls[0]["data"] == {"selection": "thx"}


def test_edit_selection_on_another_pc_uses_its_dictate_address(wav):
    post = FakePost(Resp(body={"text": "x"}))
    tc.edit_selection(wav, {"TRANSCRIBE_SOURCE": "remote",
                            "REMOTE_SERVER_URL": "http://10.0.0.2:5000/v1/"}, "y", post=post)
    assert post.calls[0]["url"] == "http://10.0.0.2:5000/dictate/v1/edit_selection"


def test_edit_selection_not_with_a_cloud_service(wav):
    with pytest.raises(tc.TranscribeError, match="cloud"):
        tc.edit_selection(wav, {"TRANSCRIBE_SOURCE": "cloud"}, "y", post=FakePost(Resp()))


def test_a_fully_highlighted_short_box_is_typed_over_not_edited():
    import text_focus
    # One click in an address bar highlights all of it: dictating replaces it.
    assert text_focus.replaces_whole_box("github.com/x", "github.com/x")
    # Part of a box, or a whole multi-line message, is edited by voice.
    assert not text_focus.replaces_whole_box("see you", "see you tomorrow")
    assert not text_focus.replaces_whole_box("Hi,\nsee you", "Hi,\nsee you")
    assert not text_focus.replaces_whole_box("x", None)       # unknown: allow
