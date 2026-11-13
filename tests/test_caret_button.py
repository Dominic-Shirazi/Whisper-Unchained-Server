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


@pytest.mark.parametrize("d", [36, 54, 72])
def test_recording_badge_is_a_stop_square_working_badge_is_dots(d):
    bx, by = cb.badge_center(d)
    R = d * 0.25
    rec = cb.render("recording", False, d)
    work = cb.render("working", False, d)
    idle = cb.render("idle", False, d)
    # Both badges are white at their centre (square / middle dot)...
    assert min(rec.getpixel((int(bx), int(by)))[:3]) > 230
    assert min(work.getpixel((int(bx), int(by)))[:3]) > 230
    # ...but between the dots the working badge is dark, where the stop
    # square is still solid white.
    px = (int(round(bx + R * 0.22)), int(round(by)))
    assert min(rec.getpixel(px)[:3]) > 230
    assert max(work.getpixel(px)[:3]) < 160
    # Idle has no badge there.
    assert idle.getpixel((int(bx), int(by)))[:3] != (255, 255, 255)


def test_idle_is_the_logo():
    """Idle shows the app logo: dark waveform bars on the lavender tile."""
    img = cb.render("idle", False, 72)
    side = img.size[0]
    m = cb.margin_for(72)
    inside = [img.getpixel((x, side // 2)) for x in range(m + 8, side - m - 8)]
    assert any(max(p[:3]) < 70 for p in inside)                              # a bar
    assert any(p[:3] == pytest.approx(cb.LOGO_BG, abs=6) for p in inside)    # the tile
    # Round: the corner of the button's square is transparent.
    assert img.getpixel((m + 1, m + 1))[3] < 128


def test_render_falls_back_without_logo(monkeypatch):
    monkeypatch.setitem(cb._logo_cache, "img", None)
    side = 36 + 2 * cb.margin_for(36)
    for s in ("idle", "recording", "working"):
        assert cb.render(s, False, 36).size == (side, side)
    rec = cb.render("recording", False, 36)
    assert rec.getpixel((side // 2, side // 2))[:3] == (255, 255, 255)


# --- dragging -----------------------------------------------------------------

def test_place_with_offset_moves_relative_to_caret():
    caret = (500, 300, 501, 320)
    x0, y0 = cb.place(caret, 36, WORK, gap=7)
    assert cb.place(caret, 36, WORK, gap=7, offset=(20, -50)) == (x0 + 20, y0 - 50)
    # Next caret jump: same offset from the new caret.
    caret2 = (900, 600, 901, 620)
    x1, y1 = cb.place(caret2, 36, WORK, gap=7)
    assert cb.place(caret2, 36, WORK, gap=7, offset=(20, -50)) == (x1 + 20, y1 - 50)


def test_offset_mirrors_at_screen_edges_instead_of_covering_the_caret():
    # Dragged 40 px above the line; caret on the top line of the screen.
    x, y = cb.place((500, 5, 501, 25), 36, WORK, gap=7, offset=(0, -40))
    assert y >= 25                     # flipped below the line, not over it
    # Dragged to the right; caret near the right edge -> left of the caret.
    x, y = cb.place((1890, 300, 1891, 320), 36, WORK, gap=7, offset=(30, 0))
    assert x + 36 <= 1890


@pytest.mark.parametrize("offset", [(5000, 5000), (-5000, -5000), (5000, -5000), (0, 0)])
def test_offset_never_goes_off_screen(offset):
    for caret in ((500, 300, 501, 320), (0, 0, 1, 20), (1919, 1030, 1920, 1040)):
        for at_el in (False, True):
            x, y = cb.place(caret, 36, WORK, gap=7, at_element=at_el, offset=offset)
            assert 0 <= x and x + 36 <= 1920
            assert 0 <= y and y + 36 <= 1040


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("at_el", [False, True])
def test_drop_offset_round_trips(scale, at_el):
    d = int(36 * scale)
    gap = max(4, d // 5)
    anchor = (100, 280, 700, 330) if at_el else (400, 300, 401, 320)
    drop = (int(430 + 37 * scale), int(250 - 20 * scale))
    off = cb.offset_from_drop(drop, anchor, d, gap, at_el, scale)
    got = cb.place(anchor, d, WORK, gap, at_element=at_el, offset=cb.offset_px(off, scale))
    assert abs(got[0] - drop[0]) <= 1 and abs(got[1] - drop[1]) <= 1


def test_drop_offset_is_dpi_independent():
    caret = (500, 300, 501, 320)
    a = cb.offset_from_drop((501 + 7 + 30, 310 - 18 - 40), caret, 36, 7, False, 1.0)
    b = cb.offset_from_drop((501 + 14 + 60, 310 - 36 - 80), caret, 72, 14, False, 2.0)
    assert a == b == (30, -40)


def test_clamp_to_work():
    assert cb.clamp_to_work(-50, -50, 36, WORK) == (0, 0)
    assert cb.clamp_to_work(5000, 5000, 36, WORK) == (1920 - 36, 1040 - 36)
    assert cb.clamp_to_work(-1000, 10, 36, (-1920, 0, 0, 1040)) == (-1000, 10)


def test_small_press_movement_is_still_a_click():
    t = cb.DragTracker(threshold=cb.drag_threshold(1.0))
    t.press((100, 100), (90, 90))
    assert t.move((102, 103)) is None
    assert t.release((103, 102)) == ("click", None)
    assert not t.pressed


def test_drag_moves_window_and_reports_drop():
    t = cb.DragTracker(threshold=4)
    t.press((100, 100), (90, 90))
    assert t.move((110, 100)) == (100, 90)
    assert t.dragging
    assert t.move((101, 101)) == (91, 91)     # once dragging, small moves count too
    assert t.release((130, 80)) == ("drag", (120, 70))
    assert not t.pressed and not t.dragging


def test_drag_is_clamped():
    t = cb.DragTracker(threshold=4)
    t.press((100, 100), (90, 90))

    def clamp(p):
        return max(0, p[0]), max(0, p[1])
    assert t.move((0, 0), clamp) == (0, 0)
    assert t.release(None) == ("drag", (0, 0))


def test_drag_threshold_scales_with_dpi():
    assert cb.drag_threshold(1.0) == 4
    assert cb.drag_threshold(1.5) == 6
    assert cb.drag_threshold(0.5) == 4


def test_release_without_press_is_nothing():
    assert cb.DragTracker().release((1, 1)) == (None, None)


def test_timeline_held_while_dragging_then_restarts():
    t = cb.FadeTimeline(full=2, faded=3)
    t.show(0)
    t.drag(True, 1)
    assert t.alpha(30) == 1.0                  # no fade mid-drag
    t.drag(False, 30)
    assert t.alpha(31.9) == 1.0                # countdown restarted on drop
    assert t.alpha(33) == pytest.approx(0.35)
    assert t.alpha(35.1) == 0.0


# --- .env: hide list and offset ------------------------------------------------

def test_parse_app_list_normalizes_like_app_styles():
    import app_styles
    raw = r' "Slack.exe, C:\Program Files\Code.exe ,chrome,,slack" '
    assert cb.parse_app_list(raw) == ["slack.exe", "code.exe", "chrome.exe"]
    for name in ("Slack", "SLACK.EXE", r"C:\x\Slack.exe", ""):
        assert cb._normalize_app(name) == app_styles.normalize_app(name)
    assert cb.parse_app_list("") == []
    assert cb.parse_app_list(None) == []
    assert cb.parse_app_list("slack.exe # chat") == ["slack.exe"]


def test_add_app():
    assert cb.add_app("", "Slack.exe") == "slack.exe"
    assert cb.add_app("slack.exe", "code.exe") == "slack.exe,code.exe"
    assert cb.add_app("slack.exe,code.exe", "SLACK") == "slack.exe,code.exe"
    assert cb.add_app(None, "my app.exe") == '"my app.exe"'


def test_parse_offset():
    assert cb.parse_offset('"12,-30"') == (12, -30)
    assert cb.parse_offset("12, -30") == (12, -30)
    assert cb.parse_offset("1.6,2") == (2, 2)
    for bad in ("", None, "12", "a,b", "1,2,3", "99999,0"):
        assert cb.parse_offset(bad) is None
    assert cb.parse_offset(cb.format_offset((-7, 40))) == (-7, 40)


def test_app_label():
    assert cb.app_label(r"C:\x\slack.exe", "Slack") == "Slack"
    assert cb.app_label(r"C:\x\chrome.exe", "Google Chrome") == "Google Chrome"
    assert cb.app_label(r"C:\x\notepad.exe", "") == "Notepad"
    assert cb.app_label(r"C:\x\foo.exe", "x" * 80) == "Foo"


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


def test_read_config(envfile):
    assert cb.read_config(envfile) == {"enabled": True, "hide_apps": [], "offset": None}
    with open(envfile, "w") as f:
        f.write('FLOATING_MIC_BUTTON=false\nFLOATING_MIC_HIDE_APPS=Slack.exe,code\n'
                'FLOATING_MIC_OFFSET="10,-20"\n')
    assert cb.read_config(envfile) == {"enabled": False, "hide_apps": ["slack.exe", "code.exe"],
                                       "offset": (10, -20)}


def _bump(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


def test_button_rereads_env_when_it_changes(envfile):
    b = cb.CaretButton(lambda: None, env_path=envfile)
    assert b._read_enabled() is True and b._hide_apps == []
    env_file.set_key(envfile, "FLOATING_MIC_HIDE_APPS", "slack.exe")
    _bump(envfile)
    b._read_enabled()
    assert b._hide_apps == ["slack.exe"]
    env_file.set_key(envfile, "FLOATING_MIC_OFFSET", '"5,6"')
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "false")
    _bump(envfile)
    assert b._read_enabled() is False
    assert b._offset == (5, 6)


def test_hide_in_app_writes_env_and_matches(envfile, monkeypatch):
    with open(envfile, "w") as f:
        f.write("HOTKEY=`\nFLOATING_MIC_HIDE_APPS=code.exe\n")
    b = cb.CaretButton(lambda: None, env_path=envfile)
    b._read_enabled()
    b._hide_in("slack.exe")
    assert env_file.read(envfile) == {"HOTKEY": "`", "FLOATING_MIC_HIDE_APPS": "code.exe,slack.exe"}
    assert b._hide_apps == ["code.exe", "slack.exe"]
    b._hide_in("slack.exe")                    # no duplicates
    assert env_file.read(envfile)["FLOATING_MIC_HIDE_APPS"] == "code.exe,slack.exe"

    paths = {1: r"C:\Apps\Slack.exe", 2: r"C:\Windows\notepad.exe"}
    monkeypatch.setattr(cb, "_window_exe_path", lambda h: paths.get(h, ""))
    assert b._hidden_app(1) is True
    assert b._hidden_app(2) is False
    assert b._hidden_app(None) is False


def test_dropped_remembers_offset_and_saves_it(envfile):
    b = cb.CaretButton(lambda: None, env_path=envfile)
    b._size = 36
    caret = (500, 300, 501, 320)
    b._placed = (caret, False, WORK, 1.0)
    m = cb.margin_for(36)
    x0, y0 = cb.place(caret, 36, WORK, gap=7)
    b.timeline.show(0)
    b._dropped((x0 - m + 25, y0 - m - 60))
    assert b._offset == (25, -60)
    assert cb.parse_offset(env_file.read(envfile)["FLOATING_MIC_OFFSET"]) == (25, -60)


def test_watcher_follows_changes(envfile):
    w = env_file.Watcher(envfile, "FLOATING_MIC_BUTTON", True)
    assert w.poll() is True
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "false")
    st = os.stat(envfile)
    os.utime(envfile, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert w.poll() is False
