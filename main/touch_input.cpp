// SPDX-License-Identifier: MIT

#include "touch_input.h"

#include "board_config.h"
#include "board_i2c.h"
#include "esp_check.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_touch.h"
#include "esp_lcd_touch_cst816s.h"
#include "esp_log.h"

namespace touch_input {
namespace {

constexpr char kTag[] = "touch";

// The CST816S powers down its I2C block when it drops into auto-sleep, which
// makes every poll fail with a NACK until the next physical touch wakes it.
// The vendor reference driver disables auto-sleep (register 0xFE) right after
// the reset; the Espressif component never does.
constexpr uint8_t kDisAutoSleepRegister = 0xFE;
constexpr uint8_t kDisAutoSleepValue = 10;

esp_lcd_touch_handle_t s_touch = nullptr;

}  // namespace

esp_err_t init() {
  if (s_touch != nullptr) return ESP_OK;
  ESP_RETURN_ON_ERROR(board_i2c::init(), kTag, "i2c init");

  const esp_lcd_touch_config_t touchConfig = {
      .x_max = BOARD_LCD_H_RES,
      .y_max = BOARD_LCD_V_RES,
      .rst_gpio_num = BOARD_TOUCH_RST,
      .int_gpio_num = BOARD_TOUCH_INT,
      .levels =
          {
              .reset = 0,
              .interrupt = 0,
          },
      .flags =
          {
              .swap_xy = 0,
              .mirror_x = 0,
              .mirror_y = 0,
          },
  };

  esp_lcd_panel_io_handle_t ioHandle = nullptr;
  // ESP_LCD_TOUCH_IO_I2C_CST816S_CONFIG() is written against the IDF 5.5 field
  // order for esp_lcd_panel_io_i2c_config_t, which puts scl_speed_hz last in
  // 5.4. Build the same configuration explicitly so the designators match.
  const esp_lcd_panel_io_i2c_config_t ioConfig = {
      .dev_addr = ESP_LCD_TOUCH_IO_I2C_CST816S_ADDRESS,
      .on_color_trans_done = nullptr,
      .user_ctx = nullptr,
      .control_phase_bytes = 1,
      .dc_bit_offset = 0,
      .lcd_cmd_bits = 8,
      .lcd_param_bits = 0,
      .flags =
          {
              .dc_low_on_data = 0,
              .disable_control_phase = 1,
          },
      .scl_speed_hz = 400 * 1000,
  };
  ESP_RETURN_ON_ERROR(
      esp_lcd_new_panel_io_i2c(board_i2c::bus(), &ioConfig, &ioHandle), kTag,
      "touch panel io");
  ESP_RETURN_ON_ERROR(
      esp_lcd_touch_new_i2c_cst816s(ioHandle, &touchConfig, &s_touch), kTag,
      "touch create");

  // Keep the controller awake so polling stays reliable between touches.
  const esp_err_t sleepResult = board_i2c::writeRegister(
      ESP_LCD_TOUCH_IO_I2C_CST816S_ADDRESS, kDisAutoSleepRegister,
      kDisAutoSleepValue);
  if (sleepResult != ESP_OK) {
    ESP_LOGW(kTag, "disable auto-sleep failed: %s",
             esp_err_to_name(sleepResult));
  } else {
    ESP_LOGI(kTag, "CST816S ready, auto-sleep disabled");
  }
  return ESP_OK;
}

Sample read() {
  Sample sample;
  if (s_touch == nullptr) return sample;
  if (esp_lcd_touch_read_data(s_touch) != ESP_OK) return sample;

  uint16_t x = 0;
  uint16_t y = 0;
  uint8_t points = 0;
  const bool pressed = esp_lcd_touch_get_coordinates(s_touch, &x, &y, nullptr,
                                                     &points, 1);
  if (!pressed || points == 0) return sample;

  sample.pressed = true;
  sample.x = x;
  sample.y = y;
  return sample;
}

}  // namespace touch_input
