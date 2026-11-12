"""Headless pairing command (`python -m client.pair`) and the shared pairing
module it uses. Certificates are made in temp folders; api/certs/ is untouched."""
import hashlib
import json
import os
import ssl
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "client"))

import pair  # noqa: E402
import pairing  # noqa: E402
import settings_gui as sg  # noqa: E402
import tls  # noqa: E402

KEY = "wr_sec_test_key_123"


@pytest.fixture
def cert(tmp_path):
    cert_path, _ = tls.ensure_cert(str(tmp_path / "certs"), hostname="testpc")
    return cert_path


def test_settings_window_reexports_the_shared_helpers():
    assert sg.pairing_payload is pairing.pairing_payload
    assert sg.qr_matrix is pairing.qr_matrix
    assert sg.detect_hosts is pairing.detect_hosts
    assert sg.generate_api_key is pairing.generate_api_key


def test_https_payload_pins_the_certificate(cert):
    env = {"SERVER_HTTPS": "true", "WHISPER_API_KEY": KEY,
           "WHISPER_MODEL_SIZE": "large-v3-turbo", "WHISPER_API_PORT": "5001"}
    payload, host, fp = pair.build(env, host="100.1.2.3", name="box", cert_path=cert)
    with open(cert, encoding="ascii") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    assert fp == hashlib.sha256(der).hexdigest() == tls.fingerprint(cert)
    assert json.loads(payload) == {
        "v": 2, "base_url": "https://100.1.2.3:5001/v1", "cert_sha256": fp,
        "model": "large-v3-turbo", "name": "box", "api_key": KEY}
    assert host == "100.1.2.3"


def test_https_without_certificate_refuses(tmp_path):
    with pytest.raises(SystemExit, match="Start the server once"):
        pair.build({"SERVER_HTTPS": "true"}, host="h", cert_path=str(tmp_path / "none.pem"))


def test_http_payload_is_v1_without_pin(cert):
    payload, _, fp = pair.build({"SERVER_HTTPS": "false"}, host="h", name="n", cert_path=cert)
    d = json.loads(payload)
    assert fp == "" and d["v"] == 1 and "cert_sha256" not in d and "api_key" not in d
    assert d["base_url"] == "http://h:5000/v1"


def test_default_host_is_the_first_detected(monkeypatch, cert):
    monkeypatch.setattr(pairing, "detect_hosts", lambda: ["100.9.9.9", "192.168.0.5"])
    _, host, _ = pair.build({}, cert_path=cert)
    assert host == "100.9.9.9"


def test_tailscale_first_then_lan(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(kw.get("creationflags", 0))
        return subprocess.CompletedProcess(cmd, 0, stdout="100.1.2.3\n", stderr="")
    monkeypatch.setattr(pairing.subprocess, "run", fake_run)
    monkeypatch.setattr(pairing, "lan_ips", lambda: ["192.168.0.5", "100.1.2.3"])
    assert pairing.detect_hosts() == ["100.1.2.3", "192.168.0.5"]
    # POSIX refuses creationflags; Windows gets CREATE_NO_WINDOW.
    assert calls == [0x08000000 if sys.platform == "win32" else 0]


def test_no_tailscale_falls_back_to_lan(monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(pairing.subprocess, "run", missing)
    monkeypatch.setattr(pairing, "lan_ips", lambda: ["192.168.0.5"])
    assert pairing.detect_hosts() == ["192.168.0.5"]
    monkeypatch.setattr(pairing, "lan_ips", lambda: [])
    assert pairing.detect_hosts() == ["127.0.0.1"]


def test_lan_ips_skip_loopback():
    assert all(not ip.startswith("127.") for ip in pairing.lan_ips())


def _decode(text, size, border=2):
    """Read the half-block drawing back into a QR matrix."""
    bits = {"█": (False, False), "▄": (True, False),
            "▀": (False, True), " ": (True, True)}
    rows = []
    for line in text.split("\n"):
        pairs = [bits[c] for c in line]
        rows.append([t for t, _ in pairs])
        rows.append([b for _, b in pairs])
    return [r[border:border + size] for r in rows[border:border + size]]


def test_terminal_qr_draws_every_module():
    m = pairing.qr_matrix(pairing.pairing_payload("100.1.2.3", KEY, "large-v3-turbo", "PC",
                                                  cert_sha256="a" * 64))
    text = pairing.qr_text(m)
    assert _decode(text, len(m)) == m
    assert len({len(l) for l in text.split("\n")}) == 1   # a clean rectangle


def test_main_prints_qr_and_payload(monkeypatch, capsys, cert):
    monkeypatch.setattr(pair, "read_env", lambda: {"SERVER_HTTPS": "true", "WHISPER_API_KEY": KEY})
    monkeypatch.setattr(pair.local_api, "CERT_PATH", cert)
    pair.main(["--host", "100.1.2.3", "--name", "box"])
    out = capsys.readouterr().out
    assert "█" in out
    assert "Certificate SHA-256: " + tls.fingerprint(cert) in out
    assert json.loads(out.strip().splitlines()[-1])["base_url"] == "https://100.1.2.3:5000/v1"


def test_read_env_parses_dotenv(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nWHISPER_API_KEY=abc\nSERVER_HTTPS=true\n# WHISPER_MODEL_SIZE=x\n")
    assert pair.read_env(str(p)) == {"WHISPER_API_KEY": "abc", "SERVER_HTTPS": "true"}
    assert pair.read_env(str(tmp_path / "missing")) == {}
