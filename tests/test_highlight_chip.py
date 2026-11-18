"""The highlight chip and its sheet (client/highlight_chip.py, drawn by
style_pill.py), which way a recording over highlighted words is sent, and the
"The AI made no changes." notice (background_listener.py).

Everything here is fakes: no window is made, no key is pressed, the clipboard
and the network are never touched, and .env is never read."""
import sys

import pytest

import highlight_chip as hc
import transcribe_client as tc

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32",
                                  reason="the chip, the sheet and this listener are Windows-only")

TEXT = "see you tomorrow at the usual place"       # 7 words


# --- the words on the chip ------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("", 0), (None, 0), ("   \n\t ", 0), ("hello", 1), ("  hello  ", 1),
    ("hello world", 2), ("one\ntwo\tthree   four", 4), ("it's a well-known thing.", 4),
])
def test_word_count_is_what_white_space_separates(text, want):
    assert hc.word_count(text) == want


def test_chip_text_and_title():
    assert hc.words_label(1) == "1 word" and hc.words_label(113) == "113 words"
    assert hc.chip_text(113, rewrite=True) == "✎ 113 words"
    assert hc.chip_text(1, rewrite=True) == "✎ 1 word"
    assert hc.chip_text(113, rewrite=False) == "113 words"       # no pencil: drawn struck through
    assert hc.sheet_title(113) == "Your 113 highlighted words"
    assert hc.sheet_title(1) == "Your 1 highlighted word"


def test_the_sheet_says_exactly_what_the_phone_says():
    assert hc.OPTIONS == (
        ("rewrite", "Rewrite them",
         "What you say is used as directions. The highlighted words and your directions "
         "are sent to your AI, and the answer replaces them."),
        ("type_over", "Type over them",
         "What you say replaces the highlighted words, as normal dictation."))
    assert hc.NO_AI_BODY == ("What you say replaces the highlighted words. Rewriting needs "
                             "your PC's AI, which this mode does not use.")
    assert hc.NO_CHANGES == "The AI made no changes."


def test_plain_words_only():
    shown = [hc.REWRITE_TITLE, hc.REWRITE_BODY, hc.TYPE_OVER_TITLE, hc.TYPE_OVER_BODY,
             hc.NO_AI_BODY, hc.NO_CHANGES, hc.sheet_title(113), hc.chip_text(113, True)]
    for text in shown:
        low = text.lower()
        for word in ("prompt", "selection", "llm", "endpoint", "instruction"):
            assert word not in low, (word, text)
        assert "—" not in text and "–" not in text


# --- the choice (one recording) ---------------------------------------------------

def test_every_recording_starts_on_rewrite():
    c = hc.HighlightChoice.for_highlight(TEXT)
    assert c.count == 7 and c.mode == hc.REWRITE and c.rewrite and not c.struck
    assert c.chip_text() == "✎ 7 words" and c.title() == "Your 7 highlighted words"
    # A new recording over the same words: a new choice, on Rewrite again.
    c.choose(hc.TYPE_OVER)
    assert hc.HighlightChoice.for_highlight(TEXT).mode == hc.REWRITE


def test_type_over_and_back():
    c = hc.HighlightChoice(113)
    v = c.version
    assert c.choose(hc.TYPE_OVER)
    assert c.mode == hc.TYPE_OVER and not c.rewrite and c.struck and c.version > v
    assert c.chip_text() == "113 words"
    assert c.choose(hc.REWRITE) and c.rewrite and c.chip_text() == "✎ 113 words"
    v = c.version
    assert c.choose(hc.REWRITE) and c.version == v          # the same again: nothing new
    assert not c.choose("something else") and c.mode == hc.REWRITE


def test_the_choice_is_final_once_the_recording_stops():
    c = hc.HighlightChoice(5)
    c.choose(hc.TYPE_OVER)
    c.close()
    assert not c.choose(hc.REWRITE) and c.mode == hc.TYPE_OVER


def test_no_ai_means_no_choice():
    c = hc.HighlightChoice.for_highlight(TEXT, can_rewrite=False)
    assert c.struck and not c.rewrite and c.chip_text() == "7 words"
    assert not c.choose(hc.REWRITE) and not c.choose(hc.TYPE_OVER)
    assert "no AI" in c.describe() and "7 words" in c.describe()


