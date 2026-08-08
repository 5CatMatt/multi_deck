// multi_deck — touchscreen deck companion for Windows.
//
// Board: Waveshare ESP32-S3-Touch-LCD-4.3 (ESP32-S3-WROOM-1-N8R8 — 8MB flash, 8MB PSRAM;
// the vendor spec page claims N16R8/16MB and is wrong, see docs/hardware-notes.md).
// See docs/hardware-notes.md for the Arduino IDE Tools settings — they are fiddly and
// getting them wrong produces confusing failures rather than obvious ones.
//
// Two USB ports are in play:
//   port A (USB TO UART, CH343P) — flashing and the `Serial` debug log
//   port B (USB, native)         — composite HID keyboard + CDC link to the agent

#include <ArduinoJson.h>

#include "assets.h"
#include "board_port.h"
#include "config.h"
#include "deck_config.h"
#include "device_time.h"
#include "hid.h"
#include "link.h"
#include "lvgl_v9_port.h"
#include "sleep_view.h"
#include "stats_view.h"
#include "ui_builder.h"

namespace {

DeckConfig g_config;
UsbLink g_link;
bool g_last_link_up = false;

void handleLayoutPush(JsonObjectConst frame) {
  JsonObjectConst data = frame["data"];
  if (data.isNull()) {
    MD_LOG.println("[main] layout frame carried no data");
    return;
  }

  String raw;
  serializeJson(data, raw);

  if (!DeckConfig::writeToSd(MD_DECK_JSON_PATH, raw.c_str(), raw.length())) {
    MD_LOG.println("[main] could not persist pushed layout — using it in memory only");
  }

  // The UI holds pointers into g_config.pages; drop them before parse() invalidates them.
  ui::releaseConfigReferences();

  if (!g_config.parse(data)) {
    MD_LOG.println("[main] pushed layout would not parse — keeping the previous one");
    // The screen is bare at this point, so put the previous layout back up.
    g_config.loadFallback();
    ui::rebuild();
    return;
  }

  g_link.setDeviceRev(g_config.rev);
  ui::rebuild();
  ui::toast("Layout updated");
}

void handleHidExec(JsonObjectConst frame) {
  JsonObjectConst action_json = frame["action"];
  if (action_json.isNull()) return;

  // The agent sequences mixed macros and calls back here for the device-local steps, so
  // ordering across the boundary stays correct.
  Action action;
  parseActionJson(action_json, action);

  if (!action.isLocal()) {
    MD_LOG.println("[main] hid_exec carried an action the device cannot run");
    return;
  }

  ui::executeLocalAction(action);
}

void onFrame(JsonObjectConst frame) {
  const char *type = frame["t"] | "";

  if (strcmp(type, "stats") == 0) {
    stats_view::update(frame);
  } else if (strcmp(type, "layout") == 0) {
    handleLayoutPush(frame);
  } else if (strcmp(type, "hid_exec") == 0) {
    handleHidExec(frame);
  } else if (strcmp(type, "toast") == 0) {
    ui::toast(String(frame["msg"] | ""));
  } else if (strcmp(type, "backlight") == 0) {
    board_port::setBacklight(frame["v"] | 80);
  } else if (strcmp(type, "time") == 0) {
    device_time::set(frame["epoch"] | 0LL, frame["tz_min"] | 0);
  } else if (strcmp(type, "power") == 0) {
    const char *state = frame["state"] | "";
    if (strcmp(state, "sleep") == 0) {
      ui::enterSleep();
    } else {
      ui::leaveSleep();
    }
  } else {
    MD_LOG.printf("[main] unhandled frame '%s'\n", type);
  }
}

}  // namespace

void setup() {
  MD_LOG.begin(MD_DEBUG_BAUD);  // UART0, port A — independent of the HID/CDC link
  delay(200);
  MD_LOG.println("\n[main] multi_deck " MD_FW_VERSION);

  if (!board_port::begin()) {
    MD_LOG.println("[main] panel bring-up failed — stopping");
    while (true) delay(1000);
  }

  // USB device order matters: every interface must be registered before USB.begin(), which
  // deck_hid::begin() calls last. CDC first, then HID, or the composite descriptor comes out
  // missing the serial interface.
  g_link.begin();
  deck_hid::begin();

  if (board_port::sdBegin()) {
    if (!g_config.loadFromSd(MD_DECK_JSON_PATH)) {
      g_config.loadFallback();
    }
    // After the layout, so there is a theme list to match the saved name against.
    g_config.loadPersistedTheme(MD_THEME_STATE_PATH);

    // Only inside this branch: with no card there is nothing to report a generation for, and
    // an empty stamp would read as "this card is unstamped" rather than "there is no card".
    const String &stamp = assets::stamp();
    g_link.setAssetStamp(stamp);
    MD_LOG.printf("[assets] card stamp %s\n", stamp.isEmpty() ? "(none)" : stamp.c_str());
  } else {
    g_config.loadFallback();
  }

  if (!lvgl_port::begin()) {
    MD_LOG.println("[main] LVGL bring-up failed — stopping");
    while (true) delay(1000);
  }

  ui::begin(&g_config, &g_link);

  g_link.setDeviceRev(g_config.rev);
  g_link.setFrameHandler(onFrame);

  MD_LOG.println("[main] ready");
}

void loop() {
  lvgl_port::poll();
  g_link.poll();

  const bool up = g_link.isUp();
  if (up != g_last_link_up) {
    g_last_link_up = up;
    ui::setLinkUp(up);

    // Layouts disagree? Ask for a push. The agent is authoritative.
    if (up && g_link.hostRev() >= 0 && g_link.hostRev() != g_config.rev) {
      MD_LOG.printf("[main] layout rev %d here, %d on host — requesting push\n",
                    g_config.rev, g_link.hostRev());
      g_link.sendLayoutRequest();
    }
  }

  ui::tick();
  delay(2);
}
