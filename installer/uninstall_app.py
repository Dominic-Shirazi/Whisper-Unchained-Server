"""Whisper Unchained Server uninstaller, run by the app's own Python.

Why not Inno Setup's unins000.exe: Windows 11 Smart App Control blocks
programs Microsoft's cloud has never seen, unless they're code-signed. The
installer .exe is the same file for everyone, so it earns a reputation, but
Inno writes unins000.exe on each PC at install time (a unique file), so Smart
App Control blocks it. The app's pythonw.exe is already allowed (the tray and
Settings run on it), so Windows' Uninstall button runs this script instead.
unins000.exe stays in the folder as a fallback for PCs without the .venv.

    pythonw installer\\uninstall_app.py              ask, then uninstall
    pythonw installer\\uninstall_app.py --quiet      no questions; keeps your data
            ... --quiet --delete-data                no questions; deletes everything
            ... --register                           point Windows' Uninstall button here
            ... --dry-run                            print what would be removed
            ... --files-only                         skip the service, processes, login
                                                     shortcut, right-click menu (tests)

What it removes, and ONLY when it points into this install folder (a developer
copy elsewhere on the PC uses the same names and must be left alone):
  1. the WhisperUnchained service (or its old name FasterWhisperAPI), its
     firewall rule and restart task: one admin prompt, done by Microsoft's own
     sc.exe / netsh / schtasks, so no unsigned script runs as admin
  2. the tray, Settings and anything else running from this folder
  3. the login shortcut, the right-click Transcribe entries, old uv leftovers
  4. the Start-menu shortcuts and the Windows "Installed apps" entry
  5. the installed files (installer\\installed_files.txt, written by
     build_exe.ps1), .venv, installer\\.tools and other rebuildable folders.
     Your settings, word lists, transcripts, recordings and models stay unless
     you tick "also delete". The folder itself goes once nothing is left in it.

The running Python can't delete itself, so the last part (.venv,
installer\\.tools and whatever was in use) is handed to a hidden cmd.exe that
waits for this process to exit.
"""
import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes

try:
    import winreg
except ImportError:  # not Windows: only the pure functions are usable (tests)
    winreg = None

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)

APP_NAME = "Whisper Unchained Server"
# installer\WhisperUnchained.iss AppId. Never changes.
APP_GUID = "{8C1F5B2E-4D7A-4E7B-9C2A-6B0F3E1D9A57}"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APP_GUID + "_is1"
MANIFEST = os.path.join("installer", "installed_files.txt")
SERVICE_NAMES = ("WhisperUnchained", "FasterWhisperAPI")
FIREWALL_RULE = "Whisper Unchained Server"
TASK_NAMES = ("Restart Whisper Unchained", "Restart FasterWhisperAPI")
STARTUP_LINKS = ("Whisper Unchained.lnk", "FasterWhisperListener.lnk")
UNINSTALL_LINK = "Uninstall Whisper Unchained Server.lnk"
DEFAULT_GROUP = "Whisper Unchained"
CLASSES = r"Software\Classes"
MEDIA_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".webm", ".aac", ".wma",
              ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp")
MENU_VERBS = ("WhisperUnchained.Clipboard", "WhisperUnchained.Open")

# Rebuilt by Setup.bat, never the user's own data. .venv and installer\.tools
# hold the running Python, so cmd.exe deletes those after this process exits.
DEFERRED_DIRS = (".venv", os.path.join("installer", ".tools"))
GENERATED_DIRS = (os.path.join("installer", "logs"),)
NO_WINDOW = 0x08000000


# --- paths ----------------------------------------------------------------------

def _norm(path):
    return os.path.normcase(os.path.abspath(os.path.expandvars(path.strip().strip('"'))))


def is_ours(path, app_dir):
    """True if path is app_dir or inside it (never a sibling like '...Unchained2')."""
    if not path or not str(path).strip().strip('"'):
        return False
    p, a = _norm(str(path)), _norm(app_dir).rstrip("\\/")
    return p == a or p.startswith(a + os.sep)


