"""Unload this project's Ollama models from THIS PC's VRAM.

Used by the tray's "Pause API (Free VRAM)". Stopping the Whisper service frees
Whisper's VRAM, but the Ollama edit model is pinned separately (keep_alive -1
holds it forever) and would stay resident -- several GB -- after a pause.

Only models this stack uses (OLLAMA_MODEL, OLLAMA_ROAST_MODEL) are unloaded, and
only from an Ollama on this machine. A remote edit box (EDIT_LLM_BASE_URL, or
an OLLAMA_*_URL pointing at another host) is never touched: pausing this PC must
not evict a model someone else's machine is serving.
"""
import json
import os
import re
import urllib.parse
import urllib.request

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
DEFAULT_LOCAL = "http://127.0.0.1:11434"


def _base(url):
    """http://host:port from any Ollama URL (e.g. .../api/generate)."""
    return re.sub(r"/api/.*$", "", (url or "").strip()).rstrip("/")


def local_ollama_bases(env):
    """The local Ollama base URLs the config points at (usually just one)."""
    bases = []
    for key in ("OLLAMA_API_URL", "OLLAMA_EDIT_API_URL"):
        b = _base(env.get(key, ""))
        if b and urllib.parse.urlparse(b).hostname in LOCAL_HOSTS and b not in bases:
            bases.append(b)
    return bases or [DEFAULT_LOCAL]


def our_models(env):
    return {m.strip() for m in (env.get("OLLAMA_MODEL", ""),
                                env.get("OLLAMA_ROAST_MODEL", "")) if m and m.strip()}


def _http_json(url, payload=None, timeout=3):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def unload_our_models(env, http=_http_json):
    """Unload our models that are currently loaded. Returns (unloaded, errors).
    Never raises: a down Ollama just means there's nothing to free."""
    wanted = our_models(env)
    unloaded, errors = [], []
    if not wanted:
        return unloaded, errors
    for base in local_ollama_bases(env):
        try:
            loaded = {m.get("name") or m.get("model")
                      for m in http(base + "/api/ps").get("models", [])}
        except Exception:
            continue  # Ollama not running here: nothing in VRAM to free.
        for name in sorted(wanted & loaded):
            try:
                # keep_alive 0 with no prompt = unload now (Ollama API).
                http(base + "/api/generate", {"model": name, "keep_alive": 0})
                unloaded.append(name)
            except Exception as e:
                errors.append("%s: %s" % (name, e))
    return unloaded, errors


def read_env(path):
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$", line)
                if m:
                    values[m.group(1)] = m.group(2).strip()
    return values
