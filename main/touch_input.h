// SPDX-License-Identifier: MIT
// CST816S capacitive touch input for the Waveshare 1.85B.

#pragma once

#include "esp_err.h"

namespace touch_input {

struct Sample {
  bool pressed = false;
  int x = 0;
  int y = 0;
};

esp_err_t init();

// Reads the controller once. Poll at a few tens of Hz.
Sample read();

}  // namespace touch_input
