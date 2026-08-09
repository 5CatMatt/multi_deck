# Editing the deck layout

Everything the deck shows — pages, tiles, labels, actions, colours, brightness — comes from a
single file. No reflashing, no recompiling.

## Where the file lives

There are two copies, and it matters which you edit.

| Copy | Role |
|---|---|
| **`sdcard/deck.json`** in this repo | **The master. Edit this one.** The PC agent reads it |
| `/deck.json` on the device's SD card | A cache. The device overwrites it whenever the agent pushes |

The device only reads its SD copy **at boot**, and only to have something to show before the
agent connects. The agent is authoritative: whenever the two disagree, the agent's copy wins and
gets written to the card.

Images are the exception, and the only thing in this file that does not hot-reload. They are too
big for the link, so they only reach the card by hand — see [Wallpapers](#wallpapers).
`sdcard/assets.ver` is a generated hash of everything else in `sdcard/`; leave it alone and let
`tools/make_assets.py` write it. It is how the deck knows to tell you the card needs rewriting.

## Getting a change onto the deck

### The quick way — tray reload

1. Edit `sdcard/deck.json`, save.
2. Right-click the tray icon → **Reload deck.json**.

The agent re-reads the file, validates it, pushes it over USB, and the deck rebuilds its screen
immediately. The device also writes the new copy to its SD card, so it survives a power cycle.

**No `rev` bump needed for this path.** A reload always pushes.

If the file has an error, the agent logs it, shows `deck.json has errors` on the deck, and **keeps
the layout that is currently running** — a typo cannot leave you with a blank screen.

### The other way — bump `rev`

The agent re-reads `deck.json` from disk whenever a session starts: at launch, and on every
reconnect. So an edit made while the deck was unplugged — or, far more often, while it was being
reflashed — is already in hand by the time the device comes back.

What it does *not* do is push unprompted. On connect it compares its `rev` against the device's
and only pushes if they differ. Edit the file without changing `rev` and the two still claim to be
the same revision, so nothing moves.

```jsonc
{
  "rev": 4,        // <-- increment this when you are not using tray reload
  ...
}
```

**Reflashing the firmware never changes the layout.** Pages, tiles and themes are data the agent
owns; the device only knows how to render them. A new page type in firmware still needs a page in
`deck.json` before it appears — bump `rev`, or just hit tray reload.

Tray reload is easier and always pushes. `rev` exists so a device that boots with a stale SD card
catches up on its own.

### The blunt way — copy the file

Pull the card, copy `sdcard/deck.json` to its root, put it back. Only needed if you want the deck
to show something specific before the agent ever connects.

## Themes

`deck.json` carries a list of themes. One is active; a tile can step through them.

```jsonc
"themes": [
  {
    "name":       "Midnight",   // how you refer to it; shown as a toast when it is selected
    "display":    "",           // tile anatomy for this theme; "" inherits — see Tile anatomy
    "wallpaper":  "",           // path to an image on the SD card, e.g. "/wall/dusk.bin"

    "bg":         "#0d1117",    // the backdrop, behind everything
    "tile":       "#1b2129",    // button face
    "tile_grad":  "#141a21",    // second gradient stop; omit for a flat fill
    "tile_opa":   100,          // 0-100 fill opacity — below 100 lets the wallpaper through
    "border":     "#8fa6c0",    // hairline around tiles
    "border_opa": 22,           // 0-100; 0 removes the border entirely
    "radius":     14,           // corner radius in px

    "accent":     "#4aa3ff",    // pressed tiles, active nav tab, gauge arcs, chart line
    "text":       "#e6edf3",    // labels
    "text_muted": "#8b949e",    // stats captions, the detail line, disabled tile labels
    "ok":         "#3fb950",    // status dot, agent connected
    "idle":       "#6e7681",    // status dot, agent absent

    "dim_opa":    null,         // how dark the idle veil goes, 0-100; null follows the build

    "flip180":    null          // true mounts the deck the other way up; null follows the build
  }
],
"settings": {
  "theme":      "Midnight",     // which one to start on; omit for the first
  "brightness": 80,             // 0-100, applied on reload
  "idle_dim_s": 120,            // dim after this many untouched seconds; 0 to never dim
  "idle_off_s": 600,            // screen off after this many; 0 to never switch off
  "sleep_clock_s": 20,          // show the clock this long after the PC goes; 0 to never
  "dim_pct":    15              // backlight level while dimmed
}
```

**Every field is optional, and every default can be written down.** `null` means "use the
built-in default" for numbers, booleans and colours; `""` means it for `display` and `wallpaper`.
So a theme that wants the stock radius says `"radius": null` rather than dropping the line, and
themes stay the same shape as each other — which is the point of a config file. An absent key
still works and means the same thing; it just tells the next reader nothing.

The shipped `sdcard/deck.json` carries every key in every theme, and `tools/protocol_test.py`
holds it to that by reading the field list out of the firmware's own `parseTheme()`.

**Two fields default from `config.h` rather than from the format, and the shipped themes leave
them `null` on purpose.** `dim_opa` is `0` with the PWM backlight rewire and `55` without it,
because the veil only exists to supply darkness the backlight cannot — and `flip180` follows
`MD_ROTATE_180`. A literal in `deck.json` overrides the build, so writing this deck's `0` into
the file would leave an unmodified board with an idle state that does nothing visible at all.
`null` keeps the key present and the decision where it belongs. Set a real number on a theme
that genuinely wants one — a pale theme needs a heavier veil than a dark one.

Colours are six-digit hex, `#` optional — including `"bg": "#000000"`, which is a real black and
not a parse failure. Anything else is rejected and the default kept.

#### The two idle timers are not the same idea

`idle_dim_s` and `idle_off_s` are **display power**, counted from the last time you touched the
deck. They are unhurried on purpose: the deck gets read as much as it gets pressed, and a panel
that dims while you are still looking at the calendar is answering the wrong question.

`sleep_clock_s` is about **the PC**, not the panel. The clock appears once the link has been gone
that long *and* you have not touched the deck for that long — one number, both halves, because
together they mean "the PC went away and you are not using the deck right now". The touch half is
what lets the ten-key and theme switching stay usable with the agent closed, instead of a clock
dropping on top of them.

These used to be one setting, and that was a mistake: the clock could not appear until the
display-off timer had elapsed untouched, so on a laptop that sleeps and blanks at the same moment
it never appeared at all. If you want the clock sooner or later, change `sleep_clock_s` and leave
the other two alone.

Edit, save, tray reload — the whole screen restyles in place. Nothing needs a reflash.

The old single-object form still works:

```jsonc
"theme": { "bg": "#101418", "tile": "#1b2129", "accent": "#4aa3ff", "text": "#e6edf3" }
```

It is read as a one-theme list. There is no migration step.

### Wallpapers

A theme can put a photograph behind the tiles. Images live on the SD card, so unlike colours
they do not hot-reload — the card has to be written.

**1. Convert.** The deck reads raw RGB565, not PNG or JPEG, so there is no decode step on the
device:

```powershell
python tools/make_assets.py wallpaper "C:\photos\dusk.jpg" --out sdcard/wall/dusk.bin --dim 35
python tools/make_assets.py wallpaper "C:\photos\*.jpg" --out-dir sdcard/wall
```

| Flag | What it does |
|---|---|
| `--dim 0-100` | Darkens the image, baked in. 25–40 usually keeps tile labels readable |
| `--anchor top\|centre\|bottom` | Which part of a too-tall photo survives the crop |
| `--blur N` | Gaussian blur radius, for a calmer backdrop under busy tiles |
| `--width` / `--height` | Defaults to 800×480 |

Images are **cropped to fill**, never letterboxed — black bars read as a fault rather than a
choice. A portrait photo keeps its middle band unless `--anchor` says otherwise.

Each wallpaper is 750 KB, which is nothing against a 32 GB card or 8 MB of PSRAM.

**2. Copy `sdcard/` to the card.** The device reads images from the card directly; the agent
never sends them.

Every conversion also rewrites `sdcard/assets.ver`, a hash of everything on the card. The device
reads it at boot and reports it when it connects, and the agent compares it with what is in the
repo — so if you forget this step, the deck says **"Assets are stale — copy sdcard/ to the card"**
the next time it connects, and the agent log names both hashes.

That check exists because forgetting to copy fails without failing: the old wallpaper is still on
the card, so it loads, and nothing looks broken — you just quietly keep seeing the previous
version. If you delete or rename an asset by hand rather than converting one, restamp:

```powershell
python tools/make_assets.py stamp
```

**3. Point a theme at it and turn the tiles translucent:**

```jsonc
{
  "name": "Dusk",
  "wallpaper": "/wall/dusk.bin",
  "tile": "#0e141b",
  "tile_opa": 68,          // <-- the photo reads through here
  "border": "#ffffff",
  "border_opa": 24,
  "radius": 16,
  "text": "#ffffff"
}
```

`tile_opa` is the whole effect. At 100 the tiles are solid and the wallpaper only shows in the
gutters; around 60–75 gives the glass look with labels still legible. Below about 45 the text
starts fighting the photo.

If the file is missing or malformed the theme falls back to its flat `bg` and says so on the
port-A log — a bad path costs you the photo, never the screen.

### Icons

A tile can carry an icon as well as, or instead of, its label. One `"icon"` field, and the
leading `/` decides how it is resolved:

```jsonc
{ "id": "edit.copy",   "label": "Copy",  "icon": "copy" }            // built-in symbol
{ "id": "launch.code", "label": "Code",  "icon": "/icons/code.bin" } // image on the SD card
```

**Built-in symbols cost nothing** — no card, no conversion, no PSRAM. They live in the fonts that
are already compiled in, and they render as text, so they pick up the theme's `text` colour and
the disabled styling automatically. Use them for verbs.

The name is **the LVGL symbol name, lowercased**. There are no aliases: it is `volume_mid`, not
`volume`. The full list:

```
audio backspace bars battery_1 battery_2 battery_3 battery_empty battery_full bell
bluetooth bullet call charge close copy cut directory down download drive edit eject
envelope eye_close eye_open file gps home image keyboard left list loop minus mute
new_line next ok paste pause play plus power prev refresh right save sd_card settings
shuffle stop tint trash up upload usb video volume_max volume_mid warning wifi
```

A name outside that list is rejected on reload, because the device's fallback — show the label —
is indistinguishable from the `icon` field being ignored.

**Images** are for the things symbols cannot do, mainly brand logos on app launchers:

```powershell
python tools/make_assets.py icon logo.png --out sdcard/icons/code.bin --size 64
```

They are ordinary SD assets, so they need the card written and they are covered by the asset
stamp. 64 px suits `icon_text`; 96 px suits `icon`, which has the whole tile to itself.

### Tile anatomy

`"display"` picks what a tile shows. It resolves **most specific first — button, then theme, then
`settings`** — and every level except `settings` is optional:

```jsonc
"settings": { "display": "icon_text" }                    // the deck-wide default
{ "name": "Kiosk", "display": "icon" }                    // this theme only, optional
{ "name": "Stars", "display": "" }                        // inherit, said out loud
{ "id": "edit.paste_plain", "display": "text" }           // this tile only, optional
```

**A theme can absolutely own its anatomy** — a photo-heavy theme may want icon-only tiles while a
light one wants labels, and setting `display` on the theme does exactly that. What `settings`
provides is a *baseline*, so a theme that does not care can stay silent.

That baseline is the only thing that changed. `display` was per-theme-only at first, with no
baseline, so a theme that omitted it fell back to `text` — which meant adding a theme silently
dropped every icon on the deck. Tiles still rendered, just as labels, so it read as "icons were
never configured" rather than "one field is missing on one theme".

The modes:

| Value | Shows |
|---|---|
| `icon_text` | icon above the label — the general-purpose choice |
| `icon` | icon only, larger. Good for a transport row where the symbol is unambiguous |
| `text` | label only, larger. The behaviour before icons existed |
| `""` | nothing of its own — take the level above. Identical to leaving the key out |

That last row is a repair, not a curiosity. Deleting the line used to be the *only* way to say
"default", which meant two themes in the same file were different shapes and the silent one gave
you nothing to read. `"display": ""` says it out loud, so every theme can carry the same keys —
and it validates, which the empty form previously did not. On `settings` it is legal but inert:
there is no level above, so tiles land on the firmware's own `icon_text`.

`"wallpaper": ""` works the same way, and everything numeric takes `null`.

Anything that cannot produce an icon — no `icon` field, an unknown name, a missing image —
falls back to `text` on that tile alone. So a deck part-way through being iconned looks
unfinished rather than broken, and the port-A log names the tile and the reason.

Worth setting `text` deliberately when the label carries information the icon cannot:
`edit.paste_plain` is "Paste Raw", and a paste icon would make it indistinguishable from
ordinary paste.

### Switching themes

```jsonc
{ "id": "ui.theme", "label": "Theme >",
  "action": { "type": "theme", "target": "next" },      // or "prev", or a theme name
  "hold":   { "type": "theme", "target": "Midnight" } }
```

This runs **on the device**, so it works with the agent closed — same as the ten-key. The choice is
written to `/theme.txt` on the SD card and restored at boot. Editing the layout does not reset it:
if the theme you were on still exists by name, you stay on it.

### The Colours page

```jsonc
{ "id": "colors", "title": "Colours", "type": "colortest" }
```

A bench diagnostic, built in firmware. It exists because judging a theme by switching to it asks
you to hold a colour in memory between screens, and human vision is poor at that — it is good at
*simultaneous* comparison. So this page puts everything side by side:

| Row | Question it answers |
|---|---|
| Greyscale ramp, `#00` to `#FF`, weighted dark | Where is this panel's black floor? If the first few patches are indistinguishable, nothing below that level can separate itself, and dark themes will always look alike |
| Eight hues at level `0x30` | Does hue survive at the luminance a dark theme actually lives at? |
| Every theme's `bg` / `tile` / `accent` / `text`, one row each | Are two themes genuinely different, or do they only look the same? |
| The resolved values as text | What the firmware actually parsed — not what the file says |

That last row matters most in practice: `tile` covers about 87% of a 4×3 page, so two themes with
similar tiles read as "identical" even when their accents are nothing alike.

### What the device tells you

Every load and every theme switch prints the resolved theme to the debug log — **port A / the
CH343P COM port, in the Arduino IDE Serial Monitor**. This is `MD_LOG` (UART0) and is a different
pipe from the agent's `deckhost.log`, which only ever sees what crosses port B:

```
[theme] "Midnight" bg=#0D1117 tile=#1B2129 grad=#141A21 opa=100 border=#8FA6C0/22 radius=14
[theme]   accent=#4AA3FF text=#E6EDF3 muted=#8B949E ok=#3FB950 idle=#6E7681 flip180=1 ...
```

These are the values *after* parsing and defaulting, so "did my edit land, and as what?" is a
question you answer by looking rather than by inferring from the panel.

### Notes from building it

- **`accent` does a lot of work.** Pressed state, active nav tab, stats arcs and the chart line. A
  colour that reads well as a large pressed tile can be too strong as a 14px dot — that is what
  `ok` and `idle` are for, so the dot no longer borrows it.
- **Contrast against `tile`, not against `bg`.** Labels sit on tiles.
- **A disabled tile no longer fades the whole button.** It keeps its fill and moves the label to
  `text_muted`, so make sure `text_muted` is legible on `tile` — otherwise closing the agent makes
  half the deck unreadable rather than merely dimmed.
- **`brightness` and `dim_pct` are real percentages** on a board whose backlight has been rewired
  for PWM, and the panel tracks them linearly from 4% up. On an unmodified board every non-zero
  value is identical, because the backlight is a bare on/off line on the CH422G expander with no
  PWM in the path — see [hardware-notes.md](hardware-notes.md) for the mod.

  It once darkened the screen in software to make itself visible. That was a mistake and was
  reverted: brightness had never had an effect, so making it live meant every existing theme
  went 20% darker on flashing, and on a near-black theme the panel just read as off. **A dead
  setting coming alive should not change how a deck already looks.**
- **`dim_opa` is the idle veil**, and belongs to the theme rather than to settings because a
  pale theme needs a heavier veil than a dark one to read as equally dimmed. It applies only
  after `idle_dim_s`, never at rest. Wake the screen before judging a colour change.

  **Its default depends on the backlight: 0 with the PWM mod, 55 without.** The two multiply, so
  a real 15% backlight behind a 55% veil lands near 7% of full and reads as a dead panel. On a
  rewired board the veil has no dimming left to do, and the overlay stays only to swallow the
  touch that wakes the screen from Off.
- **Judge a colour against a neutral background.** A grey patch surrounded by blue reads as
  yellow-olive — ordinary simultaneous contrast, and a good way to chase a bug that is not there.
  The Colours page exists for this; switch to Paper before trusting what you see on a dark theme.
- **If dark colours look wrong, suspect the firmware before the panel.** Up to 0.4.2 a GPIO
  collision pinned blue's top bit high, so black rendered as mid-blue and no dark theme could
  hold its temperature. That is fixed, but the lesson stands — see
  [hardware-notes.md](hardware-notes.md).

## What a reload does and does not touch

| Reloads live | Needs a reflash |
|---|---|
| Pages, tiles, labels, positions | Font sizes (compiled in, see `theme.cpp`) |
| Actions and macros | The numpad layout (fixed in firmware) |
| Theme colours, opacity, radius, borders | Stats page layout |
| Which theme is active | Anything in `config.h` |
| 180° flip | |
| Brightness, idle timeouts | |

The split is deliberate: data lives in `deck.json`, structure lives in firmware. The ten-key is not
expressible as a JSON grid — its tall `+` and `Enter` keys need spans a plain grid does not
describe — so it is built in code.

## Validation

The agent checks the file before pushing and rejects it on:

- a button with no `id`
- duplicate `id`s
- an action with no `type`
- a `page` action pointing at a page that does not exist
- a `theme` action naming a theme that does not exist
- `settings.theme` naming a theme that does not exist
- a colour, number, `display` or `flip180` of the wrong *type* — six hex digits, a whole number,
  one of the three modes, a boolean

The middle three are the valuable checks. Without them a typo'd target produces a button that
looks perfectly normal and silently does nothing.

The type checks all accept the written form of unset: `null` for numbers, booleans and colours,
`""` for `display`, `icon` and `wallpaper`. That is what lets every object in the file carry the
same keys — see **Themes** above. `tools/protocol_test.py` holds the shipped file to it, reading
the field lists out of the firmware's own parser so a new field cannot be added on one side only.

Validate without touching the deck:

```powershell
python -c "import sys; sys.path.insert(0,'agent'); from deckhost.config import DeckConfig; c=DeckConfig.load(); print('OK', c.rev, len(c.buttons), 'buttons')"
```