def read_manifest(app_dir):
    """The files the installer put here (relative paths), or None if this
    folder wasn't installed by the installer (a developer copy has none)."""
    try:
        with open(os.path.join(app_dir, MANIFEST), encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    out = []
    for line in lines:
        rel = line.strip().replace("/", "\\")
        if not rel or rel.startswith("#"):
            continue
        full = os.path.join(app_dir, rel)
        if is_ours(full, app_dir) and _norm(full) != _norm(app_dir):
            out.append(rel)
    return out


def safe_app_dir(app_dir):
    """Refuse to delete anything from a folder that's too close to the root
    (C:\\, C:\\Users) even if a manifest turned up there."""
    parts = [p for p in _norm(app_dir).split(os.sep) if p]
    return len(parts) >= 3


class Plan:
    """What deleting the files means, worked out before anything is touched."""

    def __init__(self, app_dir, delete_data, manifest):
        self.app_dir = os.path.abspath(app_dir)
        self.delete_data = delete_data
        j = lambda rel: os.path.join(self.app_dir, rel)  # noqa: E731
        self.deferred_dirs = [j(d) for d in DEFERRED_DIRS]
        if delete_data:
            # Everything goes; only what's in use waits for cmd.exe.
            self.files = []
            self.dirs = []
            self.empty_dirs = []
            return
        files = [j(rel) for rel in manifest]
        # Inno's own uninstaller files (the fallback) and Python bytecode.
        try:
            names = os.listdir(self.app_dir)
        except OSError:
            names = []
        files += [j(n) for n in names if n.lower().startswith("unins")
                  and os.path.splitext(n)[1].lower() in (".exe", ".dat", ".msg")]
        self.files = files
        dirs = [j(d) for d in GENERATED_DIRS]
        deferred = [_norm(d) for d in self.deferred_dirs]
        for top, subdirs, _ in os.walk(self.app_dir):
            if _norm(top) in deferred:
                subdirs[:] = []
                continue
            if "__pycache__" in subdirs:
                dirs.append(os.path.join(top, "__pycache__"))
                subdirs.remove("__pycache__")
        self.dirs = dirs
        # Folders the installed files were in: removed if that left them empty,
        # deepest first, and the install folder last.
        parents = set()
        for f in files + dirs + self.deferred_dirs:
            d = os.path.dirname(f)
            while is_ours(d, self.app_dir) and _norm(d) != _norm(self.app_dir):
                parents.add(d)
                d = os.path.dirname(d)
        self.empty_dirs = sorted(parents, key=lambda d: (-d.count(os.sep), d.lower()))
        self.empty_dirs.append(self.app_dir)


def cleanup_command(plan, leftover_files=(), tries=30):
    """cmd.exe line that finishes the job after this process exits: retries
    until the running Python lets go of its files. Only Microsoft's cmd.exe and
    ping.exe run, so Smart App Control has nothing to block."""
    wait = "ping -n 2 127.0.0.1 >nul"
    parts = []

    def remove_tree(path):
        parts.append('for /l %%i in (1,1,%d) do if exist "%s" (rd /s /q "%s" 2>nul & '
                     'if exist "%s" %s)' % (tries, path, path, path, wait))
    if plan.delete_data:
        remove_tree(plan.app_dir)
    else:
        for d in plan.deferred_dirs:
            remove_tree(d)
        for f in leftover_files:
            parts.append('for /l %%i in (1,1,%d) do if exist "%s" (del /f /q "%s" 2>nul & '
                         'if exist "%s" %s)' % (tries, f, f, f, wait))
        for d in plan.empty_dirs:
            parts.append('rd "%s" 2>nul' % d)
    # %%i is for a .cmd file; on a cmd /c line it's %i.
    return " & ".join(parts).replace("%%i", "%i")


def has_user_data(app_dir):
    """After delete_files: anything left besides what cmd.exe is about to remove?"""
    deferred = {_norm(os.path.join(app_dir, d)) for d in DEFERRED_DIRS}
    for top, subdirs, files in os.walk(app_dir):
        subdirs[:] = [s for s in subdirs if _norm(os.path.join(top, s)) not in deferred]
        if files:
            return True
    return False


def delete_files(plan, log=print):
    """Delete what can go now. Returns the files still in use (for cmd.exe)."""
    left = []
    for f in plan.files:
        if not os.path.lexists(f):
            continue
        try:
            os.chmod(f, 0o666)
        except OSError:
            pass
        try:
            os.remove(f)
        except OSError:
            left.append(f)
    for d in plan.dirs:
        shutil.rmtree(d, ignore_errors=True)
    for d in plan.empty_dirs:
        try:
            os.rmdir(d)
        except OSError:
            pass
    return left


# --- registry: Windows' "Installed apps" entry ------------------------------------

def uninstall_command(app_dir):
    pythonw = os.path.join(app_dir, ".venv", "Scripts", "pythonw.exe")
    return '"%s" "%s"' % (pythonw, os.path.join(app_dir, "installer", "uninstall_app.py"))


def _read_value(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def entry_is_ours(app_dir, key_path=UNINSTALL_KEY, hive=None):
    """The Installed-apps entry exists and is this folder's."""
    hive = hive if hive is not None else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(hive, key_path) as k:
            loc = _read_value(k, "InstallLocation") or _read_value(k, "Inno Setup: App Path")
    except OSError:
        return False
    return bool(loc) and _norm(loc).rstrip("\\") == _norm(app_dir).rstrip("\\")


def register(app_dir=APP_DIR, key_path=UNINSTALL_KEY, hive=None, programs_dir=None,
             relink=None):
    """Point Windows' Uninstall button (and the Start-menu entry) at this script.
    Run by bootstrap.ps1 once .venv exists, so every Setup / repair re-applies it
    after the .exe installer rewrites the entry. Returns True if it changed it."""
    hive = hive if hive is not None else winreg.HKEY_CURRENT_USER
    if read_manifest(app_dir) is None or not entry_is_ours(app_dir, key_path, hive):
        return False
    if not os.path.exists(os.path.join(app_dir, ".venv", "Scripts", "pythonw.exe")):
        return False
    cmd = uninstall_command(app_dir)
    with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        group = _read_value(k, "Inno Setup: Icon Group") or DEFAULT_GROUP
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, cmd)
        winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ, cmd + " --quiet")
    link = os.path.join(programs_dir or shell_folder(CSIDL_PROGRAMS), group, UNINSTALL_LINK)
    if os.path.exists(link):
        try:
            (relink or _relink_uninstall)(link, app_dir)
        except Exception:
            pass  # the Installed-apps entry is what matters
    return True


