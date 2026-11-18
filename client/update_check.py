"""Is there a newer Whisper Unchained Server? Checks, tells, never installs.

Once a day at most, this asks GitHub's public list of this project's releases
for the newest version number. Nothing about the person or the PC is sent:
no account, no identifier, a generic client name. GitHub sees the internet
address, as with any download. UPDATE_CHECK=false in .env turns it off.

It only ever says "version X is available" with a link to the release page.
Downloading and running the installer stays the person's own act: the
installer is not code-signed, so an app that fetched and ran one by itself
would be the pattern an attacker wants.

A developer copy (no installer/version.txt) never checks.
"""
import json
import os
import re
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASES_URL = ("https://api.github.com/repos/Dominic-Shirazi/"
                "Whisper-Unchained-Server/releases?per_page=30")
RELEASE_PAGE = "https://github.com/Dominic-Shirazi/Whisper-Unchained-Server/releases/tag/v%s"
CACHE_PATH = os.path.join(ROOT, "client", "update_check.json")
CHECK_EVERY = 24 * 3600      # seconds between checks
RETRY_AFTER = 3600           # after a failed check (offline, GitHub busy)
ENV_KEY = "UPDATE_CHECK"
# Only the server's own releases: "v0.9.21". Other tags on the same project
# (the speech engine's "engine-whispercpp-v1.9.5") are not versions of the app.
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def enabled(env):
    """On unless switched off."""
    return str((env or {}).get(ENV_KEY, "")).strip().lower() not in ("0", "false", "no", "off")


def parse(version):
    """"0.9.21" or "v0.9.21" -> (0, 9, 21); None for anything else."""
    m = _TAG.match("v" + str(version or "").strip().lstrip("vV"))
    return tuple(int(n) for n in m.groups()) if m else None


def current_version(root=ROOT):
    """The installed version, or "" for a copy that was not installed by the
    .exe (a developer copy: nothing to compare with)."""
    try:
        with open(os.path.join(root, "installer", "version.txt"), encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    return text if parse(text) else ""


def newest(releases):
    """The most recently published app version in GitHub's release list, as
    "0.9.21", or "". Drafts are skipped; previews count (every release so far
    is one). By date, not by number: the project has an early "v1.0.0" from
    before its numbering restarted at 0.9, which must not look like the latest.
    (check() then also requires it to be a higher number than the one installed.)"""
    best = None
    for r in releases if isinstance(releases, list) else []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        v = parse(r.get("tag_name"))
        when = str(r.get("published_at") or "")
        if v and (best is None or (when, v) > best):
            best = (when, v)
    return "%d.%d.%d" % best[1] if best else ""


def fetch(opener=None, timeout=10):
    """Ask GitHub. Returns the newest version ("" if none could be read).
    Raises OSError/ValueError when GitHub can't be reached or answers oddly."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(RELEASES_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "WhisperUnchained"})
    with opener(req, timeout=timeout) as r:
        return newest(json.loads(r.read().decode("utf-8")))


def read_cache(path=CACHE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_cache(data, path=CACHE_PATH):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def is_newer(latest, current):
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def check(env, current=None, now=None, opener=None, path=CACHE_PATH, force=False):
    """The newer version available ("0.9.21"), or "" when there is none, the
    check is switched off, or this is a developer copy. Asks GitHub only when
    the last answer is a day old (force=True: now); otherwise answers from the
    remembered one. Never raises."""
    current = current_version() if current is None else current
    if not current or not enabled(env):
        return ""
    now = time.time() if now is None else now
    cache = read_cache(path)
    due = "checked" not in cache or (
        now - float(cache.get("checked") or 0) >= float(cache.get("wait") or CHECK_EVERY))
    if force or due:
        try:
            cache = {"checked": now, "latest": fetch(opener), "wait": CHECK_EVERY,
                     "told": cache.get("told", "")}
        except Exception:
            # Keep the last good answer; look again in an hour, not a day.
            cache = {"checked": now, "latest": cache.get("latest", ""), "wait": RETRY_AFTER,
                     "told": cache.get("told", "")}
        write_cache(cache, path)
    latest = cache.get("latest") or ""
    return latest if is_newer(latest, current) else ""


def known(env, current=None, path=CACHE_PATH):
    """Like check(), but from the remembered answer only: never asks GitHub.
    For places that must not wait (drawing a menu)."""
    current = current_version() if current is None else current
    if not current or not enabled(env):
        return ""
    latest = read_cache(path).get("latest") or ""
    return latest if is_newer(latest, current) else ""


def page(version):
    """The release page for a version: what's new, and the installer."""
    return RELEASE_PAGE % version


SETTINGS_TITLE = "Version %s is available"
SETTINGS_HELP = ("You have %s. The page says what changed and has the installer; "
                 "it installs over this one and keeps your settings.")
NOTICE = "Version %s is available. Open the tray menu to see what's new."


def notice_due(version, path=CACHE_PATH):
    """True once per new version: the tray says it one time, then leaves it
    to the menu."""
    cache = read_cache(path)
    if not version or cache.get("told") == version:
        return False
    cache["told"] = version
    write_cache(cache, path)
    return True


def menu_text(version):
    return "Version %s is available..." % version
