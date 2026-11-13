"""The recording style pill and sheet (client/style_pill.py), and how the
floating button and the listener use them."""
import pytest

import style_pill as sp
import caret_button as cb


# --- gestures ---------------------------------------------------------------------

@pytest.mark.parametrize("dx, dy, want", [
    (-30, 0, "more"),     # finger moves left: the next option comes in (carousel, F-8)
    (30, 0, "less"),      # finger moves right: the previous one
    (0, -30, "next"),     # up: next style
    (0, 30, "prev"),      # down: previous style
    (-25, 20, "more"),    # mostly sideways counts as sideways
    (10, -26, "next"),
    (-24, -24, "more"),   # a tie goes to sideways, like the phone (abs(dx) >= abs(dy))
    (23, 0, None),        # below 24 px: not a swipe yet
    (0, -23, None),
    (0, 0, None),
])
def test_classify_swipe(dx, dy, want):
    assert sp.classify_swipe(dx, dy, 24) == want


def test_swipe_threshold_follows_dpi():
    assert sp.swipe_threshold(1.0) == 24
    assert sp.swipe_threshold(1.5) == 36
    assert sp.swipe_threshold(2.0) == 48


def test_press_small_wobble_is_a_tap():
    t = sp.PressTracker(24)
    t.press((100, 100))
    assert t.move((110, 95)) is None
    assert t.release((112, 104)) == "tap"
    assert not t.pressed


def test_press_swipe_fires_once_and_is_not_a_tap():
    t = sp.PressTracker(24)
    t.press((100, 100))
    assert t.move((90, 100)) is None
    assert t.move((70, 102)) == "more"
    assert t.move((10, 100)) is None          # one step per swipe
    assert t.move((70, 300)) is None
    assert t.release((0, 0)) is None          # a swipe never opens the sheet


def test_swipe_decided_on_release_if_no_move_came():
    t = sp.PressTracker(24)
    t.press((0, 0))
    assert t.release((0, -40)) is None        # it was a swipe (up), not a tap


def test_release_without_press():
    assert sp.PressTracker().release((1, 1)) is None


def test_wheel_notches_and_touchpad_deltas():
    w = sp.WheelAccumulator()
    assert w.add(-120) == ["next"]            # wheel down: next style
    assert w.add(120) == ["prev"]
    assert w.add(40) == [] and w.add(40) == [] and w.add(40) == ["prev"]
    assert w.add(240, horizontal=True) == ["more", "more"]   # tilt right: do more
    assert w.add(-120, horizontal=True) == ["less"]


# --- values ----------------------------------------------------------------------------

def test_rows_match_the_phone():
    assert [d[1] for d in sp.DIMENSIONS] == ["Grammar & flow", "Conciseness", "Punctuation"]
    assert [l for _v, l in sp.DIMENSIONS[0][2]] == ["Keep my words", "Fix grammar", "Polish"]
    assert [l for _v, l in sp.DIMENSIONS[1][2]] == ["Full length", "Shorter", "Shortest"]
    assert [v for v, _l in sp.DIMENSIONS[2][2]] == ["minimal", "light", "standard"]   # less -> more
    assert [l for _v, l in sp.TONES] == ["My own tone", "Professional", "Casual", "Friendly"]


def test_step_moves_one_and_stops_at_the_ends():
    s = sp.StyleSession(sp.DEFAULTS)
    assert s.content() == ("Grammar & flow", "Keep my words", 0, 3)
    assert s.step(-1) is False and not s.modified        # already the least
    assert s.step(1) is True and s.values["rewrite"] == "fix" and s.modified
    assert s.step(1) is True and s.values["rewrite"] == "polish"
    assert s.step(1) is False and s.values["rewrite"] == "polish"
    assert s.content()[1:] == ("Polish", 2, 3)


def test_cycle_wraps_and_is_not_a_change():
    s = sp.StyleSession(sp.DEFAULTS)
    names = []
    for _ in range(4):
        s.cycle(1)
        names.append(s.content()[0])
    assert names == ["Conciseness", "Punctuation", "Grammar & flow", "Conciseness"]
    s.cycle(-1)
    s.cycle(-1)
    assert s.content()[0] == "Punctuation"
    assert not s.modified


