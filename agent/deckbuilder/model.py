"""The document the editor edits: one deck.json, with themes, settings and pages mutable.

Holds the file's original text alongside the parsed layout, because the writer needs both — the
text to compare against, and the object to render.

It was called ThemeDoc while themes were all it could change. The name went when `pages` did,
rather than being kept as an alias: a document that owns the layout and calls itself a theme
document is a small lie that every future reader has to discover for themselves.

The rule this module exists to enforce is the one deck.json cares most about: every theme
carries every key the firmware reads, in the same order, with `null` or `""` written down where
a value is unset. tools/protocol_test.py derives that key list from the firmware's own parser
and checks it, so a theme built here with a missing key does not fail on the deck — it fails the
repo's test suite, which is a much better place to find out, but only if the shape is right by
construction rather than by remembering.

So the field order is read out of the file being edited rather than hardcoded. Add a token to
parseTheme() in the firmware, write it into one theme in deck.json, and every theme this editor
creates from then on carries it with no change here.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deckbuilder import geometry
from deckhost.config import DeckConfig

# Below this a tile is smaller than the pad of a finger, which is the point at which a grid stops
# being a deck and starts being a test. Not a firmware limit — the device will happily draw a 8x6
# grid of 90x60 tiles — which is exactly why it is worth saying out loud.
MIN_TILE_PX = 60

# Used only when the file being opened has no themes at all to copy a shape from — a new or
# emptied deck.json. Kept in step with the shipped file by a test rather than by vigilance.
THEME_FIELD_ORDER: tuple[str, ...] = (
    "name",
    "display",
    "wallpaper",
    "bg",
    "tile",
    "tile_grad",
    "tile_opa",
    "border",
    "border_opa",
    "radius",
    "dim_opa",
    "flip180",
    "accent",
    "text",
    "text_muted",
    "ok",
    "idle",
)

# What a theme with nothing said about it looks like. Every value is the written form of "take
# the default": "" for the two strings, null for everything else. Only `name` is real, because
# an unnamed theme is given a positional name by the firmware and you cannot then select it.
#
# dim_opa and flip180 stay null in particular — their defaults live in firmware/config.h and
# differ between builds, so writing a literal here would quietly override the board.
THEME_TEMPLATE: dict[str, Any] = {
    "name": "New theme",
    "display": "",
    "wallpaper": "",
    "bg": "#101418",
    "tile": "#1b2129",
    "tile_grad": None,
    "tile_opa": 100,
    "border": "#ffffff",
    "border_opa": 0,
    "radius": 10,
    "dim_opa": None,
    "flip180": None,
    "accent": "#4aa3ff",
    "text": "#e6edf3",
    "text_muted": "#8b949e",
    "ok": "#3fb950",
    "idle": "#6e7681",
}


# Pages and buttons follow the same rule as themes: one key set, one order, unset written down.
# Derived from the file being edited, with these as the fallback for an empty one — and pinned to
# the shipped file by tests, exactly as THEME_FIELD_ORDER is.
PAGE_FIELD_ORDER: tuple[str, ...] = ("id", "title", "type", "grid", "buttons")

PAGE_TEMPLATE: dict[str, Any] = {
    "id": "page",
    "title": "Page",
    "type": "grid",
    "grid": {"cols": 4, "rows": 3},
    "buttons": [],
}

BUTTON_FIELD_ORDER: tuple[str, ...] = (
    "id", "label", "icon", "display", "pos", "action", "hold",
)

# `icon` and `display` are "" rather than null, following the file: both are read as strings by
# the firmware and "" is what it already treats as unset.
BUTTON_TEMPLATE: dict[str, Any] = {
    "id": "button",
    "label": "Button",
    "icon": "",
    "display": "",
    "pos": None,
    "action": {"type": "launch", "target": ""},
    "hold": None,
}

# `pos` is null or all four, never some of them. The firmware reads each field independently and
# defaults the missing ones, so a partial pos is legal and does something — which is precisely why
# writing one is a bad idea: `{"col": 2}` means row -1, meaning auto-flow, meaning `col` is
# ignored. Keeping it all-or-nothing keeps it readable.
POS_FIELD_ORDER: tuple[str, ...] = ("col", "row", "w", "h")

# Page types whose contents the firmware draws itself. A grid on one of these is not read, and
# buttons on one are parsed and then never built — both look like an edit that did not take.
FIRMWARE_PAGE_TYPES = frozenset({"numpad", "stats", "calendar", "colortest"})


class ModelError(Exception):
    pass


# One undo step: everything the document owns, deep-copied.
State = tuple[list, dict, list]


@dataclass
class DeckDoc:
    path: Path
    original: str
    raw: dict[str, Any]
    themes: list[dict[str, Any]]
    settings: dict[str, Any]
    pages: list[dict[str, Any]]
    field_order: tuple[str, ...]
    page_order: tuple[str, ...] = PAGE_FIELD_ORDER
    button_order: tuple[str, ...] = BUTTON_FIELD_ORDER
    _undo: list[State] = field(default_factory=list)
    _redo: list[State] = field(default_factory=list)

    # -- loading -------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> DeckDoc:
        # Bytes, not read_text(): universal newline translation would hand us LF for a CRLF
        # file and the writer would then compare against text that does not match the disk.
        original = path.read_bytes().decode("utf-8")
        raw = json.loads(original)

        themes = copy.deepcopy(raw.get("themes") or [])
        settings = copy.deepcopy(raw.get("settings") or {})
        pages = copy.deepcopy(raw.get("pages") or [])

        return cls(
            path=path,
            original=original,
            raw=raw,
            themes=themes,
            settings=settings,
            pages=pages,
            field_order=cls._field_order(themes),
            page_order=cls._order_of(pages[0] if pages else None, PAGE_FIELD_ORDER),
            button_order=cls._order_of(cls._first_button(pages), BUTTON_FIELD_ORDER),
        )

    @staticmethod
    def _field_order(themes: list[dict[str, Any]]) -> tuple[str, ...]:
        """The key order new themes copy, taken from the file's first theme.

        Deliberately not a merge of every theme's keys. The file's own test insists all themes
        already agree, so the first one is either the answer or the file is already broken —
        and in the second case a merged order would paper over it.
        """
        if themes and isinstance(themes[0], dict) and themes[0]:
            return tuple(themes[0])
        return THEME_FIELD_ORDER

    @staticmethod
    def _order_of(sample: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sample) if isinstance(sample, dict) and sample else fallback

    @staticmethod
    def _first_button(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The first button anywhere in the file, not the first button of the first page.

        The shipped deck opens on a grid, but a deck whose first page is the ten-key has no
        buttons at all on `pages[0]` — and taking the shape from an empty list would silently
        fall back to the literal for a file that had a perfectly good answer three pages down.
        """
        for page in pages:
            for button in page.get("buttons") or []:
                if isinstance(button, dict) and button:
                    return button
        return None

    # -- editing -------------------------------------------------------------------------

    def snapshot(self) -> None:
        """Records the current state so the next change can be undone.

        Called before a change rather than after, so an undo returns you to what you were
        looking at when you touched the control.
        """
        self._undo.append(self._state())
        self._redo.clear()

    def _state(self) -> State:
        return (
            copy.deepcopy(self.themes),
            copy.deepcopy(self.settings),
            copy.deepcopy(self.pages),
        )

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self.themes, self.settings, self.pages = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self.themes, self.settings, self.pages = self._redo.pop()
        return True

    def set_theme_field(self, index: int, key: str, value: Any) -> None:
        if key not in self.field_order:
            raise ModelError(f"{key!r} is not a theme field the firmware reads")
        self.themes[index][key] = value

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def new_theme(self, name: str = "New theme") -> int:
        theme = self._shaped({**THEME_TEMPLATE, "name": self._unique_name(name)})
        self.themes.append(theme)
        return len(self.themes) - 1

    def duplicate(self, index: int) -> int:
        source = self.themes[index]
        copied = self._shaped(
            {**copy.deepcopy(source), "name": self._unique_name(source.get("name") or "Theme")}
        )
        self.themes.insert(index + 1, copied)
        return index + 1

    def delete(self, index: int) -> None:
        # The firmware falls back to one fully-defaulted theme when the list is empty, so this
        # is survivable — but it means the deck stops looking like anything you chose, which is
        # confusing enough to be worth refusing.
        if len(self.themes) <= 1:
            raise ModelError("a deck needs at least one theme")
        self.themes.pop(index)

    def move(self, index: int, delta: int) -> int:
        target = max(0, min(len(self.themes) - 1, index + delta))
        if target != index:
            self.themes.insert(target, self.themes.pop(index))
        return target

    def rename(self, index: int, name: str) -> None:
        """Renames a theme and follows the name everywhere else it is written.

        Themes are referenced by name from `settings.theme` and from any `theme` action, and
        both fail the same quiet way: the firmware keeps whatever theme it had and nothing says
        the name matched nothing. Renaming without fixing the references is the easiest way to
        produce that, so it is not offered as a choice.
        """
        old = self.themes[index].get("name")
        self.themes[index]["name"] = name
        if not old or old == name:
            return

        if self.settings.get("theme") == old:
            self.settings["theme"] = name

        for page in self.pages:
            for button in page.get("buttons") or []:
                for slot in ("action", "hold"):
                    _retarget_theme(button.get(slot), old, name)

    def _unique_name(self, base: str) -> str:
        taken = {t.get("name") for t in self.themes}
        if base not in taken:
            return base
        for n in range(2, 100):
            candidate = f"{base} {n}"
            if candidate not in taken:
                return candidate
        return base

    def _shaped(self, theme: dict[str, Any]) -> dict[str, Any]:
        """Rebuilds a theme dict in the canonical key order, filling anything absent.

        This is the one place a theme object is constructed, so it is the one place that has to
        get the shape right.
        """
        return {key: theme.get(key, THEME_TEMPLATE.get(key)) for key in self.field_order}

    # -- pages ---------------------------------------------------------------------------

    @property
    def boot_page(self) -> str | None:
        """What the deck shows at power-on: pages[0], with nothing in the format naming it.

        Which makes it the easiest thing to change by accident. Reordering pages to get the nav
        tabs in a nicer order silently changes what you see when you plug the deck in, and there
        is no setting anywhere to look at afterwards and work out why.
        """
        return self.pages[0].get("id") if self.pages else None

    def page_index(self, page_id: str) -> int:
        for index, page in enumerate(self.pages):
            if page.get("id") == page_id:
                return index
        raise ModelError(f"no page {page_id!r}")

    def shaped_page(self, page: dict[str, Any]) -> dict[str, Any]:
        return {key: page.get(key, PAGE_TEMPLATE.get(key)) for key in self.page_order}

    def shaped_button(self, button: dict[str, Any]) -> dict[str, Any]:
        return {key: button.get(key, BUTTON_TEMPLATE.get(key)) for key in self.button_order}

    def new_page(self, title: str = "New page", page_type: str = "grid") -> int:
        page = self.shaped_page({
            **copy.deepcopy(PAGE_TEMPLATE),
            "id": self._unique_page_id(_slug(title)),
            "title": title,
            "type": page_type,
            "grid": None if page_type in FIRMWARE_PAGE_TYPES else {"cols": 4, "rows": 3},
        })
        self.pages.append(page)
        return len(self.pages) - 1

    def duplicate_page(self, index: int) -> int:
        source = self.pages[index]
        copied = self.shaped_page(copy.deepcopy(source))
        copied["id"] = self._unique_page_id(source.get("id") or "page")
        copied["title"] = f"{source.get('title') or copied['id']} copy"
        copied["buttons"] = [
            self._rekeyed_button(button) for button in copied.get("buttons") or []
        ]
        # A duplicated page's own nav tiles should point at the copy, not back at the original,
        # or "duplicate and edit" silently leaves you on the page you were trying to replace.
        _retarget_page_in(copied, source.get("id"), copied["id"])
        self.pages.insert(index + 1, copied)
        return index + 1

    def delete_page(self, index: int) -> None:
        if len(self.pages) <= 1:
            raise ModelError("a deck needs at least one page")

        page_id = self.pages[index].get("id")
        referrers = self.page_referrers(page_id, ignore_page_index=index)
        if referrers:
            raise ModelError(
                f"{len(referrers)} button(s) navigate to {page_id!r}: "
                + ", ".join(referrers[:4])
                + ("…" if len(referrers) > 4 else "")
            )
        self.pages.pop(index)

    def move_page(self, index: int, delta: int) -> int:
        target = max(0, min(len(self.pages) - 1, index + delta))
        if target != index:
            self.pages.insert(target, self.pages.pop(index))
        return target

    def rename_page_id(self, index: int, new_id: str) -> None:
        """Changes a page's id and follows every reference to it.

        Page ids are the deck's only internal links, and a stale one is the same silent failure
        a stale theme name is: the firmware finds no page, keeps the one it is on, and says
        nothing. So this is not offered as a choice either.
        """
        old = self.pages[index].get("id")
        if new_id == old:
            return
        if any(p.get("id") == new_id for i, p in enumerate(self.pages) if i != index):
            raise ModelError(f"another page already has the id {new_id!r}")

        self.pages[index]["id"] = new_id
        if not old:
            return
        for page in self.pages:
            _retarget_page_in(page, old, new_id)

    def set_grid(self, index: int, cols: int, rows: int) -> None:
        page = self.pages[index]
        if page.get("type") in FIRMWARE_PAGE_TYPES:
            raise ModelError(f"a {page.get('type')!r} page draws its own layout")
        page["grid"] = {"cols": max(1, int(cols)), "rows": max(1, int(rows))}

    def page_referrers(
        self, page_id: str | None, *, ignore_page_index: int | None = None
    ) -> list[str]:
        """Button ids whose action or hold navigates to `page_id`.

        Deleting or parking a page with inbound references refuses and names these. Quietly
        repointing somebody's nav button is exactly the kind of helpful guess this codebase
        keeps deciding not to ship — the reference is information, and dropping it loses the
        one clue about what the page was for.
        """
        found: list[str] = []
        for index, page in enumerate(self.pages):
            if index == ignore_page_index:
                continue
            for button in page.get("buttons") or []:
                for slot in ("action", "hold"):
                    if _targets_page(button.get(slot), page_id):
                        found.append(button.get("id") or f"{page.get('id')}[?]")
                        break
        return found

    # -- buttons -------------------------------------------------------------------------

    def new_button(self, page_index: int, label: str = "New button") -> int:
        page = self.pages[page_index]
        button = self.shaped_button({
            **copy.deepcopy(BUTTON_TEMPLATE),
            "id": self._unique_button_id(_slug(label) or "button"),
            "label": label,
        })
        page.setdefault("buttons", [])
        page["buttons"].append(button)
        return len(page["buttons"]) - 1

    def duplicate_button(self, page_index: int, index: int) -> int:
        buttons = self.pages[page_index]["buttons"]
        buttons.insert(index + 1, self._rekeyed_button(copy.deepcopy(buttons[index])))
        return index + 1

    def delete_button(self, page_index: int, index: int) -> None:
        self.pages[page_index]["buttons"].pop(index)

    def move_button(self, page_index: int, index: int, delta: int) -> int:
        """Reorders within the page, which for an auto-flow page *is* the layout.

        Costs nothing on the wire, which is why it is the default way to arrange a page: the
        alternative writes a `pos` on every tile and spends about 300 bytes a page.
        """
        buttons = self.pages[page_index]["buttons"]
        target = max(0, min(len(buttons) - 1, index + delta))
        if target != index:
            buttons.insert(target, buttons.pop(index))
        return target

    def move_button_to_page(self, from_page: int, index: int, to_page: int) -> None:
        """The no-file version of parking, and the usual answer to "I need room on this page"."""
        if from_page == to_page:
            return
        target = self.pages[to_page]
        if target.get("type") in FIRMWARE_PAGE_TYPES:
            raise ModelError(f"a {target.get('type')!r} page cannot hold buttons")

        button = self.pages[from_page]["buttons"].pop(index)
        # Positions are page-local, and a tile pinned to (3,2) on a 4x3 grid is off the edge of
        # a 3x2 one. Dropping the pin puts it on the end of the flow, which is visible.
        button["pos"] = None
        target.setdefault("buttons", [])
        target["buttons"].append(button)

    # -- positions -----------------------------------------------------------------------

    def pin_all(self, page_index: int) -> None:
        """Writes down where auto-flow is currently putting every tile.

        Done as one step rather than per tile because pinning a single tile in place moves nine
        others: `flow++` fires only in the auto branch, so a pinned tile stops consuming a slot
        and everything after it shifts back one. Pinning the lot is the only version of this
        that leaves the page looking the way it looked.
        """
        page = self.pages[page_index]
        cols, rows = _grid_of(page)
        flow = 0
        for button in page.get("buttons") or []:
            pos = button.get("pos") or {}
            col, row = _as_int(pos.get("col"), -1), _as_int(pos.get("row"), -1)
            if col < 0 or row < 0:
                col, row = flow % cols, flow // cols
                flow += 1
            button["pos"] = {
                "col": col,
                "row": row,
                "w": max(1, _as_int(pos.get("w"), 1)),
                "h": max(1, _as_int(pos.get("h"), 1)),
            }

    def unpin_all(self, page_index: int) -> None:
        """Back to array order. Frees the bytes, and loses spans and deliberate gaps."""
        for button in self.pages[page_index].get("buttons") or []:
            button["pos"] = None

    def is_pinned(self, page_index: int) -> bool:
        buttons = self.pages[page_index].get("buttons") or []
        return bool(buttons) and all(b.get("pos") is not None for b in buttons)

    # -- ids -----------------------------------------------------------------------------

    def button_ids(self) -> set[str]:
        return {
            button.get("id")
            for page in self.pages
            for button in page.get("buttons") or []
            if button.get("id")
        }

    def _unique_page_id(self, base: str) -> str:
        return _unique(base or "page", {p.get("id") for p in self.pages})

    def _unique_button_id(self, base: str) -> str:
        return _unique(base or "button", self.button_ids())

    def _rekeyed_button(self, button: dict[str, Any]) -> dict[str, Any]:
        button = self.shaped_button(button)
        button["id"] = self._unique_button_id(button.get("id") or "button")
        return button

    # -- state ---------------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return (
            self.themes != self.raw.get("themes")
            or self.settings != self.raw.get("settings")
            or self.pages != self.raw.get("pages")
        )

    @property
    def rev(self) -> int:
        return int(self.raw.get("rev", 0))

    def next_rev(self) -> int:
        """The rev to save under.

        Always a bump when anything changed. The agent only pushes at connect time when its rev
        and the device's disagree, so a save that leaves rev alone is invisible to a deck that
        was unplugged while you were editing — it comes back, both sides say 15, and the layout
        you saved never arrives. Reloading from the tray does not need this; surviving a
        replug does.
        """
        return self.rev + 1 if self.dirty else self.rev

    def candidate_raw(self, *, rev: int | None = None) -> dict[str, Any]:
        """The whole layout as it would be saved, for validating and for measuring."""
        merged = dict(self.raw)
        merged["rev"] = self.rev if rev is None else rev
        merged["themes"] = self.themes
        merged["settings"] = self.settings
        merged["pages"] = self.pages
        return merged

    def problems(self) -> list[str]:
        """Everything the agent would refuse this layout for, as it stands right now."""
        try:
            candidate = DeckConfig.from_raw(
                self.candidate_raw(), path=self.path, validate=False
            )
            return candidate.problems()
        except Exception as exc:  # a malformed candidate should not take the window down
            return [f"could not be checked: {exc}"]

    def theme_names(self) -> list[str]:
        return [t.get("name") or f"Theme {i + 1}" for i, t in enumerate(self.themes)]

    def shape_problems(self) -> list[str]:
        """Anything whose keys have drifted from this file's canonical order.

        The writer used to catch drifted themes structurally — it regenerated each block and
        compared it to the file, so a theme with a missing key could not be spliced. A dump has
        no such failure mode: it will write whatever it is handed. So the guard is explicit now,
        and it is what turns the save button off.
        """
        problems = []

        for index, theme in enumerate(self.themes):
            if tuple(theme) != self.field_order:
                name = theme.get("name") or f"themes[{index}]"
                problems.append(
                    f"theme {name}: keys are not the canonical set in canonical order"
                )

        for index, page in enumerate(self.pages):
            where = page.get("id") or f"pages[{index}]"
            if not isinstance(page, dict) or tuple(page) != self.page_order:
                problems.append(
                    f"page {where}: keys are not the canonical set in canonical order "
                    f"({', '.join(self.page_order)})"
                )
                continue

            problems.extend(self._page_content_problems(page, where))

        return problems

    def _page_content_problems(self, page: dict[str, Any], where: str) -> list[str]:
        problems: list[str] = []

        # A firmware page draws its own contents. A grid or a button on one is not an error the
        # device reports — it is parsed, ignored, and paid for in wire bytes forever.
        if page.get("type") in FIRMWARE_PAGE_TYPES:
            if page.get("grid") is not None:
                problems.append(
                    f"page {where}: type is {page.get('type')!r}, which draws its own layout — "
                    "grid should be null"
                )
            if page.get("buttons"):
                problems.append(
                    f"page {where}: type is {page.get('type')!r}, so its buttons are never "
                    "built. Move them to a grid page or delete them"
                )

        for index, button in enumerate(page.get("buttons") or []):
            button_where = (button.get("id") if isinstance(button, dict) else None) \
                or f"{where}.buttons[{index}]"
            if not isinstance(button, dict) or tuple(button) != self.button_order:
                problems.append(
                    f"button {button_where}: keys are not the canonical set in canonical "
                    f"order ({', '.join(self.button_order)})"
                )
                continue

            pos = button.get("pos")
            if pos is None:
                continue
            if not isinstance(pos, dict) or tuple(pos) != POS_FIELD_ORDER:
                problems.append(
                    f"button {button_where}: pos must be null or exactly "
                    f"{{{', '.join(POS_FIELD_ORDER)}}}, in that order"
                )
                continue
            for key, value in pos.items():
                if isinstance(value, bool) or not isinstance(value, int):
                    problems.append(
                        f"button {button_where}: pos.{key} is {value!r}, expected a whole number"
                    )

        return problems

    def layout_problems(self) -> list[str]:
        """Geometry the agent's validator has no opinion about and the firmware never logs.

        DeckConfig checks what the format means; this checks what it looks like. The two worst
        entries here produce nothing at all on the device — a tile past the last column is
        created at its computed x and extends off the 800px edge, and a seventh nav tab is
        clipped by a container that does not scroll. From the deck both read as "it is not
        there", with no log line anywhere.
        """
        problems: list[str] = []

        fits = geometry.nav_capacity()
        if len(self.pages) > fits:
            problems.append(
                f"{len(self.pages)} pages, and the nav bar fits {fits} — "
                f"the last {len(self.pages) - fits} cannot be reached"
            )

        for page in self.pages:
            where = page.get("id") or "?"
            if page.get("type") in FIRMWARE_PAGE_TYPES:
                continue

            cols, rows = _grid_of(page)
            cell_w, cell_h = geometry.cells(cols, rows)
            if cell_w < MIN_TILE_PX or cell_h < MIN_TILE_PX:
                problems.append(
                    f"page {where}: a {cols}x{rows} grid gives {cell_w}x{cell_h}px tiles, "
                    "which is below what a fingertip can reliably hit"
                )

            occupied: dict[tuple[int, int], str] = {}
            flow = 0
            for button in page.get("buttons") or []:
                button_id = button.get("id") or "?"
                pos = button.get("pos")
                w = max(1, _as_int((pos or {}).get("w"), 1))
                h = max(1, _as_int((pos or {}).get("h"), 1))

                if pos is None:
                    if w != 1 or h != 1:  # unreachable while pos is None, kept for symmetry
                        problems.append(f"button {button_id}: a span needs a fixed position")
                    col, row = flow % cols, flow // cols
                    flow += 1
                else:
                    col = _as_int(pos.get("col"), -1)
                    row = _as_int(pos.get("row"), -1)
                    if col < 0 or row < 0:
                        if w != 1 or h != 1:
                            problems.append(
                                f"button {button_id}: pos spans {w}x{h} but has no col/row, "
                                "and auto-flow ignores the span"
                            )
                        col, row = flow % cols, flow // cols
                        flow += 1

                if col + w > cols:
                    problems.append(
                        f"button {button_id}: column {col} + span {w} runs past the "
                        f"{cols}-column grid on page {where}, and the device says nothing"
                    )
                if row + h > rows:
                    problems.append(
                        f"button {button_id}: row {row} + span {h} runs past the "
                        f"{rows}-row grid on page {where}"
                    )

                for dy in range(h):
                    for dx in range(w):
                        cell = (col + dx, row + dy)
                        if cell in occupied:
                            problems.append(
                                f"button {button_id}: overlaps {occupied[cell]} at "
                                f"({cell[0]}, {cell[1]}) on page {where}"
                            )
                        occupied[cell] = button_id

        return problems

    def notices(self) -> list[str]:
        """Worth saying, but not worth refusing a save over.

        The drift check is the mitigation for a real hole in shape_problems(): it compares every
        item against the order *derived from this file*, so if the file's own first button has
        grown a key, the editor adopts that shape and every check passes. Deriving is the right
        default — add a token to the firmware's parser, write it into one button, and the editor
        follows with no change here — but it should not be silent, because the other way to
        arrive at a drifted first item is a bad hand edit.

        Making it a problem instead would invert the trade: a new firmware field would render the
        file unsavable until this module was updated, which is exactly the coupling the
        derivation exists to remove.
        """
        notices: list[str] = []

        for label, derived, literal in (
            ("theme", self.field_order, THEME_FIELD_ORDER),
            ("page", self.page_order, PAGE_FIELD_ORDER),
            ("button", self.button_order, BUTTON_FIELD_ORDER),
        ):
            if derived == literal:
                continue
            extra = [k for k in derived if k not in literal]
            missing = [k for k in literal if k not in derived]
            detail = ", ".join(
                filter(None, [
                    f"extra: {', '.join(extra)}" if extra else "",
                    f"missing: {', '.join(missing)}" if missing else "",
                    "reordered" if not extra and not missing else "",
                ])
            )
            notices.append(
                f"this file's {label} shape differs from the one this editor was built "
                f"against ({detail}); new {label}s will copy the file's"
            )

        try:
            candidate = DeckConfig.from_raw(
                self.candidate_raw(), path=self.path, validate=False
            )
            notices.extend(candidate.warnings())
        except Exception:  # already reported by problems()
            pass

        return notices

    def mark_saved(self, text: str, rev: int) -> None:
        """Adopts what was just written as the new baseline."""
        self.original = text
        self.raw = dict(self.raw)
        self.raw["rev"] = rev
        self.raw["themes"] = copy.deepcopy(self.themes)
        self.raw["settings"] = copy.deepcopy(self.settings)
        self.raw["pages"] = copy.deepcopy(self.pages)


