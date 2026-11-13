"""Print the phone pairing code in a terminal: `python -m client.pair`.

For a headless server (Linux) with no Settings window. Shows the same QR the
Settings window's Phone section draws, plus its text, which the Android app
also accepts pasted. The text holds the API key, so treat it like a password.

Address: this machine's Tailscale IP, else its LAN IP (--host overrides).
HTTPS (SERVER_HTTPS=true): the QR carries the certificate's SHA-256, so the
server must have started once to create api/certs/server.pem.
"""
import argparse
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import local_api  # noqa: E402
import pairing  # noqa: E402

ENV_PATH = os.path.join(ROOT, ".env")


def read_env(path=ENV_PATH):
    """The .env values, without touching os.environ (dotenv parses quoting)."""
    if not os.path.exists(path):
        return {}
    from dotenv import dotenv_values
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def build(env, host=None, name=None, cert_path=None):
    """(payload, host, fingerprint). Raises SystemExit when HTTPS is on but the
    certificate doesn't exist yet: a v2 code without the pin can't pair."""
    host = host or pairing.detect_hosts()[0]
    cert_path = cert_path or local_api.CERT_PATH
    fp = ""
    if local_api.https_enabled(env):
        fp = local_api.fingerprint(cert_path)
        if not fp:
            raise SystemExit("HTTPS is on but %s doesn't exist yet. Start the server once "
                             "(it creates the certificate), then run this again." % cert_path)
    api_key = (env.get("WHISPER_API_KEY") or "").strip()
    payload = pairing.pairing_payload(
        host, api_key, env.get("WHISPER_MODEL_SIZE") or "large-v3-turbo",
        name or socket.gethostname(), port=int(local_api.pairing_port(env)), cert_sha256=fp)
    return payload, host, fp


def main(argv=None):
    p = argparse.ArgumentParser(description="Show the Whisper Unchained phone pairing code.")
    p.add_argument("--host", help="address the phone should use (default: Tailscale IP, else LAN IP)")
    p.add_argument("--name", help="name the phone shows for this server (default: hostname)")
    p.add_argument("--no-qr", action="store_true", help="print only the text")
    args = p.parse_args(argv)

    env = read_env()
    payload, host, fp = build(env, host=args.host, name=args.name)
    if not args.no_qr:
        matrix = pairing.qr_matrix(payload)
        if matrix is None:
            print("(qrcode isn't installed: pip install qrcode. Text only.)")
        else:
            print(pairing.qr_text(matrix))
    print()
    print("Scan in Whisper Unchained: Settings > Scan pairing code.")
    print("Address: %s:%s" % (host, local_api.pairing_port(env)))
    if fp:
        print("Certificate SHA-256: %s" % fp)
    print("Pairing text (contains the API key, keep it private):")
    print(payload)


if __name__ == "__main__":
    main()
