"""The window: controls on the left, the deck as it would look on the right.

There is no live link to the hardware. The builder writes deck.json and you reload it from the
agent's tray icon, which is a deliberate trade — nothing in this program opens the serial port,
so it cannot fight the running agent for it, and it works exactly as well with the deck
unplugged. The cost is that the preview is the only feedback loop, which is why the preview got
most of the attention.

Two things are shown at all times rather than on demand, because both fail silently otherwise:
the validator's list of problems, and how close the layout is to the line limit that makes the
deck discard a push without reporting anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import ImageTk

from deckbuilder import budget, render, writer
from deckbuilder.layout_panel import LayoutPanel
from deckbuilder.model import DeckDoc, ModelError
from deckbuilder.theme_form import (
    BASE_POINTS,
    DISPLAY_CHOICES,
    FLIP_CHOICES,
    SETTINGS_DISPLAY_CHOICES,
    ChoiceField,
    ColorField,
    IntField,
    TextField,
)
from deckhost import images
from deckhost.assets import write_stamp
from deckhost.config import default_deck_path

REDRAW_DELAY_MS = 150

ZOOMS = (0.75, 1.0, 1.25, 1.5)

BANNER_COLOURS = {
    "ok": ("#1f3d24", "#9fe0ad"),
    "warn": ("#3d3418", "#f0d68a"),
    "error": ("#3d1f22", "#f0a0a6"),
    "info": ("#20303d", "#a8cfe8"),
}

METER_COLOURS = {"ok": "#3fb950", "near": "#8fb950", "warn": "#d9a441", "over": "#e5534b"}


def enable_dpi_awareness() -> None:
    """Opts into real pixels before Tk starts.

    Without this Windows renders the window at 96 DPI and bitmap-stretches it, which blurs the
    preview — and blurring the preview defeats the point of having one. It also makes the app
    behave the same run from source as it does packaged: PyInstaller's manifest marks the exe
    DPI-aware, so without this call the two disagree about what a pixel is, and any layout
    arithmetic that works in one is wrong in the other.

    Fonts are then scaled back up from the display's DPI, so 12pt stays 12pt on the eye rather
    than becoming 12 device pixels.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def display_scale(root: tk.Tk) -> float:
    if sys.platform != "win32":
        return 1.0
    import ctypes

    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def app_dir() -> Path:
    """Where the tool keeps things that must not live in sdcard/.

    Anything inside sdcard/ is hashed into the card's asset stamp — deckhost/assets.py excludes
    only deck.json and assets.ver — so a backup left beside the file it backs up would make the
    agent report the card as permanently stale.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "multi_deck"


def _safe_stem(stem: str) -> str:
    """A filename the deck can address from deck.json.

    Asset paths are plain ASCII in a JSON string that crosses a serial link and then gets
    opened on a FAT32 card, so a photo called "Holiday 2024 (1).JPG" needs flattening before
    it becomes one.
    """
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-")
    return cleaned or "wallpaper"


class BuilderApp:
    def __init__(self, root: tk.Tk, path: Path) -> None:
        self.root = root
        self.doc = DeckDoc.load(path)
        self.index = 0
        self.photo: ImageTk.PhotoImage | None = None
        self._pending: str | None = None
        self._loading = False
        self._scroll_panes: list[tuple[tk.Canvas, int, ttk.Frame]] = []
        self._problem_signature: tuple = ()
        self._override = False
        self._drag: dict[str, Any] | None = None
        self.preview = render.Preview(image=None)  # replaced on the first draw

        writer.sweep_temp_files(path.parent)

        self.zoom = tk.DoubleVar(value=1.0)
        self.page_var = tk.StringVar()
        self.state_var = tk.StringVar(value="Normal")
        self.link_var = tk.BooleanVar(value=True)

        root.title(f"Deck theme builder — {path}")
        self.scale = display_scale(root)
        root.tk.call("tk", "scaling", self.scale * 96 / 72)
        self._style(BASE_POINTS)
        self._menu()
        self._layout()
        self._load_theme(0)
        self.refresh(immediate=True)
        self._size_window()

    # -- chrome ------------------------------------------------------------------------

    def _size_window(self) -> None:
        """Fits the window to the screen once the controls have told us how wide they are.

        Measured rather than assumed: the widgets size themselves from the font, and the font
        is a preference — a bigger one is the whole reason the text-size menu exists, and it
        moves every number a hardcoded layout would depend on.

        1:1 is the zoom worth defaulting to, being the only one where what you are looking at
        is the size the panel will show. Dropping a step beats a window running off the edge.
        """
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        max_w, max_h = screen_w - 60, screen_h - 110

        left_w = self._fit_left(cap=round(screen_w * 0.42))

        # Everything on the right that is not the preview itself, plus the footer under both.
        chrome_h = (self.controls.winfo_reqheight() + self.caption.winfo_reqheight()
                    + self.footer.winfo_reqheight() + 70)

        # "1:1" means the size it looks, not the number of device pixels. On a 150% display an
        # 800px-wide preview is physically about two thirds of the 7" panel it is standing in
        # for, which is the wrong thing to judge a colour and a corner radius against — so the
        # opening zoom follows the display scale, and steps down only if the window would not
        # otherwise fit.
        preferred = min(ZOOMS[-1], max(1.0, getattr(self, "scale", 1.0)))
        for zoom in sorted(ZOOMS, reverse=True):
            if zoom > preferred:
                continue
            if (left_w + render.SCREEN_W * zoom + 70 <= max_w
                    and chrome_h + render.SCREEN_H * zoom <= max_h):
                break
        else:
            zoom = ZOOMS[0]

        if zoom != self.zoom.get():
            self.zoom.set(zoom)
            self.zoom_box.set(f"{zoom:g}x")
            self.refresh(immediate=True)
            self.root.update_idletasks()

        # Computed rather than taken from winfo_reqwidth alone: a packed layout will happily
        # report a size that assumes the preview can be squeezed, and the preview is the one
        # thing that must never be clipped — it is the entire feedback loop.
        width = min(max_w, max(self.root.winfo_reqwidth(),
                               left_w + round(render.SCREEN_W * zoom) + 70))
        height = min(max_h, max(self.root.winfo_reqheight(),
                                chrome_h + round(render.SCREEN_H * zoom)))
        self.root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+20")
        self.root.minsize(700, 480)

    def _style(self, points: int) -> None:
        style = ttk.Style()
        for name in ("TLabel", "TButton", "TCheckbutton", "TEntry", "TCombobox",
                     "TLabelframe.Label", "TNotebook.Tab", "Treeview"):
            style.configure(name, font=("Segoe UI", points))
        style.configure("Heading.TLabel", font=("Segoe UI", points + 1, "bold"))
        style.configure("Mono.TLabel", font=("Consolas", points))
        self.root.option_add("*Font", ("Segoe UI", points))

    def _menu(self) -> None:
        bar = tk.Menu(self.root)

        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="Open deck.json…", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Save", command=self.save, accelerator="Ctrl+S")
        file_menu.add_command(label="Revert to file", command=self.revert)
        file_menu.add_separator()
        file_menu.add_command(label="Open backups folder", command=self.open_backups)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.quit)
        bar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(bar, tearoff=0)
        edit_menu.add_command(label="Undo", command=self.undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=self.redo, accelerator="Ctrl+Y")
        bar.add_cascade(label="Edit", menu=edit_menu)

        view = tk.Menu(bar, tearoff=0)
        size = tk.Menu(view, tearoff=0)
        for points in (11, 12, 13, 14, 16, 18):
            size.add_command(label=f"{points} pt", command=lambda p=points: self._style(p))
        view.add_cascade(label="Text size", menu=size)
        bar.add_cascade(label="View", menu=view)

        self.root.config(menu=bar)
        self.root.bind_all("<Control-s>", lambda _e: self.save())
        self.root.bind_all("<Control-o>", lambda _e: self.open_file())
        self.root.bind_all("<Control-z>", lambda _e: self.undo())
        self.root.bind_all("<Control-y>", lambda _e: self.redo())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    def _layout(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # The footer is packed before the panes and anchored to the bottom, so the two things
        # that must never be off-screen — how big the layout is, and what is wrong with it —
        # keep their space when the form above them grows.
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(10, 0))

        panes = ttk.Frame(outer)
        panes.pack(side="top", fill="both", expand=True)

        self.left = ttk.Frame(panes, padding=(0, 0, 10, 0), width=self.LEFT_W)
        self.left.pack(side="left", fill="y")
        # Without this the notebook grows to whatever its widest child asks for, and the
        # preview gets pushed off the right-hand edge.
        self.left.pack_propagate(False)
        right = ttk.Frame(panes)
        right.pack(side="left", fill="both", expand=True)
        left = self.left

        self._build_left(left)
        self._build_right(right)
        self._build_footer(footer)

    # -- left ---------------------------------------------------------------------------

    # A starting guess only. The real width is measured from the built form in _fit_left(),
    # because the controls size themselves from the font — and the font changes with both the
    # text-size menu and the display's DPI. The packaged exe is DPI-aware where a plain
    # `python` run is not, so the same layout is about half again as wide there; a hardcoded
    # number is right in exactly one of those two cases.
    LEFT_W = 610

    def _scrollable(self, parent: tk.Widget) -> ttk.Frame:
        """A vertically scrolling column, because seventeen tokens do not fit on a laptop."""
        canvas = tk.Canvas(parent, highlightthickness=0, width=self.LEFT_W, borderwidth=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw", width=self.LEFT_W - 24)
        canvas.configure(yscrollcommand=bar.set)

        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(-e.delta // 120, "units"),
            add="+",
        )
        self._scroll_panes.append((canvas, window, inner))
        return inner

    def _fit_left(self, cap: int) -> int:
        """Widens the controls column to whatever the widest row actually needs."""
        self.root.update_idletasks()
        # Slack for the scrollbar and the grid's own inter-column padding, which winfo_reqwidth
        # on the inner frame does not always account for.
        needed = max(inner.winfo_reqwidth() for _c, _w, inner in self._scroll_panes) + 70
        needed = max(320, min(needed, cap))

        for canvas, window, _inner in self._scroll_panes:
            canvas.configure(width=needed - 24)
            canvas.itemconfigure(window, width=needed - 24)
        self.left.configure(width=needed)
        self.root.update_idletasks()
        return needed

    def _build_left(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent, width=self.LEFT_W)
        notebook.pack(fill="both", expand=True)

        themes = ttk.Frame(notebook, padding=8)
        layout = ttk.Frame(notebook, padding=8)
        settings = ttk.Frame(notebook, padding=8)
        notebook.add(themes, text="  Themes  ")
        notebook.add(layout, text="  Layout  ")
        notebook.add(settings, text="  Settings  ")

        self._build_theme_tab(themes)
        self.layout_panel = LayoutPanel(layout, self)
        self._build_settings_tab(settings)

    def _build_theme_tab(self, parent: ttk.Frame) -> None:
        self.theme_list = tk.Listbox(parent, height=6, exportselection=False,
                                     font=("Segoe UI", BASE_POINTS), activestyle="none")
        self.theme_list.pack(fill="x")
        self.theme_list.bind("<<ListboxSelect>>", self._picked_theme)

        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", pady=(6, 10))
        for text, command in (
            ("New", self.new_theme), ("Duplicate", self.duplicate_theme),
            ("Delete", self.delete_theme), ("Up", lambda: self.move_theme(-1)),
            ("Down", lambda: self.move_theme(1)),
        ):
            ttk.Button(buttons, text=text, command=command, width=9).pack(side="left", padx=2)

        form = self._scrollable(parent)

        self.fields: dict[str, Any] = {}
        row = 0

        def add(cls, key, label, **kwargs):
            nonlocal row
            self.fields[key] = cls(form, row, key, label, self.changed, **kwargs)
            row += 1

        def heading(text):
            nonlocal row
            ttk.Label(form, text=text, style="Heading.TLabel").grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(12, 4)
            )
            row += 1

        heading("Identity")
        add(TextField, "name", "Name")
        add(ChoiceField, "display", "Tile anatomy", choices=DISPLAY_CHOICES)
        add(ChoiceField, "wallpaper", "Wallpaper", choices=self._wallpaper_choices())
        wall_row = ttk.Frame(form)
        wall_row.grid(row=row, column=1, sticky="w", pady=(0, 4))
        ttk.Button(wall_row, text="Convert an image…", command=self.convert_wallpaper).pack(
            side="left"
        )
        row += 1

        heading("Surface")
        add(ColorField, "bg", "Background")
        add(ColorField, "tile", "Tile")
        add(ColorField, "tile_grad", "Tile gradient")
        add(IntField, "tile_opa", "Tile opacity", low=0, high=100)
        add(ColorField, "border", "Border")
        add(IntField, "border_opa", "Border opacity", low=0, high=100)
        add(IntField, "radius", "Corner radius", low=0, high=64)

        heading("Colour")
        add(ColorField, "accent", "Accent")
        add(ColorField, "text", "Text")
        add(ColorField, "text_muted", "Muted text")
        add(ColorField, "ok", "Status — connected")
        add(ColorField, "idle", "Status — no agent")

        heading("From the build")
        add(IntField, "dim_opa", "Idle dim veil", low=0, high=100, nullable=True)
        add(ChoiceField, "flip180", "Rotation", choices=FLIP_CHOICES)
        ttk.Label(
            form,
            text="Left as Default these follow firmware/config.h, which differs\n"
                 "between builds. A value here overrides the board you flashed.",
            foreground="#8b949e", justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        form = self._scrollable(parent)
        self.settings_fields: dict[str, Any] = {}
        row = 0

        def add(cls, key, label, **kwargs):
            nonlocal row
            self.settings_fields[key] = cls(form, row, key, label, self.changed, **kwargs)
            row += 1

        add(ChoiceField, "theme", "Theme at boot", choices=self._theme_choices())
        add(ChoiceField, "display", "Default tile anatomy", choices=SETTINGS_DISPLAY_CHOICES)
        add(IntField, "brightness", "Brightness", low=0, high=100)
        add(IntField, "dim_pct", "Backlight when dimmed", low=0, high=100)
        add(IntField, "idle_dim_s", "Dim after (s)", low=0, high=1800)
        add(IntField, "idle_off_s", "Screen off after (s)", low=0, high=3600)
        add(IntField, "sleep_clock_s", "Clock before off (s)", low=0, high=600)

        ttk.Label(
            form,
            text="Seconds of 0 mean off. Screen-off below dim means the dim\n"
                 "stage never appears, which the validator will say so about.",
            foreground="#8b949e", justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))

    # -- right --------------------------------------------------------------------------

    def _build_right(self, parent: ttk.Frame) -> None:
        controls = self.controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))

        ttk.Label(controls, text="Page").pack(side="left")
        self.page_box = ttk.Combobox(controls, textvariable=self.page_var, state="readonly",
                                     width=14, values=self._page_titles())
        self.page_box.pack(side="left", padx=(6, 16))
        self.page_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh(immediate=True))

        ttk.Label(controls, text="First tile").pack(side="left")
        state_box = ttk.Combobox(controls, textvariable=self.state_var, state="readonly",
                                 width=11, values=("Normal", "Pressed", "Disabled"))
        state_box.pack(side="left", padx=(6, 16))
        state_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh(immediate=True))

        ttk.Checkbutton(controls, text="Agent connected", variable=self.link_var,
                        command=lambda: self.refresh(immediate=True)).pack(side="left")

        ttk.Label(controls, text="Zoom").pack(side="left", padx=(16, 6))
        self.zoom_box = ttk.Combobox(controls, state="readonly", width=6,
                                     values=[f"{z:g}x" for z in ZOOMS])
        self.zoom_box.set(f"{self.zoom.get():g}x")
        self.zoom_box.pack(side="left")
        self.zoom_box.bind("<<ComboboxSelected>>",
                           lambda e: (self.zoom.set(ZOOMS[e.widget.current()]),
                                      self.refresh(immediate=True)))

        frame = ttk.Frame(parent, relief="sunken", borderwidth=1)
        frame.pack()
        self.canvas = tk.Canvas(frame, width=render.SCREEN_W, height=render.SCREEN_H,
                                highlightthickness=0, background="#000000")
        self.canvas.pack()

        # Selection and drag feedback are canvas items drawn over the Pillow image rather than
        # baked into it. That keeps render.py a pure transcription of the firmware — it has no
        # notion of "selected", because the deck has none — and means clicking a tile does not
        # cost a re-render of an 800x480 composite.
        self.canvas.bind("<Button-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)

        self.caption = ttk.Label(parent, foreground="#8b949e", justify="left")
        self.caption.pack(anchor="w", pady=(6, 0))

    # -- footer -------------------------------------------------------------------------

    def _build_footer(self, parent: ttk.Frame) -> None:
        footer = self.footer = ttk.Frame(parent)
        footer.pack(fill="x")

        meter = ttk.Labelframe(footer, text=" Layout size ", padding=8)
        meter.pack(side="left", fill="y")
        self.meter = tk.Canvas(meter, width=320, height=16, highlightthickness=0)
        self.meter.pack()
        self.meter_label = ttk.Label(meter, style="Mono.TLabel")
        self.meter_label.pack(anchor="w", pady=(4, 0))
        self.meter_detail = ttk.Label(meter, foreground="#8b949e", wraplength=320,
                                      justify="left")
        self.meter_detail.pack(anchor="w")

        problems = ttk.Labelframe(footer, text=" Problems ", padding=8)
        problems.pack(side="left", fill="both", expand=True, padx=(10, 10))
        self.problems = tk.Text(problems, height=5, wrap="word", relief="flat",
                                font=("Consolas", BASE_POINTS - 1))
        self.problems.pack(fill="both", expand=True)
        self.problems.configure(state="disabled")

        actions = ttk.Frame(footer)
        actions.pack(side="left", fill="y")
        self.save_button = ttk.Button(actions, text="Save deck.json", command=self.save)
        self.save_button.pack(fill="x")
        self.banner = tk.Label(actions, text="", wraplength=240, justify="left",
                               anchor="w", padx=8, pady=6, font=("Segoe UI", BASE_POINTS))
        self.banner.pack(fill="both", expand=True, pady=(8, 0))
        self._say("Edit a theme, save, then reload from the tray icon.", "info")

    # -- data plumbing ------------------------------------------------------------------

    def _page_titles(self) -> list[str]:
        return [p.get("title") or p.get("id") or "?" for p in self.doc.pages]

    def _page_id(self) -> str | None:
        pages = self.doc.pages
        titles = self._page_titles()
        if self.page_var.get() in titles:
            return pages[titles.index(self.page_var.get())].get("id")
        return pages[0].get("id") if pages else None

    def _theme_choices(self) -> tuple[tuple[str, Any], ...]:
        return tuple((name, name) for name in self.doc.theme_names())

    # -- what the layout panel needs from the window -------------------------------------

    def library_dir(self) -> Path:
        """Where a park/import dialog opens.

        There is no fixed library home — that was decided deliberately, so a theme can live in
        Dropbox or beside a project — which makes "wherever you were last" the only sensible
        starting point. Falls back to the deck's own folder rather than to nothing.
        """
        return last_library() or self.doc.path.parent

    def remember_library(self, folder: Path) -> None:
        remember_library(folder)

    def app_backups(self) -> Path:
        return app_dir() / "backups"

    def _wallpaper_choices(self) -> tuple[tuple[str, Any], ...]:
        options: list[tuple[str, Any]] = [("None (flat background)", "")]
        root = self.doc.path.parent
        for path in sorted((root / "wall").glob("*.bin")) if (root / "wall").is_dir() else []:
            options.append((f"/wall/{path.name}", f"/wall/{path.name}"))
        current = self.doc.themes[self.index].get("wallpaper") if self.doc.themes else ""
        if current and current not in [value for _, value in options]:
            options.append((f"{current}  (missing)", current))
        return tuple(options)

    def _load_theme(self, index: int) -> None:
        self._loading = True
        try:
            self.index = max(0, min(index, len(self.doc.themes) - 1))
            theme = self.doc.themes[self.index]
            self.fields["wallpaper"].set_choices(self._wallpaper_choices())
            for key, field in self.fields.items():
                field.set(theme.get(key))
            for key, field in self.settings_fields.items():
                if key == "theme":
                    field.set_choices(self._theme_choices())
                field.set(self.doc.settings.get(key))
            self._refresh_list()
        finally:
            self._loading = False

    def _refresh_list(self) -> None:
        self.theme_list.delete(0, "end")
        for i, name in enumerate(self.doc.theme_names()):
            cost = budget.theme_cost(self.doc.themes[i])
            self.theme_list.insert("end", f"{name}    ({cost:,} bytes)")
        self.theme_list.selection_clear(0, "end")
        self.theme_list.selection_set(self.index)

    def _picked_theme(self, _event) -> None:
        selection = self.theme_list.curselection()
        if selection and selection[0] != self.index:
            self.collect()
            self._load_theme(selection[0])
            self.refresh(immediate=True)

    def collect(self) -> None:
        """Reads every control back into the document."""
        if self._loading or not self.doc.themes:
            return
        theme = self.doc.themes[self.index]
        for key, field in self.fields.items():
            if key == "name":
                continue  # renaming has to update references, so it goes through the model
            theme[key] = field.get()

        name = self.fields["name"].get()
        if name and name != theme.get("name"):
            self.doc.rename(self.index, name)

        for key, field in self.settings_fields.items():
            self.doc.settings[key] = field.get()

    # -- refresh ------------------------------------------------------------------------

    def changed(self) -> None:
        """Called by every control. Coalesced, because typing a hex colour fires six times."""
        if self._loading:
            return
        if self._pending is not None:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(REDRAW_DELAY_MS, lambda: self.refresh(immediate=True))

    def refresh(self, *, immediate: bool = False) -> None:
        self._pending = None
        self.collect()
        self._draw_preview()
        self._draw_meter()
        self._draw_problems()
        self._refresh_list()
        self.layout_panel.refresh()
        self.page_box.configure(values=self._page_titles())
        titles = self._page_titles()
        if self.page_var.get() not in titles:
            # A page can now be renamed, parked or deleted out from under the combobox, and a
            # stale selection there renders pages[0] while claiming to be showing something else.
            self.page_var.set(titles[0] if titles else "")

    def _draw_preview(self) -> None:
        theme = self.doc.themes[self.index]
        preview = render.render_page(
            self.doc.candidate_raw(), theme, self._page_id(),
            asset_root=self.doc.path.parent,
            link_up=self.link_var.get(),
            state=self.state_var.get().lower(),
        )
        image = preview.image
        zoom = self.zoom.get()
        if zoom != 1.0:
            image = image.resize(
                (round(render.SCREEN_W * zoom), round(render.SCREEN_H * zoom))
            )
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.configure(width=image.width, height=image.height)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        self.preview = preview
        self._draw_selection()

        caption = preview.font_name
        if preview.warnings:
            caption += "\n" + "  •  ".join(preview.warnings)
        self.caption.configure(text=caption)

    # -- the canvas as a layout tool -----------------------------------------------------

    def _draw_selection(self) -> None:
        """Outlines the selected tile, and ghosts anything the firmware would not draw."""
        self.canvas.delete("overlay")
        zoom = self.zoom.get()

        for button_id in self.preview.skipped:
            # Not in `boxes`, because it is not on the panel. Drawn at the edge as a hollow
            # marker so a tile the deck silently drops is visible here rather than merely absent
            # — "it disappeared" is the hardest version of this bug to diagnose.
            self.canvas.create_text(
                render.SCREEN_W * zoom - 8, render.SCREEN_H * zoom - 8,
                text=f"off-grid: {button_id}", anchor="se", fill="#e5534b", tags="overlay",
            )

        selected = self._selected_button_id()
        box = self.preview.boxes.get(selected) if selected else None
        if box is None:
            return

        x0, y0, x1, y1 = (round(v * zoom) for v in box)
        self.canvas.create_rectangle(
            x0, y0, x1 - 1, y1 - 1, outline="#4aa3ff", width=2, tags="overlay"
        )

    def _selected_button_id(self) -> str | None:
        picked = self.layout_panel.selection()
        if picked is None or picked[0] != "button":
            return None
        _kind, page_index, index = picked
        if self.doc.pages[page_index].get("id") != self._page_id():
            return None
        try:
            return self.doc.pages[page_index]["buttons"][index].get("id")
        except (IndexError, KeyError):
            return None

    def _at(self, event) -> tuple[int, int]:
        """Canvas coordinates in the preview's own pixels, undoing the zoom."""
        zoom = self.zoom.get() or 1.0
        return round(self.canvas.canvasx(event.x) / zoom), round(
            self.canvas.canvasy(event.y) / zoom
        )

    def _canvas_press(self, event) -> None:
        x, y = self._at(event)
        button_id = self.preview.at(x, y)
        self._drag = None
        if button_id is None:
            return

        located = self._locate(button_id)
        if located is None:
            return
        page_index, index = located
        self._drag = {"id": button_id, "page": page_index, "index": index, "from": (x, y)}
        self.layout_panel.select_button(page_index, index)

    def _canvas_drag(self, event) -> None:
        if not self._drag:
            return
        x, y = self._at(event)
        zoom = self.zoom.get()

        self.canvas.delete("drag")
        cols_rows = self.preview.cell
        if cols_rows is None:
            return

        cell = render.cell_at(x, y, *cols_rows)
        if cell is None:
            return

        cell_w, cell_h = render._cells(*cols_rows)
        box = render._tile_box(cell[0], cell[1], 1, 1, cell_w, cell_h)
        x0, y0, x1, y1 = (round(v * zoom) for v in box)
        self.canvas.create_rectangle(
            x0, y0, x1 - 1, y1 - 1, outline="#3fb950", width=2, dash=(4, 3), tags="drag"
        )

    def _canvas_release(self, event) -> None:
        drag = self._drag
        self._drag = None
        self.canvas.delete("drag")
        if not drag:
            return

        x, y = self._at(event)
        if self.preview.cell is None:
            return
        cell = render.cell_at(x, y, *self.preview.cell)
        if cell is None:
            return  # a gutter, the nav bar, or the margin — no guessing at the nearer side

        page_index, index = drag["page"], drag["index"]
        if (x, y) == drag["from"]:
            return  # a click, not a drag

        if self.doc.is_pinned(page_index):
            self._drop_pinned(page_index, index, cell)
        else:
            self._drop_auto(page_index, index, cell)

    def _drop_pinned(self, page_index: int, index: int, cell: tuple[int, int]) -> None:
        """Fixed page: the drop writes col/row, which is what Fixed is for."""
        button = self.doc.pages[page_index]["buttons"][index]
        if (button.get("pos") or {}).get("col") == cell[0] and \
                (button.get("pos") or {}).get("row") == cell[1]:
            return

        self.doc.snapshot()
        pos = dict(button.get("pos") or {"w": 1, "h": 1})
        pos["col"], pos["row"] = cell
        button["pos"] = {key: pos.get(key, 1) for key in ("col", "row", "w", "h")}
        self._say(
            f"Moved {button.get('id')} to column {cell[0]}, row {cell[1]}. "
            "Overlaps are listed below rather than prevented — two tiles in one cell is legal "
            "on the device, and the top one simply wins.",
            "ok",
        )
        self.refresh(immediate=True)

    def _drop_auto(self, page_index: int, index: int, cell: tuple[int, int]) -> None:
        """Auto page: the drop reorders the array, which costs nothing on the wire."""
        cols = (self.preview.cell or (4, 3))[0]
        target = min(cell[1] * cols + cell[0], len(self.doc.pages[page_index]["buttons"]) - 1)
        if target == index:
            return

        self.doc.snapshot()
        new_index = self.doc.move_button(page_index, index, target - index)
        self.layout_panel.select_button(page_index, new_index)
        self._say(
            "Reordered. On an Auto page the order in the file is the layout, so this costs "
            "nothing — switch the page to Fixed if you want spans or deliberate gaps.",
            "ok",
        )
        self.refresh(immediate=True)

    def _locate(self, button_id: str) -> tuple[int, int] | None:
        for page_index, page in enumerate(self.doc.pages):
            for index, button in enumerate(page.get("buttons") or []):
                if button.get("id") == button_id:
                    return page_index, index
        return None

    def _draw_meter(self) -> None:
        report = budget.report(self.doc.next_rev(), self.doc.candidate_raw())
        width, height = 320, 16
        self.meter.delete("all")
        self.meter.create_rectangle(0, 0, width, height, fill="#20262e", outline="")
        filled = min(width, round(width * report.fraction))
        self.meter.create_rectangle(0, 0, filled, height,
                                    fill=METER_COLOURS[report.level], outline="")
        warn_x = round(width * budget.WARN_FRACTION)
        self.meter.create_line(warn_x, 0, warn_x, height, fill="#e6edf3", width=1)
        self.meter_label.configure(text=report.summary())
        self.meter_detail.configure(text=report.detail())

    def _draw_problems(self) -> None:
        shape = self.doc.shape_problems()
        problems = self.doc.problems()
        notices = self.doc.notices()

        lines = [f"• {p}" for p in shape + problems] + [f"– {n}" for n in notices]
        self.problems.configure(state="normal")
        self.problems.delete("1.0", "end")
        self.problems.insert("1.0", "\n".join(lines) if lines
                             else "Nothing wrong with this layout.")
        self.problems.configure(state="disabled")

        # Arming the override is per problem list, not per session: fix one thing and the next
        # Save asks again, rather than quietly carrying permission over to a different mistake.
        signature = (tuple(shape), tuple(problems))
        if signature != self._problem_signature:
            self._problem_signature = signature
            self._override = False

        self.save_button.configure(state="normal" if self._can_save(shape) else "disabled")

    def _can_save(self, shape_problems: list[str]) -> bool:
        """Whether Save is even offered — the two things clicking again cannot fix.

        Deliberately *not* fed the validator's list. That list is offered with an override,
        because the person editing may know something the validator does not and the file is
        theirs. These two are different: a drifted key set would be written by the dump exactly
        as handed to it, and an over-limit frame is discarded by the deck in silence. Neither
        has a "yes I meant it" that produces a working deck.

        The earlier version of this method took the validator's list as an argument and then
        ignored it, which read as though the check existed.
        """
        if shape_problems:
            return False
        return not budget.report(self.doc.next_rev(), self.doc.candidate_raw()).over_limit

    def _say(self, text: str, level: str) -> None:
        background, foreground = BANNER_COLOURS[level]
        self.banner.configure(text=text, background=background, foreground=foreground)

    # -- commands -----------------------------------------------------------------------

    def new_theme(self) -> None:
        self.collect()
        self.doc.snapshot()
        self._load_theme(self.doc.new_theme())
        self.refresh(immediate=True)

    def duplicate_theme(self) -> None:
        self.collect()
        self.doc.snapshot()
        self._load_theme(self.doc.duplicate(self.index))
        self.refresh(immediate=True)

    def delete_theme(self) -> None:
        self.collect()
        name = self.doc.themes[self.index].get("name")
        if not messagebox.askyesno("Delete theme", f"Delete {name!r}?", parent=self.root):
            return
        self.doc.snapshot()
        try:
            self.doc.delete(self.index)
        except ModelError as exc:
            self.doc.undo()
            self._say(str(exc), "error")
            return
        self._load_theme(min(self.index, len(self.doc.themes) - 1))
        self.refresh(immediate=True)

    def move_theme(self, delta: int) -> None:
        self.collect()
        self.doc.snapshot()
        self._load_theme(self.doc.move(self.index, delta))
        self.refresh(immediate=True)

    def undo(self) -> None:
        self.collect()
        if self.doc.undo():
            self._load_theme(self.index)
            self.refresh(immediate=True)
            self._say("Undone.", "info")

    def redo(self) -> None:
        self.collect()
        if self.doc.redo():
            self._load_theme(self.index)
            self.refresh(immediate=True)
            self._say("Redone.", "info")

    def convert_wallpaper(self) -> None:
        """Converts an image into an MDI1 wallpaper and restamps the card folder.

        Done in-process through deckhost.images, which is the same pipeline
        tools/make_assets.py drives from the command line — not a second copy of it.

        It used to shell out to that script, which worked from a checkout and could never have
        worked from the packaged exe: there is no interpreter to run it with, and app.py is not
        even a file on disk once PyInstaller has folded it into the archive.
        """
        source = filedialog.askopenfilename(
            parent=self.root, title="Choose an image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp *.gif *.tif *.tiff"),
                       ("All files", "*.*")],
        )
        if not source:
            return

        root = self.doc.path.parent
        destination = root / "wall" / (_safe_stem(Path(source).stem) + ".bin")
        try:
            images.wallpaper(Path(source), destination, dim_percent=35)
            # The same restamp make_assets.py does on every conversion. Without it the card's
            # generation no longer describes its contents, and the agent's staleness check
            # starts reporting a mismatch that is not real.
            write_stamp(root)
        except Exception as exc:  # a bad image must not take the window down
            self._say(f"Could not convert that image: {exc}", "error")
            return

        render.clear_asset_cache()
        self.fields["wallpaper"].set_choices(self._wallpaper_choices())
        self.fields["wallpaper"].set(f"/wall/{destination.name}")
        self.refresh(immediate=True)
        self._say(
            f"Converted to /wall/{destination.name}. Images never travel over USB — "
            f"copy {root.name}/ to the card by hand.", "warn",
        )

    def save(self) -> None:
        self.collect()

        shape = self.doc.shape_problems()
        if shape:
            self._say("Refusing to write: " + shape[0], "error")
            return

        rev = self.doc.next_rev()
        report = budget.report(rev, self.doc.candidate_raw())
        if report.over_limit:
            self._say(f"Refusing to write: {report.summary()}. {report.detail()}", "error")
            return

        # The agent refuses a layout with problems and exits, and it starts at logon — so this
        # is not a warning about the deck looking wrong, it is a warning about the deck stopping
        # working until someone opens a log. Overridable, because the person editing may be
        # about to add the page the dangling reference points at, and it is their file.
        problems = self.doc.problems()
        if problems and not self._override:
            self._override = True
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            self._say(
                f"Refusing to write: {problems[0]}{more}"
                "\n\nClick Save again to write it anyway. The agent refuses a layout with "
                "problems, so it will not start at logon and the deck keeps the layout it "
                "already has.",
                "error",
            )
            return

        try:
            result = writer.write(
                self.doc.path,
                themes=self.doc.themes,
                settings=self.doc.settings,
                pages=self.doc.pages,
                rev=rev,
                backup_dir=app_dir() / "backups",
            )
        except OSError as exc:
            self._say(f"Could not write {self.doc.path}: {exc}", "error")
            return
        except writer.WriteError as exc:
            # The scope guard or the reparse check fired, which means this build has a bug
            # rather than the file having a problem. Nothing was written; say so plainly so the
            # message is not mistaken for "fix your layout".
            self._say(f"Refusing to write — the writer caught itself: {exc}", "error")
            return

        self.doc.mark_saved(result.text, result.rev)

        message = f"Saved as rev {result.rev}. Right-click the tray icon → Reload deck.json."
        level = "ok"
        if result.reformatted:
            message += "\n\n" + (result.warning or "")
            level = "warn"
        if report.over_warning:
            message += "\n\n" + report.detail()
            level = "warn"
        self._say(message, level)
        self.refresh(immediate=True)

    def revert(self) -> None:
        if self.doc.dirty and not messagebox.askyesno(
            "Revert", "Discard unsaved changes?", parent=self.root
        ):
            return
        self.doc = DeckDoc.load(self.doc.path)
        render.clear_asset_cache()
        self._load_theme(self.index)
        self.refresh(immediate=True)
        self._say("Reloaded from disk.", "info")

    def open_file(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.root, title="Open deck.json",
            filetypes=[("deck.json", "deck.json"), ("JSON", "*.json")],
        )
        if not chosen:
            return
        try:
            self.doc = DeckDoc.load(Path(chosen))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open failed", str(exc), parent=self.root)
            return
        remember(Path(chosen))
        render.clear_asset_cache()
        self.root.title(f"Deck theme builder — {chosen}")
        self.page_var.set("")
        self._load_theme(0)
        self.refresh(immediate=True)

    def open_backups(self) -> None:
        folder = app_dir() / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # type: ignore[attr-defined]

    def quit(self) -> None:
        self.collect()
        if self.doc.dirty and not messagebox.askyesno(
            "Quit", "There are unsaved changes. Quit anyway?", parent=self.root
        ):
            return
        self.root.destroy()