def test_a_highlight_with_no_words_has_no_chip():
    assert hc.HighlightChoice.for_highlight("") is None
    assert hc.HighlightChoice.for_highlight("  \n ") is None


# --- which way the recording goes --------------------------------------------------

@pytest.mark.parametrize("highlight, mode, can, want", [
    (TEXT, hc.REWRITE, True, "rewrite"),
    (TEXT, hc.TYPE_OVER, True, "dictate"),
    (TEXT, hc.REWRITE, False, "dictate"),       # no AI to rewrite with
    (TEXT, hc.TYPE_OVER, False, "dictate"),
    ("", hc.REWRITE, True, "dictate"),          # nothing highlighted
    ("   ", hc.REWRITE, True, "dictate"),       # a highlighted space is not words
])
def test_path_for(highlight, mode, can, want):
    assert hc.path_for(highlight, mode, can) == want


@pytest.mark.parametrize("answer, highlight, edited, want", [
    ("Thanks so much!", "thx", True, False),
    ("thx", "thx", True, True),
    ("  thx\n", "thx ", True, True),            # white space at the ends doesn't count
    ("thx", "thx", False, True),
    ("Thanks so much!", "thx", False, True),    # the server says it didn't edit
    ("Thx", "thx", True, False),                # a capital letter is a change
    ("", "thx", True, False),
])
def test_no_changes(answer, highlight, edited, want):
    assert hc.no_changes(answer, highlight, edited) is want


# --- the client carries "edited" through -------------------------------------------

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body


class FakePost:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def __call__(self, url, **kw):
        self.calls.append(dict(kw, url=url))
        return self.resp


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFF0000WAVE")
    return str(p)


def test_edit_selection_result_says_whether_it_was_edited(wav):
    env = {"WHISPER_API_PORT": "5055"}
    post = FakePost(Resp(body={"text": "Thanks so much!", "instruction": "politer", "edited": True}))
    assert tc.edit_selection_result(wav, env, "thx", post=post) == ("Thanks so much!", True)
    assert post.calls[0]["url"] == "http://127.0.0.1:5055/dictate/v1/edit_selection"
    assert post.calls[0]["data"] == {"selection": "thx"}
    post = FakePost(Resp(body={"text": "thx", "instruction": "", "edited": False}))
    assert tc.edit_selection_result(wav, env, "thx", post=post) == ("thx", False)
    # A server that doesn't say: the text itself decides.
    assert tc.edit_selection_result(wav, env, "thx", post=FakePost(Resp(body={"text": "x"}))) \
        == ("x", True)
    assert tc.edit_selection_result(wav, env, "thx", post=FakePost(Resp(body={}))) == ("thx", True)


def test_edit_selection_still_returns_only_the_text(wav):
    post = FakePost(Resp(body={"text": "Thanks so much!", "edited": True}))
    assert tc.edit_selection(wav, {"WHISPER_API_PORT": "5055"}, "thx", post=post) == "Thanks so much!"
    post = FakePost(Resp(body={"text": "", "edited": False}))
    assert tc.edit_selection(wav, {"WHISPER_API_PORT": "5055"}, "thx", post=post) == "thx"


def test_only_a_cloud_service_cant_rewrite():
    assert tc.can_edit({}) and tc.can_edit({"TRANSCRIBE_SOURCE": "local"})
    assert tc.can_edit({"TRANSCRIBE_SOURCE": "remote"})
    assert not tc.can_edit({"TRANSCRIBE_SOURCE": "cloud"})
    with pytest.raises(tc.TranscribeError):          # and the editor agrees
        tc._editor_endpoint({"TRANSCRIBE_SOURCE": "cloud"}, "/dictate/v1/edit_selection", None, None)


# --- the session, the chip and the sheet (drawn into pictures, no window) -----------

if sys.platform == "win32":
    import style_pill as sp


@WINDOWS_ONLY
def test_session_with_a_highlight_shows_the_chip_not_the_pill():
    c = hc.HighlightChoice(113)
    s = sp.session_for(None, {}, "local", selection=True, highlight=c, started=2.0)
    assert s is not None and s.highlight is c and s.styles and s.started == 2.0
    assert not s.pill_shown()                       # a rewrite: styles don't apply
    c.choose(hc.TYPE_OVER)
    assert s.pill_shown()                           # the normal pill is back
    assert s.step(1) and s.request_fields({})["cleanup_rewrite"] == "fix"
    c.choose(hc.REWRITE)
    assert not s.pill_shown()
    s.close()
    assert c.closed                                 # stopping freezes the choice too


