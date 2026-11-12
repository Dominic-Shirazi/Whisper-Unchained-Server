"""The server's own TLS certificate (Android contract 06).

Phones reach this PC by LAN or Tailscale address, which no public certificate
authority will sign. Instead the server makes its own certificate once, and the
pairing QR carries its SHA-256 fingerprint: the phone trusts exactly this
certificate. That pin is the server's identity, so the address can change.

The files live in api/certs/ (the service may write under api/). They are
git-ignored, so the installer never packages them and updates keep them.
"""
import datetime
import hashlib
import ipaddress
import os
import socket
import ssl
import subprocess
import sys

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
CERT_NAME = "server.pem"
KEY_NAME = "server.key"
VALID_DAYS = 20 * 365  # pinned, not CA-checked: a long life avoids forced re-pairing

TRUE = ("1", "true", "yes", "on")


def https_enabled(env=None):
    env = os.environ if env is None else env
    return (env.get("SERVER_HTTPS") or "").strip().lower() in TRUE


def paths(cert_dir=CERT_DIR):
    return os.path.join(cert_dir, CERT_NAME), os.path.join(cert_dir, KEY_NAME)


def fingerprint(cert_path):
    """Lowercase hex SHA-256 of the certificate's DER bytes (stdlib only)."""
    with open(cert_path, "r", encoding="ascii") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def _restrict_key(key_path):
    """Only this account, SYSTEM and Administrators may read the private key.

    The service account creates it at first start; a hand run (python -m
    api.serve) creates it as that user. Best effort: a failure leaves the
    folder's normal permissions, which already exclude other users' writes.
    """
    if sys.platform != "win32":
        os.chmod(key_path, 0o600)
        return
    try:
        me = subprocess.run(["whoami"], capture_output=True, text=True, timeout=10,
                            creationflags=0x08000000).stdout.strip()
        args = ["icacls", key_path, "/inheritance:r",
                "/grant:r", "*S-1-5-18:F", "/grant:r", "*S-1-5-32-544:F"]
        if me:
            args += ["/grant:r", me + ":F"]
        # The service's own limited account (scripts/install_service.ps1) must
        # read it even when a hand run created the key. Skipped if it doesn't exist.
        svc = "NT SERVICE\\FasterWhisperAPI"
        if me.lower() != svc.lower() and subprocess.run(
                ["sc", "query", "FasterWhisperAPI"], capture_output=True,
                timeout=10, creationflags=0x08000000).returncode == 0:
            args += ["/grant:r", svc + ":R"]
        subprocess.run(args, capture_output=True, timeout=10, creationflags=0x08000000)
    except Exception as e:  # never block startup over permissions
        print("[TLS] Couldn't tighten key permissions: %s" % e)


def ensure_cert(cert_dir=CERT_DIR, hostname=None):
    """Return (cert_path, key_path), creating a self-signed pair if missing.

    An existing pair is never replaced: replacing it would break every paired
    phone (their pin would no longer match).
    """
    cert_path, key_path = paths(cert_dir)
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    os.makedirs(cert_dir, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    hostname = hostname or socket.gethostname() or "localhost"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Whisper Unchained Server")])
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if hostname.lower() != "localhost":
        sans.append(x509.DNSName(hostname))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            # Self-signed and used as its own trust anchor by the desktop
            # client (cafile=server.pem), so it is marked as a CA that can't
            # sign anything else (path length 0).
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))

    # Key first, so a crash never leaves a certificate without its key.
    tmp_key = key_path + ".tmp"
    with open(tmp_key, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    _restrict_key(tmp_key)
    os.replace(tmp_key, key_path)
    tmp_cert = cert_path + ".tmp"
    with open(tmp_cert, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(tmp_cert, cert_path)
    print("[TLS] Created this server's certificate (SHA-256 %s)." % fingerprint(cert_path))
    return cert_path, key_path
