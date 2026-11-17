"""whisper.cpp as a second, optional speech engine (WHISPER_ENGINE=whisper.cpp).

Why this exists: faster-whisper (CTranslate2) only speeds up on NVIDIA cards.
whisper.cpp built with Vulkan also runs on AMD and Intel graphics. This module
makes whisper.cpp look like the one thing the server uses from faster-whisper:

    segments, info = model.transcribe(path_or_audio, **kwargs)

How: it starts whisper.cpp's own `whisper-server` program as a hidden child
process on 127.0.0.1 (free port picked at run time), keeps it running (the
first Vulkan request compiles shaders and takes 5 to 10 s, so it is warmed
once at load), and posts each clip to its /inference route.

Inputs:  a path, a file object or a 16 kHz float array, plus faster-whisper
         style keyword arguments (see build_request_fields for which are used).
Outputs: (list of Segment, Info) with the attributes api/whisper_api.py reads.

Layout: pure helpers first (no process, no network: unit-tested directly),
then the audio preparation, then the process handling, then load_from_env().
Nothing here imports faster_whisper at module level, and nothing runs unless
the server is started with WHISPER_ENGINE=whisper.cpp.
"""
import atexit
import collections
import io
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import wave
import zlib

ENGINE_FASTER_WHISPER = "faster-whisper"
ENGINE_WHISPER_CPP = "whisper.cpp"
_CPP_SPELLINGS = {"whisper.cpp", "whispercpp", "whisper-cpp", "whisper_cpp"}
_FW_SPELLINGS = {"", "faster-whisper", "faster_whisper", "fasterwhisper"}

SAMPLE_RATE = 16000
DEFAULT_BIN = os.path.join("api", "bin", "whispercpp",
                           "whisper-server.exe" if os.name == "nt" else "whisper-server")
DEFAULT_MODELS_DIR = "models"
DEFAULT_VAD_NAME = "ggml-silero-v5.1.2.bin"
AUTO_CACHE_NAME = "whispercpp_auto.json"   # the "auto" timing memory
# Six seconds of speech (made with Windows' built-in voice) for the "auto"
# timing test. Silence is no good for it: the model writes a long made-up
# line for silence, which times the wrong kind of work.
SPEED_TEST_CLIP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "assets", "speed_test.wav")

# whisper-server names the language in full ("english"); the server's callers
# expect faster-whisper's short codes ("en"). Same table as OpenAI Whisper.
LANGUAGE_CODES = {
    "english": "en", "chinese": "zh", "german": "de", "spanish": "es", "russian": "ru",
    "korean": "ko", "french": "fr", "japanese": "ja", "portuguese": "pt", "turkish": "tr",
    "polish": "pl", "catalan": "ca", "dutch": "nl", "arabic": "ar", "swedish": "sv",
    "italian": "it", "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi",
    "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms", "czech": "cs",
    "romanian": "ro", "danish": "da", "hungarian": "hu", "tamil": "ta", "norwegian": "no",
    "thai": "th", "urdu": "ur", "croatian": "hr", "bulgarian": "bg", "lithuanian": "lt",
    "latin": "la", "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk",
    "telugu": "te", "persian": "fa", "latvian": "lv", "bengali": "bn", "serbian": "sr",
    "azerbaijani": "az", "slovenian": "sl", "kannada": "kn", "estonian": "et",
    "macedonian": "mk", "breton": "br", "basque": "eu", "icelandic": "is", "armenian": "hy",
    "nepali": "ne", "mongolian": "mn", "bosnian": "bs", "kazakh": "kk", "albanian": "sq",
    "swahili": "sw", "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si",
    "khmer": "km", "shona": "sn", "yoruba": "yo", "somali": "so", "afrikaans": "af",
    "occitan": "oc", "georgian": "ka", "belarusian": "be", "tajik": "tg", "sindhi": "sd",
    "gujarati": "gu", "amharic": "am", "yiddish": "yi", "lao": "lo", "uzbek": "uz",
    "faroese": "fo", "haitian creole": "ht", "pashto": "ps", "turkmen": "tk",
    "nynorsk": "nn", "maltese": "mt", "sanskrit": "sa", "luxembourgish": "lb",
    "myanmar": "my", "tibetan": "bo", "tagalog": "tl", "malagasy": "mg", "assamese": "as",
    "tatar": "tt", "hawaiian": "haw", "lingala": "ln", "hausa": "ha", "bashkir": "ba",
    "javanese": "jw", "sundanese": "su", "cantonese": "yue",
}


class WhisperCppError(RuntimeError):
    """Anything that stops whisper.cpp from loading or answering."""


class Word:
    __slots__ = ("start", "end", "word", "probability")

    def __init__(self, start, end, word, probability):
        self.start, self.end, self.word, self.probability = start, end, word, probability

    def __repr__(self):
        return "Word(%r, %.2f-%.2f, p=%.2f)" % (self.word, self.start, self.end,
                                                self.probability)


class Segment:
    """The attributes api/whisper_api.py reads from a faster-whisper Segment."""
    __slots__ = ("id", "seek", "start", "end", "text", "tokens", "temperature",
                 "avg_logprob", "compression_ratio", "no_speech_prob", "words")

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    def __repr__(self):
        return "Segment(%.2f-%.2f %r)" % (self.start, self.end, self.text)


class Info:
    """The attributes api/whisper_api.py reads from faster-whisper's info."""
    __slots__ = ("language", "language_probability", "duration",
                 "duration_after_vad", "all_language_probs")

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))


# --- Pure helpers ---------------------------------------------------------------

