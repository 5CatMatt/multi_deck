// Board constants and tunables for multi_deck.
// Hardware facts here are cross-referenced in docs/hardware-notes.md. Anything marked UNVERIFIED
// must be settled during Phase 0 bring-up, and the note updated when it is.
#pragma once

#include <stdint.h>

// Bump this with any behavioural firmware change. It rides along in the `hello` frame and the
// agent logs it on connect, so "which build is actually on the device?" is answered by looking
// rather than by remembering.
#define MD_FW_VERSION    "0.5.4"
#define MD_DEVICE_NAME   "multi_deck"
#define MD_PROTO_VERSION 1

// ---------------------------------------------------------------------------
// Display
// ---------------------------------------------------------------------------
static constexpr uint16_t MD_SCREEN_W = 800;
static constexpr uint16_t MD_SCREEN_H = 480;

// Height in lines of each LVGL draw buffer. Two are allocated, both in PSRAM.
// 80 lines = 800 * 80 * 2 = 128000 bytes each. Trivial against 8MB of PSRAM.
static constexpr uint16_t MD_DRAW_BUF_LINES = 80;

// RGB parallel panels normally need no byte swap, unlike SPI panels. If the first
// render comes out with inverted-looking colours, set this to 1.
#define MD_RGB565_SWAP 0

// Default 180 degree rotation, for mounting the board the other way up. A theme may override
// it with "flip180"; this is what applies when none does.
//
// Implemented by mirroring both panel axes rather than with lv_display_set_rotation(): on an
// RGB panel the mirror happens inside the driver's framebuffer copy, so it costs nothing per
// frame, whereas LVGL's software rotation would transform every rendered fragment on the CPU.
// The touch controller is mirrored to match, or taps would land at the opposite corner.
//
// Only 0 and 180 are offered. 90/270 would mean a genuine transpose — RGB panels cannot swap
// axes in hardware, and the panel is physically 800x480 — so it would cost a strided PSRAM
// copy of every fragment plus a second layout. See docs/hardware-notes.md.
#define MD_ROTATE_180 1

// ---------------------------------------------------------------------------
// GPIOs the RGB panel owns — do not claim any of these for anything else
// ---------------------------------------------------------------------------
//
// The LCD peripheral drives these continuously through the pin matrix. Reconfiguring one as an
// ordinary GPIO silently deletes a colour bit and pins it to whatever level was written — the
// picture keeps working, so nothing points at the cause.
//
// This bit us badly. GPIO10 is **blue bit 4**, and it was passed to SD.begin() as a placeholder
// chip select right through to 0.4.2. SD.begin() drove it high, so blue could never fall below
// 50%: black rendered as a mid-blue and #000080 was indistinguishable from #000000, because
// bit 4 *is* the 0x80 bit. The hunt went through the theme system, the panel's contrast ratio
// and LVGL's colour format before reaching the SD card. See docs/hardware-notes.md.
//
// Mirrored from the vendor board profile, which keeps these in a private header:
//   ESP32_Display_Panel/src/board/supported/waveshare/BOARD_WAVESHARE_ESP32_S3_TOUCH_LCD_4_3.h
// They describe physical board wiring, so they do not drift.
static constexpr int MD_PANEL_PINS[] = {
    14, 38, 18, 17, 10, 39, 0, 45,  // RGB data 0-7   (B0-B4, G0-G2)
    48, 47, 21, 1,  2,  42, 41, 40, // RGB data 8-15  (G3-G5, R0-R4)
    46, 3,  5,  7,                  // HSYNC, VSYNC, DE, PCLK
};

