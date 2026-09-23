// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Codex Micro port

#include "wifi_time.h"

#include <cstring>
#include <ctime>

#include "config_portal.h"
#include "esp_event.h"
#include "esp_coexist.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "logic.h"
#include "nvs_flash.h"

namespace wifi_time {
namespace {

constexpr char kTag[] = "wifi_time";

// UTC+8, no DST. NTP hands back UTC; everything the UI shows -- the clock, the
// date, and the day/night boundary -- is local, so the offset is applied once,
// here, via the C library rather than by hand at every use.
constexpr char kTimezone[] = "CST-8";

constexpr int kConnectTimeoutMs = 15000;
constexpr int kMaxRetries = 8;
constexpr uint32_t kPollIntervalMs = 500;
constexpr uint32_t kSntpRetryMs = 30000;

// How many consecutive rejections before the portal is offered. One is not
// enough: a marginal signal also produces handshake timeouts, and tearing down
// into the setup portal because of a flaky packet would be worse than the
// problem. A genuinely wrong password fails every single time.
constexpr int kAuthFailuresBeforePortal = 3;

// Two servers so a single DNS or server failure does not strand the clock.
// CONFIG_LWIP_SNTP_MAX_SERVERS must be at least this large.
constexpr char kSntpServer0[] = "ntp.aliyun.com";
constexpr char kSntpServer1[] = "cn.pool.ntp.org";

struct Runtime {
  Phase phase = Phase::Idle;
  bool started = false;
  bool staStarted = false;
  // True only once an access point has actually been handed to the station.
  //
  // WIFI_EVENT_STA_START fires whenever the station interface comes up, and the
  // setup portal brings it up as half of APSTA -- with no access point
  // configured. Connecting in that state is meaningless, and net80211 does not
  // simply shrug: it retries about four times a second for as long as the
  // portal is open, which floods the log and keeps the radio busy while the
  // user is trying to configure the device.
  bool staConfigured = false;
  bool sntpStarted = false;
  bool everConnected = false;
  int retries = 0;
  int authFailures = 0;
  char ip[16] = {};
  uint32_t lastPollMs = 0;
  uint32_t connectStartedMs = 0;
  uint32_t lastSntpAttemptMs = 0;
};

Runtime s_runtime;
esp_netif_t* s_staNetif = nullptr;

uint32_t nowMs() { return static_cast<uint32_t>(xTaskGetTickCount() * portTICK_PERIOD_MS); }

void setPhase(Phase phase) {
  if (s_runtime.phase == phase) return;
  ESP_LOGI(kTag, "phase %d -> %d", static_cast<int>(s_runtime.phase),
           static_cast<int>(phase));
  s_runtime.phase = phase;
}

// ------------------------------------------------------------ events -------

// True when the access point actively turned us away rather than just not being
// reachable. The distinction matters because these reasons cannot fix
// themselves: retrying a rejected password forever is a device the user has no
// way to recover short of reflashing it.
//
// AUTH_FAIL is the unambiguous one. The two handshake timeouts are how most
// consumer APs report a wrong WPA2 passphrase, which is why they are included
// -- but they can also come from a weak signal, so the caller requires several
// in a row before acting.
bool credentialsRejected(uint8_t reason) {
  return reason == WIFI_REASON_AUTH_FAIL ||
         reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
         reason == WIFI_REASON_HANDSHAKE_TIMEOUT;
}

void onWifiEvent(void* arg, esp_event_base_t base, int32_t id, void* data) {
  (void)arg;
  if (base == WIFI_EVENT) {
    switch (id) {
      case WIFI_EVENT_STA_START:
        // See Runtime::staConfigured: this event also fires for the portal's
        // APSTA start, where there is nothing to join.
        if (s_runtime.staConfigured) esp_wifi_connect();
        break;
      case WIFI_EVENT_STA_CONNECTED:
        ESP_LOGI(kTag, "associated with the access point, waiting for DHCP");
        break;
      case WIFI_EVENT_STA_DISCONNECTED: {
        const auto* event = static_cast<wifi_event_sta_disconnected_t*>(data);
        s_runtime.ip[0] = '\0';

        // A rejected password is permanent, so it escalates on its own terms --
        // and it has to, because `everConnected` is exactly the wrong signal
        // here. Someone who changes their router's password has, from this
        // device's point of view, credentials that worked once; the old code
        // read that as "good credentials, just a dropout" and retried forever.
        if (credentialsRejected(event->reason)) {
          if (++s_runtime.authFailures >= kAuthFailuresBeforePortal) {
            ESP_LOGE(kTag,
                     "credentials rejected %d times (reason %u); starting "
                     "setup portal",
                     s_runtime.authFailures,
                     static_cast<unsigned>(event->reason));
            setPhase(Phase::Failed);
            startPortal();
            break;
          }
        } else {
          s_runtime.authFailures = 0;
        }

        if (s_runtime.retries < kMaxRetries) {
          ++s_runtime.retries;
          ESP_LOGW(kTag, "disconnected (reason %u), retry %d/%d",
                   static_cast<unsigned>(event->reason), s_runtime.retries,
                   kMaxRetries);
          esp_wifi_connect();
        } else if (s_runtime.everConnected) {
          // It worked once and nothing says the credentials are bad, so this
          // is a dropout or an absent access point. Keep retrying rather than
          // tearing the whole thing down into the portal: a router that is
          // merely switched off is not a misconfiguration, and a device that
          // demands reconfiguration every time the power blinks would be
          // worse than one that quietly waits.
          s_runtime.retries = 0;
          ESP_LOGW(kTag, "link dropped, reconnecting");
          esp_wifi_connect();
        } else {
          ESP_LOGE(kTag, "could not join the network; starting setup portal");
          setPhase(Phase::Failed);
          startPortal();
        }
        break;
      }
      default:
        break;
    }
    return;
  }

  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    const auto* event = static_cast<ip_event_got_ip_t*>(data);
    snprintf(s_runtime.ip, sizeof(s_runtime.ip), IPSTR,
             IP2STR(&event->ip_info.ip));
    s_runtime.retries = 0;
    s_runtime.authFailures = 0;
    s_runtime.everConnected = true;
    setPhase(Phase::Online);
    ESP_LOGI(kTag, "online at %s", s_runtime.ip);
  }
}

// -------------------------------------------------------------- SNTP -------

void startSntp() {
  if (s_runtime.sntpStarted) return;
  setenv("TZ", kTimezone, 1);
  tzset();
  esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
  esp_sntp_setservername(0, kSntpServer0);
  esp_sntp_setservername(1, kSntpServer1);
  esp_sntp_init();
  s_runtime.sntpStarted = true;
  s_runtime.lastSntpAttemptMs = nowMs();
  ESP_LOGI(kTag, "SNTP started, servers %s / %s, TZ=%s", kSntpServer0,
           kSntpServer1, kTimezone);
}

// SNTP retries on its own, but only while it believes it has not synced. If the
// first attempt lands before DHCP has fully settled it can give up silently, so
// nudge it from the poll loop while the clock is still unset.
void ensureTimeSynced() {
  if (!s_runtime.sntpStarted || timeValid()) return;
  const uint32_t now = nowMs();
  if (now - s_runtime.lastSntpAttemptMs < kSntpRetryMs) return;
  s_runtime.lastSntpAttemptMs = now;
  ESP_LOGW(kTag, "clock still unset, restarting SNTP");
  esp_sntp_restart();
}

// ---------------------------------------------------------- STA connect ----

esp_err_t startSta(const config_portal::Credentials& credentials) {
  wifi_config_t config = {};
  std::strncpy(reinterpret_cast<char*>(config.sta.ssid), credentials.ssid,
               sizeof(config.sta.ssid) - 1);
  std::strncpy(reinterpret_cast<char*>(config.sta.password), credentials.password,
               sizeof(config.sta.password) - 1);
  config.sta.threshold.authmode = WIFI_AUTH_OPEN;
#if CONFIG_ESP_WIFI_ENABLE_WPA3_SAE
  config.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
#endif

  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
  // Set before starting: esp_wifi_start fires STA_START, and the handler has to
  // already know there is an access point worth connecting to.
  s_runtime.staConfigured = true;
  esp_err_t result = esp_wifi_start();
  if (result != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_start failed: %s", esp_err_to_name(result));
    s_runtime.staConfigured = false;
    return result;
  }
  s_runtime.staStarted = true;
  s_runtime.connectStartedMs = nowMs();
  setPhase(Phase::Connecting);
  ESP_LOGI(kTag, "joining SSID=%s", credentials.ssid);
  return ESP_OK;
}

// Hand the radio over to the portal by making the station forget its access
// point. Clearing the flag alone would not be enough: the credentials would
// still sit in the driver, and the STA half of APSTA would go on retrying the
// network that just rejected them -- visible in the log as a reconnect storm
// underneath the setup page.
void forgetStaConfig() {
  s_runtime.staConfigured = false;
  wifi_config_t empty = {};
  const esp_err_t result = esp_wifi_set_config(WIFI_IF_STA, &empty);
  if (result != ESP_OK) {
    // Not fatal: the flag above already stops our own connect attempts, and the
    // portal is more useful than an aborted setup.
    ESP_LOGW(kTag, "could not clear the station config: %s",
             esp_err_to_name(result));
  }
}

}  // namespace

