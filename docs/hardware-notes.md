# Hardware notes — Waveshare ESP32-S3-Touch-LCD-4.3

Waveshare's documentation for this board is inconsistent between the wiki, the docs site and the
schematic. **Record every empirically-confirmed fact here.** Entries marked ❓ are unverified and
must be settled during Phase 0 bring-up.

## Board summary

| Item | Detail |
|---|---|
| Module | ESP32-S3-WROOM-1-**N8R8** — **8MB flash**, 8MB **octal** PSRAM, 512KB SRAM. Chip rev v0.2 |
| Display | 800x480 IPS, **RGB parallel** interface (not SPI — rules out TFT_eSPI) |
| Touch | GT911, I2C on GPIO8 (SDA) / GPIO9 (SCL), IRQ GPIO4 |
| IO expander | CH422G, same I2C bus |
| SD | SPI on GPIO11 (MOSI) / GPIO12 (SCK) / GPIO13 (MISO), CS via CH422G EXIO4 |
| Card fitted | 32GB, FAT32 |
| Power | USB-C 5V, or 3.7V LiPo on PH2.0 + CS8501 charger. ~450mA @ 5V |

## The two USB ports

This is the single most important fact about the board for this project.

| Port | Silkscreen | Path | Use |
|---|---|---|---|
| A | `USB TO UART` | CH343P bridge → UART0 | Flashing, `Serial` debug log |
| B | `USB` | FSUSB42UMX mux → ESP32-S3 native USB (GPIO19/20) | Composite HID + CDC to the PC |

The FSUSB42UMX routes GPIO19/20 either to port B or to the CAN transceiver, selected by CH422G
**EXIO5**. We always select USB; CAN is unused in this project.

Because the two ports are independent, the firmware can log to `Serial` (UART0, port A) while
simultaneously presenting as a USB keyboard on port B. This is what makes debugging HID behaviour
tractable.

## CH422G EXIO assignments

| Pin | Function | Notes |
|---|---|---|
| EXIO1 | `TP_RST` | Touch panel reset |
| EXIO2 | `DISP` | LCD backlight enable |
| EXIO4 | `SD_CS` | SD card chip select — see gotcha below |
| EXIO5 | `USB_SEL` | Selects USB vs CAN on the FSUSB42UMX — see gotcha below |

EXIO0, EXIO3, EXIO6, EXIO7 are not documented as assigned.

## ⚠️ Open questions for Phase 0

### ✅ EXIO5 polarity — resolved: **LOW selects USB**

The sources disagreed:

- Waveshare docs site: *"EXIO5 — USB_SEL — Pull low to set USB mode, otherwise CAN mode."*
- Waveshare wiki: *"the USB interface [is] used by default when the USB_SEL pin of FSUSB42UMX is set to HIGH."*

**The docs site is right.** With `MD_USB_SEL_USB_LEVEL 0` (drive EXIO5 low), port B enumerates on
the PC as `VID 0x303A PID 0x1001` — the ESP32-S3 native USB. Confirmed by `list_ports`:

```
COM4 | USB-Enhanced-SERIAL CH343 (COM4) | VID:PID 0x1a86 0x55d3   <- port A, UART bridge
COM5 | USB Serial Device (COM5)         | VID:PID 0x303a 0x1001   <- port B, native USB CDC
```

No change needed to `config.h`.

### ✅ SD chip-select handling — resolved: assert EXIO4 once and leave it

`SD_CS` hangs off the I2C expander, not a GPIO. Stock `SD.h` wants to assert and release CS around
every SPI transaction, which here would mean an I2C round-trip per transaction — unusably slow.

**The workaround works.** The card is the only device on that SPI bus, so `board_port::sdBegin()`
drives EXIO4 low once at init and leaves it asserted, passing `MD_SD_CS_PLACEHOLDER` as the nominal
CS purely to satisfy the library signature.

⚠️ **That placeholder is not free.** `SD.begin()` reconfigures whatever pin it is given as a plain
GPIO and drives it — so it must be a pin nothing else uses. It was GPIO10 up to 0.4.2, which is the
panel's blue bit 4, and the resulting colour corruption took an evening to find. It is now GPIO6,
and `config.h` `static_assert`s against `MD_PANEL_PINS`. See the GPIO section below.

Confirmed on hardware (0.4.3, 2026-08-02):

