"""Where the desktop's audio goes to become text: this PC, another PC, or a cloud.

Used by the hotkey listener and the right-click "Transcribe to Clipboard".
TRANSCRIBE_SOURCE in .env picks the path:

  local   this PC's own server (http://127.0.0.1:<port>/transcribe). Default.
  remote  another PC running Whisper Unchained Server: REMOTE_SERVER_URL is its
          base URL ending in /v1, REMOTE_API_KEY its key if it has one. That PC
          applies its own vocabulary, name corrections, "prompt AI" commands and
          cleanup, exactly as for its own hotkey.
  cloud   an OpenAI-compatible transcription service (Groq, OpenAI, ...):
          CLOUD_BASE_URL, CLOUD_API_KEY, CLOUD_MODEL. The vocabulary on this PC
          is sent as hints; name corrections and filler removal run here.
          "prompt AI" commands and cleanup aren't available in this mode yet.

Older setups that only set WHISPER_API_URL (a laptop pointed at another PC's
/transcribe) keep working: with no TRANSCRIBE_SOURCE, that URL is used as is.
"""
import os
import re
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_api  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES = ("local", "remote", "cloud")

# Presets for the cloud choice: (label, base URL, default model).
CLOUD_PRESETS = {
    "groq": ("Groq", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "whisper-1"),
    "custom": ("Other OpenAI-compatible service", "", ""),
}


class TranscribeError(Exception):
    """A failure worth showing the user as-is (it's already plain English)."""


def source(env):
    s = (env.get("TRANSCRIBE_SOURCE") or "").strip().lower()
    return s if s in SOURCES else "local"


def _local_url(env):
    legacy = (env.get("WHISPER_API_URL") or "").strip()
    if legacy and not (env.get("TRANSCRIBE_SOURCE") or "").strip():
        return legacy
    return local_api.base_url(env) + "/transcribe"


def _remote_http(env, default):
    """requests.post/get for another PC's server: pinned when it uses HTTPS with
    its own certificate (REMOTE_CERT_SHA256), else the plain function."""
    pin = local_api.normalize_pin(env.get("REMOTE_CERT_SHA256"))
    base = (env.get("REMOTE_SERVER_URL") or "").strip().lower()
    if pin and base.startswith("https://"):
        session = local_api.pinned_session(pin)
        return session.post if default is requests.post else session.get
    return default


def _bearer(key):
    key = (key or "").strip()
    return {"Authorization": "Bearer " + key} if key else {}


def _openai_error(resp):
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return "HTTP %d" % resp.status_code


def _words():
    """(vocabulary, name_corrections) from this PC's word lists."""
    sys.path.insert(0, HERE)
    import words_config
    cfg = words_config.load()
    return list(cfg.get("vocabulary", [])), dict(cfg.get("name_corrections", {}))


def apply_name_corrections(text, corrections):
    """Same rule as the server: whole word, any case, matched literally."""
    for heard, write_as in corrections.items():
        text = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(heard), lambda _m, w=write_as: w,
                      text, flags=re.IGNORECASE)
    return text


_SEP = re.compile(r"^[\s,.;:!?]*$")


def strip_hint_echo(text, hints):
    """Remove the hint list read back as speech at the end of a cloud reply
    ("...the page. Ollama, Lesley, Ame, Sara, Lesley, Ame, Sara, Lesley.").
    Text-only version of the server's rule (contract 01 section 10): the
    trailing run must be ALL hint words AND one of them must appear 3+ times.
    So "Let's ask Ollama" and "Say hi to Sara" are never touched."""
    hint_words = {w.lower() for w in re.findall(r"[\w']+", hints or "")}
    tokens = list(re.finditer(r"[\w']+", text))
    if not hint_words or not tokens or not _SEP.match(text[tokens[-1].end():]):
        return text
    start = len(tokens)
    for j in range(len(tokens) - 1, -1, -1):
        if tokens[j].group().lower() not in hint_words:
            break
        if j < len(tokens) - 1 and not _SEP.match(text[tokens[j].end():tokens[j + 1].start()]):
            break
        start = j
    run = [t.group().lower() for t in tokens[start:]]
    if not run or max(run.count(w) for w in set(run)) < 3:
        return text
    return text[:tokens[start].start()].rstrip(" ,;:")

def transcribe(path, env, timeout=60, post=requests.post):
    """Audio file -> text, via the configured source. Raises TranscribeError."""
    src = source(env)
    try:
        with open(path, "rb") as f:
            if src == "local":
                kw = {"verify": local_api.requests_verify(env)} if local_api.uses_https(env) else {}
                r = post(_local_url(env), files={"file": (os.path.basename(path), f, "audio/wav")},
                         headers=_bearer(env.get("WHISPER_API_KEY")), timeout=timeout, **kw)
                if r.status_code == 503:
                    detail = r.json().get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else ""
                    raise TranscribeError(detail or "The transcription server isn't ready.")
                r.raise_for_status()
                return (r.json().get("text") or "").strip()

            if src == "remote":
                base = (env.get("REMOTE_SERVER_URL") or "").strip().rstrip("/")
                if not base:
                    raise TranscribeError("No server address set (Settings > Transcription).")
                if post is requests.post:
                    post = _remote_http(env, requests.post)
                r = post(base + "/audio/transcriptions",
                         files={"file": (os.path.basename(path), f, "audio/wav")},
                         data={"response_format": "json"},
                         headers=_bearer(env.get("REMOTE_API_KEY")), timeout=timeout)
                if r.status_code >= 400:
                    raise TranscribeError("The server PC said: %s" % _openai_error(r))
                return (r.json().get("text") or "").strip()

            # cloud
            base = (env.get("CLOUD_BASE_URL") or "").strip().rstrip("/")
            model = (env.get("CLOUD_MODEL") or "").strip()
            if not base or not model:
                raise TranscribeError("The cloud service isn't set up (Settings > Transcription).")
            vocab, corrections = _words()
            hints = ", ".join(dict.fromkeys(vocab + list(corrections.values())))
            data = {"model": model, "response_format": "json"}
            if hints:
                data["prompt"] = hints
            r = post(base + "/audio/transcriptions",
                     files={"file": (os.path.basename(path), f, "audio/wav")},
                     data=data, headers=_bearer(env.get("CLOUD_API_KEY")), timeout=timeout)
            if r.status_code >= 400:
                raise TranscribeError("The cloud service said: %s" % _openai_error(r))
            text = strip_hint_echo((r.json().get("text") or "").strip(), hints)
    except requests.RequestException as e:
        where = {"local": "this PC's server", "remote": "the server PC",
                 "cloud": "the cloud service"}[src]
        raise TranscribeError("Couldn't reach %s (%s)." % (where, e.__class__.__name__)) from e

    # Cloud only: the parts the server would have done.
    text = apply_name_corrections(text, corrections)
    if (env.get("REMOVE_FILLERS") or "").strip().lower() in ("1", "true", "yes", "on"):
        sys.path.insert(0, os.path.join(ROOT, "api"))
        from fillers import remove_fillers
        text, _removed = remove_fillers(text)
    return text


