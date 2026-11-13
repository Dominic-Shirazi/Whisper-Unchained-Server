"""The Windows service's name, for code that queries or restarts it.

New installs register "WhisperUnchained". A PC set up before the rename may
still have the old "FasterWhisperAPI" service until scripts/install_service.ps1
is run again (it removes the old one), so callers ask `installed()`.
"""
import subprocess

NAME = "WhisperUnchained"
LEGACY = "FasterWhisperAPI"
ACCOUNT = "NT SERVICE\\" + NAME

_cache = {}


def _exists(name):
    try:
        return subprocess.run(["sc", "query", name], capture_output=True, timeout=10,
                              creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def installed(refresh=False):
    """The name of the service on this PC: NAME, else LEGACY if only the old
    one exists, else NAME (nothing installed yet)."""
    if refresh or "name" not in _cache:
        _cache["name"] = NAME if _exists(NAME) else (LEGACY if _exists(LEGACY) else NAME)
    return _cache["name"]


def account():
    """The service's own limited account (NT SERVICE\\<name>)."""
    return "NT SERVICE\\" + installed()
