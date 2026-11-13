"""Device selection on PCs without a working NVIDIA GPU (api/device_select.py).

No real model ever loads: the WhisperModel factory is a fake, and the CUDA
device count / CUDA failures are simulated. The crash marker lives in tmp_path.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api import device_select as ds  # noqa: E402


class FakeModel:
    def __init__(self, size, device, compute_type, **kw):
        self.size, self.device, self.compute_type, self.kw = size, device, compute_type, kw
        self.transcribed = 0

    def transcribe(self, audio, **kw):
        self.transcribed += 1
        return iter([]), None


class Factory:
    """Records every load; can fail CUDA at load time or at first decode
    (missing cuBLAS/cuDNN only shows up when the GPU is first used)."""

    def __init__(self, cuda_load_error=None, cuda_run_error=None, cpu_error=None):
        self.calls = []
        self.cuda_load_error, self.cuda_run_error = cuda_load_error, cuda_run_error
        self.cpu_error = cpu_error

    def __call__(self, size, device, compute_type, **kw):
        self.calls.append((device, compute_type, kw))
        if device == "cpu" and compute_type in ds._GPU_ONLY_TYPES:
            raise ValueError("Requested %s compute type, but the target device or "
                             "backend do not support efficient %s computation." % (
                                 compute_type, compute_type))
        if device == "cuda" and self.cuda_load_error:
            raise self.cuda_load_error
        if device == "cpu" and self.cpu_error:
            raise self.cpu_error
        m = FakeModel(size, device, compute_type, **kw)
        if device == "cuda" and self.cuda_run_error:
            err = self.cuda_run_error

            def boom(*a, **k):
                raise err
            m.transcribe = boom
        return m


@pytest.fixture
def load(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "supported_compute_types",
                        lambda d: {"int8", "int8_float32", "float32", "int16"})
    marker = str(tmp_path / "probe")
    logs = []

    def run(factory, size="large-v3-turbo", device="auto", compute="", gpus=0, **kw):
        kw.setdefault("threads_env", "4")
        m, info = ds.load_whisper(factory, size, device, compute, log=logs.append,
                                  marker=marker, device_count=lambda: gpus, **kw)
        return m, info
    run.marker, run.logs = marker, logs
    return run


# --- no NVIDIA GPU (AMD / Intel / CPU only) ---------------------------------------

@pytest.mark.parametrize("device", ["auto", "cuda", "cpu", "", "rocm"])
def test_no_gpu_any_setting_loads_on_cpu_int8(load, device):
    f = Factory()
    m, info = load(f, device=device, gpus=0)
    assert m is not None and m.device == "cpu" and m.compute_type == "int8"
    assert [c[0] for c in f.calls] == ["cpu"]            # CUDA never attempted
    assert info["device"] == "cpu" and info["compute_type"] == "int8"
    assert m.kw["cpu_threads"] == 4


def test_explicit_cuda_without_gpu_says_why(load):
    _, info = load(Factory(), device="cuda", gpus=0)
    assert "no NVIDIA GPU" in info["fallback"]
    assert any("PROCESSOR" in l for l in load.logs)


def test_auto_without_gpu_is_not_a_fallback(load):
    # "auto" on a CPU-only PC is the expected outcome, not a problem to flag.
    _, info = load(Factory(), device="auto", gpus=0)
    assert info["fallback"] is None and info["device"] == "cpu"


@pytest.mark.parametrize("compute", ["float16", "int8_float16", "bfloat16", "FLOAT16"])
def test_gpu_compute_type_left_in_env_is_swapped_on_cpu(load, compute):
    # Was: WHISPER_COMPUTE_TYPE=float16 + device cpu -> ValueError -> no model.
    m, info = load(Factory(), device="cpu", compute=compute)
    assert m.compute_type == "int8" and info["error"] is None


def test_cpu_compute_type_choice_is_kept(load):
    m, _ = load(Factory(), device="cpu", compute="float32")
    assert m.compute_type == "float32"


def test_no_model_set_defaults_small_on_cpu_turbo_on_gpu(load):
    m, info = load(Factory(), size="", gpus=0)
    assert m.size == "small.en" and info["model"] == "small.en"
    m, info = load(Factory(), size="", gpus=1)
    assert m.size == "large-v3-turbo" and m.device == "cuda"


def test_large_model_on_cpu_is_kept_but_warned(load):
    m, _ = load(Factory(), size="large-v3-turbo", gpus=0)
    assert m.size == "large-v3-turbo"
    assert any("slow on a processor" in l for l in load.logs)


def test_cpu_load_failure_leaves_no_model_and_reports(load):
    m, info = load(Factory(cpu_error=OSError("model files missing")), gpus=0)
    assert m is None and "model files missing" in info["error"]


# --- NVIDIA GPU present -------------------------------------------------------------

def test_working_gpu_unchanged_float16_and_smoke_tested(load):
    f = Factory()
    m, info = load(f, device="cuda", gpus=2)
    assert (m.device, m.compute_type) == ("cuda", "float16")
    assert m.transcribed == 1                    # proved cuBLAS/cuDNN actually work
    assert info["fallback"] is None and len(f.calls) == 1
    assert "cpu_threads" not in f.calls[0][2]
    assert not os.path.exists(load.marker)


def test_gpu_keeps_explicit_compute_type(load):
    m, _ = load(Factory(), device="auto", compute="int8_float16", gpus=1)
    assert (m.device, m.compute_type) == ("cuda", "int8_float16")


def test_cuda_load_error_falls_back_to_cpu(load):
    f = Factory(cuda_load_error=RuntimeError("CUDA failed with error no CUDA-capable device"))
    m, info = load(f, device="cuda", compute="float16", gpus=1)
    assert (m.device, m.compute_type) == ("cpu", "int8")
    assert [c[0] for c in f.calls] == ["cuda", "cpu"]
    assert "no CUDA-capable device" in info["fallback"]
    assert not os.path.exists(load.marker)       # it raised, so retry next start


def test_missing_cudnn_at_first_use_falls_back_to_cpu(load):
    # The model LOADS on the GPU but the first decode can't find the DLLs.
    f = Factory(cuda_run_error=RuntimeError(
        "Library cublas64_12.dll is not found or cannot be loaded"))
    m, info = load(f, device="auto", gpus=1)
    assert m.device == "cpu" and info["device"] == "cpu"
    assert "cublas64_12.dll" in info["fallback"]


def test_gpu_attempt_that_crashed_the_process_is_not_retried(load):
    # A previous start died mid-attempt (marker left behind): go straight to CPU.
    open(load.marker, "w").close()
    f = Factory()
    m, info = load(f, device="auto", gpus=1)
    assert m.device == "cpu" and [c[0] for c in f.calls] == ["cpu"]
    assert "crashed" in info["fallback"]


def test_old_crash_marker_is_retried(load):
    open(load.marker, "w").close()
    old = os.path.getmtime(load.marker) - ds.CRASH_RETRY_AFTER - 10
    os.utime(load.marker, (old, old))
    m, _ = load(Factory(), device="auto", gpus=1)
    assert m.device == "cuda" and not os.path.exists(load.marker)


def test_marker_written_during_gpu_attempt(load):
    seen = []

    class Spy(Factory):
        def __call__(self, *a, **kw):
            seen.append(os.path.exists(load.marker))
            return super().__call__(*a, **kw)
    load(Spy(), device="cuda", gpus=1)
    assert seen == [True]


# --- helpers ------------------------------------------------------------------------

def test_cuda_device_count_never_raises(monkeypatch):
    import types
    fake = types.ModuleType("ctranslate2")

    def boom():
        raise RuntimeError("driver too old")
    fake.get_cuda_device_count = boom
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    assert ds.cuda_device_count() == 0


def test_cpu_compute_type_when_cpu_lacks_int8():
    assert ds.compute_type_for("cpu", "", supported={"float32"}) == "auto"
    assert ds.compute_type_for("cpu", "float16", supported={"int8", "float32"}) == "int8"
    assert ds.compute_type_for("cuda", "") == "float16"


def test_cpu_threads(monkeypatch):
    assert ds.cpu_threads("3") == 3
    import psutil
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 16)
    assert ds.cpu_threads(None) == 8             # capped
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 2)
    assert ds.cpu_threads("junk") == 2
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: None)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert ds.cpu_threads("0") == 4               # 8 logical ~ 4 physical


def test_slow_on_cpu():
    assert ds.slow_on_cpu("large-v3-turbo") and ds.slow_on_cpu("distil-large-v3.5")
    assert ds.slow_on_cpu("C:/models/faster-whisper-medium.en/")
    assert not ds.slow_on_cpu("small.en") and not ds.slow_on_cpu("base")
