"""The recording "style pill" under the floating mic button, and the style
sheet it opens: the phone app's pill and sheet (FloatingButtonService.kt,
contracts F-6 / F-7 / F-8 / F-15) on Windows.

While a recording runs, a small pill under the mic button shows one cleanup
style and its current value, e.g.

        Grammar & flow
        Keep my words
           o  .  .            <- where the value sits in its row (left = least)

  swipe left   (finger moves right-to-left)  -> "do more": the next value
  swipe right  (finger moves left-to-right)  -> "do less"
  swipe up / down                            -> show the next / previous style
                                                (Grammar & flow -> Conciseness
                                                -> Punctuation, wrapping)

Like a photo carousel: the label leaves with the finger and the new one comes
in from the other side; at either end it just bumps. A press that moves 24 px
(at 100% scaling) is a swipe, by touch or by mouse; less is a tap. The mouse
wheel over the pill does the same: wheel down / up = next / previous style,
tilt right / left (or a touchpad's sideways scroll) = do more / do less.

After 2 s with nothing touched it shrinks (smaller, no category line, 70%
opacity) so it's out of the way; touching it, hovering over it or dragging the
button brings it back for another 2 s.

Tap (or click) the pill -> the style sheet: every option of Grammar & flow,
Conciseness, Punctuation and Tone as chips, the current one filled AND with a
check mark AND in bold (Dom is red/green colour blind: never colour alone),
the recording time, a close X and Done. The rest of the screen dims; a click
outside closes it and goes nowhere else (it can't move your caret). The
recording keeps going the whole time; the mic button stays clickable above
the dim, and stops the recording as usual.

Tone (F-15): Professional / Casual / Friendly need rewording, so picking one
while Grammar & flow is "Keep my words" moves it to "Fix grammar" and says so.
"Mean" (this PC's or the server PC's mean mode) is independent of the rest.

What's sent: nothing changes unless you touch a value. Any change (even
re-picking the current chip, as on the phone) sends that one dictation with
processing=clean and all the cleanup_* fields, so it's cleaned even if
cleanup is off; Mean sends processing=mean. The next recording starts from
the app's style (app_styles.json) or the AI Editing defaults again.

Nothing here takes focus: every window is WS_EX_NOACTIVATE and answers
WM_MOUSEACTIVATE / WM_POINTERACTIVATE with "don't activate". The windows live
on the mic button's thread (caret_button.py drives them from its timer).
"""
import ctypes
import math
import os
import threading
import time
import traceback
from ctypes import wintypes

# --- the styles (same words and order as the phone) --------------------------

# (key, name, ((value, label), ...)) from less to more, like the phone's rows.
DIMENSIONS = (
    ("rewrite", "Grammar & flow",
     (("keep", "Keep my words"), ("fix", "Fix grammar"), ("polish", "Polish"))),
    ("shorten", "Conciseness",
     (("off", "Full length"), ("light", "Shorter"), ("strong", "Shortest"))),
    ("punctuation", "Punctuation",
     (("minimal", "Minimal"), ("light", "Light"), ("standard", "Standard"))),
)
TONES = (("neutral", "My own tone"), ("professional", "Professional"),
         ("casual", "Casual"), ("friendly", "Friendly"))
MEAN = ("mean", "Mean")
TONE_HINT = "Switched to Fix grammar so the tone can change"

# The server's defaults (api/cleanup_prompt.json) and where Settings keeps ours.
DEFAULTS = {"punctuation": "standard", "rewrite": "keep", "tone": "neutral",
            "shorten": "off", "emoji": "off"}
ENV_KEYS = {"punctuation": "CLEANUP_PUNCTUATION", "rewrite": "CLEANUP_REWRITE",
            "tone": "CLEANUP_TONE", "shorten": "CLEANUP_SHORTEN", "emoji": "CLEANUP_EMOJI"}
VALID = {
    "rewrite": {v for v, _ in DIMENSIONS[0][2]},
    "shorten": {v for v, _ in DIMENSIONS[1][2]},
    "punctuation": {v for v, _ in DIMENSIONS[2][2]},
    "tone": {v for v, _ in TONES} | {MEAN[0]},
    "emoji": {"on", "off"},
}

# --- timing and sizes (from FloatingButtonService.kt; sizes in 96-DPI px) ----
IDLE_SHRINK_SECS = 2.0      # F-8: 2 s untouched -> shrink
SHRUNK_ALPHA = 0.70
SHRUNK_ZOOM = 0.88
SHRINK_MS, EXPAND_MS = 200, 150
VALUE_OUT_MS, VALUE_IN_MS, VALUE_SLIDE = 70, 80, 16
DIM_OUT_MS, DIM_IN_MS, DIM_SLIDE = 85, 95, 8
BOUNCE_MS, BOUNCE = 40, 4
SHEET_IN_MS, SHEET_OUT_MS, SHEET_FROM_ZOOM = 200, 160, 0.96
SCRIM_ALPHA = 0.40          # the dim behind the sheet (no blur on Windows)
HINT_SECS = 4.0
SWIPE_DP = 24               # F-6: ~24 dp of movement before a swipe counts
PILL_GAP = 6                # between the button and the pill
SHEET_GAP = 8
SHEET_W = 392
WHEEL_DELTA = 120

SS = 4                      # supersampling for shapes


# --- start values and what gets sent -------------------------------------------

def _env_value(env, key):
    v = str((env or {}).get(key) or "").strip().strip('"').strip("'").split(" #")[0].strip().lower()
    if key == "CLEANUP_EMOJI":
        if not v:
            return ""
        return "on" if v in ("1", "true", "yes", "on") else "off"
    return v


def default_values(env):
    """The AI Editing defaults from .env (CLEANUP_*), the server's where unset."""
    out = dict(DEFAULTS)
    for key, env_key in ENV_KEYS.items():
        v = _env_value(env, env_key)
        if v in VALID[key] and v != MEAN[0]:
            out[key] = v
    return out


def start_values(rule, env):
    """Where the pill starts: the app's own style (app_styles.json) for every
    option it sets, the AI Editing defaults for the rest."""
    out = default_values(env)
    for key in DEFAULTS:
        v = str((rule or {}).get(key) or "").strip().lower()
        if v in VALID[key] and v != MEAN[0]:
            out[key] = v
    return out


def classify_swipe(dx, dy, threshold):
    """Pointer travel since the press -> "more" | "less" | "next" | "prev",
    or None below the threshold. Carousel (F-8): the finger moving LEFT brings
    in the option on the right ("more"); moving RIGHT, the one on the left
    ("less"). Up / down switch the style shown (up = next)."""
    if abs(dx) < threshold and abs(dy) < threshold:
        return None
    if abs(dx) >= abs(dy):
        return "more" if dx < 0 else "less"
    return "next" if dy < 0 else "prev"


def swipe_threshold(scale):
    return max(8, int(round(SWIPE_DP * scale)))


class PressTracker:
    """Press -> move -> release on the pill: one swipe (decided the moment the
    pointer has travelled far enough, like the phone) or a tap."""

    def __init__(self, threshold=SWIPE_DP):
        self.threshold = threshold
        self.start = None
        self.swiped = False

    @property
    def pressed(self):
        return self.start is not None

    def press(self, pt):
        self.start, self.swiped = pt, False

    def move(self, pt):
        """The swipe this move completes, once per press; else None."""
        if self.start is None or self.swiped:
            return None
        act = classify_swipe(pt[0] - self.start[0], pt[1] - self.start[1], self.threshold)
        if act:
            self.swiped = True
        return act

    def release(self, pt=None):
        """ "tap" if the press never became a swipe, else None."""
        if self.start is None:
            return None
        if pt is not None:
            self.move(pt)
        kind = None if self.swiped else "tap"
        self.cancel()
        return kind

    def cancel(self):
        self.start, self.swiped = None, False


