# multi_deck

A touchscreen deck companion for Windows, built on a Waveshare ESP32-S3-Touch-LCD-4.3.

Touch tiles launch apps, fire macros, act as the ten-key this laptop doesn't have, and show live
system stats. The layout lives in a JSON file on the SD card, so adding a button is a text edit —
not a reflash.

## How it fits together

```
        ESP32-S3 deck                                    Windows PC
 +------------------------------+              +---------------------------+
 | LVGL 9 UI  <- deck.json (SD) |              |  deckhost (Python, tray)  |
 |      |                       |              |      |                    |
 |      +- local actions -------+-- USB HID -->|  (no software needed)     |
 |      |  hid / media / page   |   port B     |                           |
 |      |                       |              |      +- launch apps       |
 |      +- remote actions ------+-- USB CDC -->|      +- AutoHotkey        |
 |         launch / ahk / shell |   port B     |      +- shell             |
 |                              |              |                           |
 |  stats page  <---------------+---- CDC -----+  psutil / pynvml / LHM    |
 +------------------------------+              +---------------------------+
      port A (CH343P UART) -> flashing + Serial debug log, always available
```

The device resolves `hid`, `media` and `page` actions **by itself**. The numpad and media keys keep
working with the PC agent closed — they are real HID keycodes, so they also reach elevated windows,
games and the BIOS. Everything else is forwarded to the agent as an event.

## Using the deck

### The nav bar

A 56 px strip across the top, one tab per page, with a **status dot** at the right end:

| Dot | Meaning |
|---|---|
| Green | The PC agent is connected. Everything works. |
| Grey | No agent — it is closed, the cable is out, or the PC is asleep. |

Grey is not a fault. The deck is designed to be useful on its own; see *Working without the PC*
below.

### Tap and hold

**Tap** runs the tile's action. **Hold** — about 0.4 s — runs its second action, if it has one.
Not every tile does; the theme switcher is the shipped example, where tap cycles to the next
theme and hold jumps straight back to a named one.

On the ten-key, holding a digit **repeats** it, the way a real keyboard does.

### The pages

Three kinds, set by `"type"` in the layout:

- **Grid** — the general case. Rows and columns of tiles, each running one action or a sequence
  of them: launch an app, fire a keyboard shortcut, run an AutoHotkey helper, type a block of
  text, switch pages, or any chain of those.
- **Ten-key** — a full numeric keypad. These are **real HID keycodes**, not synthesised input, so
  they reach elevated windows, games and even the BIOS — places software-generated keystrokes
  cannot go. This is the one page that works with nothing installed on the PC at all.
- **Stats** — CPU, memory and GPU gauges with a 60-second CPU history, plus temperatures, memory
  used and network throughput. Updates once a second while the page is open, and only while it is
  open.

### Working without the PC

When the agent is not running, tiles that need it **dim but stay visible**, and tapping one says
so rather than doing nothing. Everything the device can do alone keeps working:

| Works standalone | Needs the agent |
|---|---|
| Ten-key, media keys, any keyboard shortcut | Launching apps and URLs |
| Page navigation, theme switching | AutoHotkey helpers, shell commands |
| Typing stored text | The stats page |

So the deck is still a numpad and a macro keyboard on a machine that has never seen the agent —
plug it into a different PC and the HID half just works.

### Themes

Tap the theme tile to cycle; hold to jump to a specific one. The choice is written to the SD card
and **survives a power cycle**, and editing the layout does not reset it — if the theme you were
on still exists by name, you stay on it.

Themes carry colours, corner radius, tile translucency, an optional background photo, and whether
tiles show an icon, a label, or both.

### Screen and messages

The backlight **dims to 10% after 60 seconds** of no touch and comes straight back when you touch
it. It does not switch off entirely.

Transient messages — layout reloaded, agent not connected, a wallpaper that would not load —
appear at the bottom of the screen for about 2.5 seconds.

### Changing what is on it

Edit `sdcard/deck.json`, then **Reload deck.json** from the tray icon. The deck rebuilds
immediately; no reflash, no restart. A layout with an error is rejected and the running one kept,
so a typo cannot leave you with a blank screen.

Colours, tiles and actions travel over USB and apply instantly. **Images are the exception** —
wallpapers and icons live on the SD card and have to be copied there. The deck notices when the
card is out of date and tells you on connect.

Full reference: [docs/editing-the-deck.md](docs/editing-the-deck.md).

## Layout

| Path | What |
|---|---|
| `firmware/multi_deck/` | Arduino sketch. Open `multi_deck.ino` in the Arduino IDE. |
| `agent/deckhost/` | Python agent that runs on the PC at logon. |
| `sdcard/` | Version-controlled mirror of what belongs on the SD card. |
| `tools/` | `make_assets.py` (wallpaper/icon converter), protocol conformance harness. |
| `docs/` | Wire protocol spec, hardware facts learned the hard way, and the backlog. |

