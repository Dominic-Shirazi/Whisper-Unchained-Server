"""Parakeet as a third, optional speech engine (WHISPER_ENGINE=parakeet).

Why this exists: on a PC with no NVIDIA card, Whisper on the processor takes
seconds per sentence. NVIDIA's Parakeet models, run by sherpa-onnx, answer in
a fraction of a second on the processor alone and use no graphics memory. The
phone app's on-device mode uses the same models and the same runtime.

This module makes Parakeet look like the one thing the server uses from
faster-whisper:

    segments, info = model.transcribe(path_or_audio, **kwargs)

How: the audio is decoded to 16 kHz, the speech detector (Silero, the one
faster-whisper ships) finds the stretches that hold speech, and each stretch
of at most MAX_PIECE_SECONDS goes through the recognizer. A recording with no
speech in it never reaches the model, so silence cannot come out as made-up
words.

Inputs:  a path, a file object or a 16 kHz float array, plus faster-whisper
         style keyword arguments. Only language and word_timestamps are used:
         Parakeet takes no vocabulary hints (the word list still corrects the
         text afterwards).
Outputs: (list of Segment, Info) with the attributes api/whisper_api.py reads.

Needs: the sherpa-onnx Python package and the three model files plus
tokens.txt in <models>/parakeet/. Nothing here is imported or run unless the
server is started with WHISPER_ENGINE=parakeet.

The models are NVIDIA's, licensed CC-BY-4.0 (credit in README.md).
"""
import math
import os
import threading

try:
    from api import whispercpp_engine
except ImportError:      # the server is also started from inside api/
    import whispercpp_engine

Info, Segment, Word = whispercpp_engine.Info, whispercpp_engine.Segment, whispercpp_engine.Word
compression_ratio = whispercpp_engine.compression_ratio

ENGINE_PARAKEET = "parakeet"
SPELLINGS = {"parakeet", "sherpa", "sherpa-onnx", "sherpa_onnx"}

SAMPLE_RATE = 16000
DEFAULT_MODELS_DIR = "models"
FOLDER = "parakeet"            # inside the models folder
# The models are trained on short utterances: longer speech is cut where the
# speaker pauses, as the phone app does (its limit is 25 s).
MAX_PIECE_SECONDS = 24
# Shorter than this there is nothing to recognise (and the model can fail).
MIN_PIECE_SAMPLES = SAMPLE_RATE // 10
PARTS = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")

# key: (file name stem, what Settings calls it, languages). The stems are the
# file names in the phone app's models-v1 release, so both use the same files.
MODELS = {
    "110m": ("parakeet-tdt-110m-en-int8.", "Parakeet 110M (English)", ("en",)),
    "0.6b-v2": ("parakeet-tdt-0.6b-v2-en-int8.", "Parakeet 0.6B v2 (English)", ("en",)),
    "0.6b-v3": ("parakeet-tdt-0.6b-v3-multi-int8.", "Parakeet 0.6B v3 (25 languages)", None),
}
DEFAULT_MODEL = "110m"


class ParakeetError(RuntimeError):
    """Anything that stops Parakeet from loading or answering."""


# --- Pure helpers ---------------------------------------------------------------

def engine_name(value, log=None):
    """Which engine WHISPER_ENGINE asks for: "parakeet", or whatever
    whispercpp_engine.engine_name makes of it (faster-whisper when unset)."""
    if (value or "").strip().lower() in SPELLINGS:
        return ENGINE_PARAKEET
    return whispercpp_engine.engine_name(value, log=log)


def model_key(value):
    """The model a setting names ("110m", "0.6b-v2", a stem or a title), or the
    default for an empty or unknown value."""
    v = (value or "").strip().lower()
    if v in MODELS:
        return v
    for key, (stem, title, _langs) in MODELS.items():
        if v and v in (stem.rstrip(".").lower(), title.lower()):
            return key
    return DEFAULT_MODEL


def models_folder(models_dir, repo_root):
    folder = models_dir or DEFAULT_MODELS_DIR
    if not os.path.isabs(folder):
        folder = os.path.join(repo_root, folder)
    return os.path.join(folder, FOLDER)