class WheelAccumulator:
    """Mouse-wheel deltas -> pill actions, one per notch (120); a precision
    touchpad's small deltas add up. Vertical: down = next style, up =
    previous. Horizontal (tilt / sideways scroll): right = more, left = less."""

    def __init__(self):
        self.v = self.h = 0

    def add(self, delta, horizontal=False):
        acts = []
        if horizontal:
            self.h += delta
            while abs(self.h) >= WHEEL_DELTA:
                acts.append("more" if self.h > 0 else "less")
                self.h -= WHEEL_DELTA if self.h > 0 else -WHEEL_DELTA
        else:
            self.v += delta
            while abs(self.v) >= WHEEL_DELTA:
                acts.append("prev" if self.v > 0 else "next")
                self.v -= WHEEL_DELTA if self.v > 0 else -WHEEL_DELTA
        return acts


class StyleSession:
    """One recording's style choices. Changed on the button's thread (pill and
    sheet), read by the listener when the recording is sent. The overrides
    are for this dictation only (the phone: the next one starts from the
    saved defaults again)."""

    def __init__(self, values, mean_available=False, started=None):
        self._lock = threading.Lock()
        self.values = dict(DEFAULTS, **(values or {}))
        self.dim = 0                      # the pill always starts on Grammar & flow
        self.modified = False
        self.mean_available = bool(mean_available)
        self.started = time.monotonic() if started is None else started
        self.closed = False
        self.hint, self.hint_at = "", 0.0
        self.version = 0

    # -- reading -------------------------------------------------------------
    def index_of(self, key):
        opts = dict((d[0], d[2]) for d in DIMENSIONS)[key]
        vals = [v for v, _ in opts]
        v = self.values.get(key)
        return vals.index(v) if v in vals else 0   # the phone: unknown -> first

    def content(self, dim=None):
        """(category, value label, index, count) the pill shows."""
        key, name, opts = DIMENSIONS[self.dim if dim is None else dim]
        i = self.index_of(key)
        return name, opts[i][1], i, len(opts)

    def tones(self):
        return TONES + ((MEAN,) if self.mean_available else ())

    def hint_text(self, now=None):
        now = time.monotonic() if now is None else now
        return self.hint if self.hint and now - self.hint_at < HINT_SECS else ""

    # -- changing --------------------------------------------------------------
    def _changed(self):
        self.modified = True
        self.version += 1

    def step(self, delta):
        """Pill swipe left (+1, "do more") / right (-1). False at either end
        (nothing changes, nothing is marked as changed)."""
        with self._lock:
            if self.closed:
                return False
            key, _name, opts = DIMENSIONS[self.dim]
            i = self.index_of(key) + (1 if delta > 0 else -1)
            if not 0 <= i < len(opts):
                return False
            self.values[key] = opts[i][0]
            self._changed()
            return True

    def cycle(self, delta):
        """Swipe up (+1) / down (-1): which style the pill shows. Not a change."""
        with self._lock:
            self.dim = (self.dim + (1 if delta > 0 else -1)) % len(DIMENSIONS)
            self.version += 1

    def pick(self, key, value, now=None):
        """A chip in the sheet. Tone per F-15: Mean on its own; Professional /
        Casual / Friendly move "Keep my words" to "Fix grammar" (they need
        rewording) and leave a hint; "My own tone" changes nothing else."""
        with self._lock:
            if self.closed or key not in VALID or value not in VALID[key]:
                return False
            if key == "tone" and value == MEAN[0] and not self.mean_available:
                return False
            if key == "tone" and value not in (MEAN[0], "neutral") and self.values["rewrite"] == "keep":
                self.values["rewrite"] = "fix"
                self.hint = TONE_HINT
                self.hint_at = time.monotonic() if now is None else now
            self.values[key] = value
            self._changed()
            return True

    def close(self):
        """The recording stopped: no more changes, the pill goes away."""
        with self._lock:
            self.closed = True
            self.version += 1

    # -- sending ---------------------------------------------------------------
    def request_fields(self, base=None):
        """The form fields for this dictation. Untouched: exactly `base` (the
        app's style, app_styles.request_fields, as before). Touched: the
        phone's rule, processing=clean + every cleanup_* option (tone is
        neutral with "Keep my words", contract 02 section 2), or
        processing=mean for Mean."""
        with self._lock:
            if not self.modified:
                return dict(base or {})
            v = dict(self.values)
        if v["tone"] == MEAN[0]:
            return {"processing": "mean"}
        return {"processing": "clean",
                "cleanup_punctuation": v["punctuation"],
                "cleanup_rewrite": v["rewrite"],
                "cleanup_tone": "neutral" if v["rewrite"] == "keep" else v["tone"],
                "cleanup_shorten": v["shorten"],
                "cleanup_emoji": v["emoji"]}

    def describe(self):
        """For the log: option names only, never text."""
        f = self.request_fields()
        return ", ".join("%s=%s" % (k.replace("cleanup_", ""), f[k]) for k in sorted(f)) or "unchanged"


def session_for(rule, env, source, selection=False, styles_ok=True, started=None):
    """The pill's session for one recording, or None when there's no pill:
    an edit of highlighted text (what you say is an instruction, not a
    dictation), a cloud service (no cleanup on Windows yet), or a setup that
    can't send per-dictation options (old WHISPER_API_URL only)."""
    if selection or source == "cloud" or not styles_ok:
        return None
    return StyleSession(start_values(rule, env), mean_available=source in ("local", "remote"),
                        started=started)


# --- placement (pure) ----------------------------------------------------------

def place_below(button, size, work, gap):
    """Top-left (x, y) of a `size` (w, h) box centred under `button` (l, t, r,
    b), or above it when there's no room below; kept inside `work`. Returns
    (x, y, above)."""
    l, t, r, b = button
    w, h = size
    wl, wt, wr, wb = work
    x = (l + r) / 2 - w / 2
    y, above = b + gap, False
    if y + h > wb and t - gap - h >= wt:
        y, above = t - gap - h, True
    x = max(wl, min(x, wr - w))
    y = max(wt, min(y, wb - h))
    return int(round(x)), int(round(y)), above


def place_sheet(button, size, work, gap):
    """The sheet: under the button (or above); with room for neither, beside
    it so the mic button (the stop control) stays clear."""
    x, y, above = place_below(button, size, work, gap)
    l, t, r, b = button
    w, h = size
    overlaps = x < r and x + w > l and y < b and y + h > t
    if overlaps:
        wl, wt, wr, wb = work
        if r + gap + w <= wr:
            x = r + gap
        elif l - gap - w >= wl:
            x = l - gap - w
    return int(round(x)), int(round(y)), above


# --- drawing -----------------------------------------------------------------------

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def palette(env_path=None):
    """theme.py colours as RGB, with the accent picked in Settings."""
    try:
        import theme
        acc = theme.ACCENTS.get(theme.accent_name(env_path) if env_path else "",
                                (theme.ACCENT, theme.ACCENT_H, theme.ON_ACCENT))
        p = {"surface": theme.SURFACE, "raised": theme.RAISED, "hover": theme.HOVER,
             "border": theme.BORDER, "text": theme.TEXT, "muted": theme.MUTED,
             "faint": theme.FAINT, "danger": theme.DANGER,
             "accent": acc[0], "accent_h": acc[1], "on_accent": acc[2]}
    except Exception:
        p = {"surface": "#1E1F22", "raised": "#27292D", "hover": "#2E3035", "border": "#35373C",
             "text": "#ECECEC", "muted": "#A9ABB0", "faint": "#7D8086", "danger": "#E0734F",
             "accent": "#8C9EFF", "accent_h": "#A6B4FF", "on_accent": "#0F1220"}
    return {k: _rgb(v) for k, v in p.items()}


