"""Floating mic button at the text cursor (like the phone app's floating mic).

When you click or tab into a text field anywhere in Windows, a small button
with the app logo pops up beside the text caret. Click it to start dictating,
click again to stop; the listener pastes the result just like the hotkey.
Drag it to move it: the spot you drop it on is remembered as an offset from
the default spot, so it shows up there next time too. Right-click for
"Hide for now" / "Hide in <app>" / "Turn off".

It looks exactly like the phone app's button (the same two pictures, a
rounded-square tile):
  idle      light lavender tile, black waveform (assets/mic_idle.png)
  recording dark grey tile, red waveform (assets/mic_recording.png)
  working   the idle tile at half opacity
Light vs dark tile tells the states apart without colour (Dom is red/green
colour blind).

While the idle button is up (2 s fully visible, then 3 s see-through) a line
in the accent colour runs once around the tile, clockwise from the top centre,
and closes the loop just as the button disappears: a visible countdown.
Hovering pauses it; showing the button again starts it over. While recording
or working the button stays put (caret moves are ignored) and has no line.

Runs in its own thread with its own Win32 layered window and message loop
(ctypes, not Tk). The window is WS_EX_NOACTIVATE and answers WM_MOUSEACTIVATE
with MA_NOACTIVATE, so clicking it never takes focus from the text field.

Trigger detection (cheap when idle):
  * SetWinEventHook(EVENT_OBJECT_FOCUS), out of context: keyboard focus moved.
  * A 50 ms timer watches the left mouse button (GetAsyncKeyState); on release
    the focused element is checked and the button shown if the click landed
    inside an editable field. UI Automation is only queried after one of
    those two events, never on a blind poll.
  * While the button is up, GetLastInputInfo + an unmoved pointer means the
    user is typing -> hide.
  * A second, faster timer (ANIM_MS) runs only while the countdown line is
    moving; it is killed when the button hides, is hovered or is pinned.

.env (re-read when the file's mtime moves):
  FLOATING_MIC_BUTTON     on/off (default true)
  FLOATING_MIC_HIDE_APPS  "slack.exe,code.exe": never show while one of these
                          is the foreground app ("Hide in <app>" adds to it)
  FLOATING_MIC_SPOT       "dx,dy": where it was last dragged to, relative to
                          the default spot beside the caret (96-DPI pixels)
  FLOATING_MIC_OFFSET     the same, saved by older versions relative to the
                          OLD default spot (right beside the caret). Read only
                          when FLOATING_MIC_SPOT is missing, and translated to
                          the new default (see LEGACY_SHIFT), so a button
                          dragged before the change stays where it was put.
"""
import ctypes
import os
import threading
import time
import traceback
from ctypes import wintypes

ENV_KEY = "FLOATING_MIC_BUTTON"
ENV_DEFAULT = True
HIDE_KEY = "FLOATING_MIC_HIDE_APPS"   # "slack.exe,code.exe": never show in these
SPOT_KEY = "FLOATING_MIC_SPOT"        # "dx,dy" (96-DPI px) from the default spot
LEGACY_OFFSET_KEY = "FLOATING_MIC_OFFSET"  # older: "dx,dy" from the OLD default spot
OFFSET_KEY = SPOT_KEY                 # what a drag writes

# Look
BASE_SIZE = 36            # button (the picture's square) at 100% scaling (96 DPI)
SUPERSAMPLE = 4
STATES = ("idle", "recording", "working")
WORKING_OPACITY = 0.5     # the phone shows "processing" as the idle tile at 50%
# Countdown line: width and its gap from the tile, at 100% scaling.
TRAIL_W = 2.5
TRAIL_GAP = 2.0
TRAIL_FALLBACK = "#8C9EFF"  # theme.ACCENT's default, if theme can't be loaded

# Default spot beside the caret, as fractions of the button size d (so it is
# the same at every DPI): its left edge is gap + 0.75 d right of the caret,
# its top 2/36 d above the line's centred position. This is where Dom
# dragged it to (FLOATING_MIC_OFFSET="27,-2" on top of the old default,
# which was gap + 0 d right, centred): 27/36 = 0.75, -2/36.
CARET_DX = 27 / 36
CARET_DY = -2 / 36
# A FLOATING_MIC_OFFSET saved against the old default spot, minus this,
# is the same place measured from the new default (96-DPI px).
LEGACY_SHIFT = (27, -2)
COLORS = {  # the plain fallback drawing (pictures missing)
    "idle": (0x2F, 0xB7, 0x9A),
    "recording": (0xD9, 0x36, 0x3E),
    "working": (0x5A, 0x60, 0x6B),
}

# Timing (seconds)
FULL_SECS = 2.0           # fully visible
FADED_SECS = 3.0          # then see-through for this long, then hidden
FADED_ALPHA = 0.35
RAMP_SECS = 0.25          # length of the fade from full to faded
TYPING_GRACE = 0.35       # input this soon after showing is the click/Tab itself
TYPING_HIDE_DELAY = 0.30  # a hotkey press may be about to start a recording
TICK_MS = 50
ANIM_MS = 25              # countdown line frame rate while it moves (40 fps)
DISABLED_TICK_MS = 1000
ENV_POLL_SECS = 2.0


# --- pure logic (unit-tested) ---------------------------------------------

def base_point(anchor, d, gap, at_element=False):
    """Where the button goes with no drag offset, before any edge handling.

    Caret: right of it (gap + CARET_DX d), about centred on the line
    (CARET_DY d up). Proportional to d, so it is the same spot at every DPI.
    Element fallback (no caret found): just inside the field's right edge;
    near the top of a tall field (a text area).
    """
    l, t, r, b = anchor
    if at_element:
        h = b - t
        y = (t + b) / 2 - d / 2 if h < d * 2.5 else t + gap
        return r - d - gap, y
    return r + gap + CARET_DX * d, (t + b) / 2 - d / 2 + CARET_DY * d


def clamp_to_work(x, y, d, work):
    """Keep a d x d button fully inside `work` (the monitor work area)."""
    wl, wt, wr, wb = work
    return max(wl, min(x, wr - d)), max(wt, min(y, wb - d))


