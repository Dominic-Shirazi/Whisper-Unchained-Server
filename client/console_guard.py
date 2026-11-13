"""Windows started with pythonw.exe (Settings, the setup wizard, the tray) have
no console, so sys.stdout and sys.stderr are None. print() copes with that,
but libraries that write to them directly (Hugging Face's download progress
bar, tqdm, warnings) crash with "'NoneType' object has no attribute 'write'".
ensure() gives them somewhere harmless to write."""
import os
import sys


def ensure():
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


ensure()
