"""Corrections and shortcuts (processing_config.json "name_corrections").

Each entry replaces what Whisper wrote (left) with what you want (right):
  - a correction:  "Leslie" -> "Lesley"
  - a shortcut:    "here's my link" -> "here's my link: cyrus-studio.com"

Correction targets are also handed to Whisper as spelling hints, so it gets
the name right before the replacement even runs. Shortcut targets are not:
a phone number, link or address isn't a word to listen for, and Whisper only
reads a limited amount of hints.

Pure module: the server imports it from api/, the Settings window from client/.
"""
import re

HEARD_MAX = 60     # the "when I say" side
TARGET_MAX = 200   # the "type this" side: room for an address or a sentence

_DOMAIN = re.compile(r"\b[\w-]+\.(?:com|net|org|io|dev|app|co|us|uk|ca|me|ai|gg|tv|info|biz)\b",
                     re.IGNORECASE)


def is_spelling_hint(target):
    """True for a name-like target (worth biasing Whisper toward): at most three
    words and 30 characters, no digits, no email, link or web address."""
    t = (target or "").strip()
    if not t or len(t) > 30 or len(t.split()) > 3:
        return False
    if any(c.isdigit() for c in t) or "@" in t or "/" in t or "," in t:
        return False
    return not _DOMAIN.search(t)


def spelling_hints(corrections):
    """The correction targets that are spelling hints, in order, once each."""
    return list(dict.fromkeys(v.strip() for v in (corrections or {}).values()
                              if is_spelling_hint(v)))
