// USB HID output — the standalone half of the deck.
//
// Everything here works with the PC agent closed, because these are genuine HID keycodes
// rather than synthesised input. That is what lets the ten-key reach elevated windows,
// full-screen games and the BIOS.
#pragma once

#include <Arduino.h>

#include <vector>

namespace deck_hid {

// Registers the keyboard and consumer-control devices and starts USB. Must be called before
// USB.begin() elsewhere — it calls it itself.
bool begin();

// Presses a chord such as {"CTRL","SHIFT","ESC"} and releases it. Unknown tokens are logged
// and the chord is rejected, rather than silently sending a partial combination.
bool sendCombo(const std::vector<String> &tokens);

// Types a literal string using the ASCII keymap.
void typeText(const String &text);

// Consumer-control keys: play_pause, next, prev, stop, mute, vol_up, vol_down.
bool sendMedia(const String &key);

// Sends one raw HID usage with modifier bits, then releases. Used by the numpad page, which
// wants specific keypad usages rather than characters.
void sendUsage(uint8_t modifiers, uint8_t usage);

// Holds a usage down without releasing, for key repeat. Pair with releaseAll().
void holdUsage(uint8_t modifiers, uint8_t usage);
void releaseAll();

// Resolves a token from deck.json to a HID usage and/or modifier bit. Returns false if the
// token is not recognised.
bool resolveToken(const String &token, uint8_t &usage, uint8_t &modifier_bit);

// Host NumLock LED state, learned from the HID output report. Keypad usages only produce
// digits when this is true, so the numpad page surfaces it.
bool numLockOn();

}  // namespace deck_hid