def model_files(key, models_dir, repo_root):
    """{part: full path} for the model's four files."""
    stem = MODELS[key][0]
    folder = models_folder(models_dir, repo_root)
    return {part: os.path.join(folder, stem + part) for part in PARTS}


def runtime_problem(importer=None):
    """One plain sentence when the sherpa-onnx package is not installed, or None."""
    try:
        (importer or _import_runtime)()
        return None
    except Exception:
        return ("The Parakeet engine is not part of this install (the sherpa-onnx "
                "package is missing). Install the newest version of Whisper Unchained.")


def check_files(files):
    """One plain sentence about the first model file that is missing, or None."""
    for part in PARTS:
        if not os.path.isfile(files[part]):
            return ("The Parakeet model is missing: %s should be there "
                    "(Settings > Transcription can download it)." % files[part])
    return None


def files_problem(env, repo_root, models_dir=None, importer=None):
    """The first thing that would stop Parakeet from loading with these
    settings, as one plain sentence, or None. Starts nothing."""
    key = model_key(env.get("PARAKEET_MODEL"))
    return runtime_problem(importer) or check_files(model_files(key, models_dir, repo_root))


def language_for(key, requested):
    """The language code reported for a result. The English models always say
    "en"; the many-language model says what was asked for (it does not report
    which language it heard), or "en" when nothing was asked."""
    langs = MODELS[key][2]
    if langs:
        return langs[0]
    return (requested or "en").strip().lower() or "en"


def split_long(spans, longest=MAX_PIECE_SECONDS * SAMPLE_RATE):
    """Stretches of speech, as (start, end) sample positions, with any that is
    still longer than the limit cut into equal parts. The speech detector is
    asked to keep to the limit already; this is the backstop."""
    out = []
    for start, end in spans:
        n = max(1, int(math.ceil((end - start) / float(longest))))
        step = (end - start) / float(n)
        for i in range(n):
            a, b = int(start + i * step), int(start + (i + 1) * step)
            if b - a >= MIN_PIECE_SAMPLES:
                out.append((a, b))
    return out


def words_from_tokens(tokens, starts, durations, log_probs, offset=0.0):
    """Whole words with their times, from the recognizer's word pieces. A piece
    that begins with a space starts a new word; punctuation sticks to the word
    before it. The first word keeps its leading space, as Whisper's do."""
    words = []
    for i, tok in enumerate(tokens):
        start = offset + (starts[i] if i < len(starts) else 0.0)
        end = start + (durations[i] if i < len(durations) else 0.0)
        p = math.exp(log_probs[i]) if i < len(log_probs) else 1.0
        if words and not tok.startswith(" "):
            w = words[-1]
            w.word, w.end, w.probability = w.word + tok, max(w.end, end), min(w.probability, p)
        else:
            words.append(Word(start, end, tok if tok.startswith(" ") else " " + tok, p))
    return [w for w in words if w.word.strip()]


def avg_logprob(log_probs):
    return sum(log_probs) / len(log_probs) if log_probs else 0.0


def segment_from_result(index, start, end, result, word_timestamps=False):
    """One faster-whisper style Segment from one recognized stretch, or None
    when nothing was heard in it."""
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return None
    log_probs = list(getattr(result, "ys_log_probs", None) or [])
    words = None
    if word_timestamps:
        words = words_from_tokens(list(getattr(result, "tokens", None) or []),
                                  list(getattr(result, "timestamps", None) or []),
                                  list(getattr(result, "durations", None) or []),
                                  log_probs, offset=start)
    return Segment(id=index, seek=0, start=start, end=end, text=" " + text, tokens=[],
                   temperature=0.0, avg_logprob=avg_logprob(log_probs),
                   compression_ratio=compression_ratio(text), no_speech_prob=0.0, words=words)


# --- Audio and the speech detector ----------------------------------------------

def load_audio(audio):
    """16 kHz mono float samples from a path, a file object or an array."""
    if hasattr(audio, "dtype") and hasattr(audio, "shape"):
        import numpy as np
        return np.asarray(audio, dtype=np.float32).reshape(-1)
    if isinstance(audio, os.PathLike):
        audio = os.fspath(audio)
    return whispercpp_engine._decode_any(audio)


