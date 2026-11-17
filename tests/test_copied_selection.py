"""The highlight read by copying it (text_focus.copied_selection), for
programs like Word that don't say what is highlighted. Fake keyboard and
clipboard: nothing real is pressed or copied."""
import text_focus as tf


class FakeOps:
    def __init__(self, highlighted="", answers_after=2, modifier=False, fails=False):
        self.highlighted, self.answers_after = highlighted, answers_after
        self.modifier, self.fails = modifier, fails
        self.clip, self.count, self.pressed, self.polls = "what you copied earlier", 7, 0, 0

    def modifier_down(self):
        return self.modifier

    def counter(self):
        if self.fails:
            raise OSError("no clipboard")
        if self.pressed and self.highlighted is not None:
            self.polls += 1
            if self.polls >= self.answers_after and self.count == 7:
                self.count, self.clip = 8, self.highlighted
        return self.count

    def press_copy(self):
        self.pressed += 1

    def text(self):
        return self.clip

    def sleep(self, seconds):
        pass


def test_a_highlight_is_found_and_reported_by_length_only():
    ops = FakeOps("Dear Sir, lunch at noon.")
    text, note = tf.copied_selection(ops)
    assert text == "Dear Sir, lunch at noon."
    assert note == "found 24 chars" and "lunch" not in note
    assert ops.pressed == 1


def test_nothing_highlighted_leaves_the_clipboard_alone():
    ops = FakeOps(highlighted=None)
    assert tf.copied_selection(ops) == ("", "nothing highlighted")
    assert ops.clip == "what you copied earlier" and ops.count == 7


def test_no_copy_while_a_modifier_key_is_held():
    ops = FakeOps("text", modifier=True)
    text, note = tf.copied_selection(ops)
    assert text == "" and note.startswith("skipped") and ops.pressed == 0


def test_a_copied_picture_or_a_huge_highlight_is_not_used():
    assert tf.copied_selection(FakeOps("   "))[0] == ""
    text, note = tf.copied_selection(FakeOps("x" * (tf.COPY_MAX_CHARS + 1)))
    assert text == "" and "over" in note


def test_never_raises():
    assert tf.copied_selection(FakeOps("x", fails=True)) == ("", "failed (OSError)")


def test_when_the_copy_is_tried():
    want = tf.copy_check_wanted
    # Word: by the button or a hotkey, whatever we were told.
    assert want("button", "winword.exe", "_WwG", "document not editable", False)
    assert want("hotkey", "WINWORD.EXE", "_WwG", "nothing highlighted", True)
    # The button, in a box that wouldn't say.
    assert want("button", "someeditor.exe", "Custom", "not a text field", False)
    # An app that answered "nothing highlighted" is believed.
    assert not want("button", "notepad.exe", "RichEditD2DPT", "nothing highlighted", True)
    # A hotkey can be pressed anywhere: no Ctrl+C into an unknown window.
    assert not want("hotkey", "someeditor.exe", "Custom", "not a text field", False)
    # Ctrl+C stops a program in a terminal and copies a line in code editors.
    for exe, cls in (("WindowsTerminal.exe", "CASCADIA_HOSTING_WINDOW_CLASS"),
                     ("cmd.exe", "ConsoleWindowClass"), ("Code.exe", "Chrome_WidgetWin_1"),
                     ("unknown.exe", "ConsoleWindowClass"), ("excel.exe", "EXCEL7")):
        assert not want("button", exe, cls, "not a text field", False)
    # Never in a password or sign-in box, or a fully highlighted search box.
    for reason in ("password", "login", "login form", "whole box"):
        assert not want("button", "winword.exe", "_WwG", reason, False)
