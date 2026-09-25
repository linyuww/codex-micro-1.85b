// SPDX-License-Identifier: MIT
// Copyright (c) 2026 imliubo
// StopWatch port changes copyright (c) 2026 Codex Micro for StopWatch contributors
// Waveshare ESP32-S3-Touch-LCD-1.85B port copyright (c) 2026 Codex Micro port
//
// Codex Micro for the Waveshare ESP32-S3-Touch-LCD-1.85B.
//
// Board mapping versus the C152 StopWatch Dev Kit:
//   * 360x360 ST77916 instead of a 466x466 CO5300 -> dashboard rescaled
//   * one BOOT key (GPIO0) instead of two side keys plus a red power key
//   * BQ27220 fuel gauge instead of M5PM1 power telemetry
//   * ES8311 + I2S amplifier instead of the M5 internal speaker
//   * no vibration motor and no PMIC, so haptics are gone and "power off" is a
//     light-sleep screen-off state rather than a rail cut
//
// The six touch keys, the centre Send key, and the four swipe directions keep
// the original semantics exactly.

#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>

#include "battery.h"
#include "board_config.h"
#include "board_i2c.h"
#include "chime.h"
#include "codex_ble.h"
#include "dashboard_ui.h"
#include "display.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gfx.h"
#include "logic.h"
#include "touch_input.h"
#include "wifi_time.h"

namespace {

constexpr char kTag[] = "app";

// Reset history. RTC memory survives a software/panic/watchdog/brownout reset
// but not a power cycle, so a magic word tells us whether the recorded reason
// belongs to the reboot we just came out of.
RTC_NOINIT_ATTR uint32_t s_lastResetReason;
RTC_NOINIT_ATTR uint32_t s_resetReasonMagic;
RTC_NOINIT_ATTR uint32_t s_bootCount;
constexpr uint32_t kResetReasonMagic = 0x0C0DE3C0u;

const char* resetReasonName(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON:
      return "power-on";
    case ESP_RST_EXT:
      return "external pin";
    case ESP_RST_SW:
      return "software";
    case ESP_RST_PANIC:
      return "panic (exception/abort)";
    case ESP_RST_INT_WDT:
      return "interrupt watchdog";
    case ESP_RST_TASK_WDT:
      return "task watchdog";
    case ESP_RST_WDT:
      return "other watchdog";
    case ESP_RST_DEEPSLEEP:
      return "deep sleep wake";
    case ESP_RST_BROWNOUT:
      return "BROWNOUT (supply sagged)";
    case ESP_RST_SDIO:
      return "sdio";
    default:
      return "unknown";
  }
}

void reportResetHistory() {
  const esp_reset_reason_t reason = esp_reset_reason();
  if (s_resetReasonMagic == kResetReasonMagic) {
    ESP_LOGW(kTag, "PREVIOUS BOOT ENDED BY: %s (boot #%lu)",
             resetReasonName(static_cast<esp_reset_reason_t>(s_lastResetReason)),
             static_cast<unsigned long>(s_bootCount));
  }
  ESP_LOGI(kTag, "reset reason: %s", resetReasonName(reason));
  s_lastResetReason = static_cast<uint32_t>(reason);
  s_resetReasonMagic = kResetReasonMagic;
  ++s_bootCount;
}

constexpr uint32_t kQuotaStaleAfterMs = 180000;
constexpr uint32_t kHostRpcLiveForMs = 300000;
constexpr uint32_t kCompletionBannerMs = 5000;
constexpr uint32_t kVoiceTapBannerMs = 900;
constexpr uint32_t kVoiceClickPulseMs = 70;
constexpr uint32_t kTouchKeyPulseMs = 55;
constexpr int kSwipeThresholdPx = 40;

constexpr uint32_t kBatteryDimAfterMs = 120000;
constexpr uint32_t kBatterySleepAfterMs = 300000;
constexpr uint32_t kDockDimAfterMs = 600000;
constexpr uint32_t kDockSleepAfterMs = 1800000;
constexpr uint32_t kPowerTelemetryIntervalMs = 2000;
// Keep the BLE link visibly alive for the host between key presses. This is
// shorter than the 9.6 s supervision timeout Windows picks by a wide margin,
// so an idle dashboard cannot look like a dead link.
constexpr uint32_t kBatteryNotifyIntervalMs = 1000;

constexpr uint32_t kPowerHoldPromptMs = 2000;
constexpr uint32_t kPowerOffHoldMs = 6000;
constexpr uint32_t kButtonDoubleClickMs = 500;
constexpr uint32_t kMicHoldThresholdMs = 700;

// Bluetooth pairing gesture. The board has exactly one user key, so the
// pairing action shares it with push-to-talk and is separated by hold length.
//
// Holding past kPairingHoldMs drops the host link, forgets every stored bond
// and goes back to pairable advertising -- the software equivalent of holding
// the pairing key on a headset. That is the recovery action for the failure
// this port has fought since day one: a host that keeps a stale half of the
// bond, brings the link up unencrypted, and reports the device as connected
// with limited functionality.
constexpr uint32_t kPairingHoldMs = 3000;
// Keep holding and the board escalates. A host record can reach a state where
// it reports the device as unpaired while refusing to start a fresh pairing,
// and then no amount of bond clearing on this side helps. Storing a new bond
// generation and rebooting makes the board advertise an address the host has
// never seen, which it pairs cleanly as a brand-new device.
constexpr uint32_t kPairingHardResetHoldMs = 8000;
// How long the pairing window stays open. Long enough to walk to the host and
// open its Bluetooth settings, short enough that the board is not left
// advertising as pairable for the rest of the day.
constexpr uint32_t kPairingWindowMs = 120000;

// The ST77916 backlight is a direct LEDC channel, so these are percentages.
constexpr int kActiveBrightness = 90;
constexpr int kDimBrightness = 18;

