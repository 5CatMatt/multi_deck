"""The editing controls: one row per field, and one idea about what "unset" looks like.

Every theme token is optional, and deck.json writes that down rather than leaving the key out —
`null` for numbers, booleans and colours, `""` for the two strings. A form built the obvious way
loses that distinction immediately, because a colour picker has no way to express "no colour"
and a slider has no way to express "no number"; you would get `#000000` and `0`, which are real
values that mean something else.

So every nullable row carries a **Default** tick alongside its control. Ticked, the control
greys out and the field is written as null. That matters most for `dim_opa` and `flip180`,
whose defaults come from firmware/config.h and differ between builds — writing a literal there
silently overrides the board you flashed.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, ttk
from typing import Any, Callable

# Type is biased up throughout. This is a tool for looking closely at small differences in
# colour on a 190x130 tile; squinting at the controls while doing it is the wrong trade.
BASE_POINTS = 12


class Field:
    """One labelled row. Subclasses own the control and the value conversion."""

    nullable = False

    def __init__(
        self,
        parent: tk.Widget,
        row: int,
        key: str,
        label: str,
        on_change: Callable[[], None],
        *,
        nullable: bool | None = None,
        hint: str = "",
    ) -> None:
        self.key = key
        self.parent = parent
        self.on_change = on_change
        self.hint = hint
        if nullable is not None:
            self.nullable = nullable

        self._muted = True  # suppress callbacks while loading a value in
        self.label = ttk.Label(parent, text=label)
        self.label.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)

        self.body = ttk.Frame(parent)
        self.body.grid(row=row, column=1, sticky="w", pady=3)
        # Column 1 deliberately does not expand. Given weight it absorbs any slack in the
        # panel and pushes the "Default" tick in column 2 off the right-hand edge — which
        # hides the only control that can express "unset".

        self.use_default = tk.BooleanVar(value=False)
        if self.nullable:
            self.default_box = ttk.Checkbutton(
                parent, text="Default", variable=self.use_default, command=self._toggled
            )
            self.default_box.grid(row=row, column=2, sticky="w", padx=(8, 0))

        self.build()

    # -- subclass hooks ------------------------------------------------------------------

    def build(self) -> None:
        raise NotImplementedError

    def read(self) -> Any:
        raise NotImplementedError

    def write(self, value: Any) -> None:
        raise NotImplementedError

    def enable(self, on: bool) -> None:
        for child in self.body.winfo_children():
            try:
                child.configure(state=("normal" if on else "disabled"))
            except tk.TclError:
                pass

    # -- shared --------------------------------------------------------------------------

    def set(self, value: Any) -> None:
        self._muted = True
        try:
            unset = value is None or (self.nullable and value == "" and self.empty_is_unset)
            self.use_default.set(bool(unset))
            if not unset:
                self.write(value)
            self.enable(not unset)
        finally:
            self._muted = False

    empty_is_unset = False

    def get(self) -> Any:
        if self.nullable and self.use_default.get():
            return "" if self.empty_is_unset else None
        return self.read()

    def changed(self, *_args) -> None:
        if not self._muted:
            self.on_change()

    def _toggled(self) -> None:
        self.enable(not self.use_default.get())
        self.changed()


class ColorField(Field):
    """Six hex digits, a swatch, and a picker.

    The entry is authoritative and the swatch follows it, rather than the other way round:
    typing is how you paste a colour from somewhere else, and that is the common case here.
    """

    nullable = True

    def build(self) -> None:
        self.var = tk.StringVar()
        self.var.trace_add("write", self.changed)

        self.swatch = tk.Canvas(
            self.body, width=34, height=26, highlightthickness=1, highlightbackground="#888"
        )
        self.swatch.pack(side="left", padx=(0, 6))
        self.swatch.bind("<Button-1>", lambda _e: self.pick())

        self.entry = ttk.Entry(self.body, textvariable=self.var, width=10)
        self.entry.pack(side="left")

        ttk.Button(self.body, text="Pick", width=6, command=self.pick).pack(
            side="left", padx=(6, 0)
        )

    def enable(self, on: bool) -> None:
        super().enable(on)
        if on:
            self.refresh_swatch()
        else:
            self.swatch.configure(background="#3a3a3a")

    def refresh_swatch(self) -> None:
        text = self.var.get().lstrip("#")
        valid = len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text)
        self.swatch.configure(background=f"#{text}" if valid else "#3a3a3a")

    def changed(self, *args) -> None:
        self.refresh_swatch()
        super().changed(*args)

    def pick(self) -> None:
        if self.use_default.get():
            return
        current = self.var.get().lstrip("#")
        initial = f"#{current}" if len(current) == 6 else "#1b2129"
        _rgb, chosen = colorchooser.askcolor(color=initial, parent=self.parent)
        if chosen:
            self.var.set(chosen.lower())

    def read(self) -> str:
        text = self.var.get().strip()
        return text if text.startswith("#") or not text else f"#{text}"

    def write(self, value: Any) -> None:
        self.var.set(str(value))
        self.refresh_swatch()


class IntField(Field):
    """A slider with the number beside it, because a slider alone cannot be typed into."""

    def __init__(self, *args, low: int = 0, high: int = 100, **kwargs) -> None:
        self.low, self.high = low, high
        super().__init__(*args, **kwargs)

    def build(self) -> None:
        self.var = tk.IntVar(value=self.low)
        self.scale = ttk.Scale(
            self.body, from_=self.low, to=self.high, orient="horizontal",
            command=self._slid, length=165,
        )
        self.scale.pack(side="left")
        self.readout = ttk.Label(self.body, width=5, anchor="e")
        self.readout.pack(side="left", padx=(8, 0))
        self._sync_readout()

    def _slid(self, _value: str) -> None:
        self._sync_readout()
        self.changed()

    def _sync_readout(self) -> None:
        self.readout.configure(text=str(int(round(self.scale.get()))))

    def enable(self, on: bool) -> None:
        self.scale.configure(state="normal" if on else "disabled")
        self.readout.configure(state="normal" if on else "disabled")

    def read(self) -> int:
        return int(round(self.scale.get()))

    def write(self, value: Any) -> None:
        self.scale.set(int(value))
        self._sync_readout()


class ChoiceField(Field):
    def __init__(self, *args, choices: tuple[tuple[str, Any], ...] = (), **kwargs) -> None:
        self.choices = choices
        super().__init__(*args, **kwargs)

    def build(self) -> None:
        self.var = tk.StringVar()
        self.var.trace_add("write", self.changed)
        self.box = ttk.Combobox(
            self.body, textvariable=self.var, state="readonly",
            values=[label for label, _ in self.choices], width=26,
        )
        self.box.pack(side="left")

    def enable(self, on: bool) -> None:
        self.box.configure(state="readonly" if on else "disabled")

    def set_choices(self, choices: tuple[tuple[str, Any], ...]) -> None:
        """Replaces the options, keeping the current value selected if it survived.

        The wallpaper list is read off the disk, so converting an image has to be able to add
        to it without rebuilding the whole form.
        """
        current = self.read()
        self.choices = choices
        self._muted = True
        try:
            self.box.configure(values=[label for label, _ in choices])
            self.write(current)
        finally:
            self._muted = False

    def read(self) -> Any:
        for label, value in self.choices:
            if label == self.var.get():
                return value
        return self.choices[0][1] if self.choices else None

    def write(self, value: Any) -> None:
        for label, candidate in self.choices:
            if candidate == value:
                self.var.set(label)
                return
        self.var.set(self.choices[0][0] if self.choices else "")


class TextField(Field):
    def build(self) -> None:
        self.var = tk.StringVar()
        self.var.trace_add("write", self.changed)
        self.entry = ttk.Entry(self.body, textvariable=self.var, width=30)
        self.entry.pack(side="left", fill="x", expand=True)

    def read(self) -> str:
        return self.var.get()

    def write(self, value: Any) -> None:
        self.var.set(str(value))


# The `display` chain, spelled out rather than left as bare tokens. "" is a real, meaningful
# value here — it is how a level says "take whatever the level above decided" — so it gets a
# name instead of being represented by an empty row.
DISPLAY_CHOICES = (
    ("Inherit (take the level above)", ""),
    ("Icon and label", "icon_text"),
    ("Icon only", "icon"),
    ("Label only", "text"),
)

SETTINGS_DISPLAY_CHOICES = DISPLAY_CHOICES[1:]

FLIP_CHOICES = (
    ("From the build (config.h)", None),
    ("Upside down", True),
    ("Normal way up", False),
)
