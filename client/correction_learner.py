"""Notice when you fix a word we just typed, and offer to remember the fix.

After a dictation is pasted, the listener watches that text box for a short
while (correction_watch in background_listener.py). If you change one word
into a similar one ("Corey" into "Cory"), it shows

    Corey  ->  Cory
    Always make this correction?      [Always]  [Not now]  [Never for this]

"Always" saves it as a name correction (the same list as Settings > Words &
Triggers). Nothing is ever saved without that click.

This module is the pure part: where the pasted text sits in the box, what
changed inside it, and whether a change looks like a fixed mishearing and not
a rewrite. The approach follows OpenWhispr's correction learner (MIT): a
word-level comparison, with filters that drop common words, tense or plural
changes, numbers and anything where too much changed.

Text from the box is only compared in memory. It is never logged or stored;
only a pair you answer "Never for this" is written down (so it isn't offered
again), in client/corrections_skipped.json, which git ignores.
"""
import difflib
import json
import os
import re

WATCH_SECONDS = 45          # how long the box is watched after a paste
POLL_SECONDS = 0.7
QUIET_SECONDS = 2.5         # no edits for this long: look at what changed
FIELD_MAX = 12000           # longer boxes aren't watched (documents, logs)
PASTED_MIN_WORDS = 2        # a one-word dictation has nothing to anchor on
MAX_OFFERS = 2              # per dictation
MIN_LETTERS = 3
MAX_WORDS_EACH_SIDE = 3     # "new york" -> "New York City" is the outer limit
MAX_CHANGED_SHARE = 0.5     # more of the dictation than this changed: a rewrite
MIN_SIMILARITY = 0.35       # of the two spellings (0 = nothing alike, 1 = same)

SKIPPED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corrections_skipped.json")
ENV_KEY = "LEARN_CORRECTIONS"          # .env: "false" switches the offers off

# Words too ordinary to be a mishearing worth a permanent rule.
COMMON = frozenset("""
a about after all also am an and any are as at be because been before but by can could
did do does done down each even few for from get go going good got had has have he her
here him his how i if in into is it its just know like made make many may me more most
much my new no not now of off on one only or other our out over own said same see she
should so some such than that the their them then there these they this those through to
too two under up us very was way we well were what when where which while who why will
with would yes you your gonna wanna kinda yeah okay ok
""".split())
_SUFFIXES = ("ing", "ed", "es", "s", "ly", "er", "est", "'s", "’s", "n't", "'re", "'ve", "'ll", "'d")


def enabled(env):
    return (env.get(ENV_KEY) or "true").strip().lower() not in ("0", "false", "no", "off")


def _tokens(text):
    """Words with their punctuation stripped, in order."""
    return re.findall(r"[^\W_]+(?:['’\-][^\W_]+)*", text or "")


def locate(pasted, field):
    """Where the pasted text sits in the box: (prefix, suffix) around it, or
    None when it isn't there exactly once (nothing to watch then)."""
    if not pasted or not field or len(field) > FIELD_MAX:
        return None
    if len(_tokens(pasted)) < PASTED_MIN_WORDS:
        return None
    i = field.find(pasted)
    if i < 0 or field.find(pasted, i + 1) >= 0:
        return None
    return field[:i], field[i + len(pasted):]


def region(anchors, field):
    """The pasted part of the box as it reads now, or None when the text
    around it changed too (typing elsewhere, the message sent, the box cleared)."""
    prefix, suffix = anchors
    if field is None or len(field) > FIELD_MAX:
        return None
    if not field.startswith(prefix) or not field.endswith(suffix):
        return None
    if len(field) < len(prefix) + len(suffix):
        return None
    return field[len(prefix):len(field) - len(suffix)]


