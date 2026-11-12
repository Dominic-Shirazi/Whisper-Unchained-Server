import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import pytest  # noqa: E402

# The `keyboard` library is only installed on Windows (requirements.txt).
keyboard = pytest.importorskip("keyboard", reason="Windows-only hotkey library")
import hotkey_config as hc  # noqa: E402


def test_defaults_are_todays_behavior():
    assert hc.from_env({}) == ("`", "toggle")
    assert hc.cleanup_backspaces("`", "toggle") == 2


def test_non_typing_keys_need_no_cleanup():
    for k in ("f9", "pause", "ctrl+alt+space", "scroll lock"):
        assert hc.cleanup_backspaces(k, "toggle") == 0
        assert hc.cleanup_backspaces(k, "hold") == 0


def test_hold_with_typing_key_rejected_and_downgraded():
    err, _ = hc.validate("`", "hold", keyboard.parse_hotkey)
    assert err and "Hold to talk" in err
    # A hand-edited .env can't make the listener auto-repeat backticks.
    assert hc.from_env({"HOTKEY": "`", "HOTKEY_MODE": "hold"}) == ("`", "toggle")


def test_every_preset_parses_and_validates():
    for key, _label in hc.PRESETS:
        assert hc.validate(key, "toggle", keyboard.parse_hotkey)[0] is None, key


def test_bad_key_name_rejected():
    assert hc.validate("ctrl+notakey", "toggle", keyboard.parse_hotkey)[0]
    assert hc.validate("", "toggle")[0]
    assert hc.validate("f9", "sometimes")[0]


def test_conflicts_warn_but_allow():
    err, warn = hc.validate("Alt + Space", "toggle", keyboard.parse_hotkey)
    assert err is None and "window menu" in warn


def test_release_key_is_last_key():
    assert hc.release_key("Ctrl + Alt + Space") == "space"
    assert hc.release_key("f9") == "f9"