// ChatGPT 26.727 exposes one combined Mic key but no independent ACT10/ACT11
// switch setting. Use the fourth configurable Command Key for Voice Chat.
constexpr char kMicSwitchKey[] = "ACT10";
constexpr char kVoiceChatCommandKey[] = "ACT09";
constexpr char kSendKey[] = "ACT12";
const char* kAgentKeys[] = {"AG00", "AG01", "AG02", "AG03", "AG04", "AG05"};

CodexMicroBle codex;
CodexMicroState state;
gfx::Canvas canvas;

uint32_t quotaWaitingSinceMs = 0;
uint32_t lastDrawMs = 0;
uint32_t lastPowerTelemetryMs = 0;
uint32_t lastBatteryNotifyMs = 0;

bool micPressed = false;
bool touchAgentPressed = false;
bool touchSendPressed = false;
bool touchTracking = false;
bool voiceTapBannerVisible = false;
bool voiceClickReleasePending = false;
int8_t activeTouchAgent = -1;
int16_t touchStartX = 0;
int16_t touchStartY = 0;
touch_gesture::Direction activeSwipe = touch_gesture::Direction::None;
uint32_t voiceTapBannerUntilMs = 0;
uint32_t voiceClickReleaseAtMs = 0;
uint32_t touchSendStartedAtMs = 0;
uint32_t lastPowerOverlayDrawMs = 0;

std::array<dashboard::AgentStatus, 6> previousAgentStatuses = {};
std::array<bool, 6> agentStatusObserved = {};
completion_banner::Timer completionBanner;

uint32_t lastActivityMs = 0;
int appliedBrightness = kActiveBrightness;
int8_t batteryPercent = -1;
bool charging = false;
bool externalPower = false;
bool discharging = false;
bool batteryTelemetryInitialized = false;
bool docked = false;
bool dockStateInitialized = false;
bool pendingDockState = false;
uint8_t pendingDockSamples = 0;
bool deskSleeping = false;
bool powerOffActive = false;

bool buttonPressed = false;
bool buttonRawPressed = false;
bool buttonStablePressed = false;
bool buttonWakeConsumed = false;
// Set when the current hold was spent on the pairing gesture, so releasing it
// must not also be read as a single click (which would send the Send key).
bool buttonPairingConsumed = false;
bool touchWakeConsumed = false;
uint32_t buttonRawChangedAtMs = 0;
constexpr uint32_t kButtonDebounceMs = 30;
uint32_t buttonPressedAtMs = 0;
bool micHoldActive = false;
button_gesture::Detector buttonGesture(kButtonDoubleClickMs);

// -------------------------------------------------------------- pairing --
bool pairingMode = false;
uint32_t pairingModeUntilMs = 0;
uint32_t pairingSecondsLeft = 0;
int pairingBondsDropped = 0;
// Set once the link has been observed down while pairing. See updatePairingMode.
bool pairingSawDisconnect = false;

bool touchPowerHoldConsumed = false;
dashboard::PowerOverlay powerOverlay = dashboard::PowerOverlay::None;

bool renderedHealthValid = false;
dashboard::LinkHealth renderedLinkHealth = dashboard::LinkHealth::Offline;
bool renderedQuotaStale = false;
int8_t renderedBatteryPercent = -1;
bool renderedExternalPower = false;
bool renderedTimeValid = false;
char renderedClock[8] = {};
bool renderedNight = false;
bool renderedSetupPortal = false;
bool renderedPairing = false;
uint32_t renderedPairingSeconds = 0;

// Refreshed once per loop iteration rather than per query, so the several
// callers inside one iteration all see the same instant.
wifi_time::Status networkStatus;
bool nightTheme = false;

void drawScreen();
void releaseControlsForPowerOff();
void enterPowerOff();
void beginVoiceChatClick();
void enterPairingMode();
void exitPairingMode(const char* reason);
void updatePairingMode();

// --------------------------------------------------------------- utilities --

uint32_t nowMs() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }

// The boot and error screens. These are diagnostics rather than UI, but they
// use the dashboard's own pixel font: Space Mono was the only other consumer of
// a 628 KB VLW blob, and dropping it takes that whole header out of the build.
void showMessage(const char* heading, const char* detail) {
  const gfx::Font& font = dashboard::pixelFont();
  canvas.fillScreen(dashboard::kBackground);
  canvas.drawTextInteger(font, heading, dashboard::kCenterX,
                         dashboard::kHeight / 2 - 16, gfx::Datum::MiddleCenter,
                         dashboard::kText, 2);
  canvas.drawTextInteger(font, detail, dashboard::kCenterX,
                         dashboard::kHeight / 2 + 16, gfx::Datum::MiddleCenter,
                         dashboard::kMuted, 1);
  board_display::flush(canvas.pixels());
}

// ------------------------------------------------------------- power policy --

void enterDeskSleep() {
  if (deskSleeping || powerOffActive || touchTracking) return;
  board_display::sleep();
  appliedBrightness = 0;
  deskSleeping = true;
  ESP_LOGI(kTag, "POWER desk_sleep dock=%d", docked ? 1 : 0);
}

void wakeDeskSleep() {
  if (!deskSleeping) return;
  deskSleeping = false;
  // Transfer the frame while the panel is still dark, then light it. Waking
  // first replays the stale framebuffer for a frame or two, which is exactly
  // what made a wake look like the screen flickering.
  drawScreen();
  board_display::wake();
  appliedBrightness = kActiveBrightness;
  board_display::set_brightness(appliedBrightness);
  ESP_LOGI(kTag, "POWER desk_wake");
}

