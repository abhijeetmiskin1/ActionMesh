"""Generate the desktop app's icon set from one drawing.

The mark is the product's own shape: one intent at the centre, three devices bound
to it by the mesh, and the lines that carry a decision outward. It is drawn with
plain circles and strokes so it stays legible at 16 px in a system tray, and every
size is rendered from the same vector-ish description rather than resized from a
single bitmap.

Outputs (all in ``desktop/assets``):
  icon.png    512x512   window icon, About window, packaging input
  tray.png     32x32    system tray (Linux and Windows)
  tray@2x.png  64x64    system tray on HiDPI
  icon.ico              Windows: 16/24/32/48/64/128/256
  icon.icns             macOS: written if the local Pillow supports it

This is a build helper, not part of the runtime: run it when the mark changes.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "desktop" / "assets"

BG_TOP = (18, 28, 62)
BG_BOTTOM = (7, 10, 22)
ACCENT = (111, 141, 255)
ACCENT_SOFT = (134, 160, 255)
INK = (237, 241, 255)

#: Draw at 8x and downsample: no anti-aliasing code, and the curves stay smooth.
SS = 8


def _rounded_background(size: int, radius_ratio: float = 0.22) -> Image.Image:
    """The tile: a soft vertical gradient inside a rounded square."""
    canvas = Image.new("RGB", (size, size), BG_BOTTOM)
    draw = ImageDraw.Draw(canvas)
    for y in range(size):
        t = y / max(size - 1, 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM, strict=True))
        draw.line([(0, y), (size, y)], fill=colour)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255
    )
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(canvas, (0, 0), mask)
    return tile


def _draw_mark(tile: Image.Image, size: int) -> Image.Image:
    """Central node, three satellite devices, edges between them."""
    draw = ImageDraw.Draw(tile, "RGBA")
    centre = (size / 2, size / 2)
    ring = size * 0.305  # satellite distance from the centre

    satellites = [
        (centre[0], centre[1] - ring),                       # top
        (centre[0] - ring * 0.90, centre[1] + ring * 0.52),  # lower left
        (centre[0] + ring * 0.90, centre[1] + ring * 0.52),  # lower right
    ]

    # Edges first, so the nodes sit on top of them.
    edge_width = max(1, int(size * 0.019))
    for point in satellites:
        draw.line([centre, point], fill=(*ACCENT, 150), width=edge_width)

    # A lighter second pass gives the mesh a lit edge rather than a flat line.
    for point in satellites:
        draw.line(
            [centre, ((centre[0] + point[0]) / 2, (centre[1] + point[1]) / 2)],
            fill=(*ACCENT_SOFT, 120),
            width=max(1, edge_width - SS),
        )

    sat_radius = size * 0.068
    for point in satellites:
        draw.ellipse(
            [
                point[0] - sat_radius, point[1] - sat_radius,
                point[0] + sat_radius, point[1] + sat_radius,
            ],
            fill=(*INK, 255),
        )

    hub_radius = size * 0.105
    # A halo only as wide as the hub needs: any larger and it eats the edges, which
    # is the whole point of the mark.
    draw.ellipse(
        [centre[0] - hub_radius * 1.18, centre[1] - hub_radius * 1.18,
         centre[0] + hub_radius * 1.18, centre[1] + hub_radius * 1.18],
        fill=(10, 16, 36, 255),
    )
    draw.ellipse(
        [centre[0] - hub_radius, centre[1] - hub_radius,
         centre[0] + hub_radius, centre[1] + hub_radius],
        fill=(*ACCENT, 255),
    )
    inner = hub_radius * 0.42
    draw.ellipse(
        [centre[0] - inner, centre[1] - inner, centre[0] + inner, centre[1] + inner],
        fill=(9, 14, 30, 255),
    )
    return tile


def render(size: int) -> Image.Image:
    big = size * SS
    tile = _rounded_background(big)
    _draw_mark(tile, big)
    return tile.resize((size, size), Image.LANCZOS)


def _tray_icon(size: int, ink: tuple[int, int, int] = INK) -> Image.Image:
    """A tray icon is not a shrunken app icon.

    At 16-22 px the connecting edges turn into noise and the shapes fuse into one
    blob. So the tray glyph keeps only what survives at that size: a ring read as
    the hub, and three detached dots read as devices. One colour, because a tray
    theme draws on an arbitrary panel colour and a two-tone mark fights it.

    Drawn in ``ink`` so a platform that wants a monochrome template image can ask
    for white; the default accent-adjacent colour reads on both light and dark
    panels.
    """
    big = size * SS
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    centre = (big / 2, big / 2)

    # Three dots on an equilateral triangle around the hub. Equal spacing is what
    # makes the glyph read at 16 px: any lopsided arrangement turns into a blob as
    # the dots anti-alias together. The triangle's bounding box — not the geometric
    # centre — is what gets centred, because that is what the eye reads as "centred".
    orbit = big * 0.345
    angles = (-90.0, 30.0, 150.0)
    dot = big * 0.098
    raw = [
        (orbit * math.cos(math.radians(a)), orbit * math.sin(math.radians(a)))
        for a in angles
    ]
    top = min(p[1] for p in raw) - dot
    bottom = max(p[1] for p in raw) + dot
    y_shift = -(top + bottom) / 2  # centre the drawn extent, not the triangle
    dots = [(centre[0] + p[0], centre[1] + p[1] + y_shift) for p in raw]
    hub = (centre[0], centre[1] + y_shift)

    for point in dots:
        draw.ellipse([point[0] - dot, point[1] - dot, point[0] + dot, point[1] + dot], fill=(*ink, 255))

    # The hub is a ring when the hole is actually resolvable, and a solid disc when
    # it is not. At 32 px a 3 px hole anti-aliases into grey mush, and a muddy ring
    # reads worse than an honest dot; the size hierarchy still says "hub".
    hub_radius = big * 0.165
    if size >= 64:
        draw.ellipse(
            [hub[0] - hub_radius, hub[1] - hub_radius,
             hub[0] + hub_radius, hub[1] + hub_radius],
            outline=(*ink, 255),
            width=max(SS, int(big * 0.070)),
        )
    else:
        draw.ellipse(
            [hub[0] - hub_radius, hub[1] - hub_radius,
             hub[0] + hub_radius, hub[1] + hub_radius],
            fill=(*ink, 255),
        )
    return layer.resize((size, size), Image.LANCZOS)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)

    icon = render(512)
    icon.save(ASSETS / "icon.png", optimize=True)

    _tray_icon(32).save(ASSETS / "tray.png", optimize=True)
    _tray_icon(64).save(ASSETS / "tray@2x.png", optimize=True)

    # Windows .ico: packed sizes, each rendered rather than resampled so the 16 px
    # entry stays sharp in Explorer and on the taskbar.
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [render(size) for size in ico_sizes]
    frames[-1].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(size, size) for size in ico_sizes],
        append_images=frames[:-1],
    )

    icns_note = ""
    try:
        # Pillow's ICNS writer is available on every platform in recent versions; if
        # this build lacks it, the mac target still has icon.png and electron-builder
        # derives the .icns on a macOS runner. Say which happened rather than
        # shipping a file that was never written.
        icns_frames = [render(size) for size in (16, 32, 64, 128, 256, 512, 1024)]
        icns_frames[-1].save(ASSETS / "icon.icns", format="ICNS", append_images=icns_frames[:-1])
        icns_note = "icon.icns written"
    except Exception as error:  # report, do not hide the reason
        icns_note = f"icon.icns NOT written ({type(error).__name__}: {error})"

    print(f"wrote {ASSETS}/icon.png, tray.png, tray@2x.png, icon.ico; {icns_note}")
    detail = ", ".join(
        f"{name}={path.stat().st_size}B"
        for name, path in sorted(
            (p.name, p) for p in ASSETS.iterdir() if p.is_file()
        )
    )
    print(detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())