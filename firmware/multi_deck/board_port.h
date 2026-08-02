// Isolation layer over ESP32_Display_Panel.
//
// Every call into the vendor library happens in board_port.cpp and nowhere else. That library
// had breaking API churn at v1.0.0 and its docs lag the headers, so when something does not
// compile, this is the only file that needs to change.
#pragma once

#include <stdint.h>

namespace board_port {

// Brings up the panel, touch controller and IO expander, and selects USB mode on the
// FSUSB42UMX so port B can enumerate. Returns false if the panel failed to start.
bool begin();

// Blits a rectangle of RGB565 pixels to the panel. Inclusive coordinates, matching LVGL's
// lv_area_t convention.
void flush(int32_t x1, int32_t y1, int32_t x2, int32_t y2, const void *pixels);

// Reads a single touch point. Returns false when nothing is touching the panel.
bool readTouch(int16_t &x, int16_t &y);

// Backlight brightness, 0-100.
void setBacklight(uint8_t percent);

// Flips the panel and the touch controller together. Only 0 and 180 are available: RGB panels
// cannot swap axes in hardware, so 90/270 would mean transposing every fragment on the CPU.
void setRotation180(bool on);

// Mounts the SD card. Handles the EXIO4 chip-select arrangement described in
// docs/hardware-notes.md, and falls back to a slower clock before giving up. Returns false if
// no card mounted.
bool sdBegin();

// Whether sdBegin() succeeded. Layout can arrive over USB without the card, so the deck looks
// entirely healthy with an unmounted card right up until something asks for a file.
bool sdMounted();

}  // namespace board_port