def test_step_acts_on_the_style_shown():
    s = sp.StyleSession(sp.DEFAULTS)
    s.cycle(1)                                            # Conciseness
    s.step(1)
    assert s.values["shorten"] == "light" and s.values["rewrite"] == "keep"
    s.cycle(1)                                            # Punctuation, starts at Standard (most)
    assert s.step(1) is False
    assert s.step(-1) and s.values["punctuation"] == "light"


def test_unknown_value_shows_as_the_first():
    s = sp.StyleSession(dict(sp.DEFAULTS, rewrite="weird"))
    assert s.content()[2] == 0


def test_tone_moves_keep_to_fix_with_a_hint():
    s = sp.StyleSession(sp.DEFAULTS)
    assert s.pick("tone", "casual", now=100.0)
    assert s.values["rewrite"] == "fix" and s.values["tone"] == "casual"
    assert s.hint_text(now=101.0) == sp.TONE_HINT
    assert s.hint_text(now=100.0 + sp.HINT_SECS + 0.1) == ""


def test_tone_keeps_polish_and_own_tone_changes_nothing_else():
    s = sp.StyleSession(dict(sp.DEFAULTS, rewrite="polish"))
    s.pick("tone", "professional")
    assert s.values["rewrite"] == "polish" and not s.hint
    s2 = sp.StyleSession(sp.DEFAULTS)
    s2.pick("tone", "neutral")
    assert s2.values["rewrite"] == "keep" and s2.modified and not s2.hint


def test_mean_is_independent_and_only_where_available():
    s = sp.StyleSession(sp.DEFAULTS, mean_available=True)
    assert s.pick("tone", "mean")
    assert s.values["rewrite"] == "keep"
    assert [v for v, _l in s.tones()][-1] == "mean"
    no = sp.StyleSession(sp.DEFAULTS, mean_available=False)
    assert no.pick("tone", "mean") is False and not no.modified
    assert "mean" not in [v for v, _l in no.tones()]


def test_pick_rejects_unknown_and_closed():
    s = sp.StyleSession(sp.DEFAULTS)
    assert s.pick("tone", "angry") is False
    assert s.pick("colour", "red") is False
    s.close()
    assert s.pick("rewrite", "fix") is False and s.step(1) is False
    assert s.values["rewrite"] == "keep"


# --- what gets sent ------------------------------------------------------------------------

def test_untouched_sends_exactly_the_app_style():
    s = sp.StyleSession(sp.DEFAULTS)
    s.cycle(1)                                  # only looked at another style
    base = {"cleanup": "on", "cleanup_tone": "casual"}
    assert s.request_fields(base) == base
    assert s.request_fields() == {}


def test_touched_sends_clean_with_every_option():
    s = sp.StyleSession(dict(sp.DEFAULTS, emoji="on"))
    s.step(1)                                   # Fix grammar
    s.pick("tone", "friendly")
    s.pick("shorten", "strong")
    assert s.request_fields({"cleanup": "off"}) == {
        "processing": "clean", "cleanup_punctuation": "standard", "cleanup_rewrite": "fix",
        "cleanup_tone": "friendly", "cleanup_shorten": "strong", "cleanup_emoji": "on"}


def test_keep_my_words_sends_own_tone():
    s = sp.StyleSession(dict(sp.DEFAULTS, tone="professional"))
    s.pick("punctuation", "minimal")
    f = s.request_fields()
    assert f["cleanup_rewrite"] == "keep" and f["cleanup_tone"] == "neutral"


def test_repicking_the_current_chip_counts_as_a_choice():
    # The phone sets "modified" on any tap in the sheet: cleaned with these styles.
    s = sp.StyleSession(sp.DEFAULTS)
    s.pick("rewrite", "keep")
    assert s.request_fields()["processing"] == "clean"


def test_mean_sends_processing_mean_only():
    s = sp.StyleSession(sp.DEFAULTS, mean_available=True)
    s.step(1)
    s.pick("tone", "mean")
    assert s.request_fields({"cleanup": "on"}) == {"processing": "mean"}
    assert s.describe() == "processing=mean"


def test_fields_are_valid_for_the_server():
    import json
    import os
    prompt = json.load(open(os.path.join(os.path.dirname(__file__), "..", "api",
                                         "cleanup_prompt.json"), encoding="utf-8"))
    s = sp.StyleSession(sp.DEFAULTS)
    s.pick("tone", "casual")
    f = s.request_fields()
    for k, v in f.items():
        if k.startswith("cleanup_"):
            assert v in prompt["style"][k[len("cleanup_"):]], (k, v)