def place(anchor, d, work, gap, at_element=False, offset=(0, 0)):
    """Top-left of a d x d button next to `anchor` (l, t, r, b), kept inside
    `work`. `offset` (px) is where the user dragged it, relative to the
    default spot.

    At a caret, a spot that doesn't fit is mirrored to the other side of the
    caret (right of it -> left of it, below -> above) before falling back to
    clamping, so the button never covers the text you're typing.
    """
    l, t, r, b = anchor
    wl, wt, wr, wb = work
    bx, by = base_point(anchor, d, gap, at_element)
    x, y = bx + offset[0], by + offset[1]
    if not at_element:
        if x < wl or x + d > wr:
            mx = (l + r) - (x + d)
            if wl <= mx and mx + d <= wr:
                x = mx
        if y < wt or y + d > wb:
            my = (t + b) - (y + d)
            if wt <= my and my + d <= wb:
                y = my
    x, y = clamp_to_work(x, y, d, work)
    return int(round(x)), int(round(y))


def offset_from_drop(drop, anchor, d, gap, at_element, scale):
    """The drag offset to remember (in 96-DPI units, so it means the same on
    every monitor) for a button dropped with its top-left at `drop`."""
    bx, by = base_point(anchor, d, gap, at_element)
    return int(round((drop[0] - bx) / scale)), int(round((drop[1] - by) / scale))


def offset_px(offset_dip, scale):
    return int(round(offset_dip[0] * scale)), int(round(offset_dip[1] * scale))


def drag_threshold(scale):
    """Pointer travel (px) before a press becomes a drag; less is a click."""
    return max(4, int(round(4 * scale)))


class DragTracker:
    """Press -> move -> release on the button: a click, or a drag.

    Positions are screen pixels. press() records where the pointer went down
    and where the window was; move() returns the window's new top-left once
    the pointer has travelled past `threshold` (None before that); release()
    says ("click", None) or ("drag", final_top_left).
    """

    def __init__(self, threshold=4):
        self.threshold = threshold
        self.start_pt = None
        self.start_pos = None
        self.pos = None
        self.dragging = False

    @property
    def pressed(self):
        return self.start_pt is not None

    def press(self, pt, win_pos):
        self.start_pt, self.start_pos, self.pos = pt, win_pos, win_pos
        self.dragging = False

    def move(self, pt, clamp=None):
        if not self.pressed:
            return None
        dx, dy = pt[0] - self.start_pt[0], pt[1] - self.start_pt[1]
        if not self.dragging and max(abs(dx), abs(dy)) < self.threshold:
            return None
        self.dragging = True
        pos = (self.start_pos[0] + dx, self.start_pos[1] + dy)
        if clamp:
            pos = clamp(pos)
        self.pos = pos
        return pos

    def release(self, pt=None, clamp=None):
        if not self.pressed:
            return None, None
        if pt is not None:
            self.move(pt, clamp)
        result = ("drag", self.pos) if self.dragging else ("click", None)
        self.cancel()
        return result

    def cancel(self):
        self.start_pt = self.start_pos = self.pos = None
        self.dragging = False


# --- .env settings -----------------------------------------------------------

def env_value(raw):
    """A .env value without its quotes or trailing ' # comment'."""
    v = (raw or "").strip()
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        return v[1:end] if end > 0 else v[1:]
    return v.split(" #", 1)[0].strip()


def _normalize_app(name):
    name = os.path.basename((name or "").strip().strip('"').strip("'")).lower()
    if name and not name.endswith(".exe"):
        name += ".exe"
    return name if name != ".exe" else ""


def parse_app_list(raw):
    """FLOATING_MIC_HIDE_APPS -> ['slack.exe', ...] (normalized like
    app_styles.normalize_app, duplicates dropped, order kept)."""
    out = []
    for part in env_value(raw).replace(";", ",").split(","):
        app = _normalize_app(part)
        if app and app not in out:
            out.append(app)
    return out


def format_app_list(apps):
    s = ",".join(apps)
    return '"%s"' % s if (" " in s or "#" in s) else s


def add_app(raw, app):
    """The FLOATING_MIC_HIDE_APPS value with `app` added (no duplicates)."""
    apps = parse_app_list(raw)
    app = _normalize_app(app)
    if app and app not in apps:
        apps.append(app)
    return format_app_list(apps)


def parse_offset(raw):
    """'dx,dy' -> (dx, dy) ints, or None when missing/garbled."""
    v = env_value(raw)
    if not v:
        return None
    try:
        dx, dy = (int(round(float(p))) for p in v.split(","))
    except ValueError:
        return None
    if abs(dx) > 4000 or abs(dy) > 4000:
        return None
    return dx, dy


def format_offset(offset):
    return '"%d,%d"' % (offset[0], offset[1])


def saved_offset(values):
    """The drag offset from the default spot, from .env values, or None.

    FLOATING_MIC_SPOT is measured from the current default spot. Without
    it, an older FLOATING_MIC_OFFSET (measured from the old default, right
    beside the caret) is moved over by LEGACY_SHIFT so the button still lands
    exactly where it was dragged to. Nothing is written back: the first drag
    saves FLOATING_MIC_SPOT, which wins from then on."""
    spot = parse_offset(values.get(SPOT_KEY))
    if spot is not None:
        return spot
    old = parse_offset(values.get(LEGACY_OFFSET_KEY))
    if old is None:
        return None
    return old[0] - LEGACY_SHIFT[0], old[1] - LEGACY_SHIFT[1]


def read_config(env_path):
    """{enabled, hide_apps, offset} from the .env (offset None if not set)."""
    import env_file
    values = env_file.read(env_path) if env_path else {}
    return {
        "enabled": env_file.as_bool(env_value(values.get(ENV_KEY)), ENV_DEFAULT),
        "hide_apps": parse_app_list(values.get(HIDE_KEY)),
        "offset": saved_offset(values),
    }


def app_label(exe_path, description=""):
    """What the menu calls an app: its file description ('Slack', 'Google
    Chrome') when it has a short one, else the file name ('Notepad')."""
    d = (description or "").strip()
    if d and len(d) <= 32:
        return d
    stem = os.path.splitext(os.path.basename(exe_path or ""))[0]
    return stem[:1].upper() + stem[1:] if stem else ""


