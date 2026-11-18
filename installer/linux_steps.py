"""The setup wizard's install actions on Linux: what steps.py does on Windows
with PowerShell scripts, a Windows service and a permission prompt.

Nothing here needs a password. The server and the tray run as systemd --user
services, the same ones scripts/install_linux.sh --user and
scripts/install_linux_desktop.sh write; a test keeps the unit files equal.
A system-wide service (its own user, needs sudo) stays with install_linux.sh.

Every action that runs a program takes it as an argument (`run`), so the tests
need no systemd, no microphone and no graphics card.
"""
import glob
import os
import posixpath
import shutil
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_UNIT = "whisper-unchained"
DESKTOP_UNIT = "whisper-unchained-desktop"
MENU_ENTRY = os.path.expanduser(
    "~/.local/share/nautilus/scripts/Transcribe with Whisper Unchained")
NVIDIA_WHEELS = ("nvidia-cublas-cu12>=12.4,<13", "nvidia-cudnn-cu12>=9,<10")
# What pip downloads for NVIDIA_WHEELS (see linux_setup_plan.PACKAGES).
NVIDIA_BYTES = 1_387_000_000
# large-v3-turbo: about 2.1 GB of the card at float16, 1.2 GB at int8_float16
# (measured on an RTX 2070 Super). The same limits as install_linux.sh.
FLOAT16_FREE_MB, INT8_FREE_MB = 2600, 1400
# How long a first start may take before we stop waiting: it loads the model.
START_TIMEOUT = 600


def venv_python(root=ROOT):
    # Written into unit files: always a Linux path, whatever runs the tests.
    return posixpath.join(root, ".venv", "bin", "python")


def unit_dir():
    return os.path.expanduser("~/.config/systemd/user")


def unit_path(name):
    return os.path.join(unit_dir(), name + ".service")


def _run(cmd, run=subprocess.run, timeout=60, **kw):
    """(ok, output). Never raises."""
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def systemctl(*args, run=subprocess.run, timeout=60):
    return _run(["systemctl", "--user", *args], run, timeout)


# --- the graphics card ------------------------------------------------------------

def pick_gpu(run=subprocess.run):
    """The NVIDIA card with the most free memory: (index, free MiB), or None
    when nvidia-smi can't be asked. A PC with one card always gets card 0;
    this matters on a PC where another program already fills one of two."""
    ok, out = _run(["nvidia-smi", "--query-gpu=index,memory.free",
                    "--format=csv,noheader,nounits"], run, 15)
    best = None
    for line in out.splitlines() if ok else ():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            if best is None or int(parts[1]) > best[1]:
                best = (int(parts[0]), int(parts[1]))
    return best


def gpu_settings(picked, compute_type):
    """The .env lines for the chosen card: which one, and a lighter way of
    loading the model when the card is nearly full. {} = the processor."""
    if picked is None:
        return {}
    index, free = picked
    if free < INT8_FREE_MB:
        return {"WHISPER_DEVICE": "cpu", "WHISPER_COMPUTE_TYPE": "int8",
                "CUDA_VISIBLE_DEVICES": ""}
    return {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(index),
            "WHISPER_COMPUTE_TYPE": compute_type if free >= FLOAT16_FREE_MB
            else "int8_float16"}


def nvidia_libs_installed(root=ROOT):
    return bool(glob.glob(os.path.join(root, ".venv", "lib", "python*", "site-packages",
                                       "nvidia", "cudnn")))


def nvidia_lib_path(root=ROOT):
    """The folders of NVIDIA's libraries inside the venv, for LD_LIBRARY_PATH."""
    return ":".join(sorted(glob.glob(os.path.join(
        root, ".venv", "lib", "python*", "site-packages", "nvidia", "*", "lib"))))


