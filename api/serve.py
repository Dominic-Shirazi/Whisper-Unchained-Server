"""Start the transcription server: `python -m api.serve [--host H] [--port P]`.

With SERVER_HTTPS=true in .env it serves HTTPS with this PC's own certificate
(api/tls.py, created on first start); otherwise plain HTTP as before. The
Windows service runs this, so turning HTTPS on is a .env change + restart.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import tls  # noqa: E402


def uvicorn_options(env=None, ensure=tls.ensure_cert):
    """Extra uvicorn.run() options: the certificate pair when HTTPS is on."""
    if not tls.https_enabled(env):
        return {}
    cert_path, key_path = ensure()
    return {"ssl_certfile": cert_path, "ssl_keyfile": key_path}


def main(argv=None):
    p = argparse.ArgumentParser(description="Whisper Unchained Server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int,
                   default=int((os.environ.get("WHISPER_API_PORT") or "5000").strip() or 5000))
    args = p.parse_args(argv)
    import uvicorn
    opts = uvicorn_options()
    print("[Serve] %s on %s:%d" % ("HTTPS" if opts else "HTTP", args.host, args.port))
    uvicorn.run("api.whisper_api:app", host=args.host, port=args.port, **opts)


if __name__ == "__main__":
    main()
