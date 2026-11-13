"""Transcribe a file: subtitles, the shrink step, the client call, the window's job."""
import json
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import subtitles  # noqa: E402
import transcribe_client as tc  # noqa: E402

SEGS = [{"start": 0.0, "end": 2.5, "text": " Hello there."},
        {"start": 3661.25, "end": 3663.0, "text": "Second line"}]


def test_srt_format():
    assert subtitles.to_srt(SEGS) == (
        "1\n00:00:00,000 --> 00:00:02,500\nHello there.\n\n"
        "2\n01:01:01,250 --> 01:01:03,000\nSecond line\n")


def test_vtt_format():
    out = subtitles.to_vtt(SEGS)
    assert out.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:02.500\nHello there.\n")


def test_long_segment_is_split_and_wrapped():
    words = " ".join("word%d" % i for i in range(40))   # ~270 characters
    cues = subtitles.split_cues([{"start": 10, "end": 40, "text": words}])
    assert len(cues) >= 3
    assert cues[0][0] == 10 and abs(cues[-1][1] - 40) < 1e-6
    assert all(len(t) <= subtitles.MAX_CUE for _a, _b, t in cues)
    assert " ".join(t for _a, _b, t in cues) == words
    for line in subtitles.to_srt([{"start": 10, "end": 40, "text": words}]).splitlines():
        assert len(line) <= subtitles.MAX_LINE + 10


def test_empty_segments_skipped():
    assert subtitles.to_srt([{"start": 0, "end": 1, "text": "  "}]) == ""


def test_shrink_video_to_opus(tmp_path):
    import av
    mp4 = str(tmp_path / "clip.mp4")
    with av.open(mp4, "w") as c:
        v = c.add_stream("mpeg4", rate=10)
        v.width, v.height, v.pix_fmt = 64, 48, "yuv420p"
        a = c.add_stream("aac", rate=48000)
        a.layout = "mono"
        for i in range(20):
            for p in v.encode(av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")):
                c.mux(p)
        tone = (0.2 * np.sin(np.arange(48000 * 2) / 48000 * 2 * np.pi * 300)).astype(np.float32)
        for k in range(0, len(tone), 1024):
            f = av.AudioFrame.from_ndarray(tone[k:k + 1024].reshape(1, -1), format="flt", layout="mono")
            f.sample_rate = 48000
            for p in a.encode(f):
                c.mux(p)
        for s in (v, a):
            for p in s.encode():
                c.mux(p)
    assert subtitles.needs_shrinking(mp4)
    out = subtitles.shrink_audio(mp4, str(tmp_path))
    assert out.endswith(".ogg") and os.path.getsize(out) < 20000
    with av.open(out) as c:
        assert abs(float(c.duration) / 1e6 - 2.0) < 0.2


def test_shrink_refuses_file_without_sound(tmp_path):
    import av
    mp4 = str(tmp_path / "silent.mp4")
    with av.open(mp4, "w") as c:
        v = c.add_stream("mpeg4", rate=10)
        v.width, v.height, v.pix_fmt = 64, 48, "yuv420p"
        for p in v.encode(av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")):
            c.mux(p)
        for p in v.encode():
            c.mux(p)
    with pytest.raises(ValueError):
        subtitles.shrink_audio(mp4, str(tmp_path))


class _Resp:
    def __init__(self, body, code=200):
        self.body, self.status_code = body, code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.body


def test_transcribe_segments_local_raw_file_and_corrections(tmp_path, monkeypatch):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    seen = {}

    def post(url, files=None, data=None, headers=None, timeout=None, **kw):
        seen.update(url=url, data=data)
        return _Resp({"text": "corey said hi", "segments": [
            {"start": 0, "end": 1.5, "text": " corey said hi"}]})
    monkeypatch.setattr(tc, "_words", lambda: ([], {"Corey": "Cory"}))
    # Default: exactly what Whisper heard.
    text, segs = tc.transcribe_segments(str(f), {"WHISPER_API_PORT": "5055"}, post=post)
    assert seen["url"] == "http://127.0.0.1:5055/v1/audio/transcriptions"
    assert seen["data"] == {"response_format": "verbose_json", "processing": "raw", "source": "file"}
    assert text == "corey said hi" and segs == [{"start": 0.0, "end": 1.5, "text": "corey said hi"}]
    # Opted in: the name corrections.
    text, segs = tc.transcribe_segments(str(f), {"WHISPER_API_PORT": "5055"}, post=post,
                                        apply_corrections=True)
    assert text == "Cory said hi" and segs == [{"start": 0.0, "end": 1.5, "text": "Cory said hi"}]


def test_transcribe_segments_remote_uses_its_corrections(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")

    def get(url, headers=None, timeout=None):
        assert url == "http://pc:5000/v1/vocabulary"
        return _Resp({"name_corrections": {"Emma": "Ame"}})

    def post(url, files=None, data=None, headers=None, timeout=None, **kw):
        assert url == "http://pc:5000/v1/audio/transcriptions" and headers == {"Authorization": "Bearer k"}
        return _Resp({"text": "emma", "segments": [{"start": 0, "end": 1, "text": "emma"}]})
    env = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "http://pc:5000/v1", "REMOTE_API_KEY": "k"}
    assert tc.transcribe_segments(str(f), env, post=post, get=get, apply_corrections=True)[0] == "Ame"

    def no_get(*a, **k):
        raise AssertionError("corrections off: the server's list isn't fetched")
    assert tc.transcribe_segments(str(f), env, post=post, get=no_get)[0] == "emma"


def test_transcribe_segments_reports_server_error(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    post = lambda *a, **k: _Resp({"error": {"message": "Audio file is larger than 100 MB."}}, 413)
    with pytest.raises(tc.TranscribeError, match="larger than 100 MB"):
        tc.transcribe_segments(str(f), {}, post=post)


def test_run_job_shrinks_video_and_cleans_up(tmp_path, monkeypatch):
    import file_window
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"not really a video")
    made = {}

    def fake_shrink(path, out_dir):
        made["dir"] = out_dir
        p = os.path.join(out_dir, "talk.ogg")
        open(p, "wb").close()
        return p
    monkeypatch.setattr(subtitles, "shrink_audio", fake_shrink)
    events = []
    file_window.run_job(str(video), {}, lambda k, v: events.append((k, v)),
                        transcribe=lambda p, env: ("hi", [{"start": 0, "end": 1, "text": "hi"}]))
    assert [k for k, _ in events] == ["status", "status", "done"]
    assert not os.path.exists(made["dir"])   # temp audio removed


def test_run_job_reports_errors(tmp_path):
    import file_window
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")

    def boom(p, env):
        raise tc.TranscribeError("Couldn't reach this PC's server (ConnectionError).")
    events = []
    file_window.run_job(str(wav), {}, lambda k, v: events.append((k, v)), transcribe=boom)
    assert events[-1] == ("error", "Couldn't reach this PC's server (ConnectionError).")


def test_cues_break_at_sentence_ends():
    t = ("Hello Cory. This is a test of the file transcription window. It should write "
         "subtitles with the right timing. Here is a second sentence, so there is more "
         "than one line to check.")
    texts = [c[2] for c in subtitles.split_cues([{"start": 0, "end": 12, "text": t}])]
    assert all(x.endswith(".") for x in texts)
    assert texts[0] == "Hello Cory. This is a test of the file transcription window."
