// SPDX-License-Identifier: MIT
//
// ST77916 QSPI bring-up for the Waveshare ESP32-S3-Touch-LCD-1.85B.
// The bus/IO parameters and the two vendor init tables are taken from the
// official Waveshare BSP so the panel revision detection matches their demo.

#include "display.h"

#include <cstring>

#include "board_config.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_st77916.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace board_display {
namespace {

constexpr char kTag[] = "display";

constexpr uint64_t kLcdOpcodeWriteCmd = 0x02ULL;
constexpr uint64_t kLcdOpcodeReadCmd = 0x0BULL;

// Rows pushed per DMA transaction. 60 rows keeps every transfer at 43200 bytes.
constexpr int kFlushBandRows = 60;

esp_lcd_panel_handle_t s_panel = nullptr;
esp_lcd_panel_io_handle_t s_io = nullptr;
SemaphoreHandle_t s_flushDone = nullptr;
int s_brightness = 0;
bool s_initialised = false;

// ---------------------------------------------------------------------------
// Vendor init tables (Waveshare, panel revision dependent).
// ---------------------------------------------------------------------------
const st77916_lcd_init_cmd_t kVendorInitVersion1[] = {
    {0xF0, (uint8_t[]){0x28}, 1, 0},
    {0xF2, (uint8_t[]){0x28}, 1, 0},
    {0x7C, (uint8_t[]){0xD1}, 1, 0},
    {0x83, (uint8_t[]){0xE0}, 1, 0},
    {0x84, (uint8_t[]){0x61}, 1, 0},
    {0xF2, (uint8_t[]){0x82}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0xF0, (uint8_t[]){0x01}, 1, 0},
    {0xF1, (uint8_t[]){0x01}, 1, 0},
    {0xB0, (uint8_t[]){0x49}, 1, 0},
    {0xB1, (uint8_t[]){0x4A}, 1, 0},
    {0xB2, (uint8_t[]){0x1F}, 1, 0},
    {0xB4, (uint8_t[]){0x46}, 1, 0},
    {0xB5, (uint8_t[]){0x34}, 1, 0},
    {0xB6, (uint8_t[]){0xD5}, 1, 0},
    {0xB7, (uint8_t[]){0x30}, 1, 0},
    {0xB8, (uint8_t[]){0x04}, 1, 0},
    {0xBA, (uint8_t[]){0x00}, 1, 0},
    {0xBB, (uint8_t[]){0x08}, 1, 0},
    {0xBC, (uint8_t[]){0x08}, 1, 0},
    {0xBD, (uint8_t[]){0x00}, 1, 0},
    {0xC0, (uint8_t[]){0x80}, 1, 0},
    {0xC1, (uint8_t[]){0x10}, 1, 0},
    {0xC2, (uint8_t[]){0x37}, 1, 0},
    {0xC3, (uint8_t[]){0x80}, 1, 0},
    {0xC4, (uint8_t[]){0x10}, 1, 0},
    {0xC5, (uint8_t[]){0x37}, 1, 0},
    {0xC6, (uint8_t[]){0xA9}, 1, 0},
    {0xC7, (uint8_t[]){0x41}, 1, 0},
    {0xC8, (uint8_t[]){0x01}, 1, 0},
    {0xC9, (uint8_t[]){0xA9}, 1, 0},
    {0xCA, (uint8_t[]){0x41}, 1, 0},
    {0xCB, (uint8_t[]){0x01}, 1, 0},
    {0xD0, (uint8_t[]){0x91}, 1, 0},
    {0xD1, (uint8_t[]){0x68}, 1, 0},
    {0xD2, (uint8_t[]){0x68}, 1, 0},
    {0xF5, (uint8_t[]){0x00, 0xA5}, 2, 0},
    {0xF1, (uint8_t[]){0x10}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0xF0, (uint8_t[]){0x02}, 1, 0},
    {0xE0,
     (uint8_t[]){0x70, 0x09, 0x12, 0x0C, 0x0B, 0x27, 0x38, 0x54, 0x4E, 0x19,
                 0x15, 0x15, 0x2C, 0x2F},
     14, 0},
    {0xE1,
     (uint8_t[]){0x70, 0x08, 0x11, 0x0C, 0x0B, 0x27, 0x38, 0x43, 0x4C, 0x18,
                 0x14, 0x14, 0x2B, 0x2D},
     14, 0},
    {0xF0, (uint8_t[]){0x10}, 1, 0},
    {0xF3, (uint8_t[]){0x10}, 1, 0},
    {0xE0, (uint8_t[]){0x08}, 1, 0},
    {0xE1, (uint8_t[]){0x00}, 1, 0},
    {0xE2, (uint8_t[]){0x0B}, 1, 0},
    {0xE3, (uint8_t[]){0x00}, 1, 0},
    {0xE4, (uint8_t[]){0xE0}, 1, 0},
    {0xE5, (uint8_t[]){0x06}, 1, 0},
    {0xE6, (uint8_t[]){0x21}, 1, 0},
    {0xE7, (uint8_t[]){0x00}, 1, 0},
    {0xE8, (uint8_t[]){0x05}, 1, 0},
    {0xE9, (uint8_t[]){0x82}, 1, 0},
    {0xEA, (uint8_t[]){0xDF}, 1, 0},
    {0xEB, (uint8_t[]){0x89}, 1, 0},
    {0xEC, (uint8_t[]){0x20}, 1, 0},
    {0xED, (uint8_t[]){0x14}, 1, 0},
    {0xEE, (uint8_t[]){0xFF}, 1, 0},
    {0xEF, (uint8_t[]){0x00}, 1, 0},
    {0xF8, (uint8_t[]){0xFF}, 1, 0},
    {0xF9, (uint8_t[]){0x00}, 1, 0},
    {0xFA, (uint8_t[]){0x00}, 1, 0},
    {0xFB, (uint8_t[]){0x30}, 1, 0},
    {0xFC, (uint8_t[]){0x00}, 1, 0},
    {0xFD, (uint8_t[]){0x00}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xFF, (uint8_t[]){0x00}, 1, 0},
    {0x60, (uint8_t[]){0x42}, 1, 0},
    {0x61, (uint8_t[]){0xE0}, 1, 0},
    {0x62, (uint8_t[]){0x40}, 1, 0},
    {0x63, (uint8_t[]){0x40}, 1, 0},
    {0x64, (uint8_t[]){0x02}, 1, 0},
    {0x65, (uint8_t[]){0x00}, 1, 0},
    {0x66, (uint8_t[]){0x40}, 1, 0},
    {0x67, (uint8_t[]){0x03}, 1, 0},
    {0x68, (uint8_t[]){0x00}, 1, 0},
    {0x69, (uint8_t[]){0x00}, 1, 0},
    {0x6A, (uint8_t[]){0x00}, 1, 0},
    {0x6B, (uint8_t[]){0x00}, 1, 0},
    {0x70, (uint8_t[]){0x42}, 1, 0},
    {0x71, (uint8_t[]){0xE0}, 1, 0},
    {0x72, (uint8_t[]){0x40}, 1, 0},
    {0x73, (uint8_t[]){0x40}, 1, 0},
    {0x74, (uint8_t[]){0x02}, 1, 0},
    {0x75, (uint8_t[]){0x00}, 1, 0},
    {0x76, (uint8_t[]){0x40}, 1, 0},
    {0x77, (uint8_t[]){0x03}, 1, 0},
    {0x78, (uint8_t[]){0x00}, 1, 0},
    {0x79, (uint8_t[]){0x00}, 1, 0},
    {0x7A, (uint8_t[]){0x00}, 1, 0},
    {0x7B, (uint8_t[]){0x00}, 1, 0},
    {0x80, (uint8_t[]){0x38}, 1, 0},
    {0x81, (uint8_t[]){0x00}, 1, 0},
    {0x82, (uint8_t[]){0x04}, 1, 0},
    {0x83, (uint8_t[]){0x02}, 1, 0},
    {0x84, (uint8_t[]){0xDC}, 1, 0},
    {0x85, (uint8_t[]){0x00}, 1, 0},
    {0x86, (uint8_t[]){0x00}, 1, 0},
    {0x87, (uint8_t[]){0x00}, 1, 0},
    {0x88, (uint8_t[]){0x38}, 1, 0},
    {0x89, (uint8_t[]){0x00}, 1, 0},
    {0x8A, (uint8_t[]){0x06}, 1, 0},
    {0x8B, (uint8_t[]){0x02}, 1, 0},
    {0x8C, (uint8_t[]){0xDE}, 1, 0},
    {0x8D, (uint8_t[]){0x00}, 1, 0},
    {0x8E, (uint8_t[]){0x00}, 1, 0},
    {0x8F, (uint8_t[]){0x00}, 1, 0},
    {0x90, (uint8_t[]){0x38}, 1, 0},
    {0x91, (uint8_t[]){0x00}, 1, 0},
    {0x92, (uint8_t[]){0x08}, 1, 0},
    {0x93, (uint8_t[]){0x02}, 1, 0},
    {0x94, (uint8_t[]){0xE0}, 1, 0},
    {0x95, (uint8_t[]){0x00}, 1, 0},
    {0x96, (uint8_t[]){0x00}, 1, 0},
    {0x97, (uint8_t[]){0x00}, 1, 0},
    {0x98, (uint8_t[]){0x38}, 1, 0},
    {0x99, (uint8_t[]){0x00}, 1, 0},
    {0x9A, (uint8_t[]){0x0A}, 1, 0},
    {0x9B, (uint8_t[]){0x02}, 1, 0},
    {0x9C, (uint8_t[]){0xE2}, 1, 0},
    {0x9D, (uint8_t[]){0x00}, 1, 0},
    {0x9E, (uint8_t[]){0x00}, 1, 0},
    {0x9F, (uint8_t[]){0x00}, 1, 0},
    {0xA0, (uint8_t[]){0x38}, 1, 0},
    {0xA1, (uint8_t[]){0x00}, 1, 0},
    {0xA2, (uint8_t[]){0x03}, 1, 0},
    {0xA3, (uint8_t[]){0x02}, 1, 0},
    {0xA4, (uint8_t[]){0xDB}, 1, 0},
    {0xA5, (uint8_t[]){0x00}, 1, 0},
    {0xA6, (uint8_t[]){0x00}, 1, 0},
    {0xA7, (uint8_t[]){0x00}, 1, 0},
    {0xA8, (uint8_t[]){0x38}, 1, 0},
    {0xA9, (uint8_t[]){0x00}, 1, 0},
    {0xAA, (uint8_t[]){0x05}, 1, 0},
    {0xAB, (uint8_t[]){0x02}, 1, 0},
    {0xAC, (uint8_t[]){0xDD}, 1, 0},
    {0xAD, (uint8_t[]){0x00}, 1, 0},
    {0xAE, (uint8_t[]){0x00}, 1, 0},
    {0xAF, (uint8_t[]){0x00}, 1, 0},
    {0xB0, (uint8_t[]){0x38}, 1, 0},
    {0xB1, (uint8_t[]){0x00}, 1, 0},
    {0xB2, (uint8_t[]){0x07}, 1, 0},
    {0xB3, (uint8_t[]){0x02}, 1, 0},
    {0xB4, (uint8_t[]){0xDF}, 1, 0},
    {0xB5, (uint8_t[]){0x00}, 1, 0},
    {0xB6, (uint8_t[]){0x00}, 1, 0},
    {0xB7, (uint8_t[]){0x00}, 1, 0},
    {0xB8, (uint8_t[]){0x38}, 1, 0},
    {0xB9, (uint8_t[]){0x00}, 1, 0},
    {0xBA, (uint8_t[]){0x09}, 1, 0},
    {0xBB, (uint8_t[]){0x02}, 1, 0},
    {0xBC, (uint8_t[]){0xE1}, 1, 0},
    {0xBD, (uint8_t[]){0x00}, 1, 0},
    {0xBE, (uint8_t[]){0x00}, 1, 0},
    {0xBF, (uint8_t[]){0x00}, 1, 0},
    {0xC0, (uint8_t[]){0x22}, 1, 0},
    {0xC1, (uint8_t[]){0xAA}, 1, 0},
    {0xC2, (uint8_t[]){0x65}, 1, 0},
    {0xC3, (uint8_t[]){0x74}, 1, 0},
    {0xC4, (uint8_t[]){0x47}, 1, 0},
    {0xC5, (uint8_t[]){0x56}, 1, 0},
    {0xC6, (uint8_t[]){0x00}, 1, 0},
    {0xC7, (uint8_t[]){0x88}, 1, 0},
    {0xC8, (uint8_t[]){0x99}, 1, 0},
    {0xC9, (uint8_t[]){0x33}, 1, 0},
    {0xD0, (uint8_t[]){0x11}, 1, 0},
    {0xD1, (uint8_t[]){0xAA}, 1, 0},
    {0xD2, (uint8_t[]){0x65}, 1, 0},
    {0xD3, (uint8_t[]){0x74}, 1, 0},
    {0xD4, (uint8_t[]){0x47}, 1, 0},
    {0xD5, (uint8_t[]){0x56}, 1, 0},
    {0xD6, (uint8_t[]){0x00}, 1, 0},
    {0xD7, (uint8_t[]){0x88}, 1, 0},
    {0xD8, (uint8_t[]){0x99}, 1, 0},
    {0xD9, (uint8_t[]){0x33}, 1, 0},
    {0xF3, (uint8_t[]){0x01}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x21, (uint8_t[]){0x00}, 0, 0},
    {0x11, (uint8_t[]){0x00}, 0, 120},
    {0x29, (uint8_t[]){0x00}, 0, 0},
};

const st77916_lcd_init_cmd_t kVendorInitVersion2[] = {
    {0xF0, (uint8_t[]){0x28}, 1, 0},
    {0xF2, (uint8_t[]){0x28}, 1, 0},
    {0x73, (uint8_t[]){0xF0}, 1, 0},
    {0x7C, (uint8_t[]){0xD1}, 1, 0},
    {0x83, (uint8_t[]){0xE0}, 1, 0},
    {0x84, (uint8_t[]){0x61}, 1, 0},
    {0xF2, (uint8_t[]){0x82}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0xF0, (uint8_t[]){0x01}, 1, 0},
    {0xF1, (uint8_t[]){0x01}, 1, 0},
    {0xB0, (uint8_t[]){0x56}, 1, 0},
    {0xB1, (uint8_t[]){0x4D}, 1, 0},
    {0xB2, (uint8_t[]){0x24}, 1, 0},
    {0xB4, (uint8_t[]){0x87}, 1, 0},
    {0xB5, (uint8_t[]){0x44}, 1, 0},
    {0xB6, (uint8_t[]){0x8B}, 1, 0},
    {0xB7, (uint8_t[]){0x40}, 1, 0},
    {0xB8, (uint8_t[]){0x86}, 1, 0},
    {0xBA, (uint8_t[]){0x00}, 1, 0},
    {0xBB, (uint8_t[]){0x08}, 1, 0},
    {0xBC, (uint8_t[]){0x08}, 1, 0},
    {0xBD, (uint8_t[]){0x00}, 1, 0},
    {0xC0, (uint8_t[]){0x80}, 1, 0},
    {0xC1, (uint8_t[]){0x10}, 1, 0},
    {0xC2, (uint8_t[]){0x37}, 1, 0},
    {0xC3, (uint8_t[]){0x80}, 1, 0},
    {0xC4, (uint8_t[]){0x10}, 1, 0},
    {0xC5, (uint8_t[]){0x37}, 1, 0},
    {0xC6, (uint8_t[]){0xA9}, 1, 0},
    {0xC7, (uint8_t[]){0x41}, 1, 0},
    {0xC8, (uint8_t[]){0x01}, 1, 0},
    {0xC9, (uint8_t[]){0xA9}, 1, 0},
    {0xCA, (uint8_t[]){0x41}, 1, 0},
    {0xCB, (uint8_t[]){0x01}, 1, 0},
    {0xD0, (uint8_t[]){0x91}, 1, 0},
    {0xD1, (uint8_t[]){0x68}, 1, 0},
    {0xD2, (uint8_t[]){0x68}, 1, 0},
    {0xF5, (uint8_t[]){0x00, 0xA5}, 2, 0},
    {0xDD, (uint8_t[]){0x4F}, 1, 0},
    {0xDE, (uint8_t[]){0x4F}, 1, 0},
    {0xF1, (uint8_t[]){0x10}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0xF0, (uint8_t[]){0x02}, 1, 0},
    {0xE0,
     (uint8_t[]){0xF0, 0x0A, 0x10, 0x09, 0x09, 0x36, 0x35, 0x33, 0x4A, 0x29,
                 0x15, 0x15, 0x2E, 0x34},
     14, 0},
    {0xE1,
     (uint8_t[]){0xF0, 0x0A, 0x0F, 0x08, 0x08, 0x05, 0x34, 0x33, 0x4A, 0x39,
                 0x15, 0x15, 0x2D, 0x33},
     14, 0},
    {0xF0, (uint8_t[]){0x10}, 1, 0},
    {0xF3, (uint8_t[]){0x10}, 1, 0},
    {0xE0, (uint8_t[]){0x07}, 1, 0},
    {0xE1, (uint8_t[]){0x00}, 1, 0},
    {0xE2, (uint8_t[]){0x00}, 1, 0},
    {0xE3, (uint8_t[]){0x00}, 1, 0},
    {0xE4, (uint8_t[]){0xE0}, 1, 0},
    {0xE5, (uint8_t[]){0x06}, 1, 0},
    {0xE6, (uint8_t[]){0x21}, 1, 0},
    {0xE7, (uint8_t[]){0x01}, 1, 0},
    {0xE8, (uint8_t[]){0x05}, 1, 0},
    {0xE9, (uint8_t[]){0x02}, 1, 0},
    {0xEA, (uint8_t[]){0xDA}, 1, 0},
    {0xEB, (uint8_t[]){0x00}, 1, 0},
    {0xEC, (uint8_t[]){0x00}, 1, 0},
    {0xED, (uint8_t[]){0x0F}, 1, 0},
    {0xEE, (uint8_t[]){0x00}, 1, 0},
    {0xEF, (uint8_t[]){0x00}, 1, 0},
    {0xF8, (uint8_t[]){0x00}, 1, 0},
    {0xF9, (uint8_t[]){0x00}, 1, 0},
    {0xFA, (uint8_t[]){0x00}, 1, 0},
    {0xFB, (uint8_t[]){0x00}, 1, 0},
    {0xFC, (uint8_t[]){0x00}, 1, 0},
    {0xFD, (uint8_t[]){0x00}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xFF, (uint8_t[]){0x00}, 1, 0},
    {0x60, (uint8_t[]){0x40}, 1, 0},
    {0x61, (uint8_t[]){0x04}, 1, 0},
    {0x62, (uint8_t[]){0x00}, 1, 0},
    {0x63, (uint8_t[]){0x42}, 1, 0},
    {0x64, (uint8_t[]){0xD9}, 1, 0},
    {0x65, (uint8_t[]){0x00}, 1, 0},
    {0x66, (uint8_t[]){0x00}, 1, 0},
    {0x67, (uint8_t[]){0x00}, 1, 0},
    {0x68, (uint8_t[]){0x00}, 1, 0},
    {0x69, (uint8_t[]){0x00}, 1, 0},
    {0x6A, (uint8_t[]){0x00}, 1, 0},
    {0x6B, (uint8_t[]){0x00}, 1, 0},
    {0x70, (uint8_t[]){0x40}, 1, 0},
    {0x71, (uint8_t[]){0x03}, 1, 0},
    {0x72, (uint8_t[]){0x00}, 1, 0},
    {0x73, (uint8_t[]){0x42}, 1, 0},
    {0x74, (uint8_t[]){0xD8}, 1, 0},
    {0x75, (uint8_t[]){0x00}, 1, 0},
    {0x76, (uint8_t[]){0x00}, 1, 0},
    {0x77, (uint8_t[]){0x00}, 1, 0},
    {0x78, (uint8_t[]){0x00}, 1, 0},
    {0x79, (uint8_t[]){0x00}, 1, 0},
    {0x7A, (uint8_t[]){0x00}, 1, 0},
    {0x7B, (uint8_t[]){0x00}, 1, 0},
    {0x80, (uint8_t[]){0x48}, 1, 0},
    {0x81, (uint8_t[]){0x00}, 1, 0},
    {0x82, (uint8_t[]){0x06}, 1, 0},
    {0x83, (uint8_t[]){0x02}, 1, 0},
    {0x84, (uint8_t[]){0xD6}, 1, 0},
    {0x85, (uint8_t[]){0x04}, 1, 0},
    {0x86, (uint8_t[]){0x00}, 1, 0},
    {0x87, (uint8_t[]){0x00}, 1, 0},
    {0x88, (uint8_t[]){0x48}, 1, 0},
    {0x89, (uint8_t[]){0x00}, 1, 0},
    {0x8A, (uint8_t[]){0x08}, 1, 0},
    {0x8B, (uint8_t[]){0x02}, 1, 0},
    {0x8C, (uint8_t[]){0xD8}, 1, 0},
    {0x8D, (uint8_t[]){0x04}, 1, 0},
    {0x8E, (uint8_t[]){0x00}, 1, 0},
    {0x8F, (uint8_t[]){0x00}, 1, 0},
    {0x90, (uint8_t[]){0x48}, 1, 0},
    {0x91, (uint8_t[]){0x00}, 1, 0},
    {0x92, (uint8_t[]){0x0A}, 1, 0},
    {0x93, (uint8_t[]){0x02}, 1, 0},
    {0x94, (uint8_t[]){0xDA}, 1, 0},
    {0x95, (uint8_t[]){0x04}, 1, 0},
    {0x96, (uint8_t[]){0x00}, 1, 0},
    {0x97, (uint8_t[]){0x00}, 1, 0},
    {0x98, (uint8_t[]){0x48}, 1, 0},
    {0x99, (uint8_t[]){0x00}, 1, 0},
    {0x9A, (uint8_t[]){0x0C}, 1, 0},
    {0x9B, (uint8_t[]){0x02}, 1, 0},
    {0x9C, (uint8_t[]){0xDC}, 1, 0},
    {0x9D, (uint8_t[]){0x04}, 1, 0},
    {0x9E, (uint8_t[]){0x00}, 1, 0},
    {0x9F, (uint8_t[]){0x00}, 1, 0},
    {0xA0, (uint8_t[]){0x48}, 1, 0},
    {0xA1, (uint8_t[]){0x00}, 1, 0},
    {0xA2, (uint8_t[]){0x05}, 1, 0},
    {0xA3, (uint8_t[]){0x02}, 1, 0},
    {0xA4, (uint8_t[]){0xD5}, 1, 0},
    {0xA5, (uint8_t[]){0x04}, 1, 0},
    {0xA6, (uint8_t[]){0x00}, 1, 0},
    {0xA7, (uint8_t[]){0x00}, 1, 0},
    {0xA8, (uint8_t[]){0x48}, 1, 0},
    {0xA9, (uint8_t[]){0x00}, 1, 0},
    {0xAA, (uint8_t[]){0x07}, 1, 0},
    {0xAB, (uint8_t[]){0x02}, 1, 0},
    {0xAC, (uint8_t[]){0xD7}, 1, 0},
    {0xAD, (uint8_t[]){0x04}, 1, 0},
    {0xAE, (uint8_t[]){0x00}, 1, 0},
    {0xAF, (uint8_t[]){0x00}, 1, 0},
    {0xB0, (uint8_t[]){0x48}, 1, 0},
    {0xB1, (uint8_t[]){0x00}, 1, 0},
    {0xB2, (uint8_t[]){0x09}, 1, 0},
    {0xB3, (uint8_t[]){0x02}, 1, 0},
    {0xB4, (uint8_t[]){0xD9}, 1, 0},
    {0xB5, (uint8_t[]){0x04}, 1, 0},
    {0xB6, (uint8_t[]){0x00}, 1, 0},
    {0xB7, (uint8_t[]){0x00}, 1, 0},
    {0xB8, (uint8_t[]){0x48}, 1, 0},
    {0xB9, (uint8_t[]){0x00}, 1, 0},
    {0xBA, (uint8_t[]){0x0B}, 1, 0},
    {0xBB, (uint8_t[]){0x02}, 1, 0},
    {0xBC, (uint8_t[]){0xDB}, 1, 0},
    {0xBD, (uint8_t[]){0x04}, 1, 0},
    {0xBE, (uint8_t[]){0x00}, 1, 0},
    {0xBF, (uint8_t[]){0x00}, 1, 0},
    {0xC0, (uint8_t[]){0x10}, 1, 0},
    {0xC1, (uint8_t[]){0x47}, 1, 0},
    {0xC2, (uint8_t[]){0x56}, 1, 0},
    {0xC3, (uint8_t[]){0x65}, 1, 0},
    {0xC4, (uint8_t[]){0x74}, 1, 0},
    {0xC5, (uint8_t[]){0x88}, 1, 0},
    {0xC6, (uint8_t[]){0x99}, 1, 0},
    {0xC7, (uint8_t[]){0x01}, 1, 0},
    {0xC8, (uint8_t[]){0xBB}, 1, 0},
    {0xC9, (uint8_t[]){0xAA}, 1, 0},
    {0xD0, (uint8_t[]){0x10}, 1, 0},
    {0xD1, (uint8_t[]){0x47}, 1, 0},
    {0xD2, (uint8_t[]){0x56}, 1, 0},
    {0xD3, (uint8_t[]){0x65}, 1, 0},
    {0xD4, (uint8_t[]){0x74}, 1, 0},
    {0xD5, (uint8_t[]){0x88}, 1, 0},
    {0xD6, (uint8_t[]){0x99}, 1, 0},
    {0xD7, (uint8_t[]){0x01}, 1, 0},
    {0xD8, (uint8_t[]){0xBB}, 1, 0},
    {0xD9, (uint8_t[]){0xAA}, 1, 0},
    {0xF3, (uint8_t[]){0x01}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x21, (uint8_t[]){0x00}, 1, 0},
    {0x11, (uint8_t[]){0x00}, 1, 120},
    {0x29, (uint8_t[]){0x00}, 1, 0},
};

bool IRAM_ATTR onFlushDone(esp_lcd_panel_io_handle_t,
                           esp_lcd_panel_io_event_data_t*, void*) {
  BaseType_t highTaskWoken = pdFALSE;
  if (s_flushDone != nullptr) {
    xSemaphoreGiveFromISR(s_flushDone, &highTaskWoken);
  }
  return highTaskWoken == pdTRUE;
}

void sendCommand(uint8_t command) {
  uint32_t lcdCommand = command;
  lcdCommand <<= 8;
  lcdCommand |= static_cast<uint32_t>(kLcdOpcodeWriteCmd << 24);
  esp_lcd_panel_io_tx_param(s_io, lcdCommand, nullptr, 0);
}

}  // namespace

