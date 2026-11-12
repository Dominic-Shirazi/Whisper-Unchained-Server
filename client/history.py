"""Past dictations, read from this PC's transcript log (api/transcripts.log).

The log is written by the server when "Keep a log of transcripts" is on
(Settings > Audio Corpus). One record per dictation:

    --- 2026-11-12 01:48:54 | lang=en dur=2.7s dropped=0 ---
    CONF : ...
    AUDIO: keep/clip.wav           (only when source clips are kept)
    RAW  : 'what Whisper heard'
    FINAL: 'after corrections / edits / cleanup'   (only when different)

Read-only: this module never writes the log.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "api", "transcripts.log")
CORPUS_DIR = os.path.join(ROOT, "api", "audio_corpus")

_HEADER = re.compile(r"^--- (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:.*?dur=([\d.]+)s)?")


class Entry:
    __slots__ = ("when", "duration", "raw", "final", "audio")

    def __init__(self, when, duration):
        self.when, self.duration = when, duration
        self.raw, self.final, self.audio = "", None, ""

    @property
    def text(self):
        """What was pasted: the final text, else the raw text."""
        return self.final if self.final is not None else self.raw

    def audio_path(self, corpus_dir=CORPUS_DIR):
        if not self.audio:
            return ""
        p = os.path.normpath(os.path.join(corpus_dir, self.audio))
        # Only ever inside the corpus folder.
        if not p.startswith(os.path.normpath(corpus_dir) + os.sep):
            return ""
        return p if os.path.isfile(p) else ""


def _value(line):
    v = line.split(":", 1)[1].strip()
    try:
        out = ast.literal_eval(v)
        return out if isinstance(out, str) else str(out)
    except (ValueError, SyntaxError):
        return v


def parse(lines):
    """Entries in file order (oldest first). Unknown lines are skipped."""
    entries, cur = [], None
    for line in lines:
        line = line.rstrip("\r\n")
        m = _HEADER.match(line)
        if m:
            cur = Entry(m.group(1), float(m.group(2)) if m.group(2) else None)
            entries.append(cur)
        elif cur is None:
            continue
        elif line.startswith("RAW  :"):
            cur.raw = _value(line)
        elif line.startswith("FINAL:"):
            cur.final = _value(line)
        elif line.startswith("AUDIO:"):
            cur.audio = line.split(":", 1)[1].strip()
    return [e for e in entries if e.text.strip()]


def load(path=LOG_PATH):
    """Newest first. [] if there's no log."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(reversed(parse(f)))
    except OSError:
        return []


def search(entries, query):
    """Entries whose text (raw or final) contains every word of the query."""
    words = (query or "").lower().split()
    if not words:
        return list(entries)
    out = []
    for e in entries:
        hay = (e.raw + " " + (e.final or "")).lower()
        if all(w in hay for w in words):
            out.append(e)
    return out
