"""Find AI (LLM) servers already running, for the AI Editing settings.

Looks at each well-known server's default port and asks it for its model list
(GET /v1/models, the OpenAI-style call they all answer; Ollama also has
/api/tags). Nothing is installed or changed. A port that answers with
something else (another app) is ignored.

Also checks whether the transcription server's own port is free, since some
of these (text-generation-webui) default to 5000 too.
"""
import json
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# (name, default port, base path). Order = how they're listed when found.
KNOWN_SERVERS = [
    ("Ollama", 11434, "/v1"),
    ("LM Studio", 1234, "/v1"),
    ("llama.cpp", 8080, "/v1"),
    ("vLLM", 8000, "/v1"),
    ("Unsloth Studio", 8888, "/v1"),
    ("Jan", 1337, "/v1"),
    ("KoboldCpp", 5001, "/v1"),
]

# Suggested editor models, best first. Matched against what a server has.
# Notes are shown next to the model; "tested" only after the contract-02/03
# checks were run on it.
# Best first; the first one a server has is picked. Ranked by the 2026-11-11
# bench (55 cases: real dictations, "scratch that" / "no wait", spoken
# formatting, a dictated "ignore all previous instructions", styles, mean mode).
# Tiny cleanup-only models (e.g. SpeakoFlow Mini) failed it: they dropped
# whole spoken sentences, so none are suggested.
SUGGESTED_MODELS = [
    ("bonsai2", "Best results; needs a big GPU. Turn on \"Skip thinking\"."),
    ("gemma4", "Best small model (e4b is about 4 GB): accurate and fast."),
    ("qwen3.5", "Good small model (4b is about 3.5 GB); weaker at mean mode."),
    ("qwen2.5:7b-instruct", "Older default (about 5 GB); still fine for cleanup."),
]


class Found:
    def __init__(self, name, host, port, base_url, models, needs_key=False):
        self.name, self.host, self.port = name, host, port
        self.base_url, self.models, self.needs_key = base_url, models, needs_key

    @property
    def is_ollama(self):
        return self.name == "Ollama"

    def label(self, local_hosts=("127.0.0.1", "localhost")):
        where = "this PC" if self.host in local_hosts else self.host
        if self.needs_key:
            what = "needs an API key"
        elif self.models:
            what = "%d model%s" % (len(self.models), "" if len(self.models) == 1 else "s")
        else:
            what = "no models loaded"
        return "%s on %s (port %d): %s" % (self.name, where, self.port, what)

    def __repr__(self):
        return "Found(%r, %r:%d, %d models)" % (self.name, self.host, self.port, len(self.models))


def _get_json(url, timeout, opener):
    with opener(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "null")


def probe(name, host, port, path="/v1", timeout=0.8, opener=urllib.request.urlopen):
    """One server: a Found, or None if nothing LLM-like answers there."""
    base = "http://%s:%d" % (host, port)
    try:
        data = _get_json(base + path + "/models", timeout, opener)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            ids = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
            return Found(name, host, port, base + path, ids)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return Found(name, host, port, base + path, [], needs_key=True)
    except Exception:
        pass
    if name == "Ollama":
        try:
            data = _get_json(base + "/api/tags", timeout, opener)
            if isinstance(data, dict) and isinstance(data.get("models"), list):
                ids = [m.get("name") for m in data["models"] if isinstance(m, dict) and m.get("name")]
                return Found(name, host, port, base + path, ids)
        except Exception:
            pass
    return None


def detect(hosts=("127.0.0.1",), skip_ports=(), timeout=0.8, opener=urllib.request.urlopen):
    """Every known server on every host, checked in parallel (about a second)."""
    jobs = [(n, h, p, path) for h in hosts for (n, p, path) in KNOWN_SERVERS
            if p not in skip_ports]
    with ThreadPoolExecutor(max_workers=min(16, len(jobs) or 1)) as pool:
        results = list(pool.map(lambda j: probe(*j, timeout=timeout, opener=opener), jobs))
    return [r for r in results if r is not None]


def parse_target(text):
    """'192.168.0.203' -> (host, None); '192.168.0.203:8081' or a URL -> (host, port)."""
    t = (text or "").strip()
    for prefix in ("http://", "https://"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
    t = t.split("/")[0]
    if not t:
        return None, None
    if ":" in t:
        host, _, port = t.rpartition(":")
        if port.isdigit():
            return host, int(port)
    return t, None


def detect_target(text, timeout=0.8, opener=urllib.request.urlopen):
    """What the user typed in "Look on another PC": a bare address checks every
    known port; address:port checks just that one."""
    host, port = parse_target(text)
    if not host:
        return []
    if port is None:
        return detect(hosts=(host,), timeout=timeout, opener=opener)
    name = next((n for n, p, _ in KNOWN_SERVERS if p == port), "AI server")
    found = probe(name, host, port, timeout=max(timeout, 2), opener=opener)
    return [found] if found else []


def best_model(models):
    """The suggested model a server has (best first), else its first model."""
    lowered = [(m, m.lower()) for m in models]
    for prefix, _note in SUGGESTED_MODELS:
        for m, low in lowered:
            if low.startswith(prefix) or ("/" + prefix) in low:
                return m
    return models[0] if models else None


def model_note(model):
    low = (model or "").lower()
    for prefix, note in SUGGESTED_MODELS:
        if low.startswith(prefix) or ("/" + prefix) in low:
            return note
    return ""


# --- the transcription server's own port -----------------------------------------

def port_status(port, opener=urllib.request.urlopen):
    """'free', 'ours' (Whisper Unchained already answers there), or 'taken'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return "free"
    except OSError:
        pass
    finally:
        s.close()
    try:
        data = _get_json("http://127.0.0.1:%d/health" % port, 1.5, opener)
        if isinstance(data, dict) and data.get("status") in ("ok", "paused", "error") \
                and "model" in data:
            return "ours"
    except Exception:
        pass
    return "taken"


def suggest_port(start=5000, status=port_status, tries=50):
    """`start` if it's free or already ours, else the next free port that isn't
    another known server's default."""
    taken_by_default = {p for _n, p, _ in KNOWN_SERVERS}
    for port in range(start, start + tries):
        if port != start and port in taken_by_default:
            continue
        if status(port) in ("free", "ours"):
            return port
    return start