_fonts = {}


def _font(bold, px):
    px = max(6.0, round(px * 2) / 2)
    key = (bool(bold), px)
    if key not in _fonts:
        from PIL import ImageFont
        paths = []
        try:
            import theme
            paths.append(theme.FONT_FILE_SB if bold else theme.FONT_FILE)
        except Exception:
            pass
        paths.append(r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf")
        font = None
        for p in paths:
            try:
                if os.path.exists(p):
                    font = ImageFont.truetype(p, px)
                    break
            except Exception:
                continue
        _fonts[key] = font or ImageFont.load_default()
    return _fonts[key]


def _line_h(font):
    """A tight line height: cap-to-descender, like a TextView without the
    font's extra padding."""
    asc, desc = font.getmetrics()
    return asc * 0.86 + desc * 0.80


def _text_w(font, text):
    return font.getlength(text)


def _composite(img, layer, ox, oy):
    """alpha_composite `layer` at (ox, oy), clipped to img."""
    W, H = img.size
    w, h = layer.size
    l, t = max(0, ox), max(0, oy)
    r, b = min(W, ox + w), min(H, oy + h)
    if r <= l or b <= t:
        return
    part = layer.crop((l - ox, t - oy, r - ox, b - oy)) if (l, t, r, b) != (ox, oy, ox + w, oy + h) else layer
    img.alpha_composite(part, (l, t))


def _rr_mask(w, h, box, radius):
    """Anti-aliased rounded-rectangle coverage (L, w x h); `box` is local."""
    from PIL import Image, ImageDraw
    big = Image.new("L", (w * SS, h * SS), 0)
    l, t, r, b = box
    if r - l > 0 and b - t > 0:
        rad = max(0.0, min(radius, (r - l) / 2, (b - t) / 2))
        ImageDraw.Draw(big).rounded_rectangle(
            (l * SS, t * SS, r * SS - 1, b * SS - 1), radius=rad * SS, fill=255)
    return big.resize((w, h), Image.BOX)


def fill_rr(img, box, radius, color, alpha=255, ring=None):
    """A rounded rectangle (or, with `ring`, only its outline that wide)."""
    from PIL import Image, ImageChops
    l, t, r, b = box
    ox, oy = int(math.floor(l)), int(math.floor(t))
    w, h = int(math.ceil(r)) - ox, int(math.ceil(b)) - oy
    if w <= 0 or h <= 0:
        return
    local = (l - ox, t - oy, r - ox, b - oy)
    m = _rr_mask(w, h, local, radius)
    if ring:
        inner = (local[0] + ring, local[1] + ring, local[2] - ring, local[3] - ring)
        m = ImageChops.subtract(m, _rr_mask(w, h, inner, max(0.0, radius - ring)))
    if alpha < 255:
        m = m.point(lambda v: v * alpha // 255)
    layer = Image.new("RGBA", (w, h), tuple(color) + (0,))
    layer.putalpha(m)
    _composite(img, layer, ox, oy)


def draw_text(img, xy, text, font, color, anchor="la", alpha=255):
    from PIL import Image, ImageDraw
    x, y = xy
    l, t, r, b = font.getbbox(text, anchor=anchor)
    ox, oy = int(math.floor(x + l)) - 2, int(math.floor(y + t)) - 2
    w, h = int(math.ceil(x + r)) - ox + 3, int(math.ceil(y + b)) - oy + 3
    if w <= 0 or h <= 0:
        return
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).text((x - ox, y - oy), text, font=font, fill=255, anchor=anchor)
    if alpha < 255:
        m = m.point(lambda v: v * alpha // 255)
    layer = Image.new("RGBA", (w, h), tuple(color) + (0,))
    layer.putalpha(m)
    _composite(img, layer, ox, oy)


def _stroke(img, pts, width, color, box):
    """A polyline with round ends inside `box` (l, t, r, b), anti-aliased."""
    from PIL import Image, ImageDraw
    l, t, r, b = box
    ox, oy = int(math.floor(l)) - 1, int(math.floor(t)) - 1
    w, h = int(math.ceil(r)) - ox + 2, int(math.ceil(b)) - oy + 2
    big = Image.new("L", (w * SS, h * SS), 0)
    dr = ImageDraw.Draw(big)
    sp = [((x - ox) * SS, (y - oy) * SS) for x, y in pts]
    ws = width * SS
    dr.line(sp, fill=255, width=max(1, int(round(ws))), joint="curve")
    for x, y in (sp[0], sp[-1]):
        dr.ellipse((x - ws / 2, y - ws / 2, x + ws / 2, y + ws / 2), fill=255)
    m = big.resize((w, h), Image.BOX)
    layer = Image.new("RGBA", (w, h), tuple(color) + (0,))
    layer.putalpha(m)
    _composite(img, layer, ox, oy)


def draw_check(img, x, cy, size, width, color):
    """A check mark (not a font glyph: every font has it then)."""
    pts = [(x, cy + size * 0.02), (x + size * 0.36, cy + size * 0.36), (x + size, cy - size * 0.36)]
    _stroke(img, pts, width, color, (x - width, cy - size * 0.5, x + size + width, cy + size * 0.5))


def draw_shadow(img, box, radius, blur, alpha, dy):
    from PIL import Image, ImageDraw, ImageFilter
    W, H = img.size
    m = Image.new("L", (W, H), 0)
    l, t, r, b = box
    ImageDraw.Draw(m).rounded_rectangle((l, t + dy, r - 1, b - 1 + dy), radius=radius, fill=alpha)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.putalpha(m)
    img.alpha_composite(layer)


# --- the pill ------------------------------------------------------------------------

CAT_PX, VAL_PX = 11, 13.5
DOT_ON, DOT_OFF, DOT_GAP = 7, 5, 4
PILL_PAD = (14, 6)
PILL_PAD_SHRUNK = (10, 4)
PILL_RADIUS = 16
PILL_ALPHA = 0xF2            # the phone's #EE background, a touch more solid on bright pages


def pill_margin(scale):
    return int(math.ceil(8 * scale))


def pill_size(content, scale, compact=False):
    """(w, h) of the pill itself (no shadow margin)."""
    cat, label, _i, count = content
    k = scale
    fv = _font(True, VAL_PX * k)
    px, py = (PILL_PAD_SHRUNK if compact else PILL_PAD)
    w = _text_w(fv, label)
    h = _line_h(fv) + (3 + DOT_ON) * k
    if not compact:
        fc = _font(False, CAT_PX * k)
        w = max(w, _text_w(fc, cat))
        h += _line_h(fc) + 2 * k
    dots = count * (DOT_OFF + DOT_GAP) * k + (DOT_ON - DOT_OFF) * k
    w = max(w, dots)
    return int(math.ceil(w + 2 * px * k)), int(math.ceil(h + 2 * py * k))


def _draw_pill_content(img, content, box, scale, compact, pal, alpha=255):
    cat, label, idx, count = content
    k = scale
    l, t, r, b = box
    px, py = (PILL_PAD_SHRUNK if compact else PILL_PAD)
    cx = (l + r) / 2
    y = t + py * k
    fv = _font(True, VAL_PX * k)
    if not compact:
        fc = _font(False, CAT_PX * k)
        draw_text(img, (cx, y + fc.getmetrics()[0] * 0.86), cat, fc, pal["muted"], "ms", alpha)
        y += _line_h(fc) + 2 * k
    draw_text(img, (cx, y + fv.getmetrics()[0] * 0.86), label, fv, pal["text"], "ms", alpha)
    y += _line_h(fv) + 3 * k
    # Position dots: the current one is bigger AND accent-coloured.
    total = count * (DOT_OFF + DOT_GAP) * k + (DOT_ON - DOT_OFF) * k - DOT_GAP * k
    x = cx - total / 2
    dcy = y + DOT_ON * k / 2
    for i in range(count):
        d = (DOT_ON if i == idx else DOT_OFF) * k
        col = pal["accent"] if i == idx else pal["faint"]
        fill_rr(img, (x, dcy - d / 2, x + d, dcy + d / 2), d / 2, col,
                alpha=alpha if i == idx else alpha * 200 // 255)
        x += d + DOT_GAP * k


def render_pill(content, scale, compact=False, zoom=1.0, offset=(0.0, 0.0), content_alpha=1.0,
                pal=None, size=None):
    """RGBA image of the pill (plus a shadow margin) at `scale` x `zoom`.
    `offset` / `content_alpha` slide and fade the words and dots inside it
    (the carousel animation); `size` forces the pill's (w, h) at zoom 1."""
    from PIL import Image
    pal = pal or palette()
    k = scale * zoom
    pw, ph = size or pill_size(content, scale, compact)
    pw, ph = pw * zoom, ph * zoom
    m = pill_margin(scale)
    W, H = int(math.ceil(pw)) + 2 * m, int(math.ceil(ph)) + 2 * m
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    box = (m, m, m + pw, m + ph)
    rad = min(PILL_RADIUS * k, ph / 2)
    draw_shadow(img, box, rad, blur=max(1.5, 3 * scale), alpha=90, dy=max(1, round(1.5 * scale)))
    fill_rr(img, box, rad, pal["surface"], alpha=PILL_ALPHA)
    fill_rr(img, box, rad, pal["border"], ring=max(1.0, round(scale)))
    # The words and dots, slid / faded, clipped to the pill.
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dx, dy = offset
    _draw_pill_content(layer, content, (box[0] + dx * k, box[1] + dy * k, box[2] + dx * k, box[3] + dy * k),
                       k, compact, pal)
    clip = Image.new("L", (W, H), 0)
    clip.paste(_rr_mask(W, H, box, rad), (0, 0))
    a = layer.getchannel("A")
    from PIL import ImageChops
    a = ImageChops.multiply(a, clip)
    if content_alpha < 1:
        a = a.point(lambda v: int(v * max(0.0, content_alpha)))
    layer.putalpha(a)
    img.alpha_composite(layer)
    return img


# --- the sheet --------------------------------------------------------------------------

SHEET_PAD = (20, 18)
SHEET_RADIUS = 16
CHIP_H, CHIP_PAD, CHIP_GAP, CHIP_PX = 30, 12, 6, 12.5
ROW_TITLE_PX = 11.5
DONE_H = 40


def sheet_margin(scale):
    return int(math.ceil(18 * scale))


def _rows(session):
    rows = [(key, name, opts) for key, name, opts in DIMENSIONS]
    rows.append(("tone", "Tone", session.tones()))
    return rows


def clock(seconds):
    s = max(0, int(seconds))
    return "%02d:%02d" % (s // 60, s % 60)


def sheet_layout(session, scale, width=SHEET_W, now=None):
    """Where everything in the sheet goes, in the sheet's own pixels (no
    shadow margin): {"size": (w, h), "items": [...]} where each item is
    (kind, box, data). Kinds: header, divider, row_title, chip, hint, done,
    close."""
    k = scale
    W = width * k
    px, py = SHEET_PAD[0] * k, SHEET_PAD[1] * k
    inner_l, inner_r = px, W - px
    items = []
    y = py
    head_h = 28 * k
    items.append(("header", (inner_l, y, inner_r, y + head_h), None))
    cs = 28 * k
    items.append(("close", (inner_r - cs + 6 * k, y, inner_r + 6 * k, y + head_h), None))
    y += head_h + 10 * k
    items.append(("divider", (inner_l, y, inner_r, y + max(1.0, round(k))), None))
    y += 12 * k
    fchip = _font(False, CHIP_PX * k)
    fsel = _font(True, CHIP_PX * k)
    ftitle = _font(True, ROW_TITLE_PX * k)
    check_w = 10 * k + 6 * k
    for key, name, opts in _rows(session):
        y += 4 * k
        th = _line_h(ftitle)
        items.append(("row_title", (inner_l, y, inner_r, y + th), name))
        y += th + 8 * k
        x = inner_l
        ch = CHIP_H * k
        sel = session.values.get(key)
        for value, label in opts:
            selected = value == sel
            # Every chip is as wide as it is when selected (bold + check), so
            # picking one never reflows the row under the pointer.
            tw = max(_text_w(fsel, label) + check_w, _text_w(fchip, label))
            cw = tw + 2 * CHIP_PAD * k
            if x > inner_l and x + cw > inner_r + 0.5:
                x = inner_l
                y += ch + CHIP_GAP * k
            items.append(("chip", (x, y, x + cw, y + ch), (key, value, label, selected)))
            x += cw + CHIP_GAP * k
        y += ch + 8 * k
        if key == "tone":
            hint = session.hint_text(now)
            if hint:
                fh = _font(False, 11.5 * k)
                hh = _line_h(fh)
                items.append(("hint", (inner_l, y - 2 * k, inner_r, y - 2 * k + hh), hint))
                y += hh + 4 * k
    y += 8 * k
    dh = DONE_H * k
    items.append(("done", (inner_l, y, inner_r, y + dh), None))
    y += dh + py
    return {"size": (int(math.ceil(W)), int(math.ceil(y))), "items": items}


def sheet_hit(layout, pt):
    """The control under `pt` (sheet pixels): ("chip", key, value) / ("close",)
    / ("done",), or None."""
    x, y = pt
    for kind, (l, t, r, b), data in layout["items"]:
        if kind in ("chip", "close", "done") and l <= x < r and t <= y < b:
            if kind == "chip":
                return ("chip", data[0], data[1])
            return (kind,)
    return None


def render_sheet(session, scale, pal=None, hover=None, pressed=None, now=None, layout=None):
    """RGBA image of the sheet with its shadow margin, and its layout."""
    from PIL import Image
    pal = pal or palette()
    now = time.monotonic() if now is None else now
    lay = layout or sheet_layout(session, scale, now=now)
    k = scale
    sw, sh = lay["size"]
    m = sheet_margin(scale)
    img = Image.new("RGBA", (sw + 2 * m, sh + 2 * m), (0, 0, 0, 0))
    card = (m, m, m + sw, m + sh)
    rad = SHEET_RADIUS * k
    draw_shadow(img, card, rad, blur=10 * k, alpha=120, dy=round(4 * k))
    fill_rr(img, card, rad, pal["surface"])
    fill_rr(img, card, rad, pal["border"], ring=max(1.0, round(k)))

    def mv(box):
        return (box[0] + m, box[1] + m, box[2] + m, box[3] + m)

    fchip = _font(False, CHIP_PX * k)
    fsel = _font(True, CHIP_PX * k)
    for kind, box, data in lay["items"]:
        l, t, r, b = mv(box)
        cy = (t + b) / 2
        if kind == "header":
            # REC (a word, not only a red dot), the time, the title.
            d = 8 * k
            fill_rr(img, (l, cy - d / 2, l + d, cy + d / 2), d / 2, pal["danger"])
            frec = _font(True, 12 * k)
            draw_text(img, (l + d + 6 * k, cy), "REC", frec, pal["danger"], "lm")
            ftime = _font(True, 13 * k)
            tx = l + d + 6 * k + _text_w(frec, "REC") + 8 * k
            draw_text(img, (tx, cy), clock(now - session.started), ftime, pal["text"], "lm")
            ftitle = _font(False, 12.5 * k)
            draw_text(img, ((l + r) / 2 + 8 * k, cy), "Dictation style", ftitle, pal["muted"], "mm")
        elif kind == "close":
            hov = hover == ("close",)
            ccx, ccy, cr = (l + r) / 2, cy, 13 * k
            if hov:
                fill_rr(img, (ccx - cr, ccy - cr, ccx + cr, ccy + cr), cr, pal["hover"])
            a = 4.5 * k
            w = 1.6 * k
            col = pal["text"] if hov else pal["muted"]
            _stroke(img, [(ccx - a, ccy - a), (ccx + a, ccy + a)], w, col, (ccx - cr, ccy - cr, ccx + cr, ccy + cr))
            _stroke(img, [(ccx - a, ccy + a), (ccx + a, ccy - a)], w, col, (ccx - cr, ccy - cr, ccx + cr, ccy + cr))
        elif kind == "divider":
            fill_rr(img, (l, t, r, b), 0, pal["border"])
        elif kind == "row_title":
            f = _font(True, ROW_TITLE_PX * k)
            draw_text(img, (l, t + f.getmetrics()[0] * 0.86), data, f, pal["muted"], "ls")
        elif kind == "chip":
            key, value, label, selected = data
            hov = hover == ("chip", key, value)
            rad_c = (b - t) / 2
            if selected:
                fill_rr(img, (l, t, r, b), rad_c, pal["accent_h"] if hov else pal["accent"])
                cs = 10 * k
                x0 = l + CHIP_PAD * k
                draw_check(img, x0, cy, cs, 1.8 * k, pal["on_accent"])
                draw_text(img, (x0 + cs + 6 * k, cy), label, fsel, pal["on_accent"], "lm")
            else:
                fill_rr(img, (l, t, r, b), rad_c, pal["hover"] if hov else pal["raised"])
                fill_rr(img, (l, t, r, b), rad_c, pal["faint"] if hov else pal["border"],
                        ring=max(1.0, round(k)))
                draw_text(img, ((l + r) / 2, cy), label, fchip, pal["text"], "mm")
        elif kind == "hint":
            f = _font(False, 11.5 * k)
            draw_text(img, (l, cy), data, f, pal["muted"], "lm")
        elif kind == "done":
            hov = hover == ("done",)
            fill_rr(img, (l, t, r, b), 10 * k, pal["accent_h"] if hov else pal["accent"])
            draw_text(img, ((l + r) / 2, cy), "Done", _font(True, 14 * k), pal["on_accent"], "mm")
    return img, lay


# --- motion -----------------------------------------------------------------------------

def _ease(k):
    return (1 - math.cos(math.pi * max(0.0, min(1.0, k)))) / 2


class Tween:
    """A value moving from where it is to `to` over `ms` (eased)."""

    def __init__(self, value):
        self.v0 = self.v1 = float(value)
        self.t0, self.dur, self.curve = 0.0, 0.0, _ease

    def to(self, value, ms, now, curve=None):
        self.v0 = self.value(now)
        self.v1 = float(value)
        self.t0, self.dur = now, ms / 1000.0
        self.curve = curve or _ease

    def set(self, value):
        self.v0 = self.v1 = float(value)
        self.dur = 0.0

    def value(self, now):
        if self.dur <= 0 or now >= self.t0 + self.dur:
            return self.v1
        return self.v0 + (self.v1 - self.v0) * self.curve((now - self.t0) / self.dur)

    def done(self, now):
        return self.dur <= 0 or now >= self.t0 + self.dur


def decel(k):
    return 1 - (1 - k) ** 2


def accel(k):
    return k * k


class Slide:
    """The words leaving one way and the new ones arriving from the other.
    axis "x" (value) or "y" (style); sign = where the old words go (-1 =
    left / up). frame(now) -> ("old" | "new", (dx, dy), alpha), or None when
    finished. A bounce (at either end) only nudges and comes back."""

    def __init__(self, now, old, axis, sign, dist, out_ms, in_ms, bounce=False):
        self.t0, self.old, self.axis, self.sign = now, old, axis, sign
        self.dist, self.out, self.inn, self.bounce = dist, out_ms / 1000.0, in_ms / 1000.0, bounce

    def _off(self, v):
        return (v, 0.0) if self.axis == "x" else (0.0, v)

    def frame(self, now):
        t = now - self.t0
        if t < self.out:
            k = _ease(t / self.out)
            if self.bounce:
                return "new", self._off(self.sign * self.dist * k), 1.0
            return "old", self._off(self.sign * self.dist * k), 1.0 - k
        t -= self.out
        if t < self.inn:
            k = _ease(t / self.inn)
            if self.bounce:
                return "new", self._off(self.sign * self.dist * (1 - k)), 1.0
            return "new", self._off(-self.sign * self.dist * (1 - k)), k
        return None


def slide_for(action, moved, old, now):
    """The animation for a pill action (F-8 carousel physics)."""
    if action in ("more", "less"):
        sign = -1 if action == "more" else 1    # the finger went left for "more"
        if not moved:
            return Slide(now, old, "x", sign, BOUNCE, BOUNCE_MS, BOUNCE_MS, bounce=True)
        return Slide(now, old, "x", sign, VALUE_SLIDE, VALUE_OUT_MS, VALUE_IN_MS)
    sign = -1 if action == "next" else 1        # up = next: old words leave upwards
    return Slide(now, old, "y", sign, DIM_SLIDE, DIM_OUT_MS, DIM_IN_MS)


# --- Win32 windows --------------------------------------------------------------------------

import caret_button as _cb  # noqa: E402  (its ctypes declarations and layered-window helpers)

user32, gdi32, kernel32 = _cb.user32, _cb.gdi32, _cb.kernel32
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte,
                                              wintypes.DWORD]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ

WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_RBUTTONDOWN = 0x0204
WM_POINTERUPDATE = 0x0245
WM_POINTERDOWN = 0x0246
WM_POINTERUP = 0x0247
WM_POINTERACTIVATE = 0x024B
WM_POINTERCAPTURECHANGED = 0x024C
PA_NOACTIVATE = 3
LWA_ALPHA = 0x2
BLACK_BRUSH = 4
IDC_ARROW = 32512
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
ANIM_TIMER, HOUSE_TIMER = 1, 2
ANIM_MS, HOUSE_MS = 15, 100

_PILL_CLASS = "WhisperUnchainedStylePill"
_SHEET_CLASS = "WhisperUnchainedStyleSheet"
_SCRIM_CLASS = "WhisperUnchainedStyleScrim"

_registry = {}       # hwnd -> handler(hwnd, msg, wp, lp) -> result or None
_registered = set()


def _dispatch(hwnd, msg, wp, lp):
    handler = _registry.get(hwnd)
    if handler is not None:
        try:
            r = handler(hwnd, msg, wp, lp)
            if r is not None:
                return r
        except Exception:
            pass
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


_WNDPROC = _cb.WNDPROC(_dispatch)


def _register(name, cursor, brush=None):
    if name in _registered:
        return
    wc = _cb.WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_cb.WNDCLASSEXW)
    wc.lpfnWndProc = _WNDPROC
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(cursor))
    if brush is not None:
        wc.hbrBackground = brush
    wc.lpszClassName = name
    user32.RegisterClassExW(ctypes.byref(wc))
    _registered.add(name)


def _signed(v):
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _lparam_pt(lp):
    return _signed(lp), _signed(lp >> 16)


class _Layered:
    """One no-activate, topmost, layered popup drawn with UpdateLayeredWindow."""

    def __init__(self, cls, title, handler):
        ex = _cb.WS_EX_LAYERED | _cb.WS_EX_TOOLWINDOW | _cb.WS_EX_TOPMOST | _cb.WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, cls, title, _cb.WS_POPUP, 0, 0, 1, 1,
                                           None, None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        _registry[self.hwnd] = handler
        self.dc = gdi32.CreateCompatibleDC(None)
        self.bmp = self.bits = None
        self.bmp_size = None
        self.shown = False
        self.pos = (0, 0)
        self.size = (0, 0)

    def update(self, img, pos, alpha=255):
        from caret_button import premultiplied_bgra
        w, h = img.size
        if (w, h) != self.bmp_size or not self.bits:
            bmi = _cb.BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_cb.BITMAPINFOHEADER)
            bmi.biWidth, bmi.biHeight = w, -h
            bmi.biPlanes, bmi.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not bmp:
                return
            old = gdi32.SelectObject(self.dc, bmp)
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            self.bmp, self.bits, self.bmp_size = bmp, bits, (w, h)
            del old
        data = premultiplied_bgra(img)
        ctypes.memmove(self.bits, data, len(data))
        screen = user32.GetDC(None)
        try:
            blend = _cb.BLENDFUNCTION(0, 0, max(1, min(255, int(alpha))), _cb.AC_SRC_ALPHA)
            p = wintypes.POINT(int(pos[0]), int(pos[1]))
            s = wintypes.SIZE(w, h)
            src = wintypes.POINT(0, 0)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(p), ctypes.byref(s),
                                       self.dc, ctypes.byref(src), 0, ctypes.byref(blend), _cb.ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)
        self.pos, self.size = (int(pos[0]), int(pos[1])), (w, h)
        if not self.shown:
            self.show()

    def show(self):
        user32.SetWindowPos(self.hwnd, _cb.HWND_TOPMOST, 0, 0, 0, 0,
                            _cb.SWP_NOMOVE | _cb.SWP_NOSIZE | _cb.SWP_NOACTIVATE | _cb.SWP_SHOWWINDOW)
        user32.ShowWindow(self.hwnd, _cb.SW_SHOWNOACTIVATE)
        self.shown = True

    def to_top(self):
        if self.shown:
            user32.SetWindowPos(self.hwnd, _cb.HWND_TOPMOST, 0, 0, 0, 0,
                                _cb.SWP_NOMOVE | _cb.SWP_NOSIZE | _cb.SWP_NOACTIVATE)

    def hide(self):
        if self.shown:
            user32.ShowWindow(self.hwnd, _cb.SW_HIDE)
        self.shown = False

    def destroy(self):
        _registry.pop(self.hwnd, None)
        try:
            user32.DestroyWindow(self.hwnd)
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            gdi32.DeleteDC(self.dc)
        except Exception:
            pass


