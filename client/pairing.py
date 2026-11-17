"""Phone pairing payload, address detection and QR matrix (any OS).

Shared by the Settings window (Windows, Tk) and the headless pairing command
(`python -m client.pair`, Linux). Kept free of Tk and ctypes.windll so it
imports on a server with no desktop.
Contract: Whisper-Unchained-Android contracts/01-transcription-api.md §7 (v1)
and contracts/06 (v2, HTTPS + pinned certificate).
"""
import base64
import json
import re
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


ALT_HOSTS_MAX = 2


def pairing_payload(host, api_key, model, name, port=5000, cert_sha256="", alt_hosts=()):
    """The JSON the phone scans. api_key is OMITTED (not empty) with auth off.

    With HTTPS (cert_sha256 given) it's version 2: an https:// address plus the
    certificate fingerprint the phone pins (Android contract 06).

    alt_hosts: this same server's other addresses (say the home-network one
    when `host` is the Tailscale one). They go in "alt_base_urls", which the
    phone tries when base_url can't be reached; same key, same certificate.
    Left out when there are none. Older apps ignore the field.
    """
    scheme = "https" if cert_sha256 else "http"
    payload = {"v": 2 if cert_sha256 else PAIRING_VERSION,
               "base_url": "%s://%s:%d/v1" % (scheme, host, port),
               "model": model, "name": name}
    alts = []
    for h in alt_hosts or ():
        h = (h or "").strip()
        if h and h != host and not h.startswith("127.") and h not in alts:
            alts.append(h)
    if alts:
        payload["alt_base_urls"] = ["%s://%s:%d/v1" % (scheme, h, port)
                                    for h in alts[:ALT_HOSTS_MAX]]
    if cert_sha256:
        payload["cert_sha256"] = cert_sha256
    if api_key:
        payload["api_key"] = api_key
    return json.dumps(payload, separators=(",", ":"))


def is_tailscale_ip(ip):
    """Tailscale hands out addresses in 100.64.0.0/10 (100.64.x to 100.127.x)."""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def _tailscale_adapter_ips():
    """Tailscale addresses read straight off the network adapters: instant, and
    works when the tailscale command is slow or missing from PATH."""
    try:
        import psutil
        found = []
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and is_tailscale_ip(a.address) \
                        and a.address not in found:
                    # The adapter is named "Tailscale" on Windows, tailscale0 on Linux:
                    # its address goes first.
                    if "tailscale" in name.lower():
                        found.insert(0, a.address)
                    else:
                        found.append(a.address)
        return found
    except Exception:
        return []


def tailscale_ips():
    """This machine's Tailscale IPv4 addresses, or [] if Tailscale isn't here.
    The adapters first (instant); the tailscale command as a fallback. A slow
    command must never leave the pairing code with only the home address, or
    the phone works at home and fails everywhere else."""
    ips = _tailscale_adapter_ips()
    if ips:
        return ips
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                                 timeout=5, creationflags=_NO_WINDOW)
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


def primary_lan_ip():
    """The address this PC uses on its home network: the one the OS routes
    outgoing traffic through. Skips virtual adapters (WSL, Hyper-V, Docker),
    and the Tailscale address. "" if there's no network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: nothing is sent
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""
    return "" if ip.startswith(("127.", "100.")) else ip


def other_hosts(chosen, tailscale=None, home=None):
    """The addresses to add beside `chosen` in the pairing code: the Tailscale
    one and the home-network one, whichever `chosen` isn't. So a code made
    with either address works at home and away. Never raises."""
    try:
        ts = tailscale_ips() if tailscale is None else list(tailscale)
        lan = primary_lan_ip() if home is None else home
    except Exception:
        return []
    out = []
    for h in (ts[:1] + ([lan] if lan else [])):
        if h and h != chosen and h not in out:
            out.append(h)
    return out


def describe_other_hosts(hosts):
    """One line for the pairing page saying what else the code carries, or ""."""
    if not hosts:
        return ""
    parts = ["%s (%s)" % (h, "Tailscale" if is_tailscale_ip(h) else "home network")
             for h in hosts]
    return "The code also carries " + " and ".join(parts) + \
        ", so the phone can switch when the first address can't be reached."


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


# --- connection code: the pairing payload as one line to copy and paste ---------
# For a PC with no camera: the same information the QR code holds (address,
# other addresses, key, certificate fingerprint), as text. "WU1-" + the JSON,
# base64url without padding. It contains the API key: treat it like a password.

CODE_PREFIX = "WU1-"
ALT_URLS_KEY = "REMOTE_ALT_URLS"


def connection_code(payload_json):
    """The pairing payload (pairing_payload's JSON) as one pasteable line."""
    raw = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    return CODE_PREFIX + raw


def parse_connection_code(text):
    """A pasted connection code -> the payload dict. Raises ValueError with a
    plain sentence when it isn't one. Also accepts the bare JSON."""
    s = "".join((text or "").split())
    if not s:
        raise ValueError("Paste the connection code first.")
    try:
        if s.startswith("{"):
            data = json.loads(text.strip())
        else:
            if not s.upper().startswith(CODE_PREFIX):
                raise ValueError
            body = s[len(CODE_PREFIX):]
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except Exception:
        raise ValueError("That isn't a connection code. Copy it again from the other PC "
                         "(Settings > Devices & Apps > Another program or PC).") from None
    if not isinstance(data, dict) or data.get("v") not in (1, 2):
        raise ValueError("This connection code is from a newer version. Update this PC's "
                         "app, then paste it again.")
    urls = [data.get("base_url")] + list(data.get("alt_base_urls") or [])
    urls = [u.strip().rstrip("/") for u in urls if isinstance(u, str) and u.strip()]
    if not urls or not all(re.fullmatch(r"https?://[^\s/]+(?::\d+)?/v1", u) for u in urls):
        raise ValueError("The connection code has no usable address. Copy it again from "
                         "the other PC.")
    pin = (data.get("cert_sha256") or "").strip().lower()
    if any(u.startswith("https://") for u in urls) and not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise ValueError("The connection code is missing the security fingerprint. Copy it "
                         "again from the other PC.")
    return {"urls": urls, "api_key": (data.get("api_key") or "").strip(),
            "cert_sha256": pin, "name": str(data.get("name") or "").strip()[:60],
            "model": str(data.get("model") or "").strip()[:60]}


def remote_settings(parsed, reachable=None):
    """The .env values for "transcription on another PC" from a parsed code.
    reachable(url) -> bool picks which address goes first (the one this PC can
    reach right now); without it, or when none answers, the code's own order."""
    urls = list(parsed["urls"])
    if reachable is not None:
        for u in list(urls):
            try:
                ok = reachable(u)
            except Exception:
                ok = False
            if ok:
                urls.remove(u)
                urls.insert(0, u)
                break
    return {"TRANSCRIBE_SOURCE": "remote", "REMOTE_SERVER_URL": urls[0],
            ALT_URLS_KEY: ",".join(urls[1:]), "REMOTE_API_KEY": parsed["api_key"],
            "REMOTE_CERT_SHA256": parsed["cert_sha256"]}
