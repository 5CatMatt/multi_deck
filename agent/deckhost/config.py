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

# What the device appends to a button id when reporting a long press it could not run itself.
# Set in firmware/multi_deck/ui_builder.cpp; the two have to agree or holds go nowhere.
HOLD_SUFFIX = ".hold"

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

# ---------------------------------------------------------------------------
# What each action type needs to do anything
# ---------------------------------------------------------------------------
#
# None of this was checked until the editor learned to write actions. By hand you copy a working
# button and change the target; from a form you can produce `{"type": "launch"}` with two clicks
# and no target at all, and every one of these failures is silent from where you are standing —
# the agent logs "launch action has no target" to a file nobody opens, the device logs a rejected
# chord to a UART nothing is attached to, and the tile just does nothing when pressed.
#
# The list mirrors the guards already in actions.py and the parse switch in
# firmware/multi_deck/deck_config.cpp::parseActionJson. Only the field that makes an action
# meaningful is required; `args` and `cwd` are genuinely optional.
ACTION_REQUIRED = {
    "launch": "target",
    "shell": "cmd",
    "ahk": "fn",
    "hid": "keys",
    "hid_text": "text",
    "media": "key",
    "page": "target",
    "seq": "steps",
    "delay": "ms",
    # `theme` is deliberately absent: an empty target means "next", which is a real thing to
    # write. THEME_KEYWORDS covers it.
}

ACTION_TYPES = frozenset(DEVICE_LOCAL_TYPES | AGENT_TYPES | {"delay", "seq"})

# Media keys, mirroring the strcmp chain in firmware/multi_deck/hid.cpp::sendMedia. An unknown
# one logs and does nothing.
MEDIA_KEYS = frozenset(
    {"play_pause", "next", "prev", "stop", "mute", "vol_up", "vol_down"}
)

# Key tokens, mirroring kNamedKeys and kNamedModifiers in firmware/multi_deck/hid.cpp. Matching
# there is case-insensitive (the token is upper-cased first), so these are stored upper-case and
# compared upper-case.
HID_MODIFIERS = frozenset(
    {"CTRL", "CONTROL", "SHIFT", "ALT", "GUI", "WIN", "CMD", "ALTGR"}
)

HID_KEY_NAMES = frozenset(
    """
    ENTER RETURN ESC ESCAPE BACKSPACE TAB SPACE MINUS EQUAL LBRACKET RBRACKET BACKSLASH
    SEMICOLON QUOTE GRAVE COMMA PERIOD SLASH CAPSLOCK
    F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F12
    PRINTSCREEN SCROLLLOCK PAUSE INSERT HOME PAGEUP DELETE END PAGEDOWN RIGHT LEFT DOWN UP MENU
    NUMLOCK KP_SLASH KP_ASTERISK KP_MINUS KP_PLUS KP_ENTER
    KP_1 KP_2 KP_3 KP_4 KP_5 KP_6 KP_7 KP_8 KP_9 KP_0 KP_DOT
    """.split()
)

# A USB keyboard report carries six non-modifier keys. The firmware rejects the whole chord past
# that rather than truncating it, so the seventh key does not cost you one key — it costs you the
# keypress.
HID_MAX_KEYS = 6


def resolve_hid_token(token: str) -> str | None:
    """Mirrors resolveToken() in hid.cpp. Returns "modifier", "key", or None.

    The single-character branch is the part worth reproducing exactly: letters and digits map
    arithmetically onto the usage page rather than appearing in any table, and an upper-case
    letter implies SHIFT — so `["A"]` is Shift+A and `["a"]` is a, which is not obvious from
    looking at deck.json and is the kind of thing an editor should be able to explain.
    """
    if not isinstance(token, str) or not token:
        return None

    upper = token.upper()
    if upper in HID_MODIFIERS:
        return "modifier"
    if upper in HID_KEY_NAMES:
        return "key"
    if len(token) == 1 and (token.isascii() and (token.isalpha() or token.isdigit())):
        return "key"
    return None


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