def remove_entry(app_dir, key_path=UNINSTALL_KEY, hive=None):
    """Delete the Installed-apps entry if it's this folder's. Returns its
    Start-menu group name (or None)."""
    hive = hive if hive is not None else winreg.HKEY_CURRENT_USER
    if not entry_is_ours(app_dir, key_path, hive):
        return None
    with winreg.OpenKey(hive, key_path) as k:
        group = _read_value(k, "Inno Setup: Icon Group") or DEFAULT_GROUP
    parent, name = key_path.rsplit("\\", 1)
    with winreg.OpenKey(hive, parent, 0, winreg.KEY_ALL_ACCESS) as p:
        winreg.DeleteKey(p, name)
    return group


def find_context_keys(app_dir, hive=None, classes=CLASSES):
    """Right-click entries whose command runs a program in this folder."""
    hive = hive if hive is not None else winreg.HKEY_CURRENT_USER
    keys = [classes + r"\*\shell\TranscribeToClipboard"]
    keys += [r"%s\SystemFileAssociations\%s\shell\%s" % (classes, ext, verb)
             for ext in MEDIA_EXTS for verb in MENU_VERBS]
    found = []
    for key in keys:
        try:
            with winreg.OpenKey(hive, key + r"\command") as k:
                command = str(_read_value(k, "") or "")
        except OSError:
            continue
        if command.startswith('"'):
            exe = command[1:].split('"', 1)[0]
        else:
            exe = command.split(" ", 1)[0]
        if is_ours(exe, app_dir):
            found.append(key)
    return found


