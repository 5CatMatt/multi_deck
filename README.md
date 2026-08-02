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

## Layout

| Path | What |
|---|---|
| `firmware/multi_deck/` | Arduino sketch. Open `multi_deck.ino` in the Arduino IDE. |
| `agent/deckhost/` | Python agent that runs on the PC at logon. |
| `sdcard/` | Version-controlled mirror of what belongs on the SD card. |
| `tools/` | `make_assets.py` (wallpaper/icon converter), protocol conformance harness. |
| `docs/` | Wire protocol spec, and hardware facts learned the hard way. |

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
