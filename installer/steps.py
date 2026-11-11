"""The setup wizard's install actions, kept apart from the UI so they can be
tested and reused (e.g. by a future .exe wrapper).

Everything logs to installer/logs/, because the wizard runs under pythonw
(no console) and an elevated PowerShell can't send its output back to us.
"""
import ctypes
import json
import os
import shutil
import subprocess
import time
import urllib.request
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
NO_WINDOW = 0x08000000


def log_path(name):
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, name)


def ensure_env(root=ROOT):
    """Create .env from .env.example on a fresh install. Returns True if it
    was created (fresh), False if one already existed (repair / reconfigure)."""
    env = os.path.join(root, ".env")
    if os.path.exists(env):
        return False
    shutil.copy(os.path.join(root, ".env.example"), env)
    return True


def _ps_env():
    # See Setup.bat: an inherited PowerShell 7 module path breaks 5.1 cmdlets.
    env = dict(os.environ)
    env.pop("PSModulePath", None)
    return env


def run_ps(script, *args, log_name=None, timeout=300):
    """Run one of scripts/*.ps1 without elevation. Returns (ok, log_file)."""
    log_file = log_path(log_name or os.path.basename(script) + ".log")
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", os.path.join(SCRIPTS, script), *args]
    with open(log_file, "w", encoding="utf-8", errors="replace") as out:
        try:
            r = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT,
                               timeout=timeout, env=_ps_env(), creationflags=NO_WINDOW)
            return r.returncode == 0, log_file
        except (OSError, subprocess.SubprocessError) as e:
            out.write("\n%s\n" % e)
            return False, log_file


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE)]


ELEVATION_CANCELLED = "cancelled"


def run_elevated_ps(script, *args, log_name=None):
    """Run scripts/<script> as Administrator (one UAC prompt) and wait for it.
    Returns (ok | ELEVATION_CANCELLED, log_file)."""
    log_file = log_path(log_name or os.path.basename(script) + ".log")
    if os.path.exists(log_file):
        os.remove(log_file)
    target = os.path.join(SCRIPTS, script)
    # The elevated shell can't pipe back to us, so it writes its own log.
    inner = "& '%s' %s *> '%s'; exit $LASTEXITCODE" % (
        target.replace("'", "''"), " ".join(args), log_file.replace("'", "''"))
    params = '-NoProfile -ExecutionPolicy Bypass -Command "%s"' % inner
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS: give us the process handle
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = params
    info.lpDirectory = ROOT
    info.nShow = 0  # hidden
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.GetLastError() == 1223:  # ERROR_CANCELLED: user said No to UAC
            return ELEVATION_CANCELLED, log_file
        return False, log_file
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return code.value == 0, log_file


def start_tray(root=ROOT):
    """Start the tray listener (a second copy exits by itself: it's single-instance)."""
    pythonw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    watchdog = os.path.join(root, "client", "listener_watchdog.py")
    subprocess.Popen([pythonw, watchdog], cwd=root, creationflags=NO_WINDOW)


def wait_for_server(port, timeout=240, interval=2, opener=urllib.request.urlopen,
                    sleep=time.sleep):
    """Poll /health until the server answers with status ok. Loading the model
    takes 10-60 s. Returns (ok, detail)."""
    deadline = time.monotonic() + timeout
    last = "no answer yet"
    while time.monotonic() < deadline:
        try:
            with opener("http://127.0.0.1:%d/health" % port, timeout=3) as r:
                body = json.load(r)
            if body.get("status") == "ok":
                return True, "model %s loaded" % body.get("model", "?")
            last = "server answered, but the model failed to load"
        except Exception as e:
            last = str(e)
        sleep(interval)
    return False, last
