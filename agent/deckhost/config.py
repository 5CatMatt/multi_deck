"""Loads deck.json and indexes it for lookup by button id.

The agent holds the master copy. The device caches a copy on its SD card and is told to
refresh whenever the revisions disagree.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Action types the device performs itself. The agent never executes these — it only sends
# them back as `hid_exec` when it is sequencing a mixed macro.
DEVICE_LOCAL_TYPES = frozenset({"hid", "hid_text", "media", "page", "theme"})

# Action types only the agent can perform.
AGENT_TYPES = frozenset({"launch", "ahk", "shell"})

# `theme` targets that never name a theme.
THEME_KEYWORDS = frozenset({"", "next", "prev"})

# Theme fields the firmware parses as colours, and the form it accepts: an optional '#' then
# exactly six hex digits. Anything else is ignored and the built-in default kept — silently, on
# the device, where you cannot see it. Catching it here is the difference between a typo you
# find in a second and a colour that mysteriously refuses to change.
THEME_COLOR_FIELDS = (
    "bg",
    "tile",
    "tile_grad",
    "border",
    "accent",
    "text",
    "text_muted",
    "ok",
    "idle",
)
COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

THEME_DISPLAY_VALUES = frozenset({"icon_text", "icon", "text"})


class ConfigError(Exception):
    pass


def is_device_local(action: dict[str, Any]) -> bool:
    """Mirrors Action::isLocal() in the firmware.

    A button whose whole action tree is device-local never reaches the agent — the device
    runs it directly, which is what keeps the ten-key working with the agent closed. The two
    implementations must agree, so this one exists to be tested against the same cases.
    """
    kind = action.get("type")

    if kind in DEVICE_LOCAL_TYPES or kind == "delay":
        return True

    if kind == "seq":
        return all(is_device_local(step) for step in action.get("steps", []))

    return False


def default_deck_path() -> Path:
    """Repo-relative default: agent/deckhost/config.py -> <repo>/sdcard/deck.json."""
    return Path(__file__).resolve().parents[2] / "sdcard" / "deck.json"


@dataclass
class DeckConfig:
    rev: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    buttons: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> DeckConfig:
        path = path or default_deck_path()

        if not path.exists():
            raise ConfigError(f"no deck.json at {path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

        config = cls(rev=int(raw.get("rev", 0)), raw=raw, path=path)
        config._index()
        config.validate()
        return config

    def _index(self) -> None:
        self.buttons = {}
        for page in self.raw.get("pages", []):
            for button in page.get("buttons", []):
                button_id = button.get("id")
                if button_id:
                    self.buttons[button_id] = button

    def theme_names(self) -> list[str]:
        """Theme names in file order, matching how the firmware names them.

        Mirrors DeckConfig::parse(): a `themes` array wins, a legacy single `theme` object is
        treated as a one-element list, and an unnamed theme gets a positional name.
        """
        raw_themes = self.raw.get("themes")
        if raw_themes is None:
            single = self.raw.get("theme")
            raw_themes = [single] if isinstance(single, dict) else []

        names = []
        for index, theme in enumerate(raw_themes):
            name = (theme or {}).get("name") or f"Theme {index + 1}"
            names.append(name)
        return names

    def validate(self) -> None:
        """Catches the layout mistakes that would otherwise surface as a dead tile."""
        problems: list[str] = []

        page_ids = {p.get("id") for p in self.raw.get("pages", [])}
        theme_names = set(self.theme_names())
        seen: set[str] = set()

        for page in self.raw.get("pages", []):
            for button in page.get("buttons", []):
                button_id = button.get("id")

                if not button_id:
                    problems.append(f"page {page.get('id')!r}: a button has no id")
                    continue

                if button_id in seen:
                    problems.append(f"duplicate button id {button_id!r}")
                seen.add(button_id)

                action = button.get("action") or {}
                self._validate_action(
                    action, button_id, page_ids, theme_names, problems
                )

        start_theme = (self.raw.get("settings") or {}).get("theme")
        if start_theme and start_theme not in theme_names:
            problems.append(
                f"settings.theme {start_theme!r} matches no theme "
                f"(have: {', '.join(sorted(theme_names)) or 'none'})"
            )

        self._validate_themes(problems)

        if problems:
            raise ConfigError("deck.json problems:\n  " + "\n  ".join(problems))

    def _validate_themes(self, problems: list[str]) -> None:
        """Rejects theme values the firmware would silently ignore.

        The device keeps its default for anything it cannot parse, which looks exactly like
        the edit never arrived — the failure mode is a colour that refuses to change with no
        error anywhere. Numeric fields are clamped rather than ignored, so only a wrong *type*
        is worth flagging there.
        """
        raw_themes = self.raw.get("themes")
        if raw_themes is None:
            single = self.raw.get("theme")
            raw_themes = [single] if isinstance(single, dict) else []

        for index, theme in enumerate(raw_themes):
            if not isinstance(theme, dict):
                problems.append(f"themes[{index}] is not an object")
                continue

            where = theme.get("name") or f"themes[{index}]"

            for field_name in THEME_COLOR_FIELDS:
                if field_name not in theme:
                    continue
                value = theme[field_name]
                if not isinstance(value, str) or not COLOR_RE.match(value):
                    problems.append(
                        f"theme {where}: {field_name} is {value!r}, "
                        "expected six hex digits like '#1b2129'"
                    )

            for field_name in ("tile_opa", "border_opa", "radius"):
                if field_name in theme and not isinstance(theme[field_name], int):
                    problems.append(
                        f"theme {where}: {field_name} is {theme[field_name]!r}, "
                        "expected a whole number"
                    )

            display = theme.get("display")
            if display is not None and display not in THEME_DISPLAY_VALUES:
                problems.append(
                    f"theme {where}: display is {display!r}, expected one of "
                    f"{', '.join(sorted(THEME_DISPLAY_VALUES))}"
                )

    def _validate_action(
        self,
        action: dict[str, Any],
        button_id: str,
        page_ids: set[str | None],
        theme_names: set[str],
        problems: list[str],
    ) -> None:
        kind = action.get("type")

        if kind is None:
            problems.append(f"{button_id}: action has no type")
            return

        if kind == "page" and action.get("target") not in page_ids:
            # A typo here produces a button that looks fine and silently does nothing.
            problems.append(
                f"{button_id}: navigates to unknown page {action.get('target')!r}"
            )

        if kind == "theme":
            target = action.get("target") or ""
            if target not in THEME_KEYWORDS and target not in theme_names:
                problems.append(
                    f"{button_id}: selects unknown theme {target!r} "
                    f"(have: {', '.join(sorted(theme_names)) or 'none'})"
                )

        if kind == "seq":
            for step in action.get("steps", []):
                self._validate_action(
                    step, button_id, page_ids, theme_names, problems
                )

    def button(self, button_id: str) -> dict[str, Any] | None:
        return self.buttons.get(button_id)

    def action_for(self, button_id: str) -> dict[str, Any] | None:
        button = self.button(button_id)
        return button.get("action") if button else None