class _Scrim:
    """The dim behind the sheet, over every monitor: one black window at a
    constant alpha. It also swallows the click that closes the sheet, so that
    click never reaches (or moves the caret in) the app underneath."""

    def __init__(self, handler):
        _register(_SCRIM_CLASS, IDC_ARROW, brush=gdi32.GetStockObject(BLACK_BRUSH))
        ex = _cb.WS_EX_LAYERED | _cb.WS_EX_TOOLWINDOW | _cb.WS_EX_TOPMOST | _cb.WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, _SCRIM_CLASS, "", _cb.WS_POPUP, 0, 0, 1, 1,
                                           None, None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        _registry[self.hwnd] = handler
        self.shown = False
        self.alpha = -1

    def set_alpha(self, a):
        a = max(0, min(255, int(a)))
        if a != self.alpha:
            user32.SetLayeredWindowAttributes(self.hwnd, 0, a, LWA_ALPHA)
            self.alpha = a

    def show(self, alpha):
        self.set_alpha(alpha)
        if not self.shown:
            x, y = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w, h = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            user32.SetWindowPos(self.hwnd, _cb.HWND_TOPMOST, x, y, w, h,
                                _cb.SWP_NOACTIVATE | _cb.SWP_SHOWWINDOW)
            user32.ShowWindow(self.hwnd, _cb.SW_SHOWNOACTIVATE)
            self.shown = True

    def hide(self):
        if self.shown:
            user32.ShowWindow(self.hwnd, _cb.SW_HIDE)
        self.shown = False

    def destroy(self):
        _registry.pop(self.hwnd, None)
        try:
            user32.DestroyWindow(self.hwnd)
        except Exception:
            pass


