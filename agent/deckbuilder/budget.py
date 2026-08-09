"""How close the layout is to the one limit that fails without saying anything.

The whole of deck.json crosses to the deck as a single newline-delimited JSON frame. The
firmware drops any line at or over MD_LINK_RX_MAX (8192 bytes) and logs "oversized line —
resynchronising" to UART0, which in daily use nobody is watching. Nothing on the host checks
outbound size. So the failure looks like this: you add a theme, hit reload, and the deck keeps
the layout it already had. No error, no toast, no clue.

An editor that makes adding themes easy is an editor that walks you into that, so the size is
measured with the real encoder — not estimated — and shown all the time.

Two lines matter, and the lower one is the one to design for:

    6553  (80%)  tools/protocol_test.py fails here, on purpose, as an early warning
    8192 (100%)  the device silently discards the frame

At the time of writing the shipped layout is 5,805 bytes, a theme costs about 292, and so the
warning line is roughly two themes away. That is close enough that "you have room" is the wrong
default assumption to build into a tool.

Two thirds of that is pages and buttons, which is why the editor grew a library: the only lever
it had was deleting a theme, and 292 bytes at a time is not a budget you can manage.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from deckhost import protocol

LIMIT = protocol.MAX_LINE_BYTES

# Where tools/protocol_test.py::test_the_shipped_layout_still_fits_in_one_line stops passing.
# Not a rounder number than the limit for its own sake — it is that test's threshold, and if
# the two ever disagree the meter is lying about which edit broke the build.
WARN_FRACTION = 0.8


@dataclass
class Report:
    used: int
    limit: int = LIMIT

    @property
    def warn_at(self) -> int:
        return int(self.limit * WARN_FRACTION)

    @property
    def fraction(self) -> float:
        return self.used / self.limit

    @property
    def over_limit(self) -> bool:
        return self.used >= self.limit

    @property
    def over_warning(self) -> bool:
        return self.used >= self.warn_at

    @property
    def level(self) -> str:
        if self.over_limit:
            return "over"
        if self.over_warning:
            return "warn"
        if self.fraction >= 0.6:
            return "near"
        return "ok"

    def summary(self) -> str:
        return f"{self.used:,} / {self.limit:,} bytes ({self.fraction * 100:.0f}%)"

    def detail(self) -> str:
        if self.over_limit:
            return (
                "Over the line limit — the deck would discard this layout without reporting "
                "anything. Remove a theme before saving."
            )
        if self.over_warning:
            return (
                f"Past {WARN_FRACTION:.0%} of the limit, where "
                "tools/protocol_test.py::test_the_shipped_layout_still_fits_in_one_line fails. "
                "Saving is allowed; the test suite will complain."
            )
        return f"{self.warn_at - self.used:,} bytes before the test suite starts failing."


def frame_bytes(rev: int, raw: dict[str, Any]) -> int:
    """The exact number of bytes this layout puts on the wire.

    Goes through protocol.encode/protocol.layout rather than measuring the file, because the
    file is pretty-printed and the frame is not — 11,756 bytes on disk is 5,805 on the wire.
    Using the real encoder also means the meter cannot drift from the thing it is measuring.
    """
    return len(protocol.encode(protocol.layout(rev, raw)))


def report(rev: int, raw: dict[str, Any]) -> Report:
    return Report(used=frame_bytes(rev, raw))


def item_cost(item: Any) -> int:
    """What one theme, page or button adds to the frame: compact form plus its comma.

    The encoder settings have to match protocol.encode exactly, and one of them did not: this
    passed `ensure_ascii=False` while protocol.encode takes json.dumps's default of True. Every
    non-ASCII character therefore weighed more on the wire than the meter said — a theme named
    `Café` measured 291 and shipped 295, and a label containing `→` is out by five. Harmless
    while everything is ASCII, and button labels are exactly where an arrow or a bullet arrives.
    The direction of the error is the bad one: a meter that under-reports lets you cross a limit
    it exists to keep you under.
    """
    return len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1


# Kept as names because the call sites read better for it, and because "what does one of these
# cost" is asked of all three kinds now.
theme_cost = item_cost
page_cost = item_cost
button_cost = item_cost


def headroom_for(rev: int, raw: dict[str, Any], item: Any) -> tuple[int, int]:
    """How many more items like this one fit before each of the two lines.

    Returned as (before the test fails, before the device drops the frame) so the UI can say
    both — the first is the number that matters, the second is the one that explains why the
    first exists.
    """
    used = frame_bytes(rev, raw)
    cost = max(1, item_cost(item))
    return (
        max(0, (int(LIMIT * WARN_FRACTION) - used) // cost),
        max(0, (LIMIT - used) // cost),
    )


headroom_in_themes = headroom_for


def delta(rev: int, before: dict[str, Any], after: dict[str, Any]) -> int:
    """The exact signed byte change between two whole layouts. Negative frees space.

    `item_cost` is an estimate with a convention in it — the `+ 1` stands in for a separating
    comma, which is right when appending to a non-empty list and wrong when the list was empty.
    That is fine for "a theme costs about 292". It is not fine for "parking this page frees
    1,835 bytes", because that number is the reason someone clicks the button, and being one out
    at the limit means the deck silently drops the frame anyway.

    So anything the UI states as a fact about a specific change is measured rather than
    estimated: encode it both ways and subtract. It costs one extra serialisation of a 6KB
    object, which is nothing next to being wrong.
    """
    return frame_bytes(rev, after) - frame_bytes(rev, before)


def removal_cost(rev: int, raw: dict[str, Any], remove) -> int:
    """Exactly how many bytes removing something frees, as a positive number.

    `remove` is handed a deep copy of the layout and mutates it — the caller already knows how
    to find the thing it wants gone, and this only needs to know what the result weighs.
    """
    after = copy.deepcopy(raw)
    remove(after)
    return -delta(rev, raw, after)
