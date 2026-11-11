"""Finding AI servers already running (client/llm_detect.py). No network:
every HTTP call goes to a fake opener."""
import io
import json
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import llm_detect as d  # noqa: E402


def opener_for(routes):
    """routes: {url: dict | int (HTTP error code)}; anything else refuses."""
    def opener(url, timeout=None):
        if url not in routes:
            raise urllib.error.URLError("refused")
        r = routes[url]
        if isinstance(r, int):
            raise urllib.error.HTTPError(url, r, "err", {}, io.BytesIO(b""))
        body = io.BytesIO(json.dumps(r).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: False
        return body
    return opener


def test_openai_style_server_found():
    op = opener_for({"http://127.0.0.1:1234/v1/models": {"data": [{"id": "qwen2.5-7b-instruct"}]}})
    f = d.probe("LM Studio", "127.0.0.1", 1234, opener=op)
    assert f.models == ["qwen2.5-7b-instruct"] and f.base_url == "http://127.0.0.1:1234/v1"
    assert f.label() == "LM Studio on this PC (port 1234): 1 model"


def test_ollama_falls_back_to_its_own_list():
    op = opener_for({"http://127.0.0.1:11434/api/tags": {"models": [{"name": "qwen2.5:7b-instruct-q4_K_M"}]}})
    f = d.probe("Ollama", "127.0.0.1", 11434, opener=op)
    assert f.is_ollama and f.models == ["qwen2.5:7b-instruct-q4_K_M"]


def test_server_wanting_a_key_is_reported():
    op = opener_for({"http://10.0.0.5:8081/v1/models": 401})
    f = d.probe("AI server", "10.0.0.5", 8081, opener=op)
    assert f.needs_key and "needs an API key" in f.label()


def test_other_apps_on_a_port_are_ignored():
    op = opener_for({"http://127.0.0.1:8000/v1/models": {"hello": "not an LLM"}})
    assert d.probe("vLLM", "127.0.0.1", 8000, opener=op) is None
    assert d.probe("vLLM", "127.0.0.1", 8000, opener=opener_for({"http://127.0.0.1:8000/v1/models": 404})) is None


def test_detect_skips_our_own_port_and_finds_several():
    op = opener_for({
        "http://127.0.0.1:11434/v1/models": {"data": [{"id": "bonsai2"}]},
        "http://127.0.0.1:8080/v1/models": {"data": []},
        "http://127.0.0.1:5001/v1/models": {"data": [{"id": "x"}]},
    })
    names = [f.name for f in d.detect(skip_ports=(5001,), opener=op)]
    assert names == ["Ollama", "llama.cpp"]


@pytest.mark.parametrize("text,expected", [
    ("192.168.0.203", ("192.168.0.203", None)),
    ("192.168.0.203:8081", ("192.168.0.203", 8081)),
    ("http://192.168.0.203:8081/v1", ("192.168.0.203", 8081)),
    ("", (None, None)),
])
def test_parse_target(text, expected):
    assert d.parse_target(text) == expected


def test_best_model_prefers_the_suggested_ones():
    assert d.best_model(["llama3.2", "qwen2.5:7b-instruct-q4_K_M"]) == "qwen2.5:7b-instruct-q4_K_M"
    assert d.best_model(["llama3.2", "mistral"]) == "llama3.2"
    assert d.best_model([]) is None


def test_suggest_port_skips_taken_and_other_servers_defaults():
    status = {5000: "taken", 5001: "free", 5002: "free"}
    assert d.suggest_port(5000, status=lambda p: status.get(p, "free")) == 5002  # 5001 is KoboldCpp's
    assert d.suggest_port(5000, status=lambda p: "ours") == 5000
