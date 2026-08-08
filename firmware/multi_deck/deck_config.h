// The deck.json model.
//
// Layout lives on the SD card, not in flash — partly so a button can be added without a
// reflash, and partly because writing flash stalls the CPU and tears the RGB panel
// (docs/hardware-notes.md).
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include <vector>

#include "config.h"

enum class ActionType : uint8_t {
  None,
  // Device-local: executed without the agent, so these keep working when the PC software
  // is closed.
  Hid,
  HidText,
  Media,
  Page,
  Delay,
  Theme,
  // Agent-side.
  Launch,
  Ahk,
  Shell,
  // Either, depending on its steps.
  Seq,
};

struct Action {
  ActionType type = ActionType::None;

  std::vector<String> keys;  // Hid
  String text;               // HidText
  String key;                // Media
  String target;             // Page, Launch, Theme
  uint32_t delay_ms = 0;     // Delay
  std::vector<Action> steps; // Seq

  // True when this action, and everything nested inside it, can run on the device alone.
  bool isLocal() const;
};

// What a tile shows, resolved most-specific-first: button, then theme, then settings.
//
// `Inherit` means "ask the next level out". Only `Settings::display` is required to be
// concrete, so a deck states its anatomy once and a theme opts out only if it wants to.
// Making every theme repeat it was the original design, and adding a theme then silently
// dropped every icon on the deck — the tiles still rendered, just as text, which looks like
// the icons were never configured rather than like a missing field.
enum class TileDisplay : uint8_t { Inherit, IconText, Icon, Text };

struct Button {
  String id;
  String label;
  String icon;
  TileDisplay display = TileDisplay::Inherit;

  int col = -1;  // -1 means auto-flow
  int row = -1;
  int w = 1;
  int h = 1;

  Action action;
  Action hold;
  bool has_hold = false;

  // Cached Action::isLocal() for the press action, computed once at parse time so the touch
  // handler does not walk the tree on every tap.
  bool local = false;
};

enum class PageType : uint8_t { Grid, Numpad, Stats, Calendar, ColorTest };

struct Page {
  String id;
  String title;
  PageType type = PageType::Grid;
  int cols = 4;
  int rows = 3;
  std::vector<Button> buttons;
};

// Everything the look of the deck is derived from. Each field is independently optional in
// deck.json; an absent one keeps the default below rather than being silently coerced, which
// is why parseColor() reports success separately from the value it produces.
struct Theme {
  String name;
  String wallpaper;  // path to an MDI1 image on SD; empty means paint `bg` flat

  uint32_t bg = 0x101418;
  uint32_t tile = 0x1B2129;
  uint32_t tile_grad = 0x1B2129;  // second gradient stop; equal to `tile` means flat
  uint32_t border = 0xFFFFFF;
  uint32_t accent = 0x4AA3FF;
  uint32_t text = 0xE6EDF3;
  uint32_t text_muted = 0x8B949E;
  uint32_t ok = 0x3FB950;    // status dot, agent connected
  uint32_t idle = 0x6E7681;  // status dot, agent absent

  uint8_t tile_opa = 100;   // 0-100; below 100 lets the wallpaper through
  uint8_t border_opa = 0;   // 0-100
  uint8_t radius = 10;

  // How dark the idle overlay goes, 0-100. A theme token rather than a setting because a pale
  // theme needs a heavier veil than a dark one to read as equally dimmed.
  //
  // The default depends on the backlight, because the two knobs multiply and this one only
  // exists to supply darkness the backlight cannot. Once the backlight can, it supplies none:
  // a real 15% behind a 55% veil is about 7% of full, which on a theme with a #060A12
  // background is indistinguishable from a dead panel. That is not a hypothetical — it is what
  // the deck did the first time it dimmed after the rewire.
  //
  // Not a branch in any logic: both states still set both knobs, every value is still a
  // percentage, and this is one number choosing a starting point. Override per theme in
  // deck.json to taste.
  uint8_t dim_opa = MD_DIM_OPA_DEFAULT;

  bool flip180 = MD_ROTATE_180;

  // Inherit by default: a theme is about colour, and most decks want one anatomy throughout.
  // Set it here only for a theme that genuinely wants a different one.
  TileDisplay display = TileDisplay::Inherit;