// The 1.85B has no PMIC, so there is no rail to cut. "Power off" is a
// backlight-off idle state that the BOOT key or a touch wakes. Deep sleep is
// deliberately avoided: the only user button is GPIO0, which is the ROM
// strapping pin, so a GPIO0 reset would be able to drop the chip into the
// serial download loader instead of the application. Light sleep is avoided
// too: this build runs XIP from PSRAM and the power management framework is
// disabled, so light sleep would power PSRAM down underneath the running code.
void enterPowerOff() {
  touchPowerHoldConsumed = true;
  // A pairing window cannot outlive the screen going off: the user is no longer
  // looking at the instructions, and the countdown would have expired before
  // the screen came back anyway.
  pairingMode = false;
  pairingSecondsLeft = 0;
  powerOverlay = dashboard::PowerOverlay::PoweringOff;
  drawScreen();
  powerOffActive = true;
  vTaskDelay(pdMS_TO_TICKS(600));

  releaseControlsForPowerOff();
  board_display::sleep();
  chime::setEnabled(false);
  ESP_LOGI(kTag, "POWER screen_off wake=boot_button_or_touch");

  // Require release of the gesture that turned the screen off, then a NEW
  // press, then its release. Never interpret the held power gesture as wake.
  bool wakeArmed = false;
  bool wakeRequested = false;
  uint32_t releasedSince = 0;
  while (true) {
    codex.poll();  // Drain RPC/quota queues even while the screen is off.
    vTaskDelay(pdMS_TO_TICKS(20));
    const bool pressed = gpio_get_level(BOARD_BUTTON_BOOT) == 0 ||
                         touch_input::read().pressed;
    if (pressed) {
      releasedSince = 0;
      if (wakeArmed) wakeRequested = true;
      continue;
    }
    if (releasedSince == 0) releasedSince = nowMs();
    if (nowMs() - releasedSince < kButtonDebounceMs) continue;
    if (!wakeArmed) {
      wakeArmed = true;
      continue;
    }
    if (!wakeRequested) continue;
    buttonGesture.cancel();
    buttonPressed = buttonRawPressed = buttonStablePressed = false;
    buttonWakeConsumed = touchWakeConsumed = false;
    touchPowerHoldConsumed = false;
    touchTracking = false;
    powerOffActive = false;
    powerOverlay = dashboard::PowerOverlay::None;
    chime::setEnabled(true);
    lastActivityMs = nowMs();
    drawScreen();
    board_display::wake();
    appliedBrightness = kActiveBrightness;
    board_display::set_brightness(appliedBrightness);
    ESP_LOGI(kTag, "POWER screen_on");
    return;
  }
}

void releaseControlsForPowerOff() {
  if (micPressed) {
    codex.sendKey(kMicSwitchKey, 0);
    micPressed = false;
  }
  if (voiceClickReleasePending) {
    codex.sendKey(kVoiceChatCommandKey, 0);
    voiceClickReleasePending = false;
  }
  if (activeSwipe != touch_gesture::Direction::None) {
    codex.sendJoystick(touch_gesture::normalizedAngle(activeSwipe), 0.0f);
    activeSwipe = touch_gesture::Direction::None;
  }
  voiceTapBannerVisible = false;
  touchTracking = false;
  touchAgentPressed = false;
  touchSendPressed = false;
  activeTouchAgent = -1;
  codex.poll();
  vTaskDelay(pdMS_TO_TICKS(80));
  codex.poll();
}

// Returns false when the screen was fully off, so the waking tap can be
// swallowed instead of acting on a control the user could not see.
bool noteActivity() {
  // Deliberately does not cancel the button gesture. Touch and the BOOT key
  // are independent inputs, and a noisy touch panel (or a palm resting on it)
  // used to silently swallow BOOT single and double clicks.
  lastActivityMs = nowMs();
  if (powerOffActive) return false;
  if (deskSleeping) {
    wakeDeskSleep();
    return false;
  }
  if (appliedBrightness == kActiveBrightness) return true;
  const bool wasOff = appliedBrightness == 0;
  appliedBrightness = kActiveBrightness;
  board_display::set_brightness(kActiveBrightness);
  if (wasOff) drawScreen();
  return !wasOff;
}

void updateIdleDimming() {
  if (powerOffActive) return;
  // A held key or an ongoing touch counts as continuous use.
  if (micPressed || touchTracking) lastActivityMs = nowMs();
  const uint32_t idleMs = nowMs() - lastActivityMs;
  const uint32_t dimAfterMs = docked ? kDockDimAfterMs : kBatteryDimAfterMs;
  const uint32_t sleepAfterMs = docked ? kDockSleepAfterMs : kBatterySleepAfterMs;
  if (idleMs >= sleepAfterMs) {
    enterDeskSleep();
    return;
  }
  if (deskSleeping) return;
  const int target = idleMs >= dimAfterMs ? kDimBrightness : kActiveBrightness;
  if (target != appliedBrightness) {
    appliedBrightness = target;
    board_display::set_brightness(target);
    ESP_LOGI(kTag, "POWER brightness=%d idle=%lus docked=%d", target,
             static_cast<unsigned long>(idleMs / 1000), docked ? 1 : 0);
  }
}

// --------------------------------------------------------------- telemetry --

dashboard::ThreadVisual threadVisual(const ThreadLight& light) {
  dashboard::ThreadVisual visual;
  visual.color = light.color;
  visual.brightness = light.brightness;
  // Current Codex Desktop marks the focused slot with s=0.4 while e remains
  // "off". Keep accepting the older explicit breath effect as a focus signal.
  visual.focused =
      strcmp(light.effect, "breath") == 0 || light.speed > 0.01f;
  return visual;
}

dashboard::AgentStatus classifyAgent(const ThreadLight& light) {
  return dashboard::classify(threadVisual(light));
}

uint32_t fiveHourResetRemaining() {
  if (!state.quota.available) return 0;
  // A snapshot restored from NVS has no anchor: the board cannot know how long
  // it was powered off, so there is no honest countdown to show. The dashboard
  // renders STALE instead of a number in that case.
  if (state.quota.restored) return 0;
  const uint32_t elapsed = (nowMs() - state.quota.receivedAtMs) / 1000;
  return elapsed >= state.quota.fiveHourResetInSeconds
             ? 0
             : state.quota.fiveHourResetInSeconds - elapsed;
}

