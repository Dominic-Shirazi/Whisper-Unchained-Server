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
_API_DIR = os.path.join(ROOT, "api")
if _API_DIR not in sys.path:
    sys.path.append(_API_DIR)
import secrets_store  # noqa: E402  (api/secrets_store.py: keys may be stored encrypted)
SOURCES = ("local", "remote", "cloud")

# Presets for the cloud choice: (label, base URL, default model). The same
# services, in the same order, as the phone app (its ProviderPreset list). The
# keys are what CLOUD_PROVIDER holds in people's settings: never rename one.
CLOUD_PRESETS = {
    "groq": ("Groq", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
    "together": ("Together AI", "https://api.together.xyz/v1", "whisper-large-v3"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "whisper-1"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai/whisper-large-v3"),
    "custom": ("Another service", "", ""),
}

# When the prices below were looked up. They are the phone app's figures
# (WhisperRemote SettingsRepository.kt, ProviderPreset): change both together.
CLOUD_PRICES_AS_OF = "November 2026"

# What Settings and setup say about each service: why you would choose it, its
# price ({as_of} = CLOUD_PRICES_AS_OF), what happens to your audio, and where
# its prices are listed.
CLOUD_INFO = {
    "groq": {
        "why": "Recommended. Very fast: usually well under a second for a short clip.",
        "price": "About $0.04 per hour of audio (as of {as_of}). A generous free tier "
                 "is included.",
        "privacy": "Groq says audio sent this way is not used to train models. See "
                   "Groq's privacy policy.",
        "url": "https://groq.com/pricing/",
    },
    "together": {
        "why": "A dedicated Whisper service on fast hardware.",
        "price": "About $0.0015 per minute, about $0.09 per hour (as of {as_of}). "
                 "Billed by Together AI.",
        "privacy": "Not used for training: its terms say customer audio is never used "
                   "to train models.",
        "url": "https://www.together.ai/pricing",
    },
    "openai": {
        "why": "The official Whisper, from its maker. Moderate speed: about 0.8 to "
               "1.5 seconds.",
        "price": "About $0.006 per minute, about $0.36 per hour (as of {as_of}). "
                 "Billed by OpenAI.",
        "privacy": "Not used for training. OpenAI keeps audio for 30 days to watch for "
                   "abuse, then deletes it.",
        "url": "https://openai.com/api/pricing/",
    },
    "openrouter": {
        "why": "One account for many providers. Slightly slower: one extra stop.",
        "price": "The provider's own rates, passed through. You pay with credits.",
        "privacy": "Your audio passes through OpenRouter to the provider, so both "
                   "privacy policies apply.",
        "url": "https://openrouter.ai/models/openai/whisper-large-v3",
    },
    "custom": {
        "why": "Your own server, or another service that works the way OpenAI's does.",
        "price": "Set by whoever runs the service.",
        "privacy": "Decided entirely by the server or service you point it at.",
        "url": "",
    },
}


def cloud_info(key):
    """The four lines for one service (see CLOUD_INFO), the date filled in.
    An unknown key reads as "custom"."""
    info = CLOUD_INFO.get(key) or CLOUD_INFO["custom"]
    return dict(info, price=info["price"].format(as_of=CLOUD_PRICES_AS_OF))


class TranscribeError(Exception):
    """A failure worth showing the user as-is (it's already plain English)."""


class Unreachable(TranscribeError):
    """The server didn't answer, or said it isn't ready (paused, starting up).
    Trying the same recording again later can work, so the listener keeps it."""


FALLBACK_KEY = "TRANSCRIBE_FALLBACK_CLOUD"


def cloud_ready(env):
    return bool((env.get("CLOUD_BASE_URL") or "").strip() and (env.get("CLOUD_MODEL") or "").strip())


def fallback_wanted(env):
    """Settings > Transcription: "If the server can't be reached, use my cloud
    service". Off unless switched on, and only with a cloud service filled in."""
    on = (env.get(FALLBACK_KEY) or "").strip().lower() in ("1", "true", "yes", "on")
    return on and source(env) != "cloud" and cloud_ready(env)


# --- another PC with more than one address (home network and Tailscale) -------------
_remote_working = {}      # the configured address -> the one that last answered


def remote_bases(env):
    """Every address of the server PC, the one that last worked first:
    REMOTE_SERVER_URL plus REMOTE_ALT_URLS (from a connection code)."""
    main = (env.get("REMOTE_SERVER_URL") or "").strip().rstrip("/")
    alts = [u.strip().rstrip("/") for u in (env.get("REMOTE_ALT_URLS") or "").split(",")]
    urls = [u for u in dict.fromkeys([main] + alts) if u]
    last = _remote_working.get(main)
    if last in urls:
        urls.remove(last)
        urls.insert(0, last)
    return urls


def remote_base(env):
    """The address to use now ("" when none is set)."""
    urls = remote_bases(env)
    return urls[0] if urls else ""


def _remember_remote(env, url):
    _remote_working[(env.get("REMOTE_SERVER_URL") or "").strip().rstrip("/")] = url


def _raise(resp, message):
    """503 = not ready (paused, model loading): worth another try later."""
    raise (Unreachable if resp.status_code == 503 else TranscribeError)(message)


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
    # Every key this module sends passes through here: .env may hold it encrypted.
    key = (secrets_store.reveal(key) or "").strip()
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

def _dictate_url(env):
    """This PC's dictation address that takes per-request style options, or
    None for an old-style WHISPER_API_URL setup (sent as is, no options)."""
    legacy = (env.get("WHISPER_API_URL") or "").strip()
    if legacy and not (env.get("TRANSCRIBE_SOURCE") or "").strip():
        return None
    return local_api.base_url(env) + "/dictate/v1/audio/transcriptions"


def transcribe(path, env, timeout=60, post=requests.post, style=None):
    """Audio file -> text, via the configured source. Raises TranscribeError
    (Unreachable when the server didn't answer or isn't ready).

    With the cloud fallback switched on (fallback_wanted), a dictation the
    server can't take goes to the cloud service instead."""
    try:
        return _transcribe_any_address(path, env, timeout, post, style)
    except Unreachable:
        if not fallback_wanted(env):
            raise
        return _transcribe(path, dict(env, TRANSCRIBE_SOURCE="cloud"), timeout, post, None)


def _transcribe_any_address(path, env, timeout, post, style):
    """Another PC known by two addresses (home network and Tailscale): when
    the first can't be reached, try the next, and remember which one worked."""
    urls = remote_bases(env) if source(env) == "remote" else []
    if len(urls) < 2:
        return _transcribe(path, env, timeout, post, style)
    last = None
    for n, url in enumerate(urls):
        try:
            # A dead address should fail fast, not after the full timeout.
            text = _transcribe(path, dict(env, REMOTE_SERVER_URL=url),
                               (4, timeout) if post is requests.post else timeout, post, style)
        except Unreachable as e:
            last = e
            continue
        _remember_remote(env, url)
        return text
    raise last


def _transcribe(path, env, timeout=60, post=requests.post, style=None):
    """style: per-request cleanup options for this one dictation (app_styles.
    request_fields: {'cleanup': 'on', 'cleanup_tone': 'casual', ...}), used by
    this PC's and another PC's server; a cloud service has no cleanup."""
    src = source(env)
    style = dict(style or {})
    try:
        with open(path, "rb") as f:
            if src == "local" and style and _dictate_url(env):
                kw = {"verify": local_api.requests_verify(env)} if local_api.uses_https(env) else {}
                r = post(_dictate_url(env),
                         files={"file": (os.path.basename(path), f, "audio/wav")},
                         data=dict({"response_format": "json", "processing": "full"}, **style),
                         headers=_bearer(env.get("WHISPER_API_KEY")), timeout=timeout, **kw)
                if r.status_code != 404:   # 404: a server older than per-app styles
                    if r.status_code >= 400:
                        _raise(r, _openai_error(r))
                    return (r.json().get("text") or "").strip()
                f.seek(0)
            if src == "local":
                kw = {"verify": local_api.requests_verify(env)} if local_api.uses_https(env) else {}
                r = post(_local_url(env), files={"file": (os.path.basename(path), f, "audio/wav")},
                         headers=_bearer(env.get("WHISPER_API_KEY")), timeout=timeout, **kw)
                if r.status_code == 503:
                    detail = r.json().get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else ""
                    raise Unreachable(detail or "The transcription server isn't ready.")
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
                         # A dictation: the server PC's corrections, trigger
                         # phrases and cleanup (its default is a plain transcription).
                         data=dict({"response_format": "json", "processing": "full"},
                                   **style),
                         headers=_bearer(env.get("REMOTE_API_KEY")), timeout=timeout)
                if r.status_code >= 400:
                    _raise(r, "The server PC said: %s" % _openai_error(r))
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
        raise Unreachable("Couldn't reach %s (%s)." % (where, e.__class__.__name__)) from e

    # Cloud only: the parts the server would have done.
    text = apply_name_corrections(text, corrections)
    if (env.get("REMOVE_FILLERS") or "").strip().lower() in ("1", "true", "yes", "on"):
        sys.path.insert(0, os.path.join(ROOT, "api"))
        from fillers import remove_fillers
        text, _removed = remove_fillers(text)
    return text


def edit_selection(path, env, selection, timeout=90, post=requests.post):
    """Voice-edit highlighted text: the recording at path is the instruction
    ("make this more polite"), selection the text to change. Returns the
    edited text (the selection itself when nothing was said or the editor
    couldn't be reached). Needs this PC's or another PC's server: a cloud
    transcription service has no editor. Raises TranscribeError."""
    return edit_selection_result(path, env, selection, timeout=timeout, post=post)[0]


def edit_selection_result(path, env, selection, timeout=90, post=requests.post):
    """edit_selection, and whether the server says the text was edited:
    (text, edited). edited is False when nothing was said or the AI gave no
    answer (text is then the selection as it was); a server too old to say
    counts as edited, so the caller compares the text itself."""
    src, url, key, kw, post = _editor_endpoint(env, "/dictate/v1/edit_selection", post, requests.post)
    try:
        with open(path, "rb") as f:
            r = post(url, files={"file": (os.path.basename(path), f, "audio/wav")},
                     data={"selection": selection}, headers=_bearer(key), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise TranscribeError("Couldn't reach %s (%s)." % (_where(src), e.__class__.__name__)) from e
    if r.status_code == 404:
        raise TranscribeError("The server is too old to edit highlighted text; update it.")
    if r.status_code >= 400:
        raise TranscribeError(_openai_error(r))
    body = r.json()
    edited = body.get("edited")
    return (body.get("text") or selection), (True if edited is None else bool(edited))


def can_edit(env):
    """True when highlighted text can be rewritten at all: the AI editor is
    part of Whisper Unchained Server (this PC or another PC). A cloud
    transcription service has none (see _editor_endpoint)."""
    return source(env) != "cloud"


def _where(src):
    return "this PC's server" if src == "local" else "the server PC"


def _editor_endpoint(env, path, http, default):
    """(source, url, key, request kwargs, http function) for one of the server's
    AI editor addresses. A cloud transcription service has no editor."""
    src = source(env)
    if src == "cloud":
        raise TranscribeError("The AI editor needs Whisper Unchained Server "
                              "(this PC or another PC), not a cloud service.")
    if src == "local":
        kw = {"verify": local_api.requests_verify(env)} if local_api.uses_https(env) else {}
        return src, local_api.base_url(env) + path, env.get("WHISPER_API_KEY"), kw, http
    base = remote_base(env)
    if not base:
        raise TranscribeError("No server address set (Settings > Transcription).")
    root = base[:-3] if base.endswith("/v1") else base
    if http is default:
        http = _remote_http(env, default)
    return src, root + path, env.get("REMOTE_API_KEY"), {}, http


def edit_text(env, instruction, selection="", timeout=90, post=requests.post):
    """A typed instruction for the AI editor (the box on the floating mic
    button's right-click). With `selection` the answer is that text, changed;
    without, new text. Returns (text, ok): ok False means the AI didn't answer
    and text is the selection as it was ("" without one). Raises
    TranscribeError when the server can't be reached or refuses."""
    src, url, key, kw, post = _editor_endpoint(env, "/dictate/v1/edit_text", post, requests.post)
    try:
        r = post(url, json={"instruction": instruction, "selection": selection or ""},
                 headers=_bearer(key), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise TranscribeError("Couldn't reach %s (%s)." % (_where(src), e.__class__.__name__)) from e
    if r.status_code in (404, 405):
        raise TranscribeError("The server is too old for typed instructions; update it"
                              + (" (Settings > Save and restart)." if src == "local" else "."))
    if r.status_code >= 400:
        raise TranscribeError(_openai_error(r))
    body = r.json()
    return (body.get("text") or ""), bool(body.get("edited"))


def name_corrections(env, timeout=4, get=requests.get):
    """{"heard as": "type this"} as the transcribing PC has them now: this PC's
    word file, or the server PC's list. {} for a cloud service or on any
    failure (never raises)."""
    try:
        src = source(env)
        if src == "local":
            return dict(_words()[1])
        if src == "remote":
            _src, url, key, kw, get = _editor_endpoint(env, "/v1/vocabulary", get, requests.get)
            r = get(url, headers=_bearer(key), timeout=timeout, **kw)
            if r.status_code == 200:
                return dict(r.json().get("name_corrections") or {})
    except Exception:
        pass
    return {}


def save_correction(env, heard, corrected, timeout=10, post=requests.post):
    """Add one name correction on the transcribing PC (it takes effect at
    once, and that PC backs up its word file when one is replaced). Raises
    TranscribeError with a plain message when it can't be saved."""
    src, url, key, kw, post = _editor_endpoint(env, "/v1/name_corrections", post, requests.post)
    try:
        r = post(url, json={"set": {heard: corrected}}, headers=_bearer(key),
                 timeout=timeout, **kw)
    except requests.RequestException as e:
        raise TranscribeError("Couldn't reach %s (%s)." % (_where(src), e.__class__.__name__)) from e
    if r.status_code >= 400:
        raise TranscribeError(_openai_error(r))


def custom_triggers(env, timeout=4, get=requests.get):
    """The switched-on custom triggers of the server that does the editing:
    this PC's word file, or the other PC's list over the network. [] when
    there are none or it can't be read; never raises."""
    try:
        if source(env) == "remote":
            _src, url, key, kw, get = _editor_endpoint(env, "/v1/custom_triggers", get, requests.get)
            r = get(url, headers=_bearer(key), timeout=timeout, **kw)
            items = r.json().get("custom_triggers", []) if r.status_code == 200 else []
        else:
            import words_config
            items = words_config.custom_triggers.load(words_config.load())
        return [t for t in items if isinstance(t, dict) and t.get("enabled", True)]
    except Exception:
        return []


def transcribe_segments(path, env, timeout=3600, post=requests.post, get=requests.get,
                        apply_corrections=False):
    """Audio/video file -> (text, segments) for the "Transcribe a file" window.

    segments = [{"start": s, "end": s, "text": ...}], for subtitles. A straight
    transcription: a Whisper Unchained server is asked for plain text
    (processing=raw), so nothing is cleaned up, rewritten or run as a command.
    Name corrections (this PC's list, or the server PC's own) are applied only
    when apply_corrections is set: someone else's speech or a song shouldn't
    be bent to match your names."""
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
            corrections = _words()[1] if apply_corrections else {}
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
            if apply_corrections:
                try:
                    r = get(base + "/vocabulary", headers=_bearer(key), timeout=10)
                    corrections = dict(r.json().get("name_corrections") or {})                         if r.status_code == 200 else {}
                except (requests.RequestException, ValueError):
                    corrections = _words()[1]
        else:
            base = (env.get("CLOUD_BASE_URL") or "").strip().rstrip("/")
            model = (env.get("CLOUD_MODEL") or "").strip()
            if not base or not model:
                raise TranscribeError("The cloud service isn't set up (Settings > Transcription).")
            vocab, corrections = _words()
            hints = ", ".join(dict.fromkeys(vocab + list(corrections.values())))
            if not apply_corrections:
                corrections = {}
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


# --- the server PC's triggers, for a PC that only listens (Settings > Triggers) ------

class TooOld(TranscribeError):
    """The server PC's app is older than this feature."""


def _server_call(env, path, method, payload=None, timeout=8, http=None):
    """GET or POST one of the server PC's /v1 routes; the parsed reply.
    Raises TranscribeError with a plain message (TooOld on 404/405)."""
    default = requests.post if method == "post" else requests.get
    src, url, key, kw, call = _editor_endpoint(env, path, http or default, default)
    try:
        if method == "post":
            r = call(url, json=payload, headers=_bearer(key), timeout=timeout, **kw)
        else:
            r = call(url, headers=_bearer(key), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise TranscribeError("Couldn't reach %s (%s)." % (_where(src), e.__class__.__name__)) from e
    if r.status_code in (404, 405):
        raise TooOld("The app on the server PC is too old for this. Update it there first.")
    if r.status_code >= 400:
        raise TranscribeError(_openai_error(r))
    return r.json()


def server_triggers(env, get=None):
    """(trigger phrase rows, custom triggers) as the server PC has them now,
    switched-off ones included."""
    phrases = _server_call(env, "/v1/trigger_phrases", "get", http=get)
    custom = _server_call(env, "/v1/custom_triggers", "get", http=get)
    return list(phrases.get("trigger_phrases") or []), list(custom.get("custom_triggers") or [])


def save_server_triggers(env, phrases, custom, loaded_custom=(), post=None):
    """Save both lists on the server PC. `loaded_custom` is the custom list as
    it was fetched, so triggers removed here are removed there. The phrases go
    first: if they are refused nothing has changed yet."""
    _server_call(env, "/v1/trigger_phrases", "post",
                 {"phrases": [{"phrase": r["phrase"], "action": r["action"]} for r in phrases]},
                 http=post)
    keep = {t.get("id") for t in custom if t.get("id")}
    gone = [t["id"] for t in loaded_custom if t.get("id") and t["id"] not in keep]
    for n in range(0, max(len(custom), 1), 20):          # the route takes 20 at a time
        batch = list(custom[n:n + 20])
        if batch or (n == 0 and gone):
            _server_call(env, "/v1/custom_triggers", "post",
                         {"set": batch, "remove": gone if n == 0 else []}, http=post)
