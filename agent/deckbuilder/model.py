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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deckhost.config import DeckConfig

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
