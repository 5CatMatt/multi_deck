"""The MDI1 container: the raw RGB565 format the deck loads from its SD card.

    offset  size   field
    0       4      magic, b"MDI1"
    4       2      width  (uint16, little-endian)
    6       2      height (uint16, little-endian)
    8       w*h*2  RGB565 pixels, row-major, little-endian

Why raw rather than PNG or JPEG is argued in tools/make_assets.py; this module is only about
where the definition lives. It used to live in make_assets.py, with a second copy of the magic
in make_icons.py and a third reading of the header in firmware/multi_deck/assets.cpp. Two of
those are Python and had no reason to disagree, so they now share this one — the same reasoning
make_assets.py already gives for importing the asset stamp rather than reimplementing it.

The decoder is new. Nothing needed one until the theme builder wanted to show a wallpaper
without the deck plugged in, and a format with no inverse is a format you cannot check: the
round-trip test in tools/protocol_test.py can only exist because `decode(encode(x))` is
something you can write down.

Deliberately stdlib-only. `encode()` takes a Pillow image but never imports Pillow — it works
off `tobytes()` — so the agent, the tools and the builder can all import this module without
pulling an image library into processes that do not have one.
"""

from __future__ import annotations

import struct

MAGIC = b"MDI1"

# Everything before the pixels. firmware/multi_deck/assets.cpp rejects a file unless
# `len(blob) - HEADER_BYTES == width * height * 2`, so this constant is part of the contract
# rather than an implementation detail of the reader.
HEADER_BYTES = 8


class Mdi1Error(ValueError):
    """A blob that is not a readable MDI1 file.

    The message says which of the three checks failed, because "wallpaper did not load" on the
    panel is otherwise indistinguishable between a truncated copy, a renamed PNG and a path
    typo — and the card is the one place you cannot put a breakpoint.
    """


def rgb_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def encode(image) -> bytes:
    """Packs an RGB Pillow image into the MDI1 container.

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


# 65,536 three-byte strings, built once on first decode. A wallpaper is 384,000 pixels; going
# through the bit arithmetic per pixel in Python costs about half a second, which is long
# enough to feel when the theme builder redraws on a keystroke. The table costs ~60ms to build
# and turns the hot loop into a list index.
_RGB888: list[bytes] | None = None


def _table() -> list[bytes]:
    global _RGB888
    if _RGB888 is None:
        table = []
        for value in range(65536):
            r = (value >> 11) & 0x1F
            g = (value >> 5) & 0x3F
            b = value & 0x1F
            # The low bits are filled from the high ones rather than with zeros, so 0x1F maps
            # to 255 and not 248. Without that, white decodes to off-white and a round-trip
            # through the format darkens an image a little every time.
            table.append(bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))))
        _RGB888 = table
    return _RGB888


def decode(blob: bytes) -> tuple[int, int, bytes]:
    """Unpacks an MDI1 container into (width, height, RGB888 pixels).

    Returns plain bytes rather than an image so this module stays free of Pillow; the caller
    does `Image.frombytes("RGB", (w, h), pixels)`.

    Checks the same three things assets.cpp does, in the same order, so a file this function
    accepts is one the device will accept too.
    """
    if len(blob) < HEADER_BYTES:
        raise Mdi1Error(f"too short to hold a header: {len(blob)} bytes")

    if blob[0:4] != MAGIC:
        raise Mdi1Error(f"not an MDI1 file: magic is {bytes(blob[0:4])!r}")

    width, height = struct.unpack("<HH", blob[4:HEADER_BYTES])
    body = blob[HEADER_BYTES:]
    expected = width * height * 2

    if len(body) != expected:
        raise Mdi1Error(
            f"{width}x{height} needs {expected} bytes of pixels, found {len(body)}"
        )

    table = _table()
    words = struct.unpack(f"<{width * height}H", body)
    return width, height, b"".join([table[word] for word in words])
