"""Editing one button: what it looks like, and what it does.

The action half is a thin per-type row over an always-visible JSON box, rather than either one
alone. JSON alone is worse ergonomics for no gain — every simple type is one widget that already
exists in theme_form.py, and typing `{"type":"launch","target":"code"}` by hand is not a feature.
A form alone cannot express `seq`, whose steps are a list of actions, and inventing a nested
list-of-forms for the one type that needs it would be most of the work of the whole panel.

So: the form drives the common case and writes into the JSON box; the JSON box is authoritative
and always shows exactly what will be saved. For `seq`, or for an action carrying keys the form
does not model, the form greys out and says the box is the thing to edit. Nothing is ever silently
dropped — an action the form cannot represent is one the form refuses to touch.

`hold` is edited through the same widgets by a toggle, because it is the same shape and the same
validation, and because a long press is the least discoverable thing on the deck: nothing about a
tile says it has one, so the editor has to.
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from deckbuilder.theme_form import BASE_POINTS, DISPLAY_CHOICES
from deckhost.config import ACTION_TYPES, MEDIA_KEYS, THEME_KEYWORDS, ahk_functions

# The single field that makes each type do anything, and the widget for it. Types absent here
# (`seq`) have no simple form and fall through to the JSON box.
SIMPLE_FIELDS: dict[str, tuple[str, str]] = {
    "launch": ("target", "text"),
    "shell": ("cmd", "text"),
    "ahk": ("fn", "choice"),
    "hid": ("keys", "chord"),
    "hid_text": ("text", "text"),
    "media": ("key", "choice"),
    "page": ("target", "choice"),
    "theme": ("target", "choice"),
    "delay": ("ms", "text"),
}

# Keys the form understands for each type. Anything else in the object means the form cannot
# represent it faithfully, so it steps aside rather than dropping it.
KNOWN_KEYS: dict[str, set[str]] = {
    "launch": {"type", "target", "args", "cwd"},
    "shell": {"type", "cmd"},
    "ahk": {"type", "fn", "args"},
    "hid": {"type", "keys"},
    "hid_text": {"type", "text"},
    "media": {"type", "key"},
    "page": {"type", "target"},
    "theme": {"type", "target"},
    "delay": {"type", "ms"},
    "seq": {"type", "steps"},
}

NO_ACTION = "— none —"


class ButtonForm:
    """The controls for whichever button is selected, or a hint when none is."""

    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app
        self.button: dict[str, Any] | None = None
        self._muted = True

        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="both", expand=True)

        head = ttk.Frame(self.frame)
        head.pack(fill="x")
        self.title = ttk.Label(head, style="Heading.TLabel", text="No button selected")
        self.title.pack(side="left")

        grid = ttk.Frame(self.frame)
        grid.pack(fill="x", pady=(6, 0))
        grid.columnconfigure(1, weight=1)

        self.label_var = self._row(grid, 0, "Label")
        self.icon_var = self._row(grid, 1, "Icon")
        self.id_var = self._row(grid, 2, "Id", readonly=True)

        ttk.Label(grid, text="Anatomy").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        self.display_var = tk.StringVar()
        self.display_box = ttk.Combobox(
            grid, textvariable=self.display_var, state="readonly", width=26,
            values=[label for label, _ in DISPLAY_CHOICES],
        )
        self.display_box.grid(row=3, column=1, sticky="w", pady=3)
        self.display_box.bind("<<ComboboxSelected>>", lambda _e: self._changed())

        # -- the action ------------------------------------------------------------------

        action = ttk.Labelframe(self.frame, text=" Action ", padding=6)
        action.pack(fill="both", expand=True, pady=(8, 0))
        action.columnconfigure(1, weight=1)

        self.slot_var = tk.StringVar(value="Press")
        slots = ttk.Frame(action)
        slots.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for text in ("Press", "Long press"):
            ttk.Radiobutton(
                slots, text=text, value=text, variable=self.slot_var,
                command=self._slot_changed,
            ).pack(side="left", padx=(0, 10))
        self.slot_note = ttk.Label(slots, foreground="#8b949e")
        self.slot_note.pack(side="left")

        ttk.Label(action, text="Type").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.type_var = tk.StringVar()
        self.type_box = ttk.Combobox(
            action, textvariable=self.type_var, state="readonly", width=18,
            values=[NO_ACTION, *sorted(ACTION_TYPES)],
        )
        self.type_box.grid(row=1, column=1, sticky="w", pady=3)
        self.type_box.bind("<<ComboboxSelected>>", lambda _e: self._type_changed())

        self.value_label = ttk.Label(action, text="")
        self.value_label.grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.value_var = tk.StringVar()
        self.value_entry = ttk.Entry(action, textvariable=self.value_var)
        self.value_box = ttk.Combobox(
            action, textvariable=self.value_var, state="readonly", width=26
        )
        self.value_var.trace_add("write", lambda *_a: self._value_changed())

        self.hint = ttk.Label(action, foreground="#8b949e", wraplength=380, justify="left")
        self.hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 4))

        ttk.Label(action, text="Raw").grid(row=4, column=0, sticky="nw", padx=(0, 8))
        self.raw = tk.Text(action, height=5, wrap="word", font=("Consolas", BASE_POINTS - 1))
        self.raw.grid(row=4, column=1, sticky="ew", pady=(0, 4))
        self.raw.bind("<FocusOut>", lambda _e: self._raw_changed())
        self.raw.bind("<Control-Return>", lambda _e: self._raw_changed())

        self.raw_note = ttk.Label(action, foreground="#8b949e", wraplength=380, justify="left")
        self.raw_note.grid(row=5, column=0, columnspan=2, sticky="w")

        self._muted = False
        self.show(None)

    def _row(self, parent: ttk.Frame, row: int, label: str, *, readonly: bool = False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        var = tk.StringVar()
        entry = ttk.Entry(
            parent, textvariable=var, state="readonly" if readonly else "normal", width=30
        )
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        if not readonly:
            var.trace_add("write", lambda *_a: self._changed())
        return var

    # -- loading -------------------------------------------------------------------------

    def show(self, button: dict[str, Any] | None) -> None:
        self._muted = True
        try:
            self.button = button
            if button is None:
                self.title.configure(text="No button selected")
                self._enable(False)
                self._set_raw("")
                self.hint.configure(text="Pick a button in the tree, or click a tile.")
                return

            self.title.configure(text=button.get("label") or button.get("id") or "Button")
            self._enable(True)
            self.label_var.set(button.get("label") or "")
            self.icon_var.set(button.get("icon") or "")
            self.id_var.set(button.get("id") or "")
            self._write_display(button.get("display") or "")
            self._load_action()
        finally:
            self._muted = False

    def _enable(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for widget in (self.display_box, self.type_box):
            widget.configure(state="readonly" if on else "disabled")
        self.raw.configure(state=state)

    def _slot(self) -> str:
        return "action" if self.slot_var.get() == "Press" else "hold"

    def _slot_changed(self) -> None:
        self._muted = True
        try:
            self._load_action()
        finally:
            self._muted = False

    def _current(self) -> dict[str, Any] | None:
        if self.button is None:
            return None
        value = self.button.get(self._slot())
        return value if isinstance(value, dict) else None

    def _load_action(self) -> None:
        action = self._current()
        self.slot_note.configure(
            text="" if self.button is None or self.button.get("hold") else
            "(no long press set)" if self._slot() == "hold" else ""
        )

        self.type_var.set(action.get("type") if action and action.get("type") else NO_ACTION)
        self._set_raw(json.dumps(action, indent=2) if action else "")
        self._build_value_row()

    # -- the per-type row ----------------------------------------------------------------

    def _build_value_row(self) -> None:
        action = self._current() or {}
        kind = action.get("type")

        self.value_entry.grid_remove()
        self.value_box.grid_remove()

        simple = SIMPLE_FIELDS.get(kind)
        extra = sorted(set(action) - KNOWN_KEYS.get(kind, {"type"})) if kind else []

        if kind == "seq":
            self.value_label.configure(text="")
            self.hint.configure(
                text=f"A sequence of {len(action.get('steps') or [])} steps. There is no simple "
                     "form for this — edit the JSON below, which is what gets saved."
            )
            self.raw_note.configure(text="")
            return

        if simple is None or extra:
            self.value_label.configure(text="")
            self.hint.configure(
                text=(
                    f"This action carries {', '.join(extra)}, which this row does not model — "
                    "so it is left alone. Edit the JSON below."
                ) if extra else "Choose a type, or write the JSON below."
            )
            self.raw_note.configure(text="")
            return

        field, widget = simple
        self.value_label.configure(text=field)

        if widget == "choice":
            self.value_box.configure(values=self._choices(kind))
            self.value_box.grid(row=2, column=1, sticky="w", pady=3)
            self.value_var.set(str(action.get(field) or ""))
        else:
            self.value_entry.grid(row=2, column=1, sticky="ew", pady=3)
            value = action.get(field)
            if widget == "chord":
                self.value_var.set("+".join(str(k) for k in (value or [])))
            else:
                self.value_var.set("" if value is None else str(value))

        self.hint.configure(text=_hint_for(kind))
        self.raw_note.configure(
            text="The JSON is authoritative — it is what gets written. Editing the row above "
                 "rewrites it; editing it directly and clicking away rereads it."
        )

    def _choices(self, kind: str) -> list[str]:
        if kind == "media":
            return sorted(MEDIA_KEYS)
        if kind == "page":
            return [p.get("id") for p in self.app.doc.pages if p.get("id")]
        if kind == "theme":
            return sorted(THEME_KEYWORDS - {""}) + self.app.doc.theme_names()
        if kind == "ahk":
            return sorted(ahk_functions() or [])
        return []

    # -- writing back --------------------------------------------------------------------

    def _changed(self) -> None:
        if self._muted or self.button is None:
            return
        self.button["label"] = self.label_var.get()
        self.button["icon"] = self.icon_var.get()
        self.button["display"] = self._read_display()
        self.app.refresh()

    def _type_changed(self) -> None:
        if self._muted or self.button is None:
            return
        kind = self.type_var.get()

        self.app.doc.snapshot()
        if kind == NO_ACTION:
            self.button[self._slot()] = None
        else:
            existing = self._current() or {}
            # Keeps only what the new type actually reads — `args` survives launch → ahk, the
            # rest goes. Carrying a `target` onto a `shell` action would leave a key the
            # firmware never looks at, costing wire bytes and reading like an unfinished edit.
            # Snapshotted above, so a switch made by mistake is one Ctrl+Z.
            kept = {k: v for k, v in existing.items() if k in KNOWN_KEYS.get(kind, set())}
            self.button[self._slot()] = {**kept, "type": kind}

        self._muted = True
        try:
            self._load_action()
        finally:
            self._muted = False
        self.app.refresh(immediate=True)

    def _value_changed(self) -> None:
        if self._muted:
            return
        action = self._current()
        if action is None:
            return
        simple = SIMPLE_FIELDS.get(action.get("type"))
        if simple is None:
            return

        field, widget = simple
        text = self.value_var.get()

        if widget == "chord":
            action[field] = [part for part in text.replace(",", "+").split("+") if part.strip()]
        elif field == "ms":
            action[field] = int(text) if text.strip().lstrip("-").isdigit() else text
        else:
            action[field] = text

        self._muted = True
        try:
            self._set_raw(json.dumps(action, indent=2))
        finally:
            self._muted = False
        self.app.refresh()

    def _raw_changed(self) -> None:
        if self._muted or self.button is None:
            return
        text = self.raw.get("1.0", "end").strip()

        if not text:
            self.button[self._slot()] = None
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                self.raw_note.configure(text=f"Not valid JSON: {exc}")
                return
            if not isinstance(parsed, dict):
                self.raw_note.configure(text="An action has to be a JSON object.")
                return
            self.app.doc.snapshot()
            self.button[self._slot()] = parsed

        self._muted = True
        try:
            self._load_action()
        finally:
            self._muted = False
        self.app.refresh(immediate=True)

    def _set_raw(self, text: str) -> None:
        self.raw.configure(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", text)
        if self.button is None:
            self.raw.configure(state="disabled")

    def _write_display(self, value: str) -> None:
        for label, candidate in DISPLAY_CHOICES:
            if candidate == value:
                self.display_var.set(label)
                return
        self.display_var.set(DISPLAY_CHOICES[0][0])

    def _read_display(self) -> str:
        for label, candidate in DISPLAY_CHOICES:
            if label == self.display_var.get():
                return candidate
        return ""


def _hint_for(kind: str) -> str:
    return {
        "launch": "A program, a document or a URL. Anything with :// or no PATH entry goes "
                  "through the shell, so your default handler wins.",
        "shell": "Run by the shell, so pipes and redirection work — and so does anything else "
                 "you type.",
        "ahk": "A function in agent/ahk/lib.ahk. An unknown name warns rather than failing, "
               "because that file is yours to edit.",
        "hid": "Tokens joined by +, matched case-insensitively. A single capital letter implies "
               "SHIFT, so A is Shift+A. Six non-modifiers maximum — a seventh makes the device "
               "reject the whole chord.",
        "hid_text": "Typed as keystrokes by the deck itself, so it works with the agent closed.",
        "media": "Sent by the deck over its own consumer-control endpoint.",
        "page": "Runs on the device, so it works with the agent closed. A target that names no "
                "page does nothing at all, silently.",
        "theme": "next or prev cycle; a name selects. Also silent if the name matches nothing.",
        "delay": "Milliseconds. Only useful inside a sequence.",
    }.get(kind, "")