@WINDOWS_ONLY
def test_session_with_a_highlight_and_no_ai_or_no_styles():
    c = hc.HighlightChoice(113, can_rewrite=False)
    s = sp.session_for(None, {}, "cloud", selection=True, highlight=c)
    assert s is not None and not s.styles and not s.pill_shown()    # the chip alone
    s = sp.session_for(None, {}, "local", selection=True, styles_ok=False,
                       highlight=hc.HighlightChoice(3))
    s.highlight.choose(hc.TYPE_OVER)
    assert not s.pill_shown()                       # an old setup has no pill to bring back
    # No choice object: as before (no pill for a highlight, none for a cloud service).
    assert sp.session_for(None, {}, "local", selection=True) is None
    assert sp.session_for(None, {}, "cloud") is None
    assert sp.session_for(None, {}, "local").pill_shown()


@WINDOWS_ONLY
def test_the_pill_goes_under_the_chip():
    button = (100, 100, 148, 148)
    assert sp.stack_anchor(button, None) == button
    assert sp.stack_anchor(button, (90, 154, 160, 182)) == (100, 100, 148, 182)    # chip below
    assert sp.stack_anchor(button, (90, 66, 160, 94)) == (100, 66, 148, 148)       # chip above
    work = (0, 0, 1920, 1080)
    chip = sp.place_below(button, (70, 28), work, 6)
    box = (chip[0], chip[1], chip[0] + 70, chip[1] + 28)
    pill = sp.place_below(sp.stack_anchor(button, box), (120, 50), work, 6)
    assert pill[1] >= box[3] + 6                    # never on top of the chip


@WINDOWS_ONLY
def test_chip_pictures():
    rewrite = sp.render_chip(113, True, 1.0)
    struck = sp.render_chip(113, False, 1.0)
    assert rewrite.size[0] > struck.size[0] and rewrite.size[1] == struck.size[1]   # the pencil
    assert sp.chip_size(113, True, 2.0)[0] > sp.chip_size(113, True, 1.0)[0]
    assert sp.chip_size(1, True, 1.0)[0] < sp.chip_size(113, True, 1.0)[0]
    assert rewrite.tobytes() != sp.render_chip(113, True, 1.0, hover=True).tobytes()
    # The rewrite chip is drawn in the accent colour, the struck one is not.
    pal = sp.palette()

    def has(img, rgb):
        return any(px[:3] == rgb and px[3] > 200 for px in img.getdata())
    assert has(rewrite, pal["accent"]) and not has(struck, pal["accent"])


@WINDOWS_ONLY
def test_sheet_has_two_choices_and_one_is_selected():
    c = hc.HighlightChoice(113)
    lay = sp.highlight_sheet_layout(c, 1.0)
    kinds = [i[0] for i in lay["items"]]
    assert kinds == ["header", "close", "divider", "option", "option", "done"]
    assert lay["items"][0][2] == "Your 113 highlighted words"
    opts = [i for i in lay["items"] if i[0] == "option"]
    assert [(o[2][0], o[2][1], o[2][3]) for o in opts] == [
        ("rewrite", "Rewrite them", True), ("type_over", "Type over them", False)]
    assert " ".join(opts[0][2][2]) == hc.REWRITE_BODY and len(opts[0][2][2]) > 1    # wrapped whole
    assert " ".join(opts[1][2][2]) == hc.TYPE_OVER_BODY
    inner = lay["size"][0] - 2 * sp.SHEET_PAD[0] - 2 * sp.OPT_PAD[0]
    font = sp._font(False, sp.OPT_BODY_PX)
    assert all(sp._text_w(font, line) <= inner for o in opts for line in o[2][2])
    l, t, r, b = opts[1][1]
    assert sp.highlight_sheet_hit(lay, ((l + r) / 2, (t + b) / 2)) == ("mode", "type_over")
    l, t, r, b = opts[0][1]
    assert sp.highlight_sheet_hit(lay, (l + 2, t + 2)) == ("mode", "rewrite")
    l, t, r, b = [i for i in lay["items"] if i[0] == "done"][0][1]
    assert sp.highlight_sheet_hit(lay, ((l + r) / 2, (t + b) / 2)) == ("done",)
    l, t, r, b = [i for i in lay["items"] if i[0] == "close"][0][1]
    assert sp.highlight_sheet_hit(lay, ((l + r) / 2, (t + b) / 2)) == ("close",)
    assert sp.highlight_sheet_hit(lay, (1, 1)) is None
    c.choose(hc.TYPE_OVER)
    opts = [i for i in sp.highlight_sheet_layout(c, 1.0)["items"] if i[0] == "option"]
    assert [o[2][3] for o in opts] == [False, True]