# --- start values ----------------------------------------------------------------------

def test_start_values_rule_then_settings_then_server_defaults():
    env = {"CLEANUP_REWRITE": "polish", "CLEANUP_SHORTEN": "light", "CLEANUP_TONE": "bogus",
           "CLEANUP_EMOJI": "true"}
    assert sp.default_values(env) == {"punctuation": "standard", "rewrite": "polish",
                                      "tone": "neutral", "shorten": "light", "emoji": "on"}
    rule = {"app": "slack.exe", "cleanup": "on", "tone": "casual", "rewrite": "fix"}
    v = sp.start_values(rule, env)
    assert v == {"punctuation": "standard", "rewrite": "fix", "tone": "casual",
                 "shorten": "light", "emoji": "on"}
    assert sp.start_values(None, {}) == sp.DEFAULTS


def test_env_values_tolerate_quotes_and_comments():
    assert sp.default_values({"CLEANUP_PUNCTUATION": '"Light"'})["punctuation"] == "light"
    assert sp.default_values({"CLEANUP_REWRITE": "fix # comment"})["rewrite"] == "fix"
    assert sp.default_values({"CLEANUP_TONE": "mean"})["tone"] == "neutral"   # never a default


def test_session_for_each_source():
    env = {"CLEANUP_REWRITE": "fix"}
    local = sp.session_for(None, env, "local", started=5.0)
    assert local.values["rewrite"] == "fix" and local.mean_available and local.started == 5.0
    assert local.dim == 0 and not local.modified          # starts on Grammar & flow
    assert sp.session_for(None, env, "remote").mean_available
    assert sp.session_for(None, env, "cloud") is None     # no cleanup on Windows' cloud path
    assert sp.session_for(None, env, "local", selection=True) is None   # an edit, not a dictation
    assert sp.session_for(None, env, "local", styles_ok=False) is None


# --- placement and drawing ---------------------------------------------------------------

WORK = (0, 0, 1920, 1040)


def test_pill_goes_under_the_button_centred():
    x, y, above = sp.place_below((500, 300, 536, 336), (120, 50), WORK, 6)
    assert (x, y, above) == (458, 342, False)


def test_pill_goes_above_near_the_bottom_and_stays_on_screen():
    x, y, above = sp.place_below((1900, 1000, 1936, 1036), (120, 50), WORK, 6)
    assert above and y == 1000 - 6 - 50 and x == 1920 - 120


def test_sheet_moves_beside_the_button_when_it_fits_neither_way():
    work = (0, 0, 1920, 600)
    x, y, _ = sp.place_sheet((500, 280, 536, 316), (400, 560), work, 8)
    assert x == 536 + 8          # not over the mic button (it's the stop control)
    assert 0 <= y and y + 560 <= 600


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_pill_render_sizes(scale):
    s = sp.StyleSession(sp.DEFAULTS)
    full = sp.pill_size(s.content(), scale)
    small = sp.pill_size(s.content(), scale, compact=True)
    assert small[0] < full[0] and small[1] < full[1]
    img = sp.render_pill(s.content(), scale)
    m = sp.pill_margin(scale)
    assert img.mode == "RGBA" and img.size == (full[0] + 2 * m, full[1] + 2 * m)
    z = sp.render_pill(s.content(), scale, compact=True, zoom=sp.SHRUNK_ZOOM)
    assert z.size[0] < img.size[0]


def test_dots_differ_in_size_not_only_colour():
    assert sp.DOT_ON > sp.DOT_OFF


def _chips(layout):
    return [(box, data) for kind, box, data in layout["items"] if kind == "chip"]


