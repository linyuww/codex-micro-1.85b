// SPDX-License-Identifier: MIT
// ST77916 QSPI panel + backlight control for the Waveshare 1.85B.

#pragma once

#include <cstdint>

#include "esp_err.h"
#include "esp_lcd_types.h"

namespace board_display {

// Resets, initialises and powers on the panel. The backlight stays off until
// set_brightness() is called.
esp_err_t init();

// percent is clamped to 0..100. 0 turns the backlight fully off.
void set_brightness(int percent);
int brightness();

// Panel sleep-in plus backlight off. The framebuffer keeps its contents.
void sleep();
void wake();

// Pushes the whole 360x360 RGB565 framebuffer. The buffer must already be in
// panel byte order (high byte first).
void flush(const uint16_t* framebuffer);

esp_lcd_panel_handle_t panel();
esp_lcd_panel_io_handle_t panel_io();

}  // namespace board_display
