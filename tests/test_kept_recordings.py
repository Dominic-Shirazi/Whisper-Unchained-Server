"""A dictation the server can't take is kept and sent again later."""
import os
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import kept_recordings as kr  # noqa: E402
import transcribe_client as tc  # noqa: E402


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP %d" % self.status_code)


NOW = time.time()


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFF....WAVE")
    return str(p)


# --- the folder --------------------------------------------------------------

def test_keep_copies_and_lists_oldest_first(tmp_path, wav):
    folder = str(tmp_path / "kept")
    a = kr.keep(wav, folder, now=NOW)
    b = kr.keep(wav, folder, now=NOW + 60)
    assert kr.pending(folder) == [a, b]
    assert open(a, "rb").read() == b"RIFF....WAVE"
    assert os.path.exists(wav)                      # a copy: the original stays


def test_two_in_the_same_second_get_their_own_names(tmp_path, wav):
    folder = str(tmp_path / "kept")
    a = kr.keep(wav, folder, now=NOW)
    b = kr.keep(wav, folder, now=NOW)
    assert a != b and len(kr.pending(folder)) == 2


def test_keep_never_raises(tmp_path):
    assert kr.keep(str(tmp_path / "missing.wav"), str(tmp_path / "kept")) is None
    assert kr.pending(str(tmp_path / "no-such-folder")) == []


def test_prune_drops_the_old_and_the_overflow(tmp_path, wav):
    folder = str(tmp_path / "kept")
    now = NOW
    old = kr.keep(wav, folder, now=now)
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    fresh = [kr.keep(wav, folder, now=now + n) for n in range(1, 5)]
    kr.prune(folder, now=now + 10, max_files=3)
    assert kr.pending(folder) == fresh[1:]          # the 8-day-old one and the oldest extra gone


def test_messages():
    title, body = kr.kept_message("Couldn't reach this PC's server (ConnectionError).", True)
    assert title.startswith("Couldn't reach") and "kept" in body and "clipboard" in body
    assert "say it again" in kr.kept_message("x", False)[1]
    assert kr.ready_message(1)[0] == "Your earlier dictation is ready."
    assert kr.ready_message(3)[0] == "Your 3 earlier dictations are ready."
    assert "Ctrl+V" in kr.ready_message(1)[1] and "oldest first" in kr.ready_message(2)[1]
    assert kr.join_texts([" one ", "", "two"]) == "one\n\ntwo"


# --- which failures are worth another try --------------------------------------

def test_no_answer_is_unreachable(wav):
    def post(*a, **k):
        raise requests.ConnectionError("refused")
    with pytest.raises(tc.Unreachable, match="this PC's server"):
        tc.transcribe(wav, {}, post=post)


def test_paused_or_loading_is_unreachable(wav):
    with pytest.raises(tc.Unreachable, match="paused"):
        tc.transcribe(wav, {}, post=lambda *a, **k: Resp(503, {"detail": "Dictation is paused."}))
    env = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://10.0.0.5:5000/v1"}
    with pytest.raises(tc.Unreachable):
        tc.transcribe(wav, env, post=lambda *a, **k: Resp(503, {"error": {"message": "paused"}}))


def test_a_refused_request_is_not_kept(wav):
    env = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://10.0.0.5:5000/v1"}
    with pytest.raises(tc.TranscribeError) as e:
        tc.transcribe(wav, env, post=lambda *a, **k: Resp(401, {"error": {"message": "bad key"}}))
    assert not isinstance(e.value, tc.Unreachable)


# --- the cloud fallback ----------------------------------------------------------

CLOUD = {"CLOUD_BASE_URL": "https://api.example.com/v1", "CLOUD_MODEL": "whisper-x",
         "CLOUD_API_KEY": "k"}


def test_fallback_is_off_unless_switched_on_and_filled_in():
    assert not tc.fallback_wanted(dict(CLOUD))
    assert tc.fallback_wanted(dict(CLOUD, TRANSCRIBE_FALLBACK_CLOUD="true"))
    assert not tc.fallback_wanted({"TRANSCRIBE_FALLBACK_CLOUD": "true"})            # no cloud details
    assert not tc.fallback_wanted(dict(CLOUD, TRANSCRIBE_FALLBACK_CLOUD="true",
                                       TRANSCRIBE_SOURCE="cloud"))                  # already the cloud


def test_fallback_sends_that_dictation_to_the_cloud(wav, monkeypatch):
    monkeypatch.setattr(tc, "_words", lambda: ([], {}))
    urls = []

    def post(url, **k):
        urls.append(url)
        if "127.0.0.1" in url:
            raise requests.ConnectionError("refused")
        return Resp(200, {"text": "from the cloud"})
    env = dict(CLOUD, TRANSCRIBE_FALLBACK_CLOUD="true")
    assert tc.transcribe(wav, env, post=post) == "from the cloud"
    assert urls[-1] == "https://api.example.com/v1/audio/transcriptions"
    with pytest.raises(tc.Unreachable):                                             # off: kept instead
        tc.transcribe(wav, dict(CLOUD), post=post)


# --- sending them again -----------------------------------------------------------

def test_retry_sends_oldest_first_and_stops_while_the_server_is_away():
    dropped, calls = [], []

    def transcribe(path):
        calls.append(path)
        if path == "b":
            raise tc.Unreachable("still down")
        return "text of " + path
    texts, waiting = kr.retry_once(["a", "b", "c"], transcribe, dropped.append,
                                   unreachable=tc.Unreachable)
    assert texts == ["text of a"] and waiting == 2
    assert dropped == ["a"] and calls == ["a", "b"]           # c not tried, b and c kept


def test_retry_drops_a_recording_that_can_never_work():
    dropped, logged = [], []

    def transcribe(path):
        if path == "bad":
            raise tc.TranscribeError("The server PC said: unsupported file")
        return "" if path == "silent" else "ok"
    texts, waiting = kr.retry_once(["bad", "silent", "good"], transcribe, dropped.append,
                                   unreachable=tc.Unreachable, log=logged.append)
    assert texts == ["ok"] and waiting == 0
    assert dropped == ["bad", "silent", "good"]
    assert logged == ["kept recording dropped: TranscribeError"]   # never the text itself
