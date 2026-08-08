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
| EXIO1 | `TP_RST` | Touch panel reset — via R22 (100R) |
| EXIO2 | `DISP` | Panel display enable **and** backlight — via R21 (100R). See the backlight section; this one net does two jobs. |
| EXIO3 | `LCD_RST` | **R20 is unpopulated from the factory** — this line is open and reaches nothing. Confirmed on the board, 2026-08-08. |
| EXIO4 | `SD_CS` | SD card chip select — see gotcha below |
| EXIO5 | `USB_SEL` | Selects USB vs CAN on the FSUSB42UMX — see gotcha below |

EXIO0, EXIO6, EXIO7 are not documented as assigned.

Each EXIO leaves the expander through its own 100R series resistor (R18–R22), which is what makes
the backlight mod a resistor removal rather than a trace cut. **R20 being a no-fit matters when
diagnosing a blank panel:** `LCD_RST` cannot be the cause, because the expander was never
connected to it.

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

## ✅ Port B can flash the board too — and it is the more reliable one

If a flash dies partway with **`A fatal error occurred: No more data to read from the serial
port`**, do not keep retrying over port A. Flash over **port B** instead.

Observed 2026-08-02: the CH343 on port A dropped off the USB bus partway through every 1.1MB app
write — at 28%, then 36%, then 28% again — at 921600, 460800 and 115200 baud alike, while the
small bootloader and partition images (a few KB each) wrote fine every time. Lowering the baud
rate does not help, because the connection is not corrupting data, it is disappearing.

**When the app is not running, port B enumerates as the ESP32-S3's built-in USB-Serial-JTAG** — a
separate COM port, plus a "USB JTAG/serial debug unit" device. That is native USB with no bridge
chip in the path, so there is simply less to fail. It wrote the whole app and verified in **7
seconds, first attempt**, having failed five times over port A.

**[tools/flash.py](../tools/flash.py) does all of this in one command** — build, download mode,
flash, reset, tail the log, with the agent stopped and restarted around it:

```powershell
python tools/flash.py
```

The rest of this section is what it does and why, for when it needs changing or the board does
something new.

It is a three-step procedure, because the JTAG port only exists while the app is stopped and
`esptool` cannot reset the board through it. **Port A drives the reset; port B carries the data.**

**1. Drop the chip into download mode from port A.** RTS drives `EN`, DTR drives `IO0`:

```python
s = serial.Serial('COM4', 115200, timeout=0.2)
s.setDTR(False); s.setRTS(True); time.sleep(0.15)   # hold in reset, IO0 high
s.setDTR(True)                                      # IO0 low = download mode
s.setRTS(False); time.sleep(0.15)                   # release reset
s.setDTR(False); s.close()
```

A new COM port appears within a couple of seconds — that is the JTAG unit on port B.

**2. Write over it with `--before no-reset`**, since the board is already where it needs to be:

```powershell
& "$env:LOCALAPPDATA\Arduino15\packages\esp32\tools\esptool_py\5.3.1\esptool.exe" `
    --chip esp32s3 -p COM5 --before no-reset --after hard-reset `
    write-flash --flash-mode dio --flash-freq 80m --flash-size 8MB `
    0x10000 "$env:LOCALAPPDATA\arduino\sketches\<hash>\multi_deck.ino.bin"
