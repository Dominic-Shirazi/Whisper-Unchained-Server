"""Windows started with pythonw.exe have no console (sys.stdout/stderr None);
libraries that write to them directly must not crash (the model download
failed with "'NoneType' object has no attribute 'write'")."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import console_guard  # noqa: E402


def test_missing_streams_get_somewhere_to_write(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    console_guard.ensure()
    sys.stderr.write("progress bar\r")
    sys.stdout.write("hello\n")


def test_real_streams_are_left_alone(monkeypatch):
    out = sys.stdout
    console_guard.ensure()
    assert sys.stdout is out
