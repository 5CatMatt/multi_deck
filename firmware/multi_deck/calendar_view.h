// A month calendar, for looking up where a date falls.
//
// Not a scheduler and not a diary — Windows already has those, buried behind a taskbar click
// and a panel that wants to sell you a meeting. This answers "what day is the 14th?" and
// nothing else.
//
// Device-local once the date is known. It needs the agent only to learn what day it is, so it
// keeps working with the PC closed, and after a power cycle it says so rather than guessing.
#pragma once

#include <lvgl.h>

namespace calendar_view {

void build(lv_obj_t *parent);

// Drops widget pointers before the parent screen is cleaned. Same contract as
// stats_view::detach().
void detach();

bool isVisible();

// Redraws when the day rolls over, or when the agent supplies a date for the first time.
// Cheap: it compares one integer and usually returns.
void tick();

}  // namespace calendar_view
