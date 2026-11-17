"""Detect the GPU and recommend a Whisper model for the setup wizard.

Recommendations come from the measured numbers in client/model_catalog.py
(large-v3-turbo used 2.1 GB of VRAM on an RTX 2070 Super) plus headroom for
the desktop and other apps sharing the card.
"""
import re
import subprocess

# A card needs this much VRAM in total before turbo is the default: 2.1 GB for
# the model, the rest for Windows, the browser, and maybe an Ollama model.
TURBO_MIN_MB = 4000
# Below this, even small runs better on the CPU than squeezed onto the card.
SMALL_GPU_MIN_MB = 1800


class Gpu:
    def __init__(self, name, total_mb, free_mb, compute_cap, cuda_ok=True):
        self.name, self.total_mb, self.free_mb = name, total_mb, free_mb
        self.compute_cap = compute_cap  # e.g. 7.5 for an RTX 2070; None if unknown
        # False when nvidia-smi lists the card but CTranslate2 can't use it
        # (driver too old for CUDA 12, card disabled): Whisper would end up on
        # the processor anyway, so size the model for that.
        self.cuda_ok = cuda_ok

    @property
    def fast_fp16(self):
        """float16 needs compute capability 7.0+ (RTX 20 / GTX 16 and newer).
        Older cards use int8, which CTranslate2 runs fine from 6.1."""
        return self.compute_cap is None or self.compute_cap >= 7.0

    def __repr__(self):
        return "Gpu(%r, %d MB)" % (self.name, self.total_mb)


