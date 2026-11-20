"""Getting a Parakeet speech model onto this PC (the third speech engine,
api/parakeet_engine.py).

A model is four files in <models>/parakeet/. They are the same files the
phone app downloads, from the same place (its "models-v1" release), fetched
with the downloader in api/engine_files.py: over HTTPS to a temporary name,
checked against the SHA-256 pinned below, and only then renamed into place.

The runtime (the sherpa-onnx Python package) is not downloaded here: it is
installed with the product (requirements.txt, pinned in constraints.txt).
runtime_installed() says whether this install has it.

Inputs:  which model is wanted, the project folder and the models folder.
Outputs: missing() lists what still has to be fetched as (FileSpec, where it
         goes), the shape engine_files.fetch() and its progress helpers take.

Used by the Settings window (client/settings_gui.py) and setup.
"""
import importlib.util
import os

try:
    from api import engine_files, parakeet_engine
except ImportError:      # Settings and the server put api/ itself on sys.path
    import engine_files
    import parakeet_engine

RELEASE = ("https://github.com/Dominic-Shirazi/Whisper-Unchained-Android/releases/"
           "download/models-v1/")

# What each of a model's files is called in a sentence. The encoder is nearly
# all of the download.
_WHAT = {"tokens.txt": "the model's vocabulary", "joiner.int8.onnx": "the model's joiner",
         "decoder.int8.onnx": "the model's decoder", "encoder.int8.onnx": "the speech model"}
# Download order: small things first.
ORDER = ("tokens.txt", "joiner.int8.onnx", "decoder.int8.onnx", "encoder.int8.onnx")

# Size and SHA-256 of every file, as GitHub lists them for the release's
# assets (read 2026-11-19). The 110M and 0.6B v2 files were also checked
# against downloaded copies, on Windows and on Linux.
_FILES = {
    "110m": {
        "tokens.txt": (9953, "450e56bd2f036fe5b6aa821865838cc5aa9d8b0106134ce9a9ba0664abe6cd10"),
        "joiner.int8.onnx": (1411403, "bf7dff69e9f2cdbe9943d70da358f38b361c115ba0105bae7e908e0d6ec782f6"),
        "decoder.int8.onnx": (3955863, "f7c331c5504c2e593c76ed22b728e3f554af6c4a383dde862e719ced08b1da19"),
        "encoder.int8.onnx": (131113202, "0f35509ddeb9b39002fb077d979a9fe74f06eb0bc4dd5c34f512f82e5111d657"),
    },
    "0.6b-v2": {
        "tokens.txt": (9384, "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d"),
        "joiner.int8.onnx": (1739080, "77a29f3e5ac7d37074e0af465391b80f314015d5b97f0f90c6c7767f1d56f428"),
        "decoder.int8.onnx": (7257753, "4d097bae3bac5c96018d5293ba39f1b657296102b142affda40378f38b020f41"),
        "encoder.int8.onnx": (652282309, "92a10bd9c23936ea0566c29f963bc57c2a5df4261b6a8921c39abea66557ca8b"),
    },
    "0.6b-v3": {
        "tokens.txt": (93939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
        "joiner.int8.onnx": (6355277, "cbd02f3696e9944970111fb439d9c3fd365a80374565f13f2b9c7007d253fc98"),
        "decoder.int8.onnx": (11845274, "0222a86147299d4dee90edf95b6d13827edb42ad8b7c9cf0b2582f030937c0b5"),
        "encoder.int8.onnx": (652282294, "1afbe69c1ffdabee94755f42e4daf1d93549131a3da11356bda84fcef0f2c479"),
    },
}


class ModelInfo:
    """One model as the list in Settings shows it."""

    def __init__(self, key, accuracy, speed, note):
        self.key, self.accuracy, self.speed, self.note = key, accuracy, speed, note
        self.title = parakeet_engine.MODELS[key][1]
        self.size = sum(size for size, _sha in _FILES[key].values())

    @property
    def size_text(self):
        return engine_files.size_text(self.size)


# Speed lines state what was measured: the middle time over real dictations
# of 15 seconds or less, on an i5-9600K (six cores, no graphics
# card used). Accuracy: against Whisper large-v3 on an hour of real dictation
# both differ in about 6 words in 100, a little more than Whisper small.
MODELS = {m.key: m for m in (
    ModelInfo("110m", "good",
              "About a quarter of a second for a short dictation on a processor "
              "(measured on an i5-9600K).",
              "The quickest choice with no graphics card. English only. Writes numbers "
              "as words."),
    ModelInfo("0.6b-v2", "good",
              "About half a second for a short dictation on a processor (measured on "
              "an i5-9600K).",
              "A little more accurate than the 110M, and a bigger download. English only."),
    ModelInfo("0.6b-v3", "good",
              "Not timed yet. The same size as the English 0.6B.",
              "25 European languages."),
)}
DEFAULT_MODEL = parakeet_engine.DEFAULT_MODEL


def runtime_installed():
    """True when this install has the sherpa-onnx package (nothing is loaded)."""
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        return False


def specs(key):
    """The model's four files as [(FileSpec, part)], in download order."""
    stem = parakeet_engine.MODELS[key][0]
    out = []
    for part in ORDER:
        size, sha = _FILES[key][part]
        out.append((engine_files.FileSpec("parakeet", _WHAT[part], stem + part,
                                          RELEASE + stem + part, sha, size), part))
    return out


def missing(key, repo_root, models_dir=None):
    """What still has to be fetched for this model: [(spec, where it goes)].
    Empty = all four files are here. An unknown key means the default model."""
    key = parakeet_engine.model_key(key)
    files = parakeet_engine.model_files(key, models_dir, repo_root)
    return [(spec, files[part]) for spec, part in specs(key)
            if not os.path.isfile(files[part])]


def downloaded(key, repo_root, models_dir=None):
    return not missing(key, repo_root, models_dir)