class StyleOverlay:
    """The pill and the sheet for the mic button. Lives on the button's
    thread: caret_button calls sync() whenever it paints, poke() when the
    button is dragged, keep_on_top() with its own re-topping, destroy() at
    the end."""

    def __init__(self, env_path=None, log_path=None, log=None, raise_button=None):
        self.env_path = env_path
        self.log_path = log_path
        self._log = log or (lambda msg: None)
        self._raise_button = raise_button or (lambda: None)   # the mic button above the dim
        self.session = None
        self.button = None          # (l, t, r, b) screen px of the mic button
        self.scale = 1.0
        self.work = (0, 0, 1920, 1080)
        self.pal = palette(env_path)
        self.pill = self.sheet = self.scrim = None
        # pill state
        self.compact = False
        self.zoom = Tween(1.0)
        self.alpha = Tween(1.0)
        self.slide = None
        self.last_touch = 0.0
        self.hover = False
        self.press = PressTracker()
        self.wheel = WheelAccumulator()
        self.touch_id = None
        self._pill_key = None
        # sheet state
        self.sheet_open = False     # open or opening (closing counts as closed)
        self.sheet_zoom = Tween(1.0)
        self.sheet_alpha = Tween(0.0)
        self.sheet_closing = False
        self._sheet_img = None
        self._sheet_layout = None
        self._sheet_box = None      # (x, y) of the sheet card on screen
        self._sheet_key = None
        self._sheet_hover = None
        self._sheet_down = None
        self._scrim_down = False
        self._anim = False
        self._house = False

    # -- setup ------------------------------------------------------------------
    def _ensure(self):
        if self.pill is None:
            _register(_PILL_CLASS, _cb.IDC_HAND)
            _register(_SHEET_CLASS, IDC_ARROW)
            self.pill = _Layered(_PILL_CLASS, "Dictation style", self._pill_msg)
            self.sheet = _Layered(_SHEET_CLASS, "Dictation style", self._sheet_msg)
            self.scrim = _Scrim(self._scrim_msg)

    def destroy(self):
        for w in (self.pill, self.sheet, self.scrim):
            if w is not None:
                w.destroy()
        self.pill = self.sheet = self.scrim = None

    def _err(self, what):
        try:
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write("--- %s\n[style pill] %s\n%s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), what, traceback.format_exc()))
        except Exception:
            pass

    # -- driven by the button ---------------------------------------------------------
    def sync(self, show, session, button, scale, work):
        """Show the pill for `session` under `button`, or take everything
        down (show False: not recording, the button hidden, no session)."""
        now = time.monotonic()
        if not show or session is None or session.closed:
            if self.session is not None or (self.pill and self.pill.shown) or self.sheet_open:
                self._end(now)
            return
        if session is not self.session:
            self._begin(session, now)
        moved = button != self.button or scale != self.scale
        self.button, self.scale, self.work = button, scale, work
        if self.sheet_open or self.sheet_closing:
            if moved and self.sheet_open:
                self._place_sheet(now, relayout=False)
            return
        self._draw_pill(now, force=moved)

    def _begin(self, session, now):
        self._ensure()
        self.session = session
        self.pal = palette(self.env_path)
        self.compact = False
        self.zoom.set(1.0)
        self.alpha.set(1.0)
        self.slide = None
        self.hover = False
        self.press.cancel()
        self.last_touch = now
        self._pill_key = None
        self._close_sheet(now, animated=False)
        self._set_house(True)

    def _end(self, now):
        if self.sheet_open:
            self._close_sheet(now, animated=True)
        elif not self.sheet_closing:
            self._close_sheet(now, animated=False)
        if self.pill is not None:
            self.pill.hide()
        self.press.cancel()
        self.session = None
        self._pill_key = None
        if not self.sheet_closing:
            self._set_anim(False)
            self._set_house(False)

    def poke(self):
        """Something touched (the button was dragged...): full size again."""
        if self.session is not None:
            self._touch(time.monotonic())

    def keep_on_top(self):
        if self.pill is not None and self.pill.shown:
            self.pill.to_top()

    # -- timers --------------------------------------------------------------------
    def _set_anim(self, on):
        if on == self._anim or self.pill is None:
            return
        if on:
            user32.SetTimer(self.pill.hwnd, ANIM_TIMER, ANIM_MS, None)
        else:
            user32.KillTimer(self.pill.hwnd, ANIM_TIMER)
        self._anim = on

    def _set_house(self, on):
        if on == self._house or self.pill is None:
            return
        if on:
            user32.SetTimer(self.pill.hwnd, HOUSE_TIMER, HOUSE_MS, None)
        else:
            user32.KillTimer(self.pill.hwnd, HOUSE_TIMER)
        self._house = on

    def _on_anim(self):
        now = time.monotonic()
        busy = False
        if self.sheet_open or self.sheet_closing:
            busy = self._sheet_frame(now)
        elif self.session is not None:
            busy = self._draw_pill(now, force=True)
        self._set_anim(busy)

    def _on_house(self):
        now = time.monotonic()
        s = self.session
        if s is None:
            if not self.sheet_closing:
                self._set_house(False)
            return
        if self.sheet_open:
            key = (clock(now - s.started), s.hint_text(now), s.version)
            if key != self._sheet_key:
                self._place_sheet(now, relayout=True)
            return
        if (not self.compact and not self.hover and not self.press.pressed
                and now - self.last_touch >= IDLE_SHRINK_SECS):
            self.compact = True
            self.zoom.to(SHRUNK_ZOOM, SHRINK_MS, now)
            self.alpha.to(SHRUNK_ALPHA, SHRINK_MS, now)
            self._draw_pill(now, force=True)
            self._set_anim(True)

    # -- the pill -------------------------------------------------------------------
    def _touch(self, now):
        self.last_touch = now
        if self.compact:
            self.compact = False
            self.zoom.to(1.0, EXPAND_MS, now)
            self.alpha.to(1.0, EXPAND_MS, now)
            self._draw_pill(now, force=True)
            self._set_anim(True)

    def _draw_pill(self, now, force=False):
        """Paint the pill for `now`; True while something is still moving."""
        s = self.session
        if s is None or self.pill is None or self.button is None:
            return False
        frame = self.slide.frame(now) if self.slide else None
        if self.slide and frame is None:
            self.slide = None
        which, offset, calpha = frame if frame else ("new", (0.0, 0.0), 1.0)
        content = self.slide.old if (frame and which == "old") else s.content()
        zoom, alpha = self.zoom.value(now), self.alpha.value(now)
        key = (content, self.compact, round(zoom, 3), round(alpha, 3), offset, round(calpha, 3),
               self.button, self.scale)
        moving = bool(self.slide) or not self.zoom.done(now) or not self.alpha.done(now)
        if key == self._pill_key and not force:
            return moving
        self._pill_key = key
        size = pill_size(content, self.scale, self.compact)
        img = render_pill(content, self.scale, self.compact, zoom, offset, calpha, self.pal, size)
        m = pill_margin(self.scale)
        w, h = img.size[0] - 2 * m, img.size[1] - 2 * m
        gap = PILL_GAP * self.scale
        # Under the button (above it near the screen's bottom), the edge next
        # to the button fixed, so it shrinks towards the button.
        x, y, _above = place_below(self.button, (w, h), self.work, gap)
        self.pill.update(img, (x - m, y - m), alpha * 255)
        return moving

    def _act(self, action):
        s = self.session
        if s is None or s.closed:
            return
        now = time.monotonic()
        old = s.content()
        if action in ("more", "less"):
            moved = s.step(1 if action == "more" else -1)
        else:
            s.cycle(1 if action == "next" else -1)
            moved = True
        self.slide = slide_for(action, moved, old, now)
        self._touch(now)
        self._draw_pill(now, force=True)
        self._set_anim(True)

    def _pill_msg(self, hwnd, msg, wp, lp):
        if msg == _cb.WM_MOUSEACTIVATE:
            return _cb.MA_NOACTIVATE
        if msg == WM_POINTERACTIVATE:
            return PA_NOACTIVATE
        if msg == _cb.WM_TIMER:
            if wp == ANIM_TIMER:
                self._on_anim()
            elif wp == HOUSE_TIMER:
                self._on_house()
            return 0
        if self.session is None:
            return None
        if msg == _cb.WM_MOUSEMOVE:
            if not self.hover:
                self.hover = True
                tme = _cb.TRACKMOUSEEVENT(ctypes.sizeof(_cb.TRACKMOUSEEVENT), _cb.TME_LEAVE, hwnd, 0)
                user32.TrackMouseEvent(ctypes.byref(tme))
            self._touch(time.monotonic())
            if self.press.pressed and self.touch_id is None:
                act = self.press.move(_cb.CaretButton._cursor())
                if act:
                    self._act(act)
            return 0
        if msg == _cb.WM_MOUSELEAVE:
            self.hover = False
            self.last_touch = time.monotonic()
            return 0
        if msg == _cb.WM_LBUTTONDOWN:
            self.press.threshold = swipe_threshold(self.scale)
            self.press.press(_cb.CaretButton._cursor())
            self.touch_id = None
            user32.SetCapture(hwnd)
            self._touch(time.monotonic())
            return 0
        if msg == _cb.WM_LBUTTONUP:
            kind = self.press.release(_cb.CaretButton._cursor()) if self.touch_id is None else None
            if user32.GetCapture() == hwnd:
                user32.ReleaseCapture()
            if kind == "tap":
                self.open_sheet()
            return 0
        if msg == _cb.WM_CAPTURECHANGED:
            if self.touch_id is None:
                self.press.cancel()
            return 0
        if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            for act in self.wheel.add(_signed(wp >> 16), horizontal=msg == WM_MOUSEHWHEEL):
                self._act(act)
            return 0
        # Touch and pen: their own pointer messages, handled here (not left
        # to Windows, which would turn a swipe into a scroll gesture).
        if msg == WM_POINTERDOWN:
            self.touch_id = wp & 0xFFFF
            self.press.threshold = swipe_threshold(self.scale)
            self.press.press(_lparam_pt(lp))
            self._touch(time.monotonic())
            return 0
        if msg == WM_POINTERUPDATE:
            if self.touch_id == (wp & 0xFFFF) and self.press.pressed:
                act = self.press.move(_lparam_pt(lp))
                if act:
                    self._act(act)
            return 0
        if msg == WM_POINTERUP:
            if self.touch_id == (wp & 0xFFFF):
                kind = self.press.release(_lparam_pt(lp))
                self.touch_id = None
                self.last_touch = time.monotonic()
                if kind == "tap":
                    self.open_sheet()
            return 0
        if msg == WM_POINTERCAPTURECHANGED:
            if self.touch_id is not None:
                self.press.cancel()
                self.touch_id = None
            return 0
        return None

    # -- the sheet -------------------------------------------------------------------
    def open_sheet(self):
        s = self.session
        if s is None or s.closed or self.sheet_open:
            return
        now = time.monotonic()
        self.press.cancel()
        self.sheet_open, self.sheet_closing = True, False
        self._sheet_hover = self._sheet_down = None
        self.pill.hide()
        self.sheet_zoom.set(SHEET_FROM_ZOOM)
        self.sheet_alpha.set(0.0)
        self.sheet_zoom.to(1.0, SHEET_IN_MS, now, decel)
        self.sheet_alpha.to(1.0, SHEET_IN_MS, now, decel)
        self.scrim.show(0)
        self._place_sheet(now, relayout=True)
        self.sheet.to_top()
        self._raise_button()   # stays clickable (it stops the recording)
        self._set_anim(True)
        self._log("style sheet opened")

    def close_sheet(self):
        self._close_sheet(time.monotonic(), animated=True)

    def _close_sheet(self, now, animated):
        if not (self.sheet_open or self.sheet_closing):
            return
        was_open = self.sheet_open
        self.sheet_open = False
        if animated and was_open and self.sheet is not None and self.sheet.shown:
            self.sheet_closing = True
            self.sheet_zoom.to(SHEET_FROM_ZOOM, SHEET_OUT_MS, now, accel)
            self.sheet_alpha.to(0.0, SHEET_OUT_MS, now, accel)
            self._set_anim(True)
            return
        self._finish_close(now)

    def _finish_close(self, now):
        self.sheet_open = self.sheet_closing = False
        if self.sheet is not None:
            self.sheet.hide()
        if self.scrim is not None:
            self.scrim.hide()
        self._sheet_img = None
        if self.session is not None and not self.session.closed:
            # Back to the pill, full size (the phone: expandPill on close).
            self.compact = False
            self.zoom.set(1.0)
            self.alpha.set(1.0)
            self.last_touch = now
            self._draw_pill(now, force=True)
        else:
            self._set_house(False)

    def _place_sheet(self, now, relayout):
        s = self.session
        if s is None or self.sheet is None:
            return
        if relayout or self._sheet_img is None:
            self._sheet_img, self._sheet_layout = render_sheet(
                s, self.scale, self.pal, hover=self._sheet_hover, now=now)
            self._sheet_key = (clock(now - s.started), s.hint_text(now), s.version)
        sw, sh = self._sheet_layout["size"]
        x, y, _above = place_sheet(self.button, (sw, sh), self.work, SHEET_GAP * self.scale)
        self._sheet_box = (x, y)
        self._sheet_frame(now)

    def _sheet_frame(self, now):
        """One frame of the sheet (and the dim); True while animating."""
        if self._sheet_img is None or self._sheet_box is None:
            return False
        from PIL import Image
        zoom, alpha = self.sheet_zoom.value(now), self.sheet_alpha.value(now)
        img = self._sheet_img
        m = sheet_margin(self.scale)
        x, y = self._sheet_box
        if zoom < 0.999:
            W, H = img.size
            zw, zh = max(1, int(round(W * zoom))), max(1, int(round(H * zoom)))
            cx, cy = x - m + W / 2, y - m + H / 2
            frame, pos = img.resize((zw, zh), Image.BILINEAR), (cx - zw / 2, cy - zh / 2)
        else:
            frame, pos = img, (x - m, y - m)
        self.sheet.update(frame, pos, alpha * 255)
        self.scrim.show(alpha * SCRIM_ALPHA * 255)
        done = self.sheet_zoom.done(now) and self.sheet_alpha.done(now)
        if done and self.sheet_closing:
            self._finish_close(now)
            return False
        return not done

    def _sheet_pt(self):
        cx, cy = _cb.CaretButton._cursor()
        m = sheet_margin(self.scale)
        return cx - self.sheet.pos[0] - m, cy - self.sheet.pos[1] - m

    def _sheet_msg(self, hwnd, msg, wp, lp):
        if msg == _cb.WM_MOUSEACTIVATE:
            return _cb.MA_NOACTIVATE
        if msg == WM_POINTERACTIVATE:
            return PA_NOACTIVATE
        if not self.sheet_open or self._sheet_layout is None:
            return None
        if msg == _cb.WM_MOUSEMOVE:
            hit = sheet_hit(self._sheet_layout, self._sheet_pt())
            if hit != self._sheet_hover:
                if self._sheet_hover is None:
                    tme = _cb.TRACKMOUSEEVENT(ctypes.sizeof(_cb.TRACKMOUSEEVENT), _cb.TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                self._sheet_hover = hit
                self._place_sheet(time.monotonic(), relayout=True)
            return 0
        if msg == _cb.WM_MOUSELEAVE:
            if self._sheet_hover is not None:
                self._sheet_hover = None
                self._place_sheet(time.monotonic(), relayout=True)
            return 0
        if msg == _cb.WM_SETCURSOR:
            hit = sheet_hit(self._sheet_layout, self._sheet_pt())
            cur = _cb.IDC_HAND if hit else IDC_ARROW
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(cur)))
            return 1
        if msg == _cb.WM_LBUTTONDOWN:
            self._sheet_down = sheet_hit(self._sheet_layout, self._sheet_pt())
            return 0
        if msg == _cb.WM_LBUTTONUP:
            hit = sheet_hit(self._sheet_layout, self._sheet_pt())
            down, self._sheet_down = self._sheet_down, None
            if hit is not None and hit == down:
                self._sheet_action(hit)
            return 0
        return None

    def _sheet_action(self, hit):
        s = self.session
        now = time.monotonic()
        if hit[0] == "chip" and s is not None:
            if s.pick(hit[1], hit[2], now):
                self._log("style: picked %s=%s" % (hit[1], hit[2]))
            self._place_sheet(now, relayout=True)
        elif hit[0] in ("close", "done"):
            self._close_sheet(now, animated=True)

    def _scrim_msg(self, hwnd, msg, wp, lp):
        if msg == _cb.WM_MOUSEACTIVATE:
            return _cb.MA_NOACTIVATE
        if msg == WM_POINTERACTIVATE:
            return PA_NOACTIVATE
        if msg in (_cb.WM_LBUTTONDOWN, WM_RBUTTONDOWN):
            self._scrim_down = True
            return 0
        if msg in (_cb.WM_LBUTTONUP, _cb.WM_RBUTTONUP):
            # Tap outside: close; the click itself goes nowhere.
            if self._scrim_down:
                self._scrim_down = False
                self.close_sheet()
            return 0
        if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            return 0
        return None