class FadeTimeline:
    """When is the button fully visible, see-through, or hidden.

    Time is passed in (seconds, monotonic), so tests can drive it.
    alpha(now) -> 1.0 full, FADED_ALPHA see-through, 0.0 hidden.
    progress(now) -> 0..1 through the countdown (1 = the moment it hides),
    or None when there is no countdown (hidden or pinned).

    Hovering pauses the countdown (fully visible meanwhile) and leaving
    resumes it where it was; dropping a drag or showing again restarts it.
    """

    def __init__(self, full=FULL_SECS, faded=FADED_SECS, faded_alpha=FADED_ALPHA,
                 ramp=RAMP_SECS):
        self.full, self.faded, self.faded_alpha, self.ramp = full, faded, faded_alpha, ramp
        self.shown_at = None
        self.hover = False
        self.pinned = False
        self.dragging = False
        self.paused_at = None     # hover started here (countdown frozen)

    @property
    def visible(self):
        return self.shown_at is not None

    @property
    def total(self):
        return self.full + self.faded

    @property
    def counting(self):
        """The countdown is running (not paused, held or pinned)."""
        return self.visible and not (self.pinned or self.hover or self.dragging)

    def show(self, now):
        self.shown_at = now
        self.paused_at = now if self.hover else None

    def hide(self):
        self.shown_at = None
        self.hover = False
        self.dragging = False
        self.paused_at = None

    def set_hover(self, hover, now):
        if hover and not self.hover:
            self.paused_at = now
        elif not hover and self.hover:
            if self.visible and self.paused_at is not None:
                self.shown_at += now - self.paused_at   # resume where it was
            self.paused_at = None
        self.hover = hover

    def pin(self, pinned, now):
        self.pinned = pinned
        if self.visible and not pinned:
            self.shown_at = now
            if self.hover:
                self.paused_at = now

    def drag(self, dragging, now):
        """Held fully visible while being dragged; dropping restarts the
        2 s + 3 s countdown."""
        self.dragging = dragging
        if self.visible:
            self.shown_at = now
            if self.hover:
                self.paused_at = now

    def progress(self, now):
        if self.shown_at is None or self.pinned:
            return None
        if self.dragging:
            return 0.0
        t = (self.paused_at if self.hover and self.paused_at is not None else now) - self.shown_at
        return max(0.0, min(1.0, t / self.total)) if self.total > 0 else 1.0

    def alpha(self, now):
        if self.shown_at is None:
            return 0.0
        if self.pinned or self.hover or self.dragging:
            return 1.0
        t = now - self.shown_at
        if t < self.full:
            return 1.0
        if t < self.full + self.ramp:
            k = (t - self.full) / self.ramp
            return 1.0 - (1.0 - self.faded_alpha) * k
        if t < self.full + self.faded:
            return self.faded_alpha
        self.shown_at = None
        return 0.0


def lighten(rgb, k=0.12):
    return tuple(int(round(c + (255 - c) * k)) for c in rgb)


def darken(rgb, k=0.22):
    return tuple(int(round(c * (1 - k))) for c in rgb)


_ICON_FONTS = (r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf")
_MIC_GLYPH = "\uE720"


def _icon_font(px):
    from PIL import ImageFont
    for path in _ICON_FONTS:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, px)
        except Exception:
            continue
    return None


def margin_for(d):
    return max(3, int(round(d * 0.2)))


_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
# The phone app's two button pictures (512 px RGBA, rounded-square tile).
IMAGE_PATHS = {
    "idle": os.path.join(_ASSETS, "mic_idle.png"),
    "recording": os.path.join(_ASSETS, "mic_recording.png"),
}
# Where the tile sits in each 512 px picture, as fractions (l, t, r, b), and
# its corner radius as a fraction of the picture (measured from the files).
TILE_BOX = {
    "idle": (23.4 / 512, 22.9 / 512, 487.5 / 512, 488.5 / 512),
    "recording": (17.4 / 512, 17.4 / 512, 493.6 / 512, 494.5 / 512),
}
TILE_RADIUS = {"idle": 136 / 512, "recording": 142 / 512}
# The plain fallback (pictures missing) is a circle filling the square.
PLAIN_BOX, PLAIN_RADIUS = (0.0, 0.0, 1.0, 1.0), 0.5

_image_cache = {}


def _image(state):
    """The phone picture for `state` (RGBA 512 px), or None if missing."""
    key = "recording" if state == "recording" else "idle"
    if key not in _image_cache:
        try:
            from PIL import Image
            _image_cache[key] = Image.open(IMAGE_PATHS[key]).convert("RGBA")
        except Exception:
            _image_cache[key] = None
    return _image_cache[key]


def tile_shape(state, d, plain=False):
    """The tile's box (l, t, r, b) and corner radius in an image rendered for a
    d-px button (canvas d + 2 * margin_for(d), tile inside the d square)."""
    m = margin_for(d)
    if plain:
        box, rad = PLAIN_BOX, PLAIN_RADIUS
    else:
        key = "recording" if state == "recording" else "idle"
        box, rad = TILE_BOX[key], TILE_RADIUS[key]
    return tuple(m + v * d for v in box), rad * d


def render(state, hover, d):
    """RGBA Pillow image: the d-px button (plus a margin for the shadow and
    the countdown line) for `state`, exactly the phone app's pictures:

    idle      the light lavender tile with the black waveform
    recording the dark grey tile with the red waveform
    working   the idle tile at half opacity

    Scaled down from 512 px with Lanczos; a soft drop shadow under the tile
    (a little stronger on hover) so it stands out on white pages.
    """
    pic = _image(state)
    if pic is None:
        return render_plain(state, hover, d)
    from PIL import Image
    m = margin_for(d)
    W = d + 2 * m
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    box, rad = tile_shape(state, d)
    _shadow(img, box, rad, d, hover)
    img.alpha_composite(pic.resize((d, d), Image.LANCZOS), (m, m))
    if state == "working":
        r, g, b, a = img.split()
        a = a.point(lambda v: int(round(v * WORKING_OPACITY)))
        img = Image.merge("RGBA", (r, g, b, a))
    return img


def _shadow(img, box, rad, d, hover=False):
    """Soft shadow under a rounded rect `box` (radius `rad`), drawn into img."""
    from PIL import Image, ImageDraw, ImageFilter
    s = SUPERSAMPLE
    W = img.size[0]
    sh = Image.new("L", (W * s, W * s), 0)
    l, t, r, b = (v * s for v in box)
    dy = s * max(1.0, d / 30)
    ImageDraw.Draw(sh).rounded_rectangle((l, t + dy, r, b + dy), radius=rad * s,
                                         fill=110 if hover else 80)
    sh = sh.filter(ImageFilter.GaussianBlur(s * max(1.2, d / 20)))
    sh = sh.resize((W, W), Image.LANCZOS)
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    layer.putalpha(sh)
    img.alpha_composite(layer)


# --- the countdown line --------------------------------------------------------

