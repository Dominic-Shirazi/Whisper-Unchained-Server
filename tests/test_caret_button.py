"""Floating mic button: placement, fade timeline and countdown line, the
phone pictures, login/password heuristics, hotkey-press counting and the
one-key .env writer. Pure logic only (no window is created)."""
import os

import pytest

import caret_button as cb
import env_file
import hotkey_config
import text_focus as tf

WORK = (0, 0, 1920, 1040)


# --- placement -------------------------------------------------------------

def test_place_right_of_caret_at_the_default_spot():
    # Gap + 3/4 of the button right of the caret, 2/36 of it above centred:
    # where Dom dragged it to (old default + FLOATING_MIC_OFFSET="27,-2").
    caret = (500, 300, 501, 320)
    x, y = cb.place(caret, 36, WORK, gap=7)
    assert x == 501 + 7 + 27
    assert y == 310 - 18 - 2


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_default_spot_is_dpi_independent(scale):
    d = int(round(36 * scale))
    gap = max(4, d // 5)
    caret = (500, 300, 501, 320)
    x, y = cb.place(caret, d, WORK, gap=gap)
    assert x == pytest.approx(501 + gap + 27 * scale, abs=1)
    assert y == pytest.approx(310 - d / 2 - 2 * scale, abs=1)


def test_dom_legacy_offset_lands_on_the_same_pixel():
    """Old default (right beside the caret) + his saved "27,-2" == the new
    default + the translated saved offset, at every scale."""
    off = cb.saved_offset({"FLOATING_MIC_OFFSET": '"27,-2"'})
    assert off == (0, 0)
    for scale in (1.0, 1.5, 2.0):
        d = int(round(36 * scale))
        gap = max(4, d // 5)
        caret = (500, 300, 501, 320)
        old_x = 501 + gap + 27 * scale
        old_y = 310 - d / 2 - 2 * scale
        x, y = cb.place(caret, d, WORK, gap=gap, offset=cb.offset_px(off, scale))
        assert abs(x - old_x) <= 1 and abs(y - old_y) <= 1


def test_place_flips_left_near_right_edge():
    caret = (1900, 300, 1901, 320)
    x, y = cb.place(caret, 36, WORK, gap=7)
    assert x == 1900 - 7 - 27 - 36
    assert x + 36 <= WORK[2]


def test_place_clamped_to_work_area_top_and_bottom():
    assert cb.place((100, 0, 101, 10), 36, WORK, gap=7)[1] == 0
    assert cb.place((100, 1030, 101, 1050), 36, WORK, gap=7)[1] == 1040 - 36


def test_place_on_second_monitor_with_negative_coordinates():
    work = (-1920, 0, 0, 1040)
    x, y = cb.place((-10, 500, -9, 520), 36, work, gap=7)
    assert -1920 <= x <= -36
    assert x == -10 - 7 - 27 - 36


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


def test_timeline_hover_pauses_and_resumes():
    t = cb.FadeTimeline(full=2, faded=3)
    t.show(0)
    t.set_hover(True, 1)
    assert t.alpha(100) == 1.0                 # hovering: never fades
    assert t.progress(100) == pytest.approx(1 / 5)   # ...and the line waits
    t.set_hover(False, 100)
    assert t.progress(100) == pytest.approx(1 / 5)   # resumes where it was
    assert t.alpha(100.9) == 1.0
    assert t.alpha(102) == pytest.approx(0.35)
    assert t.progress(103.5) == pytest.approx(4.5 / 5)
    assert t.alpha(104.1) == 0.0               # 5 s of un-hovered time in all
    assert t.progress(104.1) is None


def test_timeline_progress_one_loop_in_the_lifetime():
    t = cb.FadeTimeline(full=2, faded=3)
    assert t.progress(0) is None
    t.show(10)
    assert t.progress(10) == 0.0
    assert t.progress(11) == pytest.approx(0.2)
    assert t.progress(12.5) == pytest.approx(0.5)
    assert t.progress(14.999) == pytest.approx(1.0, abs=1e-3)
    assert t.counting
    t.show(14)                                 # re-shown: starts over
    assert t.progress(14) == 0.0
    t.pin(True, 14)                            # recording: no countdown
    assert t.progress(20) is None and not t.counting
    t.pin(False, 20)
    assert t.progress(20) == 0.0
    t.drag(True, 21)
    assert t.progress(22) == 0.0 and not t.counting
    t.drag(False, 23)                          # dropped: starts over
    assert t.progress(24) == pytest.approx(0.2)


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
    data = cb.premultiplied_bgra(cb.compose(state, False, 36, 0.6))
    assert len(data) == side * side * 4
    # Premultiplied: no colour channel exceeds its alpha.
    for i in range(0, len(data), 4 * 37):
        b, g, r, a = data[i:i + 4]
        assert max(b, g, r) <= a
    # Centre pixel: opaque, or half see-through while working.
    want = 255 if state != "working" else 128
    assert img.getpixel((side // 2, side // 2))[3] == pytest.approx(want, abs=2)


def test_states_differ_by_shape_not_just_colour():
    """Red/green colour blind: idle, recording and working must look different
    even in greyscale."""
    grey = [cb.render(s, False, 36).convert("LA") for s in ("idle", "recording", "working")]
    assert len({g.tobytes() for g in grey}) == 3
    side = grey[0].size[0]
    # The tile itself (left of the first bar) is light idle, dark recording.
    m = cb.margin_for(36)
    spot = (m + 4, side // 2)
    assert grey[0].getpixel(spot)[0] > 200
    assert grey[1].getpixel(spot)[0] < 90


@pytest.mark.parametrize("state,src", [("idle", "idle"), ("recording", "recording"),
                                       ("working", "idle")])
def test_button_is_the_phone_picture(state, src):
    """The tile is the phone app's picture, scaled (working: at 50%)."""
    import numpy as np
    from PIL import Image
    d = 96
    m = cb.margin_for(d)
    pic = Image.open(cb.IMAGE_PATHS[src]).convert("RGBA").resize((d, d), Image.LANCZOS)
    want = np.asarray(pic).astype(int)
    got = np.asarray(cb.render(state, False, d).crop((m, m, m + d, m + d))).astype(int)
    inside = want[..., 3] > 250        # where the picture is opaque
    if state == "working":
        assert np.abs(got[..., 3][inside] - 128).max() <= 2
    else:
        assert np.abs(got[inside] - want[inside]).max() <= 3


def test_tile_is_a_rounded_square_not_a_circle():
    d = 200
    m = cb.margin_for(d)
    img = cb.render("idle", False, d)
    # Along the diagonal a circle filling the square ends 0.146 d in from the
    # corner; the rounded-square tile reaches further (to ~0.123 d).
    k = int(round(0.138 * d))
    assert img.getpixel((m + k, m + k))[3] == 255
    assert img.getpixel((m + 2, m + 2))[3] < 80          # but the corner is round
    # Straight top edge: opaque just inside it, a third of the way along.
    assert img.getpixel((m + d // 3, m + int(0.07 * d)))[3] == 255


def test_render_falls_back_without_pictures(monkeypatch):
    monkeypatch.setitem(cb._image_cache, "idle", None)
    monkeypatch.setitem(cb._image_cache, "recording", None)
    side = 36 + 2 * cb.margin_for(36)
    for s in ("idle", "recording", "working"):
        assert cb.render(s, False, 36).size == (side, side)
    rec = cb.render("recording", False, 36)
    assert rec.getpixel((side // 2, side // 2))[:3] == (255, 255, 255)
    assert cb.compose("idle", False, 36, 0.5).size == (side, side)


# --- countdown line ---------------------------------------------------------------

def test_path_starts_top_centre_and_runs_clockwise():
    import math
    box = (0, 0, 100, 60)
    pts = cb.rounded_rect_path(box, 10, 1.0, step=1)
    assert pts[0] == pytest.approx((50, 0))
    assert pts[-1] == pytest.approx((50, 0))                 # one full loop
    # Clockwise on screen (y down): right along the top, then down the right.
    quarter = cb.rounded_rect_path(box, 10, 0.25, step=1)
    assert quarter[1][0] > 50 and quarter[1][1] == pytest.approx(0)
    assert quarter[-1][0] == pytest.approx(100) and quarter[-1][1] > 10
    half = cb.rounded_rect_path(box, 10, 0.5, step=1)[-1]
    assert half == pytest.approx((50, 60), abs=0.5)          # bottom centre
    # Every point is on the outline's box and steps are short.
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        assert math.hypot(x1 - x0, y1 - y0) <= 1.01
    for x, y in pts:
        assert -1e-6 <= x <= 100 + 1e-6 and -1e-6 <= y <= 60 + 1e-6


def test_path_length_and_progress():
    import math
    assert cb.path_length((0, 0, 100, 60), 10) == pytest.approx(2 * 80 + 2 * 40 + 2 * math.pi * 10)
    assert cb.path_length((0, 0, 40, 40), 20) == pytest.approx(math.pi * 40)   # a circle
    assert cb.rounded_rect_path((0, 0, 100, 60), 10, 0.0) == [(50, 0)]


def _lit(img, x, y):
    return img.getpixel((int(x), int(y)))[3] > 128


def test_trail_grows_clockwise_from_top_centre():
    d = 72
    color = (140, 158, 255)
    (l, t, r, b), rad, w = cb.trail_shape("idle", d)
    cx, cy = (l + r) / 2, (t + b) / 2
    early = cb.render_trail(0.1, d, color)
    assert _lit(early, cx + 4, t)                  # just right of top centre
    assert not _lit(early, cx - 8, t)              # not left of it yet
    assert not _lit(early, l, cy)
    late = cb.render_trail(0.8, d, color)
    assert _lit(late, r, cy) and _lit(late, cx, b) and _lit(late, l, cy)
    assert not _lit(late, cx - 8, t)
    full = cb.render_trail(1.0, d, color)
    assert _lit(full, cx - 8, t)
    assert full.getpixel((int(r), int(cy)))[:3] == color
    assert cb.render_trail(0, d, color).getextrema()[3] == (0, 0)


def test_trail_clears_the_tile():
    """A gap between the tile and the line: the tile's edge stays unlit."""
    d = 72
    (l, t, r, b), rad = cb.tile_shape("idle", d)
    trail = cb.render_trail(1.0, d, (255, 0, 0))
    cx, cy = (l + r) / 2, (t + b) / 2
    for x, y in ((cx, t + 1), (r - 1, cy), (cx, b - 1), (l + 1, cy)):
        assert trail.getpixel((int(x), int(y)))[3] < 40


def test_trail_only_while_idle():
    for state in ("recording", "working"):
        assert cb.compose(state, False, 36, 0.5).tobytes() == cb.render(state, False, 36).tobytes()
    assert cb.compose("idle", False, 36, 0.5).tobytes() != cb.render("idle", False, 36).tobytes()


def test_accent_comes_from_theme(tmp_path):
    import theme
    want = tuple(int(theme.ACCENT.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    assert cb.accent_rgb() == want
    env = tmp_path / "a.env"
    env.write_text("UI_ACCENT=amber\n")
    want = theme.ACCENTS["amber"][0].lstrip("#")
    assert cb.accent_rgb(str(env)) == tuple(int(want[i:i + 2], 16) for i in (0, 2, 4))


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
    a = cb.offset_from_drop((501 + 7 + 27 + 30, 310 - 18 - 2 - 40), caret, 36, 7, False, 1.0)
    b = cb.offset_from_drop((501 + 14 + 54 + 60, 310 - 36 - 4 - 80), caret, 72, 14, False, 2.0)
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
                'FLOATING_MIC_SPOT="10,-20"\n')
    assert cb.read_config(envfile) == {"enabled": False, "hide_apps": ["slack.exe", "code.exe"],
                                       "offset": (10, -20)}


def test_legacy_offset_is_translated_and_spot_wins():
    # FLOATING_MIC_OFFSET was measured from the old default (right beside
    # the caret); the new default is 27 right, 2 up of that.
    assert cb.saved_offset({}) is None
    assert cb.saved_offset({"FLOATING_MIC_OFFSET": '"27,-2"'}) == (0, 0)
    assert cb.saved_offset({"FLOATING_MIC_OFFSET": "10,-20"}) == (-17, -18)
    assert cb.saved_offset({"FLOATING_MIC_OFFSET": "garbage"}) is None
    both = {"FLOATING_MIC_OFFSET": "27,-2", "FLOATING_MIC_SPOT": '"5,6"'}
    assert cb.saved_offset(both) == (5, 6)
    assert cb.OFFSET_KEY == "FLOATING_MIC_SPOT"   # a drag writes the new key


def test_legacy_offset_read_without_writing_env(envfile):
    with open(envfile, "w") as f:
        f.write('HOTKEY=`\nFLOATING_MIC_OFFSET="27,-2"\n')
    before = open(envfile, "rb").read()
    b = cb.CaretButton(lambda: None, env_path=envfile)
    b._read_enabled()
    assert b._offset == (0, 0)
    assert open(envfile, "rb").read() == before


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
    env_file.set_key(envfile, "FLOATING_MIC_SPOT", '"5,6"')
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
    assert cb.parse_offset(env_file.read(envfile)["FLOATING_MIC_SPOT"]) == (25, -60)
    assert "FLOATING_MIC_OFFSET" not in env_file.read(envfile)


class _Inspector:
    def __init__(self):
        self.calls = 0

    def inspect(self):
        self.calls += 1
        return None


@pytest.mark.parametrize("state", ["recording", "working"])
def test_caret_moves_ignored_while_busy(state, monkeypatch):
    """Recording / working: the button stays where it is, visible."""
    b = cb.CaretButton(lambda: None)
    b._inspector = _Inspector()
    shown, hidden = [], []
    monkeypatch.setattr(b, "_show_at", lambda *a: shown.append(a))
    monkeypatch.setattr(b, "_hide", lambda: hidden.append(1))
    b._state = state
    b.timeline.show(0)
    b.timeline.pin(True, 0)
    for reason in ("click", "focus"):
        b._check(reason, (10, 10))
    assert shown == [] and hidden == [] and b._inspector.calls == 0
    assert b.state == state and b.visible


def test_caret_followed_again_when_idle(monkeypatch):
    b = cb.CaretButton(lambda: None)
    b._inspector = _Inspector()
    monkeypatch.setattr(b, "_hide", lambda: None)
    monkeypatch.setattr(cb.user32, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(b, "_own_window", lambda h: False)
    monkeypatch.setattr(b, "_hidden_app", lambda h: False)
    b._state = "idle"
    b._check("click", (10, 10))
    assert b._inspector.calls == 1


def test_watcher_follows_changes(envfile):
    w = env_file.Watcher(envfile, "FLOATING_MIC_BUTTON", True)
    assert w.poll() is True
    env_file.set_key(envfile, "FLOATING_MIC_BUTTON", "false")
    st = os.stat(envfile)
    os.utime(envfile, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert w.poll() is False
