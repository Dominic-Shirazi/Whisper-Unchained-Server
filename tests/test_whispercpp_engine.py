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
if "integrated" in model:
    # A PC whose only graphics are built in; "-ng" really means the processor.
    if "-ng" in args:
        sys.stderr.write("whisper_backend_init_gpu: no GPU found\n")
    else:
        sys.stderr.write("ggml_vulkan: 0 = Built-in Graphics 5 (Test) | uma: 1 | fp16: 1\n")
        sys.stderr.write("whisper_backend_init_gpu: found GPU device 0: Vulkan0 (type: 1, cnt: 0)\n")
else:
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


def _layout(tmp_path, with_bin=True, with_model=True, with_vad=True):
    root = tmp_path
    binary = root / "api" / "bin" / "whispercpp" / os.path.basename(w.DEFAULT_BIN)
    model = root / "models" / "ggml-base.bin"
    binary.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    if with_bin:
        binary.write_bytes(b"x")
    if with_model:
        model.write_bytes(b"x")
    if with_vad:
        (root / "models" / VAD_NAME).write_bytes(b"x")
    return str(root), str(binary), str(model)


VAD_NAME = "ggml-silero-v5.1.2.bin"


def test_load_from_env_success(tmp_path):
    root, binary, model = _layout(tmp_path)
    said = []
    engine, info = w.load_from_env({"WHISPER_CPP_MODEL": "base"}, root, models_dir="models",
                                   threads=4, log=said.append, engine_factory=FakeEngine)
    assert engine.args == dict(binary=binary, model_path=model, device="auto", threads=4,
                               vad_model=os.path.join(root, "models", VAD_NAME))
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


@pytest.mark.parametrize("with_bin,with_model,with_vad,env,expect", [
    (False, True, True, {"WHISPER_CPP_MODEL": "base"}, "whisper.cpp program is missing"),
    (True, False, True, {"WHISPER_CPP_MODEL": "base"}, "whisper.cpp model is missing"),
    (True, True, True, {}, "No whisper.cpp model is chosen"),
    # The silence filter is required: without it whisper.cpp invents text on silence.
    (True, True, False, {"WHISPER_CPP_MODEL": "base"}, "silence filter is missing"),
])
def test_load_from_env_missing_files_fail_with_one_plain_sentence(
        tmp_path, with_bin, with_model, with_vad, env, expect):
    root, binary, model = _layout(tmp_path, with_bin, with_model, with_vad)
    FakeEngine.made.clear()
    said = []
    engine, info = w.load_from_env(env, root, models_dir="models", log=said.append,
                                   engine_factory=FakeEngine)
    assert engine is None and not FakeEngine.made
    assert expect in info["error"]
    if env:   # where the missing file should be
        vad = os.path.join(root, "models", VAD_NAME)
        assert (binary if not with_bin else model if not with_model else vad) in info["error"]
    assert "\n" not in info["error"] and "\u2014" not in info["error"]
    assert info["error"].count(". ") == 0 and info["error"].endswith(".")   # one sentence
    assert said == ["[API] Error loading model: " + info["error"]]
    # Settings asks the same question before saving, without starting anything.
    assert w.files_problem(env, root, "models") == info["error"]


def test_missing_silence_filter_named_by_the_setting(tmp_path):
    root, _, _ = _layout(tmp_path, with_vad=False)
    env = {"WHISPER_CPP_MODEL": "base", "WHISPER_CPP_VAD_MODEL": "extras/my-vad.bin"}
    engine, info = w.load_from_env(env, root, models_dir="models", log=lambda *a: None,
                                   engine_factory=FakeEngine)
    assert engine is None
    assert os.path.join(root, "extras", "my-vad.bin") in info["error"]
    # A filter that is set but missing is not quietly replaced by another one.
    open(os.path.join(root, "models", VAD_NAME), "wb").close()
    assert w.load_from_env(env, root, models_dir="models", log=lambda *a: None,
                           engine_factory=FakeEngine)[0] is None
    assert w.files_problem({"WHISPER_CPP_MODEL": "base"}, root, "models") is None