  // Prints every token as it ended up after parsing and defaulting. This exists to answer
  // "did my edit actually reach the device, and as what?" by looking rather than by guessing —
  // the previous way of finding out was to stare at the panel and infer.
  void log() const;

  // Lets theme::apply() skip its work when nothing changed. rebuild() runs on every page tap,
  // and rebuilding ten LVGL styles each time churns a 48 KB pool for no reason.
  bool operator==(const Theme &o) const;
  bool operator!=(const Theme &o) const { return !(*this == o); }
};

// Brightness on this deck is two knobs that multiply, and the code always drives both.
//
// `board_port::setBacklight(percent)` is the real one. On the board as shipped it is a single
// on/off line on the CH422G expander — the panel profile selects
// ESP_PANEL_BACKLIGHT_TYPE_SWITCH_EXPANDER, whose setBrightness() is literally
// `(percent > 0) ? on : off` — so today every non-zero value looks the same. That is a wiring
// limitation, not a permanent one: moving the backlight enable to a free GPIO under LEDC makes
// percentages mean percentages, and it changes only board_port.cpp.
//
// The second knob is a translucent black overlay drawn over everything, which gives darkness
// below whatever the backlight's floor turns out to be. It is what makes the sleep clock
// readable at night, and it stays useful after the rewire.
//
// So: nothing here is written as a workaround for the missing PWM. Every value stays a
// percentage and every state sets both knobs.
struct Settings {
  int brightness = 80;

  // Display power, measured from the last touch of the deck. Deliberately unhurried: the deck
  // is read as often as it is pressed — a calendar or the stats page is used by looking at it —
  // and a panel that dims while you are still reading it is answering the wrong question.
  int idle_dim_s = 120;
  int idle_off_s = 600;

  // How long the deck waits, after the PC goes away *and* you stop touching it, before turning
  // into a clock. Zero disables the clock entirely.
  //
  // This used to ride on idle_off_s, which fused two unrelated ideas: when to save the panel,
  // and when the PC has gone. That made the clock unreachable in practice — it needed the full
  // display-off timer to elapse untouched *and* the link to be already down, and on this laptop
  // those never lined up. Both halves of the condition below use this one number instead.
  int sleep_clock_s = 20;

  // Backlight level while dimmed. Distinct from the overlay: this one starts working the day
  // the backlight gains PWM, without anything else changing.
  int dim_pct = 15;

  String theme_name;  // which theme to start on; empty means the first

  // The root of the display chain, and the only level that must be concrete. IconText rather
  // than Text because a tile with no usable icon falls back to text anyway — so this default
  // costs a deck without icons nothing, and saves a deck with them from having to say so.
  TileDisplay display = TileDisplay::IconText;
};

class DeckConfig {
 public:
  int rev = 0;
  std::vector<Theme> themes;
  Settings settings;
  std::vector<Page> pages;

  // Reads and parses deck.json from SD.
  bool loadFromSd(const char *path);

  // Parses an already-deserialised layout. Shared by the SD path and by the `layout` frame
  // the agent pushes, so both routes cannot drift apart.
  bool parse(JsonObjectConst root);

  // Writes raw JSON text to SD, for persisting a pushed layout.
  static bool writeToSd(const char *path, const char *json, size_t len);

  const Page *pageById(const String &id) const;
  const Button *buttonById(const String &id) const;

  // The active theme. Always returns something usable, even with no themes parsed.
  const Theme &theme() const;

  // Selects by name. False (and no change) when no theme carries that name.
  bool selectTheme(const String &name);

  // Steps through the theme list, wrapping. Returns false when there is nothing to step to.
  bool cycleTheme(int delta);

  // The chosen theme survives a reboot via a one-line file on SD. Deliberately not NVS:
  // writing flash stalls the CPU and tears the RGB panel, while SD is on its own bus.
  void loadPersistedTheme(const char *path);
  void persistTheme(const char *path) const;

  // A minimal built-in layout, used when the SD card is missing or deck.json will not parse,
  // so the device still boots to something usable instead of a blank screen.
  void loadFallback();

 private:
  int active_theme_ = 0;
};

// Parses a bare action object. Used by the `hid_exec` frame, where the agent sends a single
// action rather than a whole layout.
void parseActionJson(JsonObjectConst src, Action &out);
