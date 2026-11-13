"""Small, safe .env helpers shared by the listener, the tray and Settings.

`set_key` changes exactly one `KEY=value` line and leaves every other byte of
the file alone (comments, blank lines, ordering, CRLF/LF line endings,
commented-out `# KEY=...` fallbacks). It writes through a temp file +
os.replace, so an interrupted write never leaves a half-written .env.
"""
import os
import re

_LINE = re.compile(rb"^\s*([A-Z_][A-Z0-9_]*)\s*=")

TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")


def read(path):
    """{KEY: value} for live assignments. Commented-out lines are ignored."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$", line)
                if m:
                    values[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return values


def as_bool(value, default):
    v = (value or "").strip().strip('"').strip("'").lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return default


def get_bool(path, key, default):
    return as_bool(read(path).get(key), default)


def set_key(path, key, value):
    """Set `key=value` in the .env at `path`, touching only that line.

    Every live `key=` line is rewritten (normally there is one); if there is
    none, `key=value` is appended at the end. A missing file is created.
    """
    key_b = key.encode("ascii")
    value_b = str(value).encode("utf-8")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        data = b""

    lines = data.splitlines(keepends=True)
    # Use the file's own line ending for anything we add.
    eol = b"\r\n" if b"\r\n" in data else b"\n"
    found = False
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) == key_b:
            body = line.rstrip(b"\r\n")
            ending = line[len(body):]
            lines[i] = key_b + b"=" + value_b + ending
            found = True
    if not found:
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            lines.append(eol)
        lines.append(key_b + b"=" + value_b + eol)

    new = b"".join(lines)
    if new == data:
        return
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, path)


class Watcher:
    """Cheap change detection: re-read a key only when the file's mtime moves."""

    def __init__(self, path, key, default):
        self.path, self.key, self.default = path, key, default
        self._mtime = None
        self.value = default
        self.poll()

    def poll(self):
        """Return the current value, re-reading the file only if it changed."""
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            self.value = get_bool(self.path, self.key, self.default)
        return self.value