| | |
|---|---|
| Mount | `SD mounted at 20 MHz, 30528 MB` — the 32GB FAT32 card trains at 20 MHz |
| Fallback | `sdBegin()` retries at 4 MHz before giving up, so a slower card still works |
| Throughput | A 750 KB wallpaper read in 583 ms with 8 KB chunks (~1.3 MB/s), against a ~300 ms floor for the bus. Per-call FatFS overhead dominates, not the clock — 0.4.4 reads in 64 KB chunks |

The device parses `/deck.json` from the card at boot and reports its `rev` in the `hello` frame.
The built-in fallback layout reports `rev 0`, so the revision number is a reliable one-glance check
of whether the card was actually read — worth knowing, because **the layout also arrives over USB**,
so a deck with an unmounted card looks completely healthy until something asks for a file.

### ✅ Board macro name — resolved: `BOARD_WAVESHARE_ESP32_S3_TOUCH_LCD_4_3`

Enabled in **`firmware/multi_deck/esp_panel_board_supported_conf.h`**, a project-local file rather
than an edit to the library.

The library resolves the config header in this order:

```c
#if   __has_include("esp_panel_board_supported_conf.h")           // sketch folder — wins
#elif __has_include("../../../esp_panel_board_supported_conf.h")  // library root
```

Arduino puts the sketch directory on the include path, so the local copy takes precedence. Editing
the library's copy instead would work until the next library update silently reverted it.

The file must also carry a version stamp matching `src/esp_panel_versions.h`
(`ESP_PANEL_BOARD_SUPPORTED_*` = 1.2.0 for library v1.0.4), and those defines belong **inside** the
`#if ESP_PANEL_BOARD_DEFAULT_USE_SUPPORTED` block.

Symptom when it is missing entirely — the board boots but `init()` refuses:

```
[E][Panel][esp_panel_board.cpp:0055](init):
No default board configuration detected.
```

## ✅ Flash is 8MB, not 16MB — the spec page is wrong

**This board is an N8R8.** Waveshare's product page and docs site describe the
ESP32-S3-Touch-LCD-4.3 as carrying an ESP32-S3-WROOM-1-**N16R8** with 16MB of flash. The
hardware in hand does not. Espressif's ESP32_Display_Panel board documentation lists 8MB and
recommends the `8M with spiffs` partition scheme, and **it is the one that is correct**.

Verified directly from the chip:

```
$ esptool --chip esp32s3 --port COM4 flash-id
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)
Manufacturer: c8
Device: 4017
Detected flash size: 8MB
Flash type set in eFuse: quad (4 data lines)
```

### The symptom, so it is recognisable next time

Selecting 16MB in the IDE writes a binary whose image header claims 16MB. The second-stage
bootloader checks that against the real chip, fails, and panics **before any application code
runs** — including `Serial.begin()`, so the sketch appears completely dead:

```
E (171) spi_flash: Detected size(8192k) smaller than the size in the binary image
                   header(16384k). Probe failed.
assert failed: __esp_system_init_fn_init_flash startup_funcs.c:118
Rebooting...
```

The board then boot-loops forever. On a display board the only visible symptom is **a black
screen**, which looks exactly like a graphics bug and is not one. If the panel is dark, read
the UART log on port A before touching any display code.

After correcting the flash size, tick **Erase All Flash Before Sketch Upload** once — the
partition table has moved, and stale core-dump/NVS regions from the bad layout otherwise
produce their own spurious errors.

## ⚠️ Never log to `Serial` on this board — use `MD_LOG` (UART0)

`Serial` is not a fixed destination on the ESP32-S3. The core defines it as UART0 **only** when
*USB CDC On Boot* is disabled; with that setting enabled it becomes a USB CDC object instead
(`cores/esp32/HardwareSerial.h`, around line 451).

That matters here because this firmware enumerates **its own** `USBCDC` instance for the agent
link. With CDC-on-boot enabled there are then two CDC objects, and `Serial` is the one nothing on
the PC ever reads:

1. Every debug line written to `Serial` disappears — the device looks mute.
2. Worse, that unread CDC's TX buffer fills, and **`USBCDC::write()` blocks until its timeout**.
   The device then stalls inside what looks like a harmless `printf`, intermittently, depending on
   buffer state. Symptom: the first agent connection hangs, the second works.

