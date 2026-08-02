// Resolves a tile's `"icon"` name to one of LVGL's built-in symbol glyphs.
//
// These cost nothing: the symbol range is already inside the Montserrat fonts that are compiled
// in, so a symbol icon needs no SD card, no conversion step and no PSRAM. It renders in an
// ordinary label, which means it inherits the theme's text colour and the disabled styling for
// free — that is why verbs (copy, save, play, power) use these and only app launchers need
// images.
//
// The naming rule is deliberately mechanical: **the LVGL symbol name, lowercased**.
// LV_SYMBOL_VOLUME_MID is "volume_mid", LV_SYMBOL_SD_CARD is "sd_card". No aliases and no
// friendlier synonyms — a second vocabulary would be one more thing to keep in step with the
// agent's copy of this list, for the sake of saving four characters.
#pragma once

#include <Arduino.h>

namespace icons {

// The UTF-8 glyph for `name`, or nullptr if there is no such symbol. Callers fall back to the
// tile's text label rather than rendering nothing.
const char *symbol(const String &name);

}  // namespace icons