def engine_name(value, log=None):
    """Which engine WHISPER_ENGINE asks for. Unset or empty means faster-whisper,
    so a server with no new setting behaves exactly as before. An unknown value
    also means faster-whisper, with a note in the log."""
    v = (value or "").strip().lower()
    if v in _CPP_SPELLINGS:
        return ENGINE_WHISPER_CPP
    if v not in _FW_SPELLINGS and log:
        log("[API] Unknown WHISPER_ENGINE '%s'; using faster-whisper." % value)
    return ENGINE_FASTER_WHISPER


def resolve_binary(value, repo_root):
    """Full path of whisper-server from WHISPER_CPP_BIN (relative = in the repo)."""
    path = (value or "").strip().strip('"') or DEFAULT_BIN
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(repo_root, path))


def resolve_model(value, models_dir, repo_root):
    """Full path of the GGML model from WHISPER_CPP_MODEL.

    A file path (anything with a slash, or ending in .bin) is used as given
    (relative = in the repo). A bare name such as "base" or
    "large-v3-turbo-q5_0" means ggml-<name>.bin in the models folder.
    Returns None when the setting is empty."""
    v = (value or "").strip().strip('"')
    if not v:
        return None
    if os.path.isabs(v) or "/" in v or "\\" in v or v.lower().endswith(".bin"):
        return os.path.normpath(v if os.path.isabs(v) else os.path.join(repo_root, v))
    folder = models_dir or DEFAULT_MODELS_DIR
    if not os.path.isabs(folder):
        folder = os.path.join(repo_root, folder)
    name = v if v.lower().startswith("ggml-") else "ggml-" + v
    return os.path.normpath(os.path.join(folder, name + ".bin"))


def model_display_name(model_path):
    """"ggml-large-v3-turbo-q5_0.bin" -> "large-v3-turbo-q5_0" (for /health)."""
    name = os.path.basename(str(model_path or ""))
    if name.lower().endswith(".bin"):
        name = name[:-4]
    if name.lower().startswith("ggml-"):
        name = name[5:]
    return name


def normalize_device(value, log=None):
    """WHISPER_CPP_DEVICE as "auto", "cpu" or a Vulkan device number string."""
    v = (value or "").strip().lower() or "auto"
    if v in ("auto", "cpu"):
        return v
    if v.isdigit():
        return str(int(v))
    if log:
        log("[API] Unknown WHISPER_CPP_DEVICE '%s'; using auto." % value)
    return "auto"


def device_args(device):
    """whisper-server options for a device choice. auto adds nothing:
    whisper.cpp then uses graphics device 0, or the processor if there is none."""
    device = normalize_device(device)
    if device == "cpu":
        return ["-ng"]
    if device == "auto":
        return []
    return ["-dev", device]


def build_server_args(model_path, port, device="auto", threads=None, vad_model=None,
                      host="127.0.0.1"):
    """The whisper-server command line after the program name (options as shown
    by `whisper-server --help` for v1.9.5)."""
    args = ["-m", str(model_path), "--host", host, "--port", str(int(port))]
    args += device_args(device)
    if threads:
        args += ["-t", str(int(threads))]
    if vad_model:
        args += ["-vm", str(vad_model)]
    return args


_VK_DEVICE_RE = re.compile(r"ggml_vulkan:\s*(\d+)\s*=\s*(.+?)\s*\|\s*uma:\s*(\d)")
_GPU_USED_RE = re.compile(r"whisper_backend_init_gpu: (?:found GPU device|device) \d+: Vulkan(\d+)")


def parse_startup_log(lines):
    """What whisper-server said about graphics devices while starting.

    Returns {"devices": [{"index", "name", "integrated"}], "gpu_index": int or
    None}. gpu_index None means it runs on the processor."""
    devices, used = [], None
    for line in lines:
        m = _VK_DEVICE_RE.search(line)
        if m:
            index = int(m.group(1))
            if all(d["index"] != index for d in devices):
                devices.append({"index": index, "name": m.group(2).strip(),
                                "integrated": m.group(3) == "1"})
            continue
        m = _GPU_USED_RE.search(line)
        if m:
            used = int(m.group(1))
        elif "whisper_backend_init_gpu: no GPU found" in line:
            used = None
    return {"devices": devices, "gpu_index": used}


def pick_device(devices):
    """Best Vulkan device for "auto": the first separate graphics card, else the
    first device listed, else None (no Vulkan device: the processor)."""
    if not devices:
        return None
    for d in devices:
        if not d.get("integrated"):
            return d["index"]
    return devices[0]["index"]


# --- "auto": which device to run on ------------------------------------------------
# A separate graphics card is always used. Built-in (integrated) graphics are
# not assumed to be faster than the processor: on a PC with an Intel UHD 630
# and an i5-9600K the processor won every test. So with only built-in graphics
# both are timed once, the faster is kept, and the answer is remembered in a
# small file next to the models so later starts do not test again.

def engine_version(binary):
    """Which build of whisper-server this is, for the timing memory: the version
    the downloader wrote next to it (engine.json), else the program's size."""
    try:
        with open(os.path.join(os.path.dirname(binary), "engine.json"), encoding="utf-8") as f:
            version = json.load(f).get("version")
        if version:
            return str(version)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return "size-%d" % os.path.getsize(binary)
    except OSError:
        return "unknown"


def auto_cache_path(model_path):
    """The timing memory lives next to the models."""
    return os.path.join(os.path.dirname(str(model_path)) or ".", AUTO_CACHE_NAME)


def auto_cache_key(device_name, model_path, version):
    """One answer per graphics device, model file and engine build: a new
    card, another model or an engine update each get a new test."""
    return "%s|%s|%s" % (device_name, os.path.basename(str(model_path)), version)


