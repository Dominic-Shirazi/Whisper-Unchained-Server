# Windows, Linux and Android: what works where

The living list of features that behave differently, or don't exist, on Linux or on the Android app (last section).
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
| Second speech engine for AMD and Intel graphics cards (whisper.cpp, `WHISPER_ENGINE=whisper.cpp`) | Yes | Same | Linux has its own build (`whisper-server-linux-x64-vulkan-v1.9.5.tar.gz`, built on Ubuntu 24.04: needs glibc 2.39 or newer, the Vulkan loader `libvulkan1` and a 64-bit Intel/AMD processor). `api/engine_files.py` downloads, checks and unpacks it (a tar.gz; links and paths outside the folder are refused); the engine dies with the server. `install_linux.sh` sets it up, with one download, when there is no usable NVIDIA card but a separate AMD or Intel card (`--engine usual` declines); Settings > Transcription offers it under Advanced. Graphics are named from `lspci` (`installer/linux_hardware.py`), which lists a chip family, so an RX 6600 XT reads "AMD Radeon RX 6600 series card". Run on AI-Worker3 2026-11-18 (i5-9600K, Intel UHD 630, two RTX 2070 Supers), base model, 11 s clip: processor 0.85 s, Intel UHD 630 through Vulkan 1.6 s, RTX 2070 Super through Vulkan 0.20 s; also through the server (`/v1/audio/transcriptions`). Not run on an AMD card: there is none here. The installer step was tried piece by piece, not as a whole install on an AMD PC. |
| Third speech engine on the processor (Parakeet, `WHISPER_ENGINE=parakeet`) | Yes | Same | Same code (`api/parakeet_engine.py`, `api/parakeet_files.py`) and the same Settings choice, "The processor way". The runtime, `sherpa-onnx` 2.0.1, comes with the Linux install through `requirements.txt` and `constraints.txt` (about 15 MB; it brings its own libraries, nothing to `apt install`); wheels exist for Python 3.10 to 3.14 on x86-64 and ARM64. Run 2026-11-19 on AI-Worker3 (i5-9600K, 6 threads): the Settings window downloaded the 110M model (four files, sizes and SHA-256 as pinned) in 33 s, refused Save before the download and allowed it after; a server started with `WHISPER_ENGINE=parakeet` transcribed a 6.6 s clip in 0.15 to 0.21 s and returned no text for a silent file. Setup picks it by the Windows rule (`hardware.offer_processor`: dictation on the processor, English, and no AMD or Intel card offer): the setup window through the shared wizard, `scripts/install_linux.sh` on its own (`--engine usual` declines it; a re-run keeps an earlier choice and its model), and `linux_setup_plan.py` says it without "in a lighter version" (packages plus 136 MB, about 300 MB). Run 2026-11-19 in a clean Ubuntu 24.04 container with systemd, where the PC's NVIDIA card has no driver: `install_linux.sh --user` chose it, downloaded the model, started the service, which transcribed a clip in 0.2 s; a re-run said "Everything it needs is already on this PC."; `--engine usual` kept small.en; the setup window's own install step chose it, downloaded it as one row, and its server transcribed a clip. Not run on Linux: the 0.6B models. |
| Game mode (free the GPU while gaming) | Yes | Different | Off unless `GAME_MODE=true` (Settings > Startup), as on Windows, with the same delays (15 s to pause, 10 s to come back) and the same server pause. X11 only: the tray (`client/linux_tray.py`) asks whether the window in front is full screen (`_NET_WM_STATE_FULLSCREEN`) and is not a browser, player, terminal or other everyday app (`NOT_GAMES` in `client/linux_game_mode.py`), or whether a program named in `GAME_PROCESSES` is running (typed names; Windows picks from open windows). Checked on AI-Worker3 2026-11-18 with a full-screen window on a separate X display under openbox: paused after 15 s (1.1 GB of the card freed), resumed 10 s after it closed. Not tried with a real game. Not on Wayland: a program is not told which window is in front or whether it is full screen, so the switch is not shown there. |
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
| Tray icon | Yes | Different | `pystray` through the system's `python3-gi` (borrowed by the venv: same Python version needed). Uses AppIndicator when its library is installed, else GTK's status icon, which GNOME shows through Ubuntu's AppIndicator extension. Menu: start/stop, edit highlighted text, Settings, Transcribe a file, History, pause or resume dictation (frees the graphics card), beeps, open the log, restart the listener, quit. No "restart the server" entry. Without `python3-gi`: no icon, the listener still runs. |
| Keeps the listener running | Watchdog, restarts it every 10 idle minutes | Different | The tray process restarts the listener when it exits, and systemd restarts the tray. No timed restart: X doesn't drop the key hook the way Windows does. |
| A dictation the server couldn't take is kept and retried | Yes | Same | Same `client/kept_recordings.py`: the recording is kept in `client/kept_recordings/`, retried every 15 seconds, and the text put on the clipboard with a notice. The retry logic is covered by tests; not yet tried against a server that is really down. |
| Settings window | Yes | Different | The same `settings_gui.py`; its Windows parts ask `client/linux_settings.py` on Linux. Opened and saved on AI-Worker3 2026-11-16 (tray > Settings). Windows-only settings are left out (floating button, clipboard history, scheduled restart, LAUNCHER.bat); the Hotkey page adds the edit key, beeps and the Recording card, with the microphone name and the typed-instruction key under its "Advanced settings". The Skills page, the Transcription page ("We found an NVIDIA GeForce RTX 2070 SUPER. Dictation will run on it.", one Advanced button, the second engine with its one download) and the Advanced folds were opened on AI-Worker3 2026-11-18 on a separate X display; the Download button itself was not pressed there. "Desktop integration" switches the Files right-click script and start at login (systemd). "Save and restart" restarts the `whisper-unchained` systemd unit (a system-wide one asks for the password through pkexec: untested). The program pickers for per-app styles list nothing on Linux. |
| "Transcribe a file" window | Yes | Same | `file_window.py` runs unchanged (opened 2026-11-16; tray menu). |
| History window | Yes | Different | `history_window.py` runs unchanged, for a server on the same machine. "Play recording" uses `os.startfile` (Windows only). |
| Pick a skill for one dictation | Style pill | Different | Tray > "Skill for the next dictation": the next dictation is sent with `trigger=<id>` and the choice is used up. The Recording card names it. Checked 2026-11-16 with the library's "Half the length". |
| Typed instruction for the AI | Box on the floating button's right-click | Different | `client/linux_prompt.py`: tray > "Type an instruction...", or `PROMPT_HOTKEY` in `.env` (no default). Type an instruction, or "/" to pick a skill; the answer replaces the highlighted text (shown with a tick box, since X11's highlight can be old) or is pasted where you were. Checked 2026-11-16 in a GTK text box. Shares `prompt_panel.py`'s matching. |
| Floating mic button at the text cursor | Yes | Not on Linux | Needs the caret position. AT-SPI gives it only in apps that expose it (GTK; Firefox/Chrome/Electron only with accessibility switched on), and on Wayland a window can't place itself at screen coordinates. |
| Style pill + style sheet while recording | Yes | Not on Linux (yet) | A Tk window; fine on X11. On Wayland it can't stay on top or be placed. |
| Return to where you started before pasting | Window, text box and caret, verified | Different | The window only: if another window has the focus when the text is ready, the original is brought back (`_NET_ACTIVE_WINDOW`) and pasted into; X puts the focus back in the box that window last had. Not verified box by box. If the window can't be brought back, nothing is pasted and a notice says the text is on the clipboard. Checked 2026-11-16, also with the backtick's characters removed in both windows (not verified before deleting, as Windows does). |
| Edit highlighted text by voice (desktop side) | The same hotkey, when text is highlighted | Different | Its own key: Shift+<hotkey> (`EDIT_HOTKEY` in `.env`; `off` disables; none by default for a hotkey that types a character or uses Shift). Why: X11's PRIMARY selection is the last text highlighted anywhere, and stays after the highlight is gone, so the plain hotkey can't tell "highlighted now" from "highlighted an hour ago". Checked 2026-11-16 in a GTK text box with a spoken instruction and Bonsai as the edit model ("hello wurld this is a tset" came back "Hello, world. This is a test." over the highlight). Tk apps paste next to the highlight instead of over it. |
| Highlight chip while recording (word count; "Rewrite them" / "Type over them") | Yes | Not on Linux | Part of the floating mic button (`client/highlight_chip.py`, drawn by `style_pill.py`): a pencil and the count while the highlighted words will be rewritten, the count struck through when they will be typed over; click it to choose for that one recording. With a cloud service (no AI to rewrite with) the count is struck through and the dictation replaces the highlight. On Linux the key makes the choice: Shift+<hotkey> rewrites, the plain hotkey dictates. |
| "The AI made no changes." (a rewrite that comes back the same is not pasted) | Yes, spoken and typed | Same | The same test (`highlight_chip.no_changes`) and the same words: the Linux listener shows the notice on its card and pastes nothing; the typed box (`linux_prompt.py`) shows it and keeps your instruction to word another way. Checked 2026-11-18 against the real server and AI: correcting already-correct text came back identical and was caught; the paste being skipped is covered by tests, not watched on screen. |
| Per-app styles | Settings page with a program picker | Different | The rules work (`client/app_styles.json`, matched on the program's file name: `{"app": "firefox", "tone": "casual"}`; ".exe" is added internally). The Settings picker lists no programs on Linux: edit the file. Programs without `_NET_WM_PID` on their windows (Tk apps) aren't recognised. |
| "Hide in this app", program picker | Yes | Not on Linux | Part of the floating button and Settings. |
| Right-click "Transcribe" on files | Explorer menu | Different | GNOME Files: right-click > Scripts > "Transcribe with Whisper Unchained" (a script in `~/.local/share/nautilus/scripts/`), which opens the Transcribe a file window. Untested by hand. |
| Keep dictations out of clipboard history | Yes | Not on Linux | No standard: each clipboard manager has its own (or no) "don't record" hint. `CLIPBOARD_PRIVATE` is ignored. |
| Game mode | Yes | Different | X11 only. See Server. |
| Installer, uninstaller | Setup wizard | Different | `scripts/install_linux_desktop.sh` (`--remote URL --pin SHA256` for a desktop that uses another PC's server, `--uninstall`). Desktop-only needs `requirements-linux-desktop.txt`, not the server's packages (checked in a fresh venv). Both shell installers now say first what was found and the whole download in the Windows wizard's words (`installer/linux_setup_plan.py`: "We found an NVIDIA GeForce RTX 3060. Dictation will run on it." / "Setup will download about 3.2 GB."), ask nothing, number their steps, and when the server does not come up show the last lines of its log. The figure adds the Python packages (159 MB with the Parakeet runtime, measured 2026-11-19), NVIDIA's libraries (1.4 GB, NVIDIA only) and the model, measured 2026-11-18; what is already on the PC is not counted. Linux only: an NVIDIA card with no driver installed is named as such. Run 2026-11-18 in a clean Ubuntu 24.04 container (`--user --no-service`, processor, small.en): installed, started by hand, transcribed a clip, and a second run said "Everything it needs is already on this PC." The systemd and waiting part was not run in the container (no systemd there); its failure message was tried against a throwaway failing unit. The setup wizard: `scripts/setup_linux.sh` (step 1: makes the venv and installs about 200 MB of packages, asks nothing, and when `python3-venv` or `python3-tk` is missing prints the one `sudo apt install` line, since that needs a password) then `installer/linux_wizard.py`, which is the Windows wizard's window and pages (`installer/wizard.py`) with the Install step's work done by `installer/linux_steps.py`. Different from Windows: no permission prompt ("Nothing here needs your password."); the server and tray are `systemd --user` services (the same unit files the shell installers write); NVIDIA's libraries are part of the one download, so an NVIDIA PC reads "about 3.0 GB"; with two cards the one with the most free memory is used; an NVIDIA card with no driver is named as such; Tailscale opens its install page instead of installing; the right-click menu is GNOME Files' Scripts menu, skipped elsewhere; missing system pieces (`xclip`, `python3-gi`, `pipewire-bin`) are named in one `sudo apt install` line at the end; the wait for dictation counts the seconds and, if the server does not come up, shows the last lines of its journal. Dictation starts at log-in; starting at boot needs `sudo loginctl enable-linger`, which the Done page says when it is off. Run 2026-11-18 from nothing in an Ubuntu 24.04 container with systemd, a non-root user and a separate X display: recommended settings, processor, small.en; both services came up enabled, `/health` ok, a clip transcribed, and a second run found everything in place. Not run: the NVIDIA and AMD paths through the window (the NVIDIA library download and the driver check were run on their own on AI-Worker3), "another PC" and "cloud", "Walk me through the settings", a real desktop session with a tray and microphone, Wayland. A system-wide service still needs `sudo scripts/install_linux.sh`. |

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

## Android app

The phone app is a separate repository
([Whisper-Unchained-Android](https://github.com/Dominic-Shirazi/Whisper-Unchained-Android)).
This table compares it with the Windows desktop as of build 31 (2026-11-17).
Rows marked "By decision" were left out on purpose, not waiting to be built.

| Feature | Windows | Android | Notes |
|---|---|---|---|
| How you start a recording | Hotkey, or the mic button at the text cursor | Different | A floating mic button over any app (an accessibility service). No hotkey and no keyboard of its own. |
| Where the speech is turned into text | This PC, another PC's server, or a cloud service | Different | A paired PC, a cloud service with your own key, or the phone itself with no PC and no cloud (the "Full" download, which carries the on-phone model). The phone can also fall back to itself when the PC can't be reached. |
| Pairing with a server | Not needed on the same PC | Different | Scan one QR code from Settings on the PC. It carries the home address and the away address. |
| Skills (a catchphrase for a saved instruction) | Yes | Same | Spoken catchphrases are picked out by the server, so they work the same. The phone also lists them on the style sheet while recording and after "/" in a typed instruction. Skills are saved on the server, not the phone. |
| Typed instruction for the AI | Box on the mic button's right-click | Different | Long-press the button > "Type an instruction", or the "Type" pill that appears when text is highlighted. |
| Rewrite highlighted text by voice | Yes | Same | Contract 01 section 14 (`/dictate/v1/edit_selection`). Needs a paired PC with an AI set up. |
| Highlight chip (pencil and word count; "Rewrite them" / "Type over them") | Yes | Same | Built first on the phone (build 31); Windows matches it. |
| "The AI made no changes." | Yes | Same | Nothing is pasted when the rewrite equals the highlight. |
| Formatting in highlighted text (bold, lists) | Kept only where the app gives it as text | Different | The phone reads plain text from the app, so a rewrite comes back without bold or links. Apps that draw their text box in a web view (Gmail's editor) give plain text only. |
| Style for one recording | Style pill | Same | Style pill and sheet while recording. |
| Styles per app | Yes | Not on Android | One style for everything, set in AI Editing, plus the per-recording pill. |
| Return to where you started before pasting | Window, text box and caret, verified | Not on Android (by decision) | The phone never switches apps for you. The text goes where you are when it is ready; with no text box in focus it is left on the clipboard with "Copied - long-press the box to paste". A box you have left is not pasted into (F-68). |
| Offer to remember a correction you typed | Yes | Not on Android (by decision) | Needs to watch the text box after the paste. The phone edits the word list directly instead (Words screen). |
| Your words (vocabulary, corrections) | Settings > Words | Same | The phone edits the server's list. In on-phone mode it applies its own copy. |
| Keep dictations out of clipboard history | Yes | Not on Android | The clipboard entry is not marked sensitive, so Android's clipboard preview shows it. |
| Hide the button | Per app, or snooze | Different | Long-press > "Hide button" hides it everywhere until turned back on. No per-app hiding and no timed snooze. |
| Game mode, model and graphics card choices | Yes | Not applicable | These are the server's. |
| Transcribe a file, History window | Yes | Not on Android | |
| Android version needed | - | Android 14 or newer | |
