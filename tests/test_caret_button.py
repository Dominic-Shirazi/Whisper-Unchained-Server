"""Floating mic button: placement, fade timeline, login/password heuristics,
hotkey-press counting and the one-key .env writer. Pure logic only."""
import os

import pytest

import caret_button as cb
import env_file
import hotkey_config
import text_focus as tf

WORK = (0, 0, 1920, 1040)


# --- placement -------------------------------------------------------------

def test_place_right_of_caret_vertically_centred():
    caret = (500, 300, 501, 320)
    x, y = cb.place(caret, 36, WORK, gap=7)
    assert x == 508
    assert y == 310 - 18


def test_place_flips_left_near_right_edge():
    caret = (1900, 300, 1901, 320)
    x, y = cb.place(caret, 36, WORK, gap=7)
    assert x == 1900 - 7 - 36
    assert x + 36 <= WORK[2]


def test_place_clamped_to_work_area_top_and_bottom():
    assert cb.place((100, 0, 101, 10), 36, WORK, gap=7)[1] == 0
    assert cb.place((100, 1030, 101, 1050), 36, WORK, gap=7)[1] == 1040 - 36


def test_place_on_second_monitor_with_negative_coordinates():
    work = (-1920, 0, 0, 1040)
    x, y = cb.place((-10, 500, -9, 520), 36, work, gap=7)
    assert -1920 <= x <= -36
    assert x == -10 - 7 - 36


def test_place_element_fallback_inside_right_edge():
    field = (100, 100, 600, 134)
    x, y = cb.place(field, 36, WORK, gap=7, at_element=True)
    assert x == 600 - 36 - 7
    assert y == 117 - 18
    # Tall field (text area): near its top, not the middle.
    x, y = cb.place((100, 100, 600, 700), 36, WORK, gap=7, at_element=True)
    assert y == 107


# --- fade timeline ---------------------------------------------------------

def test_timeline_full_then_faded_then_hidden():
    t = cb.FadeTimeline(full=2, faded=3, faded_alpha=0.35, ramp=0.25)
    assert t.alpha(0) == 0.0 and not t.visible
    t.show(10)
    assert t.alpha(10) == 1.0
    assert t.alpha(11.9) == 1.0
    assert 0.35 < t.alpha(12.1) < 1.0          # ramping down
    assert t.alpha(13) == pytest.approx(0.35)
    assert t.alpha(14.9) == pytest.approx(0.35)
    assert t.alpha(15.1) == 0.0
    assert not t.visible


def test_timeline_hover_holds_and_resets():
    t = cb.FadeTimeline(full=2, faded=3)
    t.show(0)
    t.set_hover(True, 1)
    assert t.alpha(100) == 1.0                 # hovering: never fades
    t.set_hover(False, 100)
    assert t.alpha(101.9) == 1.0               # countdown restarted on leave
    assert t.alpha(103) == pytest.approx(0.35)
    assert t.alpha(105.1) == 0.0


def test_timeline_pinned_while_recording_then_restarts():
    t = cb.FadeTimeline(full=2, faded=3)
    t.show(0)
    t.pin(True, 0)
    assert t.alpha(60) == 1.0
    t.pin(False, 60)
    assert t.alpha(61) == 1.0
    assert t.alpha(66) == 0.0


def test_timeline_hide():
    t = cb.FadeTimeline()
    t.show(0)
    t.hide()
    assert t.alpha(0.1) == 0.0


# --- drawing -----------------------------------------------------------------

