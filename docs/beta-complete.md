# Beta complete — where the project stopped, and what is left

**Status as of 2026-08-02: firmware `0.5.4`, layout `rev 9`, 77 tests passing.**

The deck is feature-complete enough to live on the desk. Development is paused deliberately, to
use it for a while and gather real UI/UX feedback before building more — because most of what is
left is polish, and polish decided from a chair is usually decided wrong.

This file exists so picking the work back up costs minutes, not an afternoon of rereading code.

---

## What is done

The original build plan's **Phases 0–5** and the whole **visual design pass (S1–S4)**:

| Area | State |
|---|---|
| Panel, touch, HID ten-key, macros | Standalone — all work with the agent closed |
| SD, CDC link, PC agent, autostart | Windows scheduled task, tray reload, live layout push |
| Theme engine | One style set in `theme.cpp`; every colour comes from `deck.json` |
| Themes | 6 shipped, 3 with wallpapers; device-local switching persisted to `/theme.txt` |
| Wallpapers + glass tiles | Raw RGB565 from SD into PSRAM, ~575 ms for a full screen |
| Icons | 61 LVGL symbols by name, plus MDI1 images from SD |
| Tile anatomy | `icon_text` / `icon` / `text`, resolved button → theme → `settings` |
| Stats page | Four cards, themed arcs and chart |
| Fonts | Montserrat (tiles, nav, **all icons**), Nord Medium 40 (ten-key), Century Gothic (stats) |
| Asset stamp | Card-vs-repo hash compared on connect, warns when the card is stale |

Two guards worth remembering, because they exist to stop expensive bugs recurring:

- `config.h` **`static_assert`s every SD pin against `MD_PANEL_PINS`**. `MD_SD_CS_PLACEHOLDER`
  was once GPIO10 — the RGB panel's blue bit 4 — and `SD.begin()` pinned blue high, so black
  rendered as mid-blue.
- `UsbLink::rawWrite()` **must not gate on `availableForWrite()`**. That was a hard 64-byte
  ceiling on every frame, and `hello` sat at exactly 64. See
  [hardware-notes.md](hardware-notes.md).

---

## Feedback log

The point of this pause. Add observations here while using the deck — a note written the moment
something annoys you is worth more than a design session later.

| Date | Observation | Thought |
|---|---|---|
| | | |

Things worth paying attention to specifically:

- Which tiles do you actually press, and which are dead weight? The Launch page was populated by
  guesswork.
- Do you use the theme switcher, or settle on one? If one, the other five are cost without value.
- Is `icon_text` the right default, or do the icons alone carry it once you know the layout?
- Does the ten-key get used enough to keep a whole page?
- Is 4×3 the right grid, or is 5×3 usable at arm's length?

---

## Backlog

Ordered by value-for-effort, not by ambition.

### 1. Nav-bar clock — small

The agent already pushes a `stats` frame every second; add `time` to it and the nav bar's empty
right-hand third earns its space. Hides itself when the agent is closed, like every other
agent-dependent element.

*Touches:* `agent/deckhost/stats.py`, `ui_builder.cpp` (nav build), `docs/protocol.md`.

### 2. Slimmer nav — small

`NAV_H` is 56 px: 12% of the screen for four tabs. Icon-only tabs would roughly halve it and give
every page the space back. The icon machinery from S3 already exists — a tab is just a label, so
`icons::symbol()` works there unchanged.

*Touches:* `theme.h` (`NAV_H`), `ui_builder.cpp`, `deck_config.{h,cpp}` (a page needs an `icon`).

### 3. On-device icon browser — small

A `"type": "icons"` page listing all 61 symbols with their names, same pattern as the retired
Colours page. Picking an icon from the panel in the real font at real size beats reading the list
in [editing-the-deck.md](editing-the-deck.md).

*Touches:* new `icon_browser.{h,cpp}`, `PageType`, `ui_builder.cpp`, `agent/deckhost/config.py`.

### 4. Real frosted glass — medium

LVGL has no backdrop blur, but there is a trick that costs nothing at runtime: emit a second,
pre-blurred copy of the wallpaper and give each tile the region of it that sits behind that tile
via `lv_image_set_offset_x/y`. Genuine per-tile frost, computed offline.
`tools/make_assets.py` already has `--blur`.