def speech_spans(samples):
    """Where the speech is, as (start, end) sample positions; [] for a
    recording with no speech in it. Silero, as shipped with faster-whisper."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    found = get_speech_timestamps(samples, VadOptions(max_speech_duration_s=MAX_PIECE_SECONDS))
    return [(int(t["start"]), int(t["end"])) for t in found]


# --- The engine -----------------------------------------------------------------

def _import_runtime():
    import sherpa_onnx
    return sherpa_onnx


def make_recognizer(files, threads):
    sherpa_onnx = _import_runtime()
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=files["encoder.int8.onnx"], decoder=files["decoder.int8.onnx"],
        joiner=files["joiner.int8.onnx"], tokens=files["tokens.txt"],
        num_threads=int(threads or 4), model_type="nemo_transducer", provider="cpu")


class ParakeetEngine:
    """Looks like faster_whisper.WhisperModel to the server: transcribe()."""

    def __init__(self, key, files, threads=None, recognizer_factory=make_recognizer,
                 spans=speech_spans, loader=load_audio):
        self.key, self.files, self.threads = key, files, threads
        self._spans, self._loader = spans, loader
        self._lock = threading.Lock()
        self._recognizer = recognizer_factory(files, threads)

    def is_running(self):
        return self._recognizer is not None

    def _recognize(self, piece):
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, piece)
        self._recognizer.decode_stream(stream)
        return stream.result

    def transcribe(self, audio, **kwargs):
        """Same call shape as faster_whisper.WhisperModel.transcribe: returns
        (segments, info), segments being a list. The speech detector always
        runs, whatever vad_filter says: it is also what keeps pieces short."""
        samples = self._loader(audio)
        duration = len(samples) / float(SAMPLE_RATE)
        spans = split_long(self._spans(samples)) if len(samples) >= MIN_PIECE_SAMPLES else []
        info = Info(language=language_for(self.key, kwargs.get("language")),
                    language_probability=1.0, duration=duration,
                    duration_after_vad=sum(b - a for a, b in spans) / float(SAMPLE_RATE),
                    all_language_probs=None)
        segments = []
        with self._lock:
            for start, end in spans:
                seg = segment_from_result(
                    len(segments), start / float(SAMPLE_RATE), end / float(SAMPLE_RATE),
                    self._recognize(samples[start:end]),
                    word_timestamps=bool(kwargs.get("word_timestamps")))
                if seg is not None:
                    segments.append(seg)
        return segments, info


# --- Loading from settings ------------------------------------------------------

def load_from_env(env, repo_root, models_dir=None, threads=None, log=print,
                  engine_factory=ParakeetEngine, importer=None):
    """Load Parakeet from the PARAKEET_MODEL setting.

    Returns (engine, info) shaped like device_select.load_whisper(): engine is
    None when it could not load, and info["error"] then says why in one
    sentence. It always runs on the processor."""
    repo_root = os.path.abspath(repo_root)
    key = model_key(env.get("PARAKEET_MODEL"))
    files = model_files(key, models_dir, repo_root)
    info = {"engine": ENGINE_PARAKEET, "requested_device": "cpu", "model": MODELS[key][1],
            "device": None, "compute_type": None, "fallback": None, "cpu_threads": None,
            "error": None, "gpu_name": None}
    problem = runtime_problem(importer) or check_files(files)
    if problem:
        info["error"] = problem
        log("[API] Error loading model: " + problem)
        return None, info
    log("[API] Loading %s on the processor..." % info["model"])
    try:
        engine = engine_factory(key, files, threads=threads)
    except Exception as e:
        text = " ".join(str(e).split()) or type(e).__name__
        info["error"] = text if len(text) <= 600 else text[:597] + "..."
        log("[API] Error loading model: " + info["error"])
        return None, info
    info.update(device="cpu", compute_type="int8", cpu_threads=threads)
    log("[API] Parakeet model loaded and ready.")
    return engine, info