esp_err_t init() {
  if (s_initialised) return ESP_OK;

  s_flushDone = xSemaphoreCreateBinary();
  if (s_flushDone == nullptr) return ESP_ERR_NO_MEM;

  gpio_config_t resetConfig = {};
  resetConfig.pin_bit_mask = 1ULL << BOARD_LCD_RST;
  resetConfig.mode = GPIO_MODE_OUTPUT;
  resetConfig.pull_up_en = GPIO_PULLUP_DISABLE;
  resetConfig.pull_down_en = GPIO_PULLDOWN_DISABLE;
  resetConfig.intr_type = GPIO_INTR_DISABLE;
  ESP_RETURN_ON_ERROR(gpio_config(&resetConfig), kTag, "lcd reset gpio");

  gpio_set_level(BOARD_LCD_RST, 0);
  vTaskDelay(pdMS_TO_TICKS(10));
  gpio_set_level(BOARD_LCD_RST, 1);
  vTaskDelay(pdMS_TO_TICKS(10));

  const spi_bus_config_t busConfig = {
      .data0_io_num = BOARD_LCD_DATA0,
      .data1_io_num = BOARD_LCD_DATA1,
      .sclk_io_num = BOARD_LCD_PCLK,
      .data2_io_num = BOARD_LCD_DATA2,
      .data3_io_num = BOARD_LCD_DATA3,
      .data4_io_num = -1,
      .data5_io_num = -1,
      .data6_io_num = -1,
      .data7_io_num = -1,
      .max_transfer_sz = BOARD_LCD_H_RES * kFlushBandRows * sizeof(uint16_t),
      .flags = SPICOMMON_BUSFLAG_QUAD,
      .intr_flags = 0,
  };
  ESP_RETURN_ON_ERROR(spi_bus_initialize(BOARD_LCD_SPI_HOST, &busConfig,
                                         SPI_DMA_CH_AUTO),
                      kTag, "qspi bus init");

  esp_lcd_panel_io_spi_config_t ioConfig = {
      .cs_gpio_num = BOARD_LCD_CS,
      .dc_gpio_num = -1,
      .spi_mode = 0,
      .pclk_hz = 3 * 1000 * 1000,
      .trans_queue_depth = 10,
      .on_color_trans_done = nullptr,
      .user_ctx = nullptr,
      .lcd_cmd_bits = 32,
      .lcd_param_bits = 8,
      .flags =
          {
              .dc_low_on_data = 0,
              .octal_mode = 0,
              .quad_mode = 1,
              .sio_mode = 0,
              .lsb_first = 0,
              .cs_high_active = 0,
          },
  };
  ESP_RETURN_ON_ERROR(
      esp_lcd_new_panel_io_spi(
          static_cast<esp_lcd_spi_bus_handle_t>(BOARD_LCD_SPI_HOST),
          &ioConfig, &s_io),
      kTag, "panel io");

  // Read the panel revision register so the matching vendor table is used.
  uint8_t revision[4] = {0, 0, 0, 0};
  int readCommand = 0x04;
  readCommand &= 0xFF;
  readCommand <<= 8;
  readCommand |= static_cast<int>(kLcdOpcodeReadCmd << 24);
  const esp_err_t readResult =
      esp_lcd_panel_io_rx_param(s_io, readCommand, revision, sizeof(revision));
  if (readResult == ESP_OK) {
    ESP_LOGI(kTag, "panel revision register 0x04 = %02X %02X %02X %02X",
             revision[0], revision[1], revision[2], revision[3]);
  } else {
    ESP_LOGW(kTag, "panel revision read failed: %s",
             esp_err_to_name(readResult));
  }

  ESP_RETURN_ON_ERROR(esp_lcd_panel_io_del(s_io), kTag, "panel io del");
  s_io = nullptr;

  ioConfig.pclk_hz = BOARD_LCD_PIXEL_CLOCK_HZ;
  ioConfig.on_color_trans_done = onFlushDone;
  ESP_RETURN_ON_ERROR(
      esp_lcd_new_panel_io_spi(
          static_cast<esp_lcd_spi_bus_handle_t>(BOARD_LCD_SPI_HOST),
          &ioConfig, &s_io),
      kTag, "panel io high speed");

  st77916_vendor_config_t vendorConfig = {
      .init_cmds = nullptr,
      .init_cmds_size = 0,
      .flags = {.use_qspi_interface = 1},
  };
  if (revision[0] == 0x00 && revision[1] == 0x02 && revision[2] == 0x7F &&
      revision[3] == 0x7F) {
    vendorConfig.init_cmds = kVendorInitVersion2;
    vendorConfig.init_cmds_size =
        sizeof(kVendorInitVersion2) / sizeof(st77916_lcd_init_cmd_t);
    ESP_LOGI(kTag, "using vendor init table v2");
  } else {
    vendorConfig.init_cmds = kVendorInitVersion1;
    vendorConfig.init_cmds_size =
        sizeof(kVendorInitVersion1) / sizeof(st77916_lcd_init_cmd_t);
    ESP_LOGI(kTag, "using vendor init table v1");
  }

  esp_lcd_panel_dev_config_t panelConfig = {
      .reset_gpio_num = BOARD_LCD_RST,
      .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
      .bits_per_pixel = 16,
      .flags = {.reset_active_high = 0},
      .vendor_config = &vendorConfig,
  };
  ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st77916(s_io, &panelConfig, &s_panel),
                      kTag, "panel create");
  ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), kTag, "panel reset");
  ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), kTag, "panel init");
  ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), kTag,
                      "panel on");

  // Backlight PWM.
  const ledc_timer_config_t backlightTimer = {
      .speed_mode = LEDC_LOW_SPEED_MODE,
      .duty_resolution = LEDC_TIMER_10_BIT,
      .timer_num = LEDC_TIMER_1,
      .freq_hz = 5000,
      .clk_cfg = LEDC_AUTO_CLK,
  };
  const ledc_channel_config_t backlightChannel = {
      .gpio_num = BOARD_LCD_BACKLIGHT,
      .speed_mode = LEDC_LOW_SPEED_MODE,
      .channel = BOARD_LCD_LEDC_CH,
      .intr_type = LEDC_INTR_DISABLE,
      .timer_sel = LEDC_TIMER_1,
      .duty = 0,
      .hpoint = 0,
  };
  ESP_RETURN_ON_ERROR(ledc_timer_config(&backlightTimer), kTag,
                      "backlight timer");
  ESP_RETURN_ON_ERROR(ledc_channel_config(&backlightChannel), kTag,
                      "backlight channel");

  s_initialised = true;
  return ESP_OK;
}

