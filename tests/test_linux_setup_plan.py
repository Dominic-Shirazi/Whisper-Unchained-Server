"""What Linux setup says before it starts (installer/linux_setup_plan.py):
the statement about this PC and the one download figure.

    .venv/bin/python -m pytest tests/test_linux_setup_plan.py -q

Nothing is installed or downloaded: a temporary folder stands in for the app.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
import linux_setup_plan as plan  # noqa: E402

UHD, RX = "Intel UHD Graphics 630", "AMD Radeon RX 6600 series card"


def test_an_nvidia_card_gets_everything_in_one_figure(tmp_path):
    p = plan.server_plan(str(tmp_path), device="cuda", nvidia="GeForce RTX 3060", names=[UHD])
    assert p.found == "We found an NVIDIA GeForce RTX 3060. Dictation will run on it."
    # packages + NVIDIA's libraries + large-v3-turbo
    assert p.download == 159_000_000 + 1_387_000_000 + 1_620_000_000
    assert p.download_text == "Setup will download about 3.2 GB."
    assert p.model == "large-v3-turbo"


def test_a_separate_amd_card_gets_the_second_engine(tmp_path):
    p = plan.server_plan(str(tmp_path), engine="whisper.cpp", names=[UHD, RX])
    assert p.found == "We found an AMD Radeon RX 6600 series card. Dictation will run on it."
    assert p.download_text == "Setup will download about 750 MB."
    assert p.model == "large-v3-turbo-q5_0"


def test_no_card_means_the_processor_and_the_lighter_model(tmp_path):
    p = plan.server_plan(str(tmp_path), names=[UHD])
    assert p.found == ("No separate graphics card was found, only Intel UHD Graphics 630, "
                       "which is built into the processor. Dictation will run on the "
                       "processor, in a lighter version.")
    assert p.model == "small.en" and p.download_text == "Setup will download about 650 MB."
    assert plan.server_plan(str(tmp_path), names=[]).found.startswith(
        "No graphics card was found.")


def test_a_card_that_cannot_be_used_is_named_with_the_reason(tmp_path):
    p = plan.server_plan(str(tmp_path), nvidia="GeForce GTX 750", problem="driver", names=[])
    assert p.found == ("We found an NVIDIA GeForce GTX 750, but its driver is too old to use "
                       "it. Dictation will run on the processor, in a lighter version.")
    p = plan.server_plan(str(tmp_path), nvidia="GeForce GTX 750", problem="memory", names=[])
    assert "but it has too little memory" in p.found


def test_an_nvidia_card_without_its_driver_says_so(tmp_path):
    p = plan.server_plan(str(tmp_path), names=[UHD, "NVIDIA GeForce RTX 2070 SUPER"])
    assert p.found.startswith("We found an NVIDIA GeForce RTX 2070 SUPER, but NVIDIA's driver "
                              "is not installed")
    assert "in a lighter version" in p.found and "ubuntu-drivers" in p.found
    p = plan.server_plan(str(tmp_path), names=["NVIDIA graphics card"])
    assert p.found.startswith("We found an NVIDIA graphics card, but")


def test_the_quick_engine_on_the_processor_is_not_a_lighter_version(tmp_path):
    p = plan.server_plan(str(tmp_path), engine="parakeet", names=[UHD])
    assert p.found == ("No separate graphics card was found, only Intel UHD Graphics 630, "
                       "which is built into the processor. Dictation will run on the "
                       "processor.")
    # packages + the 110M model's four files, instead of small.en
    assert p.model == "110m" and p.download_text == "Setup will download about 300 MB."
    p = plan.server_plan(str(tmp_path), nvidia="GeForce GTX 750", problem="driver",
                         engine="parakeet", names=[])
    assert p.found.endswith("too old to use it. Dictation will run on the processor.")
    p = plan.server_plan(str(tmp_path), engine="parakeet",
                         names=[UHD, "NVIDIA GeForce RTX 2070 SUPER"])
    assert "NVIDIA's driver is not installed" in p.found and "lighter" not in p.found
    # An AMD card left out (--gpu cpu, or no Vulkan yet): the quick engine too.
    p = plan.server_plan(str(tmp_path), engine="parakeet", names=[UHD, RX])
    assert p.found == ("We found an AMD Radeon RX 6600 series card, but dictation is set "
                       "to run on the processor.")
    assert p.model == "110m"
    # Its model already here: only the packages.
    folder = tmp_path / "models" / "parakeet"
    folder.mkdir(parents=True)
    for spec, dest in plan.parakeet_files.missing("110m", str(tmp_path)):
        open(dest, "wb").close()
    assert plan.server_plan(str(tmp_path), engine="parakeet", names=[]).download == \
        plan.PACKAGES["server"]


def test_what_is_already_here_is_not_counted_again(tmp_path):
    root = tmp_path
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    (site / "faster_whisper").mkdir(parents=True)
    (site / "nvidia" / "cudnn").mkdir(parents=True)
    (root / ".env").write_text("WHISPER_MODEL_SIZE=small.en\nWHISPER_MODELS_DIR=models\n")
    p = plan.server_plan(str(root), device="cuda", nvidia="NVIDIA GeForce RTX 3060", names=[])
    assert p.model == "small.en" and p.download == 484 * 10 ** 6      # only the model
    snap = (root / "models" / "models--Systran--faster-whisper-small.en" / "snapshots" / "abc")
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    (snap / "config.json").write_text("{}")
    p = plan.server_plan(str(root), device="cuda", nvidia="NVIDIA GeForce RTX 3060", names=[])
    assert p.download == 0
    assert p.download_text == "Everything it needs is already on this PC."
    # A re-run keeps the model you chose, even on the processor.
    assert plan.server_plan(str(root), names=[]).model == "small.en"


def test_the_desktop_part_says_where_dictation_is_done(tmp_path):
    p = plan.desktop_plan(str(tmp_path))
    assert "Dictation itself is not on this PC yet" in p.found
    assert p.download_text == "Setup will download about 50 MB."
    assert plan.desktop_plan(str(tmp_path), remote="https://192.168.1.20:5001/v1").found == (
        "Dictation will be done by your other PC.")
    site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    (site / "faster_whisper").mkdir(parents=True)
    (site / "pystray").mkdir()
    p = plan.desktop_plan(str(tmp_path))
    assert p.found == "Dictation will be done by this PC." and p.download == 0


def test_the_command_prints_the_two_lines(tmp_path, capsys):
    assert plan.main(["server", "--root", str(tmp_path), "--device", "cuda",
                      "--nvidia", "GeForce RTX 3060"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["We found an NVIDIA GeForce RTX 3060. Dictation will run on it.",
                   "Setup will download about 3.2 GB."]
    plan.main(["server", "--root", str(tmp_path), "--nvidia", "GeForce GTX 750",
               "--problem", "driver", "--found-only"])
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_the_sentences_are_the_windows_wizards():
    try:
        sys.path.insert(0, os.path.join(ROOT, "client"))
        import settings_gui as sg
    except Exception:           # no Tk on this machine
        import pytest
        pytest.skip("the Settings window can't be imported here")
    assert plan.DOWNLOAD_BY_SETUP == sg.DOWNLOAD_BY_SETUP
    assert plan.NOTHING_TO_DOWNLOAD == sg.PENDING_READY
