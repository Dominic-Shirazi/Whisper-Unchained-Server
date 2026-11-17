"""Tests for the optional whisper.cpp speech engine (api/whispercpp_engine.py).

No real whisper.cpp program is needed: the process handling runs against a
tiny stand-in HTTP server written here, and the answer mapping runs against a
recorded real answer (tests/fixtures/whispercpp_verbose.json). The one test
that uses the real program is skipped unless WHISPER_CPP_TEST_BIN and
WHISPER_CPP_TEST_MODEL point at it.

    .venv\\Scripts\\python.exe -m pytest tests/test_whispercpp_engine.py -q
"""
import io
import json
import os
import socket
import sys
import time
import types
import wave
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api import whispercpp_engine as w  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "whispercpp_verbose.json")
with open(FIXTURE, encoding="utf-8") as _f:
    RECORDED = json.load(_f)

# The server's own gates (api/whisper_api.py defaults).
NO_SPEECH_THRESHOLD, LOGPROB_THRESHOLD, COMPRESSION_RATIO_THRESHOLD = 0.6, -1.0, 2.4
SERVER_CALL = dict(beam_size=5, vad_filter=True, condition_on_previous_text=False,
                   hotwords="Lesley, Ame", language=None)


# --- engine choice ---------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "  ", "faster-whisper", "Faster_Whisper"])
def test_engine_defaults_to_faster_whisper(value):
    assert w.engine_name(value) == "faster-whisper"


@pytest.mark.parametrize("value", ["whisper.cpp", "WhisperCpp", " whisper-cpp ", "whisper_cpp"])
def test_engine_whisper_cpp_spellings(value):
    assert w.engine_name(value) == "whisper.cpp"


def test_unknown_engine_falls_back_and_says_so():
    said = []
    assert w.engine_name("parakeet", log=said.append) == "faster-whisper"
    assert "parakeet" in said[0] and "faster-whisper" in said[0]


def test_server_loads_faster_whisper_when_nothing_is_set():
    """The API module, imported the way the other API tests import it, picks
    faster-whisper and never builds a whisper.cpp engine."""
    if "api.whisper_api" not in sys.modules:
        class FakeWhisperModel:
            def __init__(self, *a, **kw):
                pass

            def transcribe(self, path, **kwargs):
                return iter([]), SimpleNamespace(language="en", duration=0.0,
                                                 language_probability=1.0)
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = FakeWhisperModel
        sys.modules["faster_whisper"] = fake
    import api.whisper_api as wa
    assert wa.ENGINE == "faster-whisper"
    assert not isinstance(wa.model, w.WhisperCppEngine)
    assert "engine" not in wa.DEVICE_INFO  # loaded by device_select, as before
    assert wa.model is not None


def test_health_names_the_engine(monkeypatch):
    test_server_loads_faster_whisper_when_nothing_is_set()
    import api.whisper_api as wa
    from fastapi.testclient import TestClient
    monkeypatch.setattr(wa, "WHISPER_API_KEY", "")
    monkeypatch.setattr(wa, "API_KEY_REQUIRED", False)
    assert TestClient(wa.app).get("/health").json()["engine"] == "faster-whisper"
    monkeypatch.setattr(wa, "ENGINE", "whisper.cpp")
    assert TestClient(wa.app).get("/health").json()["engine"] == "whisper.cpp"


# --- paths and names -------------------------------------------------------------

def test_resolve_binary_default_and_override(tmp_path):
    root = str(tmp_path)
    default = w.resolve_binary("", root)
    assert default.startswith(root) and os.path.join("api", "bin", "whispercpp") in default
    assert os.path.basename(default).startswith("whisper-server")
    assert w.resolve_binary("tools/ws.exe", root) == os.path.join(root, "tools", "ws.exe")
    absolute = os.path.join(root, "x", "whisper-server.exe")
    assert w.resolve_binary(absolute, "somewhere-else") == absolute


