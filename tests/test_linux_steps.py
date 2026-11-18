"""The Linux setup wizard's install actions (installer/linux_steps.py).

    .venv/bin/python -m pytest tests/test_linux_steps.py -q

No systemd, microphone, graphics card or network: every program is a fake.
"""
import os
import re
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
import linux_steps as ls  # noqa: E402


class Ran:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def runner(answers):
    """A fake subprocess.run: answers maps a word in the command to a Ran."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        for word, ran in answers.items():
            if word in cmd:
                return ran
        return Ran()
    run.calls = calls
    return run


# --- the unit files are the ones the shell installers write ---------------------------

def _script(name):
    with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as f:
        return f.read()


def _unit_lines(text):
    return [l for l in text.splitlines() if l and not l.startswith("#")]


def test_server_unit_matches_install_linux_sh():
    script = _script("install_linux.sh")
    body = re.search(r'UNIT_BODY="(.*?)"\n', script, re.S).group(1)
    body = (body.replace("$REPO", "/opt/wu").replace("$VPY", "/opt/wu/.venv/bin/python")
            .replace("$PORT", "5000").replace("$NVLIBS", ""))
    ours = ls.server_unit_text("/opt/wu", 5000)
    assert _unit_lines(ours) == _unit_lines(body) + ["[Install]", "WantedBy=default.target"]


def test_desktop_unit_matches_install_linux_desktop_sh():
    script = _script("install_linux_desktop.sh")
    body = re.search(r'<<UNIT\n(.*?)\nUNIT\n', script, re.S).group(1)
    body = body.replace("$REPO", "/opt/wu").replace("$VPY", "/opt/wu/.venv/bin/python")
    assert _unit_lines(ls.desktop_unit_text("/opt/wu")) == _unit_lines(body)


def test_unit_names_match_the_rest_of_the_app():
    sys.path.insert(0, os.path.join(ROOT, "client"))
    import linux_settings
    assert (ls.SERVER_UNIT, ls.DESKTOP_UNIT) == (linux_settings.SERVER_UNIT,
                                                 linux_settings.DESKTOP_UNIT)
    assert ls.MENU_ENTRY == linux_settings.MENU_ENTRY
    import linux_setup_plan
    assert ls.NVIDIA_BYTES == linux_setup_plan.PACKAGES["nvidia"]


# --- which card, and how the model is loaded ---------------------------------------------

def test_the_card_with_the_most_free_memory_is_picked():
    run = runner({"nvidia-smi": Ran("0, 700\n1, 6100\n")})
    assert ls.pick_gpu(run) == (1, 6100)
    assert ls.pick_gpu(runner({"nvidia-smi": Ran("", 9)})) is None

    def missing(cmd, **kw):
        raise FileNotFoundError("nvidia-smi")
    assert ls.pick_gpu(missing) is None


def test_a_nearly_full_card_gets_a_lighter_load_or_the_processor():
    assert ls.gpu_settings((1, 6100), "float16") == {
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "1",
        "WHISPER_COMPUTE_TYPE": "float16"}
    assert ls.gpu_settings((0, 2000), "float16")["WHISPER_COMPUTE_TYPE"] == "int8_float16"
    assert ls.gpu_settings((0, 2000), "int8")["WHISPER_COMPUTE_TYPE"] == "int8_float16"
    assert ls.gpu_settings((0, 6000), "int8")["WHISPER_COMPUTE_TYPE"] == "int8"   # an old card
    full = ls.gpu_settings((0, 900), "float16")
    assert full["WHISPER_DEVICE"] == "cpu" and full["CUDA_VISIBLE_DEVICES"] == ""
    assert ls.gpu_settings(None, "float16") == {}


# --- waiting for dictation, and what to say when it doesn't start ---------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_waiting_says_how_long_it_has_been():
    clock, seen, tries = Clock(), [], []

    def wait(port, timeout, interval, https):
        tries.append((port, https))
        clock.now += 4
        return (len(tries) >= 3), ("model small.en loaded" if len(tries) >= 3 else "no answer yet")
    ok, detail, said = ls.wait_for_server(5001, True, wait, progress=seen.append,
                                          clock=clock, sleep=clock.sleep,
                                          run=runner({"is-failed": Ran("", 1),
                                                      "NRestarts": Ran("0\n")}))
    assert (ok, detail, said) == (True, "model small.en loaded", [])
    assert tries[0] == (5001, True) and seen == [4, 10]
    assert ls.waiting_text(12).startswith("starting up: 12 seconds so far")
    assert ls.waiting_text(135) == ("still starting: 2 min 15 s so far (it is loading the "
                                    "speech model)")


JOURNAL = """Started whisper-unchained.service - Whisper Unchained Server (speech to text API).
[API] Loading large-v3-turbo on cuda (float16)...
[API] Error loading model: CUDA failed with error out of memory
whisper-unchained.service: Main process exited, code=exited, status=1/FAILURE
whisper-unchained.service: Failed with result 'exit-code'.
whisper-unchained.service: Scheduled restart job, restart counter is at 3.
"""


def test_a_server_that_keeps_stopping_is_reported_with_its_own_words():
    clock = Clock()
    run = runner({"is-failed": Ran("", 1), "NRestarts": Ran("3\n"),
                  "journalctl": Ran(JOURNAL)})
    ok, detail, said = ls.wait_for_server(
        5001, True, lambda *a, **k: (False, "connection refused"),
        clock=clock, sleep=clock.sleep, run=run)
    assert not ok and detail == "it keeps stopping while it starts"
    assert said == ["[API] Loading large-v3-turbo on cuda (float16)...",
                    "[API] Error loading model: CUDA failed with error out of memory"]


def test_a_model_that_fails_to_load_stops_the_wait_at_once():
    clock = Clock()
    run = runner({"journalctl": Ran(JOURNAL)})
    ok, detail, said = ls.wait_for_server(
        5001, False, lambda *a, **k: (False, "server answered, but the model failed to load"),
        clock=clock, sleep=clock.sleep, run=run)
    assert not ok and "model failed to load" in detail and len(said) == 2
    assert clock.now == 0


def test_no_answer_gives_up_after_the_timeout():
    clock = Clock()
    run = runner({"is-failed": Ran("", 1), "NRestarts": Ran("0\n"), "journalctl": Ran("")})
    ok, detail, said = ls.wait_for_server(
        5001, True, lambda *a, **k: (False, "no answer yet"), timeout=60,
        clock=clock, sleep=clock.sleep, run=run)
    assert (ok, detail, said) == (False, "no answer after 1 minutes", [])


# --- services -------------------------------------------------------------------------------

def test_install_server_writes_the_unit_and_starts_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "unit_dir", lambda: str(tmp_path / "units"))
    run = runner({})
    assert ls.install_server("/opt/wu", 5000, run=run) == (True, "")
    text = (tmp_path / "units" / "whisper-unchained.service").read_text()
    assert "ExecStart=/opt/wu/.venv/bin/python -m api.serve --host 0.0.0.0 --port 5000" in text
    assert [c[2] for c in run.calls] == ["daemon-reload", "enable", "restart"]
    bad = runner({"restart": Ran("", 1, "Unit not found.\n")})
    assert ls.install_server("/opt/wu", 5000, run=bad) == (False, "Unit not found.")


def test_lingering_is_read_from_loginctl():
    assert ls.lingering(runner({"loginctl": Ran("yes\n")}), "dom")
    assert not ls.lingering(runner({"loginctl": Ran("no\n")}), "dom")
    assert not ls.lingering(runner({"loginctl": Ran("", 1)}), "dom")


def test_the_files_menu_is_skipped_without_gnome_files(monkeypatch, tmp_path):
    monkeypatch.setattr(ls.shutil, "which", lambda name: None)
    ok, detail = ls.install_menu("/opt/wu")
    assert ok is None and "no such menu" in detail
    monkeypatch.setattr(ls.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ls, "MENU_ENTRY", str(tmp_path / "scripts" / "Transcribe"))
    assert ls.install_menu("/opt/wu") == (True, "")
    assert "/opt/wu/client/file_window.py" in (tmp_path / "scripts" / "Transcribe").read_text()


# --- microphone ------------------------------------------------------------------------------

class FakeRecorder:
    """Stands in for pw-record: writes a WAV when 'started'."""

    def __init__(self, samples):
        self.samples = samples

    def __call__(self, cmd, **kw):
        path = cmd[-1]
        if self.samples is not None:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(self.samples)
        return self

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def _which(*have):
    return lambda name: ("/usr/bin/" + name) if name in have else None


def test_a_working_microphone_and_speakers_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)
    run = runner({"pactl": Ran("1\talsa_output.pci\tPipeWire\n")})
    assert ls.check_audio(run, _which("pw-record", "pactl"), folder=str(tmp_path),
                          popen=FakeRecorder(b"\x01\x02" * 800)) == (True, True, [])
    assert os.listdir(str(tmp_path)) == []            # the test recording is not kept


def test_silence_no_microphone_and_no_speakers_are_each_said_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)
    run = runner({"pactl": Ran("0\tauto_null\tPipeWire\n")})
    mic, speakers, problems = ls.check_audio(run, _which("pw-record", "pactl"),
                                             folder=str(tmp_path),
                                             popen=FakeRecorder(b"\x00\x00" * 800))
    assert (mic, speakers) == (False, False)
    assert "only silence" in problems[0] and "No speakers or headphones" in problems[1]
    mic, _s, problems = ls.check_audio(runner({}), _which("pw-record"), folder=str(tmp_path),
                                       popen=FakeRecorder(None))
    assert not mic and "No microphone could be opened" in problems[0]
    mic, speakers, problems = ls.check_audio(runner({}), _which(), folder=str(tmp_path))
    assert (mic, speakers) == (False, False) and "pipewire-bin" in problems[0]


def test_missing_desktop_pieces_are_named_by_package():
    run = runner({"-c": Ran("", 1)})
    assert ls.desktop_missing(_which(), run=run) == [
        "xclip", "python3-gi", "gir1.2-gtk-3.0", "pipewire-bin"]
    assert ls.desktop_missing(_which("xclip", "pw-record"), run=runner({})) == []
