#include "calendar_view.h"

#include "config.h"
#include "device_time.h"
#include "theme.h"

namespace calendar_view {
namespace {

using theme::PAD;

constexpr int COLS = 7;
constexpr int ROWS = 6;  // enough for any month: 31 days starting on a Saturday spans six weeks

constexpr int AREA_W = MD_SCREEN_W;
constexpr int AREA_H = MD_SCREEN_H - theme::NAV_H;

constexpr int HEADER_H = 52;
constexpr int WEEKDAY_H = 26;
constexpr int GRID_TOP = HEADER_H + WEEKDAY_H + PAD;

constexpr int CELL_W = (AREA_W - 2 * PAD) / COLS;
constexpr int CELL_H = (AREA_H - GRID_TOP - PAD) / ROWS;

constexpr int BUTTON_W = 76;
constexpr int BUTTON_H = 40;

lv_obj_t *g_root = nullptr;
lv_obj_t *g_title = nullptr;
lv_obj_t *g_days[ROWS * COLS] = {nullptr};

// The month on screen, which is not necessarily this one — that is the whole point of the
// prev/next controls.
int g_year = 0;
int g_month = 0;  // 0-11

// Local days since the epoch at the last render, so tick() can spot midnight and the arrival
// of the first time sync with one comparison. -1 means "nothing rendered yet".
int64_t g_rendered_day = -1;

const char *const kWeekdays[COLS] = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"};
const char *const kMonths[12] = {"January",   "February", "March",    "April",
                                 "May",       "June",     "July",     "August",
                                 "September", "October",  "November", "December"};

bool isLeap(int year) {
  return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
}

int daysInMonth(int year, int month) {
  static const int kDays[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (month == 1 && isLeap(year)) return 29;
  return kDays[month];
}

// Sakamoto's algorithm. 0 = Sunday. Chosen over walking forward from a known date because it
// is exact for any year without a loop, and it gets leap centuries right — 1900 was not a leap
// year and 2000 was, which is where the naive `year % 4` version quietly goes wrong.
int dayOfWeek(int year, int month, int day) {
  static const int kOffsets[12] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
  int y = year;
  if (month < 2) y -= 1;
  return (y + y / 4 - y / 100 + y / 400 + kOffsets[month] + day) % 7;
}

void todayParts(int &year, int &month, int &day) {
  struct tm now;
  if (!device_time::localTm(now)) {
    year = 0;
    month = 0;
    day = 0;
    return;
  }
  year = now.tm_year + 1900;
  month = now.tm_mon;
  day = now.tm_mday;
}

void render() {
  if (g_title == nullptr) return;

  const Theme &t = theme::current();

  if (!device_time::valid()) {
    lv_label_set_text(g_title, "Waiting for the PC");
    for (auto *cell : g_days) {
      if (cell == nullptr) continue;
      lv_label_set_text(cell, "");
      lv_obj_set_style_bg_opa(cell, LV_OPA_TRANSP, 0);
    }
    return;
  }

  char title[32];
  snprintf(title, sizeof(title), "%s %d", kMonths[g_month], g_year);
  lv_label_set_text(g_title, title);

  int today_year = 0;
  int today_month = 0;
  int today_day = 0;
  todayParts(today_year, today_month, today_day);

  const int lead = dayOfWeek(g_year, g_month, 1);
  const int count = daysInMonth(g_year, g_month);

  // The tail of the previous month and the head of the next, so the grid never has holes and
  // a date near a boundary can still be placed on a weekday.
  const int prev_month = (g_month + 11) % 12;
  const int prev_count = daysInMonth(g_month == 0 ? g_year - 1 : g_year, prev_month);

  for (int index = 0; index < ROWS * COLS; index++) {
    lv_obj_t *cell = g_days[index];
    if (cell == nullptr) continue;

    const int offset = index - lead;
    int number;
    bool in_month;

    if (offset < 0) {
      number = prev_count + offset + 1;
      in_month = false;
    } else if (offset >= count) {
      number = offset - count + 1;
      in_month = false;
    } else {
      number = offset + 1;
      in_month = true;
    }

    char text[4];
    snprintf(text, sizeof(text), "%d", number);
    lv_label_set_text(cell, text);

    const bool is_today = in_month && g_year == today_year && g_month == today_month &&
                          number == today_day;

    if (is_today) {
      // A filled pill on the label itself rather than a container behind it. Halves the object
      // count of the grid, which matters against a 48KB LVGL pool.
      lv_obj_set_style_bg_color(cell, lv_color_hex(t.accent), 0);
      lv_obj_set_style_bg_opa(cell, LV_OPA_COVER, 0);
      lv_obj_set_style_text_color(cell, lv_color_hex(t.bg), 0);
    } else {
      lv_obj_set_style_bg_opa(cell, LV_OPA_TRANSP, 0);
      lv_obj_set_style_text_color(cell, lv_color_hex(in_month ? t.text : t.text_muted), 0);
    }
  }

  g_rendered_day = device_time::localEpoch() / 86400;
}

void showToday() {
  int day = 0;
  todayParts(g_year, g_month, day);
  render();
}

void stepMonth(int delta) {
  int month = g_month + delta;
  int year = g_year;

  while (month < 0) {
    month += 12;
    year -= 1;
  }
  while (month > 11) {
    month -= 12;
    year += 1;
  }

  g_month = month;
  g_year = year;
  render();
}

void onPrev(lv_event_t *) { stepMonth(-1); }
void onNext(lv_event_t *) { stepMonth(1); }
void onToday(lv_event_t *) { showToday(); }

lv_obj_t *makeButton(lv_obj_t *parent, const char *label, int x, lv_event_cb_t handler) {
  lv_obj_t *button = lv_button_create(parent);
  lv_obj_remove_style_all(button);
  lv_obj_add_style(button, &theme::tile, 0);
  lv_obj_add_style(button, &theme::tile_press, LV_STATE_PRESSED);
  lv_obj_remove_flag(button, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_size(button, BUTTON_W, BUTTON_H);
  lv_obj_set_pos(button, x, (HEADER_H - BUTTON_H) / 2);

  lv_obj_t *text = lv_label_create(button);
  lv_label_set_text(text, label);
  lv_obj_add_style(text, &theme::label, 0);
  lv_obj_center(text);

  lv_obj_add_event_cb(button, handler, LV_EVENT_CLICKED, nullptr);
  return button;
}

}  // namespace

void build(lv_obj_t *parent) {
  detach();

  g_root = lv_obj_create(parent);
  lv_obj_remove_style_all(g_root);
  lv_obj_set_size(g_root, AREA_W, AREA_H);
  lv_obj_set_pos(g_root, 0, 0);
  lv_obj_remove_flag(g_root, LV_OBJ_FLAG_SCROLLABLE);

  makeButton(g_root, LV_SYMBOL_LEFT, PAD, onPrev);
  makeButton(g_root, "Today", AREA_W - PAD - BUTTON_W * 2 - PAD, onToday);
  makeButton(g_root, LV_SYMBOL_RIGHT, AREA_W - PAD - BUTTON_W, onNext);

  g_title = lv_label_create(g_root);
  lv_obj_set_style_text_font(g_title, theme::FONT_STAT_LABEL, 0);
  lv_obj_set_style_text_color(g_title, lv_color_hex(theme::current().text), 0);
  lv_obj_align(g_title, LV_ALIGN_TOP_MID, 0, (HEADER_H - 28) / 2);

  for (int col = 0; col < COLS; col++) {
    lv_obj_t *label = lv_label_create(g_root);
    lv_label_set_text(label, kWeekdays[col]);
    lv_obj_set_style_text_font(label, theme::FONT_STAT_TEXT, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(theme::current().text_muted), 0);
    lv_obj_set_width(label, CELL_W);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_pos(label, PAD + col * CELL_W, HEADER_H);
  }

  for (int index = 0; index < ROWS * COLS; index++) {
    const int col = index % COLS;
    const int row = index / COLS;

    lv_obj_t *cell = lv_label_create(g_root);
    lv_obj_set_style_text_font(cell, theme::FONT_STAT_LABEL, 0);
    lv_obj_set_width(cell, CELL_W);
    lv_obj_set_style_text_align(cell, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_radius(cell, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_ver(cell, 4, 0);
    lv_obj_set_pos(cell, PAD + col * CELL_W, GRID_TOP + row * CELL_H);

    g_days[index] = cell;
  }

  showToday();
}

void detach() {
  g_root = nullptr;
  g_title = nullptr;
  for (auto &cell : g_days) cell = nullptr;
  g_rendered_day = -1;
}

bool isVisible() { return g_root != nullptr; }

void tick() {
  if (g_root == nullptr || !device_time::valid()) return;

  // Catches two things with one comparison: midnight moving the highlight, and the first time
  // sync arriving after a cold boot, when the page went up saying "waiting for the PC".
  if (device_time::localEpoch() / 86400 == g_rendered_day) return;

  // A negative rendered day means the page went up before the agent had told us the date, so
  // this is the first real render and it should land on the current month rather than on the
  // zeroed one. Afterwards it is just midnight moving the highlight.
  if (g_rendered_day < 0) {
    showToday();
    return;
  }
  render();
}

}  // namespace calendar_view
