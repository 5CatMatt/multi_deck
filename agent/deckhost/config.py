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

# Numeric theme fields. The firmware clamps these rather than ignoring them, so only a wrong
# *type* is worth flagging — `"radius": 200` is defined behaviour, `"radius": "16"` is a typo.
THEME_NUMERIC_FIELDS = ("tile_opa", "border_opa", "radius", "dim_opa")

# ---------------------------------------------------------------------------
# Writing the default down
# ---------------------------------------------------------------------------
#
# Every theme field is optional, and for a while the *only* way to say "use the default" was to
# leave the key out. That made objects of the same kind different shapes — two themes side by
# side, one with `display` and one without — and gave the silent one nothing to read. Worse, the
# obvious guess at writing it down was rejected here, so the format punished the person trying
# to be consistent.
#
# So every field now has a written form of "unset": `null` for numbers, booleans and colours,
# `""` for the two string fields. The firmware needed no changes to accept them — `parseColor`
# returns false for a null variant, `parsePercent` fails `is<int>()`, and ArduinoJson's `|`
# yields the fallback — which is the same path an absent key already took.
#
# This matters most for the fields whose default comes from config.h rather than from the
# format: `dim_opa` is 0 with the PWM backlight rewire and 55 without it, and `flip180` follows
# MD_ROTATE_180. Writing a literal into deck.json overrides the build. `null` lets a theme carry
# the key without taking that decision away from config.h.
DISPLAY_UNSET = ("", None)

# Page types the firmware knows, mirroring the strcmp chain in DeckConfig::parse().
#
# Worth validating because the firmware's fallback for an unrecognised type is `grid` — and a
# `"type": "calender"` typo therefore produces a page that builds, navigates and renders as an
# empty grid, with nothing anywhere saying the type was ignored.
PAGE_TYPES = frozenset({"grid", "numpad", "stats", "calendar", "colortest"})

# LVGL built-in symbols the firmware can render, mirroring kIcons in
# firmware/multi_deck/icons.cpp. The naming rule there is mechanical — the LV_SYMBOL_ name
# lowercased — so this list is the same list, not a translation of it.
#
# Worth validating because the firmware's fallback for an unknown name is to show the tile's
# text label, which is indistinguishable from the `icon` field being ignored entirely. That is
# the same silent-default failure the theme colour check exists for.
ICON_NAMES = frozenset(
    """
    audio backspace bars battery_1 battery_2 battery_3 battery_empty battery_full bell
    bluetooth bullet call charge close copy cut directory down download drive edit eject
    envelope eye_close eye_open file gps home image keyboard left list loop minus mute
    new_line next ok paste pause play plus power prev refresh right save sd_card settings
    shuffle stop tint trash up upload usb video volume_max volume_mid warning wifi
    """.split()
)


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


