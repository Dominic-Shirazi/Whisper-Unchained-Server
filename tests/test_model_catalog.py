"""Model picker helpers: download detection, copying from the user's HF cache
(symlinked snapshots become real files), folder resolution."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import model_catalog as mc  # noqa: E402

TINY = mc.BY_NAME["tiny.en"]


def _fake_cache(root, info, symlink=False):
    """Lay out a HF cache entry like huggingface_hub does."""
    base = root / info.cache_folder
    (base / "refs").mkdir(parents=True)
    (base / "refs" / "main").write_text("abc123")
    snap = base / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    blobs = base / "blobs"
    blobs.mkdir()
    for name, data in (("model.bin", b"weights" * 100), ("config.json", b"{}"),
                       ("tokenizer.json", b"{}")):
        if symlink:
            (blobs / name).write_bytes(data)
            os.symlink(blobs / name, snap / name)
        else:
            (snap / name).write_bytes(data)


def test_models_dir_resolution():
    assert mc.models_dir({}) is None
    assert mc.models_dir({"WHISPER_MODELS_DIR": "models"}) == os.path.join(mc.PROJECT_DIR, "models")
    assert mc.models_dir({"WHISPER_MODELS_DIR": r"D:\m"}) == r"D:\m"


def test_is_downloaded_needs_complete_snapshot(tmp_path):
    assert not mc.is_downloaded(TINY, str(tmp_path))
    assert not mc.is_downloaded(TINY, None)
    _fake_cache(tmp_path, TINY)
    assert mc.is_downloaded(TINY, str(tmp_path))
    # An empty model.bin (interrupted copy) is not a download.
    snap = tmp_path / TINY.cache_folder / "snapshots" / "abc123"
    (snap / "model.bin").write_bytes(b"")
    assert not mc.is_downloaded(TINY, str(tmp_path))


@pytest.mark.parametrize("symlink", [False, True])
def test_copy_from_user_cache_writes_real_files(tmp_path, monkeypatch, symlink):
    user = tmp_path / "user_hub"
    shared = tmp_path / "shared"
    shared.mkdir()
    try:
        _fake_cache(user, TINY, symlink=symlink)
    except OSError:
        pytest.skip("symlinks not permitted here")
    monkeypatch.setattr(mc, "user_cache_dir", lambda: str(user))
    assert mc.download(TINY, str(shared)) == "copied"
    snap = shared / TINY.cache_folder / "snapshots" / "abc123"
    assert not os.path.islink(snap / "model.bin")
    assert (snap / "model.bin").read_bytes() == b"weights" * 100
    assert mc.is_downloaded(TINY, str(shared))
    assert not (shared / (TINY.cache_folder + ".partial")).exists()


def test_copy_skipped_when_user_cache_lacks_model(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "user_cache_dir", lambda: str(tmp_path / "empty"))
    assert mc.copy_from_user_cache(TINY, str(tmp_path)) is False


def test_catalog_repos_match_faster_whisper():
    """Folder names come from the repo ids; they must match what faster-whisper
    downloads, or the picker would say 'not downloaded' for a loaded model."""
    # test_v1_api swaps faster_whisper for a fake module; load the real one.
    saved = {k: sys.modules.pop(k) for k in list(sys.modules)
             if k == "faster_whisper" or k.startswith("faster_whisper.")}
    try:
        from faster_whisper.utils import _MODELS
    finally:
        for k in [k for k in sys.modules if k == "faster_whisper" or k.startswith("faster_whisper.")]:
            del sys.modules[k]
        sys.modules.update(saved)
    for info in mc.CATALOG:
        assert _MODELS[info.name] == info.repo, info.name


def test_folder_size_ignores_symlinks(tmp_path):
    (tmp_path / "blob").write_bytes(b"x" * 1000)
    try:
        os.symlink(tmp_path / "blob", tmp_path / "link")
    except OSError:
        pytest.skip("symlinks not permitted here")
    assert mc.folder_size(str(tmp_path)) == 1000


def test_every_model_states_punctuation():
    for info in mc.CATALOG:
        assert info.punct in ("basic", "good", "very good", "best"), info.name
