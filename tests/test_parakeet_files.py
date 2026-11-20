"""The Parakeet models' download list (api/parakeet_files.py): which files a
model is made of, where they go, and what is still missing. Nothing is
downloaded."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api import engine_files, parakeet_engine, parakeet_files as pf  # noqa: E402


def test_every_model_of_the_engine_has_its_four_pinned_files():
    assert set(pf.MODELS) == set(parakeet_engine.MODELS) and pf.DEFAULT_MODEL in pf.MODELS
    for key in pf.MODELS:
        listed = pf.specs(key)
        assert [part for _spec, part in listed] == list(pf.ORDER)
        assert set(pf.ORDER) == set(parakeet_engine.PARTS)
        stem = parakeet_engine.MODELS[key][0]
        for spec, part in listed:
            assert spec.name == stem + part and spec.kind == "file"
            assert spec.url == pf.RELEASE + spec.name and spec.url.startswith("https://github.com/")
            assert len(spec.sha256) == 64 and int(spec.sha256, 16) >= 0 and spec.size > 0
        # The big file comes last, so a broken connection fails early.
        assert listed[-1][0].size == max(spec.size for spec, _part in listed)


def test_sizes_are_the_ones_settings_shows():
    assert pf.MODELS["110m"].size_text == "136 MB"
    assert pf.MODELS["0.6b-v2"].size_text == "661 MB"
    assert pf.MODELS["0.6b-v3"].size_text == "671 MB"
    for info in pf.MODELS.values():
        assert info.title == parakeet_engine.MODELS[info.key][1]
        assert info.accuracy and info.note and ("measured" in info.speed or "Not timed" in info.speed)


def test_missing_lists_what_is_not_here_where_the_engine_looks(tmp_path):
    root = str(tmp_path)
    todo = pf.missing("110m", root, "models")
    files = parakeet_engine.model_files("110m", "models", root)
    assert [dest for _spec, dest in todo] == [files[part] for part in pf.ORDER]
    assert engine_files.total_size(todo) == pf.MODELS["110m"].size
    assert "the speech model (131 MB)" in engine_files.missing_text(todo)
    os.makedirs(os.path.dirname(files["tokens.txt"]))
    for part in ("tokens.txt", "joiner.int8.onnx", "decoder.int8.onnx"):
        open(files[part], "wb").close()
    assert [spec.name for spec, _d in pf.missing("110m", root, "models")] == [
        "parakeet-tdt-110m-en-int8.encoder.int8.onnx"]
    assert not pf.downloaded("110m", root, "models")
    open(files["encoder.int8.onnx"], "wb").close()
    assert pf.downloaded("110m", root, "models") and pf.missing("", root, "models") == []
    assert len(pf.missing("0.6b-v2", root, "models")) == 4      # another model: its own files


def test_fetching_a_file_checks_it_against_its_fingerprint(tmp_path):
    spec, dest = pf.missing("110m", str(tmp_path), "models")[0]

    class Wrong:
        def read(self, n):
            data, self.data = getattr(self, "data", b"not the file"), b""
            return data

        def close(self):
            pass
    try:
        engine_files.fetch(spec, dest, opener=lambda url: Wrong())
        raise AssertionError("a wrong file was accepted")
    except engine_files.EngineFileError as e:
        assert "not the file that was expected" in str(e)
    assert not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_runtime_installed_is_a_yes_or_no():
    assert pf.runtime_installed() in (True, False)
