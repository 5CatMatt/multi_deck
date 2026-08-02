"""Converts a font into an LVGL 9 C array. Two input formats, one emitter.

    python tools/make_font.py C:/Windows/Fonts/GOTHIC.TTF --name century --sizes 20 28 40
    python tools/make_font.py fonts/Nord-Medium-28.vlw --name nord

The official tool is lv_font_conv, which is a Node package. This does the same job with Pillow,
which the repo already depends on for tools/make_assets.py, so converting a font needs no second
toolchain on the machine.

**TTF/OTF** is rendered through Pillow's FreeType binding, at whatever `--sizes` you ask for.

**VLW** is the Processing / TFT_eSPI smooth-font format — 8bpp antialiased bitmaps, which is
close enough to LVGL's own storage that transcoding is lossless. Useful when the original
outline is gone and the baked bitmaps are all that survive. The size is fixed by the file, so
`--sizes` does not apply. Layout:

    offset  size        field
    0       4 x int32   glyph count, version (11), size, unused    } big-endian
    16      2 x int32   ascent, descent                            }
    24      count x 7 x int32   code, height, width, advance, topExtent, leftExtent, pad
    ...     sum(w*h)    8-bit alpha bitmaps, in glyph order
    end     ~27 bytes   Processing's trailing name metadata, ignored

What this deliberately does *not* do:

  * kerning — lv_font_conv emits kern classes; this emits none. At the sizes here the difference
    is sub-pixel, and a wrong kern table is worse than no kern table.
  * symbols — the FontAwesome range is left out entirely. LVGL 9 resolves a missing glyph through
    `lv_font_t.fallback`, so a generated font chains to Montserrat and the LV_SYMBOL_* glyphs
    keep working inside mixed labels. Only 14/20/28/40 Montserrat are compiled in, so those are
    the sizes with a real fallback available.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

# Everything the deck renders as text. Symbols are not here on purpose — see the docstring.
FIRST_CP = 0x20
LAST_CP = 0x7E

VLW_HEADER_BYTES = 24
VLW_GLYPH_BYTES = 28


def _require_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.exit("Pillow is required: pip install pillow")
    return Image, ImageDraw, ImageFont


class Glyph:
    __slots__ = ("code", "adv_w", "box_w", "box_h", "ofs_x", "ofs_y", "nibbles")

    def __init__(self, code, adv_w, box_w, box_h, ofs_x, ofs_y, nibbles):
        self.code = code
        self.adv_w = adv_w
        self.box_w = box_w
        self.box_h = box_h
        self.ofs_x = ofs_x
        self.ofs_y = ofs_y
        self.nibbles = nibbles


class Face:
    """A converted font: glyphs plus the vertical metrics LVGL needs."""

    def __init__(self, glyphs: list[Glyph], ascent: int, descent: int, size: int):
        self.glyphs = sorted(glyphs, key=lambda g: g.code)
        self.ascent = ascent
        self.descent = descent
        self.size = size


def _quantise(alpha: bytes, levels: int) -> list[int]:
    return [(p * levels + 127) // 255 for p in alpha]


# --- front end: TTF / OTF ---------------------------------------------------------------


def render_truetype(path: Path, size: int, bpp: int) -> Face:
    Image, ImageDraw, ImageFont = _require_pillow()

    font = ImageFont.truetype(str(path), size)
    ascent, descent = font.getmetrics()
    levels = (1 << bpp) - 1

    # Room for the glyph plus slack on every side, so nothing is clipped before it is measured.
    pad = max(size, 8)
    canvas_size = (size * 3 + 2 * pad, size * 3 + 2 * pad)

    glyphs: list[Glyph] = []
    for code in range(FIRST_CP, LAST_CP + 1):
        char = chr(code)
        adv_w = int(round(font.getlength(char) * 16))  # 1/16 px, as LVGL stores it

        # Measured from the rendered pixels, not from font.getbbox().
        #
        # getbbox() returns the *layout* box: for '"' it puts the bottom edge on the baseline
        # when the ink stops less than half way down, and it keeps the side bearings. Feeding
        # that to LVGL gives every glyph ofs_y = 0 and a box padded with blank rows, so quotes
        # and apostrophes hang at the wrong height and every glyph wastes bitmap space.
        # FreeType's own bitmap is tight, which is what lv_font_conv emits, so measure the ink.
        canvas = Image.new("L", canvas_size, 0)
        ImageDraw.Draw(canvas).text((pad, pad), char, font=font, fill=255, anchor="la")
        ink = canvas.getbbox()

        if ink is None:
            glyphs.append(Glyph(code, adv_w, 0, 0, 0, 0, []))  # space and friends
            continue

        ix0, iy0, ix1, iy1 = ink
        glyphs.append(
            Glyph(
                code,
                adv_w,
                ix1 - ix0,
                iy1 - iy0,
                ix0 - pad,
                # LVGL measures ofs_y up from the baseline to the bottom of the box, and the
                # baseline sits `ascent` below the anchor.
                (pad + ascent) - iy1,
                _quantise(canvas.crop(ink).tobytes(), levels),
            )
        )

    return Face(glyphs, ascent, descent, size)


# --- front end: VLW ---------------------------------------------------------------------


def read_vlw(path: Path) -> bytes:
    """Raw .vlw, or the PROGMEM hex dump of one inside a TFT_eSPI .h."""
    if path.suffix.lower() != ".h":
        return path.read_bytes()

    text = path.read_text(encoding="utf-8", errors="replace")
    body = text[text.index("{") + 1 : text.rindex("}")]
    return bytes(int(b, 16) for b in re.findall(r"0x([0-9A-Fa-f]{2})", body))


def render_vlw(raw: bytes, bpp: int) -> Face:
    count, version, size, _unused, _ascent, _descent = struct.unpack_from(">6i", raw, 0)
    if version != 11:
        print(f"warning: VLW version {version}, expected 11", file=sys.stderr)

    levels = (1 << bpp) - 1
    entries = []
    offset = VLW_HEADER_BYTES
    for _ in range(count):
        code, height, width, advance, top, left, _pad = struct.unpack_from(">7i", raw, offset)
        entries.append((code, height, width, advance, top, left))
        offset += VLW_GLYPH_BYTES

    # The header's ascent/descent are not trustworthy — every file to hand reports descent 0,
    # which would put the baseline on the floor of the line and clip every descender. The glyph
    # table says what the font actually does, so measure it from there.
    ascent = max(top for _c, _h, _w, _a, top, _l in entries)
    descent = max(height - top for _c, height, _w, _a, top, _l in entries)

    glyphs: list[Glyph] = []
    for code, height, width, advance, top, left in entries:
        alpha = raw[offset : offset + width * height]
        offset += width * height

        glyphs.append(
            Glyph(
                code,
                advance * 16,
                width,
                height,
                left,
                # topExtent is measured up from the baseline to the top of the box; LVGL wants
                # the bottom of the box, which is that minus the height.
                top - height,
                _quantise(alpha, levels),
            )
        )

    # Anything after the bitmaps is Processing's trailing name metadata. Not an error.
    return Face(glyphs, ascent, descent, size)


# --- emitter ------------------------------------------------------------------------------


def pack(nibbles: list[int], bpp: int) -> bytes:
    """Packs pixels continuously — rows are not byte-aligned, which LVGL relies on."""
    if bpp != 4:
        raise ValueError("only 4bpp is implemented")

    out = bytearray()
    for i in range(0, len(nibbles), 2):
        hi = nibbles[i]
        lo = nibbles[i + 1] if i + 1 < len(nibbles) else 0
        out.append((hi << 4) | lo)
    return bytes(out)


def _hex_rows(data: bytes, indent: str = "    ") -> str:
    return "\n".join(
        indent + ", ".join(f"0x{b:02x}" for b in data[s : s + 12]) + ","
        for s in range(0, len(data), 12)
    )


def _cmap(symbol: str, face: Face) -> tuple[str, str]:
    """Emits the character map, sparse only when the source is actually sparse.

    A contiguous range is cheaper, but it must not be faked: filling a gap with a blank glyph
    would make the lookup *succeed* and render nothing, which stops `fallback` from ever being
    consulted. Sparse keeps missing characters missing, so they reach Montserrat.
    """
    codes = [g.code for g in face.glyphs]
    start = codes[0]
    contiguous = codes == list(range(start, start + len(codes)))

    if contiguous:
        return "", (
            f"    {{\n"
            f"        .range_start = {start}, .range_length = {len(codes)},\n"
            f"        .glyph_id_start = 1, .unicode_list = NULL, .glyph_id_ofs_list = NULL,\n"
            f"        .list_length = 0, .type = LV_FONT_FMT_TXT_CMAP_FORMAT0_TINY\n"
            f"    }}"
        )

    offsets = [c - start for c in codes]
    rows = "\n".join(
        "    " + ", ".join(str(o) for o in offsets[s : s + 16]) + ","
        for s in range(0, len(offsets), 16)
    )
    listing = f"static const uint16_t {symbol}_unicode_list[] = {{\n{rows}\n}};\n"

    return listing, (
        f"    {{\n"
        f"        .range_start = {start}, .range_length = {offsets[-1] + 1},\n"
        f"        .glyph_id_start = 1, .unicode_list = {symbol}_unicode_list,\n"
        f"        .glyph_id_ofs_list = NULL, .list_length = {len(offsets)},\n"
        f"        .type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY\n"
        f"    }}"
    )


def emit(name: str, face: Face, source: Path, bpp: int, fallback: str | None) -> str:
    symbol = f"md_font_{name}_{face.size}"
    guard = symbol.upper()

    blob = bytearray()
    dsc_lines = [
        "    {.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, "
        ".ofs_x = 0, .ofs_y = 0} /* id = 0 reserved */,"
    ]
    for glyph in face.glyphs:
        index = len(blob)
        blob += pack(glyph.nibbles, bpp)
        dsc_lines.append(
            f"    {{.bitmap_index = {index}, .adv_w = {glyph.adv_w}, "
            f".box_w = {glyph.box_w}, .box_h = {glyph.box_h}, "
            f".ofs_x = {glyph.ofs_x}, .ofs_y = {glyph.ofs_y}}}, /* U+{glyph.code:04X} */"
        )

    unicode_list, cmap_entry = _cmap(symbol, face)

    # Baked in rather than assigned at runtime: lv_font_t is const here, as it is in every font
    # lv_font_conv emits, so the pointer has to be part of the initialiser.
    if fallback:
        fallback_name = fallback.format(size=face.size)
        fallback_decl = f"extern const lv_font_t {fallback_name};"
        fallback_ref = f"&{fallback_name}"
    else:
        fallback_decl = ""
        fallback_ref = "NULL"

    return f"""/*******************************************************************************
 * {symbol}
 *
 * Generated by tools/make_font.py from {source.name} at {face.size}px, {bpp}bpp.
 * {len(face.glyphs)} glyphs. No kerning, no symbols — missing glyphs resolve through
 * lv_font_t.fallback, wired below.
 *
 * Do not edit by hand; regenerate instead.
 ******************************************************************************/

