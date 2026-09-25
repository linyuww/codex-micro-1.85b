#!/usr/bin/env python3
"""Tests for windows_companion.py.

Run from this directory:

    python test_windows_companion.py            # hermetic unit tests
    set CX_LIVE=1 && python test_windows_companion.py   # + live App Server / PS bridge

The hermetic tests never touch Bluetooth, the network, or the Codex account.
The live tests read both real allowance windows through the real App Server and
exercise the PowerShell event bridge; they never write to the board.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import os
import unittest

import windows_companion as wc


# --------------------------------------------------------------------------
# Fixtures: the exact shape returned by codex-cli 0.154.0 on 2026-09-18
# --------------------------------------------------------------------------


def rate_limits_result(
    primary_used: float = 0,
    primary_minutes: int = 300,
    secondary_used: float = 100,
    secondary_minutes: int = 10080,
    primary_reset_at: int = 1_789_818_000,
    weekly_reset_at: int = 1_790_300_000,
) -> dict:
    bucket = {
        "limitId": "codex",
        "limitName": None,
        "primary": {
            "usedPercent": primary_used,
            "windowDurationMins": primary_minutes,
            "resetsAt": primary_reset_at,
        },
        "secondary": {
            "usedPercent": secondary_used,
            "windowDurationMins": secondary_minutes,
            "resetsAt": weekly_reset_at,
        },
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "plus",
    }
    return {
        "ordinaryUsageAllowed": True,
        "rateLimits": copy.deepcopy(bucket),
        "rateLimitsByLimitId": {"codex": copy.deepcopy(bucket)},
        "rateLimitResetCredits": {"availableCount": 0, "credits": []},
        "accountId": "00000000-0000-0000-0000-000000000000",
    }


# --------------------------------------------------------------------------
# Quota window selection
# --------------------------------------------------------------------------


class QuotaWindowTests(unittest.TestCase):
    def test_selects_both_windows_by_duration(self):
        snapshot = wc.build_snapshot(rate_limits_result(), now=1_789_800_000)
        self.assertEqual(snapshot["_source"]["five_hour"]["slot"], "primary")
        self.assertEqual(snapshot["_source"]["weekly"]["slot"], "secondary")
        self.assertEqual(snapshot["five_hour_remaining_percent"], 100)
        self.assertEqual(snapshot["weekly_remaining_percent"], 0)
        self.assertEqual(snapshot["five_hour_reset_in_seconds"], 18_000)
        self.assertEqual(snapshot["weekly_reset_in_seconds"], 500_000)

    def test_selects_swapped_slots(self):
        result = rate_limits_result(primary_minutes=10080, secondary_minutes=300)
        snapshot = wc.build_snapshot(result, now=1_789_800_000)
        self.assertEqual(snapshot["_source"]["weekly"]["slot"], "primary")
        self.assertEqual(snapshot["_source"]["five_hour"]["slot"], "secondary")

    def test_missing_weekly_window_fails(self):
        result = rate_limits_result()
        result["rateLimitsByLimitId"]["codex"]["secondary"] = None
        with self.assertRaises(wc.CompanionError) as ctx:
            wc.build_snapshot(result, now=1_789_800_000)
        self.assertIn("weekly", str(ctx.exception))

    def test_missing_five_hour_window_fails(self):
        result = rate_limits_result()
        result["rateLimitsByLimitId"]["codex"]["primary"] = None
        with self.assertRaises(wc.CompanionError) as ctx:
            wc.build_snapshot(result, now=1_789_800_000)
        self.assertIn("5-hour", str(ctx.exception))

    def test_missing_windows_entirely_fails(self):
        result = rate_limits_result()
        bucket = result["rateLimitsByLimitId"]["codex"]
        bucket.pop("primary")
        bucket.pop("secondary")
        with self.assertRaises(wc.CompanionError):
            wc.build_snapshot(result, now=0)

    def test_remaining_is_inverted_used_percent(self):
        result = rate_limits_result(primary_used=26, secondary_used=40)
        snapshot = wc.build_snapshot(result, now=1_789_800_000)
        self.assertEqual(snapshot["five_hour_remaining_percent"], 74)
        self.assertEqual(snapshot["weekly_remaining_percent"], 60)

    def test_used_percent_is_clamped(self):
        high = wc.build_snapshot(rate_limits_result(primary_used=140), now=0)
        low = wc.build_snapshot(rate_limits_result(primary_used=-40), now=0)
        self.assertEqual(high["five_hour_remaining_percent"], 0)
        self.assertEqual(low["five_hour_remaining_percent"], 100)

    def test_fractional_remaining_keeps_one_decimal(self):
        result = rate_limits_result(primary_used=33.333)
        snapshot = wc.build_snapshot(result, now=0)
        self.assertEqual(snapshot["five_hour_remaining_percent"], 66.7)

    def test_past_reset_time_never_goes_negative(self):
        result = rate_limits_result(primary_reset_at=1_000)
        snapshot = wc.build_snapshot(result, now=2_000)
        self.assertEqual(snapshot["five_hour_reset_in_seconds"], 0)

    def test_non_numeric_fields_fail(self):
        result = rate_limits_result()
        result["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = "100"
        with self.assertRaises(wc.CompanionError):
            wc.build_snapshot(result, now=0)

        result = rate_limits_result()
        result["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"] = None
        with self.assertRaises(wc.CompanionError):
            wc.build_snapshot(result, now=0)


# --------------------------------------------------------------------------
# Bucket selection
# --------------------------------------------------------------------------


class BucketSelectionTests(unittest.TestCase):
    def test_prefers_rate_limits_by_limit_id(self):
        result = rate_limits_result()
        result["rateLimits"]["limitId"] = "spark"
        bucket = wc.select_codex_bucket(result)
        self.assertEqual(bucket["limitId"], "codex")

    def test_legacy_rate_limits_fallback(self):
        result = rate_limits_result()
        del result["rateLimitsByLimitId"]
        bucket = wc.select_codex_bucket(result)
        self.assertEqual(bucket["limitId"], "codex")

    def test_legacy_spark_bucket_is_rejected(self):
        result = rate_limits_result()
        del result["rateLimitsByLimitId"]
        result["rateLimits"]["limitId"] = "spark"
        with self.assertRaises(wc.CompanionError):
            wc.select_codex_bucket(result)

    def test_empty_result_is_rejected(self):
        with self.assertRaises(wc.CompanionError):
            wc.select_codex_bucket({})


# --------------------------------------------------------------------------
# Payload contract
# --------------------------------------------------------------------------


class PayloadTests(unittest.TestCase):
    def test_payload_has_exactly_the_wire_fields(self):
        snapshot = wc.build_snapshot(
            rate_limits_result(primary_used=26, secondary_used=40),
            now=1_789_800_000,
        )
        payload = json.loads(wc.encode_payload(snapshot))
        self.assertEqual(set(payload), {
            "five_hour_remaining_percent",
            "five_hour_reset_in_seconds",
            "weekly_remaining_percent",
            "weekly_reset_in_seconds",
        })
        self.assertEqual(payload["five_hour_remaining_percent"], 74)
        self.assertEqual(payload["weekly_remaining_percent"], 60)
        self.assertIsInstance(payload["five_hour_reset_in_seconds"], int)
        self.assertIsInstance(payload["weekly_reset_in_seconds"], int)

    def test_payload_is_compact_ascii_and_within_the_firmware_limit(self):
        snapshot = wc.build_snapshot(rate_limits_result(), now=1_789_800_000)
        payload = wc.encode_payload(snapshot)
        self.assertLessEqual(len(payload), wc.MAX_PAYLOAD_BYTES)
        self.assertTrue(payload.decode("utf-8").isascii())
        self.assertNotIn(b" ", payload)

    def test_payload_round_trips_through_the_firmware_parser_rules(self):
        """Mirror of quota_payload::parse in main/logic.h."""
        snapshot = wc.build_snapshot(rate_limits_result(primary_used=26), now=1_789_800_000)
        parsed = json.loads(wc.encode_payload(snapshot))
        self.assertTrue(isinstance(parsed, dict))
        for prefix in ("five_hour", "weekly"):
            percent = parsed[f"{prefix}_remaining_percent"]
            seconds = parsed[f"{prefix}_reset_in_seconds"]
            self.assertTrue(isinstance(percent, (int, float)))
            self.assertTrue(isinstance(seconds, (int, float)))
            self.assertGreaterEqual(percent, 0.0)
            self.assertLessEqual(percent, 100.0)
            self.assertGreaterEqual(seconds, 0.0)

    def test_public_snapshot_hides_local_diagnostics(self):
        snapshot = wc.build_snapshot(rate_limits_result(), now=0)
        self.assertIn("_source", snapshot)
        self.assertNotIn("_source", wc.public_snapshot(snapshot))

    def test_format_reset(self):
        self.assertEqual(wc.format_reset(59), "0m")
        self.assertEqual(wc.format_reset(3_600), "1h 0m")
        self.assertEqual(wc.format_reset(86_400), "1d 0h")


# --------------------------------------------------------------------------
# Bluetooth address handling
# --------------------------------------------------------------------------


class AddressTests(unittest.TestCase):
    def test_valid_addresses_normalize_to_uppercase(self):
        self.assertEqual(
            wc.parse_device_address("28:84:85:b2:1c:78"), "28:84:85:B2:1C:78"
        )
        self.assertEqual(
            wc.address_to_uint64("28:84:85:B2:1C:78"), 0x288485B21C78
        )

    def test_invalid_addresses_are_rejected(self):
        for bad in ["", "288485B21C78", "28:84:85:B2:1C", "28-84-85-B2-1C-78", "zz:84:85:b2:1c:78"]:
            with self.subTest(bad=bad):
                with self.assertRaises(wc.CompanionError):
                    wc.parse_device_address(bad)


# --------------------------------------------------------------------------
# PowerShell bridge generation
# --------------------------------------------------------------------------


class PowerShellBridgeTests(unittest.TestCase):
    ADDRESS = "28:84:85:B2:1C:78"

    def test_source_is_pure_ascii_and_bakes_in_the_address(self):
        source = wc.build_ps_bridge(self.ADDRESS, payload=None, hold_seconds=130)
        self.assertTrue(source.isascii())
        self.assertIn(str(0x288485B21C78), source)
        self.assertIn("$doWrite = $false", source)
        self.assertIn("$holdSeconds = 130", source)
        self.assertIn(wc.QUOTA_SERVICE_UUID, source)
        self.assertIn(wc.QUOTA_WRITE_UUID, source)

    def test_write_mode_embeds_the_payload_as_base64(self):
        payload = (
            b'{"five_hour_remaining_percent":26,"five_hour_reset_in_seconds":3600,'
            b'"weekly_remaining_percent":74,"weekly_reset_in_seconds":356400}'
        )
        source = wc.build_ps_bridge(self.ADDRESS, payload=payload, hold_seconds=0)
        self.assertIn("$doWrite = $true", source)
        self.assertIn("$holdSeconds = 0", source)
        encoded = base64.b64encode(payload).decode("ascii")
        self.assertIn(encoded, source)
        # The bridge must decode that literal back into the exact bytes.
        start = source.index("FromBase64String('") + len("FromBase64String('")
        end = source.index("')", start)
        self.assertEqual(base64.b64decode(source[start:end]), payload)

    def test_encoded_command_round_trips_as_utf16le(self):
        source = wc.build_ps_bridge(self.ADDRESS, payload=b"{}", hold_seconds=0)
        encoded = wc.encode_ps_command(source)
        self.assertTrue(encoded.isascii())
        self.assertEqual(base64.b64decode(encoded).decode("utf-16-le"), source)

    def test_bridge_never_scans_or_matches_by_name(self):
        """Regression guard: only the pinned address may reach the bridge."""
        source = wc.build_ps_bridge(self.ADDRESS, payload=None, hold_seconds=0)
        self.assertIn("FromBluetoothAddressAsync", source)
        self.assertNotIn("BluetoothLEAdvertisementWatcher", source)
        self.assertNotIn("DeviceWatcher", source)
        self.assertNotIn("AQS", source)

    def test_boolean_literals_use_powershell_syntax(self):
        """`false` alone is parsed as a command name by Windows PowerShell."""
        source = wc.build_ps_bridge(self.ADDRESS, payload=None, hold_seconds=0)
        self.assertNotIn("= false", source)
        self.assertNotIn("= true", source)

    def test_clixml_stderr_is_unwrapped(self):
        clixml = (
            '#< CLIXML\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<S S="Error">boom_x000D__x000A_</S><S S="Error">line two</S></Objs>'
        )
        self.assertEqual(wc.clean_ps_stderr(clixml), "boom\nline two")

    def test_plain_stderr_passes_through(self):
        self.assertEqual(wc.clean_ps_stderr("  plain error  "), "plain error")


# --------------------------------------------------------------------------
# Command line validation
# --------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def parse(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return wc.options_from_args(argv)

    def test_json_only_needs_no_device_address(self):
        options = self.parse(["--json-only"])
        self.assertTrue(options.json_only)
        self.assertIsNone(options.device_address)

    def test_ble_modes_require_a_pinned_address(self):
        for argv in (["--once"], ["--watch"], ["--probe-only"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    self.parse(argv)

    def test_probe_only_parses_a_valid_address(self):
        options = self.parse(["--probe-only", "--device-address", "28:84:85:b2:1c:78"])
        self.assertEqual(options.device_address, "28:84:85:B2:1C:78")
        self.assertEqual(options.hold_seconds, wc.DEFAULT_PROBE_HOLD_SECONDS)
        self.assertGreaterEqual(options.hold_seconds, 130)

    def test_conflicting_modes_are_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["--probe-only", "--json-only", "--device-address", "28:84:85:B2:1C:78"])
        with self.assertRaises(SystemExit):
            self.parse(["--probe-only", "--watch", "--device-address", "28:84:85:B2:1C:78"])

    def test_watch_interval_floor(self):
        with self.assertRaises(SystemExit):
            self.parse(["--watch", "--interval", "5", "--device-address", "28:84:85:B2:1C:78"])
        options = self.parse(["--watch", "--interval", "10", "--device-address", "28:84:85:B2:1C:78"])
        self.assertEqual(options.interval, 10)

    def test_no_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse([])

    def test_bad_address_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["--once", "--device-address", "not-an-address"])


# --------------------------------------------------------------------------
# Bridge event parsing (no subprocess)
# --------------------------------------------------------------------------


class BleResultTests(unittest.TestCase):
    def test_write_acknowledged_requires_the_ack_event(self):
        without_ack = wc.BleResult(
            0,
            [
                {"event": "device", "name": "Codex Micro"},
                {"event": "write_result", "status": "Success"},
            ],
            "",
        )
        self.assertFalse(without_ack.write_acknowledged)

        with_ack = wc.BleResult(0, [{"event": "write_ack", "status": "Success"}], "")
        self.assertTrue(with_ack.write_acknowledged)

    def test_last_error_returns_the_final_error(self):
        result = wc.BleResult(
            11,
            [
                {"event": "error", "code": "first", "message": "a"},
                {"event": "error", "code": "second", "message": "b"},
            ],
            "",
        )
        self.assertEqual(result.last_error()["code"], "second")


# --------------------------------------------------------------------------
# Live tests (opt in with CX_LIVE=1)
# --------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("CX_LIVE") == "1", "set CX_LIVE=1 to run live tests")
class LiveTests(unittest.TestCase):
    def test_app_server_returns_a_weekly_snapshot(self):
        options = wc.Options()
        snapshot = wc.read_snapshot(options)
        self.assertIn("five_hour_remaining_percent", snapshot)
        self.assertIn("weekly_remaining_percent", snapshot)
        self.assertEqual(
            snapshot["_source"]["five_hour"]["window_minutes"],
            wc.FIVE_HOUR_WINDOW_MINUTES,
        )
        self.assertEqual(
            snapshot["_source"]["weekly"]["window_minutes"],
            wc.WEEKLY_WINDOW_MINUTES,
        )
        for prefix in ("five_hour", "weekly"):
            self.assertGreaterEqual(snapshot[f"{prefix}_remaining_percent"], 0)
            self.assertLessEqual(snapshot[f"{prefix}_remaining_percent"], 100)
            self.assertGreater(snapshot[f"{prefix}_reset_in_seconds"], 0)
        self.assertLessEqual(len(wc.encode_payload(snapshot)), wc.MAX_PAYLOAD_BYTES)

    def test_powershell_bridge_emits_parseable_events(self):
        source = (
            "$ErrorActionPreference='Stop'\n"
            "try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }\n"
            "Write-Output ('@@CX@@' + (@{ event = 'start'; address = 'x' } | ConvertTo-Json -Compress))\n"
            "Write-Output ('@@CX@@' + (@{ event = 'done' } | ConvertTo-Json -Compress))\n"
            "exit 0\n"
        )
        result = wc.run_ps_bridge(source, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.find("start"))
        self.assertIsNotNone(result.find("done"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