```

**3. Reset from port A again**, with `IO0` left high this time — the same snippet without the
`setDTR(True)` line. `--after hard-reset` toggles RTS on the *JTAG* port, which is not wired to
`EN`, so the board stays in the ROM and looks like a failed flash when it is nothing of the kind.
That step is the one that is easy to miss.

Two further things to know:

- **Its COM number is not port B's usual one.** The JTAG unit and the running app's TinyUSB
  composite are different USB devices, so they get different ports — COM5 and COM6 here. The
  agent copes (it re-enumerates every reconnect and logs `deck moved from COM5 to COM6`), but do
  not go looking for the deck's normal port while the app is stopped.
- **`arduino-cli upload` cannot use this path**, since it drives one port for both jobs. Compile
  with `arduino-cli compile`, then flash the `.bin` out of its build directory by hand.

### Corollary: esptool 5.x does diff-based writes, which a corrupt flash defeats

esptool 5.3.1 compares flash against the image and rewrites only changed sectors ("fast
reflashing", visible as many small scattered writes rather than one large one). That is normally a
speed win, but after an interrupted upload it can leave a **corrupt image looking partly
up-to-date**, and the board then boot-loops on

```
Assert failed in verify_load_addresses, esp_image_format.c:437 (load_end > load_addr)
```

`erase-flash` first, then write, when the previous upload did not finish cleanly. Nothing in
this project keeps state in flash — the layout, the theme and the assets all live on the SD
card — so a full erase costs nothing but the flashing time.

## Diagnosing "the deck is dead after a flash"

Both halves of this looked like a bad firmware build and neither was. Work through it in this
order — it takes two minutes and skips an hour of reading diffs.

**1. Is the firmware running?** Reset over port A and read the log. `esptool` is not needed:

```python
s = serial.Serial('COM4', 115200, timeout=0.2)
s.setDTR(False); s.setRTS(True); time.sleep(0.2); s.setRTS(False)   # EN follows RTS
```

A healthy boot ends with `[main] ready`. If you see that, **the firmware is fine** and both a
black screen and a dead link are something else. A crash or boot loop looks completely different:
truncated output, or the banner repeating.

**2. Is the panel actually off, or just dark?** `[ui] idle=... backlight=... veil=...` is printed
on every idle transition for exactly this reason. `backlight=80 veil=0` with a black screen means
the panel is lit and the *content* is dark — check the theme before the hardware. A near-black
theme under a dark wallpaper is indistinguishable from a dead panel across a room.

**3. Is the host seeing the device at all?**

```powershell
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like "*VID_303A*" }
```

**Zero rows is a physical-layer answer, not a firmware one.** The firmware will still happily log
`[link] USB CDC started` and `[hid] keyboard + consumer control started`, because those run before
anything checks whether a host is there — TinyUSB cannot attach without VBUS on port B, and it
does not complain about its absence. Zero rows means the cable is out, is power-only, or has
failed. A device that is present but failing enumeration shows up instead as a present
*Unknown USB Device (Device Descriptor Request Failed)*, which is a different problem entirely.

Drop `-PresentOnly` and you get every device the machine has ever seen, which is misleading —
ghost entries all read `Status: Unknown` and will happily show you a COM port that has not existed
for months.

## ✅ The backlight is on/off only — until it is rewired (done, 2026-08-08)

**Completed on this board.** `MD_BACKLIGHT_PWM_GPIO 16`, EN measured at 1.26 V for 80% with DISP
holding a steady 3.3 V, and the panel tracks the percentage smoothly. Everything below is the
record of how, including two reworks that did not succeed and why.

**Swept 4–100% in 26 steps, both directions, and every default held:**

| Result | Confirms |
|---|---|
| Response is **linear** across the whole range | The EN-window mapping is right. A raw duty map would have given a dead zone below 21% and a cliff between 21 and 42. |
| **Still legible at 4%** (EN 728 mV) | `MD_BACKLIGHT_EN_MIN_MV 700` sits just above the converter's real cutoff, and `MD_BACKLIGHT_MIN_PCT 4` is a floor that still produces light. |
| **100 clearly brighter than 80** | `MD_BACKLIGHT_EN_MAX_MV 1400` is not past saturation; the top of the scale is doing work. |
| **No flicker at any level** | 20 kHz is far enough above the 159 Hz corner that no ripple survives the filter. |

So the datasheet numbers survived contact with the board unmodified — unusual enough to be worth
saying. The one thing the sweep suggests for later: the sleep clock shares `dim_pct` with the idle
dim, and 4% being visible means a night clock could sit well below a dim level that still has to
be usable. Splitting those is a future change, not a fix.

The short version for anyone repeating it: **remove R10**, wire the GPIO through a 1k to R10's
EN-side pad, and leave R21 and R4 alone. Do not try to cut the DISP net — see below.


`settings.brightness` did nothing for the project's first five months, and the README described a
dim-to-10% behaviour that could not happen. Not a bug in our code: the board profile
(`BOARD_WAVESHARE_ESP32_S3_TOUCH_LCD_4_3.h:245`) selects

```c
#define ESP_PANEL_BOARD_BACKLIGHT_TYPE  (ESP_PANEL_BACKLIGHT_TYPE_SWITCH_EXPANDER)
#define ESP_PANEL_BOARD_BACKLIGHT_IO    (2)     // CH422G EXIO2
```

and that driver's brightness control is, in full:

```c
int level = (percent > 0) ? _config.on_level : !_config.on_level;   // switch_expander.cpp:95
```

The backlight enable hangs off the CH422G I2C expander, which has no PWM. **Every non-zero
percentage is identical**; only `setBacklight(0)` does anything.

**This is a wiring limitation, not a permanent one, and the firmware side is already written.**

### ✅ What the schematic actually says (settled 2026-08-08)

Three facts, and all three changed the plan:

**1. There is nothing to cut.** Every EXIO line leaves the CH422G through a 100R series
resistor — R18/R19 on IO4/IO5, R20 on IO3, **R21 on IO2**, R22 on IO1. Removing R21 isolates the
expander from the DISP net completely. No scalpel, no lifted pad, and it solders straight back.

**2. DISP is not an on/off line — it is an analogue dimming input.** It goes through R10 (1k) to
the **EN pin of an MP3302 LED boost converter**, with C12 (1uF) to ground. That RC has a corner
at about **159 Hz**, so EN never sees a square wave; it sees the DC average. The datasheet:

> Apply a 200Hz to 1kHz square waveform to the EN pin to implement PWM dimming […] For high
> frequency PWM dimming (>1kHz), it is also recommended that the dimming control be implemented
> as shown in Figure 3 […] The DC voltage on EN pin is then equal to the PWM high level voltage
> multiplied by the PWM duty. The DC voltage from **0.7V to 1.4V** programs the output current
> from 0~100%.

Figure 3's filter is already fitted. This board was built for high-frequency dimming.

**3. EXIO2 also drives nothing else.** The pin table lists it as `DISP` alone.

There is also **R4, a 10K pull-up to 3V3 on the DISP net**, which is a useful safety property: a
floating GPIO leaves the backlight *on*, not dark.

### ⚠️ DISP is two signals on one net — this is the trap

**`DISP` is not a backlight enable. It is also `PORT1` pin 31, the LCD panel's own display
enable.** The net fans out three ways:

```
CH422G IO2 ─R21─┬─ LCD PORT1 pin 31      panel display enable
                ├─ R4 10K to 3V3          pull-up
                └─ R10 1k ─┬─ C12 ─ GND   the RC filter
                           └─ MP3302 EN   backlight brightness
