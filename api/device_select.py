"""Pick where Whisper runs (NVIDIA GPU or processor) and load it there safely.

Why this exists: faster-whisper's engine (CTranslate2) only accelerates NVIDIA
GPUs through CUDA. On a PC with an AMD or Intel graphics card, or none, the
server used to start with WHISPER_DEVICE=cuda (or auto + float16) and fail the
load outright -- CTranslate2 refuses float16 on the CPU -- leaving the server
with no model at all. And on an NVIDIA PC whose CUDA libraries are broken,
the model often LOADS fine and only fails on the first dictation (cuBLAS and
cuDNN are loaded lazily), so a successful load proved nothing.

What load_whisper() does instead:
- "auto" and "cuda" try the GPU only when CTranslate2 actually sees one, then
  run one tiny decode to prove the CUDA libraries work;
- anything that goes wrong there falls back to the processor with int8 and a
  thread count sized to the PC, and says why (log + /health);
- a GPU attempt that killed the whole process last time (some cuDNN failures
  abort instead of raising) is not retried for a while, so the service can't
  crash-loop: a marker file is written before the attempt and removed after it.

Kept free of faster_whisper imports so tests can drive it with fakes.
"""
import os
import tempfile
import time

# float16-style types need a GPU; CTranslate2 raises ValueError for them on CPU.
_GPU_ONLY_TYPES = {"float16", "int8_float16", "bfloat16", "int8_bfloat16"}
# A start that died mid-GPU-attempt is not retried for this long (seconds).
CRASH_RETRY_AFTER = 6 * 3600
DEFAULT_CRASH_MARKER = os.path.join(tempfile.gettempdir(), "whisper_unchained_cuda_probe")
# Without a GPU, the large models take many seconds per sentence (measured:
# ~11 s to encode one clip with large-v3-turbo int8 on a 6-core CPU).
CPU_DEFAULT_MODEL = "small.en"
GPU_DEFAULT_MODEL = "large-v3-turbo"
SLOW_ON_CPU = ("medium", "large", "distil-large", "turbo")


