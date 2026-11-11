"""Desktop transcription sources: this PC, another PC, a cloud service."""
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import transcribe_client as tc  # noqa: E402


class Resp:
    def __init__(self, status=200, body=None, ctype="application/json"):
        self.status_code, self._body = status, body or {}
        self.headers = {"content-type": ctype}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP %d" % self.status_code)


class FakePost:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def __call__(self, url, files=None, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data or {}, "headers": headers or {}})
        return self.resp


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFF....WAVE")
    return str(p)


@pytest.fixture
def words(monkeypatch):
    monkeypatch.setattr(tc, "_words", lambda: (["Ollama"], {"Leslie": "Lesley"}))


def test_local_default_and_port(wav):
    post = FakePost(Resp(body={"text": " hi "}))
    assert tc.transcribe(wav, {"WHISPER_API_PORT": "5055"}, post=post) == "hi"
    assert post.calls[0]["url"] == "http://127.0.0.1:5055/transcribe"


def test_legacy_whisper_api_url_still_works(wav):
    post = FakePost(Resp(body={"text": "x"}))
    tc.transcribe(wav, {"WHISPER_API_URL": "http://100.1.2.3:5000/transcribe"}, post=post)
    assert post.calls[0]["url"] == "http://100.1.2.3:5000/transcribe"


def test_local_paused_message_passes_through(wav):
    post = FakePost(Resp(503, {"detail": "Dictation is paused on the PC (for gaming)."}))
    with pytest.raises(tc.TranscribeError, match="paused"):
        tc.transcribe(wav, {}, post=post)


def test_remote_uses_v1_and_key(wav):
    post = FakePost(Resp(body={"text": "from head pc"}))
    env = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://100.1.2.3:5000/v1/",
           "REMOTE_API_KEY": "wr_sec_abc"}
    assert tc.transcribe(wav, env, post=post) == "from head pc"
    c = post.calls[0]
    assert c["url"] == "http://100.1.2.3:5000/v1/audio/transcriptions"
    assert c["headers"] == {"Authorization": "Bearer wr_sec_abc"}


def test_remote_error_is_readable(wav):
    post = FakePost(Resp(401, {"error": {"message": "Invalid or missing API key."}}))
    env = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://x/v1"}
    with pytest.raises(tc.TranscribeError, match="Invalid or missing API key"):
        tc.transcribe(wav, env, post=post)


def test_cloud_sends_model_and_hints_then_corrects_locally(wav, words):
    post = FakePost(Resp(body={"text": "Um, leslie uses ollama."}))
    env = {"TRANSCRIBE_SOURCE": "cloud", "CLOUD_BASE_URL": "https://api.groq.com/openai/v1",
           "CLOUD_MODEL": "whisper-large-v3-turbo", "CLOUD_API_KEY": "gsk_x",
           "REMOVE_FILLERS": "true"}
    assert tc.transcribe(wav, env, post=post) == "Lesley uses ollama."
    c = post.calls[0]
    assert c["data"]["model"] == "whisper-large-v3-turbo"
    assert c["data"]["prompt"] == "Ollama, Lesley"
    assert c["headers"]["Authorization"] == "Bearer gsk_x"


def test_cloud_not_set_up(wav, words):
    with pytest.raises(tc.TranscribeError, match="isn't set up"):
        tc.transcribe(wav, {"TRANSCRIBE_SOURCE": "cloud"}, post=FakePost(Resp()))


def test_unreachable_names_the_place(wav):
    def post(*a, **k):
        raise requests.ConnectionError("boom")
    with pytest.raises(tc.TranscribeError, match="the server PC"):
        tc.transcribe(wav, {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://x/v1"}, post=post)


def test_unknown_source_falls_back_to_local(wav):
    post = FakePost(Resp(body={"text": "x"}))
    tc.transcribe(wav, {"TRANSCRIBE_SOURCE": "mars"}, post=post)
    assert post.calls[0]["url"].startswith("http://127.0.0.1:")


@pytest.mark.parametrize("status,ok,msg", [(200, True, "Connected"), (401, False, "rejected"),
                                           (500, False, "answered")])
def test_check_connection(status, ok, msg):
    seen = {}

    def get(url, headers=None, timeout=None):
        seen["url"] = url
        return Resp(status, {"error": {"message": "boom"}})
    env = {"TRANSCRIBE_SOURCE": "cloud", "CLOUD_BASE_URL": "https://api.openai.com/v1"}
    result = tc.check_connection(env, get=get)
    assert result[0] is ok and msg in result[1]
    assert seen["url"] == "https://api.openai.com/v1/models"


def test_name_corrections_literal():
    assert tc.apply_name_corrections("st. louis and C++", {"St. Louis": "Saint Louis", "C++": "C#"}) \
        == "Saint Louis and C#"