def transcribe_segments(path, env, timeout=3600, post=requests.post, get=requests.get):
    """Audio/video file -> (text, segments) for the "Transcribe a file" window.

    segments = [{"start": s, "end": s, "text": ...}], for subtitles. A Whisper
    Unchained server is asked for plain text (processing=raw), so a spoken
    "prompt AI" inside a recording is never run as a command; name corrections
    are then applied here (this PC's list, or the server PC's own list)."""
    src = source(env)
    data = {"response_format": "verbose_json"}
    corrections, hints = {}, ""
    try:
        if src == "local":
            legacy = (env.get("WHISPER_API_URL") or "").strip()
            if legacy and not (env.get("TRANSCRIBE_SOURCE") or "").strip():
                url = legacy.rstrip("/").rsplit("/", 1)[0] + "/v1/audio/transcriptions"
            else:
                url = local_api.base_url(env) + "/v1/audio/transcriptions"
            kw = {"verify": local_api.requests_verify(env)} if local_api.uses_https(env) else {}
            key, where = env.get("WHISPER_API_KEY"), "This PC's server"
            data["processing"] = "raw"
            data["source"] = "file"   # not a dictation: kept out of the corpus and log
            corrections = _words()[1]
        elif src == "remote":
            base = (env.get("REMOTE_SERVER_URL") or "").strip().rstrip("/")
            if not base:
                raise TranscribeError("No server address set (Settings > Transcription).")
            url, kw = base + "/audio/transcriptions", {}
            key, where = env.get("REMOTE_API_KEY"), "The server PC"
            data["processing"] = "raw"
            data["source"] = "file"
            if post is requests.post:
                post, get = _remote_http(env, requests.post), _remote_http(env, requests.get)
            try:
                r = get(base + "/vocabulary", headers=_bearer(key), timeout=10)
                corrections = dict(r.json().get("name_corrections") or {}) if r.status_code == 200 else {}
            except (requests.RequestException, ValueError):
                corrections = _words()[1]
        else:
            base = (env.get("CLOUD_BASE_URL") or "").strip().rstrip("/")
            model = (env.get("CLOUD_MODEL") or "").strip()
            if not base or not model:
                raise TranscribeError("The cloud service isn't set up (Settings > Transcription).")
            vocab, corrections = _words()
            hints = ", ".join(dict.fromkeys(vocab + list(corrections.values())))
            data["model"] = model
            if hints:
                data["prompt"] = hints
            url, kw = base + "/audio/transcriptions", {}
            key, where = env.get("CLOUD_API_KEY"), "The cloud service"
        with open(path, "rb") as f:
            r = post(url, files={"file": (os.path.basename(path), f, "application/octet-stream")},
                     data=data, headers=_bearer(key), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise TranscribeError("Couldn't reach %s (%s)." % (
            {"local": "this PC's server", "remote": "the server PC",
             "cloud": "the cloud service"}[src], e.__class__.__name__)) from e
    if r.status_code >= 400:
        raise TranscribeError("%s said: %s" % (where, _openai_error(r)))
    body = r.json()
    segments = [{"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
                 "text": apply_name_corrections((s.get("text") or "").strip(), corrections)}
                for s in body.get("segments") or []]
    text = apply_name_corrections((body.get("text") or "").strip(), corrections)
    if hints:
        text = strip_hint_echo(text, hints)
    return text, segments


def check_connection(env, get=requests.get, timeout=10):
    """(ok, message) for the wizard's / Settings' connection test, without audio:
    GET <base>/models, which our server, OpenAI and Groq all answer."""
    src = source(env)
    if src == "local":
        return True, "This PC"
    base = (env.get("REMOTE_SERVER_URL" if src == "remote" else "CLOUD_BASE_URL") or "").strip().rstrip("/")
    key = env.get("REMOTE_API_KEY" if src == "remote" else "CLOUD_API_KEY")
    if not base:
        return False, "No address set."
    if src == "remote" and get is requests.get:
        get = _remote_http(env, requests.get)
    try:
        r = get(base + "/models", headers=_bearer(key), timeout=timeout)
    except requests.RequestException as e:
        return False, "Couldn't connect (%s)." % e.__class__.__name__
    if r.status_code in (401, 403):
        return False, "Connected, but the API key was rejected."
    if r.status_code >= 400:
        return False, "Connected, but it answered %s." % _openai_error(r)
    return True, "Connected."