def test_load_from_env_says_when_a_test_picked_the_processor(tmp_path):
    root, _, _ = _layout(tmp_path)

    class Tested(FakeEngine):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.gpu_index = self.gpu_name = None
            self.auto_note = "the processor was faster than Built-in Graphics 5 in a short test"

    said = []
    _, info = w.load_from_env({"WHISPER_CPP_MODEL": "base"}, root, models_dir="models",
                              log=said.append, engine_factory=Tested)
    assert info["device"] == "cpu" and info["fallback"].startswith("the processor was faster")
    assert not any("WARNING" in line for line in said)     # a choice, not a problem


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


# --- "auto": which device to run on ----------------------------------------------

CARD = {"index": 0, "name": "Test Card 9000 (Test)", "integrated": False}
BUILT_IN = {"index": 0, "name": "Built-in Graphics 5 (Test)", "integrated": True}


def test_plan_auto_uses_a_separate_card_without_testing():
    plan = w.plan_auto([dict(BUILT_IN), dict(CARD, index=1)], {}, "m/ggml-base.bin", "v1")
    assert (plan["action"], plan["index"], plan["name"]) == ("use", 1, CARD["name"])
    # This PC: an NVIDIA card and Intel built-in graphics.
    plan = w.plan_auto(w.parse_startup_log(GPU_LOG)["devices"], {}, "m.bin", "v1")
    assert (plan["action"], plan["index"]) == ("use", 0)


def test_plan_auto_with_no_graphics_is_the_processor():
    assert w.plan_auto([], {}, "m.bin", "v1")["action"] == "cpu"


def test_plan_auto_tests_built_in_graphics_once():
    plan = w.plan_auto([dict(BUILT_IN)], {}, "models/ggml-base.bin", "v1.9.5")
    assert plan["action"] == "test" and plan["index"] == 0
    assert plan["key"] == "Built-in Graphics 5 (Test)|ggml-base.bin|v1.9.5"
    for choice, action in (("cpu", "cpu"), ("gpu", "use")):
        entry = {"choice": choice, "gpu_seconds": 3.2, "cpu_seconds": 1.4}
        again = w.plan_auto([dict(BUILT_IN)], {"tests": {plan["key"]: entry}},
                            "models/ggml-base.bin", "v1.9.5")
        assert again["action"] == action and again["entry"] == entry
    # Another model, another engine build or another device: test again.
    cache = {"tests": {plan["key"]: {"choice": "cpu"}}}
    assert w.plan_auto([dict(BUILT_IN)], cache, "models/ggml-small.bin", "v1.9.5")["action"] == "test"
    assert w.plan_auto([dict(BUILT_IN)], cache, "models/ggml-base.bin", "v1.9.6")["action"] == "test"
    other = dict(BUILT_IN, name="Other Graphics")
    assert w.plan_auto([other], cache, "models/ggml-base.bin", "v1.9.5")["action"] == "test"
    # A damaged entry is not trusted.
    cache = {"tests": {plan["key"]: {"choice": "maybe"}}}
    assert w.plan_auto([dict(BUILT_IN)], cache, "models/ggml-base.bin", "v1.9.5")["action"] == "test"


def test_faster_choice():
    assert w.faster_choice(3.2, 1.4) == "cpu"
    assert w.faster_choice(0.4, 1.4) == "gpu"
    assert w.faster_choice(1.0, 1.0) == "cpu"      # a tie: no graphics memory needed
    assert w.faster_choice(None, 1.4) == "cpu"     # the graphics could not be timed
    assert w.faster_choice(0.4, None) == "gpu"


def test_auto_log_line_is_one_plain_line_with_both_timings():
    line = w.auto_log_line("Built-in Graphics 5", "cpu", 3.21, 1.44)
    assert line == ("[API] whisper.cpp: using the processor. Short test: graphics "
                    "(Built-in Graphics 5) 3.2 s, processor 1.4 s.")
    line = w.auto_log_line("Built-in Graphics 5", "gpu", 0.5, 1.44, remembered=True)
    assert "using the graphics (Built-in Graphics 5)" in line and "earlier start" in line
    assert "could not be tested" in w.auto_log_line("X", "cpu", None, 1.0)
    assert "\n" not in line and "—" not in line