def cuda_device_count():
    """NVIDIA GPUs CTranslate2 can use. 0 on AMD/Intel/no GPU, a missing or too
    old driver, or anything else going wrong: never raises."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def supported_compute_types(device):
    try:
        import ctranslate2
        return set(ctranslate2.get_supported_compute_types(device))
    except Exception:
        return set()


def compute_type_for(device, requested, supported=None):
    """The compute type to load with. CPU never gets a GPU-only type (a GPU
    PC's float16 left in .env after switching Settings to cpu used to break the
    load); an empty setting means float16 on CUDA, int8 on CPU."""
    requested = (requested or "").strip().lower()
    if device == "cuda":
        return requested or "float16"
    if requested and requested not in _GPU_ONLY_TYPES:
        return requested
    supported = supported_compute_types("cpu") if supported is None else supported
    if not supported or "int8" in supported:
        return "int8"
    return "auto"  # CTranslate2 picks the fastest type this CPU supports


def cpu_threads(env_value=None):
    """Threads for CPU inference. WHISPER_CPU_THREADS wins; otherwise one per
    physical core (hyperthreads don't help matrix maths), at most 8 so the PC
    stays responsive. CTranslate2's own default is a fixed 4."""
    try:
        n = int(env_value) if env_value not in (None, "") else 0
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    physical = None
    try:
        import psutil
        physical = psutil.cpu_count(logical=False)
    except Exception:
        pass
    if not physical:
        logical = os.cpu_count() or 4
        physical = logical // 2 if logical >= 4 else logical
    return max(1, min(8, physical))


def default_model(has_gpu):
    return GPU_DEFAULT_MODEL if has_gpu else CPU_DEFAULT_MODEL


def slow_on_cpu(model_size):
    name = os.path.basename(str(model_size or "").rstrip("/\\")).lower()
    return any(k in name for k in SLOW_ON_CPU)


def _smoke_test(model):
    """One tiny decode. Proves cuBLAS/cuDNN load and work, which only happens
    on first use, not at model load. Also warms the GPU for the first dictation."""
    import numpy as np
    segments, _ = model.transcribe(
        np.zeros(16000, dtype=np.float32), language="en", beam_size=1,
        vad_filter=False, without_timestamps=True, max_new_tokens=1,
        condition_on_previous_text=False)
    list(segments)


def _recent_crash(marker, now):
    try:
        return now - os.path.getmtime(marker) < CRASH_RETRY_AFTER
    except OSError:
        return False


def _touch(marker):
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _remove(marker):
    try:
        os.remove(marker)
    except OSError:
        pass


def _short(e):
    text = " ".join(str(e).split()) or type(e).__name__
    return text if len(text) <= 300 else text[:297] + "..."


def load_whisper(factory, model_size, device_pref, compute_pref, download_root=None,
                 threads_env=None, log=print, marker=DEFAULT_CRASH_MARKER,
                 device_count=cuda_device_count, now=time.time):
    """Load Whisper on the best working device.

    factory: faster_whisper.WhisperModel (or a fake). Returns (model, info):
    model is None only if the CPU load failed too. info = {device, compute_type,
    requested_device, model, fallback (why the GPU isn't used, or None),
    cpu_threads, error}."""
    pref = (device_pref or "auto").strip().lower()
    if pref not in ("auto", "cuda", "cpu"):
        log(f"[API] Unknown WHISPER_DEVICE '{device_pref}'; using auto.")
        pref = "auto"
    gpus = device_count() if pref != "cpu" else 0
    model_size = (model_size or "").strip() or default_model(gpus > 0)
    info = {"requested_device": pref, "model": model_size, "device": None,
            "compute_type": None, "fallback": None, "cpu_threads": None, "error": None}

    if pref != "cpu":
        if gpus <= 0:
            reason = ("no NVIDIA GPU found (CTranslate2 sees no CUDA device; AMD and "
                      "Intel graphics can't run it)")
            if pref == "cuda":
                info["fallback"] = reason
            else:
                log(f"[API] Auto device: {reason}; using the processor.")
        elif _recent_crash(marker, now()):
            info["fallback"] = ("the last start crashed while starting the GPU; "
                                "using the processor (GPU retried after %d h)"
                                % (CRASH_RETRY_AFTER // 3600))
        else:
            ctype = compute_type_for("cuda", compute_pref)
            log(f"[API] Loading {model_size} on the NVIDIA GPU ({ctype})...")
            _touch(marker)
            m = None
            try:
                m = factory(model_size, device="cuda", compute_type=ctype,
                            download_root=download_root)
                _smoke_test(m)
                _remove(marker)
                info.update(device="cuda", compute_type=ctype)
                log(f"[API] Model loaded on the GPU ({ctype}) and ready.")
                return m, info
            except Exception as e:
                _remove(marker)  # it raised rather than crashed: retry next start
                info["fallback"] = "the GPU failed to load the model: " + _short(e)
                m = None  # release any half-loaded GPU model before the CPU one

    if info["fallback"]:
        log(f"[API] WARNING: running Whisper on the PROCESSOR, not the GPU: "
            f"{info['fallback']}.")
    threads = cpu_threads(threads_env)
    ctype = compute_type_for("cpu", compute_pref)
    if (compute_pref or "").strip() and ctype != (compute_pref or "").strip().lower():
        log(f"[API] WHISPER_COMPUTE_TYPE={compute_pref} needs a GPU; using {ctype} on the processor.")
    if slow_on_cpu(model_size):
        log(f"[API] Note: {model_size} is slow on a processor (several seconds or more "
            f"per sentence). {CPU_DEFAULT_MODEL} is the recommended model without an "
            f"NVIDIA GPU (Settings > Transcription).")
    log(f"[API] Loading {model_size} on the processor ({ctype}, {threads} threads)...")
    info.update(cpu_threads=threads)
    try:
        m = factory(model_size, device="cpu", compute_type=ctype, cpu_threads=threads,
                    download_root=download_root)
    except Exception as e:
        info["error"] = _short(e)
        log(f"[API] Error loading model: {info['error']}")
        return None, info
    info.update(device="cpu", compute_type=ctype)
    log("[API] Model loaded on the processor and ready.")
    return m, info
