"""Getting the files the whisper.cpp speech engine needs onto this PC.

Three files: the engine itself (whisper-server in a zip on Windows, a tar.gz
on Linux), one speech model,
and the silence filter (a small VAD model; without it whisper.cpp invents text
on silent recordings, so the engine refuses to load without one).

Every file is downloaded over HTTPS to a temporary name, checked against the
SHA-256 pinned below, and only then renamed into place. A file that does not
match is deleted and never used.

Inputs:  which model is wanted, the project folder and the models folder.
Outputs: the files in place (see missing() and fetch()), or an EngineFileError
         whose text is one plain sentence for the person at the screen.

Used by the Settings window (client/settings_gui.py) and the setup wizard
(installer/wizard.py). Nothing here needs more than the standard library, and
the downloader can be replaced (tests use a fake one: no network).
"""
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile

ENGINE_VERSION = "v1.9.5"
ENGINE_MARKER = "engine.json"   # written next to whisper-server after unpacking
CHUNK = 1024 * 1024


class EngineFileError(RuntimeError):
    """A download or check that failed. str() is one plain sentence."""


class Cancelled(EngineFileError):
    """The person pressed Cancel."""


class FileSpec:
    """One file to fetch. kind: "zip" or "tar" (an archive, unpacked into a
    folder) or "file"."""

    def __init__(self, key, what, name, url, sha256, size, kind="file", title="", note="",
                 accuracy="", speed=""):
        self.key, self.what, self.name, self.url = key, what, name, url
        self.sha256, self.size, self.kind = sha256.lower(), int(size), kind
        self.title, self.note = title or name, note
        # For the list of models in Settings (note = why you would pick it).
        self.accuracy, self.speed = accuracy, speed

    @property
    def size_text(self):
        return size_text(self.size)

    def __repr__(self):
        return "FileSpec(%s)" % self.name


def size_text(n):
    """Bytes as "574 MB" or "1 MB" (never 0 MB for a small file). Counted in
    millions of bytes, the way download pages show sizes."""
    return "%d MB" % max(1, round(n / 1e6))


def about_size_text(n):
    """Bytes as a round figure for a sentence: "600 MB", "180 MB", "1.6 GB"."""
    mb = n / 1e6
    if mb >= 950:
        return "%.1f GB" % (mb / 1000)
    if mb < 15:
        return size_text(n)
    step = 50 if mb >= 300 else 10
    return "%d MB" % (int(mb / step + 0.5) * step)


_HF = "https://huggingface.co/%s/resolve/main/%s"

# Built by .github/workflows/build-whispercpp.yml from the upstream source and
# attached to this project's release "engine-whispercpp-v1.9.5". The SHA-256 is
# the one that workflow printed into the release notes.
ENGINE_WINDOWS = FileSpec(
    "engine", "the speech engine", "whisper-server-win-x64-vulkan-v1.9.5.zip",
    "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server/releases/download/"
    "engine-whispercpp-v1.9.5/whisper-server-win-x64-vulkan-v1.9.5.zip",
    "2873fd1fb4edce7ea2dc892554e7804fd1b605f01809f33cd958de523b11a8c4", 30081094, kind="zip")
# The Linux build from the same workflow (Ubuntu 24.04, Vulkan, AVX2: it needs
# glibc 2.39 or newer and the system's Vulkan loader, libvulkan1). Size and
# SHA-256 are the release asset's, and were checked against a downloaded copy
# on 2026-11-18.
ENGINE_LINUX = FileSpec(
    "engine", "the speech engine", "whisper-server-linux-x64-vulkan-v1.9.5.tar.gz",
    "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server/releases/download/"
    "engine-whispercpp-v1.9.5/whisper-server-linux-x64-vulkan-v1.9.5.tar.gz",
    "71196598e69a7a1524149b2c53f8e65f03e5c0fa3bed3419ea0aa0cfb4ad8167", 25106685, kind="tar")
ENGINE = ENGINE_WINDOWS if os.name == "nt" else ENGINE_LINUX

