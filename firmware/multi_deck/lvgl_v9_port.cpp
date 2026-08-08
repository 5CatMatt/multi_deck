#include "lvgl_v9_port.h"

#include <Arduino.h>
#include <esp_heap_caps.h>

#include "board_port.h"
#include "config.h"

namespace lvgl_port {
namespace {

lv_display_t *g_display = nullptr;
lv_indev_t *g_touch_indev = nullptr;

uint8_t *g_buf1 = nullptr;
uint8_t *g_buf2 = nullptr;

uint32_t tickCallback() { return millis(); }

void flushCallback(lv_display_t *display, const lv_area_t *area, uint8_t *px_map) {
#if MD_RGB565_SWAP
  lv_draw_sw_rgb565_swap(px_map, lv_area_get_size(area));
#endif

  board_port::flush(area->x1, area->y1, area->x2, area->y2, px_map);

  // Safe to report immediately: board_port::flush is a synchronous copy into the RGB
  // framebuffer, not an async transfer. See the comment there.
  lv_display_flush_ready(display);
}

void touchReadCallback(lv_indev_t *indev, lv_indev_data_t *data) {
  int16_t x = 0;
  int16_t y = 0;

  if (board_port::readTouch(x, y)) {
    data->point.x = x;
    data->point.y = y;
    data->state = LV_INDEV_STATE_PRESSED;
  } else {
    data->state = LV_INDEV_STATE_RELEASED;
  }
}

}  // namespace

bool begin() {
  lv_init();
  lv_tick_set_cb(tickCallback);

  const size_t buf_bytes = MD_SCREEN_W * MD_DRAW_BUF_LINES * sizeof(uint16_t);

  // Draw buffers go in PSRAM. Internal SRAM is only 512KB and the RGB framebuffer already
  // has first claim on the memory that matters.
  g_buf1 = static_cast<uint8_t *>(heap_caps_malloc(buf_bytes, MALLOC_CAP_SPIRAM));
  g_buf2 = static_cast<uint8_t *>(heap_caps_malloc(buf_bytes, MALLOC_CAP_SPIRAM));

  if (g_buf1 == nullptr || g_buf2 == nullptr) {
    MD_LOG.println("[lvgl] draw buffer alloc failed — is PSRAM set to OPI?");
    return false;
  }

  g_display = lv_display_create(MD_SCREEN_W, MD_SCREEN_H);
  if (g_display == nullptr) {
    MD_LOG.println("[lvgl] lv_display_create failed");
    return false;
  }

  lv_display_set_color_format(g_display, LV_COLOR_FORMAT_RGB565);
  lv_display_set_flush_cb(g_display, flushCallback);
  lv_display_set_buffers(g_display, g_buf1, g_buf2, buf_bytes,
                         LV_DISPLAY_RENDER_MODE_PARTIAL);

  g_touch_indev = lv_indev_create();
  lv_indev_set_type(g_touch_indev, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(g_touch_indev, touchReadCallback);
  lv_indev_set_display(g_touch_indev, g_display);

  // Without these two calls the constants in config.h are decorative and LVGL's own defaults
  // apply. See the comment there for why one threshold covers both holds and ten-key repeat.
  lv_indev_set_long_press_time(g_touch_indev, MD_LONG_PRESS_MS);
  lv_indev_set_long_press_repeat_time(g_touch_indev, MD_KEY_REPEAT_RATE_MS);

  MD_LOG.printf("[lvgl] up, 2 x %u byte draw buffers in PSRAM\n",
                static_cast<unsigned>(buf_bytes));
  return true;
}

void poll() { lv_timer_handler(); }

}  // namespace lvgl_port
