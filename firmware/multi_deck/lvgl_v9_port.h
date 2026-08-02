// LVGL 9 display and input glue.
//
// ESP32_Display_Panel ships only an LVGL v8 port (issue #63 is still open), so this is our
// own translation of it to the v9 API. It talks to the panel exclusively through board_port.
#pragma once

#include <lvgl.h>

namespace lvgl_port {

// Initialises LVGL, allocates draw buffers in PSRAM, and registers the display and the
// touch input device. Call after board_port::begin().
bool begin();

// Drives LVGL's timers. Call from loop().
void poll();

}  // namespace lvgl_port