@WINDOWS_ONLY
def test_sheet_without_an_ai_only_explains():
    c = hc.HighlightChoice(113, can_rewrite=False)
    lay = sp.highlight_sheet_layout(c, 1.0)
    assert [i[0] for i in lay["items"]] == ["header", "close", "divider", "note", "done"]
    note = [i for i in lay["items"] if i[0] == "note"][0]
    assert " ".join(note[2]) == hc.NO_AI_BODY
    l, t, r, b = note[1]
    assert sp.highlight_sheet_hit(lay, ((l + r) / 2, (t + b) / 2)) is None       # nothing to pick
    assert lay["size"][1] < sp.highlight_sheet_layout(hc.HighlightChoice(113), 1.0)["size"][1]


@WINDOWS_ONLY
def test_sheet_pictures_follow_the_choice_and_the_clock():
    c = hc.HighlightChoice(113)
    a, lay = sp.render_highlight_sheet(c, 0.0, 1.0, now=4.0)
    m = sp.sheet_margin(1.0)
    assert a.size == (lay["size"][0] + 2 * m, lay["size"][1] + 2 * m)
    assert a.tobytes() != sp.render_highlight_sheet(c, 0.0, 1.0, now=5.0)[0].tobytes()
    c.choose(hc.TYPE_OVER)
    assert a.tobytes() != sp.render_highlight_sheet(c, 0.0, 1.0, now=4.0)[0].tobytes()
    sp.render_highlight_sheet(c, 0.0, 1.5, hover=("mode", "rewrite"), now=4.0)
    sp.render_highlight_sheet(hc.HighlightChoice(1, can_rewrite=False), 0.0, 2.0, now=4.0)


@WINDOWS_ONLY
def test_the_style_sheet_still_draws_the_same_parts():
    # Its header, close X, divider and Done are now shared with the highlight sheet.
    s = sp.StyleSession({})
    img, lay = sp.render_sheet(s, 1.0, now=s.started + 5)
    assert [i[0] for i in lay["items"]][:3] == ["header", "close", "divider"]
    assert img.tobytes() != sp.render_sheet(s, 1.0, hover=("done",), now=s.started + 5)[0].tobytes()
    assert img.tobytes() != sp.render_sheet(s, 1.0, hover=("close",), now=s.started + 5)[0].tobytes()


@WINDOWS_ONLY
def test_a_click_in_the_sheet_changes_the_choice(monkeypatch):
    # The overlay's own logic, with its windows never made.
    o = sp.StyleOverlay(log=lambda msg: lines.append(msg))
    lines, drawn = [], []
    monkeypatch.setattr(o, "_place_sheet", lambda now, relayout: drawn.append(relayout))
    c = hc.HighlightChoice(113)
    o.session = sp.session_for(None, {}, "local", highlight=c)
    o.sheet_kind = "highlight"
    o._sheet_action(("mode", "type_over"))
    assert c.mode == hc.TYPE_OVER and drawn == [True]
    assert lines == ["highlight: 113 words, type over"]
    o._sheet_action(("mode", "rewrite"))
    assert c.rewrite and lines[-1] == "highlight: 113 words, rewrite"
    assert o.pill is None and o.chip is None and o.sheet is None        # no window was needed


