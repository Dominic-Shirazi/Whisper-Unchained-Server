#!/usr/bin/env bash
# Whisper Unchained on a Linux desktop: the hotkey listener and tray icon.
# No root. Run it as the person who will dictate, from their desktop session.
#
#   scripts/install_linux_desktop.sh
#
# The audio goes to this machine's own server (scripts/install_linux.sh) unless
# you point it at another PC running Whisper Unchained Server:
#
#   scripts/install_linux_desktop.sh --remote https://192.168.1.20:5001/v1 \
#                                    --pin <the server's certificate SHA-256>
#
# (it then asks for that server's API key; nothing is echoed or printed).
#
# Options:
#   --remote URL     send audio to another PC's server (its address ending in /v1)
#   --pin SHA256     that server's certificate fingerprint (HTTPS with its own cert)
#   --no-autostart   don't start at login (start by hand: .venv/bin/python -m client.linux_tray)
#   --uninstall      stop it and remove the login service and the Files menu entry
#
# Safe to re-run. X11 gets the full listener; on Wayland see the note it prints.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="whisper-unchained-desktop"
UNIT_FILE="$HOME/.config/systemd/user/$UNIT_NAME.service"
MENU_ENTRY="$HOME/.local/share/nautilus/scripts/Transcribe with Whisper Unchained"
REMOTE=""
PIN=""
AUTOSTART=1
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --remote) REMOTE="$2"; shift ;;
        --pin) PIN="$2"; shift ;;
        --no-autostart) AUTOSTART=0 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
if [ "$(id -u)" -eq 0 ]; then
    echo "Don't use sudo: this installs for the person who dictates." >&2
    exit 2
fi

say() { printf '\n== %s\n' "$*"; }
cd "$REPO"

if [ "$UNINSTALL" = 1 ]; then
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE" "$MENU_ENTRY"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Removed the login service and the Files menu entry. The folder, .venv and .env are untouched."
    exit 0
fi

# --- Python + venv -----------------------------------------------------------
say "Python environment"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$(command -v "$c")"; break
    fi
done
[ -n "$PY" ] || { echo "Needs Python 3.10+ with venv (apt install python3-venv)." >&2; exit 1; }
# The system's own Python where possible: the tray icon borrows its python3-gi.
[ -x /usr/bin/python3 ] && /usr/bin/python3 -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && PY=/usr/bin/python3
[ -x .venv/bin/python ] || "$PY" -m venv .venv
VPY="$REPO/.venv/bin/python"
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q -r requirements-linux-desktop.txt -c constraints.txt
echo "venv: $REPO/.venv ($("$VPY" --version))"

# --- .env ---------------------------------------------------------------------
say ".env"
[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
chmod 600 .env
if [ -n "$REMOTE" ]; then
    KEY=""
    if [ -t 0 ]; then
        read -r -s -p "API key of that server (Enter: keep the one in .env): " KEY; echo
    fi
    REMOTE="$REMOTE" PIN="$PIN" KEY="$KEY" "$VPY" - <<'PYEOF'
import os, sys
sys.path.insert(0, "client")
import env_file
env_file.set_key(".env", "TRANSCRIBE_SOURCE", "remote")
env_file.set_key(".env", "REMOTE_SERVER_URL", os.environ["REMOTE"].rstrip("/"))
if os.environ["PIN"]:
    env_file.set_key(".env", "REMOTE_CERT_SHA256", os.environ["PIN"])
if os.environ["KEY"]:
    env_file.set_key(".env", "REMOTE_API_KEY", os.environ["KEY"])
print("audio goes to %s" % os.environ["REMOTE"])
PYEOF
fi

# --- What this desktop has ------------------------------------------------------
say "This desktop"
CHECK=0
"$VPY" -m client.linux_listener --check || CHECK=$?
MISSING=""
command -v xclip >/dev/null || command -v xsel >/dev/null || command -v wl-copy >/dev/null || MISSING="$MISSING xclip"
"$VPY" -c 'import tkinter' 2>/dev/null || MISSING="$MISSING python3-tk"
/usr/bin/python3 -c 'import gi' 2>/dev/null || MISSING="$MISSING python3-gi"
command -v pw-record >/dev/null || command -v parecord >/dev/null || command -v arecord >/dev/null || MISSING="$MISSING pipewire-bin"
[ -z "$MISSING" ] || echo "Worth installing:  sudo apt install$MISSING"
if [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    echo
    echo "Wayland: programs can't see your keys, so there's no built-in hotkey."
    echo "Add a keyboard shortcut in your desktop's settings that runs:"
    echo "  $VPY -m client.linux_listener --toggle"
    echo "Pasting needs wtype or ydotool; without one the text is left on the clipboard."
    echo "(Or pick 'Ubuntu on Xorg' at the login screen for the full listener.)"
fi

# --- Right-click "Transcribe" in the Files app ------------------------------------
if command -v nautilus >/dev/null; then
    mkdir -p "$(dirname "$MENU_ENTRY")"
    printf '#!/bin/sh\n# Right-click > Scripts > this. Opens the "Transcribe a file" window.\nfor f in "$@"; do\n    "%s" "%s/client/file_window.py" "$f" &\ndone\n' "$VPY" "$REPO" > "$MENU_ENTRY"
    chmod +x "$MENU_ENTRY"
    echo "Files app: right-click a recording > Scripts > Transcribe with Whisper Unchained"
fi

[ "$AUTOSTART" = 1 ] || { say "Done. Start it: $VPY -m client.linux_tray"; exit "$CHECK"; }

# --- Start at login -----------------------------------------------------------------
say "Start at login (systemd --user)"
mkdir -p "$(dirname "$UNIT_FILE")"
cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Whisper Unchained (hotkey listener and tray icon)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
WorkingDirectory=$REPO
ExecStart=$VPY -m client.linux_tray
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME" >/dev/null
systemctl --user restart "$UNIT_NAME"
sleep 2
if systemctl --user is-active --quiet "$UNIT_NAME"; then
    say "Running. Look for the icon in the top bar."
else
    echo "It didn't stay up. Check: journalctl --user -u $UNIT_NAME -e" >&2
    exit 1
fi
echo "Status: systemctl --user status $UNIT_NAME    Logs: journalctl --user -u $UNIT_NAME -f"
echo "Hotkey and other settings: $REPO/.env (HOTKEY, HOTKEY_MODE, EDIT_HOTKEY, MIC_DEVICE)"
exit "$CHECK"
