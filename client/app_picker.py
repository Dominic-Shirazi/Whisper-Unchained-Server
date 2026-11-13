"""Pick programs from a list instead of typing their file names.

Settings has boxes that take program names ("Also pause for these programs":
`minecraft.exe, blender.exe`). Nobody knows that Slack runs as `slack.exe`
or that Discord's is `discord.exe`, so those boxes get a "Choose..." button
that opens this list: programs running now first, then the ones used most
recently, then everything else installed, A-Z, each with a checkbox.

Where the list comes from (any source that fails just adds nothing):

* windows open right now (EnumWindows + the owning process's file),
* Windows' own record of what was started and when (the UserAssist key under
  HKCU, read only, never written),
* Start-menu shortcuts (this user's and all users'), whose names are the
  friendly program names people know.

`catalog()` gathers it (about a tenth of a second, cached); `pick_apps()`
shows the dialog. The pure helpers below it are what the tests exercise.
"""
import codecs
import ctypes
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import app_styles
from app_styles import normalize_app

# --- What an entry is ------------------------------------------------------


@dataclass
class App:
    exe: str                        # "slack.exe": how settings match programs
    name: str                       # "Slack": what people call it
    running: bool = False           # has a visible window right now
    last_used: datetime = None      # last start Windows recorded (UTC), or None
    path: str = ""                  # full file path when known (not shown)


# Programs nobody would want in the list: Windows' own plumbing, this app's
# Python, and installer/uninstaller/updater helpers.
NOISE = {
    "explorer.exe", "svchost.exe", "python.exe", "pythonw.exe", "py.exe", "pyw.exe",
    "applicationframehost.exe", "textinputhost.exe", "searchhost.exe",
    "searchapp.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "lockapp.exe", "sihost.exe", "ctfmon.exe", "dwm.exe", "rundll32.exe",
    "dllhost.exe", "conhost.exe", "msiexec.exe", "consent.exe", "werfault.exe",
    "runtimebroker.exe", "systemsettingsbroker.exe", "shellhost.exe",
    "widgets.exe", "widgetservice.exe", "gamebar.exe", "gamebarftserver.exe",
    "openwith.exe", "pickerhost.exe", "credentialuibroker.exe", "mmc.exe",
    "hh.exe", "control.exe", "taskmgr.exe", "regedit.exe", "notification_helper.exe",
    "chrome_proxy.exe", "msedge_proxy.exe",   # web-app shortcuts (Gmail...) run through these
}
NOISE_PARTS = ("unins", "uninst", "setup", "update", "install", "crashpad", "crashreport",
               "crashhandler", "elevation", "repair", "migrat", "-helper", "_helper")


def is_noise(exe):
    exe = (exe or "").lower()
    if not exe.endswith(".exe") or exe == ".exe" or exe in NOISE:
        return True
    stem = exe[:-4]
    return any(p in stem for p in NOISE_PARTS)


# --- Pure helpers (tested) ---------------------------------------------------

def parse_names(value):
    """'Minecraft, blender.exe' (or a list of names) -> ['minecraft.exe',
    'blender.exe']: normalized, in order, without duplicates or blanks."""
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = []
    for p in parts:
        n = normalize_app(p)
        if n and n != ".exe" and n not in out:
            out.append(n)
    return out


def format_names(names):
    """The other way: ['a.exe', 'b.exe'] -> 'a.exe, b.exe' for a settings box."""
    return ", ".join(parse_names(names))


def rot13(s):
    return codecs.decode(s, "rot13")


FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime_to_datetime(ft):
    """A Windows FILETIME (100 ns ticks since 1601) -> UTC datetime; None for 0."""
    if not ft or ft <= 0:
        return None
    try:
        return FILETIME_EPOCH + timedelta(microseconds=ft // 10)
    except (OverflowError, ValueError):
        return None


# Known-folder GUIDs UserAssist writes instead of the folder itself.
KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": ("ProgramW6432", "ProgramFiles"),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": ("ProgramFiles(x86)",),
    "{905E63B6-C1BF-494E-B29C-65B732D3D21A}": ("ProgramFiles",),
    "{6365D5A7-0F0D-45E5-87F6-0DA56B6A4F7D}": ("CommonProgramW6432", "CommonProgramFiles"),
    "{F7F1ED05-9F6D-47A2-AAAE-29D317C6F066}": ("CommonProgramFiles",),
    "{DE974D24-D9C6-4D3E-BF91-F4455120B917}": ("CommonProgramFiles(x86)",),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": ("@system",),
    "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": ("@systemx86",),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": ("SystemRoot", "windir"),
    "{5E6C858F-0E22-4760-9AFE-EA3317B67173}": ("USERPROFILE",),
    "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}": ("LOCALAPPDATA",),
    "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}": ("APPDATA",),
    "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}": ("ProgramData",),
}


