"""Start the transcription server: `python -m api.serve [--host H] [--port P] [--https-port Q]`.

Two ports, one server (one copy of the model in memory):
  - WHISPER_API_PORT (5000): plain HTTP for programs on this network, the usual
    way self-hosted AI servers work. Off with SERVER_PLAIN_HTTP=false. Only
    answers this PC, the local network and Tailscale (Tailscale encrypts on its
    own); anything else gets 403.
  - WHISPER_HTTPS_PORT (5001): HTTPS with this PC's own certificate (api/tls.py,
    created on first start), when SERVER_HTTPS=true. Phones pair to this one.
The Windows service runs this, so changing either is a .env change + restart.
"""
import argparse
import asyncio
import ipaddress
import json
import os
import signal
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import tls  # noqa: E402


def uvicorn_options(env=None, ensure=tls.ensure_cert):
    """Extra uvicorn options for the HTTPS port: the certificate pair."""
    if not tls.https_enabled(env):
        return {}
    cert_path, key_path = ensure()
    return {"ssl_certfile": cert_path, "ssl_keyfile": key_path}


def is_nearby(host):
    """True for this PC, private networks, link-local and Tailscale (100.64/10):
    every address that isn't on the open internet."""
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not ip.is_global


class NearbyOnly:
    """ASGI wrapper for the unencrypted port: refuse clients from the internet,
    so a forwarded router port can't expose dictations in plain text."""

    def __init__(self, app, https_port=None):
        self.app, self.https_port = app, https_port

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client") or ("", 0)
            if not is_nearby(client[0]):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                msg = ("Unencrypted connections are only accepted from this PC's own "
                       "network and Tailscale.")
                if self.https_port:
                    msg += " Use the encrypted address: https on port %d." % self.https_port
                body = json.dumps({"error": {"message": msg, "type": "forbidden",
                                             "code": "unencrypted_from_internet"}}).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def plan(env=None, plain_port=None, https_port=None):
    """[(kind, port)] the server should open, in start order."""
    plain_port = plain_port or tls.plain_port(env)
    https_port = https_port or tls.https_port(env)
    out = []
    if tls.https_enabled(env):
        out.append(("https", https_port))
    if tls.plain_enabled(env):
        if out and plain_port == https_port:
            print("[Serve] WHISPER_API_PORT and WHISPER_HTTPS_PORT are both %d; "
                  "serving HTTPS only. Pick two different ports in Settings." % plain_port)
        else:
            out.append(("http", plain_port))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Whisper Unchained Server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=None, help="plain HTTP port (WHISPER_API_PORT)")
    p.add_argument("--https-port", type=int, default=None, help="HTTPS port (WHISPER_HTTPS_PORT)")
    args = p.parse_args(argv)
    sys.path.insert(0, ROOT)
    from api.whisper_api import app  # loads the model once, for both ports
    run(app, args.host, args.port, args.https_port)


def run(app, host="0.0.0.0", plain_port=None, https_port=None):
    import uvicorn

    ports = plan(plain_port=plain_port, https_port=https_port)
    servers = []
    for i, (kind, port) in enumerate(ports):
        if kind == "https":
            cfg = uvicorn.Config(app, host=host, port=port, **uvicorn_options())
        else:
            https = next((q for k, q in ports if k == "https"), None)
            cfg = uvicorn.Config(NearbyOnly(app, https), host=host, port=port)
        if i:
            cfg.lifespan = "off"  # startup hooks run once, on the first port
        print("[Serve] %s on %s:%d" % (kind.upper(), host, port))
        servers.append(_Server(cfg))
    asyncio.run(_serve_all(servers))


class _Server:
    """uvicorn.Server whose own Ctrl+C handling is turned off: _serve_all stops
    every port together instead (each server would otherwise replace the
    previous one's signal handler, leaving a port running)."""

    def __new__(cls, config):
        import contextlib
        import uvicorn

        class S(uvicorn.Server):
            def capture_signals(self):
                return contextlib.nullcontext()
        return S(config)


async def _serve_all(servers):
    def stop(*_):
        for s in servers:
            s.should_exit = True
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except (ValueError, OSError):
                pass
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop()  # one port failed or stopped: stop the rest, so the service restarts whole
    await asyncio.gather(*tasks, return_exceptions=True)
    for t in done:
        if t.exception():
            raise t.exception()
    if any(not s.started for s in servers):
        raise SystemExit(1)  # e.g. a port already in use


if __name__ == "__main__":
    main()
