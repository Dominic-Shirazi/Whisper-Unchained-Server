"""The "Transcribe a file" window (tray menu > Transcribe a file...).

Pick an audio or video file, read the text, then copy it or save it as text or
subtitles (.srt / .vtt). Uses the same transcription source as dictation
(this PC, another PC, or a cloud service). Video and large files are first
shrunk to a small speech-only audio file on this PC (subtitles.py).

Run: pythonw client/file_window.py [path]
"""
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings_gui as sg  # noqa: E402  (palette, fonts, DPI awareness, icon)
import subtitles  # noqa: E402
import transcribe_client as tc  # noqa: E402

FILE_TYPES = [("Audio or video", "*.wav *.mp3 *.m4a *.ogg *.opus *.flac *.webm *.aac *.wma "
                                 "*.mp4 *.mov *.mkv *.avi *.m4v *.3gp"),
              ("All files", "*.*")]


def describe_source(env):
    src = tc.source(env)
    if src == "remote":
        return "your server PC"
    if src == "cloud":
        base = (env.get("CLOUD_BASE_URL") or "").lower()
        for label, url, _m in tc.CLOUD_PRESETS.values():
            if url and url.lower() in base:
                return label
        return "your cloud service"
    return "this PC"


def run_job(path, env, report, transcribe=tc.transcribe_segments):
    """Shrink if needed, transcribe, clean up. report(kind, value) is called
    with ("status", text), then ("done", (text, segments)) or ("error", text)."""
    tmp_dir = None
    try:
        send = path
        if subtitles.needs_shrinking(path):
            report("status", "Preparing the audio...")
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="wu_file_")
            send = subtitles.shrink_audio(path, tmp_dir)
        report("status", "Transcribing...")
        text, segments = transcribe(send, env)
        report("done", (text, segments))
    except tc.TranscribeError as e:
        report("error", str(e))
    except ValueError as e:
        report("error", str(e))
    except Exception as e:  # e.g. a file PyAV can't read
        report("error", "Couldn't read this file (%s)." % e.__class__.__name__)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class FileWindow:
    def __init__(self, root, env):
        self.root, self.env = root, env
        self.events = queue.Queue()
        self.path, self.segments, self.started = None, [], None
        self.busy = False

        root.title("Transcribe a file")
        root.configure(bg=sg.BG)
        root.geometry("760x560")
        root.minsize(520, 380)
        sg.set_window_icon(root)
        sg.dark_title_bar(root)
        style = ttk.Style()
        sg.style_scrollbar(style)

        pad = dict(padx=24)
        tk.Label(root, text="Transcribe a file", font=sg.F_TITLE, bg=sg.BG,
                 fg=sg.TEXT).pack(anchor="w", pady=(20, 2), **pad)
        tk.Label(root, text="Audio or video. Sent to %s, the same as your dictation. "
                            "Nothing is pasted anywhere." % describe_source(env),
                 font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED, justify="left",
                 wraplength=700).pack(anchor="w", **pad)

        row = tk.Frame(root, bg=sg.BG)
        row.pack(fill="x", pady=(14, 6), **pad)
        self.choose = sg.Button(row, "Choose a file...", self.pick, kind="primary")
        self.choose.pack(side="left")
        self.name = tk.Label(row, text="", font=sg.F_LABEL, bg=sg.BG, fg=sg.TEXT, anchor="w")
        self.name.pack(side="left", padx=(14, 0), fill="x", expand=True)

        self.status = tk.Label(root, text="", font=sg.F_HELP, bg=sg.BG, fg=sg.MUTED,
                               anchor="w", justify="left", wraplength=700)
        self.status.pack(fill="x", **pad)

        # Buttons first (packed to the bottom), so a short window shrinks the
        # text box instead of pushing the buttons out of sight.
        bar = tk.Frame(root, bg=sg.BG)
        bar.pack(side="bottom", fill="x", pady=(0, 18), **pad)
        box = tk.Frame(root, bg=sg.BORDER, padx=1, pady=1)
        box.pack(fill="both", expand=True, pady=(8, 10), **pad)
        self.text = tk.Text(box, wrap="word", font=sg.F_BODY, bg=sg.RAISED, fg=sg.TEXT,
                            insertbackground=sg.ACCENT, relief="flat", padx=12, pady=10,
                            undo=True)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.text.yview,
                               style="Vertical.TScrollbar")
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.copy_btn = sg.Button(bar, "Copy text", self.copy)
        self.save_btn = sg.Button(bar, "Save text...", self.save_text)
        self.subs_btn = sg.Button(bar, "Save subtitles...", self.save_subtitles)
        for b in (self.copy_btn, self.save_btn, self.subs_btn):
            b.pack(side="left", padx=(0, 8))
        self._set_result_buttons(False)
        self.root.after(100, self._poll)

    # --- actions ------------------------------------------------------------
    def pick(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="Choose an audio or video file",
                                          filetypes=FILE_TYPES)
        if path:
            self.start(path)

    def start(self, path):
        if not os.path.isfile(path):
            self._show_error("File not found: %s" % os.path.basename(path))
            return
        self.path, self.segments, self.busy = path, [], True
        self.started = time.monotonic()
        self.name.config(text=os.path.basename(path))
        self.text.delete("1.0", "end")
        self._set_result_buttons(False)
        self.choose.set_enabled(False)
        threading.Thread(target=run_job, args=(path, self.env, self._report),
                         daemon=True).start()

    def _report(self, kind, value):  # worker thread: hand over to Tk
        self.events.put((kind, value))

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self._status_text = value
                elif kind == "done":
                    self._finish(*value)
                elif kind == "error":
                    self._show_error(value)
        except queue.Empty:
            pass
        if self.busy:
            secs = int(time.monotonic() - self.started)
            self.status.config(fg=sg.MUTED, text="%s %d:%02d  (long recordings take a few minutes)"
                               % (getattr(self, "_status_text", "Working..."), secs // 60, secs % 60))
        self.root.after(250, self._poll)

    def _finish(self, text, segments):
        self.busy = False
        self.segments = segments
        self.choose.set_enabled(True)
        self.text.insert("1.0", text)
        secs = int(time.monotonic() - self.started)
        if text:
            words = len(text.split())
            self.status.config(fg=sg.MUTED, text="Done: %d words in %d:%02d. You can edit the text "
                               "before copying or saving it." % (words, secs // 60, secs % 60))
            self._set_result_buttons(True)
            self.subs_btn.set_enabled(bool(segments))
        else:
            self.status.config(fg=sg.MUTED, text="Done, but no speech was found in this file.")

    def _show_error(self, message):
        self.busy = False
        self.choose.set_enabled(True)
        # Words first, colour second (never colour alone).
        self.status.config(fg=sg.DANGER, text="Didn't work: " + message)

    def _set_result_buttons(self, on):
        for b in (self.copy_btn, self.save_btn, self.subs_btn):
            b.set_enabled(on)

    def _current_text(self):
        return self.text.get("1.0", "end").strip()

    def copy(self):
        text = self._current_text()
        if not text:
            return
        try:
            import clipboard_win
            if clipboard_win.wanted(self.env):
                clipboard_win.set_text(text, private=True)
            else:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
        except Exception:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        self.status.config(fg=sg.MUTED, text="Copied.")

    def _save_dialog(self, ext, types):
        base = os.path.splitext(os.path.basename(self.path or "transcript"))[0]
        return filedialog.asksaveasfilename(
            parent=self.root, defaultextension=ext, filetypes=types,
            initialdir=os.path.dirname(self.path) if self.path else None,
            initialfile=base + ext)

    def save_text(self):
        dest = self._save_dialog(".txt", [("Text", "*.txt")])
        if dest:
            self._write(dest, self._current_text() + "\n")

    def save_subtitles(self):
        dest = self._save_dialog(".srt", [("SubRip subtitles", "*.srt"),
                                          ("WebVTT subtitles", "*.vtt")])
        if not dest:
            return
        body = subtitles.to_vtt(self.segments) if dest.lower().endswith(".vtt") \
            else subtitles.to_srt(self.segments)
        self._write(dest, body)

    def _write(self, dest, body):
        try:
            with open(dest, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            self.status.config(fg=sg.MUTED, text="Saved %s." % os.path.basename(dest))
        except OSError as e:
            self._show_error("Couldn't save: %s" % e)


def main():
    env = dict(os.environ)
    env.update(sg.read_env())
    root = tk.Tk()
    win = FileWindow(root, env)
    if len(sys.argv) > 1 and sys.argv[1] != "--selftest":
        root.after(200, lambda: win.start(sys.argv[1]))
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