def delete_tree(hive, key):
    """winreg has no recursive delete."""
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                delete_tree(hive, key + "\\" + sub)
        winreg.DeleteKey(hive, key)
    except OSError:
        pass


def find_astral_keys(app_dir, hive=None, base=r"Software\Python\Astral"):
    """Older setups (before 0.9.12) let uv register its Python with Windows."""
    hive = hive if hive is not None else winreg.HKEY_CURRENT_USER
    found = []
    try:
        with winreg.OpenKey(hive, base) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(hive, "%s\\%s\\InstallPath" % (base, sub)) as ip:
                        if is_ours(_read_value(ip, "") or "", app_dir):
                            found.append(base + "\\" + sub)
                except OSError:
                    pass
    except OSError:
        pass
    return found


def find_uv_bins(app_dir, bin_dir):
    """...and drop a python3.x.exe in ~\\.local\\bin that names its real path."""
    found = []
    try:
        names = os.listdir(bin_dir)
    except OSError:
        return found
    needle = _norm(app_dir).lower()
    for n in names:
        if not (n.lower().startswith("python3") and n.lower().endswith(".exe")):
            continue
        p = os.path.join(bin_dir, n)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        for enc in ("utf-8", "utf-16-le"):
            if needle in data.decode(enc, "ignore").lower():
                found.append(p)
                break
    return found


# --- shortcuts ----------------------------------------------------------------------

CSIDL_PROGRAMS, CSIDL_STARTUP = 2, 7


def shell_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


def _wsh():
    import comtypes.client
    return comtypes.client.CreateObject("WScript.Shell", dynamic=True)


def shortcut_refs(path):
    """Target, arguments and working folder of a .lnk (what decides whose it is)."""
    try:
        s = _wsh().CreateShortcut(path)
        return [s.TargetPath or "", s.WorkingDirectory or ""]
    except Exception:
        pass
    # No COM: the .lnk stores its paths as plain (UTF-16 or ANSI) text.
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    return [data.decode("utf-16-le", "ignore"), data[1:].decode("utf-16-le", "ignore"),
            data.decode("mbcs" if os.name == "nt" else "latin-1", "ignore")]


def shortcut_is_ours(path, app_dir, refs=None):
    found = (refs or shortcut_refs)(path)
    if not found:
        return False
    if len(found) == 2:  # exact: the target decides; a target-less link, its folder
        return is_ours(found[0], app_dir) if found[0] else is_ours(found[1], app_dir)
    needle = _norm(app_dir).lower().rstrip("\\") + "\\"
    return any(needle in t.lower() for t in found)


def find_shortcuts(folder, names, app_dir, refs=None):
    """names=None: every .lnk in folder."""
    try:
        listing = os.listdir(folder)
    except OSError:
        return []
    if names is None:
        names = [n for n in listing if n.lower().endswith(".lnk")]
    return [os.path.join(folder, n) for n in names
            if n in listing and shortcut_is_ours(os.path.join(folder, n), app_dir, refs)]


def _relink_uninstall(link, app_dir):
    """Point the Start-menu "Uninstall" entry at this script (it was made for
    unins000.exe by the .exe installer on a first install)."""
    if not shortcut_is_ours(link, app_dir):
        return
    s = _wsh().CreateShortcut(link)
    s.TargetPath = os.path.join(app_dir, ".venv", "Scripts", "pythonw.exe")
    s.Arguments = '"%s"' % os.path.join(app_dir, "installer", "uninstall_app.py")
    s.WorkingDirectory = tempfile.gettempdir()
    icon = os.path.join(app_dir, "client", "assets", "app_icon.ico")
    if os.path.exists(icon):
        s.IconLocation = icon + ", 0"
    s.Save()


# --- service and processes -------------------------------------------------------------