def _known_folder(guid, env):
    for var in KNOWN_FOLDERS.get(guid.upper(), ()):
        if var == "@system":
            root = env.get("SystemRoot") or env.get("windir")
            return os.path.join(root, "System32") if root else ""
        if var == "@systemx86":
            root = env.get("SystemRoot") or env.get("windir")
            return os.path.join(root, "SysWOW64") if root else ""
        if env.get(var):
            return env[var]
    return ""


def resolve_userassist_path(name, env=None):
    """A decoded UserAssist name -> a file path: '{6D80...}\\Foo\\foo.exe' ->
    'C:\\Program Files\\Foo\\foo.exe'. Unknown folders keep their GUID (the
    file name at the end is what matters)."""
    env = os.environ if env is None else env
    if name.startswith("{") and "}" in name:
        guid, rest = name[:name.index("}") + 1], name[name.index("}") + 1:]
        base = _known_folder(guid, env)
        if base:
            return base.rstrip("\\/") + "\\" + rest.lstrip("\\/")
    return name


def parse_userassist(values, env=None):
    """[(rot13 value name, bytes)] from a UserAssist ...\\Count key -> [(path,
    last run UTC datetime or None)], .exe programs only. Each value is 72
    bytes on Windows 7+, with the last run's FILETIME at offset 60."""
    out = []
    for raw_name, data in values:
        try:
            name = rot13(raw_name)
        except Exception:
            continue
        if not name.lower().endswith(".exe"):
            continue
        when = None
        if isinstance(data, (bytes, bytearray)) and len(data) >= 68:
            when = filetime_to_datetime(struct.unpack_from("<Q", data, 60)[0])
        out.append((resolve_userassist_path(name, env), when))
    return out


def better_name(current, new):
    """The friendlier of two names for one program: any real name beats the
    file-name stem, and the shorter of two real names wins ('Visual Studio
    Code' over 'Visual Studio Code (Administrator)')."""
    if not new:
        return current
    if not current:
        return new
    return new if len(new) < len(current) else current


def merge(sources):
    """Join entries from every source into one per program (by exe): running
    if any says so, the latest last use, the best name, the first known path.
    Noise is dropped here. Entries are dicts or App objects."""
    by_exe = {}
    for src in sources:
        for e in src or ():
            if isinstance(e, App):
                e = vars(e)
            exe = normalize_app(e.get("exe") or e.get("path") or "")
            if not exe or is_noise(exe):
                continue
            cur = by_exe.get(exe)
            if cur is None:
                cur = by_exe[exe] = App(exe=exe, name="")
            cur.running = cur.running or bool(e.get("running"))
            lu = e.get("last_used")
            if lu and (cur.last_used is None or lu > cur.last_used):
                cur.last_used = lu
            cur.name = better_name(cur.name, (e.get("name") or "").strip())
            if not cur.path and e.get("path"):
                cur.path = e["path"]
    return list(by_exe.values())


def stem_name(exe):
    """'blender.exe' -> 'blender' (the fallback name)."""
    return exe[:-4] if exe.lower().endswith(".exe") else exe


SORT_RECENT, SORT_AZ = "Recently used", "A\u2013Z"
SORTS = (SORT_RECENT, SORT_AZ)


def _az(a):
    return ((a.name or a.exe).casefold(), a.exe)


def sort_apps(apps, how=SORT_RECENT, first=()):
    """Order for the list. Recently used: running now, then by last use
    (newest first), then the rest A-Z. A-Z: by name. Programs in `first`
    (the ones already chosen when the window opened) lead either way, so a
    choice is never buried; they don't move while you tick boxes."""
    first = set(first)
    if how == SORT_AZ:
        return sorted(apps, key=lambda a: (a.exe not in first,) + _az(a))
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def key(a):
        lu = a.last_used or oldest
        return (a.exe not in first, not a.running, a.last_used is None,
                -lu.timestamp() if a.last_used else 0) + _az(a)
    return sorted(apps, key=key)


def group_of(app, how=SORT_RECENT, first=()):
    """The heading a row sits under in the list."""
    if app.exe in first:
        return "Chosen"
    if how == SORT_AZ:
        return "All programs"
    if app.running:
        return "Running now"
    if app.last_used:
        return "Recently used"
    return "Other programs"


def filter_apps(apps, query):
    """Rows whose name or file name contain every word typed (any case)."""
    words = (query or "").casefold().split()
    if not words:
        return list(apps)
    return [a for a in apps
            if all(w in (a.name + " " + a.exe).casefold() for w in words)]