def _retarget_theme(action: Any, old: str, new: str) -> None:
    """Rewrites `theme` action targets through nested seq steps."""
    if not isinstance(action, dict):
        return
    if action.get("type") == "theme" and action.get("target") == old:
        action["target"] = new
    for step in action.get("steps") or []:
        _retarget_theme(step, old, new)


def _retarget_page(action: Any, old: str, new: str) -> None:
    """The same for `page` targets. Same shape, because the failure is the same shape."""
    if not isinstance(action, dict):
        return
    if action.get("type") == "page" and action.get("target") == old:
        action["target"] = new
    for step in action.get("steps") or []:
        _retarget_page(step, old, new)


def _retarget_page_in(page: dict[str, Any], old: str | None, new: str) -> None:
    if not old:
        return
    for button in page.get("buttons") or []:
        for slot in ("action", "hold"):
            _retarget_page(button.get(slot), old, new)


def _targets_page(action: Any, page_id: str | None) -> bool:
    if not isinstance(action, dict):
        return False
    if action.get("type") == "page" and action.get("target") == page_id:
        return True
    return any(_targets_page(step, page_id) for step in action.get("steps") or [])


def _as_int(value: Any, fallback: int) -> int:
    """ArduinoJson's `variant | default`, which is how the firmware reads every pos field."""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _grid_of(page: dict[str, Any]) -> tuple[int, int]:
    """Mirrors ui_builder.cpp's `page.cols > 0 ? page.cols : 4`."""
    grid = page.get("grid") or {}
    cols, rows = _as_int(grid.get("cols"), 0), _as_int(grid.get("rows"), 0)
    return (cols if cols > 0 else 4, rows if rows > 0 else 3)


def _slug(text: str) -> str:
    """A plain-ASCII id from a label, since ids cross the wire and get compared with `==`."""
    cleaned = re.sub(r"[^a-z0-9]+", ".", (text or "").lower()).strip(".")
    return cleaned


def _unique(base: str, taken: set) -> str:
    """`base-2`, not `base 2`.

    Ids are matched exactly by the firmware and appear in `page`/`theme` action targets, so a
    space in one is a thing that works right up until someone retypes it.
    """
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    return base
