#include "deck_config.h"

#include <SD.h>
#include <ctype.h>

#include "config.h"

namespace {

ActionType actionTypeFromString(const char *s) {
  if (s == nullptr) return ActionType::None;
  if (strcmp(s, "hid") == 0) return ActionType::Hid;
  if (strcmp(s, "hid_text") == 0) return ActionType::HidText;
  if (strcmp(s, "media") == 0) return ActionType::Media;
  if (strcmp(s, "page") == 0) return ActionType::Page;
  if (strcmp(s, "delay") == 0) return ActionType::Delay;
  if (strcmp(s, "theme") == 0) return ActionType::Theme;
  if (strcmp(s, "launch") == 0) return ActionType::Launch;
  if (strcmp(s, "ahk") == 0) return ActionType::Ahk;
  if (strcmp(s, "shell") == 0) return ActionType::Shell;
  if (strcmp(s, "seq") == 0) return ActionType::Seq;
  return ActionType::None;
}

// Writes `out` and returns true only for a well-formed colour. The caller keeps its own
// default on false.
//
// The previous version signalled failure by *returning* a fallback colour, which made
// "absent" and "black" indistinguishable to every caller — with a dozen tokens instead of
// four, that ambiguity gets expensive. It was also loose about length: `strlen < 6` let
// "#1b2129ff" through, and strtoul then quietly produced a completely different colour from
// the low 24 bits.
bool parseColor(JsonVariantConst value, uint32_t &out) {
  const char *s = value.as<const char *>();
  if (s == nullptr) return false;
  if (*s == '#') s++;

  if (strlen(s) != 6) return false;
  for (int i = 0; i < 6; i++) {
    if (!isxdigit(static_cast<unsigned char>(s[i]))) return false;
  }

  out = static_cast<uint32_t>(strtoul(s, nullptr, 16));
  return true;
}

// Percentages are clamped rather than rejected: a theme is cosmetic, and refusing to render
// because someone typed 120 would be a worse outcome than showing them 100.
bool parsePercent(JsonVariantConst value, uint8_t &out) {
  if (!value.is<int>()) return false;
  int v = value.as<int>();
  if (v < 0) v = 0;
  if (v > 100) v = 100;
  out = static_cast<uint8_t>(v);
  return true;
}

TileDisplay tileDisplayFromString(const char *s, TileDisplay fallback) {
  if (s == nullptr) return fallback;
  if (strcmp(s, "icon_text") == 0) return TileDisplay::IconText;
  if (strcmp(s, "icon") == 0) return TileDisplay::Icon;
  if (strcmp(s, "text") == 0) return TileDisplay::Text;
  return fallback;
}

const char *tileDisplayName(TileDisplay d) {
  switch (d) {
    case TileDisplay::IconText: return "icon_text";
    case TileDisplay::Icon:     return "icon";
    case TileDisplay::Text:     return "text";
    default:                    return "inherit";
  }
}

Theme parseTheme(JsonObjectConst src) {
  Theme theme;
  if (src.isNull()) return theme;

  theme.name = String(src["name"] | "");
  theme.wallpaper = String(src["wallpaper"] | "");

  parseColor(src["bg"], theme.bg);
  parseColor(src["accent"], theme.accent);
  parseColor(src["text"], theme.text);
  parseColor(src["text_muted"], theme.text_muted);
  parseColor(src["border"], theme.border);
  parseColor(src["ok"], theme.ok);
  parseColor(src["idle"], theme.idle);

  // tile_grad follows tile unless it is given explicitly, so a theme that sets only `tile`
  // gets a flat fill rather than a gradient into the old default.
  if (parseColor(src["tile"], theme.tile)) theme.tile_grad = theme.tile;
  parseColor(src["tile_grad"], theme.tile_grad);

  parsePercent(src["tile_opa"], theme.tile_opa);
  parsePercent(src["border_opa"], theme.border_opa);
  parsePercent(src["dim_opa"], theme.dim_opa);

  if (src["radius"].is<int>()) {
    int r = src["radius"].as<int>();
    if (r < 0) r = 0;
    if (r > 64) r = 64;
    theme.radius = static_cast<uint8_t>(r);
  }

  theme.flip180 = src["flip180"] | theme.flip180;
  theme.display = tileDisplayFromString(src["display"], theme.display);
  return theme;
}

}  // namespace