So all firmware logging goes through **`MD_LOG`** (defined as `Serial0` in `config.h`), which is
UART0 in both configurations. Do not reintroduce bare `Serial.print` calls.

### Diagnosing this

The tell is that vendor `[I][Panel]` lines appear on port A while none of our `[main]`/`[board]`/
`[link]` lines do. Arduino's `log_i()` macros use `ets_printf` straight to UART0 and bypass
`Serial` completely, so they show up regardless — which makes the firmware look alive while our
own logging is silently going elsewhere.

Belt and braces: keep *USB CDC On Boot* **Disabled** in the IDE as the settings table says. `MD_LOG`
makes logging correct either way, but a stray second CDC interface is not worth having.

## ⚠️ The CDC DTR flag misses reconnects — don't gate logic on it

`USBCDC::operator bool()` (what `UsbLink::isAttached()` returns) reflects the CDC DTR line state.
On this board it **does not reliably go true on every reconnect**: with an agent restarted
repeatedly, alternate connections left it false while the host was sending frames perfectly well.

That caused a genuinely confusing failure, because reads were never gated and writes were:

1. The host connects and sends `identify`.
2. The device receives it — reception works fine — and builds the `hello` reply.
3. `rawWrite` checked `isAttached()`, saw false, and **discarded the reply**.
4. The host waits forever for a `hello` that was written and thrown away.

One-directional deafness looks like the *other* end is at fault, which sent the investigation the
wrong way for a while. Symptom at the desk: every other agent launch does nothing.

**Rule:** inbound traffic is the authority on whether a host is present; the DTR flag is only ever
corroborating evidence. Writes are never gated on it. Blocking is prevented instead by
`setTxTimeoutMs(MD_CDC_TX_TIMEOUT_MS)` **and nothing else** — see the next section, which is the
same mistake made one layer down, and which kept this symptom alive after this fix landed.

## ⚠️ The CDC TX FIFO is 64 bytes — never gate a write on `availableForWrite()`

`USBCDC::availableForWrite()` returns free space in the TinyUSB CDC transmit FIFO, whose size is
`CFG_TUD_CDC_TX_BUFSIZE` = **64 bytes**. It can never report more.

So this, which `UsbLink::rawWrite()` did until 0.4.6, is not a flow-control check:

```cpp
if (g_cdc.availableForWrite() < static_cast<int>(len)) return false;   // WRONG
```

It is a **hard 64-byte ceiling on every frame the device can ever send**, written in a form that
looks transient. Frames under 64 bytes go out; frames over it are dropped, always, with no error
anywhere on the wire.

### Why it survived a whole earlier investigation

`hello` was **exactly 64 bytes** at 0.4.4:

```
{"t":"hello","proto":1,"fw":"0.4.4","dev":"multi_deck","rev":7}\n
```

`64 < 64` is false, so it passed — *but only when the FIFO was completely empty*. Any residue and
the handshake silently failed. That is the true cause of the "every other launch does nothing"
intermittency documented in the section above. Removing the `isAttached()` gate fixed one layer
and left this one, so the symptom got rarer and looked solved.

Adding one field to `hello` in 0.4.5 took it to 76 bytes and the deck stopped handshaking
entirely — which is what finally made it findable.

### The fix

Delete the check. `USBCDC::write()` already loops internally: it writes what fits, calls
`tud_cdc_n_write_flush()`, and repeats until done or until `tx_timeout_ms` expires (read it in
`Arduino15/packages/esp32/hardware/esp32/<ver>/cores/esp32/USBCDC.cpp`). Blocking stays bounded by
`setTxTimeoutMs()` without capping frame size — which is what the check was actually for.

**General rule:** a "is there room?" guard against a fixed-size buffer *is* a size limit on the
payload. If the payload can grow, the guard has to chunk, not refuse.

### Diagnosing this

The port-A log says it directly:

```
[link] identify received but hello could not be sent
```

Inbound works, outbound does not. **One-directional failure is always the local write path**, never
the host — the host cannot cause your write to fail.

`[link] partial write: N of M bytes` covers the remaining case. Note that a **zero**-byte write is
deliberately *not* logged: `USBCDC::write()` returns 0 when the port is closed, which is normal for
a second or two after a reset while the device announces itself before the host reopens COM. Only
a partial write indicates a desynchronised stream.