def test_resolve_model_names_and_paths(tmp_path):
    root = str(tmp_path)
    models = os.path.join(root, "models")
    assert w.resolve_model("base", models, root) == os.path.join(models, "ggml-base.bin")
    assert w.resolve_model("large-v3-turbo-q5_0", "models", root) == os.path.join(
        models, "ggml-large-v3-turbo-q5_0.bin")
    assert w.resolve_model("ggml-base", models, root) == os.path.join(models, "ggml-base.bin")
    # No models folder configured: the repo's "models".
    assert w.resolve_model("base", None, root) == os.path.join(models, "ggml-base.bin")
    # Anything that looks like a file is used as given.
    assert w.resolve_model("stuff/my.bin", models, root) == os.path.join(root, "stuff", "my.bin")
    assert w.resolve_model("my.bin", models, root) == os.path.join(root, "my.bin")
    absolute = os.path.join(root, "elsewhere", "ggml-small.bin")
    assert w.resolve_model(absolute, models, "other") == absolute
    assert w.resolve_model("", models, root) is None
    assert w.resolve_model(None, models, root) is None


def test_model_display_name():
    assert w.model_display_name(r"C:\m\ggml-large-v3-turbo-q5_0.bin") == "large-v3-turbo-q5_0"
    assert w.model_display_name("/m/custom.bin") == "custom"
    assert w.model_display_name(None) == ""


# --- device and command line -----------------------------------------------------

def test_device_args():
    assert w.device_args("auto") == []
    assert w.device_args("") == []
    assert w.device_args("cpu") == ["-ng"]
    assert w.device_args("CPU ") == ["-ng"]
    assert w.device_args("1") == ["-dev", "1"]
    assert w.device_args("01") == ["-dev", "1"]


def test_unknown_device_means_auto_and_says_so():
    said = []
    assert w.normalize_device("cuda", log=said.append) == "auto"
    assert "cuda" in said[0]


def test_build_server_args():
    args = w.build_server_args("m.bin", 5123, "cpu", threads=6, vad_model="vad.bin")
    assert args == ["-m", "m.bin", "--host", "127.0.0.1", "--port", "5123", "-ng",
                    "-t", "6", "-vm", "vad.bin"]
    assert w.build_server_args("m.bin", 7, "2") == [
        "-m", "m.bin", "--host", "127.0.0.1", "--port", "7", "-dev", "2"]
    # Only ever this PC: never a public address.
    assert "0.0.0.0" not in w.build_server_args("m.bin", 7)


# Lines whisper-server v1.9.5 really printed on a PC with two graphics devices.
GPU_LOG = [
    "whisper_init_with_params_no_state: use gpu    = 1",
    "ggml_vulkan: Found 2 Vulkan devices:",
    "ggml_vulkan: 0 = NVIDIA GeForce RTX 2070 SUPER (NVIDIA) | uma: 0 | fp16: 1 | bf16: 1 | "
    "fp4: 0 | warp size: 32 | shared memory: 49152 | int dot: 1 | matrix cores: NV_coopmat2",
    "ggml_vulkan: 1 = Intel(R) UHD Graphics 630 (Intel Corporation) | uma: 1 | fp16: 1 | "
    "bf16: 0 | fp4: 0 | warp size: 32 | shared memory: 32768 | int dot: 0 | matrix cores: none",
    "whisper_model_load:      Vulkan0 total size =   147.37 MB",
    "whisper_backend_init_gpu: device 0: Vulkan0 (type: 1)",
    "whisper_backend_init_gpu: found GPU device 0: Vulkan0 (type: 1, cnt: 0)",
]


def test_parse_startup_log_gpu():
    found = w.parse_startup_log(GPU_LOG)
    assert found["gpu_index"] == 0
    assert [d["index"] for d in found["devices"]] == [0, 1]
    assert found["devices"][0]["name"].startswith("NVIDIA GeForce RTX 2070 SUPER")
    assert [d["integrated"] for d in found["devices"]] == [False, True]


def test_parse_startup_log_processor():
    lines = GPU_LOG[:4] + ["whisper_backend_init_gpu: no GPU found"]
    assert w.parse_startup_log(lines)["gpu_index"] is None
    assert w.parse_startup_log([]) == {"devices": [], "gpu_index": None}


def test_pick_device_prefers_a_separate_graphics_card():
    assert w.pick_device([]) is None
    assert w.pick_device([{"index": 0, "integrated": True}]) == 0
    assert w.pick_device([{"index": 0, "integrated": True},
                          {"index": 1, "integrated": False}]) == 1
    assert w.pick_device(w.parse_startup_log(GPU_LOG)["devices"]) == 0


