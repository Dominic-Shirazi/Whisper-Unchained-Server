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
| A typed instruction for the AI (`/dictate/v1/edit_text`) | Yes | Same | No audio: the instruction is text, the highlight optional. Server side only; the box you type in (the floating mic button's right-click panel) is Windows-only. |
| Per-dictation style fields (`cleanup_*` on `/dictate/v1`) | Yes | Same | Checked on AI-Worker3 2026-11-13. Used by the phone pill and per-app styles. |
| GPU/CPU choice (`api/device_select.py`), CPU fallback | Yes | Same | Checked on AI-Worker3 2026-11-13: GPU; forced CPU; `auto` with no GPU visible; `auto` and `cuda`+`int8_float16` on a full GPU (out of memory -> CPU int8, reason in `/health`). `install_linux.sh` also picks the GPU with the most free memory and a compute type that fits. The crash marker survives systemd restarts (`PrivateTmp` keeps /tmp across auto-restarts). Needs `psutil` for the core count (now in requirements.txt). |
| API keys encrypted at rest (`api/secrets_store.py`) | DPAPI, machine scope | Not on Linux | No DPAPI. Plain text in `.env` (mode 600, owner only). A Windows `dpapi:` value copied to Linux is treated as missing; for the server's own key that locks the server (checked: every request gets 401). Re-running `install_linux.sh` replaces a `dpapi:` server key with a new one (phones re-pair). The server's message names both fixes (Settings on Windows, re-running `install_linux.sh` on Linux). |
| Game mode (free the GPU while gaming) | Yes | Not on Linux | Needs Windows full-screen detection. X11 could use `_NET_WM_STATE_FULLSCREEN`; Wayland doesn't expose it. |
| Runs as a background service | Windows service (NSSM) | Different | systemd unit: a system service as its own user (`sudo install_linux.sh`) or a `--user` service. |
| Cleanup prompt (incl. spoken "slash") | Yes | Same | Shared `api/cleanup_prompt.json`. Checked: "input slash output" -> "input/output" with `cleanup=on`. |

## Desktop (hotkey, tray, floating button, Settings)

The Linux desktop is its own small set of files (`client/linux_listener.py`,
`linux_tray.py`, `linux_x11.py`, `linux_hotkeys.py`), installed by
`scripts/install_linux_desktop.sh` (no root; a `systemd --user` service). It
shares `.env`, the hotkey strings, per-app styles and `transcribe_client.py`
with Windows. "Linux" below means an **X11** session unless it says Wayland;
Wayland has its own section below. "Not on Linux (yet)" =
portable, not built.

| Feature | Windows | Linux | Notes |
|---|---|---|---|
| Hotkey listener (record, transcribe, paste) | Yes | Different | Checked on AI-Worker3 2026-11-16 (X11, GNOME 46) with a virtual microphone: F9 and backtick (toggle), Ctrl+Alt+Space (hold). Keys are heard through X's RECORD extension (`python-xlib`, no root), the microphone through `pw-record` / `parecord` / `arecord` (16 kHz mono, kept in memory under `$XDG_RUNTIME_DIR`, deleted after each dictation), the paste is the clipboard (`xclip`) plus a pressed Ctrl+V. Modifiers must match exactly: Shift+F9 is not F9. `MIC_DEVICE` in `.env` picks a microphone other than the default. |
| Pasting into terminals | Ctrl+V | Different | Terminals paste with Ctrl+Shift+V: sent when the program in front is a known terminal (`TERMINALS` in `linux_listener.py`, more with `TERMINAL_APPS=a,b` in `.env`). `PASTE_KEYS=shift+insert` forces one combination everywhere. Checked in GNOME Terminal. F9 types an escape code in a terminal; Pause or Ctrl+Alt+Space don't. |
| Beeps | Yes | Same | Same tones, played with `pw-play` / `paplay` / `aplay`. `BEEPS=false` in `.env` (or the tray) turns them off: Linux only. |
| "Transcribing..." card | Middle of the screen | Different | A card at the bottom of the screen, which also shows "Recording" while the microphone is open (`RECORDING_INDICATOR=false` hides that), since there is no floating button to turn red. Without `python3-tk`: desktop notifications. |
| Tray icon | Yes | Different | `pystray` through the system's `python3-gi` (borrowed by the venv: same Python version needed). Uses AppIndicator when its library is installed, else GTK's status icon, which GNOME shows through Ubuntu's AppIndicator extension. Menu: start/stop, edit highlighted text, Settings, Transcribe a file, History, beeps, open the log, restart the listener, quit. No game mode or "restart the server" entries. Without `python3-gi`: no icon, the listener still runs. |
| Keeps the listener running | Watchdog, restarts it every 10 idle minutes | Different | The tray process restarts the listener when it exits, and systemd restarts the tray. No timed restart: X doesn't drop the key hook the way Windows does. |
| A dictation the server couldn't take is kept and retried | Yes | Same | Same `client/kept_recordings.py`: the recording is kept in `client/kept_recordings/`, retried every 15 seconds, and the text put on the clipboard with a notice. The retry logic is covered by tests; not yet tried against a server that is really down. |
| Settings window | Yes | Different | The same `settings_gui.py`; its Windows parts ask `client/linux_settings.py` on Linux. Opened and saved on AI-Worker3 2026-11-16 (tray > Settings). Windows-only settings are left out (floating button, clipboard history, game mode, scheduled restart, LAUNCHER.bat); the Hotkey page adds the edit key, microphone, beeps and the Recording card. "Desktop integration" switches the Files right-click script and start at login (systemd). "Save & Restart API" restarts the `whisper-unchained` systemd unit (a system-wide one asks for the password through pkexec: untested). The program pickers for per-app styles list nothing on Linux. |
| "Transcribe a file" window | Yes | Same | `file_window.py` runs unchanged (opened 2026-11-16; tray menu). |
| History window | Yes | Different | `history_window.py` runs unchanged, for a server on the same machine. "Play recording" uses `os.startfile` (Windows only). |
| Pick a custom trigger for one dictation | Style pill | Different | Tray > "Trigger for the next dictation": the next dictation is sent with `trigger=<id>` and the choice is used up. The Recording card names it. Checked 2026-11-16 with the library's "Half the length". |
| Typed instruction for the AI | Box on the floating button's right-click | Different | `client/linux_prompt.py`: tray > "Type an instruction...", or `PROMPT_HOTKEY` in `.env` (no default). Type an instruction, or "/" to pick a custom trigger; the answer replaces the highlighted text (shown with a tick box, since X11's highlight can be old) or is pasted where you were. Checked 2026-11-16 in a GTK text box. Uses its own simple matching until `prompt_panel.py` imports off Windows. |
| Floating mic button at the text cursor | Yes | Not on Linux | Needs the caret position. AT-SPI gives it only in apps that expose it (GTK; Firefox/Chrome/Electron only with accessibility switched on), and on Wayland a window can't place itself at screen coordinates. |
| Style pill + style sheet while recording | Yes | Not on Linux (yet) | A Tk window; fine on X11. On Wayland it can't stay on top or be placed. |
| Return to where you started before pasting | Window, text box and caret, verified | Different | The window only: if another window has the focus when the text is ready, the original is brought back (`_NET_ACTIVE_WINDOW`) and pasted into; X puts the focus back in the box that window last had. Not verified box by box. If the window can't be brought back, nothing is pasted and a notice says the text is on the clipboard. Checked 2026-11-16, also with the backtick's characters removed in both windows (not verified before deleting, as Windows does). |
| Edit highlighted text by voice (desktop side) | The same hotkey, when text is highlighted | Different | Its own key: Shift+<hotkey> (`EDIT_HOTKEY` in `.env`; `off` disables; none by default for a hotkey that types a character or uses Shift). Why: X11's PRIMARY selection is the last text highlighted anywhere, and stays after the highlight is gone, so the plain hotkey can't tell "highlighted now" from "highlighted an hour ago". Checked 2026-11-16 in a GTK text box with a spoken instruction and Bonsai as the edit model ("hello wurld this is a tset" came back "Hello, world. This is a test." over the highlight). Tk apps paste next to the highlight instead of over it. |
| Per-app styles | Settings page with a program picker | Different | The rules work (`client/app_styles.json`, matched on the program's file name: `{"app": "firefox", "tone": "casual"}`; ".exe" is added internally). The Settings picker lists no programs on Linux: edit the file. Programs without `_NET_WM_PID` on their windows (Tk apps) aren't recognised. |
| "Hide in this app", program picker | Yes | Not on Linux | Part of the floating button and Settings. |
| Right-click "Transcribe" on files | Explorer menu | Different | GNOME Files: right-click > Scripts > "Transcribe with Whisper Unchained" (a script in `~/.local/share/nautilus/scripts/`), which opens the Transcribe a file window. Untested by hand. |
| Keep dictations out of clipboard history | Yes | Not on Linux | No standard: each clipboard manager has its own (or no) "don't record" hint. `CLIPBOARD_PRIVATE` is ignored. |
| Game mode | Yes | Not on Linux | See Server. |
| Installer, uninstaller | Setup wizard | Different | `scripts/install_linux_desktop.sh` (`--remote URL --pin SHA256` for a desktop that uses another PC's server, `--uninstall`). Desktop-only needs `requirements-linux-desktop.txt`, not the server's packages (checked in a fresh venv). |

### Wayland (untested: no Wayland machine yet)

Ubuntu's default session. Programs can't see keys, windows or the focus, so:

- No built-in hotkey. Bind a keyboard shortcut in the desktop's settings to
  `.venv/bin/python -m client.linux_listener --toggle` (and `--toggle-edit`).
  That is a toggle; hold-to-talk isn't possible.
- Pasting needs `wtype` (not on GNOME) or `ydotool` (needs `/dev/uinput`
  access); without one the text is left on the clipboard (`wl-clipboard`)
  with a notice.
- No per-app styles, no terminal detection (set `PASTE_KEYS`), no return to
  the starting window.
- The tray works. The card shows but can't be placed or kept on top.

The code paths exist (`session_type()`, `_deliver_wayland`) but have only
been read, not run. Choosing "Ubuntu on Xorg" at the login screen gives the
full listener.

### Which Linux apps let us read the text box (measured 2026-11-16, AI-Worker3)

Correction offers and a mic button at the text cursor both need the app in
front to hand over its text and caret through AT-SPI (the accessibility
interface). Measured by typing a marker into each app and looking for it:

| App | Text readable | Caret position on screen | Needs |
|---|---|---|---|
| gedit (GTK 3) | Yes | Yes | Nothing |
| GNOME Text Editor (GTK 4) | Yes | No (GTK 4 gives no screen coordinates) | Nothing |
| GNOME Terminal | Yes, read-only (the screen, not a text box) | Yes | Nothing |
| LibreOffice Writer | Yes | Yes | Nothing |
| Firefox (snap) | Yes | Sometimes (2 of 3 runs returned 0,0) | Nothing on a used profile; two runs on a brand-new profile were inconclusive (its first-run page took the click) |
| Chrome | No by default: only its toolbar | - | Started with `--force-renderer-accessibility`, or the desktop's screen-reader flag on: then yes, with the caret |

The desktop's general accessibility switch (`org.a11y.Status IsEnabled`)
made no difference to any of them. No Electron app (Slack, VS Code, Discord)
is installed here; they are built on Chrome and are expected to behave like
it, which is not measured. Nothing is built on this yet.

### Testing the listener without a microphone

AI-Worker3 has no microphone, so the checks above used a virtual one: a
PipeWire null sink (`pactl load-module module-null-sink sink_name=wu_sink`),
looped into a virtual source (`pw-loopback` with
`--playback-props='media.class=Audio/Source node.name=wu_mic'`), the listener
started with `MIC_DEVICE=wu_mic`, a speech file played with
`pw-play --target wu_sink`, and the hotkey pressed with `xdotool key F9` into
a test text box. It has not been used by a person speaking into a real
microphone yet.