```

Drive PWM onto that net and you dim the backlight *and* chop the panel's display enable at the
PWM rate. At a 21–42% duty the panel is told "off" most of the time and blanks completely —
while the backlight, which averages through R10/C12, dims perfectly.

The symptom is unmistakable once you know it: **black screen, correct dimming, working touch,
tiles that still fire when tapped.** The firmware is fine and the boot log says so; LVGL is
rendering into a panel that is not displaying.

So the PWM must go on the **backlight branch only**, after the split — not on the shared net.

### Doing the rewire

1. **Leave R21 and R4 in place.** The CH422G keeps driving `DISP` and R4 keeps pulling it up,
   which together hold the *panel* enabled. `board_port::begin()` drives EXIO2 high whenever
   `MD_BACKLIGHT_PWM_GPIO` is defined, so that line is load-bearing rather than incidental.
2. **Remove R10 completely.** It is the only component bridging the DISP node and the EN node, so
   taking it off isolates them unconditionally — whatever the copper does underneath.
3. **Solder the GPIO to R10's EN-side pad through a 1k** (R10 itself will do). The path is then
   `GPIO ─ 1k ─ C12 ─ MP3302 EN`, with the datasheet's filter intact. Never connect the GPIO to
   that node directly: it would drive 1µF with no series resistance and hand EN a raw square wave.

**Do not try to cut the DISP net.** Two attempts failed here. The trace to LCD pin 31 runs through
a via under a passive and is not visible, so a cut that looks complete leaves the nodes joined —
and the obvious continuity check (GPIO-to-R10) passes while the one that matters still shorts. The
tell is measuring DISP and EN and getting the *same* number; they must differ. Removing a
component is verifiable by inspection, which is why step 2 is a removal.
4. **Uncomment one line** in `config.h`:
   ```c
   #define MD_BACKLIGHT_PWM_GPIO 16
   ```
5. Flash. That is the entire software change — `board_port::setBacklight()` already has the LEDC
   branch, and everything above it passes percentages rather than booleans, so nothing else knew
   the number was being discarded and nothing else needs telling that it no longer is.

Afterwards the two signals are properly separate: the panel's display enable is a static high
from the expander, and the backlight is a control voltage from the GPIO. `setBacklight(0)` takes
EN below threshold and stops the converter without disabling the panel.

**Do not inject onto the shared DISP net** — that is the mistake described above, and it produces
a black screen that dims convincingly. If R21 has already been removed, put it back; a net held
up only by R4's 10K works, but nothing is then actively driving the panel's enable.

The pin choice is guarded at compile time, verified by building against each bad case:

| Pin | Result |
|---|---|
| 10 | `MD_BACKLIGHT_PWM_GPIO is an RGB panel pin…` |
| 6 | `MD_BACKLIGHT_PWM_GPIO collides with MD_SD_CS_PLACEHOLDER` |
| 30 | `GPIO26-32 are flash and GPIO33-37 are octal PSRAM…` |

Defining the macro before doing the mod is harmless — `begin()` still drives EXIO2 high, so the
panel sits at full brightness and the PWM goes to an unconnected pin. It looks like the mod
failing rather than like the mod not having happened, which is worth knowing before you go
hunting.

### ⚠️ Free pins are scarcer than they look

The vendor board profile only lists what *it* drives, and the panel/USB pin table only covers the
panel and USB. Both leave GPIO15 and GPIO16 blank. **They are the RS485 transceiver**, and that
omission nearly put the backlight on one of them.

The honest accounting for this board:

| GPIO | Status |
|---|---|
| **6** | **Sensor header J6 pin 3 (`AD`).** Otherwise idle. |
| **15, 16** | **RS485 transceiver.** Blank in the pin table; not free. |
| 33–37 | **Octal PSRAM** on an N8R8 with `PSRAM=opi`. Costs the LVGL draw buffers — i.e. the display. |
| 26–32 | Flash. |
| 11, 12, 13 | SD SPI (ours). |
| 43, 44 | UART0 — the `MD_LOG` debug channel on port A. How this board gets debugged at all. |
| 8, 9 | I2C: touch and the CH422G. |
| 19, 20 | Native USB, port B. |
| 4 | `CTP_IRQ`, an output from the GT911. |

**There is no free pin — only a choice of what to give up. Take GPIO16.**

The RS485 sheet is the thing that settles it, and the two RS485 pins are not equivalent:

| Pin | Net | What it actually touches | Verdict |
|---|---|---|---|
| **16** | `RS485_RXD` | R68 (4.7k) into S1's base; R67 (4.7k) pull-up. **Nothing drives it.** | ✅ **no cut** |
| 15 | `RS485_TXD` | The SP3485's **`RO` output**. R64 is a pull-up, not a series resistor. | ❌ needs a cut |

`DI` is tied to GND and `DE`/`RE̅` are driven from S1's collector, so the ESP transmits by
modulating the driver enable — which means **GPIO16 is already an output in normal use**, and
repurposing it as PWM is doing what it already did. The load is ~0.7mA fighting R67 plus ~0.55mA
into the base. Tap it at the R67/R68 pad on the IO16 side.

The only side effect is that the SP3485's `DE` now follows the PWM, so the transceiver enables and
disables at 20kHz with `DI` low. With nothing plugged into J1/J2 that drives only the 10k bias
network — microamps, and the part is rated for 10 Mbps. Removing R68 would isolate it completely,
but it is not worth the extra rework: with R68 gone the base floats, R66 pulls `DE` permanently
high, and the driver sits enabled instead. Chattering into nothing is the quieter of the two.

**GPIO6 remains the fallback** if RS485 is wanted and the sensor header is not. Physically it is
the easiest tap of all — a through-hole header pin rather than a chip pad — but it costs the
sensor header *and* a software change: `MD_SD_CS_PLACEHOLDER` sits on GPIO6, and while the card
ignores that pin (its real chip select is EXIO4, asserted once and left asserted), `SPI.begin()`
and `SD.begin()` still configure it as an output and toggle it per transaction, which would fight
the PWM. The `static_assert` refuses the collision if this is forgotten.

The DISP end of the jumper is identical whichever pin is chosen: R21 off, wire from its net-side
pad.

### ⚠️ Duty is a control voltage, not a brightness

This is the part that would have looked like a broken mod. With a 3.3 V swing, the datasheet's
0.7–1.4 V window is only **21% to 42% duty**. Mapping `brightness` linearly onto 0–100% duty —
which is what the firmware did before the schematic was read — gives:

| `brightness` | Result |
|---|---|
| below ~21 | dark, converter under threshold |
| 21 – 42 | the *entire* usable range, as a cliff |
| above 42 | saturated, no further change |

So `setBacklight()` maps the percentage into the voltage window instead, via
`MD_BACKLIGHT_EN_MIN_MV` (700) and `MD_BACKLIGHT_EN_MAX_MV` (1400). They are in millivolts
because that is the form the datasheet states and the form a multimeter on the EN side of R10
reports — **if a sweep saturates early or never quite reaches full, trim those two rather than
the mapping code.** Each level logs its own arithmetic:

```
[board] backlight 40% via LEDC on GPIO15 (EN 980 mV, duty 303/1023)
```

**`MD_BACKLIGHT_PWM_HZ` is now 20000.** The datasheet wants the filter corner ten times below the
PWM frequency; R10/C12 put that corner at ~159 Hz, so the floor is about 1.6 kHz. 20 kHz clears it
comfortably. The earlier 1000 Hz default was a hedge against DISP feeding a bare enable pin that
had to restart the converter each cycle — it does not, and 1 kHz would sit only ~6× above the
corner and let visible ripple through.

Note that there is **no audible-whine risk from the PWM itself here**: the converter runs
continuously on a DC control voltage rather than being chopped at the dimming rate. Still sweep
and listen, but the expected failure is ripple, not singing.

`MD_BACKLIGHT_MIN_PCT` (default 4) floors any non-zero request so a low setting reads as dim
rather than as a dead panel. Zero is honoured exactly, and is the one case that drops duty to a
true 0% so EN falls below threshold and the converter shuts down rather than idling at minimum.

**So do not write code that assumes brightness is binary.** The UI carries two knobs that
multiply: the real backlight, always called with a real percentage, and a full-screen
translucent black overlay that supplies darkness below the backlight's floor. The overlay is
what makes the sleep clock readable at night and stays useful after the rewire; the backlight
call is what starts working the day the wire moves. Neither is a workaround for the other.

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

## ✅ This laptop blanks the display and sleeps in the same second — on battery

Read the power scheme before theorising about what the deck sees when the PC goes away:

```powershell
powercfg /q SCHEME_CURRENT SUB_SLEEP  | Select-String "Current AC|Current DC"
powercfg /q SCHEME_CURRENT SUB_VIDEO VIDEOIDLE | Select-String "Current AC|Current DC"
```

|              | Display off | Sleep    |
|--------------|-------------|----------|
| **On AC**    | 60 min      | 120 min  |
| **On battery** | 3 min     | **3 min** |

Both battery values are `0xb4` — 180 seconds. That single fact explains a behaviour that looked
like a firmware bug for a whole release:

- `GUID_CONSOLE_DISPLAY_STATE` going to 0 is a **display** signal, not a sleep signal. On AC it
  fires a full hour before the machine sleeps, with the PC wide awake and the link healthy, and
  the `power` frame delivers perfectly.
- On battery the two coincide, so by the time the agent is notified the USB bus is already
  suspended. The agent log shows the deck's last inbound frame landing *one second before* the
  display-off notification. The frame is written into a dead port every time.

The agent log also shows this signal firing on ordinary screen blanks that end 9–16 seconds later
— it is genuinely "the screen went off", nothing more. Do not treat it as proof of sleep.

Also worth knowing: **`HIBERNATEIDLE` is 0 on both AC and DC**, so idle hibernation is off and is
not what cuts power to the deck. If the deck turns out to be dark rather than showing its clock
after a long sleep, suspect the port being powered down on battery, not hibernation.

The lesson, repeating one from the CH343 saga: two `powercfg` queries would have settled in
thirty seconds what an afternoon of reading firmware diffs did not.

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

### Large faces need `--chars`

Bitmap cost grows with the **square** of the size, so the full ASCII range that is fine at 28 px
becomes absurd at 96: about **180 KB of flash** for glyphs you will never draw. The sleep clock
needs eleven of them.

```powershell
python tools/make_font.py C:/Windows/Fonts/GOTHIC.TTF --name centuryclock --sizes 96 `
    --chars "0123456789:" --fallback lv_font_montserrat_40
```

That is ~20 KB instead. `0`–`:` is U+0030–U+003A, contiguous, so it still takes the cheap
`FORMAT0_TINY` cmap rather than the sparse one. Name a subset face for its job rather than its
family — `centuryclock`, not `century` — because `font_century_96.c` would look like a
general-purpose size and be reached for as one.

Give a subset an explicit `--fallback` that exists: a label that unexpectedly contains a letter
then renders small rather than not at all.

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