# Function definitions in lib.ahk: a name at column zero followed by a parameter list. AHK v2
# has no other way to declare one, so this needs no more than it looks like it needs.
AHK_DEF_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(", re.MULTILINE)


def ahk_functions() -> set[str] | None:
    """The helpers agent/ahk/lib.ahk defines, or None if there is no lib.ahk to read.

    None is a third answer, not an empty set: the theme builder ships as an exe with no checkout
    beside it, and "this deck references no AHK functions at all" is a very different statement
    from "I cannot see the file". Only the first is worth putting on screen.
    """
    lib = Path(__file__).resolve().parents[1] / "ahk" / "lib.ahk"
    try:
        text = lib.read_text(encoding="utf-8")
    except OSError:
        return None
    return set(AHK_DEF_RE.findall(text))


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

        pages = self.raw.get("pages") or []
        page_ids = {p.get("id") for p in pages}
        theme_names = set(self.theme_names())
        seen: set[str] = set()
        seen_pages: set[str] = set()

        for index, page in enumerate(pages):
            page_type = page.get("type", "grid")
            if page_type not in PAGE_TYPES:
                problems.append(
                    f"page {page.get('id')!r}: type is {page_type!r}, expected one of "
                    f"{', '.join(sorted(PAGE_TYPES))}"
                )

            # Button ids have always been checked for uniqueness; page ids never were, and they
            # are looked up the same way — the firmware takes the first match, so a duplicate
            # makes every nav button pointing at that id go to whichever page comes first, and
            # the other page becomes unreachable without anything saying so.
            page_id = page.get("id")
            if not page_id:
                problems.append(f"pages[{index}] has no id, so nothing can navigate to it")
            elif page_id in seen_pages:
                problems.append(f"duplicate page id {page_id!r}")
            else:
                seen_pages.add(page_id)

            for button in page.get("buttons") or []:
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

                # A long press is an entirely separate action, parsed by the same code
                # (deck_config.cpp:296) and never validated here until now. It is also the
                # least visible thing on the deck — nothing about a tile says it has one — so
                # a broken hold can sit in a layout indefinitely.
                if button.get("hold") is not None:
                    self._validate_action(
                        button["hold"], f"{button_id} (hold)", page_ids, theme_names, problems
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
        if not isinstance(action, dict):
            problems.append(f"{button_id}: action is {action!r}, expected an object")
            return

        kind = action.get("type")

        if kind is None:
            problems.append(f"{button_id}: action has no type")
            return

        if kind not in ACTION_TYPES:
            problems.append(
                f"{button_id}: action type is {kind!r}, expected one of "
                f"{', '.join(sorted(ACTION_TYPES))}"
            )
            return

        required = ACTION_REQUIRED.get(kind)
        if required is not None and not action.get(required):
            # `not` rather than `is None` on purpose: "", [] and 0 are all as useless here as an
            # absent key, and all three are what an editor produces from an untouched field.
            problems.append(
                f"{button_id}: {kind} action has no {required}, so pressing it does nothing"
            )
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

        if kind == "media" and action.get("key") not in MEDIA_KEYS:
            problems.append(
                f"{button_id}: media key {action.get('key')!r} is not one of "
                f"{', '.join(sorted(MEDIA_KEYS))}"
            )

        if kind == "hid":
            self._validate_chord(action, button_id, problems)

        if kind == "delay":
            ms = action.get("ms")
            if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
                problems.append(f"{button_id}: delay ms is {ms!r}, expected a whole number")

        if kind == "seq":
            for step in action.get("steps") or []:
                self._validate_action(
                    step, button_id, page_ids, theme_names, problems
                )

    def _validate_chord(
        self, action: dict[str, Any], button_id: str, problems: list[str]
    ) -> None:
        """Rejects a key chord the device would reject.

        sendCombo() refuses the whole chord on the first token it cannot resolve, and again past
        six non-modifier keys. Both write a line to UART0 and then do nothing, so from the deck
        the failure is a tile that no longer types anything.
        """
        keys = action.get("keys")
        if not isinstance(keys, list):
            problems.append(f"{button_id}: hid keys is {keys!r}, expected a list of tokens")
            return

        pressed = 0
        for token in keys:
            resolved = resolve_hid_token(token)
            if resolved is None:
                problems.append(
                    f"{button_id}: key token {token!r} is not one the device knows, "
                    "so the whole chord is rejected. Modifiers: "
                    f"{', '.join(sorted(HID_MODIFIERS))}; or a single letter or digit"
                )
                return
            if resolved == "key":
                pressed += 1

        if pressed > HID_MAX_KEYS:
            problems.append(
                f"{button_id}: {pressed} non-modifier keys, and a USB report carries "
                f"{HID_MAX_KEYS} — the device rejects the whole chord"
            )
        elif pressed == 0:
            problems.append(
                f"{button_id}: modifiers only ({', '.join(keys)}), so nothing is typed"
            )

    def actions(self):
        """Yields (subject, action) for every action in the layout, holds and seq steps included.

        One walk, so a check added later cannot quietly miss the places `_validate_action`
        already reaches — which is exactly how `hold` went unvalidated for as long as it did.
        """

        def walk(action: Any, subject: str):
            if not isinstance(action, dict):
                return
            yield subject, action
            for step in action.get("steps") or []:
                yield from walk(step, subject)

        for page in self.raw.get("pages") or []:
            for button in page.get("buttons") or []:
                where = button.get("id") or f"page {page.get('id')!r}"
                yield from walk(button.get("action"), where)
                yield from walk(button.get("hold"), f"{where} (hold)")

    def warnings(self) -> list[str]:
        """Things worth saying that must not stop a load.

        The distinction matters because `problems()` is what makes the agent refuse to start.
        `ahk.fn` names a function in agent/ahk/lib.ahk, which is a file you are meant to edit —
        an unrecognised name is as likely to be a helper you have not written yet as a typo, and
        refusing to boot the agent over the first would be wrong. So it warns, and the editor
        shows it next to the problems rather than instead of them.
        """
        known = ahk_functions()
        if known is None:
            return []  # no checkout to compare against; the packaged editor is one of these

        found: list[str] = []
        for subject, action in self.actions():
            if action.get("type") != "ahk":
                continue
            fn = action.get("fn")
            if fn and fn not in known and fn not in found:
                found.append(
                    f"{subject}: ahk function {fn!r} is not in agent/ahk/lib.ahk "
                    f"(have: {', '.join(sorted(known)) or 'none'})"
                )
        return found

    def button(self, button_id: str) -> dict[str, Any] | None:
        return self.buttons.get(button_id)

    def action_for(self, button_id: str) -> dict[str, Any] | None:
        """The action a press frame names, including the long-press form.

        A long press the device cannot run itself arrives as `<id>.hold`
        (firmware/multi_deck/ui_builder.cpp:374), and there is no button with that id — the
        index is keyed by the ids written in deck.json. So every agent-side hold answered with
        "Unknown button" and a toast, which reads as a corrupt layout rather than a missing six
        lines here. A device-local hold worked fine, which is what kept it hidden.

        The exact id is tried first, so a button somebody genuinely named `foo.hold` still wins
        over `foo`'s long press.
        """
        button = self.button(button_id)
        if button is not None:
            return button.get("action")

        if button_id.endswith(HOLD_SUFFIX):
            base = self.button(button_id[: -len(HOLD_SUFFIX)])
            if base is not None:
                return base.get("hold")

        return None