def test_sheet_has_every_row_and_option():
    s = sp.StyleSession(sp.DEFAULTS, mean_available=True)
    lay = sp.sheet_layout(s, 1.0)
    titles = [d for k, _b, d in lay["items"] if k == "row_title"]
    assert titles == ["Grammar & flow", "Conciseness", "Punctuation", "Tone"]
    labels = [d[2] for _b, d in _chips(lay)]
    assert labels == ["Keep my words", "Fix grammar", "Polish", "Full length", "Shorter",
                      "Shortest", "Minimal", "Light", "Standard", "My own tone", "Professional",
                      "Casual", "Friendly", "Mean"]
    selected = [d[2] for _b, d in _chips(lay) if d[3]]
    assert selected == ["Keep my words", "Full length", "Standard", "My own tone"]
    assert "Mean" not in [d[2] for _b, d in _chips(sp.sheet_layout(sp.StyleSession(sp.DEFAULTS), 1.0))]


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_sheet_chips_fit_and_dont_overlap(scale):
    s = sp.StyleSession(sp.DEFAULTS, mean_available=True)
    lay = sp.sheet_layout(s, scale)
    w, _h = lay["size"]
    boxes = [b for b, _d in _chips(lay)]
    for l, t, r, b in boxes:
        assert l >= 0 and r <= w
    for i, a in enumerate(boxes):
        for c in boxes[i + 1:]:
            assert a[2] <= c[0] or c[2] <= a[0] or a[3] <= c[1] or c[3] <= a[1]
    # The three-option rows each fit on one line.
    for key in ("rewrite", "shorten", "punctuation"):
        tops = {round(b[1]) for b, d in _chips(lay) if d[0] == key}
        assert len(tops) == 1, key


def test_sheet_hit_testing():
    s = sp.StyleSession(sp.DEFAULTS, mean_available=True)
    lay = sp.sheet_layout(s, 1.5)
    for (l, t, r, b), d in _chips(lay):
        assert sp.sheet_hit(lay, ((l + r) / 2, (t + b) / 2)) == ("chip", d[0], d[1])
    done = next(b for k, b, _d in lay["items"] if k == "done")
    close = next(b for k, b, _d in lay["items"] if k == "close")
    assert sp.sheet_hit(lay, ((done[0] + done[2]) / 2, (done[1] + done[3]) / 2)) == ("done",)
    assert sp.sheet_hit(lay, ((close[0] + close[2]) / 2, (close[1] + close[3]) / 2)) == ("close",)
    assert sp.sheet_hit(lay, (2, 2)) is None


def test_picking_keeps_chips_in_place():
    s = sp.StyleSession(sp.DEFAULTS)
    before = [b for b, _d in _chips(sp.sheet_layout(s, 1.0))]
    s.pick("rewrite", "polish")
    after = [b for b, _d in _chips(sp.sheet_layout(s, 1.0))]
    assert before == after


def test_hint_line_appears_under_tone():
    s = sp.StyleSession(sp.DEFAULTS)
    s.pick("tone", "casual", now=10.0)
    kinds = [k for k, _b, _d in sp.sheet_layout(s, 1.0, now=11.0)["items"]]
    assert "hint" in kinds
    assert "hint" not in [k for k, _b, _d in sp.sheet_layout(s, 1.0, now=30.0)["items"]]


def test_selected_chip_has_a_check_mark_not_just_a_colour():
    """Grey out the colours: the selected chip still has a mark the others don't."""
    s = sp.StyleSession(sp.DEFAULTS)
    img, lay = sp.render_sheet(s, 2.0)
    grey = img.convert("L")
    m = sp.sheet_margin(2.0)
    chips = {d[2]: b for b, d in _chips(lay)}

    def left_strip(box):
        l, t, r, b = (int(v) + m for v in box)
        x0 = l + int(sp.CHIP_PAD * 2.0)
        return [grey.getpixel((x, y)) for x in range(x0, x0 + 20) for y in range(t + 6, b - 6)]
    sel = left_strip(chips["Keep my words"])
    unsel = left_strip(chips["Fix grammar"])
    assert max(sel) - min(sel) > 60       # the check's strokes against the fill
    assert max(unsel) - min(unsel) < 30   # plain chip there (its text is centred, further in)


def test_clock():
    assert sp.clock(0) == "00:00" and sp.clock(75.9) == "01:15" and sp.clock(-3) == "00:00"