# --- request fields --------------------------------------------------------------

def test_fields_for_the_servers_own_call_without_vad_model():
    fields, ignored = w.build_request_fields(SERVER_CALL)
    assert fields["response_format"] == "verbose_json"
    assert fields["language"] == "auto"          # whisper-server's default is "en"
    assert fields["beam_size"] == "5"
    assert fields["prompt"] == "Lesley, Ame"
    assert fields["carry_initial_prompt"] == "true"
    assert fields["token_timestamps"] == "false"  # else segments are cut at 60 characters
    assert fields["suppress_nst"] == "true"
    assert fields["no_language_probabilities"] == "true"
    assert "vad" not in fields and "max_len" not in fields
    assert ignored == ["vad_filter", "condition_on_previous_text"]


def test_fields_with_vad_model_always_use_vad():
    fields, ignored = w.build_request_fields(SERVER_CALL, vad_available=True)
    assert fields["vad"] == "true" and ignored == ["condition_on_previous_text"]
    fields, ignored = w.build_request_fields(
        {"vad_filter": True, "vad_parameters": {"threshold": 0.4, "speech_pad_ms": 100}},
        vad_available=True)
    assert fields["vad_threshold"] == "0.4" and fields["vad_speech_pad_ms"] == "100"
    # Mixing VAD and no-VAD requests in one process gives wrong times, so a
    # request cannot turn it off.
    fields, ignored = w.build_request_fields({"vad_filter": False}, vad_available=True)
    assert fields["vad"] == "true" and ignored == ["vad_filter"]


def test_fields_language_prompt_and_words():
    fields, _ = w.build_request_fields({"language": "de", "initial_prompt": "Hallo.",
                                        "word_timestamps": True, "task": "translate"})
    assert fields["language"] == "de" and fields["translate"] == "true"
    assert fields["prompt"] == "Hallo." and "carry_initial_prompt" not in fields
    assert (fields["token_timestamps"], fields["split_on_word"], fields["max_len"]) == (
        "true", "true", "1")
    fields, _ = w.build_request_fields({"initial_prompt": "Notes.", "hotwords": "Ame"})
    assert fields["prompt"] == "Notes. Ame"
    fields, _ = w.build_request_fields({"hotwords": None})
    assert "prompt" not in fields


def test_fields_temperature_and_thresholds():
    fields, _ = w.build_request_fields({"temperature": 0.2})
    assert (fields["temperature"], fields["temperature_inc"]) == ("0.2", "0")
    fields, _ = w.build_request_fields({"temperature": [0.0, 0.2, 0.4]})
    assert (fields["temperature"], fields["temperature_inc"]) == ("0", "0.2")
    fields, _ = w.build_request_fields({"no_speech_threshold": 0.5, "log_prob_threshold": -0.8,
                                        "best_of": 3})
    assert fields["no_speech_thold"] == "0.5" and fields["logprob_thold"] == "-0.8"
    assert fields["best_of"] == "3"
    assert "temperature" not in w.build_request_fields({})[0]


def test_fields_report_what_they_cannot_use():
    fields, ignored = w.build_request_fields({"max_new_tokens": 1, "patience": 2.0,
                                              "without_timestamps": True,
                                              "word_timestamps": True})
    assert ignored == ["max_new_tokens", "patience"]
    assert fields["no_timestamps"] == "true" and fields["token_timestamps"] == "false"


# --- mapping a real answer -------------------------------------------------------

def _passes_server_gates(seg):
    if seg.no_speech_prob > NO_SPEECH_THRESHOLD and seg.avg_logprob < LOGPROB_THRESHOLD:
        return False
    return seg.compression_ratio <= COMPRESSION_RATIO_THRESHOLD


def test_compression_ratio_matches_whispers_formula():
    assert w.compression_ratio("") == 0.0
    normal = w.compression_ratio(" Here is a second sentence, so there is more than one line.")
    looping = w.compression_ratio(" I'm" * 60)
    assert normal < 2.4 < looping


