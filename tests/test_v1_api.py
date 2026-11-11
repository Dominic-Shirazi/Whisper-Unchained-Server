"""Tests for the OpenAI-compatible routes, auth, and /health
(contract: WhisperRemote contracts/01-transcription-api.md).

The real Whisper model never loads: faster_whisper.WhisperModel is swapped for a
fake before the API module imports, so these run in a second with no GPU.

    .venv\\Scripts\\python.exe -m pytest tests -q
"""
import io
import os
import sys
import types
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CALLS = []  # every model.transcribe() call: (path, kwargs)


class FakeWhisperModel:
    def __init__(self, *a, **kw):
        pass

    def transcribe(self, path, **kwargs):
        CALLS.append((path, kwargs))
        seg = SimpleNamespace(start=0.0, end=1.2, text=" hello lesley",
                              avg_logprob=-0.2, no_speech_prob=0.01,
                              compression_ratio=1.1)
        return iter([seg]), SimpleNamespace(language="en", duration=1.2,
                                            language_probability=0.99)


fake_fw = types.ModuleType("faster_whisper")
fake_fw.WhisperModel = FakeWhisperModel
sys.modules["faster_whisper"] = fake_fw

from fastapi.testclient import TestClient  # noqa: E402

import api.whisper_api as wa  # noqa: E402

# The real fast-path layer, saved before the autouse fixture stubs it out.
REAL_PROCESS = wa.process_transcribed_text

KEY = "wr_sec_test_key_123"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(wa, "DENOISE_ENABLED", False)
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "")
    monkeypatch.setattr(wa, "HOTWORDS", "Lesley, Ame")
    # No corpus files or log lines from tests.
    monkeypatch.setattr(wa, "_archive_audio", lambda *a, **k: None)
    monkeypatch.setattr(wa, "_log_transcript", lambda *a, **k: None)
    # Stand-in for the fast-path layer, so tests can see whether it ran.
    monkeypatch.setattr(wa, "process_transcribed_text", lambda t: t.upper())
    # Every test must clean up its temp upload.
    before = set(os.listdir(wa.TEMP_DIR))
    yield
    leaked = set(os.listdir(wa.TEMP_DIR)) - before
    assert not leaked, f"temp files left behind: {leaked}"


client = TestClient(wa.app)


def _post(data=None, headers=None, with_file=True):
    files = {"file": ("clip.wav", io.BytesIO(b"RIFF....WAVE"), "audio/wav")} if with_file else None
    return client.post("/v1/audio/transcriptions", data=data or {}, files=files, headers=headers)


# --- /v1/audio/transcriptions -------------------------------------------------

def test_json_default_runs_full_processing():
    r = _post()
    assert r.status_code == 200
    assert r.json() == {"text": "HELLO LESLEY"}


def test_raw_processing_skips_fast_path():
    r = _post({"processing": "raw"})
    assert r.json() == {"text": "hello lesley"}


