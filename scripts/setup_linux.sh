#!/usr/bin/env bash
# Whisper Unchained on a Linux desktop: step 1 of 2. Gets Python ready, then
# opens the setup window (installer/linux_wizard.py), which does the rest and
# asks the same things as the Windows setup.
#
#   scripts/setup_linux.sh               no root, nothing asked here
#   scripts/setup_linux.sh --dry-run     the window saves settings and
#                                        downloads, but starts no service
#
# A PC with no screen (a server): use scripts/install_linux.sh instead.
# Safe to run again: it carries on where it stopped.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [ "$(id -u)" -eq 0 ]; then
    echo "Don't use sudo: this sets dictation up for the person who is logged in." >&2
    exit 2
fi
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "There is no screen to show the setup window on." >&2
    echo "On a server, run: scripts/install_linux.sh --user   (or with sudo for a system service)" >&2
    exit 2
fi

# The system's own Python where possible: the tray icon borrows its python3-gi.
PY=""
for c in /usr/bin/python3 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$(command -v "$c")"; break
    fi
done
[ -n "$PY" ] || { echo "Setup needs Python 3.10 or newer. On Ubuntu: sudo apt install python3" >&2; exit 1; }

# Two things only the system can provide, and installing them needs your
# password, so setup can't do it for you: say the one command and stop.
NEED=""
"$PY" -c 'import venv, ensurepip' 2>/dev/null || NEED="$NEED python3-venv"
"$PY" -c 'import tkinter' 2>/dev/null || NEED="$NEED python3-tk"
if [ -n "$NEED" ]; then
    echo "Setup needs two small pieces of Python that are not on this PC yet." >&2
    echo "Run this, then run setup again:" >&2
    echo "  sudo apt install$NEED" >&2
    exit 1
fi

VPY="$REPO/.venv/bin/python"
if [ ! -x "$VPY" ] || ! "$VPY" -c 'import faster_whisper, pystray' 2>/dev/null; then
    echo "Getting setup ready: about 200 MB of programs. This can take a few minutes."
    [ -x "$VPY" ] || "$PY" -m venv .venv
    "$VPY" -m pip install -q --upgrade pip
    echo "  1 of 2: what dictation is made of..."
    "$VPY" -m pip install -q -r requirements.txt -c constraints.txt
    echo "  2 of 2: the hotkey and tray icon..."
    "$VPY" -m pip install -q -r requirements-linux-desktop.txt -c constraints.txt
fi

echo "Opening the setup window..."
exec "$VPY" installer/linux_wizard.py "$@"