connection_health::Result connectionHealth() {
  connection_health::Input input;
  input.bleConnected = state.connected;
  input.hostRpcObserved = state.hostRpcObserved;
  input.lastHostRpcAtMs = state.lastHostRpcAtMs;
  input.quotaWaitingSinceMs = quotaWaitingSinceMs;
  input.quotaAvailable = state.quota.available;
  input.quotaReceivedAtMs = state.quota.receivedAtMs;
  // A value restored from NVS is stale by definition -- it was written before
  // the last power cycle. Backdate the receive instant past the TTL so the
  // shared state machine classifies it as Stale rather than Fresh, and the dial
  // says so plainly instead of showing a countdown it cannot justify.
  if (state.quota.available && state.quota.restored) {
    input.quotaReceivedAtMs = nowMs() - (kQuotaStaleAfterMs + 1);
  }
  return connection_health::evaluate(input, nowMs(), kHostRpcLiveForMs,
                                     kQuotaStaleAfterMs);
}

dashboard::LinkHealth dashboardLinkHealth(connection_health::Link link) {
  switch (link) {
    case connection_health::Link::CodexLive:
      return dashboard::LinkHealth::CodexLive;
    case connection_health::Link::BleOnly:
      return dashboard::LinkHealth::BleOnly;
    case connection_health::Link::Offline:
      return dashboard::LinkHealth::Offline;
  }
  return dashboard::LinkHealth::Offline;
}

void observeDockState(bool candidate) {
  if (dockStateInitialized && candidate == docked) {
    pendingDockSamples = 0;
    return;
  }
  if (pendingDockSamples == 0 || pendingDockState != candidate) {
    pendingDockState = candidate;
    pendingDockSamples = 1;
    return;
  }
  if (++pendingDockSamples < 2) return;

  const bool changed = !dockStateInitialized || docked != candidate;
  docked = candidate;
  dockStateInitialized = true;
  pendingDockSamples = 0;
  if (!changed) return;

  ESP_LOGI(kTag, "POWER mode=%s", docked ? "dock" : "battery");
  if (docked) {
    // Plugging into a dock is intentional activity and should reveal the
    // externally-powered state even if the panel was already in desk sleep.
    lastActivityMs = nowMs();
    if (deskSleeping) wakeDeskSleep();
  }
}

void updatePowerTelemetry(bool force = false) {
  const uint32_t now = nowMs();
  bool batteryChanged = false;

  if (force || lastPowerTelemetryMs == 0 ||
      now - lastPowerTelemetryMs >= kPowerTelemetryIntervalMs) {
    lastPowerTelemetryMs = now;
    const battery::Sample sample = battery::read(now);
    const int8_t nextLevel =
        sample.percent < 0 ? -1 : static_cast<int8_t>(sample.percent);
    batteryChanged = batteryPercent != nextLevel || charging != sample.charging;
    const bool telemetryChanged =
        !batteryTelemetryInitialized || batteryChanged ||
        externalPower != sample.externalPower ||
        discharging != sample.discharging;
    batteryPercent = nextLevel;
    charging = sample.charging;
    externalPower = sample.externalPower;
    discharging = sample.discharging;
    batteryTelemetryInitialized = true;
    observeDockState(externalPower);

    if (telemetryChanged) {
      ESP_LOGI(kTag,
               "BATTERY level=%d current=%d mA dsg=%d charging=%d external=%d",
               batteryPercent, sample.currentMa, discharging ? 1 : 0,
               charging ? 1 : 0, externalPower ? 1 : 0);
    }
  }

  // Re-publish the battery level periodically even when it has not changed.
  // The host only sees link activity when we notify, and a completely silent
  // peripheral is what makes the host drop an idle link; a real HID device is
  // never fully quiet.
  //
  // This runs even when the gauge cannot be read. Keying the keepalive off a
  // valid battery level meant one bad fuel gauge reading was enough to go
  // silent for good, and Windows then tore the link down on its supervision
  // timeout every ~18 s and reconnected, forever.
  if (batteryChanged ||
      now - lastBatteryNotifyMs >= kBatteryNotifyIntervalMs) {
    lastBatteryNotifyMs = now;
    const uint8_t level =
        batteryPercent >= 0 ? static_cast<uint8_t>(batteryPercent) : 0;
    codex.setBattery(level, charging);
  }
}

// -------------------------------------------------------------------- touch --

// Pulls the network clock forward and re-evaluates the theme from it.
//
// The theme deliberately outlives a failed evaluation. Before the first NTP
// sync the hour is meaningless, and running the rule against it would read 0
// and force the night theme on every cold boot -- so until a sync lands the
// last known theme is held, defaulting to day, which is both brighter and the
// more honest "the device is working" state. This is the one place the rule is
// allowed to be evaluated, and it is guarded by hasTime.
void refreshNetwork() {
  // snapshot() copies two strings and calls time() to decide hasTime, and the
  // main loop runs every 8 ms. None of it can change faster than the clock's
  // minute, so rebuilding it 125 times a second is pure waste; a quarter of a
  // second is well below anything a person can perceive here.
  constexpr uint32_t kRefreshIntervalMs = 250;
  static uint32_t lastRefreshMs = 0;
  const uint32_t now = nowMs();
  if (lastRefreshMs != 0 && now - lastRefreshMs < kRefreshIntervalMs) return;
  lastRefreshMs = now;

  networkStatus = wifi_time::snapshot();
  if (networkStatus.hasTime) {
    nightTheme = theme::isNight(networkStatus.now.hour);
  }
}