# From huggingface.co/ggerganov/whisper.cpp. Each size and SHA-256 is what
# Hugging Face lists for that file (its API's "size" and "lfs.oid", read
# 2026-11-17 from /api/models/ggerganov/whisper.cpp/tree/main). Only base and
# large-v3-turbo-q5_0 were also checked against a downloaded copy.
#
# Speed lines only state timings that were measured: a 13 second clip on an
# i5-9600K (processor) and on an RTX 2070 Super (through this engine). The
# rest say where the model sits by size. Accuracy is the model's general
# standing, not something measured here.
_NOT_TIMED = "Not timed yet. "


def _model(key, sha256, size, accuracy, speed, note, title=""):
    name = "ggml-%s.bin" % key
    return FileSpec("model", "the speech model", name, _HF % ("ggerganov/whisper.cpp", name),
                    sha256, size, title=title or key, note=note, accuracy=accuracy,
                    speed=speed)


# Order: smallest download first (the order Settings lists them in).
MODELS = {m.name[5:-4]: m for m in (
    _model("tiny.en", "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f",
           77704715, "basic", _NOT_TIMED + "Half the size of base.",
           "The smallest download. For quick notes or a very old PC. English only."),
    _model("tiny", "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
           77691713, "basic", _NOT_TIMED + "Half the size of base.",
           "The smallest download, for any language."),
    _model("base.en", "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
           147964211, "basic", _NOT_TIMED + "The same size as base.",
           "Like base, a little more accurate in English. English only."),
    _model("base", "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
           147951465, "basic",
           "About 1 to 2 seconds for a 13 second clip on a processor (measured on an "
           "i5-9600K). 0.13 seconds on a graphics card (RTX 2070 Super).",
           "Quick on a PC with no graphics card. Any language."),
    _model("small.en", "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
           487614201, "good", _NOT_TIMED + "Between base and large-v3-turbo in size.",
           "More accurate than base and a smaller download than the large ones. "
           "English only."),
    _model("small", "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
           487601967, "good", _NOT_TIMED + "Between base and large-v3-turbo in size.",
           "More accurate than base and a smaller download than the large ones. "
           "Any language."),
    _model("large-v3-turbo-q5_0",
           "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2", 574041195,
           "best",
           "0.3 to 0.6 seconds for a 13 second clip on a graphics card (measured on an "
           "RTX 2070 Super). About 36 seconds on a processor (i5-9600K), so it needs a "
           "graphics card.",
           "Recommended with a graphics card. Any language.",
           title="large-v3-turbo (smaller file)"),
    _model("medium.en", "cc37e93478338ec7700281a7ac30a10128929eb8f427dda2e865faa8f6da4356",
           1533774781, "very good", _NOT_TIMED + "Close to the full large-v3-turbo in size.",
           "An older model. Worth a try only if large-v3-turbo gives you trouble. "
           "English only."),
    _model("medium", "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
           1533763059, "very good", _NOT_TIMED + "Close to the full large-v3-turbo in size.",
           "An older model. Worth a try only if large-v3-turbo gives you trouble. "
           "Any language."),
    _model("large-v3-turbo", "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
           1624555275, "best",
           _NOT_TIMED + "Nearly three times the size of the smaller file.",
           "The same model, not squeezed to save space. For when disk space is no "
           "concern. Any language.",
           title="large-v3-turbo (full size)"),
)}
DEFAULT_MODEL = "large-v3-turbo-q5_0"
CPU_MODEL = "base"

# From huggingface.co/ggml-org/whisper-vad (same check as the models).
VAD = FileSpec(
    "vad", "the silence filter", "ggml-silero-v5.1.2.bin",
    _HF % ("ggml-org/whisper-vad", "ggml-silero-v5.1.2.bin"),
    "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf", 885098)


def available():
    """True where there is an engine build to download: Windows, and Linux on
    an ordinary 64-bit Intel or AMD processor."""
    if os.name == "nt":
        return True
    return sys.platform.startswith("linux") and platform.machine().lower() in (
        "x86_64", "amd64")


# --- Where the files go ---------------------------------------------------------

def engine_dir(repo_root):
    """The folder whisper-server is unpacked into (the engine's default place)."""
    return os.path.join(repo_root, "api", "bin", "whispercpp")


