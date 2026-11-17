"""The tray icon on Linux, and the listener's keeper: starts the hotkey
listener (linux_listener.py), starts it again if it dies, and gives it a menu.
The Linux counterpart of listener_watchdog.py (Windows).

    python -m client.linux_tray

The icon needs GTK's Python bindings (python3-gi, on every GNOME/Ubuntu
desktop) and, on GNOME, the AppIndicator extension (Ubuntu ships it switched
on). Without them there is no icon and this just keeps the listener running;
`python -m client.linux_listener --toggle` and the hotkey still work.
"""
import os
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV_FILE = os.path.join(ROOT, ".env")
sys.path.insert(0, HERE)
import env_file  # noqa: E402
import hotkey_config  # noqa: E402
import linux_listener  # noqa: E402

APP_NAME = linux_listener.APP_NAME
ICON_PATH = os.path.join(HERE, "assets", "app_icon.png")

process = None
running = True


def _system_gi():
    """A venv can't see the system's python3-gi (it can't be pip-installed
    without compilers and headers). Same Python version as the system's: add
    the system package folders at the END of the path, for this process only,
    so `gi` is found there and everything else still comes from the venv."""
    try:
        import gi  # noqa: F401
        return True
    except ImportError:
        pass
    for folder in ("/usr/lib/python3/dist-packages",
                   "/usr/lib64/python%d.%d/site-packages" % sys.version_info[:2],
                   "/usr/lib/python%d.%d/site-packages" % sys.version_info[:2]):
        if os.path.isdir(os.path.join(folder, "gi")) and folder not in sys.path:
            sys.path.append(folder)
            try:
                import gi  # noqa: F401
                return True
            except ImportError:      # built for another Python version
                sys.path.remove(folder)
                sys.modules.pop("gi", None)
    return False


def keep_listener_running():
    """Start the listener; when it exits (crash, lost X server), start it
    again. Only the first start plays the startup beep."""
    global process
    first = True
    while running:
        env = dict(os.environ, WF_STARTUP_BEEP="1" if first else "0")
        started = time.monotonic()
        try:
            process = subprocess.Popen([sys.executable, "-m", "client.linux_listener"],
                                       cwd=ROOT, env=env)
            process.wait()
        except OSError as e:
            print("[Tray] couldn't start the listener: %s" % e, flush=True)
        first = False
        if running:
            # Dying at once, again and again (no display yet at login): back off.
            time.sleep(1 if time.monotonic() - started > 10 else 5)


def stop_listener():
    p = process
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def hotkey_line():
    hotkey, mode = hotkey_config.from_env(env_file.read(ENV_FILE))
    return "%s %s to dictate" % ("Hold" if mode == "hold" else "Press",
                                 linux_listener.pretty_key(hotkey))


def _open(path):
    try:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _pick_backend():
    """pystray gives up when the AppIndicator library isn't installed, instead
    of trying GTK's own status icon. Choose here (PYSTRAY_BACKEND set by hand
    wins)."""
    if os.environ.get("PYSTRAY_BACKEND"):
        return
    import gi
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            os.environ["PYSTRAY_BACKEND"] = "appindicator"
            return
        except ValueError:
            pass
    os.environ["PYSTRAY_BACKEND"] = "gtk"


def build_icon():
    _pick_backend()
    import pystray
    from PIL import Image

    def toggle(icon, item):
        linux_listener.signal_running(signal.SIGUSR1)

    def toggle_edit(icon, item):
        linux_listener.signal_running(signal.SIGUSR2)

    def restart(icon, item):
        stop_listener()              # keep_listener_running() starts a new one

    def quit_(icon, item):
        global running
        running = False
        stop_listener()
        icon.stop()

    def beeps_on(item=None):
        return env_file.get_bool(ENV_FILE, "BEEPS", True)

    def toggle_beeps(icon, item):
        env_file.set_key(ENV_FILE, "BEEPS", "false" if beeps_on() else "true")

    def window(script):
        # Its own process: Tk needs its own main thread (as on Windows).
        return lambda icon, item: subprocess.Popen(
            [sys.executable, os.path.join(HERE, script)], cwd=ROOT)

    Item, Menu = pystray.MenuItem, pystray.Menu
    menu = Menu(
        Item(lambda item: hotkey_line(), None, enabled=False),
        Menu.SEPARATOR,
        Item("Start / stop dictating", toggle, default=True),
        Item("Edit the highlighted text by voice", toggle_edit),
        Menu.SEPARATOR,
        Item("Settings", window("settings_gui.py")),
        Item("Transcribe a file...", window("file_window.py")),
        Item("History", window("history_window.py")),
        Menu.SEPARATOR,
        Item("Beeps", toggle_beeps, checked=beeps_on),
        Item("Open the listener log", lambda icon, item: _open(linux_listener.ERROR_LOG_PATH)),
        Item("Restart the listener", restart),
        Menu.SEPARATOR,
        Item("Quit", quit_),
    )
    image = Image.open(ICON_PATH).convert("RGBA").resize((64, 64), Image.LANCZOS)
    return pystray.Icon(linux_listener.SLUG, image, APP_NAME, menu)


def main():
    global running
    threading.Thread(target=keep_listener_running, name="listener-keeper", daemon=True).start()
    icon = None
    if _system_gi():
        try:
            icon = build_icon()
        except Exception as e:
            print("[Tray] no tray icon (%s: %s)" % (e.__class__.__name__, e), flush=True)
    else:
        print("[Tray] no tray icon: python3-gi isn't installed (or is for another Python).",
              flush=True)
    try:
        if icon is not None:
            icon.run()               # until Quit
        else:
            while running:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        stop_listener()
    return 0


def _terminated(*_):
    stop_listener()
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminated)
    sys.exit(main())