dashboard::State dashboardState() {
  dashboard::State ui;
  const connection_health::Result health = connectionHealth();
  ui.linkHealth = dashboardLinkHealth(health.link);
  ui.batteryPercent = batteryPercent;
  ui.externalPower = docked;
  ui.quotaAvailable = state.quota.available;
  ui.quotaStale = health.quota == connection_health::Quota::Stale;
  ui.fiveHourRemainingPercent = state.quota.fiveHourRemainingPercent;
  ui.fiveHourResetInSeconds = fiveHourResetRemaining();
  ui.weeklyRemainingPercent = state.quota.weeklyRemainingPercent;
  ui.micPressed = micPressed;
  ui.voicePressed = voiceTapBannerVisible;
  ui.sendPressed = touchSendPressed;
  ui.activeTouchAgent = touchAgentPressed ? activeTouchAgent : -1;
  ui.swipeDirection = static_cast<int>(activeSwipe);
  ui.completedAgent =
      completionBanner.visible(nowMs()) ? completionBanner.agent : -1;
  ui.powerOverlay = powerOverlay;
  // State's own defaults are the "no clock yet" placeholders, so the fields are
  // only overwritten once there is a real time to show.
  ui.timeValid = networkStatus.hasTime;
  if (networkStatus.hasTime) {
    wifi_time::formatClock(networkStatus.now, ui.clock, sizeof(ui.clock));
    wifi_time::formatDate(networkStatus.now, ui.date, sizeof(ui.date));
  }
  ui.night = nightTheme;
  ui.setupPortal = networkStatus.portalRunning;
  if (networkStatus.portalRunning) {
    std::snprintf(ui.setupSsid, sizeof(ui.setupSsid), "%s", networkStatus.apSsid);
  }
  ui.pairing = pairingMode;
  ui.pairingSecondsLeft = pairingSecondsLeft;
  if (touchPowerHoldConsumed) {
    const uint32_t heldMs = nowMs() - touchSendStartedAtMs;
    ui.powerHoldProgress = std::max(
        0.0f,
        std::min(1.0f, static_cast<float>(heldMs - kPowerHoldPromptMs) /
                           static_cast<float>(kPowerOffHoldMs -
                                              kPowerHoldPromptMs)));
  }
  for (int i = 0; i < 6; ++i) ui.threads[i] = threadVisual(state.threads[i]);
  return ui;
}

void drawScreen() {
  if (deskSleeping || powerOffActive) return;
  const dashboard::State ui = dashboardState();
  dashboard::render(canvas, ui);
  board_display::flush(canvas.pixels());
  lastDrawMs = nowMs();
  renderedHealthValid = true;
  renderedLinkHealth = ui.linkHealth;
  renderedQuotaStale = ui.quotaStale;
  renderedBatteryPercent = ui.batteryPercent;
  renderedExternalPower = ui.externalPower;
  renderedTimeValid = ui.timeValid;
  renderedNight = ui.night;
  renderedSetupPortal = ui.setupPortal;
  renderedPairing = ui.pairing;
  renderedPairingSeconds = ui.pairingSecondsLeft;
  std::snprintf(renderedClock, sizeof(renderedClock), "%s", ui.clock);
}

void beginTouchAgent(int8_t agent) {
  if (agent < 0 || agent >= 6) return;
  touchAgentPressed = true;
  activeTouchAgent = agent;
  drawScreen();
}

void commitTouchAgent() {
  if (!touchAgentPressed || activeTouchAgent < 0) return;
  const int8_t agent = activeTouchAgent;
  codex.sendKey(kAgentKeys[agent], 1, agent);
  vTaskDelay(pdMS_TO_TICKS(kTouchKeyPulseMs));
  codex.sendKey(kAgentKeys[agent], 0, agent);
  touchAgentPressed = false;
  activeTouchAgent = -1;
  drawScreen();
}

void beginTouchSend() {
  if (touchSendPressed) return;
  touchSendPressed = true;
  touchSendStartedAtMs = nowMs();
  touchPowerHoldConsumed = false;
  drawScreen();
}

void commitTouchSend() {
  if (!touchSendPressed) return;
  codex.sendKey(kSendKey, 1);
  vTaskDelay(pdMS_TO_TICKS(kTouchKeyPulseMs));
  codex.sendKey(kSendKey, 0);
  touchSendPressed = false;
  drawScreen();
}

void clearTouchCandidate() {
  touchAgentPressed = false;
  touchSendPressed = false;
  activeTouchAgent = -1;
}

void beginTouchGesture(int x, int y) {
  touchTracking = true;
  touchStartX = static_cast<int16_t>(x);
  touchStartY = static_cast<int16_t>(y);
  activeSwipe = touch_gesture::Direction::None;

  const int agent = dashboard::agentAtPoint(x, y);
  ESP_LOGI(kTag, "TOUCH begin x=%d y=%d agent=%d send=%d", x, y, agent,
           dashboard::sendAtPoint(x, y) ? 1 : 0);
  if (agent >= 0) {
    beginTouchAgent(static_cast<int8_t>(agent));
  } else if (dashboard::sendAtPoint(x, y)) {
    beginTouchSend();
  }
}

void updateTouchGesture(int x, int y) {
  if (!touchTracking || touchPowerHoldConsumed ||
      activeSwipe != touch_gesture::Direction::None) {
    return;
  }
  const touch_gesture::Direction direction = touch_gesture::classifySwipe(
      x - touchStartX, y - touchStartY, kSwipeThresholdPx);
  if (direction == touch_gesture::Direction::None) return;

  clearTouchCandidate();
  activeSwipe = direction;
  const float angle = touch_gesture::normalizedAngle(direction);
  codex.sendJoystick(angle, 1.0f);
  ESP_LOGI(kTag, "SWIPE direction=%s angle=%.2f action=press",
           touch_gesture::name(direction), angle);
  drawScreen();
}

void finishTouchGesture() {
  if (!touchTracking) return;
  touchTracking = false;

  if (touchPowerHoldConsumed) {
    touchPowerHoldConsumed = false;
    powerOverlay = dashboard::PowerOverlay::None;
    clearTouchCandidate();
    drawScreen();
    return;
  }

  if (activeSwipe != touch_gesture::Direction::None) {
    const touch_gesture::Direction direction = activeSwipe;
    const float angle = touch_gesture::normalizedAngle(direction);
    codex.sendJoystick(angle, 0.0f);
    ESP_LOGI(kTag, "SWIPE direction=%s angle=%.2f action=release",
             touch_gesture::name(direction), angle);
    activeSwipe = touch_gesture::Direction::None;
    clearTouchCandidate();
    drawScreen();
    return;
  }

  if (touchAgentPressed) {
    commitTouchAgent();
  } else if (touchSendPressed) {
    commitTouchSend();
  } else {
    clearTouchCandidate();
  }
}

