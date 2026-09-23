// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Codex Micro port

#include "config_portal.h"

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

namespace config_portal {
namespace {

constexpr char kTag[] = "config_portal";

// Kept deliberately small: this is a setup screen, not a product page.
constexpr std::size_t kHtmlSize = 6144;
constexpr int kMaxScanResults = 16;
constexpr int kApChannel = 1;
constexpr int kApMaxConnections = 4;
constexpr char kApPrefix[] = "CODEX";

constexpr char kNvsNamespace[] = "wifi_cfg";
constexpr char kNvsSsidKey[] = "ssid";
constexpr char kNvsPasswordKey[] = "pass";

httpd_handle_t s_server = nullptr;
char s_apSsid[33] = {};
bool s_running = false;

// --------------------------------------------------------------- NVS ------

esp_err_t openNvs(nvs_open_mode_t mode, nvs_handle_t& handle) {
  esp_err_t result = nvs_open(kNvsNamespace, mode, &handle);
  if (result != ESP_OK) {
    ESP_LOGW(kTag, "nvs_open(%s) failed: %s", kNvsNamespace,
             esp_err_to_name(result));
  }
  return result;
}

// ------------------------------------------------------------ helpers ------

void append(char* html, std::size_t* used, const char* text) {
  if (*used >= kHtmlSize) return;
  const int written = snprintf(html + *used, kHtmlSize - *used, "%s", text);
  if (written > 0) {
    *used += static_cast<std::size_t>(written);
    if (*used >= kHtmlSize) *used = kHtmlSize - 1;
  }
}

// SSIDs come off the air, so they are attacker-controlled as far as the form is
// concerned. Escaping them is not optional.
void escape(const char* input, char* output, std::size_t outputSize) {
  std::size_t used = 0;
  for (const char* cursor = input; *cursor != '\0' && used + 6 < outputSize;
       ++cursor) {
    const char* replacement = nullptr;
    switch (*cursor) {
      case '&': replacement = "&amp;"; break;
      case '<': replacement = "&lt;"; break;
      case '>': replacement = "&gt;"; break;
      case '"': replacement = "&quot;"; break;
      case '\'': replacement = "&#39;"; break;
      default: break;
    }
    if (replacement != nullptr) {
      const std::size_t length = std::strlen(replacement);
      std::memcpy(output + used, replacement, length);
      used += length;
    } else {
      output[used++] = *cursor;
    }
  }
  output[used] = '\0';
}

int hexValue(char value) {
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  return -1;
}

void urlDecode(char* value) {
  char* write = value;
  for (char* read = value; *read != '\0';) {
    if (*read == '+') {
      *write++ = ' ';
      ++read;
    } else if (*read == '%' && isxdigit(static_cast<unsigned char>(read[1])) &&
               isxdigit(static_cast<unsigned char>(read[2]))) {
      *write++ = static_cast<char>((hexValue(read[1]) << 4) | hexValue(read[2]));
      read += 3;
    } else {
      *write++ = *read++;
    }
  }
  *write = '\0';
}

// Pull one field out of an application/x-www-form-urlencoded body.
void formValue(const char* body, const char* name, char* target,
               std::size_t targetSize) {
  if (targetSize == 0) return;
  target[0] = '\0';
  const std::size_t nameLength = std::strlen(name);
  for (const char* cursor = body; cursor != nullptr && *cursor != '\0';) {
    const char* next = std::strchr(cursor, '&');
    const std::size_t pairLength =
        next == nullptr ? std::strlen(cursor)
                        : static_cast<std::size_t>(next - cursor);
    const char* equals = static_cast<const char*>(
        std::memchr(cursor, '=', pairLength));
    if (equals != nullptr &&
        static_cast<std::size_t>(equals - cursor) == nameLength &&
        std::strncmp(cursor, name, nameLength) == 0) {
      std::size_t valueLength = pairLength - nameLength - 1;
      if (valueLength >= targetSize) valueLength = targetSize - 1;
      std::memcpy(target, equals + 1, valueLength);
      target[valueLength] = '\0';
      urlDecode(target);
      return;
    }
    cursor = next == nullptr ? nullptr : next + 1;
  }
}

esp_err_t readFormBody(httpd_req_t* request, char* body, std::size_t bodySize) {
  if (request->content_len <= 0 ||
      static_cast<std::size_t>(request->content_len) >= bodySize) {
    httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid form size");
    return ESP_FAIL;
  }
  int received = 0;
  while (received < request->content_len) {
    const int chunk =
        httpd_req_recv(request, body + received, request->content_len - received);
    if (chunk <= 0) {
      httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                          "Failed to read request");
      return ESP_FAIL;
    }
    received += chunk;
  }
  body[received] = '\0';
  return ESP_OK;
}

// ------------------------------------------------------------- form --------

int scanNetworks(wifi_ap_record_t* records, int maxRecords) {
  wifi_scan_config_t scan = {};
  scan.show_hidden = false;
  scan.scan_type = WIFI_SCAN_TYPE_ACTIVE;
  if (esp_wifi_scan_start(&scan, true) != ESP_OK) {
    ESP_LOGW(kTag, "scan failed");
    return 0;
  }
  uint16_t count = static_cast<uint16_t>(maxRecords);
  if (esp_wifi_scan_get_ap_records(&count, records) != ESP_OK) return 0;
  return static_cast<int>(count);
}

void renderForm(char* html, std::size_t* used) {
  append(html, used,
         "<!doctype html><html><head>"
         "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
         "<title>Codex Micro Setup</title><style>"
         "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;"
         "margin:24px;background:#020b1a;color:#eef3f8}"
         "main{max-width:420px;margin:auto}"
         "label{display:block;margin:14px 0 6px;color:#aeb9c5}"
         "input,select{box-sizing:border-box;width:100%;padding:12px;"
         "border-radius:8px;border:1px solid #3b4752;background:#0a1626;color:#fff}"
         "button{margin-top:20px;width:100%;padding:14px;border:0;"
         "border-radius:8px;background:#38c8f0;color:#02121c;font-weight:700}"
         "p{line-height:1.5;color:#c8d2dc}.muted{font-size:13px;color:#8d9aa7}"
         "</style></head><body><main>"
         "<h1>Codex Micro Setup</h1>"
         "<p>Pick your Wi-Fi and enter its password. The device restarts and "
         "joins your network.</p><form method=\"post\" action=\"/save\">");

  static wifi_ap_record_t records[kMaxScanResults];
  const int count = scanNetworks(records, kMaxScanResults);
  if (count > 0) {
    append(html, used, "<label>Wi-Fi network</label><select name=\"ssid\" required>");
    for (int i = 0; i < count; ++i) {
      char ssid[33] = {};
      char safe[200] = {};
      std::strncpy(ssid, reinterpret_cast<const char*>(records[i].ssid),
                   sizeof(ssid) - 1);
      escape(ssid, safe, sizeof(safe));
      char option[260];
      snprintf(option, sizeof(option), "<option value=\"%s\">%s (%d dBm)</option>",
               safe, safe, records[i].rssi);
      append(html, used, option);
    }
    append(html, used, "</select>");
  } else {
    append(html, used,
           "<label>Wi-Fi network</label>"
           "<input name=\"ssid\" maxlength=\"32\" required>"
           "<p class=\"muted\">No networks found. Type the name in manually, or "
           "reload to scan again.</p>");
  }

  append(html, used,
         "<label>Password</label>"
         "<input name=\"password\" maxlength=\"64\" type=\"password\">"
         "<button type=\"submit\">Save and restart</button></form>"
         "<p class=\"muted\">Leave the password empty for an open network.</p>"
         "</main></body></html>");
}

// ---------------------------------------------------------- handlers -------

esp_err_t handleRoot(httpd_req_t* request) {
  char* html = static_cast<char*>(calloc(1, kHtmlSize));
  if (html == nullptr) {
    httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Out of memory");
    return ESP_FAIL;
  }
  std::size_t used = 0;
  renderForm(html, &used);
  httpd_resp_set_type(request, "text/html; charset=utf-8");
  const esp_err_t result = httpd_resp_send(request, html, HTTPD_RESP_USE_STRLEN);
  free(html);
  return result;
}

// Browsers request this unprompted on every page load. Without a handler it
// falls through to a 404, which is harmless but noisy; a 204 says "nothing to
// see" and costs one branch.
esp_err_t handleFavicon(httpd_req_t* request) {
  httpd_resp_set_status(request, "204 No Content");
  return httpd_resp_send(request, nullptr, 0);
}

esp_err_t handleSave(httpd_req_t* request) {
  char body[512] = {};
  if (readFormBody(request, body, sizeof(body)) != ESP_OK) return ESP_FAIL;

  Credentials credentials;
  formValue(body, "ssid", credentials.ssid, sizeof(credentials.ssid));
  formValue(body, "password", credentials.password, sizeof(credentials.password));

  if (!credentials.valid()) {
    httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Network name is required");
    return ESP_FAIL;
  }
  if (save(credentials) != ESP_OK) {
    httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                        "Failed to save");
    return ESP_FAIL;
  }

