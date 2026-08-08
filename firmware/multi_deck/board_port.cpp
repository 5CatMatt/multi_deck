#include "board_port.h"

#include <Arduino.h>
#include <SD.h>
#include <SPI.h>
#include <esp_display_panel.hpp>

#include "config.h"

using namespace esp_panel::board;
using namespace esp_panel::drivers;

namespace board_port {
namespace {


Board *g_board = nullptr;
LCD *g_lcd = nullptr;
Touch *g_touch = nullptr;

SPIClass g_sd_spi(HSPI);

bool g_rotated = false;
bool g_sd_mounted = false;

// Drives one CH422G pin. The expander is reached through the board object rather than a
// separate I2C client so we do not fight the vendor library over bus ownership.
void expanderWrite(uint8_t exio, uint8_t level) {
  auto *expander = g_board->getIO_Expander();
  if (expander == nullptr) {
    MD_LOG.println("[board] no IO expander — cannot drive EXIO");
    return;
  }
  expander->getBase()->pinMode(exio, OUTPUT);
  expander->getBase()->digitalWrite(exio, level);
}

}  // namespace

bool begin() {
  g_board = new Board();

  if (!g_board->init()) {
    MD_LOG.println("[board] init() failed");
    return false;
  }

  // Bus tuning belongs between init() and begin(). If tearing shows up under load, a bounce
  // buffer is the first thing to reach for — see the flicker note in docs/hardware-notes.md.
  // Left off deliberately: it costs CPU, and it may simply not be needed here.

  if (!g_board->begin()) {
    MD_LOG.println("[board] begin() failed");
    return false;
  }

  g_lcd = g_board->getLCD();
  g_touch = g_board->getTouch();

  if (g_lcd == nullptr) {
    MD_LOG.println("[board] no LCD from board profile");
    return false;
  }
  if (g_touch == nullptr) {
    MD_LOG.println("[board] no touch from board profile — check the GT911 on I2C");
  }

  // Must come after begin(). Themes may change this later; this is the boot default.
  setRotation180(MD_ROTATE_180);

  // Route GPIO19/20 to port B rather than the CAN transceiver, so the PC can enumerate us.
  // Polarity is UNVERIFIED — see MD_USB_SEL_USB_LEVEL in config.h.
  expanderWrite(MD_EXIO_USB_SEL, MD_USB_SEL_USB_LEVEL);

#ifdef MD_BACKLIGHT_PWM_GPIO
  // Before ui::begin(), which sets the backlight as soon as the idle state is applied. A
  // ledcWrite() to an unattached pin is silently ignored, so getting this order wrong would look
  // like the mod not working rather than like a missing init.
  if (!ledcAttach(MD_BACKLIGHT_PWM_GPIO, MD_BACKLIGHT_PWM_HZ, MD_BACKLIGHT_PWM_BITS)) {
    MD_LOG.printf("[board] LEDC would not attach to GPIO%d — backlight will not respond\n",
                  MD_BACKLIGHT_PWM_GPIO);
  } else {
    MD_LOG.printf("[board] backlight on GPIO%d, %d Hz, %d-bit\n", MD_BACKLIGHT_PWM_GPIO,
                  MD_BACKLIGHT_PWM_HZ, MD_BACKLIGHT_PWM_BITS);
  }

  // The expander line is no longer wired to anything after the mod, but drive it high anyway:
  // if the trace was left intact and the GPIO merely paralleled onto it, a low here would fight
  // the PWM and hold the panel dark.
  expanderWrite(MD_EXIO_DISP, HIGH);
#endif

  MD_LOG.printf("[board] up: %ux%u\n", MD_SCREEN_W, MD_SCREEN_H);
  return true;
}

void setRotation180(bool on) {
  if (g_lcd == nullptr) return;

  // Called from ui::rebuild(), so it runs on every page change. The panel default is
  // unmirrored, which is what g_rotated starts as, so this early-out is correct from boot.
  if (on == g_rotated) return;
  g_rotated = on;

  // Mirroring both axes gives a 180 degree rotation. These call through to
  // esp_lcd_panel_mirror(), which folds the transform into the framebuffer copy the driver
  // already performs — so unlike LVGL's software rotation it costs nothing per frame.
  //
  // The touch controller is mirrored to match. Forgetting that half is a memorable bug: the
  // picture is the right way up and every tap lands at the diagonally opposite tile.
  g_lcd->mirrorX(on);
  g_lcd->mirrorY(on);

  if (g_touch != nullptr) {
    g_touch->mirrorX(on);
    g_touch->mirrorY(on);
  }

  MD_LOG.printf("[board] rotation %s\n", on ? "180" : "0");
}

void flush(int32_t x1, int32_t y1, int32_t x2, int32_t y2, const void *pixels) {
  if (g_lcd == nullptr) return;

  // On an RGB parallel panel this is a memcpy into the framebuffer the DMA is already
  // scanning out — there is no transaction to wait on, so the caller may signal completion
  // immediately. That would not hold for an SPI panel.
  g_lcd->drawBitmap(x1, y1, (x2 - x1 + 1), (y2 - y1 + 1),
                    static_cast<const uint8_t *>(pixels));
}

bool readTouch(int16_t &x, int16_t &y) {
  if (g_touch == nullptr) return false;

  // Non-blocking poll: LVGL calls this from its own read timer, so it must never wait.
  // A false return here is the ordinary "nothing is touching the panel" case, not an error.
  if (!g_touch->readRawData(-1, -1, 0)) return false;

  TouchPoint points[1];
  const int count = g_touch->getPoints(points, 1);
  if (count <= 0) return false;

  x = static_cast<int16_t>(points[0].x);
  y = static_cast<int16_t>(points[0].y);
  return true;
}

void setBacklight(uint8_t percent) {
  if (percent > 100) percent = 100;

#ifdef MD_BACKLIGHT_PWM_GPIO
  // The rewired path: percentages mean percentages.
  //
  // This is the whole of the PWM change. Everything above passes a percentage rather than a
  // boolean — the idle machine, the theme, the `backlight` frame — so none of it knew or cared
  // that the number was being thrown away, and none of it needed touching when it stopped being.
  //
  // Zero is honoured exactly; anything else is floored, so a low setting reads as dim rather
  // than as a dead panel that sends you looking for a fault.
  const uint8_t level = (percent == 0) ? 0
                                       : (percent < MD_BACKLIGHT_MIN_PCT ? MD_BACKLIGHT_MIN_PCT
                                                                         : percent);
  constexpr uint32_t kMaxDuty = (1u << MD_BACKLIGHT_PWM_BITS) - 1;
  ledcWrite(MD_BACKLIGHT_PWM_GPIO, static_cast<uint32_t>(level) * kMaxDuty / 100);
  MD_LOG.printf("[board] backlight %u%% via LEDC on GPIO%d\n", level, MD_BACKLIGHT_PWM_GPIO);
  return;
#else
  auto *backlight = g_board->getBacklight();
  if (backlight != nullptr) {
    const bool ok = backlight->setBrightness(percent);
    MD_LOG.printf("[board] backlight %u%% via driver%s\n", percent, ok ? "" : " FAILED");
    return;
  }

  MD_LOG.printf("[board] backlight %u%% via EXIO%u\n", percent, MD_EXIO_DISP);
#endif

  // Fall back to the raw enable line if the profile exposes no PWM backlight.
  expanderWrite(MD_EXIO_DISP, percent > 0 ? HIGH : LOW);
}

bool sdBegin() {
  // SD_CS hangs off the I2C expander, so it cannot be toggled per SPI transaction at any
  // useful speed. The card is the only device on this bus, so assert chip select once and
  // leave it asserted; the pin handed to SD.begin() is a placeholder the library never uses
  // meaningfully. See docs/hardware-notes.md.
  expanderWrite(MD_EXIO_SD_CS, LOW);

  g_sd_spi.begin(MD_SD_SCK, MD_SD_MISO, MD_SD_MOSI, MD_SD_CS_PLACEHOLDER);

  // Try fast, then fall back. 20MHz makes a 750KB wallpaper load in a fraction of a second,
  // but it is an optimisation and not a requirement: chip select is held by an I2C expander on
  // wiring never intended for speed, and a card that will not train at 20MHz must still work.
  // Failing the mount outright would take the layout, the icons and the wallpapers with it.
  const uint32_t speeds[] = {MD_SD_FREQ_HZ, 4000000};

  for (uint32_t freq : speeds) {
    if (SD.begin(MD_SD_CS_PLACEHOLDER, g_sd_spi, freq)) {
      g_sd_mounted = true;
      MD_LOG.printf("[board] SD mounted at %lu MHz, %llu MB\n",
                    static_cast<unsigned long>(freq / 1000000),
                    SD.cardSize() / (1024ULL * 1024ULL));
      return true;
    }
    MD_LOG.printf("[board] SD did not mount at %lu MHz\n",
                  static_cast<unsigned long>(freq / 1000000));
    SD.end();
  }

  MD_LOG.println("[board] SD mount failed — card seated? formatted FAT32?");
  return false;
}

bool sdMounted() { return g_sd_mounted; }

}  // namespace board_port