@WINDOWS_ONLY
def test_the_chip_never_takes_the_keyboard_and_a_click_opens_its_sheet(monkeypatch):
    # The chip's message handler, called by hand: no window, no real click.
    o = sp.StyleOverlay()
    cb = sp._cb
    # With or without a recording, a press must not make it the active window
    # (the highlight in the app would be lost).
    assert o._chip_msg(0, cb.WM_MOUSEACTIVATE, 0, 0) == cb.MA_NOACTIVATE
    assert o._chip_msg(0, sp.WM_POINTERACTIVATE, 0, 0) == sp.PA_NOACTIVATE
    assert o._sheet_msg(0, cb.WM_MOUSEACTIVATE, 0, 0) == cb.MA_NOACTIVATE
    opened = []
    monkeypatch.setattr(o, "open_sheet", lambda kind="style": opened.append(kind))
    assert o._chip_msg(0, cb.WM_LBUTTONDOWN, 0, 0) is None      # no recording: not ours
    o.session = sp.session_for(None, {}, "local", highlight=hc.HighlightChoice(113))
    assert o._chip_msg(0, cb.WM_MOUSEACTIVATE, 0, 0) == cb.MA_NOACTIVATE
    o._chip_msg(0, cb.WM_LBUTTONUP, 0, 0)                       # a release without a press
    assert opened == []
    o._chip_msg(0, cb.WM_LBUTTONDOWN, 0, 0)
    o._chip_msg(0, cb.WM_LBUTTONUP, 0, 0)
    assert opened == ["highlight"]


# --- the listener ------------------------------------------------------------------

class Target:
    def __init__(self, selection="", highlight=None, style=None, app="notepad.exe"):
        self.selection, self.highlight, self.style, self.app = selection, highlight, style, app
        self.spot, self.stopped, self.released, self.whole_box = None, False, 0, False

    def wait(self, timeout=2.0):
        return self

    def release(self):
        self.released += 1


class Overlay:
    def __init__(self, log):
        self.log = log

    def notice_safe(self, title, detail="", seconds=6):
        self.log["notice"].append((title, detail))

    def update_label_safe(self, text):
        self.log["label"].append(text)

    def show_safe(self):
        pass

    def hide_safe(self):
        pass


@pytest.fixture
def listener(monkeypatch):
    """The listener with every way out replaced: no server, no paste, no
    keys, no clipboard, no window, and its own pretend settings."""
    import background_listener as bl
    log = {"notice": [], "label": [], "delivered": [], "lines": [], "edit": [], "dictate": [],
           "typed": [], "watched": [], "keys": [], "env": {"TRANSCRIBE_SOURCE": "local"},
           "answer": ("Thanks so much!", True), "said": "hello there"}
    monkeypatch.setattr(bl, "_env", lambda: dict(log["env"]))
    monkeypatch.setattr(bl, "overlay", Overlay(log))
    monkeypatch.setattr(bl, "_log", lambda msg: log["lines"].append(msg))
    monkeypatch.setattr(bl, "deliver", lambda text, cleanup, stop_chars, target, env:
                        log["delivered"].append((text, target.selection)))
    monkeypatch.setattr(bl, "_watch_corrections", lambda text, env: log["watched"].append(text))
    monkeypatch.setattr(bl.app_styles, "load", lambda: [])
    monkeypatch.setattr(bl.keyboard, "send", lambda *a, **k: log["keys"].append(a))
    monkeypatch.setattr(bl.time, "sleep", lambda s: None)

    def edit(path, env, selection, **kw):
        log["edit"].append(selection)
        return log["answer"]

    def dictate(path, env, timeout=60, style=None):
        log["dictate"].append(style)
        return log["said"]

    def typed(env, instruction, selection="", **kw):
        log["typed"].append((instruction, selection))
        return log["answer"]
    monkeypatch.setattr(bl.transcribe_client, "edit_selection_result", edit)
    monkeypatch.setattr(bl.transcribe_client, "transcribe", dictate)
    monkeypatch.setattr(bl.transcribe_client, "edit_text", typed)
    return bl, log


