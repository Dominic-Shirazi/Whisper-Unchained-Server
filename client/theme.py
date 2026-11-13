"""Colours and fonts shared by every Whisper Unchained window on Windows
(Settings, the installer, the file and history windows, the recording pill,
the floating mic button).

Neutral greys with one accent colour. The accent is picked in Settings
(UI_ACCENT in .env) from ACCENTS below; add a new entry there to try another.
Blue-family accents are the default because they stay distinct for red/green
colour blindness; state is never shown by colour alone anyway.
"""
import ctypes
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV = os.path.join(os.path.dirname(_HERE), ".env")

# name -> (accent, hover, text on an accent-filled button)
ACCENTS = {
    "periwinkle": ("#8C9EFF", "#A6B4FF", "#0F1220"),
    "blue":       ("#4C9AFF", "#6FAEFF", "#081320"),
    "lavender":   ("#B9A7F5", "#CBBDF8", "#17121F"),
    "teal":       ("#2FB79A", "#3ACFAF", "#0E1418"),
    "amber":      ("#E0A84A", "#EBBC66", "#1E1608"),
}
DEFAULT_ACCENT = "periwinkle"
ACCENT_LABELS = [("Periwinkle (default)", "periwinkle"), ("Blue", "blue"),
                 ("Lavender", "lavender"), ("Teal", "teal"), ("Amber", "amber")]


def accent_name(env_path=_ENV):
    try:
        from dotenv import dotenv_values
        v = (dotenv_values(env_path).get("UI_ACCENT") or "").strip().lower()
    except Exception:
        v = ""
    return v if v in ACCENTS else DEFAULT_ACCENT


# --- Neutral greys (no blue tint) -------------------------------------------
BG       = "#17181A"   # window ground
SURFACE  = "#1E1F22"   # sidebar, footer, cards
RAISED   = "#27292D"   # input fields, ghost buttons
HOVER    = "#2E3035"   # hover on raised things
BORDER   = "#35373C"
TEXT     = "#ECECEC"
MUTED    = "#A9ABB0"   # help text, secondary labels
FAINT    = "#7D8086"   # tertiary text, disabled
DANGER   = "#E0734F"
WARN     = "#E0A84A"

ACCENT, ACCENT_H, ON_ACCENT = ACCENTS[accent_name()]


# --- Fonts -------------------------------------------------------------------
# Noto Sans (SIL Open Font License, client/assets/fonts), loaded privately for
# this process, in the Medium weight for normal text: regular Segoe UI read as
# too thin on the dark background. Falls back to Segoe UI.
def _load_bundled_fonts():
    folder = os.path.join(_HERE, "assets", "fonts")
    loaded = 0
    for name in ("NotoSans-SemiCondensedMedium.ttf", "NotoSans-SemiCondensedSemiBold.ttf"):
        try:
            loaded += bool(ctypes.windll.gdi32.AddFontResourceExW(os.path.join(folder, name), 0x10, 0))
        except Exception:
            pass
    return loaded == 2


if _load_bundled_fonts():
    # (Windows truncates the SemiBold family name to 31 characters: "...SemiBol".)
    FONT, FONT_SB = "Noto Sans SemiCondensed Medium", "Noto Sans SemiCondensed SemiBol"
else:
    FONT, FONT_SB = "Segoe UI", "Segoe UI Semibold"
FONT_DISPLAY = FONT_SB

# Font files for PIL drawing (the floating button, toasts drawn as images).
FONT_FILE = os.path.join(_HERE, "assets", "fonts", "NotoSans-SemiCondensedMedium.ttf")
FONT_FILE_SB = os.path.join(_HERE, "assets", "fonts", "NotoSans-SemiCondensedSemiBold.ttf")
