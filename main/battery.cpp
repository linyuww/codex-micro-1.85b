// SPDX-License-Identifier: MIT

#include "battery.h"

#include <cstring>

#include "battery_logic.h"
#include "board_config.h"
#include "board_i2c.h"
#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "esp_timer.h"

namespace battery {
namespace {

constexpr char kTag[] = "battery";

// Standard command registers. The gauge serves 0x00..0x3F as one readable
// block, so a single transaction refreshes every field we care about.
constexpr uint8_t kRegVoltage = 0x08;
constexpr uint8_t kRegBatteryStatus = 0x0A;
constexpr uint8_t kRegCurrent = 0x0C;
constexpr uint8_t kRegFullChargeCapacity = 0x12;
constexpr uint8_t kRegStateOfCharge = 0x2C;
constexpr uint8_t kBlockStart = 0x00;
constexpr size_t kBlockLength = 0x40;

// BatteryStatus bit 0 (DSG) is set while the pack is discharging.
constexpr uint16_t kStatusDischarging = 0x0001;

// Refresh cadence. The gauge updates its own SOC once per second, so anything
// faster only burns I2C bandwidth.
constexpr uint32_t kRefreshIntervalMs = 5000;

// A plausible single-cell Li-ion reading. Anything outside means the gauge is
// not populated (or not answering) on this board revision.
constexpr uint16_t kMinPlausibleMv = 2000;
constexpr uint16_t kMaxPlausibleMv = 4600;

Sample s_sample;
uint32_t s_lastReadMs = 0;
bool s_probed = false;

void refreshExternalPower() {
  s_sample.externalPower = battery_logic::hasExternalPower(
      usb_serial_jtag_is_connected(), s_sample.valid, s_sample.discharging);
}

inline uint16_t littleEndian16(const uint8_t* p) {
  return static_cast<uint16_t>(p[0] | (p[1] << 8));
}

inline int16_t littleEndianS16(const uint8_t* p) {
  return static_cast<int16_t>(littleEndian16(p));
}

bool readBlock(uint8_t* block) {
  return board_i2c::readRegister(BOARD_I2C_ADDR_BQ27220, kBlockStart, block,
                                 kBlockLength) == ESP_OK;
}

}  // namespace

esp_err_t init() {
  uint8_t block[kBlockLength] = {};
  if (!readBlock(block)) {
    ESP_LOGW(kTag, "BQ27220 did not answer at 0x%02X",
             BOARD_I2C_ADDR_BQ27220);
    return ESP_ERR_NOT_FOUND;
  }
  const uint16_t voltage = littleEndian16(block + kRegVoltage);
  if (voltage < kMinPlausibleMv || voltage > kMaxPlausibleMv) {
    ESP_LOGW(kTag, "BQ27220 reported implausible voltage %u mV", voltage);
    return ESP_ERR_INVALID_RESPONSE;
  }
  s_probed = true;
  ESP_LOGI(kTag, "BQ27220 online voltage=%u mV", voltage);
  return ESP_OK;
}

Sample read(uint32_t nowMs) {
  if (!s_probed) {
    s_probed = true;
    if (init() != ESP_OK) {
      // Leave percent at -1 so the dashboard renders "--%".
      return s_sample;
    }
    s_lastReadMs = 0;
  }

  if (s_lastReadMs != 0 && nowMs - s_lastReadMs < kRefreshIntervalMs) {
    // USB SOF presence can change between gauge refreshes, so never cache it.
    refreshExternalPower();
    return s_sample;
  }

  uint8_t block[kBlockLength] = {};
  if (!readBlock(block)) {
    ESP_LOGW(kTag, "BQ27220 read failed; keeping last sample");
    s_lastReadMs = nowMs;
    return s_sample;
  }

  const uint16_t voltage = littleEndian16(block + kRegVoltage);
  if (voltage < kMinPlausibleMv || voltage > kMaxPlausibleMv) {
    ESP_LOGW(kTag, "BQ27220 voltage out of range (%u mV); ignoring", voltage);
    s_lastReadMs = nowMs;
    return s_sample;
  }

  const uint16_t status = littleEndian16(block + kRegBatteryStatus);
  const int16_t current = littleEndianS16(block + kRegCurrent);
  const uint16_t soc = littleEndian16(block + kRegStateOfCharge);

  s_sample.voltageMv = voltage;
  s_sample.currentMa = current;
  s_sample.percent = soc <= 100 ? static_cast<int>(soc) : -1;
  s_sample.discharging = (status & kStatusDischarging) != 0;
  s_sample.charging =
      battery_logic::isCharging(current, s_sample.discharging);
  s_sample.fullChargeCapacityMah =
      littleEndian16(block + kRegFullChargeCapacity);
  s_sample.valid = s_sample.percent >= 0;
  refreshExternalPower();

  s_lastReadMs = nowMs;
  ESP_LOGD(kTag, "soc=%d%% %u mV %d mA dsg=%d charging=%d external=%d",
           s_sample.percent, voltage, current, s_sample.discharging ? 1 : 0,
           s_sample.charging ? 1 : 0, s_sample.externalPower ? 1 : 0);
  return s_sample;
}

const Sample& cached() { return s_sample; }

}  // namespace battery
