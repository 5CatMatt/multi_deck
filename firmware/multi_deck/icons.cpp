#include "icons.h"

#include <lvgl.h>

namespace icons {
namespace {

struct Entry {
  const char *name;
  const char *symbol;
};

// Every symbol LVGL builds into the Montserrat fonts, named by the rule in icons.h. Sorted so
// that adding one is obvious and so this reads as the reference list it doubles as — the agent
// mirrors it in ICON_NAMES, and protocol_test.py parses this table to check the two agree.
//
// LV_SYMBOL_DUMMY is omitted deliberately: it is LVGL's internal marker for "no glyph", not
// something a tile should ever ask for.
const Entry kIcons[] = {
    {"audio", LV_SYMBOL_AUDIO},
    {"backspace", LV_SYMBOL_BACKSPACE},
    {"bars", LV_SYMBOL_BARS},
    {"battery_1", LV_SYMBOL_BATTERY_1},
    {"battery_2", LV_SYMBOL_BATTERY_2},
    {"battery_3", LV_SYMBOL_BATTERY_3},
    {"battery_empty", LV_SYMBOL_BATTERY_EMPTY},
    {"battery_full", LV_SYMBOL_BATTERY_FULL},
    {"bell", LV_SYMBOL_BELL},
    {"bluetooth", LV_SYMBOL_BLUETOOTH},
    {"bullet", LV_SYMBOL_BULLET},
    {"call", LV_SYMBOL_CALL},
    {"charge", LV_SYMBOL_CHARGE},
    {"close", LV_SYMBOL_CLOSE},
    {"copy", LV_SYMBOL_COPY},
    {"cut", LV_SYMBOL_CUT},
    {"directory", LV_SYMBOL_DIRECTORY},
    {"down", LV_SYMBOL_DOWN},
    {"download", LV_SYMBOL_DOWNLOAD},
    {"drive", LV_SYMBOL_DRIVE},
    {"edit", LV_SYMBOL_EDIT},
    {"eject", LV_SYMBOL_EJECT},
    {"envelope", LV_SYMBOL_ENVELOPE},
    {"eye_close", LV_SYMBOL_EYE_CLOSE},
    {"eye_open", LV_SYMBOL_EYE_OPEN},
    {"file", LV_SYMBOL_FILE},
    {"gps", LV_SYMBOL_GPS},
    {"home", LV_SYMBOL_HOME},
    {"image", LV_SYMBOL_IMAGE},
    {"keyboard", LV_SYMBOL_KEYBOARD},
    {"left", LV_SYMBOL_LEFT},
    {"list", LV_SYMBOL_LIST},
    {"loop", LV_SYMBOL_LOOP},
    {"minus", LV_SYMBOL_MINUS},
    {"mute", LV_SYMBOL_MUTE},
    {"new_line", LV_SYMBOL_NEW_LINE},
    {"next", LV_SYMBOL_NEXT},
    {"ok", LV_SYMBOL_OK},
    {"paste", LV_SYMBOL_PASTE},
    {"pause", LV_SYMBOL_PAUSE},
    {"play", LV_SYMBOL_PLAY},
    {"plus", LV_SYMBOL_PLUS},
    {"power", LV_SYMBOL_POWER},
    {"prev", LV_SYMBOL_PREV},
    {"refresh", LV_SYMBOL_REFRESH},
    {"right", LV_SYMBOL_RIGHT},
    {"save", LV_SYMBOL_SAVE},
    {"sd_card", LV_SYMBOL_SD_CARD},
    {"settings", LV_SYMBOL_SETTINGS},
    {"shuffle", LV_SYMBOL_SHUFFLE},
    {"stop", LV_SYMBOL_STOP},
    {"tint", LV_SYMBOL_TINT},
    {"trash", LV_SYMBOL_TRASH},
    {"up", LV_SYMBOL_UP},
    {"upload", LV_SYMBOL_UPLOAD},
    {"usb", LV_SYMBOL_USB},
    {"video", LV_SYMBOL_VIDEO},
    {"volume_max", LV_SYMBOL_VOLUME_MAX},
    {"volume_mid", LV_SYMBOL_VOLUME_MID},
    {"warning", LV_SYMBOL_WARNING},
    {"wifi", LV_SYMBOL_WIFI},
};

}  // namespace

const char *symbol(const String &name) {
  if (name.isEmpty()) return nullptr;

  // Linear over ~60 entries, and only while building a page. Not worth a sorted lookup.
  for (const Entry &entry : kIcons) {
    if (name == entry.name) return entry.symbol;
  }
  return nullptr;
}

}  // namespace icons
