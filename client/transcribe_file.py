import sys
import requests
import pyperclip
import tkinter as tk
from tkinter import ttk
import os
import threading
from dotenv import load_dotenv

# Load .env from the repo root so WHISPER_API_URL (and friends) are picked up
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

API_URL = os.environ.get("WHISPER_API_URL", "http://127.0.0.1:5000/transcribe")
# Same .env as the server: when the server requires a key, send it.
_API_KEY = os.environ.get("WHISPER_API_KEY", "").strip()
API_HEADERS = {"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}


class ProcessingOverlay:
    """Frameless 'Transcribing...' window with an indeterminate progress bar.

    Deliberately mirrors LoadingOverlay in background_listener.py so the
    right-click path gives the same visual feedback as the backtick hotkey:
    the indicator stays up for the *whole* API call, not a fixed timeout, so a
    slow or hung transcription looks different from a finished one. Kept as a
    separate class rather than imported because this process is one-shot -- it
    ends by dissolving the window and exiting, where the listener's persists.
    """

    def __init__(self, message):
        self.root = tk.Tk()
        self.root.overrideredirect(True)  # Frameless
        self.root.attributes('-topmost', True)  # Always on top
        self.root.attributes('-alpha', 0.9)  # Slight transparency

        # Style (matches the listener overlay)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", background="#333", foreground="white", font=("Segoe UI", 12))
        style.configure("TFrame", background="#333")

        # Layout
        self.frame = ttk.Frame(self.root, padding=20)
        self.frame.pack(fill=tk.BOTH, expand=True)

        self.label = ttk.Label(self.frame, text=message)
        self.label.pack(pady=(0, 10))

        self.progress = ttk.Progressbar(self.frame, mode='indeterminate', length=200)
        self.progress.pack()
        self.progress.start(10)

        self.center_window()

    def center_window(self):
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        self.root.geometry(f'+{x}+{y}')

    def finish(self, message, duration=2500):
        """Swap to a final message, drop the spinner, then close after `duration`."""
        self.progress.stop()
        self.progress.pack_forget()
        self.label.config(text=message)
        self.center_window()
        self.root.after(duration, self.root.destroy)

    # Thread-safe wrapper -- worker threads must not touch Tk directly.
    def finish_safe(self, message, duration=2500):
        self.root.after(0, lambda: self.finish(message, duration))

    def start(self):
        self.root.mainloop()


def transcribe_file(file_path):
    if not os.path.exists(file_path):
        # Shown, not printed: launched from the context menu we run under
        # pythonw.exe, so there is no console for a print() to land in.
        overlay = ProcessingOverlay("Starting...")
        overlay.finish(f"File not found:\n{os.path.basename(file_path)}", 4000)
        overlay.start()
        return

    overlay = ProcessingOverlay(f"Transcribing {os.path.basename(file_path)}...")

    def do_work():
        try:
            with open(file_path, 'rb') as f:
                files = {'file': (os.path.basename(file_path), f)}
                response = requests.post(API_URL, files=files, headers=API_HEADERS, timeout=300)
                response.raise_for_status()
                text = response.json().get("text", "").strip()
        except Exception as e:
            overlay.finish_safe(f"Error: {e}", 4000)
            return

        if text:
            pyperclip.copy(text)
            overlay.finish_safe("Transcribed to Clipboard!", 2500)
        else:
            overlay.finish_safe("No speech detected.", 3000)

    threading.Thread(target=do_work, daemon=True).start()

    # Blocks until the worker's finish_safe() timer destroys the window.
    overlay.start()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        transcribe_file(sys.argv[1])
    else:
        print("Usage: python transcribe_file.py <path_to_file>")