**Where the project stopped and what is left** — [docs/beta-complete.md](docs/beta-complete.md).
Development is paused at firmware `0.5.4` to use the deck and gather UI/UX feedback; that file
holds the remaining backlog, the rejected ideas and why, and a log to record observations in.

**Changing what the deck shows** — edit `sdcard/deck.json` and hit *Reload deck.json* in the tray.
See [docs/editing-the-deck.md](docs/editing-the-deck.md).

## Getting started

**Firmware** — see [docs/hardware-notes.md](docs/hardware-notes.md) for the exact Arduino IDE Tools
settings. They are fiddly and getting them wrong produces confusing failures.

**Agent** — install it once as an editable package so it runs from any directory:

```powershell
pip install -e agent
```

Then, from anywhere:

```powershell
python -m deckhost --simulate   # fake device, no hardware needed
python -m deckhost              # talk to the real deck, autodetected by USB VID
```

`--simulate` runs against an in-process fake deck, so action dispatch and the stats pipeline can be
exercised with nothing plugged in. Note it implies `--dry-run` — actions are logged, not performed.

Without the editable install, `python -m deckhost` only works from inside `agent/`.

The install also creates a `deckhost` command, but Python's `Scripts` directory is often missing
from `PATH` on Windows; `python -m deckhost` avoids that entirely.

Optional extras, each of which degrades to "field absent" rather than failing:

```powershell
pip install -e "agent[all]"     # GPU stats, tray icon, AutoHotkey backend
```

**SD card** — copy the contents of `sdcard/` to the root of a FAT32 card. Wallpapers and icons
live here; convert them first with `tools/make_assets.py`. Colours and layout arrive over USB and
need no card write.

## Running it at logon

```powershell
.\agent\install_autostart.ps1
```

Registers a Scheduled Task that starts the agent at logon under `pythonw.exe` (no console
window) with a tray icon. It runs **with highest privileges**, which matters: input the agent
synthesises cannot reach elevated windows otherwise. Keystrokes the *device* sends as HID are
unaffected either way — those are real keycodes and always get through.

The script resolves the interpreter by asking Python where it lives rather than trusting `PATH`,
because `pythonw.exe` on Windows often resolves to the Microsoft Store alias — a different
installation that does not have `deckhost` on its path. It also verifies the package is
importable before wiring anything to logon.

| | |
|---|---|
| Start now | `Start-ScheduledTask -TaskName "multi_deck agent"` |
| Stop | Quit from the tray, or `Stop-ScheduledTask -TaskName "multi_deck agent"` |
| Remove | `.\agent\install_autostart.ps1 -Remove` |
| Log | `%LOCALAPPDATA%\multi_deck\deckhost.log` |

The tray menu shows connection status and offers **Reload deck.json** — edit the layout and reload
without restarting anything. A layout that fails validation is rejected and the running one kept, so
a typo cannot leave you with a blank deck.

There is no console under `pythonw`, so the log file and the tray icon are how you see what it is
doing. The log is UTF-8; PowerShell 5.1's `Get-Content` renders that as mojibake, so use
`Get-Content -Encoding UTF8` or open it in an editor.

## Compile check without the IDE

The Arduino IDE bundles `arduino-cli`, which will build the sketch headlessly — useful for
checking a change compiles without clicking through the IDE, and for catching errors before
flashing:

```powershell
& "C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" `
    compile --fqbn "esp32:esp32:esp32s3:PSRAM=opi,FlashSize=8M,PartitionScheme=default_8MB,USBMode=default,CDCOnBoot=default" `
    firmware\multi_deck
```

Those FQBN options are the same settings listed in [docs/hardware-notes.md](docs/hardware-notes.md):
`FlashSize=8M` is *8MB (64Mb)*, `PartitionScheme=default_8MB` is *8M with spiffs (3MB APP/1.5MB
SPIFFS)*, `USBMode=default` is *USB-OTG (TinyUSB)* (`build.usb_mode=0` — the mode HID requires), and
`CDCOnBoot=default` is *Disabled*, which keeps `Serial` on UART0.

Current footprint: ~865KB program (25% of the 3MB app partition), ~106KB static RAM.

Linker warnings about `missing .note.GNU-stack section` are toolchain noise and can be ignored.

## Development workflow

Two USB cables during development, and this is intentional rather than a workaround:

- **Port A** (marked `USB TO UART`, behind the CH343P bridge) — flashing and the `Serial` debug log.
- **Port B** (the plain `USB` port, native ESP32-S3 USB) — the composite HID + CDC device the PC sees.

Because they are separate, the device can be debugged over the serial log *while* it is acting as a
keyboard. In daily use only port B is plugged in.
