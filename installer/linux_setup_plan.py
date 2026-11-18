"""What Linux setup says before it starts: what was found in this PC, and one
number for everything it will download.

Used by scripts/install_linux.sh and scripts/install_linux_desktop.sh (as a
command, see main()) and by the setup wizard. The words are the Windows
wizard's (hardware.found_text, "Setup will download about ..."), so the two
systems say the same thing about the same PC.

Only the standard library, hardware.py, api/engine_files.py and
client/model_catalog.py: it runs on the system's Python before there is a
venv.

    python3 installer/linux_setup_plan.py server --device cuda --nvidia "GeForce RTX 3060"
    python3 installer/linux_setup_plan.py desktop
"""
import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "api"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import engine_files  # noqa: E402
import hardware  # noqa: E402
import model_catalog  # noqa: E402

# What pip downloads, in bytes. Measured with `pip download` on Ubuntu 24.04
# (Python 3.12, x86-64) on 2026-11-18; they move a little with every release
# of the packages, and the sentence only ever says "about".
#   server:  requirements.txt with constraints.txt
#   nvidia:  nvidia-cublas-cu12 and nvidia-cudnn-cu12 (only with an NVIDIA card)
#   desktop: requirements-linux-desktop.txt (hotkey and tray)
PACKAGES = {"server": 144_000_000, "nvidia": 1_387_000_000, "desktop": 49_000_000}

# The same sentences as the Windows wizard (client/settings_gui.py:
# DOWNLOAD_BY_SETUP and PENDING_READY; a test keeps them equal).
DOWNLOAD_BY_SETUP = "Setup will download about %s."
NOTHING_TO_DOWNLOAD = "Everything it needs is already on this PC."
# Linux only: Windows installs a driver for every card it finds.
NO_DRIVER = ("We found %s, but NVIDIA's driver is not installed, so it can't be used "
             "yet. Dictation will run on the processor%s. Install the driver (on Ubuntu: "
             "sudo ubuntu-drivers install), restart, and run setup again to use the card.")
DEFAULT_MODEL = "large-v3-turbo"     # WHISPER_MODEL_SIZE in .env.example
CPU_MODEL = "small.en"               # a new install without a usable card


def _installed(root, package):
    """True when the venv already has this Python package."""
    return bool(glob.glob(os.path.join(root, ".venv", "lib", "python*", "site-packages",
                                       package)))


def env_value(root, key):
    """A setting from .env ("" when there is no file or no such line)."""
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8") as f:
            for line in f:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == key:
                    return value.strip().strip('"')
    except OSError:
        pass
    return ""


def model_bytes(root, name, models_dir):
    """Bytes still to download for a faster-whisper model (0: it is here, or
    it is a model of the person's own that setup doesn't fetch)."""
    info = model_catalog.BY_NAME.get(name)
    if info is None:
        return 0
    folder = engine_files.models_folder(models_dir, root)
    if model_catalog.is_downloaded(info, folder):
        return 0
    return info.size_mb * 10 ** 6


class Plan:
    """found: the statement about this PC. download: bytes setup will fetch.
    model: the speech model that goes with it."""

    def __init__(self, found, download, model):
        self.found, self.download, self.model = found, int(download), model

    @property
    def download_text(self):
        if self.download <= 0:
            return NOTHING_TO_DOWNLOAD
        return DOWNLOAD_BY_SETUP % engine_files.about_size_text(self.download)

    def lines(self):
        return [self.found, self.download_text]


def server_plan(root=ROOT, device="cpu", nvidia="", problem="", engine="",
                names=None, fresh=None):
    """The server's plan.

    device:  "cuda" (dictation runs on the NVIDIA card) or "cpu"
    nvidia:  the NVIDIA card's name as nvidia-smi gives it ("" = none found)
    problem: why a card that was found is not used: "", "driver" or "memory"
    engine:  "whisper.cpp" when the second engine will run on an AMD or Intel card
    names:   the PC's graphics names (default: asked of the system)
    fresh:   no .env yet (default: looked up); a new install without a usable
             card starts on the smaller model"""
    if names is None:
        names = hardware.display_adapters()
    if fresh is None:
        fresh = not os.path.isfile(os.path.join(root, ".env"))
    models_dir = env_value(root, "WHISPER_MODELS_DIR") or None
    card, built_in = hardware.pick_cards(names)
    packages = 0 if _installed(root, "faster_whisper") else PACKAGES["server"]

    if engine == "whisper.cpp":
        other = [n for n in hardware.other_graphics(names) if hardware.is_separate_card(n)]
        model = env_value(root, "WHISPER_CPP_MODEL") or engine_files.DEFAULT_MODEL
        todo = engine_files.missing(model, root, models_dir)
        return Plan(hardware.found_text(other[0] if other else card, built_in),
                    packages + engine_files.total_size(todo), model)

    nvidia = hardware.nvidia_name(nvidia) if nvidia else ""
    model = env_value(root, "WHISPER_MODEL_SIZE") or DEFAULT_MODEL
    if device == "cuda":
        libs = 0 if _installed(root, os.path.join("nvidia", "cudnn")) else PACKAGES["nvidia"]
        return Plan(hardware.found_text(nvidia or card, built_in),
                    packages + libs + model_bytes(root, model, models_dir), model)
    if fresh:
        model = CPU_MODEL
    info = model_catalog.BY_NAME.get(model)
    smaller = info is None or info.size_mb < 1000
    # A card that can't be used still gets named, with the reason.
    if not nvidia and "nvidia" in card.lower():
        # The card is in the PC but nvidia-smi isn't: no driver for it.
        found = NO_DRIVER % (hardware.with_article(card),
                             ", in a lighter version" if smaller else "")
    else:
        found = hardware.found_text(nvidia, built_in, False, smaller, problem)
    return Plan(found, packages + model_bytes(root, model, models_dir), model)


def desktop_plan(root=ROOT, remote=""):
    """The hotkey and tray's plan: where dictation is done, and the download."""
    if remote:
        found = "Dictation will be done by your other PC."
    elif _installed(root, "faster_whisper") or env_value(root, "WHISPER_ENGINE"):
        found = "Dictation will be done by this PC."
    else:
        found = ("The hotkey and tray icon will be set up. Dictation itself is not on "
                 "this PC yet: run scripts/install_linux.sh, or run this again with "
                 "--remote for another PC of yours.")
    return Plan(found, 0 if _installed(root, "pystray") else PACKAGES["desktop"], "")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("what", choices=("server", "desktop"))
    p.add_argument("--root", default=ROOT)
    p.add_argument("--device", default="cpu", choices=("cuda", "cpu"))
    p.add_argument("--nvidia", default="")
    p.add_argument("--problem", default="", choices=("", "driver", "memory"))
    p.add_argument("--engine", default="")
    p.add_argument("--remote", default="")
    p.add_argument("--found-only", action="store_true",
                   help="print only the statement about this PC")
    a = p.parse_args(argv)
    if a.what == "desktop":
        plan = desktop_plan(a.root, a.remote)
    else:
        plan = server_plan(a.root, a.device, a.nvidia, a.problem, a.engine)
    print("\n".join(plan.lines()[:1] if a.found_only else plan.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
