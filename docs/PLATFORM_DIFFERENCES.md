# Windows vs Linux: what works where

The living list of features that behave differently, or don't exist, on Linux.
Update it in the same commit as any change that adds, ports or drops a feature.
"Server" = the transcription server (`api/`), "Desktop" = the tray, hotkey,
floating button and Settings (`client/`).

Status words: **Same** (works the same), **Different** (works, but differently:
say how), **Not on Linux** (missing; say why), **Untested** (should work, not yet
checked on a Linux machine).

## Server

| Feature | Windows | Linux | Notes |
|---|---|---|---|
| Transcription (`/transcribe`, `/v1`, `/dictate/v1`) | Yes | Same | |
| Plain HTTP 5000 + HTTPS 5001, phone pairing | Yes | Different | Linux pairs from the terminal (`python -m client.pair`). `install_linux.sh` turns the plain port off (`SERVER_PLAIN_HTTP=false`) and moves either port to the next free one if taken. |
| Voice-edit highlighted text (`/dictate/v1/edit_selection`) | Yes | Same | Checked on AI-Worker3 2026-11-13 (edit model on an OpenAI-compatible server). Server side only; the desktop part is Windows-only (below). |
| Per-dictation style fields (`cleanup_*` on `/dictate/v1`) | Yes | Same | Checked on AI-Worker3 2026-11-13. Used by the phone pill and per-app styles. |
| GPU/CPU choice (`api/device_select.py`), CPU fallback | Yes | Same | Checked on AI-Worker3 2026-11-13: GPU; forced CPU; `auto` with no GPU visible; `auto` and `cuda`+`int8_float16` on a full GPU (out of memory -> CPU int8, reason in `/health`). `install_linux.sh` also picks the GPU with the most free memory and a compute type that fits. The crash marker survives systemd restarts (`PrivateTmp` keeps /tmp across auto-restarts). Needs `psutil` for the core count (now in requirements.txt). |
| API keys encrypted at rest (`api/secrets_store.py`) | DPAPI, machine scope | Not on Linux | No DPAPI. Plain text in `.env` (mode 600, owner only). A Windows `dpapi:` value copied to Linux is treated as missing; for the server's own key that locks the server (checked: every request gets 401). Re-running `install_linux.sh` replaces a `dpapi:` server key with a new one (phones re-pair). The server's message names both fixes (Settings on Windows, re-running `install_linux.sh` on Linux). |
| Game mode (free the GPU while gaming) | Yes | Not on Linux | Needs Windows full-screen detection. X11 could use `_NET_WM_STATE_FULLSCREEN`; Wayland doesn't expose it. |
| Runs as a background service | Windows service (NSSM) | Different | systemd unit: a system service as its own user (`sudo install_linux.sh`) or a `--user` service. |
| Cleanup prompt (incl. spoken "slash") | Yes | Same | Shared `api/cleanup_prompt.json`. Checked: "input slash output" -> "input/output" with `cleanup=on`. |

## Desktop (hotkey, tray, floating button, Settings)

Nothing desktop runs on Linux yet. The "Linux" column is what's realistic, from
the assessment below. "Not on Linux (yet)" = portable, not built.

| Feature | Windows | Linux | Notes |
|---|---|---|---|
| Hotkey listener (record, transcribe, paste) | Yes | Not on Linux (yet) | X11: portable (global hotkey via `pynput`/Xlib, paste via `xclip` + `xdotool key ctrl+v`). Wayland: hotkey only as a desktop-environment shortcut that runs a command; typing via `ydotool` (needs `/dev/uinput` access) or `wtype` (not on GNOME). |
| Tray icon | Yes | Not on Linux (yet) | `pystray` with the AppIndicator backend needs `gir1.2-ayatanaappindicator3-0.1`; GNOME also needs the AppIndicator extension (Ubuntu ships it). Works on X11 and Wayland. |
| Settings window | Yes | Not on Linux (yet) | Tk itself runs on Linux. Blocked by Windows parts inside `settings_gui.py`: `winreg`, the Windows service, Task Scheduler, startup entries. Needs those behind platform checks; a big shared-file change. |
| Floating mic button at the text cursor | Yes | Not on Linux | Needs the caret position. AT-SPI gives it only in apps that expose it (GTK; Firefox/Chrome/Electron only with accessibility switched on), and on Wayland a window can't place itself at screen coordinates. |
| Style pill + style sheet while recording | Yes | Not on Linux (yet) | A Tk window; fine on X11. On Wayland it can't stay on top or be placed. |
| Return to the original text box before pasting | Yes | Not on Linux | X11: `xdotool windowactivate` can bring the window back, but not the exact text box inside it. Wayland doesn't let a program focus another window at all. |
| Edit highlighted text by voice (desktop side) | Yes | Not on Linux (yet) | X11: the PRIMARY selection (`xclip -o -selection primary`) is the highlighted text, then paste the result. Wayland: only via ctrl+c through `ydotool`. |
| Per-app styles, "Hide in this app", program picker | Yes | Not on Linux (yet) | X11: the active window's program via `xdotool getactivewindow getwindowpid`. Wayland: not available to programs. The picker reads the Start menu and UserAssist (Windows); Linux would read `.desktop` files. |
| Right-click "Transcribe" on files | Explorer menu | Not on Linux (yet) | GNOME Files: a script in `~/.local/share/nautilus/scripts/`. |
| Keep dictations out of clipboard history | Yes | Not on Linux | No standard: each clipboard manager has its own (or no) "don't record" hint. |

### Linux desktop assessment (2026-11-13, AI-Worker3)

AI-Worker3 runs GNOME on **X11**, with `xdotool`, `xclip`, AT-SPI and Tk 8.6, so
an X11 listener (hotkey -> record -> `/dictate/v1` -> paste) is the
straightforward first port, as new `client/linux_*.py` modules. Not built:
this box has no microphone (only the onboard analog input) and isn't a
desktop anyone dictates on, so it couldn't be tested end to end. Build it on a
Linux machine with a mic. Ubuntu's default is Wayland, where only the
listener and the tray are realistic (see the table).
