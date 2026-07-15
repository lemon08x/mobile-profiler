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
            self.assertEqual(snapshot["performance"]["one_percent_low_fps"], 50.0)
            self.assertIn("power_pressure", snapshot)
            self.assertIn("render_performance", snapshot)
            self.assertTrue(snapshot["runtime_settings"]["available"] is False)


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
                with urlopen(base + "/app.css?v=platform-ui-13", timeout=5) as response:
                    css = response.read().decode("utf-8")
                with urlopen(base + "/app.js?v=platform-ui-13", timeout=5) as response:
                    javascript = response.read().decode("utf-8")
                with urlopen(base + "/api/state", timeout=5) as response:
                    state = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                manager.close()
                thread.join(timeout=5)

            self.assertIn("Mobile Profiler", html)
            self.assertIn("TEST PLATFORM", html)
            self.assertIn("ADB / gfxinfo", html)
            self.assertIn("DVT / RemoteXPC", html)
            self.assertIn("HDC / SmartPerf", html)
            self.assertIn("ADB IP", html)
            self.assertIn("无线 ADB", html)
            self.assertIn("鸿蒙无线", html)
            self.assertIn("iOS 无线", html)
            self.assertIn("断开无线", html)
            self.assertIn("性能上下文", html)
            self.assertIn("功耗测试模式", html)
            self.assertIn("FPS / 1% Low / 调度", html)
            self.assertIn("必要设置", html)
            self.assertIn("更多采集设置", html)
            self.assertIn("设备亮度", html)
            self.assertIn('id="brightness-input"', html)
            self.assertIn("platform-ui-13", html)
            self.assertNotIn('<details class="advanced-settings" open', html)
            self.assertIn(".capture-feature-card input", css)
            self.assertIn("pointer-events: auto", css)
            self.assertIn("captureFeaturesOverridden", javascript)
            self.assertIn('api("/api/brightness"', javascript)
            self.assertIn("扫描手机应用", html)
            self.assertIn("32 分钟", html)
            self.assertIn("资源调度分配", html)
            self.assertIn("功耗压力解释", html)
            self.assertIn("帧率数据流与渲染链路", html)
            self.assertIn("内存频率", html)
            self.assertIn("FRAME RATE", html)
            self.assertIn("硬件采样率未公开", html)
            self.assertNotIn('data-view="thermal"', html)
            self.assertIn("工具与交付", html)
            self.assertIn("导入 BTR2 日志", html)
            self.assertTrue(state["active"]["is_demo"])
            self.assertIn("portable_build_available", state["tooling"])
            self.assertEqual(len(state["active"]["series"]), 240)
            self.assertEqual(state["active"]["performance"]["current_refresh_rate_hz"], 120.0)
            self.assertEqual(
                state["active"]["performance"]["frame_flow"]["primary_key"],
                "surface_present",
            )
            self.assertEqual(
                len(state["active"]["performance"]["frame_flow"]["stages"]),
                4,
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
            self.assertTrue(tcpip_result["tcpip_enabled"])
            self.assertTrue(disconnect_result["disconnected"])
            self.assertEqual(apps_result["apps"][0]["package"], "com.example.game")
            self.assertIn("comparison", comparison_html)
            manager.regenerate_run.assert_called_once_with({"run_name": "phone-a"})
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
                str(source / "dist" / "mobile-profiler-portable"),
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
