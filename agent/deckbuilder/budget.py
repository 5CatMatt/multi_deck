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

At the time of writing the shipped layout is 5,791 bytes, a theme costs about 284, and so the
warning line is roughly two themes away. That is close enough that "you have room" is the wrong
default assumption to build into a tool.
"""

from __future__ import annotations

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
    file is pretty-printed and the frame is not — 11,227 bytes on disk is 5,791 on the wire.
    Using the real encoder also means the meter cannot drift from the thing it is measuring.
    """
    return len(protocol.encode(protocol.layout(rev, raw)))


def report(rev: int, raw: dict[str, Any]) -> Report:
    return Report(used=frame_bytes(rev, raw))


def theme_cost(theme: dict[str, Any]) -> int:
    """Roughly what one theme adds to the frame: its compact form, plus the separating comma."""
    import json

    return len(json.dumps(theme, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1


def headroom_in_themes(rev: int, raw: dict[str, Any], theme: dict[str, Any]) -> tuple[int, int]:
    """How many more themes like this one fit before each of the two lines.

    Returned as (before the test fails, before the device drops the frame) so the UI can say
    both — the first is the number that matters, the second is the one that explains why the
    first exists.
    """
    used = frame_bytes(rev, raw)
    cost = max(1, theme_cost(theme))
    return (
        max(0, (int(LIMIT * WARN_FRACTION) - used) // cost),
        max(0, (LIMIT - used) // cost),
    )
