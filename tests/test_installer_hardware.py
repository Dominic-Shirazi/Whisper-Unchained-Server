import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "installer"))

import hardware as hw  # noqa: E402


def gpu(name="RTX", total=8192, cap=7.5):
    return hw.Gpu(name, total, total - 500, cap)


def test_parse_two_gpus_and_missing_fields():
    out = "NVIDIA GeForce RTX 2070 SUPER, 8192, 4500, 7.5\nGTX 1060, 6144, 6000, [N/A]\n"
    g = hw.parse_nvidia_smi(out)
    assert [x.name for x in g] == ["NVIDIA GeForce RTX 2070 SUPER", "GTX 1060"]
    assert g[0].total_mb == 8192 and g[0].compute_cap == 7.5
    assert g[1].compute_cap is None


def test_detect_no_driver_means_no_gpus():
    def run(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    assert hw.detect_gpus(run=run) == []


def test_detect_falls_back_when_driver_lacks_compute_cap():
    calls = []

    def run(cmd, **k):
        calls.append(cmd[1])
        if "compute_cap" in cmd[1]:
            return SimpleNamespace(returncode=2, stdout="")
        return SimpleNamespace(returncode=0, stdout="Old GPU, 4096, 4000\n")
    g = hw.detect_gpus(run=run)
    assert len(calls) == 2 and g[0].name == "Old GPU" and g[0].compute_cap is None


def test_detect_sorts_biggest_first():
    def run(cmd, **k):
        return SimpleNamespace(returncode=0, stdout="Small, 2048, 2000, 8.6\nBig, 12288, 12000, 8.6\n")
    assert hw.detect_gpus(run=run)[0].name == "Big"


def test_turbo_on_8gb_card():
    r = hw.recommend([gpu(total=8192)])
    assert (r.model, r.device, r.compute_type) == ("large-v3-turbo", "cuda", "float16")


def test_small_on_3gb_card():
    r = hw.recommend([gpu(total=3072)])
    assert (r.model, r.device) == ("small.en", "cuda")
    assert hw.recommend([gpu(total=3072)], english_only=False).model == "small"


def test_cpu_when_no_or_tiny_gpu():
    for gpus in ([], [gpu(total=1024)]):
        r = hw.recommend(gpus)
        assert (r.device, r.compute_type) == ("cpu", "int8")


def test_old_card_uses_int8():
    r = hw.recommend([gpu(total=8192, cap=6.1)])
    assert r.compute_type == "int8" and r.device == "cuda"


def test_env_values():
    assert hw.recommend([]).env() == {"WHISPER_MODEL_SIZE": "small.en",
                                      "WHISPER_DEVICE": "cpu",
                                      "WHISPER_COMPUTE_TYPE": "int8"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows hardware probes")
def test_this_pc_detects_without_error():
    hw.detect_gpus()  # real nvidia-smi if present; must never raise


# --- no usable NVIDIA GPU (AMD / Intel / CPU only / broken driver) ----------------

def _smi(stdout="RTX 3060, 12288, 11000, 8.6\n"):
    return lambda cmd, **k: SimpleNamespace(returncode=0, stdout=stdout)


def test_listed_card_ctranslate2_cant_use_goes_to_cpu():
    # nvidia-smi sees the card, CTranslate2 doesn't (driver too old for CUDA 12):
    # recommending turbo on cuda would leave the server running turbo on the CPU.
    gpus = hw.detect_gpus(run=_smi(), cuda_count=lambda: 0)
    assert gpus[0].name == "RTX 3060" and gpus[0].cuda_ok is False
    r = hw.recommend(gpus)
    assert (r.model, r.device, r.compute_type) == ("small.en", "cpu", "int8")
    assert "driver" in r.reason


@pytest.mark.parametrize("count", [1, None])   # None: ctranslate2 not importable
def test_usable_or_unknown_cuda_keeps_gpu_recommendation(count):
    gpus = hw.detect_gpus(run=_smi(), cuda_count=lambda: count)
    assert hw.recommend(gpus).device == "cuda"


def test_no_nvidia_never_probes_ctranslate2():
    def run(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    def probe():
        raise AssertionError("not needed without an NVIDIA card")
    assert hw.detect_gpus(run=run, cuda_count=probe) == []


def test_cuda_device_count_never_raises(monkeypatch):
    import types
    fake = types.ModuleType("ctranslate2")
    fake.get_cuda_device_count = lambda: (_ for _ in ()).throw(RuntimeError("no driver"))
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    assert hw.cuda_device_count() is None


# --- graphics that are not NVIDIA (the whisper.cpp offer) -------------------------

# What Windows listed on a PC with an NVIDIA card and Intel built-in graphics
# (one line per screen output, so names repeat).
THIS_PC = ["NVIDIA GeForce RTX 2070 SUPER"] * 4 + ["Intel(R) UHD Graphics 630"] * 3


def test_real_graphics_drops_repeats_and_things_that_are_not_hardware():
    assert hw.real_graphics(THIS_PC) == ["NVIDIA GeForce RTX 2070 SUPER",
                                         "Intel(R) UHD Graphics 630"]
    fake = ["Microsoft Basic Display Adapter", "Microsoft Remote Display Adapter",
            "Parsec Virtual Display Adapter", "DisplayLink USB Device", "VMware SVGA 3D",
            "Microsoft Hyper-V Video", "Citrix Indirect Display Adapter", "", None]
    assert hw.real_graphics(fake) == []
    assert hw.real_graphics(None) == []
    assert hw.real_graphics(fake + ["AMD Radeon RX 6600"]) == ["AMD Radeon RX 6600"]


def test_other_graphics_leaves_nvidia_out():
    assert hw.other_graphics(THIS_PC) == ["Intel(R) UHD Graphics 630"]
    assert hw.other_graphics(["NVIDIA GeForce GTX 1050"]) == []
    assert hw.detect_other_graphics(adapters=lambda: ["AMD Radeon RX 580 Series"] * 2) == [
        "AMD Radeon RX 580 Series"]
    assert hw.detect_other_graphics(adapters=lambda: []) == []


def test_asking_windows_never_raises():
    names = hw.windows_display_adapters()
    assert isinstance(names, list) and all(isinstance(n, str) for n in names)


@pytest.mark.parametrize("name,separate", [
    ("AMD Radeon RX 6600 XT", True),
    ("Radeon RX 580 Series", True),
    ("Radeon (TM) RX 480 Graphics", True),
    ("AMD Radeon Pro W5500", True),
    ("AMD Radeon R9 200 Series", True),
    ("AMD Radeon HD 7900 Series", True),
    ("Radeon RX Vega 56", True),
    ("Intel(R) Arc(TM) A750 Graphics", True),
    ("Intel(R) Arc(TM) B580 Graphics", True),
    ("NVIDIA GeForce GTX 1050", True),
    ("Intel(R) UHD Graphics 630", False),
    ("Intel(R) Iris(R) Xe Graphics", False),
    ("Intel(R) Arc(TM) Graphics", False),            # built into the processor
    ("AMD Radeon(TM) Graphics", False),
    ("AMD Radeon(TM) 780M", False),
    ("AMD Radeon(TM) Vega 8 Graphics", False),
    ("AMD Radeon(TM) RX Vega 11 Graphics", False),
    ("Some Unknown Adapter", False),                 # not recognised: the careful answer
    ("", False), (None, False),
])
def test_is_separate_card_from_the_name(name, separate):
    assert hw.is_separate_card(name) is separate


def test_no_offer_with_an_nvidia_card_or_with_no_graphics():
    assert hw.offer_engine([gpu()], ["Intel(R) UHD Graphics 630"]) is None
    assert hw.offer_engine([gpu()], []) is None
    assert hw.offer_engine([], []) is None
    # An NVIDIA card whose driver is too old still counts: Settings offers the
    # other engine there, setup does not.
    broken = gpu()
    broken.cuda_ok = False
    assert hw.offer_engine([broken], ["AMD Radeon RX 6600"]) is None


def test_offer_for_a_separate_amd_card_is_the_accurate_model():
    offer = hw.offer_engine([], ["AMD Radeon(TM) Graphics", "AMD Radeon RX 6600"])
    assert offer.model == "large-v3-turbo-q5_0"
    assert offer.env() == {"WHISPER_ENGINE": "whisper.cpp",
                           "WHISPER_CPP_MODEL": "large-v3-turbo-q5_0",
                           "WHISPER_CPP_DEVICE": "auto"}
    assert offer.title == "Recommended: large-v3-turbo on your AMD Radeon RX 6600"
    assert "AMD Radeon RX 6600" in offer.reason


def test_built_in_graphics_alone_get_no_offer_during_setup():
    # Measured slower than the processor: setup keeps the usual engine there.
    assert hw.offer_engine([], ["Intel(R) UHD Graphics 630"]) is None
    assert hw.offer_engine([], ["AMD Radeon(TM) Graphics"]) is None


@pytest.mark.parametrize("names", [["AMD Radeon RX 6600"], ["Intel(R) Arc(TM) A750 Graphics"]])
def test_offer_wording_is_plain_and_honest(names):
    offer = hw.offer_engine([], names)
    text = offer.title + " " + offer.reason
    assert "only been tried on a few PCs" in offer.reason      # it is new: say so
    assert "—" not in text and "Vulkan" not in text and "whisper.cpp" not in text
    assert "signed" not in text.lower()
    # Explained in two sentences.
    assert offer.reason.count(". ") + 1 == 2


def test_offered_models_are_ones_the_downloader_knows():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "api"))
    import engine_files
    for names in (["AMD Radeon RX 6600"], ["Intel(R) Arc(TM) A750 Graphics"]):
        assert hw.offer_engine([], names).model in engine_files.MODELS