bool Theme::operator==(const Theme &o) const {
  return name == o.name && wallpaper == o.wallpaper && bg == o.bg && tile == o.tile &&
         tile_grad == o.tile_grad && border == o.border && accent == o.accent &&
         text == o.text && text_muted == o.text_muted && ok == o.ok && idle == o.idle &&
         tile_opa == o.tile_opa && border_opa == o.border_opa && radius == o.radius &&
         dim_opa == o.dim_opa && flip180 == o.flip180 && display == o.display;
}

void Theme::log() const {
  MD_LOG.printf(
      "[theme] \"%s\" bg=#%06X tile=#%06X grad=#%06X opa=%u border=#%06X/%u radius=%u "
      "dim_opa=%u\n",
      name.c_str(), bg, tile, tile_grad, tile_opa, border, border_opa, radius, dim_opa);
  MD_LOG.printf(
      "[theme]   accent=#%06X text=#%06X muted=#%06X ok=#%06X idle=#%06X flip180=%d "
      "display=%s wallpaper=%s\n",
      accent, text, text_muted, ok, idle, flip180 ? 1 : 0, tileDisplayName(display),
      wallpaper.isEmpty() ? "(none)" : wallpaper.c_str());
}

void parseActionJson(JsonObjectConst src, Action &out) {
  out.type = actionTypeFromString(src["type"]);

  switch (out.type) {
    case ActionType::Hid:
      for (JsonVariantConst k : src["keys"].as<JsonArrayConst>()) {
        out.keys.push_back(String(k.as<const char *>()));
      }
      break;

    case ActionType::HidText:
      out.text = String(src["text"] | "");
      break;

    case ActionType::Media:
      out.key = String(src["key"] | "");
      break;

    case ActionType::Page:
    case ActionType::Launch:
    case ActionType::Theme:
      out.target = String(src["target"] | "");
      break;

    case ActionType::Delay:
      out.delay_ms = src["ms"] | 0;
      break;

    case ActionType::Seq:
      for (JsonObjectConst step : src["steps"].as<JsonArrayConst>()) {
        Action child;
        parseActionJson(step, child);
        out.steps.push_back(child);
      }
      break;

    default:
      // Ahk and Shell carry payload the agent reads from its own copy of the layout, so the
      // device does not need to model their fields.
      break;
  }
}

bool Action::isLocal() const {
  switch (type) {
    case ActionType::None:
    case ActionType::Hid:
    case ActionType::HidText:
    case ActionType::Media:
    case ActionType::Page:
    case ActionType::Delay:
    case ActionType::Theme:
      return true;

    case ActionType::Launch:
    case ActionType::Ahk:
    case ActionType::Shell:
      return false;

    case ActionType::Seq:
      for (const auto &step : steps) {
        if (!step.isLocal()) return false;
      }
      return true;
  }
  return false;
}

