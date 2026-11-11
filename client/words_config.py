"""Read and write the server's word lists (api/processing_config.json) for the
Settings window's "Words & Triggers" page.

The file also holds trigger regexes, the insults path and _comment_* notes;
saving only replaces `vocabulary` and `name_corrections` and keeps every other
key exactly as it was. A fresh install has no processing_config.json yet, so the
first save starts from processing_config.example.json.
"""
import json
import os

API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")
CONFIG_PATH = os.path.join(API_DIR, "processing_config.json")
EXAMPLE_PATH = os.path.join(API_DIR, "processing_config.example.json")

# Whisper reads about 224 tokens of hints; names and jargon split into short
# tokens (~2.4 characters each), so roughly this many characters fit.
HINT_BUDGET_CHARS = 540


def load(path=None, example=None):
    path, example = path or CONFIG_PATH, example or EXAMPLE_PATH
    for p in (path, example):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return {}


def clean_vocabulary(lines):
    """Trimmed, non-empty, de-duplicated (case-insensitive), order kept."""
    seen, out = set(), []
    for w in lines:
        w = w.strip().strip(",")
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def clean_corrections(pairs):
    """[(heard, write_as), ...] -> ({heard: write_as}, error or None)."""
    out, seen = {}, set()
    for heard, write_as in pairs:
        heard, write_as = heard.strip(), write_as.strip()
        if not heard and not write_as:
            continue  # an empty row the user added and never filled in
        if not heard or not write_as:
            return None, "Name corrections: fill in both sides, or remove the row."
        if heard.lower() in seen:
            return None, "Name corrections: '%s' is listed twice." % heard
        seen.add(heard.lower())
        out[heard] = write_as
    return out, None


def hint_chars(vocabulary, corrections):
    """Characters of hints the server sends Whisper (vocabulary + every
    correction target, as the server builds them)."""
    words = list(dict.fromkeys(list(vocabulary) + list(corrections.values())))
    return len(", ".join(words))


def save(vocabulary, corrections, path=None, example=None):
    """Replace the two lists, keep everything else. Atomic write."""
    path = path or CONFIG_PATH
    data = load(path, example)
    data["vocabulary"] = list(vocabulary)
    data["name_corrections"] = dict(corrections)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def trigger_summary(config):
    """Plain-English description of the triggers, for display."""
    return {
        "edit": len(config.get("trigger_patterns", [])),
        "mean": len(config.get("roast_trigger_patterns", [])),
    }