## ✅ Benign boot warning: GT911 "Unable to initialize the I2C address"

```
[W][Panel][esp_lcd_touch_gt911.c:0144]: Unable to initialize the I2C address
```

**Expected on this board. Ignore it.**

The GT911 answers on one of two I2C addresses (0x5D or 0x14), chosen by the level on its INT pin
while RST is released. The driver only performs that selection when **both** INT and RST are real
GPIOs:

```c
if (gt911_config && rst_gpio_num != GPIO_NUM_NC && int_gpio_num != GPIO_NUM_NC) {
    /* toggle RST/INT to select the address */
} else {
    ESP_LOGW(TAG, "Unable to initialize the I2C address");
    touch_gt911_reset(...);   /* falls back, and works */
}
```

Here TP_RST hangs off CH422G **EXIO1**, not a GPIO, so `rst_gpio_num` is `GPIO_NUM_NC` and that
branch is unreachable. The driver falls back and talks to the chip at its default address
successfully — the proof is the next two log lines:

```
TouchPad_ID:0x39,0x31,0x31        <- ASCII "911"
TouchPad_Config_Version:67
```

Silencing it would mean teaching the vendor driver to drive a reset line through an I2C expander.
Not worth it for a warning that costs nothing.

## Telling which firmware is on the device

`MD_FW_VERSION` in `config.h` rides along in every `hello`, and the agent prints it on connect:

```
deckhost: device connected: fw 0.2.0, layout rev 1
```

Bump it with any behavioural change. "Did I flash that fix?" should be answered by reading a log
line, not by memory.

## ✅ Confirmed vendor API (ESP32_Display_Panel v1.x, read from the installed headers)

The docs were wrong or silent on several of these; these signatures came from the headers and are
what `board_port.cpp` is written against.

| Call | Signature | Notes |
|---|---|---|
| `Board::getLCD()` | `drivers::LCD *` | |
| `Board::getTouch()` | `drivers::Touch *` | |
| `Board::getBacklight()` | `drivers::Backlight *` | |
| `Board::getIO_Expander()` | `drivers::IO_Expander *` | `Board::getExpander()` is deprecated; use `getIO_Expander()->getBase()` |
| `Backlight::setBrightness` | `bool setBrightness(int percent)` | |
| `LCD::drawBitmap` | `bool drawBitmap(int x, int y, int w, int h, const uint8_t *data, int timeout_ms = 0)` | |
| `Touch::readRawData` | `bool readRawData(int points_num, int max_buttons_num, int timeout_ms)` | Returns `false` on timeout — the ordinary "not touching" case. Pass `-1, -1, 0` for a non-blocking poll |
| `Touch::getPoints` | `int getPoints(TouchPoint points[], uint8_t num)` | Returns count, or -1 on failure. **There is no `getPointsNum()`.** Vector overloads also exist |
| `TouchPoint` | fields `x`, `y`, `strength` (all `int`) | |
| `esp_expander::Base` | `bool pinMode(uint8_t, uint8_t)`, `bool digitalWrite(uint8_t, uint8_t)` | Reached via `getIO_Expander()->getBase()` |

### ❓ RGB bounce buffer size

Start without one; add and tune only if tearing appears. Record the value that works.

Result: _(unverified — fill in during bring-up)_

## The flicker constraint

The RGB panel's DMA streams the framebuffer out of PSRAM continuously. Flash and PSRAM share SPI1,
so **any flash write stalls the CPU** — it can no longer fetch instructions — which starves the panel
refill and shows up as a visible glitch or tear.

The ESP-IDF fix is `CONFIG_SPIRAM_XIP_FROM_PSRAM`, which relocates `.text`/`.rodata` into PSRAM.
**It is not reachable from the Arduino IDE**, because the core ships precompiled. So we design around
it instead:

1. All mutable data (layout, icons, logs) lives on the **SD card**, which is on a different SPI bus
   and does not stall the CPU. This is the real reason the project prefers SD over LittleFS.
2. Configure RGB **bounce buffers** if needed (runtime field, set between `init()` and `begin()`).
3. Keep NVS writes rare, and blank the UI around them if they must happen.
4. Load icons at page-build time, never during an animation.

## Arduino IDE Tools settings

These matter and are easy to get wrong.

