"""Where the deck puts things: the arithmetic from ui_builder.cpp, and nothing else.

Split out of render.py because three things need it and only one of them wants Pillow. The
preview draws with it, the model validates against it, and the canvas turns a mouse position back
into a grid cell — and a model that had to import the renderer to ask how many columns fit would
drag a 40MB imaging library into every validation pass.

Everything here is integer arithmetic with a firmware citation, and every function has an exact
counterpart on the device. That is the property worth protecting: if a tile lands somewhere in the
preview it lands there on the panel, and if the editor says a layout does not fit, it does not fit.

`cell_at` is the only thing here with no firmware counterpart — the device never needs to run the
grid backwards. It lives next to `tile_box` rather than in the canvas code so the two cannot drift,
which is the whole reason this file exists.
"""

from __future__ import annotations

from typing import Any

# firmware/multi_deck/config.h
SCREEN_W = 800
SCREEN_H = 480

# firmware/multi_deck/theme.h:67-72
NAV_H = 56
PAD = 8

# Nav tabs: ui_builder.cpp:522 sizes them 120 x NAV_H-16, and :534 steps x by 128.
TAB_W = 120
TAB_H = NAV_H - 16
TAB_STEP = 128


def as_int(value: Any, fallback: int) -> int:
    """ArduinoJson's `variant | default`, which is how the firmware reads every numeric field.

    deck_config.cpp:287 is `pos["col"] | -1`, and that yields -1 for a *null* variant exactly as
    it does for an absent key — so `{"col": null, "row": null, "w": 2, "h": 1}` is legal, and
    means "auto-flow, but span two columns". A Python `.get("col", -1)` returns None for that and
    is wrong in the one direction that matters.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def grid_size(grid: dict | None) -> tuple[int, int]:
    """The columns and rows the firmware would use — ui_builder.cpp:398-399.

    `page.cols > 0 ? page.cols : 4`, which is not the same as "falsy means default": a grid
    written as `{"cols": -2}` falls back on the device, and dividing by -2 anywhere here would
    describe a layout that does not exist.
    """
    grid = grid or {}
    cols, rows = as_int((grid).get("cols"), 0), as_int((grid).get("rows"), 0)
    return (cols if cols > 0 else 4, rows if rows > 0 else 3)


def cells(cols: int, rows: int) -> tuple[int, int]:
    """Tile size for a grid — C integer division, deliberately, per ui_builder.cpp:400-401.

    (424 - 8*4)//3 is 130, not 130.67, and the 10px left over at the bottom of a 3-row grid is
    a real gap on the panel. Rounding it away would make the preview subtly wrong in the one
    dimension people notice.
    """
    return (
        (SCREEN_W - PAD * (cols + 1)) // cols,
        (SCREEN_H - NAV_H - PAD * (rows + 1)) // rows,
    )


def tile_box(col: int, row: int, w: int, h: int, cell_w: int, cell_h: int) -> tuple:
    """The pixel box of a tile — ui_builder.cpp:424, plus the span arithmetic.

    y includes NAV_H because the preview draws the whole screen; the firmware positions tiles
    inside a content container that already starts below the bar.
    """
    x = PAD + col * (cell_w + PAD)
    y = NAV_H + PAD + row * (cell_h + PAD)
    return x, y, x + cell_w * w + PAD * (w - 1), y + cell_h * h + PAD * (h - 1)


def cell_at(x: int, y: int, cols: int, rows: int) -> tuple[int, int] | None:
    """The grid cell a screen point falls in, or None for the nav bar, gutters and margins.

    The exact inverse of `tile_box` for w=h=1, including the gaps: the padding between tiles
    belongs to no cell, so a drag released in a gutter reports nothing rather than guessing at
    the nearer side. Guessing there is how a tile ends up one column from where it was dropped.
    """
    cell_w, cell_h = cells(cols, rows)

    col = _axis(x, PAD, cell_w, cols)
    row = _axis(y, NAV_H + PAD, cell_h, rows)
    if col is None or row is None:
        return None
    return col, row


def _axis(value: int, origin: int, size: int, count: int) -> int | None:
    offset = value - origin
    if offset < 0:
        return None
    index, within = divmod(offset, size + PAD)
    if index >= count or within >= size:
        return None
    return index


def nav_capacity() -> int:
    """How many nav tabs fit before one is clipped at the screen edge.

    The firmware does not stop at the edge (ui_builder.cpp:516-534): it creates every tab and
    lets the non-scrollable container clip whatever runs past 800px. So a page beyond this is not
    absent, it is present and unreachable — which is worse, and is why the number is stated
    rather than assumed.
    """
    return sum(
        1 for index in range(64) if PAD + index * TAB_STEP + TAB_W <= SCREEN_W
    )


def auto_flow(page: dict[str, Any]) -> list[tuple[str, int, int, int, int]]:
    """Where every tile on a page actually lands, after auto-flow — ui_builder.cpp:403-424.

    Returns (id, col, row, w, h) in draw order. The subtle line is `flow++`, which fires only in
    the auto branch: a pinned tile consumes no slot, so pinning one tile in place moves every
    auto tile after it back by one. That is why pinning is offered for a whole page at a time and
    never for a single tile.
    """
    cols, _rows = grid_size(page.get("grid"))
    placed: list[tuple[str, int, int, int, int]] = []
    flow = 0

    for button in page.get("buttons") or []:
        pos = button.get("pos") or {}
        col, row = as_int(pos.get("col"), -1), as_int(pos.get("row"), -1)
        w, h = max(1, as_int(pos.get("w"), 1)), max(1, as_int(pos.get("h"), 1))

        if col < 0 or row < 0:
            col, row = flow % cols, flow // cols
            flow += 1

        placed.append((button.get("id") or "?", col, row, w, h))

    return placed
