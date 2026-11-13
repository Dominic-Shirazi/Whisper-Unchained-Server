"""API keys encrypted at rest in .env (api/secrets_store.py, Windows DPAPI).

Every test works on temp files; the real .env is never read or written.
"""
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "api"))

import secrets_store as ss  # noqa: E402

windows_only = pytest.mark.skipif(not ss.available(), reason="DPAPI is Windows-only")

KEY = "wr_sec_test_key_123"
# Valid base64, but not a DPAPI blob made on this PC.
FOREIGN = "dpapi:" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="


# --- protect / reveal ------------------------------------------------------

@windows_only
def test_round_trip():
    enc = ss.protect(KEY)
    assert enc.startswith("dpapi:") and KEY not in enc
    assert ss.reveal(enc) == KEY
    assert ss.protect(enc) == enc            # never double-encrypted
    assert ss.protect(KEY) != enc            # salted: each save differs


@pytest.mark.parametrize("value", ["", None, KEY, "  spaced  ", "gsk_abc=="])
def test_plain_values_pass_through(value):
    assert ss.reveal(value) == value


def test_bad_blob_is_missing_and_never_logged(capsys, monkeypatch):
    monkeypatch.setattr(ss, "_warned", set())
    for bad in (FOREIGN, "dpapi:not base64!!", "dpapi:"):
        assert ss.reveal(bad, "CLOUD_API_KEY") == ""
        assert ss.unreadable(bad)
    err = capsys.readouterr().err
    assert "CLOUD_API_KEY" in err and "QUJD" not in err


def test_no_dpapi_means_plain_passthrough(monkeypatch):
    """Linux / non-Windows: nothing is encrypted and nothing crashes."""
    monkeypatch.setattr(ss, "_dpapi", lambda: None)
    assert not ss.available()
    assert ss.protect(KEY) == KEY
    assert ss.reveal(KEY) == KEY
    assert ss.reveal(FOREIGN, "X") == ""
    assert ss.migrate_env_file(os.path.join(ROOT, "no-such.env")) == []


@windows_only
def test_reveal_env_only_touches_secret_keys():
    enc = ss.protect(KEY)
    out = ss.reveal_env({"CLOUD_API_KEY": enc, "OTHER": "dpapi:x", "WHISPER_API_KEY": None})
    assert out == {"CLOUD_API_KEY": KEY, "OTHER": "dpapi:x", "WHISPER_API_KEY": None}


# --- migration of an existing plaintext .env -------------------------------

@windows_only
def test_migrate_encrypts_in_place_and_keeps_everything_else(tmp_path):
    p = tmp_path / ".env"
    original = (b"# comment with WHISPER_API_KEY=old\r\n"
                b"WHISPER_API_KEY=" + KEY.encode() + b"\r\n"
                b"\r\n"
                b"CLOUD_API_KEY=\"gsk_quoted\"\r\n"
                b"REMOTE_API_KEY=\r\n"
                b"# EDIT_LLM_API_KEY=commented\r\n"
                b"HOTKEY=`\r\n")
    p.write_bytes(original)
    assert sorted(ss.migrate_env_file(str(p))) == ["CLOUD_API_KEY", "WHISPER_API_KEY"]
    data = p.read_bytes()
    assert KEY.encode() not in data and b"gsk_quoted" not in data
    lines = data.split(b"\r\n")
    assert lines[0] == b"# comment with WHISPER_API_KEY=old"
    assert lines[4] == b"REMOTE_API_KEY=" and lines[5] == b"# EDIT_LLM_API_KEY=commented"
    assert lines[6] == b"HOTKEY=`"
    from dotenv import dotenv_values
    vals = ss.reveal_env(dotenv_values(str(p)))
    assert vals["WHISPER_API_KEY"] == KEY and vals["CLOUD_API_KEY"] == "gsk_quoted"
    # Second run: nothing left to do, file untouched.
    assert ss.migrate_env_file(str(p)) == []
    assert p.read_bytes() == data
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


# --- Settings / installer (settings_gui.read_env / write_env) --------------

@pytest.fixture
def sg_env(tmp_path, monkeypatch):
    import settings_gui as sg
    p = tmp_path / ".env"
    p.write_text("# keys\nWHISPER_API_KEY=%s\nCLOUD_API_KEY=\nHOTKEY=`\n" % KEY, encoding="utf-8")
    monkeypatch.setattr(sg, "ENV_PATH", str(p))
    return sg, p