def test_map_plain_answer():
    segments, info = w.map_response(RECORDED["plain"], requested_language="auto")
    assert info.language == "en"                      # "english" in the answer
    assert info.duration == pytest.approx(12.887, abs=0.01)
    assert info.language_probability == 1.0
    assert len(segments) == len(RECORDED["plain"]["segments"]) >= 1
    text = "".join(s.text for s in segments).strip()
    assert text.startswith("Hello Corey.") and text.endswith("one line to check.")
    for s, raw in zip(segments, RECORDED["plain"]["segments"]):
        assert s.text == raw["text"]
        assert (s.start, s.end) == (raw["start"], raw["end"]) and s.end >= s.start
        assert s.no_speech_prob == raw["no_speech_prob"]
        assert -1.0 < s.avg_logprob <= 0.0
        # Worked out again from the token probabilities: never more confident
        # than whisper-server's own figure, which counts timestamp tokens.
        assert s.avg_logprob <= raw["avg_logprob"] + 1e-6
        assert 0.5 < s.compression_ratio < 2.4
        assert s.words is None
        assert _passes_server_gates(s)
    assert [s.id for s in segments] == list(range(1, len(segments) + 1))


def test_map_words_answer():
    segments, info = w.map_response(RECORDED["words"], word_timestamps=True)
    plain, _ = w.map_response(RECORDED["plain"])
    assert "".join(s.text for s in segments).split() == "".join(s.text for s in plain).split()
    # One segment per sentence.
    assert [s.text.strip()[-1] for s in segments] == ["."] * len(segments)
    assert len(segments) == 4
    last_end = 0.0
    for s in segments:
        assert s.words and "".join(x.word for x in s.words) == s.text
        assert s.start == s.words[0].start and s.end == max(x.end for x in s.words)
        assert _passes_server_gates(s)
        for x in s.words:
            assert x.word.startswith(" ") and x.word.strip()
            assert 0.0 <= x.start <= x.end <= info.duration + 0.5
            assert 0.0 < x.probability <= 1.0
            assert x.start >= last_end - 0.05
            last_end = x.end
    # A word made of several tokens (" Corey" + ".") is one word.
    assert segments[0].words[1].word == " Corey."


def test_map_words_splits_on_a_long_silence():
    raw = {"language": "english", "duration": 9.0, "segments": [
        {"text": " one", "start": 0.0, "end": 0.4, "no_speech_prob": 0.1,
         "words": [{"word": " one", "probability": 0.9}]},
        {"text": "", "start": 0.4, "end": 0.4, "words": []},
        {"text": " two", "start": 0.5, "end": 0.9, "no_speech_prob": 0.3,
         "words": [{"word": " tw", "probability": 0.8}, {"word": "o", "probability": 0.4}]},
        {"text": " three", "start": 5.0, "end": 5.5, "no_speech_prob": 0.2,
         "words": [{"word": " three", "probability": 0.7}]}]}
    segments, _ = w.map_response(raw, word_timestamps=True)
    assert [s.text for s in segments] == [" one two", " three"]
    assert segments[0].no_speech_prob == 0.3            # the worst of its words
    assert segments[0].words[1].probability == pytest.approx(0.6)
    assert (segments[1].start, segments[1].end) == (5.0, 5.5)


def test_map_silence_and_odd_answers():
    segments, info = w.map_response({"language": "english", "duration": 5.0, "segments": []})
    assert segments == [] and info.duration == 5.0
    segments, info = w.map_response({"duration": 1.0, "segments": [
        {"text": " hi", "start": 0.0, "end": 0.5, "avg_logprob": float("nan")}]},
        requested_language="fr")
    assert info.language == "fr"
    # Nothing to measure: neutral values that trip no gate.
    assert segments[0].avg_logprob == 0.0 and segments[0].no_speech_prob == 0.0
    assert _passes_server_gates(segments[0])
    with pytest.raises(w.WhisperCppError, match="failed to process audio"):
        w.map_response({"error": "failed to process audio"})


def test_language_names_cover_whispers_list():
    assert w.LANGUAGE_CODES["english"] == "en" and w.LANGUAGE_CODES["cantonese"] == "yue"
    assert len(w.LANGUAGE_CODES) == 100 == len(set(w.LANGUAGE_CODES.values()))


# --- audio preparation -----------------------------------------------------------