def engine_binary(repo_root):
    return os.path.join(engine_dir(repo_root),
                        "whisper-server.exe" if os.name == "nt" else "whisper-server")


def models_folder(models_dir, repo_root):
    """The models folder as a full path. Same rule as the engine: the setting
    WHISPER_MODELS_DIR, relative = inside the project, unset = "models"."""
    folder = models_dir or "models"
    return folder if os.path.isabs(folder) else os.path.join(repo_root, folder)


def model_key(value):
    """WHISPER_CPP_MODEL as one of the keys in MODELS, or None when it names
    something else (a person's own file; the engine reads anything ending in
    .bin as a path, so only bare names count)."""
    v = (value or "").strip().strip('"')
    if v.lower().startswith("ggml-"):
        v = v[5:]
    return v if v in MODELS else None


def _has_vad(folder):
    try:
        return any(n.lower().startswith("ggml-silero") and n.lower().endswith(".bin")
                   for n in os.listdir(folder))
    except OSError:
        return False


def missing(model, repo_root, models_dir=None):
    """What still has to be fetched for this model: a list of (spec, where it
    goes), in download order (small things first). Empty = everything is here.
    model: a key in MODELS; anything else means "a file of the person's own",
    for which only the engine and the silence filter are checked."""
    folder = models_folder(models_dir, repo_root)
    todo = []
    if not os.path.isfile(engine_binary(repo_root)):
        todo.append((ENGINE, engine_dir(repo_root)))
    if not _has_vad(folder):
        todo.append((VAD, os.path.join(folder, VAD.name)))
    spec = MODELS.get(model)
    if spec is not None and not os.path.isfile(os.path.join(folder, spec.name)):
        todo.append((spec, os.path.join(folder, spec.name)))
    return todo


def total_size(todo):
    """Bytes still to download for what missing() listed."""
    return sum(spec.size for spec, _dest in todo)


