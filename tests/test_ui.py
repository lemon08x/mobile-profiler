from __future__ import annotations

import base64
from collections import Counter
import json
import re
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
from mobile_profiler.collector import (
    _brightness_tracking_required,
    _sampler_shell_command,
    build_sampler_script,
    parse_sampler_line,
)
from mobile_profiler.harmony import (
    HarmonySmartPerfParser,
    SMARTPERF_UNLIMITED_BATCH_SAMPLE_COUNT,
    _smartperf_command,
    build_harmony_sample,
    build_harmony_smartperf_sample,
)
from mobile_profiler.ios_bridge import _battery_payload
from mobile_profiler.models import (
    ContextSample,
    GpuSource,
    RawSample,
    Sample,
    ThermalSnapshot,
)
from mobile_profiler.ui import (
    DashboardHandler,
    DashboardHTTPServer,
    DashboardManager,
    LiveTelemetryReader,
    _decimate,
    _decimate_timeline_rows,
    _percentile,
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
    @staticmethod
    def _timeline_sample(
        index: int,
        *,
        power_mw: float = 400.0,
        cpu_pct: float = 50.0,
    ) -> Sample:
        return Sample(
            index=index,
            elapsed_s=float(index),
            uptime_s=100.0 + index,
            current_ma=100.0,
            signed_current_ma=-100.0,
            voltage_mv=4000.0,
            power_mw=power_mw,
            direction="discharging",
            cpu_pct=cpu_pct,
            power_source="battery_current_voltage",
            power_valid_for_consumption=True,
            external_power=False,
        )

    def test_live_decimation_preserves_long_session_extrema_and_bound(self) -> None:
        samples = [
            self._timeline_sample(
                index,
                power_mw=4200.0 if index == 1877 else 400.0,
                cpu_pct=2.0 if index == 2133 else 50.0,
            )
            for index in range(62 * 60 + 1)
        ]

        displayed = _decimate(samples, limit=900)
        displayed_indexes = [sample.index for sample in displayed]

        self.assertEqual(len(displayed), 900)
        self.assertEqual(displayed_indexes[0], 0)
        self.assertEqual(displayed_indexes[-1], 62 * 60)
        self.assertIn(1877, displayed_indexes)
        self.assertIn(2133, displayed_indexes)

    def test_live_snapshot_exposes_unlimited_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "requested_duration_s": 0,
                        "duration_unlimited": True,
                        "sample_interval_s": 1.0,
                    }
                ),
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(output_dir).snapshot()

        self.assertTrue(snapshot["duration_unlimited"])
        self.assertEqual(snapshot["requested_duration_s"], 0.0)
        self.assertEqual(snapshot["progress"], 0.0)

    def test_live_timeline_decimation_keeps_short_steps_and_fps_drops(self) -> None:
        refresh_rows = [
            {
                "uptime_s": float(index),
                "elapsed_s": float(index),
                "value": 60.0 if index == 1877 else 120.0,
            }
            for index in range(62 * 60 + 1)
        ]
        fps_rows = [
            {
                "uptime_s": float(index),
                "elapsed_s": float(index),
                "value": 7.0 if index == 1499 else 60.0,
            }
            for index in range(32 * 60 + 1)
        ]

        refresh = _decimate_timeline_rows(
            refresh_rows,
            limit=900,
            preserve_steps=True,
        )
        fps = _decimate_timeline_rows(fps_rows, limit=900)
        refresh_indexes = [int(row["uptime_s"]) for row in refresh]
        fps_indexes = [int(row["uptime_s"]) for row in fps]

        self.assertEqual(len(refresh), 900)
        self.assertEqual(len(fps), 900)
        self.assertTrue({1876, 1877, 1878}.issubset(refresh_indexes))
        self.assertIn(1499, fps_indexes)

    def test_timeline_decimation_remains_bounded_with_many_breaks(self) -> None:
        rows = [
            {
                "elapsed_s": float(index),
                "value": float(index % 5),
                "report_break_before": index > 0 and index % 2 == 0,
            }
            for index in range(200)
        ]

        displayed = _decimate_timeline_rows(rows, limit=17)

        self.assertEqual(len(displayed), 17)
        self.assertEqual(displayed[0]["elapsed_s"], 0.0)
        self.assertEqual(displayed[-1]["elapsed_s"], 199.0)
        self.assertTrue(any(row.get("report_break_before") for row in displayed[1:]))

    def test_frontend_timeline_downsampler_has_shape_preservation_contract(self) -> None:
        javascript = (
            Path(__file__).parents[1]
            / "src"
            / "mobile_profiler"
            / "web"
            / "app.js"
        ).read_text(encoding="utf-8")
        start = javascript.index("function downsampleTimelinePoints")
        end = javascript.index("function timelinePoints", start)
        block = javascript[start:end]

        self.assertIn("boundedLimit", block)
        self.assertIn("breakBoundaryGroups", block)
        self.assertIn("stepBoundaryGroups", block)
        self.assertIn("bucketExtrema", block)
        self.assertIn("skippedBreak", block)
        self.assertNotIn("const step = candidates.length / remaining", block)

    def test_inactive_vendor_brightness_does_not_present_candidate_gear_as_effective(self) -> None:
        javascript = (
            Path(__file__).parents[1]
            / "src"
            / "mobile_profiler"
            / "web"
            / "app.js"
        ).read_text(encoding="utf-8")
        start = javascript.index("function renderBrightnessThrottling")
        end = javascript.index("function svgNode", start)
        block = javascript[start:end]

        self.assertIn("const vendorLimitDescribesActive = vendorActive", block)
        self.assertIn("运行时 active=false，当前没有生效档位", block)
        self.assertIn("候选表不能证明哪些档位会在实际温控中触发", block)
        self.assertIn("只有 active=true 时才把运行时档位和标称 nit 作为限亮证据", block)

    def test_frontend_error_notifications_are_recorded_in_runtime_console(self) -> None:
        javascript = (
            Path(__file__).parents[1]
            / "src"
            / "mobile_profiler"
            / "web"
            / "app.js"
        ).read_text(encoding="utf-8")
        record_start = javascript.index("function recordUiError")
        notify_start = javascript.index("function notify", record_start)
        set_busy_start = javascript.index("function setBusy", notify_start)
        console_start = javascript.index("function renderConsole")
        console_end = javascript.index("const liveTimelinePalette", console_start)
        clear_start = javascript.index('$("#clear-console").addEventListener')
        clear_end = javascript.index('$("#refresh-history")', clear_start)
        record_block = javascript[record_start:notify_start]
        notify_block = javascript[notify_start:set_busy_start]
        console_block = javascript[console_start:console_end]
        clear_block = javascript[clear_start:clear_end]

        self.assertIn("uiLogs: []", javascript)
        self.assertIn('if (type === "error") recordUiError(title, detail)', notify_block)
        self.assertIn('notify("无法开始采集", error.message, "error", 8000)', javascript)
        self.assertIn("Date.now() / 1000", record_block)
        self.assertIn("uiErrorDedupWindowS", record_block)
        self.assertIn("previous?.signature === signature", record_block)
        self.assertIn('source: "ui"', record_block)
        self.assertIn('type: "error"', record_block)
        self.assertIn("normalizedTitle", record_block)
        self.assertIn("normalizedDetail", record_block)
        self.assertIn("active?.logs", console_block)
        self.assertIn("...app.uiLogs", console_block)
        self.assertIn("logs.sort(", console_block)
        self.assertIn('item.type === "error"', console_block)
        self.assertRegex(clear_block, r"app\.uiLogs\s*=\s*\[\]")
        self.assertIn("app.consoleClearedAt", clear_block)
        self.assertIn("renderConsole", clear_block)

    def test_frontend_supports_unlimited_duration_mode(self) -> None:
        web_root = Path(__file__).parents[1] / "src" / "mobile_profiler" / "web"
        html = (web_root / "index.html").read_text(encoding="utf-8")
        css = (web_root / "app.css").read_text(encoding="utf-8")
        javascript = (web_root / "app.js").read_text(encoding="utf-8")
        readiness_start = javascript.index("function recordingStartReadiness")
        readiness_end = javascript.index("function updateStartControlState", readiness_start)
        payload_start = javascript.index("function recordPayload")
        payload_end = javascript.index("function bindEvents", payload_start)
        session_start = javascript.index("function renderSession")
        session_end = javascript.index("function renderClusters", session_start)

        self.assertIn('data-duration="unlimited">无上限 · 手动停止</button>', html)
        self.assertIn('id="duration-mode-hint"', html)
        self.assertIn('.duration-presets button[data-duration="unlimited"]', css)
        self.assertIn("grid-column: 1 / -1", css)
        self.assertIn(".progress-track.indeterminate span", css)
        self.assertIn("durationUnlimited: false", javascript)
        self.assertIn("!app.durationUnlimited", javascript[readiness_start:readiness_end])
        self.assertIn("duration_unlimited: app.durationUnlimited", javascript[payload_start:payload_end])
        self.assertIn("active?.config?.duration_unlimited", javascript)
        self.assertIn("/ 无上限", javascript[session_start:session_end])
        self.assertIn('classList.toggle("indeterminate"', javascript[session_start:session_end])

    def test_frontend_distinguishes_ios_bluetooth_pan_and_wifi(self) -> None:
        javascript = (
            Path(__file__).parents[1]
            / "src"
            / "mobile_profiler"
            / "web"
            / "app.js"
        ).read_text(encoding="utf-8")
        helper_start = javascript.index("function iosWirelessTransport")
        helper_end = javascript.index("function recordingDeviceReadiness", helper_start)
        devices_start = javascript.index("function deviceConnectionLabel")
        devices_end = javascript.index("function renderDevices", devices_start)
        performance_start = javascript.index("function renderPerformanceMetrics")
        performance_end = javascript.index("function renderPerformanceResources", performance_start)
        helper_block = javascript[helper_start:helper_end]
        devices_block = javascript[devices_start:devices_end]
        performance_block = javascript[performance_start:performance_end]

        self.assertIn('return "bluetooth-pan"', helper_block)
        self.assertIn('return "wifi"', helper_block)
        self.assertIn('"bluetooth-pan": "蓝牙热点（PAN）RemotePairing"', helper_block)
        self.assertIn('wifi: "Wi-Fi RemotePairing"', helper_block)
        self.assertIn("iosWirelessTransportLabel(device)", devices_block)
        self.assertIn("无线设备（Wi-Fi / 蓝牙 PAN）", javascript)
        self.assertIn("device?.config?.wireless_transport", helper_block)
        self.assertLess(
            performance_block.index("const performance = active?.performance || {}"),
            performance_block.index('activePlatform(active) === "ios"'),
        )

    def test_live_decimation_preserves_power_source_switch_boundaries(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=10.0,
                signed_current_ma=-10.0,
                voltage_mv=4000.0,
                power_mw=40.0 if index < 10 else 600.0,
                direction="discharging",
                cpu_pct=10.0,
                power_source=(
                    "ios_battery_current_voltage"
                    if index < 10
                    else "ios_power_telemetry_system_load"
                ),
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(20)
        ]

        displayed = _decimate(samples, limit=5)

        displayed_indexes = [sample.index for sample in displayed]
        self.assertIn(9, displayed_indexes)
        self.assertIn(10, displayed_indexes)
        self.assertLess(displayed_indexes.index(9), displayed_indexes.index(10))

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

    def test_reader_separates_raw_power_from_consumption_valid_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "test_mode": "performance",
                        "requested_duration_s": 60,
                        "sample_interval_s": 1.0,
                        "battery_start": {
                            "voltage_mv": 4000.0,
                            "status": "discharging",
                            "powered": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            rows = []
            for index, state in enumerate(("D", "D", "C", "C", "D", "D")):
                discharging = state == "D"
                power_mw = 1000.0 if discharging else 10000.0
                current_ma = power_mw / 4.0
                rows.append(
                    {
                        "index": index,
                        "elapsed_s": float(index),
                        "uptime_s": 100.0 + index,
                        "current_ma": current_ma,
                        "signed_current_ma": -current_ma if discharging else current_ma,
                        "voltage_mv": 4000.0,
                        "power_mw": power_mw,
                        "direction": "discharging" if discharging else "charging",
                        "cpu_pct": float(index * 10),
                        "power_source": "ios_power_telemetry_system_load",
                        "power_sample_age_s": float(index),
                        "power_valid_for_consumption": discharging,
                        "external_power": not discharging,
                    }
                )
            (root / "raw" / "sampler-stream.txt").write_text(
                "\n".join(f"N|{json.dumps(row)}" for row in rows) + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            summary = snapshot["summary"]
            self.assertTrue(summary["power_valid_for_consumption"])
            self.assertEqual(summary["power_flow_direction"], "mixed")
            self.assertAlmostEqual(summary["average_power_mw"], 1000.0)
            self.assertAlmostEqual(summary["p95_power_mw"], 1000.0)
            self.assertAlmostEqual(summary["energy_mwh"], 2.0 * 1000.0 / 3600.0)
            self.assertAlmostEqual(summary["observed_power_average_mw"], 4600.0)
            self.assertAlmostEqual(
                summary["observed_power_energy_mwh"],
                23000.0 / 3600.0,
            )
            self.assertEqual(summary["consumption_covered_duration_s"], 2.0)
            self.assertEqual(snapshot["power_pressure"]["drivers"], [])
            self.assertAlmostEqual(
                snapshot["render_performance"]["power_recording"]["average_power_mw"],
                1000.0,
            )
            self.assertFalse(snapshot["series"][2]["power_valid_for_consumption"])
            self.assertTrue(snapshot["series"][2]["external_power"])
            self.assertEqual(
                snapshot["series"][2]["power_source"],
                "ios_power_telemetry_system_load",
            )
            self.assertIn("collector_cpu_pct", snapshot["series"][2])

    def test_reader_keeps_mixed_ios_system_load_statistics_out_of_battery_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "platform": "ios",
                        "test_mode": "power",
                        "requested_duration_s": 60,
                        "sample_interval_s": 1.0,
                        "battery_start": {
                            "voltage_mv": 4000.0,
                            "status": "discharging",
                            "powered": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            rows = []
            for index, (power_mw, current_ma, power_source) in enumerate(
                (
                    (100.0, 25.0, "ios_battery_current_voltage"),
                    (200.0, 50.0, "ios_battery_current_voltage"),
                    (1000.0, 75.0, "ios_power_telemetry_system_load"),
                    (1200.0, 100.0, "ios_power_telemetry_system_load"),
                )
            ):
                rows.append(
                    {
                        "index": index,
                        "elapsed_s": float(index),
                        "uptime_s": 100.0 + index,
                        "current_ma": current_ma,
                        "signed_current_ma": -current_ma,
                        "voltage_mv": 4000.0,
                        "power_mw": power_mw,
                        "direction": "discharging",
                        "cpu_pct": 20.0,
                        "power_source": power_source,
                        "power_sample_age_s": (
                            float(index - 2)
                            if power_source == "ios_power_telemetry_system_load"
                            else None
                        ),
                        "power_valid_for_consumption": True,
                        "external_power": False,
                    }
                )
            (root / "raw" / "sampler-stream.txt").write_text(
                "\n".join(f"N|{json.dumps(row)}" for row in rows) + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            summary = snapshot["summary"]
            self.assertEqual(
                summary["power_sources"],
                [
                    "ios_battery_current_voltage",
                    "ios_power_telemetry_system_load",
                ],
            )
            self.assertTrue(summary["system_load_available"])
            self.assertEqual(summary["system_load_sample_count"], 2)
            self.assertEqual(summary["system_load_latest_power_mw"], 1200.0)
            self.assertAlmostEqual(
                summary["system_load_observed_average_power_mw"],
                1100.0,
            )
            self.assertAlmostEqual(
                summary["system_load_consumption_average_power_mw"],
                1100.0,
            )
            self.assertAlmostEqual(summary["battery_flow_average_power_mw"], 250.0)
            self.assertNotAlmostEqual(
                summary["observed_power_average_mw"],
                summary["system_load_observed_average_power_mw"],
            )
            self.assertEqual(
                [point["power_source"] for point in snapshot["series"]],
                [row["power_source"] for row in rows],
            )

    def test_reader_marks_stale_ios_system_load_as_raw_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "platform": "ios",
                        "test_mode": "power",
                        "requested_duration_s": 60,
                        "sample_interval_s": 5.0,
                        "battery_start": {
                            "voltage_mv": 4000.0,
                            "status": "discharging",
                            "powered": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            rows = [
                {
                    "index": index,
                    "elapsed_s": float(index * 5),
                    "uptime_s": 100.0 + index * 5,
                    "current_ma": 250.0,
                    "signed_current_ma": -250.0,
                    "voltage_mv": 4000.0,
                    "power_mw": 1000.0,
                    "direction": "discharging",
                    "cpu_pct": 10.0,
                    "power_source": "ios_power_telemetry_system_load",
                    "power_sample_age_s": 31.0 + index,
                    "power_valid_for_consumption": True,
                    "external_power": False,
                }
                for index in range(2)
            ]
            (root / "raw" / "sampler-stream.txt").write_text(
                "\n".join(f"N|{json.dumps(row)}" for row in rows) + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            self.assertFalse(snapshot["summary"]["power_valid_for_consumption"])
            self.assertIsNone(snapshot["summary"]["average_power_mw"])
            self.assertIn("超过 30 秒未刷新", snapshot["summary"]["power_consumption_unavailable_reason"])
            self.assertTrue(
                all(not point["power_valid_for_consumption"] for point in snapshot["series"])
            )
            self.assertTrue(all(point["power_sample_stale"] for point in snapshot["series"]))

    def test_reader_exposes_ios_live_capability_boundaries_without_inventing_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "platform": "ios",
                        "test_mode": "performance",
                        "requested_duration_s": 60,
                        "sample_interval_s": 1.0,
                        "battery_start": {"status": "external_power", "powered": ["USB"]},
                        "ios_collection_stats": {
                            "application_state_notifications_observed": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "raw" / "sampler-stream.txt").write_text("", encoding="utf-8")

            snapshot = LiveTelemetryReader(root).snapshot()

            performance = snapshot["performance"]
            self.assertIn("不提供通用应用 FPS", performance["frame_unavailable_reason"])
            self.assertIn("标准 1% Low", performance["frame_unavailable_reason"])
            self.assertIn("逐帧 Core Animation 时间戳", performance["frame_unavailable_reason"])
            self.assertIn("没有可验证的屏幕刷新率", performance["refresh_rate_unavailable_reason"])
            self.assertEqual(
                performance["foreground_state_status"],
                "unknown",
            )
            self.assertFalse(performance["foreground_state_available"])
            self.assertTrue(performance["application_state_change_observed"])
            self.assertIn("不能确认当前前台", performance["foreground_state_reason"])
            self.assertIsNone(snapshot["context"])

    def test_reader_distinguishes_ios_running_and_suspended_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            metadata = {
                "platform": "ios",
                "test_mode": "performance",
                "requested_duration_s": 60,
                "sample_interval_s": 1.0,
                "battery_start": {"status": "external_power", "powered": ["USB"]},
                "ios_collection_stats": {
                    "application_state_notifications_observed": True,
                },
            }
            (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            (root / "raw" / "sampler-stream.txt").write_text("", encoding="utf-8")
            context_path = root / "contexts.jsonl"
            context_path.write_text(
                json.dumps(
                    {
                        "uptime_s": 100.0,
                        "foreground_package": "com.example.game",
                        "foreground_activity": "Running",
                        "source": "ios_dvt_notifications",
                        "performance": {"platform": "ios"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            running = LiveTelemetryReader(root).snapshot()["performance"]
            self.assertEqual(running["foreground_state_status"], "observed")
            self.assertTrue(running["foreground_state_available"])
            self.assertTrue(running["application_state_change_observed"])

            context_path.write_text(
                json.dumps(
                    {
                        "uptime_s": 101.0,
                        "foreground_package": None,
                        "foreground_activity": "Suspended",
                        "source": "ios_dvt_notifications",
                        "performance": {"platform": "ios"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            suspended = LiveTelemetryReader(root).snapshot()["performance"]
            self.assertEqual(suspended["foreground_state_status"], "unknown")
            self.assertFalse(suspended["foreground_state_available"])
            self.assertTrue(suspended["application_state_change_observed"])
            self.assertIn("Suspended", suspended["foreground_state_reason"])

    def test_reader_does_not_publish_zero_consumption_for_charging_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "requested_duration_s": 60,
                        "sample_interval_s": 1.0,
                        "battery_start": {
                            "voltage_mv": 4000.0,
                            "status": "charging",
                            "powered": ["USB"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            rows = [
                {
                    "index": index,
                    "elapsed_s": float(index),
                    "uptime_s": 100.0 + index,
                    "current_ma": 500.0,
                    "signed_current_ma": 500.0,
                    "voltage_mv": 4000.0,
                    "power_mw": 5000.0,
                    "direction": "charging",
                    "cpu_pct": 20.0,
                    "power_source": "ios_power_telemetry_system_load",
                    "power_valid_for_consumption": False,
                    "external_power": True,
                }
                for index in range(2)
            ]
            (root / "raw" / "sampler-stream.txt").write_text(
                "\n".join(f"N|{json.dumps(row)}" for row in rows) + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            summary = snapshot["summary"]
            self.assertFalse(summary["power_valid_for_consumption"])
            self.assertEqual(summary["direction"], "charging")
            self.assertIsNone(summary["average_power_mw"])
            self.assertIsNone(summary["average_current_ma"])
            self.assertIsNone(summary["energy_mwh"])
            self.assertEqual(summary["observed_power_average_mw"], 5000.0)
            self.assertEqual(summary["battery_flow_average_power_mw"], 2000.0)
            self.assertIsNone(
                snapshot["render_performance"]["power_recording"]["average_power_mw"]
            )

    def test_reader_does_not_backfill_unknown_new_format_power_state_from_session_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "schema_version": 9,
                        "sample_interval_s": 1.0,
                        "current_unit": "auto",
                        "battery_start": {
                            "voltage_mv": 4000.0,
                            "status": "discharging",
                            "powered": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "raw" / "sampler-stream.txt").write_text(
                "S|0|100.0|-250000|4000|300|-1|3|100|0|50|850|0|0|0|0\n"
                "S|1|101.0|-250000|4000|300|-1|3|120|0|60|920|0|0|0|0\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            self.assertFalse(snapshot["summary"]["power_valid_for_consumption"])
            self.assertIsNone(snapshot["summary"]["average_power_mw"])
            self.assertIsNone(snapshot["summary"]["energy_mwh"])
            self.assertIsNone(snapshot["series"][0]["external_power"])
            self.assertFalse(snapshot["series"][0]["power_valid_for_consumption"])

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


class PlatformTelemetrySemanticsTests(unittest.TestCase):
    def test_live_percentile_matches_final_nearest_rank_analysis(self) -> None:
        self.assertEqual(
            _percentile([100, 200, 300, 400, 500, 600, 700, 1800], 0.95),
            1800,
        )

    def test_android_sampler_drops_missed_deadlines_instead_of_catch_up_bursts(self) -> None:
        script = build_sampler_script(5, 1.0, [], None)

        self.assertIn("due = sample + step", script)
        self.assertIn("due = now + step; delay = step", script)
        self.assertNotIn("due = now; delay = 0", script)

    def test_root_only_gpu_uses_one_read_only_sampler_su_session(self) -> None:
        source = GpuSource(
            name="Adreno",
            frequency_path="/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq",
            load_path="/sys/class/kgsl/kgsl-3d0/gpubusy",
            load_format="busy_total",
            requires_root=True,
        )
        script = build_sampler_script(
            5,
            1.0,
            [],
            source,
            session_has_root=True,
        )
        remote_command = _sampler_shell_command(script, True)

        self.assertNotIn("su -c", script)
        self.assertIn("cat /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq", script)
        self.assertIn("cat /sys/class/kgsl/kgsl-3d0/gpubusy", script)
        self.assertTrue(remote_command.startswith("su -c "))
        self.assertEqual(remote_command.count("su -c"), 1)

    def test_android_sampler_parser_tracks_mid_session_external_power(self) -> None:
        unplugged = parse_sampler_line(
            "S|0|100.0|-250000|4000|300|0|3|100|0|50|850|0|0|0|0",
            [],
            None,
        )
        plugged = parse_sampler_line(
            "S|1|101.0|-250000|4000|300|1|4|120|0|60|920|0|0|0|0",
            [],
            None,
        )
        unknown = parse_sampler_line(
            "S|2|102.0|-250000|4000|300|-1|-1|140|0|70|990|0|0|0|0",
            [],
            None,
        )

        self.assertIsInstance(unplugged, RawSample)
        self.assertIsInstance(plugged, RawSample)
        self.assertIsInstance(unknown, RawSample)
        assert isinstance(unplugged, RawSample)
        assert isinstance(plugged, RawSample)
        assert isinstance(unknown, RawSample)
        self.assertFalse(unplugged.external_power)
        self.assertEqual(unplugged.battery_status, "discharging")
        self.assertTrue(plugged.external_power)
        self.assertEqual(plugged.battery_status, "not_charging")
        self.assertIsNone(unknown.external_power)
        self.assertIsNone(unknown.battery_status)
        self.assertIn("else print -1", build_sampler_script(5, 1.0, [], None))

    def test_harmony_native_and_smartperf_reject_external_negative_current(self) -> None:
        native, _ = build_harmony_sample(
            0,
            100.0,
            "pluggedType: 2\nchargingStatus: 0\nvoltage: 4000000\n"
            "nowCurrent: -100\ntemperature: 280\n",
            "cpu 100 0 50 850 0 0 0 0\n",
            None,
            [],
            {},
        )
        smartperf = build_harmony_smartperf_sample(
            {
                "timestamp": "1783934231000",
                "currentNow": "-100000",
                "voltageNow": "4000000",
            },
            0,
            [],
            external_power=True,
        )

        self.assertIsNotNone(native)
        self.assertIsNotNone(smartperf)
        assert native is not None
        assert smartperf is not None
        for sample in (native, smartperf):
            self.assertTrue(sample.external_power)
            self.assertFalse(sample.power_valid_for_consumption)
            self.assertEqual(sample.direction, "external_power")

        unknown_native, _ = build_harmony_sample(
            1,
            101.0,
            "chargingStatus: 0\nvoltage: 4000000\nnowCurrent: -100\n",
            "cpu 120 0 60 920 0 0 0 0\n",
            None,
            [],
            {},
        )
        unknown_smartperf = build_harmony_smartperf_sample(
            {
                "timestamp": "1783934232000",
                "currentNow": "-100000",
                "voltageNow": "4000000",
            },
            1,
            [],
            external_power=None,
        )
        self.assertIsNotNone(unknown_native)
        self.assertIsNotNone(unknown_smartperf)
        assert unknown_native is not None
        assert unknown_smartperf is not None
        for sample in (unknown_native, unknown_smartperf):
            self.assertIsNone(sample.external_power)
            self.assertFalse(sample.power_valid_for_consumption)
            self.assertEqual(sample.direction, "discharging")

    def test_smartperf_requests_full_sample_span_and_ignores_terminal_summary(self) -> None:
        command = _smartperf_command(7, "com.example.game", {"cpu_usage": True})
        self.assertIn("-N 7", command)
        unlimited_batch = _smartperf_command(
            SMARTPERF_UNLIMITED_BATCH_SAMPLE_COUNT,
            "com.example.game",
            {"cpu_usage": True},
        )
        self.assertEqual(SMARTPERF_UNLIMITED_BATCH_SAMPLE_COUNT, 3601)
        self.assertIn("-N 3601", unlimited_batch)

        parser = HarmonySmartPerfParser()
        self.assertIsNone(parser.feed_line("order:0 Battery=28"))
        self.assertIsNone(parser.feed_line("order:1 timestamp=1784200000000"))
        self.assertIsNone(parser.feed_line("order:2 currentNow=-100"))
        self.assertIsNone(parser.feed_line("order:3 voltageNow=4400000"))
        complete = parser.feed_line("order:0 Battery=28")
        self.assertIsNotNone(complete)
        self.assertEqual(complete["timestamp"], "1784200000000")
        self.assertIsNone(parser.feed_line("order:1 summary=done"))
        self.assertIsNone(parser.feed_line("command exec finished"))
        self.assertIsNone(parser.finish())

    def test_ios_normalized_parser_preserves_explicit_power_validity(self) -> None:
        parsed = parse_sampler_line(
            'N|{"index":0,"uptime_s":100.0,"current_ma":200.0,'
            '"signed_current_ma":-200.0,"voltage_mv":4000.0,"power_mw":1500.0,'
            '"direction":"discharging","power_source":"ios_power_telemetry_system_load",'
            '"power_valid_for_consumption":false,"external_power":true}',
            [],
            None,
        )

        self.assertIsInstance(parsed, Sample)
        assert isinstance(parsed, Sample)
        self.assertFalse(parsed.power_valid_for_consumption)
        self.assertTrue(parsed.external_power)
        self.assertEqual(parsed.power_source, "ios_power_telemetry_system_load")

    def test_ios_missing_external_connected_state_is_not_treated_as_unplugged(self) -> None:
        unknown = _battery_payload(
            {
                "InstantAmperage": -200,
                "Voltage": 4000,
                "PowerTelemetryData": {"SystemLoad": 1500},
            }
        )
        unplugged = _battery_payload(
            {
                "ExternalConnected": False,
                "InstantAmperage": -200,
                "Voltage": 4000,
            }
        )

        self.assertFalse(unknown["external_power_state_available"])
        self.assertIsNone(unknown["external_connected"])
        self.assertEqual(unknown["powered"], [])
        self.assertTrue(unplugged["external_power_state_available"])
        self.assertFalse(unplugged["external_connected"])

    def test_vendor_brightness_table_does_not_activate_tracking_without_runtime_flag(self) -> None:
        inactive = ThermalSnapshot(
            uptime_s=1.0,
            host_epoch_s=1.0,
            display_brightness={
                "vendor_thermal_active": False,
                "vendor_thermal_level": 0,
                "vendor_thermal_limit_nits": 9999.0,
                "vendor_thermal_nit_table": [700, 600, 500],
            },
        )
        active = ThermalSnapshot(
            uptime_s=2.0,
            host_epoch_s=2.0,
            display_brightness={
                "vendor_thermal_active": True,
                "vendor_thermal_level": 4,
                "vendor_thermal_limit_nits": 450.0,
            },
        )

        self.assertFalse(_brightness_tracking_required(inactive))
        self.assertTrue(_brightness_tracking_required(active))


class UiServerTests(unittest.TestCase):
    def test_response_write_ignores_normal_client_disconnects(self) -> None:
        for error in (
            BrokenPipeError(),
            ConnectionAbortedError(),
            ConnectionResetError(),
        ):
            with self.subTest(error=type(error).__name__):
                handler = DashboardHandler.__new__(DashboardHandler)
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = Mock()
                handler.wfile.write.side_effect = error

                handler._send_bytes(b"payload", "text/plain")

                handler.wfile.write.assert_called_once_with(b"payload")

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
                                "wireless_ready": "true",
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

    def test_usb_iphone_can_be_probed_but_cannot_start_without_remote_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            usb = {
                "serial": "ios:IPHONE",
                "udid": "IPHONE",
                "state": "device",
                "platform": "ios",
                "connection_type": "usb",
                "transports": "usb",
                "wireless_ready": "false",
            }
            completed = Mock(
                returncode=0,
                stdout=json.dumps({"device": {"model": "iPhone 16"}}),
                stderr="",
            )
            with (
                patch.object(manager, "devices", return_value=([usb], None)),
                patch("mobile_profiler.ui.subprocess.run", return_value=completed) as run,
            ):
                probe = manager.probe({"device": "ios:IPHONE", "platform": "ios"})

            self.assertEqual(probe["data"]["device"]["model"], "iPhone 16")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--platform") + 1], "ios")
            self.assertEqual(command[command.index("--device") + 1], "ios:IPHONE")

            with (
                patch.object(manager, "devices", return_value=([usb], None)),
                patch("mobile_profiler.ui.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ValueError, "RemotePairing"):
                    manager.start_record(
                        {
                            "device": "ios:IPHONE",
                            "platform": "ios",
                            "test_mode": "performance",
                            "run_name": "iOS without Wi-Fi",
                        }
                    )
            popen.assert_not_called()

    def test_ios_link_local_remote_xpc_requires_external_power_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            link_local = {
                "serial": "ios:IPHONE",
                "udid": "IPHONE",
                "state": "device",
                "platform": "ios",
                "connection_type": "usb",
                "transports": "usb,remote-xpc",
                "remote_xpc_ready": "true",
                "wireless_ready": "false",
                "unplug_ready": "false",
                "endpoint_scope": "link-local",
                "host": "169.254.47.225",
                "port": "49152",
            }
            with (
                patch.object(manager, "devices", return_value=([link_local], None)),
                patch("mobile_profiler.ui.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ValueError, "USB-NCM"):
                    manager.start_record(
                        {
                            "device": "ios:IPHONE",
                            "platform": "ios",
                            "test_mode": "performance",
                            "require_unplugged": True,
                            "run_name": "iOS link local blocked",
                        }
                    )
            popen.assert_not_called()

            fake_active = Mock()
            fake_active.running = True
            fake_active.snapshot.return_value = {"running": True, "status": "starting"}
            with (
                patch.object(manager, "devices", return_value=([link_local], None)),
                patch("mobile_profiler.ui.subprocess.Popen", return_value=object()) as popen,
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active),
            ):
                manager.start_record(
                    {
                        "device": "ios:IPHONE",
                        "platform": "ios",
                        "test_mode": "performance",
                        "require_unplugged": False,
                        "run_name": "iOS link local external power",
                    }
                )
            self.assertIn("--allow-external-power", popen.call_args.args[0])

    def test_ios_pair_accepts_remote_xpc_but_does_not_claim_unplug_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            usb = {
                "serial": "ios:IPHONE",
                "udid": "IPHONE",
                "state": "device",
                "platform": "ios",
                "connection_type": "usb",
            }
            paired = {
                **usb,
                "transports": "usb,remote-xpc",
                "remote_xpc_ready": "true",
                "wireless_ready": "false",
                "unplug_ready": "false",
                "endpoint_scope": "link-local",
            }
            with (
                patch.object(
                    manager,
                    "devices",
                    side_effect=[([usb], None), ([paired], None)],
                ),
                patch(
                    "mobile_profiler.ui.pair_ios_device",
                    return_value={
                        "serial": "ios:IPHONE",
                        "connected": True,
                        "endpoint": {
                            "host": "169.254.47.225",
                            "port": 49152,
                            "scope": "link-local",
                            "remote_xpc_ready": True,
                            "unplug_ready": False,
                        },
                    },
                ),
            ):
                result = manager.pair_ios({"device": "ios:IPHONE"})

            self.assertEqual(result["device"]["remote_xpc_ready"], "true")
            self.assertEqual(result["device"]["unplug_ready"], "false")

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

    def test_ios_bluetooth_connect_refreshes_the_wireless_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            wireless = {
                "serial": "ios:IPHONE",
                "udid": "IPHONE",
                "state": "device",
                "platform": "ios",
                "connection_type": "wireless",
                "wireless_transport": "bluetooth-pan",
                "remote_xpc_ready": "true",
                "wireless_ready": "true",
                "unplug_ready": "true",
                "host": "172.20.10.1",
                "port": "49152",
            }
            with (
                patch.object(manager, "devices", return_value=([wireless], None)),
                patch(
                    "mobile_profiler.ui.connect_ios_bluetooth_device",
                    return_value={
                        "serial": "ios:IPHONE",
                        "connected": True,
                        "address": "172.20.10.3",
                        "endpoint": {"host": "172.20.10.1", "port": 49152},
                    },
                ) as connect,
            ):
                result = manager.connect_ios_bluetooth({"device": "ios:IPHONE"})

            connect.assert_called_once_with("ios:IPHONE", "ios-python", 30.0)
            self.assertEqual(result["device"]["connection_type"], "wireless")
            self.assertEqual(result["device"]["wireless_transport"], "bluetooth-pan")
            self.assertEqual(result["device"]["unplug_ready"], "true")
            self.assertEqual(result["devices"], [wireless])

    def test_ios_wireless_transport_is_kept_in_active_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            wireless = {
                "serial": "ios:IPHONE",
                "udid": "IPHONE",
                "state": "device",
                "platform": "ios",
                "connection_type": "wireless",
                "wireless_transport": "wifi",
                "transport_adapter": "Wi-Fi adapter",
                "remote_xpc_ready": "true",
                "wireless_ready": "true",
                "unplug_ready": "true",
                "host": "192.0.2.10",
                "port": "49152",
            }
            fake_active = Mock()
            fake_active.running = True
            fake_active.snapshot.return_value = {"running": True, "status": "starting"}
            with (
                patch.object(manager, "devices", return_value=([wireless], None)),
                patch("mobile_profiler.ui.subprocess.Popen", return_value=object()),
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active) as active_run,
            ):
                manager.start_record(
                    {
                        "device": "ios:IPHONE",
                        "platform": "ios",
                        "run_name": "iOS Wi-Fi transport",
                    }
                )

            config = active_run.call_args.args[2]
            self.assertEqual(config["wireless_transport"], "wifi")
            self.assertEqual(config["transport_adapter"], "Wi-Fi adapter")

    def test_ios_bluetooth_connect_rejects_a_non_ios_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            with patch("mobile_profiler.ui.connect_ios_bluetooth_device") as connect:
                with self.assertRaisesRegex(ValueError, "Select an iPhone"):
                    manager.connect_ios_bluetooth({"device": "ANDROID"})
            connect.assert_not_called()

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
                with urlopen(base + "/app.css?v=platform-ui-42", timeout=5) as response:
                    css = response.read().decode("utf-8")
                with urlopen(base + "/app.js?v=platform-ui-42", timeout=5) as response:
                    javascript = response.read().decode("utf-8").replace("\r\n", "\n")
                with urlopen(base + "/api/state", timeout=5) as response:
                    state = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                manager.close()
                thread.join(timeout=5)

            self.assertIn("Mobile Profiler", html)
            self.assertIn("v0.7.2", html)
            self.assertIn('class="app-version-badge"', html)
            self.assertEqual(state["version"], __version__)
            self.assertIn("TEST PLATFORM", html)
            self.assertIn("ADB / gfxinfo", html)
            self.assertIn("DVT / RemoteXPC", html)
            self.assertIn("HDC / SmartPerf", html)
            self.assertIn("ADB IP", html)
            self.assertIn("无线 ADB", html)
            self.assertIn("鸿蒙无线", html)
            self.assertIn('id="pair-ios"', html)
            self.assertIn('id="connect-ios-bluetooth"', html)
            self.assertIn("1. 创建 RemotePairing", html)
            self.assertIn("2. 连接蓝牙热点", html)
            self.assertNotIn('id="ios-pair-hint"', html)
            self.assertIn("grid-template-columns: repeat(2, max-content)", css)
            self.assertIn("断开无线", html)
            self.assertNotIn('data-view="system"', html)
            self.assertNotIn('data-panel="system"', html)
            self.assertIn("功耗测试模式", html)
            self.assertIn("FPS / 1% Low / 渲染链路", html)
            self.assertIn("开始任务", html)
            self.assertIn("设备与场景条件", html)
            self.assertIn("更多采集设置", html)
            self.assertIn("设备亮度", html)
            self.assertIn('id="brightness-input"', html)
            self.assertIn('/app.css?v=platform-ui-42', html)
            self.assertIn('/app.js?v=platform-ui-42', html)
            self.assertNotIn("platform-ui-40", html)
            self.assertIn("默认 1 秒读取电流、CPU 与频率", html)
            self.assertIn("当前电池放电功率", html)
            self.assertIn("当前功率通道", html)
            self.assertIn("测试窗口平均电池侧功率", html)
            self.assertIn("与顶部当前值同源 · 有效区间平均", html)
            self.assertNotIn("<dt>整机功耗记录</dt>", html)
            self.assertIn("屏幕热降亮监控", html)
            self.assertNotIn('<details class="advanced-settings" open', html)
            self.assertIn(".capture-feature-card input", css)
            self.assertIn("pointer-events: auto", css)
            self.assertIn("captureFeaturesOverridden", javascript)
            self.assertIn('powerLabel: "当前整机 SystemLoad 功率"', javascript)
            self.assertNotIn("以物理整机功耗", javascript)
            self.assertIn("以整机原始 SystemLoad、电池流量", javascript)
            self.assertIn('"当前电池放电功率"', javascript)
            self.assertIn('["有效放电区间 SystemLoad 均值"', javascript)
            self.assertIn('["原始 SystemLoad 均值"', javascript)
            self.assertIn("powerSources.includes(iosSystemLoadPowerSource)", javascript)
            self.assertIn("RenderService 背光原始值 ${brightness}（非 nit、非热限亮", javascript)
            self.assertIn("背光原始值 ${brightness}（非 nit、非热限亮", javascript)
            self.assertIn('harmony ? "温度传感器" : "温度 / 热限制"', javascript)
            self.assertIn("不含公开热严重度、限亮档位或热降亮上限", javascript)
            self.assertIn(
                'function frameTimingTimelineLane(active) {\n    if (!captureFeatureEnabled(active, "frame_rate"))',
                javascript,
            )
            self.assertIn(
                'function frameIssueTimelineLane(active) {\n    if (!captureFeatureEnabled(active, "frame_rate"))',
                javascript,
            )
            self.assertIn("逐帧呈现间隔；详细 framestats 开启时补充阶段数据", javascript)
            self.assertIn("温度采样周期（固定）", javascript)
            self.assertIn("SmartPerf 温度来自同一 SP_daemon 主流，固定约每 1 秒一条", javascript)
            self.assertNotIn("powerSources.every(source => source ===", javascript)
            self.assertIn(
                "row?.power_source === iosSystemLoadPowerSource ? definition.value(row) : null",
                javascript,
            )
            self.assertIn("system_load_observed_average_power_mw", javascript)
            self.assertIn("外供时可能接近 SystemPowerIn", javascript)
            render_context = javascript[
                javascript.index("function renderContext(active)") :
                javascript.index(
                    "/* Legacy performance-context",
                    javascript.index("function renderContext(active)"),
                )
            ]
            self.assertIn(
                'const processSnapshotsEnabled = captureFeatureEnabled(active, "process_snapshots");',
                render_context,
            )
            self.assertLess(
                render_context.index("const processSnapshotsEnabled"),
                render_context.index('processSnapshotsEnabled ? "等待 DVT 进程快照"'),
            )
            self.assertIn(
                'powerFlow.systemLoad ? "SystemLoad 当前样本" : "电池流量 I×V"',
                javascript,
            )
            self.assertIn(
                "当前未收到 SystemLoad；只展示电池端流量",
                javascript,
            )
            self.assertIn(
                'classList.toggle("hidden", !powerFlow.systemLoad)',
                javascript,
            )
            self.assertIn("const gpuObserved = finite(latest.gpu_load_pct)", javascript)
            self.assertIn('gpuObserved\n        ? "iOS DVT sysmond / GPU counters"', javascript)
            self.assertIn("本次只收到 CPU / 进程诊断遥测；未收到 GPU 数据", javascript)
            self.assertIn(
                "entry.data?.battery?.external_power_state_available === true",
                javascript,
            )
            mode_defaults_start = javascript.index("const modeDefaults = {")
            mode_defaults_end = javascript.index("const captureFeatureNames", mode_defaults_start)
            mode_defaults = javascript[mode_defaults_start:mode_defaults_end]
            self.assertEqual(mode_defaults.count('"interval-input": 1'), 2)
            self.assertNotIn('"interval-input": .5', mode_defaults)
            self.assertIn("原始时间序列", html)
            self.assertIn("手机采集内容", html)
            self.assertIn("默认只开启有原始曲线或有效结论的项目", html)
            self.assertIn(
                "Harmony SmartPerf（设备原生约 1 秒，FPS / GPU 增强）",
                html,
            )
            self.assertNotIn("Harmony SmartPerf（约 1 秒，高开销）", html)
            self.assertIn("CPU 核心组频率约 30 秒刷新", javascript)
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
            self.assertIn(
                'const smartPerf = platform === "harmony" && effectiveCapturePreset() === "harmony-smartperf"',
                javascript,
            )
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
            self.assertNotIn('"frame_details"', performance_preset)
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
            self.assertIn('id="live-range-panel"', html)
            self.assertIn('id="live-range-status"', html)
            self.assertIn('id="live-range-clear"', html)
            self.assertIn('id="live-range-statistics"', html)
            self.assertIn("在任意图表中按住并横向拖动", html)
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
            self.assertIn("function liveTimelineDefinitionSupported", javascript)
            self.assertIn("本次没有有效数据", javascript)
            self.assertIn("熄屏保留配置，非当前输出", javascript)
            self.assertIn(
                "capture_features: app.captureFeaturesOverridden ? captureFeatures : {}",
                javascript,
            )
            self.assertIn("当前 iOS 后端不提供通用应用帧链路", javascript)
            self.assertIn("已超过 ${iosSystemLoadStaleAfterS} 秒未刷新", javascript)
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
            self.assertIn(".live-range-panel", css)
            self.assertIn(".live-range-statistics", css)
            self.assertIn("#live-chart .timeline-range-window", css)
            self.assertIn("#live-chart .timeline-range-boundary", css)
            self.assertIn(".live-timeline-config-row", css)
            self.assertIn(".live-timeline-config-toggle", css)
            self.assertIn(".live-timeline-order-button", css)
            self.assertIn(".live-timeline-config-copy small { white-space: normal; }", css)
            self.assertIn("one_percent_low_fps", javascript)
            self.assertIn("function normalizeLiveTimeRange", javascript)
            self.assertIn("function liveSeriesRangeStatistics", javascript)
            self.assertIn("function exactLiveRangeSummary", javascript)
            self.assertIn('api("/api/range-summary"', javascript)
            self.assertIn('full_resolution', javascript)
            self.assertIn("完整数据时间加权均值", javascript)
            self.assertIn('class: "timeline-range-window"', javascript)
            self.assertIn('class: "timeline-range-boundary"', javascript)
            self.assertIn('chartWrap.addEventListener("pointerdown"', javascript)
            self.assertIn('chartWrap.addEventListener("pointermove"', javascript)
            self.assertIn('chartWrap.addEventListener("pointerup"', javascript)
            self.assertIn("chartWrap.setPointerCapture", javascript)
            self.assertIn("chartWrap.releasePointerCapture", javascript)
            self.assertIn("geometry.lanes.map", javascript)
            self.assertIn("完整采集数据统计", javascript)
            self.assertIn("step: true", javascript)
            self.assertIn("SurfaceFlinger · 前台应用渲染层 GraphicBuffer（启动时快照）", javascript)
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
            html_ids = re.findall(r'\bid="([A-Za-z0-9_-]+)"', html)
            duplicate_ids = sorted(
                name for name, count in Counter(html_ids).items() if count > 1
            )
            executable_javascript = re.sub(
                r"/\*.*?\*/",
                "",
                javascript,
                flags=re.S,
            )
            executable_javascript = re.sub(
                r"(?m)^\s*//.*$",
                "",
                executable_javascript,
            )
            static_id_refs = set(
                re.findall(
                    r'\$\(\s*["\']#([A-Za-z0-9_-]+)["\']\s*\)',
                    executable_javascript,
                )
            )
            static_id_refs.update(
                re.findall(
                    r'getElementById\(\s*["\']([A-Za-z0-9_-]+)["\']\s*\)',
                    executable_javascript,
                )
            )
            label_targets = set(
                re.findall(r'<label\b[^>]*\bfor="([A-Za-z0-9_-]+)"', html)
            )
            aria_targets = {
                target
                for values in re.findall(
                    r'\baria-(?:controls|labelledby|describedby)="([^"]+)"',
                    html,
                )
                for target in values.split()
            }
            self.assertEqual(duplicate_ids, [])
            self.assertEqual(sorted(static_id_refs - set(html_ids)), [])
            self.assertEqual(sorted(label_targets - set(html_ids)), [])
            self.assertEqual(sorted(aria_targets - set(html_ids)), [])
            self.assertNotIn("function renderSystem(active)", executable_javascript)
            self.assertNotIn("function renderSchedulerHistory(active)", executable_javascript)
            self.assertIn("function iosRemoteXpcReady", javascript)
            self.assertIn("function iosUnplugReady", javascript)
            self.assertIn('api("/api/ios/bluetooth"', javascript)
            self.assertIn('$("#connect-ios-bluetooth")', javascript)
            self.assertIn("169.254/16 可能只是 USB-NCM", javascript)
            self.assertIn("state.active && (state.active.running || activeIsNewRun)", javascript)
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
            self.assertIn('data-view="agent"', html)
            self.assertIn('data-panel="agent"', html)
            self.assertIn('id="adb-agent-form"', html)
            self.assertIn('id="agent-screen-image"', html)
            self.assertIn('id="agent-task-list"', html)
            self.assertIn('id="agent-task-template-select"', html)
            self.assertIn('id="agent-model-provider-input"', html)
            self.assertIn('id="agent-api-key-mode-input"', html)
            self.assertIn('id="agent-system-prompt-input"', html)
            self.assertIn('id="agent-task-results"', html)
            self.assertIn('api("/api/ai-agent/start"', javascript)
            self.assertIn('api("/api/ai-agent/stop"', javascript)
            self.assertIn("function renderAdbAgent", javascript)
            self.assertIn("function readAgentTasks", javascript)
            self.assertIn("workflow_name:", javascript)
            self.assertIn("model_provider:", javascript)
            self.assertIn("api_key_mode:", javascript)
            self.assertIn("function applyAgentProviderPresentation", javascript)
            self.assertIn("system_prompt: systemPrompt", javascript)
            self.assertIn("tasks,", javascript)
            self.assertIn("模型不能下发任意 shell", html)
            self.assertIn("局域网千问是默认配置而非协议绑定", html)
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
            self.assertNotIn("appPickerTarget.append(appPicker)", javascript)
            self.assertNotIn("target.prepend(modeBar)", javascript)
            self.assertIn('id="home-start-panel"', html)
            self.assertIn('id="home-duration-slot"', html)
            self.assertIn('id="home-package-slot"', html)
            self.assertIn('id="home-start-slot"', html)
            self.assertIn("durationSlot.append(durationField, durationPresets)", javascript)
            self.assertIn("startSlot.append(startButton)", javascript)
            self.assertIn("function placeTargetPackageField", javascript)
            self.assertIn('app.testMode === "performance"', javascript)
            self.assertIn(
                'const scannerOnHome = performance && selectedPlatform() === "android"',
                javascript,
            )
            self.assertIn("appPickerDestination.insertBefore(appPicker", javascript)
            self.assertIn('classList.toggle("scanner-on-home", scannerOnHome)', javascript)
            self.assertNotIn('id="home-open-apps"', html)
            self.assertNotIn('$("#home-open-apps")', javascript)
            self.assertIn('startButton.setAttribute("form", "record-form")', javascript)
            self.assertIn("function updateStartControlState", javascript)
            self.assertIn("const minimumDuration = Number(durationInput?.min || 2)", javascript)
            self.assertIn('.test-mode-switch [data-test-mode]', javascript)
            self.assertIn(".config-view-columns", css)
            self.assertIn("grid-template-columns: minmax(0, 1.35fr) minmax(360px, .85fr)", css)
            self.assertIn(".live-overview-grid", css)
            self.assertIn(".live-overview-stack", css)
            self.assertIn('class="live-overview-stack"', html)
            self.assertIn(".monitoring-stack > .live-timeline-panel", css)
            self.assertIn("grid-column: 1 / -1", css)
            self.assertIn(".config-app-panel.scanner-on-home", css)
            self.assertIn('body:not([data-platform="android"]) .config-app-placeholder', css)
            self.assertEqual(html.count('id="package-input"'), 1)
            self.assertEqual(html.count('id="duration-input"'), 1)
            self.assertEqual(html.count('id="start-record"'), 1)
            self.assertEqual(html.count('id="record-form"'), 1)
            self.assertEqual(html.count('id="scan-apps"'), 1)
            self.assertIn('id="duration-input" name="duration" form="record-form"', html)
            self.assertIn('id="package-input" name="package" form="record-form"', html)
            self.assertIn('id="start-record" type="submit" form="record-form"', html)
            self.assertIn('$("#package-input").required = packageRequired', javascript)
            self.assertIn("targetPackageRequired(payload.platform, payload.test_mode) && !payload.package", javascript)
            self.assertIn("#home-start-panel input", javascript)
            self.assertIn(".home-start-panel", css)
            self.assertIn('const legacyTools = view === "tools"', javascript)
            self.assertIn('const legacySystem = view === "system" || view === "thermal"', javascript)
            self.assertIn('const target = ["live", "config", "agent", "device", "history"].includes(requested)', javascript)
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
            manager.range_summary = Mock(
                return_value={
                    "run_name": "phone-a",
                    "start_elapsed_s": 1.25,
                    "end_elapsed_s": 3.75,
                    "full_resolution": True,
                    "metrics": {
                        "power_mw": {
                            "average": 1234.0,
                            "minimum": 1200.0,
                            "maximum": 1300.0,
                        }
                    },
                }
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
            manager.connect_ios_bluetooth = Mock(
                return_value={
                    "serial": "ios:IPHONE",
                    "connected": True,
                    "address": "172.20.10.3",
                    "endpoint": {"host": "172.20.10.1", "port": 49152},
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
                range_summary_payload = {
                    "run_name": "phone-a",
                    "start_elapsed_s": 1.25,
                    "end_elapsed_s": 3.75,
                }
                range_summary_request = Request(
                    base + "/api/range-summary",
                    data=json.dumps(range_summary_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(range_summary_request, timeout=5) as response:
                    range_summary_result = json.loads(response.read().decode("utf-8"))
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
                bluetooth_request = Request(
                    base + "/api/ios/bluetooth",
                    data=json.dumps({"device": "ios:IPHONE"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(bluetooth_request, timeout=5) as response:
                    bluetooth_result = json.loads(response.read().decode("utf-8"))
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
            self.assertTrue(range_summary_result["full_resolution"])
            self.assertEqual(
                range_summary_result["metrics"]["power_mw"]["average"],
                1234.0,
            )
            self.assertEqual(delete_range_result["deleted_sample_count"], 3)
            self.assertTrue(tcpip_result["tcpip_enabled"])
            self.assertTrue(disconnect_result["disconnected"])
            self.assertEqual(apps_result["apps"][0]["package"], "com.example.game")
            self.assertTrue(bluetooth_result["connected"])
            self.assertEqual(bluetooth_result["endpoint"]["host"], "172.20.10.1")
            self.assertIn("comparison", comparison_html)
            manager.regenerate_run.assert_called_once_with({"run_name": "phone-a"})
            manager.range_summary.assert_called_once_with(range_summary_payload)
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
                        "duration": 900,
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
            self.assertEqual(command[duration_index + 1], "900")
            self.assertTrue(any(Path(value).name == "UI-smoke" for value in command))
            self.assertEqual(result["status"], "starting")

    def test_start_record_launches_unlimited_cli_workflow(self) -> None:
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
                patch("mobile_profiler.ui.ActiveRun", return_value=fake_active) as active_run,
            ):
                result = manager.start_record(
                    {
                        "device": "SERIAL",
                        "platform": "android",
                        "duration": 1,
                        "duration_unlimited": True,
                        "run_name": "Unlimited smoke",
                    }
                )

            command = popen.call_args.args[0]
            config = active_run.call_args.args[2]
            self.assertIn("--unlimited", command)
            self.assertNotIn("--duration", command)
            self.assertEqual(config["duration"], 0)
            self.assertTrue(config["duration_unlimited"])
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

            self.assertEqual(
                active_run.call_args.args[1].resolve(),
                (root / "retry-run").resolve(),
            )
            self.assertTrue((root / "retry-run").is_dir())

    def test_ui_defaults_require_unplugged_in_both_test_modes(self) -> None:
        cases = (
            ("power", "", True, True),
            ("performance", "com.example.game", False, True),
        )
        for test_mode, package, expects_session_mode, expects_require_unplugged in cases:
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
                self.assertEqual(
                    "--require-unplugged" in command,
                    expects_require_unplugged,
                )

    def test_ui_explicitly_allows_external_power(self) -> None:
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
                manager.start_record(
                    {
                        "device": "SERIAL",
                        "platform": "android",
                        "run_name": "allow external power",
                        "require_unplugged": False,
                    }
                )

            command = popen.call_args.args[0]
            self.assertIn("--allow-external-power", command)
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

    def test_harmony_ui_rejects_durations_too_short_for_each_backend(self) -> None:
        cases = (
            ("auto", 7, ""),
            ("harmony-smartperf", 3, "com.example.game"),
        )
        for preset, duration, package in cases:
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as directory:
                manager = DashboardManager("custom-adb", Path(directory), hdc="custom-hdc")
                with (
                    patch("mobile_profiler.ui.list_adb_devices", return_value=([], None)),
                    patch(
                        "mobile_profiler.ui.list_harmony_devices",
                        return_value=([{
                            "serial": "harmony:PHONE",
                            "hdc_target": "PHONE",
                            "state": "device",
                            "platform": "harmony",
                        }], None),
                    ),
                    patch("mobile_profiler.ui.list_ios_devices", return_value=([], None)),
                    patch("mobile_profiler.ui.subprocess.Popen") as popen,
                ):
                    with self.assertRaisesRegex(ValueError, "duration"):
                        manager.start_record({
                            "device": "harmony:PHONE",
                            "platform": "harmony",
                            "test_mode": "performance",
                            "capture_preset": preset,
                            "duration": duration,
                            "package": package,
                            "run_name": f"short-{preset}",
                        })
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
            self.assertEqual(command[command.index("--thermal-interval") + 1], "1.0")
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
                                "wireless_ready": "true",
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

    def test_range_summary_uses_full_resolution_samples_for_time_weighted_statistics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(
                json.dumps({"sample_interval_s": 1.0}),
                encoding="utf-8",
            )
            samples = [
                Sample(
                    index=index,
                    elapsed_s=float(index),
                    uptime_s=100.0 + index,
                    current_ma=250.0,
                    signed_current_ma=-250.0,
                    voltage_mv=4000.0,
                    power_mw=1000.0 + 2.0 * index,
                    direction="discharging",
                    cpu_pct=20.0,
                    power_valid_for_consumption=True,
                    external_power=False,
                )
                for index in range(1501)
            ]
            self.assertEqual(len(_decimate(samples)), 900)
            manager = DashboardManager("adb", root)
            with (
                patch("mobile_profiler.ui.read_samples_csv", return_value=samples),
                patch("mobile_profiler.ui.load_contexts", return_value=[]),
            ):
                result = manager.range_summary(
                    {
                        "run_name": "phone-a",
                        "start_elapsed_s": 0.25,
                        "end_elapsed_s": 1499.75,
                    }
                )

        power = result["metrics"]["power_mw"]
        self.assertTrue(result["full_resolution"])
        self.assertEqual(result["sample_count"], 1499)
        self.assertAlmostEqual(result["duration_s"], 1499.5)
        self.assertEqual(power["calculation"], "time_weighted_full_resolution")
        self.assertEqual(power["sample_count"], 1499)
        self.assertAlmostEqual(power["covered_duration_s"], 1499.5)
        self.assertAlmostEqual(power["average"], 2500.0)
        self.assertAlmostEqual(power["minimum"], 1000.5)
        self.assertAlmostEqual(power["maximum"], 3999.5)

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

    def test_report_range_delete_allows_context_only_range_and_regenerates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text("{}", encoding="utf-8")
            manager = DashboardManager("adb", root)
            samples = [Mock(uptime_s=float(value)) for value in range(100, 103)]
            contexts = [
                ContextSample(
                    uptime_s=110.25,
                    foreground_package="com.example.game",
                    performance={"frame_rate_fps": 60.0},
                )
            ]
            with (
                patch("mobile_profiler.ui.read_samples_csv", return_value=samples),
                patch("mobile_profiler.ui.load_contexts", return_value=contexts),
                patch.object(manager, "_run_cli", return_value="ok") as run_cli,
            ):
                result = manager.delete_report_range(
                    {
                        "run_name": "phone-a",
                        "start_uptime_s": 110.0,
                        "end_uptime_s": 111.0,
                    }
                )
            edits = json.loads(
                (run_dir / "report-edits.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["deleted_sample_count"], 0)
        self.assertEqual(result["deleted_context_count"], 1)
        self.assertEqual(
            edits["excluded_ranges"],
            [{"start_uptime_s": 110.0, "end_uptime_s": 111.0}],
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
        self.assertEqual(Path(imported["log_path"]).resolve(), log_path.resolve())

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
            self.assertEqual(
                Path(result["zip_path"]).resolve(),
                Path(f"{output_dir}.zip").resolve(),
            )
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

    def test_cli_parser_exposes_unlimited_recording(self) -> None:
        args = build_parser().parse_args(
            ["record", "--platform", "ios", "--unlimited"]
        )

        self.assertTrue(args.unlimited)
        with patch("mobile_profiler.cli.run_ios_record", return_value=0) as run_ios:
            self.assertEqual(args.handler(args), 0)

        self.assertEqual(run_ios.call_args.args[0].duration, 0)

    def test_cli_defaults_to_requiring_unplugged_power(self) -> None:
        default = build_parser().parse_args(["record"])
        allowed = build_parser().parse_args(["record", "--allow-external-power"])

        self.assertTrue(default.require_unplugged)
        self.assertFalse(allowed.require_unplugged)

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
