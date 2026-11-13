"""Going back to the box a recording started in before pasting (text_focus.
unchanged / go_back / ready_to_paste, and the listener's deliver()).

All with fake window/focus operations: no real window is touched."""
import pytest

import text_focus as tf

A, B = 1001, 2002      # the original window, somewhere else
PID_A = 77


class FakeOps:
    """A pretend desktop. Knobs say what Windows / the app will allow."""

    def __init__(self, fg=B, alive=True, pid=PID_A, minimized=False, front_ok=True,
                 focus="other", set_focus_works=True, caret_works=True, click_works=False,
                 selection_kept=True):
        self.fg, self.alive, self.pid, self.minimized = fg, alive, pid, minimized
        self.front_ok, self.focus = front_ok, focus
        self.set_focus_works, self.caret_works = set_focus_works, caret_works
        self.click_works, self.selection_kept = click_works, selection_kept
        self.calls = []

    def foreground(self):
        return self.fg

    def is_window(self, hwnd):
        return self.alive

    def window_pid(self, hwnd):
        return self.pid

    def is_minimized(self, hwnd):
        return self.minimized

    def restore(self, hwnd):
        self.calls.append("restore")
        self.minimized = False

    def bring_to_front(self, hwnd):
        self.calls.append("front")
        if self.fg == hwnd:
            return "already"
        if not self.front_ok:
            return ""
        self.fg = hwnd
        return "attach"

    def focus_state(self, spot):
        return self.focus if self.fg == spot.hwnd else "other"

    def set_focus(self, spot):
        self.calls.append("set_focus")
        if self.set_focus_works:
            self.focus = "same"

    def restore_caret(self, spot):
        self.calls.append("restore_caret")
        return self.caret_works

    def selection_exact(self, spot):
        return self.selection_kept

    def click_caret(self, spot):
        self.calls.append("click")
        if self.click_works:
            self.focus = "same"
        return self.click_works


def spot(**kw):
    base = dict(hwnd=A, pid=PID_A, exe="notepad.exe", app_name="Notepad", focus_hwnd=1100,
                focus_class="Edit", editable=True, win32_sel=(5, 5), sel_len=0,
                caret_pt=(300, 200))
    base.update(kw)
    return tf.Spot(**base)


# --- unchanged: zero behaviour change in the ordinary case -------------------

def test_unchanged_when_still_in_the_same_window_and_box():
    assert tf.unchanged(spot(), FakeOps(fg=A, focus="same"))


def test_unchanged_when_focus_cant_be_compared():
    # Same window, UIA can't say which box: never yank focus around for that.
    assert tf.unchanged(spot(), FakeOps(fg=A, focus="unknown"))


def test_changed_when_another_window_is_in_front():
    assert not tf.unchanged(spot(), FakeOps(fg=B, focus="same"))


def test_changed_when_another_box_in_the_same_window_has_focus():
    assert not tf.unchanged(spot(), FakeOps(fg=A, focus="other"))


# --- go_back ---------------------------------------------------------------

def test_window_closed():
    out = tf.go_back(spot(), FakeOps(alive=False))
    assert not out.ok and out.reason == "window closed"


def test_window_handle_reused_by_another_program_counts_as_closed():
    out = tf.go_back(spot(), FakeOps(pid=PID_A + 1))
    assert not out.ok and out.reason == "window closed"


def test_foreground_refused():
    ops = FakeOps(front_ok=False)
    out = tf.go_back(spot(), ops)
    assert not out.ok and out.reason == "foreground refused"
    assert "set_focus" not in ops.calls and "click" not in ops.calls


def test_minimized_window_is_restored_first():
    ops = FakeOps(minimized=True)
    out = tf.go_back(spot(), ops)
    assert out.ok and ops.calls[:2] == ["restore", "front"]
    assert "unminimized" in out.steps


def test_back_with_focus_and_exact_caret():
    ops = FakeOps()
    out = tf.go_back(spot(), ops)
    assert out.ok and out.reason == "exact"
    assert ops.fg == A
    assert ops.calls == ["front", "set_focus", "restore_caret"]


def test_already_focused_box_only_needs_the_caret():
    # Window came back with the right box focused: no SetFocus.
    ops = FakeOps(focus="same")
    out = tf.go_back(spot(), ops)
    assert out.ok and "set_focus" not in ops.calls


def test_caret_not_restorable_still_pastes_in_the_box():
    ops = FakeOps(caret_works=False)
    out = tf.go_back(spot(caret_pt=None), ops)
    assert out.ok and out.reason == "box (caret not restored)"


def test_caret_not_restorable_falls_back_to_clicking_where_it_was():
    ops = FakeOps(caret_works=False, click_works=True)
    out = tf.go_back(spot(), ops)
    assert out.ok and out.reason == "exact" and "caret:clicked" in out.steps


def test_focus_mismatch_that_cant_be_fixed_doesnt_paste():
    ops = FakeOps(set_focus_works=False, click_works=False)
    out = tf.go_back(spot(), ops)
    assert not out.ok and out.reason == "box not focused"
    assert "click" in ops.calls