def read_auto_cache(path):
    """{"tests": {key: {choice, gpu_seconds, cpu_seconds}}, "last": {...}}.
    A missing or unreadable file is an empty memory."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("tests"), dict):
        data["tests"] = {}
    return data


def write_auto_cache(path, key=None, entry=None, last=None):
    """Remember one test result and/or what the last start chose. Never raises:
    a memory that cannot be written only means the test runs again next time."""
    try:
        data = read_auto_cache(path)
        if key and entry:
            data["tests"][key] = entry
        if last is not None:
            data["last"] = last
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def faster_choice(gpu_seconds, cpu_seconds):
    """"gpu" or "cpu" from the two timings. None means that one could not be
    timed. The graphics have to be really faster: a tie goes to the processor,
    which needs no graphics memory."""
    if gpu_seconds is None:
        return "cpu"
    if cpu_seconds is None:
        return "gpu"
    return "gpu" if gpu_seconds < cpu_seconds else "cpu"


def plan_auto(devices, cache, model_path, version):
    """What "auto" should do with the Vulkan devices whisper-server listed.

    Returns {"action", "index", "name", "key", "entry"}:
      action "use":  run on graphics device `index` (a separate card, or
                     built-in graphics that won an earlier test: `entry`).
      action "cpu":  run on the processor (no device, or it won an earlier test).
      action "test": only built-in graphics and no remembered answer: time
                     device `index` and the processor, keep the faster."""
    plan = {"action": "cpu", "index": None, "name": None, "key": None, "entry": None}
    if not devices:
        return plan
    for d in devices:
        if not d.get("integrated"):
            plan.update(action="use", index=d["index"], name=d.get("name"))
            return plan
    d = devices[0]
    key = auto_cache_key(d.get("name"), model_path, version)
    plan.update(index=d["index"], name=d.get("name"), key=key)
    entry = (cache or {}).get("tests", {}).get(key)
    if isinstance(entry, dict) and entry.get("choice") in ("gpu", "cpu"):
        plan.update(action="use" if entry["choice"] == "gpu" else "cpu", entry=entry)
    else:
        plan["action"] = "test"
    return plan


def _seconds(value):
    return "could not be tested" if value is None else "%.1f s" % value


def auto_log_line(name, choice, gpu_seconds, cpu_seconds, remembered=False):
    """The one line in the log that says what was chosen and the two timings."""
    chosen = ("the graphics (%s)" % name) if choice == "gpu" else "the processor"
    return "[API] whisper.cpp: using %s. %s: graphics (%s) %s, processor %s." % (
        chosen, "Tested on an earlier start" if remembered else "Short test",
        name, _seconds(gpu_seconds), _seconds(cpu_seconds))


def speed_test_clip():
    """WAV bytes for the timing test: the bundled speech clip, or five seconds
    of silence if that file is gone (a rougher test, but still the same work
    on both devices)."""
    if is_ready_wav(SPEED_TEST_CLIP):
        with open(SPEED_TEST_CLIP, "rb") as f:
            return f.read()
    import numpy as np
    return _pcm16_wav_bytes(np.zeros(SAMPLE_RATE * 5, dtype=np.float32))


def _temperature_fields(temperature):
    """faster-whisper takes one temperature (no retries) or a list to step
    through; whisper.cpp takes a start and a step."""
    if isinstance(temperature, (list, tuple)):
        if not temperature:
            return {}
        first = float(temperature[0])
        step = float(temperature[1]) - first if len(temperature) > 1 else 0.0
        return {"temperature": "%g" % first, "temperature_inc": "%g" % max(step, 0.0)}
    return {"temperature": "%g" % float(temperature), "temperature_inc": "0"}


# faster-whisper keyword arguments this engine knowingly does nothing with.
# condition_on_previous_text: whisper-server always decodes each 30 s window
# without the previous one's text (its no_context is fixed on), which is what
# every call site in the server asks for (False) anyway.
IGNORED_KWARGS = ("condition_on_previous_text", "max_new_tokens", "compression_ratio_threshold",
                  "suppress_tokens", "suppress_blank", "patience", "length_penalty",
                  "repetition_penalty", "no_repeat_ngram_size", "prefix", "chunk_length",
                  "clip_timestamps", "hallucination_silence_threshold", "multilingual",
                  "language_detection_threshold", "language_detection_segments",
                  "prepend_punctuations", "append_punctuations", "prompt_reset_on_temperature",
                  "log_progress", "max_initial_timestamp")

_VAD_PARAM_FIELDS = {
    "threshold": "vad_threshold",
    "min_speech_duration_ms": "vad_min_speech_duration_ms",
    "min_silence_duration_ms": "vad_min_silence_duration_ms",
    "max_speech_duration_s": "vad_max_speech_duration_s",
    "speech_pad_ms": "vad_speech_pad_ms",
}


def build_request_fields(kwargs, vad_available=False):
    """Turn faster-whisper transcribe() keyword arguments into whisper-server
    /inference form fields. Returns (fields, ignored) where ignored lists the
    arguments that were passed but have no whisper-server equivalent.

    Used: language, task, beam_size, best_of, temperature, initial_prompt,
    hotwords, word_timestamps, without_timestamps, no_speech_threshold,
    log_prob_threshold, and vad_parameters when the engine was started with a
    VAD model. vad_filter itself is decided by whether there is a VAD model
    (see below), so asking for the opposite lands in `ignored`."""
    kw = dict(kwargs or {})
    fields = {
        "response_format": "verbose_json",
        # whisper-server's own default is "en"; faster-whisper's is detection.
        "language": (kw.pop("language", None) or "auto"),
        # Working out a probability for every language costs one more pass over
        # the audio and nothing in the server reads it.
        "no_language_probabilities": "true",
        # Same as faster-whisper's default suppress_tokens=[-1]: no "[BLANK_AUDIO]",
        # music notes or bracketed sound descriptions in a dictation.
        "suppress_nst": "true",
    }
    if kw.pop("task", "transcribe") == "translate":
        fields["translate"] = "true"

    beam = kw.pop("beam_size", None)
    if beam:
        fields["beam_size"] = str(int(beam))
    best_of = kw.pop("best_of", None)
    if best_of:
        fields["best_of"] = str(int(best_of))
    temperature = kw.pop("temperature", None)
    if temperature is not None:
        fields.update(_temperature_fields(temperature))

    # Vocabulary. faster-whisper puts hotwords in front of EVERY 30 s window;
    # whisper.cpp does the same for its prompt when carry_initial_prompt is on.
    initial_prompt = kw.pop("initial_prompt", None)
    hotwords = kw.pop("hotwords", None)
    prompt = " ".join(p.strip() for p in (initial_prompt, hotwords)
                      if isinstance(p, str) and p.strip())
    if prompt:
        fields["prompt"] = prompt
        if hotwords:
            fields["carry_initial_prompt"] = "true"

    words = bool(kw.pop("word_timestamps", False))
    if kw.pop("without_timestamps", False):
        fields["no_timestamps"] = "true"
        words = False
    if words:
        # One word per returned segment (whisper.cpp's own recipe for word
        # times). Measured on v1.9.5: this is the only layout whose times are
        # right when VAD has cut silence out; the per-token times inside a
        # segment stay on the shortened, silence-free clock. map_response()
        # puts the words back together into sentences.
        fields.update(token_timestamps="true", split_on_word="true", max_len="1")
    else:
        # Off: otherwise verbose_json turns them on and cuts segments at 60
        # characters, mid-word. Word probabilities still come back.
        fields["token_timestamps"] = "false"

    for ours, theirs in (("no_speech_threshold", "no_speech_thold"),
                         ("log_prob_threshold", "logprob_thold")):
        value = kw.pop(ours, None)
        if value is not None:
            fields[theirs] = "%g" % float(value)

    # VAD is all or nothing for one whisper-server process. Measured on
    # v1.9.5: after any request with VAD, a later request without it gets
    # segment times run through the earlier clip's silence map (wrong times).
    # So with a VAD model every request uses it, and without one none does.
    ignored = []
    vad = kw.pop("vad_filter", None)
    vad_params = kw.pop("vad_parameters", None)
    if vad_available:
        fields["vad"] = "true"
        if isinstance(vad_params, dict):
            for ours, theirs in _VAD_PARAM_FIELDS.items():
                if vad_params.get(ours) is not None:
                    fields[theirs] = "%g" % float(vad_params[ours])
        if vad is False:
            ignored.append("vad_filter")
    elif vad:
        ignored.append("vad_filter")
    ignored.extend(sorted(kw))
    return fields, ignored


def compression_ratio(text):
    """Same formula as faster-whisper and OpenAI Whisper: how hard the text
    compresses. Repetitive text ("I'm I'm I'm ...") scores high."""
    data = (text or "").encode("utf-8")
    if not data:
        return 0.0
    return len(data) / len(zlib.compress(data))


def _avg_logprob(seg):
    """Mean log probability of the segment's text tokens.

    whisper-server's own avg_logprob divides by a token count that includes the
    timestamp tokens it left out of the sum, which makes short segments look
    more confident than they are (a 3-token "Thank you." would score 40% closer
    to zero). The server's silence gate needs the real figure for exactly those
    short segments, so it is worked out again here from the token probabilities."""
    probs = [float(t.get("probability") or 0.0) for t in seg.get("words") or []]
    if probs:
        return sum(math.log(max(p, 1e-10)) for p in probs) / len(probs)
    value = seg.get("avg_logprob")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


_SENTENCE_END = (".", "?", "!", "\u3002", "\uff1f", "\uff01")
# A silence this long between two words also starts a new segment (seconds).
WORD_GAP_SPLIT = 1.0


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _num(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _segments_from_words(raw_segments):
    """Word mode: whisper-server was asked for one word per segment, so each
    raw segment is a word with trustworthy start and end. Join them back into
    segments: a new one starts after a word that ends a sentence, or after a
    silence of WORD_GAP_SPLIT seconds. (faster-whisper keeps Whisper's own
    segment breaks instead; these are close but not identical.)"""
    groups, current = [], []
    for seg in raw_segments or []:
        text = seg.get("text") or ""
        if not text.strip():
            continue
        start = _num(seg.get("start"))
        end = max(_num(seg.get("end"), start), start)
        probs = [_num(t.get("probability")) for t in seg.get("words") or []]
        item = {"word": Word(start, end, text, _mean(probs)), "probs": probs,
                "nsp": _num(seg.get("no_speech_prob")),
                "tokens": list(seg.get("tokens") or []),
                "temperature": _num(seg.get("temperature"))}
        if current:
            prev = current[-1]["word"]
            if prev.word.rstrip().endswith(_SENTENCE_END) or start - prev.end >= WORD_GAP_SPLIT:
                groups.append(current)
                current = []
        current.append(item)
    if current:
        groups.append(current)

    segments = []
    for i, group in enumerate(groups):
        words = [g["word"] for g in group]
        text = "".join(w.word for w in words)
        probs = [p for g in group for p in g["probs"]]
        logp = _mean([math.log(max(p, 1e-10)) for p in probs])
        segments.append(Segment(
            id=i + 1, seek=0, start=words[0].start, end=max(w.end for w in words),
            text=text, tokens=[t for g in group for t in g["tokens"]],
            temperature=group[0]["temperature"], avg_logprob=logp,
            compression_ratio=compression_ratio(text),
            no_speech_prob=max(g["nsp"] for g in group), words=words))
    return segments


def map_response(data, requested_language=None, word_timestamps=False):
    """whisper-server verbose_json -> (list of Segment, Info).

    Where each attribute comes from:
      text, start, end, no_speech_prob, temperature, tokens: whisper.cpp.
      avg_logprob: worked out from whisper.cpp's token probabilities (_avg_logprob).
      compression_ratio: whisper.cpp does not report it; worked out here from the
        text with the same formula faster-whisper uses, so the decode-loop gate
        behaves the same.
      words: None unless word_timestamps was asked for (as in faster-whisper).
        In word mode whisper.cpp returns one word per segment; word text, start
        and end come from that, probability is the mean of its tokens', and the
        segments are rebuilt from the words (_segments_from_words).
      seek: no equivalent; 0.
      info.language: whisper.cpp's language name turned into its short code.
      info.duration: whisper.cpp (length of the audio it was given).
      info.language_probability: not measured (it would cost a second pass over
        the audio and nothing reads it). 1.0, the value faster-whisper gives for
        a language that was set by the caller; a neutral value no check trips on.
      info.duration_after_vad: no equivalent; same as duration.
      info.all_language_probs: no equivalent; None."""
    if not isinstance(data, dict):
        raise WhisperCppError("whisper.cpp sent an answer that is not a JSON object.")
    if data.get("error"):
        raise WhisperCppError("whisper.cpp could not transcribe the audio: %s" % data["error"])
    name = str(data.get("language") or "").strip().lower()
    forced = (requested_language or "").strip().lower()
    forced = "" if forced == "auto" else forced
    language = LANGUAGE_CODES.get(name) or (name if len(name) in (2, 3) else "") or forced or "en"
    duration = float(data.get("duration") or 0.0)

    info = Info(language=language, language_probability=1.0, duration=duration,
                duration_after_vad=duration, all_language_probs=None)
    if word_timestamps:
        return _segments_from_words(data.get("segments")), info

    segments = []
    for seg in data.get("segments") or []:
        text = seg.get("text") or ""
        if not text.strip():
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") if seg.get("end") is not None else start)
        # Unknown counts as 0: that can never trip the "silence" gate on its own.
        nsp = _num(seg.get("no_speech_prob"))
        segments.append(Segment(
            id=len(segments) + 1, seek=0, start=start, end=max(end, start), text=text,
            tokens=list(seg.get("tokens") or []),
            temperature=float(seg.get("temperature") or 0.0),
            avg_logprob=_avg_logprob(seg), compression_ratio=compression_ratio(text),
            no_speech_prob=nsp, words=None))
    return segments, info


# --- Audio preparation ----------------------------------------------------------

def _pcm16_wav_bytes(samples):
    """A float array in -1..1 (16 kHz mono) as 16-bit WAV bytes."""
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def is_ready_wav(source):
    """True when a path or file object is already 16 kHz mono 16-bit PCM WAV,
    the one format sent to whisper-server untouched."""
    pos = None
    try:
        if hasattr(source, "read"):
            pos = source.tell()
        with wave.open(source, "rb") as w:
            return (w.getnchannels() == 1 and w.getframerate() == SAMPLE_RATE
                    and w.getsampwidth() == 2 and w.getcomptype() == "NONE")
    except Exception:
        return False
    finally:
        if pos is not None:
            try:
                source.seek(pos)
            except Exception:
                pass


def _decode_any(source):
    """Decode any audio or video container to 16 kHz mono float samples with
    PyAV: the same decoder faster-whisper feeds itself with, so every format the
    server accepts today (m4a from the phone, mp3, ogg, video files) still
    works. No ffmpeg program is needed."""
    try:
        from faster_whisper.audio import decode_audio
        return decode_audio(source, sampling_rate=SAMPLE_RATE)
    except ImportError:
        pass
    import av
    import numpy as np
    chunks = []
    with av.open(source) as container:
        if not container.streams.audio:
            raise WhisperCppError("There is no audio in this file.")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(container.streams.audio[0]):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        try:
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray())
        except Exception:
            pass
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=1)[0].astype(np.float32) / 32768.0


def prepare_wav(audio):
    """Bytes of a 16 kHz mono 16-bit WAV for whisper-server, from a path, a
    file object or a float array. Returns (wav_bytes, converted)."""
    if hasattr(audio, "dtype") and hasattr(audio, "shape"):  # numpy array
        return _pcm16_wav_bytes(audio), True
    if isinstance(audio, os.PathLike):
        audio = os.fspath(audio)
    if is_ready_wav(audio):
        if hasattr(audio, "read"):
            return audio.read(), False
        with open(audio, "rb") as f:
            return f.read(), False
    return _pcm16_wav_bytes(_decode_any(audio)), True


# --- Process handling -----------------------------------------------------------

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_with_parent_job(proc):
    """Windows: put the child in a job object that kills it when the last handle
    to the job closes. This process holds the only handle, so however it ends
    (normal exit, crash, Task Manager, service stop) Windows ends whisper-server
    too. Returns the job handle (keep it), or None if that could not be set up."""
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if ok:
            ok = k32.AssignProcessToJobObject(job, int(proc._handle))
        if not ok:
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _close_job(job):
    if job:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32")
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle(job)
        except Exception:
            pass


def _die_with_parent():
    """Linux: ask the kernel to kill the child when this process dies."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


