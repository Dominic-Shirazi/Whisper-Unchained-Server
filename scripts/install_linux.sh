#!/usr/bin/env bash
# Whisper Unchained Server on Linux (headless): venv, CUDA check, .env, systemd.
#
#   sudo scripts/install_linux.sh            system service, runs as its own
#                                            user (whisper-unchained)
#   scripts/install_linux.sh --user          systemd --user service, runs as you
#                                            (no root; needs `loginctl enable-linger`
#                                            to start at boot)
#
# Options:
#   --port N        plain HTTP port (default: WHISPER_API_PORT from .env, else 5000);
#                   HTTPS for phones is WHISPER_HTTPS_PORT, else port + 1. Taken
#                   ports move to the next free one
#   --gpu N|cpu     GPU index to use, or cpu (default: the GPU with most free VRAM)
#   --engine usual  keep the usual speech engine. Otherwise setup picks the
#                   second one (whisper.cpp) when there is no NVIDIA card but a
#                   separate AMD or Intel graphics card, and the quick one for
#                   processors (Parakeet, English) when dictation would run on
#                   the processor, also with that card left out by --gpu cpu.
#                   --engine whisper.cpp asks for the second one even when an
#                   earlier run chose another
#   --service-user  system mode only: account name (default whisper-unchained)
#   --no-service    venv + .env only
#
# Safe to re-run: an existing .env keeps its API key and settings (only the
# GPU/port lines are refreshed), and an existing certificate is never replaced
# (that would un-pair every phone). Then pair a phone with:
#   .venv/bin/python -m client.pair
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="whisper-unchained"
SVC_USER="whisper-unchained"
MODE=""
PORT=""
GPU=""
ENGINE_CHOICE=""
INSTALL_SERVICE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --user) MODE=user ;;
        --port) PORT="$2"; shift ;;
        --gpu) GPU="$2"; shift ;;
        --service-user) SVC_USER="$2"; shift ;;
        --no-service) INSTALL_SERVICE=0 ;;
        --engine) ENGINE_CHOICE="$2"; shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$MODE" ]; then
    if [ "$(id -u)" -eq 0 ]; then MODE=system; else
        echo "Run with sudo for a system service, or pass --user for a per-user service." >&2
        exit 2
    fi
fi
if [ "$MODE" = user ] && [ "$(id -u)" -eq 0 ]; then
    echo "--user installs a service for the account that runs it: don't use sudo." >&2
    exit 2
fi

say() { printf '\n== %s\n' "$*"; }
cd "$REPO"

# In system mode the venv and .env belong to the person who ran sudo, so a
# later `git pull` / `pip install` doesn't need root.
OWNER="${SUDO_USER:-$(id -un)}"
as_owner() { if [ "$(id -un)" != "$OWNER" ]; then sudo -u "$OWNER" "$@"; else "$@"; fi; }

# --- Python -------------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$(command -v "$c")"; break
    fi
done
[ -n "$PY" ] || { echo "Needs Python 3.10+ with venv (apt install python3-venv)." >&2; exit 1; }
VPY="$REPO/.venv/bin/python"

# A re-run: stop our own service first, so its model's VRAM and its port count
# as free (it's restarted at the end).
if [ "$MODE" = system ]; then systemctl stop "$UNIT_NAME" 2>/dev/null || true
else systemctl --user stop "$UNIT_NAME" 2>/dev/null || true; fi

# --- This PC: what was found, and the one download -----------------------------
# Said before anything is installed, in the Windows wizard's words
# (installer/linux_setup_plan.py). Nothing is asked.
say "This PC"
DEVICE=cpu
COMPUTE=int8
PROBLEM=""
NVIDIA_NAME=""
FREE=""
if [ "$GPU" != cpu ] && command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    # free MiB per GPU, PCI order (matches CUDA_DEVICE_ORDER=PCI_BUS_ID below)
    if [ -z "$GPU" ]; then
        GPU="$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits \
               | sort -t, -k2 -nr | head -1 | cut -d, -f1 | tr -d ' ')"
    fi
    FREE="$(nvidia-smi -i "$GPU" --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')"
    NVIDIA_NAME="$(nvidia-smi -i "$GPU" --query-gpu=name --format=csv,noheader | sed 's/^ *//;s/ *$//')"
    # large-v3-turbo: ~2.1 GB at float16, ~1.2 GB at int8_float16 (measured on
    # an RTX 2070 Super, CUDA context included). Below that, the CPU.
    if [ "$FREE" -ge 2600 ]; then DEVICE=cuda; COMPUTE=float16
    elif [ "$FREE" -ge 1400 ]; then DEVICE=cuda; COMPUTE=int8_float16
    else PROBLEM=memory
    fi
