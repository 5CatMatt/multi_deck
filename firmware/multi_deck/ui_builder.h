// Builds the LVGL screen from a DeckConfig and routes touches to actions.
#pragma once

#include <ArduinoJson.h>

#include "deck_config.h"
#include "link.h"

namespace ui {

bool begin(DeckConfig *config, Link *link);

// Tears down and rebuilds the whole screen. Called after a layout push.
void rebuild();

// Drops every pointer the UI holds into the DeckConfig. Must be called *before* reparsing a
// layout: tile bindings hold `const Button*` into DeckConfig::pages, and parse() clears that
// vector, so those pointers dangle until the subsequent rebuild().
void releaseConfigReferences();

void showPage(const String &id);

// Switches theme and persists the choice. `target` is "next", "prev", or a theme name.
// Device-local, so it works with the agent closed.
void switchTheme(const String &target);

// Greys out tiles whose actions need the agent. Device-local tiles stay live.
void setLinkUp(bool up);

void toast(const String &message);

// Handles a `hid_exec` frame: the agent sequencing a mixed macro asks us to perform a
// device-local step.
void executeLocalAction(const Action &action);

// Per-loop housekeeping: idle dimming and the toast timer.
void tick();

}  // namespace ui
