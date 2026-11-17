"""Getting the whisper.cpp engine's files (api/engine_files.py): what is
missing, downloading to a temporary name, the SHA-256 check before a file gets
its real name, unpacking the engine, cancelling. The downloader is a stand-in:
nothing here touches the network, the real models folder or api/bin.

    .venv\\Scripts\\python.exe -m pytest tests/test_engine_files.py -q
"""
import hashlib
import io
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api import engine_files as ef  # noqa: E402

PROGRAM = os.path.basename(ef.engine_binary("x"))


class Response(io.BytesIO):
    """What the downloader hands back: bytes to read, and where they came from."""

    def __init__(self, data, url="https://example.invalid/file", fail_after=None):
        super().__init__(data)
        self._url, self._fail_after, self.closed_by_us = url, fail_after, False

    def geturl(self):
        return self._url

    def read(self, n=-1):
        if self._fail_after is not None and self.tell() >= self._fail_after:
            raise ConnectionResetError("connection lost")
        return super().read(n)

    def close(self):
        self.closed_by_us = True
        super().close()


def spec_for(data, name="ggml-test.bin", kind="file", what="the speech model", key="model"):
    return ef.FileSpec(key, what, name, "https://example.invalid/" + name,
                       hashlib.sha256(data).hexdigest(), len(data), kind=kind)


def engine_zip(extra=None, program=PROGRAM):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(program, b"MZ the engine")
        z.writestr("LICENSE-whisper.cpp.txt", b"MIT")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def no_leftovers(folder):
    return [n for n in os.listdir(folder) if n.endswith((".part", ".zip", ".new", ".tmp"))]


# --- the pinned files -------------------------------------------------------------

