"""Pause API also unloads our local Ollama models, and only ours, only locally."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import ollama_unload as ou  # noqa: E402


class FakeOllama:
    def __init__(self, loaded, down=False):
        self.loaded, self.down, self.calls = set(loaded), down, []

    def __call__(self, url, payload=None, timeout=3):
        self.calls.append((url, payload))
        if self.down:
            raise OSError("connection refused")
        if url.endswith("/api/ps"):
            return {"models": [{"name": n} for n in self.loaded]}
        self.loaded.discard(payload["model"])
        return {"done": True}


ENV = {"OLLAMA_API_URL": "http://127.0.0.1:11434/api/generate",
       "OLLAMA_MODEL": "qwen-edit", "OLLAMA_ROAST_MODEL": "roaster"}


def test_unloads_only_our_loaded_models():
    fake = FakeOllama(["qwen-edit", "someone-elses-model"])
    unloaded, errors = ou.unload_our_models(ENV, http=fake)
    assert unloaded == ["qwen-edit"] and errors == []
    assert fake.loaded == {"someone-elses-model"}
    gen = [p for u, p in fake.calls if u.endswith("/api/generate")]
    assert gen == [{"model": "qwen-edit", "keep_alive": 0}]


def test_remote_ollama_never_touched():
    env = dict(ENV, OLLAMA_API_URL="http://100.1.2.3:11434/api/generate",
               OLLAMA_EDIT_API_URL="http://ai-worker3:11434/api/generate")
    fake = FakeOllama(["qwen-edit"])
    ou.unload_our_models(env, http=fake)
    assert all(u.startswith(ou.DEFAULT_LOCAL) for u, _ in fake.calls)


def test_ollama_down_is_not_an_error():
    assert ou.unload_our_models(ENV, http=FakeOllama([], down=True)) == ([], [])


def test_no_models_configured_does_nothing():
    fake = FakeOllama(["x"])
    assert ou.unload_our_models({}, http=fake) == ([], [])
    assert fake.calls == []


def test_local_bases_dedup_and_parse():
    env = {"OLLAMA_API_URL": "http://localhost:11434/api/generate",
           "OLLAMA_EDIT_API_URL": "http://localhost:11434/api/generate"}
    assert ou.local_ollama_bases(env) == ["http://localhost:11434"]