def _rr_segments(box, radius):
    """A rounded rectangle's outline as segments, from the top centre
    clockwise (screen coordinates, y down): ("line", start, end) or
    ("arc", centre, R, angle0, angle1) in degrees."""
    l, t, r, b = box
    R = max(0.0, min(radius, (r - l) / 2, (b - t) / 2))
    cx = (l + r) / 2
    return [
        ("line", (cx, t), (r - R, t)),
        ("arc", (r - R, t + R), R, -90, 0),
        ("line", (r, t + R), (r, b - R)),
        ("arc", (r - R, b - R), R, 0, 90),
        ("line", (r - R, b), (l + R, b)),
        ("arc", (l + R, b - R), R, 90, 180),
        ("line", (l, b - R), (l, t + R)),
        ("arc", (l + R, t + R), R, 180, 270),
        ("line", (l + R, t), (cx, t)),
    ]


def _seg_length(seg):
    import math
    if seg[0] == "line":
        return math.hypot(seg[2][0] - seg[1][0], seg[2][1] - seg[1][1])
    return seg[2] * math.radians(seg[4] - seg[3])


def _seg_point(seg, k):
    import math
    if seg[0] == "line":
        (x0, y0), (x1, y1) = seg[1], seg[2]
        return x0 + (x1 - x0) * k, y0 + (y1 - y0) * k
    (ox, oy), R, a0, a1 = seg[1], seg[2], seg[3], seg[4]
    a = math.radians(a0 + (a1 - a0) * k)
    return ox + R * math.cos(a), oy + R * math.sin(a)


def path_length(box, radius):
    return sum(_seg_length(sg) for sg in _rr_segments(box, radius))


def rounded_rect_path(box, radius, progress=1.0, step=1.0):
    """Points along a rounded rectangle's outline, from the top centre
    clockwise (screen coordinates, y down), covering `progress` (0..1) of its
    length; about one point every `step` px, both ends included."""
    import math
    segs = _rr_segments(box, radius)
    want = max(0.0, min(1.0, progress)) * sum(_seg_length(sg) for sg in segs)
    pts = [segs[0][1]]
    done = 0.0
    for seg in segs:
        n = _seg_length(seg)
        if n <= 0:
            continue
        upto = min(n, want - done)
        if upto <= 1e-9:
            break
        k = max(1, int(math.ceil(upto / max(step, 1e-3))))
        for i in range(1, k + 1):
            pts.append(_seg_point(seg, upto * i / k / n))
        done += n
    return pts


def trail_shape(state, d, plain=False):
    """Box, corner radius and line width (px) of the countdown line: the
    tile's outline grown by the gap plus half the line width."""
    k = d / BASE_SIZE
    w = max(1.5, TRAIL_W * k)
    grow = TRAIL_GAP * k + w / 2
    (l, t, r, b), rad = tile_shape(state, d, plain)
    return (l - grow, t - grow, r + grow, b + grow), rad + grow, w


def accent_rgb(env_path=None):
    """theme.ACCENT as (r, g, b). With `env_path`, the accent picked in that
    .env right now (UI_ACCENT), so a change in Settings shows without a
    restart."""
    hexval = TRAIL_FALLBACK
    try:
        import theme
        hexval = theme.ACCENT or TRAIL_FALLBACK
        if env_path:
            hexval = theme.ACCENTS[theme.accent_name(env_path)][0] or hexval
    except Exception:
        pass
    try:
        h = hexval.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        h = TRAIL_FALLBACK.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def render_trail(progress, d, color, state="idle", plain=False):
    """Transparent RGBA image (same canvas as render()) with the countdown
    line `progress` (0..1) of the way round the tile, from the top centre
    clockwise, with round ends. Supersampled for smooth edges."""
    from PIL import Image, ImageDraw
    s = SUPERSAMPLE
    W = d + 2 * margin_for(d)
    out = Image.new("RGBA", (W, W), tuple(color) + (0,))
    if progress is None or progress <= 0:
        return out
    box, rad, w = trail_shape(state, d, plain)
    pts = rounded_rect_path(tuple(v * s for v in box), rad * s, progress, step=s * 0.75)
    mask = Image.new("L", (W * s, W * s), 0)
    dr = ImageDraw.Draw(mask)
    ws = w * s
    if len(pts) > 1:
        dr.line(pts, fill=255, width=max(1, int(round(ws))), joint="curve")
    h = ws / 2
    for x, y in (pts[0], pts[-1]):
        dr.ellipse((x - h, y - h, x + h, y + h), fill=255)
    out.putalpha(mask.resize((W, W), Image.LANCZOS))
    return out


def compose(state, hover, d, progress=None, color=None, base=None):
    """One frame: render() (or `base`, its cached result) plus the countdown
    line when `progress` is not None. Idle only: never while recording or
    working."""
    img = base if base is not None else render(state, hover, d)
    if progress is None or state != "idle":
        return img
    plain = _image(state) is None
    img = img.copy()
    img.alpha_composite(render_trail(progress, d, color or accent_rgb(), state, plain))
    return img


def render_plain(state, hover, d):
    """Fallback when the pictures are missing: a coloured circle with a white
    microphone / stop square / three dots."""
    from PIL import Image, ImageDraw
    s = SUPERSAMPLE
    m = margin_for(d)
    W = (d + 2 * m) * s
    D = d * s
    o = m * s  # circle top-left inside the canvas
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    base = COLORS.get(state, COLORS["idle"])
    fill = lighten(base) if hover else base
    dr = ImageDraw.Draw(img)
    dr.ellipse((o, o, o + D - 1, o + D - 1), fill=fill + (255,),
               outline=darken(base) + (255,), width=s)

    cx = cy = o + D / 2
    white = (255, 255, 255, 255)
    if state == "recording":
        half = D * 0.17
        dr.rounded_rectangle((cx - half, cy - half, cx + half, cy + half),
                             radius=D * 0.05, fill=white)
    elif state == "working":
        r = D * 0.065
        for dx in (-D * 0.2, 0, D * 0.2):
            dr.ellipse((cx + dx - r, cy - r, cx + dx + r, cy + r), fill=white)
    else:
        font = _icon_font(int(D * 0.48))
        if font is not None:
            box = dr.textbbox((0, 0), _MIC_GLYPH, font=font)
            gw, gh = box[2] - box[0], box[3] - box[1]
            dr.text((cx - gw / 2 - box[0], cy - gh / 2 - box[1]), _MIC_GLYPH,
                    font=font, fill=white)
        else:
            _draw_mic_shapes(dr, cx, cy, D, white)
    out = Image.new("RGBA", (d + 2 * m, d + 2 * m), (0, 0, 0, 0))
    box, rad = tile_shape(state, d, plain=True)
    _shadow(out, box, rad, d, hover)
    out.alpha_composite(img.resize((d + 2 * m, d + 2 * m), Image.LANCZOS))
    return out


