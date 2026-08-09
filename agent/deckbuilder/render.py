"""Draws what the deck would show, from the same rules the firmware uses.

This is the whole point of the editor. There is no live link to the deck — the builder writes
deck.json and you reload it from the tray — so the preview is the only feedback between picking
a colour and seeing it on hardware, and it has to be worth believing.

It is a transcription of two files: theme.cpp turns the theme tokens into styles, ui_builder.cpp
lays out the grid. Every constant here cites where it came from, because the failure mode of a
preview is not that it crashes, it is that it quietly stops matching and you spend an evening
adjusting a colour against a lie.

What is exact: geometry, colours, opacity, gradients, corner radii, borders, the display chain,
and which tiles are disabled. What is not: fonts, because the device's are compiled into the
firmware and none of them exist on the PC, and icons, which are stand-ins from a Windows system
font. The window says so under the canvas rather than leaving you to find out.

The frame is composited in Pillow and handed to tkinter as one image. Driving canvas primitives
instead would mean giving up per-tile alpha over a wallpaper, which is exactly the thing the
themes here are built around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from deckbuilder import icons

# The layout arithmetic lives in geometry.py, which the model and the canvas also use — a preview
# and a validator that disagreed about where a tile lands would be worse than having neither.
# Re-exported here because this is where callers have always looked for it.
from deckbuilder.geometry import (  # noqa: F401
    NAV_H,
    PAD,
    SCREEN_H,
    SCREEN_W,
    TAB_H,
    TAB_STEP,
    TAB_W,
    as_int as _int_or,
    auto_flow,
    cell_at,
    cells as _cells,
    grid_size,
    tile_box as _tile_box,
)
from deckhost import mdi1
from deckhost.config import is_device_local

CARD_PAD = PAD + 4

# firmware/multi_deck/ui_builder.cpp:273
ICON_GAP = 6

# Status dot: ui_builder.cpp:539-540, right-aligned with a PAD margin, centred in the bar.
DOT = 14

# theme.cpp:218 clamps the nav tab radius so tabs stay tab-shaped under a very round theme.
TAB_MAX_RADIUS = 8

# Rounded corners and hairline borders are drawn into a mask at this multiple and scaled back
# down. Pillow's rounded_rectangle has hard edges where LVGL antialiases, and an aliased
# preview looks worse than the hardware it is previewing — which costs the tool the only thing
# it has, which is being believed about how something looks.
SUPERSAMPLE = 4

# Theme token defaults, mirroring the initialisers in firmware/multi_deck/deck_config.h:90-122.
# A null in deck.json means "keep the default", so the preview has to know the same numbers or
# it will draw an unset field as black.
DEFAULTS: dict[str, Any] = {
    "bg": 0x101418,
    "tile": 0x1B2129,
    "tile_grad": None,  # follows `tile` unless given; deck_config.cpp:98-99
    "border": 0xFFFFFF,
    "accent": 0x4AA3FF,
    "text": 0xE6EDF3,
    "text_muted": 0x8B949E,
    "ok": 0x3FB950,
    "idle": 0x6E7681,
    "tile_opa": 100,
    "border_opa": 0,
    "radius": 10,
}

# LVGL font sizes bound to roles in theme.cpp:22-39. The preview substitutes a system face, and
# a straight pixel-for-point substitution runs large, so everything is scaled by one number
# that is easy to argue about in one place.
FONT_BASE = 20  # nav tabs
FONT_TILE = 28  # grid tile labels
FONT_SYMBOL = 28  # icon beside a label
FONT_SYMBOL_LG = 40  # icon-only tile
FONT_PAD = 40  # ten-key digits
FONT_SCALE = 0.75

TEXT_FACES = ("Montserrat-Medium.ttf", "montserrat.ttf", "segoeui.ttf", "arial.ttf")

# ui_builder.cpp:449-463. Transcribed rather than derived: the ten-key is a fixed layout in the
# firmware, and rendering it here is what shows a theme on tiles with real spans.
NUMPAD_KEYS = (
    ("Num", 0, 0, 1, 1), ("/", 1, 0, 1, 1), ("*", 2, 0, 1, 1), ("-", 3, 0, 1, 1),
    ("7", 0, 1, 1, 1), ("8", 1, 1, 1, 1), ("9", 2, 1, 1, 1), ("+", 3, 1, 1, 2),
    ("4", 0, 2, 1, 1), ("5", 1, 2, 1, 1), ("6", 2, 2, 1, 1),
    ("1", 0, 3, 1, 1), ("2", 1, 3, 1, 1), ("3", 2, 3, 1, 1), ("Ent", 3, 3, 1, 2),
    ("0", 0, 4, 2, 1), (".", 2, 4, 1, 1),
)
NUMPAD_COLS = 4
NUMPAD_ROWS = 5


@dataclass
class Preview:
    image: Image.Image
    warnings: list[str] = field(default_factory=list)
    font_name: str = ""


# -- colour ------------------------------------------------------------------------------


def parse_color(value: Any, fallback: int) -> tuple[int, int, int]:
    """Six hex digits with an optional '#', exactly as deck_config.cpp:33-45 accepts them.

    Anything else keeps the default, which is the firmware's behaviour and the reason the
    editor validates colours at all: on the device a rejected colour and an unchanged one look
    identical.
    """
    if isinstance(value, str):
        text = value.lstrip("#")
        if len(text) == 6:
            try:
                number = int(text, 16)
            except ValueError:
                number = fallback
            return (number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF
    return (fallback >> 16) & 0xFF, (fallback >> 8) & 0xFF, fallback & 0xFF


def token(theme: dict[str, Any], key: str) -> tuple[int, int, int]:
    return parse_color(theme.get(key), DEFAULTS[key])


def percent(theme: dict[str, Any], key: str) -> int:
    """0-100, clamped rather than rejected — deck_config.cpp:49-56 clamps too."""
    value = theme.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        value = DEFAULTS[key]
    return max(0, min(100, value))


def radius_of(theme: dict[str, Any]) -> int:
    value = theme.get("radius")
    if isinstance(value, bool) or not isinstance(value, int):
        value = DEFAULTS["radius"]
    return max(0, min(64, value))  # deck_config.cpp:105-110


def opa(pct: int) -> int:
    return round(pct * 255 / 100)


# -- fonts -------------------------------------------------------------------------------


_font_cache: dict[tuple[str, int], Any] = {}
_face_used = ""


def _text_font(size: int):
    global _face_used
    px = max(8, round(size * FONT_SCALE))
    key = ("text", px)
    if key not in _font_cache:
        for face in TEXT_FACES:
            try:
                _font_cache[key] = ImageFont.truetype(face, px)
                _face_used = face
                break
            except OSError:
                continue
        else:
            _font_cache[key] = ImageFont.load_default()
            _face_used = "default"
    return _font_cache[key]


def _icon_font(size: int):
    px = max(8, round(size * FONT_SCALE))
    key = ("icon", px)
    if key not in _font_cache:
        path = icons.font_path()
        try:
            _font_cache[key] = ImageFont.truetype(str(path), px) if path else None
        except OSError:
            _font_cache[key] = None
    return _font_cache[key]


def face_description() -> str:
    face = _face_used or TEXT_FACES[-1]
    icon_font = icons.font_path()
    icon_name = icon_font.name if icon_font else "none"
    return (
        f"Preview — {face} at {FONT_SCALE:g}x (the deck uses Montserrat, compiled in); "
        f"icons approximated from {icon_name}"
    )


# -- primitives --------------------------------------------------------------------------


def _rounded_mask(size: tuple[int, int], radius: int, width: int = 0) -> Image.Image:
    w, h = size
    s = SUPERSAMPLE
    mask = Image.new("L", (w * s, h * s), 0)
    draw = ImageDraw.Draw(mask)
    box = (0, 0, w * s - 1, h * s - 1)
    if width:
        draw.rounded_rectangle(box, radius=radius * s, outline=255, width=width * s)
    else:
        draw.rounded_rectangle(box, radius=radius * s, fill=255)
    return mask.resize((w, h), Image.LANCZOS)


def _gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    """A vertical two-stop gradient, matching LV_GRAD_DIR_VER."""
    w, h = size
    if top == bottom or h < 2:
        return Image.new("RGB", size, top)
    column = Image.new("RGB", (1, h))
    pixels = column.load()
    for y in range(h):
        t = y / (h - 1)
        pixels[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return column.resize(size)


def draw_card(
    base: Image.Image,
    box: tuple[int, int, int, int],
    theme: dict[str, Any],
    *,
    fill_opa: int,
    radius: int,
    fill: tuple[int, int, int] | None = None,
    gradient: bool = True,
    border_opa: int | None = None,
) -> None:
    """Paints one tile, tab or panel — theme.cpp:96-121's styleAsCard.

    `fill` overrides the tile colour for the pressed and active-tab states, which paint accent
    instead. `border_opa` overrides the theme's, because the pressed and disabled states use
    fixed opacities rather than scaling the theme's own (theme.cpp:192 and :202).
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return

    top = fill if fill is not None else token(theme, "tile")
    if fill is not None or not gradient:
        bottom = top
    else:
        # deck_config.cpp:98-99 — tile_grad follows tile unless the theme names it, and
        # theme.cpp:103-108 skips the gradient entirely when the two are equal.
        bottom = (
            parse_color(theme.get("tile_grad"), 0) if theme.get("tile_grad") is not None else top
        )

    layer = _gradient((w, h), top, bottom).convert("RGBA")
    mask = _rounded_mask((w, h), radius)
    if fill_opa < 100:
        mask = mask.point(lambda v: v * fill_opa // 100)
    base.paste(layer, (x0, y0), mask)

    edge = percent(theme, "border_opa") if border_opa is None else border_opa
    # theme.cpp:110-116 — zero opacity is zero *width*, not a transparent line.
    if edge > 0:
        outline = Image.new("RGB", (w, h), token(theme, "border"))
        edge_mask = _rounded_mask((w, h), radius, width=1)
        base.paste(outline, (x0, y0), edge_mask.point(lambda v: v * edge // 100))


def _draw_text(
    base: Image.Image, xy: tuple[int, int], text: str, font, colour: tuple[int, int, int]
) -> None:
    ImageDraw.Draw(base).text(xy, text, font=font, fill=colour, anchor="mm")


def _draw_glyph(
    base: Image.Image, xy: tuple[int, int], glyph: str, font, colour: tuple[int, int, int]
) -> None:
    """Renders an icon through a mask so it is monochrome in the theme's own colour.

    Some of these codepoints are colour glyphs in some Windows builds, and a preview that
    suddenly shows a full-colour emoji where the deck shows a flat white symbol is worse than
    no icon at all.
    """
    box = font.getbbox(glyph)
    w, h = max(1, box[2] - box[0]), max(1, box[3] - box[1])
    pad = 4
    mask = Image.new("L", (w + pad * 2, h + pad * 2), 0)
    ImageDraw.Draw(mask).text((pad - box[0], pad - box[1]), glyph, font=font, fill=255)
    patch = Image.new("RGB", mask.size, colour)
    base.paste(patch, (xy[0] - mask.width // 2, xy[1] - mask.height // 2), mask)


# -- tiles -------------------------------------------------------------------------------


def display_for(button: dict, theme: dict, settings: dict) -> str:
    """Most specific wins: button, then theme, then settings — ui_builder.cpp:280-288."""
    for level in (button.get("display"), theme.get("display"), settings.get("display")):
        if level:
            return level
    return "icon_text"  # the firmware's Settings default


def _tile_state(theme: dict, state: str) -> tuple[int, int | None, bool]:
    """(fill opacity, border opacity override, use accent) for normal/pressed/disabled."""
    base = percent(theme, "tile_opa")
    if state == "pressed":
        # theme.cpp:191-192
        return (100 if base > 75 else base + 25), 60, True
    if state == "disabled":
        # theme.cpp:201-202
        return (base - 20 if base > 30 else base), 10, False
    return base, None, False


def _fill_tile(
    base: Image.Image, box: tuple[int, int, int, int], button: dict, theme: dict,
    settings: dict, *, enabled: bool, asset_root: Path | None, warnings: list[str],
) -> None:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    colour = token(theme, "text" if enabled else "text_muted")
    label = button.get("label") or ""
    mode = display_for(button, theme, settings)

    glyph = None
    image = None
    icon = button.get("icon") or ""

    if mode != "text" and icon:
        if icon.startswith("/"):
            image = _load_asset(icon, asset_root, warnings)
        else:
            glyph = icons.glyph_for(icon)

    # ui_builder.cpp:319 — anything that cannot be produced falls back to the label alone.
    if glyph is None and image is None:
        mode = "text"

    if mode == "text":
        _draw_text(base, (cx, cy), label, _text_font(FONT_TILE), colour)
        return

    icon_px = FONT_SYMBOL_LG if mode == "icon" else FONT_SYMBOL
    show_label = mode == "icon_text" and bool(label)

    if show_label:
        label_font = _text_font(FONT_BASE)
        text_h = round(FONT_BASE * FONT_SCALE)
        icon_h = round(icon_px * FONT_SCALE)
        total = icon_h + ICON_GAP + text_h
        icon_y = cy - total // 2 + icon_h // 2
        text_y = cy + total // 2 - text_h // 2
    else:
        icon_y, text_y, label_font = cy, cy, None

    if image is not None:
        thumb = image.copy()
        thumb.thumbnail((x1 - x0 - PAD, y1 - y0 - PAD))
        base.paste(thumb, (cx - thumb.width // 2, icon_y - thumb.height // 2))
    else:
        font = _icon_font(icon_px)
        if font is None:
            _draw_text(base, (cx, cy), label, _text_font(FONT_TILE), colour)
            return
        _draw_glyph(base, (cx, icon_y), glyph, font, colour)

    if show_label:
        _draw_text(base, (cx, text_y), label, label_font, colour)


_asset_cache: dict[Path, Image.Image] = {}


def _load_asset(path: str, asset_root: Path | None, warnings: list[str]) -> Image.Image | None:
    """Loads an MDI1 image the way the device addresses it: '/wall/x.bin' from the card root."""
    if asset_root is None:
        return None
    full = asset_root / path.lstrip("/")
    if full in _asset_cache:
        return _asset_cache[full]
    try:
        width, height, pixels = mdi1.decode(full.read_bytes())
        image = Image.frombytes("RGB", (width, height), pixels)
    except FileNotFoundError:
        warnings.append(f"{path}: not found under {asset_root}")
        return None
    except (mdi1.Mdi1Error, OSError, ValueError) as exc:
        warnings.append(f"{path}: {exc}")
        return None
    _asset_cache[full] = image
    return image


def clear_asset_cache() -> None:
    """Called after converting a wallpaper, so the preview picks up the new file."""
    _asset_cache.clear()


# -- pages -------------------------------------------------------------------------------


def _draw_grid_page(
    base: Image.Image, page: dict, theme: dict, settings: dict, *, link_up: bool,
    state: str, asset_root: Path | None, warnings: list[str],
) -> None:
    cols, rows = grid_size(page.get("grid"))
    cell_w, cell_h = _cells(cols, rows)
    radius = radius_of(theme)

    flow = 0
    for index, button in enumerate(page.get("buttons") or []):
        pos = button.get("pos") or {}
        col, row = _int_or(pos.get("col"), -1), _int_or(pos.get("row"), -1)
        w, h = _int_or(pos.get("w"), 1), _int_or(pos.get("h"), 1)

        if col < 0 or row < 0:
            # ui_builder.cpp:409 — flow++ fires only in this branch, so a pinned tile consumes
            # no slot and every auto tile after it shifts.
            col, row = flow % cols, flow // cols
            flow += 1

        if row >= rows:
            warnings.append(f"{button.get('id')}: falls outside the {cols}x{rows} grid")
            continue
        if col + w > cols:
            # The device logs nothing for this at all. The tile is created at its computed x and
            # simply extends past the 800px edge, so from the deck it reads as a tile that is
            # not there — hence saying it here, where it can still be fixed.
            warnings.append(
                f"{button.get('id')}: starts at column {col} and spans {w}, "
                f"past the {cols}-column grid"
            )
        if w < 1 or h < 1:
            warnings.append(f"{button.get('id')}: pos w/h is {w}x{h}, drawn as 1x1")
            w, h = max(1, w), max(1, h)

        # ui_builder.cpp:421 — a tile whose action the agent has to run is dead without it.
        enabled = is_device_local(button.get("action") or {}) or link_up
        tile_state = state if index == 0 and state != "normal" else "normal"
        if not enabled:
            tile_state = "disabled"

        fill_opa, border_opa, accent = _tile_state(theme, tile_state)
        box = _tile_box(col, row, w, h, cell_w, cell_h)
        draw_card(
            base, box, theme,
            fill_opa=fill_opa, radius=radius, border_opa=border_opa,
            fill=token(theme, "accent") if accent else None,
        )
        # theme.cpp:196 — a press moves the tile down two pixels rather than scaling it.
        content = (box[0], box[1] + 2, box[2], box[3] + 2) if tile_state == "pressed" else box
        _fill_tile(
            base, content, button, theme, settings,
            enabled=tile_state != "disabled", asset_root=asset_root, warnings=warnings,
        )


def _draw_numpad_page(base: Image.Image, theme: dict) -> None:
    cell_w, cell_h = (
        (SCREEN_W - PAD * (NUMPAD_COLS + 1)) // NUMPAD_COLS,
        (SCREEN_H - NAV_H - PAD * (NUMPAD_ROWS + 1)) // NUMPAD_ROWS,
    )
    radius = radius_of(theme)
    font = _text_font(FONT_PAD)
    colour = token(theme, "text")
    for label, col, row, w, h in NUMPAD_KEYS:
        box = _tile_box(col, row, w, h, cell_w, cell_h)
        draw_card(base, box, theme, fill_opa=percent(theme, "tile_opa"), radius=radius)
        _draw_text(base, ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), label, font, colour)


def _draw_panel_page(base: Image.Image, page: dict, theme: dict) -> None:
    """A stand-in for the pages the firmware builds itself.

    stats and calendar are laid out in C++ from live data, so there is nothing in deck.json to
    read. Drawing one panel in the theme's own card style still answers the question worth
    asking — whether text stays readable on a panel over this wallpaper — without pretending
    to be the real page.
    """
    box = (PAD, NAV_H + PAD, SCREEN_W - PAD, SCREEN_H - PAD)
    draw_card(base, box, theme, fill_opa=percent(theme, "tile_opa"), radius=radius_of(theme))

    cx = (box[0] + box[2]) // 2
    _draw_text(base, (cx, box[1] + 70), page.get("title") or "", _text_font(FONT_PAD),
               token(theme, "text"))
    _draw_text(base, (cx, box[1] + 130),
               f"the firmware builds this page — not previewable",
               _text_font(FONT_BASE), token(theme, "text_muted"))
    _draw_text(base, (cx, box[1] + 175), "accent, ok and idle:",
               _text_font(FONT_BASE), token(theme, "text_muted"))

    # A swatch row, so the tokens this page would actually use are still visible somewhere.
    for i, key in enumerate(("accent", "ok", "idle", "text", "text_muted")):
        sw = 90
        x = cx - (5 * sw + 4 * PAD) // 2 + i * (sw + PAD)
        y = box[1] + 205
        draw_card(base, (x, y, x + sw, y + 56), theme, fill_opa=100,
                  radius=min(8, radius_of(theme)), fill=token(theme, key))


def _draw_nav(
    base: Image.Image, raw: dict, theme: dict, active_page_id: str, link_up: bool,
    warnings: list[str],
) -> None:
    # theme.cpp:207-212 — the bar only carries a scrim when there is a wallpaper behind it.
    if theme.get("wallpaper"):
        scrim = Image.new("RGBA", (SCREEN_W, NAV_H), token(theme, "bg") + (opa(40),))
        base.paste(scrim, (0, 0), scrim)

    radius = min(radius_of(theme), TAB_MAX_RADIUS)
    for index, page in enumerate(raw.get("pages") or []):
        x = PAD + index * TAB_STEP

        # The firmware does not stop here (ui_builder.cpp:516-534): it creates every tab and
        # lets the non-scrollable nav container clip whatever runs past 800px. This used to
        # break instead, which made the preview clean at exactly the point the deck stops
        # being usable — the one case where it most needed to show you the problem.
        if x + TAB_W > SCREEN_W:
            warnings.append(
                f"page {page.get('id')!r}: the nav bar fits {(SCREEN_W - PAD) // TAB_STEP} "
                "tabs, and this one is cut off at the edge with no way to reach it"
            )

        active = page.get("id") == active_page_id
        draw_card(
            base, (x, PAD, x + TAB_W, PAD + TAB_H), theme,
            # theme.cpp:220 — the active tab is opaque accent, gradient off.
            fill_opa=100 if active else percent(theme, "tile_opa"),
            radius=radius,
            fill=token(theme, "accent") if active else None,
        )
        _draw_text(base, (x + TAB_W // 2, PAD + TAB_H // 2), page.get("title") or "",
                   _text_font(FONT_BASE), token(theme, "text"))

    dot = Image.new("RGB", (DOT, DOT), token(theme, "ok" if link_up else "idle"))
    mask = _rounded_mask((DOT, DOT), DOT // 2)
    base.paste(dot, (SCREEN_W - PAD - DOT, (NAV_H - DOT) // 2), mask)


# -- entry point -------------------------------------------------------------------------


def render_page(
    raw: dict[str, Any],
    theme: dict[str, Any],
    page_id: str | None = None,
    *,
    asset_root: Path | None = None,
    link_up: bool = True,
    state: str = "normal",
) -> Preview:
    """Renders one page of the deck at 1:1, in the given theme.

    `state` styles the first tile of a grid page as normal, pressed or disabled, because those
    are derived from tile_opa by arithmetic nobody can do in their head — a theme at 58% is
    pressed at 83% and disabled at 38%, and whether those are still legible over a photo is a
    question about this theme, not a general one.
    """
    warnings: list[str] = []
    pages = raw.get("pages") or []
    page = next((p for p in pages if p.get("id") == page_id), pages[0] if pages else {})
    settings = raw.get("settings") or {}

    base = Image.new("RGB", (SCREEN_W, SCREEN_H), token(theme, "bg"))

    wallpaper = theme.get("wallpaper") or ""
    if wallpaper:
        image = _load_asset(wallpaper, asset_root, warnings)
        if image is not None:
            # theme.cpp:151-156 — pasted, never scaled, so `bg` shows around a wrong-sized
            # image exactly as it does on the deck.
            base.paste(image, (0, 0))

    kind = page.get("type") or "grid"
    if kind == "grid":
        _draw_grid_page(base, page, theme, settings, link_up=link_up, state=state,
                        asset_root=asset_root, warnings=warnings)
    elif kind == "numpad":
        _draw_numpad_page(base, theme)
    else:
        _draw_panel_page(base, page, theme)

    _draw_nav(base, raw, theme, page.get("id"), link_up, warnings)

    return Preview(image=base, warnings=warnings, font_name=face_description())