*Touches:* `make_assets.py`, `theme.cpp`, `ui_builder.cpp`.

### 5. Per-tile background photos — medium

Same mechanism as wallpapers. Skipped originally because a good icon on a glass tile beats a
photo tile and costs far less asset wrangling. Revisit only if the icons disappoint in use.

### 6. Assets over USB — medium

So the SD card never has to come out. Two routes: chunked binary transfer over the existing link
(~150 lines across firmware and agent), or a TinyUSB mass-storage interface exposing the card as
a drive letter — slicker, but the device must unmount its own SD while the PC holds it.

The asset stamp already tells you *when* the card is stale, which takes most of the pain out of
the manual copy. Judge this after living with it.

### 7. Wireless — large, own plan

Phase 6, the only unbuilt phase of the original plan. The protocol was designed for it: adding
WiFi means implementing `Link` on both sides and **no frames change**
([protocol.md](protocol.md)). BLE HID is a separate question from the agent link and could land
independently.

Treat as its own plan rather than a follow-on. Consider whether it is wanted at all first — the
device sits on a desk beside the laptop it drives.

---

## Loose ends

- **`color_test.{h,cpp}`** is still compiled but no longer referenced by `deck.json`; the tab was
  retired once the GPIO10 bug was found. `--gc-sections` keeps it out of the binary. Delete it,
  or keep it as a bench diagnostic — it is genuinely useful if the panel ever misbehaves again.
- **`fonts/nord30.h`, `nord40.h`, `nord42.h`** are redundant: the `.vlw` originals beside them
  cover those sizes and more, and `make_font.py` reads both.
- **Nord Medium 10/12/26/30/42/44** are unconverted. Only 14/20/28/40 have a same-size Montserrat
  to fall back on, which matters because Nord has no space glyph.
- **Geoform Bold** exists only as a 72 px VLW — too large for anything in this UI.
- **Four settings are parsed but never used.** Found while writing the README user guide, so the
  guide documents the real behaviour rather than the intended one:
  - `settings.idle_off_s` — the backlight dims to 10% after `idle_dim_s` and never switches off.
    Either implement it in `ui::tick()` beside the dim check, or drop the field.
  - `MD_LONG_PRESS_MS` (600), `MD_KEY_REPEAT_DELAY_MS` (400) and `MD_KEY_REPEAT_RATE_MS` (60) in
    `config.h` are dead. The real values are LVGL's indev defaults — **400 ms** to hold and
    100 ms between repeats — because nothing calls `lv_indev_set_long_press_time()`. Wire them
    up or delete them; right now they document timings that are not in force.

---

## Rejected, with reasons

Recorded so they are not re-litigated.

- **Portrait (90°/270°).** RGB panels cannot rotate in hardware, so it means LVGL transposing
  every rendered fragment on the CPU — strided PSRAM writes, the slow direction — *plus* a second
  layout, since the nav bar and grid are built for a wide screen. More work than S1–S4 together.
  The 180° flip is free and is a theme setting; smart cropping in `make_assets.py` gets portrait
  photos onto the deck without any of it.
- **Widget scale/zoom press animations.** Not a preference: `LV_USE_MATRIX` is `0` and a
  transformed 190×130 tile needs a ~49 KB layer from a 48 KB non-expanding pool. It would fail at
  runtime. Press feedback uses `translate_y`, which needs no layer.
- **Opaque stats page.** The plan called for it on cost grounds; the arithmetic does not hold
  (~150k px/s against a 384k full frame) and it would hide the wallpaper on the largest page.
  The real readability problem — a hairline chart trace over a photo — is fixed on the trace.

---

## Picking this back up

1. Read the feedback log above first. It is the only new information.
2. `python tools/protocol_test.py` — 77 tests, no hardware needed.
3. Flash and watch port A (COM4, 115200). A healthy boot ends with `[main] ready` then
   `[link] session up`. The exact compile command is in the README under
   *Compile check without the IDE*.
4. [hardware-notes.md](hardware-notes.md) before touching firmware. It is the most valuable file
   in the repo and every entry cost hours.