class WhisperCppEngine:
    """Owns one whisper-server child process and talks to it.

    binary/model_path: files that must exist. device: "auto", "cpu" or a Vulkan
    device number. command_prefix replaces the program in the command line
    (tests start a small Python stand-in this way). auto_timer(device) returns
    the seconds one short request took on "cpu" or a device number, or None
    (replaceable so tests need no real timing); auto_cache is the file the
    "auto" answer is remembered in (default: next to the model)."""

    def __init__(self, binary, model_path, device="auto", threads=None, vad_model=None,
                 log=print, command_prefix=None, startup_timeout=180.0,
                 request_timeout=3600.0, warm_up=True, start=True,
                 auto_timer=None, auto_cache=None):
        self.binary = str(binary)
        self.model_path = str(model_path)
        self.device = normalize_device(device, log)
        self.threads = threads
        self.vad_model = str(vad_model) if vad_model else None
        self.log = log or (lambda *_a, **_k: None)
        self.command_prefix = list(command_prefix) if command_prefix else [self.binary]
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.warm_up = warm_up
        self.port = None
        self.gpu_index = None     # Vulkan device really in use, None = processor
        self.gpu_name = None
        self.restarts = 0
        self.last_ignored = []    # kwargs the last transcribe() could not use
        self._device_arg = self.device
        self.auto_timer = auto_timer or self._time_device
        self.auto_cache = auto_cache or auto_cache_path(self.model_path)
        self.auto_choice = None   # "cpu" or a device number once "auto" has decided
        self.auto_note = None     # why, when a test picked the processor
        self._warmed = False
        self._unloaded = False
        self._proc = None
        self._job = None
        self._stderr = collections.deque(maxlen=200)   # bounded: last 200 lines
        self._startup = []                             # lines up to "ready"
        self._collect_startup = False
        # One request at a time, like the single faster-whisper model. The
        # server already holds its own pipeline lock around dictations; this
        # also covers the routes that call transcribe() directly.
        self._lock = threading.RLock()
        self._closed = False
        self._session = None
        atexit.register(self.close)
        if start:
            self.start()

    # -- lifecycle --

    def start(self):
        """Start whisper-server, wait until it answers, warm it once. With
        device "auto", settle which device to use first (see plan_auto)."""
        with self._lock:
            self._closed = False
            self._unloaded = False
            self._launch()
            if self.device == "auto" and self.auto_choice is None:
                self._settle_auto()
            if self.warm_up and not self._warmed:
                self._warm()

    def _settle_auto(self):
        """Decide once what "auto" means on this PC and move to it. Later
        starts (after a pause or a crash) go straight to the same device."""
        found = parse_startup_log(self._startup)
        plan = plan_auto(found["devices"], read_auto_cache(self.auto_cache),
                         self.model_path, engine_version(self.binary))
        name, entry = plan["name"], plan["entry"]
        target = "cpu" if plan["action"] == "cpu" else str(plan["index"])
        if plan["action"] == "test":
            gpu_s = self.auto_timer(str(plan["index"]))
            cpu_s = self.auto_timer("cpu")
            choice = faster_choice(gpu_s, cpu_s)
            entry = {"choice": choice, "gpu_seconds": gpu_s, "cpu_seconds": cpu_s,
                     "tested": time.strftime("%Y-%m-%d")}
            write_auto_cache(self.auto_cache, plan["key"], entry)
            self.log(auto_log_line(name, choice, gpu_s, cpu_s))
            target = str(plan["index"]) if choice == "gpu" else "cpu"
        elif entry:
            self.log(auto_log_line(name, entry["choice"], entry.get("gpu_seconds"),
                                   entry.get("cpu_seconds"), remembered=True))
        elif plan["action"] == "use" and found["gpu_index"] != plan["index"]:
            self.log("[API] whisper.cpp: using the separate graphics card (%s)." % name)
        if entry and entry["choice"] == "cpu":
            self.auto_note = ("the processor was faster than %s in a short test "
                              "(%s against %s)" % (name, _seconds(entry.get("cpu_seconds")),
                                                   _seconds(entry.get("gpu_seconds"))))
        self._switch_to(target)
        self.auto_choice = target
        write_auto_cache(self.auto_cache, last={
            "device": "cpu" if self.gpu_index is None else "gpu",
            "name": self.gpu_name, "found": [d["name"] for d in found["devices"]],
            "model": os.path.basename(self.model_path),
            "gpu_seconds": (entry or {}).get("gpu_seconds"),
            "cpu_seconds": (entry or {}).get("cpu_seconds")})

    def _switch_to(self, device_arg):
        """Make sure whisper-server runs on device_arg ("cpu" or a number),
        starting it again only if it is on something else."""
        on = "cpu" if self.gpu_index is None else str(self.gpu_index)
        self._device_arg = device_arg
        if on != device_arg or not self.is_running():
            self._stop()
            self._launch()

    def _time_device(self, device_arg):
        """Seconds for one short request on device_arg, after a warm-up (so the
        graphics' one-off shader compile is not counted). None when the
        graphics device could not be used."""
        try:
            self._switch_to(device_arg)
            if device_arg != "cpu" and self.gpu_index is None:
                return None
            self._warm()
            # Shaped like a dictation (beam search), but no second tries at a
            # higher temperature: the same work on both.
            fields, _ = build_request_fields({"beam_size": 5, "temperature": 0.0})
            clip = speed_test_clip()
            t0 = time.time()
            self._post(clip, fields)
            return time.time() - t0
        except WhisperCppError as e:
            if device_arg == "cpu":
                raise
            self.log("[API] whisper.cpp: the graphics could not be tested (%s)."
                     % " ".join(str(e).split())[:200])
            return None

    def _launch(self):
        last_error = None
        for _attempt in range(3):  # a free port can be taken before the child binds it
            port = _free_port()
            args = self.command_prefix + build_server_args(
                self.model_path, port, self._device_arg, self.threads, self.vad_model)
            self._stderr.clear()
            self._startup = []
            self._collect_startup = True
            self._warmed = False
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            elif sys.platform.startswith("linux"):
                kwargs["preexec_fn"] = _die_with_parent
            try:
                proc = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, cwd=os.path.dirname(self.binary) or None,
                    **kwargs)
            except OSError as e:
                raise WhisperCppError("The whisper.cpp program at %s could not be started: %s"
                                      % (self.binary, e))
            self._proc, self.port = proc, port
            if os.name == "nt":
                self._job = _kill_with_parent_job(proc)
            threading.Thread(target=self._drain, args=(proc,), daemon=True,
                             name="whispercpp-stderr").start()
            try:
                self._wait_ready(proc)
            except WhisperCppError as e:
                last_error = e
                self._stop()
                if "listen failed" in str(e):
                    continue
                raise
            self._collect_startup = False
            found = parse_startup_log(self._startup)
            self.gpu_index = found["gpu_index"]
            self.gpu_name = next((d["name"] for d in found["devices"]
                                  if d["index"] == self.gpu_index), None)
            return
        raise last_error

    def _drain(self, proc):
        """Keep reading the child's messages so its pipe never fills up; keep
        only the newest lines, for error messages."""
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._stderr.append(line)
                if self._collect_startup and proc is self._proc and len(self._startup) < 400:
                    self._startup.append(line)
        except Exception:
            pass

    def _wait_ready(self, proc):
        deadline = time.time() + self.startup_timeout
        url = "http://127.0.0.1:%d/health" % self.port
        while time.time() < deadline:
            if proc.poll() is not None:
                time.sleep(0.2)  # let the reader thread collect the last lines
                raise WhisperCppError("whisper.cpp stopped while starting: %s"
                                      % self.error_tail())
            try:
                if self._http().get(url, timeout=2).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.15)
        raise WhisperCppError("whisper.cpp did not become ready within %d seconds: %s"
                              % (self.startup_timeout, self.error_tail()))

    def _warm(self):
        """One second of silence through the model. On Vulkan this is where the
        shaders get compiled (5 to 10 s), so the first real dictation is fast."""
        import numpy as np
        t0 = time.time()
        # Same shape as a real dictation (beam search, language detection) so
        # the same code paths are compiled. Never with VAD: it would skip the
        # silence and the model would not run at all.
        fields, _ = build_request_fields({"beam_size": 5})
        self._post(_pcm16_wav_bytes(np.zeros(SAMPLE_RATE, dtype=np.float32)), fields)
        self._warmed = True
        self.log("[API] whisper.cpp warmed up in %.1f s." % (time.time() - t0))

    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def error_tail(self, lines=6):
        """The last few things whisper-server said, on one line."""
        tail = [l for l in list(self._stderr)[-lines:] if l.strip()]
        return " | ".join(tail) if tail else "it gave no message"

    def _stop(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            except Exception:
                pass
            try:
                proc.stderr.close()
            except Exception:
                pass
        job, self._job = self._job, None
        _close_job(job)

    def unload(self):
        """Pause: stop whisper-server so its memory (graphics memory too) is
        free. The engine stays usable: reload() starts it again."""
        with self._lock:
            self._unloaded = True
            self._stop()

    def reload(self):
        """Resume after unload(): start whisper-server again."""
        with self._lock:
            if not self._closed and not self.is_running():
                self.start()

    def close(self):
        """Stop whisper-server. Safe to call more than once; runs at exit."""
        with self._lock:
            self._closed = True
            self._stop()
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None

    # -- requests --

    def _http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.trust_env = False  # never send 127.0.0.1 through a proxy
        return self._session

    def _post(self, wav_bytes, fields):
        url = "http://127.0.0.1:%d/inference" % self.port
        try:
            r = self._http().post(url, data=fields, timeout=self.request_timeout,
                                  files={"file": ("audio.wav", wav_bytes, "audio/wav")})
        except Exception as e:
            if not self.is_running():
                time.sleep(0.2)
                raise WhisperCppError("whisper.cpp stopped during a transcription: %s"
                                      % self.error_tail())
            raise WhisperCppError("whisper.cpp did not answer (%s)." % type(e).__name__)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code != 200 or not isinstance(data, dict) or data.get("error"):
            detail = (data or {}).get("error") if isinstance(data, dict) else None
            raise WhisperCppError("whisper.cpp could not transcribe the audio: %s"
                                  % (detail or "HTTP %d" % r.status_code))
        return data

    def transcribe(self, audio, **kwargs):
        """Same call shape as faster_whisper.WhisperModel.transcribe:
        returns (segments, info). segments is a list (faster-whisper gives a
        generator; every caller only loops over it)."""
        wav_bytes, _converted = prepare_wav(audio)
        fields, ignored = build_request_fields(kwargs, vad_available=bool(self.vad_model))
        with self._lock:
            if self._closed:
                raise WhisperCppError("whisper.cpp has been shut down.")
            self.last_ignored = ignored
            if not self.is_running():
                # It died since the last request (driver reset, out of memory,
                # killed by hand): start it again, once, then carry on.
                if not self._unloaded:
                    self.log("[API] whisper.cpp was not running; starting it again. Last "
                             "messages: %s" % self.error_tail())
                    self.restarts += 1
                self._stop()
                self.start()
            data = self._post(wav_bytes, fields)
        return map_response(data, requested_language=fields.get("language"),
                            word_timestamps=fields.get("token_timestamps") == "true")


# --- Loading from settings ------------------------------------------------------

def find_vad_model(value, models_dir, repo_root):
    """The silence filter (VAD model) for whisper.cpp: WHISPER_CPP_VAD_MODEL, or
    the newest ggml-silero*.bin in the models folder. None when there is none;
    the engine then does not load (see check_vad)."""
    path = resolve_model(value, models_dir, repo_root)
    if path:
        return path if os.path.isfile(path) else None
    folder = models_dir or DEFAULT_MODELS_DIR
    if not os.path.isabs(folder):
        folder = os.path.join(repo_root, folder)
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n.lower().startswith("ggml-silero") and n.lower().endswith(".bin"))
    except OSError:
        return None
    return os.path.join(folder, names[-1]) if names else None