def test_every_download_is_https_with_a_full_sha256():
    specs = [ef.ENGINE, ef.VAD] + list(ef.MODELS.values())
    assert len(specs) == 4
    for s in specs:
        assert s.url.startswith("https://")
        assert len(s.sha256) == 64 and int(s.sha256, 16) >= 0 and s.sha256 == s.sha256.lower()
        assert s.size > 0 and s.url.endswith("/" + s.name)
    assert ef.ENGINE.url.startswith(
        "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server/releases/download/"
        "engine-whispercpp-v1.9.5/")
    assert ef.ENGINE.kind == "zip" and ef.ENGINE_VERSION in ef.ENGINE.name
    assert ef.MODELS["large-v3-turbo-q5_0"].url == (
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin")
    assert ef.MODELS["base"].url.endswith("ggerganov/whisper.cpp/resolve/main/ggml-base.bin")
    assert ef.VAD.url == (
        "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin")


def test_sizes_read_the_way_download_pages_show_them():
    assert ef.MODELS["large-v3-turbo-q5_0"].size_text == "574 MB"
    assert ef.MODELS["base"].size_text == "148 MB"
    assert ef.VAD.size_text == "1 MB" and ef.ENGINE.size_text == "30 MB"


def test_model_names_match_what_the_engine_looks_for(tmp_path):
    """The key saved as WHISPER_CPP_MODEL has to resolve to the downloaded file."""
    from api import whispercpp_engine as w
    for key, spec in ef.MODELS.items():
        assert w.resolve_model(key, "models", str(tmp_path)) == os.path.join(
            ef.models_folder("models", str(tmp_path)), spec.name)
    assert os.path.basename(w.DEFAULT_BIN) == PROGRAM
    assert w.resolve_binary("", str(tmp_path)) == ef.engine_binary(str(tmp_path))
    assert ef.VAD.name == w.DEFAULT_VAD_NAME and ef.ENGINE_MARKER == "engine.json"


def test_model_key():
    assert ef.model_key("base") == "base" and ef.model_key(" ggml-base ") == "base"
    assert ef.model_key("large-v3-turbo-q5_0") == "large-v3-turbo-q5_0"
    # The engine reads anything ending in .bin as a path of the person's own.
    assert ef.model_key("ggml-base.bin") is None
    assert ef.model_key("C:/m/ggml-base.bin") is None and ef.model_key("small") is None
    assert ef.model_key("") is None and ef.model_key(None) is None


# --- what is missing --------------------------------------------------------------

def test_missing_lists_what_is_not_there_small_things_first(tmp_path):
    root = str(tmp_path)
    todo = ef.missing("base", root, "models")
    assert [s.key for s, _d in todo] == ["engine", "vad", "model"]
    assert todo[0][1] == os.path.join(root, "api", "bin", "whispercpp")
    assert todo[1][1] == os.path.join(root, "models", "ggml-silero-v5.1.2.bin")
    assert todo[2][1] == os.path.join(root, "models", "ggml-base.bin")
    assert ef.missing_text(todo) == ("the speech engine (30 MB), the silence filter (1 MB) "
                                     "and the speech model (148 MB)")
    os.makedirs(os.path.dirname(ef.engine_binary(root)))
    open(ef.engine_binary(root), "wb").close()
    os.makedirs(os.path.join(root, "models"))
    open(os.path.join(root, "models", "ggml-silero-v6.2.0.bin"), "wb").close()   # any silero
    todo = ef.missing("large-v3-turbo-q5_0", root, "models")
    assert [s.name for s, _d in todo] == ["ggml-large-v3-turbo-q5_0.bin"]
    assert ef.missing_text(todo) == "the speech model (574 MB)"
    open(todo[0][1], "wb").close()
    assert ef.missing("large-v3-turbo-q5_0", root, "models") == []
    assert [s.name for s, _d in ef.missing("base", root, "models")] == ["ggml-base.bin"]
    # A model file of the person's own: only the engine and the filter are ours.
    assert ef.missing("C:/mine/custom.bin", root, "models") == []
    assert ef.missing_text([]) == ""


def test_models_folder_follows_the_setting(tmp_path):
    root = str(tmp_path)
    assert ef.models_folder(None, root) == os.path.join(root, "models")
    assert ef.models_folder("", root) == os.path.join(root, "models")
    assert ef.models_folder("speech", root) == os.path.join(root, "speech")
    elsewhere = str(tmp_path / "elsewhere")
    assert ef.models_folder(elsewhere, root) == elsewhere
    assert ef.missing("base", root, elsewhere)[-1][1] == os.path.join(elsewhere, "ggml-base.bin")


# --- downloading ------------------------------------------------------------------

def test_download_checks_the_hash_then_gives_the_file_its_name(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "CHUNK", 1000)
    data = os.urandom(4500)
    spec = spec_for(data)
    dest = str(tmp_path / "models" / spec.name)
    seen, during = [], []
    resp = Response(data)

    def progress(s, done, total):
        seen.append((done, total))
        during.append((os.path.exists(dest), os.path.exists(dest + ".part")))

    assert ef.download(spec, dest, opener=lambda url: resp, progress=progress) == dest
    with open(dest, "rb") as f:
        assert f.read() == data
    assert seen[-1] == (4500, 4500) and [d for d, _t in seen] == sorted(d for d, _t in seen)
    assert len(seen) == 5
    # While it downloads only the temporary name exists.
    assert set(during) == {(False, True)}
    assert no_leftovers(os.path.dirname(dest)) == [] and resp.closed_by_us
    assert ef.sha256_file(dest) == spec.sha256


def test_wrong_hash_is_refused_and_nothing_is_left(tmp_path):
    data = b"what was expected"
    spec = spec_for(data)
    dest = str(tmp_path / spec.name)
    with pytest.raises(ef.EngineFileError) as err:
        ef.download(spec, dest, opener=lambda url: Response(b"something else entirely"))
    text = str(err.value)
    assert "the speech model" in text and "not the file that was expected" in text
    assert "was not used" in text and "\n" not in text and "\u2014" not in text
    assert os.listdir(str(tmp_path)) == []


def test_wrong_hash_never_replaces_a_good_file(tmp_path):
    spec = spec_for(b"good")
    dest = tmp_path / spec.name
    dest.write_bytes(b"good")
    with pytest.raises(ef.EngineFileError):
        ef.download(spec, str(dest), opener=lambda url: Response(b"bad!"))
    assert dest.read_bytes() == b"good" and os.listdir(str(tmp_path)) == [spec.name]


def test_cancel_stops_the_download_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "CHUNK", 100)
    data = os.urandom(1000)
    spec = spec_for(data)
    dest = str(tmp_path / spec.name)
    seen = []
    with pytest.raises(ef.Cancelled) as err:
        ef.download(spec, dest, opener=lambda url: Response(data),
                    progress=lambda s, done, total: seen.append(done),
                    cancelled=lambda: len(seen) >= 3)
    assert str(err.value) == "Download cancelled." and len(seen) == 3
    assert os.listdir(str(tmp_path)) == []
    assert isinstance(err.value, ef.EngineFileError)


