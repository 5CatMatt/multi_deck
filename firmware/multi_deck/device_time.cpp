#include "device_time.h"

#include "config.h"

namespace device_time {
namespace {

// Local epoch at the moment of the last sync, and the millis() reading that went with it.
// Storing the local value rather than UTC plus an offset means every reader does one
// subtraction and no timezone arithmetic.
int64_t g_local_at_sync = 0;
uint32_t g_millis_at_sync = 0;
bool g_valid = false;

int64_t g_last_reported_minute = -1;

}  // namespace

void set(int64_t epoch_utc, int tz_offset_min) {
  const bool first = !g_valid;

  g_local_at_sync = epoch_utc + static_cast<int64_t>(tz_offset_min) * 60;
  g_millis_at_sync = millis();
  g_valid = true;

  // Only the first sync is logged in full. The rest are a minute apart forever, and a line a
  // minute would drown the log this project is debugged from.
  if (first) {
    struct tm now;
    if (localTm(now)) {
      MD_LOG.printf("[time] synced: %04d-%02d-%02d %02d:%02d (UTC%+d:%02d)\n",
                    now.tm_year + 1900, now.tm_mon + 1, now.tm_mday, now.tm_hour, now.tm_min,
                    tz_offset_min / 60, abs(tz_offset_min) % 60);
    } else {
      // set() cannot fail, so this means the epoch itself was unusable — a frame carrying 0,
      // typically, which is what `frame["epoch"] | 0LL` yields when the field is missing or
      // arrived as something other than a number.
      MD_LOG.printf("[time] sync rejected: epoch %lld is not a usable date\n",
                    static_cast<long long>(epoch_utc));
    }
  }
}

bool valid() { return g_valid; }

int64_t localEpoch() {
  if (!g_valid) return 0;

  // Unsigned subtraction, so the 49.7-day millis() rollover comes out right rather than as a
  // jump backwards of about seven weeks.
  const uint32_t elapsed_ms = millis() - g_millis_at_sync;
  return g_local_at_sync + static_cast<int64_t>(elapsed_ms / 1000);
}

bool localTm(struct tm &out) {
  if (!g_valid) return false;

  // gmtime_r on an already-localised epoch: the offset was folded in at sync time, so this
  // decomposes without consulting a timezone the device does not have.
  const time_t local = static_cast<time_t>(localEpoch());
  return gmtime_r(&local, &out) != nullptr;
}

bool minuteChanged(bool consume) {
  if (!g_valid) return false;

  const int64_t minute = localEpoch() / 60;
  if (minute == g_last_reported_minute) return false;

  if (consume) g_last_reported_minute = minute;
  return true;
}

}  // namespace device_time
