"""The pages-and-buttons half of the window: a tree, the operations on it, and the library.

Kept out of app.py because it is about as large as the theme form is, and because the two have
almost nothing in common — the theme form edits seventeen fields of one object, this moves objects
around and tells you what each one costs.

Two things drive every decision here.

The first is that **every mutating command states its price before it commits**. The layout is
5,805 of 8,192 bytes, and every feature in this panel makes spending easier while only parking
frees anything. A byte column on every row and a measured cost in every confirmation is not
polish; it is the only reason the panel is safe to hand someone.

The second is that **references are the dangerous part**. A page id appears in nav buttons, a
theme name appears in actions, and every stale one fails the same silent way on the device: the
firmware finds nothing, keeps what it had, and says nothing at all. So renaming follows references,
deleting a referenced page refuses and names the referrers, and neither is offered as a choice.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from deckbuilder import budget, geometry, library
from deckbuilder.button_form import ButtonForm
from deckbuilder.model import FIRMWARE_PAGE_TYPES, ModelError

PAGE_TYPES = ("grid", "numpad", "stats", "calendar", "colortest")


class LayoutPanel:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app

        # Rebuilding the tree restores the selection, restoring a selection fires
        # <<TreeviewSelect>>, and that handler refreshes the window, which rebuilds the tree.
        # Nothing about that loop terminates, and it needs two guards because the event arrives
        # by two different routes:
        #
        #   _settling      — Tk can deliver the virtual event synchronously inside selection_set,
        #                    which would re-enter a rebuild that is halfway through.
        #   _last_selection — and it can also *queue* it, delivered once the rebuild has returned
        #                    and _settling is back to False. That is the one that actually bit:
        #                    an endless refresh loop with a stack only eight frames deep, so it
        #                    pins the CPU rather than raising RecursionError, and no test that
        #                    skips the event loop can see it.
        #
        # The second is the honest statement of what the handler is for: a person choosing a
        # *different* row. Re-selecting what is already selected is a no-op.
        self._settling = False
        self._last_selection: tuple | None = None
        self._built = False

        # A deck of five pages is thirty rows once the buttons are showing, and at a readable
        # row height only a handful fit — so the scrollbar is not optional furniture.
        holder = ttk.Frame(parent)
        holder.pack(fill="x")

        # Everything here packs with fill="x" and no vertical expand: this panel lives inside a
        # scrolling frame, which has no vertical slack to hand out — a widget asking to expand
        # into it gets nothing and quietly collapses.
        self.tree = ttk.Treeview(
            holder, columns=("kind", "bytes"), show="tree headings", height=10,
            selectmode="browse",
        )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.heading("#0", text="Page / button")
        self.tree.heading("kind", text="Type")
        self.tree.heading("bytes", text="Bytes")
        self.tree.column("#0", width=250, stretch=True)
        self.tree.column("kind", stretch=False, anchor="w")
        self.tree.column("bytes", stretch=False, anchor="e")
        self.restyle(app.points)

        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="x", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._selected())

        self.summary = ttk.Label(parent, foreground="#8b949e", justify="left")
        self.summary.pack(anchor="w", pady=(4, 6))

        self._build_buttons(parent)

        editor = ttk.Frame(parent)
        editor.pack(fill="x", pady=(10, 0))
        self.button_form = ButtonForm(editor, app)

    # -- chrome --------------------------------------------------------------------------

    def restyle(self, points: int) -> None:
        """Sizes the fixed columns to the text that goes in them.

        Column widths are a property of the widget, not of the style, so they do not follow the
        text-size menu on their own — and at 18pt a 90px Type column shows "launc". Measured from
        the widest value each column can hold rather than padded by eye.
        """
        import tkinter.font as tkfont

        font = tkfont.Font(font=("Segoe UI", points))
        self.tree.column("kind", width=font.measure("hid_text") + 20)
        self.tree.column("bytes", width=font.measure("8,888") + 20)

    def _build_buttons(self, parent: ttk.Frame) -> None:
        rows = ttk.Frame(parent)
        rows.pack(fill="x")

        first = ttk.Frame(rows)
        first.pack(fill="x", pady=(0, 4))
        for text, command, width in (
            ("New page", self.new_page, 10),
            ("New button", self.new_button, 11),
            ("Duplicate", self.duplicate, 10),
            ("Delete", self.delete, 8),
        ):
            ttk.Button(first, text=text, command=command, width=width).pack(side="left", padx=2)

        second = ttk.Frame(rows)
        second.pack(fill="x", pady=(0, 4))
        for text, command, width in (
            ("Up", lambda: self.move(-1), 5),
            ("Down", lambda: self.move(1), 7),
            ("Move to page…", self.move_to_page, 14),
            ("Grid…", self.set_grid, 8),
        ):
            ttk.Button(second, text=text, command=command, width=width).pack(side="left", padx=2)

        third = ttk.Labelframe(rows, text=" Library ", padding=6)
        third.pack(fill="x", pady=(6, 0))
        for text, command, width in (
            ("Park…", self.park, 8),
            ("Export…", self.export, 10),
            ("Import…", self.import_items, 10),
            ("From a backup…", self.import_backup, 15),
        ):
            ttk.Button(third, text=text, command=command, width=width).pack(side="left", padx=2)

        fixed = ttk.Frame(rows)
        fixed.pack(fill="x", pady=(6, 0))
        self.pin_var = tk.StringVar(value="Auto")
        ttk.Label(fixed, text="Tile positions").pack(side="left")
        self.pin_box = ttk.Combobox(
            fixed, textvariable=self.pin_var, state="readonly", width=8,
            values=("Auto", "Fixed"),
        )
        self.pin_box.pack(side="left", padx=(6, 8))
        self.pin_box.bind("<<ComboboxSelected>>", lambda _e: self.set_pinning())
        self.pin_hint = ttk.Label(fixed, foreground="#8b949e")
        self.pin_hint.pack(side="left")

    # -- state ---------------------------------------------------------------------------

    @property
    def doc(self):
        return self.app.doc

    def selection(self) -> tuple[str, int, int | None] | None:
        """(kind, page index, button index or None) for whatever is highlighted."""
        chosen = self.tree.focus()
        if not chosen:
            return None
        parts = chosen.split(":")
        if parts[0] == "page":
            return "page", int(parts[1]), None
        if parts[0] == "button":
            return "button", int(parts[1]), int(parts[2])
        return None

    def _selected(self) -> None:
        if self._settling:
            return
        picked = self.selection()
        if picked is None or picked == self._last_selection:
            return
        self._last_selection = picked
        _kind, page_index, _button = picked

        # Selecting anything follows the preview to that page, because the alternative is two
        # controls that disagree about what you are looking at.
        titles = self.app._page_titles()
        if 0 <= page_index < len(titles):
            self.app.page_var.set(titles[page_index])

        self._sync_pin_box(page_index)
        self.app.refresh(immediate=True)

    def _sync_pin_box(self, page_index: int) -> None:
        page = self.doc.pages[page_index]
        if page.get("type") in FIRMWARE_PAGE_TYPES:
            self.pin_box.configure(state="disabled")
            self.pin_hint.configure(text="this page draws its own layout")
            return

        self.pin_box.configure(state="readonly")
        pinned = self.doc.is_pinned(page_index)
        self.pin_var.set("Fixed" if pinned else "Auto")
        self.pin_hint.configure(
            text="drag writes col/row; spans allowed" if pinned
            else "drag reorders; costs nothing"
        )

    def refresh(self) -> None:
        """Rebuilds the tree, keeping the selection where it was."""
        self._settling = True
        try:
            self._rebuild()
        finally:
            self._settling = False

    def _rebuild(self) -> None:
        remembered = self.tree.focus()
        open_pages = {row for row in self.tree.get_children("") if self.tree.item(row, "open")}

        # "Nothing is open" and "this is the first build" are different states, and conflating
        # them meant collapsing every page and then touching anything sprang them all open
        # again — the panel arguing with you about the view you just chose.
        expand_all = not self._built
        self._built = True

        self.tree.delete(*self.tree.get_children(""))

        total_buttons = 0
        for page_index, page in enumerate(self.doc.pages):
            key = f"page:{page_index}"
            cost = budget.page_cost(page)
            title = page.get("title") or page.get("id") or "?"
            self.tree.insert(
                "", "end", iid=key, text=f"{title}   ({page.get('id')})",
                values=(page.get("type") or "grid", f"{cost:,}"),
                open=expand_all or key in open_pages,
            )

            for index, button in enumerate(page.get("buttons") or []):
                total_buttons += 1
                label = button.get("label") or button.get("id") or "?"
                kind = ((button.get("action") or {}).get("type")) or "—"
                pin = "" if button.get("pos") is None else "  ⌗"
                self.tree.insert(
                    key, "end", iid=f"button:{page_index}:{index}",
                    text=f"{label}{pin}   ({button.get('id')})",
                    values=(kind, f"{budget.button_cost(button):,}"),
                )

        if remembered and self.tree.exists(remembered):
            self.tree.focus(remembered)
            self.tree.selection_set(remembered)

        report = budget.report(self.doc.next_rev(), self.doc.candidate_raw())
        self.summary.configure(
            text=f"{len(self.doc.pages)} pages, {total_buttons} buttons  ·  "
                 f"{geometry.nav_capacity()} nav tabs fit  ·  {report.summary()}"
        )

        picked = self.selection()
        if picked is not None:
            self._sync_pin_box(picked[1])
        self.button_form.show(self._selected_button())

    def _selected_button(self) -> dict[str, Any] | None:
        picked = self.selection()
        if picked is None or picked[0] != "button":
            return None
        _kind, page_index, index = picked
        try:
            return self.doc.pages[page_index]["buttons"][index]
        except (IndexError, KeyError):
            return None

    # -- commands ------------------------------------------------------------------------

    def _need(self, kind: str | None = None):
        picked = self.selection()
        if picked is None:
            self.app._say("Select a page or a button first.", "info")
            return None
        if kind is not None and picked[0] != kind:
            self.app._say(f"Select a {kind} first.", "info")
            return None
        return picked

    def _after(self, message: str, level: str = "ok", *, boot_before: str | None = None) -> None:
        if boot_before is not None and self.doc.boot_page != boot_before:
            message += (
                f"\n\nThe deck now starts on {self.doc.boot_page!r} — the first page is the "
                "boot page, and nothing in deck.json names it."
            )
            level = "warn" if level == "ok" else level
        self.app._say(message, level)
        self.app.refresh(immediate=True)

    def new_page(self) -> None:
        title = simpledialog.askstring("New page", "Page title:", parent=self.app.root)
        if not title:
            return
        self.doc.snapshot()
        index = self.doc.new_page(title)
        cost = budget.page_cost(self.doc.pages[index])
        self._select(f"page:{index}")
        self._after(f"Added {title!r} (+{cost:,} bytes).")

    def new_button(self) -> None:
        picked = self._need()
        if picked is None:
            return
        _kind, page_index, _index = picked
        page = self.doc.pages[page_index]
        if page.get("type") in FIRMWARE_PAGE_TYPES:
            self.app._say(f"A {page['type']!r} page draws its own layout.", "error")
            return

        label = simpledialog.askstring("New button", "Button label:", parent=self.app.root)
        if not label:
            return
        self.doc.snapshot()
        index = self.doc.new_button(page_index, label)
        cost = budget.button_cost(page["buttons"][index])
        self._select(f"button:{page_index}:{index}")
        self._after(
            f"Added {label!r} to {page.get('title')} (+{cost:,} bytes). "
            "Give it an action — a button with no target is a problem, not a placeholder."
        )

    def duplicate(self) -> None:
        picked = self._need()
        if picked is None:
            return
        kind, page_index, index = picked

        self.doc.snapshot()
        if kind == "page":
            new_index = self.doc.duplicate_page(page_index)
            cost = budget.page_cost(self.doc.pages[new_index])
            self._select(f"page:{new_index}")
        else:
            new_index = self.doc.duplicate_button(page_index, index)
            cost = budget.button_cost(self.doc.pages[page_index]["buttons"][new_index])
            self._select(f"button:{page_index}:{new_index}")
        self._after(f"Duplicated (+{cost:,} bytes).")

    def delete(self) -> None:
        picked = self._need()
        if picked is None:
            return
        kind, page_index, index = picked
        boot_before = self.doc.boot_page

        if kind == "page":
            page = self.doc.pages[page_index]
            freed = budget.removal_cost(
                self.doc.rev, self.doc.candidate_raw(),
                lambda raw: raw["pages"].pop(page_index),
            )
            if not messagebox.askyesno(
                "Delete page",
                f"Delete {page.get('title')!r} and its {len(page.get('buttons') or [])} "
                f"buttons?\n\nThis frees {freed:,} bytes. Park it instead if you might want "
                "it back.",
                parent=self.app.root,
            ):
                return
            self.doc.snapshot()
            try:
                self.doc.delete_page(page_index)
            except ModelError as exc:
                self.doc.undo()
                self.app._say(f"Cannot delete: {exc}", "error")
                return
        else:
            button = self.doc.pages[page_index]["buttons"][index]
            freed = budget.removal_cost(
                self.doc.rev, self.doc.candidate_raw(),
                lambda raw: raw["pages"][page_index]["buttons"].pop(index),
            )
            self.doc.snapshot()
            self.doc.delete_button(page_index, index)

        self.tree.selection_remove(*self.tree.selection())
        self._after(f"Deleted, freeing {freed:,} bytes.", boot_before=boot_before)

    def move(self, delta: int) -> None:
        picked = self._need()
        if picked is None:
            return
        kind, page_index, index = picked
        boot_before = self.doc.boot_page

        self.doc.snapshot()
        if kind == "page":
            new_index = self.doc.move_page(page_index, delta)
            self._select(f"page:{new_index}")
            self._after("Moved. Nav tab order follows the file.", boot_before=boot_before)
        else:
            new_index = self.doc.move_button(page_index, index, delta)
            self._select(f"button:{page_index}:{new_index}")
            if self.doc.is_pinned(page_index):
                self._after(
                    "Moved in the file, but this page has fixed positions — so the tile did "
                    "not move on screen. Switch it to Auto, or drag the tile instead.", "warn",
                )
            else:
                self._after("Moved. On an Auto page the order is the layout, and costs nothing.")

    def move_to_page(self) -> None:
        picked = self._need("button")
        if picked is None:
            return
        _kind, page_index, index = picked

        choices = [
            (i, p) for i, p in enumerate(self.doc.pages)
            if i != page_index and p.get("type") not in FIRMWARE_PAGE_TYPES
        ]
        if not choices:
            self.app._say("There is no other grid page to move it to.", "info")
            return

        target = _choose(
            self.app.root, "Move to page", "Move this button to:",
            [f"{p.get('title')} ({p.get('id')})" for _i, p in choices],
        )
        if target is None:
            return

        self.doc.snapshot()
        try:
            self.doc.move_button_to_page(page_index, index, choices[target][0])
        except ModelError as exc:
            self.doc.undo()
            self.app._say(str(exc), "error")
            return
        self._after(
            "Moved. Costs nothing on the wire — it is the same button on a different page.",
        )

    def set_grid(self) -> None:
        picked = self._need()
        if picked is None:
            return
        _kind, page_index, _index = picked
        page = self.doc.pages[page_index]
        if page.get("type") in FIRMWARE_PAGE_TYPES:
            self.app._say(f"A {page['type']!r} page draws its own layout.", "error")
            return

        cols, rows = geometry.grid_size(page.get("grid"))
        answer = simpledialog.askstring(
            "Grid", "Columns x rows:", initialvalue=f"{cols}x{rows}", parent=self.app.root
        )
        if not answer:
            return
        try:
            new_cols, new_rows = (int(part) for part in answer.lower().split("x", 1))
        except ValueError:
            self.app._say(f"Could not read {answer!r} as 'columns x rows'.", "error")
            return

        cell_w, cell_h = geometry.cells(max(1, new_cols), max(1, new_rows))
        self.doc.snapshot()
        self.doc.set_grid(page_index, new_cols, new_rows)
        self._after(
            f"{new_cols}x{new_rows}, giving {cell_w}x{cell_h}px tiles. "
            "Fixed positions are not rescaled — anything now off the grid is listed below."
        )

    def set_pinning(self) -> None:
        picked = self.selection()
        if picked is None:
            return
        _kind, page_index, _index = picked
        page = self.doc.pages[page_index]
        want_fixed = self.pin_var.get() == "Fixed"

        if want_fixed == self.doc.is_pinned(page_index):
            return

        if want_fixed:
            cost = budget.removal_cost(
                self.doc.rev, self.doc.candidate_raw(),
                lambda raw: _pin_in(raw, page_index),
            )
            # removal_cost returns bytes freed, so pinning comes back negative.
            spend = -cost
            report = budget.report(self.doc.next_rev(), self.doc.candidate_raw())
            if not messagebox.askyesno(
                "Fix positions",
                f"Write a col/row on all {len(page.get('buttons') or [])} tiles of "
                f"{page.get('title')!r}?\n\n"
                f"Costs {spend:,} bytes → {report.used + spend:,} / {budget.LIMIT:,}.\n\n"
                "Nothing moves on screen — it writes down where auto-flow already puts each "
                "tile. Spans and deliberate gaps need this; simple reordering does not.",
                parent=self.app.root,
            ):
                self._sync_pin_box(page_index)
                return
            self.doc.snapshot()
            self.doc.pin_all(page_index)
            self._after(f"Fixed positions on {page.get('title')!r} (+{spend:,} bytes).")
        else:
            self.doc.snapshot()
            self.doc.unpin_all(page_index)
            self._after(
                f"Back to auto-flow on {page.get('title')!r}. Spans and gaps are gone; "
                "order in the file is the layout again."
            )

    # -- library -------------------------------------------------------------------------

    def _library_dir(self) -> Path:
        return self.app.library_dir()

    def park(self) -> None:
        self._save_out(remove=True)

    def export(self) -> None:
        self._save_out(remove=False)

    def _save_out(self, *, remove: bool) -> None:
        picked = self._need()
        if picked is None:
            return
        kind, page_index, index = picked
        indexes = [page_index] if kind == "page" else [index]
        items = (
            [self.doc.pages[page_index]]
            if kind == "page"
            else [self.doc.pages[page_index]["buttons"][index]]
        )

        chosen = filedialog.asksaveasfilename(
            parent=self.app.root,
            title="Park to a library file" if remove else "Export a copy",
            initialdir=str(self._library_dir()),
            initialfile=library.suggested_filename(kind, items),
            defaultextension=".json",
            filetypes=[("multi_deck library", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return

        path = Path(chosen)
        self.app.remember_library(path.parent)
        boot_before = self.doc.boot_page

        try:
            if remove:
                self.doc.snapshot()
                written, freed = library.park(
                    self.doc, kind, indexes, path,
                    page_index=page_index if kind == "button" else None,
                )
            else:
                written = library.export(
                    self.doc, kind, indexes, path,
                    page_index=page_index if kind == "button" else None,
                )
                freed = 0
        except (library.LibraryError, ModelError, OSError) as exc:
            if remove:
                self.doc.undo()
            self.app._say(str(exc), "error")
            return

        if remove:
            self.tree.selection_remove(*self.tree.selection())
            self._after(
                f"Parked to {written.name}, freeing {freed:,} bytes. "
                "Import it back whenever you want it.",
                boot_before=boot_before,
            )
        else:
            self.app._say(f"Copied to {written}. The deck still has its own.", "ok")

    def import_items(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.app.root, title="Import from the library",
            initialdir=str(self._library_dir()),
            filetypes=[
                ("multi_deck library or deck.json", "*.json"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            self.app.remember_library(Path(chosen).parent)
            self._import(Path(chosen))

    def import_backup(self) -> None:
        """The builder already keeps twenty backups; this makes them an undo across sessions."""
        folder = self.app.app_backups()
        folder.mkdir(parents=True, exist_ok=True)
        chosen = filedialog.askopenfilename(
            parent=self.app.root, title="Import out of a backup",
            initialdir=str(folder), filetypes=[("Backups", "deck-*.json"), ("JSON", "*.json")],
        )
        if chosen:
            self._import(Path(chosen))

    def _import(self, path: Path) -> None:
        try:
            fragment = library.read(path)
        except library.LibraryError as exc:
            messagebox.showerror("Cannot read that file", str(exc), parent=self.app.root)
            return

        if fragment.source == "deck":
            kind = _choose(
                self.app.root, "Import from a deck",
                f"{path.name} is a whole layout. Take:",
                ["Themes", "Pages", "Buttons"],
            )
            if kind is None:
                return
            raw = json.loads(path.read_bytes().decode("utf-8"))
            fragment = library.pick(fragment, ("theme", "page", "button")[kind], raw)

        if not fragment.items:
            self.app._say(f"{path.name} has no {fragment.kind}s in it.", "info")
            return

        which = _choose(
            self.app.root, "Import", f"Import which {fragment.kind}?",
            [library._name_of(fragment.kind, item) for item in fragment.items],
            allow_all=True,
        )
        if which is None:
            return
        if which >= 0:
            fragment.items = [fragment.items[which]]

        into_page = None
        if fragment.kind == "button":
            choices = [
                (i, p) for i, p in enumerate(self.doc.pages)
                if p.get("type") not in FIRMWARE_PAGE_TYPES
            ]
            if not choices:
                self.app._say("There is no grid page to import a button onto.", "error")
                return
            target = _choose(
                self.app.root, "Import", "Onto which page?",
                [f"{p.get('title')} ({p.get('id')})" for _i, p in choices],
            )
            if target is None:
                return
            into_page = choices[target][0]

        plan = library.plan_import(self.doc, fragment, into_page=into_page)
        if not self._confirm_import(path, plan):
            return

        self.doc.snapshot()
        try:
            library.apply(self.doc, plan, into_page=into_page)
        except library.LibraryError as exc:
            self.doc.undo()
            self.app._say(str(exc), "error")
            return

        message = f"Imported {len(plan.items)} {plan.kind}(s) — {plan.summary()}."
        level = "ok"
        if plan.renamed:
            pairs = ", ".join(f"{old} → {new}" for old, new in plan.renamed[:3])
            message += f"\n\nRenamed to avoid collisions: {pairs}"
        if plan.missing_assets:
            message += (
                f"\n\nMissing from {self.doc.path.parent.name}/: "
                f"{', '.join(plan.missing_assets)}. Images never travel over USB — convert "
                "and copy them by hand, or the deck shows the background instead."
            )
            level = "warn"
        if plan.over_limit or plan.used_after >= int(budget.LIMIT * budget.WARN_FRACTION):
            message += f"\n\n{plan.detail()}"
            level = "warn"

        self._after(message, level)

    def _confirm_import(self, path: Path, plan: library.Plan) -> bool:
        if not plan.ok:
            messagebox.showerror(
                "Cannot import",
                "\n\n".join(plan.problems),
                parent=self.app.root,
            )
            return False

        lines = [f"From {path.name}: {len(plan.items)} {plan.kind}(s).", "", plan.summary()]
        lines.append(plan.detail())
        if plan.notes:
            lines += ["", *plan.notes[:6]]
        if plan.missing_assets:
            lines += ["", "Images not in this deck's folder: " + ", ".join(plan.missing_assets)]

        if plan.over_limit:
            lines += [
                "",
                "This would put the layout past 8192 bytes, and the deck discards an "
                "oversized frame without reporting anything. Park something first.",
            ]
            messagebox.showerror("Too big to import", "\n".join(lines), parent=self.app.root)
            return False

        return messagebox.askokcancel("Import", "\n".join(lines), parent=self.app.root)

    # -- helpers -------------------------------------------------------------------------

    def _select(self, key: str) -> None:
        self.refresh()
        if not self.tree.exists(key):
            return
        self._settling = True
        try:
            self.tree.see(key)
            self.tree.focus(key)
            self.tree.selection_set(key)
            # Claimed before the queued <<TreeviewSelect>> arrives, so the handler sees the work
            # as already done rather than doing it again and refreshing on top of the caller.
            self._last_selection = self.selection()
        finally:
            self._settling = False

        # The rebuild above ran before this row was selected, so the panels that follow the
        # selection are still showing the previous one. Updated here rather than by refreshing
        # again, which would rebuild the tree and undo the selection we just made.
        self._sync_pin_box(self.selection()[1])
        self.button_form.show(self._selected_button())

    def select_button(self, page_index: int, index: int) -> None:
        self._select(f"button:{page_index}:{index}")


def _pin_in(raw: dict[str, Any], page_index: int) -> None:
    """pin_all against a bare layout dict, for measuring the cost before committing to it."""
    page = raw["pages"][page_index]
    cols, _rows = geometry.grid_size(page.get("grid"))
    flow = 0
    for button in page.get("buttons") or []:
        pos = button.get("pos") or {}
        col = geometry.as_int(pos.get("col"), -1)
        row = geometry.as_int(pos.get("row"), -1)
        if col < 0 or row < 0:
            col, row = flow % cols, flow // cols
            flow += 1
        button["pos"] = {
            "col": col, "row": row,
            "w": max(1, geometry.as_int(pos.get("w"), 1)),
            "h": max(1, geometry.as_int(pos.get("h"), 1)),
        }


def _choose(
    root: tk.Misc, title: str, prompt: str, options: list[str], *, allow_all: bool = False
) -> int | None:
    """A modal list picker. Returns the index, -1 for "all", or None if cancelled.

    tkinter has no built-in for this, and a combobox in a dialog is about twenty lines — which
    is still less than the alternative of making the caller build a whole window.
    """
    window = tk.Toplevel(root)
    window.title(title)
    window.transient(root)
    window.resizable(False, False)

    result: dict[str, int | None] = {"value": None}

    ttk.Label(window, text=prompt, padding=(12, 12, 12, 6)).pack(anchor="w")

    listbox = tk.Listbox(window, height=min(12, max(3, len(options))), exportselection=False,
                         width=max(28, min(60, max((len(o) for o in options), default=28) + 4)))
    for option in options:
        listbox.insert("end", option)
    listbox.selection_set(0)
    listbox.pack(fill="both", expand=True, padx=12)

    row = ttk.Frame(window, padding=12)
    row.pack(fill="x")

    def take(value: int | None) -> None:
        result["value"] = value
        window.destroy()

    def chosen() -> None:
        picked = listbox.curselection()
        take(picked[0] if picked else None)

    ttk.Button(row, text="Cancel", command=lambda: take(None)).pack(side="right", padx=(6, 0))
    ttk.Button(row, text="OK", command=chosen).pack(side="right")
    if allow_all and len(options) > 1:
        ttk.Button(row, text="All of them", command=lambda: take(-1)).pack(side="left")

    listbox.bind("<Double-Button-1>", lambda _e: chosen())
    window.bind("<Return>", lambda _e: chosen())
    window.bind("<Escape>", lambda _e: take(None))

    window.grab_set()
    listbox.focus_set()
    root.wait_window(window)
    return result["value"]
