/**
 * @file esp_panel_board_supported_conf.h
 * @brief Selects the Waveshare ESP32-S3-Touch-LCD-4.3 board profile.
 *
 * ESP32_Display_Panel looks for this file with:
 *
 *     #if   __has_include("esp_panel_board_supported_conf.h")        <- sketch folder (this)
 *     #elif __has_include("../../../esp_panel_board_supported_conf.h") <- library root
 *
 * so a copy here takes precedence over the one shipped in the library. Keeping it in the
 * sketch means the board selection is version-controlled with the project and survives an
 * ESP32_Display_Panel upgrade, which would otherwise silently revert an edit made in the
 * library folder. The vendor's own examples each carry a local copy for the same reason.
 *
 * This is a minimal file rather than a copy of the library's full board list — only the
 * selected board and the version stamp are needed.
 */
#pragma once

#define ESP_PANEL_BOARD_DEFAULT_USE_SUPPORTED (1)

#if ESP_PANEL_BOARD_DEFAULT_USE_SUPPORTED

// Exactly one board may be enabled; more than one is a compile error.
// Note this is the plain 4.3, not the _B variant, which has different hardware.
#define BOARD_WAVESHARE_ESP32_S3_TOUCH_LCD_4_3

////////////////////////////////////////////////////////////////////////////////////////////
///////////////////////////////////// File Version /////////////////////////////////////////
////////////////////////////////////////////////////////////////////////////////////////////
/**
 * Must match the library's expected version in src/esp_panel_versions.h. A major mismatch is
 * an incompatibility; a minor mismatch means this file may be missing newer options.
 * Checked against ESP32_Display_Panel v1.0.4 (supported-conf version 1.2.0).
 */
#define ESP_PANEL_BOARD_SUPPORTED_FILE_VERSION_MAJOR 1
#define ESP_PANEL_BOARD_SUPPORTED_FILE_VERSION_MINOR 2
#define ESP_PANEL_BOARD_SUPPORTED_FILE_VERSION_PATCH 0

#endif