def test_auto_cache_round_trip_and_damage(tmp_path):
    path = str(tmp_path / "models" / "whispercpp_auto.json")
    assert w.read_auto_cache(path) == {"tests": {}}
    assert not w.write_auto_cache(path, "k", {"choice": "cpu"})     # no such folder: no crash
    os.makedirs(os.path.dirname(path))
    assert w.write_auto_cache(path, "k", {"choice": "cpu", "cpu_seconds": 1.5})
    assert w.write_auto_cache(path, last={"device": "cpu"})
    data = w.read_auto_cache(path)
    assert data["tests"]["k"]["cpu_seconds"] == 1.5 and data["last"] == {"device": "cpu"}
    with open(path, "w") as f:
        f.write("{not json")
    assert w.read_auto_cache(path) == {"tests": {}}
    assert w.auto_cache_path(os.path.join("models", "ggml-base.bin")) == os.path.join(
        "models", "whispercpp_auto.json")


def test_engine_version_prefers_the_note_the_downloader_left(tmp_path):
    binary = tmp_path / "whisper-server.exe"
    assert w.engine_version(str(binary)) == "unknown"
    binary.write_bytes(b"12345")
    assert w.engine_version(str(binary)) == "size-5"
    (tmp_path / "engine.json").write_text('{"version": "v1.9.5"}')
    assert w.engine_version(str(binary)) == "v1.9.5"


def test_speed_test_clip_is_ready_speech():
    assert w.is_ready_wav(w.SPEED_TEST_CLIP)
    rate, channels, width, frames = _wav_shape(w.speed_test_clip())
    assert (rate, channels, width) == (16000, 1, 2) and 3 * 16000 < frames < 10 * 16000


def test_auto_times_built_in_graphics_and_keeps_the_processor(fake, tmp_path):
    asked, said = [], []

    def timer(device):
        asked.append(device)
        return {"0": 3.2, "cpu": 1.4}[device]

    e = fake.make(model="ggml-integrated.bin", auto_timer=timer, log=said.append)
    assert asked == ["0", "cpu"]
    assert e.auto_choice == "cpu" and e.gpu_index is None and e.is_running()
    assert "-ng" in fake.seen()[-1]["_argv"]                 # really moved to the processor
    assert "processor was faster than Built-in Graphics 5" in e.auto_note
    line = [s for s in said if "Short test" in s]
    assert line == ["[API] whisper.cpp: using the processor. Short test: graphics "
                    "(Built-in Graphics 5 (Test)) 3.2 s, processor 1.4 s."]
    data = w.read_auto_cache(str(tmp_path / "whispercpp_auto.json"))
    key = "Built-in Graphics 5 (Test)|ggml-integrated.bin|" + w.engine_version(e.binary)
    assert data["tests"][key]["choice"] == "cpu"
    assert (data["tests"][key]["gpu_seconds"], data["tests"][key]["cpu_seconds"]) == (3.2, 1.4)
    assert data["last"]["device"] == "cpu" and data["last"]["cpu_seconds"] == 1.4
    e.close()

    # A later start remembers: no second test, one line saying so.
    asked.clear()
    said.clear()
    e2 = fake.make(model="ggml-integrated.bin", auto_timer=timer, log=said.append)
    assert asked == [] and e2.auto_choice == "cpu" and e2.gpu_index is None
    assert any("Tested on an earlier start" in s and "3.2 s" in s and "1.4 s" in s for s in said)


def test_auto_keeps_built_in_graphics_when_they_are_faster(fake, tmp_path):
    e = fake.make(model="ggml-integrated.bin",
                  auto_timer=lambda device: {"0": 0.5, "cpu": 1.4}[device])
    assert e.auto_choice == "0" and e.gpu_index == 0
    assert e.gpu_name == "Built-in Graphics 5 (Test)" and e.auto_note is None
    assert w.read_auto_cache(e.auto_cache)["last"]["device"] == "gpu"