fi

# AMD or Intel graphics card: the second engine. faster-whisper only speeds up
# on NVIDIA. With no usable NVIDIA card but a separate AMD or Intel card, the
# whisper.cpp engine runs on it through Vulkan (same rule as the Windows setup:
# installer/hardware.py offer_engine). Built-in graphics measured slower than
# the processor, so they don't get it. Only when nothing was ever chosen (a
# WHISPER_ENGINE line in .env is kept) or --engine whisper.cpp asks for it.
ENGINE=""
CPP_MODEL=""
NEEDS_VULKAN=0
CHOSEN=0
grep -qs '^[[:space:]]*WHISPER_ENGINE[[:space:]]*=' .env && CHOSEN=1
if [ "$DEVICE" = cpu ] && [ -z "$NVIDIA_NAME" ] \
        && { [ "$CHOSEN" = 0 ] || [ "$ENGINE_CHOICE" = whisper.cpp ]; }; then
    CPP_MODEL="$("$PY" - <<'EOF'
import sys
sys.path.insert(0, "installer"); sys.path.insert(0, "api")
import hardware, engine_files
if engine_files.available():
    names = hardware.real_graphics(hardware.display_adapters())
    offer = hardware.offer_engine([], hardware.other_graphics(names))
    if offer:
        print(offer.model)
EOF
)" || CPP_MODEL=""
    if [ -n "$CPP_MODEL" ] && [ "$GPU" != cpu ] && [ "$ENGINE_CHOICE" != usual ]; then
        if ldconfig -p 2>/dev/null | grep -q 'libvulkan\.so\.1'; then ENGINE=whisper.cpp
        else NEEDS_VULKAN=1; fi
    fi
fi

# Dictation on the processor: the quick engine for processors (Parakeet 110M,
# English), a fraction of a second per dictation where Whisper small takes a
# few seconds. Same rule as the Windows setup (installer/hardware.py
# offer_processor): also on a PC with the AMD or Intel card offer above when
# that card is not used (--gpu cpu, or no Vulkan yet); --engine usual declines
# both. Its package comes with requirements.txt.
QUICK_OK=0
if [ "$ENGINE" != whisper.cpp ] && [ "$ENGINE_CHOICE" != usual ] && [ "$CHOSEN" = 0 ]; then
    QUICK_OK=1
fi
if [ "$DEVICE" = cpu ] && [ "$QUICK_OK" = 1 ]; then ENGINE=parakeet; fi
# A re-run where the quick engine was chosen before: keep it, and its model.
if [ "$CHOSEN" = 1 ] && [ "$ENGINE" != whisper.cpp ] && sed -n 's/^[[:space:]]*WHISPER_ENGINE[[:space:]]*=[[:space:]]*//p' .env \
        | tail -1 | grep -qix 'parakeet[[:space:]]*'; then
    ENGINE=parakeet
fi

"$PY" installer/linux_setup_plan.py server --root "$REPO" --device "$DEVICE" \
    --nvidia "$NVIDIA_NAME" --problem "$PROBLEM" --engine "$ENGINE" || true
if [ "$PROBLEM" = memory ]; then
    echo "(Card $GPU has ${FREE} MiB of its memory free. Close what is using it and run this again to use the card.)"
fi
if [ "$NEEDS_VULKAN" = 1 ]; then
    echo "This PC's graphics card could do the work, but it needs the Vulkan graphics library first:"
    echo "  sudo apt install libvulkan1 mesa-vulkan-drivers"
    echo "Install it and run this again with --engine whisper.cpp. Until then dictation runs on the processor."
fi
if [ "$ENGINE_CHOICE" = whisper.cpp ] && [ -z "$CPP_MODEL" ]; then
    echo "--engine whisper.cpp: there is no AMD or Intel graphics card here that it can use, so nothing changes."
fi
[ "$DEVICE" = cuda ] || [ -n "$NVIDIA_NAME" ] || GPU=""

# --- Python packages -----------------------------------------------------------
say "Step 1 of 3: the programs dictation is made of (a few minutes on a slow connection)"
[ -x .venv/bin/python ] || as_owner "$PY" -m venv .venv
as_owner "$VPY" -m pip install -q --upgrade pip
as_owner "$VPY" -m pip install -q -r requirements.txt -c constraints.txt
echo "venv: $REPO/.venv ($("$VPY" --version))"