| Setting | Value | Why |
|---|---|---|
| Board | `ESP32S3 Dev Module` | |
| PSRAM | **OPI PSRAM** | The R8 suffix means 8MB octal PSRAM; wrong setting = no PSRAM at all |
| Flash Size | **8MB (64Mb)** | ⚠️ Confirmed by esptool, see below. Setting 16MB here bricks boot |
| Flash Mode | QIO 80MHz | |
| Partition Scheme | **8M with spiffs (3MB APP/1.5MB SPIFFS)** | LVGL + the panel driver are not small, so the APP region needs to be generous. SPIFFS goes unused — assets live on SD |
| **USB Mode** | **`USB-OTG (TinyUSB)`** | **Required for HID.** `Hardware CDC and JTAG` cannot do HID |
| **USB CDC On Boot** | **Disabled** | Keeps `Serial` on UART0 → debug log on port A, while `USBSerial` is the agent link on port B |
| Upload Mode | UART0 / Hardware CDC | Upload over port A |

If the `USB CDC On Boot` setting is ever changed, enable *Erase All Flash Before Sketch Upload* once,
or the board will not print serial logs correctly afterwards.

### Library versions

| Library | Version |
|---|---|
| arduino-esp32 core | >= 3.1.0 |
| ESP32_Display_Panel | >= 1.0.4 |
| ESP32_IO_Expander | >= 1.0.0, < 2.0.0 |
| esp-lib-utils | >= 0.2.0, < 0.3.0 |
| lvgl | 9.x |
| ArduinoJson | 7.x |

## ⚠️ Never claim a GPIO the RGB panel uses — GPIO10 is blue bit 4

**The single most expensive bug in this project so far.** Fixed in firmware 0.4.2.

`MD_SD_CS_PLACEHOLDER` was GPIO10, handed to `SD.begin()` as a nominal chip select because
SD_CS actually lives on the IO expander (EXIO4) and the API insists on a pin number. **GPIO10 is
the RGB bus's blue bit 4.** `SD.begin()` reconfigures its chip select as an ordinary output and
drives it high, which pulled GPIO10 out of the LCD peripheral's pin matrix and pinned it to 1.

Blue could then never fall below 50%. The symptoms:

| Test | Observed | Because |
|---|---|---|
| `#000000` | A dark blue, never black | Blue forced to 16/31 |
| `#000080` | Indistinguishable from `#000000` | Bit 4 *is* the 0x80 bit, so both are 16/31 |
| `#404040` | Reads blue | Blue 8 → 24 |
| `#808080` | Correct neutral grey | Blue is already 16; forcing bit 4 changes nothing |
| Full primaries | All correct | Saturated colours have blue at 0 or 31; only 0 shifts, and red still reads red |

### Why it took so long to find

Everything visible pointed elsewhere. The picture was sharp, touch was accurate, the SD card
mounted and served `deck.json` without complaint. Two dark themes looking alike was blamed first
on a thin palette, then on backlight bleed, then on the panel's contrast ratio — all plausible,
all wrong. The one clue that actually mattered was `#000080` looking identical to `#000000`,
which no analog effect can produce: a bleed adds light, it does not make two different signals
equal. That is a stuck bit, and a stuck bit at the 0x80 position is bit 4.

**Rule the firmware out before blaming the panel.** The `colortest` page's full-saturation row was
correct throughout, which was read as "the pipeline is fine" — but a stuck *low* bit is invisible
at full saturation. A single-channel bit ladder would have found this in one look.

### The guard

`config.h` lists every panel GPIO in `MD_PANEL_PINS` and `static_assert`s that the SD pins avoid
them. Setting the placeholder back to 10 now fails the build with an explanatory message
(verified). The list is mirrored from the vendor profile, which keeps those macros in a private
header; they describe physical wiring, so they do not drift.

**Anything that claims a GPIO must go through that check.** Free pins on this board: GPIO 6, 15,
16. Avoid flash (26-32) and octal PSRAM (33-37) as well as everything in `MD_PANEL_PINS`.

## ✅ The colour pipeline is correct

Confirmed with the `colortest` page. All eight full-saturation primaries render as themselves,
so `MD_RGB565_SWAP 0` is right, the channel order is right, and no channel is lost or swapped.

### A perceptual trap worth knowing

