"""Writes the layout back into deck.json.

This module used to splice. It re-serialised `rev`, `themes` and `settings`, and carried
everything from `"pages":` onward across byte for byte, so that changing one colour did not
reformat a file that was hand-maintained — the `seq` action steps were written one per line on
purpose, and a full re-dump exploded them into twenty-odd lines of diff.

That trade stopped paying when the editor learned to edit pages. The splice worked by
regenerating a block and comparing it to the file, which is a genuinely good check, but it can
only cover blocks the editor does not own. Owning `pages` too leaves `rev` and the braces, and
"regenerate everything, compare it to the file, then write the regenerated version" is a full
dump with extra steps. The old docstring named this exit condition itself: if the fallback ever
fires in normal use, delete the splice rather than keep a clever path that never runs. The
converse arrived first — after sdcard/deck.json was normalised (commit 92c238c) the splice never
fails and never earns anything.

So this writes `json.dumps(indent=2)` and keeps two guards that survived the change:

  * **the reparse check** — the text must parse back to exactly the object we meant to write;
  * **the scope guard**, which is the honest successor to the old "tail is unchanged" check.
    That invariant was never really "pages are safe": it was *every top-level key this module
    does not own is unchanged*, and it only happened to be expressible as a string compare
    because the unowned part was a contiguous suffix. It is now asserted directly, and it says
    something the string compare could not — that `nav`, or anything a future firmware adds,
    comes through a save untouched no matter where in the file it sits.

`SaveResult.reformatted` reports that the file was not already in canonical form, so a save that
re-indents somebody's hand-formatting still says so once, loudly, before the diff surprises them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The top-level keys this module rewrites. Everything else in the file is carried through and
# the scope guard proves it. `rev` is included because a save always bumps it.
OWNED = ("rev", "themes", "settings", "pages")


class WriteError(Exception):
    pass


@dataclass
class SaveResult:
    path: Path
    rev: int
    reformatted: bool
    text: str = ""
    backup: Path | None = None

    @property
    def warning(self) -> str | None:
        if not self.reformatted:
            return None
        return (
            "deck.json was reformatted — hand-formatting elsewhere in the file was replaced "
            "with standard indentation. Check `git diff sdcard/deck.json` before committing."
        )


def canonical(data: dict[str, Any]) -> str:
    """The one true rendering of a layout: two-space indent, LF, trailing newline.

    Every other question in this module — is the file already in this shape, what will the diff
    look like, does the result still mean what it meant — is answered against this function, so
    there is exactly one place the format is decided.
    """
    return json.dumps(data, indent=2) + "\n"


def is_canonical(text: str) -> bool:
    """Whether saving this file would reformat anything beyond the values that changed.

    A file with mixed line endings is never canonical: it cannot be reproduced faithfully, so a
    save normalises it and the caller is told.
    """
    if newline_style(text) is None:
        return False
    try:
        return _lf(text) == canonical(json.loads(text))
    except json.JSONDecodeError:
        return False


def newline_style(original: str) -> str | None:
    r"""Returns the file's newline sequence, or None if it is inconsistent.

    This file is edited on Windows and the checked-in copy has CRLF endings, which caught this
    module out during development in the exact way the module was written to prevent: the first
    check of "does json.dumps reproduce this file" was run through `open()`, whose universal
    newline translation quietly turned every CRLF into LF, and the answer came back yes for a
    file that does not actually look like that on disk.

    So everything here runs in LF space and the endings are put back at the end. A file with
    mixed endings gets no restoration — there is no faithful way to do it, and guessing would
    rewrite lines at random. It is normalised to LF and reported as reformatted.
    """
    if "\r\n" not in original:
        return None if "\r" in original else "\n"
    if original.count("\n") != original.count("\r\n"):
        return None
    return "\r\n"


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _expected(
    data: dict[str, Any], *, themes: list, settings: dict, pages: list, rev: int
) -> dict[str, Any]:
    """The object the file must parse back to. Key order follows the file, not this dict."""
    merged = dict(data)
    merged["rev"] = rev
    merged["themes"] = themes
    merged["settings"] = settings
    merged["pages"] = pages
    return merged


def check_scope(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Raises unless every top-level key outside OWNED came through untouched.

    Order is checked as well as content, and across the whole top level rather than only the
    unowned keys — comparing unowned keys to each other would not notice `nav` moving from after
    `rev` to before it, since neither key is missing and the two remaining orders are both just
    `["nav"]`. deck.json is read by people at least as often as by the firmware, and a writer
    that quietly reshuffles the top of the file is one you have to diff every time to trust.

    The one permitted difference is an owned key the file did not have, which is appended: a
    deck.json with no `pages` at all gains one the first time a page is added.
    """
    expected_order = list(before) + [key for key in OWNED if key not in before]
    if list(after) != expected_order:
        added = sorted(set(after) - set(expected_order))
        lost = sorted(set(expected_order) - set(after))
        if added or lost:
            raise WriteError(
                "the save changed which top-level keys exist: "
                + ", ".join(filter(None, [
                    f"added {added}" if added else "",
                    f"removed {lost}" if lost else "",
                ]))
            )
        raise WriteError("the save reordered the top-level keys")

    for key in before:
        if key not in OWNED and before[key] != after[key]:
            raise WriteError(f'the save changed "{key}", which this module does not own')


