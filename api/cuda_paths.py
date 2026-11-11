"""Let CTranslate2 find NVIDIA's CUDA libraries when they came from pip.

The installer puts cuBLAS in the venv (the nvidia-cublas-cu12 wheel) instead of
requiring a system-wide CUDA install. Those DLLs live in
site-packages/nvidia/<lib>/bin, which Windows doesn't search, so CTranslate2
would fail to load the GPU. This adds those folders to the DLL search path. It
must run BEFORE faster_whisper / ctranslate2 is imported.

No-op on Linux (the wheels' .so files are found via their RPATH) and when the
nvidia wheels aren't installed (e.g. a PC with a system-wide CUDA toolkit).
"""
import os


def add_nvidia_dll_dirs():
    """Returns the folders added, for logging."""
    if os.name != "nt":
        return []
    try:
        import nvidia  # namespace package created by the nvidia-* wheels
    except ImportError:
        return []
    added = []
    for base in list(getattr(nvidia, "__path__", [])):
        try:
            libs = os.listdir(base)
        except OSError:
            continue
        for lib in libs:
            bin_dir = os.path.join(base, lib, "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
                # CTranslate2 loads cuBLAS with a plain LoadLibrary, which
                # ignores add_dll_directory; PATH covers that case.
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                added.append(bin_dir)
    return added