@windows_only
def test_settings_save_encrypts_and_reads_back_plain(sg_env):
    sg, p = sg_env
    assert sg.read_env()["WHISPER_API_KEY"] == KEY          # plaintext still reads
    sg.write_env({"WHISPER_API_KEY": KEY, "CLOUD_API_KEY": "gsk_x", "HOTKEY": "f9"})
    text = p.read_text(encoding="utf-8")
    assert KEY not in text and "gsk_x" not in text and "HOTKEY=f9" in text
    assert text.startswith("# keys\n")
    vals = sg.read_env()
    assert vals["WHISPER_API_KEY"] == KEY and vals["CLOUD_API_KEY"] == "gsk_x"
    # Saving the same key again keeps the exact stored line (no churn).
    sg.write_env({"WHISPER_API_KEY": KEY, "CLOUD_API_KEY": "gsk_x"})
    assert p.read_text(encoding="utf-8") == text
    # A changed key is re-encrypted.
    sg.write_env({"WHISPER_API_KEY": "wr_sec_new"})
    assert sg.read_env()["WHISPER_API_KEY"] == "wr_sec_new"
    # Clearing a key really clears it.
    sg.write_env({"CLOUD_API_KEY": ""})
    assert "CLOUD_API_KEY=\n" in p.read_text(encoding="utf-8")


def test_settings_keeps_a_foreign_key_it_shows_empty(sg_env):
    """.env from another PC: the page shows the key empty; saving the page must
    not silently drop it (the server stays locked, not open)."""
    sg, p = sg_env
    p.write_text("WHISPER_API_KEY=%s\n" % FOREIGN, encoding="utf-8")
    assert sg.read_env()["WHISPER_API_KEY"] == ""
    sg.write_env({"WHISPER_API_KEY": ""})
    assert FOREIGN in p.read_text(encoding="utf-8")


# --- client readers ------------------------------------------------------------

@windows_only
def test_client_sends_the_plain_key():
    import transcribe_client
    assert transcribe_client._bearer(ss.protect(KEY)) == {"Authorization": "Bearer " + KEY}
    assert transcribe_client._bearer(KEY) == {"Authorization": "Bearer " + KEY}
    assert transcribe_client._bearer(FOREIGN) == {}


@windows_only
def test_pairing_code_carries_the_plain_key(tmp_path):
    import pair
    p = tmp_path / ".env"
    p.write_text("WHISPER_API_KEY=%s\nSERVER_HTTPS=false\n" % ss.protect(KEY), encoding="utf-8")
    env = pair.read_env(str(p))
    assert env["WHISPER_API_KEY"] == KEY
    payload, _host, _fp = pair.build(env, host="127.0.0.1", name="t")
    assert KEY in payload


@windows_only
def test_game_mode_sends_the_plain_key(monkeypatch):
    import game_mode
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None, context=None):
        seen["auth"] = req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(game_mode.urllib.request, "urlopen", fake_urlopen)
    game_mode._request({"WHISPER_API_KEY": ss.protect(KEY), "SERVER_HTTPS": "false"}, "/health")
    assert seen["auth"] == "Bearer " + KEY


# --- the server ------------------------------------------------------------

_SERVER_PROBE = textwrap.dedent("""
    import sys, types
    sys.path.insert(0, sys.argv[1])
    fw = types.ModuleType("faster_whisper")
    class M:
        def __init__(self, *a, **k): pass
    fw.WhisperModel = M
    sys.modules["faster_whisper"] = fw
    from fastapi.testclient import TestClient
    import api.whisper_api as wa
    c = TestClient(wa.app)
    codes = [c.get("/v1/models", headers={"Authorization": "Bearer " + sys.argv[2]}).status_code,
             c.get("/v1/models").status_code,
             c.get("/v1/models", headers={"Authorization": "Bearer " + sys.argv[3]}).status_code]
    print("CODES", *codes)
""")


def _server_codes(stored_key, try_key):
    # load_dotenv never overrides a variable that's already set, so these win
    # over whatever the real .env holds.
    env = dict(os.environ, WHISPER_API_KEY=stored_key, WHISPER_API_KEY_REQUIRED="true",
               EDIT_LLM_API_KEY="", MEAN_LLM_API_KEY="")
    r = subprocess.run([sys.executable, "-c", _SERVER_PROBE, ROOT, try_key, stored_key],
                       env=env, capture_output=True, text=True, timeout=120, cwd=ROOT)
    line = [l for l in r.stdout.splitlines() if l.startswith("CODES")]
    assert line, "server probe failed (exit %d)" % r.returncode
    return [int(x) for x in line[-1].split()[1:]]


@windows_only
def test_server_accepts_the_plain_key_when_env_holds_an_encrypted_one():
    enc = ss.protect(KEY)
    plain_ok, missing, blob_itself = _server_codes(enc, KEY)
    assert (plain_ok, missing, blob_itself) == (200, 401, 401)


def test_server_fails_closed_on_a_key_from_another_pc():
    """An undecryptable key must lock the server, never switch auth off."""
    assert _server_codes(FOREIGN, "") [:2] == [401, 401]
