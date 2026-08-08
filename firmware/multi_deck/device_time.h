// Wall-clock time on a board that has no battery-backed RTC.
//
// The agent sends a `time` frame on connect and once a minute after, and this holds the last
// one plus the millis() at which it arrived. Everything in between is arithmetic.
//
// Deliberately reports invalid until the first sync rather than defaulting to an epoch date.
// A clock showing 01:00 and a calendar sitting on January 1970 both look like bugs, and both
// would send you looking in the wrong place — "waiting for the PC" is the truth and says what
// to do about it.
//
// Accuracy: the ESP32's crystal drifts around 20ppm, so roughly 1.7 seconds a day. The minute
// re-sync makes that irrelevant; it matters only across a long spell with the agent closed.
#pragma once

#include <Arduino.h>

#include <time.h>

namespace device_time {

// `epoch_utc` is seconds since 1970 UTC; `tz_offset_min` is the local offset in minutes,
// positive east of Greenwich. The offset is supplied rather than derived because the device
// has no timezone database and no way to know about daylight saving.
void set(int64_t epoch_utc, int tz_offset_min);

// False until the first `time` frame arrives. Callers must render something honest instead.
bool valid();

// True when the local minute has changed since the last call with `consume` set — the cheap
// way to redraw a clock only when it would actually differ.
bool minuteChanged(bool consume);

// Local time, decomposed. Returns false when no sync has happened.
bool localTm(struct tm &out);

// Local seconds since the epoch, for date arithmetic. Zero when invalid.
int64_t localEpoch();

}  // namespace device_time