@WINDOWS_ONLY
def test_recording_path(listener):
    bl, _log = listener
    local, cloud = {"TRANSCRIBE_SOURCE": "local"}, {"TRANSCRIBE_SOURCE": "cloud"}
    assert bl.recording_path(None, local) == "dictate"
    assert bl.recording_path(Target(), local) == "dictate"
    assert bl.recording_path(Target(TEXT), local) == "rewrite"          # no chip yet: Rewrite
    assert bl.recording_path(Target(TEXT, hc.HighlightChoice(7)), local) == "rewrite"
    over = hc.HighlightChoice(7)
    over.choose(hc.TYPE_OVER)
    assert bl.recording_path(Target(TEXT, over), local) == "dictate"
    assert bl.recording_path(Target(TEXT, over), {"TRANSCRIBE_SOURCE": "remote"}) == "dictate"
    assert bl.recording_path(Target(TEXT), cloud) == "dictate"
    assert bl.recording_path(Target(TEXT, hc.HighlightChoice(7, can_rewrite=False)), cloud) == "dictate"
    # A one-line box highlighted from end to end (an address bar): typed over,
    # with the chip there to say so and to change it.
    box = hc.HighlightChoice.for_highlight("github.com/x", whole_box=True)
    assert box.mode == hc.TYPE_OVER and box.struck and box.chip_text() == "1 word"
    assert bl.recording_path(Target(TEXT, box), local) == "dictate"
    assert box.choose(hc.REWRITE) and bl.recording_path(Target(TEXT, box), local) == "rewrite"
    early = Target(TEXT)             # stopped before the chip was up
    early.whole_box = True
    assert bl.recording_path(early, local) == "dictate"


@WINDOWS_ONLY
def test_a_highlight_is_rewritten_and_the_answer_pasted_over_it(listener):
    bl, log = listener
    bl.transcribe_and_paste(0, Target(TEXT, hc.HighlightChoice(7)), 0)
    assert log["edit"] == [TEXT] and log["dictate"] == []
    assert log["delivered"] == [("Thanks so much!", TEXT)]
    assert log["notice"] == [] and log["watched"] == []
    assert any("7 words" in line and "rewrite" in line for line in log["lines"])


@WINDOWS_ONLY
@pytest.mark.parametrize("answer", [
    (TEXT, True),                    # the same words back
    ("  " + TEXT + "\n", True),      # the same, give or take white space at the ends
    (TEXT, False),                   # the server says it wasn't edited
    ("Something else", False),
])
def test_an_unchanged_rewrite_is_not_pasted_and_says_so(listener, answer):
    bl, log = listener
    log["answer"] = answer
    bl.transcribe_and_paste(0, Target(TEXT, hc.HighlightChoice(7)), 0)
    assert log["delivered"] == []
    assert log["notice"] == [("The AI made no changes.", "")]
    said = [line for line in log["lines"] if "no changes" in line]
    assert len(said) == 1 and "%d chars" % len(TEXT) in said[0]
    assert not any(TEXT in line or "Something else" in line for line in log["lines"])   # lengths only


@WINDOWS_ONLY
def test_type_over_is_sent_as_ordinary_dictation_and_pasted_over_the_highlight(listener):
    bl, log = listener
    choice = hc.HighlightChoice(7)
    import style_pill
    session = style_pill.session_for(None, {}, "local", highlight=choice)
    choice.choose(hc.TYPE_OVER)
    session.step(1)                                  # the pill is back, and was used
    session.close()
    bl.transcribe_and_paste(0, Target(TEXT, choice, style=session), 0)
    assert log["edit"] == []
    assert log["dictate"][0]["cleanup_rewrite"] == "fix"
    assert log["delivered"] == [("hello there", TEXT)]      # deliver() re-selects it (need_sel)
    assert log["notice"] == []
    assert any("dictate" in line for line in log["lines"])


@WINDOWS_ONLY
def test_pill_changes_dont_reach_a_rewrite(listener):
    bl, log = listener
    choice = hc.HighlightChoice(7)
    import style_pill
    session = style_pill.session_for(None, {}, "local", highlight=choice)
    choice.choose(hc.TYPE_OVER)
    session.step(1)
    choice.choose(hc.REWRITE)                        # changed their mind
    bl.transcribe_and_paste(0, Target(TEXT, choice, style=session), 0)
    assert log["edit"] == [TEXT] and log["dictate"] == []


@WINDOWS_ONLY
def test_with_a_cloud_service_a_highlight_is_typed_over(listener):
    bl, log = listener
    log["env"] = {"TRANSCRIBE_SOURCE": "cloud"}
    bl.transcribe_and_paste(0, Target(TEXT, hc.HighlightChoice(7, can_rewrite=False)), 0)
    assert log["edit"] == [] and log["dictate"] == [{}]
    assert log["delivered"] == [("hello there", TEXT)]