def find_services(app_dir, names=SERVICE_NAMES, app_directory=None):
    """Our services: their NSSM AppDirectory (readable without admin) is this folder."""
    if app_directory is None:
        def app_directory(name):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SYSTEM\CurrentControlSet\Services\%s\Parameters" % name) as k:
                    return _read_value(k, "AppDirectory")
            except OSError:
                return None
    return [n for n in names if is_ours(app_directory(n) or "", app_dir)]


def elevated_command(services):
    """One admin cmd.exe line: Microsoft's sc.exe, netsh and schtasks only, so
    nothing unsigned runs elevated (and PowerShell's Constrained Language Mode
    can't get in the way)."""
    parts = []
    for s in services:
        parts += ['sc.exe stop "%s" >nul 2>&1' % s, "ping -n 3 127.0.0.1 >nul",
                  'sc.exe delete "%s" >nul 2>&1' % s]
    parts.append('netsh advfirewall firewall delete rule name="%s" >nul 2>&1' % FIREWALL_RULE)
    parts += ['schtasks /Delete /TN "%s" /F >nul 2>&1' % t for t in TASK_NAMES]
    return " & ".join(parts)


ELEVATION_CANCELLED = "cancelled"


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE)]


def run_elevated_cmd(line):
    """cmd.exe /c line as Administrator (one UAC prompt); waits. True, False or
    ELEVATION_CANCELLED."""
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = os.path.join(system32, "cmd.exe")
    info.lpParameters = '/d /s /c "%s"' % line
    info.lpDirectory = system32
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return ELEVATION_CANCELLED if ctypes.GetLastError() == 1223 else False
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 120000)
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return True


def find_processes(app_dir, procs, own_pid):
    """PIDs running a program from this folder: the tray, its watchdog,
    Settings... but not this uninstaller (nor the .venv launcher that started it)."""
    out = []
    for pid, exe, cmdline in procs:
        if pid == own_pid or not exe or not is_ours(exe, app_dir):
            continue
        if any("uninstall_app.py" in str(a).lower() for a in (cmdline or [])):
            continue
        out.append(pid)
    return out


def _all_processes():
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            out.append((p.info["pid"], p.info["exe"], p.info["cmdline"]))
        except Exception:
            pass
    return out


def stop_processes(app_dir, log=print):
    import psutil
    import time
    # The watchdog restarts the tray, so sweep until nothing's left.
    for _ in range(5):
        pids = find_processes(app_dir, _all_processes(), os.getpid())
        if not pids:
            return
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
        time.sleep(0.5)


# --- the uninstall --------------------------------------------------------------------------

