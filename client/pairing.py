"""Phone pairing payload, address detection and QR matrix (any OS).

Shared by the Settings window (Windows, Tk) and the headless pairing command
(`python -m client.pair`, Linux). Kept free of Tk and ctypes.windll so it
imports on a server with no desktop.
Contract: Whisper-Unchained-Android contracts/01-transcription-api.md §7 (v1)
and contracts/06 (v2, HTTPS + pinned certificate).
"""
import json
import secrets
import socket
import subprocess
import sys

PAIRING_VERSION = 1
# Windows only: keeps a console window from flashing up. POSIX rejects it.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def generate_api_key():
    """Installer-format key: wr_sec_ + 32 url-safe random characters."""
    return "wr_sec_" + secrets.token_urlsafe(24)


def pairing_payload(host, api_key, model, name, port=5000, cert_sha256=""):
    """The JSON the phone scans. api_key is OMITTED (not empty) with auth off.

    With HTTPS (cert_sha256 given) it's version 2: an https:// address plus the
    certificate fingerprint the phone pins (Android contract 06).
    """
    scheme = "https" if cert_sha256 else "http"
    payload = {"v": 2 if cert_sha256 else PAIRING_VERSION,
               "base_url": "%s://%s:%d/v1" % (scheme, host, port),
               "model": model, "name": name}
    if cert_sha256:
        payload["cert_sha256"] = cert_sha256
    if api_key:
        payload["api_key"] = api_key
    return json.dumps(payload, separators=(",", ":"))


def tailscale_ips():
    """This machine's Tailscale IPv4 addresses, or [] if Tailscale isn't here."""
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                                 timeout=2, creationflags=_NO_WINDOW)
        except Exception:
            continue
        ips = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        if ips:
            return ips
    return []


def lan_ips():
    """Non-loopback IPv4 addresses. The UDP connect sends nothing; it only asks
    the OS which interface would route out, which Linux's hostname lookup
    (often 127.0.1.1) doesn't tell us."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never actually reached
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    out = []
    for ip in ips:
        if not ip.startswith("127.") and ip not in out:
            out.append(ip)
    return out


def detect_hosts():
    """Addresses the phone could reach this PC on, best first:
    the Tailscale 100.x address, then LAN IPv4s. Never raises."""
    hosts = tailscale_ips()
    hosts += [ip for ip in lan_ips() if ip not in hosts]
    return hosts or ["127.0.0.1"]


def qr_matrix(data):
    """QR modules as rows of booleans, or None if qrcode isn't installed."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def qr_text(matrix, border=2):
    """Draw a QR matrix with half-block characters, two modules per line, so it
    scans from a terminal. Light modules are drawn as blocks and dark ones left
    blank, so it reads correctly on a dark-background terminal (the default)."""
    size = len(matrix)
    full = [[False] * (size + 2 * border) for _ in range(border)]
    rows = full + [[False] * border + list(r) + [False] * border for r in matrix] + full
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    # (top dark, bottom dark) -> character; light modules are drawn, dark are blank
    glyph = {(False, False): "\u2588", (True, False): "\u2584",
             (False, True): "\u2580", (True, True): " "}
    return "\n".join("".join(glyph[(top, bottom)] for top, bottom in zip(rows[i], rows[i + 1]))
                     for i in range(0, len(rows), 2))