@WINDOWS_ONLY
def test_no_highlight_is_plain_dictation_as_before(listener):
    bl, log = listener
    bl.transcribe_and_paste(0, Target(), 0)
    assert log["edit"] == [] and log["delivered"] == [("hello there", "")]
    assert not any("highlight" in line for line in log["lines"])


class Btn:
    def __init__(self):
        self.got = []

    def begin_style(self, session):
        self.got.append(session)


@WINDOWS_ONLY
def test_a_recording_over_a_highlight_gets_the_chip(listener, monkeypatch):
    bl, log = listener
    btn, asked = Btn(), []
    monkeypatch.setattr(bl, "caret_btn", btn)
    monkeypatch.setattr(bl.transcribe_client, "custom_triggers", lambda env: asked.append(1) or [])
    t = Target(TEXT)
    monkeypatch.setattr(bl, "current_target", t)
    bl._begin_style(t, 3.0)
    assert t.highlight.count == 7 and t.highlight.rewrite
    assert btn.got == [t.style] and t.style.highlight is t.highlight and not t.style.pill_shown()
    assert asked == [1]                              # skills, for when the pill comes back
    bl._end_style(t)
    assert t.highlight.closed and t.style.closed


@WINDOWS_ONLY
def test_the_chip_is_struck_through_with_a_cloud_service(listener, monkeypatch):
    bl, log = listener
    log["env"] = {"TRANSCRIBE_SOURCE": "cloud"}
    btn, asked = Btn(), []
    monkeypatch.setattr(bl, "caret_btn", btn)
    monkeypatch.setattr(bl.transcribe_client, "custom_triggers", lambda env: asked.append(1) or [])
    t = Target(TEXT)
    monkeypatch.setattr(bl, "current_target", t)
    bl._begin_style(t, 0.0)
    assert t.highlight.struck and not t.highlight.can_rewrite
    assert btn.got[0] is t.style and not t.style.pill_shown() and asked == []
    # And nothing highlighted with a cloud service: nothing to show, as before.
    t2 = Target()
    monkeypatch.setattr(bl, "current_target", t2)
    bl._begin_style(t2, 0.0)
    assert t2.highlight is None and t2.style is None and btn.got[-1] is None


# --- typed directions (the mic button's right-click box) ------------------------------

@WINDOWS_ONLY
def test_typed_with_the_highlight_changed_is_pasted(listener):
    bl, log = listener
    t = Target(TEXT)
    bl._typed_instruction(t, "make it friendlier", True)
    assert log["typed"] == [("make it friendlier", TEXT)]
    assert log["delivered"] == [("Thanks so much!", TEXT)] and log["notice"] == []
    assert t.released == 1 and log["keys"] == []


@WINDOWS_ONLY
@pytest.mark.parametrize("answer", [(TEXT, True), (TEXT + "  ", True), (TEXT, False), ("", False)])
def test_typed_with_the_highlight_unchanged_is_not_pasted(listener, monkeypatch, answer):
    bl, log = listener
    monkeypatch.setattr(bl, "_typed_draft", "")
    log["answer"] = answer
    bl._typed_instruction(Target(TEXT), "make it friendlier", True)
    assert log["delivered"] == []
    assert log["notice"] == [("The AI made no changes.", "")]
    assert bl._typed_draft == "make it friendlier"           # kept, to word it another way
    assert not any(TEXT in line or "friendlier" in line for line in log["lines"])


@WINDOWS_ONLY
def test_typed_without_a_highlight_is_as_before(listener, monkeypatch):
    bl, log = listener
    monkeypatch.setattr(bl, "_typed_draft", "")
    bl._typed_instruction(Target(), "write a polite no", False)
    assert log["delivered"] == [("Thanks so much!", "")] and log["notice"] == []
    log["delivered"].clear()
    log["answer"] = ("", False)                      # the AI gave nothing
    bl._typed_instruction(Target(), "write a polite no", False)
    assert log["delivered"] == []
    assert log["notice"] == [bl.typed_failure_message(False)]
    assert bl._typed_draft == "write a polite no"
