"""What the Settings window (settings_gui.py) does differently on Linux.

settings_gui.py is one window for both systems. Where Windows uses the
registry, PowerShell scripts, a Windows service or a UAC prompt, it asks this
module instead (every call site is behind `sys.platform != "win32"`), so the
Linux answers live in one place and the Windows code paths are unchanged.

Also drops the settings that do nothing on Linux (floating button, game
mode, clipboard history, LAUNCHER.bat...) and adds the Linux-only ones the
listener reads (linux_listener.py): EDIT_HOTKEY, MIC_DEVICE, BEEPS,
RECORDING_INDICATOR.
"""
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESKTOP_UNIT = "whisper-unchained-desktop"
SERVER_UNIT = "whisper-unchained"
MENU_ENTRY = os.path.expanduser(
    "~/.local/share/nautilus/scripts/Transcribe with Whisper Unchained")

# Settings that only mean something on Windows (see docs/PLATFORM_DIFFERENCES.md).
WINDOWS_ONLY = {
    "FLOATING_MIC_BUTTON", "FLOATING_MIC_HIDE_APPS", "CLIPBOARD_PRIVATE",
    "RESTART_INTERVAL_HOURS", "API_VISIBLE",
    "LISTENER_RESTART_INTERVAL_MINS", "LISTEN_DELAY_SECONDS", "_h_listener",
}

# Page intros that mention Windows-only things.
INTROS = {
    "Hotkey": "How you start dictating on this PC.\n"
              "• The hotkey, and the key that edits highlighted text",
    "Startup": "How these windows look, and what starts with your desktop.",
}

# The footer's "Save and restart" tip: no Windows permission prompt here.
SAVE_RESTART_TIP = ("Saves your changes and restarts dictation so they take effect.\n"
                    "Dictation stops for up to a minute while it restarts.")
# Linux-only settings most people never change: under the Hotkey page's
# "Advanced settings" (settings_gui.ADVANCED_KEYS).
ADVANCED_KEYS = {"MIC_DEVICE", "PROMPT_HOTKEY", "GAME_PROCESSES"}
# Game mode has to know which window is in front and whether it is full
# screen. X11 says; Wayland doesn't, so the switch isn't shown there.
GAME_KEYS = {"GAME_MODE", "GAME_PROCESSES", "_h_server"}   # and their heading


def _wayland():
    # The same rule as linux_listener.session_type().
    kind = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    return kind == "wayland" or (not kind and bool(os.environ.get("WAYLAND_DISPLAY")))

# (key, title, help): the same two rows as Windows, in Linux terms.
INTEGRATIONS = [
    ("menu", "Right-click \"Transcribe\" in the Files app",
     "Right-click an audio or video file > Scripts > Transcribe with Whisper Unchained.\n"
     "• Opens the Transcribe a file window with that file\n"
     "• GNOME Files (Nautilus) only",
     None),
    ("login", "Start when I log in",
     "Starts the tray icon and hotkey when you sign in.",
     None),
]


def trigger_example(example):
    """The custom-trigger example (settings_gui.CUSTOM_TRIGGER_EXAMPLE) with
    Linux's way of starting an edit: there is no mic button to click."""
    return tuple((text.replace("click the mic button and say ",
                               "press Shift + your hotkey and say "), tag)
                 for text, tag in example)