def test_slides_follow_the_finger():
    old = ("Grammar & flow", "Keep my words", 0, 3)
    more = sp.slide_for("more", True, old, 0.0)
    which, (dx, _), _a = more.frame(0.05)
    assert which == "old" and dx < 0                  # leaves to the left with the finger
    which, (dx, _), _a = more.frame(0.10)
    assert which == "new" and dx > 0                  # the new one comes in from the right
    assert more.frame(1.0) is None
    which, (dx, _), _a = sp.slide_for("less", True, old, 0.0).frame(0.05)
    assert dx > 0
    which, (_, dy), _a = sp.slide_for("next", True, old, 0.0).frame(0.05)
    assert which == "old" and dy < 0                  # swipe up: the old style leaves upwards
    which, (dx, _), a = sp.slide_for("more", False, old, 0.0).frame(0.03)
    assert which == "new" and dx < 0 and a == 1.0     # at the end: a bump, same words


def test_tween():
    t = sp.Tween(1.0)
    t.to(0.5, 200, now=0.0)
    assert t.value(0.0) == 1.0 and t.value(0.2) == 0.5 and 0.5 < t.value(0.1) < 1.0
    assert t.done(0.2) and not t.done(0.1)


# --- the floating button ------------------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.calls = []

    def sync(self, show, session, box, scale, work):
        self.calls.append((show, session))

    def poke(self):
        pass

    def keep_on_top(self):
        pass

    def destroy(self):
        pass


def _button(monkeypatch):
    b = cb.CaretButton(lambda: None)
    b.hwnd = 1
    b._style = _FakeOverlay()
    monkeypatch.setattr(b, "_monitor", lambda x, y: ((0, 0, 1920, 1040), 96))
    return b


def test_pill_shown_only_while_recording_with_a_session(monkeypatch):
    b = _button(monkeypatch)
    s = sp.StyleSession(sp.DEFAULTS)
    b._state = "recording"
    b._sync_style(True)
    assert b._style.calls[-1] == (False, None)        # no session yet
    b._style_session = s
    b._sync_style(True)
    assert b._style.calls[-1] == (True, s)
    b._sync_style(False)                              # button hidden
    assert b._style.calls[-1][0] is False
    b._state = "working"
    b._sync_style(True)
    assert b._style.calls[-1][0] is False
    b._state = "recording"
    s.close()
    b._sync_style(True)
    assert b._style.calls[-1][0] is False


def test_no_pill_when_the_button_is_off(monkeypatch):
    b = _button(monkeypatch)
    b._state, b._enabled = "recording", False
    b._style_session = sp.StyleSession(sp.DEFAULTS)
    b._sync_style(True)
    assert b._style.calls[-1][0] is False


def test_begin_style_sets_the_session():
    b = cb.CaretButton(lambda: None)
    s = sp.StyleSession(sp.DEFAULTS)
    b.begin_style(s)
    assert b._style_session is s
    b.begin_style(None)
    assert b._style_session is None


def test_hotkey_recording_brings_the_button_up(monkeypatch):
    b = cb.CaretButton(lambda: None)
    b._state = "recording"                            # the listener's set_state
    b._follow_state(10.0)
    assert b._pending and b._pending[1] == "record"


def test_hotkey_recording_with_button_already_up_just_pins_it():
    b = cb.CaretButton(lambda: None)
    b.timeline.show(0)
    b._state = "recording"
    b._follow_state(1.0)
    assert b._pending is None and b.timeline.pinned


class _Info:
    def __init__(self, caret, ok=True, source="caret"):
        self.ok, self.caret, self.caret_source = ok, caret, source
        self.hwnd = self.element = self.control_type = self.name = None


class _Inspector:
    def __init__(self, info):
        self.info = info

    def inspect(self):
        return self.info