#include <lvgl.h>

#ifndef {guard}
    #define {guard} 1
#endif

#if {guard}

{fallback_decl}

static LV_ATTRIBUTE_LARGE_CONST const uint8_t {symbol}_bitmap[] = {{
{_hex_rows(bytes(blob))}
}};

static const lv_font_fmt_txt_glyph_dsc_t {symbol}_glyph_dsc[] = {{
{chr(10).join(dsc_lines)}
}};

{unicode_list}
static const lv_font_fmt_txt_cmap_t {symbol}_cmaps[] = {{
{cmap_entry}
}};

static const lv_font_fmt_txt_dsc_t {symbol}_dsc = {{
    .glyph_bitmap = {symbol}_bitmap,
    .glyph_dsc = {symbol}_glyph_dsc,
    .cmaps = {symbol}_cmaps,
    .kern_dsc = NULL,
    .kern_scale = 0,
    .cmap_num = 1,
    .bpp = {bpp},
    .kern_classes = 0,
    .bitmap_format = 0,
}};

const lv_font_t {symbol} = {{
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,
    .line_height = {face.ascent + face.descent},
    .base_line = {face.descent},
    .subpx = LV_FONT_SUBPX_NONE,
    .underline_position = {-max(1, face.size // 14)},
    .underline_thickness = {max(1, face.size // 20)},
    .dsc = &{symbol}_dsc,
    .fallback = {fallback_ref},
}};

#endif
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_font", description=__doc__.splitlines()[0])
    parser.add_argument("sources", type=Path, nargs="+", help="TTF, OTF, VLW, or TFT_eSPI .h")
    parser.add_argument("--name", required=True, help="identifier stem, e.g. 'nord'")
    parser.add_argument(
        "--sizes", type=int, nargs="+", help="pixel sizes; TTF/OTF only, VLW carries its own"
    )
    parser.add_argument("--bpp", type=int, default=4, choices=(4,))
    parser.add_argument("--out-dir", type=Path, default=Path("firmware/multi_deck"))
    parser.add_argument(
        "--fallback",
        default="lv_font_montserrat_{size}",
        help=(
            "font to chain to for glyphs this one lacks, with {size} substituted. Keeps "
            "LV_SYMBOL_* working in labels that mix icons and text. Pass '' for none."
        ),
    )
    args = parser.parse_args(argv)

    faces: list[tuple[Face, Path]] = []
    for source in args.sources:
        if not source.is_file():
            print(f"error: no such font: {source}", file=sys.stderr)
            return 1

        if source.suffix.lower() in (".ttf", ".otf"):
            if not args.sizes:
                print(f"error: {source.name} needs --sizes", file=sys.stderr)
                return 1
            faces.extend((render_truetype(source, s, args.bpp), source) for s in args.sizes)
        else:
            if args.sizes:
                print(f"note: {source.name} is VLW; --sizes ignored", file=sys.stderr)
            faces.append((render_vlw(read_vlw(source), args.bpp), source))

    args.out_dir.mkdir(parents=True, exist_ok=True)

    for face, source in faces:
        text = emit(args.name, face, source, args.bpp, args.fallback)
        dst = args.out_dir / f"font_{args.name}_{face.size}.c"
        dst.write_text(text, encoding="utf-8", newline="\n")
        gap = sorted(set(range(FIRST_CP, LAST_CP + 1)) - {g.code for g in face.glyphs})
        note = f", {len(gap)} ASCII gaps -> fallback" if gap else ""
        print(f"{source.name} -> {dst}  ({face.size}px, {len(face.glyphs)} glyphs{note})")

    print("\nDeclare them in fonts.h and expose them through theme.h.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