# -- entry point -------------------------------------------------------------------------


def _settings_file() -> Path:
    return app_dir() / "builder.json"


def _stored() -> dict:
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(**values) -> None:
    try:
        _settings_file().parent.mkdir(parents=True, exist_ok=True)
        _settings_file().write_text(json.dumps({**_stored(), **values}), encoding="utf-8")
    except OSError:
        pass


def remember(path: Path) -> None:
    _store(deck=str(path))


def remember_library(folder: Path) -> None:
    """Kept separately from the deck path, and separately on purpose.

    Library files live wherever you want them — that was the decision — so the folder you park
    into has nothing to do with where deck.json is. Storing one key for both means the dialog
    opens in the wrong place every other time you use it.
    """
    _store(library=str(folder))


def last_used() -> Path | None:
    """Where the last session was pointed.

    A packaged exe has no idea where the repo is, so the alternative to remembering is a file
    dialog on every launch.
    """
    stored = _stored().get("deck")
    return Path(stored) if stored and Path(stored).exists() else None


def last_library() -> Path | None:
    stored = _stored().get("library")
    return Path(stored) if stored and Path(stored).is_dir() else None


def resolve(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit if explicit.exists() else None
    remembered = last_used()
    if remembered:
        return remembered
    default = default_deck_path()
    return default if default.exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckbuilder", description="Edit the themes and settings in deck.json"
    )
    parser.add_argument("--deck", type=Path, help="path to deck.json")
    args = parser.parse_args(argv)

    enable_dpi_awareness()
    root = tk.Tk()
    path = resolve(args.deck)

    if path is None:
        chosen = filedialog.askopenfilename(
            parent=root, title="Where is deck.json?",
            filetypes=[("deck.json", "deck.json"), ("JSON", "*.json")],
        )
        if not chosen:
            root.destroy()
            return 1
        path = Path(chosen)

    try:
        app = BuilderApp(root, path)
    except (OSError, ValueError) as exc:
        messagebox.showerror("Could not open deck.json", f"{path}\n\n{exc}")
        root.destroy()
        return 1

    remember(app.doc.path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