void set_brightness(int percent) {
  if (percent < 0) percent = 0;
  if (percent > 100) percent = 100;
  s_brightness = percent;
  const uint32_t duty = (1023u * static_cast<uint32_t>(percent)) / 100u;
  ledc_set_duty(LEDC_LOW_SPEED_MODE, BOARD_LCD_LEDC_CH, duty);
  ledc_update_duty(LEDC_LOW_SPEED_MODE, BOARD_LCD_LEDC_CH);
}

int brightness() { return s_brightness; }

void sleep() {
  if (!s_initialised) return;
  set_brightness(0);
  sendCommand(0x28);  // display off
  sendCommand(0x10);  // sleep in
  vTaskDelay(pdMS_TO_TICKS(10));
}

void wake() {
  if (!s_initialised) return;
  sendCommand(0x11);  // sleep out
  vTaskDelay(pdMS_TO_TICKS(120));
  sendCommand(0x29);  // display on
}

void flush(const uint16_t* framebuffer) {
  if (!s_initialised || framebuffer == nullptr) return;
  for (int row = 0; row < BOARD_LCD_V_RES; row += kFlushBandRows) {
    int lastRow = row + kFlushBandRows;
    if (lastRow > BOARD_LCD_V_RES) lastRow = BOARD_LCD_V_RES;
    const uint16_t* band =
        framebuffer + static_cast<size_t>(row) * BOARD_LCD_H_RES;
    if (esp_lcd_panel_draw_bitmap(s_panel, 0, row, BOARD_LCD_H_RES, lastRow,
                                  band) != ESP_OK) {
      return;
    }
    // Wait for this band to leave the DMA queue before reusing the buffer.
    xSemaphoreTake(s_flushDone, pdMS_TO_TICKS(200));
  }
}

esp_lcd_panel_handle_t panel() { return s_panel; }
esp_lcd_panel_io_handle_t panel_io() { return s_io; }

}  // namespace board_display
