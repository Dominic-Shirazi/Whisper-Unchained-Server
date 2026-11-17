"""What happens to highlighted words when a recording starts over them: the
phone app's highlight chip and sheet (FloatingButtonService.kt, F-65 / F-66 /
F-66b) on Windows. The pure parts: no windows here (style_pill.py draws the
chip and the sheet, background_listener.py sends the recording).

A recording that starts with words highlighted shows a small chip where the
style pill normally sits:

        (pencil) 113 words     the highlight will be rewritten: what you say
                               is used as directions for your AI
        113 words (struck)     the highlight will be typed over: what you say
                               is ordinary dictation and replaces it

Click the chip for a sheet that explains both and lets you pick one. The
choice is for that one recording; the next one starts on Rewrite again. Where
there is no AI to rewrite with (a cloud service transcribes), there is no
choice: the count is struck through and the sheet says why.
"""
import threading

REWRITE, TYPE_OVER = "rewrite", "type_over"
MODES = (REWRITE, TYPE_OVER)
PENCIL = chr(0x270E)     # the pencil sign

# The sheet's words (the same as the phone's).
REWRITE_TITLE = "Rewrite them"
REWRITE_BODY = ("What you say is used as directions. The highlighted words and your "
                "directions are sent to your AI, and the answer replaces them.")
TYPE_OVER_TITLE = "Type over them"
TYPE_OVER_BODY = "What you say replaces the highlighted words, as normal dictation."
NO_AI_BODY = ("What you say replaces the highlighted words. Rewriting needs your PC's AI, "
              "which this mode does not use.")
OPTIONS = ((REWRITE, REWRITE_TITLE, REWRITE_BODY), (TYPE_OVER, TYPE_OVER_TITLE, TYPE_OVER_BODY))

# The notice when a rewrite comes back the same as the highlight.
NO_CHANGES = "The AI made no changes."


def word_count(text):
    """Words in the highlight: whatever white space separates."""
    return len((text or "").split())


def words_label(count):
    return "1 word" if count == 1 else "%d words" % count


def chip_text(count, rewrite):
    """What the chip says: a pencil and the count for a rewrite; only the
    count (drawn struck through, see `struck`) when it will be typed over."""
    return (PENCIL + " " if rewrite else "") + words_label(count)


def sheet_title(count):
    return "Your 1 highlighted word" if count == 1 else "Your %d highlighted words" % count


def path_for(highlight, mode=REWRITE, can_rewrite=True):
    """Where a finished recording goes: "rewrite" (what was said is directions
    for the AI, the answer replaces the highlight) or "dictate" (ordinary
    dictation, pasted over the highlight if there is one)."""
    if word_count(highlight) and can_rewrite and mode != TYPE_OVER:
        return "rewrite"
    return "dictate"


def no_changes(answer, highlight, edited=True):
    """True when a rewrite must not be pasted: the server says nothing was
    edited, or the answer is the highlight again (white space at the ends
    aside)."""
    return (not edited) or (answer or "").strip() == (highlight or "").strip()


class HighlightChoice:
    """One recording's choice for its highlight. Changed on the mic button's
    thread (the sheet), read by the listener when the recording is sent."""

    def __init__(self, count, can_rewrite=True):
        self._lock = threading.Lock()
        self.count = int(count)
        self.can_rewrite = bool(can_rewrite)
        self.mode = REWRITE           # every recording starts on Rewrite
        self.closed = False
        self.version = 0

    @classmethod
    def for_highlight(cls, text, can_rewrite=True):
        """The choice for this highlight, or None when it has no words."""
        n = word_count(text)
        return cls(n, can_rewrite) if n else None

    @property
    def rewrite(self):
        """True while the highlight is going to be rewritten."""
        return self.can_rewrite and self.mode == REWRITE

    @property
    def struck(self):
        return not self.rewrite

    def choose(self, mode):
        """A choice in the sheet. False when there is nothing to choose (no
        AI to rewrite with) or the recording has stopped."""
        with self._lock:
            if self.closed or not self.can_rewrite or mode not in MODES:
                return False
            if mode != self.mode:
                self.mode = mode
                self.version += 1
            return True

    def close(self):
        """The recording stopped: the choice is final."""
        with self._lock:
            self.closed = True
            self.version += 1

    def chip_text(self):
        return chip_text(self.count, self.rewrite)

    def title(self):
        return sheet_title(self.count)

    def describe(self):
        """For the log: the count and the choice, never the words."""
        return "%s, %s" % (words_label(self.count),
                           "rewrite" if self.rewrite else
                           ("type over" if self.can_rewrite else "type over (no AI here)"))
