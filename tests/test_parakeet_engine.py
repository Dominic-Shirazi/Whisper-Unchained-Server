"""The Parakeet engine (api/parakeet_engine.py), without the real model or the
sherpa-onnx package: a fake recognizer stands in for both.

What is checked: which engine a setting names, where the model files are
looked for and what is said when they are missing, that a recording with no
speech never reaches the model, that long speech is cut into short pieces,
and that a result looks like faster-whisper's to the server.
"""
import math
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api import parakeet_engine as p  # noqa: E402

RATE = p.SAMPLE_RATE


class FakeRecognizer:
    """Answers each piece with the next prepared result and remembers the pieces."""

    def __init__(self, results):
        self.results, self.pieces = list(results), []

    def create_stream(self):
        rec = self

        class Stream:
            def accept_waveform(self, rate, samples):
                assert rate == RATE
                rec.pieces.append(len(samples))
                self.result = rec.results.pop(0) if rec.results else result("")
        return Stream()

    def decode_stream(self, stream):
        pass


def result(text, tokens=(), timestamps=(), durations=(), ys_log_probs=()):
    return SimpleNamespace(text=text, tokens=list(tokens), timestamps=list(timestamps),
                           durations=list(durations), ys_log_probs=list(ys_log_probs))


def engine(results, spans, seconds=10.0, key="110m"):
    rec = FakeRecognizer(results)
    e = p.ParakeetEngine(key, {}, recognizer_factory=lambda files, threads: rec,
                         spans=lambda samples: spans,
                         loader=lambda audio: np.zeros(int(seconds * RATE), dtype=np.float32))
    return e, rec


@pytest.mark.parametrize("value", ["parakeet", "Parakeet", " sherpa-onnx ", "sherpa_onnx"])
def test_engine_name_knows_parakeet(value):
    assert p.engine_name(value) == "parakeet"


def test_engine_name_leaves_the_other_engines_as_they_were():
    said = []
    assert p.engine_name("") == "faster-whisper"
    assert p.engine_name(None) == "faster-whisper"
    assert p.engine_name("whisper.cpp") == "whisper.cpp"
    assert p.engine_name("nonsense", log=said.append) == "faster-whisper" and said


def test_model_key_and_files(tmp_path):
    assert p.model_key("") == "110m" and p.model_key("something else") == "110m"
    assert p.model_key("0.6B-v2") == "0.6b-v2"
    assert p.model_key("parakeet-tdt-0.6b-v3-multi-int8") == "0.6b-v3"
    files = p.model_files("110m", "models", str(tmp_path))
    assert set(files) == set(p.PARTS)
    assert files["tokens.txt"] == os.path.join(
        str(tmp_path), "models", "parakeet", "parakeet-tdt-110m-en-int8.tokens.txt")
    assert p.model_files("110m", str(tmp_path / "m"), "ignored")["tokens.txt"].startswith(
        os.path.join(str(tmp_path / "m"), "parakeet"))


def _write_model(tmp_path, key="110m"):
    files = p.model_files(key, "models", str(tmp_path))
    os.makedirs(os.path.dirname(files["tokens.txt"]), exist_ok=True)
    for path in files.values():
        open(path, "wb").close()
    return files


def test_files_problem_says_what_is_missing(tmp_path):
    def no_runtime():
        raise ImportError("no sherpa_onnx")
    env = {"PARAKEET_MODEL": "110m"}
    assert "not part of this install" in p.files_problem(env, str(tmp_path), "models", importer=no_runtime)
    missing = p.files_problem(env, str(tmp_path), "models", importer=lambda: None)
    assert "model is missing" in missing and "encoder.int8.onnx" in missing
    _write_model(tmp_path)
    assert p.files_problem(env, str(tmp_path), "models", importer=lambda: None) is None


def test_load_from_env_reports_like_the_other_engines(tmp_path):
    said, made = [], []
    eng, info = p.load_from_env({}, str(tmp_path), "models", threads=6, log=said.append,
                                importer=lambda: None, engine_factory=lambda *a, **k: made.append(1))
    assert eng is None and "model is missing" in info["error"] and not made
    assert info["engine"] == "parakeet" and info["device"] is None

    _write_model(tmp_path, "0.6b-v2")
    eng, info = p.load_from_env({"PARAKEET_MODEL": "0.6b-v2"}, str(tmp_path), "models", threads=6,
                                log=said.append, importer=lambda: None,
                                engine_factory=lambda key, files, threads=None: ("engine", key, threads))
    assert eng == ("engine", "0.6b-v2", 6)
    assert info["error"] is None and info["device"] == "cpu" and info["compute_type"] == "int8"
    assert info["model"] == "Parakeet 0.6B v2 (English)" and info["cpu_threads"] == 6

    def broken(*a, **k):
        raise RuntimeError("bad\n  model file")
    eng, info = p.load_from_env({"PARAKEET_MODEL": "0.6b-v2"}, str(tmp_path), "models",
                                log=said.append, importer=lambda: None, engine_factory=broken)
    assert eng is None and info["error"] == "bad model file"


