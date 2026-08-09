"""Stand-in glyphs for the deck's built-in icons.

The deck's icons are LVGL's built-in symbols, which are FontAwesome glyphs compiled into the
firmware. None of that exists on the PC: there is no FontAwesome in the repo, and the compiled
font is a C array of bitmaps rather than something Pillow can open. So the preview substitutes
Segoe MDL2 Assets, which ships with Windows and covers most of the same vocabulary.

These are approximations and the window says so. That is a smaller compromise than it sounds,
because an icon's shape is the same in every theme — what the preview is actually for is
whether a colour, an opacity and a radius work together, and a stand-in glyph of the right size
in the right place answers that as well as the real one would.

Two rules keep the approximation honest:

  - every name the firmware knows has an entry here, checked by a test against ICON_NAMES the
    same way config.py's list is checked against icons.cpp. The damage from a bad guess is
    then bounded at "wrong picture", never "blank tile", because a blank tile in a preview
    reads as a layout bug you do not have.
  - anything unmapped, and anything the font turns out not to draw, falls back to a visible
    placeholder rather than to nothing.
"""

from __future__ import annotations

from pathlib import Path

# Windows ships both; the second is the newer name for the same idea. Checked in order.
FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\segmdl2.ttf"),
    Path(r"C:\Windows\Fonts\SegoeIcons.ttf"),
)

# LVGL symbol name -> Segoe MDL2 Assets codepoint.
#
# A few are deliberate reinterpretations rather than matches. `tint` is a droplet in
# FontAwesome, but on this deck it is the theme-cycling button, so a colour palette says more
# about what the tile does. `eject` has no counterpart in this font and borrows the download
# arrow, which is the same gesture pointing at the same bar.
PREVIEW_GLYPHS: dict[str, str] = {
    "audio": "\ue8d6",
    "backspace": "\ue750",
    "bars": "\ue700",
    # E850-E859 is Battery0-Battery9, a monotonic fill ramp; E85A starts the *charging* series
    # over from empty, which is why the obvious "one past nine" pick for full draws less than
    # battery_3 does. Spread the deck's five levels across the ten.
    "battery_empty": "\ue850",
    "battery_1": "\ue852",
    "battery_2": "\ue855",
    "battery_3": "\ue857",
    "battery_full": "\ue859",
    "bell": "\ue7ed",
    "bluetooth": "\ue702",
    "bullet": "\ue91f",
    "call": "\ue717",
    "charge": "\ue945",
    "close": "\ue711",
    "copy": "\ue8c8",
    "cut": "\ue8c6",
    "directory": "\uf12b",
    "down": "\ue70d",
    "download": "\ue896",
    "drive": "\ueda2",
    "edit": "\ue70f",
    "eject": "\ue896",
    "envelope": "\ue715",
    "eye_close": "\ued1a",
    "eye_open": "\ue7b3",
    "file": "\ue7c3",
    "gps": "\ue707",
    "home": "\ue80f",
    "image": "\ueb9f",
    "keyboard": "\ue765",
    "left": "\ue76b",
    "list": "\ue8fd",
    "loop": "\ue8ee",
    "minus": "\ue738",
    "mute": "\ue74f",
    "new_line": "\ue751",
    "next": "\ue893",
    "ok": "\ue8fb",
    "paste": "\ue77f",
    "pause": "\ue769",
    "play": "\ue768",
    "plus": "\ue710",
    "power": "\ue7e8",
    "prev": "\ue892",
    "refresh": "\ue72c",
    "right": "\ue76c",
    "save": "\ue74e",
    "sd_card": "\ue7f1",
    "settings": "\ue713",
    "shuffle": "\ue8b1",
    "stop": "\ue71a",
    "tint": "\ue790",
    "trash": "\ue74d",
    "up": "\ue70e",
    "upload": "\ue898",
    "usb": "\uecf0",
    "video": "\ue714",
    "volume_max": "\ue995",
    "volume_mid": "\ue994",
    "warning": "\ue7ba",
    "wifi": "\ue701",
}


def font_path() -> Path | None:
    """The first icon font present, or None on a machine that has neither."""
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def glyph_for(name: str) -> str | None:
    """The stand-in character for an LVGL symbol name, or None to draw the placeholder."""
    return PREVIEW_GLYPHS.get(name)
