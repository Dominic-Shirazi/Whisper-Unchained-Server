"""Two ports, one server: plain HTTP for programs on this network, HTTPS for
phones. The plain port refuses clients from the open internet."""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "client"))

import local_api  # noqa: E402
import serve  # noqa: E402
import tls  # noqa: E402
from pairing import pairing_payload  # noqa: E402


def test_plain_port_defaults():
    # Old installs had no SERVER_PLAIN_HTTP line: keep their plain port.
    assert tls.plain_enabled({})
    assert tls.plain_enabled({"SERVER_HTTPS": "true"})
    assert not tls.plain_enabled({"SERVER_HTTPS": "true", "SERVER_PLAIN_HTTP": "false"})
    # Never both off: with HTTPS off the plain port stays on.
    assert tls.plain_enabled({"SERVER_HTTPS": "false", "SERVER_PLAIN_HTTP": "false"})
    assert tls.plain_port({}) == 5000 and tls.https_port({}) == 5001
    assert tls.https_port({"WHISPER_HTTPS_PORT": "6001"}) == 6001
    assert tls.https_port({"WHISPER_HTTPS_PORT": "nope"}) == 5001


def test_plan():
    assert serve.plan({}) == [("http", 5000)]
    assert serve.plan({"SERVER_HTTPS": "true"}) == [("https", 5001), ("http", 5000)]
    assert serve.plan({"SERVER_HTTPS": "true", "SERVER_PLAIN_HTTP": "false"}) == [("https", 5001)]
    # Same number for both: HTTPS wins rather than failing to start.
    env = {"SERVER_HTTPS": "true", "WHISPER_API_PORT": "5001"}
    assert serve.plan(env) == [("https", 5001)]
    assert serve.plan({"SERVER_HTTPS": "true"}, plain_port=7000, https_port=7001) == \
        [("https", 7001), ("http", 7000)]


def test_nearby_addresses():
    for ip in ("127.0.0.1", "::1", "192.168.0.20", "10.1.2.3", "172.16.5.4",
               "100.89.217.5", "fe80::1", "fd7a:115c:a1e0::1", "::ffff:192.168.1.5"):
        assert serve.is_nearby(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "::ffff:8.8.8.8", "", "junk"):
        assert not serve.is_nearby(ip), ip


def _call(app, client_ip):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "client": (client_ip, 50000), "path": "/health",
             "method": "GET", "headers": []}
    asyncio.run(app(scope, receive, send))
    return sent


def test_plain_port_refuses_internet_clients():
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["client"][0])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = serve.NearbyOnly(inner, https_port=5001)
    assert _call(app, "192.168.0.7")[0]["status"] == 200
    out = _call(app, "8.8.8.8")
    assert out[0]["status"] == 403
    body = json.loads(out[1]["body"])
    assert body["error"]["code"] == "unencrypted_from_internet"
    assert "5001" in body["error"]["message"]
    assert reached == ["192.168.0.7"]


def test_lifespan_passes_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(serve.NearbyOnly(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_phone_pairs_to_https_port():
    env = {"SERVER_HTTPS": "true", "WHISPER_API_PORT": "5000", "WHISPER_HTTPS_PORT": "5001"}
    assert local_api.pairing_port(env) == "5001"
    payload = json.loads(pairing_payload("192.168.0.2", "", "m", "pc",
                                         port=int(local_api.pairing_port(env)),
                                         cert_sha256="ab" * 32))
    assert payload["base_url"] == "https://192.168.0.2:5001/v1"
    assert local_api.pairing_port({"WHISPER_API_PORT": "5005"}) == "5005"


def test_own_client_uses_plain_port_when_on():
    env = {"SERVER_HTTPS": "true"}
    assert not local_api.uses_https(env)
    assert local_api.requests_verify(env) is True and local_api.ssl_context(env) is None
    env["SERVER_PLAIN_HTTP"] = "false"
    assert local_api.uses_https(env)
    assert local_api.base_url(env) == "https://127.0.0.1:5001"