bool DeckConfig::parse(JsonObjectConst root) {
  // Captured before themes is cleared: an ordinary layout edit should not knock the deck back
  // to theme zero, so if the theme you were on still exists by name, you stay on it.
  const String previous = themes.empty() ? String("") : theme().name;

  pages.clear();
  themes.clear();

  rev = root["rev"] | 0;

  JsonArrayConst theme_list = root["themes"];
  if (!theme_list.isNull()) {
    for (JsonObjectConst t : theme_list) themes.push_back(parseTheme(t));
  } else {
    // Legacy single-object form. A null object yields a fully defaulted theme, so a deck.json
    // with no theme block at all still lands here rather than in the empty case below.
    themes.push_back(parseTheme(root["theme"]));
  }
  if (themes.empty()) themes.push_back(Theme{});

  for (size_t i = 0; i < themes.size(); i++) {
    if (themes[i].name.isEmpty()) themes[i].name = "Theme " + String(static_cast<int>(i) + 1);
  }

  JsonObjectConst s = root["settings"];
  settings.brightness = s["brightness"] | 80;
  settings.idle_dim_s = s["idle_dim_s"] | 120;
  settings.idle_off_s = s["idle_off_s"] | 600;
  settings.sleep_clock_s = s["sleep_clock_s"] | 20;
  settings.dim_pct = s["dim_pct"] | 15;
  settings.theme_name = String(s["theme"] | "");
  settings.display = tileDisplayFromString(s["display"], TileDisplay::IconText);
  MD_LOG.printf("[config] tile display: %s\n", tileDisplayName(settings.display));

  active_theme_ = 0;
  if (!(previous.length() && selectTheme(previous))) {
    if (settings.theme_name.length() && !selectTheme(settings.theme_name)) {
      MD_LOG.printf("[config] settings.theme '%s' matches no theme — using '%s'\n",
                    settings.theme_name.c_str(), themes[0].name.c_str());
    }
  }

  for (JsonObjectConst p : root["pages"].as<JsonArrayConst>()) {
    Page page;
    page.id = String(p["id"] | "");
    page.title = String(p["title"] | page.id);

    const char *type = p["type"] | "grid";
    if (strcmp(type, "numpad") == 0) {
      page.type = PageType::Numpad;
    } else if (strcmp(type, "stats") == 0) {
      page.type = PageType::Stats;
    } else if (strcmp(type, "calendar") == 0) {
      page.type = PageType::Calendar;
    } else if (strcmp(type, "colortest") == 0) {
      page.type = PageType::ColorTest;
    } else {
      page.type = PageType::Grid;
    }

    JsonObjectConst grid = p["grid"];
    page.cols = grid["cols"] | 4;
    page.rows = grid["rows"] | 3;

    for (JsonObjectConst b : p["buttons"].as<JsonArrayConst>()) {
      Button button;
      button.id = String(b["id"] | "");
      button.label = String(b["label"] | "");
      button.icon = String(b["icon"] | "");
      // Parsed here so the deck.json schema is settled in one go; the renderer starts
      // honouring it in S3, when icons exist for it to choose between.
      button.display = tileDisplayFromString(b["display"], TileDisplay::Inherit);

      if (button.id.isEmpty()) {
        MD_LOG.println("[config] button with no id — skipped");
        continue;
      }

      JsonObjectConst pos = b["pos"];
      if (!pos.isNull()) {
        button.col = pos["col"] | -1;
        button.row = pos["row"] | -1;
        button.w = pos["w"] | 1;
        button.h = pos["h"] | 1;
      }

      parseActionJson(b["action"], button.action);
      button.local = button.action.isLocal();

      JsonObjectConst hold = b["hold"];
      if (!hold.isNull()) {
        parseActionJson(hold, button.hold);
        button.has_hold = true;
      }

      page.buttons.push_back(button);
    }

    pages.push_back(page);
  }

  if (pages.empty()) {
    MD_LOG.println("[config] layout contained no pages");
    return false;
  }

  MD_LOG.printf("[config] rev %d, %u pages, %u themes\n", rev,
                static_cast<unsigned>(pages.size()),
                static_cast<unsigned>(themes.size()));
  theme().log();
  return true;
}

const Theme &DeckConfig::theme() const {
  static const Theme kDefault;
  if (themes.empty()) return kDefault;
  if (active_theme_ < 0 || active_theme_ >= static_cast<int>(themes.size())) return themes[0];
  return themes[active_theme_];
}

bool DeckConfig::selectTheme(const String &name) {
  for (size_t i = 0; i < themes.size(); i++) {
    if (themes[i].name == name) {
      active_theme_ = static_cast<int>(i);
      return true;
    }
  }
  return false;
}

bool DeckConfig::cycleTheme(int delta) {
  const int count = static_cast<int>(themes.size());
  if (count < 2 || delta == 0) return false;

  // Modulo of a negative left operand is implementation-defined territory in older C++, and
  // `prev` from index 0 is exactly that case — so bias into the positive range first.
  active_theme_ = ((active_theme_ + delta) % count + count) % count;
  return true;
}

