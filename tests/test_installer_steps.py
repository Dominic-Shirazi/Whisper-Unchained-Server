import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "installer"))

import steps  # noqa: E402


def test_ensure_env_fresh_then_existing(tmp_path):
    (tmp_path / ".env.example").write_text("A=1\n")
    assert steps.ensure_env(str(tmp_path)) is True
    assert (tmp_path / ".env").read_text() == "A=1\n"
    (tmp_path / ".env").write_text("A=2\n")
    assert steps.ensure_env(str(tmp_path)) is False          # never overwrites
    assert (tmp_path / ".env").read_text() == "A=2\n"


def test_wait_for_server_ok_after_retries():
    answers = [OSError("refused"), OSError("refused"), {"status": "ok", "model": "turbo"}]

    def opener(url, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(json.dumps(a).encode())
    ok, detail = steps.wait_for_server(5000, timeout=60, opener=opener, sleep=lambda s: None)
    assert ok and "turbo" in detail


def test_wait_for_server_reports_model_failure():
    def opener(url, timeout):
        return io.BytesIO(json.dumps({"status": "error"}).encode())
    t = iter(range(0, 1000, 50))
    import time
    real = time.monotonic
    try:
        time.monotonic = lambda: next(t)
        ok, detail = steps.wait_for_server(5000, timeout=100, opener=opener, sleep=lambda s: None)
    finally:
        time.monotonic = real
    assert not ok and "failed to load" in detail


@pytest.mark.skipif(sys.platform != "win32", reason="runs PowerShell")
def test_run_ps_logs_output(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "LOG_DIR", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "hello.ps1").write_text("param([switch]$NoPause)\nWrite-Host 'hi there'\nexit 3\n")
    monkeypatch.setattr(steps, "SCRIPTS", str(scripts))
    ok, log = steps.run_ps("hello.ps1", "-NoPause")
    assert ok is False                                   # exit 3
    assert "hi there" in open(log, encoding="utf-8").read()


def test_wait_for_server_uses_https_when_on():
    seen = []

    def opener(url, timeout):
        seen.append(url)
        return io.BytesIO(json.dumps({"status": "ok", "model": "turbo"}).encode())
    ok, _ = steps.wait_for_server(5055, opener=opener, sleep=lambda s: None, https=True)
    assert ok and seen == ["https://127.0.0.1:5055/health"]


def test_fresh_install_defaults_to_https():
    with open(os.path.join(steps.ROOT, ".env.example"), encoding="utf-8") as f:
        assert "\nSERVER_HTTPS=true\n" in f.read()
