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
| Plain HTTP 5000 + HTTPS 5001, phone pairing | Yes | Same | Linux pairs from the terminal (`install_linux.sh`). |
| Voice-edit highlighted text (`/dictate/v1/edit_selection`) | Yes | Untested | Server side only; the desktop part is Windows-only (below). |
| Per-dictation style fields (`cleanup_*` on `/dictate/v1`) | Yes | Untested | Used by the phone pill and per-app styles. |
| GPU/CPU choice (`api/device_select.py`), CPU fallback | Yes | Untested | |
| API keys encrypted at rest (`api/secrets_store.py`) | DPAPI, machine scope | Not on Linux | Plain text in `.env` (mode 600). A Windows `dpapi:` value copied to Linux is treated as missing; for the server's own key that locks the server. |
| Game mode (free the GPU while gaming) | Yes | Not on Linux | Needs Windows full-screen detection. |
| Runs as a background service | Windows service (NSSM) | systemd unit | |
| Cleanup prompt (incl. spoken "slash") | Yes | Same | Shared `api/cleanup_prompt.json`. |

## Desktop (hotkey, tray, floating button, Settings)

| Feature | Windows | Linux | Notes |
|---|---|---|---|
| Tray icon + hotkey listener | Yes | Not on Linux (yet) | Linux is headless so far. |
| Settings window | Yes | Not on Linux (yet) | Uses winreg, the Windows service and startup entries. |
| Floating mic button at the text cursor | Yes | Not on Linux (yet) | Needs caret position (UI Automation / MSAA on Windows). |
| Style pill + style sheet while recording | Yes (in progress) | Not on Linux (yet) | |
| Return to the original text box before pasting | Yes | Not on Linux (yet) | Needs focus control, which Wayland doesn't allow. |
| Edit highlighted text by voice (desktop side) | Yes | Not on Linux (yet) | |
| Per-app styles, "Hide in this app", program picker | Yes | Not on Linux (yet) | |
| Right-click "Transcribe" on files | Explorer menu | Not on Linux (yet) | |
| Keep dictations out of clipboard history | Yes | Not on Linux (yet) | |