def _record_check(monkeypatch, info):
    b = cb.CaretButton(lambda: None)
    b._inspector = _Inspector(info)
    shown = []
    monkeypatch.setattr(b, "_show_at", lambda anchor, at_el: shown.append((anchor, at_el)))
    monkeypatch.setattr(b, "_hide", lambda: shown.append("hide"))
    monkeypatch.setattr(cb.user32, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(b, "_own_window", lambda h: False)
    monkeypatch.setattr(b, "_hidden_app", lambda h: False)
    monkeypatch.setattr(cb.CaretButton, "_cursor", staticmethod(lambda: (700, 400)))
    b._state = "recording"
    b._check("record", None)
    return shown


def test_record_check_uses_the_caret(monkeypatch):
    assert _record_check(monkeypatch, _Info((10, 20, 11, 40))) == [((10, 20, 11, 40), False)]


def test_record_check_falls_back_to_the_pointer(monkeypatch):
    # A remote desktop or a game: no caret, so beside the mouse pointer.
    assert _record_check(monkeypatch, None) == [((700, 392, 700, 408), False)]
    assert _record_check(monkeypatch, _Info(None, ok=False)) == [((700, 392, 700, 408), False)]


def test_record_check_respects_hidden_apps(monkeypatch):
    b = cb.CaretButton(lambda: None)
    b._inspector = _Inspector(_Info((10, 20, 11, 40)))
    shown = []
    monkeypatch.setattr(b, "_show_at", lambda *a: shown.append(a))
    monkeypatch.setattr(cb.user32, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(b, "_own_window", lambda h: False)
    monkeypatch.setattr(b, "_hidden_app", lambda h: True)
    b._state = "recording"
    b._check("record", None)
    assert shown == []


# --- the listener ----------------------------------------------------------------------------

class _Target:
    def __init__(self, app="slack.exe", style=None):
        self.app, self.selection, self.spot, self.style = app, "", None, style
        self.stopped = False

    def wait(self, timeout=2.0):
        return self


def _listener(monkeypatch, rules):
    import background_listener as bl
    sent = {}
    monkeypatch.setattr(bl.app_styles, "load", lambda: rules)
    monkeypatch.setattr(bl.transcribe_client, "transcribe",
                        lambda path, env, timeout=60, style=None: sent.setdefault("style", style) and "")
    monkeypatch.setattr(bl, "overlay", None)
    monkeypatch.setattr(bl, "_log", lambda msg: sent.setdefault("log", []).append(msg))
    return bl, sent


def test_listener_sends_app_style_when_pill_untouched(monkeypatch):
    bl, sent = _listener(monkeypatch, [{"app": "slack.exe", "tone": "casual"}])
    bl.transcribe_and_paste(0, _Target(style=sp.StyleSession(sp.DEFAULTS)), 0)
    assert sent["style"] == {"cleanup_tone": "casual"}


def test_listener_sends_pill_choices(monkeypatch):
    bl, sent = _listener(monkeypatch, [{"app": "slack.exe", "cleanup": "off"}])
    s = sp.StyleSession(sp.DEFAULTS)
    s.step(1)
    s.close()
    bl.transcribe_and_paste(0, _Target(style=s), 0)
    assert sent["style"]["processing"] == "clean" and sent["style"]["cleanup_rewrite"] == "fix"
    assert "cleanup" not in sent["style"]
    assert any("style pill" in line for line in sent["log"])


def test_listener_begin_style_starts_from_the_app_rule(monkeypatch):
    import background_listener as bl
    got = []

    class Btn:
        def begin_style(self, session):
            got.append(session)
    monkeypatch.setattr(bl.app_styles, "load", lambda: [{"app": "slack.exe", "rewrite": "polish"}])
    monkeypatch.setattr(bl, "_env", lambda: {"TRANSCRIBE_SOURCE": "local"})
    monkeypatch.setattr(bl, "caret_btn", Btn())
    t = _Target()
    monkeypatch.setattr(bl, "current_target", t)
    bl._begin_style(t, 3.0)
    assert t.style is got[0] and got[0].values["rewrite"] == "polish" and got[0].started == 3.0


def test_listener_no_pill_for_cloud_or_after_stop(monkeypatch):
    import background_listener as bl
    got = []

    class Btn:
        def begin_style(self, session):
            got.append(session)
    monkeypatch.setattr(bl.app_styles, "load", lambda: [])
    monkeypatch.setattr(bl, "caret_btn", Btn())
    t = _Target()
    monkeypatch.setattr(bl, "current_target", t)
    monkeypatch.setattr(bl, "_env", lambda: {"TRANSCRIBE_SOURCE": "cloud"})
    bl._begin_style(t, 0.0)
    assert t.style is None and got == [None]
    # Stopped before the lookup finished: the session is made but closed, never shown.
    monkeypatch.setattr(bl, "_env", lambda: {"TRANSCRIBE_SOURCE": "local"})
    t2 = _Target()
    monkeypatch.setattr(bl, "current_target", t2)
    bl._end_style(t2)
    bl._begin_style(t2, 0.0)
    assert t2.style is not None and t2.style.closed and len(got) == 1
