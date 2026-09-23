// SPDX-License-Identifier: MIT
// Pure battery decisions shared by the gauge driver and dashboard.

#pragma once

#include <cstdint>

namespace battery_logic {

constexpr int kChargeCurrentThresholdMa = 20;

constexpr int clampPercent(int percent) {
  return percent < 0 ? -1 : (percent > 100 ? 100 : percent);
}

// Five inclusive bands: 0..20, 21..40, 41..60, 61..80, 81..100.
constexpr int iconLevel(int percent) {
  const int clamped = clampPercent(percent);
  return clamped <= 0 ? 1 : (clamped + 19) / 20;
}

constexpr bool isCharging(int currentMa, bool discharging) {
  return !discharging && currentMa >= kChargeCurrentThresholdMa;
}

constexpr bool hasExternalPower(bool usbHostConnected, bool gaugeValid,
                                bool discharging) {
  return usbHostConnected || (gaugeValid && !discharging);
}

// Boundary and truth-table regression checks compile with the firmware.
static_assert(iconLevel(-1) == 1);
static_assert(iconLevel(0) == 1);
static_assert(iconLevel(1) == 1);
static_assert(iconLevel(20) == 1);
static_assert(iconLevel(21) == 2);
static_assert(iconLevel(40) == 2);
static_assert(iconLevel(41) == 3);
static_assert(iconLevel(60) == 3);
static_assert(iconLevel(61) == 4);
static_assert(iconLevel(80) == 4);
static_assert(iconLevel(81) == 5);
static_assert(iconLevel(100) == 5);
static_assert(iconLevel(101) == 5);

static_assert(hasExternalPower(true, false, true));
static_assert(hasExternalPower(true, true, true));
static_assert(hasExternalPower(false, true, false));
static_assert(!hasExternalPower(false, true, true));
static_assert(!hasExternalPower(false, false, false));
static_assert(isCharging(20, false));
static_assert(!isCharging(19, false));
static_assert(!isCharging(20, true));

}  // namespace battery_logic
