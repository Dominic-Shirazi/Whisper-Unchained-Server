"""Recordings kept because the transcription server couldn't be reached.

When a dictation can't be sent (this PC's server is restarting or paused, the
server PC is off, no network), the listener copies the recording here instead
of throwing it away, says so, and tries again every little while. Once the
server answers, the text goes on the clipboard with a notice: by then you have
moved on, so it is never pasted into whatever happens to have focus.

Only plain dictations are kept. A spoken instruction about highlighted text
makes no sense later, so that one just reports the error.

The folder (client/kept_recordings/) holds your voice: it is git-ignored, and
files are removed as soon as they are transcribed, when there are more than
MAX_FILES, or after MAX_AGE_DAYS.
"""
import os
import shutil
import time

FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kept_recordings")
MAX_FILES = 20
MAX_AGE_DAYS = 7
RETRY_SECONDS = 15


def pending(folder=FOLDER):
    """Kept recordings, oldest first."""
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".wav"))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names]


def prune(folder=FOLDER, now=None, max_files=MAX_FILES, max_age_days=MAX_AGE_DAYS):
    """Drop recordings that are too old, then the oldest beyond max_files."""
    now = time.time() if now is None else now
    files = pending(folder)
    for path in list(files):
        try:
            if now - os.path.getmtime(path) > max_age_days * 86400:
                os.remove(path)
                files.remove(path)
        except OSError:
            pass
    for path in files[:max(0, len(files) - max_files)]:
        drop(path)


def keep(wav_path, folder=FOLDER, now=None):
    """Copy a recording into the folder. Returns the new path, or None if it
    couldn't be kept (never raises: the caller is already handling a failure)."""
    now = time.time() if now is None else now
    try:
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        path = os.path.join(folder, stamp + ".wav")
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(folder, "%s-%d.wav" % (stamp, n))
        shutil.copyfile(wav_path, path)
        prune(folder, now)
        return path
    except OSError:
        return None


def drop(path):
    try:
        os.remove(path)
    except OSError:
        pass


def retry_once(paths, transcribe, drop=drop, unreachable=Exception, log=None):
    """Send each kept recording (oldest first). Returns (texts, still_waiting).
    Stops at the first one the server can't take yet (`transcribe` raises
    `unreachable`, normally transcribe_client.Unreachable); a recording that
    fails for any other reason is dropped, since it would fail the same way
    every time. Shared by the Windows and Linux listeners."""
    texts = []
    for n, path in enumerate(paths):
        try:
            text = transcribe(path)
        except unreachable:
            return texts, len(paths) - n
        except Exception as e:
            if log:
                log("kept recording dropped: %s" % e.__class__.__name__)
            drop(path)
            continue
        drop(path)
        if text:
            texts.append(text)
    return texts, 0


def kept_message(error, kept):
    """(title, body) of the notice when a dictation couldn't be sent. Pure."""
    title = str(error) or "Couldn't reach the transcription server."
    if kept:
        return title, ("Your recording is kept. It will be transcribed as soon as the "
                       "server answers, and the text put on the clipboard.")
    return title, "The recording couldn't be kept either. Please say it again."


def ready_message(count):
    """(title, body) once kept recordings have been transcribed. Pure."""
    title = ("Your earlier dictation is ready." if count == 1
             else "Your %d earlier dictations are ready." % count)
    return title, "It's on the clipboard: press Ctrl+V to paste it." if count == 1 else \
        "They're on the clipboard, oldest first: press Ctrl+V to paste them."


def join_texts(texts):
    """Several transcribed recordings as one clipboard text. Pure."""
    return "\n\n".join(t.strip() for t in texts if t and t.strip())