def test_focus_fixed_by_a_click():
    ops = FakeOps(set_focus_works=False, click_works=True)
    out = tf.go_back(spot(), ops)
    assert out.ok and "clicked" in out.steps


def test_unknown_focus_after_going_back_is_not_good_enough():
    # Back in the window but UIA can't confirm the box: don't guess.
    ops = FakeOps(focus="unknown", set_focus_works=False)
    out = tf.go_back(spot(caret_pt=None), ops)
    assert not out.ok and out.reason == "box not focused"


def test_selection_edit_reselected_exactly():
    s = spot(win32_sel=(3, 9), sel_len=6)
    out = tf.go_back(s, FakeOps(), need_selection=True)
    assert out.ok and out.reason == "exact"


def test_selection_edit_without_exact_reselect_doesnt_paste():
    s = spot(win32_sel=(3, 9), sel_len=6)
    ops = FakeOps(caret_works=False, click_works=True)
    out = tf.go_back(s, ops, need_selection=True)
    assert not out.ok and out.reason == "highlight not re-selected"
    # A click can't recreate a highlight (and would collapse it): not tried.
    assert "click" not in ops.calls


def test_selection_edit_click_restores_focus_then_reselects():
    s = spot(win32_sel=(3, 9), sel_len=6)
    ops = FakeOps(set_focus_works=False, click_works=True)
    out = tf.go_back(s, ops, need_selection=True)
    assert out.ok
    assert ops.calls[-2:] == ["click", "restore_caret"]


def test_selection_edit_with_no_way_to_reselect_doesnt_paste():
    s = spot(win32_sel=None, range=None, sel_len=6, caret_pt=None)
    out = tf.go_back(s, FakeOps(), need_selection=True)
    assert not out.ok and out.reason == "highlight not re-selected"


# --- ready_to_paste (after the clipboard focus bounce) ----------------------

def test_ready_when_nothing_moved():
    assert tf.ready_to_paste(spot(), FakeOps(fg=A, focus="same")).ok


def test_ready_brings_the_window_back_once_more():
    ops = FakeOps(fg=B, focus="same")
    out = tf.ready_to_paste(spot(), ops)
    assert out.ok and ops.fg == A


def test_not_ready_if_window_refuses():
    out = tf.ready_to_paste(spot(), FakeOps(fg=B, front_ok=False))
    assert not out.ok and out.reason == "lost foreground"


def test_not_ready_if_highlight_lost_and_cant_be_reselected():
    s = spot(win32_sel=(3, 9), sel_len=6)
    ops = FakeOps(fg=A, focus="same", selection_kept=False, caret_works=False)
    out = tf.ready_to_paste(s, ops, need_selection=True)
    assert not out.ok and out.reason == "highlight lost"


def test_ready_reselects_a_lost_highlight():
    s = spot(win32_sel=(3, 9), sel_len=6)
    ops = FakeOps(fg=A, focus="same", selection_kept=False, caret_works=True)
    assert tf.ready_to_paste(s, ops, need_selection=True).ok


# --- small pure bits ---------------------------------------------------------

def test_spot_precise_hwnd_only_for_a_classic_edit_child():
    assert spot().precise_hwnd()
    assert not spot(focus_class="Chrome_RenderWidgetHostHWND").precise_hwnd()
    assert not spot(focus_hwnd=A).precise_hwnd()


def test_spot_describe_has_no_text():
    d = spot(sel_len=4).describe()
    assert "notepad.exe" in d and "sel_len=4" in d


@pytest.mark.parametrize("path,desc,title,want", [
    (r"C:\x\chrome.exe", "Google Chrome", "Inbox - Gmail", "Google Chrome"),
    (r"C:\x\notepad.exe", "", "Untitled", "Notepad"),
    (r"C:\x\weird.exe", "A very long description that goes on and on", "", "Weird"),
    (r"C:\Windows\ApplicationFrameHost.exe", "Application Frame Host", "Calculator",
     "Calculator"),
    ("", "", "", ""),
])
def test_app_display_name(path, desc, title, want):
    assert tf.app_display_name(path, desc, title) == want


# --- the listener's deliver() --------------------------------------------------

class InlineWorker:
    def run(self, fn, timeout=None):
        return fn(None)

    def submit(self, fn):
        fn(None)


class Target:
    def __init__(self, s, selection=""):
        self.spot, self.selection = s, selection