def test_auto_real_timing_against_the_stand_in(fake):
    """The engine's own timer: both devices are started, warmed and timed."""
    said = []
    e = fake.make(model="ggml-integrated.bin", log=said.append)
    assert e.auto_choice in ("0", "cpu") and e.is_running()
    entry = list(w.read_auto_cache(e.auto_cache)["tests"].values())[0]
    assert entry["gpu_seconds"] > 0 and entry["cpu_seconds"] > 0
    assert entry["choice"] == w.faster_choice(entry["gpu_seconds"], entry["cpu_seconds"])
    assert sum("Short test" in s for s in said) == 1
    # No request carried VAD: the model has to really run for the timing.
    assert all("vad" not in r for r in fake.seen())


def test_auto_with_a_separate_card_never_tests(fake):
    def no_timer(device):
        raise AssertionError("a separate graphics card must not be timed")

    e = fake.make(auto_timer=no_timer)
    assert e.auto_choice == "0" and e.gpu_index == 0
    assert w.read_auto_cache(e.auto_cache)["tests"] == {}


def test_a_set_device_is_never_second_guessed(fake):
    def no_timer(device):
        raise AssertionError("only auto decides for itself")

    e = fake.make(model="ggml-integrated.bin", device="0", auto_timer=no_timer)
    assert e.auto_choice is None and e.gpu_index == 0
    assert not os.path.exists(e.auto_cache)


# --- pause and resume -------------------------------------------------------------

def test_unload_stops_the_program_and_reload_starts_it(fake, tmp_path):
    e = fake.make(model="ggml-integrated.bin",
                  auto_timer=lambda device: {"0": 3.2, "cpu": 1.4}[device])
    proc, port = e._proc, e.port
    e.unload()
    assert proc.poll() is not None and not e.is_running() and _wait_closed(port)
    e.unload()                                   # twice is fine
    e.reload()
    assert e.is_running() and e.restarts == 0
    assert e.gpu_index is None                   # back on what auto chose, no new test
    segments, _ = e.transcribe(_write_wav(tmp_path / "clip.wav", 16000, 1))
    assert segments
    e.close()
    e.reload()                                   # after close it stays closed
    assert not e.is_running()


def test_transcribe_after_unload_starts_it_without_calling_it_a_crash(fake, tmp_path):
    said = []
    e = fake.make(log=said.append)
    e.unload()
    segments, _ = e.transcribe(_write_wav(tmp_path / "clip.wav", 16000, 1))
    assert segments and e.restarts == 0
    assert not any("was not running" in s for s in said)


class _PausableCpp:
    """Stands in for the whisper.cpp engine in the pause code of the server."""

    def __init__(self):
        self.running, self.calls = True, []

    def is_running(self):
        return self.running

    def unload(self):
        self.calls.append("unload")
        self.running = False

    def reload(self):
        self.calls.append("reload")
        self.running = True


def test_server_pause_stops_whisper_cpp_on_graphics_and_resume_starts_it(monkeypatch):
    test_server_loads_faster_whisper_when_nothing_is_set()
    import api.whisper_api as wa
    cpp = _PausableCpp()
    monkeypatch.setattr(wa, "model", cpp)
    monkeypatch.setattr(wa, "ENGINE", "whisper.cpp")
    monkeypatch.setattr(wa, "DEVICE", "vulkan")
    monkeypatch.setattr(wa, "PAUSE", {"paused": False, "reason": None, "since": None})
    wa.pause_model("game")
    assert cpp.calls == ["unload"] and wa.PAUSE["paused"] and wa.PAUSE["reason"] == "game"
    wa.pause_model("game")                        # already stopped: nothing more to do
    assert cpp.calls == ["unload"]
    wa.resume_model()
    assert cpp.calls == ["unload", "reload"] and not wa.PAUSE["paused"]


def test_server_pause_leaves_whisper_cpp_on_the_processor_running(monkeypatch):
    test_server_loads_faster_whisper_when_nothing_is_set()
    import api.whisper_api as wa
    cpp = _PausableCpp()
    monkeypatch.setattr(wa, "model", cpp)
    monkeypatch.setattr(wa, "ENGINE", "whisper.cpp")
    monkeypatch.setattr(wa, "DEVICE", "cpu")
    monkeypatch.setattr(wa, "PAUSE", {"paused": False, "reason": None, "since": None})
    wa.pause_model("user")
    wa.resume_model()
    assert cpp.calls == [] and not wa.PAUSE["paused"]   # no graphics memory to free