// ---------------------------------------------------------------- API ------

esp_err_t begin() {
  if (s_runtime.started) return ESP_OK;

  // The quota cache in codex_ble also uses NVS, so this may already be up.
  esp_err_t result = nvs_flash_init();
  if (result == ESP_ERR_NVS_NO_FREE_PAGES ||
      result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    result = nvs_flash_init();
  }
  if (result != ESP_OK) return result;

  ESP_ERROR_CHECK(esp_netif_init());
  // BLE already runs its own task, so the default loop may exist by now.
  result = esp_event_loop_create_default();
  if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) return result;

  s_staNetif = esp_netif_create_default_wifi_sta();
  if (s_staNetif == nullptr) return ESP_FAIL;
  esp_netif_create_default_wifi_ap();

  wifi_init_config_t initConfig = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&initConfig));
  ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
  ESP_ERROR_CHECK(esp_coex_preference_set(ESP_COEX_PREFER_BT));
  ESP_LOGI(kTag, "Wi-Fi/BLE coexistence set to Bluetooth preference");

  ESP_ERROR_CHECK(esp_event_handler_instance_register(
      WIFI_EVENT, ESP_EVENT_ANY_ID, &onWifiEvent, nullptr, nullptr));
  ESP_ERROR_CHECK(esp_event_handler_instance_register(
      IP_EVENT, IP_EVENT_STA_GOT_IP, &onWifiEvent, nullptr, nullptr));

  s_runtime.started = true;
  s_runtime.lastPollMs = nowMs();

  config_portal::Credentials credentials;
  if (!config_portal::load(credentials)) {
    ESP_LOGW(kTag, "no Wi-Fi credentials stored; starting setup portal");
    setPhase(Phase::Portal);
    return config_portal::start();
  }

  return startSta(credentials);
}