def when_text(dt, now=None):
    """'today', 'yesterday', '3 days ago', '2 weeks ago'... for a last use."""
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    days = (now - dt).days
    if days <= 0:
        return "Used today"
    if days == 1:
        return "Used yesterday"
    if days < 14:
        return "Used %d days ago" % days
    if days < 60:
        return "Used %d weeks ago" % (days // 7)
    if days < 730:
        return "Used %d months ago" % (days // 30)
    return "Used %d years ago" % (days // 365)


# --- .lnk shortcut files (pure Python, MS-SHLLINK) ---------------------------

def parse_lnk(data):
    """The target of a Windows shortcut, from its bytes: (path, arguments).
    ('', '') when it can't be read. Handles the local-path link info and the
    environment-variable block (%ProgramFiles%\\...), which is what Start-menu
    shortcuts use; shortcuts to Store apps have neither and return ''."""
    try:
        if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
            return "", ""
        flags = struct.unpack_from("<I", data, 0x14)[0]
        unicode = bool(flags & 0x80)
        pos = 0x4C
        if flags & 0x01:                                   # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        path = ""
        if flags & 0x02:                                   # HasLinkInfo
            info_size, header = struct.unpack_from("<II", data, pos)
            info_flags = struct.unpack_from("<I", data, pos + 8)[0]
            if info_flags & 0x01:                          # VolumeIDAndLocalBasePath
                # The path is LocalBasePath + CommonPathSuffix (often 'C:\' + the rest).
                if header >= 0x24:
                    base, suffix = struct.unpack_from("<II", data, pos + 0x1C)
                    path = _wstr(data, pos + base) + (_wstr(data, pos + suffix) if suffix else "")
                if not path:
                    base = struct.unpack_from("<I", data, pos + 0x10)[0]
                    suffix = struct.unpack_from("<I", data, pos + 0x18)[0]
                    path = _astr(data, pos + base) + (_astr(data, pos + suffix) if suffix else "")
            pos += info_size
        strings = []
        for bit in (0x04, 0x08, 0x10, 0x20, 0x40):        # name, relpath, dir, args, icon
            if flags & bit:
                n = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                size = n * 2 if unicode else n
                raw = data[pos:pos + size]
                strings.append(raw.decode("utf-16-le" if unicode else "mbcs", "replace"))
                pos += size
            else:
                strings.append("")
        args = strings[3]
        if not path:
            # Extra data blocks: the environment-variable target.
            while pos + 8 <= len(data):
                size, sig = struct.unpack_from("<II", data, pos)
                if size < 4:
                    break
                if sig == 0xA0000001 and size >= 0x314:
                    path = _wstr(data, pos + 8 + 260) or _astr(data, pos + 8)
                    path = os.path.expandvars(path)
                    break
                pos += size
        if not path and strings[1]:
            path = strings[1]                              # relative path: name still useful
        return path, args
    except (struct.error, IndexError, UnicodeDecodeError, LookupError):
        return "", ""


def _wstr(data, pos):
    end = pos
    while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
        end += 2
    return data[pos:end].decode("utf-16-le", "replace")


def _astr(data, pos):
    end = data.find(b"\0", pos)
    raw = data[pos:end if end >= 0 else len(data)]
    try:
        return raw.decode("mbcs")
    except LookupError:
        return raw.decode("latin-1")


def shortcut_target_exe(path, args=""):
    """The program a shortcut really starts. Squirrel apps (Discord, Slack,
    Teams classic) point at Update.exe --processStart Discord.exe."""
    exe = normalize_app(path) if path.lower().endswith(".exe") else ""
    if exe == "update.exe" and "--processstart" in (args or "").lower():
        parts = args.split()
        low = [p.lower() for p in parts]
        i = low.index("--processstart")
        if i + 1 < len(parts):
            return normalize_app(parts[i + 1].strip('"'))
    return exe


# --- Sources (Windows; each returns [] on any failure) -----------------------

def running_windows():
    """[{exe, path, running}] for programs with a visible, titled, top-level
    window (not tool windows or hidden Store-app frames)."""
    out = {}
    try:
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        dwmapi = getattr(ctypes.windll, "dwmapi", None)
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, GW_OWNER = -20, 0x80, 4
        pids = {}

        def cb(hwnd, _l):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                if user32.GetWindow(hwnd, GW_OWNER):
                    return True
                if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                    return True
                if dwmapi is not None:
                    cloaked = ctypes.c_int(0)
                    if dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked),
                                                    ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                        return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids[pid.value] = True
            except Exception:
                pass
            return True
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proc(cb), 0)
        for pid in pids:
            h = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    out[normalize_app(buf.value)] = buf.value
            finally:
                kernel32.CloseHandle(h)
    except Exception:
        pass
    return [{"exe": exe, "path": p, "running": True} for exe, p in out.items()]


USERASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"


def recent_programs():
    """[{exe, path, last_used}] from HKCU UserAssist (read only)."""
    out = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USERASSIST) as ua:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(ua, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(ua, guid + r"\Count") as count:
                        values, j = [], 0
                        while True:
                            try:
                                name, data, _t = winreg.EnumValue(count, j)
                            except OSError:
                                break
                            j += 1
                            values.append((name, data))
                except OSError:
                    continue
                for path, when in parse_userassist(values):
                    if when:
                        out.append({"exe": path, "path": path, "last_used": when})
    except Exception:
        pass
    return out


def start_menu_dirs():
    dirs = []
    for var in ("APPDATA", "ProgramData"):
        base = os.environ.get(var)
        if base:
            dirs.append(os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs"))
    return dirs


SKIP_FOLDERS = {"administrative tools", "windows tools", "startup", "maintenance",
                "accessibility", "windows administrative tools"}


def start_menu_programs(dirs=None):
    """[{exe, name, path}] from Start-menu shortcuts: the shortcut's name is
    the friendly program name."""
    out = []
    for top in (start_menu_dirs() if dirs is None else dirs):
        try:
            walker = os.walk(top)
        except Exception:
            continue
        for folder, subs, files in walker:
            # Windows' admin consoles and the Startup folder aren't programs
            # anyone dictates into.
            subs[:] = [d for d in subs if d.lower() not in SKIP_FOLDERS]
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                try:
                    with open(os.path.join(folder, f), "rb") as fh:
                        data = fh.read(65536)
                except OSError:
                    continue
                target, args = parse_lnk(data)
                exe = shortcut_target_exe(target, args)
                if exe:
                    out.append({"exe": exe, "name": f[:-4], "path": target})
    return out


def file_description(path):
    """The FileDescription in an .exe's version info ('Slack'), or ''."""
    try:
        from ctypes import wintypes
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        langs = []
        if ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr),
                              ctypes.byref(n)) and n.value >= 4:
            raw = ctypes.string_at(ptr, n.value)
            langs = ["%04x%04x" % struct.unpack_from("<HH", raw, k)
                     for k in range(0, n.value - 3, 4)]
        for lang in langs + ["040904b0", "040904e4"]:
            if ver.VerQueryValueW(buf, "\\StringFileInfo\\%s\\FileDescription" % lang,
                                  ctypes.byref(ptr), ctypes.byref(n)) and n.value > 1:
                text = ctypes.wstring_at(ptr, n.value).rstrip("\0").strip()
                if text:
                    return text
    except Exception:
        pass
    return ""


