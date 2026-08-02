#include "hid.h"

#include <USB.h>
#include <USBHIDConsumerControl.h>
#include <USBHIDKeyboard.h>

#include "config.h"

namespace deck_hid {
namespace {

USBHIDKeyboard g_keyboard;
USBHIDConsumerControl g_consumer;

volatile bool g_num_lock = false;

// Modifier bits in the HID keyboard report.
constexpr uint8_t MOD_CTRL = 0x01;
constexpr uint8_t MOD_SHIFT = 0x02;
constexpr uint8_t MOD_ALT = 0x04;
constexpr uint8_t MOD_GUI = 0x08;
constexpr uint8_t MOD_RALT = 0x40;

// Consumer page usages. Passed as raw values so we do not depend on the core's macro names.
constexpr uint16_t CC_PLAY_PAUSE = 0x00CD;
constexpr uint16_t CC_SCAN_NEXT = 0x00B5;
constexpr uint16_t CC_SCAN_PREV = 0x00B6;
constexpr uint16_t CC_STOP = 0x00B7;
constexpr uint16_t CC_MUTE = 0x00E2;
constexpr uint16_t CC_VOL_UP = 0x00E9;
constexpr uint16_t CC_VOL_DOWN = 0x00EA;

struct NamedUsage {
  const char *name;
  uint8_t usage;
};

// Keyboard/Keypad usage page (0x07). These values are fixed by the USB HID spec, which is
// why building reports by hand is more durable here than chasing library #defines.
const NamedUsage kNamedKeys[] = {
    {"ENTER", 0x28},     {"RETURN", 0x28},   {"ESC", 0x29},      {"ESCAPE", 0x29},
    {"BACKSPACE", 0x2A}, {"TAB", 0x2B},      {"SPACE", 0x2C},    {"MINUS", 0x2D},
    {"EQUAL", 0x2E},     {"LBRACKET", 0x2F}, {"RBRACKET", 0x30}, {"BACKSLASH", 0x31},
    {"SEMICOLON", 0x33}, {"QUOTE", 0x34},    {"GRAVE", 0x35},    {"COMMA", 0x36},
    {"PERIOD", 0x37},    {"SLASH", 0x38},    {"CAPSLOCK", 0x39},

    {"F1", 0x3A},        {"F2", 0x3B},       {"F3", 0x3C},       {"F4", 0x3D},
    {"F5", 0x3E},        {"F6", 0x3F},       {"F7", 0x40},       {"F8", 0x41},
    {"F9", 0x42},        {"F10", 0x43},      {"F11", 0x44},      {"F12", 0x45},

    {"PRINTSCREEN", 0x46}, {"SCROLLLOCK", 0x47}, {"PAUSE", 0x48}, {"INSERT", 0x49},
    {"HOME", 0x4A},      {"PAGEUP", 0x4B},   {"DELETE", 0x4C},   {"END", 0x4D},
    {"PAGEDOWN", 0x4E},  {"RIGHT", 0x4F},    {"LEFT", 0x50},     {"DOWN", 0x51},
    {"UP", 0x52},        {"MENU", 0x65},

    // Keypad. The ten-key page uses these rather than the number-row usages, so the host
    // sees a real numeric keypad.
    {"NUMLOCK", 0x53},   {"KP_SLASH", 0x54}, {"KP_ASTERISK", 0x55}, {"KP_MINUS", 0x56},
    {"KP_PLUS", 0x57},   {"KP_ENTER", 0x58}, {"KP_1", 0x59},     {"KP_2", 0x5A},
    {"KP_3", 0x5B},      {"KP_4", 0x5C},     {"KP_5", 0x5D},     {"KP_6", 0x5E},
    {"KP_7", 0x5F},      {"KP_8", 0x60},     {"KP_9", 0x61},     {"KP_0", 0x62},
    {"KP_DOT", 0x63},
};

struct NamedModifier {
  const char *name;
  uint8_t bit;
};

const NamedModifier kNamedModifiers[] = {
    {"CTRL", MOD_CTRL},   {"CONTROL", MOD_CTRL}, {"SHIFT", MOD_SHIFT},
    {"ALT", MOD_ALT},     {"GUI", MOD_GUI},      {"WIN", MOD_GUI},
    {"CMD", MOD_GUI},     {"ALTGR", MOD_RALT},
};

void onUsbHidEvent(void *, esp_event_base_t base, int32_t id, void *event_data) {
  if (base != ARDUINO_USB_HID_KEYBOARD_EVENTS) return;
  if (id != ARDUINO_USB_HID_KEYBOARD_LED_EVENT) return;

  auto *data = static_cast<arduino_usb_hid_keyboard_event_data_t *>(event_data);
  g_num_lock = data->numlock;
}

}  // namespace

bool begin() {
  g_keyboard.begin();
  g_consumer.begin();

  // The host tells us its lock-key state through the HID output report. Without this the
  // numpad cannot warn that NumLock is off, and keypad digits would silently act as arrows.
  g_keyboard.onEvent(onUsbHidEvent);

  USB.productName(MD_DEVICE_NAME);
  USB.manufacturerName("multi_deck");
  USB.begin();

  MD_LOG.println("[hid] keyboard + consumer control started");
  return true;
}

bool resolveToken(const String &token, uint8_t &usage, uint8_t &modifier_bit) {
  usage = 0;
  modifier_bit = 0;

  String upper = token;
  upper.toUpperCase();

  for (const auto &mod : kNamedModifiers) {
    if (upper == mod.name) {
      modifier_bit = mod.bit;
      return true;
    }
  }

  for (const auto &key : kNamedKeys) {
    if (upper == key.name) {
      usage = key.usage;
      return true;
    }
  }

  // Single characters map arithmetically onto the usage page.
  if (token.length() == 1) {
    const char c = token[0];
    if (c >= 'a' && c <= 'z') {
      usage = 0x04 + (c - 'a');
      return true;
    }
    if (c >= 'A' && c <= 'Z') {
      usage = 0x04 + (c - 'A');
      modifier_bit = MOD_SHIFT;
      return true;
    }
    if (c >= '1' && c <= '9') {
      usage = 0x1E + (c - '1');
      return true;
    }
    if (c == '0') {
      usage = 0x27;
      return true;
    }
  }

  return false;
}

void holdUsage(uint8_t modifiers, uint8_t usage) {
  KeyReport report = {};
  report.modifiers = modifiers;
  report.keys[0] = usage;
  g_keyboard.sendReport(&report);
}

void releaseAll() {
  KeyReport report = {};
  g_keyboard.sendReport(&report);
}

void sendUsage(uint8_t modifiers, uint8_t usage) {
  holdUsage(modifiers, usage);
  delay(8);  // brief hold so the host registers a distinct keypress
  releaseAll();
}

bool sendCombo(const std::vector<String> &tokens) {
  uint8_t modifiers = 0;
  uint8_t keys[6] = {};
  size_t key_count = 0;

  for (const auto &token : tokens) {
    uint8_t usage = 0;
    uint8_t modifier_bit = 0;

    if (!resolveToken(token, usage, modifier_bit)) {
      MD_LOG.printf("[hid] unknown key token '%s' — chord rejected\n", token.c_str());
      return false;
    }

    if (modifier_bit != 0) {
      modifiers |= modifier_bit;
    } else if (key_count < 6) {
      keys[key_count++] = usage;
    } else {
      MD_LOG.println("[hid] more than 6 non-modifier keys — chord rejected");
      return false;
    }
  }

  KeyReport report = {};
  report.modifiers = modifiers;
  for (size_t i = 0; i < key_count; i++) report.keys[i] = keys[i];

  g_keyboard.sendReport(&report);
  delay(8);
  releaseAll();
  return true;
}

void typeText(const String &text) { g_keyboard.print(text); }

bool sendMedia(const String &key) {
  uint16_t usage = 0;

  if (key == "play_pause") {
    usage = CC_PLAY_PAUSE;
  } else if (key == "next") {
    usage = CC_SCAN_NEXT;
  } else if (key == "prev") {
    usage = CC_SCAN_PREV;
  } else if (key == "stop") {
    usage = CC_STOP;
  } else if (key == "mute") {
    usage = CC_MUTE;
  } else if (key == "vol_up") {
    usage = CC_VOL_UP;
  } else if (key == "vol_down") {
    usage = CC_VOL_DOWN;
  } else {
    MD_LOG.printf("[hid] unknown media key '%s'\n", key.c_str());
    return false;
  }

  g_consumer.press(usage);
  delay(8);
  g_consumer.release();
  return true;
}

bool numLockOn() { return g_num_lock; }

}  // namespace deck_hid