def _draw_mic_shapes(dr, cx, cy, D, color):
    """A microphone from plain shapes (when no icon font is installed)."""
    w = D * 0.16
    top, bot = cy - D * 0.24, cy + D * 0.05
    dr.rounded_rectangle((cx - w, top, cx + w, bot), radius=w, fill=color)
    lw = max(2, int(D * 0.045))
    r = D * 0.25
    dr.arc((cx - r, cy - D * 0.2 - r * 0.2, cx + r, cy + D * 0.13), 20, 160, fill=color, width=lw)
    dr.line((cx, cy + D * 0.13, cx, cy + D * 0.24), fill=color, width=lw)
    dr.line((cx - D * 0.1, cy + D * 0.24, cx + D * 0.1, cy + D * 0.24), fill=color, width=lw)


def premultiplied_bgra(img):
    """Pillow RGBA -> bytes in the premultiplied BGRA that UpdateLayeredWindow wants."""
    import numpy as np
    a = np.asarray(img, dtype=np.uint16)
    alpha = a[..., 3:4]
    rgb = (a[..., :3] * alpha + 127) // 255
    out = np.empty(a.shape, dtype=np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = a[..., 3]
    return out.tobytes()


# --- Win32 ---------------------------------------------------------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                ("lPrivate", wintypes.DWORD)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                       ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SetCursor.argtypes = [wintypes.HANDLE]
user32.SetCursor.restype = wintypes.HANDLE
user32.SetCapture.argtypes = [wintypes.HWND]
user32.SetCapture.restype = wintypes.HWND
user32.GetCapture.restype = wintypes.HWND
user32.ReleaseCapture.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CAPTURECHANGED = 0x0215
WM_MOUSEACTIVATE = 0x0021
WM_MOUSELEAVE = 0x02A3
WM_NULL = 0x0000
WM_SETCURSOR = 0x0020
MA_NOACTIVATE = 3
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1
EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
VK_LBUTTON = 0x01
TME_LEAVE = 0x2
MF_STRING = 0x0
MF_SEPARATOR = 0x800
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2
IDC_HAND = 32649
IDC_SIZEALL = 32646
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

ANIM_TIMER = 2   # timer id of the countdown line's frames (1 is the tick)

MENU_HIDE = 1
MENU_TURN_OFF = 2
MENU_HIDE_APP = 3

_CLASS_NAME = "WhisperUnchainedCaretMic"


def _log_error(log_path, what):
    try:
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("--- %s\n[caret button] %s\n%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), what, traceback.format_exc()))
    except Exception:
        pass


def _process_image(pid):
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _file_description(path):
    """The FileDescription in an exe's version info ('Slack'), or ''."""
    try:
        ver = ctypes.windll.version
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        langs = []
        if (ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(n))
                and n.value >= 4):
            arr = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))
            langs.append("%04x%04x" % (arr[0], arr[1]))
        langs += ["040904b0", "040904e4", "000004b0"]
        for lang in langs:
            q = r"\StringFileInfo\%s\FileDescription" % lang
            if ver.VerQueryValueW(buf, q, ctypes.byref(ptr), ctypes.byref(n)) and n.value:
                return ctypes.wstring_at(ptr, n.value).split(chr(0))[0].strip()
    except Exception:
        pass
    return ""


def _window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _window_exe_path(hwnd):
    """Full path of the program that owns `hwnd`. For a Store app (its frame
    belongs to ApplicationFrameHost.exe) the app inside the frame."""
    pid = _window_pid(hwnd)
    path = _process_image(pid) if pid else ""
    if os.path.basename(path).lower() == "applicationframehost.exe":
        found = []

        def cb(child, _l):
            cpid = _window_pid(child)
            if cpid and cpid != pid:
                found.append(cpid)
                return False
            return True
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
        user32.EnumChildWindows(hwnd, proc, 0)
        if found:
            path = _process_image(found[0]) or path
    return path


def _force_foreground(hwnd):
    """SetForegroundWindow that works from a background thread: attach to the
    current foreground thread's input queue for the moment of the switch."""
    try:
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_tid, False)
    except Exception:
        pass