class Uninstaller:
    def __init__(self, app_dir=APP_DIR, delete_data=False, dry_run=False, files_only=False,
                 log=print, ask_retry=None):
        self.app_dir = os.path.abspath(app_dir)
        self.delete_data = delete_data
        self.dry = dry_run
        self.files_only = files_only
        self.log = log
        # Called when the admin prompt is declined: "retry", "skip" or "cancel".
        self.ask_retry = ask_retry or (lambda: "skip")
        self.problems = []
        self.plan = None
        self.leftovers = []

    def _do(self, what, fn, *a):
        self.log(("would remove: " if self.dry else "removing: ") + what)
        if not self.dry:
            try:
                fn(*a)
            except Exception as e:
                self.problems.append("%s (%s)" % (what, e))

    def system_part(self):
        """Service, firewall, task (admin), processes, login shortcut,
        right-click menu, uv leftovers. Returns False if the user cancelled."""
        services = find_services(self.app_dir)
        if services:
            line = elevated_command(services)
            self.log(("would run as admin: " if self.dry else "admin: ") + line)
            if not self.dry:
                while True:
                    r = run_elevated_cmd(line)
                    if r is not ELEVATION_CANCELLED:
                        break
                    choice = self.ask_retry()
                    if choice == "cancel":
                        return False
                    if choice == "skip":
                        self.problems.append(
                            "The background service wasn't removed (no admin permission). "
                            "An administrator can remove it with: sc.exe delete " + services[0])
                        break
                self._wait_stopped(services)
        if not self.dry:
            try:
                stop_processes(self.app_dir, self.log)
            except Exception as e:
                self.log("couldn't stop running copies: %s" % e)
        for lnk in find_shortcuts(shell_folder(CSIDL_STARTUP), STARTUP_LINKS, self.app_dir):
            self._do(lnk, os.remove, lnk)
        hive = winreg.HKEY_CURRENT_USER
        for key in find_context_keys(self.app_dir):
            self._do("HKCU\\" + key, delete_tree, hive, key)
        for key in find_astral_keys(self.app_dir):
            self._do("HKCU\\" + key, delete_tree, hive, key)
        for exe in find_uv_bins(self.app_dir, os.path.join(os.path.expanduser("~"), ".local", "bin")):
            self._do(exe, os.remove, exe)
        return True

    def _wait_stopped(self, services, timeout=20):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            running = False
            for s in services:
                try:
                    out = subprocess.run(["sc.exe", "query", s], capture_output=True, text=True,
                                         timeout=10, creationflags=NO_WINDOW).stdout
                except Exception:
                    out = ""
                if "STOPPED" not in out and "1060" not in out:  # 1060: no such service
                    running = True
            if not running:
                return
            time.sleep(1)

    def shell_part(self):
        """Start-menu shortcuts and the Installed-apps entry."""
        if self.dry:
            group = DEFAULT_GROUP
            if entry_is_ours(self.app_dir):
                self.log("would remove: HKCU\\" + UNINSTALL_KEY)
        else:
            group = None
            try:
                group = remove_entry(self.app_dir)
                if group:
                    self.log("removed: HKCU\\" + UNINSTALL_KEY)
            except OSError as e:
                self.problems.append("the Installed-apps entry (%s)" % e)
            group = group or DEFAULT_GROUP
        folder = os.path.join(shell_folder(CSIDL_PROGRAMS), group)
        for lnk in find_shortcuts(folder, None, self.app_dir):
            self._do(lnk, os.remove, lnk)
        if not self.dry:
            try:
                os.rmdir(folder)
            except OSError:
                pass

    def files_part(self, manifest):
        self.plan = Plan(self.app_dir, self.delete_data, manifest)
        if self.dry:
            if self.delete_data:
                self.log("would remove: " + self.app_dir + " (everything)")
            else:
                for f in self.plan.files + self.plan.dirs + self.plan.deferred_dirs:
                    if os.path.lexists(f):
                        self.log("would remove: " + f)
            return
        if not self.delete_data:
            self.leftovers = delete_files(self.plan, self.log)

    def handoff(self):
        """Start the hidden cmd.exe that removes what this process still holds."""
        if self.dry or self.plan is None:
            return
        line = cleanup_command(self.plan, self.leftovers)
        if len(line) > 8000:  # cmd.exe's limit: drop the empty-folder sweep
            self.plan.empty_dirs = [self.plan.app_dir]
            line = cleanup_command(self.plan, self.leftovers[:40])
        cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
        subprocess.Popen('"%s" /d /s /c "%s"' % (cmd, line), cwd=tempfile.gettempdir(),
                         creationflags=NO_WINDOW | 0x00000200,  # + CREATE_NEW_PROCESS_GROUP
                         close_fds=True)

    def run(self, manifest):
        if not self.files_only:
            if not self.system_part():
                return False
        self.shell_part()
        self.files_part(manifest)
        return True


# --- window ------------------------------------------------------------------------------

DATA_TEXT = "settings, word lists, transcripts, recordings and downloaded speech models"


