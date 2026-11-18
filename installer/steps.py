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
import webbrowser
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


def run_elevated_ps(script, *args, log_name=None, started=None):
    """Run scripts/<script> as Administrator (one UAC prompt) and wait for it.
    started: called once, when the script has begun writing its log (so the
    prompt was approved and the wait from here on is the work itself).
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
    # 0x102 = WAIT_TIMEOUT: still running, look again in half a second.
    while ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 500) == 0x102:
        if started and os.path.exists(log_file):
            started()
            started = None
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return code.value == 0, log_file


def log_says(log_file, phrase):
    """True when an elevated script's log contains the phrase. Windows
    PowerShell writes those logs as UTF-16; newer ones as UTF-8."""
    try:
        with open(log_file, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    for enc in ("utf-16", "utf-8"):
        try:
            if phrase in raw.decode(enc):
                return True
        except UnicodeError:
            pass
    return False


# --- The install log ---------------------------------------------------------
# installer/logs/wizard.log is what someone sends when setup goes wrong, so it
# says what this PC is, what each step did and when, and, when the server
# would not start, what happened when it was started by hand.

def write_log(text):
    """One time-stamped line in installer/logs/wizard.log. Never raises."""
    try:
        with open(log_path("wizard.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def app_version(root=ROOT):
    """The installed version (installer/version.txt, written when the .exe is
    built), or "a developer copy"."""
    try:
        with open(os.path.join(root, "installer", "version.txt"), encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "a developer copy"


def file_version(path):
    """A Windows file's version as "14.44.35211.0", or "" when it has none or
    is not there."""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        # VS_FIXEDFILEINFO: signature, struct version, then the file version.
        nums = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 4)).contents
        ms, ls = nums[2], nums[3]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return ""


def system_facts(adapters=None):
    """Lines describing this PC for the install log. Nothing personal: no user
    name, no paths under the user's folder, no settings values."""
    import platform
    facts = ["Whisper Unchained Server %s" % app_version(),
             "Windows: %s" % platform.platform(),
             "Processor: %s (%s logical)" % (platform.processor() or "unknown", os.cpu_count()),
             "Python: %s" % platform.python_version()]
    try:
        import psutil
        mem = psutil.virtual_memory()
        facts.append("Memory: %.1f GB, %.1f GB free" % (mem.total / 2 ** 30,
                                                       mem.available / 2 ** 30))
    except Exception:
        pass
    try:
        facts.append("Disk free where installed: %.1f GB"
                     % (shutil.disk_usage(ROOT).free / 2 ** 30))
    except OSError:
        pass
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    facts.append("Visual C++ runtime (msvcp140.dll): %s"
                 % (file_version(os.path.join(system32, "msvcp140.dll")) or "not found"))
    if adapters is not None:
        try:
            facts.append("Graphics: %s" % (", ".join(adapters()) or "none listed"))
        except Exception:
            pass
    return facts


# Lines of the server's own output that are left out of the log: they list the
# person's own words.
_PRIVATE_LINES = ("hotwords", "vocabulary", "corrections")


def diagnose_start(root=ROOT, seconds=90, run=subprocess.Popen, ports=(5650, 5651)):
    """The server would not start as a service: start it by hand on spare ports,
    hidden, and write what it printed and how it ended to
    installer/logs/server_start.log. Returns one sentence for the wizard log.
    A program that dies inside its speech engine prints nothing, so the exit
    code is the evidence (0xC0000005 is an access violation)."""
    import threading
    python = os.path.join(root, ".venv", "Scripts", "python.exe")
    try:
        proc = run([python, "-m", "api.serve", "--host", "127.0.0.1",
                    "--port", str(ports[0]), "--https-port", str(ports[1])],
                   cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   text=True, errors="replace", creationflags=NO_WINDOW)
        lines = []

        def pump():
            for line in proc.stdout:
                if not any(w in line.lower() for w in _PRIVATE_LINES):
                    lines.append(line)
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            code = None
            proc.kill()
        reader.join(timeout=5)
        printed = "".join(lines)
        if code is None:
            verdict = ("started by hand, the server was still running after %d s (so it "
                       "starts; the service's account may be the problem)" % seconds)
        else:
            verdict = ("started by hand, the server ended by itself with exit code %d "
                       "(0x%08X)" % (code, code & 0xFFFFFFFF))
            if "Traceback" not in printed:
                verdict += ", with no error message"
        with open(log_path("server_start.log"), "w", encoding="utf-8", errors="replace") as out:
            out.write(printed)
            out.write("\n[setup] %s\n" % verdict)
        return verdict
    except Exception as e:
        return "could not start the server by hand: %s" % e