A neutral grey patch surrounded by a strongly coloured field takes on the opponent hue —
`#808080` on a blue background reads yellow-olive. Ordinary simultaneous contrast, not a panel
fault. Judge a suspicious colour against a *neutral* background before believing it, and be aware
that mid-tone colour naming by eye is unreliable enough to send you chasing a bug that is not
there. Photographs are worse: a phone camera white-balancing in a dark room rendered the same
`#FFFFFF` patch as white under one theme and light blue under another.

## ⚠️ LVGL configuration — an untracked build input

`lv_conf.h` must sit in the Arduino `libraries/` root, *beside* the `lvgl` folder, not inside it:

```
Documents/Arduino/libraries/lv_conf.h     <- the real one, outside this repo
Documents/Arduino/libraries/lvgl/
```

Nothing in this repository can pin it. Reinstalling or updating LVGL silently reverts it, and the
firmware then behaves differently with no visible cause. `firmware/lv_conf.reference.h` is a tracked
copy — **diff against it first** when something renders wrong that used to render right.

An early version of this file was ignored entirely because it carried an `#if 0` guard; the only
symptom was one easily-missed `#pragma message`. If a setting below appears not to take effect,
check that guard before anything else.

Settings this project depends on:

| Setting | Value | Why |
|---|---|---|
| `LV_COLOR_DEPTH` | `16` | RGB565, matching the panel |
| `LV_FONT_MONTSERRAT_20 / _28 / _40` | `1` | The three UI sizes. LVGL's built-in Montserrat also carries the FontAwesome symbol range, so `LV_SYMBOL_*` icons need no extra font |
| `LV_USE_ARC`, `LV_USE_CHART`, `LV_USE_BAR` | `1` | Stats page |
| `LV_DRAW_SW_COMPLEX` | `1` | Rounded-corner masking |

And the ones that constrain what the UI can do:

| Setting | Value | Consequence |
|---|---|---|
| `LV_MEM_SIZE` | `48 KB`, `LV_MEM_POOL_EXPAND_SIZE 0` | A fixed pool with no growth. **Widget scale/rotate transforms are unusable**: LVGL renders a transformed object to a temporary layer allocated from here, and a 190×130 tile needs ~49 KB — more than the entire pool. Press feedback uses `translate_y`, which needs no layer. |
| `LV_USE_MATRIX` | `0` | Same conclusion, from the other direction |
| `LV_USE_DRAW_SW_COMPLEX_GRADIENTS` | `0` | No radial or conical gradients. Two-stop linear (`bg_grad_dir = LV_GRAD_DIR_VER`) is unaffected — that flag only gates the complex kinds |
| `LV_CACHE_DEF_SIZE` | `0` | Image cache off. Fine for RAM-resident RGB565 descriptors, which decode to a pointer. First knob to turn if wallpaper drawing stutters |
| `LV_USE_FS_ARDUINO_SD` | `0` | LVGL cannot open SD paths itself; `assets.cpp` reads files and hands over a descriptor |

## Fonts — what is built in, and how to add one

Fonts are compiled into flash as `lv_font_t` C arrays. There is no runtime font loading: LVGL
cannot read TTF, OTF or VLW on this build, so every face has to be converted first.

**Built in already:** Montserrat 14 / 20 / 28 / 40, enabled in `lv_conf.h`. These carry the
FontAwesome symbol range, which is where every `LV_SYMBOL_*` icon comes from — see the icon
section of [editing-the-deck.md](editing-the-deck.md). **Adding a Montserrat size means editing
the out-of-repo `lv_conf.h`** and is not something `tools/` can do for you.

**Custom faces:** [tools/make_font.py](../tools/make_font.py) converts a TTF or OTF.

```powershell
python tools/make_font.py C:/Windows/Fonts/GOTHIC.TTF --name century --sizes 20 28 40
```

It writes `firmware/multi_deck/font_<name>_<size>.c`, one per size. Declare them in
[fonts.h](../firmware/multi_deck/fonts.h) and expose them through `theme.h` alongside the
Montserrat pointers. Century Gothic at 20/28/40 costs **43 KB of flash** for all three.

### Why not lv_font_conv