def test_text_format_is_plain():
    r = _post({"response_format": "text"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "HELLO LESLEY"


def test_verbose_json_shape():
    r = _post({"response_format": "verbose_json"})
    body = r.json()
    assert body["task"] == "transcribe"
    assert body["language"] == "en"
    assert body["text"] == "HELLO LESLEY"
    assert body["raw_text"] == "hello lesley"
    assert body["model"] == wa.MODEL_SIZE
    assert body["segments"][0]["text"] == " hello lesley"
    assert set(body["segments"][0]) >= {"id", "start", "end", "text", "avg_logprob", "no_speech_prob"}


def test_openai_fields_accepted_and_model_ignored():
    r = _post({"model": "whisper-1", "temperature": "0.2"})
    assert r.status_code == 200


def test_prompt_merges_with_server_vocab_server_first():
    _post({"prompt": "Kubernetes, Lesley, Ollama"})
    hotwords = CALLS[-1][1]["hotwords"]
    assert hotwords == "Lesley, Ame, Kubernetes, Ollama"


def test_no_prompt_uses_server_vocab_only():
    _post()
    assert CALLS[-1][1]["hotwords"] == "Lesley, Ame"


def test_language_passed_through():
    _post({"language": "en"})
    assert CALLS[-1][1]["language"] == "en"
    _post()
    assert CALLS[-1][1]["language"] is None


@pytest.mark.parametrize("data,code", [
    ({"response_format": "srt"}, "invalid_response_format"),
    ({"processing": "fancy"}, "invalid_processing"),
])
def test_bad_params_400_openai_shape(data, code):
    r = _post(data)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code


def test_missing_file_400():
    r = _post(with_file=False)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_file"


def test_model_not_loaded_503_with_retry_after(monkeypatch):
    monkeypatch.setattr(wa, "model", None)
    r = _post()
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "model_unavailable"
    assert r.headers["retry-after"] == "30"


def test_pipeline_crash_500_openai_shape(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cuda fell over")
    monkeypatch.setattr(wa, "_run_pipeline", boom)
    r = _post()
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "server_error"


def test_upload_name_cannot_escape_temp_dir():
    files = {"file": ("..\\..\\evil.wav", io.BytesIO(b"x"), "audio/wav")}
    client.post("/v1/audio/transcriptions", files=files)
    saved = CALLS[-1][0]
    assert os.path.dirname(os.path.abspath(saved)) == os.path.abspath(wa.TEMP_DIR)


# --- legacy /transcribe unchanged -----------------------------------------------

def test_legacy_transcribe_shape_unchanged():
    files = {"file": ("clip.wav", io.BytesIO(b"x"), "audio/wav")}
    r = client.post("/transcribe", files=files)
    assert r.status_code == 200
    assert r.json() == {"text": "HELLO LESLEY", "language": "en", "duration": 1.2}
    assert CALLS[-1][1]["hotwords"] == "Lesley, Ame"


# --- /v1/models, /health ------------------------------------------------------

def test_models_lists_loaded_model():
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == wa.MODEL_SIZE


def test_health_stable_fields():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["api_version"] == 1
    assert body["auth_required"] is False
    assert body["model"] == wa.MODEL_SIZE


def test_health_reports_error_when_model_missing(monkeypatch):
    monkeypatch.setattr(wa, "model", None)
    assert client.get("/health").json()["status"] == "error"


# --- auth -------------------------------------------------------------------

@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(wa, "WHISPER_API_KEY", KEY)


def test_health_open_even_with_auth(auth_on):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


@pytest.mark.parametrize("headers", [
    None,
    {"Authorization": "Bearer wrong"},
    {"Authorization": KEY},                 # missing scheme
    {"Authorization": f"Basic {KEY}"},      # wrong scheme
])
def test_bad_or_missing_key_401(auth_on, headers):
    r = _post(headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"
    assert CALLS == []  # rejected before any GPU work


def test_good_key_passes(auth_on):
    r = _post(headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    assert client.get("/v1/models", headers={"Authorization": f"bearer {KEY}"}).status_code == 200


def test_auth_covers_legacy_and_models(auth_on):
    files = {"file": ("clip.wav", io.BytesIO(b"x"), "audio/wav")}
    assert client.post("/transcribe", files=files).status_code == 401
    assert client.get("/v1/models").status_code == 401
    assert client.post("/warmup").status_code == 401


# --- filler removal (REMOVE_FILLERS) ----------------------------------------

@pytest.fixture
def um_audio(monkeypatch):
    """Whisper 'hears' a filler; capture what gets logged."""
    real = wa._decode

    def decode(path, prompt=None, language=None):
        r = real(path, prompt=prompt, language=language)
        r["text"] = r["raw_text"] = "Um, hello, uh, there."
        return r
    monkeypatch.setattr(wa, "_decode", decode)
    monkeypatch.setattr(wa, "process_transcribed_text", lambda t: t)
    logged = {}
    monkeypatch.setattr(wa, "_log_transcript",
                        lambda *a, **k: logged.update(final=a[1], fillers=k.get("fillers")))
    return logged


def test_fillers_kept_when_off(um_audio):
    assert _post().json()["text"] == "Um, hello, uh, there."


def test_fillers_removed_and_logged_when_on(um_audio, monkeypatch):
    monkeypatch.setattr(wa, "REMOVE_FILLERS", True)
    assert _post().json()["text"] == "Hello there."
    assert [w.lower() for w in um_audio["fillers"]] == ["um", "uh"]


def test_raw_processing_keeps_fillers_even_when_on(um_audio, monkeypatch):
    monkeypatch.setattr(wa, "REMOVE_FILLERS", True)
    assert _post({"processing": "raw"}).json()["text"] == "Um, hello, uh, there."


# --- name corrections (server) ---------------------------------------------------

def test_name_corrections_literal_and_whole_word(monkeypatch):
    monkeypatch.setitem(wa.CONFIG, "name_corrections",
                        {"Leslie": "Lesley", "St. Louis": "Saint Louis", "C++": "C plus plus"})
    monkeypatch.setitem(wa.CONFIG, "trigger_patterns", [])
    monkeypatch.setitem(wa.CONFIG, "roast_trigger_patterns", [])
    out = REAL_PROCESS("leslie flew to St. Louis to teach C++. Lesliex stays. StX Louis stays.")
    assert out == "Lesley flew to Saint Louis to teach C plus plus. Lesliex stays. StX Louis stays."


# --- shared vocabulary (GET/POST /v1/vocabulary) ------------------------------

@pytest.fixture
def vocab_file(tmp_path, monkeypatch):
    """Point the server's word lists at a temp file (never the real one)."""
    import json as _json
    p = tmp_path / "processing_config.json"
    p.write_text(_json.dumps({"vocabulary": ["Ollama"], "name_corrections": {"Leslie": "Lesley"},
                              "trigger_patterns": ["keep me"], "_comment": "keep"}), encoding="utf-8")
    monkeypatch.setattr(wa, "_CONFIG_PATH", str(p))
    monkeypatch.setattr(wa, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(wa, "_config_seen_mtime", None)   # force a reload from the temp file
    monkeypatch.setattr(wa, "CONFIG", wa.CONFIG)
    monkeypatch.setattr(wa, "HOTWORDS", wa.HOTWORDS)
    return p


def test_get_vocabulary(vocab_file):
    body = client.get("/v1/vocabulary").json()
    assert body["vocabulary"] == ["Ollama"]
    assert body["from_name_corrections"] == ["Lesley"]
    assert body["max_chars"] == 540 and body["chars"] == len("Ollama, Lesley")


def test_post_vocabulary_adds_dedupes_and_persists(vocab_file):
    import json as _json
    r = client.post("/v1/vocabulary", json={"add": ["Kubernetes", "ollama", "lesley", "Kubernetes"]})
    assert r.status_code == 200
    assert r.json()["added"] == ["Kubernetes"]
    on_disk = _json.loads(vocab_file.read_text(encoding="utf-8"))
    assert on_disk["vocabulary"] == ["Ollama", "Kubernetes"]
    assert on_disk["trigger_patterns"] == ["keep me"] and on_disk["_comment"] == "keep"
    assert "Kubernetes" in wa.HOTWORDS          # live, no restart


@pytest.mark.parametrize("bad", [{"add": []}, {"add": ["x" * 61]}, {"add": ["a, b"]},
                                 {"add": ["w%d" % i for i in range(51)]}])
def test_post_vocabulary_rejects_bad_input(vocab_file, bad):
    r = client.post("/v1/vocabulary", json=bad)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_vocabulary"


def test_vocabulary_needs_key_when_auth_on(vocab_file, monkeypatch):
    monkeypatch.setattr(wa, "WHISPER_API_KEY", KEY)
    assert client.get("/v1/vocabulary").status_code == 401
    assert client.post("/v1/vocabulary", json={"add": ["x"]}).status_code == 401


def test_external_edit_reloads_without_restart(vocab_file):
    import json as _json, os as _os, time as _time
    client.get("/v1/vocabulary")                       # load the temp file
    data = _json.loads(vocab_file.read_text(encoding="utf-8"))
    data["vocabulary"].append("Tailscale")
    vocab_file.write_text(_json.dumps(data), encoding="utf-8")
    _os.utime(vocab_file, (_time.time() + 5, _time.time() + 5))  # make sure mtime moves
    assert "Tailscale" in client.get("/v1/vocabulary").json()["vocabulary"]


def test_prompt_merge_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(wa, "HOTWORDS", "Ollama, Lesley")
    assert wa._merge_hotwords("ollama, Kubernetes, LESLEY") == "Ollama, Lesley, Kubernetes"


# --- dictation cleanup in the pipeline -------------------------------------------

@pytest.fixture
def llm(monkeypatch):
    """Fake edit backend: records what it was sent, answers 'CLEANED'."""
    calls = []

    def fake(system_prompt, user_prompt, fallback):
        calls.append((system_prompt, user_prompt))
        return "CLEANED", True
    monkeypatch.setattr(wa, "_call_edit_llm", fake)
    monkeypatch.setattr(wa, "process_transcribed_text", lambda t: t)
    return calls


def test_cleanup_off_by_default(llm):
    assert _post().json()["text"] == "hello lesley"
    assert llm == []


def test_processing_clean_runs_cleanup(llm):
    assert _post({"processing": "clean"}).json()["text"] == "CLEANED"
    system, user = llm[0]
    assert user == "<dictation>\nhello lesley\n</dictation>"
    assert "Style rules" in system


def test_cleanup_always_applies_to_full_not_raw(llm, monkeypatch):
    monkeypatch.setattr(wa, "CLEANUP_ALWAYS", True)
    assert _post().json()["text"] == "CLEANED"
    assert _post({"processing": "raw"}).json()["text"] == "hello lesley"
    assert len(llm) == 1


def test_cleanup_skipped_when_trigger_fired(llm, monkeypatch):
    monkeypatch.setattr(wa, "CLEANUP_ALWAYS", True)
    monkeypatch.setattr(wa, "_trigger_fired", lambda t: True)
    _post({"processing": "clean"})
    assert llm == []


def test_cleanup_style_override_and_validation(llm):
    _post({"processing": "clean", "cleanup_rewrite": "polish", "cleanup_emoji": "on"})
    import api.cleanup as c
    assert c.PROMPT["style"]["rewrite"]["polish"] in llm[0][0]
    r = _post({"processing": "clean", "cleanup_rewrite": "pirate"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cleanup_option"


def test_cleanup_failure_returns_dictation(monkeypatch):
    monkeypatch.setattr(wa, "_call_edit_llm", lambda s, u, fallback: (fallback, False))
    monkeypatch.setattr(wa, "process_transcribed_text", lambda t: t)
    assert _post({"processing": "clean"}).json()["text"] == "hello lesley"


def test_trigger_detection_uses_config(monkeypatch):
    monkeypatch.setitem(wa.CONFIG, "trigger_patterns", [r"prompt\s*ai"])
    monkeypatch.setitem(wa.CONFIG, "roast_trigger_patterns", [])
    assert wa._trigger_fired("fix this. prompt AI make it formal")
    assert not wa._trigger_fired("a prompt answer")


# --- soft pause (POST /v1/pause, /v1/resume) --------------------------------------

class _FakeCt2:
    def __init__(self):
        self.model_is_loaded = True
        self.calls = []

    def unload_model(self, to_cpu=False):
        self.calls.append(("unload", to_cpu))
        self.model_is_loaded = False

    def load_model(self):
        self.calls.append(("load",))
        self.model_is_loaded = True


@pytest.fixture
def ct2(monkeypatch):
    fake = _FakeCt2()
    monkeypatch.setattr(wa.model, "model", fake, raising=False)
    monkeypatch.setitem(wa.PAUSE, "paused", False)
    monkeypatch.setitem(wa.PAUSE, "reason", None)
    yield fake
    wa.PAUSE.update(paused=False, reason=None, since=None)


def test_pause_moves_model_off_gpu_and_refuses_dictation(ct2):
    r = client.post("/v1/pause", json={"reason": "game"})
    assert r.json() == {"status": "paused", "paused_reason": "game"}
    assert ct2.calls == [("unload", True)]
    h = client.get("/health").json()
    assert h["status"] == "paused" and h["paused_reason"] == "game"
    r = _post()
    assert r.status_code == 503 and r.json()["error"]["code"] == "server_paused"
    assert "gaming" in r.json()["error"]["message"]
    files = {"file": ("clip.wav", io.BytesIO(b"x"), "audio/wav")}
    assert client.post("/transcribe", files=files).status_code == 503
    assert CALLS == []                     # nothing reached Whisper


def test_resume_reloads_and_dictation_works(ct2):
    client.post("/v1/pause", json={})
    assert client.post("/v1/resume").json() == {"status": "ok"}
    assert ct2.calls[-1] == ("load",)
    assert client.get("/health").json()["status"] == "ok"
    assert _post().status_code == 200


def test_pause_default_reason_and_bad_reason(ct2):
    assert client.post("/v1/pause").json()["paused_reason"] == "user"
    assert client.post("/v1/pause", json={"reason": "nap"}).status_code == 400


def test_pause_needs_key_when_auth_on(ct2, monkeypatch):
    monkeypatch.setattr(wa, "WHISPER_API_KEY", KEY)
    assert client.post("/v1/pause").status_code == 401
