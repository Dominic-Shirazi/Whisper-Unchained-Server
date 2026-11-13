"""How the desktop pieces reach a Whisper Unchained Server (Android contract 06).

This PC's own server: http://127.0.0.1:<WHISPER_API_PORT>, or, when only the
HTTPS port is on, https://127.0.0.1:<WHISPER_HTTPS_PORT> verified against the
server's own certificate in api/certs/server.pem.

Another PC's server (transcription source "remote"): its certificate is pinned
by SHA-256 fingerprint (REMOTE_CERT_SHA256), the same way the phone does it.
"""
import hashlib
import os
import ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERT_PATH = os.path.join(ROOT, "api", "certs", "server.pem")
TRUE = ("1", "true", "yes", "on")


def https_enabled(env):
    return (env.get("SERVER_HTTPS") or "").strip().lower() in TRUE


def plain_enabled(env):
    """Same rule as api/tls.plain_enabled: a missing line means on, and plain
    HTTP is the fallback when HTTPS is off."""
    raw = (env.get("SERVER_PLAIN_HTTP") or "").strip().lower()
    return (raw in TRUE or not raw) or not https_enabled(env)


def _port(env, key, default):
    raw = (env.get(key) or "").strip()
    return raw if raw.isdigit() and 1 <= int(raw) <= 65535 else default


def port(env):
    """The plain HTTP port (WHISPER_API_PORT)."""
    return _port(env, "WHISPER_API_PORT", "5000")


def https_port(env):
    return _port(env, "WHISPER_HTTPS_PORT", "5001")


def pairing_port(env):
    """The port phones pair to: HTTPS when it's on."""
    return https_port(env) if https_enabled(env) else port(env)


def uses_https(env):
    """Does this PC's own client (hotkey, tray) talk HTTPS to its server? Only
    when the plain port is off: on 127.0.0.1 plain HTTP never leaves the PC."""
    return https_enabled(env) and not plain_enabled(env)


def base_url(env):
    if uses_https(env):
        return "https://127.0.0.1:%s" % https_port(env)
    return "http://127.0.0.1:%s" % port(env)


def requests_verify(env):
    """`verify=` for requests: our own certificate when talking HTTPS."""
    return CERT_PATH if uses_https(env) else True


def ssl_context(env):
    """`context=` for urllib: trusts only our own certificate when talking HTTPS."""
    if not uses_https(env):
        return None
    return ssl.create_default_context(cafile=CERT_PATH)


def fingerprint(cert_path=CERT_PATH):
    """SHA-256 of this server's certificate, for the pairing QR, or "" if none yet."""
    try:
        with open(cert_path, "r", encoding="ascii") as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(der).hexdigest()


def normalize_pin(pin):
    """Accept "AB:CD:…" or "abcd…"; return lowercase hex, or "" if not a SHA-256."""
    p = (pin or "").replace(":", "").replace(" ", "").strip().lower()
    return p if len(p) == 64 and all(c in "0123456789abcdef" for c in p) else ""


def pinned_session(pin):
    """A requests.Session that accepts only the certificate with this fingerprint.

    urllib3 checks assert_fingerprint itself; CA and hostname checks are off
    because the pin, not a name, is the server's identity (as on the phone).
    """
    import requests
    from requests.adapters import HTTPAdapter

    pin = normalize_pin(pin)
    if not pin:
        raise ValueError("Not a SHA-256 certificate fingerprint.")

    class _Pinned(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw.update(cert_reqs="CERT_NONE", assert_hostname=False, assert_fingerprint=pin)
            return super().init_poolmanager(*a, **kw)

    s = requests.Session()
    s.mount("https://", _Pinned())
    # Without this, requests switches CA checking back on per request. The
    # fingerprint check above still runs on every connection.
    s.verify = False
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s
