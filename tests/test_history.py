"""History: parsing the transcript log (synthetic, never the real one)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import history  # noqa: E402

LOG = """--- 2026-11-10 09:00:00 | lang=en dur=2.0s dropped=0 ---
CONF : wlogp=-0.1 minlogp=-0.2 mean_nsp=0.01 segs=1
RAW  : "leslie it's on"
FINAL: "Lesley, it's on."
--- 2026-11-11 10:00:00 | lang=en dur=3.5s dropped=1 ---
CONF : wlogp=-0.1 minlogp=-0.2 mean_nsp=0.01 segs=1
AUDIO: keep/clip-1.wav
RAW  : 'Ship the build tonight'
DROP : nsp=0.90 logp=-1.00 cr=1.00 ' you'
--- 2026-11-11 11:00:00 | lang=en dur=0.5s dropped=0 ---
RAW  : ''
"""


def test_parse_newest_first_and_skips_empty(tmp_path):
    p = tmp_path / "transcripts.log"
    p.write_text(LOG, encoding="utf-8")
    entries = history.load(str(p))
    assert [e.when for e in entries] == ["2026-11-11 10:00:00", "2026-11-10 09:00:00"]
    assert entries[0].text == "Ship the build tonight" and entries[0].duration == 3.5
    assert entries[1].raw == "leslie it's on" and entries[1].text == "Lesley, it's on."


def test_search_all_words_any_case_raw_or_final(tmp_path):
    p = tmp_path / "transcripts.log"
    p.write_text(LOG, encoding="utf-8")
    entries = history.load(str(p))
    assert [e.when for e in history.search(entries, "LESLIE on")] == ["2026-11-10 09:00:00"]
    assert len(history.search(entries, "")) == 2
    assert history.search(entries, "ship nothing") == []


def test_missing_log_is_empty(tmp_path):
    assert history.load(str(tmp_path / "none.log")) == []


def test_audio_path_only_inside_corpus(tmp_path):
    corpus = tmp_path / "audio_corpus"
    (corpus / "keep").mkdir(parents=True)
    (corpus / "keep" / "clip-1.wav").write_bytes(b"RIFF")
    e = history.Entry("2026-11-11 10:00:00", 1.0)
    e.audio = "keep/clip-1.wav"
    assert e.audio_path(str(corpus)).endswith("clip-1.wav")
    e.audio = "../../secret.txt"
    assert e.audio_path(str(corpus)) == ""
    e.audio = "keep/missing.wav"
    assert e.audio_path(str(corpus)) == ""
