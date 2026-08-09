"""Turning a photo into something the deck can show.

The pipeline that produces wallpapers and icons: EXIF-correct the orientation, crop to cover
the panel rather than letterbox it, optionally blur and darken, then pack into the MDI1
container from deckhost/mdi1.py.

It lives in the package rather than in tools/make_assets.py — where it was written and where
its command-line front end still is — because two programs need it now. The theme builder ships
as a PyInstaller exe on machines with no Python and no checkout, so "run the script" is not
something it can do: there is no interpreter to run it with and no script to point at. Importing
is the only version of this that works in both places, and it is the same conclusion this file's
neighbours reached about the asset stamp and the MDI1 magic.

Everything here takes plain arguments rather than an argparse namespace, so the caller does not
have to fake one.
"""

from __future__ import annotations

from pathlib import Path

from deckhost.mdi1 import encode

# The panel. Wallpapers that are not exactly this still load — the firmware pastes at the origin
# and shows `bg` around the edges — but there is no reason to make one that way.
SCREEN_W = 800
SCREEN_H = 480

# Icons are flattened against this rather than kept transparent, because MDI1 carries no alpha.
# It is the Midnight tile colour, which is the background most icons end up sitting on.
DEFAULT_ICON_BG = "1b2129"

ANCHORS = ("top", "centre", "bottom")


def cover_crop(image, target_w: int, target_h: int, anchor: str = "centre"):
    """Scales to cover the target, then crops the overflow.

    Cover rather than fit: letterbox bars read as a rendering fault. `anchor` picks which part
    of the overflowing axis is kept, so a portrait photo with its subject near the top is not
    cropped through the middle.
    """
    from PIL import Image

    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    scaled = image.resize(
        (max(target_w, round(src_w * scale)), max(target_h, round(src_h * scale))),
        Image.LANCZOS,
    )

    scaled_w, scaled_h = scaled.size
    overflow_x = scaled_w - target_w
    overflow_y = scaled_h - target_h

    if anchor == "top":
        left, top = overflow_x // 2, 0
    elif anchor == "bottom":
        left, top = overflow_x // 2, overflow_y
    else:
        left, top = overflow_x // 2, overflow_y // 2

    return scaled.crop((left, top, left + target_w, top + target_h))


def dim(image, percent: int):
    """Darkens by `percent`. Multiplicative, so highlights keep their shape."""
    if percent <= 0:
        return image
    from PIL import Image

    factor = max(0.0, 1.0 - percent / 100.0)
    return Image.eval(image, lambda v: int(v * factor))


def blur(image, radius: float):
    if radius <= 0:
        return image
    from PIL import ImageFilter

    return image.filter(ImageFilter.GaussianBlur(radius))


def wallpaper(
    src: Path,
    dst: Path,
    *,
    width: int = SCREEN_W,
    height: int = SCREEN_H,
    anchor: str = "centre",
    blur_radius: float = 0.0,
    dim_percent: int = 0,
) -> tuple[int, int]:
    """Converts one photo into a full-screen wallpaper. Returns the size written."""
    from PIL import Image, ImageOps

    image = Image.open(src)
    # Phone photos carry rotation in EXIF rather than in the pixels; without this a portrait
    # shot arrives on its side.
    image = ImageOps.exif_transpose(image).convert("RGB")

    image = cover_crop(image, width, height, anchor)
    image = blur(image, blur_radius)
    image = dim(image, dim_percent)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(encode(image))
    return image.size


def icon(src: Path, dst: Path, *, size: int = 96, bg: str = DEFAULT_ICON_BG) -> tuple[int, int]:
    """Converts one image into a square tile icon. Returns the size written."""
    from PIL import Image, ImageOps

    image = Image.open(src)
    image = ImageOps.exif_transpose(image).convert("RGBA")
    image = image.resize((size, size), Image.LANCZOS)

    # The panel has no alpha, so transparency is flattened against the tile colour. Doing it
    # here rather than on the device keeps edges from fringing against the background.
    bg_hex = bg.lstrip("#")
    background = tuple(int(bg_hex[i : i + 2], 16) for i in (0, 2, 4))
    flat = Image.new("RGB", image.size, background)
    flat.paste(image, mask=image.split()[3])

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(encode(flat))
    return flat.size