  ESP_LOGI(kTag, "saved credentials for SSID=%s, restarting",
           credentials.ssid);
  httpd_resp_set_type(request, "text/html; charset=utf-8");
  httpd_resp_sendstr(request,
                     "<!doctype html><html><body style=\"font-family:system-ui;"
                     "background:#020b1a;color:#eef3f8;margin:24px\">"
                     "<h1>Saved</h1><p>The device is restarting and will join "
                     "your network.</p></body></html>");
  vTaskDelay(pdMS_TO_TICKS(700));
  esp_restart();
  return ESP_OK;
}

esp_err_t startHttpServer() {
  if (s_server != nullptr) return ESP_OK;

  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.lru_purge_enable = true;

  esp_err_t result = httpd_start(&s_server, &config);
  if (result != ESP_OK) {
    ESP_LOGE(kTag, "httpd_start failed: %s", esp_err_to_name(result));
    return result;
  }

  static const httpd_uri_t kRoot = {
      .uri = "/", .method = HTTP_GET, .handler = handleRoot, .user_ctx = nullptr};
  static const httpd_uri_t kSave = {
      .uri = "/save", .method = HTTP_POST, .handler = handleSave,
      .user_ctx = nullptr};
  static const httpd_uri_t kFavicon = {
      .uri = "/favicon.ico", .method = HTTP_GET, .handler = handleFavicon,
      .user_ctx = nullptr};

  ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &kRoot));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &kSave));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &kFavicon));
  return ESP_OK;
}

