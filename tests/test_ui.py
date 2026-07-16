from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from mobile_profiler.cli import (
    apply_record_interval_defaults,
    build_parser,
    install_console_interrupt_handlers,
    requested_platform,
)
from mobile_profiler.ui import (
    DashboardHTTPServer,
    DashboardManager,
    LiveTelemetryReader,
    _parse_android_brightness_capability,
    android_icon_data_uri,
    parse_android_apk_icon_candidates,
    parse_android_launcher_activities,
    parse_android_package_list,
    parse_android_package_paths,
    parse_device_ipv4_addresses,
    sanitize_run_name,
)
from mobile_profiler import __version__


class LiveTelemetryTests(unittest.TestCase):
    def test_reader_keeps_charging_direction_before_samples_arrive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "requested_duration_s": 60,
                        "sample_interval_s": 1.0,
                        "battery_start": {
                            "voltage_mv": 3640.0,
                            "status": "charging",
                            "powered": ["AC"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "raw" / "sampler-stream.txt").write_text("", encoding="utf-8")

            snapshot = LiveTelemetryReader(root).snapshot()

            self.assertEqual(snapshot["sample_count"], 0)
            self.assertIsNone(snapshot["latest"])
            self.assertEqual(snapshot["summary"]["direction"], "charging")
            self.assertEqual(snapshot["battery"]["powered"], ["AC"])

    def test_reader_exposes_cpu_cluster_history_for_live_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "requested_duration_s": 60,
                        "sample_interval_s": 1.0,
                        "battery_start": {
                            "voltage_mv": 3800.0,
                            "status": "discharging",
                        },
                        "cpu_policies": [
                            {
                                "name": "policy0",
                                "path": "/sys/policy0",
                                "cluster_index": 0,
                                "label": "Little",
                                "cores": [0, 1, 2, 3],
                                "max_khz": 2200000,
                            },
                            {
                                "name": "policy4",
                                "path": "/sys/policy4",
                                "cluster_index": 1,
                                "label": "Big",
                                "cores": [4, 5, 6],
                                "max_khz": 3200000,
                            },
                            {
                                "name": "policy7",
                                "path": "/sys/policy7",
                                "cluster_index": 2,
                                "label": "Prime",
                                "cores": [7],
                                "max_khz": 3800000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            normalized = [
                {
                    "index": 0,
                    "elapsed_s": 0.0,
                    "uptime_s": 100.0,
                    "current_ma": 300.0,
                    "signed_current_ma": -300.0,
                    "voltage_mv": 3800.0,
                    "power_mw": 1140.0,
                    "direction": "discharging",
                    "cpu_pct": 30.0,
                    "cluster_cpu_pct": {
                        "policy0": 40.0,
                        "policy4": 20.0,
                        "policy7": 10.0,
                    },
                    "frequencies_mhz": {
                        "policy0": 1200.0,
                        "policy4": 1800.0,
                        "policy7": 2200.0,
                    },
                },
                {
                    "index": 1,
                    "elapsed_s": 1.0,
                    "uptime_s": 101.0,
                    "current_ma": 320.0,
                    "signed_current_ma": -320.0,
                    "voltage_mv": 3798.0,
                    "power_mw": 1215.36,
                    "direction": "discharging",
                    "cpu_pct": 45.0,
                    "cluster_cpu_pct": {
                        "policy0": 55.0,
                        "policy4": 35.0,
                        "policy7": 15.0,
                    },
                    "frequencies_mhz": {
                        "policy0": 1500.0,
                        "policy4": 2300.0,
                        "policy7": 2800.0,
                    },
                },
            ]
            (root / "raw" / "sampler-stream.txt").write_text(
                "\n".join(f"N|{json.dumps(item)}" for item in normalized) + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            self.assertEqual(
                snapshot["series"][0]["cluster_cpu_pct"],
                {"policy0": 40.0, "policy4": 20.0, "policy7": 10.0},
            )
            self.assertEqual(snapshot["series"][1]["frequencies_mhz"]["policy7"], 2800.0)
            self.assertEqual(snapshot["latest"]["cluster_cpu_pct"]["policy4"], 35.0)
            self.assertEqual(
                [item["name"] for item in snapshot["clusters"]],
                ["policy0", "policy4", "policy7"],
            )

    def test_reader_converts_append_only_sampler_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            metadata = {
                "title": "UI test",
                "test_mode": "performance",
                "requested_duration_s": 60,
                "sample_interval_s": 1.0,
                "current_unit": "auto",
                "cpu_policies": [],
                "gpu_source": None,
                "battery_start": {
                    "voltage_mv": 3800.0,
                    "status": "discharging",
                    "powered": [],
                },
                "collection_warnings": [],
            }
            (root / "metadata.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            (root / "raw" / "sampler-stream.txt").write_text(
                "\n".join(
                    [
                        "S|0|100.0|-300000|3800|300|100|0|50|800|0|0|0|0",
                        "CTX|101.0|com.example.app/.MainActivity|Awake|120|60.0",
                        "S|1|101.0|-320000|3798|301|130|0|60|820|0|0|0|0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "system-snapshots.jsonl").write_text(
                json.dumps(
                    {
                        "uptime_s": 101.0,
                        "host_epoch_s": 1000.0,
                        "processes": [{"pid": 456, "name": "dex2oat64", "cpu_pct": 70.0}],
                        "watched_processes": [
                            {
                                "pid": 456,
                                "name": "dex2oat64",
                                "watch_name": "dex2oat",
                                "watch_label": "DEX AOT compilation",
                                "activity_active": True,
                            }
                        ],
                        "process_count": 500,
                        "thread_count": 2500,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "contexts.jsonl").write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {
                            "uptime_s": 100.2,
                            "foreground_package": "com.example.app",
                            "foreground_activity": ".MainActivity",
                            "screen_state": "Awake",
                            "refresh_rate_hz": 60.0,
                            "source": "android-performance-context",
                            "performance": {
                                "platform": "android",
                                "foreground_window_name": "com.example.app/.MainActivity",
                                "frame_counter_total": 100,
                                "frame_counter_deadline_missed": 2,
                                "frame_histogram_ms": {"10": 80, "20": 20},
                            },
                        },
                        {
                            "uptime_s": 101.0,
                            "foreground_package": "com.example.app",
                            "foreground_activity": ".MainActivity",
                            "screen_state": "Awake",
                            "refresh_rate_hz": 60.0,
                            "source": "android-performance-context",
                            "performance": {
                                "platform": "android",
                                "foreground_window_name": "com.example.app/.MainActivity",
                                "frame_counter_total": 140,
                                "frame_counter_deadline_missed": 4,
                                "frame_histogram_ms": {"10": 110, "20": 30},
                            },
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "thermal-snapshots.jsonl").write_text(
                json.dumps(
                    {
                        "uptime_s": 101.0,
                        "host_epoch_s": 1000.0,
                        "status": 0,
                        "temperatures": [{"name": "CPU", "value_c": 40.0}],
                        "cooling_devices": [{"name": "lcd-backlight", "value": 1.0}],
                        "display_brightness": {
                            "available": True,
                            "screen_state": "ON",
                            "setting_raw": 204.0,
                            "setting_float": 0.8,
                            "current_screen_brightness": 0.8,
                            "screen_brightness": 0.5,
                            "adjusted_brightness": 0.5,
                            "thermal_cap": 0.5,
                            "thermal_applied": True,
                            "thermal_status": 3,
                            "brightness_max_reason": 1,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "scheduler-snapshots.jsonl").write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {
                            "uptime_s": 100.5,
                            "host_epoch_s": 999.5,
                            "cpusets": {"foreground": "0-5", "background": "0-3"},
                            "hint_sessions": [],
                            "watched_processes": [
                                {
                                    "pid": 123,
                                    "name": "com.example.app",
                                    "current_sched_group": 2,
                                    "current_proc_state": 5,
                                }
                            ],
                        },
                        {
                            "uptime_s": 101.0,
                            "host_epoch_s": 1000.0,
                            "cpusets": {"foreground": "0-7", "background": "0-3"},
                            "hint_sessions": [
                                {
                                    "uid": 1000,
                                    "pid": 123,
                                    "tids": [124],
                                    "graphics_pipeline": True,
                                }
                            ],
                            "watched_processes": [
                                {
                                    "pid": 123,
                                    "name": "com.example.app",
                                    "current_sched_group": 3,
                                    "current_proc_state": 2,
                                    "adj_type": "top-activity",
                                }
                            ],
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            self.assertEqual(snapshot["sample_count"], 2)
            self.assertAlmostEqual(snapshot["latest"]["current_ma"], 320.0)
            self.assertAlmostEqual(snapshot["latest"]["power_mw"], 1215.36)
            self.assertEqual(snapshot["latest"]["direction"], "discharging")
            self.assertAlmostEqual(snapshot["latest"]["cpu_pct"], 66.6666666, places=4)
            self.assertEqual(snapshot["context"]["foreground_package"], "com.example.app")
            self.assertGreater(snapshot["summary"]["energy_mwh"], 0.3)
            self.assertEqual(snapshot["system_monitor"]["process_count"], 500)
            self.assertEqual(
                snapshot["system_monitor"]["active_priority"][0]["watch_name"],
                "dex2oat",
            )
            self.assertEqual(snapshot["system_monitor"]["thermal"]["status"], 0)
            self.assertEqual(
                snapshot["system_monitor"]["scheduler"]["cpusets"]["background"],
                "0-3",
            )
            self.assertEqual(len(snapshot["scheduler_series"]), 2)
            self.assertEqual(snapshot["scheduler_series"][-1]["cpuset_cpu_count"], 8)
            self.assertEqual(snapshot["scheduler_series"][-1]["foreground_sched_group"], 3)
            self.assertEqual(snapshot["scheduler_series"][-1]["graphics_session_count"], 1)
            self.assertEqual(snapshot["test_mode"], "performance")
            self.assertEqual(len(snapshot["performance_series"]), 1)
            self.assertAlmostEqual(
                snapshot["performance_series"][0]["frame_rate_fps"],
                50.0,
            )
            self.assertAlmostEqual(
                snapshot["performance_series"][0]["one_percent_low_fps"],
                50.0,
            )
            self.assertEqual(snapshot["performance"]["one_percent_low_fps"], 50.0)
            frame_flow = {
                item["key"]: item
                for item in snapshot["performance"]["frame_flow"]["stages"]
            }
            self.assertAlmostEqual(
                frame_flow["app_submission"]["timeline"][0]["elapsed_s"],
                1.0,
            )
            self.assertEqual(
                [
                    round(item["elapsed_s"], 1)
                    for item in frame_flow["display_scanout"]["timeline"]
                ],
                [0.2, 1.0],
            )
            self.assertEqual(
                [item["value"] for item in snapshot["performance"]["refresh_rate_timeline"]],
                [60.0, 60.0],
            )
            self.assertIn("power_pressure", snapshot)
            self.assertIn("render_performance", snapshot)
            self.assertTrue(snapshot["runtime_settings"]["available"] is False)
            self.assertEqual(snapshot["brightness_throttling"]["point_count"], 1)
            self.assertTrue(snapshot["brightness_throttling"]["current_active"])
            self.assertEqual(
                snapshot["brightness_throttling"]["current_state"]["status"],
                "confirmed",
            )


class UiServerTests(unittest.TestCase):
    def test_android_brightness_capability_reports_range_and_step(self) -> None:
        capability = _parse_android_brightness_capability(
            "101\n",
            "0.3954986\n",
            "0\n",
            "mScreenBrightnessMinimum=0.0\nmScreenBrightnessMaximum=1.0\n",
        )

        self.assertEqual(capability["current"], 101)
        self.assertEqual(capability["minimum"], 0)
        self.assertEqual(capability["maximum"], 255)
        self.assertEqual(capability["step"], 1)
        self.assertAlmostEqual(capability["normalized_step"], 1 / 255)
        self.assertFalse(capability["automatic"])

        oem_raw = _parse_android_brightness_capability(
            "2119\n",
            "null\n",
            "0\n",
            "mScreenBrightnessMinimum=0.016\nmScreenBrightnessMaximum=1.0\n",
            "2119.0\n",
            0,
            "mScreenBrightnessRangeMinimum=2.0\n"
            "mScreenBrightnessRangeMaximum=4675.0\n"
            "mScreenBrightnessNormalMaximum=4095.0\n",
        )
        self.assertEqual(oem_raw["minimum"], 2)
        self.assertEqual(oem_raw["maximum"], 4095)
        self.assertEqual(oem_raw["display_id"], 0)
        self.assertEqual(oem_raw["display_value_format"], "raw")
        self.assertIn("raw brightness range", oem_raw["range_source"])

        automatic = _parse_android_brightness_capability(
            "0\n",
            "null\n",
            "1\n",
            "mScreenBrightnessMinimum=0.0\nmScreenBrightnessMaximum=1.0\n",
        )
        self.assertEqual(automatic["maximum"], 255)
        self.assertTrue(automatic["automatic"])
        self.assertIn("fallback", automatic["range_source"])

    def test_android_brightness_read_and_numeric_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            ready_devices = ([{
                "serial": "ANDROID",
                "state": "device",
                "platform": "android",
            }], None)
            before = {
                "supported": True,
                "device": "ANDROID",
                "platform": "android",
                "writable": True,
                "current": 101,
                "minimum": 0,
                "maximum": 255,
                "step": 1,
                "normalized_minimum": 0.0,
                "normalized_maximum": 1.0,
                "normalized_step": 1 / 255,
                "mode": 1,
                "automatic": True,
            }
            after = {**before, "current": 100, "mode": 0, "automatic": False}

            with (
                patch.object(manager, "devices", return_value=ready_devices),
                patch.object(manager, "_android_brightness_capability", return_value=before),
            ):
                read = manager.brightness({
                    "device": "ANDROID",
                    "platform": "android",
                    "action": "read",
                })
            self.assertEqual(read["current"], 101)
            self.assertEqual(read["maximum"], 255)

            command_result = Mock(ok=True, stdout="", stderr="")
            with (
                patch.object(manager, "devices", return_value=ready_devices),
                patch.object(
                    manager,
                    "_android_brightness_capability",
                    side_effect=[before, after],
                ),
                patch("mobile_profiler.ui.adb_shell", return_value=command_result) as shell,
                patch("mobile_profiler.ui.time.sleep"),
            ):
                applied = manager.brightness({
                    "device": "ANDROID",
                    "platform": "android",
                    "action": "set",
                    "value": 100,
                })

            commands = [call.args[2] for call in shell.call_args_list]
            self.assertEqual(
                commands,
                [
                    ["settings", "put", "system", "screen_brightness_mode", "0"],
                    ["settings", "put", "system", "screen_brightness", "100"],
                    ["cmd", "display", "set-brightness", "0.3921569"],
                ],
            )
            self.assertTrue(applied["applied"])
            self.assertTrue(applied["manual_mode_changed"])
            self.assertEqual(applied["previous_mode"], 1)

    def test_android_brightness_normalizes_display_command_for_oem_scale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            ready_devices = ([{
                "serial": "ROOTED",
                "state": "device",
                "platform": "android",
            }], None)
            before = {
                "current": 2119,
                "minimum": 2,
                "maximum": 4095,
                "normalized_minimum": 0.016,
                "normalized_maximum": 1.0,
                "normalized_step": 0.984 / 4093,
                "display_current": 2119.0,
                "display_value_format": "raw",
                "mode": 0,
            }
            after = {**before, "current": 2000, "display_current": 2000.0}
            command_result = Mock(ok=True, stdout="", stderr="")
            with (
                patch.object(manager, "devices", return_value=ready_devices),
                patch.object(
                    manager,
                    "_android_brightness_capability",
                    side_effect=[before, after],
                ),
                patch("mobile_profiler.ui.adb_shell", return_value=command_result) as shell,
                patch("mobile_profiler.ui.time.sleep"),
            ):
                applied = manager.brightness({
                    "device": "ROOTED",
                    "platform": "android",
                    "action": "set",
                    "value": 2000,
                })

            commands = [call.args[2] for call in shell.call_args_list]
            normalized = 0.016 + ((2000 - 2) / (4095 - 2) * 0.984)
            self.assertEqual(
                commands,
                [
                    ["settings", "put", "system", "screen_brightness", "2000"],
                    ["cmd", "display", "set-brightness", f"{normalized:.7f}"],
                ],
            )
            self.assertAlmostEqual(applied["display_requested"], normalized)
            self.assertTrue(applied["display_applied"])
            self.assertTrue(applied["applied"])

    def test_android_brightness_rejects_invalid_values_and_active_recording(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            ready_devices = ([{
                "serial": "ANDROID",
                "state": "device",
                "platform": "android",
            }], None)
            capability = {
                "current": 101,
                "minimum": 0,
                "maximum": 255,
                "normalized_minimum": 0.0,
                "normalized_maximum": 1.0,
                "mode": 0,
            }
            with (
                patch.object(manager, "devices", return_value=ready_devices),
                patch.object(manager, "_android_brightness_capability", return_value=capability),
                patch("mobile_profiler.ui.adb_shell") as shell,
            ):
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    manager.brightness({"device": "ANDROID", "action": "set", "value": 12.5})
                with self.assertRaisesRegex(ValueError, "between 0 and 255"):
                    manager.brightness({"device": "ANDROID", "action": "set", "value": 256})
                manager.active = Mock(running=True)
                with self.assertRaisesRegex(RuntimeError, "Stop the active recording"):
                    manager.brightness({"device": "ANDROID", "action": "set", "value": 100})
            shell.assert_not_called()

    def test_android_application_parsers_prioritize_launchable_user_apps(self) -> None:
        third_party = parse_android_package_list(
            "package:com.example.game\npackage:com.example.reader\ninvalid\n"
        )
        apps = parse_android_launcher_activities(
            "\n".join(
                [
                    "com.android.settings/.Settings",
                    "com.example.game/.MainActivity",
                    "com.example.game/.AlternateActivity",
                    "No activities found",
                ]
            ),
            third_party,
        )

        self.assertEqual(third_party, ["com.example.game", "com.example.reader"])
        self.assertEqual([item["package"] for item in apps], ["com.example.game", "com.android.settings"])
        self.assertTrue(apps[0]["user_app"])
        self.assertEqual(len(apps[0]["activities"]), 2)

    def test_android_package_paths_and_icon_candidates_support_thumbnails(self) -> None:
        paths = parse_android_package_paths(
            "package:/data/app/example/base.apk=com.example.game\n"
            "Error: ignored\n"
        )
        self.assertEqual(paths["com.example.game"], "/data/app/example/base.apk")
        candidates = parse_android_apk_icon_candidates(
            "  1200  2026-01-01 12:00 res/drawable-xhdpi/notification_icon.png\n"
            " 24084  2026-01-01 12:00 res/drawable-xhdpi/icon.png\n"
            " 77137  2026-01-01 12:00 res/drawable-xxxhdpi/icon.png\n"
        )
        self.assertEqual(candidates[0]["name"], "res/drawable-xxxhdpi/icon.png")
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 600).decode("ascii")
        self.assertTrue(
            android_icon_data_uri(encoded, "res/drawable/icon.png").startswith(
                "data:image/png;base64,"
            )
        )

    def test_android_app_scan_uses_launcher_activities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            third_party = Mock(
                ok=True,
                stdout="package:com.example.game\npackage:com.example.reader\n",
                stderr="",
            )
            launcher = Mock(
                ok=True,
                stdout="com.android.settings/.Settings\ncom.example.game/.MainActivity\n",
                stderr="",
            )
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "ANDROID", "state": "device"}], None),
                ),
                patch(
                    "mobile_profiler.ui.adb_shell",
                    side_effect=[third_party, launcher],
                ) as shell,
            ):
                result = manager.scan_android_apps(
                    {"device": "ANDROID", "platform": "android"}
                )

        self.assertEqual(result["source"], "launcher-activities")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["apps"][0]["package"], "com.example.game")
        self.assertEqual(shell.call_count, 2)
        self.assertIn("query-activities", shell.call_args_list[1].args[2])

    def test_ui_merges_android_harmony_and_ios_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "ANDROID", "state": "device"}], None),
                ),
                patch(
                    "mobile_profiler.ui.list_ios_devices",
                    return_value=(
                        [
                            {
                                "serial": "ios:IPHONE",
                                "udid": "IPHONE",
                                "state": "device",
                                "platform": "ios",
                                "connection_type": "wireless",
                            }
                        ],
                        None,
                    ),
                ),
                patch(
                    "mobile_profiler.ui.list_harmony_devices",
                    return_value=(
                        [
                            {
                                "serial": "harmony:HDC-TARGET",
                                "hdc_target": "HDC-TARGET",
                                "state": "device",
                                "platform": "harmony",
                                "connection_type": "usb",
                            }
                        ],
                        None,
                    ),
                ),
            ):
                devices, error = manager.devices(force=True)

        self.assertIsNone(error)
        self.assertEqual(
            {item["serial"] for item in devices},
            {"ANDROID", "harmony:HDC-TARGET", "ios:IPHONE"},
        )
        self.assertEqual(
            next(item for item in devices if item["serial"] == "ANDROID")["platform"],
            "android",
        )

    def test_platform_refresh_only_calls_selected_device_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            with (
                patch("mobile_profiler.ui.list_adb_devices") as android,
                patch("mobile_profiler.ui.list_harmony_devices") as harmony,
                patch(
                    "mobile_profiler.ui.list_ios_devices",
                    return_value=([], None),
                ) as ios,
            ):
                manager.devices(
                    force=True,
                    refresh_android=False,
                    refresh_harmony=False,
                    refresh_ios=True,
                )

        android.assert_not_called()
        harmony.assert_not_called()
        ios.assert_called_once_with("ios-python")

    def test_ios_pair_requires_a_usb_device_and_preserves_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            wireless = {
                "serial": "ios:IPHONE",
                "udid": "IPHONE",
                "state": "device",
                "platform": "ios",
                "connection_type": "wireless",
            }
            with patch(
                "mobile_profiler.ui.list_ios_devices",
                return_value=([wireless], None),
            ), patch("mobile_profiler.ui.pair_ios_device") as pair:
                with self.assertRaisesRegex(ValueError, "USB-connected iPhone"):
                    manager.pair_ios({"device": "ios:IPHONE"})
            pair.assert_not_called()

            usb = {**wireless, "connection_type": "usb"}
            with patch(
                "mobile_profiler.ui.list_ios_devices",
                return_value=([usb], None),
            ), patch(
                "mobile_profiler.ui.pair_ios_device",
                side_effect=RuntimeError("Bonjour endpoint discovery timed out"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Bonjour endpoint discovery timed out"):
                    manager.pair_ios({"device": "ios:IPHONE"})

    def test_demo_server_serves_assets_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager(
                "definitely-missing-adb",
                Path(directory),
                demo_mode=True,
            )
            server = DashboardHTTPServer(("127.0.0.1", 0), manager)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(base + "/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                with urlopen(base + "/app.css?v=platform-ui-27", timeout=5) as response:
                    css = response.read().decode("utf-8")
                with urlopen(base + "/app.js?v=platform-ui-27", timeout=5) as response:
                    javascript = response.read().decode("utf-8")
                with urlopen(base + "/api/state", timeout=5) as response:
                    state = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                manager.close()
                thread.join(timeout=5)

            self.assertIn("Mobile Profiler", html)
            self.assertIn("v0.7.1", html)
            self.assertIn('class="app-version-badge"', html)
            self.assertEqual(state["version"], __version__)
            self.assertIn("TEST PLATFORM", html)
            self.assertIn("ADB / gfxinfo", html)
            self.assertIn("DVT / RemoteXPC", html)
            self.assertIn("HDC / SmartPerf", html)
            self.assertIn("ADB IP", html)
            self.assertIn("无线 ADB", html)
            self.assertIn("鸿蒙无线", html)
            self.assertIn("iOS 无线", html)
            self.assertIn("断开无线", html)
            self.assertNotIn('data-view="system"', html)
            self.assertNotIn('data-panel="system"', html)
            self.assertIn("功耗测试模式", html)
            self.assertIn("FPS / 1% Low / 渲染链路", html)
            self.assertIn("必要设置", html)
            self.assertIn("更多采集设置", html)
            self.assertIn("设备亮度", html)
            self.assertIn('id="brightness-input"', html)
            self.assertIn("platform-ui-27", html)
            self.assertIn("屏幕热降亮监控", html)
            self.assertNotIn('<details class="advanced-settings" open', html)
            self.assertIn(".capture-feature-card input", css)
            self.assertIn("pointer-events: auto", css)
            self.assertIn("captureFeaturesOverridden", javascript)
            self.assertIn("原始时间序列", html)
            self.assertIn("手机采集内容", html)
            self.assertIn("默认只开启有原始曲线或有效结论的项目", html)
            self.assertIn("专项诊断", html)
            self.assertIn("默认关闭 · 仅在明确问题假设下开启", html)
            self.assertIn("保留现有 BatteryStats（--no-reset）", html)
            self.assertIn("归因可能混入测试前的活动", html)
            self.assertIn("启用 BatteryStats full-history", html)
            self.assertIn("当前版本测试结束后不会自动关闭 full-history", html)
            self.assertIn(".compact-check.option-with-help", css)
            self.assertNotIn("性能干扰监控", html)
            self.assertIn('id="system-monitor-input" name="system_monitor" type="checkbox" checked hidden', html)
            self.assertIn('data-capture-feature="foreground_window" checked', html)
            self.assertIn('data-capture-feature="runtime_settings" checked', html)
            for feature in (
                "target_process",
                "process_snapshots",
                "hot_threads",
                "scheduler",
                "power_attribution",
                "harmony_hitches",
                "touch_events",
            ):
                self.assertNotIn(f'data-capture-feature="{feature}" checked', html)
            self.assertIn("const platformRequiredFeatures", javascript)
            self.assertIn('android: new Set(["harmony_hitches", "touch_events"])', javascript)
            self.assertIn('effectiveCapturePreset() !== "harmony-smartperf"', javascript)
            self.assertIn('["gpu_metrics", "memory_frequency", "frame_details", "target_process"]', javascript)
            self.assertIn("capture-interval-disabled", javascript)
            self.assertIn(".capture-interval-disabled", css)
            self.assertIn(".capture-feature-card.feature-required", css)
            power_preset_start = javascript.index('"power-standard": new Set([')
            power_preset_end = javascript.index("]),", power_preset_start)
            power_preset = javascript[power_preset_start:power_preset_end]
            self.assertIn('"runtime_settings"', power_preset)
            self.assertNotIn('"process_snapshots"', power_preset)
            self.assertNotIn('"power_attribution"', power_preset)
            performance_preset_start = javascript.index('"performance-standard": new Set([')
            performance_preset_end = javascript.index("]),", performance_preset_start)
            performance_preset = javascript[performance_preset_start:performance_preset_end]
            self.assertIn('"frame_rate"', performance_preset)
            self.assertIn('"frame_details"', performance_preset)
            self.assertNotIn('"target_process"', performance_preset)
            self.assertNotIn('"scheduler"', performance_preset)
            self.assertIn("function defaultMarkerName", javascript)
            self.assertIn("const defaultName = defaultMarkerName()", javascript)
            self.assertIn('发现异常", defaultName)', javascript)
            self.assertIn("if (name === null) return", javascript)
            self.assertIn("name.trim() || defaultName", javascript)
            self.assertIn("JSON.stringify({ name: resolvedName })", javascript)
            self.assertIn('api("/api/brightness"', javascript)
            self.assertIn("renderBrightnessThrottling", javascript)
            self.assertIn("renderFrameFlowHistory", javascript)
            self.assertIn('lane.key === "display_scanout"', javascript)
            self.assertIn('}, `0 ${lane.unit}`)', javascript)
            self.assertIn("仅有阶段耗时，无独立 FPS 计数", javascript)
            self.assertIn("暂无有效刷新率时间序列", javascript)
            self.assertNotIn('cpu_cluster:', javascript)
            self.assertIn("cpuCoreGroupLabel", javascript)
            self.assertIn("共享频率", javascript)
            self.assertIn("CPU CORE FREQUENCY", html)
            self.assertIn("grid-template-columns: minmax(140px, 1fr) auto", css)
            self.assertNotIn(".cluster-frequency { display: none; }", css)
            self.assertIn("let minimum = finite(axis.fixedMin) ? Number(axis.fixedMin) : 0", javascript)
            self.assertIn("const minimum = 0", javascript)
            self.assertIn("brightness-dim-marker", css)
            self.assertIn("flow-history-line", css)
            self.assertIn('id="live-timeline-source"', html)
            self.assertIn('id="live-timeline-config"', html)
            self.assertIn('id="live-timeline-config-list"', html)
            self.assertIn('id="live-timeline-config-count"', html)
            self.assertIn("仅控制实时图表，不影响采集数据和测试报告", html)
            self.assertIn('id="live-resolution-note"', html)
            self.assertIn("实时数据时间轴", html)
            self.assertNotIn('class="metric-tabs"', html)
            self.assertIn("liveTimelineLanes", javascript)
            self.assertIn('const liveTimelineLayoutStorageKey = "mobile-profiler-live-timeline-layout-v1"', javascript)
            self.assertIn("function normalizeLiveTimelineOrder", javascript)
            self.assertIn("function loadLiveTimelineLayouts", javascript)
            self.assertIn("function saveLiveTimelineLayouts", javascript)
            self.assertIn("function applyLiveTimelineLayout", javascript)
            self.assertIn("function renderLiveTimelineConfiguration", javascript)
            self.assertIn("function moveLiveTimelineLayoutItem", javascript)
            self.assertIn('return `${platform}:${mode}`', javascript)
            self.assertIn("localStorage.getItem(liveTimelineLayoutStorageKey)", javascript)
            self.assertIn("liveTimelineLayoutStorageKey,\n        JSON.stringify({ version: 1, contexts })", javascript)
            self.assertIn("data-live-timeline-toggle", javascript)
            self.assertIn("data-live-timeline-move", javascript)
            self.assertIn("renderLiveTimelineConfiguration(active, availableLanes)", javascript)
            self.assertIn("applyLiveTimelineLayout(active, availableLanes)", javascript)
            self.assertIn("已隐藏所有图表", javascript)
            self.assertIn("等待有效数据", javascript)
            self.assertIn("cpuFrequencyTimelineLane", javascript)
            self.assertIn("frameFlowTimelineLanes", javascript)
            self.assertIn("const minTime = 0", javascript)
            self.assertIn("Number(active?.elapsed_s || 0), maxPointElapsed", javascript)
            layout_start = javascript.index("const liveTimelineLayoutDefinitions = [")
            layout_end = javascript.index("];", layout_start)
            layout_block = javascript[layout_start:layout_end]
            expected_layout = [
                "cpu_pct",
                "cpu_frequency",
                "frame_rate_fps",
                "frame_flow",
                "refresh_rate_hz",
                "frame_time_ms",
                "frame_issue_pct",
                "gpu_load_pct",
                "gpu_frequency_mhz",
                "memory_frequency_mhz",
                "power_mw",
                "current_ma",
                "voltage_mv",
                "temperature_c",
            ]
            for previous, following in zip(expected_layout, expected_layout[1:]):
                self.assertLess(layout_block.index(previous), layout_block.index(following))
            self.assertLess(
                javascript.index("const metadataFeatures = active?.metadata?.capture_configuration?.features"),
                javascript.index("const liveFeatures = active?.config?.capture_features"),
            )
            self.assertIn("timeline-series-line", css)
            self.assertIn("timeline-hover-line", css)
            self.assertIn(".live-timeline-config-row", css)
            self.assertIn(".live-timeline-config-toggle", css)
            self.assertIn(".live-timeline-order-button", css)
            self.assertIn(".live-timeline-config-copy small { white-space: normal; }", css)
            self.assertIn("one_percent_low_fps", javascript)
            self.assertIn("step: true", javascript)
            self.assertIn("SurfaceFlinger · 前台 SurfaceView/BLAST GraphicBuffer", javascript)
            self.assertIn("不等同于游戏引擎内部渲染分辨率", html)
            self.assertIn("扫描手机应用", html)
            self.assertIn("扫描出的应用", html)
            self.assertNotIn("选择测试游戏 / 应用", html)
            self.assertIn('id="app-result-details"', html)
            self.assertNotIn('id="app-result-details" open', html)
            self.assertIn("已扫描应用与包名", html)
            self.assertNotIn('id="app-picker-selection"', html)
            self.assertIn('.app-result-details:not([open]) > :not(summary)', css)
            self.assertNotIn('$("#app-result-details").open = false', javascript)
            self.assertIn('$("#app-result-details").open = true', javascript)
            self.assertIn('title="${escapeHtml(packageName)}"', javascript)
            self.assertIn("overflow-wrap: anywhere", css)
            self.assertIn("32 分钟", html)
            self.assertIn("性能资源状态", html)
            self.assertNotIn('id="resource-top-app-cpuset"', html)
            self.assertNotIn('$("#resource-top-app-cpuset")', javascript)
            self.assertIn("功耗压力解释", html)
            self.assertIn("帧率数据流与渲染链路", html)
            self.assertIn('id="frame-flow-history-chart"', html)
            self.assertIn("完整链路节点帧率趋势", html)
            self.assertIn("内存频率", html)
            self.assertNotIn('data-view="thermal"', html)
            self.assertIn('data-view="config"', html)
            self.assertIn("测试配置", html)
            self.assertNotIn('data-view="tools"', html)
            self.assertNotIn('data-panel="tools"', html)
            self.assertIn('id="history-tools-details"', html)
            self.assertIn("报告维护与交付工具", html)
            self.assertIn("mountConfigurationView", javascript)
            self.assertIn('id="config-view-columns"', html)
            self.assertIn('id="config-form-column"', html)
            self.assertIn('id="config-app-column"', html)
            self.assertIn('id="config-app-picker-content"', html)
            self.assertIn("formTarget.append(controlPanel)", javascript)
            self.assertIn("appPickerTarget.append(appPicker)", javascript)
            self.assertIn(".config-view-columns", css)
            self.assertIn("grid-template-columns: minmax(0, 1.35fr) minmax(360px, .85fr)", css)
            self.assertIn('body:not([data-platform="android"]) .config-app-placeholder', css)
            self.assertEqual(html.count('id="package-input"'), 1)
            self.assertEqual(html.count('id="scan-apps"'), 1)
            self.assertIn('const legacyTools = view === "tools"', javascript)
            self.assertIn('const legacySystem = view === "system" || view === "thermal"', javascript)
            self.assertIn('const target = ["live", "config", "device", "history"].includes(requested)', javascript)
            self.assertNotIn('system: "性能上下文"', javascript)
            self.assertIn('window.history.replaceState(null, "", `#${target}`)', javascript)
            self.assertIn("historyTools.open = true", javascript)
            self.assertIn(".runtime-layout.monitoring-only", css)
            self.assertIn(".history-tools-details", css)
            self.assertIn("导入 BTR2 日志", html)
            self.assertTrue(state["active"]["is_demo"])
            self.assertEqual(state["active"]["test_mode"], "performance")
            self.assertIn("portable_build_available", state["tooling"])
            self.assertEqual(len(state["active"]["series"]), 240)
            self.assertTrue(
                all(
                    "one_percent_low_fps" in item
                    for item in state["active"]["performance_series"]
                )
            )
            self.assertEqual(
                set(state["active"]["series"][0]["cluster_cpu_pct"]),
                {"policy0", "policy4", "policy7"},
            )
            self.assertEqual(
                set(state["active"]["series"][0]["frequencies_mhz"]),
                {"policy0", "policy4", "policy7"},
            )
            self.assertEqual(state["active"]["performance_series"][0]["elapsed_s"], 2.0)
            self.assertEqual(
                state["active"]["performance"]["refresh_rate_timeline"][0]["elapsed_s"],
                2.0,
            )
            self.assertTrue(
                state["active"]["metadata"]["capture_configuration"]["features"]["frame_rate"]
            )
            self.assertEqual(
                state["active"]["performance"]["render_resolution_source"],
                "演示数据 · 模拟 SurfaceFlinger GraphicBuffer",
            )
            self.assertIn(
                "BLAST Consumer",
                state["active"]["performance"]["render_resolution_evidence"],
            )
            self.assertFalse(
                state["active"]["performance"]["render_resolution_estimated"]
            )
            self.assertEqual(state["active"]["performance"]["current_refresh_rate_hz"], 120.0)
            self.assertEqual(
                state["active"]["performance"]["frame_flow"]["primary_key"],
                "surface_present",
            )
            self.assertEqual(
                len(state["active"]["performance"]["frame_flow"]["stages"]),
                4,
            )
            demo_flow = {
                item["key"]: item
                for item in state["active"]["performance"]["frame_flow"]["stages"]
            }
            self.assertEqual(demo_flow["render_queue"]["timeline"], [])
            self.assertEqual(
                set(item["value"] for item in demo_flow["display_scanout"]["timeline"]),
                {60.0, 120.0},
            )
            self.assertEqual(
                state["active"]["performance"]["frame_flow"]["timeline_stage_count"],
                2,
            )
            self.assertEqual(
                state["active"]["system_monitor"]["active_priority"][0]["watch_name"],
                "dex2oat",
            )

    def test_report_path_stays_inside_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("adb", Path(directory))
            self.assertIsNone(manager.report_path("..%2Foutside"))

    def test_tool_api_and_comparison_report_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comparison_dir = root / "comparisons" / "phone-a-vs-b"
            comparison_dir.mkdir(parents=True)
            (comparison_dir / "comparison.html").write_text(
                "<h1>comparison</h1>", encoding="utf-8"
            )
            manager = DashboardManager("missing-adb", root)
            manager.regenerate_run = Mock(
                return_value={"run_name": "phone-a", "report_url": "/runs/phone-a/report.html"}
            )
            manager.delete_report_range = Mock(
                return_value={
                    "run_name": "phone-a",
                    "report_url": "/runs/phone-a/report.html",
                    "deleted_sample_count": 3,
                }
            )
            manager.enable_tcpip = Mock(
                return_value={
                    "tcpip_enabled": True,
                    "suggested_address": "192.168.21.90:5555",
                }
            )
            manager.disconnect_device = Mock(
                return_value={
                    "address": "192.168.21.90:5555",
                    "disconnected": True,
                }
            )
            manager.scan_android_apps = Mock(
                return_value={
                    "device": "USB123",
                    "platform": "android",
                    "source": "launcher-activities",
                    "count": 1,
                    "apps": [{"package": "com.example.game"}],
                    "warnings": [],
                }
            )
            server = DashboardHTTPServer(("127.0.0.1", 0), manager)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                request = Request(
                    base + "/api/report",
                    data=json.dumps({"run_name": "phone-a"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    api_result = json.loads(response.read().decode("utf-8"))
                delete_range_payload = {
                    "run_name": "phone-a",
                    "start_uptime_s": 101.0,
                    "end_uptime_s": 103.0,
                }
                delete_range_request = Request(
                    base + "/api/report/delete-range",
                    data=json.dumps(delete_range_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(delete_range_request, timeout=5) as response:
                    delete_range_result = json.loads(response.read().decode("utf-8"))
                tcpip_request = Request(
                    base + "/api/tcpip",
                    data=json.dumps({"device": "USB123", "port": 5555}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(tcpip_request, timeout=5) as response:
                    tcpip_result = json.loads(response.read().decode("utf-8"))
                disconnect_request = Request(
                    base + "/api/disconnect",
                    data=json.dumps({"address": "192.168.21.90:5555"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(disconnect_request, timeout=5) as response:
                    disconnect_result = json.loads(response.read().decode("utf-8"))
                apps_request = Request(
                    base + "/api/apps",
                    data=json.dumps({"device": "USB123", "platform": "android"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(apps_request, timeout=5) as response:
                    apps_result = json.loads(response.read().decode("utf-8"))
                with urlopen(
                    base + "/comparisons/phone-a-vs-b/comparison.html", timeout=5
                ) as response:
                    comparison_html = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                manager.close()
                thread.join(timeout=5)

            self.assertEqual(api_result["run_name"], "phone-a")
            self.assertEqual(delete_range_result["deleted_sample_count"], 3)
            self.assertTrue(tcpip_result["tcpip_enabled"])
            self.assertTrue(disconnect_result["disconnected"])
            self.assertEqual(apps_result["apps"][0]["package"], "com.example.game")
            self.assertIn("comparison", comparison_html)
            manager.regenerate_run.assert_called_once_with({"run_name": "phone-a"})
            manager.delete_report_range.assert_called_once_with(delete_range_payload)
            manager.enable_tcpip.assert_called_once_with({"device": "USB123", "port": 5555})
            manager.disconnect_device.assert_called_once_with(
                {"address": "192.168.21.90:5555"}
            )
            manager.scan_android_apps.assert_called_once_with(
                {"device": "USB123", "platform": "android"}
            )

    def test_start_record_launches_existing_cli_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            fake_active = Mock()
            fake_active.running = True
            fake_active.snapshot.return_value = {"running": True, "status": "starting"}
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "SERIAL", "state": "device"}], None),
                ),
                patch("mobile_profiler.ui.subprocess.Popen", return_value=object()) as popen,
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active),
            ):
                result = manager.start_record(
                    {
                        "device": "SERIAL",
                        "platform": "android",
                        "interval": 1,
                        "run_name": "UI smoke",
                        "session_mode": True,
                        "require_unplugged": True,
                        "test_mode": "performance",
                        "performance_interval": 1.5,
                        "package": "com.example.game",
                    }
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[:5], [
                sys.executable,
                "-m",
                "mobile_profiler",
                "--adb",
                "custom-adb",
            ])
            self.assertIn("record", command)
            self.assertNotIn("--session-mode", command)
            self.assertIn("--require-unplugged", command)
            self.assertIn("--test-mode", command)
            self.assertEqual(command[command.index("--test-mode") + 1], "performance")
            self.assertEqual(
                command[command.index("--performance-interval") + 1],
                "1.5",
            )
            self.assertEqual(command[command.index("--package") + 1], "com.example.game")
            self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertIn("--process-interval", command)
            self.assertIn("--thread-interval", command)
            self.assertNotIn("--no-system-monitor", command)
            duration_index = command.index("--duration")
            self.assertEqual(command[duration_index + 1], "1920")
            self.assertTrue(any(Path(value).name == "UI-smoke" for value in command))
            self.assertEqual(result["status"], "starting")

    def test_repeated_start_reserves_unique_run_directories_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = DashboardManager("custom-adb", root)
            created_runs = []

            def create_active(process, output_dir, config, command):
                active = Mock()
                active.running = False
                active.output_dir = output_dir
                active.snapshot.return_value = {
                    "running": False,
                    "status": "finished",
                    "run_name": output_dir.name,
                }
                created_runs.append((output_dir, config, command))
                return active

            payload = {
                "device": "SERIAL",
                "platform": "android",
                "run_name": "repeatable run",
                "test_mode": "power",
            }
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "SERIAL", "state": "device"}], None),
                ),
                patch("mobile_profiler.ui.subprocess.Popen", return_value=object()),
                patch("mobile_profiler.ui.ActiveRun", side_effect=create_active),
            ):
                manager.start_record(payload)
                first_data = root / "repeatable-run" / "samples.csv"
                first_data.write_text("first run\n", encoding="utf-8")

                manager.start_record(payload)
                second_data = root / "repeatable-run-2" / "samples.csv"
                second_data.write_text("second run\n", encoding="utf-8")

                manager.start_record(payload)

            self.assertEqual(
                [output_dir.name for output_dir, _, _ in created_runs],
                ["repeatable-run", "repeatable-run-2", "repeatable-run-3"],
            )
            self.assertEqual(
                [config["run_name"] for _, config, _ in created_runs],
                ["repeatable-run", "repeatable-run-2", "repeatable-run-3"],
            )
            self.assertEqual(first_data.read_text(encoding="utf-8"), "first run\n")
            self.assertEqual(second_data.read_text(encoding="utf-8"), "second run\n")
            self.assertTrue((root / "repeatable-run-3").is_dir())
            for output_dir, _, command in created_runs:
                output_index = command.index("--output")
                self.assertEqual(Path(command[output_index + 1]), output_dir)

    def test_start_failure_releases_reserved_run_directory_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = DashboardManager("custom-adb", root)
            fake_active = Mock()
            fake_active.running = False
            fake_active.output_dir = root / "retry-run"
            fake_active.snapshot.return_value = {
                "running": False,
                "status": "finished",
                "run_name": "retry-run",
            }
            payload = {
                "device": "SERIAL",
                "platform": "android",
                "run_name": "retry run",
            }
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "SERIAL", "state": "device"}], None),
                ),
                patch(
                    "mobile_profiler.ui.subprocess.Popen",
                    side_effect=[OSError("unable to launch"), object()],
                ),
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active) as active_run,
            ):
                with self.assertRaisesRegex(OSError, "unable to launch"):
                    manager.start_record(payload)
                self.assertFalse((root / "retry-run").exists())

                manager.start_record(payload)

            self.assertEqual(active_run.call_args.args[1], root / "retry-run")
            self.assertTrue((root / "retry-run").is_dir())

    def test_ui_defaults_optional_power_disconnect_off_in_both_modes(self) -> None:
        cases = (
            ("power", "", True),
            ("performance", "com.example.game", False),
        )
        for test_mode, package, expects_session_mode in cases:
            with self.subTest(test_mode=test_mode), tempfile.TemporaryDirectory() as directory:
                manager = DashboardManager("custom-adb", Path(directory))
                fake_active = Mock()
                fake_active.running = True
                fake_active.snapshot.return_value = {"running": True, "status": "starting"}
                with (
                    patch(
                        "mobile_profiler.ui.list_adb_devices",
                        return_value=([{"serial": "SERIAL", "state": "device"}], None),
                    ),
                    patch("mobile_profiler.ui.subprocess.Popen", return_value=object()) as popen,
                    patch("mobile_profiler.ui.ActiveRun", return_value=fake_active),
                ):
                    manager.start_record(
                        {
                            "device": "SERIAL",
                            "platform": "android",
                            "run_name": f"{test_mode} defaults",
                            "test_mode": test_mode,
                            "package": package,
                        }
                    )

                command = popen.call_args.args[0]
                self.assertEqual("--session-mode" in command, expects_session_mode)
                self.assertNotIn("--require-unplugged", command)

    def test_performance_ui_requires_target_application(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            with patch(
                "mobile_profiler.ui.list_adb_devices",
                return_value=([{"serial": "SERIAL", "state": "device"}], None),
            ):
                with self.assertRaisesRegex(ValueError, "target game or application"):
                    manager.start_record(
                        {
                            "device": "SERIAL",
                            "platform": "android",
                            "test_mode": "performance",
                            "run_name": "missing-game",
                        }
                    )

    def test_ui_rejects_incompatible_harmony_smartperf_preset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "SERIAL", "state": "device"}], None),
                ),
                patch("mobile_profiler.ui.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ValueError, "requires a HarmonyOS HDC device"):
                    manager.start_record(
                        {
                            "device": "SERIAL",
                            "platform": "android",
                            "test_mode": "performance",
                            "package": "com.example.game",
                            "capture_preset": "harmony-smartperf",
                        }
                    )
            popen.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), hdc="custom-hdc")
            with (
                patch("mobile_profiler.ui.list_adb_devices", return_value=([], None)),
                patch(
                    "mobile_profiler.ui.list_harmony_devices",
                    return_value=(
                        [{"serial": "harmony:PHONE", "state": "device", "platform": "harmony"}],
                        None,
                    ),
                ),
                patch("mobile_profiler.ui.list_ios_devices", return_value=([], None)),
                patch("mobile_profiler.ui.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ValueError, "available only in performance mode"):
                    manager.start_record(
                        {
                            "device": "harmony:PHONE",
                            "platform": "harmony",
                            "test_mode": "power",
                            "capture_preset": "harmony-smartperf",
                        }
                    )
            popen.assert_not_called()

    def test_harmony_ui_passes_capture_switches_and_high_performance_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), hdc="custom-hdc")
            fake_active = Mock()
            fake_active.running = True
            fake_active.snapshot.return_value = {"running": True, "status": "starting"}
            with (
                patch("mobile_profiler.ui.list_adb_devices", return_value=([], None)),
                patch(
                    "mobile_profiler.ui.list_harmony_devices",
                    return_value=(
                        [
                            {
                                "serial": "harmony:USB123",
                                "hdc_target": "USB123",
                                "state": "device",
                                "platform": "harmony",
                                "connection_type": "usb",
                            }
                        ],
                        None,
                    ),
                ),
                patch("mobile_profiler.ui.list_ios_devices", return_value=([], None)),
                patch("mobile_profiler.ui.subprocess.Popen", return_value=object()) as popen,
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active),
            ):
                manager.start_record(
                    {
                        "device": "harmony:USB123",
                        "platform": "harmony",
                        "test_mode": "performance",
                        "capture_preset": "harmony-smartperf",
                        "capture_features": {
                            "frame_rate": True,
                            "frame_details": True,
                            "touch_events": False,
                            "process_snapshots": False,
                        },
                        "harmony_high_performance": True,
                        "package": "com.example.game",
                        "run_name": "Harmony high performance",
                    }
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--capture-preset") + 1], "harmony-smartperf")
            self.assertIn("--harmony-high-performance", command)
            self.assertIn("--enable-feature", command)
            self.assertIn("--disable-feature", command)
            touch_index = command.index("touch_events")
            self.assertEqual(command[touch_index - 1], "--disable-feature")

    def test_ios_ui_passes_manual_platform_and_supported_capture_switches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager(
                "custom-adb",
                Path(directory),
                ios_python="ios-python",
            )
            fake_active = Mock()
            fake_active.running = True
            fake_active.snapshot.return_value = {"running": True, "status": "starting"}
            with (
                patch("mobile_profiler.ui.list_adb_devices", return_value=([], None)),
                patch("mobile_profiler.ui.list_harmony_devices", return_value=([], None)),
                patch(
                    "mobile_profiler.ui.list_ios_devices",
                    return_value=(
                        [
                            {
                                "serial": "ios:IPHONE",
                                "udid": "IPHONE",
                                "state": "device",
                                "platform": "ios",
                                "connection_type": "wireless",
                            }
                        ],
                        None,
                    ),
                ),
                patch("mobile_profiler.ui.subprocess.Popen", return_value=object()) as popen,
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active),
            ):
                manager.start_record(
                    {
                        "device": "ios:IPHONE",
                        "platform": "ios",
                        "test_mode": "performance",
                        "capture_features": {
                            "cpu_usage": True,
                            "gpu_metrics": True,
                            "frame_rate": False,
                            "process_snapshots": True,
                        },
                        "package": "com.example.iosapp",
                        "run_name": "iOS performance",
                    }
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--platform") + 1], "ios")
            self.assertEqual(command[command.index("--device") + 1], "ios:IPHONE")
            self.assertEqual(command[command.index("--package") + 1], "com.example.iosapp")
            frame_index = command.index("frame_rate")
            self.assertEqual(command[frame_index - 1], "--disable-feature")

    def test_ui_rejects_manual_platform_and_device_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=(
                        [
                            {
                                "serial": "ANDROID",
                                "state": "device",
                                "platform": "android",
                                "connection_type": "usb",
                            }
                        ],
                        None,
                    ),
                ),
                patch("mobile_profiler.ui.list_harmony_devices", return_value=([], None)),
                patch("mobile_profiler.ui.list_ios_devices", return_value=([], None)),
            ):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    manager.start_record(
                        {
                            "device": "ANDROID",
                            "platform": "harmony",
                            "run_name": "wrong-platform",
                        }
                    )

    def test_ui_can_run_adb_connect_and_refresh_device_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            completed = Mock(returncode=0, stdout="connected to 192.168.1.20:5555\n", stderr="")
            with (
                patch("mobile_profiler.ui.subprocess.run", return_value=completed) as run,
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "192.168.1.20:5555", "state": "device"}], None),
                ),
            ):
                result = manager.connect_device({"address": "192.168.1.20:5555"})
        self.assertTrue(result["connected"])
        self.assertEqual(run.call_args.args[0], ["custom-adb", "connect", "192.168.1.20:5555"])

    def test_ui_can_disconnect_wireless_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            completed = Mock(
                returncode=0,
                stdout="disconnected 192.168.1.20:5555\n",
                stderr="",
            )
            with (
                patch("mobile_profiler.ui.subprocess.run", return_value=completed) as run,
                patch.object(manager, "devices", return_value=([], None)),
            ):
                result = manager.disconnect_device({"address": "192.168.1.20:5555"})

            self.assertTrue(result["disconnected"])
            self.assertEqual(
                run.call_args.args[0],
                ["custom-adb", "disconnect", "192.168.1.20:5555"],
            )
            with self.assertRaises(ValueError):
                manager.disconnect_device({"address": "USB123"})
            manager.active = Mock(running=True)
            with self.assertRaises(RuntimeError):
                manager.disconnect_device({"address": "192.168.1.20:5555"})

    def test_wifi_address_parser_prioritizes_wlan(self) -> None:
        addresses = parse_device_ipv4_addresses(
            "19: rmnet_data0 inet 10.20.30.40/30 scope global rmnet_data0\n"
            "24: wlan0 inet 192.168.21.90/24 brd 192.168.21.255 scope global wlan0\n"
        )
        self.assertEqual(addresses[0]["interface"], "wlan0")
        self.assertEqual(addresses[0]["address"], "192.168.21.90")
        self.assertTrue(addresses[0]["wifi"])

    def test_ui_can_enable_tcpip_and_auto_connect_wifi_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            ip_result = Mock(
                ok=True,
                stdout=(
                    "24: wlan0 inet 192.168.21.90/24 brd 192.168.21.255 "
                    "scope global wlan0\n"
                ),
            )
            tcpip_result = Mock(
                returncode=0,
                stdout="restarting in TCP mode port: 5555\n",
                stderr="",
            )
            connected = {
                "connected": True,
                "output": "connected to 192.168.21.90:5555",
            }
            with (
                patch(
                    "mobile_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "USB123", "state": "device"}], None),
                ),
                patch("mobile_profiler.ui.adb_shell", return_value=ip_result) as shell,
                patch("mobile_profiler.ui.subprocess.run", return_value=tcpip_result) as run,
                patch.object(manager, "connect_device", return_value=connected) as connect,
                patch("mobile_profiler.ui.time.sleep", return_value=None),
            ):
                result = manager.enable_tcpip(
                    {"device": "USB123", "port": 5555, "auto_connect": True}
                )

        self.assertTrue(result["tcpip_enabled"])
        self.assertTrue(result["connected"])
        self.assertEqual(result["suggested_address"], "192.168.21.90:5555")
        self.assertEqual(
            shell.call_args.args[2],
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
        )
        self.assertEqual(
            run.call_args.args[0],
            ["custom-adb", "-s", "USB123", "tcpip", "5555"],
        )
        connect.assert_called_once_with({"address": "192.168.21.90:5555"})

    def test_ui_can_enable_harmony_tcpip_and_refresh_hdc_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager(
                "custom-adb",
                Path(directory),
                hdc="custom-hdc",
            )
            enabled = {
                "tcpip_enabled": True,
                "connected": True,
                "suggested_address": "192.168.21.8:8710",
                "suggested_device": "harmony:192.168.21.8:8710",
            }
            refreshed = [
                {
                    "serial": "harmony:192.168.21.8:8710",
                    "state": "device",
                    "platform": "harmony",
                }
            ]
            with (
                patch("mobile_profiler.ui.enable_harmony_tcp", return_value=enabled) as enable,
                patch.object(manager, "devices", return_value=(refreshed, None)) as devices,
            ):
                result = manager.enable_harmony_tcpip(
                    {"device": "harmony:USB123", "port": 8710, "auto_connect": True}
                )

        self.assertTrue(result["connected"])
        self.assertEqual(result["devices"], refreshed)
        enable.assert_called_once_with(
            "custom-hdc",
            "harmony:USB123",
            8710,
            auto_connect=True,
        )
        devices.assert_called_once_with(
            force=True,
            refresh_ios=False,
            refresh_harmony=True,
        )

    def test_runtime_marker_is_appended_with_device_uptime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = DashboardManager("adb", root)
            active = Mock()
            active.running = True
            active.output_dir = root / "run"
            active.output_dir.mkdir()
            active._lock = threading.RLock()
            active.logs = []
            active.snapshot.return_value = {
                "latest": {"uptime_s": 123.5},
                "context": {"foreground_package": "com.example", "foreground_activity": ".Main"},
            }
            manager.active = active
            marker = manager.add_marker({"name": "BTR2 开始"})
            stored = json.loads((active.output_dir / "events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(marker["device_uptime_s"], 123.5)
        self.assertEqual(stored["source"], "runtime_ui")
        self.assertEqual(stored["metadata"]["foreground_package"], "com.example")

    def test_report_range_delete_persists_edit_and_regenerates_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text("{}", encoding="utf-8")
            manager = DashboardManager("adb", root)
            samples = [Mock(uptime_s=float(value)) for value in range(100, 105)]
            with (
                patch("mobile_profiler.ui.read_samples_csv", return_value=samples),
                patch.object(manager, "_run_cli", return_value="ok") as run_cli,
            ):
                result = manager.delete_report_range(
                    {
                        "run_name": "phone-a",
                        "start_uptime_s": 101.0,
                        "end_uptime_s": 103.0,
                    }
                )
            edits = json.loads((run_dir / "report-edits.json").read_text(encoding="utf-8"))

        self.assertEqual(result["deleted_sample_count"], 3)
        self.assertEqual(result["excluded_range_count"], 1)
        self.assertEqual(
            edits["excluded_ranges"],
            [{"start_uptime_s": 101.0, "end_uptime_s": 103.0}],
        )
        run_cli.assert_called_once_with(
            ["report", str(run_dir.resolve())],
            "Deleting report range for phone-a",
        )

    def test_report_range_delete_rolls_back_edit_when_regeneration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text("{}", encoding="utf-8")
            edits_path = run_dir / "report-edits.json"
            original = b'{"schema_version":1,"excluded_ranges":[]}'
            edits_path.write_bytes(original)
            manager = DashboardManager("adb", root)
            samples = [Mock(uptime_s=float(value)) for value in range(100, 106)]
            with (
                patch("mobile_profiler.ui.read_samples_csv", return_value=samples),
                patch.object(manager, "_run_cli", side_effect=RuntimeError("failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    manager.delete_report_range(
                        {
                            "run_name": "phone-a",
                            "start_uptime_s": 101.0,
                            "end_uptime_s": 102.0,
                        }
                    )
            restored = edits_path.read_bytes()

        self.assertEqual(restored, original)

    def test_report_range_delete_rejects_removing_all_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text("{}", encoding="utf-8")
            manager = DashboardManager("adb", root)
            samples = [Mock(uptime_s=float(value)) for value in range(100, 103)]
            with patch("mobile_profiler.ui.read_samples_csv", return_value=samples):
                with self.assertRaisesRegex(ValueError, "fewer than two samples"):
                    manager.delete_report_range(
                        {
                            "run_name": "phone-a",
                            "start_uptime_s": 100.0,
                            "end_uptime_s": 102.0,
                        }
                    )

            self.assertFalse((run_dir / "report-edits.json").exists())

    def test_ui_report_recover_and_import_reuse_cli_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text("{}", encoding="utf-8")
            log_path = root / "btr2.log"
            log_path.write_text("2026-07-12 10:00:00 start\n", encoding="utf-8")
            rules_path = root / "rules.json"
            rules_path.write_text("[]", encoding="utf-8")
            manager = DashboardManager("custom-adb", root)
            completed = Mock(returncode=0, stdout="ok\n", stderr="")
            with patch("mobile_profiler.ui.subprocess.run", return_value=completed) as run:
                report = manager.regenerate_run({"run_name": "phone-a"})
                recovered = manager.recover_run({"run_name": "phone-a"})
                imported = manager.import_run_log(
                    {
                        "run_name": "phone-a",
                        "log_path": str(log_path),
                        "rules_path": str(rules_path),
                        "replace": True,
                        "match": "phone_key=phone1",
                    }
                )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("report", commands[0])
        self.assertIn("recover", commands[1])
        self.assertIn("import-log", commands[2])
        self.assertIn("--replace", commands[2])
        self.assertEqual(commands[2][-2:], ["--match", "phone_key=phone1"])
        self.assertEqual(report["report_url"], "/runs/phone-a/report.html")
        self.assertEqual(recovered["run_name"], "phone-a")
        self.assertEqual(imported["log_path"], str(log_path))

    def test_ui_archive_accepts_external_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text('{"title":"Phone A"}', encoding="utf-8")
            (run_dir / "samples.csv").write_text("sample\n", encoding="utf-8")
            attachment = root / "btr2.log"
            attachment.write_text("log\n", encoding="utf-8")
            manager = DashboardManager("adb", root)

            result = manager.archive_history_run(
                {"run_name": "phone-a", "attachments": str(attachment)}
            )

            self.assertTrue(Path(result["archive_path"]).is_file())
            self.assertEqual(result["attachment_count"], 1)
            self.assertGreaterEqual(result["entry_count"], 2)

    def test_ui_compare_uses_history_runs_and_safe_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("phone-a", "phone-b"):
                run_dir = root / name
                run_dir.mkdir()
                (run_dir / "metadata.json").write_text("{}", encoding="utf-8")
            manager = DashboardManager("adb", root)
            completed = Mock(returncode=0, stdout="comparison ready\n", stderr="")
            with patch("mobile_profiler.ui.subprocess.run", return_value=completed) as run:
                result = manager.compare_history_runs(
                    {
                        "run_a": "phone-a",
                        "run_b": "phone-b",
                        "label_a": "Phone A",
                        "label_b": "Phone B",
                        "output_name": "A vs B",
                    }
                )

            command = run.call_args.args[0]
            self.assertIn("compare", command)
            self.assertIn("--label-a", command)
            self.assertEqual(result["output_name"], "A-vs-B")
            self.assertEqual(result["comparison_url"], "/comparisons/A-vs-B/comparison.html")

    def test_portable_build_is_limited_to_source_dist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "tools").mkdir()
            (source / "tools" / "build-portable.ps1").write_text("", encoding="utf-8")
            manager = DashboardManager("adb", source / "runs")
            manager.source_root = source
            output_dir = source / "dist" / "portable-test"
            self.assertEqual(
                manager.tooling_state()["portable_output_default"],
                str(source / "dist" / f"mobile-profiler-v{__version__}-portable"),
            )

            def fake_build(command, operation, **kwargs):
                output_dir.mkdir(parents=True)
                Path(f"{output_dir}.zip").write_bytes(b"zip")
                return "built"

            with (
                patch("mobile_profiler.ui.shutil.which", return_value="powershell.exe"),
                patch.object(manager, "_run_command", side_effect=fake_build) as build,
            ):
                result = manager.build_portable_bundle(
                    {"output_directory": str(output_dir), "include_adb": False}
                )

            command = build.call_args.args[0]
            self.assertIn("-SkipAdb", command)
            self.assertIn("-PythonVersion", command)
            self.assertEqual(result["version"], __version__)
            self.assertEqual(result["zip_path"], f"{output_dir}.zip")
            with self.assertRaises(ValueError):
                manager.build_portable_bundle(
                    {"output_directory": str(source / "unsafe-output")}
                )


class UiConfigurationTests(unittest.TestCase):
    def test_windows_ctrl_break_is_converted_to_keyboard_interrupt(self) -> None:
        with (
            patch("mobile_profiler.cli.sys.platform", "win32"),
            patch("mobile_profiler.cli.signal.SIGBREAK", 21, create=True),
            patch("mobile_profiler.cli.signal.signal") as register,
        ):
            install_console_interrupt_handlers()

        signum, handler = register.call_args.args
        self.assertEqual(signum, 21)
        with self.assertRaises(KeyboardInterrupt):
            handler(signum, None)

    def test_run_name_is_safe_and_readable(self) -> None:
        self.assertRegex(sanitize_run_name(""), r"^mobile-profile-\d{8}-\d{6}$")
        self.assertEqual(sanitize_run_name(" BTR2 round 001 "), "BTR2-round-001")
        self.assertEqual(sanitize_run_name("../unsafe/name"), "unsafe-name")
        self.assertEqual(sanitize_run_name("续航 第 1 轮"), "续航-第-1-轮")

    def test_cli_parser_exposes_ui_options(self) -> None:
        args = build_parser().parse_args(
            ["ui", "--host", "127.0.0.1", "--port", "0", "--no-browser", "--demo"]
        )
        self.assertEqual(args.command, "ui")
        self.assertEqual(args.port, 0)
        self.assertEqual(args.output_root, Path("profiler-runs"))
        self.assertTrue(args.no_browser)
        self.assertTrue(args.demo)

    def test_cli_parser_exposes_system_monitor_options(self) -> None:
        args = build_parser().parse_args(
            [
                "record",
                "--duration",
                "60",
                "--process-interval",
                "5",
                "--thread-interval",
                "20",
                "--thermal-interval",
                "5",
                "--scheduler-interval",
                "20",
                "--no-system-monitor",
            ]
        )
        self.assertEqual(args.process_interval, 5.0)
        self.assertEqual(args.thread_interval, 20.0)
        self.assertTrue(args.no_system_monitor)

        performance = build_parser().parse_args(
            [
                "record",
                "--test-mode",
                "performance",
                "--performance-interval",
                "1.5",
                "--capture-preset",
                "harmony-smartperf",
                "--disable-feature",
                "touch_events",
                "--harmony-high-performance",
            ]
        )
        self.assertEqual(performance.test_mode, "performance")
        self.assertEqual(performance.performance_interval, 1.5)
        self.assertEqual(performance.capture_preset, "harmony-smartperf")
        self.assertEqual(performance.disable_feature, ["touch_events"])
        self.assertTrue(performance.harmony_high_performance)
        apply_record_interval_defaults(performance)
        self.assertEqual(performance.process_interval, 2.0)
        self.assertEqual(performance.thread_interval, 5.0)
        self.assertEqual(performance.thermal_interval, 5.0)
        self.assertEqual(performance.scheduler_interval, 5.0)

    def test_cli_rejects_multi_app_session_in_performance_mode(self) -> None:
        args = build_parser().parse_args(
            ["record", "--test-mode", "performance", "--session-mode"]
        )

        with patch("builtins.print") as print_mock:
            result = args.handler(args)

        self.assertEqual(result, 2)
        self.assertIn("power test mode", print_mock.call_args.args[0])

    def test_cli_parser_exposes_ios_sidecar_workflow(self) -> None:
        record = build_parser().parse_args(
            [
                "--ios-python",
                "C:/ios/python.exe",
                "record",
                "--platform",
                "ios",
                "--device",
                "ios:00008150-TEST",
            ]
        )
        pair = build_parser().parse_args(
            [
                "--ios-python",
                "C:/ios/python.exe",
                "ios-pair",
                "--device",
                "00008150-TEST",
            ]
        )
        self.assertEqual(record.platform, "ios")
        self.assertEqual(record.device, "ios:00008150-TEST")
        self.assertEqual(record.ios_python, "C:/ios/python.exe")
        self.assertEqual(pair.command, "ios-pair")

    def test_cli_parser_exposes_harmony_hdc_workflow(self) -> None:
        record = build_parser().parse_args(
            [
                "--hdc",
                "C:/DevEco/hdc.exe",
                "record",
                "--platform",
                "harmony",
                "--device",
                "harmony:192.168.21.8:8710",
            ]
        )
        probe = build_parser().parse_args(
            [
                "--hdc",
                "custom-hdc",
                "probe",
                "--platform",
                "harmony",
                "--device",
                "harmony:USB123",
            ]
        )
        self.assertEqual(record.platform, "harmony")
        self.assertEqual(record.hdc, "C:/DevEco/hdc.exe")
        self.assertEqual(probe.device, "harmony:USB123")
        self.assertEqual(requested_platform(record), "harmony")

    def test_cli_parser_exposes_portable_workflow_commands(self) -> None:
        record = build_parser().parse_args(
            ["record", "--duration", "60", "--start-context", "desktop", "--start-note", "BTR2 later"]
        )
        archive = build_parser().parse_args(["archive", "run-a", "--attach", "btr2.log"])
        compare = build_parser().parse_args(["compare", "run-a", "run-b", "--label-a", "Phone A"])
        import_log = build_parser().parse_args(
            ["import-log", "run-a", "combined.log", "--rules", "rules.json", "--match", "phone_key=phone1"]
        )
        self.assertEqual(record.start_context, "desktop")
        self.assertEqual(record.start_note, "BTR2 later")
        self.assertEqual(archive.command, "archive")
        self.assertEqual(compare.command, "compare")
        self.assertEqual(import_log.match, ["phone_key=phone1"])


if __name__ == "__main__":
    unittest.main()