void updateTouchPowerHold() {
  if (!touchTracking || activeSwipe != touch_gesture::Direction::None) return;
  if (!touchSendPressed && !touchPowerHoldConsumed) return;

  const uint32_t now = nowMs();
  const uint32_t heldMs = now - touchSendStartedAtMs;
  if (!touchPowerHoldConsumed && heldMs >= kPowerHoldPromptMs) {
    touchPowerHoldConsumed = true;
    touchSendPressed = false;  // releasing now must not emit Send
    powerOverlay = dashboard::PowerOverlay::HoldToPowerOff;
    lastPowerOverlayDrawMs = 0;
  }
  if (!touchPowerHoldConsumed) return;
  if (heldMs >= kPowerOffHoldMs) enterPowerOff();
  if (lastPowerOverlayDrawMs == 0 || now - lastPowerOverlayDrawMs >= 100) {
    lastPowerOverlayDrawMs = now;
    drawScreen();
  }
}

// ------------------------------------------------------------------ pairing --

// Entering pairing mode is deliberately destructive, because that is what the
// situation calls for: the only reason to ask for it is that the existing
// pairing is unusable. Dropping this board's half of the bond is the one
// recovery step that does not need a serial console, a reflash, or a working
// Windows Settings entry.
void enterPairingMode() {
  const int removed = codex.enterPairingMode();
  pairingBondsDropped = removed;
  pairingMode = true;
  pairingSawDisconnect = false;
  pairingModeUntilMs = nowMs() + kPairingWindowMs;
  pairingSecondsLeft = kPairingWindowMs / 1000;
  // The pairing prompt is the whole point of the gesture, so it must be lit
  // even if the device was dimmed when the key was pressed.
  lastActivityMs = nowMs();
  ESP_LOGW(kTag, "PAIRING on: bonds_dropped=%d window=%lus advertising=%d",
           removed, static_cast<unsigned long>(kPairingWindowMs / 1000),
           codex.advertising() ? 1 : 0);
  drawScreen();
}

void exitPairingMode(const char* reason) {
  if (!pairingMode) return;
  pairingMode = false;
  pairingSecondsLeft = 0;
  ESP_LOGI(kTag, "PAIRING off: %s (bonds_dropped=%d)", reason,
           pairingBondsDropped);
  drawScreen();
}

void updatePairingMode() {
  if (!pairingMode) return;

  // A board that is advertising and waiting to be paired is in use by
  // definition, so it must not dim or fall asleep under the user's hands.
  lastActivityMs = nowMs();

  // The success condition is a *new* connection, not merely a live one. The
  // gesture is normally used while the old host is still attached -- that is
  // the whole point -- so `state.connected` is still true for the few hundred
  // milliseconds it takes the forced disconnect to land. Exiting on that would
  // close the window before the user ever got to the host's settings screen.
  // Waiting for the link to be observed down first makes the two cases
  // distinguishable without depending on event timing.
  if (!state.connected) pairingSawDisconnect = true;

  if (pairingSawDisconnect && state.connected) {
    exitPairingMode("host connected");
    return;
  }

  const uint32_t now = nowMs();
  if (static_cast<int32_t>(now - pairingModeUntilMs) >= 0) {
    exitPairingMode("window expired");
    return;
  }
  pairingSecondsLeft = (pairingModeUntilMs - now + 999) / 1000;
}

// ------------------------------------------------------------------ button --

void beginButtonPress() {
  buttonPressed = true;
  buttonPressedAtMs = nowMs();
  lastActivityMs = buttonPressedAtMs;
  micHoldActive = false;
  buttonPairingConsumed = false;
  buttonWakeConsumed = deskSleeping;
  if (buttonWakeConsumed) {
    buttonGesture.cancel();
    wakeDeskSleep();
  }
  // The rest of the gesture is logged on release; this line exists because a
  // press that appears to blank the screen has to be told apart from a press
  // that merely woke it.
  ESP_LOGI(kTag, "BUTTON down desk_sleep=%d power_off=%d brightness=%d",
           deskSleeping ? 1 : 0, powerOffActive ? 1 : 0, appliedBrightness);
}

void updateButtonHold() {
  if (!buttonPressed || buttonWakeConsumed) return;
  const uint32_t heldMs = nowMs() - buttonPressedAtMs;

  // The pairing thresholds are tested before the push-to-talk early-return, so
  // that a hold which keeps going past 3 s still ends in pairing mode rather
  // than leaving the microphone engaged. Push-to-talk has already fired by
  // then, at 700 ms; the pairing step releases it again on the way in.
  if (pairingMode) {
    // Already pairing: a further hold escalates to a fresh Bluetooth address.
    if (heldMs < kPairingHardResetHoldMs) return;
    buttonPairingConsumed = true;
    ESP_LOGW(kTag, "BUTTON hold action=bond_generation_reset hold=%lums",
             static_cast<unsigned long>(heldMs));
    if (!codex.resetBondGenerationAndRestart()) {
      ESP_LOGE(kTag, "bond generation reset failed; staying on this address");
    }
    return;
  }
  if (heldMs >= kPairingHoldMs) {
    buttonPairingConsumed = true;
    if (micHoldActive) {
      // The mic was engaged by this same hold. Release it before switching
      // modes, or the host is left with a stuck push-to-talk.
      micHoldActive = false;
      micPressed = false;
      codex.sendKey(kMicSwitchKey, 0);
      ESP_LOGI(kTag, "BUTTON hold key=%s action=release reason=pairing",
               kMicSwitchKey);
    }
    buttonGesture.cancel();  // A hold cancels a pending first click.
    enterPairingMode();
    return;
  }

  if (micHoldActive) return;
  if (heldMs < kMicHoldThresholdMs) return;
  // Holding the key is the microphone push-to-talk gesture, mirroring the C152
  // left key. It fires once the double-click window has clearly expired.
  buttonGesture.cancel();  // A hold cancels a pending first click.
  micHoldActive = true;
  micPressed = true;
  codex.sendKey(kMicSwitchKey, 1);
  ESP_LOGI(kTag, "BUTTON hold key=%s action=press", kMicSwitchKey);
  drawScreen();
}