@pytest.fixture
def listener(monkeypatch):
    import background_listener as bl
    log = {"pasted_here": [], "paste": 0, "copy": [], "backspaces": [], "notice": [],
           "lines": []}
    monkeypatch.setattr(tf, "worker", lambda: InlineWorker())
    monkeypatch.setattr(bl, "_paste_here", lambda text, cleanup, private:
                        log["pasted_here"].append(cleanup))
    monkeypatch.setattr(bl, "_paste", lambda: log.__setitem__("paste", log["paste"] + 1))
    monkeypatch.setattr(bl, "_copy", lambda text, hwnd, private: log["copy"].append(hwnd))
    monkeypatch.setattr(bl, "_remove_hotkey_chars", lambda n: log["backspaces"].append(n))
    monkeypatch.setattr(bl, "_log", lambda msg: log["lines"].append(msg))
    monkeypatch.setattr(bl, "CRD_SYNC_DELAY", 0)
    monkeypatch.setattr(bl, "HOTKEY", "`")

    class Overlay:
        def notice_safe(self, title, detail="", seconds=6):
            log["notice"].append((title, detail, seconds))
    monkeypatch.setattr(bl, "overlay", Overlay())
    return bl, log


def use_ops(monkeypatch, ops, before=None):
    ops.text_before_caret = lambda n: before
    monkeypatch.setattr(tf, "LiveOps", lambda ins: ops)


def test_deliver_unchanged_uses_the_old_path(listener, monkeypatch):
    bl, log = listener
    use_ops(monkeypatch, FakeOps(fg=A, focus="same"))
    bl.deliver("hello", 2, 1, Target(spot()), {})
    assert log["pasted_here"] == [2] and log["paste"] == 0 and not log["notice"]
    assert not log["lines"]       # nothing to log in the ordinary case


def test_deliver_without_a_spot_uses_the_old_path(listener):
    bl, log = listener
    bl.deliver("hello", 0, 0, Target(None), {})
    assert log["pasted_here"] == [0]


def test_deliver_started_outside_a_text_box_pastes_where_you_are(listener, monkeypatch):
    bl, log = listener
    use_ops(monkeypatch, FakeOps(fg=B))
    bl.deliver("hello", 0, 0, Target(spot(editable=False)), {})
    assert log["pasted_here"] == [0]


def test_deliver_goes_back_and_pastes(listener, monkeypatch):
    bl, log = listener
    ops = FakeOps(fg=B)
    use_ops(monkeypatch, ops)
    bl.deliver("hello", 0, 0, Target(spot()), {})
    assert ops.fg == A
    assert log["copy"] == [A] and log["paste"] == 1 and not log["pasted_here"]
    assert not log["notice"]
    assert all("hello" not in line for line in log["lines"])   # no dictated text


def test_deliver_cant_go_back_leaves_text_on_clipboard_and_says_so(listener, monkeypatch):
    bl, log = listener
    ops = FakeOps(fg=B, alive=False)
    use_ops(monkeypatch, ops)
    bl.deliver("hello", 0, 0, Target(spot()), {"CLIPBOARD_PRIVATE": "1"})
    assert log["paste"] == 0 and not log["pasted_here"]
    # Clipboard written (even in private mode), focus left where it is now.
    assert len(log["copy"]) == 1 and log["copy"][0] != A
    (title, detail, secs), = log["notice"]
    assert title == "Couldn't get back to Notepad."
    assert "Ctrl+V" in detail and secs == 6


def test_deliver_selection_edit_without_reselect_doesnt_paste(listener, monkeypatch):
    bl, log = listener
    use_ops(monkeypatch, FakeOps(fg=B, caret_works=False))
    s = spot(win32_sel=(3, 9), sel_len=6)
    bl.deliver("edited", 0, 0, Target(s, selection="orig t"), {})
    assert log["paste"] == 0
    assert log["notice"][0][0].startswith("Couldn't re-select your highlighted text")


def test_deliver_not_ready_after_bounce_doesnt_paste(listener, monkeypatch):
    bl, log = listener
    ops = FakeOps(fg=B)
    use_ops(monkeypatch, ops)
    monkeypatch.setattr(bl, "_copy", lambda text, hwnd, private: (
        log["copy"].append(hwnd), setattr(ops, "fg", B), setattr(ops, "front_ok", False)))
    bl.deliver("hello", 0, 0, Target(spot()), {})
    assert log["paste"] == 0 and log["notice"]


def test_deliver_removes_hotkey_chars_only_when_confirmed(listener, monkeypatch):
    bl, log = listener
    use_ops(monkeypatch, FakeOps(fg=B), before="`")
    # backtick start (in A) + backtick stop (where you were): 1 + 1
    bl.deliver("hello", 2, 1, Target(spot()), {})
    assert log["backspaces"] == [1, 1] and log["paste"] == 1


def test_deliver_leaves_unconfirmed_hotkey_chars(listener, monkeypatch):
    bl, log = listener
    use_ops(monkeypatch, FakeOps(fg=B), before="x")
    bl.deliver("hello", 2, 1, Target(spot()), {})
    assert log["backspaces"] == [] and log["paste"] == 1


def test_notice_message_wording():
    import background_listener as bl
    assert bl.couldnt_go_back_message("", False) == (
        "Couldn't get back to the window you were in.",
        "Your text is on the clipboard: press Ctrl+V to paste it.")
    t, _ = bl.couldnt_go_back_message("Word", True, "window closed")
    assert t == "Couldn't get back to Word."