constexpr bool md_uses_panel_pin(int pin) {
  for (int panel_pin : MD_PANEL_PINS) {
    if (pin == panel_pin) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// CH422G IO expander
// ---------------------------------------------------------------------------
static constexpr uint8_t MD_EXIO_TP_RST  = 1;  // touch panel reset
static constexpr uint8_t MD_EXIO_DISP    = 2;  // backlight enable
static constexpr uint8_t MD_EXIO_SD_CS   = 4;  // SD chip select
static constexpr uint8_t MD_EXIO_USB_SEL = 5;  // FSUSB42UMX: native USB vs CAN

// UNVERIFIED — the Waveshare docs site and wiki disagree on this polarity.
// The docs site says "pull low to set USB mode"; the wiki says USB is selected when
// USB_SEL is high. Try 0, check Device Manager for a new device on port B, flip if
// nothing appears, then record the answer in docs/hardware-notes.md.
#define MD_USB_SEL_USB_LEVEL 0

// ---------------------------------------------------------------------------
// SD card (SPI, separate bus from flash — see the flicker note in hardware-notes.md)
// ---------------------------------------------------------------------------
static constexpr int MD_SD_MOSI = 11;
static constexpr int MD_SD_SCK  = 12;
static constexpr int MD_SD_MISO = 13;

// SD_CS lives on the IO expander, so there is no GPIO to hand the SD library. The card is the
// only device on this bus, so EXIO4 is asserted once at init and left asserted, and this pin
// number is passed purely to satisfy an API that insists on one.
//
// ⚠️ It must be a pin the panel does not use. This was GPIO10 until 0.4.2 — which is the RGB
// bus's **blue bit 4**. SD.begin() reconfigures its chip select as a plain output and drives
// it high, which tore B4 out of the LCD peripheral's pin matrix and pinned it to 1. Blue could
// then never fall below 50%: black rendered as mid-blue, and #000080 was indistinguishable
// from #000000 because bit 4 *is* the 0x80 bit. See docs/hardware-notes.md.
//
// GPIO6 is claimed by nothing in the board profile — not the panel, touch, expander or
// backlight — and is neither a flash pin (26-32) nor an octal PSRAM pin (33-37).
static constexpr int MD_SD_CS_PLACEHOLDER = 6;

static_assert(!md_uses_panel_pin(MD_SD_CS_PLACEHOLDER),
              "MD_SD_CS_PLACEHOLDER is an RGB panel pin. SD.begin() reconfigures its chip "
              "select as a plain GPIO, which would delete a colour bit. Pick another pin.");
static_assert(!md_uses_panel_pin(MD_SD_MOSI), "MD_SD_MOSI is an RGB panel pin");
static_assert(!md_uses_panel_pin(MD_SD_SCK), "MD_SD_SCK is an RGB panel pin");
static_assert(!md_uses_panel_pin(MD_SD_MISO), "MD_SD_MISO is an RGB panel pin");

#define MD_DECK_JSON_PATH "/deck.json"
#define MD_ICON_DIR       "/icons"

// Which generation of images the card holds, written by tools/make_assets.py and echoed back
// in `hello` so the agent can compare it with the repo's. The device never computes it — it
// only carries it.
//
// This exists because images are the one thing the link cannot keep in sync. Layout and colours
// arrive over USB and are never stale; wallpapers and icons are copied to the card by hand, and
// a card that was never rewritten fails silently — the old picture is still there, so nothing
// errors and nothing looks wrong.
#define MD_ASSET_STAMP_PATH "/assets.ver"

// Longer than this is not a stamp, it is whatever else got written to that filename. Treated as
// absent rather than reported, so a stray file cannot become a permanent phantom mismatch.
static constexpr size_t MD_ASSET_STAMP_MAX = 32;

// Which theme is currently selected, one name per line. Kept on SD rather than in NVS on
// purpose: an NVS write stalls the CPU on the shared SPI1 bus and tears the RGB panel, while
// the SD card is on its own bus. See docs/hardware-notes.md.
#define MD_THEME_STATE_PATH "/theme.txt"

// SD SPI clock. The library defaults to 4 MHz, which is fine for a 6 KB deck.json and slow
// for the 750 KB wallpapers that arrive in S2.
static constexpr uint32_t MD_SD_FREQ_HZ = 20000000;

// ---------------------------------------------------------------------------
// Link (see docs/protocol.md)
// ---------------------------------------------------------------------------
static constexpr uint32_t MD_LINK_HELLO_INTERVAL_MS = 2000;
static constexpr uint32_t MD_LINK_TIMEOUT_MS        = 5000;
static constexpr size_t   MD_LINK_RX_MAX            = 8192;  // longest line we will accept

// How recently we must have received something to consider a host present even when the CDC
// DTR flag says otherwise. Comfortably longer than the host's 2s identify/ping cadence, and
// well short of MD_LINK_TIMEOUT_MS.
static constexpr uint32_t MD_LINK_HEARD_RECENTLY_MS = 3000;

// Ignore an `identify` arriving this soon after a hello already went out, so the host's
// connect-time probe and our own announcement timer do not produce two handshakes.
static constexpr uint32_t MD_LINK_HELLO_DEDUPE_MS = 300;

// Cap on how long a CDC write may block. Without this a write to a host that is not draining
// the port stalls the whole UI loop, since everything runs on one thread.
//
// This is the *only* thing that should bound a write. The CDC TX FIFO is 64 bytes, and gating
// on whether a whole frame fits in it — which link_usb.cpp used to do — is a frame-size limit
// wearing a flow-control disguise. USBCDC::write() chunks and flushes against this timeout by
// itself. See the comment in UsbLink::rawWrite().
static constexpr uint32_t MD_CDC_TX_TIMEOUT_MS = 20;

// ---------------------------------------------------------------------------
// Interaction
// ---------------------------------------------------------------------------
static constexpr uint32_t MD_LONG_PRESS_MS       = 600;
static constexpr uint32_t MD_KEY_REPEAT_DELAY_MS = 400;  // hold before repeat starts
static constexpr uint32_t MD_KEY_REPEAT_RATE_MS  = 60;   // interval once repeating

// ---------------------------------------------------------------------------
// Debug logging
// ---------------------------------------------------------------------------
//
// Always UART0 (port A), never `Serial`.
//
// `Serial` is not a fixed destination on the ESP32-S3: the core defines it as UART0 only
// when "USB CDC On Boot" is disabled, and as a USB CDC object when it is enabled. With that
// setting on, `Serial` becomes a CDC instance separate from the one this firmware
// enumerates for the agent link — so every log line is silently discarded and the device
// appears mute, which is a genuinely awful thing to debug.
//
// `Serial0` is UART0 in both configurations, so logging is deterministic and survives
// someone toggling an IDE menu.
#define MD_LOG Serial0

static constexpr uint32_t MD_DEBUG_BAUD = 115200;
