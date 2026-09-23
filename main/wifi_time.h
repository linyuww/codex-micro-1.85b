// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Codex Micro port
//
// Wi-Fi station link plus NTP clock -- the device's only time source.
//
// The board has no RTC driver (the PCF85063 at 0x51 is declared in
// board_config.h but nothing talks to it), which is why the dashboard used to
// have to render a restored quota snapshot as STALE: with no clock it cannot
// know how long it was powered off. Once NTP has synced, that snapshot can be
// aged properly instead.
//
// Until the first sync the local time is meaningless, so `hasTime()` gates
// everything that depends on it. In particular the day/night rule must not be
// evaluated: treating an unknown hour as 0 would force the night theme.

#pragma once

#include <cstddef>
#include <cstdint>

#include "esp_err.h"

namespace wifi_time {

enum class Phase : std::uint8_t {
  Idle,        // begin() not called
  Portal,      // no credentials: SoftAP setup portal is up
  Connecting,  // joining the configured network
  Online,      // has an IP
  Failed,      // gave up; the portal is the way out
};

struct Time {
  bool valid = false;
  int year = 0;
  int month = 0;   // 1-12
  int day = 0;     // 1-31
  int hour = 0;    // local, 0-23
  int minute = 0;
  int second = 0;
  int weekday = 0; // 0 = Sunday, matching struct tm
};

struct Status {
  Phase phase = Phase::Idle;
  bool hasTime = false;
  char ip[16] = {};
  int rssi = 0;
  bool portalRunning = false;
  char apSsid[33] = {};
  Time now;
};

// Brings up netif, the event loop, Wi-Fi and (when credentials exist) NTP.
// Never blocks on the network: connection progress happens in the event
// handlers and is reported through snapshot().
esp_err_t begin();

// Drive periodic work (reconnect, SNTP start). Cheap; call from the main loop.
void poll();

Status snapshot();

// Drop the stored credentials and bring up the setup portal. This is the
// recovery path when the network changes.
esp_err_t startPortal();

// Erase stored credentials only. The caller decides whether to restart.
esp_err_t forget();

bool timeValid();
bool localTime(Time& out);

// Local calendar date as the design formats it, e.g. "SEP 22 MON" and "10:08".
void formatDate(const Time& time, char* out, std::size_t size);
void formatClock(const Time& time, char* out, std::size_t size);

}  // namespace wifi_time