def start_tray(root=ROOT):
    """Start the tray listener (a second copy exits by itself: it's single-instance)."""
    pythonw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    watchdog = os.path.join(root, "client", "listener_watchdog.py")
    subprocess.Popen([pythonw, watchdog], cwd=root, creationflags=NO_WINDOW)


NOT_STARTED = "the speech server has not started"


def _own_cert_opener(url, timeout):
    """urlopen that trusts only this server's own certificate (api/tls.py).
    Built per call: the service creates the certificate on its first start."""
    import ssl
    cafile = os.path.join(ROOT, "api", "certs", "server.pem")
    if not os.path.isfile(cafile):
        # The server makes this file once its speech model has loaded.
        raise OSError(NOT_STARTED)
    return urllib.request.urlopen(url, timeout=timeout,
                                  context=ssl.create_default_context(cafile=cafile))


def wait_for_server(port, timeout=240, interval=2, opener=None, sleep=time.sleep, https=False):
    """Poll /health until the server answers with status ok. Loading the model
    takes 10-60 s. Returns (ok, detail)."""
    if opener is None:
        opener = _own_cert_opener if https else urllib.request.urlopen
    url = "%s://127.0.0.1:%d/health" % ("https" if https else "http", port)
    deadline = time.monotonic() + timeout
    last = "no answer yet"
    while time.monotonic() < deadline:
        try:
            with opener(url, timeout=3) as r:
                body = json.load(r)
            if body.get("status") == "ok":
                return True, "model %s loaded" % body.get("model", "?")
            last = "server answered, but the model failed to load"
        except Exception as e:
            last = str(e)
        sleep(interval)
    return False, last


# --- Tailscale: reaching this PC from the phone when away from home ----------
# Optional and never forced. Most home internet has no fixed (static) address
# and blocks incoming connections, so a phone on mobile data can't find the PC;
# Tailscale links the user's own devices privately without opening anything.

TAILSCALE_EXE = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                             "Tailscale", "tailscale.exe")
TAILSCALE_DOWNLOAD = "https://tailscale.com/download/windows"


SOUND_SETTINGS = "ms-settings:sound"
MIC_PRIVACY_SETTINGS = "ms-settings:privacy-microphone"


def check_audio(sd=None, seconds=0.4):
    """Is there a working microphone, and something to play the beeps on?
    Returns (mic_ok, speakers_ok, problems): problems is a list of plain
    sentences saying what to switch on. Never raises. A device that is
    disabled in Windows doesn't exist as far as this can see, which is exactly
    the case to catch."""
    problems = []
    try:
        if sd is None:
            import sounddevice as sd
    except Exception:
        return False, False, ["Couldn't check the microphone (the audio library didn't load)."]
    mic_ok = speakers_ok = True
    try:
        sd.query_devices(kind="input")
    except Exception:
        mic_ok = False
        problems.append("No microphone is switched on. In Windows, open Settings > System > "
                        "Sound, and under Input enable or plug in a microphone.")
    if mic_ok:
        try:
            data = sd.rec(int(16000 * seconds), samplerate=16000, channels=1, dtype="int16")
            sd.wait()
            if not getattr(data, "any", lambda: True)():
                mic_ok = False
                problems.append("The microphone gave only silence. Check it isn't muted, and "
                                "that Windows lets desktop apps use it (Settings > Privacy & "
                                "security > Microphone).")
        except Exception:
            mic_ok = False
            problems.append("The microphone couldn't be opened. Check it is enabled in "
                            "Settings > System > Sound, and that Windows lets desktop apps "
                            "use it (Settings > Privacy & security > Microphone).")
    try:
        sd.query_devices(kind="output")
    except Exception:
        speakers_ok = False
        problems.append("No speakers or headphones are switched on, so you won't hear the "
                        "start and stop beeps. Enable one under Output in Settings > System > "
                        "Sound.")
    return mic_ok, speakers_ok, problems


def open_sound_settings(startfile=None):
    try:
        (startfile or os.startfile)(SOUND_SETTINGS)
    except Exception:
        pass


def tailscale_installed():
    return bool(shutil.which("tailscale")) or os.path.exists(TAILSCALE_EXE)


def install_tailscale(popen=subprocess.Popen, open_page=webbrowser.open,
                      which=shutil.which):
    """Start Tailscale's own installer without waiting for it. With winget it
    runs in a console window the user can watch (Windows asks for permission
    there); without winget, open the download page. Returns "winget" or "browser"."""
    winget = which("winget")
    if winget:
        popen([winget, "install", "--id", "Tailscale.Tailscale", "-e",
               "--accept-package-agreements", "--accept-source-agreements"],
              creationflags=subprocess.CREATE_NEW_CONSOLE)
        return "winget"
    open_page(TAILSCALE_DOWNLOAD)
    return "browser"