# --- NVIDIA: CUDA 12 / cuDNN 9 --------------------------------------------------
# CTranslate2 (faster-whisper) needs the CUDA 12 cuBLAS and cuDNN 9 runtimes.
# They come as pip wheels into the venv, so no system CUDA toolkit is needed;
# only the NVIDIA driver. Same ranges as installer/bootstrap.ps1 on Windows.
if [ "$DEVICE" = cuda ]; then
    echo "NVIDIA's libraries (the largest part of the download)..."
    nvidia-smi --query-gpu=driver_version,name --format=csv,noheader | head -1 | sed 's/^/driver: /'
    as_owner "$VPY" -m pip install -q "nvidia-cublas-cu12>=12.4,<13" "nvidia-cudnn-cu12>=9,<10"
    echo "GPU $GPU: ${FREE} MiB free -> $DEVICE / $COMPUTE"
fi
# The wheels' lib folders, for LD_LIBRARY_PATH in the unit.
NVLIBS="$("$VPY" - <<'EOF'
import glob, os
try:
    import nvidia
except ImportError:
    raise SystemExit
print(":".join(sorted(d for base in nvidia.__path__ for d in glob.glob(os.path.join(base, "*", "lib")))))
EOF
)"
if [ "$DEVICE" = cuda ]; then
    if ! CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES="$GPU" LD_LIBRARY_PATH="$NVLIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        "$VPY" -c 'import ctranslate2, sys; sys.exit(ctranslate2.get_cuda_device_count() < 1)'; then
        DEVICE=cpu; COMPUTE=int8; GPU=""
        [ "$QUICK_OK" = 1 ] && ENGINE=parakeet
        "$PY" installer/linux_setup_plan.py server --root "$REPO" --device cpu \
            --nvidia "$NVIDIA_NAME" --problem driver --engine "$ENGINE" --found-only >&2 || true
        echo "Update the NVIDIA driver and run this again to use the card." >&2
    fi
fi
[ "$DEVICE" = cuda ] || GPU=""
if [ "$ENGINE" = parakeet ] && ! "$VPY" -c 'import importlib.util as u, sys; sys.exit(u.find_spec("sherpa_onnx") is None)'; then
    ENGINE=""
    echo "The quick engine's package did not install. Dictation will run on the processor the usual way."
fi

# --- The second or quick engine's files --------------------------------------------
if [ "$ENGINE" = whisper.cpp ]; then
    # No .env yet on a new install: sed fails, and pipefail would stop setup.
    MODELS_DIR="$(sed -n 's/^WHISPER_MODELS_DIR=//p' .env 2>/dev/null | tail -1 || true)"
    if ! as_owner env "CPP_MODEL=$CPP_MODEL" "MODELS_DIR=$MODELS_DIR" "$VPY" - <<'EOF'
"""The engine, the silence filter and the model, with one percentage."""
import os, subprocess, sys
sys.path.insert(0, "api")
import engine_files as ef
model, folder = os.environ["CPP_MODEL"], os.environ.get("MODELS_DIR") or None
todo = ef.missing(model, ".", folder)
if todo:
    print("Downloading the speech engine and its model...", flush=True)
    overall, shown = ef.Overall(todo), [-5]
    def progress(spec, done, _total):
        pct = overall.update(spec, done)
        if pct >= shown[0] + 5:
            shown[0] = pct
            print("\r  %d%%" % pct, end="", flush=True)
    try:
        ef.fetch_missing(model, ".", folder, progress=progress)
    except ef.EngineFileError as e:
        print("\n%s" % e)
        sys.exit(1)
    print("\r  100%")
# Built on Ubuntu 24.04: an older system can't start it. Find out now.
try:
    ok = subprocess.run([os.path.abspath(ef.engine_binary(".")), "--help"],
                        capture_output=True, timeout=30).returncode == 0
except (OSError, subprocess.SubprocessError):
    ok = False
if not ok:
    print("The second speech engine can't start on this system (it was built for "
          "Ubuntu 24.04 and needs a Linux at least that new).")
    sys.exit(1)
EOF
    then
        ENGINE=""
        echo "Dictation will run on the processor instead."
    fi