void finishButtonPress() {
  if (!buttonPressed) return;
  buttonPressed = false;
  const uint32_t heldMs = nowMs() - buttonPressedAtMs;
  if (buttonWakeConsumed) {
    buttonWakeConsumed = false;
    buttonGesture.cancel();
    ESP_LOGI(kTag, "BUTTON wake_only");
    return;
  }

  // The hold was spent on the pairing gesture, so its release is not a click.
  if (buttonPairingConsumed) {
    buttonPairingConsumed = false;
    buttonGesture.cancel();
    ESP_LOGI(kTag, "BUTTON pairing_hold hold=%lums",
             static_cast<unsigned long>(heldMs));
    return;
  }

  if (micHoldActive) {
    micHoldActive = false;
    micPressed = false;
    codex.sendKey(kMicSwitchKey, 0);
    ESP_LOGI(kTag, "BUTTON hold key=%s action=release hold=%lums",
             kMicSwitchKey, static_cast<unsigned long>(heldMs));
    drawScreen();
    return;
  }

  if (buttonGesture.release(nowMs()) == button_gesture::Event::DoubleClick) {
    // Mirrors the C152 right key, which issues a short Voice Chat click rather
    // than a press/release pair.
    ESP_LOGI(kTag, "BUTTON double_click key=%s", kVoiceChatCommandKey);
    voiceTapBannerVisible = true;
    voiceTapBannerUntilMs = nowMs() + kVoiceTapBannerMs;
    beginVoiceChatClick();
    drawScreen();
  }
}

void updateButtonGesture() {
  const button_gesture::Event delayed = buttonGesture.poll(nowMs());
  if (delayed != button_gesture::Event::SingleClick) return;
  if (deskSleeping) return;
  // During the pairing window the board is deliberately unattached, so a
  // single click has nothing to send to. Swallowing it also means an
  // accidental tap cannot look like the gesture did something.
  if (pairingMode) return;
  ESP_LOGI(kTag, "BUTTON single_click key=%s", kSendKey);
  codex.sendKey(kSendKey, 1);
  vTaskDelay(pdMS_TO_TICKS(kTouchKeyPulseMs));
  codex.sendKey(kSendKey, 0);
}

void updateButton() {
  const uint32_t now = nowMs();
  const bool raw = gpio_get_level(BOARD_BUTTON_BOOT) == 0;
  if (raw != buttonRawPressed) {
    buttonRawPressed = raw;
    buttonRawChangedAtMs = now;
  }
  if (now - buttonRawChangedAtMs >= kButtonDebounceMs &&
      buttonStablePressed != buttonRawPressed) {
    buttonStablePressed = buttonRawPressed;
    if (buttonStablePressed) beginButtonPress();
    else finishButtonPress();
  }
  if (buttonStablePressed) updateButtonHold();
  if (!buttonPressed && !buttonRawPressed) updateButtonGesture();
}

// ------------------------------------------------------------------- voice --

void beginVoiceChatClick() {
  if (voiceClickReleasePending) {
    codex.sendKey(kVoiceChatCommandKey, 0);
  }
  codex.sendKey(kVoiceChatCommandKey, 1);
  voiceClickReleasePending = true;
  voiceClickReleaseAtMs = nowMs() + kVoiceClickPulseMs;
}

void updateVoiceChatClick() {
  if (!voiceClickReleasePending) return;
  if (static_cast<int32_t>(nowMs() - voiceClickReleaseAtMs) < 0) return;
  codex.sendKey(kVoiceChatCommandKey, 0);
  voiceClickReleasePending = false;
}

void updateVoiceTapBanner() {
  if (!voiceTapBannerVisible) return;
  if (static_cast<int32_t>(nowMs() - voiceTapBannerUntilMs) < 0) return;
  voiceTapBannerVisible = false;
  drawScreen();
}

// --------------------------------------------------------------- completion --

void startCompletionChime(int8_t agent) {
  noteActivity();  // a finished agent is worth lighting the screen for
  completionBanner.show(agent, nowMs(), kCompletionBannerMs);
  chime::playCompletion();
  ESP_LOGI(kTag, "Agent %d completed; soft chime played", agent + 1);
}

void detectAgentTransitions(const CodexMicroState& latest) {
  for (int i = 0; i < 6; ++i) {
    const dashboard::AgentStatus next = classifyAgent(latest.threads[i]);
    if (agentStatusObserved[i] &&
        previousAgentStatuses[i] != dashboard::AgentStatus::Complete &&
        next == dashboard::AgentStatus::Complete) {
      startCompletionChime(static_cast<int8_t>(i));
    }
    // Requires-input also wakes the screen; routine status refreshes must not,
    // or host polling would keep the panel lit around the clock.
    if (agentStatusObserved[i] && previousAgentStatuses[i] != next &&
        next == dashboard::AgentStatus::RequiresInput) {
      noteActivity();
    }
    previousAgentStatuses[i] = next;
    agentStatusObserved[i] = true;
  }
}

// ------------------------------------------------------------------- startup --

void configureButton() {
  const gpio_config_t config = {
      .pin_bit_mask = 1ULL << BOARD_BUTTON_BOOT,
      .mode = GPIO_MODE_INPUT,
      .pull_up_en = GPIO_PULLUP_ENABLE,
      .pull_down_en = GPIO_PULLDOWN_DISABLE,
      .intr_type = GPIO_INTR_DISABLE,
  };
  gpio_config(&config);
}