def check_display(value: Any, subject: str, problems: list[str]) -> None:
    """One rule for `display`, shared by all three levels it can appear at.

    `subject` names the field the way the reader wrote it — "settings.display",
    "theme Kiosk: display", "edit.paste_plain: display" — so the message points at a line rather
    than at a level of the format.
    """
    if value in DISPLAY_UNSET:
        return

    if value not in THEME_DISPLAY_VALUES:
        problems.append(
            f"{subject} is {value!r}, expected one of "
            f'{", ".join(sorted(THEME_DISPLAY_VALUES))} — or "" to take the level above'
        )


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

        return cls.from_raw(raw, path=path)

    @classmethod
    def from_raw(
        cls, raw: dict[str, Any], path: Path | None = None, *, validate: bool = True
    ) -> DeckConfig:
        """Indexes a layout that is already in memory, and by default validates it too.

        The half of load() after the file read, split out because the theme builder holds a
        candidate layout it has not written yet and needs to know whether it is legal on every
        keystroke. Writing it to a temp file to find out would work and would be silly.

        `validate=False` still indexes, and leaves you to call `problems()` yourself. That is
        what an editor wants: it needs the list on every keystroke, and half the entries are
        transient — a colour is invalid for as long as it takes to type the third hex digit,
        which is not an occasion to raise.

        `path` is still worth passing when there is one: it is what `asset_root` reads to
        resolve a wallpaper, so a candidate loaded without it cannot say where its images live.
        """
        config = cls(rev=int(raw.get("rev", 0)), raw=raw, path=path)
        config._index()
        if validate:
            config.validate()
        return config

    @property
    def asset_root(self) -> Path | None:
        """The folder that gets copied to the SD card: the one holding deck.json.

        Wallpapers and icons are addressed from the card's root, so `/wall/dusk.bin` on the
        device is `sdcard/wall/dusk.bin` here. That makes deck.json's own directory the
        authority on what the card should contain, and means the asset stamp needs no
        configuration of its own.
        """
        return self.path.parent if self.path is not None else None

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
        problems = self.problems()
        if problems:
            raise ConfigError("deck.json problems:\n  " + "\n  ".join(problems))

    def problems(self) -> list[str]:
        """The same checks as validate(), returned rather than raised.

        The agent wants an exception — a layout it cannot trust should stop the load. An editor
        wants the list, because it is showing you the problems *while* you create them and half
        of them are transient: a colour is invalid for as long as it takes to type the third
        hex digit. Reassembling that list by splitting the exception message would work right
        up until a problem string contained a newline.
        """
        problems: list[str] = []

        page_ids = {p.get("id") for p in self.raw.get("pages", [])}
        theme_names = set(self.theme_names())
        seen: set[str] = set()

        for page in self.raw.get("pages", []):
            page_type = page.get("type", "grid")
            if page_type not in PAGE_TYPES:
                problems.append(
                    f"page {page.get('id')!r}: type is {page_type!r}, expected one of "
                    f"{', '.join(sorted(PAGE_TYPES))}"
                )

            for button in page.get("buttons", []):
                button_id = button.get("id")

                if not button_id:
                    problems.append(f"page {page.get('id')!r}: a button has no id")
                    continue

                if button_id in seen:
                    problems.append(f"duplicate button id {button_id!r}")
                seen.add(button_id)

                self._validate_button(button, button_id, problems)

                action = button.get("action") or {}
                self._validate_action(
                    action, button_id, page_ids, theme_names, problems
                )

        settings = self.raw.get("settings") or {}
        # An empty baseline is legal but pointless: the chain has nowhere left to fall through
        # to, so the firmware's own default (`icon_text`) is what a tile ends up with.
        check_display(settings.get("display"), "settings.display", problems)

        start_theme = settings.get("theme")
        if start_theme and start_theme not in theme_names:
            problems.append(
                f"settings.theme {start_theme!r} matches no theme "
                f"(have: {', '.join(sorted(theme_names)) or 'none'})"
            )

        self._validate_timings(settings, problems)

        self._validate_themes(problems)

        return problems

    # Seconds-valued settings, and the smallest value that is not simply "off". ArduinoJson's
    # `| default` yields the default for a wrong *type*, so a string here reads as an edit that
    # never arrived — the same silent class of failure as the theme fields below.
    TIMING_SETTINGS = ("idle_dim_s", "idle_off_s", "sleep_clock_s")

    def _validate_timings(self, settings: dict, problems: list[str]) -> None:
        for key in self.TIMING_SETTINGS:
            value = settings.get(key)
            if value is None:
                continue
            # bool is an int subclass, and `true` here is a mistake worth naming.
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"settings.{key} is {value!r}, expected a whole number of seconds")
            elif value < 0:
                problems.append(f"settings.{key} is {value}, expected 0 (off) or more")

        dim = settings.get("idle_dim_s")
        off = settings.get("idle_off_s")
        if isinstance(dim, int) and isinstance(off, int) and 0 < off < dim:
            # Not fatal — the firmware tests Off first precisely so this still reaches Off — but
            # it means the dim stage never appears, which is rarely what someone meant to write.
            problems.append(
                f"settings.idle_off_s ({off}) is below idle_dim_s ({dim}), "
                "so the screen goes off without ever dimming"
            )

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
                value = theme.get(field_name)
                if value is None:
                    continue
                if not isinstance(value, str) or not COLOR_RE.match(value):
                    problems.append(
                        f"theme {where}: {field_name} is {value!r}, "
                        "expected six hex digits like '#1b2129' — or null for the default"
                    )

            for field_name in THEME_NUMERIC_FIELDS:
                value = theme.get(field_name)
                if value is None:
                    continue
                # bool is an int subclass, and `"radius": true` is a mistake worth naming.
                if isinstance(value, bool) or not isinstance(value, int):
                    problems.append(
                        f"theme {where}: {field_name} is {value!r}, "
                        "expected a whole number — or null for the default"
                    )

            flip = theme.get("flip180")
            if flip is not None and not isinstance(flip, bool):
                problems.append(
                    f"theme {where}: flip180 is {flip!r}, expected true, false or null"
                )

            check_display(theme.get("display"), f"theme {where}: display", problems)

    def _validate_button(
        self, button: dict[str, Any], button_id: str, problems: list[str]
    ) -> None:
        """Checks the presentation fields, which fail silently on the device.

        Both of these degrade to "show the label" when the firmware cannot make sense of them,
        so a typo costs you the icon and tells you nothing.
        """
        check_display(button.get("display"), f"{button_id}: display", problems)

        icon = button.get("icon")
        if not icon:
            return

        if not isinstance(icon, str):
            problems.append(f"{button_id}: icon is {icon!r}, expected a string")
            return

        # A leading slash means an image on the SD card. Its existence cannot be checked from
        # here — the card is the authority, and the asset stamp is what catches a stale one —
        # so only the extension is worth a word.
        if icon.startswith("/"):
            if not icon.endswith(".bin"):
                problems.append(
                    f"{button_id}: icon {icon!r} looks like a path but is not a .bin — "
                    "convert it with tools/make_assets.py icon"
                )
            return

        if icon not in ICON_NAMES:
            problems.append(
                f"{button_id}: icon {icon!r} is not a built-in symbol. Use an SD path "
                "starting with '/', or one of: " + ", ".join(sorted(ICON_NAMES))
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
