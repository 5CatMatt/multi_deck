// The screen the deck shows while the PC is asleep.
//
// Deliberately not a PageType. It has to appear over whatever page happens to be up, and it
// must not be reachable from the nav bar — you cannot navigate to "asleep", you are told about
// it. So it is a layer above everything rather than a page among the others.
//
// Entered only on an explicit `power` frame saying the PC is going away. Silence on the link is
// not enough: a closed agent, an unplugged cable and a sleeping PC are indistinguishable from
// the deck's side, and turning the deck into a clock because someone quit the tray icon would
// be wrong. Left on touch, on a `wake` frame, or on any new session.
#pragma once

#include <lvgl.h>

namespace sleep_view {

// Puts the sleep screen up over `screen`. Idempotent.
//
// `on_dismiss` fires when the screen is touched. Passed in rather than called back into the UI
// from here, so this file knows nothing about pages, themes or the idle state — it draws a
// clock and reports that someone touched it.
void enter(lv_obj_t *screen, lv_event_cb_t on_dismiss);

// Takes it down. Idempotent, so the several things that can end a sleep may all just call it.
void leave();

bool isActive();

// Drops cached widget pointers without deleting them, for when the screen is cleaned from
// underneath us. Same contract as stats_view::detach().
void detach();

// Repaints the clock when the minute rolls over, and nudges its position occasionally. Cheap
// enough to call from the main loop; does nothing at all when not active.
void tick();

}  // namespace sleep_view
