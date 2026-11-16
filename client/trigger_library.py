"""The shared trigger library, for the Settings window.

The library is one public file in this project's repository
(trigger_library/library.json). Settings downloads it when the library is
opened, so new triggers appear without an update; if the download fails (no
internet, GitHub unreachable) the copy that came with the app is shown instead.

Nothing about the user is sent: the download is a plain GET of a public file.
Sharing a trigger opens a pre-filled form in the browser, and only the user
pressing Submit there publishes anything.
"""
import json
import os
import urllib.parse
import urllib.request

import words_config  # noqa: F401  (puts api/ on sys.path)
import custom_triggers

REPO_URL = "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server"
LIBRARY_URL = ("https://raw.githubusercontent.com/Dominic-Shirazi/Whisper-Unchained-Server/"
               "main/trigger_library/library.json")
BROWSE_URL = REPO_URL + "/tree/main/trigger_library"
SHARE_URL = REPO_URL + "/issues/new"
BUNDLED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "trigger_library", "library.json")
TIMEOUT = 6
# Browsers and GitHub both cut off very long links; past this the form opens
# with instructions to paste, and the trigger is put on the clipboard instead.
SHARE_URL_MAX = 6000


def _parse(raw):
    """(entries, updated) from the file's bytes or text; raises ValueError."""
    if isinstance(raw, bytes):
        if len(raw) > custom_triggers.LIBRARY_MAX_BYTES:
            raise ValueError("The library file is too large.")
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    entries, err = custom_triggers.validate_library(data)
    if err:
        raise ValueError(err)
    return entries, str(data.get("updated") or "")


def bundled(path=None):
    """(entries, updated) from the copy that came with the app; ([], "") if it
    is missing or damaged."""
    try:
        with open(path or BUNDLED_PATH, "rb") as f:
            return _parse(f.read())
    except Exception:
        return [], ""


def fetch(url=None, opener=None):
    """(entries, updated) from the published library; raises on any failure."""
    req = urllib.request.Request(url or LIBRARY_URL,
                                 headers={"User-Agent": "WhisperUnchained"})
    with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as r:
        return _parse(r.read(custom_triggers.LIBRARY_MAX_BYTES + 1))


def load(url=None, opener=None, path=None):
    """{"entries", "updated", "online", "note"}: the published library, or the
    bundled copy with a plain-English note saying so."""
    try:
        entries, updated = fetch(url, opener)
        return {"entries": entries, "updated": updated, "online": True, "note": ""}
    except Exception:
        entries, updated = bundled(path)
        note = ("Couldn't reach the online library. Showing the triggers that came with "
                "this version." if entries else
                "Couldn't reach the online library. Check your internet connection.")
        return {"entries": entries, "updated": updated, "online": False, "note": note}


def installed_state(entry, installed):
    """"new", "added" or "update" (the library has a newer version of a trigger
    that was added from it)."""
    for item in installed:
        if item.get("library_id") == entry["id"]:
            return "update" if entry["version"] > item.get("library_version", 0) else "added"
    return "new"


def phrase_clashes(entry, installed, builtin_phrases=()):
    """Phrases of `entry` already taken by another trigger, so Add can say so
    before the user saves."""
    taken = {p.lower() for p in builtin_phrases}
    for item in installed:
        if item.get("library_id") != entry["id"]:
            taken.update(p.lower() for p in item.get("phrases", []))
    return [p for p in entry["phrases"] if p.lower() in taken]


def share_json(item):
    return json.dumps(custom_triggers.to_share(item), indent=2, ensure_ascii=False)


def share_link(item):
    """(url, fits): the pre-filled "share a trigger" form. fits is False when
    the trigger is too long for a link; the form then asks for it to be pasted."""
    title = "New trigger: " + item["name"]
    intro = ("**What it does and who it is for** (one or two sentences):\n\n\n"
             "**Tried with this model:**\n\n\n")
    body = intro + "**The trigger:**\n\n```json\n" + share_json(item) + "\n```\n"
    url = SHARE_URL + "?" + urllib.parse.urlencode(
        {"title": title, "labels": "trigger", "body": body})
    if len(url) <= SHARE_URL_MAX:
        return url, True
    body = intro + "**The trigger:** (paste it here; it is on your clipboard)\n\n"
    return SHARE_URL + "?" + urllib.parse.urlencode(
        {"title": title, "labels": "trigger", "body": body}), False