# --- The catalog -------------------------------------------------------------

_cache = {"apps": None, "at": 0.0}
_lock = threading.Lock()
CACHE_SECONDS = 300


def catalog(refresh=False, sources=None):
    """Every program worth offering, one App per exe (unsorted; see
    sort_apps). Shortcuts and history are cached for five minutes; what's
    running is always looked up fresh. `sources` (a dict of zero-argument
    callables: running / recent / installed) is for tests."""
    s = {"running": running_windows, "recent": recent_programs,
         "installed": start_menu_programs}
    s.update(sources or {})

    def safe(fn):
        try:
            return list(fn() or [])
        except Exception:
            return []

    with _lock:
        cached = _cache["apps"] if sources is None else None
        if cached is None or refresh or time.monotonic() - _cache["at"] > CACHE_SECONDS:
            base = merge([safe(s["installed"]), safe(s["recent"])])
            _describe(base)
            if sources is None:
                _cache.update(apps=base, at=time.monotonic())
        else:
            base = cached
    running = safe(s["running"])
    apps = merge([[vars(a) | {"running": False} for a in base], running])
    _describe(apps)
    return apps


_descriptions = {}
GENERIC = {"setup", "application", "app", "launcher", "program", "main", "client"}


def display_name(path, exe, description=""):
    """A name for a program no shortcut names: its FileDescription when that
    is short and specific ('Steam', 'MF Toolbox Application'), else the file
    name as the file spells it ('ScreenToGif', 'Core Temp')."""
    d = (description or "").strip()
    if d and len(d) <= 32 and len(d.split()) <= 4 and d.lower() not in GENERIC             and d.lower() != exe:
        return d
    base = os.path.basename(path or "")
    if base.lower() == exe:
        return stem_name(base)
    return stem_name(exe)


def _describe(apps, budget=0.4):
    """Give nameless entries a name. Reading version info is quick once
    Windows has the file cached but can be slow the first time, so it stops
    after `budget` seconds (later calls carry on; answers are remembered).
    Network paths are never touched: an offline share can hang for seconds."""
    deadline = time.monotonic() + budget
    for a in apps:
        if a.name:
            continue
        p = a.path or ""
        local = len(p) > 2 and p[1] == ":" and not p.startswith("\\\\")
        if local and p not in _descriptions and time.monotonic() < deadline:
            _descriptions[p] = file_description(p)
        a.name = display_name(p, a.exe, _descriptions.get(p, ""))


