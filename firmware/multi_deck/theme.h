// The deck's look, as a set of LVGL styles built once per theme.
//
// Before this existed, every widget set its own colours inline — five setters per tile,
// scattered across two builders. That is why several visible colours (the status dot, the
// stats captions, the arc track) were never wired to the theme at all: there was no single
// place that knew what the deck was supposed to look like, so tokens simply got missed.
//
// Now the styles below are the only place colour is decided. Builders attach them and say
// nothing about appearance.
#pragma once

#include <lvgl.h>

#include "deck_config.h"

namespace theme {

// Rebuilds every style from `t`. Safe to call repeatedly; the caller must rebuild the screen
// afterwards, since LVGL objects hold pointers to these styles rather than copies.
void apply(const Theme &t);

// The theme the styles were last built from. Valid only after apply().
const Theme &current();

// Returns and clears whatever went wrong during the last apply() — a wallpaper that would not
// load, typically. Empty when all was well.
//
// It is returned rather than logged because the log lives on port A, and a wallpaper silently
// not appearing is exactly the kind of failure you cannot debug from the front of the device.
String takeError();

// --- styles, in the order they are attached -----------------------------------------------

extern lv_style_t screen;      // background: wallpaper if the theme has one, else flat `bg`
extern lv_style_t surface;     // content pane; transparent, see note in theme.cpp
extern lv_style_t panel;       // a card that holds a readout rather than a press target
extern lv_style_t nav_scrim;   // nav bar: transparent, or a scrim when a wallpaper is set
extern lv_style_t tile;        // grid and numpad keys
extern lv_style_t tile_press;  // LV_STATE_PRESSED
extern lv_style_t tile_off;    // agent-dependent tile with no agent
extern lv_style_t tab;         // nav tab, inactive
extern lv_style_t tab_active;
extern lv_style_t label;       // tile and tab text
extern lv_style_t label_muted; // captions, secondary readouts
extern lv_style_t toast;

// Fonts. Sized up deliberately for legibility at arm's length; FONT_BASE is applied to the
// screen so anything that does not override it inherits the larger size.
extern const lv_font_t *const FONT_BASE;  // nav tabs, toasts
extern const lv_font_t *const FONT_TILE;  // grid tile labels
extern const lv_font_t *const FONT_PAD;   // ten-key digits

// Icon glyphs, always Montserrat whatever face the text uses. LV_SYMBOL_* lives in Montserrat's
// FontAwesome range and no custom face carries it. The fallback chain would cover this, but a
// tile silently losing its icon is not a thing to leave to an implicit mechanism.
extern const lv_font_t *const FONT_SYMBOL;     // icon beside a label
extern const lv_font_t *const FONT_SYMBOL_LG;  // icon-only tile

// The stats page reads as a instrument panel rather than as a list of controls, so it gets its
// own face — Century Gothic, see fonts.h. Kept separate from the three above because those are
// also what symbol labels render in, and Century carries no symbol glyphs.
extern const lv_font_t *const FONT_STAT_VALUE;  // 40px gauge readings
extern const lv_font_t *const FONT_STAT_LABEL;  // 28px gauge captions
extern const lv_font_t *const FONT_STAT_TEXT;   // 20px detail line

// Geometry, so the builders stop hard-coding it.
constexpr int NAV_H = 56;
constexpr int PAD = 8;

// Interior padding of a `panel`. Lives here because the stats page lays out against a card's
// content box and would otherwise have to guess it — two numbers that must agree, in two files.
constexpr int CARD_PAD = PAD + 4;

// Status dot colours, which are theme tokens rather than styles because the dot switches
// between them at runtime.
lv_color_t statusColor(bool link_up);

}  // namespace theme
