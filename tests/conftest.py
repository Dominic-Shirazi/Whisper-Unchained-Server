"""Shared test setup."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import llm_detect  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_server_search(request, monkeypatch):
    """The Settings window and wizard look for AI servers when they open. Tests
    must never depend on what's running on the test machine, so that search
    finds nothing and every port looks free (test_llm_detect tests the real code)."""
    if request.module.__name__.endswith("test_llm_detect"):
        return
    monkeypatch.setattr(llm_detect, "detect", lambda *a, **k: [])
    monkeypatch.setattr(llm_detect, "detect_target", lambda *a, **k: [])
    monkeypatch.setattr(llm_detect, "port_status", lambda *a, **k: "free")