def start_loading():
    """Warm the cache in the background (e.g. when Settings opens) so the
    picker's list is ready the moment it's asked for."""
    threading.Thread(target=lambda: catalog(), daemon=True).start()


# --- The dialog ----------------------------------------------------------------

def _sg():
    """The Settings module for its palette and widgets. When Settings runs as
    a script it is __main__; importing it again by name would make a second
    copy (with its own Button class, so round_widgets wouldn't recognize ours)."""
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "SettingsApp") and hasattr(main, "Toggle"):
        return main
    import settings_gui
    return settings_gui


def _check_photo(widget, checked, focus, behind, size=20):
    """A checkbox that reads without colour: checked is a filled square with a
    check mark, unchecked an empty outlined square."""
    sg = _sg()
    cache = sg._image_cache(widget)
    key = ("appcheck", checked, focus, behind, size)
    if key in cache:
        return cache[key]
    from PIL import Image, ImageDraw, ImageTk
    k = 8
    S = size * k
    img = Image.new("RGB", (S, S), sg._hex_rgb(behind))
    d = ImageDraw.Draw(img)
    pad = 2 * k
    box = [pad, pad, S - pad - 1, S - pad - 1]
    if checked:
        d.rounded_rectangle(box, radius=4 * k, fill=sg._hex_rgb(sg.ACCENT))
        d.line([(S * 0.30, S * 0.52), (S * 0.44, S * 0.66), (S * 0.71, S * 0.36)],
               fill=sg._hex_rgb("#0E1418"), width=int(2.2 * k), joint="curve")
    else:
        d.rounded_rectangle(box, radius=4 * k, outline=sg._hex_rgb("#9AA2AE"),
                            width=int(1.5 * k))
    if focus:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=6 * k,
                            outline=sg._hex_rgb(sg.TEXT), width=k)
    photo = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS), master=widget)
    cache[key] = photo
    return photo


def _ensure_combobox_style(root):
    """Settings styles its drop-downs once per window; outside Settings (tests,
    another window) do the same here so the sort box isn't a white Windows one."""
    sg = _sg()
    from tkinter import ttk
    try:
        if str(ttk.Style(root).lookup("TCombobox", "fieldbackground")) == sg.RAISED:
            return
        holder = type("StyleHost", (), {})()
        holder.root = root
        sg.SettingsApp._style(holder)
        root._wu_combo_style = holder        # keeps its chevron images alive
    except Exception:
        pass


