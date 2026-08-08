#include "sleep_view.h"

#include "config.h"
#include "device_time.h"
#include "fonts.h"
#include "theme.h"

namespace sleep_view {
namespace {

lv_obj_t *g_root = nullptr;
lv_obj_t *g_clock = nullptr;
lv_obj_t *g_date = nullptr;
lv_obj_t *g_hint = nullptr;

bool g_active = false;
uint32_t g_next_nudge_ms = 0;
int g_nudge = 0;

// The panel cannot dim its backlight below "on" as wired, so a night-time clock has to get
// dark by being drawn dark. These are deliberately close to the background rather than
// theme::text — bright white at 3am across a dark room is the thing this avoids.
constexpr uint8_t CLOCK_OPA = 140;
constexpr uint8_t DATE_OPA = 90;
constexpr uint8_t HINT_OPA = 60;

// How far the clock wanders, and how often. An IPS panel does not burn in the way an OLED
// does, but eight unmoving hours a night is exactly the recipe for image retention, and a few
// pixels of drift costs nothing to insure against.
constexpr int NUDGE_PX = 6;
constexpr uint32_t NUDGE_INTERVAL_MS = 5UL * 60UL * 1000UL;

const char *const kWeekdays[] = {"Sunday",   "Monday", "Tuesday", "Wednesday",
                                 "Thursday", "Friday", "Saturday"};
const char *const kMonths[] = {"January",   "February", "March",    "April",
                               "May",       "June",     "July",     "August",
                               "September", "October",  "November", "December"};

void refresh() {
  if (g_clock == nullptr) return;

  struct tm now;
  if (!device_time::localTm(now)) {
    // No sync since power-up. Saying so beats inventing a time — and unlike a wrong clock, it
    // tells you where to look.
    lv_label_set_text(g_clock, "--:--");
    lv_label_set_text(g_date, "waiting for the PC");
    return;
  }

  char buffer[16];
  snprintf(buffer, sizeof(buffer), "%02d:%02d", now.tm_hour, now.tm_min);
  lv_label_set_text(g_clock, buffer);

  char date[48];
  snprintf(date, sizeof(date), "%s %d %s", kWeekdays[now.tm_wday % 7], now.tm_mday,
           kMonths[now.tm_mon % 12]);
  lv_label_set_text(g_date, date);
}

void place() {
  if (g_clock == nullptr) return;

  const int dx = (g_nudge % 2 == 0) ? -NUDGE_PX : NUDGE_PX;
  const int dy = ((g_nudge / 2) % 2 == 0) ? -NUDGE_PX : NUDGE_PX;

  lv_obj_align(g_clock, LV_ALIGN_CENTER, dx, dy - 30);
  lv_obj_align(g_date, LV_ALIGN_CENTER, dx, dy + 48);
  lv_obj_align(g_hint, LV_ALIGN_BOTTOM_MID, dx, -24 + dy);
}

lv_obj_t *makeLabel(lv_obj_t *parent, const lv_font_t *font, uint8_t opa) {
  lv_obj_t *label = lv_label_create(parent);
  lv_obj_set_style_text_font(label, font, 0);
  lv_obj_set_style_text_color(label, lv_color_hex(theme::current().text), 0);
  lv_obj_set_style_text_opa(label, opa, 0);
  return label;
}

}  // namespace

void enter(lv_obj_t *screen, lv_event_cb_t on_dismiss) {
  if (g_active || screen == nullptr) return;

  g_root = lv_obj_create(screen);
  lv_obj_remove_style_all(g_root);
  lv_obj_set_size(g_root, MD_SCREEN_W, MD_SCREEN_H);
  lv_obj_set_pos(g_root, 0, 0);
  lv_obj_remove_flag(g_root, LV_OBJ_FLAG_SCROLLABLE);

  // Opaque black rather than a veil over the wallpaper. This is the one screen where hiding
  // the artwork is the point: a photo behind a clock at night is exactly the light you do not
  // want, and an unchanging bright image is also the worst case for retention.
  lv_obj_set_style_bg_color(g_root, lv_color_black(), 0);
  lv_obj_set_style_bg_opa(g_root, LV_OPA_COVER, 0);

  // Clickable, so the touch that dismisses the clock is swallowed here rather than pressing
  // whatever tile happens to lie beneath it.
  lv_obj_add_flag(g_root, LV_OBJ_FLAG_CLICKABLE);
  if (on_dismiss != nullptr) {
    lv_obj_add_event_cb(g_root, on_dismiss, LV_EVENT_CLICKED, nullptr);
  }

  g_clock = makeLabel(g_root, &md_font_centuryclock_96, CLOCK_OPA);
  g_date = makeLabel(g_root, theme::FONT_STAT_LABEL, DATE_OPA);
  g_hint = makeLabel(g_root, theme::FONT_STAT_TEXT, HINT_OPA);
  lv_label_set_text(g_hint, "PC asleep - touch to dismiss");

  g_active = true;
  g_next_nudge_ms = millis() + NUDGE_INTERVAL_MS;

  refresh();
  place();
  device_time::minuteChanged(true);

  MD_LOG.println("[sleep] PC asleep - showing the clock");
}

void leave() {
  if (!g_active) return;

  if (g_root != nullptr) lv_obj_delete(g_root);
  detach();
  MD_LOG.println("[sleep] back to the deck");
}

void detach() {
  g_root = nullptr;
  g_clock = nullptr;
  g_date = nullptr;
  g_hint = nullptr;
  g_active = false;
}

bool isActive() { return g_active; }

void tick() {
  if (!g_active) return;

  if (device_time::minuteChanged(true)) refresh();

  if (millis() >= g_next_nudge_ms) {
    g_next_nudge_ms = millis() + NUDGE_INTERVAL_MS;
    g_nudge = (g_nudge + 1) % 4;
    place();
  }
}

}  // namespace sleep_view
