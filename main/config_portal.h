// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Codex Micro port
//
// SoftAP configuration portal: the only way Wi-Fi credentials get into the
// device. There is no companion-app provisioning step and no build-time
// secret -- the user joins a temporary access point, fills in a form in any
// browser, and the device reboots onto their network.
//
// This follows the same shape as linyuww/esp32-ornament's config_portal: an
// APSTA SoftAP plus a small esp_http_server, rather than wifi_prov_mgr. That
// avoids needing the "ESP SoftAP Prov" app installed on the phone, which is
// the whole point of keeping it simple.

#pragma once

#include <cstddef>

#include "esp_err.h"

namespace config_portal {

constexpr std::size_t kSsidMax = 32;
constexpr std::size_t kPasswordMax = 64;

struct Credentials {
  char ssid[kSsidMax + 1] = {};
  char password[kPasswordMax + 1] = {};

  bool valid() const { return ssid[0] != '\0'; }
};

// --- credentials, persisted in NVS -----------------------------------------
bool load(Credentials& out);
esp_err_t save(const Credentials& credentials);
esp_err_t clear();

// --- the portal itself ------------------------------------------------------
// Brings up the SoftAP and the form server. The user joins `ssid()` and opens
// http://192.168.4.1. Safe to call when Wi-Fi is already running: it stops the
// radio, switches to APSTA and starts again.
esp_err_t start();
bool running();
const char* ssid();

}  // namespace config_portal