def test_a_recording_with_no_speech_never_reaches_the_model():
    e, rec = engine([result("You")], spans=[], seconds=4.0)
    segments, info = e.transcribe("silence.wav", vad_filter=False)
    assert segments == [] and rec.pieces == []
    assert info.duration == 4.0 and info.duration_after_vad == 0.0 and info.language == "en"


def test_a_recording_too_short_to_hold_speech_is_not_looked_at():
    looked = []
    rec = FakeRecognizer([result("x")])
    e = p.ParakeetEngine("110m", {}, recognizer_factory=lambda f, t: rec,
                         spans=lambda s: looked.append(1) or [(0, len(s))],
                         loader=lambda a: np.zeros(100, dtype=np.float32))
    assert e.transcribe("blip.wav")[0] == [] and not looked and not rec.pieces


def test_each_stretch_of_speech_becomes_a_segment():
    spans = [(RATE, 3 * RATE), (5 * RATE, 9 * RATE)]
    e, rec = engine([result("Hello there.", ys_log_probs=[-0.1, -0.3]), result("Second part.")], spans)
    segments, info = e.transcribe("clip.wav", beam_size=5, hotwords="Lesley", vad_filter=True)
    assert rec.pieces == [2 * RATE, 4 * RATE]
    assert [(s.start, s.end, s.text) for s in segments] == [(1.0, 3.0, " Hello there."),
                                                            (5.0, 9.0, " Second part.")]
    assert "".join(s.text for s in segments).strip() == "Hello there. Second part."
    first = segments[0]
    assert first.avg_logprob == pytest.approx(-0.2) and first.no_speech_prob == 0.0
    assert first.compression_ratio > 0 and first.words is None and first.id == 0
    assert segments[1].avg_logprob == 0.0 and segments[1].id == 1
    assert info.duration == 10.0 and info.duration_after_vad == 6.0


def test_a_stretch_where_nothing_was_heard_is_left_out():
    e, _rec = engine([result("  "), result("Only this.")], [(0, RATE), (2 * RATE, 4 * RATE)])
    segments, _info = e.transcribe("clip.wav")
    assert [s.text for s in segments] == [" Only this."] and segments[0].id == 0


def test_long_speech_is_cut_into_pieces_within_the_limit():
    limit = p.MAX_PIECE_SECONDS * RATE
    assert p.split_long([(0, limit)]) == [(0, limit)]
    parts = p.split_long([(1000, 1000 + 60 * RATE)])
    assert len(parts) == 3 and parts[0][0] == 1000 and parts[-1][1] == 1000 + 60 * RATE
    assert all(b - a <= limit for a, b in parts)
    assert all(parts[i][1] == parts[i + 1][0] for i in range(len(parts) - 1))
    assert p.split_long([(0, 10)]) == []           # too short to recognise
    e, rec = engine([result("a"), result("b"), result("c")], [(0, 60 * RATE)], seconds=60.0)
    segments, _info = e.transcribe("long.wav")
    assert len(rec.pieces) == 3 and max(rec.pieces) <= limit and len(segments) == 3


def test_words_are_put_together_from_pieces_with_their_times():
    tokens = [" This", " is", " a", " sh", "ort", " t", "est", "."]
    starts = [0.24, 0.40, 0.56, 0.72, 0.88, 1.04, 1.20, 1.84]
    durs = [0.16] * 8
    logp = [math.log(0.9), math.log(0.8), math.log(1.0), math.log(0.6), math.log(0.9),
            math.log(0.7), math.log(0.5), math.log(0.9)]
    words = p.words_from_tokens(tokens, starts, durs, logp, offset=2.0)
    assert [w.word for w in words] == [" This", " is", " a", " short", " test."]
    assert words[0].start == pytest.approx(2.24) and words[0].end == pytest.approx(2.40)
    assert words[3].start == pytest.approx(2.72) and words[3].end == pytest.approx(3.04)
    assert words[3].probability == pytest.approx(0.6)        # the least sure piece
    assert words[4].end == pytest.approx(4.0) and words[4].probability == pytest.approx(0.5)
    assert p.words_from_tokens(["Hi"], [0.0], [0.1], [])[0].word == " Hi"

    e, _rec = engine([result("This is a short test.", tokens, starts, durs, logp)], [(2 * RATE, 5 * RATE)])
    segments, _info = e.transcribe("clip.wav", word_timestamps=True)
    assert [w.word for w in segments[0].words] == [" This", " is", " a", " short", " test."]


def test_language_reported():
    assert p.language_for("110m", "fr") == "en" and p.language_for("0.6b-v2", None) == "en"
    assert p.language_for("0.6b-v3", "DE") == "de" and p.language_for("0.6b-v3", None) == "en"
    e, _rec = engine([result("Guten Tag.")], [(0, 2 * RATE)], key="0.6b-v3")
    assert e.transcribe("clip.wav", language="de")[1].language == "de"


def test_an_array_is_taken_as_it_is():
    a = np.ones((2, 8), dtype=np.float64)
    out = p.load_audio(a)
    assert out.dtype == np.float32 and out.shape == (16,)