def build(
    original: str, *, themes: list, settings: dict, pages: list, rev: int
) -> tuple[str, bool]:
    """Produces the new file text and says whether anything beyond the values was reformatted.

    Keyword-only on purpose: the argument list is this module's scope statement, and four
    positional collections in a row is exactly the shape that gets silently transposed. Separated
    from the disk write so the tests — and a dry run — can look at the text without a temp
    directory.
    """
    newline = newline_style(original)
    data = json.loads(_lf(original))

    expected = _expected(data, themes=themes, settings=settings, pages=pages, rev=rev)
    text = canonical(expected)

    written = json.loads(text)
    if written != expected:
        raise WriteError("the rendered file does not parse back to the intended layout")
    check_scope(data, written)

    reformatted = not is_canonical(original)
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")

    return text, reformatted


def sweep_temp_files(directory: Path) -> None:
    """Removes leftovers from a crash mid-write.

    Not housekeeping: deckhost/assets.py hashes every file under sdcard/ except deck.json and
    assets.ver to build the card's stamp, so one abandoned temp file permanently changes the
    stamp and the agent then reports the card as stale forever, for a file that never goes near
    the card. Backups are written outside the tree for the same reason.
    """
    for stale in directory.glob(".deck.json.*.tmp"):
        try:
            stale.unlink()
        except OSError:
            pass


def write(
    path: Path,
    *,
    themes: list,
    settings: dict,
    pages: list,
    rev: int,
    backup_dir: Path | None = None,
) -> SaveResult:
    # read_bytes().decode() rather than read_text(): the latter applies universal-newline
    # translation, so a file someone once saved from Notepad would come back LF-only and every
    # line in it would show up in the diff of a one-value change.
    original = path.read_bytes().decode("utf-8")

    text, reformatted = build(original, themes=themes, settings=settings, pages=pages, rev=rev)
    backup = _back_up(path, original, backup_dir)

    sweep_temp_files(path.parent)
    temp = path.parent / f".deck.json.{os.getpid()}.tmp"
    temp.write_bytes(text.encode("utf-8"))
    os.replace(temp, path)

    return SaveResult(path=path, rev=rev, reformatted=reformatted, text=text, backup=backup)


KEEP_BACKUPS = 20


def _back_up(path: Path, original: str, backup_dir: Path | None) -> Path | None:
    if backup_dir is None:
        return None

    from datetime import datetime

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"deck-{stamp}.json"
    backup.write_bytes(original.encode("utf-8"))

    for old in sorted(backup_dir.glob("deck-*.json"))[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass

    return backup