elif [ "$ENGINE" = parakeet ]; then
    # No .env yet on a new install: sed fails, and pipefail would stop setup.
    MODELS_DIR="$(sed -n 's/^WHISPER_MODELS_DIR=//p' .env 2>/dev/null | tail -1 || true)"
    PK_MODEL="$(sed -n 's/^PARAKEET_MODEL=//p' .env 2>/dev/null | tail -1 || true)"
    if ! as_owner env "MODELS_DIR=$MODELS_DIR" "PK_MODEL=$PK_MODEL" "$VPY" - <<'EOF'
"""The quick engine's model (four files), with one percentage."""
import os, sys
sys.path.insert(0, "api")
import engine_files as ef, parakeet_files as pf
todo = pf.missing(os.environ.get("PK_MODEL") or "110m", ".", os.environ.get("MODELS_DIR") or None)
if todo:
    print("Downloading the speech model...", flush=True)
    overall, shown = ef.Overall(todo), [-5]
    def progress(spec, done, _total):
        pct = overall.update(spec, done)
        if pct >= shown[0] + 5:
            shown[0] = pct
            print("\r  %d%%" % pct, end="", flush=True)
    try:
        for spec, dest in todo:
            ef.fetch(spec, dest, progress=progress)
    except ef.EngineFileError as e:
        print("\n%s" % e)
        sys.exit(1)
    print("\r  100%")
EOF
    then
        ENGINE=""
        echo "Dictation will run on the processor the usual way instead."
    fi
fi

# --- Port -----------------------------------------------------------------------
port_busy() { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }
if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(sed -n 's/^WHISPER_API_PORT=\([0-9]*\).*/\1/p' .env | tail -1)"
fi
PORT="${PORT:-5000}"
while port_busy "$PORT"; do echo "Port $PORT is in use, trying $((PORT + 1))"; PORT=$((PORT + 1)); done
# Phones pair to the HTTPS port, so it must be free too (and not the plain one).
HTTPS_PORT=""
[ -f .env ] && HTTPS_PORT="$(sed -n 's/^WHISPER_HTTPS_PORT=\([0-9]*\).*/\1/p' .env | tail -1)"
HTTPS_PORT="${HTTPS_PORT:-$((PORT + 1))}"
while [ "$HTTPS_PORT" = "$PORT" ] || port_busy "$HTTPS_PORT"; do
    echo "Port $HTTPS_PORT is in use, trying $((HTTPS_PORT + 1))"; HTTPS_PORT=$((HTTPS_PORT + 1))
done
echo "port: $PORT (plain), $HTTPS_PORT (https)"

# --- .env -------------------------------------------------------------------------
say "Step 2 of 3: settings"
FRESH_ENV=0
[ -f .env ] || { as_owner cp .env.example .env; echo "created .env from .env.example"; FRESH_ENV=1; }
chmod 600 .env
ENGINE="$ENGINE" CPP_MODEL="$CPP_MODEL" HTTPS_PORT="$HTTPS_PORT" FRESH_ENV="$FRESH_ENV" DEVICE="$DEVICE" COMPUTE="$COMPUTE" GPU="$GPU" PORT="$PORT" "$VPY" - <<'EOF'
"""Set values in .env line by line, keeping every comment (as the Settings
window does). The API key is created only if there isn't one, never printed."""
import os, re, secrets
path = ".env"
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

def live(key):
    for i, l in enumerate(lines):
        if re.match(r"^\s*%s\s*=" % key, l):
            return i
    return None

def put(key, value, note=None):
    i = live(key)
    if i is not None:
        lines[i] = "%s=%s" % (key, value)
    else:
        if note:
            lines.extend(["", "# " + note])
        lines.append("%s=%s" % (key, value))

i = live("WHISPER_API_KEY")
current = lines[i].split("=", 1)[1].strip() if i is not None else ""
if current.startswith("dpapi:"):
    # Encrypted by Windows (api/secrets_store.py): unreadable here, and the
    # server refuses every request with it. Replace it.
    put("WHISPER_API_KEY", "wr_sec_" + secrets.token_urlsafe(24))
    print("the API key was encrypted on Windows and can't be read here: made a new one (re-pair phones)")
elif not current:
    put("WHISPER_API_KEY", "wr_sec_" + secrets.token_urlsafe(24))
    print("created a new API key (pair a phone to use it)")
else:
    print("kept the existing API key")
put("SERVER_HTTPS", "true")
# Phones use HTTPS on WHISPER_HTTPS_PORT. The plain port (WHISPER_API_PORT) is for
# programs on this network; a missing line means on, so write it out once.
put("WHISPER_HTTPS_PORT", os.environ["HTTPS_PORT"])
if live("SERVER_PLAIN_HTTP") is None:
    put("SERVER_PLAIN_HTTP", "false")