def check_files(binary, model_path):
    """One plain sentence about the first thing that is missing, or None."""
    if not os.path.isfile(binary):
        return ("The whisper.cpp program is missing: it should be at %s "
                "(or set WHISPER_CPP_BIN to where whisper-server is)." % binary)
    if not model_path:
        return ("No whisper.cpp model is chosen: set WHISPER_CPP_MODEL to a GGML model "
                "file or to a name such as base.")
    if not os.path.isfile(model_path):
        return ("The whisper.cpp model is missing: it should be at %s "
                "(set by WHISPER_CPP_MODEL)." % model_path)
    return None


def check_vad(vad_model, value, models_dir, repo_root):
    """One plain sentence when the silence filter is missing, or None. It is
    required: without it whisper.cpp writes made-up text for silent recordings."""
    if vad_model:
        return None
    expected = resolve_model(value, models_dir, repo_root)
    if not expected:
        folder = models_dir or DEFAULT_MODELS_DIR
        if not os.path.isabs(folder):
            folder = os.path.join(repo_root, folder)
        expected = os.path.normpath(os.path.join(folder, DEFAULT_VAD_NAME))
    return ("The whisper.cpp silence filter is missing: it should be at %s "
            "(Settings > Transcription can download it; without it silent "
            "recordings come out as made-up text)." % expected)


