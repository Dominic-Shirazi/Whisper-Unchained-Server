"""Phone pairing helpers in the Settings window
(contract: WhisperRemote contracts/01-transcription-api.md §7)."""
import json

import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import settings_gui as sg  # noqa: E402


def test_payload_with_key():
    d = json.loads(sg.pairing_payload("100.1.2.3", "wr_sec_abc123def456", "large-v3-turbo", "PC"))
    assert d == {"v": 1, "base_url": "http://100.1.2.3:5000/v1",
                 "model": "large-v3-turbo", "name": "PC", "api_key": "wr_sec_abc123def456"}


def test_payload_without_key_omits_field():
    d = json.loads(sg.pairing_payload("100.1.2.3", "", "m", "PC"))
    assert "api_key" not in d
    assert d["base_url"].endswith("/v1")


def test_generated_key_format_and_unique():
    a, b = sg.generate_api_key(), sg.generate_api_key()
    assert a.startswith("wr_sec_") and len(a) == len("wr_sec_") + 32
    assert a != b
    # A generated key must pass the field's own validation.
    assert sg.Field("WHISPER_API_KEY", "API key", "secret").coerce(a) == (a, None)


def test_secret_field_validation():
    f = sg.Field("WHISPER_API_KEY", "API key", "secret")
    assert f.coerce("") == ("", None)                 # empty = auth off
    assert f.coerce("  ") == ("", None)
    assert f.coerce("short")[1]                      # too short
    assert f.coerce("has spaces in it!!")[1]          # bad characters
    assert f.coerce("wr_sec_bad=key#123")[1]          # '=' / '#' would corrupt .env


def test_qr_matrix_square():
    m = sg.qr_matrix(sg.pairing_payload("100.1.2.3", sg.generate_api_key(), "large-v3-turbo", "PC"))
    assert m and len(m) == len(m[0])


def test_pairing_code_carries_the_other_addresses():
    import pairing
    d = json.loads(sg.pairing_payload("100.89.1.2", "", "m", "PC", port=5001, cert_sha256="ab" * 32,
                                      alt_hosts=["192.168.0.2", "100.89.1.2", "127.0.0.1", ""]))
    assert d["base_url"] == "https://100.89.1.2:5001/v1"
    assert d["alt_base_urls"] == ["https://192.168.0.2:5001/v1"]      # no repeats, no loopback
    d = json.loads(sg.pairing_payload("100.89.1.2", "", "m", "PC"))
    assert "alt_base_urls" not in d                                   # left out when there are none
    many = json.loads(sg.pairing_payload("a", "", "m", "PC", alt_hosts=["b", "c", "d"]))
    assert many["alt_base_urls"] == ["http://b:5000/v1", "http://c:5000/v1"]
    # Whichever address is chosen, the other kind is added.
    assert pairing.other_hosts("100.89.1.2", tailscale=["100.89.1.2"], home="192.168.0.2") == ["192.168.0.2"]
    assert pairing.other_hosts("192.168.0.2", tailscale=["100.89.1.2"], home="192.168.0.2") == ["100.89.1.2"]
    assert pairing.other_hosts("192.168.0.2", tailscale=[], home="192.168.0.2") == []
    assert pairing.describe_other_hosts([]) == ""
    assert "192.168.0.2 (home network)" in pairing.describe_other_hosts(["192.168.0.2"])
    assert "100.89.1.2 (Tailscale)" in pairing.describe_other_hosts(["100.89.1.2"])


def test_detect_hosts_never_empty():
    hosts = sg.detect_hosts()
    assert hosts and all(isinstance(h, str) for h in hosts)


def test_tailscale_addresses_are_recognised():
    import pairing
    assert pairing.is_tailscale_ip("100.89.163.64")
    assert pairing.is_tailscale_ip("100.64.0.1") and pairing.is_tailscale_ip("100.127.255.254")
    assert not pairing.is_tailscale_ip("100.128.0.1")      # outside 100.64.0.0/10
    assert not pairing.is_tailscale_ip("192.168.0.2")
    assert not pairing.is_tailscale_ip("not an ip")


