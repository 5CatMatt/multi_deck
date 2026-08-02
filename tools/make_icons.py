"""Converts PNG icons to the raw RGB565 format the deck loads from SD.

    python tools/make_icons.py source_dir --out sdcard/icons --size 96

Why raw rather than PNG: decoding a PNG on the device costs time and heap at page-build
time, and the SD card has 32GB spare. Trading a few hundred KB of card space for instant
page builds is the right way round here.

File format (little-endian):

    offset  size  field
    0       4     magic, b"MDI1"
    4       2     width  (uint16)
    6       2     height (uint16)
    8       w*h*2 RGB565 pixels, row-major

Transparency is flattened against the theme tile colour, since the panel has no alpha.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

MAGIC = b"MDI1"
DEFAULT_BG = (0x1B, 0x21, 0x29)  # theme.tile from deck.json


def rgb_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def convert(src: Path, dst: Path, size: int, bg: tuple[int, int, int]) -> None:
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is required: pip install pillow")

    image = Image.open(src).convert("RGBA")
    image = image.resize((size, size), Image.LANCZOS)

    # Flatten alpha against the tile colour so edges do not fringe against the background.
    flat = Image.new("RGB", image.size, bg)
    flat.paste(image, mask=image.split()[3])

    payload = bytearray()
    payload += MAGIC
    payload += struct.pack("<HH", size, size)

    for pixel in flat.getdata():
        payload += struct.pack("<H", rgb_to_rgb565(*pixel))

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(bytes(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="directory of PNG files")
    parser.add_argument(
        "--out", type=Path, default=Path("sdcard/icons"), help="output directory"
    )
    parser.add_argument("--size", type=int, default=96, help="square icon size in pixels")
    parser.add_argument(
        "--bg",
        default="1b2129",
        help="hex colour to flatten transparency against (default: theme tile)",
    )
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        return _fail(f"{args.source} is not a directory")

    bg_hex = args.bg.lstrip("#")
    if len(bg_hex) != 6:
        return _fail(f"--bg must be a 6-digit hex colour, got {args.bg!r}")
    bg = tuple(int(bg_hex[i : i + 2], 16) for i in (0, 2, 4))

    sources = sorted(args.source.glob("*.png"))
    if not sources:
        return _fail(f"no .png files in {args.source}")

    for src in sources:
        dst = args.out / (src.stem + ".bin")
        convert(src, dst, args.size, bg)  # type: ignore[arg-type]
        print(f"{src.name} -> {dst}  ({args.size}x{args.size})")

    print(f"\n{len(sources)} icons written to {args.out}")
    print("Copy the sdcard/ tree to the root of the SD card.")
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