def files_problem(env, repo_root, models_dir=None):
    """The first thing that would stop whisper.cpp from loading with these
    settings, as one plain sentence, or None. The same checks load_from_env
    makes, without starting anything (Settings asks before saving)."""
    binary = resolve_binary(env.get("WHISPER_CPP_BIN"), repo_root)
    model_path = resolve_model(env.get("WHISPER_CPP_MODEL"), models_dir, repo_root)
    value = env.get("WHISPER_CPP_VAD_MODEL")
    return check_files(binary, model_path) or check_vad(
        find_vad_model(value, models_dir, repo_root), value, models_dir, repo_root)


def load_from_env(env, repo_root, models_dir=None, threads=None, log=print,
                  engine_factory=WhisperCppEngine):
    """Load whisper.cpp from the WHISPER_CPP_* settings.

    Returns (engine, info) shaped like device_select.load_whisper(): engine is
    None when it could not load, and info["error"] then says why in one
    sentence. info = {engine, device ("vulkan" or "cpu"), compute_type, model,
    requested_device, fallback, cpu_threads, error, gpu_name}."""
    binary = resolve_binary(env.get("WHISPER_CPP_BIN"), repo_root)
    model_path = resolve_model(env.get("WHISPER_CPP_MODEL"), models_dir, repo_root)
    device = normalize_device(env.get("WHISPER_CPP_DEVICE"), log)
    info = {"engine": ENGINE_WHISPER_CPP, "requested_device": device,
            "model": model_display_name(model_path) or "", "device": None,
            "compute_type": None, "fallback": None, "cpu_threads": None,
            "error": None, "gpu_name": None}
    vad_value = env.get("WHISPER_CPP_VAD_MODEL")
    vad_model = find_vad_model(vad_value, models_dir, repo_root)
    problem = check_files(binary, model_path) or check_vad(
        vad_model, vad_value, models_dir, repo_root)
    if problem:
        info["error"] = problem
        log("[API] Error loading model: " + problem)
        return None, info
    log("[API] Loading %s with whisper.cpp (device: %s)..." % (info["model"], device))
    try:
        engine = engine_factory(binary, model_path, device=device, threads=threads,
                                vad_model=vad_model, log=log)
    except Exception as e:
        text = " ".join(str(e).split()) or type(e).__name__
        info["error"] = text if len(text) <= 600 else text[:597] + "..."
        log("[API] Error loading model: " + info["error"])
        return None, info
    on_gpu = getattr(engine, "gpu_index", None) is not None
    info.update(device="vulkan" if on_gpu else "cpu", compute_type="ggml",
                gpu_name=getattr(engine, "gpu_name", None),
                cpu_threads=None if on_gpu else threads)
    if device != "cpu" and not on_gpu:
        note = getattr(engine, "auto_note", None)
        if note:   # a choice, not a problem: the timing test picked the processor
            info["fallback"] = note
        else:
            info["fallback"] = ("whisper.cpp found no graphics card it can use (Vulkan); "
                                "using the processor")
            log("[API] WARNING: " + info["fallback"] + ".")
    where = ("graphics card %s" % (info["gpu_name"] or engine.gpu_index)) if on_gpu \
        else "the processor"
    log("[API] whisper.cpp model loaded on %s and ready." % where)
    return engine, info