def _folder_bytes(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def install_nvidia_libs(root=ROOT, progress=None, cancelled=None, popen=subprocess.Popen,
                        sleep=time.sleep):
    """Download NVIDIA's libraries into the venv, reporting bytes as they
    arrive. They are downloaded to a folder first (so there is something to
    measure), then installed from it. Returns (ok, detail)."""
    py = venv_python(root)
    folder = os.path.join(root, ".venv", "nvidia-download")
    os.makedirs(folder, exist_ok=True)
    try:
        proc = popen([py, "-m", "pip", "download", "-q", "-d", folder, *NVIDIA_WHEELS],
                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if cancelled is not None and cancelled():
                proc.terminate()
                return False, "cancelled"
            if progress is not None:
                progress(_folder_bytes(folder), NVIDIA_BYTES)
            sleep(0.5)
        if proc.returncode != 0:
            return False, _last_line(proc.stderr.read() if proc.stderr else "")
        ok, out = _run([py, "-m", "pip", "install", "-q", "--no-index", "--find-links",
                        folder, *NVIDIA_WHEELS], timeout=900)
        return ok, "" if ok else _last_line(out)
    except OSError as e:
        return False, str(e)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def cuda_works(root=ROOT, gpu=None, run=subprocess.run):
    """Can the speech engine really use the card (driver new enough)?"""
    env = dict(os.environ, LD_LIBRARY_PATH=nvidia_lib_path(root))
    if gpu is not None:
        env.update(CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=str(gpu))
    ok, _out = _run([venv_python(root), "-c",
                     "import ctranslate2, sys; sys.exit(ctranslate2.get_cuda_device_count() < 1)"],
                    run, 60, env=env)
    return ok


def _last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


# --- the background services --------------------------------------------------------

def server_unit_text(root=ROOT, port=5000):
    """The same unit scripts/install_linux.sh --user writes."""
    return ("[Unit]\n"
            "Description=Whisper Unchained Server (speech to text API)\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "WorkingDirectory=%(root)s\n"
            "ExecStart=%(py)s -m api.serve --host 0.0.0.0 --port %(port)d\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "Environment=LD_LIBRARY_PATH=%(libs)s\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "# Loading the model (and downloading it on first start) takes a while.\n"
            "TimeoutStartSec=600\n"
            "NoNewPrivileges=yes\n"
            "PrivateTmp=yes\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n") % {
        "root": root, "py": venv_python(root), "port": int(port),
        "libs": nvidia_lib_path(root)}


def desktop_unit_text(root=ROOT):
    """The same unit scripts/install_linux_desktop.sh writes."""
    return ("[Unit]\n"
            "Description=Whisper Unchained (hotkey listener and tray icon)\n"
            "PartOf=graphical-session.target\n"
            "After=graphical-session.target\n"
            "\n"
            "[Service]\n"
            "WorkingDirectory=%(root)s\n"
            "ExecStart=%(py)s -m client.linux_tray\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=graphical-session.target\n") % {"root": root, "py": venv_python(root)}


def _write_unit(name, text):
    os.makedirs(unit_dir(), exist_ok=True)
    with open(unit_path(name), "w", encoding="utf-8") as f:
        f.write(text)


def install_server(root=ROOT, port=5000, run=subprocess.run):
    """Write the server's service, set it to start with the PC's login, and
    (re)start it. Returns (ok, detail)."""
    try:
        _write_unit(SERVER_UNIT, server_unit_text(root, port))
    except OSError as e:
        return False, str(e)
    systemctl("daemon-reload", run=run)
    systemctl("enable", SERVER_UNIT, run=run)
    ok, out = systemctl("restart", SERVER_UNIT, run=run)
    return ok, "" if ok else _last_line(out)


def lingering(run=subprocess.run, user=None):
    """True when the server also starts at boot, before anyone logs in."""
    user = user or os.environ.get("USER") or ""
    ok, out = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"], run, 10)
    return ok and out.strip() == "yes"


def install_desktop(root=ROOT, at_login=True, run=subprocess.run):
    """Write the tray's service and start it now; with at_login it also
    starts whenever you sign in. Returns (ok, detail)."""
    try:
        _write_unit(DESKTOP_UNIT, desktop_unit_text(root))
    except OSError as e:
        return False, str(e)
    systemctl("daemon-reload", run=run)
    systemctl("enable" if at_login else "disable", DESKTOP_UNIT, run=run)
    ok, out = systemctl("restart", DESKTOP_UNIT, run=run)
    if not ok:
        return False, _last_line(out)
    time.sleep(2)                    # a tray that can't start dies at once
    alive, _ = systemctl("is-active", "--quiet", DESKTOP_UNIT, run=run)
    return alive, "" if alive else "it stopped again"


def install_menu(root=ROOT):
    """Right-click > Scripts > Transcribe, in GNOME Files. Returns (ok,
    detail); (None, ...) when this desktop has no Files app to add it to."""
    if not shutil.which("nautilus"):
        return None, "this desktop's file manager has no such menu"
    try:
        os.makedirs(os.path.dirname(MENU_ENTRY), exist_ok=True)
        with open(MENU_ENTRY, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\n# Right-click > Scripts > this. Opens the "Transcribe a file" '
                    'window.\nfor f in "$@"; do\n    "%s" "%s/client/file_window.py" "$f" &\n'
                    'done\n' % (venv_python(root), root))
        os.chmod(MENU_ENTRY, 0o755)
        return True, ""
    except OSError as e:
        return False, str(e)


def journal_tail(unit=SERVER_UNIT, lines=6, run=subprocess.run):
    """The last things a service said, without systemd's own chatter: what
    the person needs when it didn't start."""
    ok, out = _run(["journalctl", "--user", "-u", unit, "-n", "40", "--no-pager", "-o", "cat"],
                   run, 15)
    if not ok:
        return []
    noise = ("Started ", "Stopped ", "Stopping ", "Scheduled restart", "Main process exited",
             "Failed with result", "Consumed ", "INFO:")
    said = [l.strip() for l in out.splitlines()
            if l.strip() and not any(n in l for n in noise)]
    return said[-lines:]


def wait_for_server(port, https, wait, progress=None, timeout=START_TIMEOUT,
                    clock=time.monotonic, sleep=time.sleep, run=subprocess.run):
    """Wait for dictation to answer, saying how long it has been. `wait` is
    steps.wait_for_server (one short try at a time). Returns (ok, detail,
    journal lines when it failed)."""
    start = clock()
    last = "no answer yet"
    while clock() - start < timeout:
        ok, last = wait(port, timeout=4, interval=1, https=https)
        if ok:
            return True, last, []
        if "model failed to load" in last:
            break
        failed, _ = systemctl("is-failed", "--quiet", SERVER_UNIT, run=run)
        _ok, restarts = systemctl("show", "-p", "NRestarts", "--value", SERVER_UNIT, run=run)
        if failed or (restarts.strip().isdigit() and int(restarts.strip()) >= 3):
            last = "it keeps stopping while it starts"
            break
        if progress is not None:
            progress(int(clock() - start))
        sleep(2)
    else:
        last = "no answer after %d minutes" % (timeout // 60)
    return False, last, journal_tail(run=run)


def waiting_text(seconds):
    """What the "Check that dictation is ready" row says while it waits."""
    if seconds < 60:
        return "starting up: %d seconds so far (a first start can take a few minutes)" % seconds
    return "still starting: %d min %02d s so far (it is loading the speech model)" % (
        seconds // 60, seconds % 60)


# --- microphone and speakers -----------------------------------------------------------

def recorder(which=shutil.which):
    """The recording program the listener would use, or None."""
    return next((r for r in ("pw-record", "parecord", "arecord") if which(r)), None)


def check_audio(run=subprocess.run, which=shutil.which, seconds=0.5, folder=None,
                popen=subprocess.Popen):
    """Is there a working microphone, and something to play the beeps on?
    Returns (mic_ok, speakers_ok, problems) like steps.check_audio. Records
    half a second with the same program the listener uses."""
    problems = []
    rec = recorder(which)
    if rec is None:
        return False, False, ["Couldn't check the microphone: no recording program is "
                              "installed. Install one with: sudo apt install pipewire-bin"]
    folder = folder or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    path = os.path.join(folder, "whisper-unchained-setup-check.wav")
    cmd = {"pw-record": ["pw-record", "--rate", "16000", "--channels", "1", path],
           "parecord": ["parecord", "--rate=16000", "--channels=1", "--file-format=wav", path],
           "arecord": ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", path]}[rec]
    mic_ok = True
    try:
        proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(seconds)
        proc.terminate()
        proc.wait(timeout=3)
        with wave.open(path) as w:
            frames = w.readframes(w.getnframes())
        if not frames:
            raise ValueError("nothing recorded")
        if not any(frames):
            mic_ok = False
            problems.append("The microphone gave only silence. Check it isn't muted: open "
                            "Settings > Sound and look under Input.")
    except Exception:
        mic_ok = False
        problems.append("No microphone could be opened. Plug one in, or switch it on in "
                        "Settings > Sound, under Input.")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    speakers_ok = True
    if which("pactl"):
        ok, out = _run(["pactl", "list", "short", "sinks"], run, 10)
        real = [l for l in out.splitlines() if l.strip() and "auto_null" not in l]
        if ok and not real:
            speakers_ok = False
            problems.append("No speakers or headphones are switched on, so you won't hear "
                            "the start and stop beeps. Choose one under Output in "
                            "Settings > Sound.")
    return mic_ok, speakers_ok, problems


def open_sound_settings(popen=subprocess.Popen, which=shutil.which):
    for cmd in (["gnome-control-center", "sound"], ["pavucontrol"],
                ["systemsettings", "kcm_pulseaudio"]):
        if which(cmd[0]):
            try:
                popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
            return


def open_folder(path, popen=subprocess.Popen):
    try:
        popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --- Tailscale ---------------------------------------------------------------------------

TAILSCALE_DOWNLOAD = "https://tailscale.com/download/linux"


def tailscale_installed(which=shutil.which):
    return bool(which("tailscale"))


def install_tailscale(open_page=None):
    """Tailscale's Linux install needs a password in a terminal, which a
    setup window can't give: open its own page instead. Returns "browser"."""
    if open_page is None:
        import webbrowser
        open_page = webbrowser.open
    open_page(TAILSCALE_DOWNLOAD)
    return "browser"


def desktop_missing(which=shutil.which, system_python="/usr/bin/python3",
                    run=subprocess.run):
    """System packages the hotkey and tray want that aren't there (apt names)."""
    missing = []
    if not any(which(t) for t in ("xclip", "xsel", "wl-copy")):
        missing.append("xclip")
    # The tray icon is drawn with the system's GTK through python3-gi.
    ok, _ = _run([system_python, "-c",
                  "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk"],
                 run, 10)
    if not ok:
        missing += ["python3-gi", "gir1.2-gtk-3.0"]
    if not any(which(t) for t in ("pw-record", "parecord", "arecord")):
        missing.append("pipewire-bin")
    return missing


if __name__ == "__main__":      # a quick look at this PC, for a person debugging
    print("gpu:", pick_gpu(), "| nvidia libs:", nvidia_libs_installed(),
          "| tailscale:", tailscale_installed(), "| missing:", desktop_missing())
    sys.exit(0)
