// Loads MDI1 images from the SD card into PSRAM and hands LVGL a descriptor for them.
//
// MDI1 is a bare RGB565 blob behind a four-field header — see tools/make_assets.py. There is
// no decode step, so "loading" is a read straight into PSRAM and a struct to point at it.
// That matters here: decoding a PNG at page-build time would cost both time and internal heap,
// and the card has 32GB spare.
#pragma once

#include <lvgl.h>

#include <Arduino.h>

namespace assets {

// Returns a descriptor owned by this module, or nullptr if the file is missing or malformed.
// Repeated calls for the same path return the same descriptor rather than re-reading.
const lv_image_dsc_t *load(const String &path);

// Frees everything. Call before switching themes: a wallpaper is 750KB, and holding several
// is a waste of the PSRAM the framebuffers care about.
void clear();

// Bytes currently held, for the log line on a theme switch.
size_t bytesHeld();

// Why the last load() returned nullptr, phrased for a toast. There are several distinct ways
// to end up with no image — no card, wrong path, wrong format, no PSRAM — and they need
// different fixes, so "it did not work" is not a useful thing to put on screen.
const String &lastError();

// The card's asset generation, read from /assets.ver, or "" if the card carries no stamp.
//
// Purely a value to carry: the device hashes nothing and compares nothing. It repeats this in
// `hello` and the agent, which has the originals, decides whether the card is current. Read
// once — the card is not swapped while running.
const String &stamp();

}  // namespace assets
