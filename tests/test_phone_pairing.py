"""Phone pairing helpers in the Settings window
(contract: WhisperRemote contracts/01-transcription-api.md §7)."""
import json
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


def test_detect_hosts_never_empty():
    hosts = sg.detect_hosts()
    assert hosts and all(isinstance(h, str) for h in hosts)