class CaretButton:
    """The floating mic button. `on_click()` is called on a worker thread.

    Thread-safe from outside: set_state("idle" | "recording" | "working"),
    stop(). Everything else runs on the button's own thread.
    """

    def __init__(self, on_click, env_path=None, log_path=None, enabled=None, log=None):
        self.on_click = on_click
        self.env_path = env_path
        self.log_path = log_path
        self._forced_enabled = enabled   # tests/demo: ignore .env
        self._log = log or (lambda msg: None)
        self.hwnd = None
        self.thread = None
        self._state = "idle"             # requested by the listener
        self._seen_state = "idle"        # last state the tick acted on
        self._drawn = None               # (state, hover, size, alpha) last painted
        self.timeline = FadeTimeline()
        self._size = BASE_SIZE
        self._pos = (0, 0)
        self._enabled = True
        self._pending = None             # (due, reason, click_pt)
        self._pending_hide = None
        self._lbutton = False
        self._down_pt = None
        self._down_own = False
        self._last_input = 0
        self._input_cursor = None
        self._shown_mono = 0.0
        self._next_env_poll = 0.0
        self._next_topmost = 0.0
        self._hook = None
        self._owner = None
        self._menu_open = False
        self._focus_key = None
        self._fg = None
        self._timer_ms = None
        self._mem_dc = None
        self._bmp = None
        self._old_bmp = None
        self._bmp_key = None             # canvas size of the DIB in _bmp
        self._bits = None                # the DIB's pixel memory
        self._base = None                # ((state, hover, size), render()) cache
        self._anim = False               # ANIM_TIMER running
        self._accent = None              # countdown line colour (r, g, b)
        self._inspector = None
        self._own_pid = os.getpid()
        # Dragging. The offset (96-DPI px from the default spot) lives for the
        # session and in .env, so a listener restart keeps it.
        self._drag = DragTracker()
        self._up_since = None            # button seen physically up mid-press (tick fallback)
        self._offset = (0, 0)
        self._placed = None              # (anchor, at_element, work, scale) of the last show
        self._hide_apps = []             # FLOATING_MIC_HIDE_APPS, normalized
        self._env_mtime = object()       # "never read"
        self._fg_app = (None, "")        # (hwnd, "slack.exe"): last foreground app looked up
        self._ready = threading.Event()
        # Keep ctypes callbacks alive for the window's lifetime.
        self._wndproc = WNDPROC(self._wnd_proc)
        self._eventproc = WINEVENTPROC(self._on_win_event)

    # -- public, any thread ---------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._run, name="caret-button", daemon=True)
        self.thread.start()
        self._ready.wait(5)
        return self

    @property
    def state(self):
        """What the listener last asked for: "idle", "recording" or "working"
        (a click on the button also sets "recording" straight away)."""
        return self._state

    @property
    def visible(self):
        """True while the button is on screen (fully or see-through)."""
        return self.timeline.visible

    def set_state(self, state):
        """Called by the listener: "recording", "working" or "idle" (done)."""
        self._state = state
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_TIMER, 1, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # -- thread ----------------------------------------------------------
    def _run(self):
        try:
            try:
                user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            except Exception:
                pass
            import text_focus
            self._inspector = text_focus.FocusInspector()
            self._create_window()
        except Exception:
            _log_error(self.log_path, "start failed")
            self._ready.set()
            return
        self._ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        wc.lpszClassName = _CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))  # fails harmlessly if registered
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, _CLASS_NAME, "Whisper Unchained mic", WS_POPUP,
                                           0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        self._mem_dc = gdi32.CreateCompatibleDC(None)
        self._apply_enabled(self._read_enabled(), force=True)

    def _menu_owner(self):
        if not self._owner:
            hinst = kernel32.GetModuleHandleW(None)
            self._owner = user32.CreateWindowExW(WS_EX_TOOLWINDOW, _CLASS_NAME, "", WS_POPUP,
                                                 -32000, -32000, 1, 1, None, None, hinst, None)
        return self._owner

    # -- enable / disable -----------------------------------------------
    def _read_enabled(self):
        """On/off, re-reading .env (and the hide list and drag offset in it)
        only when its mtime has moved."""
        enabled = ENV_DEFAULT
        if self.env_path:
            try:
                mtime = os.stat(self.env_path).st_mtime_ns
            except OSError:
                mtime = None
            if mtime != self._env_mtime:
                self._env_mtime = mtime
                try:
                    cfg = read_config(self.env_path)
                    self._env_enabled = cfg["enabled"]
                    self._hide_apps = cfg["hide_apps"]
                    if cfg["offset"] is not None:
                        self._offset = cfg["offset"]
                    self._accent = None   # UI_ACCENT may have changed too
                    self._log("config: hide=%r offset=%r" % (self._hide_apps, self._offset))
                except Exception:
                    _log_error(self.log_path, "reading .env")
            enabled = getattr(self, "_env_enabled", ENV_DEFAULT)
        if self._forced_enabled is not None:
            return self._forced_enabled
        return enabled

    def _write_env(self, key, value):
        if not self.env_path:
            return
        try:
            import env_file
            env_file.set_key(self.env_path, key, value)
        except Exception:
            _log_error(self.log_path, "writing .env")

    def _apply_enabled(self, enabled, force=False):
        if enabled == self._enabled and not force:
            return
        self._enabled = enabled
        if enabled:
            if not self._hook:
                self._hook = user32.SetWinEventHook(
                    EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, None, self._eventproc, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            self._set_timer(TICK_MS)
        else:
            if self._hook:
                user32.UnhookWinEvent(self._hook)
                self._hook = None
            self._hide()
            self._set_timer(DISABLED_TICK_MS)
        self._log("enabled=%s" % enabled)

    def _set_timer(self, ms):
        if ms != self._timer_ms:
            user32.SetTimer(self.hwnd, 1, ms, None)
            self._timer_ms = ms

    # -- window procedure -----------------------------------------------
    def _wnd_proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_TIMER:
                if wp == ANIM_TIMER:
                    self._paint()     # countdown line frame
                else:
                    self._tick()
                return 0
            if msg == WM_MOUSEMOVE and hwnd == self.hwnd:
                if self._drag.pressed:
                    self._drag_move()
                if not self.timeline.hover:
                    self.timeline.set_hover(True, time.monotonic())
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self._paint()
                return 0
            if msg == WM_MOUSELEAVE:
                self.timeline.set_hover(False, time.monotonic())
                self._paint()
                return 0
            if msg == WM_LBUTTONDOWN and hwnd == self.hwnd:
                self._press()
                return 0
            if msg == WM_LBUTTONUP and hwnd == self.hwnd:
                self._release()
                return 0
            if msg == WM_CAPTURECHANGED and hwnd == self.hwnd:
                # Capture taken away mid-press (a menu, Alt+Tab...): finish
                # with what we have rather than leave a half-made drag.
                if self._drag.pressed and not self._releasing:
                    self._release(click_ok=False)
                return 0
            if msg == WM_RBUTTONUP:
                self._context_menu()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                if self._hook:
                    user32.UnhookWinEvent(self._hook)
                    self._hook = None
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            _log_error(self.log_path, "window message %#x" % msg)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _on_win_event(self, hook, event, hwnd, id_object, id_child, thread, ms):
        # Called from our message loop. Defer the (slower) UIA query to the
        # timer so a burst of focus events costs one lookup.
        try:
            if not self._enabled or not hwnd:
                return
            self._pending = (time.monotonic() + 0.06, "focus", None)
        except Exception:
            pass

    # -- press, drag, click -------------------------------------------------
    _releasing = False

    @staticmethod
    def _cursor():
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _scale(self):
        return self._placed[3] if self._placed else self._size / BASE_SIZE

    def _press(self):
        self._drag.threshold = drag_threshold(self._scale())
        self._drag.press(self._cursor(), self._pos)
        # Capture so the drag keeps going if the pointer outruns the window.
        # Capture doesn't activate anything: the text field keeps focus.
        user32.SetCapture(self.hwnd)

    def _clamp_window_pos(self, pos):
        """Window top-left -> the same, with the button (not its shadow
        margin) kept inside the work area of the monitor under the pointer."""
        m = margin_for(self._size)
        cur = self._cursor()
        work, _ = self._monitor(cur[0], cur[1])
        x, y = clamp_to_work(pos[0] + m, pos[1] + m, self._size, work)
        return int(x) - m, int(y) - m

    def _drag_move(self):
        was = self._drag.dragging
        pos = self._drag.move(self._cursor(), self._clamp_window_pos)
        if pos is None:
            return
        now = time.monotonic()
        if not was:
            self.timeline.drag(True, now)
            self._pending_hide = None
        user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_SIZEALL)))
        if pos != self._pos:
            self._pos = pos
            self._paint()

    def _release(self, click_ok=True):
        self._releasing = True
        try:
            kind, pos = self._drag.release(self._cursor(), self._clamp_window_pos)
            if user32.GetCapture() == self.hwnd:
                user32.ReleaseCapture()
        finally:
            self._releasing = False
        if kind == "click" and click_ok:
            self._clicked()
        elif kind == "drag":
            self._dropped(pos)

    def _cancel_press(self):
        if self._drag.pressed:
            self._drag.cancel()
            self._releasing = True
            try:
                if user32.GetCapture() == self.hwnd:
                    user32.ReleaseCapture()
            finally:
                self._releasing = False
        self.timeline.dragging = False

    def _dropped(self, pos):
        """Remember where it was dropped, relative to the caret, and restart
        the visible -> faded -> hidden countdown."""
        self._pos = pos
        now = time.monotonic()
        self.timeline.drag(False, now)
        self._shown_mono = now
        if self._placed:
            anchor, at_element, _work, scale = self._placed
            m = margin_for(self._size)
            gap = max(4, self._size // 5)
            self._offset = offset_from_drop((pos[0] + m, pos[1] + m), anchor, self._size,
                                            gap, at_element, scale)
            self._log("dragged: offset=%r" % (self._offset,))
            self._write_env(OFFSET_KEY, format_offset(self._offset))
        self._paint()

    def _clicked(self):
        if self._state == "working":
            return
        now = time.monotonic()
        if self._state == "idle":
            # Show the recording tile right away; the listener confirms.
            self._state = "recording"
        self.timeline.show(now)
        self.timeline.pin(True, now)
        self._paint()

        def run():
            try:
                self.on_click()
            except Exception:
                _log_error(self.log_path, "click handler")
        threading.Thread(target=run, name="caret-button-click", daemon=True).start()

    def _context_menu(self):
        """Right-click menu. A popup menu only works (and closes when you click
        elsewhere) if its owner is the foreground window, and our button can't
        be foreground (WS_EX_NOACTIVATE). So a hidden, ordinary owner window
        borrows the foreground for the menu, then hands it straight back to
        the window you were typing in."""
        cmd = 0
        self._cancel_press()
        prev = user32.GetForegroundWindow()
        app, label = "", ""
        if prev and not self._own_window(prev):
            path = _window_exe_path(prev)
            app = _normalize_app(path)
            label = app_label(path, _file_description(path)) if app else ""
        menu = user32.CreatePopupMenu()
        self._menu_open = True
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_HIDE, "Hide for now")
            if app and label:
                user32.AppendMenuW(menu, MF_STRING, MENU_HIDE_APP,
                                   "Hide in %s" % label.replace("&", "&&"))
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_TURN_OFF, "Turn off the floating mic button")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            owner = self._menu_owner()
            _force_foreground(owner)
            cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                        pt.x, pt.y, 0, owner, None)
            user32.PostMessageW(owner, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
            if prev:
                _force_foreground(prev)
            self._menu_open = False
            self._pending = None
        if cmd == MENU_HIDE:
            if self._state == "idle":
                self._hide()
        elif cmd == MENU_HIDE_APP:
            self._hide_in(app)
        elif cmd == MENU_TURN_OFF:
            if self.env_path:
                try:
                    import env_file
                    env_file.set_key(self.env_path, ENV_KEY, "false")
                except Exception:
                    _log_error(self.log_path, "writing .env")
            self._forced_enabled = None if self._forced_enabled is None else False
            self._apply_enabled(False)

    def _hide_in(self, app):
        """Never show in `app` again: add it to FLOATING_MIC_HIDE_APPS."""
        if not app:
            return
        raw = None
        if self.env_path:
            try:
                import env_file
                raw = env_file.read(self.env_path).get(HIDE_KEY)
            except Exception:
                raw = None
        if raw is None:
            raw = format_app_list(self._hide_apps)
        value = add_app(raw, app)
        self._hide_apps = parse_app_list(value)
        self._write_env(HIDE_KEY, value)
        self._log("hidden in %s" % app)
        if self._state == "idle":
            self._hide()

    def _hidden_app(self, fg):
        """True if the foreground window `fg` belongs to an app on the hide list."""
        if not self._hide_apps or not fg:
            return False
        if self._fg_app[0] != fg:
            self._fg_app = (fg, _normalize_app(_window_exe_path(fg)))
        return self._fg_app[1] in self._hide_apps

    # -- the 50 ms tick ----------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        if now >= self._next_env_poll:
            self._next_env_poll = now + ENV_POLL_SECS
            self._apply_enabled(self._read_enabled())
        if not self._enabled or self._menu_open:
            return

        fg = user32.GetForegroundWindow()
        if fg != self._fg:
            self._fg = fg
            self._focus_key = None   # back in a window: its field may show again
            if (self.timeline.visible and self._state == "idle"
                    and not self._drag.pressed and self._hidden_app(fg)):
                self._hide()
        self._follow_state(now)
        self._watch_mouse(now)
        self._watch_typing(now)

        if self._drag.pressed:
            # Mid-press / mid-drag: follow the pointer even if a mouse message
            # went missing, finish if the button came up without telling us,
            # and don't re-place or hide the button under the user's hand.
            if user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                self._up_since = None
                self._drag_move()
            elif self._up_since is None:
                self._up_since = now
            elif now - self._up_since > 0.25 and user32.GetCapture() != self.hwnd:
                # The button is physically up. Normally WM_LBUTTONUP (which
                # lags the physical state a little) ends the press; only if
                # it never came (capture lost) finish here, and not as a click.
                self._release(click_ok=False)
            if self._drag.pressed:
                self._paint()
                return
        self._up_since = None
        if self._pending and now >= self._pending[0]:
            _, reason, pt = self._pending
            self._pending = None
            self._check(reason, pt)
        if self.timeline.visible and now >= self._next_topmost:
            # Another always-on-top window (activated by the click that showed
            # us) can land above the button; keep it on top while it's up.
            self._next_topmost = now + 0.5
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if self._pending_hide and now >= self._pending_hide:
            self._pending_hide = None
            if self._state == "idle":
                self._hide()
        self._paint()

    def _follow_state(self, now):
        state = self._state
        prev, self._seen_state = self._seen_state, state
        if state == prev:
            return
        if state == "idle":
            # Recording / transcription finished: done -> hide.
            self.timeline.pin(False, now)
            self._hide()
        else:
            self._pending_hide = None
            if self.timeline.visible:
                self.timeline.pin(True, now)

    def _watch_mouse(self, now):
        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if down and not self._lbutton:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            self._down_pt = (pt.x, pt.y)
            hit = user32.WindowFromPoint(pt)
            self._down_own = bool(hit) and hit == self.hwnd
        elif not down and self._lbutton:
            if not self._down_own and self._down_pt:
                self._pending = (now + 0.08, "click", self._down_pt)
        self._lbutton = down

    def _watch_typing(self, now):
        lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return
        changed = lii.dwTime != self._last_input
        self._last_input = lii.dwTime
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cursor = (pt.x, pt.y)
        moved = cursor != self._input_cursor
        self._input_cursor = cursor
        if (changed and not moved and not self._lbutton and self.timeline.visible
                and self._state == "idle" and not self.timeline.hover
                and now - self._shown_mono > TYPING_GRACE and not self._pending_hide):
            # Keyboard input while the button is up: the user is typing.
            # Wait a moment: it may be the hotkey starting a recording.
            self._pending_hide = now + TYPING_HIDE_DELAY

    # -- show / hide ---------------------------------------------------------
    def _own_window(self, hwnd):
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == self._own_pid:
            return True
        # Settings / History / file windows: Tk windows run by our Python.
        import text_focus
        cls = text_focus.window_class(hwnd)
        if cls.startswith("Tk"):
            exe = os.path.basename(_process_image(pid.value)).lower()
            return exe.startswith("python")
        return False

    def _check(self, reason, click_pt):
        if self._state == "working":
            return
        if self._state != "idle" and self.timeline.visible:
            # Recording: the button stays exactly where it is (where it was
            # clicked); it follows the caret again once it's back to idle.
            return
        fg = user32.GetForegroundWindow()
        if not fg or self._own_window(fg) or self._hidden_app(fg):
            if self._state == "idle":
                self._hide()
            return
        info = self._inspector.inspect()
        self._log("check %s: %r" % (reason, info))
        if info is None or not info.ok or not info.caret:
            self._focus_key = None
            if self._state == "idle":
                self._hide()
            return
        key = (info.hwnd, info.element, info.control_type, info.name)
        if reason == "focus" and key == self._focus_key:
            return   # a repeat focus event for the field we already handled
        self._focus_key = key
        if reason == "click" and click_pt and info.element:
            l, t, r, b = info.element
            x, y = click_pt
            if not (l - 4 <= x <= r + 4 and t - 4 <= y <= b + 4):
                return   # clicked elsewhere (toolbar, scrollbar...): leave it
        self._show_at(info.caret, info.caret_source == "element")

    def _show_at(self, anchor, at_element):
        cx = (anchor[0] + anchor[2]) // 2
        cy = (anchor[1] + anchor[3]) // 2
        work, dpi = self._monitor(cx, cy)
        self._size = max(24, int(round(BASE_SIZE * dpi / 96)))
        d = self._size
        scale = dpi / 96
        x, y = place(anchor, d, work, gap=max(4, d // 5), at_element=at_element,
                     offset=offset_px(self._offset, scale))
        self._placed = (anchor, at_element, work, scale)
        m = margin_for(d)
        self._pos = (x - m, y - m)
        now = time.monotonic()
        self._shown_mono = now
        self._pending_hide = None
        self.timeline.show(now)
        if self._state != "idle":
            self.timeline.pin(True, now)
        self._drawn = None  # force repaint at the new place
        self._paint()

    def _hide(self):
        self._set_anim(False)
        self._cancel_press()
        self.timeline.hide()
        self._pending_hide = None
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_HIDE)
        self._drawn = None

    def _monitor(self, x, y):
        work, dpi = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)), 96
        try:
            hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                w = mi.rcWork
                work = (w.left, w.top, w.right, w.bottom)
            dx, dy = wintypes.UINT(), wintypes.UINT()
            if ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                dpi = dx.value or 96
        except Exception:
            pass
        return work, dpi

    # -- drawing ---------------------------------------------------------------
    def _set_anim(self, on):
        """Run the countdown line's frame timer only while the line moves."""
        if on == self._anim or not self.hwnd:
            return
        if on:
            user32.SetTimer(self.hwnd, ANIM_TIMER, ANIM_MS, None)
        else:
            user32.KillTimer(self.hwnd, ANIM_TIMER)
        self._anim = on

    def _paint(self):
        if not self.hwnd:
            return
        now = time.monotonic()
        alpha = self.timeline.alpha(now)
        if alpha <= 0:
            self._set_anim(False)
            if self._drawn is not None:
                self._hide()
            return
        state = self._state if self._state in STATES else "idle"
        progress = self.timeline.progress(now) if state == "idle" else None
        self._set_anim(progress is not None and self.timeline.counting)
        # Quantized so a frame is only drawn when the line has visibly moved.
        prog_key = None if progress is None else int(progress * 2000)
        key = (state, self.timeline.hover, self._size, int(alpha * 255), self._pos, prog_key)
        if key == self._drawn:
            return
        first = self._drawn is None
        self._update_layered(state, self.timeline.hover, progress, int(alpha * 255))
        if first:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self._pos[0], self._pos[1], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._drawn = key

    def _frame(self, state, hover, progress):
        """The button image for this frame (the tile is rendered once per
        state / hover / size; only the countdown line is drawn per frame)."""
        bkey = (state, hover, self._size)
        if not self._base or self._base[0] != bkey:
            self._base = (bkey, render(state, hover, self._size))
        if progress is None:
            return self._base[1]
        if self._accent is None:
            self._accent = accent_rgb(self.env_path)
        return compose(state, hover, self._size, progress, self._accent, base=self._base[1])

    def _update_layered(self, state, hover, progress, alpha):
        img_size = self._size + 2 * margin_for(self._size)
        data = premultiplied_bgra(self._frame(state, hover, progress))
        if img_size != self._bmp_key or not self._bits:
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = img_size
            bmi.biHeight = -img_size  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bits = ctypes.c_void_p()
            bmp = gdi32.CreateDIBSection(self._mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not bmp:
                return
            old = gdi32.SelectObject(self._mem_dc, bmp)
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
            else:
                self._old_bmp = old
            self._bmp = bmp
            self._bits = bits
            self._bmp_key = img_size
        ctypes.memmove(self._bits, data, len(data))
        screen = user32.GetDC(None)
        try:
            blend = BLENDFUNCTION(0, 0, max(1, min(255, alpha)), AC_SRC_ALPHA)
            pos = wintypes.POINT(*self._pos)
            size = wintypes.SIZE(img_size, img_size)
            src = wintypes.POINT(0, 0)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pos), ctypes.byref(size),
                                       self._mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)
