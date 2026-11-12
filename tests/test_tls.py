"""HTTPS with the server's own certificate (Android contract 06).

Everything here uses temporary folders: the real api/certs/ is never touched.
"""
import http.server
import json
import os
import ssl
import sys
import threading

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "client"))

import local_api  # noqa: E402
import settings_gui as sg  # noqa: E402
import tls  # noqa: E402
import transcribe_client as tc  # noqa: E402


@pytest.fixture
def pair(tmp_path):
    cert, key = tls.ensure_cert(str(tmp_path / "certs"), hostname="testpc")
    return cert, key


@pytest.fixture
def https_server(pair):
    """A tiny HTTPS server on 127.0.0.1 using the generated certificate."""
    cert, key = pair

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "model": "test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "https://127.0.0.1:%d" % httpd.server_address[1], cert
    httpd.shutdown()


def test_cert_created_once_and_fingerprint_stable(tmp_path):
    d = str(tmp_path / "certs")
    cert, key = tls.ensure_cert(d, hostname="testpc")
    fp = tls.fingerprint(cert)
    assert len(fp) == 64 and fp == local_api.fingerprint(cert)
    with open(key, "rb") as f:
        key_bytes = f.read()
    # A second start must never replace the pair (paired phones pin it).
    tls.ensure_cert(d, hostname="other")
    assert tls.fingerprint(cert) == fp
    with open(key, "rb") as f:
        assert f.read() == key_bytes


def test_https_switch():
    assert not tls.https_enabled({})
    assert not tls.https_enabled({"SERVER_HTTPS": "false"})
    assert tls.https_enabled({"SERVER_HTTPS": "true"})
    assert local_api.base_url({"WHISPER_API_PORT": "5055"}) == "http://127.0.0.1:5055"
    assert local_api.base_url({"SERVER_HTTPS": "true"}) == "https://127.0.0.1:5000"


def test_serve_options(monkeypatch):
    import serve
    assert serve.uvicorn_options({"SERVER_HTTPS": "false"}) == {}
    opts = serve.uvicorn_options({"SERVER_HTTPS": "true"}, ensure=lambda: ("c.pem", "k.key"))
    assert opts == {"ssl_certfile": "c.pem", "ssl_keyfile": "k.key"}


def test_local_client_trusts_own_certificate(https_server, monkeypatch):
    url, cert = https_server
    monkeypatch.setattr(local_api, "CERT_PATH", cert)
    env = {"SERVER_HTTPS": "true"}
    r = requests.get(url + "/health", verify=local_api.requests_verify(env), timeout=5)
    assert r.json()["status"] == "ok"
    import urllib.request
    with urllib.request.urlopen(url + "/health", timeout=5,
                                context=local_api.ssl_context(env)) as resp:
        assert json.load(resp)["model"] == "test"


def test_untrusted_without_our_certificate(https_server):
    url, _cert = https_server
    with pytest.raises(requests.exceptions.SSLError):
        requests.get(url + "/health", timeout=5)  # system CAs don't know it


def test_pinned_session_accepts_right_pin_and_refuses_wrong(https_server):
    url, cert = https_server
    good = local_api.pinned_session(tls.fingerprint(cert))
    assert good.get(url + "/health", timeout=5).json()["status"] == "ok"
    bad = local_api.pinned_session("ab" * 32)
    with pytest.raises(requests.exceptions.SSLError):
        bad.get(url + "/health", timeout=5)


def test_normalize_pin():
    fp = "AB:" * 31 + "AB"
    assert local_api.normalize_pin(fp) == "ab" * 32
    assert local_api.normalize_pin("not a pin") == ""
    with pytest.raises(ValueError):
        local_api.pinned_session("short")


def test_pairing_payload_v2_with_pin():
    fp = "cd" * 32
    d = json.loads(sg.pairing_payload("100.1.2.3", "wr_sec_x", "large-v3-turbo", "PC",
                                      port=5000, cert_sha256=fp))
    assert d["v"] == 2
    assert d["base_url"] == "https://100.1.2.3:5000/v1"
    assert d["cert_sha256"] == fp
    old = json.loads(sg.pairing_payload("100.1.2.3", "", "m", "PC"))
    assert old["v"] == 1 and old["base_url"].startswith("http://") and "cert_sha256" not in old


def test_transcribe_local_https_passes_our_certificate(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF....WAVE")
    seen = {}

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"text": "hi"}

        def raise_for_status(self):
            pass

    def post(url, files=None, headers=None, timeout=None, verify=None, data=None):
        seen.update(url=url, verify=verify)
        return R()

    monkeypatch.setattr(local_api, "CERT_PATH", "C:/certs/server.pem")
    assert tc.transcribe(str(wav), {"SERVER_HTTPS": "true"}, post=post) == "hi"
    assert seen["url"] == "https://127.0.0.1:5000/transcribe"
    assert seen["verify"] == "C:/certs/server.pem"


def test_remote_https_uses_pinned_session(monkeypatch):
    calls = {}

    class FakeSession:
        def get(self, url, **kw):
            calls["url"] = url

            class R:
                status_code = 200
            return R()

        post = get

    monkeypatch.setattr(local_api, "pinned_session", lambda pin: calls.setdefault("pin", pin) and FakeSession())
    env = {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "https://100.1.2.3:5000/v1",
           "REMOTE_CERT_SHA256": "EF" * 32}
    ok, _msg = tc.check_connection(env)
    assert ok and calls["pin"] == "ef" * 32 and calls["url"] == "https://100.1.2.3:5000/v1/models"
