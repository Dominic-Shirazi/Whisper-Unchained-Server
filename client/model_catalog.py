"""Whisper model catalog + the shared model folder, for the Settings model picker.

Why a project-local model folder (WHISPER_MODELS_DIR, default <repo>/models):
the Whisper Unchained service runs as LocalSystem, whose Hugging Face cache
(C:\\Windows\\System32\\config\\systemprofile\\.cache) a normal user can't even
read. So the Settings window could neither show what's downloaded nor fetch a
model ahead of time, and every model switch meant the service downloading it on
restart with dictation offline for minutes. A folder inside the project is
readable by both, so the picker can download first and the switch is a quick
restart.

VRAM figures marked "measured" are from Head-PC's RTX 2070 Super (fp16,
2026-08-05 benchmark); the rest are estimates.
"""
import os
import shutil

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ModelInfo:
    def __init__(self, name, repo, size_mb, vram, cpu, langs, notes, punct):
        self.name, self.repo, self.size_mb = name, repo, size_mb
        self.vram, self.cpu, self.langs, self.notes = vram, cpu, langs, notes
        # Every Whisper model punctuates and capitalizes on its own (it was
        # trained on punctuated transcripts); smaller ones do it less reliably.
        self.punct = punct

    @property
    def cache_folder(self):
        return "models--" + self.repo.replace("/", "--")


# Order: smallest to largest. Last field = how reliable the automatic
# punctuation/capitalization is (all of them do it without you dictating it).
CATALOG = [
    ModelInfo("tiny.en", "Systran/faster-whisper-tiny.en", 75, "~0.4 GB", "Yes, fast",
              "English", "Lowest accuracy. Quick notes, very old PCs.", punct='basic'),
    ModelInfo("base.en", "Systran/faster-whisper-base.en", 145, "~0.5 GB", "Yes",
              "English", "", punct='basic'),
    ModelInfo("small.en", "Systran/faster-whisper-small.en", 484, "~0.6 GB", "Yes, usable",
              "English", "Best choice without a GPU.", punct='good'),
    ModelInfo("small", "Systran/faster-whisper-small", 484, "~0.6 GB", "Yes, usable",
              "Multilingual", "Like small.en, any language.", punct='good'),
    ModelInfo("medium.en", "Systran/faster-whisper-medium.en", 1530, "~1.5 GB", "Slow",
              "English", "Mostly beaten by turbo now.", punct='very good'),
    ModelInfo("distil-large-v3", "Systran/faster-distil-whisper-large-v3", 1510,
              "2.0 GB measured", "No", "English",
              "Fastest large model; shallower decoder than turbo.", punct='very good'),
    ModelInfo("distil-large-v3.5", "distil-whisper/distil-large-v3.5-ct2", 1510,
              "2.0 GB measured", "No", "English", "Newer distil; about the same speed.", punct='very good'),
    ModelInfo("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1620,
              "2.1 GB measured", "No", "Multilingual",
              "Recommended. Near large-v3 accuracy at about half the time. Can't translate.", punct='best'),
    ModelInfo("large-v3", "Systran/faster-whisper-large-v3", 3090,
              "3.9 GB measured", "No", "Multilingual",
              "Most accurate, about 2.5x slower than turbo.", punct='best'),
]
BY_NAME = {m.name: m for m in CATALOG}


def models_dir(env):
    """Where the service loads models from. Relative paths are under the repo.
    Returns None when unset (the service then uses LocalSystem's own cache)."""
    raw = (env.get("WHISPER_MODELS_DIR") or "").strip()
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.join(PROJECT_DIR, raw)


def _snapshot_dirs(root, info):
    snaps = os.path.join(root, info.cache_folder, "snapshots")
    if not os.path.isdir(snaps):
        return []
    return [os.path.join(snaps, d) for d in os.listdir(snaps)]


def is_downloaded(info, root):
    """True when a complete snapshot (model.bin + config.json) is on disk."""
    if not root:
        return False
    for snap in _snapshot_dirs(root, info):
        model = os.path.join(snap, "model.bin")
        if (os.path.isfile(model) and os.path.getsize(model) > 0
                and os.path.isfile(os.path.join(snap, "config.json"))):
            return True
    return False


def user_cache_dir():
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    return os.path.join(home, "hub")


def folder_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            path = os.path.join(dirpath, f)
            # A downloaded snapshot links into blobs/; counting the link too
            # would double the size and stall the progress bar at 99% halfway.
            if os.path.islink(path):
                continue
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return total


def copy_from_user_cache(info, root):
    """Copy a model the user already downloaded (their own HF cache) into the
    shared folder instead of downloading it again. Snapshot files in the user
    cache are often symlinks into blobs/, so they are copied as real files.
    Returns True when a complete copy is in place."""
    src_root = user_cache_dir()
    if not is_downloaded(info, src_root):
        return False
    src = os.path.join(src_root, info.cache_folder)
    dst = os.path.join(root, info.cache_folder)
    tmp = dst + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    if os.path.isdir(os.path.join(src, "refs")):
        shutil.copytree(os.path.join(src, "refs"), os.path.join(tmp, "refs"))
    for snap in _snapshot_dirs(src_root, info):
        out = os.path.join(tmp, "snapshots", os.path.basename(snap))
        os.makedirs(out)
        for name in os.listdir(snap):
            # copy2 follows symlinks: writes the real file content.
            shutil.copy2(os.path.join(snap, name), os.path.join(out, name))
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(tmp, dst)
    return is_downloaded(info, root)


def download(info, root):
    """Get a model into the shared folder: copy from the user's cache when it's
    already there, otherwise download from Hugging Face. Blocking; run in a thread."""
    os.makedirs(root, exist_ok=True)
    if copy_from_user_cache(info, root):
        return "copied"
    # No progress bar: Settings and the wizard have no console to draw it in
    # (the window shows its own percentage from the folder size).
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    from huggingface_hub import snapshot_download
    try:   # in case huggingface_hub was imported before the setting above
        from huggingface_hub.utils import disable_progress_bars
        disable_progress_bars()
    except Exception:
        pass
    # Same file set faster-whisper itself fetches.
    snapshot_download(info.repo, cache_dir=root, allow_patterns=[
        "config.json", "preprocessor_config.json", "model.bin",
        "tokenizer.json", "vocabulary.*"])
    return "downloaded"
