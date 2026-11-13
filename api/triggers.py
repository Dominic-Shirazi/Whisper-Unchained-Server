"""Trigger phrases: plain words the speaker says to hand a dictation to an LLM.

processing_config.json holds them as rows a non-technical user can edit in
Settings:

    "trigger_phrases": [{"phrase": "prompt AI", "action": "edit"}, ...]

action is "edit" (the AI editor follows the spoken instruction), "mean" (mean
mode) or "end" (marks the end of the spoken instruction, like "end prompt").

Each phrase becomes a case-insensitive regex (phrase_to_pattern): words may run
together or be separated by up to three spaces/punctuation marks, and an
ALL-CAPS word of 2-5 letters as typed ("AI", "GPT") is an acronym whose letters
may also be spelled out the way Whisper writes them ("a.i.", "A. Eye").

Files written before trigger_phrases existed keep their regex lists
(trigger_patterns, roast_trigger_patterns); patterns() reads those exactly as
the server always has until the user saves trigger phrases from Settings.

Pure module (no FastAPI): the server imports it from api/, the Settings window
from client/ via sys.path.
"""
import re

ACTIONS = ("edit", "mean", "end")
ACTION_LABELS = {"edit": "AI edit", "mean": "Mean mode", "end": "End of instruction"}

PHRASE_MIN, PHRASE_MAX = 2, 60

# How each letter of an acronym may come out of Whisper: the letter itself or
# its spoken name. Deliberately short: common spellings only, no fuzzy matching.
LETTER_NAMES = {
    "A": ("a", "ay", "eh"),
    "B": ("b", "bee"),
    "C": ("c", "see", "cee"),
    "D": ("d", "dee"),
    "E": ("e", "ee"),
    "F": ("f", "ef", "eff"),
    "G": ("g", "gee"),
    "H": ("h", "aitch"),
    "I": ("i", "eye"),
    "J": ("j", "jay"),
    "K": ("k", "kay"),
    "L": ("l", "el", "ell"),
    "M": ("m", "em"),
    "N": ("n", "en"),
    "O": ("o", "oh"),
    "P": ("p", "pee"),
    "Q": ("q", "cue", "queue"),
    "R": ("r", "ar"),
    "S": ("s", "es", "ess"),
    "T": ("t", "tee"),
    "U": ("u", "you"),
    "V": ("v", "vee"),
    "W": ("w", r"double[\W_]{0,2}you", r"double[\W_]{0,2}u"),
    "X": ("x", "ex"),
    "Y": ("y", "why"),
    "Z": ("z", "zee", "zed"),
}

WORD_GAP = r"[\W_]{0,3}"    # between words: "promptai", "prompt, AI", "prompt-AI"
LETTER_GAP = r"[\W_]{0,2}"  # between acronym letters: "a.i", "A. Eye", "a eye"

# Today's behaviour as plain rows. The old regexes also allowed "to" before
# roast and "some" before disses, and the AI-prefixed forms; each is its own
# row here. "tik tok" also matches "tiktok"/"tik-tok" (words may run together).
DEFAULT_PHRASES = [
    {"phrase": "prompt AI", "action": "edit"},
    {"phrase": "prompte I", "action": "edit"},
    {"phrase": "end prompt", "action": "end"},
    {"phrase": "prompt roast", "action": "mean"},
    {"phrase": "prompt to roast", "action": "mean"},
    {"phrase": "prompt AI roast", "action": "mean"},
    {"phrase": "prompt AI to roast", "action": "mean"},
    {"phrase": "prompt tik tok", "action": "mean"},
    {"phrase": "prompt tick tock", "action": "mean"},
    {"phrase": "prompt AI tik tok", "action": "mean"},
    {"phrase": "prompt AI tick tock", "action": "mean"},
    {"phrase": "prompt diss", "action": "mean"},
    {"phrase": "prompt disses", "action": "mean"},
    {"phrase": "prompt some disses", "action": "mean"},
    {"phrase": "prompt AI diss", "action": "mean"},
    {"phrase": "prompt AI disses", "action": "mean"},
    {"phrase": "prompt AI some disses", "action": "mean"},
    {"phrase": "prompt mean mode", "action": "mean"},
    {"phrase": "prompt meme mode", "action": "mean"},
    {"phrase": "prompt AI mean mode", "action": "mean"},
    {"phrase": "prompt AI meme mode", "action": "mean"},
]


