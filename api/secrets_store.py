"""Keep API keys in .env encrypted at rest (Windows DPAPI).

A protected value looks like `dpapi:<base64>` in .env. It is encrypted with
Windows DPAPI in *machine* scope (CRYPTPROTECT_LOCAL_MACHINE), because two
different accounts must read it: the server runs as a Windows service
(LocalSystem, or the NT SERVICE\\FasterWhisperAPI account) and the tray,
listener and Settings run as the signed-in user.

What this protects against, honestly:
  - a copied .env, a backup or cloud-synced copy of the folder, a screenshot or
    screen share of the file, or committing it to git by accident: the value is
    useless anywhere but this PC;
What it does NOT protect against:
  - a program already running on this PC. Any process on this machine, under
    any account, can ask Windows to decrypt it. It's the same protection as a
    browser's saved passwords without a master password.

Rules:
  - `reveal` passes plain values through unchanged (older .env files, Linux,
    hand-edited keys). It never raises.
  - A `dpapi:` value that can't be decrypted (the .env came from another PC, or
    this isn't Windows) is treated as missing, with one log line naming the
    setting. Key values are never logged.
  - `protect` is a no-op off Windows, and returns the plain value rather than
    lose the key if encryption fails.
"""
import base64
import os
import sys
import tempfile

PREFIX = "dpapi:"

# Every .env setting that holds a secret. Reads go through reveal(), writes
# through protect().
SECRET_KEYS = (
    "WHISPER_API_KEY",    # this server's own key (phones, other programs)
    "REMOTE_API_KEY",     # another PC's Whisper Unchained Server
    "CLOUD_API_KEY",      # Groq / OpenAI / custom cloud transcription
    "EDIT_LLM_API_KEY",   # OpenAI-compatible AI Editing server
    "MEAN_LLM_API_KEY",   # OpenAI-compatible mean-mode server
)

# Fixed extra entropy: a DPAPI blob made by some other program on this PC
# won't decrypt as ours, and ours won't decrypt elsewhere by accident.
_ENTROPY = b"WhisperUnchained/.env/v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_CRYPTPROTECT_LOCAL_MACHINE = 0x04

_warned = set()


def _log(msg):
    try:
        print("[secrets] " + msg, file=sys.stderr, flush=True)
    except Exception:
        pass


# --- DPAPI via ctypes (Windows only) ---------------------------------------

def _dpapi():
    """(crypt32, kernel32, DATA_BLOB) or None off Windows."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
            fn.restype = wintypes.BOOL
        return ctypes, crypt32, kernel32, DATA_BLOB
    except Exception:
        return None


def _call(protecting, data):
    """Run CryptProtectData / CryptUnprotectData on bytes. Raises OSError."""
    api = _dpapi()
    if api is None:
        raise OSError("DPAPI isn't available on this system")
    ctypes, crypt32, kernel32, DATA_BLOB = api
    in_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    entropy = DATA_BLOB(len(_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if protecting:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed (error %d)" % ctypes.GetLastError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def available():
    """True when values can be encrypted here (Windows with DPAPI)."""
    return _dpapi() is not None


# --- the public API --------------------------------------------------------

def is_protected(value):
    return isinstance(value, str) and value.strip().startswith(PREFIX)


def protect(plain):
    """`plain` -> "dpapi:<base64>" on Windows. Empty, already-protected and
    non-Windows values come back unchanged; so does `plain` if encryption fails
    (a readable key beats a lost one)."""
    if not plain or is_protected(plain) or not available():
        return plain
    try:
        blob = _call(True, plain.encode("utf-8"))
        return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")
    except Exception as e:
        _log("couldn't encrypt a key, it stays readable in .env (%s)" % type(e).__name__)
        return plain


def _decrypt(value):
    """Plain text of a dpapi: value. Raises on failure."""
    raw = value.strip()[len(PREFIX):].strip().strip('"').strip("'")
    blob = base64.urlsafe_b64decode(raw.encode("ascii"))
    return _call(False, blob).decode("utf-8")


def unreadable(value):
    """True for a dpapi: value this machine can't decrypt (another PC's .env,
    or not Windows)."""
    if not is_protected(value):
        return False
    try:
        _decrypt(value)
        return False
    except Exception:
        return True


def reveal(value, name=None):
    """The plain key. Plain values pass through unchanged; None stays None; a
    protected value that can't be decrypted becomes "" (missing), logged once
    per setting. Never raises, never logs the value."""
    if not is_protected(value):
        return value
    try:
        return _decrypt(value)
    except Exception:
        label = name or "an API key"
        if label not in _warned:
            _warned.add(label)
            _log("%s in .env is encrypted for a different PC (or this isn't Windows) and "
                 "can't be read here; treating it as missing. Enter it again in "
                 "Settings." % label)
        return ""


def reveal_env(env):
    """Copy of an env mapping with every secret setting revealed."""
    out = dict(env)
    for k in SECRET_KEYS:
        if k in out and out[k] is not None:
            out[k] = reveal(out[k], k)
    return out


def reveal_environ(environ=None):
    """Decrypt secret settings in os.environ in place (after load_dotenv), so
    code that reads os.getenv sees plain keys."""
    environ = os.environ if environ is None else environ
    for k in SECRET_KEYS:
        v = environ.get(k)
        if v is not None and is_protected(v):
            environ[k] = reveal(v, k)


# --- one-time migration of a plaintext .env --------------------------------

def migrate_env_file(path):
    """Encrypt any plaintext secret settings in the .env at `path`, in place.

    Only touches the value of live `KEY=` lines for SECRET_KEYS; comments,
    blank lines, order and line endings stay byte-for-byte. Each new value is
    verified to decrypt back to the original before it's written, and the file
    is replaced atomically (unique temp file + os.replace), so an interrupted
    or failed migration leaves the old, readable .env. If the file changes
    while we work, nothing is written. Returns the names encrypted.
    No-op off Windows.
    """
    if not available():
        return []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    import re
    line_re = re.compile(rb"^(\s*)([A-Z_][A-Z0-9_]*)(\s*=\s*)(.*?)(\s*)$")
    lines = data.splitlines(keepends=True)
    changed = []
    for i, line in enumerate(lines):
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        m = line_re.match(body)
        if not m:
            continue
        name = m.group(2).decode("ascii")
        if name not in SECRET_KEYS:
            continue
        value = m.group(4).decode("utf-8", "replace")
        quote = value[:1] if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2 else ""
        plain = value[1:-1] if quote else value
        if not plain or is_protected(plain):
            continue
        enc = protect(plain)
        if not is_protected(enc) or reveal(enc, name) != plain:
            continue   # couldn't encrypt (or verify): leave it readable
        lines[i] = (m.group(1) + m.group(2) + m.group(3) + enc.encode("ascii")
                    + m.group(5) + ending)
        changed.append(name)
    if not changed:
        return []
    new = b"".join(lines)
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".migrate.tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new)
        # Settings or the installer may have saved meanwhile: theirs wins.
        with open(path, "rb") as f:
            if f.read() != data:
                os.unlink(tmp)
                return []
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _log("couldn't encrypt the keys in .env (%s); they stay as they were" % type(e).__name__)
        return []
    return changed
