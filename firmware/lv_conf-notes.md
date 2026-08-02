# lv_conf.h setup

LVGL is configured at compile time by `lv_conf.h`. This repo does not ship one, because the
file must match the exact LVGL version installed — copying a stale one in is a reliable way
to produce a hundred confusing compile errors.

## Status on this machine

**Done.** `Documents/Arduino/libraries/lv_conf.h` exists (LVGL v9.5.0) and its guard has been
flipped to `#if 1`. Nothing further is needed unless LVGL is upgraded.

## The `#if 0` trap

The shipped `lv_conf.h` opens with:

```c
#if 0 /* Set this to "1" to enable content */
```

Left at `0`, the file is found and included but **every setting inside is skipped**, so LVGL
silently falls back to its built-in defaults. The only symptom is a `#pragma message` buried
in the build output:

> Possible failure to include lv_conf.h, please read the comment in this file if you get errors

It is a note, not an error, so the build proceeds — and then widgets are missing or the
colours are wrong for reasons that appear unrelated. If LVGL ever behaves as though this file
is being ignored, check this line first.

## Placement

Arduino IDE expects `lv_conf.h` in the **`libraries/` root, beside the `lvgl` folder** — not
inside it:

```
Documents/Arduino/libraries/
    lv_conf.h        <-- here
    lvgl/
    ESP32_Display_Panel/
    ...
```

## Settings

LVGL 9.5.0's default template already matches what this project needs, so no edits beyond the
guard were required. For reference, these are the ones that matter and their confirmed values:

| Setting | Value | Why |
|---|---|---|
| `LV_COLOR_DEPTH` | `16` | The panel is RGB565 |
| `LV_USE_OS` | `LV_OS_NONE` | We drive `lv_timer_handler()` from `loop()` |
| `LV_USE_CHART` | `1` | Stats page CPU history |
| `LV_USE_ARC` | `1` | Stats page gauges |
| `LV_USE_BAR` | `1` | |
| `LV_FONT_MONTSERRAT_20/28/40` | `1` | Tile labels, numpad keys, gauge values |

Optional during bring-up: set `LV_USE_LOG` to `1` with `LV_LOG_LEVEL` at `LV_LOG_LEVEL_WARN`,
and point `LV_LOG_PRINTF` at `Serial`. Turn it back off once things work.

Leave `LV_TICK_CUSTOM` alone — LVGL 9 uses `lv_tick_set_cb()`, which `lvgl_v9_port.cpp`
already calls with `millis`.

## If colours look wrong

RGB parallel panels generally need **no** byte swap, unlike SPI panels. If the first render
comes out with red and blue transposed, set `MD_RGB565_SWAP` to `1` in
`firmware/multi_deck/config.h` rather than changing anything in `lv_conf.h` — the port
handles the swap itself.