class Dialog:
    """One window, styled like the app: confirm (with the keep-my-data choice),
    progress, done."""

    def __init__(self, app_dir):
        import tkinter as tk
        sys.path.insert(0, os.path.join(app_dir, "client"))
        try:
            import theme as t
        except Exception:
            t = None
        g = lambda n, d: getattr(t, n, d)  # noqa: E731
        self.c = dict(bg=g("BG", "#17181A"), surface=g("SURFACE", "#1E1F22"),
                      raised=g("RAISED", "#27292D"), hover=g("HOVER", "#2E3035"),
                      text=g("TEXT", "#ECECEC"), muted=g("MUTED", "#A9ABB0"),
                      accent=g("ACCENT", "#8C9EFF"), accent_h=g("ACCENT_H", "#A6B4FF"),
                      on_accent=g("ON_ACCENT", "#0F1220"), danger=g("DANGER", "#E0734F"))
        font, font_sb = g("FONT", "Segoe UI"), g("FONT_SB", "Segoe UI Semibold")
        self.f_title, self.f_body = (font_sb, 15), (font, 10)
        self.f_button = (font_sb, 10)
        self.tk = tk
        self.app_dir = app_dir
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Uninstall " + APP_NAME)
        self.root.configure(bg=self.c["bg"])
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(os.path.join(app_dir, "client", "assets", "app_icon.ico"))
        except Exception:
            pass
        self.body = None
        self.result = None

    def _page(self, title, text):
        tk, c = self.tk, self.c
        if self.body is not None:
            self.body.destroy()
        self.body = tk.Frame(self.root, bg=c["bg"])
        self.body.pack(fill="both", expand=True, padx=28, pady=24)
        tk.Label(self.body, text=title, font=self.f_title, bg=c["bg"], fg=c["text"],
                 anchor="w").pack(anchor="w")
        tk.Label(self.body, text=text, font=self.f_body, bg=c["bg"], fg=c["muted"],
                 justify="left", anchor="w", wraplength=460).pack(anchor="w", pady=(8, 0))
        return self.body

    def _button(self, parent, text, command, primary=False):
        tk, c = self.tk, self.c
        bg, fg, hover = ((c["accent"], c["on_accent"], c["accent_h"]) if primary
                         else (c["raised"], c["text"], c["hover"]))
        b = tk.Label(parent, text=text, font=self.f_button, bg=bg, fg=fg, padx=16, pady=7,
                     cursor="hand2", takefocus=1, highlightthickness=2,
                     highlightbackground=c["bg"], highlightcolor=c["text"])
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Return>", lambda _e: command())
        b.bind("<space>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.config(bg=hover))
        b.bind("<Leave>", lambda _e: b.config(bg=bg))
        return b

    def _buttons(self, *specs):
        row = self.tk.Frame(self.body, bg=self.c["bg"])
        row.pack(fill="x", pady=(22, 0))
        made = []
        for text, cmd, primary in reversed(specs):
            b = self._button(row, text, cmd, primary)
            b.pack(side="right", padx=(10, 0))
            made.append(b)
        return made

    def _show(self):
        self.root.update_idletasks()
        w, h = max(self.root.winfo_reqwidth(), 520), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _wait(self):
        self.result = None
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._set(None))
        while self.result is None and self.root.winfo_exists():
            self.root.update()
            if getattr(self, "_closed", False):
                break
        return self.result

    def _set(self, value):
        if value is None:
            self._closed = True
            self.result = "cancel"
        else:
            self.result = value

    def confirm(self):
        """Returns None (cancel) or delete_data (bool)."""
        tk, c = self.tk, self.c
        self._closed = False
        body = self._page("Uninstall %s?" % APP_NAME,
                          "Removes the app, its background service, the login shortcut and "
                          "the right-click menu entries. Windows may ask once for permission, "
                          "to remove the service.")
        var = tk.BooleanVar(self.root, value=False)
        tk.Checkbutton(body, text="Also delete my " + DATA_TEXT, variable=var,
                       font=self.f_body, bg=c["bg"], fg=c["text"], selectcolor=c["raised"],
                       activebackground=c["bg"], activeforeground=c["text"],
                       wraplength=440, justify="left", anchor="w").pack(anchor="w", pady=(16, 0))
        tk.Label(body, text="Left unticked, they stay in\n%s\nso a reinstall picks up where "
                 "you left off." % self.app_dir, font=self.f_body, bg=c["bg"], fg=c["muted"],
                 justify="left", anchor="w", wraplength=460).pack(anchor="w", padx=(26, 0))
        cancel, go = self._buttons(("Cancel", lambda: self._set("cancel"), False),
                                   ("Uninstall", lambda: self._set("go"), True))[::-1]
        self._show()
        go.focus_set()
        if self._wait() != "go":
            return None
        return bool(var.get())

    def working(self):
        self._page("Uninstalling...", "Removing %s. This takes a few seconds." % APP_NAME)
        self._show()
        self.root.update()

    def ask_retry(self):
        """Admin prompt declined."""
        self._closed = False
        self._page("Permission needed",
                   "Windows needs permission to remove the background service. Without "
                   "it the service stays installed, pointing at a deleted folder, until "
                   "an administrator removes it.")
        self._buttons(("Cancel uninstall", lambda: self._set("cancel"), False),
                      ("Continue without", lambda: self._set("skip"), False),
                      ("Ask again", lambda: self._set("retry"), True))
        self._show()
        r = self._wait()
        self.working()
        return r if r in ("retry", "skip") else "cancel"

    def done(self, delete_data, problems):
        self._closed = False
        text = "%s was removed." % APP_NAME
        if not delete_data and has_user_data(self.app_dir):
            text += "\n\nYour %s are still in\n%s\nDelete that folder to remove them too." % (
                DATA_TEXT, self.app_dir)
        if problems:
            text += "\n\nNot everything could be removed:\n  - " + "\n  - ".join(problems)
        self._page("Uninstalled" if not problems else "Uninstalled, with problems", text)
        self._buttons(("Close", lambda: self._set("ok"), True))
        self._show()
        self._wait()
        try:
            self.root.destroy()
        except Exception:
            pass

    def message(self, title, text):
        self._closed = False
        self._page(title, text)
        self._buttons(("Close", lambda: self._set("ok"), True))
        self._show()
        self._wait()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Uninstall " + APP_NAME)
    ap.add_argument("--quiet", action="store_true", help="no questions; keeps your data")
    ap.add_argument("--delete-data", action="store_true", help="with --quiet: delete everything")
    ap.add_argument("--register", action="store_true",
                    help="point Windows' Uninstall button at this script")
    ap.add_argument("--dry-run", action="store_true", help="print what would be removed")
    ap.add_argument("--files-only", action="store_true",
                    help="skip the service, processes, login shortcut and right-click menu")
    ap.add_argument("--app-dir", default=APP_DIR, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    app_dir = os.path.abspath(args.app_dir)

    if args.register:
        try:
            changed = register(app_dir)
            print("Uninstall entry points at %s" % uninstall_command(app_dir) if changed
                  else "Uninstall entry left as it is (not an installed copy, or no .venv).")
        except Exception as e:
            print("Couldn't update the uninstall entry: %s" % e)
        return 0

    # Never hold the folder open: it's about to be deleted.
    os.chdir(tempfile.gettempdir())
    manifest = read_manifest(app_dir)
    quiet = args.quiet or args.dry_run
    if manifest is None or not safe_app_dir(app_dir):
        msg = ("%s isn't a folder the installer set up (installer\\installed_files.txt is "
               "missing), so nothing was removed." % app_dir)
        if quiet:
            print(msg)
        else:
            Dialog(app_dir).message("Nothing to uninstall here", msg)
        return 1

    dialog = None if quiet else Dialog(app_dir)
    if dialog:
        delete_data = dialog.confirm()
        if delete_data is None:
            return 2
        dialog.working()
    else:
        delete_data = args.delete_data
    u = Uninstaller(app_dir, delete_data=delete_data, dry_run=args.dry_run,
                    files_only=args.files_only, log=print if quiet else (lambda *_: None),
                    ask_retry=dialog.ask_retry if dialog else None)
    if not u.run(manifest):
        return 2
    if dialog:
        dialog.done(delete_data, u.problems)
    elif u.problems:
        print("Problems:\n  " + "\n  ".join(u.problems))
    u.handoff()
    return 0


if __name__ == "__main__":
    sys.exit(main())