put("WHISPER_API_PORT", os.environ["PORT"])
put("WHISPER_DEVICE", os.environ["DEVICE"])
# A new install without a usable GPU: large-v3-turbo takes ~10 s per sentence on
# a processor, so start with small.en (a re-run keeps whatever model you chose).
if os.environ.get("ENGINE") == "whisper.cpp":
    put("WHISPER_ENGINE", os.environ["ENGINE"],
        "Linux installer: the second speech engine, for an AMD or Intel graphics card")
    put("WHISPER_CPP_MODEL", os.environ["CPP_MODEL"])
    put("WHISPER_CPP_DEVICE", "auto")
    print("dictation runs on the graphics card (second speech engine)")
elif os.environ.get("ENGINE") == "parakeet":
    put("WHISPER_ENGINE", "parakeet",
        "Linux installer: the quick speech engine for processors (English)")
    if live("PARAKEET_MODEL") is None:   # a re-run keeps the model you chose
        put("PARAKEET_MODEL", "110m")
    if os.environ.get("FRESH_ENV") == "1":
        put("WHISPER_MODEL_SIZE", "small.en")   # for the usual way, if chosen later
    print("dictation runs on the processor, the quick way (Parakeet)")
elif os.environ["DEVICE"] == "cpu" and os.environ.get("FRESH_ENV") == "1":
    put("WHISPER_MODEL_SIZE", "small.en")
    print("no usable GPU: model small.en (change WHISPER_MODEL_SIZE in .env)")
put("WHISPER_COMPUTE_TYPE", os.environ["COMPUTE"],
    "Linux installer: float16 needs ~2.1 GB VRAM, int8_float16 ~1.2 GB, cpu uses int8")
gpu = os.environ["GPU"]
if gpu:
    put("CUDA_DEVICE_ORDER", "PCI_BUS_ID",
        "Linux installer: which GPU (nvidia-smi numbering). Re-run the installer to re-pick.")
    put("CUDA_VISIBLE_DEVICES", gpu)
elif live("CUDA_VISIBLE_DEVICES") is not None:
    put("CUDA_VISIBLE_DEVICES", "")  # CPU: hide the GPUs a previous run picked
# Don't pin a 7B edit model in this machine's Ollama at every start: a server
# box usually has other jobs on its GPUs. Turn it back on deliberately.
if live("OLLAMA_EDIT_PRELOAD") is not None and "OLLAMA_EDIT_PRELOAD=false" not in lines:
    lines[live("OLLAMA_EDIT_PRELOAD")] = "OLLAMA_EDIT_PRELOAD=false"
    print("OLLAMA_EDIT_PRELOAD=false (set true to preload the edit model)")
