"""The setup steps added after a laptop install hung with no explanation
(2026-11-18): reading the elevated script's log, and saying plainly that the
server has not started when its certificate is not there yet."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in ("installer", "client"):
    sys.path.insert(0, os.path.join(ROOT, p))

import local_api  # noqa: E402
import steps  # noqa: E402


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8"])
def test_log_says_reads_both_kinds_of_powershell_log(tmp_path, encoding):
    log = tmp_path / "service.log"
    log.write_text("Starting...\nRESTART NEEDED: the runtime was installed\n", encoding=encoding)
    assert steps.log_says(str(log), "RESTART NEEDED")
    assert not steps.log_says(str(log), "something else")


def test_log_says_is_false_for_a_missing_log(tmp_path):
    assert not steps.log_says(str(tmp_path / "none.log"), "RESTART NEEDED")


def test_health_check_without_a_certificate_says_not_started(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "ROOT", str(tmp_path))
    ok, detail = steps.wait_for_server(5999, timeout=0.2, interval=0.01, https=True,
                                       sleep=lambda s: None)
    assert not ok and detail == steps.NOT_STARTED


def test_missing_certificate_error_is_put_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(local_api, "CERT_PATH", str(tmp_path / "server.pem"))
    error = OSError("Could not find a suitable TLS CA certificate bundle, invalid path: x")
    assert local_api.plain_error(error) == local_api.NOT_STARTED
    assert "TLS" not in local_api.plain_error(error)


def test_other_errors_keep_their_text(tmp_path, monkeypatch):
    monkeypatch.setattr(local_api, "CERT_PATH", str(tmp_path / "server.pem"))
    assert local_api.plain_error(OSError("timed out")) == "API Error: timed out"
    # The certificate exists: a TLS complaint is then a real one, shown as is.
    (tmp_path / "server.pem").write_text("x")
    error = OSError("Could not find a suitable TLS CA certificate bundle")
    assert local_api.plain_error(error).startswith("API Error:")


# --- The install log ----------------------------------------------------------

def test_system_facts_name_the_runtime_and_nothing_personal():
    facts = steps.system_facts(lambda: ["Intel(R) UHD Graphics 620"])
    text = "\n".join(facts)
    assert "Visual C++ runtime (msvcp140.dll):" in text
    assert "Graphics: Intel(R) UHD Graphics 620" in text
    assert os.path.expanduser("~") not in text


def test_file_version_of_a_missing_file_is_empty(tmp_path):
    assert steps.file_version(str(tmp_path / "nothing.dll")) == ""


class _Ended:
    """A server that prints two lines and ends with an access violation."""

    def __init__(self, *a, **k):
        self.stdout = iter(["[API] Loading small.en on the processor\n",
                            "[API] Whisper hotwords: Zanzibar\n"])

    def wait(self, timeout=None):
        return 3221225477

    def kill(self):
        pass


def test_diagnose_start_records_a_silent_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "LOG_DIR", str(tmp_path))
    verdict = steps.diagnose_start(root=str(tmp_path), run=_Ended)
    assert "0xC0000005" in verdict and "no error message" in verdict
    log = (tmp_path / "server_start.log").read_text(encoding="utf-8")
    assert "Loading small.en" in log and "0xC0000005" in log
    assert "Zanzibar" not in log          # the person's own words stay out


def test_write_log_adds_a_time_stamped_line(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "LOG_DIR", str(tmp_path))
    steps.write_log("step check: fail")
    line = (tmp_path / "wizard.log").read_text(encoding="utf-8")
    assert line.rstrip().endswith("step check: fail") and line[:2] == "20"
