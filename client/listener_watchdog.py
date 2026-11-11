import os
import subprocess
import threading
import sys
import time
import pystray
import ctypes
from PIL import Image, ImageDraw
# State
process = None
running = True
# Handle on the settings window, so a second click reuses it instead of
# opening a rival editor for the same .env.
settings_process = None


def ensure_single_instance():
    """Exit immediately if another watchdog is already running.

    Without this, launching the watchdog twice (e.g. the login startup task AND
    a manual LAUNCHER.bat, or relaunching before quitting the tray icon) spawns a
    second watchdog -- and each watchdog spawns its own background_listener, so
    you end up with duplicate hotkey listeners double-firing every transcription.

    A Windows named mutex makes the second launch a no-op. The handle is kept for
    the life of the process (Windows frees it automatically on exit).
    """
    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "FasterWhisperWatchdogSingleton")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # Another watchdog already owns the mutex; this instance is redundant.
        os._exit(0)
    return handle

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "app_icon.png")


def create_tray_icon():
    # The same waveform icon as the WhisperRemote Android app.
    try:
        return Image.open(ICON_PATH).convert("RGBA").resize((64, 64), Image.LANCZOS)
    except Exception:
        pass
    # Fallback if the asset is missing: a simple red circle.
    width, height = 64, 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    dc.ellipse((16, 16, 48, 48), fill=(220, 20, 60, 255), outline=(255, 255, 255, 255), width=2)
    return image

def run_subprocess():
    global process, running
    
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background_listener.py")
    
    # Force pythonw.exe so the child listener creates absolutely no console window
    python_exe = sys.executable
    if "python.exe" in python_exe.lower() and "pythonw.exe" not in python_exe.lower():
        pythonw_path = python_exe.lower().replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw_path):
            python_exe = pythonw_path
            
    first_launch = True
    while running:
        print("[Watchdog] Starting child listener process...")
        # Only the very first launch plays the startup beep; periodic restarts stay silent.
        child_env = os.environ.copy()
        child_env["WF_STARTUP_BEEP"] = "1" if first_launch else "0"
        try:
            # We don't pipe stdout because pythonw suppresses it anyway, and we don't strictly need it
            process = subprocess.Popen([python_exe, script_path], creationflags=subprocess.CREATE_NO_WINDOW, env=child_env)
            process.wait()
        except Exception as e:
            print(f"[Watchdog] Error running listener: {e}")

        first_launch = False
        if running:
            # If the process exited gracefully (idle timeout) or crashed, give it a tiny breath, then restart it.
            time.sleep(1)

def _kill_listener_tree():
    """Kill the child listener process AND any grandchild it spawned.

    Popen hands us the venv's pythonw stub, which launches the real interpreter as
    its own child. Terminating only the stub can leave that grandchild alive -- it
    would keep the backtick hotkey registered, and once the watchdog spawns a
    replacement you'd have two listeners double-firing every transcription.
    taskkill /T kills the whole tree, so a restart or quit is always clean.
    """
    global process
    if not process:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True,
        )
    except Exception as e:
        print(f"[Watchdog] taskkill failed ({e}); falling back to terminate()")
        try:
            process.terminate()
        except Exception:
            pass


def restart_listener(icon, item):
    """Restart just the listener child, leaving the tray icon and API untouched.

    The hotkey can stop responding while the listener process is still alive --
    Windows silently drops a low-level keyboard hook if the process is busy too
    long, or after a UAC prompt. The watchdog only respawns on process EXIT, so it
    can't detect a dead hook. Killing the child here makes run_subprocess()'s
    process.wait() return and the loop starts a fresh listener within ~1 second.
    """
    print("[Watchdog] Restarting listener child on request...")
    _kill_listener_tree()
    icon.notify("Listener restarting, backtick should work in a moment.", "Listener Restarted")


def on_quit(icon, item):
    global running, process
    print("[Watchdog] Quitting via system tray...")
    running = False

    _kill_listener_tree()

    icon.stop()
    os._exit(0)