with open(path + ".tmp", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.replace(path + ".tmp", path)
EOF
chown "$OWNER" .env 2>/dev/null || true
chmod 600 .env
echo "phones: https port $HTTPS_PORT"

[ "$INSTALL_SERVICE" = 1 ] || { say "Done (no service). Start by hand: .venv/bin/python -m api.serve --port $PORT"; exit 0; }

# --- systemd ------------------------------------------------------------------------
say "Step 3 of 3: starting dictation in the background (systemd, $MODE)"
UNIT_BODY="[Unit]
Description=Whisper Unchained Server (speech to text API)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$REPO
ExecStart=$VPY -m api.serve --host 0.0.0.0 --port $PORT
Environment=PYTHONUNBUFFERED=1
Environment=LD_LIBRARY_PATH=$NVLIBS
Restart=on-failure
RestartSec=5
# Loading the model (and downloading it on first start) takes a while.
TimeoutStartSec=600
NoNewPrivileges=yes
PrivateTmp=yes"

if [ "$MODE" = system ]; then
    if ! id "$SVC_USER" >/dev/null 2>&1; then
        useradd --system --user-group --no-create-home --home-dir /nonexistent \
                --shell /usr/sbin/nologin "$SVC_USER"
        echo "created system user $SVC_USER"
    fi
    # The service user must reach the repo: a 750 home folder blocks it.
    if ! sudo -u "$SVC_USER" test -r "$REPO/api/serve.py"; then
        echo "User $SVC_USER can't read $REPO (a private home folder?)." >&2
        echo "Clone the repo somewhere it can, e.g. /opt/whisper-unchained-server, or use --user." >&2
        exit 1
    fi
    # It writes under api/ (certificate, logs, settings) and models/ (Whisper
    # downloads), and reads .env. Group access keeps $OWNER able to edit too.
    mkdir -p models api/certs
    chown "$OWNER" models
    chgrp -R "$SVC_USER" api models .env
    chmod -R g+rwX api models
    find api models -type d -exec chmod g+s {} +
    chmod 640 .env
    # The private key stays owner-only (api/tls.py chmods it 600 when it makes
    # it); a key made by an earlier hand run is handed to the service user.
    [ -f api/certs/server.key ] && chown "$SVC_USER" api/certs/server.key && chmod 600 api/certs/server.key
    usermod -aG "$SVC_USER" "$OWNER" 2>/dev/null || true
    printf '%s\nUser=%s\nGroup=%s\nUMask=0007\nProtectSystem=full\n\n[Install]\nWantedBy=multi-user.target\n' \
        "$UNIT_BODY" "$SVC_USER" "$SVC_USER" > "/etc/systemd/system/$UNIT_NAME.service"
    systemctl daemon-reload
    systemctl enable "$UNIT_NAME" >/dev/null
    systemctl restart "$UNIT_NAME"
    STATUS="systemctl status $UNIT_NAME"; LOGS="journalctl -u $UNIT_NAME -f"
else
    mkdir -p "$HOME/.config/systemd/user"
    printf '%s\n\n[Install]\nWantedBy=default.target\n' "$UNIT_BODY" \
        > "$HOME/.config/systemd/user/$UNIT_NAME.service"
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT_NAME" >/dev/null
    systemctl --user restart "$UNIT_NAME"
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != yes ]; then
        echo "Note: it only starts at boot after: sudo loginctl enable-linger $(id -un)"
    fi
    STATUS="systemctl --user status $UNIT_NAME"; LOGS="journalctl --user -u $UNIT_NAME -f"
fi

# --- Health -----------------------------------------------------------------------------
# Say what is happening and how long it may take; when it doesn't come up,
# show what the server itself last said instead of a bare "no answer".
if [ "$MODE" = system ]; then SCTL="systemctl"; JCTL="journalctl -u $UNIT_NAME"
else SCTL="systemctl --user"; JCTL="journalctl --user -u $UNIT_NAME"; fi
last_words() {
    echo "The last things the server said:" >&2
    $JCTL -n 12 --no-pager -o cat 2>/dev/null | sed 's/^/  /' >&2 \
        || echo "  (its log could not be read; try: $LOGS)" >&2
}
models_mb() { du -sm "$REPO/models" 2>/dev/null | cut -f1; }
say "Waiting for dictation to start"
if [ -n "$ENGINE" ]; then     # setup already fetched that engine's model
    echo "This takes under a minute."
else
    echo "The first start downloads the speech model, which can take several minutes. Later starts take under a minute."
fi
WAITED=0
while [ "$WAITED" -lt 900 ]; do
    HEALTH="$(curl -ks "https://127.0.0.1:$HTTPS_PORT/health" 2>/dev/null || true)"
    if printf '%s' "$HEALTH" | grep -q '"status":"ok"'; then
        # The certificate is public (its fingerprint is in every pairing code);
        # make it readable so client.pair works without sudo. The key stays 600.
        [ "$MODE" = system ] && chmod 644 api/certs/server.pem
        say "Dictation is running."
        echo "Pair a phone:  cd $REPO && .venv/bin/python -m client.pair"
        echo "Status: $STATUS    Logs: $LOGS"
        exit 0
    fi
    if printf '%s' "$HEALTH" | grep -q '"status":"error"'; then
        echo "The server started, but the speech model could not be loaded." >&2
        printf '%s' "$HEALTH" | sed -n 's/.*"error":"\([^"]*\)".*/  \1/p' >&2
        last_words
        exit 1
    fi
    # The unit restarts itself on failure, so "failed" alone would miss a
    # server that dies at every start.
    RESTARTS="$($SCTL show -p NRestarts --value "$UNIT_NAME" 2>/dev/null || echo 0)"
    if $SCTL is-failed --quiet "$UNIT_NAME" 2>/dev/null || [ "${RESTARTS:-0}" -ge 3 ] 2>/dev/null; then
        echo "The server keeps stopping while it starts." >&2
        last_words
        exit 1
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    if [ $((WAITED % 30)) -eq 0 ]; then
        echo "  still starting: ${WAITED} seconds so far, $(models_mb) MB in the models folder"
    fi
done
echo "Dictation did not start within 15 minutes." >&2
last_words
exit 1