void DeckConfig::loadPersistedTheme(const char *path) {
  File file = SD.open(path, FILE_READ);
  if (!file) return;  // Nothing saved yet is the ordinary first-boot case, not an error.

  String name = file.readStringUntil('\n');
  file.close();
  name.trim();

  if (name.isEmpty()) return;

  if (selectTheme(name)) {
    // Logged again, in full. parse() already dumped a theme, but that was whichever one
    // settings.theme named — and restoring a different one here left the log showing the
    // tokens of a theme that is not on screen, which is worse than showing none.
    MD_LOG.printf("[theme] restored '%s'\n", name.c_str());
    theme().log();
  } else {
    MD_LOG.printf("[theme] saved theme '%s' is gone — using '%s'\n", name.c_str(),
                  theme().name.c_str());
  }
}

void DeckConfig::persistTheme(const char *path) const {
  File file = SD.open(path, FILE_WRITE);
  if (!file) {
    MD_LOG.printf("[theme] cannot write %s\n", path);
    return;
  }
  file.println(theme().name);
  file.close();
}

bool DeckConfig::loadFromSd(const char *path) {
  File file = SD.open(path, FILE_READ);
  if (!file) {
    MD_LOG.printf("[config] cannot open %s\n", path);
    return false;
  }

  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, file);
  file.close();

  if (error) {
    MD_LOG.printf("[config] %s is not valid JSON: %s\n", path, error.c_str());
    return false;
  }

  return parse(doc.as<JsonObjectConst>());
}

bool DeckConfig::writeToSd(const char *path, const char *json, size_t len) {
  File file = SD.open(path, FILE_WRITE);
  if (!file) {
    MD_LOG.printf("[config] cannot write %s\n", path);
    return false;
  }

  const size_t written = file.write(reinterpret_cast<const uint8_t *>(json), len);
  file.close();

  if (written != len) {
    MD_LOG.printf("[config] short write to %s (%u of %u)\n", path,
                  static_cast<unsigned>(written), static_cast<unsigned>(len));
    return false;
  }
  return true;
}

const Page *DeckConfig::pageById(const String &id) const {
  for (const auto &page : pages) {
    if (page.id == id) return &page;
  }
  return nullptr;
}

const Button *DeckConfig::buttonById(const String &id) const {
  for (const auto &page : pages) {
    for (const auto &button : page.buttons) {
      if (button.id == id) return &button;
    }
  }
  return nullptr;
}

void DeckConfig::loadFallback() {
  pages.clear();
  rev = 0;

  themes.clear();
  themes.push_back(Theme{});
  themes[0].name = "Default";
  active_theme_ = 0;

  // Deliberately HID-only and navigation-only: if we are here, the SD card is unreadable,
  // so the layout that survives should be one that needs neither SD nor the agent.
  Page home;
  home.id = "home";
  home.title = "No deck.json";
  home.type = PageType::Grid;
  home.cols = 3;
  home.rows = 2;

  auto addHid = [&home](const char *id, const char *label,
                        std::initializer_list<const char *> keys) {
    Button b;
    b.id = id;
    b.label = label;
    b.action.type = ActionType::Hid;
    for (const char *k : keys) b.action.keys.push_back(String(k));
    b.local = true;
    home.buttons.push_back(b);
  };

  addHid("fb.copy", "Copy", {"CTRL", "c"});
  addHid("fb.paste", "Paste", {"CTRL", "v"});
  addHid("fb.taskmgr", "Task Mgr", {"CTRL", "SHIFT", "ESC"});

  Button numpad;
  numpad.id = "fb.numpad";
  numpad.label = "Ten-Key";
  numpad.action.type = ActionType::Page;
  numpad.action.target = "numpad";
  numpad.local = true;
  home.buttons.push_back(numpad);

  pages.push_back(home);

  Page pad;
  pad.id = "numpad";
  pad.title = "Ten-Key";
  pad.type = PageType::Numpad;
  pages.push_back(pad);

  MD_LOG.println("[config] using built-in fallback layout");
}
