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
#   --port N        API port (default: WHISPER_API_PORT from .env, else 5000;
#                   the next free port if that one is taken)
#   --gpu N|cpu     GPU index to use, or cpu (default: the GPU with most free VRAM)
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
INSTALL_SERVICE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --user) MODE=user ;;
        --port) PORT="$2"; shift ;;
        --gpu) GPU="$2"; shift ;;
        --service-user) SVC_USER="$2"; shift ;;
        --no-service) INSTALL_SERVICE=0 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
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

# --- Python + venv -----------------------------------------------------------
say "Python environment"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$(command -v "$c")"; break
    fi
done
[ -n "$PY" ] || { echo "Needs Python 3.10+ with venv (apt install python3-venv)." >&2; exit 1; }
[ -x .venv/bin/python ] || as_owner "$PY" -m venv .venv
VPY="$REPO/.venv/bin/python"
as_owner "$VPY" -m pip install -q --upgrade pip
as_owner "$VPY" -m pip install -q -r requirements.txt -c constraints.txt
echo "venv: $REPO/.venv ($("$VPY" --version))"

# --- NVIDIA driver + CUDA 12 / cuDNN 9 ----------------------------------------
# CTranslate2 (faster-whisper) needs the CUDA 12 cuBLAS and cuDNN 9 runtimes.
# They come as pip wheels into the venv, so no system CUDA toolkit is needed;
# only the NVIDIA driver. Same ranges as installer/bootstrap.ps1 on Windows.
# A re-run: stop our own service first, so its model's VRAM and its port count
# as free (it's restarted at the end).
if [ "$MODE" = system ]; then systemctl stop "$UNIT_NAME" 2>/dev/null || true
else systemctl --user stop "$UNIT_NAME" 2>/dev/null || true; fi

say "GPU"
DEVICE=cpu
COMPUTE=int8
if [ "$GPU" != cpu ] && command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=driver_version,name --format=csv,noheader | head -1 | sed 's/^/driver: /'
    as_owner "$VPY" -m pip install -q "nvidia-cublas-cu12>=12.4,<13" "nvidia-cudnn-cu12>=9,<10"
    # free MiB per GPU, PCI order (matches CUDA_DEVICE_ORDER=PCI_BUS_ID below)
    if [ -z "$GPU" ]; then
        GPU="$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits \
               | sort -t, -k2 -nr | head -1 | cut -d, -f1 | tr -d ' ')"
    fi
    FREE="$(nvidia-smi -i "$GPU" --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')"
    # large-v3-turbo: ~2.1 GB at float16, ~1.2 GB at int8_float16 (measured on
    # an RTX 2070 Super, CUDA context included). Below that, the CPU.
    if [ "$FREE" -ge 2600 ]; then DEVICE=cuda; COMPUTE=float16
    elif [ "$FREE" -ge 1400 ]; then DEVICE=cuda; COMPUTE=int8_float16
    else echo "GPU $GPU has only ${FREE} MiB free: using the CPU (slow). Free some VRAM and re-run."
    fi
    echo "GPU $GPU: ${FREE} MiB free -> $DEVICE / $COMPUTE"
else
    echo "No NVIDIA driver (nvidia-smi) found, or --gpu cpu: using the CPU (slow)."
    GPU=""
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
        echo "CTranslate2 can't see the GPU (driver too old for CUDA 12?). Using the CPU." >&2
        DEVICE=cpu; COMPUTE=int8; GPU=""
    fi
fi

# --- Port -----------------------------------------------------------------------
port_busy() { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }
if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(sed -n 's/^WHISPER_API_PORT=\([0-9]*\).*/\1/p' .env | tail -1)"
fi
PORT="${PORT:-5000}"
while port_busy "$PORT"; do echo "Port $PORT is in use, trying $((PORT + 1))"; PORT=$((PORT + 1)); done
echo "port: $PORT"

# --- .env -------------------------------------------------------------------------
say ".env"
[ -f .env ] || { as_owner cp .env.example .env; echo "created .env from .env.example"; }
chmod 600 .env
DEVICE="$DEVICE" COMPUTE="$COMPUTE" GPU="$GPU" PORT="$PORT" "$VPY" - <<'EOF'
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
if i is None or not lines[i].split("=", 1)[1].strip():
    put("WHISPER_API_KEY", "wr_sec_" + secrets.token_urlsafe(24))
    print("created a new API key (pair a phone to use it)")
else:
    print("kept the existing API key")
put("SERVER_HTTPS", "true")
# Phones use HTTPS on WHISPER_HTTPS_PORT. The plain port (WHISPER_API_PORT) is for
# programs on this network; a missing line means on, so write it out once.
if live("WHISPER_HTTPS_PORT") is None:
    put("WHISPER_HTTPS_PORT", str(int(os.environ["PORT"]) + 1))
if live("SERVER_PLAIN_HTTP") is None:
    put("SERVER_PLAIN_HTTP", "false")
put("WHISPER_API_PORT", os.environ["PORT"])
put("WHISPER_DEVICE", os.environ["DEVICE"])
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
HTTPS_PORT="$(sed -n 's/^WHISPER_HTTPS_PORT=\([0-9]*\).*/\1/p' .env | tail -1)"
HTTPS_PORT="${HTTPS_PORT:-5001}"
echo "phones: https port $HTTPS_PORT"

[ "$INSTALL_SERVICE" = 1 ] || { say "Done (no service). Start by hand: .venv/bin/python -m api.serve --port $PORT"; exit 0; }

# --- systemd ------------------------------------------------------------------------
say "systemd ($MODE)"
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
say "Waiting for https://127.0.0.1:$HTTPS_PORT/health (first start downloads the model)"
for _ in $(seq 1 120); do
    HEALTH="$(curl -ks "https://127.0.0.1:$HTTPS_PORT/health" 2>/dev/null || true)"
    if printf '%s' "$HEALTH" | grep -q '"status":"ok"'; then
        echo "$HEALTH"
        # The certificate is public (its fingerprint is in every pairing code);
        # make it readable so client.pair works without sudo. The key stays 600.
        [ "$MODE" = system ] && chmod 644 api/certs/server.pem
        say "Running. Pair a phone:  cd $REPO && .venv/bin/python -m client.pair"
        echo "Status: $STATUS    Logs: $LOGS"
        exit 0
    fi
    if printf '%s' "$HEALTH" | grep -q '"status":"error"'; then
        echo "The server is up but the model failed to load: $HEALTH" >&2
        echo "Check: $LOGS" >&2
        exit 1
    fi
    sleep 5
done
echo "No answer after 10 minutes. Check: $LOGS" >&2
exit 1
