// Custom fonts generated from a TTF by tools/make_font.py.
//
// Century Gothic, at the three sizes the stats page uses. Generated rather than converted with
// lv_font_conv, which is a Node package — Pillow is already a dependency of tools/, so a font
// can be regenerated without a second toolchain:
//
//   python tools/make_font.py C:/Windows/Fonts/GOTHIC.TTF --name century --sizes 20 28 40
//
// Each carries `.fallback = &lv_font_montserrat_<size>`, which matters more than it sounds.
// These cover U+0020-U+007E only, and the stats detail line puts LV_SYMBOL_UP / LV_SYMBOL_DOWN
// *inside* the same label as its text. LVGL 9 resolves a missing glyph through the fallback
// chain, so those arrows still render in Montserrat while the digits around them are Century.
// Without it that line would come out full of holes.
//
// This is also why the font is applied to individual styles rather than to `theme::screen`:
// setting it screen-wide would push it onto the symbol labels that tiles use for icons.
#pragma once

#include <lvgl.h>

// The generated files compile as C, so their symbols need C linkage to be visible from the
// C++ side of the sketch.
extern "C" {

// Century Gothic, from C:/Windows/Fonts/GOTHIC.TTF. Used by the stats page.
extern const lv_font_t md_font_century_20;
extern const lv_font_t md_font_century_28;
extern const lv_font_t md_font_century_40;

// Nord Medium, transcoded from the TFT_eSPI .vlw files in fonts/. Only the 40px is in use, for
// the ten-key — see the note on FONT_PAD in theme.cpp. The other three stay declared because
// the linker's --gc-sections drops an unreferenced font entirely, so they cost nothing until a
// style points at one, and their sizes match the Montserrat set exactly.
//
// Note these carry no space glyph (the source has none), nor < > ^ ` ~. The fallback supplies
// them, which matters if one is ever used for prose rather than for digits.
extern const lv_font_t md_font_nord_14;
extern const lv_font_t md_font_nord_20;
extern const lv_font_t md_font_nord_28;
extern const lv_font_t md_font_nord_40;
}
