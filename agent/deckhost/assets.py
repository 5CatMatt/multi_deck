"""The asset stamp: one string that says whether the SD card is current.

Colours and layout reach the deck over USB, so they are never stale. Images cannot — they are
copied to the card by hand. That leaves a gap the device cannot notice on its own: a wallpaper
regenerated with a different `--dim` and never copied still *exists*, so nothing fails. The deck
shows the old picture and reports no error, which is the same problem a browser has with a
cached stylesheet.

`tools/make_assets.py` writes the stamp to `sdcard/assets.ver` whenever it converts anything.
Copying the tree carries that file along, so the card ends up declaring which generation of
assets it holds. The device reads the file and repeats it in `hello`; the agent recomputes it
from the repo and compares.

Deliberately a content hash rather than a number you increment: a version you have to remember
to bump is only correct while you remember, and this exists precisely for the times you forgot.

The implementation lives here, in the installed package, and `tools/make_assets.py` imports it.
Two copies of a hash would drift and report mismatches that are not real.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

STAMP_FILE = "assets.ver"

# Not part of the stamp. `deck.json` travels over USB and has its own `rev`; the stamp file
# cannot describe itself.
EXCLUDED = frozenset({"deck.json", STAMP_FILE})

STAMP_CHARS = 12


def asset_stamp(root: Path) -> str:
    """Hashes every asset under `root`. Returns "" when there are no assets to sync.

    An empty result means "nothing to check" rather than "everything is missing", so a repo
    with no wallpapers yet does not nag about a card it does not need.
    """
    if not root.is_dir():
        return ""

    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(root).as_posix()
        if relative in EXCLUDED:
            continue

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{relative}:{digest}")

    if not entries:
        return ""

    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()[:STAMP_CHARS]


def write_stamp(root: Path) -> str:
    """Writes the stamp file into `root` and returns it."""
    stamp = asset_stamp(root)
    path = root / STAMP_FILE
    if stamp:
        path.write_text(stamp + "\n", encoding="ascii")
    elif path.exists():
        # No assets left, so a stale stamp would claim a generation that no longer exists.
        path.unlink()
    return stamp


def read_stamp(root: Path) -> str:
    """Reads a stamp file the way the firmware does: first line, trimmed.

    An empty or blank file reads as no stamp rather than raising. write_stamp() never produces
    one, but a truncated copy onto the card can — and that is exactly the moment this must not
    be the thing that breaks.
    """
    path = root / STAMP_FILE
    if not path.is_file():
        return ""

    text = path.read_text(encoding="ascii", errors="replace").strip()
    if not text:
        return ""
    return text.splitlines()[0].strip()
