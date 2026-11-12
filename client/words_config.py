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


def merge_vocabulary(page, loaded, disk):
    """Apply what the user changed on the page (page vs loaded) on top of what
    is on disk now. The phone may have added or removed words meanwhile
    (POST /v1/vocabulary); those changes are kept unless the user touched the
    same word."""
    base = {w.lower() for w in loaded}
    on_disk = {w.lower() for w in disk}
    page_low = {w.lower() for w in page}
    removed_elsewhere = base - on_disk
    out = [w for w in page if not (w.lower() in base and w.lower() in removed_elsewhere)]
    out += [w for w in disk if w.lower() not in base and w.lower() not in page_low]
    return clean_vocabulary(out)


def merge_corrections(page, loaded, disk):
    """Same idea for name corrections ("heard as" compared case-insensitively).
    A pair the user left untouched follows the disk (changed or removed on the
    phone); a pair the user edited or added wins; pairs added elsewhere are kept."""
    low = lambda d: {k.lower(): (k, v) for k, v in d.items()}
    base, now, mine = low(loaded), low(disk), low(page)
    out = {}
    for key, (k, v) in mine.items():
        untouched = key in base and base[key] == (k, v)
        if untouched:
            if key in now:
                out[now[key][0]] = now[key][1]
            # else: removed elsewhere, stays removed
        else:
            out[k] = v
    for key, (k, v) in now.items():
        if key not in base and key not in mine:
            out[k] = v
    return out


def save(vocabulary, corrections, path=None, example=None, loaded_vocabulary=None,
         loaded_corrections=None):
    """Replace the two lists, keep everything else. Atomic write.

    loaded_vocabulary / loaded_corrections: the lists as they were when the page
    opened. With them, only the user's own changes are applied on top of what's
    on disk now, so edits the phone made meanwhile (POST /v1/vocabulary,
    /v1/name_corrections) aren't erased by saving Settings."""
    path = path or CONFIG_PATH
    data = load(path, example)
    if loaded_vocabulary is not None:
        vocabulary = merge_vocabulary(list(vocabulary), loaded_vocabulary,
                                      data.get("vocabulary", []))
    if loaded_corrections is not None:
        corrections = merge_corrections(dict(corrections), loaded_corrections,
                                        data.get("name_corrections", {}))
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