def _stem(word):
    w = word.lower()
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _worth_learning(heard, fixed):
    """One changed stretch: does it look like a mishearing that was fixed?"""
    h, f = " ".join(heard), " ".join(fixed)
    if h.lower() == f.lower():
        return False                                  # capital letters only
    if not (1 <= len(heard) <= MAX_WORDS_EACH_SIDE and 1 <= len(fixed) <= MAX_WORDS_EACH_SIDE):
        return False
    if any(ch.isdigit() for ch in h + f):
        return False                                  # numbers are facts, not spellings
    letters = [c for c in f if c.isalpha()]
    if len(letters) < MIN_LETTERS or len([c for c in h if c.isalpha()]) < 2:
        return False
    if all(w.lower() in COMMON for w in fixed) or all(w.lower() in COMMON for w in heard):
        return False                                  # a change of wording
    if len(heard) == len(fixed) and all(_stem(a) == _stem(b) for a, b in zip(heard, fixed)):
        return False                                  # walk -> walked, cat -> cats
    return _similar(h, f) >= MIN_SIMILARITY


def substitutions(pasted, edited, limit=MAX_OFFERS):
    """[(heard, corrected)] for the words that were fixed inside the pasted
    text, at most `limit`. [] for no change, a rewrite, or nothing that looks
    like a mishearing."""
    if pasted is None or edited is None or pasted == edited:
        return []
    old, new = _tokens(pasted), _tokens(edited)
    if not old or not new:
        return []
    sm = difflib.SequenceMatcher(None, [w.lower() for w in old], [w.lower() for w in new],
                                 autojunk=False)
    changed, found = 0, []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            continue
        changed += max(a1 - a0, b1 - b0)
        if op == "replace" and _worth_learning(old[a0:a1], new[b0:b1]):
            found.append((" ".join(old[a0:a1]), " ".join(new[b0:b1])))
    if changed > max(1, len(old)) * MAX_CHANGED_SHARE:
        return []                                      # too much changed: a rewrite
    out = []
    for pair in found:
        if pair not in out:
            out.append(pair)
    return out[:limit]


def new_pairs(pairs, corrections=None, skipped=None):
    """Drop pairs that are already a correction, or were answered "Never"."""
    known = {(k or "").lower(): (v or "") for k, v in (corrections or {}).items()}
    never = {(h.lower(), c) for h, c in (skipped or [])}
    return [(h, c) for h, c in pairs
            if known.get(h.lower()) != c and (h.lower(), c) not in never]


# --- "Never for this" ------------------------------------------------------------

def load_skipped(path=SKIPPED_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [(str(h), str(c)) for h, c in data if h and c]
    except Exception:
        return []


def add_skipped(pair, path=SKIPPED_PATH, limit=500):
    """Remember that this pair must not be offered again. Never raises."""
    items = [p for p in load_skipped(path) if (p[0].lower(), p[1]) != (pair[0].lower(), pair[1])]
    items.append((pair[0], pair[1]))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump([list(p) for p in items[-limit:]], f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# --- the offer's words -------------------------------------------------------------

OFFER_QUESTION = "Always make this correction?"
OFFER_BUTTONS = ("Always", "Not now", "Never for this")
OFFER_SECONDS = 12


def offer_line(pair):
    """"Corey  →  Cory" (what was typed, what you changed it to)."""
    return "%s  →  %s" % pair


def saved_message(pair):
    return ("Saved: “%s” will be typed as “%s”." % pair,
            "Change or remove it in Settings > Words.")


# --- one watch, step by step (no clock or screen in here: testable) ----------------

class Watch:
    """Feed it what the box reads at each look; it says when to stop and what
    to offer. States: "watching" until done; then `pairs` holds the result."""

    def __init__(self, pasted, first_field, now):
        self.pasted = pasted
        self.anchors = locate(pasted, first_field)
        self.last = pasted                 # the pasted part as it last read
        self.changed_at = None
        self.started = now
        self.done = self.anchors is None
        self.pairs = []

    def look(self, field, now, still_there=True):
        """field: the box's text now (None if it can't be read). still_there:
        the same window and box still have focus. Returns True when finished."""
        if self.done:
            return True
        current = region(self.anchors, field) if still_there else None
        if current is None:
            return self._finish()          # sent, cleared, clicked away, or typing elsewhere
        if current != self.last:
            self.last, self.changed_at = current, now
        elif self.changed_at is not None and now - self.changed_at >= QUIET_SECONDS:
            return self._finish()
        if now - self.started >= WATCH_SECONDS:
            return self._finish()
        return False

    def _finish(self):
        self.done = True
        self.pairs = substitutions(self.pasted, self.last)
        return True
