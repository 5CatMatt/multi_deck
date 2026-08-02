// A bench diagnostic page: flat colour patches, no styling of any kind.
//
// It exists because "this theme looks wrong" is very hard to act on. Comparing two themes by
// switching between them asks you to hold a colour in memory, which human vision is bad at —
// simultaneous comparison is what it is good at. So this page puts the greyscale floor, the
// dark-hue response and every theme's palette on screen at the same time, with the intended
// hex printed underneath.
//
// Add `{"id": "colors", "title": "Colours", "type": "colortest"}` to deck.json to reach it.
#pragma once

#include <lvgl.h>

#include "deck_config.h"

namespace color_test {

void build(lv_obj_t *parent, const DeckConfig &config);

}  // namespace color_test