void poll() {
  if (!s_runtime.started) return;
  const uint32_t now = nowMs();
  if (now - s_runtime.lastPollMs < kPollIntervalMs) return;
  s_runtime.lastPollMs = now;

  if (s_runtime.phase == Phase::Online) {
    if (!s_runtime.sntpStarted) startSntp();
    ensureTimeSynced();
    return;
  }

  if (s_runtime.phase == Phase::Connecting &&
      now - s_runtime.connectStartedMs > kConnectTimeoutMs) {
    ESP_LOGW(kTag, "join timed out after %d ms", kConnectTimeoutMs);
    s_runtime.connectStartedMs = now;
    esp_wifi_connect();
  }
}

Status snapshot() {
  Status status;
  status.phase = s_runtime.phase;
  status.hasTime = timeValid();
  status.portalRunning = config_portal::running();
  std::strncpy(status.ip, s_runtime.ip, sizeof(status.ip) - 1);
  std::strncpy(status.apSsid, config_portal::ssid(), sizeof(status.apSsid) - 1);
  localTime(status.now);

  wifi_ap_record_t accessPoint = {};
  if (esp_wifi_sta_get_ap_info(&accessPoint) == ESP_OK) {
    status.rssi = accessPoint.rssi;
  }
  return status;
}

esp_err_t startPortal() {
  ESP_LOGW(kTag, "switching to setup portal");
  setPhase(Phase::Portal);
  forgetStaConfig();
  return config_portal::start();
}

esp_err_t forget() {
  s_runtime.everConnected = false;
  s_runtime.retries = 0;
  return config_portal::clear();
}

// --------------------------------------------------------------- time ------

bool timeValid() {
  // time() returns seconds since the epoch. Anything before 2020 means SNTP has
  // not landed yet and the value is just the build-time fallback.
  return std::time(nullptr) > 1577836800;
}

bool localTime(Time& out) {
  out = Time{};
  const std::time_t now = std::time(nullptr);
  if (now <= 1577836800) return false;

  std::tm local = {};
  if (localtime_r(&now, &local) == nullptr) return false;

  out.valid = true;
  out.year = local.tm_year + 1900;
  out.month = local.tm_mon + 1;
  out.day = local.tm_mday;
  out.hour = local.tm_hour;
  out.minute = local.tm_min;
  out.second = local.tm_sec;
  out.weekday = local.tm_wday;
  return true;
}

void formatDate(const Time& time, char* out, std::size_t size) {
  static const char* kMonths[] = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};
  // tm_wday is Sunday-first; the design shows Monday-first.
  static const char* kDays[] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
  const int month = (time.month >= 1 && time.month <= 12) ? time.month - 1 : 0;
  const int day = (time.weekday >= 0 && time.weekday <= 6) ? time.weekday : 0;
  snprintf(out, size, "%s %02d %s", kMonths[month], time.day, kDays[day]);
}

void formatClock(const Time& time, char* out, std::size_t size) {
  snprintf(out, size, "%02d:%02d", time.hour, time.minute);
}

}  // namespace wifi_time