def test_no_connection_is_one_plain_sentence_pair(tmp_path):
    def opener(url):
        raise OSError("getaddrinfo failed")

    spec = spec_for(b"x")
    with pytest.raises(ef.EngineFileError) as err:
        ef.download(spec, str(tmp_path / spec.name), opener=opener)
    assert str(err.value) == ("Could not download the speech model. Check the internet "
                              "connection and try again. (getaddrinfo failed)")
    assert os.listdir(str(tmp_path)) == []


def test_a_connection_lost_part_way_is_reported_and_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "CHUNK", 100)
    data = os.urandom(1000)
    spec = spec_for(data)
    with pytest.raises(ef.EngineFileError, match="stopped part way"):
        ef.download(spec, str(tmp_path / spec.name),
                    opener=lambda url: Response(data, fail_after=300))
    assert os.listdir(str(tmp_path)) == []


def test_only_https_is_ever_downloaded(tmp_path):
    spec = spec_for(b"x")
    spec.url = "http://example.invalid/ggml-test.bin"
    called = []
    with pytest.raises(ef.EngineFileError, match="not HTTPS"):
        ef.download(spec, str(tmp_path / spec.name), opener=called.append)
    assert called == []
    # Sent on to a plain-HTTP address: refused too.
    spec = spec_for(b"x")
    with pytest.raises(ef.EngineFileError, match="not HTTPS"):
        ef.download(spec, str(tmp_path / spec.name),
                    opener=lambda url: Response(b"x", url="http://mirror.invalid/x"))
    assert os.listdir(str(tmp_path)) == []


def test_the_default_downloader_goes_through_urlopen(tmp_path, monkeypatch):
    """With no stand-in given, download() uses open_url. It is replaced here
    too: no test reaches the network."""
    asked = []

    def fake_urlopen(req, timeout=None):
        asked.append((req.full_url, timeout))
        return Response(b"payload")

    monkeypatch.setattr(ef.urllib.request, "urlopen", fake_urlopen)
    spec = spec_for(b"payload")
    dest = str(tmp_path / spec.name)
    assert ef.download(spec, dest) == dest
    assert asked == [(spec.url, 30)]


# --- the engine zip ---------------------------------------------------------------

def test_fetch_unpacks_the_engine_and_leaves_a_version_note(tmp_path):
    data = engine_zip({"vulkan-1.dll": b"dll"})
    spec = spec_for(data, name="engine.zip", kind="zip", what="the speech engine", key="engine")
    dest = str(tmp_path / "api" / "bin" / "whispercpp")
    program = ef.fetch(spec, dest, opener=lambda url: Response(data))
    assert program == os.path.join(dest, PROGRAM) and os.path.isfile(program)
    assert sorted(os.listdir(dest)) == sorted(
        [PROGRAM, "LICENSE-whisper.cpp.txt", "vulkan-1.dll", "engine.json"])
    assert no_leftovers(os.path.dirname(dest)) == []        # the zip itself is gone
    from api import whispercpp_engine as w
    assert w.engine_version(program) == ef.ENGINE_VERSION


def test_a_new_engine_replaces_the_old_one(tmp_path):
    dest = tmp_path / "whispercpp"
    dest.mkdir()
    (dest / "old-file.dll").write_bytes(b"old")
    data = engine_zip()
    spec = spec_for(data, name="engine.zip", kind="zip", what="the speech engine", key="engine")
    ef.fetch(spec, str(dest), opener=lambda url: Response(data))
    assert "old-file.dll" not in os.listdir(str(dest)) and PROGRAM in os.listdir(str(dest))


