"""File transcription helpers for the "Transcribe a file" window: subtitles
(SRT / WebVTT) from timed segments, and shrinking audio or video to a small
speech-only file before upload.

Why shrink: the server takes at most 100 MB and cloud services 25 MB, while a
phone video is often bigger. Opus at 24 kbit/s mono is about 10 MB per hour and
keeps speech intact; timestamps don't move.
"""
import os
import re
import tempfile

MAX_LINE = 42          # characters per subtitle line (two lines per cue)
MAX_CUE = MAX_LINE * 2
SMALL_ENOUGH = 20 * 1024 * 1024
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm", ".aac", ".wma")


def _stamp(seconds, sep):
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d%s%03d" % (h, m, s, sep, ms)


def _wrap(text):
    """One or two lines, broken at the space nearest the middle."""
    if len(text) <= MAX_LINE:
        return text
    mid = len(text) // 2
    spaces = [i for i, c in enumerate(text) if c == " "]
    if not spaces:
        return text
    cut = min(spaces, key=lambda i: abs(i - mid))
    return text[:cut] + "\n" + text[cut + 1:]


def split_cues(segments):
    """[(start, end, text)] with long segments split into readable cues. Time is
    shared out by character count, which is close enough for speech."""
    cues = []
    for seg in segments:
        start, end = float(seg["start"]), float(seg["end"])
        text = " ".join((seg.get("text") or "").split())
        if not text:
            continue
        if len(text) <= MAX_CUE:
            cues.append((start, end, text))
            continue
        # Break at sentence ends first, then pack sentences into cues; only a
        # sentence too long for one cue is split between words.
        pieces = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            cur = ""
            for word in sentence.split(" "):
                if cur and len(cur) + 1 + len(word) > MAX_CUE:
                    pieces.append(cur)
                    cur = word
                else:
                    cur = (cur + " " + word).strip()
            if cur:
                pieces.append(cur)
        chunks = []
        for p in pieces:
            if chunks and len(chunks[-1]) + 1 + len(p) <= MAX_CUE:
                chunks[-1] += " " + p
            else:
                chunks.append(p)
        total = sum(len(c) for c in chunks)
        t = start
        for c in chunks:
            d = (end - start) * len(c) / total
            cues.append((t, t + d, c))
            t += d
    return cues


def to_srt(segments):
    out = []
    for i, (a, b, text) in enumerate(split_cues(segments), 1):
        out.append("%d\n%s --> %s\n%s\n" % (i, _stamp(a, ","), _stamp(b, ","), _wrap(text)))
    return "\n".join(out)


def to_vtt(segments):
    out = ["WEBVTT\n"]
    for a, b, text in split_cues(segments):
        out.append("%s --> %s\n%s\n" % (_stamp(a, "."), _stamp(b, "."), _wrap(text)))
    return "\n".join(out)


def needs_shrinking(path):
    ext = os.path.splitext(path)[1].lower()
    return ext not in AUDIO_EXTS or os.path.getsize(path) > SMALL_ENOUGH


def shrink_audio(path, out_dir=None):
    """Audio track of `path` -> a 16 kHz mono Opus .ogg in out_dir (a temp folder
    by default). Returns the new path; the caller deletes it. Raises ValueError
    if the file has no audio."""
    import av

    out_dir = out_dir or tempfile.mkdtemp(prefix="wu_file_")
    dest = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".ogg")
    with av.open(path) as src:
        if not src.streams.audio:
            raise ValueError("This file has no sound track.")
        with av.open(dest, "w", format="ogg") as dst:
            stream = dst.add_stream("libopus", rate=48000)
            stream.bit_rate = 24000
            stream.layout = "mono"
            resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            for frame in src.decode(audio=0):
                for f in resampler.resample(frame):
                    for packet in stream.encode(f):
                        dst.mux(packet)
            for f in resampler.resample(None):
                for packet in stream.encode(f):
                    dst.mux(packet)
            for packet in stream.encode(None):
                dst.mux(packet)
    return dest