// ESP-IDF has no implicit main loop; the application owns it. This runs on the
// main task for the lifetime of the firmware.
[[noreturn]] void runLoop() {
  for (;;) {
    codex.poll();
    // Both are cheap and non-blocking: poll() only nudges a reconnect or starts
    // SNTP, and refreshNetwork() is a struct copy plus, once a minute, the
    // theme rule.
    wifi_time::poll();
    refreshNetwork();
    updateButton();

    const touch_input::Sample touch = touch_input::read();
    if (touchWakeConsumed) {
      if (!touch.pressed) touchWakeConsumed = false;
    } else if (touch.pressed && !touchTracking) {
      if (noteActivity()) {
        beginTouchGesture(touch.x, touch.y);
      } else {
        touchWakeConsumed = true;
      }
    } else if (touch.pressed && touchTracking) {
      updateTouchGesture(touch.x, touch.y);
      updateTouchPowerHold();
    } else if (!touch.pressed && touchTracking) {
      finishTouchGesture();
    }

    CodexMicroState latest = codex.snapshot();
    const bool shouldRedraw = latest.dirty;
    if (shouldRedraw) {
      detectAgentTransitions(latest);
    }
    state = latest;
    updatePairingMode();
    updatePowerTelemetry();
    const bool completionBannerExpired = completionBanner.expire(nowMs());

    const dashboard::State currentUi = dashboardState();
    const bool derivedStateChanged =
        !renderedHealthValid || currentUi.linkHealth != renderedLinkHealth ||
        currentUi.quotaStale != renderedQuotaStale ||
        currentUi.batteryPercent != renderedBatteryPercent ||
        currentUi.externalPower != renderedExternalPower ||
        // The clock ticks on its own, once a minute, with no event behind it,
        // and the theme flips twice a day. Comparing the formatted clock rather
        // than the minute keeps this to one string compare and catches the
        // transition from the "--:--" placeholder to a real time as well.
        currentUi.timeValid != renderedTimeValid ||
        currentUi.night != renderedNight ||
        currentUi.setupPortal != renderedSetupPortal ||
        // The pairing prompt counts down on its own, so it needs the same
        // treatment as the clock: compare the value that is actually drawn.
        currentUi.pairing != renderedPairing ||
        currentUi.pairingSecondsLeft != renderedPairingSeconds ||
        strcmp(currentUi.clock, renderedClock) != 0;
    if (!deskSleeping && !powerOffActive &&
        (shouldRedraw || derivedStateChanged || completionBannerExpired)) {
      drawScreen();
    }

    // The quota countdown and the clock are the only values that drift without
    // an event. The clock is caught by the string compare above as soon as its
    // minute turns; this slower sweep exists for the countdown, which changes
    // every minute too but is a number rather than a string.
    if (!deskSleeping && !powerOffActive && appliedBrightness > 0 &&
        state.quota.available && nowMs() - lastDrawMs > 30000) {
      drawScreen();
    }

    updateIdleDimming();
    updateVoiceTapBanner();
    updateVoiceChatClick();
    vTaskDelay(pdMS_TO_TICKS(8));
  }
}

}  // namespace

extern "C" void app_main(void) {
  reportResetHistory();
  ESP_LOGI(kTag, "Codex Micro Waveshare ESP32-S3-Touch-LCD-1.85B boot");

  if (!canvas.begin()) {
    ESP_LOGE(kTag, "framebuffer allocation failed");
    return;
  }
  canvas.fillScreen(dashboard::kBackground);

  esp_err_t result = board_display::init();
  if (result != ESP_OK) {
    ESP_LOGE(kTag, "display init failed: %s", esp_err_to_name(result));
    return;
  }
  board_display::set_brightness(kActiveBrightness);
  showMessage("CODEX MICRO", "DISPLAY OK");

  result = board_i2c::init();
  if (result != ESP_OK) {
    ESP_LOGE(kTag, "i2c init failed: %s", esp_err_to_name(result));
  }

  configureButton();

  const esp_err_t touchStatus = touch_input::init();
  if (touchStatus != ESP_OK) {
    ESP_LOGW(kTag, "touch init failed: %s", esp_err_to_name(touchStatus));
  }

  const esp_err_t batteryStatus = battery::init();
  if (batteryStatus != ESP_OK) {
    ESP_LOGW(kTag, "fuel gauge unavailable: %s", esp_err_to_name(batteryStatus));
  }

  const esp_err_t chimeStatus = chime::init();
  if (chimeStatus != ESP_OK) {
    ESP_LOGW(kTag, "chime unavailable: %s", esp_err_to_name(chimeStatus));
  }

  quotaWaitingSinceMs = nowMs();
  lastActivityMs = nowMs();

  result = codex.begin();
  if (result != ESP_OK) {
    ESP_LOGW(kTag, "BLE bring-up failed: %s", esp_err_to_name(result));
  }

  // BLE owns the Codex Micro identity. It restores the quota cache, derives the
  // stable Bluetooth address, and migrates stale bonds before the shared radio
  // is touched by Wi-Fi. Bring networking up only after that identity is fixed;
  // otherwise Windows can try to resume an old bond and leave the HID/quota
  // characteristics unusable after reboot.
  const esp_err_t wifiStatus = wifi_time::begin();
  if (wifiStatus != ESP_OK) {
    ESP_LOGW(kTag, "network bring-up failed: %s", esp_err_to_name(wifiStatus));
  }
  refreshNetwork();

  state = codex.snapshot();
  updatePowerTelemetry(true);
  drawScreen();
  ESP_LOGI(kTag, "CODEX_MICRO_WAVESHARE_1_85B_READY clock=%s theme=%s",
           networkStatus.hasTime ? "synced" : "pending",
           theme::name(nightTheme));

  runLoop();
}
