"""Game mode on Linux (X11): is a game running full screen right now?

The Linux half of game_mode.py. That module keeps the rules that are the same
everywhere (the start and end delays in Tracker, pausing and resuming the
server); this one answers the question Windows answers with its "busy / full
screen" state:

- the window in front is full screen (_NET_WM_STATE_FULLSCREEN, which the
  window manager sets for games, borderless or not), and its program is not a
  browser, a video player or another everyday app that people put full screen;
- or any program named in GAME_PROCESSES is running (games in a window).

X11 only. On Wayland a program is not told which window is in front or
whether it is full screen, so there is nothing to ask and game mode does not
run there (GAME_PROCESSES alone would still work, but half a feature that
looks whole is worse than none).

Checked every few seconds by the tray (linux_tray.py). Off unless
GAME_MODE=true, as on Windows.
"""
import os

# Full screen but not a game: never pause for these (program file names).
NOT_GAMES = {
    # browsers
    "firefox", "firefox-bin", "firefox-esr", "chrome", "chromium", "chromium-browser",
    "brave", "brave-browser", "vivaldi", "vivaldi-bin", "opera", "msedge", "epiphany",
    "librewolf", "zen", "zen-bin",
    # video and music players, viewers, slides
    "vlc", "mpv", "totem", "celluloid", "smplayer", "kodi", "kodi.bin", "mplayer",
    "parole", "haruna", "showtime", "clapper", "stremio", "spotify", "eog", "loupe",
    "evince", "papers", "okular", "soffice.bin", "gwenview",
    # everyday apps people put full screen (F11)
    "gnome-terminal-server", "ptyxis", "kgx", "konsole", "alacritty", "kitty", "foot",
    "wezterm-gui", "xterm", "tilix", "terminator", "ghostty", "code", "codium",
    "gedit", "gnome-text-editor", "kate", "nautilus", "dolphin", "obs", "zoom",
    "teams", "slack", "discord",
    # remote desktops and virtual machines
    "remmina", "vncviewer", "xfreerdp", "virt-viewer", "virt-manager", "gnome-boxes",
    "virtualboxvm", "vmware", "rustdesk",
}


def parse_list(raw):
    """'Game.exe, factorio' -> {'game.exe', 'game', 'factorio'}. A Windows game
    under Proton runs as Game.exe, a native one has no ending: accept either."""
    names = set()
    for p in (raw or "").split(","):
        n = p.strip().strip('"').lower()
        if n:
            names.add(n)
            names.add(n[:-4] if n.endswith(".exe") else n + ".exe")
    return names


def running_programs(proc="/proc"):
    """Lowercase names of every running program: the file each process runs,
    and the name it goes by (which is what a Proton game's "Game.exe" is)."""
    names = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return names
    for pid in pids:
        try:
            names.add(os.path.basename(os.readlink(os.path.join(proc, pid, "exe"))).lower())
        except OSError:
            pass                     # another user's process, or it just ended
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                first = f.read(4096).split(b"\0")[0].decode("utf-8", "replace")
            first = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if first:
                names.add(first)
        except OSError:
            pass
    return names


def front_fullscreen(desk):
    """(the window in front is full screen, the names its program goes by:
    file name and window class). (False, set()) when it can't be told."""
    try:
        wid = desk.active_window()
        if not wid:
            return False, set()
        exe, cls = desk.program(wid)
        names = {(exe or "").lower(), (cls or "").lower()} - {""}
        return bool(desk.is_fullscreen(wid)), names
    except Exception:
        return False, set()


def is_gaming(desk, extra_games=frozenset(), running=None, front=None):
    """True if a game appears to be running. running and front are
    injectable for tests."""
    if extra_games:
        procs = running if running is not None else running_programs()
        if procs & set(extra_games):
            return True
    full, names = front_fullscreen(desk) if front is None else front
    return bool(full) and not (set(names) & NOT_GAMES)