class AppPicker:
    """The dialog. pick_apps() is the way in; tests drive this directly."""

    ROW_H = 36
    HEAD_H = 30

    def __init__(self, parent, title, selected, help_text=None, load=None):
        import tkinter as tk
        from tkinter import ttk
        sg = self.sg = _sg()
        self.tk = tk
        self.result = None
        self.initial = parse_names(selected)
        self.checked = list(self.initial)        # ordered: kept order + new picks after
        self.apps = {}                            # exe -> App (catalog + extras)
        for exe in self.initial:
            self.apps[exe] = App(exe=exe, name=stem_name(exe))
        self.loaded = False
        self.sort = SORT_RECENT
        self.rows = []                            # [("head", text) | ("app", App)]
        self.cursor = 0                           # index into rows (an "app" row)
        self.hover = None

        top = self.top = tk.Toplevel(parent)
        top.withdraw()
        top.title(title)
        top.configure(bg=sg.BG)
        try:
            top.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass
        sg.set_window_icon(top)
        _ensure_combobox_style(top._root())

        body = tk.Frame(top, bg=sg.BG)
        body.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        tk.Label(body, text=title, font=sg.F_SECTION, bg=sg.BG, fg=sg.TEXT,
                 anchor="w").pack(fill="x")
        if help_text:
            tk.Label(body, text=help_text, font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED,
                     anchor="w", justify="left", wraplength=560).pack(fill="x", pady=(4, 0))

        # Search + sort.
        bar = tk.Frame(body, bg=sg.BG)
        bar.pack(fill="x", pady=(14, 8))
        self.query = tk.StringVar()
        sbox = tk.Frame(bar, bg=sg.RAISED, highlightthickness=1,
                        highlightbackground=sg.BORDER, highlightcolor=sg.ACCENT)
        sbox.pack(side="left", fill="x", expand=True)
        self.search = tk.Entry(sbox, textvariable=self.query, font=sg.F_BODY, bg=sg.RAISED,
                               fg=sg.TEXT, relief="flat", insertbackground=sg.ACCENT,
                               borderwidth=6, highlightthickness=0)
        self.search.pack(fill="x")
        sg.Placeholder(self.search, self.query, "Search by name, e.g. Slack")
        self.search.bind("<FocusIn>", lambda e: self._edge(sbox, sg.ACCENT), add="+")
        self.search.bind("<FocusOut>", lambda e: self._edge(sbox, sg.BORDER), add="+")
        self.search.bind("<Down>", lambda e: (self.list.focus_set(), "break")[1])
        self.query.trace_add("write", lambda *_: self.refresh(keep_top=False))
        tk.Label(bar, text="Sort", font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED).pack(
            side="left", padx=(16, 6))
        self.sort_var = tk.StringVar(value=SORT_RECENT)
        self.sort_box = ttk.Combobox(bar, textvariable=self.sort_var, values=SORTS,
                                     state="readonly", width=13, font=sg.F_BODY)
        self.sort_box.pack(side="left")
        self.sort_box.bind("<<ComboboxSelected>>", lambda e: self._set_sort(self.sort_var.get()))
        # Settings makes the wheel over any drop-down scroll its page; here
        # it scrolls the list instead (and never changes the sort).
        self.sort_box.bind("<MouseWheel>", self._wheel)

        # The list: one canvas, rows drawn on it (hundreds of programs; a
        # widget per row would make every keystroke in the search box slow).
        card = tk.Frame(body, bg=sg.SURFACE)
        card.pack(fill="both", expand=True)
        self.list = tk.Canvas(card, bg=sg.SURFACE, highlightthickness=0, bd=0,
                              takefocus=1, width=560, height=380)
        self.scroll = sg.SlimScrollbar(card, self.list.yview)
        self.list.configure(yscrollcommand=self.scroll.set)
        self.list.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        self.scroll.pack(side="right", fill="y", pady=4)
        self.list.bind("<Configure>", lambda e: self.draw())
        self.list.bind("<Button-1>", self._click)
        self.list.bind("<Motion>", self._motion)
        self.list.bind("<Leave>", lambda e: self._set_hover(None))
        self.list.bind("<FocusIn>", lambda e: self.draw())
        self.list.bind("<FocusOut>", lambda e: self.draw())
        for key, step in (("<Up>", -1), ("<Down>", 1), ("<Prior>", -10), ("<Next>", 10),
                          ("<Home>", -10 ** 6), ("<End>", 10 ** 6)):
            self.list.bind(key, lambda e, s=step: (self.move(s), "break")[1])
        self.list.bind("<space>", lambda e: (self.toggle_cursor(), "break")[1])
        # The wheel scrolls this list, and never the Settings page behind
        # (Settings binds the wheel for every widget; "break" stops it here).
        top.bind("<MouseWheel>", self._wheel)

        # Add one by name.
        add = tk.Frame(body, bg=sg.BG)
        add.pack(fill="x", pady=(12, 0))
        tk.Label(add, text="Not listed? Add a program by name", font=sg.F_HELP,
                 bg=sg.BG, fg=sg.MUTED).pack(side="left")
        self.add_var = tk.StringVar()
        abox = tk.Frame(add, bg=sg.RAISED, highlightthickness=1,
                        highlightbackground=sg.BORDER, highlightcolor=sg.ACCENT)
        abox.pack(side="left", padx=(10, 8), fill="x", expand=True)
        self.add_entry = tk.Entry(abox, textvariable=self.add_var, font=sg.F_BODY,
                                  bg=sg.RAISED, fg=sg.TEXT, relief="flat", width=18,
                                  insertbackground=sg.ACCENT, borderwidth=6,
                                  highlightthickness=0)
        self.add_entry.pack(fill="x")
        sg.Placeholder(self.add_entry, self.add_var, "e.g. minecraft.exe")
        self.add_entry.bind("<FocusIn>", lambda e: self._edge(abox, sg.ACCENT), add="+")
        self.add_entry.bind("<FocusOut>", lambda e: self._edge(abox, sg.BORDER), add="+")
        self.add_entry.bind("<Return>", lambda e: (self.add_typed(), "break")[1])
        sg.Button(add, "Add", self.add_typed).pack(side="left")

        # Footer: count on the left, OK then Cancel on the right (Windows order).
        foot = tk.Frame(top, bg=sg.SURFACE)
        foot.pack(fill="x", pady=(18, 0))
        self.status = tk.Label(foot, font=sg.F_HELP, bg=sg.SURFACE, fg=sg.MUTED, anchor="w")
        self.status.pack(side="left", padx=24)
        sg.Button(foot, "Cancel", self.cancel).pack(side="right", padx=(6, 22), pady=14)
        sg.Button(foot, "OK", self.ok, kind="primary").pack(side="right", pady=14)

        top.bind("<Return>", lambda e: self.ok())
        top.bind("<Escape>", lambda e: self.cancel())
        top.protocol("WM_DELETE_WINDOW", self.cancel)
        sg.round_widgets(top)
        self.refresh()

        # Fill the list from the catalog without holding up the window.
        self._pending = {}
        loader = load or catalog
        threading.Thread(target=self._load_in_background, args=(loader,), daemon=True).start()
        self._poll()

    # -- loading
    def _load_in_background(self, loader):
        try:
            self._pending["apps"] = list(loader() or [])
        except Exception:
            self._pending["apps"] = []

    def _poll(self):
        try:
            if not self.top.winfo_exists():
                return
        except self.tk.TclError:
            return
        if "apps" in self._pending:
            self.set_catalog(self._pending.pop("apps"))
        else:
            self.top.after(40, self._poll)

    def set_catalog(self, apps):
        """Show these programs (keeping chosen ones the catalog doesn't know)."""
        for a in apps:
            if isinstance(a, dict):
                a = App(**{k: a.get(k) for k in ("exe", "name", "running", "last_used")
                           if a.get(k) is not None})
            a.exe = normalize_app(a.exe)
            if a.exe:
                a.name = a.name or stem_name(a.exe)
                self.apps[a.exe] = a
        self.loaded = True
        self.refresh()

    # -- model
    def is_checked(self, exe):
        return exe in self.checked

    def toggle(self, exe):
        if exe in self.checked:
            self.checked.remove(exe)
        else:
            self.checked.append(exe)
        self.draw()

    def add_typed(self):
        names = parse_names(self.add_var.get())
        if not names:
            self.add_entry.focus_set()
            return
        for exe in names:
            if exe not in self.apps:
                self.apps[exe] = App(exe=exe, name=stem_name(exe))
            if exe not in self.checked:
                self.checked.append(exe)
        self.add_var.set("")
        # Show what was added: clear the search and put the cursor on it.
        self.query.set("")
        self.refresh()
        for i, (kind, a) in enumerate(self.rows):
            if kind == "app" and a.exe == names[-1]:
                self.cursor = i
                self._see(i)
        self.draw()

    def _set_sort(self, how):
        self.sort = how if how in SORTS else SORT_RECENT
        self.sort_var.set(self.sort)
        self.refresh(keep_top=False)
        self.list.focus_set()

    def refresh(self, keep_top=True):
        """Rebuild the visible rows from search + sort."""
        apps = filter_apps(self.apps.values(), self.query.get())
        apps = sort_apps(apps, self.sort, first=self.initial)
        rows, last = [], None
        for a in apps:
            g = group_of(a, self.sort, self.initial)
            if g != last:
                rows.append(("head", g))
                last = g
            rows.append(("app", a))
        self.rows = rows
        self.cursor = next((i for i, r in enumerate(rows) if r[0] == "app"), 0)
        if not keep_top:
            self.list.yview_moveto(0)
        self.draw()

    def visible_exes(self):
        return [a.exe for kind, a in self.rows if kind == "app"]

    # -- drawing
    def _y_of(self, i):
        y = 0
        for kind, _ in self.rows[:i]:
            y += self.HEAD_H if kind == "head" else self.ROW_H
        return y

    def _row_at(self, y):
        top = 0
        for i, (kind, _) in enumerate(self.rows):
            h = self.HEAD_H if kind == "head" else self.ROW_H
            if top <= y < top + h:
                return i
            top += h
        return None

    def draw(self):
        sg, c = self.sg, self.list
        try:
            c.delete("all")
            focus = c.focus_get() is c
        except (self.tk.TclError, KeyError):
            focus = False
        w = max(c.winfo_width(), 200)
        y = 0
        for i, (kind, item) in enumerate(self.rows):
            if kind == "head":
                c.create_text(14, y + self.HEAD_H - 8, anchor="sw", text=item.upper(),
                              font=(sg.F_HELP[0], -12), fill=sg.MUTED)
                y += self.HEAD_H
                continue
            a, h = item, self.ROW_H
            here = i == self.cursor and focus
            bg = sg.RAISED if (i == self.hover or here) else sg.SURFACE
            if bg != sg.SURFACE:
                c.create_rectangle(4, y + 1, w - 4, y + h - 1, fill=bg, outline="")
            if here:    # keyboard focus: an outline, as on every Windows control
                c.create_rectangle(4, y + 1, w - 5, y + h - 2, outline=sg.TEXT, width=1)
            photo = _check_photo(c, self.is_checked(a.exe), False, bg)
            c.create_image(14, y + h / 2, anchor="w", image=photo)
            name_id = c.create_text(46, y + h / 2, anchor="w", text=a.name, font=sg.F_BODY,
                                    fill=sg.TEXT)
            x = c.bbox(name_id)[2] + 10
            if a.exe.lower() != a.name.lower():
                c.create_text(x, y + h / 2 + 1, anchor="w", text=a.exe, font=sg.F_HELP,
                              fill=sg.MUTED)
            # The right edge: "Running" as a tag in words, or when it was last used.
            if a.running:
                t = c.create_text(w - 22, y + h / 2, anchor="e", text="Running",
                                  font=sg.F_HELP, fill=sg.TEXT)
                x0, y0, x1, y1 = c.bbox(t)
                c.create_rectangle(x0 - 8, y0 - 1, x1 + 8, y1 + 1, outline="#5A6270")
            elif a.last_used and self.sort == SORT_RECENT:
                c.create_text(w - 14, y + h / 2, anchor="e", text=when_text(a.last_used),
                              font=sg.F_HELP, fill=sg.MUTED)
            y += h
        if not any(k == "app" for k, _ in self.rows):
            msg = ("No program matches \u201c%s\u201d. Add it by name below."
                   % self.query.get().strip()) if self.query.get().strip() else \
                ("Looking for programs\u2026" if not self.loaded else "No programs found.")
            c.create_text(18, 24, anchor="w", text=msg, font=sg.F_HELP, fill=sg.MUTED)
            y = 48
        elif not self.loaded:
            c.create_text(18, y + 18, anchor="w", text="Looking for more programs\u2026",
                          font=sg.F_HELP, fill=sg.MUTED)
            y += 36
        c.configure(scrollregion=(0, 0, w, y))
        n = len(self.checked)
        self.status.config(text="Nothing chosen" if not n else
                           "%d program%s chosen" % (n, "" if n == 1 else "s"))

    # -- input
    def _wheel(self, e):
        self.list.yview_scroll(int(-e.delta / 120) * 2, "units")
        return "break"

    def _canvas_y(self, e):
        return int(self.list.canvasy(e.y))

    def _click(self, e):
        self.list.focus_set()
        i = self._row_at(self._canvas_y(e))
        if i is not None and self.rows[i][0] == "app":
            self.cursor = i
            self.toggle(self.rows[i][1].exe)

    def _motion(self, e):
        i = self._row_at(self._canvas_y(e))
        self._set_hover(i if i is not None and self.rows[i][0] == "app" else None)

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.list.configure(cursor="hand2" if i is not None else "")
            self.draw()

    def move(self, step):
        apps = [i for i, r in enumerate(self.rows) if r[0] == "app"]
        if not apps:
            return
        pos = apps.index(self.cursor) if self.cursor in apps else 0
        self.cursor = apps[max(0, min(len(apps) - 1, pos + step))]
        self._see(self.cursor)
        self.draw()

    def _see(self, i):
        total = max(1, self._y_of(len(self.rows)))
        y0 = self._y_of(i)
        y1 = y0 + self.ROW_H
        if i > 0 and self.rows[i - 1][0] == "head":
            y0 -= self.HEAD_H           # keep a group's heading in view with its first row
        view_h = self.list.winfo_height()
        top = self.list.canvasy(0)
        if y0 < top:
            self.list.yview_moveto(y0 / total)
        elif y1 > top + view_h:
            self.list.yview_moveto((y1 - view_h) / total)

    def toggle_cursor(self):
        if 0 <= self.cursor < len(self.rows) and self.rows[self.cursor][0] == "app":
            self.toggle(self.rows[self.cursor][1].exe)

    @staticmethod
    def _edge(box, color):
        box.config(highlightbackground=color)
        c = getattr(box, "_corners", None)
        if c is not None:
            box.after_idle(c.refresh)

    # -- closing
    def ok(self):
        # Something typed in the add box but not added yet counts too.
        if self.add_var.get().strip():
            self.add_typed()
        self.result = list(self.checked)
        self.top.destroy()

    def cancel(self):
        self.result = None
        self.top.destroy()

    def show(self, parent):
        """Centre over the parent window and make it modal."""
        top = self.top
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        try:
            p = parent.winfo_toplevel()
            x = p.winfo_rootx() + (p.winfo_width() - w) // 2
            y = p.winfo_rooty() + max(0, (p.winfo_height() - h) // 3)
        except self.tk.TclError:
            x, y = 200, 120
        top.geometry("+%d+%d" % (x, y))
        top.minsize(520, 420)
        top.deiconify()
        self.sg.dark_title_bar(top)
        try:
            top.grab_set()
        except self.tk.TclError:
            pass
        self.search.focus_set()


def pick_apps(parent, title, selected, help_text=None, load=None):
    """Open the picker over `parent` and wait. `selected` is the current
    choice (a list of names or the box's comma-separated text). Returns the
    chosen exe names as a list (kept order first, new picks after; e.g.
    ['minecraft.exe', 'slack.exe']), or None if cancelled. format_names()
    turns it back into box text. `load` replaces catalog() (tests)."""
    picker = AppPicker(parent, title, selected, help_text, load)
    picker.show(parent)
    parent.wait_window(picker.top)
    return picker.result


__all__ = ["App", "catalog", "pick_apps", "parse_names", "format_names", "start_loading",
           "AppPicker", "sort_apps", "filter_apps", "parse_userassist", "parse_lnk", "merge"]