def open_settings(icon, item):
    """Open the settings window as its own process.

    Deliberately NOT a thread: pystray owns this process's main thread and Tk
    requires the main thread of its own interpreter, so the two cannot share
    one. A subprocess is the only arrangement where the tray stays responsive
    while the window is open.

    Reuses the existing window if it is already open, since two of them editing
    the same .env would let the second one overwrite the first one's save.
    """
    global settings_process
    if settings_process is not None and settings_process.poll() is None:
        icon.notify("The settings window is already open.", "Settings")
        return

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_gui.py")
    # pythonw so no console window flashes up behind the settings window.
    python_exe = sys.executable
    if "python.exe" in python_exe.lower():
        pythonw = python_exe.lower().replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw):
            python_exe = pythonw
    try:
        settings_process = subprocess.Popen(
            [python_exe, script], creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print(f"[Watchdog] Could not open settings: {e}")
        icon.notify(f"Could not open settings: {e}", "Settings")


def restart_api(icon, item):
    print("[Watchdog] Requesting elevated restart of FasterWhisperAPI...")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", "-WindowStyle Hidden -Command Restart-Service -Name FasterWhisperAPI -Force", None, 0)
    icon.notify("Reloading Config and AI Model...", "API Restarting")

def pause_api(icon, item):
    print("[Watchdog] Requesting elevated pause of FasterWhisperAPI...")
    # 0 = SW_HIDE (hides the cmd window, but UAC prompt still shows)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", "/c net stop FasterWhisperAPI", None, 0)
    # Stopping the service frees Whisper's VRAM, but a pinned Ollama edit model
    # (keep_alive -1) would stay resident. Unload ours from the local Ollama too.
    # No elevation needed for this part.
    try:
        from ollama_unload import read_env, unload_our_models
        env = read_env(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
        unloaded, errors = unload_our_models(env)
    except Exception as e:
        unloaded, errors = [], [str(e)]
    msg = "Freeing VRAM... Waiting for Windows Service to stop."
    if unloaded:
        msg += " Unloaded from Ollama: " + ", ".join(unloaded) + "."
    if errors:
        msg += " Ollama unload failed: " + "; ".join(errors)
        print("[Watchdog] Ollama unload errors: %s" % errors)
    icon.notify(msg, "API Paused")

def resume_api(icon, item):
    print("[Watchdog] Requesting elevated resume of FasterWhisperAPI...")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", "/c net start FasterWhisperAPI", None, 0)
    icon.notify("Loading AI Model into VRAM...", "API Resumed")

def setup_tray():
    icon_image = create_tray_icon()
    menu = pystray.Menu(
        pystray.MenuItem('Settings...', open_settings),
        pystray.Menu.SEPARATOR,
        # No elevation needed and it's the usual fix when backtick goes dead,
        # so it sits above the API actions (which all trigger a UAC prompt).
        pystray.MenuItem('Restart Listener (fix dead hotkey)', restart_listener),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Restart API (Apply Config Changes)', restart_api),
        pystray.MenuItem('Resume API (Load Model)', resume_api),
        pystray.MenuItem('Pause API (Free VRAM)', pause_api),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit Faster-Whisper Listener', on_quit)
    )
    icon = pystray.Icon("faster_whisper_listener", icon_image, "Faster-Whisper Listener", menu)
    
    # Start the subprocess loop in a background daemon thread
    t = threading.Thread(target=run_subprocess, daemon=True)
    t.start()
    
    # Run the tray icon (this blocks the main thread)
    icon.run()

if __name__ == "__main__":
    # Bail out if a watchdog is already running, so we never end up with
    # duplicate listeners double-firing every transcription.
    _singleton_handle = ensure_single_instance()

    # If the user double clicked the watchdog and it opened a console window by mistake (e.g., using python.exe)
    # the creationflags=subprocess.CREATE_NO_WINDOW in Popen will still ensure the child is invisible.
    setup_tray()
