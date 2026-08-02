"""Converts images to the raw RGB565 format the deck loads from SD.

    python tools/make_assets.py wallpaper photo.jpg --out sdcard/wall/dusk.bin --dim 35
    python tools/make_assets.py wallpaper photos/*.jpg --out-dir sdcard/wall
    python tools/make_assets.py icon logo.png --out sdcard/icons/logo.bin --size 96

Why raw rather than PNG or JPEG: decoding on the device costs time and heap at page-build
time, and the SD card has 32GB spare. A full-screen wallpaper is 750KB either way once it is
in PSRAM; storing it undecoded just moves work off the ESP32.

File format (little-endian), shared with tools/make_icons.py:

    offset  size   field
    0       4      magic, b"MDI1"
    4       2      width  (uint16)
    6       2      height (uint16)
    8       w*h*2  RGB565 pixels, row-major

Wallpapers are cropped to fill 800x480 rather than letterboxed — a band of black at the edges
looks like a bug, not a choice. `--anchor` decides which part of a tall photo survives.

`--dim` is baked in here rather than done on the device with bg_image_recolor, which is a
per-pixel blend on every redraw. Dimming at conversion time costs nothing at runtime, and the
deck needs the headroom: this panel washes out dark tones (see docs/hardware-notes.md), so a
wallpaper usually wants dimming to keep tile labels legible.
"""

from __future__ import annotations

import argparse
import glob
import struct
import sys
from pathlib import Path

MAGIC = b"MDI1"
SCREEN_W = 800
SCREEN_H = 480

DEFAULT_ICON_BG = "1b2129"


def _require_pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        sys.exit("Pillow is required: pip install pillow")
    return Image, ImageOps


def rgb_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def encode(image) -> bytes:
    """Packs an RGB image into the MDI1 container.

    Works off tobytes() rather than getdata(): getdata() is deprecated in Pillow 13, and
    per-pixel struct.pack over 384,000 pixels is slow enough to notice.
    """
    width, height = image.size
    raw = image.tobytes()  # three bytes per pixel, row-major

    pixels = bytearray(width * height * 2)
    for src in range(0, len(raw), 3):
        value = rgb_to_rgb565(raw[src], raw[src + 1], raw[src + 2])
        dst = (src // 3) * 2
        pixels[dst] = value & 0xFF  # little-endian, matching the ESP32's uint16 reads
        pixels[dst + 1] = value >> 8

    return bytes(MAGIC) + struct.pack("<HH", width, height) + bytes(pixels)


def cover_crop(image, target_w: int, target_h: int, anchor: str):
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


def convert_wallpaper(src: Path, dst: Path, args) -> tuple[int, int]:
    Image, ImageOps = _require_pillow()

    image = Image.open(src)
    # Phone photos carry rotation in EXIF rather than in the pixels; without this a portrait
    # shot arrives on its side.
    image = ImageOps.exif_transpose(image).convert("RGB")

    image = cover_crop(image, args.width, args.height, args.anchor)
    image = blur(image, args.blur)
    image = dim(image, args.dim)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(encode(image))
    return image.size


def convert_icon(src: Path, dst: Path, args) -> tuple[int, int]:
    Image, ImageOps = _require_pillow()

    image = Image.open(src)
    image = ImageOps.exif_transpose(image).convert("RGBA")
    image = image.resize((args.size, args.size), Image.LANCZOS)

    # The panel has no alpha, so transparency is flattened against the tile colour. Doing it
    # here rather than on the device keeps edges from fringing against the background.
    bg_hex = args.bg.lstrip("#")
    background = tuple(int(bg_hex[i : i + 2], 16) for i in (0, 2, 4))
    flat = Image.new("RGB", image.size, background)
    flat.paste(image, mask=image.split()[3])

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(encode(flat))
    return flat.size


def _resolve_sources(patterns: list[str]) -> list[Path]:
    """Expands globs ourselves, since cmd.exe and PowerShell do not.

    Uses glob.glob rather than Path.glob: the latter raises NotImplementedError on absolute
    patterns from Python 3.13, and an absolute path is the normal way to point at a photo
    folder outside the repo.
    """
    found: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if path.is_file():
            found.append(path)
            continue

        matches = sorted(Path(m) for m in glob.glob(pattern, recursive=True))
        matches = [m for m in matches if m.is_file()]
        if not matches:
            print(f"warning: nothing matched {pattern!r}", file=sys.stderr)
        found.extend(matches)
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_assets", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    wall = sub.add_parser("wallpaper", help="crop and convert a full-screen background")
    wall.add_argument("source", nargs="+", help="image files (globs allowed)")
    wall.add_argument("--out", type=Path, help="output .bin (single source only)")
    wall.add_argument("--out-dir", type=Path, default=Path("sdcard/wall"))
    wall.add_argument("--width", type=int, default=SCREEN_W)
    wall.add_argument("--height", type=int, default=SCREEN_H)
    wall.add_argument(
        "--anchor",
        choices=("top", "centre", "bottom"),
        default="centre",
        help="which part of a too-tall image to keep (default: centre)",
    )
    wall.add_argument(
        "--dim",
        type=int,
        default=0,
        help="darken by this percent, baked in. 25-40 usually keeps tile labels legible",
    )
    wall.add_argument(
        "--blur",
        type=float,
        default=0.0,
        help="gaussian blur radius; a blurred copy makes a calmer backdrop for busy photos",
    )

    icon = sub.add_parser("icon", help="convert a square icon")
    icon.add_argument("source", nargs="+", help="image files (globs allowed)")
    icon.add_argument("--out", type=Path, help="output .bin (single source only)")
    icon.add_argument("--out-dir", type=Path, default=Path("sdcard/icons"))
    icon.add_argument("--size", type=int, default=96)
    icon.add_argument(
        "--bg",
        default=DEFAULT_ICON_BG,
        help="hex colour to flatten transparency against (default: theme tile)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    sources = _resolve_sources(args.source)
    if not sources:
        return _fail("no input files")

    if args.out is not None and len(sources) > 1:
        return _fail("--out takes a single source; use --out-dir for several")

    if args.mode == "icon":
        bg_hex = args.bg.lstrip("#")
        if len(bg_hex) != 6 or any(c not in "0123456789abcdefABCDEF" for c in bg_hex):
            return _fail(f"--bg must be six hex digits, got {args.bg!r}")

    convert = convert_wallpaper if args.mode == "wallpaper" else convert_icon
    total = 0

    for src in sources:
        dst = args.out if args.out is not None else args.out_dir / (src.stem + ".bin")
        try:
            width, height = convert(src, dst, args)
        except OSError as exc:
            print(f"error: {src}: {exc}", file=sys.stderr)
            continue

        size_kb = dst.stat().st_size / 1024
        print(f"{src.name} -> {dst}  ({width}x{height}, {size_kb:.0f} KB)")
        total += 1

    if total == 0:
        return _fail("nothing converted")

    print(f"\n{total} file(s) written. Copy the sdcard/ tree to the root of the SD card,")
    print('then point a theme at it:  "wallpaper": "/wall/<name>.bin"')
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
