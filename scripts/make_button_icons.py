"""Draw the button / app-icon pictures in client/assets from scratch.

    python scripts/make_button_icons.py

One rounded-square tile with the nine-bar waveform, in three looks:
  ready      periwinkle tile, near-black bars   (mic_idle.png, app_icon.png, app_icon.ico)
  recording  dark tile, red bars                (mic_recording.png, app_icon_recording.png)
  thinking   pale periwinkle tile, soft bars    (mic_working.png)
Change a colour here and run it again; nothing else needs editing.
"""
import os

from PIL import Image, ImageDraw

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "client", "assets")
SIZE = 512
TILE = (23.4, 22.9, 487.5, 488.5)      # caret_button.TILE_BOX, in 512 px
RADIUS = 136                           # caret_button.TILE_RADIUS
# (centre x, top, bottom) of each bar, 28 px wide.
BARS = [(92, 228, 283), (133, 130, 368), (174, 200, 308), (215, 90, 420), (256, 155, 352),
        (297, 228, 283), (338, 172, 332), (379, 136, 374), (420, 198, 306)]
BAR_W = 28

LOOKS = {
    "ready": ("#8C9EFF", "#11142A"),
    "recording": ("#1B1D2A", "#FF3B3B"),
    "thinking": ("#E3E8FF", "#6C76B8"),
}
FILES = {
    "ready": ("mic_idle.png", "app_icon.png"),
    "recording": ("mic_recording.png", "app_icon_recording.png"),
    "thinking": ("mic_working.png",),
}
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def draw(look, k=4):
    bg, fg = LOOKS[look]
    img = Image.new("RGBA", (SIZE * k, SIZE * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([v * k for v in TILE], radius=RADIUS * k, fill=bg)
    for cx, top, bot in BARS:
        d.rounded_rectangle([(cx - BAR_W / 2) * k, top * k, (cx + BAR_W / 2) * k, bot * k],
                            radius=BAR_W / 2 * k, fill=fg)
    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    for look, names in FILES.items():
        img = draw(look)
        for name in names:
            img.save(os.path.join(ASSETS, name))
            print("wrote", name)
    draw("ready").save(os.path.join(ASSETS, "app_icon.ico"),
                       sizes=[(s, s) for s in ICO_SIZES])
    print("wrote app_icon.ico")


if __name__ == "__main__":
    main()