def test_an_engine_zip_with_the_wrong_hash_is_never_unpacked(tmp_path):
    dest = tmp_path / "whispercpp"
    dest.mkdir()
    (dest / PROGRAM).write_bytes(b"the old engine")
    spec = spec_for(engine_zip(), name="engine.zip", kind="zip", what="the speech engine",
                    key="engine")
    tampered = engine_zip({"extra.dll": b"surprise"})
    with pytest.raises(ef.EngineFileError, match="not the file that was expected"):
        ef.fetch(spec, str(dest), opener=lambda url: Response(tampered))
    assert os.listdir(str(dest)) == [PROGRAM]
    assert (dest / PROGRAM).read_bytes() == b"the old engine"
    assert os.listdir(str(tmp_path)) == ["whispercpp"]


@pytest.mark.parametrize("bad_name", ["../outside.exe", "/abs.exe", "sub/../../up.exe",
                                      "C:/Windows/evil.exe"])
def test_a_zip_that_writes_outside_its_folder_is_refused(tmp_path, bad_name):
    data = engine_zip({bad_name: b"x"})
    spec = spec_for(data, name="engine.zip", kind="zip", what="the speech engine", key="engine")
    dest = tmp_path / "bin" / "whispercpp"
    with pytest.raises(ef.EngineFileError, match="does not belong there"):
        ef.fetch(spec, str(dest), opener=lambda url: Response(data))
    assert not dest.exists() and os.listdir(str(tmp_path / "bin")) == []
    assert not (tmp_path / "outside.exe").exists()


def test_a_zip_without_the_engine_or_not_a_zip_is_refused(tmp_path):
    data = engine_zip(program="something-else.exe")
    spec = spec_for(data, name="engine.zip", kind="zip", what="the speech engine", key="engine")
    dest = tmp_path / "whispercpp"
    with pytest.raises(ef.EngineFileError, match="does not contain the engine"):
        ef.fetch(spec, str(dest), opener=lambda url: Response(data))
    junk = b"this is not a zip file"
    spec = spec_for(junk, name="engine.zip", kind="zip", what="the speech engine", key="engine")
    with pytest.raises(ef.EngineFileError, match="Could not unpack the speech engine"):
        ef.fetch(spec, str(dest), opener=lambda url: Response(junk))
    assert os.listdir(str(tmp_path)) == []


# --- everything at once -----------------------------------------------------------

def test_fetch_missing_gets_all_three_and_then_nothing_is_missing(tmp_path, monkeypatch):
    files = {"engine": engine_zip(), "vad": b"silence filter", "model": b"speech model"}
    engine = spec_for(files["engine"], name="e.zip", kind="zip", what="the speech engine",
                      key="engine")
    vad = spec_for(files["vad"], name="ggml-silero-v5.1.2.bin", what="the silence filter",
                   key="vad")
    model = spec_for(files["model"], name="ggml-base.bin")
    monkeypatch.setattr(ef, "ENGINE", engine)
    monkeypatch.setattr(ef, "VAD", vad)
    monkeypatch.setattr(ef, "MODELS", {"base": model})
    by_url = {s.url: files[s.key] for s in (engine, vad, model)}
    asked, seen = [], []

    def opener(url):
        asked.append(url)
        return Response(by_url[url])

    root = str(tmp_path)
    got = ef.fetch_missing("base", root, "models", opener=opener,
                           progress=lambda s, done, total: seen.append(s.key))
    assert [s.key for s in got] == ["engine", "vad", "model"] and asked == [
        engine.url, vad.url, model.url]
    assert set(seen) == {"engine", "vad", "model"}
    assert ef.missing("base", root, "models") == []
    # What the engine itself checks before it loads.
    from api import whispercpp_engine as w
    assert w.files_problem({"WHISPER_CPP_MODEL": "base"}, root, "models") is None
    # Asked again: nothing to do, nothing downloaded.
    asked.clear()
    assert ef.fetch_missing("base", root, "models", opener=opener) == [] and asked == []