def _write_wav(path, rate, channels, seconds=0.5):
    import numpy as np
    t = np.arange(int(rate * seconds)) / rate
    mono = (np.sin(2 * np.pi * 440 * t) * 12000).astype("<i2")
    data = np.repeat(mono[:, None], channels, axis=1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(data.tobytes())
    return str(path)


def _wav_shape(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as f:
        return f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()


def test_ready_wav_is_sent_untouched(tmp_path):
    path = _write_wav(tmp_path / "a.wav", 16000, 1)
    assert w.is_ready_wav(path)
    wav_bytes, converted = w.prepare_wav(path)
    assert not converted
    with open(path, "rb") as f:
        assert wav_bytes == f.read()


def test_other_audio_is_converted_to_16k_mono(tmp_path):
    path = _write_wav(tmp_path / "b.wav", 22050, 2, seconds=1.0)
    assert not w.is_ready_wav(path)
    wav_bytes, converted = w.prepare_wav(path)
    assert converted
    rate, channels, width, frames = _wav_shape(wav_bytes)
    assert (rate, channels, width) == (16000, 1, 2)
    assert frames == pytest.approx(16000, abs=400)


def test_not_audio_is_not_a_ready_wav(tmp_path):
    junk = tmp_path / "c.wav"
    junk.write_bytes(b"not a wav at all")
    assert not w.is_ready_wav(str(junk))


def test_array_becomes_wav():
    import numpy as np
    wav_bytes, converted = w.prepare_wav(np.zeros(8000, dtype=np.float32))
    assert converted and _wav_shape(wav_bytes) == (16000, 1, 2, 8000)


# --- process handling, against a stand-in server ---------------------------------

FAKE_SERVER = r'''
import json, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
model = args[args.index("-m") + 1]
record, fixture = args[0], args[1]
if "broken" in model:
    sys.stderr.write("error: failed to initialize whisper context\n")
    sys.exit(0)
sys.stderr.write("ggml_vulkan: 0 = Test Card 9000 (Test) | uma: 0 | fp16: 1\n")
sys.stderr.write("whisper_backend_init_gpu: found GPU device 0: Vulkan0 (type: 1, cnt: 0)\n")
sys.stderr.flush()
answers = json.load(open(fixture, encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"status": "ok"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        fields = dict(re.findall(rb'name="([a-z_]+)"\r\n\r\n([^\r]*)\r\n', body))
        fields = {k.decode(): v.decode() for k, v in fields.items()}
        fields["_audio_bytes"] = len(body)
        fields["_argv"] = args[2:]
        with open(record, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields) + "\n")
        if fields.get("language") == "xx":
            self._send(500, {"error": "failed to process audio"})
            return
        self._send(200, answers["words" if fields.get("max_len") == "1" else "plain"])


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_closed(port, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not _port_open(port):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake_whisper_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    record = tmp_path / "requests.jsonl"
    engines = []

    def make(model="ggml-test.bin", **kw):
        kw.setdefault("log", lambda *a: None)
        kw.setdefault("startup_timeout", 30)
        e = w.WhisperCppEngine(
            str(tmp_path / "whisper-server.exe"), str(tmp_path / model),
            command_prefix=[sys.executable, str(script), str(record), FIXTURE], **kw)
        engines.append(e)
        return e

    def requests_seen():
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]

    yield SimpleNamespace(make=make, seen=requests_seen, tmp=tmp_path)
    for e in engines:
        e.close()


def test_engine_starts_warms_and_transcribes(fake, tmp_path):
    e = fake.make(device="cpu", threads=3)
    assert e.is_running() and _port_open(e.port)
    assert e.port not in (5000, 5001)
    assert e.gpu_index == 0 and e.gpu_name == "Test Card 9000 (Test)"
    seen = fake.seen()
    assert len(seen) == 1                       # the warm-up
    assert seen[0]["_argv"] == ["-m", str(tmp_path / "ggml-test.bin"), "--host", "127.0.0.1",
                                "--port", str(e.port), "-ng", "-t", "3"]
    assert "vad" not in seen[0]

    clip = _write_wav(tmp_path / "clip.wav", 16000, 1)
    segments, info = e.transcribe(clip, **SERVER_CALL)
    assert "".join(s.text for s in segments).strip().startswith("Hello Corey.")
    assert info.language == "en" and info.duration > 12
    assert e.last_ignored == ["vad_filter", "condition_on_previous_text"]
    sent = fake.seen()[-1]
    assert sent["language"] == "auto" and sent["beam_size"] == "5"
    assert sent["prompt"] == "Lesley, Ame" and sent["response_format"] == "verbose_json"
    assert sent["_audio_bytes"] > os.path.getsize(clip)

    segments, _ = e.transcribe(clip, word_timestamps=True)
    assert segments[0].words[0].word == " Hello"
    assert e.restarts == 0


def test_engine_restarts_once_when_the_program_has_died(fake, tmp_path):
    e = fake.make()
    first_proc, first_port = e._proc, e.port
    first_proc.kill()
    first_proc.wait(timeout=10)
    assert not e.is_running()
    segments, _ = e.transcribe(_write_wav(tmp_path / "clip.wav", 16000, 1))
    assert segments and e.restarts == 1 and e.is_running()
    assert e._proc is not first_proc
    # Nothing of the first one is left behind (on Windows the venv's python.exe
    # starts the real one as its own child; the job object ends that too).
    if e.port != first_port:
        assert _wait_closed(first_port)


def test_close_stops_the_program_and_is_repeatable(fake, tmp_path):
    e = fake.make()
    proc, port = e._proc, e.port
    e.close()
    e.close()
    assert proc.poll() is not None and not e.is_running()
    assert _wait_closed(port)
    with pytest.raises(w.WhisperCppError, match="shut down"):
        e.transcribe(_write_wav(tmp_path / "clip.wav", 16000, 1))


def test_failed_start_reports_what_the_program_said(fake):
    with pytest.raises(w.WhisperCppError, match="failed to initialize whisper context"):
        fake.make(model="ggml-broken.bin")


def test_error_answer_becomes_an_exception(fake, tmp_path):
    e = fake.make()
    with pytest.raises(w.WhisperCppError, match="failed to process audio"):
        e.transcribe(_write_wav(tmp_path / "clip.wav", 16000, 1), language="xx")
    assert e.is_running()


def test_vad_model_is_passed_and_used(fake, tmp_path):
    e = fake.make(vad_model=str(tmp_path / "ggml-silero.bin"))
    e.transcribe(_write_wav(tmp_path / "clip.wav", 16000, 1), **SERVER_CALL)
    warm, real = fake.seen()
    assert warm["_argv"][-2:] == ["-vm", str(tmp_path / "ggml-silero.bin")]
    assert "vad" not in warm            # the warm-up must really run the model
    assert real["vad"] == "true" and e.last_ignored == ["condition_on_previous_text"]


def test_stderr_buffer_is_bounded(fake):
    e = fake.make(start=False)
    for i in range(1000):
        e._stderr.append("line %d" % i)
    assert len(e._stderr) == 200 and "line 999" in e.error_tail()


# --- loading from settings -------------------------------------------------------

class FakeEngine:
    made = []

    def __init__(self, binary, model_path, device="auto", threads=None, vad_model=None,
                 log=print):
        self.args = dict(binary=binary, model_path=model_path, device=device,
                         threads=threads, vad_model=vad_model)
        self.gpu_index, self.gpu_name = (None, None) if device == "cpu" else (0, "Test Card")
        FakeEngine.made.append(self)


def _layout(tmp_path, with_bin=True, with_model=True):
    root = tmp_path
    binary = root / "api" / "bin" / "whispercpp" / os.path.basename(w.DEFAULT_BIN)
    model = root / "models" / "ggml-base.bin"
    binary.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    if with_bin:
        binary.write_bytes(b"x")
    if with_model:
        model.write_bytes(b"x")
    return str(root), str(binary), str(model)


def test_load_from_env_success(tmp_path):
    root, binary, model = _layout(tmp_path)
    said = []
    engine, info = w.load_from_env({"WHISPER_CPP_MODEL": "base"}, root, models_dir="models",
                                   threads=4, log=said.append, engine_factory=FakeEngine)
    assert engine.args == dict(binary=binary, model_path=model, device="auto", threads=4,
                               vad_model=None)
    assert info["engine"] == "whisper.cpp" and info["model"] == "base"
    assert info["device"] == "vulkan" and info["gpu_name"] == "Test Card"
    assert info["error"] is None and info["fallback"] is None
    assert info["requested_device"] == "auto"
    # Same keys the server reads from device_select.load_whisper()'s info.
    for key in ("model", "device", "compute_type", "requested_device", "fallback", "cpu_threads"):
        assert key in info


def test_load_from_env_cpu_and_vad_model_in_models_folder(tmp_path):
    root, binary, model = _layout(tmp_path)
    vad = os.path.join(root, "models", "ggml-silero-v5.1.2.bin")
    open(vad, "wb").close()
    engine, info = w.load_from_env({"WHISPER_CPP_MODEL": model, "WHISPER_CPP_DEVICE": "cpu"},
                                   root, models_dir=os.path.join(root, "models"), threads=4,
                                   log=lambda *a: None, engine_factory=FakeEngine)
    assert engine.args["device"] == "cpu" and engine.args["vad_model"] == vad
    assert info["device"] == "cpu" and info["cpu_threads"] == 4 and info["fallback"] is None


def test_load_from_env_says_when_no_graphics_card_was_usable(tmp_path):
    root, _, _ = _layout(tmp_path)

    class NoGpu(FakeEngine):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.gpu_index = self.gpu_name = None

    _, info = w.load_from_env({"WHISPER_CPP_MODEL": "base"}, root, models_dir="models",
                              log=lambda *a: None, engine_factory=NoGpu)
    assert info["device"] == "cpu" and "no graphics card" in info["fallback"]


@pytest.mark.parametrize("with_bin,with_model,env,expect", [
    (False, True, {"WHISPER_CPP_MODEL": "base"}, "whisper.cpp program is missing"),
    (True, False, {"WHISPER_CPP_MODEL": "base"}, "whisper.cpp model is missing"),
    (True, True, {}, "No whisper.cpp model is chosen"),
])
def test_load_from_env_missing_files_fail_with_one_plain_sentence(
        tmp_path, with_bin, with_model, env, expect):
    root, binary, model = _layout(tmp_path, with_bin, with_model)
    FakeEngine.made.clear()
    said = []
    engine, info = w.load_from_env(env, root, models_dir="models", log=said.append,
                                   engine_factory=FakeEngine)
    assert engine is None and not FakeEngine.made
    assert expect in info["error"]
    if env:
        assert (binary if not with_bin else model) in info["error"]   # where it should be
    assert "\n" not in info["error"] and "\u2014" not in info["error"]
    assert said == ["[API] Error loading model: " + info["error"]]


def test_load_from_env_start_failure_is_reported_not_raised(tmp_path):
    root, _, _ = _layout(tmp_path)

    def boom(*a, **kw):
        raise w.WhisperCppError("whisper.cpp stopped while starting: bad\n model file")

    engine, info = w.load_from_env({"WHISPER_CPP_MODEL": "base"}, root, models_dir="models",
                                   log=lambda *a: None, engine_factory=boom)
    assert engine is None
    assert info["error"] == "whisper.cpp stopped while starting: bad model file"


# --- the real program (skipped unless pointed at it) -----------------------------

REAL_BIN = os.environ.get("WHISPER_CPP_TEST_BIN", "")
REAL_MODEL = os.environ.get("WHISPER_CPP_TEST_MODEL", "")


@pytest.mark.skipif(not (REAL_BIN and REAL_MODEL),
                    reason="set WHISPER_CPP_TEST_BIN and WHISPER_CPP_TEST_MODEL to run "
                           "against the real whisper-server")
def test_real_whisper_server(tmp_path):
    e = w.WhisperCppEngine(REAL_BIN, REAL_MODEL, log=lambda *a: None,
                           device=os.environ.get("WHISPER_CPP_TEST_DEVICE", "cpu"))
    try:
        clip = os.environ.get("WHISPER_CPP_TEST_CLIP") or _write_wav(
            tmp_path / "tone.wav", 22050, 2, seconds=2.0)
        segments, info = e.transcribe(clip, **SERVER_CALL)
        assert isinstance(segments, list) and info.duration > 1.0 and info.language
        for s in segments:
            assert s.end >= s.start and s.avg_logprob <= 0.0 and s.compression_ratio >= 0.0
        port, proc = e.port, e._proc
    finally:
        e.close()
    assert proc.poll() is not None and _wait_closed(port)