def adapt(schema, Field):
    """The Settings pages for Linux: Windows-only settings removed, the
    Linux listener's own added to the Hotkey page."""
    out = []
    hidden = WINDOWS_ONLY | (GAME_KEYS if _wayland() else set())
    for name, fields in schema:
        fields = [f for f in fields if f.key not in hidden]
        for f in fields:
            if f.key == "GAME_PROCESSES":
                # Windows picks from its open windows (app_picker.py); here, names.
                f.kind = "otext"
                f.help = ("Optional, for games that run in a window.\n"
                          "• Program names with commas between: factorio, Game.exe\n"
                          "• Dictation pauses while any of these is running")
        if name == "Hotkey":
            for f in fields:
                if f.key == "HOTKEY":
                    f.help = ("Pick a preset, or Custom... for your own key combo.\n"
                              "• Best: keys that type nothing (Pause, Ctrl+Alt+Space, F9)\n"
                              "• In a terminal F9 types a code; Pause doesn't\n"
                              "• The backtick types a ` that must be deleted before pasting\n"
                              "• Applies after Restart the listener in the tray")
                if f.key == "EDIT_SELECTION":
                    f.help = ("Highlight text, press the edit key below, and say what to do.\n"
                              "• e.g. \"make this more polite\", \"translate to Spanish\"\n"
                              "• The AI's version replaces the highlight\n"
                              "• Uses the editor on the AI Editing page")
            at = next((i for i, f in enumerate(fields) if f.key == "EDIT_SELECTION"), len(fields) - 1)
            fields[at + 1:at + 1] = [
                Field("EDIT_HOTKEY", "Edit key", "otext",
                      "The key that edits highlighted text.\n"
                      "• Empty: Shift + your dictation hotkey\n"
                      "• Or your own (ctrl+f9), or off\n"
                      "• Applies after Restart the listener in the tray"),
            ]
            fields += [
                Field("PROMPT_HOTKEY", "Typed instruction key", "otext",
                      "Opens a box to type an instruction for the AI instead of saying it.\n"
                      "• Empty: none (it's also in the tray menu)\n"
                      "• e.g. ctrl+alt+p\n"
                      "• Applies after Restart the listener in the tray"),
                Field("MIC_DEVICE", "Microphone", "otext",
                      "Empty: the system's default microphone.\n"
                      "• Or a source name from: pactl list short sources\n"
                      "• Applies to the next dictation"),
                Field("BEEPS", "Beeps", "toggle",
                      "A tone when recording starts and stops.", default="true"),
                Field("RECORDING_INDICATOR", "Show \"Recording\" on screen", "toggle",
                      "A small card at the bottom of the screen while the microphone is open.",
                      default="true"),
            ]
        out.append((name, fields))
    return out


def parse_hotkey(text):
    """Raises for a hotkey the Linux listener can't use (hotkey_config.validate)."""
    import linux_hotkeys
    return linux_hotkeys.parse(text)


def open_path(path):
    """Open a folder or file in the desktop's own app."""
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _systemctl(*args, timeout=20):
    try:
        r = subprocess.run(["systemctl", "--user"] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def integration_on(key):
    if key == "menu":
        return os.path.exists(MENU_ENTRY)
    return _systemctl("is-enabled", DESKTOP_UNIT)[1] == "enabled"


def _python():
    return os.path.join(ROOT, ".venv", "bin", "python")


def toggle_integration(key, remove):
    """Add or remove one row. True if it worked."""
    try:
        if key == "menu":
            if remove:
                os.remove(MENU_ENTRY)
                return True
            os.makedirs(os.path.dirname(MENU_ENTRY), exist_ok=True)
            with open(MENU_ENTRY, "w", encoding="utf-8") as f:
                f.write('#!/bin/sh\n# Right-click > Scripts > this. Opens the "Transcribe a file" '
                        'window.\nfor f in "$@"; do\n    "%s" "%s/client/file_window.py" "$f" &\n'
                        'done\n' % (_python(), ROOT))
            os.chmod(MENU_ENTRY, 0o755)
            return True
        if remove:
            # Off at the next login; the listener that is running now stays.
            return _systemctl("disable", DESKTOP_UNIT)[0]
        unit = os.path.expanduser("~/.config/systemd/user/%s.service" % DESKTOP_UNIT)
        if not os.path.exists(unit):
            # Never installed as a service: the installer writes the unit.
            r = subprocess.run([os.path.join(ROOT, "scripts", "install_linux_desktop.sh")],
                               capture_output=True, timeout=300)
            return r.returncode == 0
        return _systemctl("enable", DESKTOP_UNIT)[0]
    except (OSError, subprocess.SubprocessError):
        return False


def restart_server():
    """Restart the transcription server after a save. (ok, message)."""
    if os.path.exists(os.path.expanduser("~/.config/systemd/user/%s.service" % SERVER_UNIT)):
        ok, _ = _systemctl("restart", SERVER_UNIT, timeout=60)
        return ok, ("Saved. The server is restarting." if ok else
                    "Saved, but the restart failed: journalctl --user -u %s -e" % SERVER_UNIT)
    if os.path.exists("/etc/systemd/system/%s.service" % SERVER_UNIT):
        if shutil.which("pkexec"):       # the desktop's own password prompt
            subprocess.Popen(["pkexec", "systemctl", "restart", SERVER_UNIT],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "Saved. Enter your password to restart the server."
        return False, "Saved. Restart it with: sudo systemctl restart %s" % SERVER_UNIT
    return False, "Saved. No server service on this machine; restart the server by hand."


def restart_listener():
    """The tray restarts the listener when it exits. True if one was running."""
    import signal
    import linux_listener
    return linux_listener.signal_running(signal.SIGTERM)