def parse_nvidia_smi(output):
    """Parse `nvidia-smi --query-gpu=name,memory.total,memory.free,compute_cap
    --format=csv,noheader,nounits`. Unknown fields become None/0."""
    gpus = []
    for line in (output or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue
        def num(s, cast=int):
            try:
                return cast(s)
            except (TypeError, ValueError):
                return None
        cap = num(parts[3], float) if len(parts) > 3 else None
        gpus.append(Gpu(parts[0], num(parts[1]) or 0, num(parts[2]) or 0, cap))
    return gpus


def cuda_device_count():
    """GPUs CTranslate2 (faster-whisper's engine) can actually use; None if it
    can't be asked (not installed yet). Never raises."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return None


def detect_gpus(run=subprocess.run, cuda_count=cuda_device_count):
    """NVIDIA GPUs on this PC, biggest first. [] when there's no NVIDIA driver
    (AMD, Intel or no graphics card: faster-whisper can only use NVIDIA)."""
    gpus = _nvidia_smi_gpus(run)
    if gpus and cuda_count() == 0:
        for g in gpus:
            g.cuda_ok = False
    return gpus


def _nvidia_smi_gpus(run):
    query = "name,memory.total,memory.free,compute_cap"
    for fields in (query, "name,memory.total,memory.free"):  # old drivers lack compute_cap
        try:
            r = run(["nvidia-smi", "--query-gpu=" + fields,
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=0x08000000)  # no console window
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode == 0:
            return sorted(parse_nvidia_smi(r.stdout), key=lambda g: -g.total_mb)
    return []


class Recommendation:
    def __init__(self, model, device, compute_type, reason, gpu=None):
        self.model, self.device, self.compute_type = model, device, compute_type
        self.reason, self.gpu = reason, gpu

    def env(self):
        """The .env values this recommendation sets."""
        return {"WHISPER_MODEL_SIZE": self.model, "WHISPER_DEVICE": self.device,
                "WHISPER_COMPUTE_TYPE": self.compute_type}


def recommend(gpus, english_only=True):
    small = "small.en" if english_only else "small"
    if not gpus:
        return Recommendation(
            small, "cpu", "int8",
            "No NVIDIA graphics card found, so transcription runs on the processor. "
            "%s is the best balance there: a few seconds per sentence." % small)
    g = gpus[0]
    if not g.cuda_ok:
        return Recommendation(
            small, "cpu", "int8",
            "Your %s can't run Whisper right now (its driver is probably too old for "
            "CUDA 12; updating the NVIDIA driver and running setup again fixes that). "
            "Until then transcription runs on the processor, where %s is the best "
            "balance." % (g.name, small), g)
    ctype = "float16" if g.fast_fp16 else "int8"
    gb = g.total_mb / 1024
    if g.total_mb >= TURBO_MIN_MB:
        return Recommendation(
            "large-v3-turbo", "cuda", ctype,
            "Your %s has %.0f GB of video memory: plenty for large-v3-turbo, the most "
            "accurate fast model (about 2 GB, under a second per sentence)." % (g.name, gb), g)
    if g.total_mb >= SMALL_GPU_MIN_MB:
        return Recommendation(
            small, "cuda", ctype,
            "Your %s has %.1f GB of video memory, too little for the large models "
            "alongside Windows. %s fits comfortably and is still fast." % (g.name, gb, small), g)
    return Recommendation(
        small, "cpu", "int8",
        "Your %s has only %.1f GB of video memory, so the processor will do a better "
        "job. %s is the best balance there." % (g.name, gb, small), g)


# --- Graphics that are not NVIDIA (for the whisper.cpp engine) -----------------
# faster-whisper only speeds up on NVIDIA cards. The second engine, whisper.cpp,
# can also use AMD and Intel graphics, so setup needs to know whether this PC
# has any. Windows is asked for its display adapters by name; nothing is started.

# Adapters that are not real graphics hardware: Windows' fallback driver,
# remote-desktop and virtual-screen drivers, virtual machines.
_NOT_HARDWARE = ("microsoft basic", "remote", "virtual", "mirror", "parsec", "displaylink",
                 "citrix", "vmware", "hyper-v", "indirect", "idd", "spacedesk", "oculus",
                 "meta ", "dameware", "teamviewer", "splashtop", "rdp")


def windows_display_adapters():
    """The names Windows gives its display adapters, e.g. "AMD Radeon RX 6600".
    [] when it can't be asked (not Windows). Never raises."""
    try:
        import ctypes
        from ctypes import wintypes

        class DisplayDevice(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                        ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                        ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

        names, i = [], 0
        while i < 64:
            dev = DisplayDevice()
            dev.cb = ctypes.sizeof(dev)
            if not ctypes.windll.user32.EnumDisplayDevicesW(None, i, ctypes.byref(dev), 0):
                break
            names.append(dev.DeviceString)
            i += 1
        return names
    except Exception:
        return []


def real_graphics(names):
    """From a list of display adapter names: the real graphics hardware, each
    once, in the order given."""
    found = []
    for name in names or []:
        name = " ".join(str(name or "").split())
        low = name.lower()
        if not name or any(word in low for word in _NOT_HARDWARE):
            continue
        if name not in found:
            found.append(name)
    return found


def other_graphics(names):
    """The real graphics hardware that is not NVIDIA (AMD, Intel, ...)."""
    return [n for n in real_graphics(names) if "nvidia" not in n.lower()]


def is_separate_card(name):
    """A guess from the name alone: is this a separate graphics card (worth a
    large model) rather than graphics built into the processor? Built-in
    graphics are often no faster than the processor itself, so anything not
    recognised counts as built-in. NVIDIA is always a separate card."""
    low = " ".join(str(name or "").lower().replace("(tm)", "").replace("(r)", "").split())
    if "nvidia" in low:
        return True
    if "intel" in low:      # Arc A380, A750, B580 ...: cards. "Arc Graphics", UHD, Iris: built in.
        return bool(re.search(r"\barc (pro )?[ab]\d{3}", low))
    if "amd" in low or "radeon" in low or "firepro" in low:
        if re.search(r"vega (?!56|64|frontier)|\d{3}m\b|radeon graphics", low):   # Vega 8, 780M, "Radeon Graphics"
            return False
        return bool(re.search(r"\brx ?\d|vega (56|64|frontier)|\bpro\b|firepro|\br[579] \d{3}|\bhd ?\d{4}", low))
    return False


def detect_other_graphics(adapters=windows_display_adapters):
    """AMD or Intel graphics on this PC (names), [] when there is none."""
    return other_graphics(adapters())


class EngineOffer:
    """What setup offers a PC with no NVIDIA card but other graphics: the
    whisper.cpp engine, which can run on them."""

    def __init__(self, model, title, reason, names):
        self.model, self.title, self.reason, self.names = model, title, reason, names

    def env(self):
        return {"WHISPER_ENGINE": "whisper.cpp", "WHISPER_CPP_MODEL": self.model,
                "WHISPER_CPP_DEVICE": "auto"}


def offer_engine(nvidia_gpus, other_names):
    """The whisper.cpp offer for this PC, or None when it does not apply (an
    NVIDIA card is present: Settings offers it there; or no graphics at all).

    Only a separate AMD or Intel card gets the offer (with the accurate
    model). Measured on an i5-9600K with built-in Intel UHD 630: the large
    model took over 30 s for a 13 s clip on either, and the built-in chip was
    slower than the processor even with the small model."""
    if nvidia_gpus or not other_names:
        return None
    cards = [n for n in other_names if is_separate_card(n)]
    if cards:
        return EngineOffer(
            "large-v3-turbo-q5_0", "Recommended: large-v3-turbo on your %s" % cards[0],
            "No NVIDIA graphics card found, but this PC has other graphics (%s), so setup "
            "will use a second speech engine that can run on them. That engine is new and "
            "has only been tried on a few PCs so far." % cards[0], other_names)
    # Built-in graphics only: not offered during setup. The usual engine's
    # small English model on the processor is the proven path there, and the
    # built-in chip measured slower than the processor. Settings still offers
    # the second engine to anyone who wants to try it.
    return None
