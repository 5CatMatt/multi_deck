"""Writes themes and settings back into deck.json without touching anything else.

The obvious implementation is `json.dump(data, indent=2)`, and it is wrong here. deck.json is
hand-maintained and contains hand-formatting a dump does not reproduce — the `seq` action steps
are written one per line on purpose, and a full re-dump explodes them into twenty-odd lines of
diff for a change that touched one colour. A tool whose diffs you cannot read is a tool you stop
trusting with the file.

So this splices. It re-serialises only `rev`, `themes` and `settings`, and carries everything
from `"pages":` onward across byte for byte.

That is only safe because of a coincidence worth stating plainly: `json.dumps(indent=2)`
reproduces the existing themes and settings blocks *exactly*, character for character, because
those blocks have no hand-formatting in them. That gives the splice something better than a
parser — it knows the length of the text it is replacing because it can regenerate it and
compare. If the regeneration does not match what is in the file, the assumption has broken and
the splice refuses rather than guesses.

There are then two independent checks before anything reaches the disk: the result must parse
back to exactly the object we meant to write, and the tail must be unchanged. Failing either
falls back to a full re-dump, which is uglier but never wrong. `SaveResult.spliced` says which
happened, so if the fallback turns out to fire in normal use the honest response is to delete
the splice rather than keep a clever path that never runs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The two blocks this module owns, in the order they appear in the file. `rev` is handled
# separately — it is a scalar on one line, not a block.
BLOCKS = ("themes", "settings")

# Top-level keys are the only ones written at exactly two spaces of indent; everything nested
# is at four or more. That makes this anchor unambiguous without parsing.
ANCHOR = '\n  "{key}": '

# Scoped to the text before `themes` so it cannot reach a `rev` inside a page or an action.
REV_RE = re.compile(r'("rev"\s*:\s*)(-?\d+)')


class WriteError(Exception):
    pass


@dataclass
class SaveResult:
    path: Path
    rev: int
    spliced: bool
    text: str = ""
    backup: Path | None = None

    @property
    def warning(self) -> str | None:
        if self.spliced:
            return None
        return (
            "deck.json was reformatted — pages and buttons were re-indented rather than left "
            "alone. Check `git diff sdcard/deck.json` before committing."
        )


def render_block(value: Any, level: int = 1, indent: int = 2) -> str:
    """Serialises one value the way json.dumps(indent=2) would render it in place.

    json.dumps has no notion of "this object is nested two spaces in", so every line after the
    first needs the parent's indent added back. Getting this wrong is not subtle — the splice
    compares the result against the file and refuses on any mismatch.
    """
    text = json.dumps(value, indent=indent)
    pad = " " * (indent * level)
    head, *rest = text.split("\n")
    if not rest:
        return head
    return head + "\n" + "\n".join(pad + line for line in rest)


def _find_block(text: str, key: str, current: Any) -> tuple[int, int]:
    """Returns the character span of `key`'s value, or raises if it is not where we expect.

    The end offset comes from regenerating the current value rather than from counting
    brackets. A bracket counter has to understand strings and escapes to avoid stopping at a
    `}` inside a label; regenerating has no such failure mode, and it doubles as the check that
    the file is in the shape this module knows how to edit.
    """
    anchor = ANCHOR.format(key=key)
    first = text.find(anchor)
    if first < 0:
        raise WriteError(f'no top-level "{key}" block')
    if text.find(anchor, first + 1) >= 0:
        raise WriteError(f'more than one top-level "{key}" block')

    start = first + len(anchor)
    rendered = render_block(current)
    if text[start : start + len(rendered)] != rendered:
        raise WriteError(f'the "{key}" block is not formatted the way json.dumps writes it')

    return start, start + len(rendered)


def splice(original: str, data: dict[str, Any], themes: list, settings: dict, rev: int) -> str:
    """Returns `original` with rev, themes and settings replaced, or raises WriteError."""
    spans = {key: _find_block(original, key, data[key]) for key in BLOCKS}

    replacements = [
        (spans["themes"], render_block(themes)),
        (spans["settings"], render_block(settings)),
    ]

    # rev lives before both blocks, and is rewritten within that prefix only.
    head_end = min(start for start, _ in spans.values())
    head = original[:head_end]
    new_head, count = REV_RE.subn(lambda m: f"{m.group(1)}{rev}", head, count=1)
    if count != 1:
        raise WriteError("no top-level rev to update")

    # Right to left, so replacing a later span cannot move an earlier one.
    text = original
    for (start, end), rendered in sorted(replacements, key=lambda item: -item[0][0]):
        text = text[:start] + rendered + text[end:]

    return new_head + text[head_end:]


def _expected(data: dict[str, Any], themes: list, settings: dict, rev: int) -> dict[str, Any]:
    """The object the file must parse back to. Key order follows the file, not this dict."""
    merged = dict(data)
    merged["rev"] = rev
    merged["themes"] = themes
    merged["settings"] = settings
    return merged


def newline_style(original: str) -> str | None:
    r"""Returns the file's newline sequence, or None if it is inconsistent.

    This file is edited on Windows and the checked-in copy has CRLF endings, which caught this
    module out during development in the exact way the module was written to prevent: the first
    check of "does json.dumps reproduce these blocks" was run through `open()`, whose universal
    newline translation quietly turned every CRLF into LF, and the answer came back yes for a
    file that does not actually look like that on disk.

    So the splice runs entirely in LF space and the endings are put back at the end. A file
    with mixed endings gets no conversion at all — there is no way to restore it faithfully,
    and guessing would rewrite lines this module promises not to touch. The block comparison
    then refuses and the full re-dump takes over.
    """
    if "\r\n" not in original:
        return None if "\r" in original else "\n"
    if original.count("\n") != original.count("\r\n"):
        return None
    return "\r\n"


def build(original: str, themes: list, settings: dict, rev: int) -> tuple[str, bool]:
    """Produces the new file text and says whether the splice held.

    Separated from the disk write so the tests — and a dry run — can look at the text without
    a temp directory.
    """
    newline = newline_style(original)
    source = original.replace("\r\n", "\n") if newline == "\r\n" else original

    data = json.loads(source)
    expected = _expected(data, themes, settings, rev)
    tail_anchor = ANCHOR.format(key="pages")
    tail_at = source.find(tail_anchor)

    try:
        text = splice(source, data, themes, settings, rev)

        # Two checks, deliberately independent. The first says the file means the right thing;
        # the second says we did not disturb the part we promised not to touch. A splice that
        # somehow rewrote pages into an equivalent-but-reformatted shape would pass the first
        # and fail the second, which is exactly the failure this module exists to prevent.
        if json.loads(text) != expected:
            raise WriteError("the spliced file does not parse back to the intended layout")
        if tail_at >= 0 and text[text.find(tail_anchor) :] != source[tail_at:]:
            raise WriteError('the spliced file changed something after "pages"')

        spliced = True
    except (WriteError, json.JSONDecodeError):
        text, spliced = json.dumps(expected, indent=2) + "\n", False

    if newline == "\r\n":
        text = text.replace("\n", "\r\n")

    return text, spliced


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
    path: Path, themes: list, settings: dict, rev: int, *, backup_dir: Path | None = None
) -> SaveResult:
    # read_bytes().decode() rather than read_text(): the latter applies universal-newline
    # translation, so a file someone once saved from Notepad would come back LF-only and every
    # line in it would show up in the diff of a one-colour change.
    original = path.read_bytes().decode("utf-8")

    text, spliced = build(original, themes, settings, rev)
    backup = _back_up(path, original, backup_dir)

    sweep_temp_files(path.parent)
    temp = path.parent / f".deck.json.{os.getpid()}.tmp"
    temp.write_bytes(text.encode("utf-8"))
    os.replace(temp, path)

    return SaveResult(path=path, rev=rev, spliced=spliced, text=text, backup=backup)


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