def _trim(phrase):
    """Whitespace and leading/trailing punctuation off ('"Jarvis!"' -> 'Jarvis'),
    so a phrase always starts and ends on a letter or digit."""
    return re.sub(r"^[\W_]+|[\W_]+$", "", (phrase or "").strip())


def _is_acronym(word):
    return 2 <= len(word) <= 5 and word.isascii() and word.isalpha() and word.isupper()


def _letter(ch):
    names = sorted(LETTER_NAMES[ch], key=len, reverse=True)
    return "(?:" + "|".join(names) + ")"


def phrase_to_pattern(phrase):
    """Regex (use with re.IGNORECASE) for one trigger phrase. Matching whole
    words is up to the caller (the server wraps patterns in \\b...\\b; a trimmed
    phrase always starts and ends on a letter or digit, so that works)."""
    parts = []
    for word in _trim(phrase).split():
        if _is_acronym(word):
            parts.append(LETTER_GAP.join(_letter(c) for c in word))
        else:
            parts.append(re.escape(word))
    return WORD_GAP.join(parts)


def _legacy(config):
    edit = [p for p in config.get("trigger_patterns", []) if "end" not in p.lower()]
    end = [p for p in config.get("trigger_patterns", []) if "end" in p.lower()]
    return {"edit": edit, "mean": list(config.get("roast_trigger_patterns", [])), "end": end}


def uses_phrases(config):
    return isinstance(config.get("trigger_phrases"), list)


def patterns(config):
    """{"edit": [...], "mean": [...], "end": [...]} regex strings. From
    trigger_phrases when the config has them, otherwise the legacy regex lists
    (edit = trigger_patterns without "end" in them, end = those with it,
    mean = roast_trigger_patterns)."""
    if not uses_phrases(config):
        return _legacy(config)
    out = {a: [] for a in ACTIONS}
    for row in config["trigger_phrases"]:
        if not isinstance(row, dict) or row.get("action") not in ACTIONS:
            continue
        pat = phrase_to_pattern(row.get("phrase", ""))
        if pat and pat not in out[row["action"]]:
            out[row["action"]].append(pat)
    return out


def rows(config):
    """The rows Settings should show (copies, safe to edit)."""
    src = config.get("trigger_phrases") if uses_phrases(config) else DEFAULT_PHRASES
    return [{"phrase": r.get("phrase", ""), "action": r.get("action", "edit")}
            for r in src if isinstance(r, dict)]


def validate(rows_in):
    """(clean_rows, None) or (None, plain-English error for the status bar).
    Accepts dicts {"phrase", "action"} or (phrase, action) pairs."""
    out, seen = [], set()
    for row in rows_in:
        if isinstance(row, dict):
            phrase, action = row.get("phrase", ""), row.get("action", "")
        else:
            phrase, action = row
        raw = (phrase or "").strip()
        action = (action or "").strip()
        if not raw:
            continue  # an empty row the user added and never filled in (the
            # action drop-down always has a value, so the phrase decides)
        if "\n" in raw or "\r" in raw or "," in raw:
            return None, 'Trigger phrase "%s" can\'t contain commas or line breaks.' % raw[:60]
        phrase = re.sub(r"\s+", " ", _trim(raw))
        if not re.search(r"[^\W\d_]", phrase):
            return None, 'Trigger phrase "%s" needs at least one letter.' % raw[:60]
        if len(phrase) < PHRASE_MIN:
            return None, 'Trigger phrase "%s" is too short (%d letters minimum).' % (raw, PHRASE_MIN)
        if len(phrase) > PHRASE_MAX:
            return None, 'Trigger phrase "%s..." is too long (%d characters maximum).' % (
                raw[:30], PHRASE_MAX)
        if action not in ACTIONS:
            return None, 'Trigger phrase "%s": choose what it does (%s).' % (
                phrase, ", ".join(ACTION_LABELS[a] for a in ACTIONS))
        if phrase.lower() in seen:
            return None, 'Trigger phrase "%s" is listed twice.' % phrase
        seen.add(phrase.lower())
        out.append({"phrase": phrase, "action": action})
    return out, None
