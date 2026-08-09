"""Parking things on disk so they can come back.

The layout crosses to the deck as one 8192-byte line, and two thirds of it is pages and buttons.
That makes the budget the binding constraint on everything the editor can do — and until now the
only lever was deleting a theme, which buys about 292 bytes and loses the theme. Nobody deletes a
page they spent an evening on; they leave it there unpressed, paying for itself in bytes forever.

So: park it. Write it to a file, take it out of the deck, and put it back when you want it. That
is the whole idea, and everything below is the consequences of it being reversible.

**The file format** is an envelope with a manifest:

    {
      "multi_deck": 1,
      "kind": "page",
      "created": "2026-08-09T14:02:11",
      "from":   {"deck": "…/sdcard/deck.json", "rev": 16},
      "origin": {"index": 1},
      "assets": [{"path": "/wall/stars.bin", "sha256": "…", "bytes": 768008}],
      "items":  [ … ]
    }

One `kind` per file. Mixed bundles force a dependency-ordering question — import the theme before
the button that switches to it, or after? — that this does not need to answer, and a wrong answer
is a silent one.

`assets` is a manifest and not a payload. A wallpaper is 768 KB and would become a megabyte of
base64 inside a file whose main virtue is that you can open it in an editor and read it. The
manifest is enough to say *which* image is missing and whether the one you have is the same one.

There is no byte cost in the file. Derived numbers in a saved file go stale, and a stale cost is
worse than none, because it is the number someone decides on.

**Reading sniffs.** A `multi_deck` key means a fragment; a `pages` or `themes` key means a whole
deck.json. That one branch gives "import a theme out of another deck" for free, and pointing the
same dialog at the builder's own backup folder turns the existing twenty-file rotation into
undo-across-sessions.

**Reshaping is asymmetric on purpose.** An item missing keys this deck has is filled from the
template and noted — that is an older export, and the defaults are the same defaults. An item with
keys this deck does not have is refused and the extras named, because dropping a key the firmware
reads changes what the thing does, quietly, in a way you would find out on the deck.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from deckbuilder import budget
from deckbuilder.model import (
    BUTTON_TEMPLATE,
    PAGE_TEMPLATE,
    THEME_TEMPLATE,
    DeckDoc,
    ModelError,
    _retarget_page_in,
    _unique,
)

FORMAT_VERSION = 1
EXTENSION = ".mdpart.json"

KINDS = ("theme", "page", "button")

TEMPLATES = {
    "theme": THEME_TEMPLATE,
    "page": PAGE_TEMPLATE,
    "button": BUTTON_TEMPLATE,
}


class LibraryError(Exception):
    pass


# -- reading and writing ------------------------------------------------------------------


@dataclass
class Fragment:
    """What was in the file, however the file was shaped."""

    kind: str
    items: list[dict[str, Any]]
    path: Path
    source: str = "fragment"  # or "deck", for a whole deck.json opened as a source
    origin: dict[str, Any] = field(default_factory=dict)
    assets: list[dict[str, Any]] = field(default_factory=list)
    created: str = ""
    rev: int | None = None

    def label(self) -> str:
        names = [_name_of(self.kind, item) for item in self.items]
        listed = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        return f"{len(self.items)} {self.kind}{'s' if len(self.items) != 1 else ''}: {listed}"


def envelope(
    kind: str,
    items: list[dict[str, Any]],
    *,
    deck_path: Path | None,
    rev: int,
    origin: dict[str, Any] | None = None,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise LibraryError(f"unknown kind {kind!r}")

    return {
        "multi_deck": FORMAT_VERSION,
        "kind": kind,
        # Seconds, local, no timezone: this is a note for a human reading the folder listing,
        # not a field anything sorts on.
        "created": datetime.now().replace(microsecond=0).isoformat(),
        "from": {"deck": str(deck_path) if deck_path else None, "rev": rev},
        "origin": origin or {},
        "assets": asset_manifest(items, asset_root),
        "items": copy.deepcopy(items),
    }


def write(path: Path, data: dict[str, Any]) -> Path:
    path = path.with_suffix(path.suffix or ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def read(path: Path) -> Fragment:
    """Loads a fragment, a whole deck.json, or one of the builder's backups."""
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LibraryError(f"{path.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise LibraryError(f"{path.name} is not a multi_deck file")

    if "multi_deck" in raw:
        return _read_fragment(path, raw)
    if "pages" in raw or "themes" in raw:
        return _read_deck(path, raw)

    raise LibraryError(
        f"{path.name} is neither a library file nor a deck.json — no 'multi_deck', "
        "'pages' or 'themes' key"
    )


def _read_fragment(path: Path, raw: dict[str, Any]) -> Fragment:
    version = raw.get("multi_deck")
    if version != FORMAT_VERSION:
        raise LibraryError(
            f"{path.name} is format version {version!r}; this build reads {FORMAT_VERSION}"
        )

    kind = raw.get("kind")
    if kind not in KINDS:
        raise LibraryError(f"{path.name}: kind is {kind!r}, expected one of {', '.join(KINDS)}")

    items = raw.get("items")
    if not isinstance(items, list) or not items:
        raise LibraryError(f"{path.name}: no items")
    if not all(isinstance(item, dict) for item in items):
        raise LibraryError(f"{path.name}: every item must be an object")

    return Fragment(
        kind=kind,
        items=copy.deepcopy(items),
        path=path,
        origin=raw.get("origin") or {},
        assets=raw.get("assets") or [],
        created=raw.get("created") or "",
        rev=(raw.get("from") or {}).get("rev"),
    )


def _read_deck(path: Path, raw: dict[str, Any]) -> Fragment:
    """A whole deck.json, offered as a source to pick items out of.

    The kind is not stated anywhere in a deck file, so it is decided by the caller through
    `pick()`. Themes come back by default because that is what the folder full of backups is
    most often opened for.
    """
    return Fragment(
        kind="theme",
        items=copy.deepcopy(raw.get("themes") or []),
        path=path,
        source="deck",
        created="",
        rev=raw.get("rev"),
    )


def pick(fragment: Fragment, kind: str, raw: dict[str, Any]) -> Fragment:
    """Re-reads a whole-deck source as a different kind."""
    if fragment.source != "deck":
        raise LibraryError("only a whole deck.json can be re-read as another kind")

    if kind == "theme":
        items = raw.get("themes") or []
    elif kind == "page":
        items = raw.get("pages") or []
    elif kind == "button":
        items = [b for p in raw.get("pages") or [] for b in (p.get("buttons") or [])]
    else:
        raise LibraryError(f"unknown kind {kind!r}")

    return Fragment(
        kind=kind, items=copy.deepcopy(items), path=fragment.path, source="deck",
        rev=fragment.rev,
    )


# -- assets -------------------------------------------------------------------------------


def asset_paths(kind: str, item: dict[str, Any]) -> list[str]:
    """Card paths an item needs. Images never travel over USB, so these are the loose ends."""
    found: list[str] = []

    if kind == "theme":
        wallpaper = item.get("wallpaper")
        if isinstance(wallpaper, str) and wallpaper.startswith("/"):
            found.append(wallpaper)
        return found

    buttons = [item] if kind == "button" else (item.get("buttons") or [])
    for button in buttons:
        icon = (button or {}).get("icon")
        if isinstance(icon, str) and icon.startswith("/"):
            found.append(icon)
    return found


def asset_manifest(
    items: list[dict[str, Any]], asset_root: Path | None
) -> list[dict[str, Any]]:
    """Hash and size of each referenced image, so a missing one can be told from a changed one."""
    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in items:
        for kind in KINDS:
            for card_path in asset_paths(kind, item):
                if card_path in seen:
                    continue
                seen.add(card_path)
                manifest.append(_describe_asset(card_path, asset_root))

    return manifest


def _describe_asset(card_path: str, asset_root: Path | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": card_path, "sha256": None, "bytes": None}
    local = resolve_asset(card_path, asset_root)
    if local is not None and local.is_file():
        blob = local.read_bytes()
        entry["sha256"] = hashlib.sha256(blob).hexdigest()
        entry["bytes"] = len(blob)
    return entry


def resolve_asset(card_path: str, asset_root: Path | None) -> Path | None:
    """`/wall/dusk.bin` on the card is `<asset_root>/wall/dusk.bin` here."""
    if asset_root is None or not card_path.startswith("/"):
        return None
    return asset_root / card_path.lstrip("/")


# -- importing ----------------------------------------------------------------------------


@dataclass
class Plan:
    """What an import would do, worked out before anything is committed.

    Everything the dialog shows comes from here, byte cost included, because import is where the
    budget actually gets blown and after the fact is the wrong time to find out.
    """

    kind: str
    items: list[dict[str, Any]]
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    missing_assets: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    bytes_delta: int = 0
    used_after: int = 0
    limit: int = budget.LIMIT

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def over_limit(self) -> bool:
        return self.used_after >= self.limit

    def summary(self) -> str:
        percent = self.used_after / self.limit * 100
        return (
            f"adds {self.bytes_delta:,} bytes → {self.used_after:,} / {self.limit:,} "
            f"({percent:.0f}%)"
        )

    def detail(self) -> str:
        if self.over_limit:
            return "Past the limit — the deck would discard the whole layout in silence."
        warn_at = int(self.limit * budget.WARN_FRACTION)
        if self.used_after >= warn_at:
            return "Past the warning line, where the repo's test suite starts failing."
        return f"{warn_at - self.used_after:,} bytes still spare."


def plan_import(doc: DeckDoc, fragment: Fragment, *, into_page: int | None = None) -> Plan:
    """Works out what importing this would do, without doing any of it."""
    plan = Plan(kind=fragment.kind, items=[])

    if fragment.kind == "button" and into_page is None:
        plan.problems.append("choose a page to import the button onto")
        return plan

    template = TEMPLATES[fragment.kind]
    order = {
        "theme": doc.field_order,
        "page": doc.page_order,
        "button": doc.button_order,
    }[fragment.kind]

    taken = _taken_names(doc, fragment.kind)

    for item in fragment.items:
        name = _name_of(fragment.kind, item)

        extra = [key for key in item if key not in order]
        if extra:
            # Refused rather than dropped: a key this deck does not carry is one the firmware may
            # still read, and losing it changes behaviour without saying so.
            plan.problems.append(
                f"{fragment.kind} {name}: has keys this deck does not "
                f"({', '.join(extra)}) — it may be from a newer firmware"
            )
            continue

        missing = [key for key in order if key not in item]
        if missing:
            plan.notes.append(
                f"{fragment.kind} {name}: filled in {', '.join(missing)} from the defaults"
            )

        shaped = {key: copy.deepcopy(item.get(key, template.get(key))) for key in order}
        unique = _unique(name, taken)
        if unique != name:
            plan.renamed.append((name, unique))
            _set_name(fragment.kind, shaped, unique)
        taken.add(unique)

        if fragment.kind == "page":
            _reshape_page(doc, shaped, name, unique, plan)

        if fragment.kind == "button":
            _unpin(shaped, plan)

        plan.items.append(shaped)

    plan.missing_assets = _missing_assets(doc, fragment.kind, plan.items)
    plan.notes.extend(_dangling_targets(doc, fragment.kind, plan.items))
    _measure(doc, plan, into_page)
    return plan


def _reshape_page(
    doc: DeckDoc, page: dict[str, Any], old_id: str, new_id: str, plan: Plan
) -> None:
    # A page's own nav tiles refer to it by id. If the id had to change, they follow it here —
    # otherwise "import the page I parked" lands you back on the original.
    if old_id != new_id:
        _retarget_page_in(page, old_id, new_id)

    # Positions are page-local, and a page brings its own grid — so its pins stay. Only a button
    # arriving on its own loses one, because there is nothing to say what grid it was pinned to.
    taken = doc.button_ids()
    for button in page.get("buttons") or []:
        button_id = button.get("id") or "button"
        unique = _unique(button_id, taken)
        if unique != button_id:
            plan.renamed.append((button_id, unique))
            button["id"] = unique
        taken.add(unique)


def _unpin(button: dict[str, Any], plan: Plan) -> None:
    if button.get("pos") is None:
        return
    plan.notes.append(
        f"button {button.get('id')}: dropped its fixed position — it was relative to a grid "
        "that did not come with it, so it lands at the end of the flow"
    )
    button["pos"] = None


def _dangling_targets(doc: DeckDoc, kind: str, items: list[dict[str, Any]]) -> list[str]:
    """Nav tiles pointing at pages this deck does not have.

    Reported here rather than only at save time, because at save time it is a refusal on a change
    you have already made and can no longer see the shape of. Not a refusal here: importing two
    pages that link to each other is entirely reasonable, and the second one fixes the first.
    """
    if kind == "theme":
        return []

    known = {page.get("id") for page in doc.pages}
    known |= {item.get("id") for item in items} if kind == "page" else set()

    notes: list[str] = []
    for item in items:
        buttons = [item] if kind == "button" else (item.get("buttons") or [])
        for button in buttons:
            for slot in ("action", "hold"):
                for target in _page_targets(button.get(slot)):
                    if target not in known:
                        notes.append(
                            f"button {button.get('id')}: navigates to {target!r}, which this "
                            "deck does not have — add that page or the agent will refuse to load"
                        )
    return notes


def _page_targets(action: Any) -> list[str]:
    if not isinstance(action, dict):
        return []
    found = []
    if action.get("type") == "page" and action.get("target"):
        found.append(action["target"])
    for step in action.get("steps") or []:
        found.extend(_page_targets(step))
    return found


def _missing_assets(doc: DeckDoc, kind: str, items: list[dict[str, Any]]) -> list[str]:
    root = doc.path.parent if doc.path else None
    missing: list[str] = []
    for item in items:
        for card_path in asset_paths(kind, item):
            local = resolve_asset(card_path, root)
            if (local is None or not local.is_file()) and card_path not in missing:
                missing.append(card_path)
    return missing


def _measure(doc: DeckDoc, plan: Plan, into_page: int | None) -> None:
    before = doc.candidate_raw()
    used_before = budget.frame_bytes(doc.next_rev(), before)

    after = copy.deepcopy(before)
    _place(after, plan.kind, plan.items, into_page)

    plan.used_after = budget.frame_bytes(doc.next_rev(), after)
    plan.bytes_delta = plan.used_after - used_before


def _place(
    raw: dict[str, Any], kind: str, items: list[dict[str, Any]], into_page: int | None
) -> None:
    if kind == "theme":
        raw.setdefault("themes", []).extend(copy.deepcopy(items))
    elif kind == "page":
        raw.setdefault("pages", []).extend(copy.deepcopy(items))
    else:
        page = raw["pages"][into_page]
        page.setdefault("buttons", [])
        page["buttons"].extend(copy.deepcopy(items))


def apply(doc: DeckDoc, plan: Plan, *, into_page: int | None = None) -> None:
    """Commits a plan. The caller snapshots first, so the whole import is one undo."""
    if not plan.ok:
        raise LibraryError("this import has unresolved problems")

    if plan.kind == "theme":
        doc.themes.extend(copy.deepcopy(plan.items))
    elif plan.kind == "page":
        doc.pages.extend(copy.deepcopy(plan.items))
    else:
        if into_page is None:
            raise LibraryError("a button needs a page to land on")
        page = doc.pages[into_page]
        page.setdefault("buttons", [])
        page["buttons"].extend(copy.deepcopy(plan.items))


# -- parking ------------------------------------------------------------------------------


def export(
    doc: DeckDoc,
    kind: str,
    indexes: list[int],
    path: Path,
    *,
    page_index: int | None = None,
    origin: dict[str, Any] | None = None,
) -> Path:
    """Writes selected items to a library file and returns where they landed.

    `page_index` is required for buttons and meaningless otherwise — kept as its own keyword
    rather than folded into `indexes`, which is what it was first, and which produced a parameter
    that was a list of ints for two kinds and a tuple of (int, list) for the third.
    """
    items = [copy.deepcopy(item) for item in _select(doc, kind, indexes, page_index)]
    if not items:
        raise LibraryError("nothing selected")

    data = envelope(
        kind, items,
        deck_path=doc.path, rev=doc.rev,
        origin=origin if origin is not None else _origin(kind, indexes, page_index, doc),
        asset_root=doc.path.parent if doc.path else None,
    )
    return write(path, data)


def park(
    doc: DeckDoc, kind: str, indexes: list[int], path: Path, *, page_index: int | None = None
) -> tuple[Path, int]:
    """Export, verify the file reads back, *then* remove. Returns (file, bytes freed).

    The order is the whole point. Writing and deleting in one step is one bad path away from
    losing the thing you were trying to keep, so nothing is removed until the file on disk has
    been parsed again and found to contain what was meant to be in it.

    The caller snapshots first, which makes park a single undo — the file stays behind, which is
    correct: undoing the removal should not delete your library.
    """
    indexes = sorted(indexes)
    written = export(doc, kind, indexes, path, page_index=page_index)

    check = read(written)
    if check.kind != kind or len(check.items) != len(indexes):
        raise LibraryError(f"{written.name} did not read back as it was written; nothing removed")

    before = budget.frame_bytes(doc.rev, doc.candidate_raw())
    try:
        _remove(doc, kind, indexes, page_index)
    except (ModelError, LibraryError) as exc:
        # The file is already on disk and is not deleted — losing it would be the one outcome
        # this ordering exists to prevent — so the message has to say where it is.
        raise LibraryError(f"{exc} — nothing was removed, and the copy is at {written}") from exc

    freed = before - budget.frame_bytes(doc.rev, doc.candidate_raw())

    return written, freed


def _select(doc: DeckDoc, kind: str, indexes: list[int], page_index: int | None):
    if kind == "theme":
        return [doc.themes[i] for i in indexes]
    if kind == "page":
        return [doc.pages[i] for i in indexes]
    if page_index is None:
        raise LibraryError("a button selection needs the page it is on")
    return [doc.pages[page_index]["buttons"][i] for i in indexes]


def _origin(
    kind: str, indexes: list[int], page_index: int | None, doc: DeckDoc
) -> dict[str, Any]:
    """Where this came from, so a folder listing says something six months later.

    The page is recorded by id rather than by index: an index is only meaningful against the
    deck it was taken from, and by the time anyone reads this the deck has moved on.
    """
    origin: dict[str, Any] = {"index": list(indexes)}
    if kind == "button" and page_index is not None:
        origin["page"] = doc.pages[page_index].get("id")
    return origin


def _remove(doc: DeckDoc, kind: str, indexes: list[int], page_index: int | None) -> None:
    if kind == "theme":
        if len(doc.themes) - len(indexes) < 1:
            raise ModelError("a deck needs at least one theme")
        for index in sorted(indexes, reverse=True):
            doc.themes.pop(index)
        return

    if kind == "page":
        if len(doc.pages) - len(indexes) < 1:
            raise ModelError("a deck needs at least one page")
        for index in sorted(indexes, reverse=True):
            # Refuses and names the referrers, exactly as deleting does. Parking is not a way
            # around the check — a parked page leaves the same dangling nav tile behind.
            doc.delete_page(index)
        return

    if page_index is None:
        raise LibraryError("a button selection needs the page it is on")
    for index in sorted(indexes, reverse=True):
        doc.delete_button(page_index, index)


# -- naming -------------------------------------------------------------------------------


def _name_of(kind: str, item: dict[str, Any]) -> str:
    if kind == "theme":
        return item.get("name") or "Theme"
    return item.get("id") or kind


def _set_name(kind: str, item: dict[str, Any], name: str) -> None:
    item["name" if kind == "theme" else "id"] = name


def _taken_names(doc: DeckDoc, kind: str) -> set[str]:
    if kind == "theme":
        return {t.get("name") for t in doc.themes if t.get("name")}
    if kind == "page":
        return {p.get("id") for p in doc.pages if p.get("id")}
    return doc.button_ids()


def suggested_filename(kind: str, items: list[dict[str, Any]]) -> str:
    """A filename you can recognise in a folder listing six months later."""
    import re

    if len(items) == 1:
        stem = _name_of(kind, items[0])
    else:
        stem = f"{len(items)}-{kind}s"
    cleaned = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or kind
    return f"{cleaned}{EXTENSION}"