def test_tailscale_address_found_even_when_the_command_is_slow(monkeypatch):
    """The pairing code must never fall back to the home address just because
    'tailscale ip' was slow: the adapters are read first."""
    import pairing
    import subprocess
    monkeypatch.setattr(pairing, "_tailscale_adapter_ips", lambda: ["100.89.1.2"])

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("tailscale", 5)
    monkeypatch.setattr(pairing.subprocess, "run", slow)
    monkeypatch.setattr(pairing, "lan_ips", lambda: ["192.168.0.2"])
    assert pairing.detect_hosts() == ["100.89.1.2", "192.168.0.2"]


# --- the connection code (the same payload, as one line to paste) ---------------------

def _payload(**kw):
    import pairing
    args = dict(host="100.89.1.2", api_key="wr_sec_abc123def456", model="large-v3-turbo",
                name="Head-PC", port=5001, cert_sha256="ab" * 32, alt_hosts=["192.168.0.2"])
    args.update(kw)
    return pairing.pairing_payload(**args)


def test_connection_code_round_trip():
    import pairing
    code = pairing.connection_code(_payload())
    assert code.startswith("WU1-") and " " not in code and "\n" not in code and "=" not in code
    got = pairing.parse_connection_code("  " + code[:40] + "\n " + code[40:] + "  ")   # wrapped by an email
    assert got == {"urls": ["https://100.89.1.2:5001/v1", "https://192.168.0.2:5001/v1"],
                   "api_key": "wr_sec_abc123def456", "cert_sha256": "ab" * 32,
                   "name": "Head-PC", "model": "large-v3-turbo"}
    assert pairing.parse_connection_code(_payload())["urls"][0] == "https://100.89.1.2:5001/v1"  # bare JSON too


def test_connection_code_without_key_or_https():
    import pairing
    got = pairing.parse_connection_code(pairing.connection_code(
        _payload(api_key="", cert_sha256="", port=5000, alt_hosts=())))
    assert got["urls"] == ["http://100.89.1.2:5000/v1"] and got["api_key"] == "" and got["cert_sha256"] == ""


@pytest.mark.parametrize("bad, says", [
    ("", "Paste the connection code"),
    ("hello there", "isn't a connection code"),
    ("WU1-not_base64!!", "isn't a connection code"),
    ("WU1-" + "e30", "newer version"),                       # {} : no version
])
def test_connection_code_refuses_junk(bad, says):
    import pairing
    with pytest.raises(ValueError, match=says):
        pairing.parse_connection_code(bad)


def test_connection_code_needs_the_fingerprint_for_https():
    import json as _json
    import pairing
    d = _json.loads(_payload())
    del d["cert_sha256"]
    with pytest.raises(ValueError, match="security fingerprint"):
        pairing.parse_connection_code(pairing.connection_code(_json.dumps(d)))
    d = _json.loads(_payload())
    d["base_url"] = "ftp://x/v1"
    with pytest.raises(ValueError, match="no usable address"):
        pairing.parse_connection_code(pairing.connection_code(_json.dumps(d)))


def test_remote_settings_put_the_reachable_address_first():
    import pairing
    parsed = pairing.parse_connection_code(pairing.connection_code(_payload()))
    env = pairing.remote_settings(parsed)
    assert env == {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": "https://100.89.1.2:5001/v1",
                   "REMOTE_ALT_URLS": "https://192.168.0.2:5001/v1",
                   "REMOTE_API_KEY": "wr_sec_abc123def456", "REMOTE_CERT_SHA256": "ab" * 32}
    env = pairing.remote_settings(parsed, reachable=lambda u: "192.168" in u)
    assert env["REMOTE_SERVER_URL"] == "https://192.168.0.2:5001/v1"
    assert env["REMOTE_ALT_URLS"] == "https://100.89.1.2:5001/v1"
    env = pairing.remote_settings(parsed, reachable=lambda u: 1 / 0)     # a check that blows up
    assert env["REMOTE_SERVER_URL"] == "https://100.89.1.2:5001/v1"
