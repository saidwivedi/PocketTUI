#!/usr/bin/env python3
"""Compose assets/og-image.png, the 1200x630 social preview card.

The two hero screenshots are regenerated from time to time, so the card is
built from them by this script rather than being drawn once by hand: the
rounded corners, the drop shadows and the phone-over-desktop overlap are all
produced here, from the flat square-cornered sources.
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))

WIDTH, HEIGHT = 1200, 630
BG = (0x16, 0x14, 0x0F)          # the landing page's dark paper
FG = "#f3ede0"
MUTED = "#a89e8c"
ACCENT = "#d4865a"

TITLE = "PocketTUI"
# Broken by hand: the tagline reads as two balanced lines, not wherever a
# greedy wrap happens to land.
TAGLINE = ["Your terminal.", "Any browser. Any device."]
BADGE = "Free · Open source · Self-hosted"

# Composite geometry, in the desktop screenshot's own pixels. The phone keeps
# its aspect, stands taller than the desktop window and hangs off its lower
# right corner, which is the arrangement the landing page's hero uses.
PHONE_HEIGHT = 1426
PHONE_OVERHANG_X = 200           # how far the phone juts past the right edge
PHONE_DROP_Y = 60                # how far its centre sits below the desktop's
DESKTOP_RADIUS = 36
PHONE_RADIUS = 60
SHADOW_BLUR = 40
SHADOW_OFFSET = (0, 26)
SHADOW_ALPHA = 150
SHADOW_MARGIN = 160              # room around the composite for the blur

HERO_WIDTH = 740                 # the composite's width on the 1200x630 card

# First family present wins; each entry is (bold, regular).
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
     "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
]


def pick_fonts():
    for bold, regular in FONT_CANDIDATES:
        if os.path.exists(bold) and os.path.exists(regular):
            return bold, regular
    return None, None


def rounded(im, radius):
    """Return im with its corners cut to radius."""
    im = im.convert("RGBA")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, im.width - 1, im.height - 1), radius=radius, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def paste_with_shadow(canvas, im, pos):
    """Composite im onto canvas at pos, over a blurred silhouette of itself."""
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    silhouette = Image.new("RGBA", im.size, (0, 0, 0, 0))
    silhouette.putalpha(im.split()[3].point(lambda a: a * SHADOW_ALPHA // 255))
    shadow.paste(silhouette,
                 (pos[0] + SHADOW_OFFSET[0], pos[1] + SHADOW_OFFSET[1]))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)))
    canvas.alpha_composite(im, pos)


def compose_hero(desktop_path, phone_path):
    desktop = Image.open(desktop_path).convert("RGBA")
    phone = Image.open(phone_path).convert("RGBA")

    phone_w = round(phone.width * PHONE_HEIGHT / phone.height)
    phone = phone.resize((phone_w, PHONE_HEIGHT), Image.LANCZOS)

    desktop = rounded(desktop, DESKTOP_RADIUS)
    phone = rounded(phone, PHONE_RADIUS)

    dx, dy = 0, 0
    px = desktop.width - phone.width + PHONE_OVERHANG_X
    py = (desktop.height - phone.height) // 2 + PHONE_DROP_Y

    # Normalise so the topmost/leftmost piece lands on the shadow margin, then
    # size the canvas to whatever the two pieces plus that margin need.
    off_x = SHADOW_MARGIN - min(dx, px)
    off_y = SHADOW_MARGIN - min(dy, py)
    w = max(dx + desktop.width, px + phone.width) + off_x + SHADOW_MARGIN
    h = max(dy + desktop.height, py + phone.height) + off_y + SHADOW_MARGIN

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    paste_with_shadow(canvas, desktop, (dx + off_x, dy + off_y))
    paste_with_shadow(canvas, phone, (px + off_x, py + off_y))
    return canvas


def build(desktop_path, phone_path, out_path):
    card = Image.new("RGB", (WIDTH, HEIGHT), BG)

    hero = compose_hero(desktop_path, phone_path)
    box = hero.getbbox()
    if box:
        hero = hero.crop(box)
    hero_h = round(hero.height * HERO_WIDTH / hero.width)
    hero = hero.resize((HERO_WIDTH, hero_h), Image.LANCZOS)
    hero_x = WIDTH - HERO_WIDTH - 8
    hero_y = (HEIGHT - hero_h) // 2
    card.paste(hero, (hero_x, hero_y), hero)

    draw = ImageDraw.Draw(card)
    bold, regular = pick_fonts()
    if bold:
        f_title = ImageFont.truetype(bold, 66)
        f_tag = ImageFont.truetype(regular, 27)
        f_badge = ImageFont.truetype(bold, 18)
    else:
        print("no system TTF found, falling back to PIL's bitmap default",
              file=sys.stderr)
        f_title = f_tag = f_badge = ImageFont.load_default()

    left = 76
    title_h, tag_lh, badge_h = 78, 38, 24
    gap_title, gap_badge = 22, 34
    block_h = (title_h + gap_title + tag_lh * len(TAGLINE)
               + gap_badge + badge_h)
    y = (HEIGHT - block_h) // 2

    draw.text((left, y), TITLE, font=f_title, fill=FG)
    y += title_h + gap_title
    for line in TAGLINE:
        draw.text((left, y), line, font=f_tag, fill=MUTED)
        y += tag_lh
    y += gap_badge
    draw.text((left, y), BADGE, font=f_badge, fill=ACCENT)

    # Palette-quantized: the card is flat colour plus two screenshots, and a
    # 255-colour PNG of it stays well under the 300 KB budget.
    card.convert("P", palette=Image.ADAPTIVE, colors=255).save(
        out_path, optimize=True)
    return out_path


def main():
    assets = os.path.join(ROOT, "assets", "landing_assets")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desktop", default=os.path.join(assets, "hero-desktop.png"),
                   help="desktop screenshot, the back plate of the composite")
    p.add_argument("--phone", default=os.path.join(assets, "hero-phone.png"),
                   help="phone screenshot, overlapped on its lower right")
    p.add_argument("--out",
                   default=os.path.join(ROOT, "assets", "og-image.png"),
                   help="where to write the 1200x630 PNG")
    args = p.parse_args()

    out = build(args.desktop, args.phone, args.out)
    print("%s  %dx%d  %d bytes" % (out, WIDTH, HEIGHT, os.path.getsize(out)))


if __name__ == "__main__":
    main()