@pytest.mark.parametrize("state", ["idle", "recording", "working"])
def test_render_size_and_premultiplied(state):
    img = cb.render(state, False, 36)
    side = 36 + 2 * cb.margin_for(36)
    assert img.size == (side, side)
    data = cb.premultiplied_bgra(img)
    assert len(data) == side * side * 4
    # Premultiplied: no colour channel exceeds its alpha.
    for i in range(0, len(data), 4 * 37):
        b, g, r, a = data[i:i + 4]
        assert max(b, g, r) <= a
    # Centre pixel is opaque.
    assert img.getpixel((side // 2, side // 2))[3] == 255


def test_states_differ_by_shape_not_just_colour():
    """Red/green colour blind: idle, recording and working must look different
    even in greyscale."""
    grey = [cb.render(s, False, 36).convert("LA").tobytes() for s in ("idle", "recording", "working")]
    assert len(set(grey)) == 3
    rec = cb.render("recording", False, 36)
    side = rec.size[0]
    # The stop square is white at the centre; the dots are grey between them.
    assert rec.getpixel((side // 2, side // 2))[:3] == (255, 255, 255)


# --- password / login heuristics ----------------------------------------------

@pytest.mark.parametrize("name,aid,help_text", [
    ("Username", "", ""),
    ("User name", "", ""),
    ("Email or phone", "", ""),
    ("Enter your email, phone, or Skype.", "", ""),
    ("", "loginfmt", ""),
    ("", "login_field", ""),
    ("", "identifierId", ""),
    ("Email address", "", ""),
    ("Email", "", ""),
    ("Sign in", "", ""),
    ("", "", "Enter your user ID"),
    ("Account name", "", ""),
    ("Password", "", ""),
    ("User ID:", "", ""),
])
def test_login_fields_detected(name, aid, help_text):
    assert tf.looks_like_login(name, aid, help_text)


@pytest.mark.parametrize("name,aid", [
    ("", ""),
    ("Message", ""),
    ("Search users", ""),
    ("Email body", ""),
    ("Subject", "subjectbox"),
    ("Type a message", "chat-input"),
    ("Notes", ""),
    ("Write a comment", ""),
])
def test_ordinary_fields_not_login(name, aid):
    assert not tf.looks_like_login(name, aid)


def test_decide_password_never():
    assert tf.decide(tf.CT_EDIT, is_password=True) == (False, "password")
    # Win32 ES_PASSWORD on a plain Edit even when UIA doesn't say so.
    ok, why = tf.decide(tf.CT_EDIT, win32_class="Edit", win32_style=tf.ES_PASSWORD)
    assert (ok, why) == (False, "password")


def test_decide_login_and_readonly():
    assert tf.decide(tf.CT_EDIT, login=True) == (False, "login")
    assert tf.decide(tf.CT_EDIT, read_only=True)[0] is False
    assert tf.decide(tf.CT_EDIT, win32_class="Edit", win32_style=tf.ES_READONLY)[0] is False


def test_decide_editable_kinds():
    assert tf.decide(tf.CT_EDIT)[0]
    assert tf.decide(tf.CT_EDIT, read_only=False)[0]
    assert tf.decide(tf.CT_DOCUMENT, read_only=False)[0]
    assert not tf.decide(tf.CT_DOCUMENT)[0]                # a web page
    assert tf.decide(tf.CT_DOCUMENT, has_win32_caret=True)[0]
    assert tf.decide(50026, read_only=False, has_text=True)[0]  # contenteditable group
    assert not tf.decide(50000)[0]                         # a button
    assert not tf.decide(tf.CT_EDIT, enabled=False)[0]


def test_caret_inside():
    assert tf.caret_inside((110, 110, 111, 125), (100, 100, 400, 140))
    assert not tf.caret_inside((900, 900, 901, 915), (100, 100, 400, 140))
    assert tf.caret_inside((900, 900, 901, 915), None)


# --- hotkey press counting ------------------------------------------------------

@pytest.mark.parametrize("start,stop,expected", [
    ("hotkey", "hotkey", 2),
    ("button", "hotkey", 1),
    ("hotkey", "button", 1),
    ("button", "button", 0),
    ("hotkey", "timeout", 1),
    ("button", "timeout", 0),
])
def test_backtick_backspaces_only_real_presses(start, stop, expected):
    t = hotkey_config.PressTally("`", "toggle")
    t.started(start)
    assert t.stopped(stop) == expected


def test_matches_old_cleanup_for_hotkey_only():
    for hk, mode in (("`", "toggle"), ("f9", "toggle"), ("f9", "hold"), ("ctrl+alt+space", "toggle")):
        t = hotkey_config.PressTally(hk, mode)
        t.started("hotkey")
        assert t.stopped("hotkey") == hotkey_config.cleanup_backspaces(hk, mode)


def test_non_typing_hotkey_never_backspaces():
    t = hotkey_config.PressTally("f9", "toggle")
    t.started("hotkey")
    assert t.stopped("hotkey") == 0


def test_tally_resets_between_recordings():
    t = hotkey_config.PressTally("`", "toggle")
    t.started("hotkey")
    t.stopped("hotkey")
    t.started("button")
    assert t.stopped("button") == 0


# --- .env one-key writer ----------------------------------------------------------

@pytest.fixture
def envfile(tmp_path):
    return str(tmp_path / "test.env")


def test_set_key_changes_only_that_line(envfile):
    original = (b"# comment\r\nHOTKEY=`\r\n\r\n# FLOATING_MIC_BUTTON=true\r\n"
                b"FLOATING_MIC_BUTTON=true\r\nOTHER = x # note\r\n")
    with open(envfile, "wb") as f:
        f.write(original)
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "false")
    with open(envfile, "rb") as f:
        got = f.read()
    assert got == original.replace(b"\r\nFLOATING_MIC_BUTTON=true", b"\r\nFLOATING_MIC_BUTTON=false")
    assert b"# FLOATING_MIC_BUTTON=true" in got           # commented fallback untouched
    assert not os.path.exists(envfile + ".tmp")


def test_set_key_appends_when_missing_keeping_line_endings(envfile):
    with open(envfile, "wb") as f:
        f.write(b"A=1\nB=2")                               # LF, no final newline
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "false")
    with open(envfile, "rb") as f:
        assert f.read() == b"A=1\nB=2\nFLOATING_MIC_BUTTON=false\n"


def test_set_key_creates_missing_file(envfile):
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "true")
    assert env_file.read(envfile) == {"FLOATING_MIC_BUTTON": "true"}


def test_set_key_no_change_no_write(envfile):
    with open(envfile, "wb") as f:
        f.write(b"FLOATING_MIC_BUTTON=true\n")
    before = os.stat(envfile).st_mtime_ns
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "true")
    assert os.stat(envfile).st_mtime_ns == before


def test_get_bool_default_on(envfile):
    assert env_file.get_bool(envfile, "FLOATING_MIC_BUTTON", True) is True
    with open(envfile, "w") as f:
        f.write("FLOATING_MIC_BUTTON=\n")
    assert env_file.get_bool(envfile, "FLOATING_MIC_BUTTON", True) is True
    for v, want in (("false", False), ("0", False), ("off", False), ("TRUE", True), ("yes", True)):
        with open(envfile, "w") as f:
            f.write("FLOATING_MIC_BUTTON=%s\n" % v)
        assert env_file.get_bool(envfile, "FLOATING_MIC_BUTTON", True) is want


def test_watcher_follows_changes(envfile):
    w = env_file.Watcher(envfile, "FLOATING_MIC_BUTTON", True)
    assert w.poll() is True
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "false")
    st = os.stat(envfile)
    os.utime(envfile, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert w.poll() is False
