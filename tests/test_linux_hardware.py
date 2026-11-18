"""Which graphics a Linux PC has (installer/linux_hardware.py), and that the
shared hardware module (installer/hardware.py) can be asked on Linux.

    .venv/bin/python -m pytest tests/test_linux_hardware.py -q

No lspci, no /sys and no graphics card are needed: the output is canned.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
import hardware  # noqa: E402
import linux_hardware as lh  # noqa: E402

# `lspci -mm` on Worker3 (i5-9600K, two RTX 2070 Supers), 2026-11-18.
WORKER3 = '''00:00.0 "Host bridge" "Intel Corporation" "8th/9th Gen Core Processor Host Bridge" -r0d -p00 "ASUSTeK Computer Inc." "Device 8694"
00:02.0 "VGA compatible controller" "Intel Corporation" "CoffeeLake-S GT2 [UHD Graphics 630]" -p00 "ASUSTeK Computer Inc." "CoffeeLake-S GT2 [UHD Graphics 630]"
01:00.0 "VGA compatible controller" "NVIDIA Corporation" "TU104 [GeForce RTX 2070 SUPER]" -ra1 -p00 "eVga.com. Corp." "TU104 [GeForce RTX 2070 SUPER]"
01:00.1 "Audio device" "NVIDIA Corporation" "TU104 HD Audio Controller" -ra1 -p00 "eVga.com. Corp." "TU104 HD Audio Controller"
02:00.0 "VGA compatible controller" "NVIDIA Corporation" "TU104 [GeForce RTX 2070 SUPER]" -ra1 -p00 "eVga.com. Corp." "TU104 [GeForce RTX 2070 SUPER]"
'''


class Ran:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_lspci_names_read_the_way_people_say_them():
    assert lh.parse_lspci(WORKER3) == [
        "Intel UHD Graphics 630", "NVIDIA GeForce RTX 2070 SUPER",
        "NVIDIA GeForce RTX 2070 SUPER"]
    amd = "Advanced Micro Devices, Inc. [AMD/ATI]"
    # lspci lists a chip's whole family: say so instead of guessing one.
    assert lh.adapter_name(amd, "Navi 23 [Radeon RX 6600/6600 XT/6600M]") == (
        "AMD Radeon RX 6600 series card")
    assert lh.adapter_name(amd, "Rembrandt [Radeon 680M]") == "AMD Radeon 680M"
    assert lh.adapter_name("Intel Corporation", "DG2 [Arc A770]") == "Intel Arc A770"
    assert lh.adapter_name("Intel Corporation", "Device 4680") == "Intel Device 4680"
    assert lh.parse_lspci("") == [] and lh.parse_lspci("garbage\n") == []


def test_the_names_sort_into_cards_and_built_in_graphics():
    assert hardware.pick_cards(lh.parse_lspci(WORKER3)) == (
        "NVIDIA GeForce RTX 2070 SUPER", "Intel UHD Graphics 630")
    assert hardware.is_separate_card("AMD Radeon RX 6600 series card")
    assert hardware.is_separate_card("Intel Arc A770")
    assert not hardware.is_separate_card("AMD Radeon 680M")
    assert not hardware.is_separate_card("Intel UHD Graphics 630")
    assert hardware.found_text("AMD Radeon RX 6600 series card") == (
        "We found an AMD Radeon RX 6600 series card. Dictation will run on it.")


def test_only_a_separate_amd_or_intel_card_gets_the_second_engine():
    offer = hardware.offer_engine([], ["Intel UHD Graphics 630",
                                       "AMD Radeon RX 6600 series card"])
    assert offer and offer.env()["WHISPER_ENGINE"] == "whisper.cpp"
    assert hardware.offer_engine([], ["Intel UHD Graphics 630"]) is None
    assert hardware.offer_engine(["an nvidia card"], ["Intel Arc A770"]) is None


def test_without_lspci_the_maker_is_read_from_sys(tmp_path):
    for n, (cls, vendor) in enumerate((("0x030000", "0x8086"), ("0x030200", "0x1002"),
                                       ("0x040300", "0x10de"), ("0x030000", "0x1234"))):
        d = tmp_path / ("0000-0%d-00.0" % n)      # no colons: Windows file names
        d.mkdir()
        (d / "class").write_text(cls + "\n")
        (d / "vendor").write_text(vendor + "\n")
    from_sys = lambda: lh._from_sys(str(tmp_path / "*"))  # noqa: E731

    def no_lspci(*a, **k):
        raise FileNotFoundError("lspci")
    assert lh.display_adapters(no_lspci, from_sys) == ["Intel graphics", "AMD graphics"]
    assert lh.display_adapters(lambda *a, **k: Ran("", 1), from_sys) == [
        "Intel graphics", "AMD graphics"]
    # A maker alone is never taken for a separate card (except NVIDIA, which always is).
    assert not hardware.is_separate_card("AMD graphics")
    assert hardware.is_separate_card("NVIDIA graphics")


def test_display_adapters_never_raises():
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("lspci", 10)

    def broken():
        raise RuntimeError("no /sys")
    assert lh.display_adapters(slow, broken) == []
    assert lh.display_adapters(lambda *a, **k: Ran(WORKER3), broken)[0] == (
        "Intel UHD Graphics 630")


def test_nvidia_smi_is_asked_without_a_windows_only_flag():
    """creationflags is an error off Windows: detect_gpus() used to raise there."""
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return Ran("NVIDIA GeForce RTX 2070 SUPER, 8192, 600, 7.5\n")
    gpus = hardware.detect_gpus(run, cuda_count=lambda: 1)
    assert [g.name for g in gpus] == ["NVIDIA GeForce RTX 2070 SUPER"]
    assert ("creationflags" in seen) == (os.name == "nt")
