"""Optional removal of filler SOUNDS ("um", "uh") from dictated text.

Off by default (REMOVE_FILLERS=false). Deliberately narrow, because losing a
word the user meant to say costs far more than an "um" they can delete:

- Only sounds that are never real words: um, umm, uh, uhh, hmm. NOT "like",
  "you know", "so", "er" (the ER) or "mm" (millimetres).
- Only standalone tokens: "uh-oh", "umbrella", "UM" (all caps, e.g. a name or
  acronym) are left alone.
- Everything removed is returned so the caller can log it for auditing.

Whisper already drops most fillers on its own (it was trained on cleaned-up
transcripts), so this only catches the ones that slip through.
"""
import re

DEFAULT_FILLERS = ("um", "umm", "uh", "uhh", "hmm")


def _pattern(words):
    alts = "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))
    # The filler, optionally wrapped in commas/ellipses, as its own token:
    # not touching a letter, digit, apostrophe or hyphen (so "uh-oh" survives).
    return re.compile(
        r"(?P<lead>,\s*)?(?<![\w'\-])(?P<word>" + alts + r")(?:\.\.\.|…)?"
        r"(?P<trail>\s*,)?(?![\w'\-])",
        re.IGNORECASE)


def remove_fillers(text, words=DEFAULT_FILLERS):
    """Return (cleaned_text, removed_words)."""
    if not text or not words:
        return text, []
    removed = []

    def repl(m):
        word = m.group("word")
        if word.isupper() and len(word) > 1:  # "UM", "UH": likely an acronym
            return m.group(0)
        removed.append(word)
        # "I, um, think" -> "I think" (drop both commas); "um, so" -> "so".
        if m.group("lead") and m.group("trail"):
            return ""
        if m.group("lead"):
            return ","  # "yes, um" -> "yes," ; tidied below
        return ""

    out = _pattern(words).sub(repl, text)
    if not removed:
        return text, []
    out = re.sub(r"\s{2,}", " ", out)                 # collapse gaps
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)        # no space before punctuation
    out = re.sub(r",\s*([.!?])", r"\1", out)           # "yes,." -> "yes."
    out = re.sub(r"^[\s,]+", "", out).strip()
    # Re-capitalize a sentence whose first word was the filler: "Um, so" -> "So".
    out = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)
    return out, removed