class Overall:
    """One percentage for several files downloaded one after the other.
    update(spec, bytes of that file so far) returns 0 to 99."""

    def __init__(self, todo):
        self.specs = [spec for spec, _dest in todo]
        self.total = sum(s.size for s in self.specs)
        self.percent = 0

    def update(self, spec, done):
        before = 0
        for s in self.specs:
            if s is spec:
                break
            before += s.size
        got = before + min(done, spec.size)
        self.percent = max(0, min(99, 100 * got // max(1, self.total)))
        return self.percent


def missing_text(todo):
    """"the speech engine (31 MB) and the speech model (574 MB)"."""
    parts = ["%s (%s)" % (spec.what, spec.size_text) for spec, _dest in todo]
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


# --- Downloading and checking ---------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def open_url(url, timeout=30):
    """The real downloader: an HTTPS GET that follows redirects."""
    req = urllib.request.Request(url, headers={"User-Agent": "WhisperUnchained"})
    return urllib.request.urlopen(req, timeout=timeout)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download(spec, dest, opener=None, progress=None, cancelled=None):
    """Download spec.url to the file dest. The bytes go to dest + ".part"; the
    file gets its real name only after its SHA-256 matched.

    opener(url) returns an object with read(n) (and close()).
    progress(spec, bytes_so_far, bytes_in_all) is called as it goes.
    cancelled() returning True stops it (raises Cancelled)."""
    if not spec.url.lower().startswith("https://"):
        raise EngineFileError("Refusing to download %s: its address is not HTTPS." % spec.what)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    digest = hashlib.sha256()
    done = 0
    try:
        try:
            resp = (opener or open_url)(spec.url)
        except Exception as e:
            raise EngineFileError(
                "Could not download %s. Check the internet connection and try again. (%s)"
                % (spec.what, _short(e)))
        try:
            final = getattr(resp, "geturl", lambda: spec.url)() or spec.url
            if not str(final).lower().startswith("https://"):
                raise EngineFileError(
                    "Refusing to download %s: it was sent to an address that is not HTTPS."
                    % spec.what)
            with open(part, "wb") as out:
                while True:
                    if cancelled is not None and cancelled():
                        raise Cancelled("Download cancelled.")
                    try:
                        block = resp.read(CHUNK)
                    except Exception as e:
                        raise EngineFileError(
                            "The download of %s stopped part way. Check the internet "
                            "connection and try again. (%s)" % (spec.what, _short(e)))
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress is not None:
                        progress(spec, done, max(spec.size, done))
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if digest.hexdigest() != spec.sha256:
            raise EngineFileError(
                "The downloaded copy of %s is not the file that was expected, so it "
                "was not used. Try again." % spec.what)
        os.replace(part, dest)
    except OSError as e:
        _remove(part)
        raise EngineFileError("Could not save %s on this PC. (%s)" % (spec.what, _short(e)))
    except BaseException:
        _remove(part)
        raise
    return dest


def _short(e):
    text = " ".join(str(e).split()) or type(e).__name__
    return text if len(text) <= 160 else text[:157] + "..."


def _safe_member(name):
    """An archive member's name stays inside the folder it is unpacked into."""
    name = name.replace("\\", "/")
    return not (name.startswith("/") or ":" in name or ".." in name.split("/"))


_NOT_BELONGING = ("The speech engine download contains a file that does not "
                  "belong there, so it was not used.")


def _unpack_zip(path, new):
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if not _safe_member(info.filename):
                raise EngineFileError(_NOT_BELONGING)
        z.extractall(new)


def _unpack_tar(path, new):
    """Plain files and folders only: a link or device in the archive could
    point outside the folder, so one is refused."""
    with tarfile.open(path, "r:gz") as t:
        members = t.getmembers()
        for m in members:
            if not _safe_member(m.name) or not (m.isfile() or m.isdir()):
                raise EngineFileError(_NOT_BELONGING)
        os.makedirs(new, exist_ok=True)
        for m in members:
            target = os.path.join(new, *[p for p in m.name.split("/") if p not in ("", ".")])
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with t.extractfile(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o100 else 0o644)


def unpack_engine(zip_path, dest_dir, spec=None):
    """Unpack the engine archive into dest_dir, replacing what was there. The
    files are unpacked into a new folder first, so an archive that turns out to
    be broken leaves the old engine alone. Returns the path of whisper-server."""
    spec = spec or ENGINE
    new = dest_dir.rstrip("\\/") + ".new"
    shutil.rmtree(new, ignore_errors=True)
    try:
        (_unpack_tar if spec.kind == "tar" else _unpack_zip)(zip_path, new)
        program = "whisper-server.exe" if os.name == "nt" else "whisper-server"
        if not os.path.isfile(os.path.join(new, program)):
            raise EngineFileError(
                "The speech engine download does not contain the engine, so it was "
                "not used.")
        with open(os.path.join(new, ENGINE_MARKER), "w", encoding="utf-8") as f:
            json.dump({"version": ENGINE_VERSION, "sha256": spec.sha256}, f)
        shutil.rmtree(dest_dir, ignore_errors=True)
        if os.path.exists(dest_dir):
            raise EngineFileError(
                "Could not replace the old speech engine: it is in use. Pause or stop "
                "the server, then try again.")
        os.replace(new, dest_dir)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as e:
        shutil.rmtree(new, ignore_errors=True)
        raise EngineFileError("Could not unpack the speech engine. (%s)" % _short(e))
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    return os.path.join(dest_dir, program)


def fetch(spec, dest, opener=None, progress=None, cancelled=None):
    """Get one file into place: download, check, and (for the engine) unpack.
    dest: the file's final path, or for the engine the folder to unpack into."""
    if spec.kind not in ("zip", "tar"):
        return download(spec, dest, opener, progress, cancelled)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    zip_path = dest.rstrip("\\/") + ".zip"
    download(spec, zip_path, opener, progress, cancelled)
    try:
        return unpack_engine(zip_path, dest, spec)
    finally:
        _remove(zip_path)


def fetch_missing(model, repo_root, models_dir=None, opener=None, progress=None,
                  cancelled=None):
    """Fetch whatever missing() lists. Returns the specs that were fetched."""
    got = []
    for spec, dest in missing(model, repo_root, models_dir):
        fetch(spec, dest, opener, progress, cancelled)
        got.append(spec)
    return got
