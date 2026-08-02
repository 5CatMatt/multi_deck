// The system stats page.
//
// Every field except `cpu` is optional in a `stats` frame — a missing NVIDIA GPU or a
// stopped LibreHardwareMonitor simply means fewer keys arrive. Absent values render as "--"
// rather than holding a stale reading.
#pragma once

#include <ArduinoJson.h>
#include <lvgl.h>

namespace stats_view {

// Colours come from theme::current(), which the caller has already applied — passing a couple
// of them in was how the arc track, chart grid and captions ended up unthemed.
void build(lv_obj_t *parent);

// Drops widget pointers before the parent screen is cleaned, so a later update() cannot
// write through dangling handles.
void detach();

void update(JsonObjectConst frame);

// True while the stats page is on screen — the agent only needs to push while it is.
bool isVisible();

}  // namespace stats_view
