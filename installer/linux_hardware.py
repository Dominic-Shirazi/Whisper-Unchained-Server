"""Which graphics hardware a Linux PC has, by name (for hardware.py).

Windows is asked for its display adapters; Linux has no such list, so this
reads the PCI devices: `lspci` for names ("NVIDIA GeForce RTX 2070 SUPER",
"Intel UHD Graphics 630"), or, without lspci, /sys for the maker alone.
Nothing is started on the graphics card and no root is needed.

The names are shaped the way hardware.is_separate_card() and
hardware.found_text() expect them: maker first, then the model.
"""
import glob
import os
import re
import subprocess

# PCI class 03: display controllers (VGA, 3D, other).
_DISPLAY_CLASSES = ("vga compatible controller", "3d controller", "display controller")
_MAKERS = (("nvidia", "NVIDIA"), ("advanced micro devices", "AMD"), ("amd", "AMD"),
           ("ati ", "AMD"), ("intel", "Intel"))
_VENDOR_IDS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def _maker(vendor):
    low = (vendor or "").lower() + " "
    for word, name in _MAKERS:
        if word in low:
            return name
    return " ".join((vendor or "").replace("Corporation", "").replace(", Inc.", "").split())


def adapter_name(vendor, device):
    """One graphics name from lspci's vendor and device strings.

    ("NVIDIA Corporation", "TU104 [GeForce RTX 2070 SUPER]") -> "NVIDIA GeForce RTX 2070 SUPER"
    ("Intel Corporation", "CoffeeLake-S GT2 [UHD Graphics 630]") -> "Intel UHD Graphics 630"
    ("Advanced Micro Devices, Inc. [AMD/ATI]", "Navi 23 [Radeon RX 6600/6600 XT/6600M]")
        -> "AMD Radeon RX 6600 series card" (lspci can't tell those apart)"""
    maker = _maker(vendor)
    inside = re.findall(r"\[([^\]]+)\]", device or "")
    model = inside[-1] if inside else (device or "")
    if "/" in model:
        model = model.split("/")[0].strip() + " series card"
    model = " ".join(model.split())
    if model.lower().startswith(maker.lower()):
        return model
    return ("%s %s" % (maker, model)).strip()


def parse_lspci(output):
    """Graphics names from `lspci -mm` output, in the order listed."""
    names = []
    for line in (output or "").splitlines():
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) >= 3 and parts[0].lower() in _DISPLAY_CLASSES:
            names.append(adapter_name(parts[1], parts[2]))
    return names


def _from_sys(pattern="/sys/bus/pci/devices/*"):
    """Without lspci: the maker of each display device, from /sys."""
    names = []
    for dev in sorted(glob.glob(pattern)):
        try:
            with open(os.path.join(dev, "class")) as f:
                if not f.read().strip().startswith("0x03"):
                    continue
            with open(os.path.join(dev, "vendor")) as f:
                maker = _VENDOR_IDS.get(f.read().strip().lower())
        except OSError:
            continue
        if maker:      # NVIDIA only makes separate cards; the others can't be told apart
            names.append("NVIDIA graphics card" if maker == "NVIDIA" else "%s graphics" % maker)
    return names


def display_adapters(run=subprocess.run, from_sys=_from_sys):
    """Names of this PC's graphics hardware, e.g. ["Intel UHD Graphics 630",
    "NVIDIA GeForce RTX 2070 SUPER"]. [] when it can't be told. Never raises."""
    try:
        r = run(["lspci", "-mm"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            names = parse_lspci(r.stdout)
            if names:
                return names
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        return from_sys()
    except Exception:
        return []
