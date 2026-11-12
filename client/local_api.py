"""How the desktop pieces reach a Whisper Unchained Server (Android contract 06).

This PC's own server: http://127.0.0.1:<port>, or https:// when SERVER_HTTPS is
on, verified against the server's own certificate in api/certs/server.pem.

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


def port(env):
    return (env.get("WHISPER_API_PORT") or "5000").strip() or "5000"


def base_url(env):
    return "%s://127.0.0.1:%s" % ("https" if https_enabled(env) else "http", port(env))


def requests_verify(env):
    """`verify=` for requests: our own certificate when HTTPS is on."""
    return CERT_PATH if https_enabled(env) else True


def ssl_context(env):
    """`context=` for urllib: trusts only our own certificate when HTTPS is on."""
    if not https_enabled(env):
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
