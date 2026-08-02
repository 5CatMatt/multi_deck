#include "ui_builder.h"

#include <lvgl.h>

#include <vector>

#include "assets.h"
#include "board_port.h"
#include "color_test.h"
#include "config.h"
#include "hid.h"
#include "icons.h"
#include "stats_view.h"
#include "theme.h"

namespace ui {
namespace {

using theme::FONT_BASE;
using theme::FONT_PAD;
using theme::FONT_TILE;
using theme::NAV_H;
using theme::PAD;

DeckConfig *g_config = nullptr;
Link *g_link = nullptr;

lv_obj_t *g_screen = nullptr;
lv_obj_t *g_nav = nullptr;
lv_obj_t *g_content = nullptr;
lv_obj_t *g_status_dot = nullptr;
lv_obj_t *g_toast = nullptr;

String g_current_page;
bool g_link_up = false;
uint32_t g_toast_until = 0;
uint8_t g_brightness = 80;
bool g_dimmed = false;

// Binding for one tile. Heap-allocated and owned by g_bindings so the pointer handed to
// LVGL as user data stays valid regardless of container growth.
struct Binding {
  const Button *button;
  String page_id;
};

std::vector<Binding *> g_bindings;

// A numpad key: label plus the raw HID usage it emits.
struct PadKey {
  const char *label;
  uint8_t usage;
  int col, row, w, h;
};

void clearBindings() {
  for (auto *binding : g_bindings) delete binding;
  g_bindings.clear();
}

void executeAction(const Action &action);

void runSequence(const Action &action) {
  for (const auto &step : action.steps) {
    if (step.type == ActionType::Delay) {
      // Blocking, so LVGL stops rendering for the duration. Acceptable for the short delays
      // a local macro needs; anything longer belongs in an agent-side sequence, where the
      // agent does the waiting.
      delay(step.delay_ms);
    } else {
      executeAction(step);
    }
  }
}

void executeAction(const Action &action) {
  switch (action.type) {
    case ActionType::Hid:
      deck_hid::sendCombo(action.keys);
      break;
    case ActionType::HidText:
      deck_hid::typeText(action.text);
      break;
    case ActionType::Media:
      deck_hid::sendMedia(action.key);
      break;
    case ActionType::Page:
      showPage(action.target);
      break;
    case ActionType::Theme:
      switchTheme(action.target);
      break;
    case ActionType::Delay:
      delay(action.delay_ms);
      break;
    case ActionType::Seq:
      runSequence(action);
      break;
    default:
      // Agent-side types never reach here; press() forwards them over the link instead.
      break;
  }
}

// Every tile in the UI goes through here, so appearance is decided in exactly one place.
// LVGL's own button styling is removed first: the default theme paints buttons its primary
// blue, and leaving that underneath means any token the theme forgets to set shows up as
// stray blue rather than as an obvious omission.
lv_obj_t *makeTile(lv_obj_t *parent, bool enabled) {
  lv_obj_t *tile = lv_button_create(parent);
  lv_obj_remove_style_all(tile);
  lv_obj_add_style(tile, enabled ? &theme::tile : &theme::tile_off, 0);
  if (enabled) lv_obj_add_style(tile, &theme::tile_press, LV_STATE_PRESSED);

  // Buttons are scrollable by default. Harmless while every tile held one centred label, but an
  // icon stack that overflows slightly would become draggable rather than simply clipped.
  lv_obj_remove_flag(tile, LV_OBJ_FLAG_SCROLLABLE);
  return tile;
}

lv_obj_t *makeTileLabel(lv_obj_t *tile, const char *text, const lv_font_t *font, bool enabled) {
  lv_obj_t *label = lv_label_create(tile);
  lv_label_set_text(label, text);
  lv_obj_add_style(label, enabled ? &theme::label : &theme::label_muted, 0);
  lv_obj_set_style_text_font(label, font, 0);
  lv_obj_center(label);
  return label;
}

// Gap between an icon and its label. Smaller than PAD, which separates whole tiles — the two
// halves of one tile should read as a unit.
constexpr int ICON_GAP = 6;

// What this tile should show. Most specific wins: button, then theme, then settings.
//
// Settings is the only level allowed to be vague-free, so the chain always terminates. The
// TileDisplay::Text at the end is unreachable via deck.json and exists only for the window
// before ui::begin() has a config.
TileDisplay displayFor(const Button &button) {
  if (button.display != TileDisplay::Inherit) return button.display;
  if (g_config == nullptr) return TileDisplay::Text;

  const TileDisplay from_theme = g_config->theme().display;
  if (from_theme != TileDisplay::Inherit) return from_theme;

  return g_config->settings.display;
}

// Puts the icon and/or label inside a tile.
//
// Any icon that cannot be produced — unknown name, missing image, no `icon` field at all —
// degrades to the text label. A half-iconned deck then reads as unfinished rather than broken,
// which matters because icons arrive a few at a time.
void fillTile(lv_obj_t *tile, const Button &button, bool enabled) {
  TileDisplay mode = displayFor(button);

  const char *symbol = nullptr;
  const lv_image_dsc_t *image = nullptr;

  if (mode != TileDisplay::Text && !button.icon.isEmpty()) {
    if (button.icon.startsWith("/")) {
      // A leading slash is the whole distinction between the two resolvers: it is already how
      // every other SD path in deck.json is written, so there is nothing new to remember.
      image = assets::load(button.icon);
      if (image == nullptr) {
        MD_LOG.printf("[ui] '%s': %s\n", button.id.c_str(), assets::lastError().c_str());
      }
    } else {
      symbol = icons::symbol(button.icon);
      if (symbol == nullptr) {
        MD_LOG.printf("[ui] '%s' wants icon '%s', which is not a built-in symbol — showing "
                      "its label instead\n",
                      button.id.c_str(), button.icon.c_str());
      }
    }
  }

  if (symbol == nullptr && image == nullptr) mode = TileDisplay::Text;

  // Text-only is one centred label. This is also every fallback path, so it stays the simplest
  // branch in here.
  if (mode == TileDisplay::Text) {
    makeTileLabel(tile, button.label.c_str(), FONT_TILE, enabled);
    return;
  }

  // Icon modes stack children vertically. Flex rather than hand-computed y offsets: tiles span
  // one or two cells, so any fixed offset that centres a 1x1 tile is wrong on a 2x2 one.
  lv_obj_set_flex_flow(tile, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_flex_align(tile, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_set_style_pad_row(tile, ICON_GAP, 0);

  if (image != nullptr) {
    lv_obj_t *img = lv_image_create(tile);
    lv_image_set_src(img, image);

    // MDI1 carries no alpha, so a disabled image icon cannot simply fade. Recolouring towards
    // the muted text colour keeps it legible instead of turning it to mud over a wallpaper —
    // the same reasoning as tile_off.
    if (!enabled && g_config != nullptr) {
      lv_obj_set_style_image_recolor(img, lv_color_hex(g_config->theme().text_muted), 0);
      lv_obj_set_style_image_recolor_opa(img, LV_OPA_60, 0);
    }
  } else {
    // A symbol is just text, so it picks up the theme's label styling and disabled treatment
    // with no special casing. Icon-only tiles get the larger font since nothing shares the room.
    lv_obj_t *glyph = lv_label_create(tile);
    lv_label_set_text(glyph, symbol);
    lv_obj_add_style(glyph, enabled ? &theme::label : &theme::label_muted, 0);
    lv_obj_set_style_text_font(
        glyph, mode == TileDisplay::Icon ? theme::FONT_SYMBOL_LG : theme::FONT_SYMBOL, 0);
  }

  if (mode == TileDisplay::IconText && !button.label.isEmpty()) {
    lv_obj_t *label = lv_label_create(tile);
    lv_label_set_text(label, button.label.c_str());
    lv_obj_add_style(label, enabled ? &theme::label : &theme::label_muted, 0);
    lv_obj_set_style_text_font(label, FONT_BASE, 0);
  }
}

void onTileEvent(lv_event_t *event) {
  auto *binding = static_cast<Binding *>(lv_event_get_user_data(event));
  if (binding == nullptr || binding->button == nullptr) return;

  const Button &button = *binding->button;
  const lv_event_code_t code = lv_event_get_code(event);

  if (code == LV_EVENT_LONG_PRESSED && button.has_hold) {
    if (button.hold.isLocal()) {
      executeAction(button.hold);
    } else if (g_link != nullptr && g_link->isUp()) {
      g_link->sendPress(button.id + ".hold", binding->page_id);
    }
    return;
  }

  if (code != LV_EVENT_CLICKED) return;

  if (button.local) {
    // Never touches the wire. This is what keeps working with the agent closed.
    executeAction(button.action);
    return;
  }

  if (g_link != nullptr && g_link->isUp()) {
    g_link->sendPress(button.id, binding->page_id);
  } else {
    toast("Agent not connected");
  }
}

void buildGridPage(const Page &page) {
  const int cols = page.cols > 0 ? page.cols : 4;
  const int rows = page.rows > 0 ? page.rows : 3;

  const int area_w = MD_SCREEN_W;
  const int area_h = MD_SCREEN_H - NAV_H;
  const int cell_w = (area_w - PAD * (cols + 1)) / cols;
  const int cell_h = (area_h - PAD * (rows + 1)) / rows;

  int flow = 0;

  for (const auto &button : page.buttons) {
    int col = button.col;
    int row = button.row;

    if (col < 0 || row < 0) {
      col = flow % cols;
      row = flow / cols;
      flow++;
    }

    if (row >= rows) {
      MD_LOG.printf("[ui] '%s' falls outside the %dx%d grid - skipped\n",
                    button.id.c_str(), cols, rows);
      continue;
    }

    const bool enabled = button.local || g_link_up;

    lv_obj_t *tile = makeTile(g_content, enabled);
    lv_obj_set_pos(tile, PAD + col * (cell_w + PAD), PAD + row * (cell_h + PAD));
    lv_obj_set_size(tile, cell_w * button.w + PAD * (button.w - 1),
                    cell_h * button.h + PAD * (button.h - 1));

    fillTile(tile, button, enabled);

    auto *binding = new Binding{&button, page.id};
    g_bindings.push_back(binding);

    lv_obj_add_event_cb(tile, onTileEvent, LV_EVENT_CLICKED, binding);
    if (button.has_hold) {
      lv_obj_add_event_cb(tile, onTileEvent, LV_EVENT_LONG_PRESSED, binding);
    }
  }
}

void onPadKeyEvent(lv_event_t *event) {
  const auto usage = static_cast<uint8_t>(
      reinterpret_cast<uintptr_t>(lv_event_get_user_data(event)));
  deck_hid::sendUsage(0, usage);
}

void buildNumpadPage() {
  // Fixed layout: expressing a ten-key as a generic JSON grid would buy nothing, and the
  // tall + and Enter keys need spans a plain grid does not describe well.
  static const PadKey kKeys[] = {
      {"Num", 0x53, 0, 0, 1, 1}, {"/", 0x54, 1, 0, 1, 1},
      {"*", 0x55, 2, 0, 1, 1},   {"-", 0x56, 3, 0, 1, 1},

      {"7", 0x5F, 0, 1, 1, 1},   {"8", 0x60, 1, 1, 1, 1},
      {"9", 0x61, 2, 1, 1, 1},   {"+", 0x57, 3, 1, 1, 2},

      {"4", 0x5C, 0, 2, 1, 1},   {"5", 0x5D, 1, 2, 1, 1},
      {"6", 0x5E, 2, 2, 1, 1},

      {"1", 0x59, 0, 3, 1, 1},   {"2", 0x5A, 1, 3, 1, 1},
      {"3", 0x5B, 2, 3, 1, 1},   {"Ent", 0x58, 3, 3, 1, 2},

      {"0", 0x62, 0, 4, 2, 1},   {".", 0x63, 2, 4, 1, 1},
  };

  constexpr int cols = 4;
  constexpr int rows = 5;

  const int area_w = MD_SCREEN_W;
  const int area_h = MD_SCREEN_H - NAV_H;
  const int cell_w = (area_w - PAD * (cols + 1)) / cols;
  const int cell_h = (area_h - PAD * (rows + 1)) / rows;

  for (const auto &key : kKeys) {
    lv_obj_t *tile = makeTile(g_content, true);
    lv_obj_set_pos(tile, PAD + key.col * (cell_w + PAD), PAD + key.row * (cell_h + PAD));
    lv_obj_set_size(tile, cell_w * key.w + PAD * (key.w - 1),
                    cell_h * key.h + PAD * (key.h - 1));

    makeTileLabel(tile, key.label, FONT_PAD, true);

    void *usage_as_data = reinterpret_cast<void *>(static_cast<uintptr_t>(key.usage));
    lv_obj_add_event_cb(tile, onPadKeyEvent, LV_EVENT_CLICKED, usage_as_data);
    // LVGL re-fires this while a key is held, which gives key repeat without a timer.
    lv_obj_add_event_cb(tile, onPadKeyEvent, LV_EVENT_LONG_PRESSED_REPEAT, usage_as_data);
  }

  // No NumLock warning banner here by choice. Keypad usages only produce digits while the
  // host has NumLock on, but the remedy is already on this page: the "Num" tile sends usage
  // 0x53, so the deck supplies the NumLock key that this laptop's keyboard lacks. If the
  // digits ever come out as arrows and Home/End, press Num.
  //
  // A banner would also have been unreliable as written: deck_hid::numLockOn() reflects the
  // host's last HID LED report, and this page is built once rather than re-rendered when that
  // arrives, so it could show a stale warning. Restoring it would mean making it reactive.
}

void onNavEvent(lv_event_t *event) {
  auto *page_id = static_cast<String *>(lv_event_get_user_data(event));
  if (page_id != nullptr) showPage(*page_id);
}

std::vector<String *> g_nav_ids;

void buildNav() {
  for (auto *id : g_nav_ids) delete id;
  g_nav_ids.clear();

  g_nav = lv_obj_create(g_screen);
  lv_obj_remove_style_all(g_nav);
  lv_obj_add_style(g_nav, &theme::nav_scrim, 0);
  lv_obj_set_size(g_nav, MD_SCREEN_W, NAV_H);
  lv_obj_set_pos(g_nav, 0, 0);
  lv_obj_remove_flag(g_nav, LV_OBJ_FLAG_SCROLLABLE);

  int x = PAD;
  for (const auto &page : g_config->pages) {
    const bool active = page.id == g_current_page;

    lv_obj_t *tab = lv_button_create(g_nav);
    lv_obj_remove_style_all(tab);
    lv_obj_add_style(tab, active ? &theme::tab_active : &theme::tab, 0);
    lv_obj_set_size(tab, 120, NAV_H - 16);
    lv_obj_set_pos(tab, x, PAD);

    lv_obj_t *label = lv_label_create(tab);
    lv_label_set_text(label, page.title.c_str());
    lv_obj_add_style(label, &theme::label, 0);
    lv_obj_center(label);

    auto *id = new String(page.id);
    g_nav_ids.push_back(id);
    lv_obj_add_event_cb(tab, onNavEvent, LV_EVENT_CLICKED, id);

    x += 128;
  }

  g_status_dot = lv_obj_create(g_nav);
  lv_obj_remove_style_all(g_status_dot);
  lv_obj_set_size(g_status_dot, 14, 14);
  lv_obj_align(g_status_dot, LV_ALIGN_RIGHT_MID, -PAD, 0);
  lv_obj_set_style_radius(g_status_dot, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_bg_opa(g_status_dot, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(g_status_dot, theme::statusColor(g_link_up), 0);
}

}  // namespace

bool begin(DeckConfig *config, Link *link) {
  g_config = config;
  g_link = link;
  g_brightness = config->settings.brightness;

  g_screen = lv_screen_active();
  lv_obj_remove_flag(g_screen, LV_OBJ_FLAG_SCROLLABLE);

  if (!config->pages.empty()) g_current_page = config->pages[0].id;

  // rebuild() builds the theme styles, so nothing here needs to touch appearance.
  rebuild();
  board_port::setBacklight(g_brightness);
  return true;
}

void rebuild() {
  clearBindings();
  stats_view::detach();
  lv_obj_clean(g_screen);

  // lv_obj_clean() destroyed every child, so each cached handle now points at freed memory.
  // Forgetting to drop g_toast here caused a use-after-free crash: toast() and tick() both
  // call lv_obj_delete() on it when non-null, so any rebuild while a toast was on screen —
  // a layout push, or simply changing page within the 2.5s toast window — took the device
  // down with a LoadProhibited panic. stats_view::detach() above does the same job for the
  // stats widgets.
  g_toast = nullptr;
  g_nav = nullptr;
  g_content = nullptr;
  g_status_dot = nullptr;

  // theme::apply() calls lv_style_reset(), which frees the property arrays that live objects
  // point into — so the screen must be stripped of its style *before* the rebuild, and the
  // rebuilt style attached after. Doing this on every rebuild rather than only on a theme
  // change keeps one ordering to get right instead of two.
  lv_obj_remove_style(g_screen, &theme::screen, LV_PART_ANY | LV_STATE_ANY);
  theme::apply(g_config->theme());
  lv_obj_add_style(g_screen, &theme::screen, 0);

  const uint8_t wanted = static_cast<uint8_t>(g_config->settings.brightness);
  if (wanted != g_brightness) {
    g_brightness = wanted;
    if (!g_dimmed) board_port::setBacklight(g_brightness);
  }
  board_port::setRotation180(g_config->theme().flip180);

  g_content = lv_obj_create(g_screen);
  lv_obj_remove_style_all(g_content);
  lv_obj_add_style(g_content, &theme::surface, 0);
  lv_obj_set_size(g_content, MD_SCREEN_W, MD_SCREEN_H - NAV_H);
  lv_obj_set_pos(g_content, 0, NAV_H);
  lv_obj_remove_flag(g_content, LV_OBJ_FLAG_SCROLLABLE);

  const Page *page = g_config->pageById(g_current_page);
  if (page == nullptr && !g_config->pages.empty()) {
    page = &g_config->pages[0];
    g_current_page = page->id;
  }

  if (page != nullptr) {
    switch (page->type) {
      case PageType::Grid:
        buildGridPage(*page);
        break;
      case PageType::Numpad:
        buildNumpadPage();
        break;
      case PageType::Stats:
        stats_view::build(g_content);
        break;
      case PageType::ColorTest:
        color_test::build(g_content, *g_config);
        break;
    }
  }

  buildNav();

  // After buildNav(), because toast() creates an object on the screen and rebuild() would
  // otherwise destroy it. takeError() clears as it reads, so a page change does not re-toast
  // a problem already reported.
  const String error = theme::takeError();
  if (!error.isEmpty()) toast(error);
}

void releaseConfigReferences() {
  // Tiles keep raw pointers into DeckConfig::pages. Clearing the bindings here means a
  // reparse cannot leave live LVGL objects pointing at freed Buttons, even briefly.
  clearBindings();
  lv_obj_clean(g_screen);
  g_toast = nullptr;
  g_nav = nullptr;
  g_content = nullptr;
  g_status_dot = nullptr;
  stats_view::detach();
}

void showPage(const String &id) {
  if (g_config->pageById(id) == nullptr) {
    MD_LOG.printf("[ui] no such page '%s'\n", id.c_str());
    return;
  }
  g_current_page = id;
  rebuild();
}

void setLinkUp(bool up) {
  if (up == g_link_up) return;
  g_link_up = up;
  rebuild();
}

// `target` is "next", "prev", or a theme name. Runs entirely on the device — no agent
// involved — so the deck can be restyled with the PC software closed, same as the ten-key.
void switchTheme(const String &target) {
  bool changed = false;

  if (target.isEmpty() || target == "next") {
    changed = g_config->cycleTheme(1);
  } else if (target == "prev") {
    changed = g_config->cycleTheme(-1);
  } else {
    changed = g_config->selectTheme(target);
    if (!changed) {
      MD_LOG.printf("[ui] no theme named '%s'\n", target.c_str());
      toast(String("No theme '") + target + "'");
      return;
    }
  }

  if (!changed) {
    toast("Only one theme");
    return;
  }

  g_config->persistTheme(MD_THEME_STATE_PATH);
  rebuild();
  g_config->theme().log();
  toast(g_config->theme().name);
}

void executeLocalAction(const Action &action) { executeAction(action); }

void toast(const String &message) {
  if (g_toast != nullptr) lv_obj_delete(g_toast);

  g_toast = lv_label_create(g_screen);
  lv_label_set_text(g_toast, message.c_str());
  lv_obj_add_style(g_toast, &theme::toast, 0);
  lv_obj_align(g_toast, LV_ALIGN_BOTTOM_MID, 0, -PAD);

  g_toast_until = millis() + 2500;
}

void tick() {
  if (g_toast != nullptr && millis() > g_toast_until) {
    lv_obj_delete(g_toast);
    g_toast = nullptr;
  }

  const uint32_t idle_ms = lv_display_get_inactive_time(nullptr);
  const bool should_dim = idle_ms > (uint32_t)g_config->settings.idle_dim_s * 1000UL;

  if (should_dim != g_dimmed) {
    g_dimmed = should_dim;
    board_port::setBacklight(should_dim ? 10 : g_brightness);
  }
}

}  // namespace ui