void buildApSsid() {
  uint8_t mac[6] = {};
  esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
  snprintf(s_apSsid, sizeof(s_apSsid), "%s-%02X%02X", kApPrefix, mac[4], mac[5]);
}

}  // namespace

// ------------------------------------------------------------------ NVS ----

bool load(Credentials& out) {
  out = Credentials{};
  nvs_handle_t handle = 0;
  if (openNvs(NVS_READONLY, handle) != ESP_OK) return false;

  std::size_t ssidLength = sizeof(out.ssid);
  std::size_t passwordLength = sizeof(out.password);
  const esp_err_t ssidResult =
      nvs_get_str(handle, kNvsSsidKey, out.ssid, &ssidLength);
  const esp_err_t passwordResult =
      nvs_get_str(handle, kNvsPasswordKey, out.password, &passwordLength);
  nvs_close(handle);

  if (ssidResult != ESP_OK) out.ssid[0] = '\0';
  if (passwordResult != ESP_OK) out.password[0] = '\0';
  return out.valid();
}

esp_err_t save(const Credentials& credentials) {
  nvs_handle_t handle = 0;
  esp_err_t result = openNvs(NVS_READWRITE, handle);
  if (result != ESP_OK) return result;

  result = nvs_set_str(handle, kNvsSsidKey, credentials.ssid);
  if (result == ESP_OK) {
    result = nvs_set_str(handle, kNvsPasswordKey, credentials.password);
  }
  if (result == ESP_OK) result = nvs_commit(handle);
  nvs_close(handle);
  return result;
}

esp_err_t clear() {
  nvs_handle_t handle = 0;
  esp_err_t result = openNvs(NVS_READWRITE, handle);
  if (result != ESP_OK) return result;

  result = nvs_erase_key(handle, kNvsSsidKey);
  if (result == ESP_ERR_NVS_NOT_FOUND) result = ESP_OK;
  esp_err_t passwordResult = nvs_erase_key(handle, kNvsPasswordKey);
  if (passwordResult == ESP_ERR_NVS_NOT_FOUND) passwordResult = ESP_OK;
  if (result == ESP_OK) result = passwordResult;
  if (result == ESP_OK) result = nvs_commit(handle);
  nvs_close(handle);
  return result;
}

// -------------------------------------------------------------- portal -----

bool running() { return s_running; }

const char* ssid() { return s_apSsid[0] != '\0' ? s_apSsid : kApPrefix; }

esp_err_t start() {
  if (s_running) return ESP_OK;
  buildApSsid();

  wifi_config_t apConfig = {};
  std::strncpy(reinterpret_cast<char*>(apConfig.ap.ssid), s_apSsid,
               sizeof(apConfig.ap.ssid) - 1);
  apConfig.ap.ssid_len = std::strlen(s_apSsid);
  apConfig.ap.channel = kApChannel;
  apConfig.ap.max_connection = kApMaxConnections;
  // Open AP: the only thing behind it is a Wi-Fi form, and a password the user
  // has to type from a screen that is 360 px wide is worse than the risk.
  apConfig.ap.authmode = WIFI_AUTH_OPEN;
  apConfig.ap.pmf_cfg.required = false;

  const esp_err_t stopResult = esp_wifi_stop();
  if (stopResult != ESP_OK && stopResult != ESP_ERR_WIFI_NOT_STARTED) {
    return stopResult;
  }

  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &apConfig));
  ESP_ERROR_CHECK(esp_wifi_start());

  const esp_err_t result = startHttpServer();
  if (result != ESP_OK) return result;

  s_running = true;
  ESP_LOGW(kTag, "setup portal up: SSID=%s  open http://192.168.4.1", s_apSsid);
  return ESP_OK;
}

}  // namespace config_portal