The official converter is a Node package, and there is no Node on this machine. `make_font.py`
uses Pillow, which `tools/make_assets.py` already depends on, so a font can be regenerated
without adding a second toolchain. What it gives up: **kerning** (lv_font_conv emits kern
classes; this emits none — sub-pixel at these sizes, and a wrong kern table is worse than none)
and **compression** (`--no-compress` equivalent, which is what we want anyway).

### The fallback chain is not optional

`lv_font_t.fallback` is resolved recursively by LVGL 9, and every generated font is baked with
`.fallback = &lv_font_montserrat_<size>`.

This matters because a generated font covers U+0020–U+007E only — no symbols. The stats detail
line puts `LV_SYMBOL_UP` / `LV_SYMBOL_DOWN` **inside the same label as its text**, so without the
fallback that line renders with holes in it. The fallback also covers any character outside
ASCII.

For the same reason a custom font is attached to **individual styles**, never to
`theme::screen`: a screen-wide text font would push it onto the symbol labels that tiles use for
their icons, and every icon on the deck would vanish.

### The trap: Pillow's `getbbox()` is not the ink box

It returns the *layout* box. For `"` it puts the bottom edge on the baseline when the ink stops
less than halfway down, and it keeps the side bearings. Feeding that to LVGL yields `ofs_y = 0`
for every glyph — quotes and apostrophes sit at the wrong height, and each glyph carries blank
padding rows.

`make_font.py` renders each glyph and takes `Image.getbbox()` of the result instead, which is
tight, and which is what FreeType hands lv_font_conv. Tightening the boxes alone cut the 28 px
bitmap from 14,074 to 11,357 bytes. **If glyph positioning ever looks wrong, check this first.**

### VLW — converting TFT_eSPI fonts

`make_font.py` also reads Processing/TFT_eSPI smooth fonts, either as raw `.vlw` or as the
PROGMEM hex dump inside a TFT_eSPI `.h`:

```powershell
python tools/make_font.py fonts/Nord-Medium-28.vlw --name nord
```

Both formats store 8bpp antialiased bitmaps, so this is a transcode rather than a re-render — no
quality is lost, but the size is fixed by the file and `--sizes` does not apply. Worth having
when the original outline is gone and the baked bitmaps are all that survive. **If you do have
the TTF, prefer it** — it converts to any size and could carry kerning.

Layout, for reference:

| offset | size | field |
|---|---|---|
| 0 | 6 × `int32` BE | glyph count, version (11), size, unused, ascent, descent |
| 24 | count × 7 × `int32` BE | code, height, width, advance, topExtent, leftExtent, pad |
| … | Σ(w×h) | 8-bit alpha bitmaps, in glyph order |
| end | ~27 bytes | Processing's trailing name metadata — ignore it, it is not corruption |

Two gotchas:

- **The header's `descent` is not trustworthy.** Every file to hand reports 0, which would put
  the baseline on the floor of the line and clip every descender. `make_font.py` derives both
  metrics from the glyph table instead — `ascent = max(topExtent)`,
  `descent = max(height - topExtent)`.
- **A VLW rarely covers all of ASCII.** Nord Medium is missing space, `<`, `>`, `^`, `` ` `` and
  `~`. The emitter therefore uses a **sparse** cmap for gapped fonts rather than padding the gaps
  with blank glyphs — a blank glyph would make the lookup *succeed* and draw nothing, which stops
  `fallback` from ever being consulted. Sparse keeps missing characters missing, so they reach
  Montserrat.

### Unused fonts are free

The linker runs with `--gc-sections`, so a generated font that nothing references is dropped
entirely: adding the four Nord sizes to the sketch changed the binary by **0 bytes**. Fonts can
sit declared in `fonts.h` until a style actually points at one.

## Why ESP32_Display_Panel but not its LVGL port

ESP32_Display_Panel v1.0.4 ships only an LVGL **v8** port; v9 support is still an open enhancement
([issue #63](https://github.com/esp-arduino-libs/ESP32_Display_Panel/issues/63), open since June
2024).

So we use the library purely as the **driver / board-profile layer** — it hands us correct RGB
timings, the GT911 and the CH422G for free — and supply our own LVGL 9 glue in
`firmware/multi_deck/lvgl_v9_port.cpp`. Their `lvgl_v8_port.cpp` is the reference to translate from.

All calls into the vendor library are funnelled through `board_port.cpp`, which has a deliberately
tiny surface (init, flush, read touch, backlight). If the vendor API shifts, that one file changes.
