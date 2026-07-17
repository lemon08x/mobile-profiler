from __future__ import annotations

import io
import asyncio
import json
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from mobile_profiler.analysis import (
    _analysis_data_sources,
    _filter_analysis_data_sources,
    analyze_android_frame_pipeline,
    analyze_brightness_throttling,
    build_findings,
    build_performance_findings,
    analyze_memory_frequency,
    analyze_performance_contexts,
    analyze_performance_test_items,
    analyze_run,
    analyze_cpu,
    analyze_gpu,
    analyze_scheduler_history,
    analyze_system_activity,
    analyze_test_items,
    analyze_thermal_history,
    convert_samples,
)
from mobile_profiler.collector import (
    AndroidPerformanceContextWorker,
    _android_performance_context_script,
    adb_connection_type,
    android_surface_frame_metrics,
    build_sampler_script,
    compact_android_performance_probe_for_metadata,
    collect_android_performance_context,
    collect_streaming_session,
    detect_gpu_source,
    gpu_load_from_text,
    is_gpu_core_devfreq,
    is_memory_devfreq,
    parse_android_display_performance,
    parse_android_frame_interpolation,
    parse_android_gfxinfo,
    parse_android_runtime_settings,
    parse_android_surface_render_resolution,
    parse_android_surface_latency,
    parse_android_surface_layers,
    parse_android_surfaceflinger_performance,
    parse_android_touch_devices,
    parse_android_window_performance,
    parse_sampler_line,
    parse_normalized_samples,
    probe_android_performance,
)
from mobile_profiler.cli import (
    _filter_report_events,
    _migrate_legacy_ios_connection_metadata,
    filter_events_by_metadata,
    finalize_run,
    print_run_summary,
    run_harmony_record,
)
from mobile_profiler.comparison import build_comparison_html, build_run_comparison
from mobile_profiler.evidence import create_evidence_archive
from mobile_profiler.features import (
    capture_features_from_metadata,
    resolve_capture_configuration,
)
from mobile_profiler.ios import (
    _classify_windows_route_adapter,
    _endpoint_reachable,
    _endpoint_scope,
    _ios_wireless_transport_details,
    _load_endpoints,
    _run_windows_bluetooth_pan,
    _windows_endpoint_route,
    collect_ios_session,
    connect_ios_bluetooth,
    list_ios_devices,
    pair_ios_device,
    probe_ios_device,
    select_ios_device,
)
from mobile_profiler import ios_bridge
from mobile_profiler.harmony import (
    HARMONY_NATIVE_CPU_FREQUENCY_INTERVAL_S,
    _sampler_script,
    build_harmony_smartperf_context,
    build_harmony_smartperf_sample,
    build_harmony_sample,
    harmony_cpu_policies,
    harmony_native_cpu_frequency_schedule_s,
    harmony_policy_frequencies_mhz,
    parse_harmony_battery,
    parse_harmony_cpufreq,
    parse_harmony_compositor_fps,
    parse_harmony_foreground,
    parse_harmony_gles,
    parse_harmony_hitches,
    parse_harmony_input_devices,
    parse_harmony_input_events,
    parse_harmony_refresh_counts,
    parse_harmony_render_screen,
    parse_harmony_thermal,
    parse_harmony_window_manager,
    parse_harmony_smartperf_output,
    parse_hdc_targets,
    read_harmony_power_mode,
    set_harmony_power_mode,
)
from mobile_profiler.log_import import (
    host_epoch_to_device_uptime,
    import_timestamped_log,
)
from mobile_profiler.models import (
    ClockSyncPoint,
    CommandResult,
    ContextSample,
    CpuPolicy,
    CpuTimes,
    ExternalEvent,
    GpuSource,
    MemorySource,
    RawSample,
    SCHEMA_VERSION,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
    IOS_SYSTEM_LOAD_STALE_AFTER_S,
    is_consumption_power_sample,
)
from mobile_profiler.parsers import (
    parse_activity_processes,
    parse_cpuset_policy_state,
    parse_display_brightness_state,
    parse_gpu_dump,
    parse_gpu_work,
    parse_performance_hint,
    parse_power_profile,
    parse_thermalservice,
    parse_top_processes,
    parse_top_threads,
)
from mobile_profiler.report import (
    _capture_configuration_rows,
    _frame_flow_history_section,
    _performance_context_rows,
    _report_mode_profile,
    _report_platform_profile,
    _report_warning_items,
    _report_bundle,
    build_report_fragment,
)
from mobile_profiler.messages import localize_collection_warning
from mobile_profiler.storage import (
    RunJournal,
    load_contexts,
    normalize_report_excluded_ranges,
    load_scheduler_snapshots,
    load_system_snapshots,
    load_thermal_snapshots,
    read_samples_csv,
    sample_to_dict,
    write_jsonl,
    write_report_excluded_ranges,
    write_samples_csv,
)


class ParserTests(unittest.TestCase):
    def test_collection_warning_localization_keeps_measurement_boundaries(self) -> None:
        ios = localize_collection_warning(
            "Average normalized iOS collector CPU overhead was 6.45% during this run."
        )
        harmony = localize_collection_warning(
            "hidumper --cpufreq is sampled at a lower cadence than /proc/stat because a full 12-core dump is "
            "comparatively expensive; the effective refresh cadence is about 30 seconds and intermediate "
            "samples retain the latest value."
        )

        self.assertIn("CPU 平均上界为 6.45%", ios)
        self.assertIn("不等于采集工具净开销", ios)
        self.assertIn("约每 30 秒刷新一次", harmony)
        self.assertIn("中间样本沿用最近值", harmony)

    def test_report_warning_filter_keeps_harmony_capability_boundary(self) -> None:
        rows = _report_warning_items(
            {
                "warnings": [
                    "HarmonyOS BatteryService reports whole-device battery current and voltage. Android "
                    "BatteryStats, ADPF and dumpsys GPU attribution are not available and are not inferred.",
                    "GPU 回退证据当前无效。",
                ],
                "gpu": {},
            },
            "power",
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("系统不提供 Android BatteryStats", rows[0])
        self.assertIn("报告不会推算这些数据", rows[0])

    def test_report_warning_filter_avoids_duplicate_ios_observer_copy(self) -> None:
        generic = (
            "iOS DVT sysmond/DTServiceHub/remotepairingdeviced add measurable collection overhead; "
            "collector_cpu_pct is retained in samples and profiler processes are tagged in system snapshots."
        )
        average = "Average normalized iOS collector CPU overhead was 6.45% during this run."

        performance_rows = _report_warning_items(
            {
                "warnings": [generic, average],
                "findings": [{"title": "观察者相关进程 CPU 上界"}],
                "gpu": {"load_available": True},
            },
            "performance",
        )
        power_rows = _report_warning_items(
            {"warnings": [generic, average], "findings": [], "gpu": {}},
            "power",
        )

        self.assertEqual(performance_rows, [])
        self.assertEqual(len(power_rows), 1)
        self.assertIn("CPU 平均上界为 6.45%", power_rows[0])

    def test_adb_connection_type_distinguishes_usb_wireless_and_emulator(self) -> None:
        self.assertEqual(adb_connection_type("dba55d4dd66d"), "usb")
        self.assertEqual(adb_connection_type("192.168.21.90:5555"), "wireless")
        self.assertEqual(adb_connection_type("[fe80::1234]:37123"), "wireless")
        self.assertEqual(
            adb_connection_type("adb-device._adb-tls-connect._tcp"),
            "wireless",
        )
        self.assertEqual(adb_connection_type("emulator-5554"), "emulator")

    def test_normalized_sidecar_samples_preserve_platform_fields_and_reconnect_order(self) -> None:
        rows = parse_normalized_samples(
            "\n".join(
                [
                    'N|{"index":8,"elapsed_s":99,"uptime_s":100.0,"current_ma":303,"signed_current_ma":-303,"voltage_mv":4373,"power_mw":1277,"direction":"discharging","cpu_pct":20,"gpu_load_pct":12.5,"power_source":"ios_power_telemetry_system_load","power_sample_age_s":4.5,"collector_cpu_pct":6.4}',
                    'N|{"index":0,"elapsed_s":0,"uptime_s":99.0,"current_ma":999,"signed_current_ma":-999,"voltage_mv":4000,"power_mw":3996,"direction":"discharging"}',
                    'N|{"index":0,"elapsed_s":0,"uptime_s":101.0,"current_ma":310,"signed_current_ma":-310,"voltage_mv":4360,"power_mw":1300,"direction":"discharging","cpu_pct":25,"power_source":"ios_power_telemetry_system_load","power_sample_age_s":5.5,"collector_cpu_pct":7.0}',
                ]
            ),
            [],
            None,
        )
        self.assertEqual([item.index for item in rows], [0, 1])
        self.assertEqual([item.elapsed_s for item in rows], [0.0, 1.0])
        self.assertEqual(rows[0].power_source, "ios_power_telemetry_system_load")
        self.assertEqual(rows[0].power_sample_age_s, 4.5)
        self.assertEqual(rows[0].collector_cpu_pct, 6.4)
        self.assertEqual(rows[0].gpu_load_pct, 12.5)

    def test_power_profile_multiline_array(self) -> None:
        profile = parse_power_profile(
            """
            battery.capacity=6200.0
            cpu.core_speeds.cluster0=[339000.0, 400000.0,
              500000.0, 600000.0]
            cpu.core_power.cluster0=[9.0, 12.0,
              14.0, 18.0]
            """
        )
        self.assertEqual(profile["battery.capacity"], 6200.0)
        self.assertEqual(profile["cpu.core_speeds.cluster0"], [339000.0, 400000.0, 500000.0, 600000.0])
        self.assertEqual(profile["cpu.core_power.cluster0"], [9.0, 12.0, 14.0, 18.0])

    def test_gpu_work_table(self) -> None:
        parsed = parse_gpu_work(
            "GPU work information.\n"
            "gpu_id uid total_active_duration_ns total_inactive_duration_ns\n"
            "0 10287 123000000 400000000\n"
        )
        self.assertEqual(parsed[10287]["active_ns"], 123000000)

    def test_qualcomm_gpu_dump_memory_and_driver(self) -> None:
        parsed = parse_gpu_dump(
            "Stable Game Driver: unsupported\n"
            "Pre-release Game Driver: com.qualcomm.qti.gpudrivers.sun.api35\n\n"
            "Memory snapshot for GPU 0:\n"
            "Global total: 764825600\n"
            "Proc 2035 total: 408252416\n"
            "Proc 4507 total: 130965504\n"
        )
        self.assertTrue(parsed["memory_available"])
        self.assertEqual(parsed["global_total_bytes"], 764825600)
        self.assertEqual(parsed["process_memory"][0]["pid"], 2035)
        self.assertIn("qualcomm", parsed["prerelease_game_driver"])

    def test_extended_policy_parser_reads_walt_core_ctl(self) -> None:
        parsed = parse_cpuset_policy_state(
            "POLICY|policy0|walt|384000|3532800|384000|3532800|ok|0 1 2 3 4 5|1|4|6\n"
        )
        policy = parsed["cpu_policies"][0]
        self.assertEqual(policy["governor"], "walt")
        self.assertEqual(policy["related_cpus"], "0 1 2 3 4 5")
        self.assertTrue(policy["core_ctl_enabled"])
        self.assertEqual(policy["core_ctl_min_cpus"], 4)

    def test_qualcomm_gpu_load_and_devfreq_classification(self) -> None:
        self.assertAlmostEqual(gpu_load_from_text("25 100", "busy_total"), 25.0)
        self.assertAlmostEqual(gpu_load_from_text("37%", "percentage"), 37.0)
        self.assertTrue(is_gpu_core_devfreq("3d00000.qcom,kgsl-3d0"))
        self.assertFalse(is_gpu_core_devfreq("soc:qcom,gpubw kgsl-busmon"))

    def test_memory_devfreq_classification_excludes_bandwidth_and_memlat_nodes(self) -> None:
        self.assertTrue(is_memory_devfreq("/sys/class/devfreq/1d84000.ufshc-dmc dram"))
        self.assertTrue(is_memory_devfreq("soc:mif memory-controller"))
        self.assertFalse(is_memory_devfreq("soc:qcom,memlat-cpu0"))
        self.assertFalse(is_memory_devfreq("soc:qcom,gpubw bw_hwmon"))

    def test_sampler_memory_column_and_runtime_settings_parser(self) -> None:
        memory = MemorySource(
            name="dmc",
            frequency_path="/sys/class/devfreq/dmc/cur_freq",
        )
        parsed = parse_sampler_line(
            "S|0|100.0|-300000|3800|300|100|0|50|800|0|0|0|0|933000000",
            [],
            None,
            memory,
        )
        self.assertIsInstance(parsed, RawSample)
        assert isinstance(parsed, RawSample)
        self.assertEqual(parsed.memory_frequency_raw, 933000000.0)
        script = build_sampler_script(5, 1.0, [], None, memory)
        self.assertIn("/sys/class/devfreq/dmc/cur_freq", script)

        settings = parse_android_runtime_settings(
            "SETTING|system|screen_brightness|180\n"
            "SETTING|system|peak_refresh_rate|120.0\n"
            "SETTING|global|low_power|0\n"
            "SETTING|secure|location_mode|null\n"
        )
        self.assertEqual(settings["system.screen_brightness"], 180)
        self.assertEqual(settings["system.peak_refresh_rate"], 120)
        self.assertEqual(settings["global.low_power"], 0)
        self.assertIsNone(settings["secure.location_mode"])

    def test_sampler_converts_qualcomm_gpubusy_pair(self) -> None:
        source = GpuSource(
            name="Adreno",
            load_path="/sys/class/kgsl/kgsl-3d0/gpubusy",
            load_format="busy_total",
            source_type="qualcomm_kgsl",
        )
        script = build_sampler_script(5, 1.0, [], source)
        self.assertIn("100 * $1 / $2", script)
        self.assertIn("gpu_f=-1", script)

    def test_root_gpu_sysfs_is_detected_and_sampled_via_su(self) -> None:
        frequency_path = "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq"

        def fake_shell(adb, device, command, timeout_s=30):
            command_text = command if isinstance(command, str) else " ".join(command)
            if command == ["cat", "/sys/class/kgsl/kgsl-3d0/gpu_model"]:
                return CommandResult([], 1, "", "Permission denied", 0.01)
            if command == ["cat", frequency_path]:
                return CommandResult([], 1, "", "Permission denied", 0.01)
            if isinstance(command, str) and command.startswith("su -c"):
                if "id -u" in command_text:
                    return CommandResult([], 0, "0\n", "", 0.01)
                if "gpu_model" in command_text:
                    return CommandResult([], 0, "Adreno 830\n", "", 0.01)
                if frequency_path in command_text:
                    return CommandResult([], 0, "710000000\n", "", 0.01)
                return CommandResult([], 1, "", "No such file", 0.01)
            return CommandResult([], 0, "", "", 0.01)

        with (
            patch(
                "mobile_profiler.collector.get_prop",
                side_effect=["Qualcomm", "qcom", "sm8750", "false"],
            ),
            patch("mobile_profiler.collector.adb_shell", side_effect=fake_shell),
        ):
            source, probe = detect_gpu_source("adb", "ROOTED")

        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.frequency_path, frequency_path)
        self.assertTrue(source.requires_root)
        self.assertTrue(probe["root_access_used"])
        self.assertEqual(probe["initial_frequency_mhz"], 710.0)
        script = build_sampler_script(5, 1.0, [], source)
        self.assertIn("su -c", script)
        self.assertIn(frequency_path, script)

    def test_sampler_context_line(self) -> None:
        parsed = parse_sampler_line(
            "CTX|123.50|tv.danmaku.bili/.MainActivity|Awake|101|60.0",
            [],
            None,
        )
        self.assertIsInstance(parsed, ContextSample)
        assert isinstance(parsed, ContextSample)
        self.assertEqual(parsed.foreground_package, "tv.danmaku.bili")
        self.assertEqual(parsed.foreground_activity, ".MainActivity")
        self.assertEqual(parsed.refresh_rate_hz, 60.0)
        script = build_sampler_script(5, 1.0, [], None)
        self.assertIn("/ResumedActivity|mFocusedApp/", script)
        self.assertNotIn("mLastPausedActivity", script)

    def test_sampler_script_supports_unlimited_recording(self) -> None:
        script = build_sampler_script(0, 1.0, [], None)

        self.assertIn("end=0", script)
        self.assertIn('[ "$end" -gt 0 ] && [ "$now" -ge "$end" ]', script)

    def test_android_display_surface_and_touch_performance_parsers(self) -> None:
        display = parse_android_display_performance(
            "mSfDisplayModes=\n"
            "DisplayMode{id=0, width=1260, height=2800, peakRefreshRate=120.00001, vsyncRate=120.00001}\n"
            "DisplayMode{id=1, width=1260, height=2800, peakRefreshRate=90.0, vsyncRate=90.0}\n"
            "DisplayMode{id=2, width=1260, height=2800, peakRefreshRate=60.0, vsyncRate=60.0}\n"
            "mActiveSfDisplayMode=DisplayMode{id=2, width=1260, height=2800, peakRefreshRate=60.0}\n"
            "mActiveRenderFrameRate=60.0\n"
        )
        self.assertEqual(display["display_width_px"], 1260)
        self.assertEqual(display["display_height_px"], 2800)
        self.assertEqual(display["refresh_rate_hz"], 60.0)
        self.assertEqual(display["supported_refresh_rates_hz"], [60.0, 90.0, 120.0])

        surface = parse_android_surfaceflinger_performance(
            "ScreenOff: 3d02:46:40.574\n"
            "120.00 Hz: 0d00:00:27.839\n"
            "60.00 Hz: 0d01:12:01.945\n"
            "GLES: ARM, Mali-G925-Immortalis MC12, OpenGL ES 3.2\n"
        )
        self.assertAlmostEqual(surface["refresh_rate_durations_s"]["120"], 27.839)
        self.assertAlmostEqual(surface["refresh_rate_durations_s"]["60"], 4321.945)
        self.assertEqual(surface["gpu_vendor"], "ARM")
        self.assertEqual(surface["gpu_renderer"], "Mali-G925-Immortalis MC12")

        touch = parse_android_touch_devices(
            "add device 1: /dev/input/event6\n"
            '  name:     "vivo_ts"\n'
            "    ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0\n"
            "    ABS_MT_POSITION_X     : value 0, min 0, max 12599, fuzz 0\n"
            "    ABS_MT_POSITION_Y     : value 0, min 0, max 27999, fuzz 0\n"
            "  INPUT_PROP_DIRECT\n"
        )
        self.assertEqual(touch["devices"][0]["name"], "vivo_ts")
        self.assertEqual(touch["devices"][0]["max_touch_points"], 10)
        self.assertFalse(touch["sampling_rate_available"])

    def test_android_surface_layer_and_latency_parser_support_vivo_android_16(self) -> None:
        layers = parse_android_surface_layers(
            "RequestedLayerState{Bounds for - com.hottagames.yh.laohu/"
            "com.epicgames.unreal.GameActivity#1738 parentId=1735}\n"
            "RequestedLayerState{5a0d38c SurfaceView[com.hottagames.yh.laohu/"
            "com.epicgames.unreal.GameActivity](BLAST) 07-14 19:19:43.866#1740 "
            "parentId=1739}\n"
            "RequestedLayerState{com.hottagames.yh.laohu/"
            "com.epicgames.unreal.GameActivity#9999 parentId=1739}\n"
            "RequestedLayerState{Background for 5a0d38c SurfaceView["
            "com.hottagames.yh.laohu/com.epicgames.unreal.GameActivity]#1741 "
            "parentId=1739 z=-2147483648}\n",
            "com.hottagames.yh.laohu",
            "com.epicgames.unreal.GameActivity",
        )
        self.assertEqual(layers["surface_layer_type"], "blast_surfaceview")
        self.assertIn("(BLAST)", layers["surface_layer_name"])

        latency = parse_android_surface_latency(
            "8333333\n"
            "900000000 1000000000 1000100000\n"
            "916666667 1016666667 1016766667\n"
            "933333334 9223372036854775807 1016866667\n"
            "0 1033333334 1033433334\n"
        )
        self.assertEqual(latency["surface_refresh_period_ns"], 8_333_333)
        self.assertEqual(
            latency["surface_frame_timestamps_ns"],
            [1_000_000_000, 1_016_666_667],
        )

    def test_android_surface_layer_parser_falls_back_to_deep_foreground_app_buffer(self) -> None:
        layers = parse_android_surface_layers(
            "RequestedLayerState{ActivityRecord{14383997 u0 "
            "com.android.launcher/.Launcher t2}#56 parentId=55}\n"
            "RequestedLayerState{cbdd7b1 ActivityRecordInputSink "
            "com.android.launcher/.Launcher#57 parentId=56 z=-2147483648}\n"
            "RequestedLayerState{8d5172b com.android.launcher/"
            "com.android.launcher.Launcher#66 parentId=56}\n"
            "RequestedLayerState{com.android.launcher/"
            "com.android.launcher.Launcher#3400 parentId=66}\n"
            "RequestedLayerState{com.android.launcher/"
            "com.android.launcher.Launcher#3425 parentId=3400}\n"
            "RequestedLayerState{Bounds for - com.android.launcher/"
            "com.android.launcher.Launcher#9998 parentId=3425}\n"
            "RequestedLayerState{Background for com.android.launcher/"
            "com.android.launcher.Launcher#9999 parentId=3425}\n",
            "com.android.launcher",
            ".Launcher",
        )

        self.assertEqual(
            layers["surface_layer_name"],
            "com.android.launcher/com.android.launcher.Launcher#3425",
        )
        self.assertEqual(layers["surface_layer_type"], "app_layer")
        self.assertEqual(
            layers["surface_layer_source"],
            "SurfaceFlinger --list foreground application layer",
        )

    def test_android_surface_frame_metrics_report_fps_p99_and_one_percent_low(self) -> None:
        metrics = android_surface_frame_metrics(
            [16.666] * 99 + [33.269],
            refresh_rate_hz=120.0,
        )
        self.assertEqual(metrics["frame_sample_count"], 100)
        self.assertAlmostEqual(metrics["compositor_fps"], 59.41, places=1)
        self.assertAlmostEqual(metrics["one_percent_low_fps"], 30.06, places=1)
        self.assertGreater(metrics["frame_interval_p99_ms"], 16.66)
        self.assertEqual(metrics["missed_vsync_interval_count"], 1)
        self.assertEqual(metrics["frame_cadence_divisor"], 2)
        self.assertTrue(metrics["surface_frame_source"])
        self.assertEqual(
            metrics["frame_counter_source"],
            "Android SurfaceFlinger foreground application-layer present timestamps",
        )
        self.assertEqual(len(metrics["frame_intervals_ms"]), 100)

    def test_android_gfxinfo_parser_selects_foreground_window(self) -> None:
        parsed = parse_android_gfxinfo(
            "Total frames rendered: 200\n"
            "Window: com.example/com.example.MainActivity\n"
            "Total frames rendered: 120\n"
            "Janky frames: 4 (3.3%)\n"
            "Number Missed Vsync: 2\n"
            "Number Frame deadline missed: 3\n"
            "HISTOGRAM: 10ms=100 20ms=20\n"
            "Window: com.example/com.example.OldActivity\n"
            "Total frames rendered: 900\n"
            "HISTOGRAM: 10ms=800 20ms=100\n",
            "com.example",
            ".MainActivity",
        )
        self.assertEqual(parsed["foreground_window_name"], "com.example/com.example.MainActivity")
        self.assertEqual(parsed["frame_counter_total"], 120)
        self.assertEqual(parsed["frame_counter_deadline_missed"], 3)
        self.assertEqual(parsed["frame_histogram_ms"], {"10": 100, "20": 20})

    def test_android_gfxinfo_parser_preserves_detailed_framestats(self) -> None:
        columns = [
            "Flags",
            "FrameTimelineVsyncId",
            "IntendedVsync",
            "Vsync",
            "InputEventId",
            "HandleInputStart",
            "AnimationStart",
            "PerformTraversalsStart",
            "DrawStart",
            "FrameDeadline",
            "FrameStartTime",
            "SyncQueued",
            "SyncStart",
            "IssueDrawCommandsStart",
            "SwapBuffers",
            "FrameCompleted",
            "DequeueBufferDuration",
            "QueueBufferDuration",
            "GpuCompleted",
            "SwapBuffersCompleted",
            "DisplayPresentTime",
            "CommandSubmissionCompleted",
        ]
        values = [
            0,
            42,
            1_000_000_000,
            1_001_000_000,
            0,
            1_001_100_000,
            1_002_000_000,
            1_003_000_000,
            1_004_000_000,
            1_016_000_000,
            1_001_000_000,
            1_006_000_000,
            1_006_500_000,
            1_007_000_000,
            1_009_000_000,
            1_018_000_000,
            500_000,
            300_000,
            1_015_000_000,
            1_010_000_000,
            1_018_000_000,
            1_010_000_000,
        ]
        parsed = parse_android_gfxinfo(
            "Window: com.example/com.example.MainActivity\n"
            "Total frames rendered: 10\n"
            + ",".join(columns)
            + "\n"
            + ",".join(str(value) for value in values)
            + "\n",
            "com.example",
            ".MainActivity",
        )
        self.assertEqual(len(parsed["frame_records"]), 1)
        self.assertEqual(parsed["frame_records"][0]["FrameTimelineVsyncId"], 42)
        self.assertEqual(parsed["frame_records"][0]["GpuCompleted"], 1_015_000_000)

    def test_android_window_and_interpolation_parsers_keep_evidence_explicit(self) -> None:
        window = parse_android_window_performance(
            "  Window #3 Window{abc123 u0 com.example/.MainActivity}:\n"
            "    mFrame=[0,72][1260,2736]\n"
            "    mBounds=Rect(0, 0 - 1260, 2800)\n",
            "com.example",
            ".MainActivity",
        )
        self.assertEqual(window["render_width_px"], 1260)
        self.assertEqual(window["render_height_px"], 2664)
        self.assertEqual(window["render_resolution_source"], "WindowManager mFrame")
        self.assertTrue(window["render_resolution_estimated"])

        render = parse_android_surface_render_resolution(
            "+ name:abc123 SurfaceView[com.example/.MainActivity]#1(BLAST Consumer)1, "
            "id:42, size:4500.00KiB, w/h:720x1600, usage: 0xb00, req fmt:1,\n"
            "+ name:VRI[MainActivity,type=1]#0(BLAST Consumer)0, "
            "id:43, size:12000.00KiB, w/h:1260x2800, usage: 0xb00, req fmt:1,\n",
            "com.example",
            "abc123 SurfaceView[com.example/.MainActivity](BLAST)",
        )
        self.assertEqual(render["render_width_px"], 720)
        self.assertEqual(render["render_height_px"], 1600)
        self.assertEqual(render["render_resolution_source"], "SurfaceFlinger GraphicBuffer")
        self.assertFalse(render["render_resolution_estimated"])

        interpolation = parse_android_frame_interpolation(
            "[persist.vendor.display.memc.enable]: [1]\n"
        )
        self.assertEqual(interpolation["frame_interpolation_status"], "detected")
        self.assertEqual(interpolation["frame_interpolation_confidence"], "medium")
        self.assertEqual(interpolation["frame_interpolation_scope"], "device")
        self.assertTrue(interpolation["frame_interpolation_evidence"])

        current_game = parse_android_frame_interpolation(
            "[ro.config.per_app_memcg]: [false]\n"
            "[ro.vendor.mtk.gpu.game_memc]: [1]\n"
            "[sys.game.memc.postprocessing.enable]: [1]\n"
            "gamecube_frame_interpolation=0:-1:22590:0:0\n"
            "[sys.vivo.memcg.delayinit]: [1]\n"
            "memc_main=0\n",
            "com.example",
            22590,
        )
        self.assertEqual(current_game["frame_interpolation_status"], "disabled")
        self.assertEqual(current_game["frame_interpolation_scope"], "current_session")
        self.assertEqual(current_game["frame_interpolation_confidence"], "high")
        self.assertTrue(
            current_game["frame_interpolation_evidence"][0].startswith(
                "gamecube_frame_interpolation="
            )
        )
        self.assertFalse(
            any("memcg" in item for item in current_game["frame_interpolation_evidence"])
        )

        unavailable = parse_android_frame_interpolation("ro.surface_flinger.use_color_management=true")
        self.assertEqual(unavailable["frame_interpolation_status"], "unavailable")

    def test_top_process_and_thread_parsers_prioritize_dex2oat(self) -> None:
        processes = parse_top_processes(
            "123 shell 1 20 0 fg 6.8M R 99.0 0.0 0:00.10 top -b -q -n 1\n"
            "456 root 651 20 0 bg 120M R 72.5 0.7 1:02.50 /apex/com.android.art/bin/dex2oat64 --dex-file=/data/app/base.apk\n"
            "1764 system 1 20 0 fg 648M S 12.0 4.2 3:04.00 system_server\n"
        )
        self.assertEqual(len(processes), 2)
        self.assertEqual(processes[0]["watch_name"], "dex2oat")
        self.assertTrue(processes[0]["activity_active"])
        self.assertEqual(processes[0]["resident_bytes"], 120 * 1024 * 1024)

        threads = parse_top_threads(
            "456 457 root 20 0 bg 120M R 68.0 0.7 0:12.50 dex2oat-worker /apex/com.android.art/bin/dex2oat64\n"
            "123 123 shell 20 0 fg 8M R 80.0 0.0 0:00.20 top top -H -b -q -n 1\n"
        )
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["watch_kind"], "dex_optimization")

    def test_gc_and_kworker_threads_are_classified_by_activity(self) -> None:
        threads = parse_top_threads(
            "13646 14321 u0_a287 20 0 fg 220M R 24.0 2.0 0:04.00 HeapTaskDaemon tv.danmaku.bili\n"
            "88 88 root 20 0 bg 0 R 18.0 0.0 0:02.00 kworker/u25:2-ufs [kworker/u25:2-ufs]\n"
        )
        by_name = {item["name"]: item for item in threads}
        self.assertEqual(by_name["HeapTaskDaemon"]["activity_family"], "gc")
        self.assertEqual(by_name["HeapTaskDaemon"]["subsystem"], "art_runtime")
        self.assertEqual(by_name["kworker/u25:2-ufs"]["activity_family"], "kworker")
        self.assertEqual(by_name["kworker/u25:2-ufs"]["subsystem"], "storage")

    def test_thermalservice_and_adpf_parsers(self) -> None:
        thermal = parse_thermalservice(
            "Thermal Status: 2\nHAL Ready: true\n"
            "Current temperatures from HAL:\n"
            " Temperature{mValue=44.5, mType=0, mName=CPU, mStatus=2}\n"
            "Current cooling devices from HAL:\n"
            " CoolingDevice{mValue=1, mType=6, mName=lcd-backlight}\n"
            "Temperature static thresholds from HAL:\n"
            " TemperatureThreshold{mType=0, mName=CPU, mHotThrottlingThresholds=[NaN, 50.0, 60.0], mColdThrottlingThresholds=[NaN, NaN, NaN]}\n"
            "Temperature headroom thresholds:\n[NaN, 0.8, 1.0]\n"
        )
        self.assertEqual(thermal["status"], 2)
        self.assertEqual(thermal["temperatures"][0]["value_c"], 44.5)
        self.assertEqual(thermal["thresholds"][0]["hot_c"], [None, 50.0, 60.0])
        self.assertEqual(thermal["cooling_devices"][0]["value"], 1.0)

        hint = parse_performance_hint(
            "HintSessionPreferredRate: 8333333\nHint Session Support: true\n"
            "Active Sessions:\nUid 10072:\n  Session:\n    SessionPID: 2583\n"
            "    SessionUID: 10072\n    SessionTIDs: [2717, 2718]\n"
            "    SessionTargetDurationNanos: 1000000000\n"
            "    PowerEfficient: false\n    GraphicsPipeline: true\n"
            "CPU Headroom Supported: false\nGPU Headroom Supported: false\n"
        )
        self.assertTrue(hint["hint_session_supported"])
        self.assertEqual(hint["sessions"][0]["tids"], [2717, 2718])
        self.assertTrue(hint["sessions"][0]["graphics_pipeline"])

    def test_android_performance_context_script_skips_frame_probes_while_screen_inactive(self) -> None:
        script = _android_performance_context_script(
            include_surface_flinger=True,
            include_foreground=True,
            include_frame_rate=True,
            include_window=True,
            include_frame_details=True,
        )

        self.assertIn("app_ctx_screen_active=0", script)
        self.assertIn(
            'if [ "$app_ctx_screen_active" = 1 ] && [ "$app_ctx_package" != unknown ]; then dumpsys gfxinfo',
            script.replace("\n", " "),
        )
        self.assertIn(
            'if [ "$app_ctx_screen_active" = 1 ]; then dumpsys SurfaceFlinger',
            script.replace("\n", " "),
        )

        surface_only = _android_performance_context_script(
            include_surface_flinger=True,
            include_foreground=True,
            include_frame_rate=True,
            include_window=True,
            include_frame_details=False,
            include_gfxinfo_summary=False,
        )
        self.assertNotIn("dumpsys gfxinfo", surface_only)
        self.assertIn("dumpsys SurfaceFlinger --list", surface_only)

    def test_android_context_worker_skips_redundant_gfxinfo_after_surface_timestamps(self) -> None:
        worker = AndroidPerformanceContextWorker(
            "adb",
            "serial",
            Mock(),
            [],
            include_frame_rate=True,
            include_frame_details=False,
        )
        worker._surface_layer_name = "com.example.game/BLAST#1"
        worker._surface_last_timestamp_ns = 123
        with patch(
            "mobile_profiler.collector.collect_android_performance_context",
            return_value=(None, {}, None),
        ) as collect:
            worker._collect(False)

        self.assertFalse(collect.call_args.kwargs["include_gfxinfo_summary"])

    def test_android_inactive_screen_context_drops_cached_and_historical_frame_data(self) -> None:
        output = "\n".join(
            [
                "__APP_ANDROID_CONTEXT__|unknown|Asleep|2500",
                "__APP_ANDROID_DISPLAY__",
                "mActiveRenderFrameRate=120",
                "__APP_ANDROID_SURFACE__",
                "__APP_ANDROID_LAYER__",
                "__APP_ANDROID_WINDOW__",
                "__APP_ANDROID_GFXINFO__",
            ]
        )
        with patch(
            "mobile_profiler.collector._timed_shell_output",
            return_value=(100.0, 1000.0, output, 5.0, None),
        ):
            context, surface_update, error = collect_android_performance_context(
                "adb",
                "serial",
                include_frame_details=True,
                cached_surface={
                    "frame_records": [{"FrameCompleted": 10}],
                    "frame_histogram_ms": {"16": 100},
                    "surface_layer_name": "stale-layer",
                },
            )

        self.assertIsNone(error)
        self.assertEqual(surface_update, {})
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.screen_state, "Asleep")
        self.assertIsNone(context.foreground_package)
        self.assertIsNone(context.refresh_rate_hz)
        self.assertFalse(context.performance["frame_data_available"])
        self.assertFalse(context.performance["refresh_rate_live_valid"])
        self.assertNotIn("frame_records", context.performance)
        self.assertNotIn("frame_histogram_ms", context.performance)
        self.assertNotIn("surface_layer_name", context.performance)

    def test_android_probe_separates_screen_inactive_history_from_live_frame_data(self) -> None:
        commands = []

        def fake_adb_shell(_adb, _device, args, timeout_s=0):
            command = " ".join(args) if isinstance(args, list) else str(args)
            commands.append(command)
            if command == "dumpsys power":
                return CommandResult([], 0, "mWakefulness=Asleep\n", "", 0.01)
            if command == "dumpsys display":
                return CommandResult([], 0, "mActiveRenderFrameRate=120\n", "", 0.01)
            if command.startswith("dumpsys gfxinfo"):
                return CommandResult(
                    [],
                    0,
                    "Window: com.example/.Main\nTotal frames rendered: 100\nJanky frames: 2\n",
                    "",
                    0.01,
                )
            if command == "settings get system screen_brightness":
                return CommandResult([], 0, "2500\n", "", 0.01)
            return CommandResult([], 0, "", "", 0.01)

        with (
            patch("mobile_profiler.collector.adb_shell", side_effect=fake_adb_shell),
            patch(
                "mobile_profiler.collector.collect_foreground_package",
                return_value="com.example",
            ),
        ):
            result = probe_android_performance("adb", "serial")

        performance = result["performance"]
        self.assertEqual(result["screen_state"], "Asleep")
        self.assertIsNone(result["foreground_package"])
        self.assertEqual(result["last_known_foreground_package"], "com.example")
        self.assertFalse(performance["frame_data_available"])
        self.assertFalse(performance["refresh_rate_live_valid"])
        self.assertIsNone(performance["refresh_rate_hz"])
        self.assertEqual(performance["last_reported_refresh_rate_hz"], 120.0)
        self.assertIn("不代表当前 FPS", performance["frame_unavailable_reason"])
        self.assertNotIn("frame_records", performance)
        self.assertNotIn("frame_histogram_ms", performance)
        self.assertFalse(result["capabilities"]["gfxinfo_frame_counters"])
        self.assertFalse(result["capabilities"]["frame_rate"])
        self.assertTrue(
            result["capabilities"]["frame_probe_skipped_inactive_screen"]
        )
        self.assertFalse(any(command.startswith("dumpsys gfxinfo") for command in commands))

    def test_android_probe_collects_live_gfxinfo_only_while_screen_awake(self) -> None:
        commands = []

        def fake_adb_shell(_adb, _device, args, timeout_s=0):
            command = " ".join(args) if isinstance(args, list) else str(args)
            commands.append(command)
            if command == "dumpsys power":
                return CommandResult([], 0, "mWakefulness=Awake\n", "", 0.01)
            if command == "dumpsys display":
                return CommandResult([], 0, "mActiveRenderFrameRate=120\n", "", 0.01)
            if command.startswith("dumpsys gfxinfo"):
                return CommandResult(
                    [],
                    0,
                    "Window: com.example/.Main\nTotal frames rendered: 100\nJanky frames: 2\n",
                    "",
                    0.01,
                )
            if command == "settings get system screen_brightness":
                return CommandResult([], 0, "2500\n", "", 0.01)
            return CommandResult([], 0, "", "", 0.01)

        with (
            patch("mobile_profiler.collector.adb_shell", side_effect=fake_adb_shell),
            patch(
                "mobile_profiler.collector.collect_foreground_package",
                return_value="com.example",
            ),
        ):
            result = probe_android_performance("adb", "serial")

        self.assertTrue(any(command.startswith("dumpsys gfxinfo") for command in commands))
        self.assertTrue(result["performance"]["frame_data_available"])
        self.assertTrue(result["capabilities"]["gfxinfo_frame_counters"])
        self.assertFalse(
            result["capabilities"]["frame_probe_skipped_inactive_screen"]
        )

    def test_android_record_preflight_skips_frame_history_when_frames_are_disabled(self) -> None:
        commands = []

        def fake_adb_shell(_adb, _device, args, timeout_s=0):
            command = " ".join(args) if isinstance(args, list) else str(args)
            commands.append(command)
            if command == "dumpsys power":
                return CommandResult([], 0, "mWakefulness=Awake\n", "", 0.01)
            if command == "dumpsys display":
                return CommandResult([], 0, "mActiveRenderFrameRate=120\n", "", 0.01)
            if command.startswith("dumpsys gfxinfo"):
                raise AssertionError("disabled record preflight must not request framestats")
            return CommandResult([], 0, "", "", 0.01)

        with (
            patch("mobile_profiler.collector.adb_shell", side_effect=fake_adb_shell),
            patch(
                "mobile_profiler.collector.collect_foreground_package",
                return_value="com.example",
            ),
        ):
            result = probe_android_performance(
                "adb",
                "serial",
                include_frame_rate=False,
                include_frame_details=False,
            )

        self.assertFalse(any(command.startswith("dumpsys gfxinfo") for command in commands))
        self.assertFalse(result["performance"]["frame_data_available"])
        self.assertFalse(result["capabilities"]["frame_rate"])
        self.assertTrue(
            result["capabilities"]["frame_probe_skipped_by_configuration"]
        )
        self.assertFalse(
            result["capabilities"]["frame_probe_skipped_inactive_screen"]
        )

    def test_android_performance_probe_metadata_omits_bulk_raw_frame_history(self) -> None:
        source = {
            "refresh_rate_hz": 120.0,
            "frame_counter_total": 123,
            "frame_records": [{"FrameCompleted": 1}, {"FrameCompleted": 2}],
            "frame_histogram_ms": {"16": 100, "32": 3},
            "frame_stats_columns": ["Flags", "FrameCompleted"],
            "frame_intervals_ms": [16.7, 33.4],
        }

        compact = compact_android_performance_probe_for_metadata(source)

        self.assertEqual(compact["refresh_rate_hz"], 120.0)
        self.assertEqual(compact["frame_counter_total"], 123)
        self.assertNotIn("frame_records", compact)
        self.assertNotIn("frame_histogram_ms", compact)
        self.assertNotIn("frame_stats_columns", compact)
        self.assertNotIn("frame_intervals_ms", compact)
        self.assertTrue(compact["raw_frame_history_omitted_from_metadata"])
        self.assertEqual(compact["raw_frame_record_count"], 2)
        self.assertEqual(compact["raw_frame_histogram_bin_count"], 2)
        self.assertEqual(compact["raw_frame_stats_column_count"], 2)
        self.assertIn("frame_records", source)

    def test_display_brightness_parser_separates_requested_and_thermal_cap(self) -> None:
        parsed = parse_display_brightness_state(
            """Display Power Controller Thread State:
  mScreenState=ON
  mScreenBrightness=0.2800000
  mSdrScreenBrightness=0.2800000
DisplayBrightnessController:
  mCurrentScreenBrightness=0.3954986
  mLastUserSetScreenBrightness=0.3954986
mCachedBrightnessInfo.brightness=0.2800000
mCachedBrightnessInfo.adjustedBrightness=0.2800000
mCachedBrightnessInfo.brightnessMin=0.0
mCachedBrightnessInfo.brightnessMax=0.2800000
mCachedBrightnessInfo.brightnessMaxReason=1
BrightnessThermalClamper:
  mThrottlingStatus=3
  mBrightnessCap=0.2800000
  mApplied=true
HighBrightnessModeController:
  mBrightness=0.2800000
  mUnthrottledBrightness=0.3954986
  mThrottlingReason=thermal
  mCurrentMax=0.2800000
""",
            "101\n",
            "0.3954986\n",
        )

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["screen_state"], "ON")
        self.assertEqual(parsed["setting_raw"], 101.0)
        self.assertAlmostEqual(parsed["current_screen_brightness"], 0.3954986)
        self.assertAlmostEqual(parsed["screen_brightness"], 0.28)
        self.assertAlmostEqual(parsed["thermal_cap"], 0.28)
        self.assertTrue(parsed["thermal_applied"])
        self.assertEqual(parsed["brightness_max_reason"], 1)
        self.assertEqual(parsed["hbm_throttling_reason"], "thermal")

    def test_activity_manager_process_state_parser(self) -> None:
        parsed = parse_activity_processes(
            "  *APP* UID 10287 ProcessRecord{abc 13646:tv.danmaku.bili/u0a287}\n"
            "    mCurSchedGroup=3 setSchedGroup=3 systemNoUi=false\n"
            "    curProcState=2 mRepProcState=2 setProcState=2 mAdjType=top-app\n"
            "    isFrozen=false\n",
            {13646},
        )
        self.assertEqual(parsed[0]["current_proc_state"], 2)
        self.assertEqual(parsed[0]["current_sched_group"], 3)
        self.assertFalse(parsed[0]["frozen"])


class ConversionTests(unittest.TestCase):
    def test_smartperf_missing_ddr_explains_backend_specific_gap(self) -> None:
        result = analyze_memory_frequency(
            [],
            {"capture_configuration": {"backend": "harmony_smartperf"}},
        )

        self.assertFalse(result["available"])
        self.assertIn("SmartPerf SP_daemon -d", result["limitations"])

    def test_discharge_current_is_positive_magnitude(self) -> None:
        policy = CpuPolicy(
            name="policy0",
            path="/test",
            cluster_index=0,
            label="CPU",
            cores=[0],
            max_khz=2_000_000,
        )
        raw = [
            RawSample(
                index=0,
                uptime_s=10.0,
                current_raw=300.0,
                voltage_mv=3800.0,
                temperature_tenths_c=300.0,
                cpu=CpuTimes(idle=100.0),
                core_cpu={0: CpuTimes(idle=100.0)},
                frequencies_khz={"policy0": 1_000_000.0},
            ),
            RawSample(
                index=1,
                uptime_s=11.0,
                current_raw=320.0,
                voltage_mv=3799.0,
                temperature_tenths_c=301.0,
                cpu=CpuTimes(user=30.0, idle=170.0),
                core_cpu={0: CpuTimes(user=30.0, idle=170.0)},
                frequencies_khz={"policy0": 1_500_000.0},
            ),
        ]
        samples, warnings = convert_samples(
            raw,
            [policy],
            None,
            3800.0,
            3799.0,
            "ma",
            "discharging",
            external_power=False,
        )
        self.assertEqual(samples[0].current_ma, 300.0)
        self.assertEqual(samples[0].signed_current_ma, -300.0)
        self.assertEqual(samples[0].direction, "discharging")
        self.assertGreater(samples[0].power_mw, 0.0)
        self.assertTrue(samples[0].power_valid_for_consumption)
        self.assertFalse(samples[0].external_power)
        self.assertTrue(is_consumption_power_sample(samples[0]))
        self.assertTrue(warnings)

    def test_external_power_with_negative_current_is_not_consumption(self) -> None:
        raw = [
            RawSample(
                index=index,
                uptime_s=10.0 + index,
                current_raw=-250.0,
                voltage_mv=4000.0,
                temperature_tenths_c=300.0,
                cpu=CpuTimes(idle=100.0 + index * 100.0),
                external_power=True,
                battery_status="discharging",
            )
            for index in range(2)
        ]

        samples, _ = convert_samples(
            raw,
            [],
            None,
            4000.0,
            4000.0,
            "ma",
            "discharging",
            external_power=False,
        )

        self.assertEqual(samples[0].signed_current_ma, -250.0)
        self.assertEqual(samples[0].direction, "external_power")
        self.assertFalse(samples[0].power_valid_for_consumption)
        self.assertTrue(samples[0].external_power)
        self.assertFalse(is_consumption_power_sample(samples[0]))

    def test_new_sampler_unknown_external_power_does_not_use_session_start_fallback(self) -> None:
        raw = [
            RawSample(
                index=index,
                uptime_s=10.0 + index,
                current_raw=250.0,
                voltage_mv=4000.0,
                temperature_tenths_c=300.0,
                cpu=CpuTimes(idle=100.0 + index * 100.0),
                external_power=None,
                battery_status="discharging",
            )
            for index in range(2)
        ]

        samples, warnings = convert_samples(
            raw,
            [],
            None,
            4000.0,
            4000.0,
            "ma",
            "discharging",
            external_power=False,
        )

        self.assertEqual(samples[0].direction, "discharging")
        self.assertIsNone(samples[0].external_power)
        self.assertFalse(samples[0].power_valid_for_consumption)
        self.assertFalse(is_consumption_power_sample(samples[0]))
        self.assertTrue(any("未能确认是否接入外部电源" in item for item in warnings))


class PowerConsumptionValidityTests(unittest.TestCase):
    @staticmethod
    def _metadata(platform: str = "android") -> dict[str, object]:
        enable_features = ["power_attribution"] if platform == "android" else []
        return {
            "schema_version": SCHEMA_VERSION,
            "platform": platform,
            "test_mode": "power",
            "sample_interval_s": 1.0,
            "session_mode": False,
            "target_package": "com.example.game",
            "cpu_policies": [],
            "battery_start": {
                "level_pct": 80,
                "powered": False,
                "status": "discharging",
                "full_charge_capacity_mah": 4000.0,
            },
            "battery_end": {
                "level_pct": 79,
                "powered": False,
                "status": "discharging",
            },
            "capture_configuration": resolve_capture_configuration(
                "power",
                platform,
                "power-standard",
                enable_features=enable_features,
            ),
        }

    @staticmethod
    def _sample(
        index: int,
        power_mw: float,
        direction: str,
        valid: bool,
        external_power: bool,
        *,
        power_source: str = "battery_current_voltage",
    ) -> Sample:
        voltage_mv = 4000.0
        current_ma = power_mw / voltage_mv * 1000.0
        signed_current_ma = -current_ma if direction == "discharging" else current_ma
        return Sample(
            index=index,
            elapsed_s=float(index),
            uptime_s=100.0 + index,
            current_ma=current_ma,
            signed_current_ma=signed_current_ma,
            voltage_mv=voltage_mv,
            power_mw=power_mw,
            direction=direction,
            cpu_pct=20.0,
            power_source=power_source,
            power_valid_for_consumption=valid,
            external_power=external_power,
        )

    @staticmethod
    def _attribution_outputs() -> dict[str, str]:
        return {
            "packages": "package:com.example.game uid:10123\n",
            "batterystats_usage": (
                "Capacity: 4000, Computed drain: 20, actual drain: 19\n"
                "UID 10123: 10.0\n"
            ),
            "batterystats": "Statistics since last charge:\nKernel Wakelock test: 10s\n",
            "batterystats_checkin": "9,10123,l,nt,1,2,3,4,5,6\n",
            "power_profile": "battery.capacity=4000\n",
        }

    def test_charging_session_keeps_raw_power_but_has_no_consumption_conclusions(self) -> None:
        samples = [
            self._sample(index, 10_000.0, "charging", False, True)
            for index in range(4)
        ]

        analysis = analyze_run(
            samples,
            self._metadata(),
            self._attribution_outputs(),
            [],
        )
        summary = analysis["summary"]

        self.assertFalse(summary["power_valid_for_consumption"])
        self.assertFalse(summary["consumption_session_representative"])
        self.assertAlmostEqual(summary["observed_power_average_mw"], 10_000.0)
        self.assertAlmostEqual(summary["battery_flow_average_power_mw"], 10_000.0)
        for key in (
            "average_current_ma",
            "minimum_current_ma",
            "maximum_current_ma",
            "average_power_mw",
            "median_power_mw",
            "p95_power_mw",
            "minimum_power_mw",
            "maximum_power_mw",
            "energy_mwh",
            "discharge_mah",
            "drain_pct_per_hour",
            "full_runtime_h",
            "remaining_runtime_h",
        ):
            self.assertIsNone(summary[key], key)
        self.assertEqual(analysis["battery_usage"]["uids"], [])
        self.assertEqual(analysis["components"], [])
        self.assertEqual(analysis["wakelocks"], [])
        self.assertEqual(analysis["stats_window"], {})
        self.assertEqual(analysis["findings"], [])
        self.assertTrue(analysis["test_items"]["analysis_disabled"])

    def test_mixed_power_session_integrates_only_contiguous_discharge_intervals(self) -> None:
        samples = [
            self._sample(index, power_mw, direction, valid, external)
            for index, (power_mw, direction, valid, external) in enumerate(
                (
                    (1000.0, "discharging", True, False),
                    (1000.0, "discharging", True, False),
                    (10_000.0, "charging", False, True),
                    (10_000.0, "charging", False, True),
                    (1000.0, "discharging", True, False),
                    (1000.0, "discharging", True, False),
                )
            )
        ]

        analysis = analyze_run(
            samples,
            self._metadata(),
            self._attribution_outputs(),
            [],
        )
        summary = analysis["summary"]

        self.assertTrue(summary["power_valid_for_consumption"])
        self.assertFalse(summary["consumption_session_representative"])
        self.assertEqual(summary["consumption_covered_duration_s"], 2.0)
        self.assertAlmostEqual(summary["average_power_mw"], 1000.0)
        self.assertAlmostEqual(summary["p95_power_mw"], 1000.0)
        self.assertAlmostEqual(summary["average_current_ma"], 250.0)
        self.assertAlmostEqual(summary["energy_mwh"], 0.5555556, places=6)
        self.assertAlmostEqual(summary["observed_power_average_mw"], 4600.0)
        self.assertNotAlmostEqual(
            summary["energy_mwh"],
            summary["observed_power_energy_mwh"],
        )
        self.assertIsNone(summary["drain_pct_per_hour"])
        self.assertIsNone(summary["full_runtime_h"])
        self.assertIsNone(summary["remaining_runtime_h"])
        self.assertTrue(analysis["battery_usage"]["analysis_disabled"])
        self.assertEqual(analysis["battery_usage"]["uids"], [])
        self.assertEqual(analysis["components"], [])
        self.assertEqual(analysis["wakelocks"], [])
        self.assertEqual(analysis["stats_window"], {})
        self.assertIsNone(analysis["target_app"]["uid"])
        self.assertIsNone(analysis["target_app"]["usage"])
        self.assertIsNone(analysis["target_app"]["network"])

    def test_complete_discharge_session_allows_full_session_attribution(self) -> None:
        samples = [
            self._sample(index, 1000.0, "discharging", True, False)
            for index in range(4)
        ]

        analysis = analyze_run(
            samples,
            self._metadata(),
            self._attribution_outputs(),
            [],
        )
        summary = analysis["summary"]

        self.assertTrue(summary["consumption_session_representative"])
        self.assertIsNotNone(summary["drain_pct_per_hour"])
        self.assertIsNotNone(summary["full_runtime_h"])
        self.assertEqual(analysis["battery_usage"]["uids"][0]["uid"], 10123)
        self.assertEqual(analysis["target_app"]["uid"], 10123)
        self.assertEqual(analysis["target_app"]["usage"]["mah"], 10.0)
        self.assertEqual(
            analysis["target_app"]["network"],
            {
                "mobile_rx_bytes": 1,
                "mobile_tx_bytes": 2,
                "wifi_rx_bytes": 3,
                "wifi_tx_bytes": 4,
                "bluetooth_rx_bytes": 5,
                "bluetooth_tx_bytes": 6,
            },
        )

    def test_ios_system_load_and_battery_current_voltage_remain_separate(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=250.0,
                signed_current_ma=250.0,
                voltage_mv=4000.0,
                power_mw=2000.0,
                direction="charging",
                cpu_pct=20.0,
                power_source="ios_power_telemetry_system_load",
                power_valid_for_consumption=False,
                external_power=True,
            )
            for index in range(2)
        ]

        analysis = analyze_run(samples, self._metadata("ios"), {}, [])
        summary = analysis["summary"]

        self.assertEqual(summary["observed_power_average_mw"], 2000.0)
        self.assertEqual(summary["battery_flow_average_power_mw"], 1000.0)
        self.assertEqual(
            summary["observed_power_sources"],
            ["ios_power_telemetry_system_load"],
        )
        self.assertEqual(summary["battery_flow_power_source"], "battery_current_voltage")
        self.assertIsNone(summary["average_power_mw"])
        self.assertIsNone(summary["energy_mwh"])
        self.assertEqual(analysis["findings"], [])

    def test_ios_system_load_uses_sample_age_for_step_integration(self) -> None:
        values = (1000.0, 1000.0, 2000.0, 2000.0)
        ages = (0.0, 5.0, 4.0, 9.0)
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index * 5),
                uptime_s=100.0 + index * 5,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=values[index],
                direction="discharging",
                cpu_pct=20.0,
                power_source="ios_power_telemetry_system_load",
                power_sample_age_s=ages[index],
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(4)
        ]
        metadata = self._metadata("ios")
        metadata["sample_interval_s"] = 5.0

        analysis = analyze_run(samples, metadata, {}, [])
        summary = analysis["summary"]

        # The new 2000 mW value was already active at t=6 s, not first at the
        # host observation at t=10 s: (1000*6 + 2000*9) / 15 = 1600 mW.
        self.assertAlmostEqual(summary["observed_power_average_mw"], 1600.0)
        self.assertAlmostEqual(summary["average_power_mw"], 1600.0)
        self.assertAlmostEqual(summary["energy_mwh"], 1600.0 * 15.0 / 3600.0)

    def test_all_external_power_samples_use_external_power_direction(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=2.0,
                signed_current_ma=2.0,
                voltage_mv=4400.0,
                power_mw=8.8,
                direction="external_power",
                cpu_pct=5.0,
                power_source="harmony_battery_service",
                power_valid_for_consumption=False,
                external_power=True,
            )
            for index in range(3)
        ]

        analysis = analyze_run(samples, self._metadata("harmony"), {}, [])

        self.assertEqual(analysis["summary"]["power_flow_direction"], "external_power")
        self.assertIsNone(analysis["summary"]["average_power_mw"])

    def test_ios_mixed_power_sources_never_share_one_average(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=1000.0 if index == 0 else 2000.0,
                direction="discharging",
                cpu_pct=20.0,
                power_source=(
                    "ios_battery_current_voltage"
                    if index == 0
                    else "ios_power_telemetry_system_load"
                ),
                power_sample_age_s=float(index - 1) if index > 0 else None,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(11)
        ]

        analysis = analyze_run(samples, self._metadata("ios"), {}, [])
        summary = analysis["summary"]

        self.assertEqual(
            summary["observed_power_primary_source"],
            "ios_power_telemetry_system_load",
        )
        self.assertEqual(
            summary["observed_power_excluded_sources"],
            ["ios_battery_current_voltage"],
        )
        self.assertAlmostEqual(summary["observed_power_covered_duration_s"], 9.0)
        self.assertAlmostEqual(summary["observed_power_average_mw"], 2000.0)
        self.assertAlmostEqual(summary["average_power_mw"], 2000.0)
        self.assertAlmostEqual(summary["battery_flow_average_power_mw"], 1000.0)
        self.assertTrue(summary["consumption_session_representative"])
        self.assertFalse(samples[0].power_valid_for_consumption)
        self.assertTrue(any("SystemLoad" in item and "I×V" in item for item in analysis["warnings"]))

    def test_stale_ios_system_load_breaks_consumption_energy_integration(self) -> None:
        ages = [0.0, 1.0, IOS_SYSTEM_LOAD_STALE_AFTER_S + 1.0, 0.0]
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=1000.0,
                direction="discharging",
                cpu_pct=20.0,
                power_source="ios_power_telemetry_system_load",
                power_sample_age_s=age,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index, age in enumerate(ages)
        ]

        analysis = analyze_run(samples, self._metadata("ios"), {}, [])
        summary = analysis["summary"]

        self.assertTrue(is_consumption_power_sample(samples[0]))
        self.assertFalse(is_consumption_power_sample(samples[2]))
        self.assertFalse(samples[2].power_valid_for_consumption)
        self.assertEqual(summary["consumption_covered_duration_s"], 1.0)
        self.assertAlmostEqual(summary["energy_mwh"], 1000.0 / 3600.0)
        self.assertTrue(
            any("超过 30 秒未刷新" in item and "积分会在这些区间断开" in item for item in analysis["warnings"])
        )

    def test_cli_summary_does_not_turn_unavailable_consumption_into_zero_watts(self) -> None:
        analysis = {
            "summary": {
                "duration_s": 10.0,
                "power_valid_for_consumption": False,
                "average_current_ma": None,
                "average_power_mw": None,
                "p95_power_mw": None,
                "observed_power_average_mw": 611.0,
                "battery_flow_average_power_mw": 13.23,
                "observed_power_sources": ["ios_power_telemetry_system_load"],
            },
            "warnings": [],
        }

        with patch("builtins.print") as mocked_print:
            print_run_summary(Path("run"), analysis, Path("run/report.html"))

        output = "\n".join(str(call.args[0]) for call in mocked_print.call_args_list)
        self.assertIn("Consumption power: unavailable", output)
        self.assertIn("Observed iOS SystemLoad: 0.611 W (raw only)", output)
        self.assertIn("Battery-flow magnitude: 0.013 W", output)
        self.assertNotIn("Average power: 0.000 W", output)
        self.assertNotIn("P95 power: 0.000 W", output)

    def test_analysis_reports_sampling_cadence_degradation_without_retiming_data(self) -> None:
        samples = [
            self._sample(index, 1000.0, "discharging", True, False)
            for index in range(12)
        ]
        for sample in samples[6:]:
            sample.uptime_s += 2.0

        analysis = analyze_run(samples, self._metadata(), {}, [])
        summary = analysis["summary"]

        self.assertTrue(summary["sampling_cadence_assessment_available"])
        self.assertFalse(summary["sampling_cadence_stable"])
        self.assertEqual(summary["observed_sample_interval_p95_s"], 3.0)
        self.assertEqual(summary["estimated_missed_sample_slots"], 2)
        self.assertTrue(any("主采样节奏未达到" in item for item in analysis["warnings"]))


class CpuAnalysisTests(unittest.TestCase):
    def test_missing_frequency_policies_do_not_claim_android_power_model(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=100.0,
                signed_current_ma=-100.0,
                voltage_mv=4000.0,
                power_mw=400.0,
                direction="discharging",
                cpu_pct=25.0,
            )
            for index in range(2)
        ]

        result = analyze_cpu(samples, [], {})

        self.assertIsNone(result["source"])
        self.assertIsNone(result["modeled_power_mw"])
        self.assertTrue(all(item["modeled_power_mw"] is None for item in result["timeline"]))
        self.assertIn("only total CPU load", result["limitations"])

    def test_high_frequency_produces_positive_premium(self) -> None:
        samples = []
        for index, frequency in enumerate((500.0, 2000.0, 2000.0)):
            samples.append(
                Sample(
                    index=index,
                    elapsed_s=float(index),
                    uptime_s=100.0 + index,
                    current_ma=300.0,
                    signed_current_ma=-300.0,
                    voltage_mv=3800.0,
                    power_mw=1140.0,
                    direction="discharging",
                    cpu_pct=50.0,
                    core_cpu_pct={"0": 50.0},
                    cluster_cpu_pct={"policy0": 50.0},
                    frequencies_mhz={"policy0": frequency},
                )
            )
        result = analyze_cpu(
            samples,
            [
                {
                    "name": "policy0",
                    "label": "CPU",
                    "cluster_index": 0,
                    "cores": [0],
                    "max_khz": 2_000_000,
                }
            ],
            {
                "cpu.core_speeds.cluster0": [500_000.0, 2_000_000.0],
                "cpu.core_power.cluster0": [20.0, 120.0],
            },
        )
        cluster = result["clusters"][0]
        self.assertTrue(cluster["model_available"])
        self.assertGreater(cluster["frequency_premium_mw"], 0.0)
        self.assertGreater(cluster["modeled_power_mw"], cluster["frequency_premium_mw"])

    def test_frequency_residency_skips_deleted_range_boundary(self) -> None:
        samples = []
        for index, (uptime_s, frequency) in enumerate(
            ((100.0, 500.0), (103.0, 2000.0), (104.0, 2000.0))
        ):
            samples.append(
                Sample(
                    index=index,
                    elapsed_s=float(index),
                    uptime_s=uptime_s,
                    current_ma=300.0,
                    signed_current_ma=-300.0,
                    voltage_mv=3800.0,
                    power_mw=1140.0,
                    direction="discharging",
                    cpu_pct=50.0,
                    core_cpu_pct={"0": 50.0},
                    cluster_cpu_pct={"policy0": 50.0},
                    frequencies_mhz={"policy0": frequency},
                )
            )
        samples[1]._report_break_before = True

        result = analyze_cpu(
            samples,
            [
                {
                    "name": "policy0",
                    "label": "CPU",
                    "cluster_index": 0,
                    "cores": [0],
                    "max_khz": 2_000_000,
                }
            ],
            {},
            max_gap_s=3.0,
        )

        residency = {
            item["band"]: item
            for item in result["clusters"][0]["residency"]
        }
        self.assertEqual(residency["low"]["time_pct"], 0.0)
        self.assertEqual(residency["high"]["time_pct"], 100.0)
        self.assertEqual(residency["low"]["load_weighted_pct"], 0.0)
        self.assertEqual(residency["high"]["load_weighted_pct"], 100.0)


class GpuAnalysisTests(unittest.TestCase):
    def test_qualcomm_fallback_keeps_uid_work_and_process_memory(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=300.0,
                signed_current_ma=-300.0,
                voltage_mv=3800.0,
                power_mw=1140.0,
                direction="discharging",
                cpu_pct=20.0,
            )
            for index in range(2)
        ]
        raw_outputs = {
            "gpu_start": (
                "Global total: 1000\nProc 2035 total: 600\nGPU work information.\n"
                "gpu_id uid total_active_duration_ns total_inactive_duration_ns\n"
                "1 1000 1000000 2000000\n"
            ),
            "gpu_end": (
                "Stable Game Driver: unsupported\n"
                "Pre-release Game Driver: com.qualcomm.qti.gpudrivers.sun.api35\n"
                "Global total: 1600\nProc 2035 total: 900\nProc 4507 total: 400\n"
                "GPU work information.\n"
                "gpu_id uid total_active_duration_ns total_inactive_duration_ns\n"
                "1 1000 4000000 5000000\n"
            ),
        }
        result = analyze_gpu(
            samples,
            {"gpu_probe": {"provider": "qualcomm_kgsl", "model": "Adreno830v2"}},
            raw_outputs,
            {1000: ["android"]},
            1000,
            1.0,
            [
                SystemSnapshot(
                    uptime_s=101.0,
                    host_epoch_s=1.0,
                    processes=[{"pid": 2035, "name": "surfaceflinger"}],
                )
            ],
        )
        self.assertEqual(result["model"], "Adreno830v2")
        self.assertTrue(result["work_source_available"])
        self.assertEqual(result["memory"]["change_bytes"], 600)
        self.assertEqual(result["memory"]["processes"][0]["name"], "surfaceflinger")

    def test_harmony_smartperf_gpu_limitations_acknowledge_observed_counters(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=100.0,
                signed_current_ma=100.0,
                voltage_mv=4300.0,
                power_mw=430.0,
                direction="charging",
                cpu_pct=20.0,
                gpu_frequency_mhz=335.0,
                gpu_load_pct=0.0,
                power_valid_for_consumption=False,
                external_power=True,
            )
            for index in range(2)
        ]
        result = analyze_gpu(
            samples,
            {
                "platform": "harmony",
                "gpu_source": {"source_type": "harmony_smartperf"},
                "gpu_probe": {"provider": "harmony_smartperf"},
            },
            {},
            {},
            None,
            1.0,
        )

        self.assertTrue(result["frequency_available"])
        self.assertTrue(result["load_available"])
        self.assertIn("supplied by SmartPerf", result["limitations"])
        self.assertNotIn("explicitly unavailable", result["limitations"])


class SystemAnalysisTests(unittest.TestCase):
    @staticmethod
    def _samples() -> list[Sample]:
        return [
            Sample(
                index=index,
                elapsed_s=float(index * 10),
                uptime_s=100.0 + index * 10,
                current_ma=(1000.0 if index in {2, 3} else 500.0) / 3.8,
                signed_current_ma=-(1000.0 if index in {2, 3} else 500.0) / 3.8,
                voltage_mv=3800.0,
                power_mw=1000.0 if index in {2, 3} else 500.0,
                direction="discharging",
                cpu_pct=50.0,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(6)
        ]

    def test_dex_activity_is_correlated_with_power_window(self) -> None:
        snapshots = []
        for index in range(6):
            active = index in {2, 3}
            dex = {
                "pid": 456,
                "user": "root",
                "name": "dex2oat64",
                "command": "/apex/com.android.art/bin/dex2oat64",
                "cpu_pct": 70.0 if active else None,
                "policy": "bg",
                "state": "R" if active else None,
                "watch_name": "dex2oat",
                "watch_kind": "dex_optimization",
                "watch_label": "DEX AOT compilation",
                "watch_impact": "ART compilation",
                "watch_trigger": "presence",
                "activity_active": active,
            }
            snapshots.append(
                SystemSnapshot(
                    uptime_s=100.0 + index * 10,
                    host_epoch_s=1000.0 + index * 10,
                    processes=[dex] if active else [],
                    watched_processes=[dex] if active else [],
                    process_count=500,
                )
            )
        result = analyze_system_activity(self._samples(), snapshots, 30.0)
        activity = result["priority_activities"]["rows"][0]
        self.assertEqual(activity["name"], "dex2oat")
        self.assertGreater(activity["power_delta_mw"], 0.0)
        self.assertEqual(activity["confidence"], "medium")

    def test_gc_and_kworker_activity_groups_are_correlated_separately(self) -> None:
        snapshots = []
        for index in range(6):
            threads = []
            if index in {0, 1}:
                threads.append(
                    {
                        "pid": 88,
                        "tid": 88,
                        "user": "root",
                        "state": "R",
                        "cpu_pct": 12.0,
                        "name": "kworker/u25:2-ufs",
                        "process": "[kworker/u25:2-ufs]",
                    }
                )
            if index in {2, 3}:
                threads.append(
                    {
                        "pid": 456,
                        "tid": 457,
                        "user": "u0_a1",
                        "state": "R",
                        "cpu_pct": 30.0,
                        "name": "HeapTaskDaemon",
                        "process": "com.example.app",
                    }
                )
            snapshots.append(
                SystemSnapshot(
                    uptime_s=100.0 + index * 10,
                    host_epoch_s=1000.0 + index * 10,
                    threads=threads,
                    process_count=500,
                )
            )
        thermal = [
            ThermalSnapshot(
                uptime_s=100.0 + index * 10,
                host_epoch_s=1000.0 + index * 10,
                temperatures=[{"name": "CPU", "value_c": 40.0 + index}],
            )
            for index in range(6)
        ]
        result = analyze_system_activity(self._samples(), snapshots, 30.0, thermal)
        groups = {item["family"]: item for item in result["activity_groups"]["rows"]}
        self.assertIn("gc", groups)
        self.assertIn("kworker", groups)
        self.assertGreater(groups["gc"]["power_delta_mw"], 0.0)
        self.assertEqual(groups["gc"]["maximum_temperature_c"], 43.0)
        self.assertEqual(groups["kworker"]["subsystem"], "storage")

    def test_test_item_matrix_tracks_gc_kworker_and_dex_overlap(self) -> None:
        samples = self._samples()
        snapshots = []
        for index in range(6):
            threads = []
            processes = []
            watched = []
            if index in {0, 1}:
                threads.append(
                    {
                        "pid": 88,
                        "tid": 88,
                        "state": "R",
                        "cpu_pct": 14.0,
                        "name": "kworker/u25:2-ufs",
                        "process": "[kworker/u25:2-ufs]",
                    }
                )
            if index in {2, 3}:
                threads.append(
                    {
                        "pid": 456,
                        "tid": 457,
                        "state": "R",
                        "cpu_pct": 28.0,
                        "name": "HeapTaskDaemon",
                        "process": "com.example.app",
                    }
                )
                dex = {
                    "pid": 900,
                    "name": "dex2oat64",
                    "command": "/apex/com.android.art/bin/dex2oat64",
                    "cpu_pct": 40.0,
                    "state": "R",
                    "watch_name": "dex2oat",
                    "watch_kind": "dex_optimization",
                    "watch_label": "DEX AOT 编译",
                    "activity_active": True,
                }
                processes.append(dex)
                watched.append(dex)
            snapshots.append(
                SystemSnapshot(
                    uptime_s=100.0 + index * 10,
                    host_epoch_s=1000.0 + index * 10,
                    processes=processes,
                    threads=threads,
                    watched_processes=watched,
                )
            )
        thermal_snapshots = [
            ThermalSnapshot(
                uptime_s=100.0 + index * 10,
                host_epoch_s=1000.0 + index * 10,
                status=1 if index == 3 else 0,
                temperatures=[{"name": "CPU", "value_c": 40.0 + index}],
            )
            for index in range(6)
        ]
        system = analyze_system_activity(samples, snapshots, 30.0, thermal_snapshots)
        thermal = analyze_thermal_history(samples, thermal_snapshots)
        scheduler = analyze_scheduler_history(samples, [])
        result = analyze_test_items(
            samples,
            [ContextSample(100.0, "com.example.app", ".MainActivity")],
            [
                ExternalEvent(100.0, "启动", "测试", "span", duration_s=20.0),
                ExternalEvent(120.0, "滚动", "测试", "span", duration_s=30.0),
            ],
            snapshots,
            system,
            thermal,
            scheduler,
            30.0,
            10.0,
        )
        rows = {item["name"]: item for item in result["rows"]}
        self.assertGreater(rows["启动"]["kworker"]["snapshot_count"], 0)
        self.assertGreater(rows["滚动"]["gc"]["snapshot_count"], 0)
        self.assertGreater(rows["滚动"]["dex_update_overlap_s"], 0.0)
        self.assertGreater(rows["滚动"]["energy_mwh"], rows["启动"]["energy_mwh"])

    def test_test_items_fall_back_to_merged_foreground_activity_windows(self) -> None:
        samples = self._samples()
        result = analyze_test_items(
            samples,
            [
                ContextSample(100.0, "com.example", ".MainActivity"),
                ContextSample(110.0, "com.example", ".MainActivity"),
                ContextSample(120.0, "com.example", ".VideoActivity"),
                ContextSample(130.0, "com.example", ".VideoActivity"),
            ],
            [],
            [],
            {"available": False, "timeline": [], "activity_groups": {}, "priority_activities": {}},
            {"timeline": []},
            {"timeline": []},
            30.0,
            10.0,
        )
        self.assertEqual(result["source_mode"], "foreground_activity")
        self.assertEqual(result["span_count"], 2)
        self.assertEqual({item["name"] for item in result["rows"]}, {".MainActivity", ".VideoActivity"})

    def test_power_test_items_ignore_explicitly_inactive_foreground_window(self) -> None:
        samples = self._samples()
        result = analyze_test_items(
            samples,
            [
                ContextSample(
                    100.0,
                    "com.ohos.sceneboard",
                    "EngineServiceAbility",
                    screen_state="SLEEP",
                )
            ],
            [],
            [],
            {"available": False, "timeline": [], "activity_groups": {}, "priority_activities": {}},
            {"timeline": []},
            {"timeline": []},
            30.0,
            10.0,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["source_mode"], "unavailable")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["spans"], [])

    def test_thermal_and_scheduler_histories(self) -> None:
        samples = self._samples()
        thermal = [
            ThermalSnapshot(
                uptime_s=100.0 + index * 20,
                host_epoch_s=1000.0 + index * 20,
                status=index,
                hal_ready=True,
                temperatures=[{"name": "CPU", "value_c": 40.0 + index * 5, "status": index}],
                cooling_devices=[{"name": "fan", "value": float(index)}],
                thresholds=[{"name": "CPU", "hot_c": [None, 50.0, 60.0]}],
            )
            for index in range(3)
        ]
        thermal_result = analyze_thermal_history(samples, thermal)
        self.assertTrue(thermal_result["throttling_observed"])
        self.assertEqual(thermal_result["maximum_status"], 2)
        self.assertEqual(thermal_result["sensors"][0]["first_hot_threshold_c"], 50.0)

        scheduler = [
            SchedulerSnapshot(
                uptime_s=100.0,
                host_epoch_s=1000.0,
                cpusets={"background": "0-3"},
                cpu_policies=[{"name": "policy0", "cpuinfo_min_khz": 300000, "cpuinfo_max_khz": 2000000, "status": "limits-only"}],
                hint_sessions=[{"uid": 1000, "pid": 123, "tids": [124], "target_duration_ns": 10_000_000}],
                availability={"hint_session_supported": True},
            )
        ]
        scheduler_result = analyze_scheduler_history(samples, scheduler)
        self.assertEqual(scheduler_result["cpusets"][0]["latest_cpus"], "0-3")
        self.assertEqual(scheduler_result["maximum_hint_session_count"], 1)
        self.assertFalse(scheduler_result["cpu_policies"][0]["runtime_controls_visible"])
        self.assertEqual(scheduler_result["timeline"][0]["cpuset_name"], "background")
        self.assertEqual(scheduler_result["timeline"][0]["cpuset_cpu_count"], 4)
        self.assertEqual(scheduler_result["timeline"][0]["top_app_process_count"], 0)
        self.assertEqual(scheduler_result["timeline"][0]["frozen_process_count"], 0)

    def test_brightness_throttling_groups_confirmed_points_and_recovery(self) -> None:
        samples = self._samples()
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example.game",
                foreground_activity=".GameActivity",
                screen_state="Awake",
            )
        ]

        def snapshot(
            uptime_s: float,
            effective: float,
            cap: float,
            applied: bool,
            cooling: float,
            maximum_reason: int,
        ) -> ThermalSnapshot:
            return ThermalSnapshot(
                uptime_s=uptime_s,
                host_epoch_s=1000.0 + uptime_s,
                status=2 if applied else 0,
                temperatures=[{"name": "skin", "value_c": 44.0 if applied else 38.0}],
                cooling_devices=[{"name": "lcd-backlight", "value": cooling}],
                display_brightness={
                    "available": True,
                    "screen_state": "ON",
                    "setting_raw": 204.0,
                    "setting_float": 0.8,
                    "current_screen_brightness": 0.8,
                    "last_user_set_brightness": 0.8,
                    "screen_brightness": effective,
                    "adjusted_brightness": effective,
                    "thermal_cap": cap,
                    "thermal_applied": applied,
                    "thermal_status": 3 if applied else 0,
                    "brightness_max_reason": maximum_reason,
                },
            )

        result = analyze_brightness_throttling(
            samples,
            contexts,
            [
                snapshot(100.0, 0.8, 1.0, False, 0.0, 0),
                snapshot(110.0, 0.58, 0.58, True, 1.0, 1),
                snapshot(120.0, 0.48, 0.48, True, 2.0, 1),
                snapshot(130.0, 0.8, 1.0, False, 0.0, 0),
            ],
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["point_count"], 2)
        self.assertEqual(result["confirmed_point_count"], 2)
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["events"][0]["point_count"], 2)
        self.assertTrue(result["events"][0]["setting_unchanged"])
        self.assertAlmostEqual(result["events"][0]["minimum_effective_raw_estimate"], 122.4)
        self.assertFalse(result["current_active"])
        self.assertEqual(result["current_state"]["status"], "none")
        self.assertEqual(result["points"][0]["foreground_package"], "com.example.game")

    def test_brightness_throttling_normalizes_oem_raw_range_before_hbm_placeholder(self) -> None:
        result = analyze_brightness_throttling(
            self._samples(),
            [
                ContextSample(
                    100.0,
                    foreground_package="com.example.game",
                    foreground_activity=".GameActivity",
                    screen_state="Awake",
                )
            ],
            [
                ThermalSnapshot(
                    uptime_s=100.0,
                    host_epoch_s=1100.0,
                    display_brightness={
                        "available": True,
                        "screen_state": "ON",
                        "setting_raw": 2500.0,
                        "screen_brightness": 2500.0,
                        "sdr_screen_brightness": 2500.0,
                        "current_screen_brightness": 2500.0,
                        "last_user_set_brightness": 2500.0,
                        "cached_brightness": 2500.0,
                        "adjusted_brightness": 2500.0,
                        "brightness_minimum": 2.0,
                        "brightness_maximum": 4095.0,
                        "brightness_max_reason": 0,
                        "hbm_brightness": 0.0,
                        "hbm_unthrottled_brightness": 0.0,
                        "hbm_current_maximum": 1.0,
                        "hbm_throttling_reason": "none",
                    },
                )
            ],
        )

        expected = (2500.0 - 2.0) / (4095.0 - 2.0)
        current = result["current_state"]
        self.assertAlmostEqual(current["requested_brightness"], expected)
        self.assertAlmostEqual(current["effective_brightness"], expected)
        self.assertEqual(
            current["effective_brightness_source"],
            "Display Power Controller",
        )
        self.assertAlmostEqual(current["effective_raw_estimate"], 2500.0)
        self.assertEqual(current["status"], "none")

    def test_vendor_brightness_limits_do_not_imply_throttling_while_inactive(self) -> None:
        result = analyze_brightness_throttling(
            self._samples(),
            [],
            [
                ThermalSnapshot(
                    uptime_s=100.0,
                    host_epoch_s=1100.0,
                    display_brightness={
                        "available": True,
                        "vendor_thermal_provider": "OplusFeatureTemperatureLimitBrightness",
                        "vendor_thermal_active": False,
                        "vendor_thermal_level": 0,
                        "vendor_thermal_limit_nits": 9999.0,
                        "vendor_thermal_temperature_c": 37.0,
                        "vendor_thermal_candidate_caps_nits": {
                            "1": 700.0,
                            "11": 100.0,
                        },
                    },
                ),
                ThermalSnapshot(
                    uptime_s=110.0,
                    host_epoch_s=1110.0,
                    display_brightness={
                        "available": True,
                        "vendor_thermal_provider": "OplusFeatureTemperatureLimitBrightness",
                        "vendor_thermal_active": False,
                        "vendor_thermal_level": 4,
                        "vendor_thermal_limit_nits": 450.0,
                        "vendor_thermal_temperature_c": 43.0,
                        "vendor_thermal_candidate_caps_nits": {
                            "1": 700.0,
                            "4": 450.0,
                            "11": 100.0,
                        },
                    },
                ),
            ],
        )

        self.assertTrue(result["available"])
        self.assertTrue(result["vendor_thermal_available"])
        self.assertEqual(result["point_count"], 0)
        self.assertEqual(result["vendor_thermal_confirmed_point_count"], 0)
        self.assertFalse(result["current_active"])
        self.assertEqual(result["current_state"]["status"], "none")
        self.assertFalse(result["current_state"]["vendor_thermal_active"])
        self.assertEqual(result["current_state"]["vendor_thermal_limit_nits"], 450.0)
        self.assertIsNone(result["current_state"]["vendor_thermal_cap_label"])
        self.assertEqual(result["current_state"]["confirmation_sources"], [])
        self.assertIn("runtime state", result["source"])
        self.assertIn("does not prove", result["limitations"])
        self.assertIn("temperature-to-level mapping", result["limitations"])

    def test_vendor_brightness_last_known_state_keeps_age_without_claiming_current_activation(self) -> None:
        result = analyze_brightness_throttling(
            self._samples(),
            [],
            [
                ThermalSnapshot(
                    uptime_s=100.0,
                    host_epoch_s=1100.0,
                    display_brightness={
                        "available": True,
                        "vendor_thermal_provider": "OplusFeatureTemperatureLimitBrightness",
                        "vendor_thermal_active": False,
                        "vendor_thermal_level": 0,
                        "vendor_thermal_limit_nits": 9999.0,
                        "vendor_thermal_temperature_c": 32.0,
                        "vendor_thermal_candidate_caps_nits": {"0": 9999.0, "1": 700.0},
                    },
                ),
                ThermalSnapshot(
                    uptime_s=110.0,
                    host_epoch_s=1110.0,
                    cooling_devices=[{"name": "lcd-backlight", "value": 0}],
                    display_brightness={},
                ),
            ],
        )

        current = result["current_state"]
        self.assertIsNone(current["vendor_thermal_active"])
        self.assertFalse(current["vendor_thermal_last_known_active"])
        self.assertTrue(current["vendor_thermal_state_carried_forward"])
        self.assertFalse(current["vendor_thermal_state_stale"])
        self.assertEqual(current["vendor_thermal_observed_age_s"], 10.0)
        self.assertEqual(current["vendor_thermal_level"], 0)
        self.assertEqual(current["vendor_thermal_limit_nits"], 9999.0)
        self.assertEqual(current["vendor_thermal_candidate_caps_nits"]["1"], 700.0)
        self.assertEqual(result["point_count"], 0)
        self.assertFalse(result["current_active"])

    def test_vendor_brightness_active_flag_is_explicit_throttling_evidence(self) -> None:
        result = analyze_brightness_throttling(
            self._samples(),
            [],
            [
                ThermalSnapshot(
                    uptime_s=100.0,
                    host_epoch_s=1100.0,
                    status=0,
                    cooling_devices=[],
                    display_brightness={
                        "available": True,
                        "screen_state": "ON",
                        "vendor_thermal_provider": "OplusFeatureTemperatureLimitBrightness",
                        "vendor_thermal_active": True,
                        "vendor_thermal_level": 7,
                        "vendor_thermal_limit_nits": 250.0,
                        "vendor_thermal_temperature_c": 46.5,
                        "vendor_thermal_candidate_caps_nits": {
                            "1": 700.0,
                            "7": 250.0,
                            "11": 100.0,
                        },
                    },
                )
            ],
        )

        self.assertEqual(result["point_count"], 1)
        self.assertEqual(result["confirmed_point_count"], 1)
        self.assertEqual(result["vendor_thermal_confirmed_point_count"], 1)
        self.assertTrue(result["current_active"])
        point = result["points"][0]
        self.assertEqual(point["status"], "confirmed")
        self.assertEqual(point["confidence"], "high")
        self.assertEqual(point["confirmation_sources"], ["vendor_runtime_active"])
        self.assertEqual(point["vendor_thermal_level"], 7)
        self.assertEqual(point["vendor_thermal_limit_nits"], 250.0)
        self.assertEqual(point["vendor_thermal_temperature_c"], 46.5)
        self.assertEqual(
            point["vendor_thermal_cap_label"],
            "系统标称上限 250 nit（非亮度计实测）",
        )
        self.assertIn("当前档位 7", point["reason"])
        self.assertIn("系统标称上限 250 nit（非亮度计实测）", point["reason"])
        self.assertEqual(result["events"][0]["vendor_thermal_levels"], [7])
        self.assertEqual(
            result["events"][0]["minimum_vendor_thermal_limit_nits"],
            250.0,
        )

    def test_bcl_vbat_status_does_not_trigger_thermal_shutdown(self) -> None:
        result = analyze_thermal_history(
            self._samples(),
            [
                ThermalSnapshot(
                    uptime_s=100.0,
                    host_epoch_s=1000.0,
                    status=0,
                    hal_ready=True,
                    temperatures=[
                        {"name": "CPU7", "value_c": 84.1, "type": 0, "status": 0},
                        {"name": "vbat", "value_c": 4.4, "type": 6, "status": 6},
                        {"name": "ibat", "value_c": 0.2, "type": 7, "status": 6},
                        {"name": "socd", "value_c": 65.0, "type": 8, "status": 6},
                    ],
                )
            ],
        )

        sensors = {item["name"]: item for item in result["sensors"]}
        self.assertEqual(result["maximum_status"], 0)
        self.assertEqual(result["maximum_status_label"], "none")
        self.assertFalse(result["throttling_observed"])
        self.assertEqual(result["hottest_sensor"]["name"], "CPU7")
        self.assertFalse(sensors["vbat"]["contributes_to_thermal_status"])
        self.assertEqual(sensors["vbat"]["maximum_status_label"], "not_applicable")
        self.assertEqual(sensors["vbat"]["unit"], "V")
        self.assertEqual(sensors["ibat"]["unit"], "A")
        self.assertFalse(sensors["ibat"]["contributes_to_thermal_status"])
        self.assertFalse(sensors["socd"]["contributes_to_thermal_status"])
        self.assertEqual(sensors["socd"]["unit"], "%")

    def test_missing_sensor_severity_remains_unknown(self) -> None:
        result = analyze_thermal_history(
            self._samples(),
            [
                ThermalSnapshot(
                    uptime_s=100.0,
                    host_epoch_s=1000.0,
                    status=None,
                    hal_ready=True,
                    temperatures=[
                        {
                            "name": "Battery",
                            "value_c": 34.0,
                            "type": "BATTERY",
                            "status": 0,
                            "status_available": False,
                        }
                    ],
                )
            ],
        )

        self.assertFalse(result["severity_available"])
        self.assertIsNone(result["maximum_status"])
        self.assertIsNone(result["throttling_observed"])
        self.assertIsNone(result["sensors"][0]["maximum_status"])
        self.assertEqual(result["sensors"][0]["maximum_status_label"], "unknown")


class FindingSelectionTests(unittest.TestCase):
    def test_power_findings_do_not_repeat_raw_power_statistics(self) -> None:
        findings = build_findings(
            {
                "platform": "android",
                "test_mode": "power",
                "summary": {
                    "power_valid_for_consumption": True,
                    "average_power_mw": 1250.0,
                    "average_current_ma": 320.0,
                    "p95_power_mw": 1800.0,
                },
                "gpu": {"frequency_available": True},
            }
        )

        self.assertNotIn("电池侧实测功率", [item["title"] for item in findings])

    def test_performance_findings_do_not_repeat_raw_power_statistics(self) -> None:
        findings = build_performance_findings(
            {
                "platform": "android",
                "performance": {},
                "render_performance": {
                    "power_recording": {"average_power_mw": 1250.0}
                },
                "brightness_throttling": {},
            }
        )

        self.assertNotIn(
            "测试窗口平均电池侧功率",
            [item["title"] for item in findings],
        )

    def test_performance_finding_interprets_tail_instability_with_observed_label(self) -> None:
        findings = build_performance_findings(
            {
                "platform": "android",
                "performance": {
                    "sampled_frame_rate_fps": 57.9,
                    "one_percent_low_fps": 12.3,
                    "frame_metric_p99_ms": 16.64,
                    "frame_issue_pct": 0.36,
                    "frame_issue_count": 25,
                    "frame_sample_count": 6927,
                    "frame_rate_label": "应用呈现帧率",
                    "frame_rate_unit": "FPS",
                },
                "render_performance": {},
                "brightness_throttling": {},
            }
        )

        self.assertEqual(findings[0]["title"], "尾部帧稳定性不足")
        self.assertIn("平均应用呈现帧率 57.9 FPS", findings[0]["detail"])
        self.assertIn("1% Low 12.3 FPS", findings[0]["detail"])
        self.assertIn("口径不同", findings[0]["detail"])
        self.assertNotIn("平均提交帧率", findings[0]["detail"])

    def test_performance_findings_do_not_repeat_a_lone_average_frame_rate(self) -> None:
        findings = build_performance_findings(
            {
                "platform": "android",
                "performance": {
                    "sampled_frame_rate_fps": 59.8,
                    "frame_rate_label": "应用 UI 帧提交速率",
                    "frame_rate_unit": "帧/s",
                },
                "render_performance": {},
                "brightness_throttling": {},
            }
        )

        self.assertEqual(findings, [])

    def test_performance_finding_can_conclude_stable_frame_pacing(self) -> None:
        findings = build_performance_findings(
            {
                "platform": "android",
                "performance": {
                    "sampled_frame_rate_fps": 60.0,
                    "one_percent_low_fps": 55.0,
                    "frame_metric_p99_ms": 17.2,
                    "frame_issue_pct": 0.4,
                    "frame_issue_count": 24,
                    "frame_sample_count": 6000,
                    "frame_rate_label": "应用呈现帧率",
                    "frame_rate_unit": "FPS",
                },
                "render_performance": {},
                "brightness_throttling": {},
            }
        )

        self.assertEqual(findings[0]["title"], "帧节奏整体稳定")
        self.assertIn("1% Low 保持率", findings[0]["detail"])

    def test_ios_findings_keep_observer_interpretation_not_raw_cpu_gpu_power(self) -> None:
        findings = build_performance_findings(
            {
                "platform": "ios",
                "summary": {
                    "average_cpu_pct": 22.0,
                    "maximum_cpu_pct": 51.0,
                    "average_collector_cpu_pct": 1.5,
                },
                "gpu": {"average_load_pct": 35.0, "maximum_load_pct": 70.0},
                "performance": {},
                "render_performance": {
                    "power_recording": {"average_power_mw": 2300.0}
                },
                "brightness_throttling": {},
            }
        )

        self.assertEqual(
            [item["title"] for item in findings],
            ["观察者相关进程 CPU 上界"],
        )


class StorageTests(unittest.TestCase):
    def test_csv_round_trip_preserves_current_semantics(self) -> None:
        sample = Sample(
            index=0,
            elapsed_s=0.0,
            uptime_s=1.0,
            current_ma=250.0,
            signed_current_ma=-250.0,
            voltage_mv=3800.0,
            power_mw=950.0,
            direction="discharging",
            cpu_pct=20.0,
            core_cpu_pct={"0": 20.0},
            cluster_cpu_pct={"policy0": 20.0},
            frequencies_mhz={"policy0": 1000.0},
            power_source="ios_power_telemetry_system_load",
            power_sample_age_s=7.5,
            collector_cpu_pct=6.4,
            memory_frequency_mhz=933.0,
            power_valid_for_consumption=True,
            external_power=False,
        )
        metadata = {"cpu_policies": [{"name": "policy0"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.csv"
            write_samples_csv(path, [sample], metadata)
            loaded = read_samples_csv(path)[0]
        self.assertEqual(loaded.current_ma, 250.0)
        self.assertEqual(loaded.signed_current_ma, -250.0)
        self.assertEqual(loaded.direction, "discharging")
        self.assertEqual(loaded.power_source, "ios_power_telemetry_system_load")
        self.assertEqual(loaded.power_sample_age_s, 7.5)
        self.assertEqual(loaded.collector_cpu_pct, 6.4)
        self.assertEqual(loaded.memory_frequency_mhz, 933.0)
        self.assertTrue(loaded.power_valid_for_consumption)
        self.assertFalse(loaded.external_power)

    def test_csv_and_json_round_trip_preserve_optional_power_validity(self) -> None:
        samples = [
            Sample(
                index=0,
                elapsed_s=0.0,
                uptime_s=10.0,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=1000.0,
                direction="discharging",
                cpu_pct=None,
                power_valid_for_consumption=True,
                external_power=False,
            ),
            Sample(
                index=1,
                elapsed_s=1.0,
                uptime_s=11.0,
                current_ma=500.0,
                signed_current_ma=500.0,
                voltage_mv=4000.0,
                power_mw=2000.0,
                direction="charging",
                cpu_pct=None,
                power_valid_for_consumption=False,
                external_power=True,
            ),
            Sample(
                index=2,
                elapsed_s=2.0,
                uptime_s=12.0,
                current_ma=0.0,
                signed_current_ma=0.0,
                voltage_mv=4000.0,
                power_mw=0.0,
                direction="unknown",
                cpu_pct=None,
                power_valid_for_consumption=None,
                external_power=None,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.csv"
            write_samples_csv(path, samples, {})
            csv_loaded = read_samples_csv(path)

        json_loaded = [
            Sample(**json.loads(json.dumps(sample_to_dict(sample))))
            for sample in samples
        ]
        expected = [(True, False), (False, True), (None, None)]
        self.assertEqual(
            [
                (sample.power_valid_for_consumption, sample.external_power)
                for sample in csv_loaded
            ],
            expected,
        )
        self.assertEqual(
            [
                (sample.power_valid_for_consumption, sample.external_power)
                for sample in json_loaded
            ],
            expected,
        )

    def test_jsonl_recovery_ignores_truncated_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "contexts.jsonl").write_text(
                '{"uptime_s":1.0,"foreground_package":"com.example"}\n{"uptime_s":',
                encoding="utf-8",
            )
            contexts = load_contexts(root)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].foreground_package, "com.example")

    def test_system_snapshot_journal_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RunJournal(root) as journal:
                journal.append_system_snapshot(
                    SystemSnapshot(1.0, 1000.0, processes=[{"pid": 1, "cpu_pct": 2.0}])
                )
                journal.append_thermal_snapshot(
                    ThermalSnapshot(
                        1.0,
                        1000.0,
                        status=1,
                        temperatures=[{"name": "CPU", "value_c": 50.0}],
                    )
                )
                journal.append_scheduler_snapshot(
                    SchedulerSnapshot(1.0, 1000.0, cpusets={"background": "0-3"})
                )
            systems = load_system_snapshots(root)
            thermal = load_thermal_snapshots(root)
            scheduler = load_scheduler_snapshots(root)
        self.assertEqual(systems[0].processes[0]["pid"], 1)
        self.assertEqual(thermal[0].status, 1)
        self.assertEqual(scheduler[0].cpusets["background"], "0-3")

    def test_evidence_archive_contains_hash_manifest_and_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "phone-a"
            (run_dir / "raw").mkdir(parents=True)
            (run_dir / "metadata.json").write_text(
                json.dumps({"title": "Phone A", "device": {"model": "A"}}),
                encoding="utf-8",
            )
            (run_dir / "raw" / "sampler-stream.txt").write_text("sample\n", encoding="utf-8")
            attachment = root / "btr2.log"
            attachment.write_text("BTR2 original log\n", encoding="utf-8")
            archive_path, manifest = create_evidence_archive(
                run_dir,
                root / "evidence.zip",
                [attachment],
            )
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                archived_manifest = json.loads(
                    archive.read("phone-a/evidence-manifest.json").decode("utf-8")
                )
        self.assertIn("phone-a/raw/sampler-stream.txt", names)
        self.assertIn("phone-a/attachments/external/btr2.log", names)
        self.assertEqual(archived_manifest["entry_count"], manifest["entry_count"])
        self.assertTrue(all(item.get("sha256") for item in archived_manifest["entries"]))


class ComparisonTests(unittest.TestCase):
    @staticmethod
    def _write_run(root: Path, name: str, power: float, rate: float) -> Path:
        run_dir = root / name
        run_dir.mkdir()
        metadata = {
            "title": name,
            "adb_serial": name,
            "device": {"brand": "Demo", "model": name, "android": "16", "soc_model": "SoC"},
            "sample_interval_s": 1.0,
            "battery_start": {"level_pct": 80, "temperature_c": 30.0},
            "capture_start": {"expected_context": "desktop", "note": "BTR2 later"},
        }
        analysis = {
            "summary": {
                "power_valid_for_consumption": True,
                "consumption_session_representative": True,
                "average_power_mw": power,
                "p95_power_mw": power * 1.2,
                "maximum_power_mw": power * 1.4,
                "energy_per_minute_mwh": rate,
                "average_current_ma": power / 3.8,
                "average_cpu_pct": 30.0,
                "maximum_cpu_pct": 60.0,
                "temperature_delta_c": 2.0,
                "coverage_pct": 100.0,
            },
            "display": {"active_refresh_hz": 60.0},
            "test_items": {
                "rows": [
                    {
                        "phase": "测试",
                        "name": f"视频播放 / {name}",
                        "comparison_key": "视频播放",
                        "duration_s": 60.0,
                        "power_valid_for_consumption": True,
                        "mwh_per_minute": rate,
                        "average_power_mw": power,
                        "p95_power_mw": power * 1.2,
                        "average_cpu_pct": 30.0,
                        "maximum_temperature_c": 40.0,
                        "gc": {"overlap_s": 6.0},
                        "kworker": {"overlap_s": 3.0},
                        "dex_update_overlap_s": 0.0,
                        "interference_level": "low",
                        "top_processes": [{"name": "com.example"}],
                    }
                ]
            },
        }
        (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (run_dir / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        write_samples_csv(
            run_dir / "samples.csv",
            [
                Sample(
                    0,
                    0.0,
                    100.0,
                    300.0,
                    -300.0,
                    3800.0,
                    power,
                    "discharging",
                    30.0,
                    power_valid_for_consumption=True,
                    external_power=False,
                ),
                Sample(
                    1,
                    1.0,
                    101.0,
                    300.0,
                    -300.0,
                    3800.0,
                    power,
                    "discharging",
                    30.0,
                    power_valid_for_consumption=True,
                    external_power=False,
                ),
            ],
            {},
        )
        return run_dir

    def test_two_phone_comparison_pairs_test_items_and_reports_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_a = self._write_run(root, "phone-a", 1000.0, 16.67)
            run_b = self._write_run(root, "phone-b", 1200.0, 20.0)
            comparison = build_run_comparison(run_a, run_b, "A", "B")
            report = build_comparison_html(comparison)
        average = next(item for item in comparison["summary_rows"] if item["key"] == "average_power_mw")
        self.assertEqual(average["delta"]["absolute"], 200.0)
        self.assertEqual(comparison["matched_test_item_count"], 1)
        self.assertEqual(comparison["test_items"][0]["lower_energy_rate"], "A")
        self.assertIn("双机续航与系统活动对比", report)
        self.assertIn("视频播放", report)

    def test_comparison_conditions_are_platform_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_a = self._write_run(root, "phone-a", 1000.0, 16.67)
            run_b = self._write_run(root, "phone-b", 900.0, 15.0)
            metadata_path = run_b / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "platform": "ios",
                    "device_id": "ios:00008150-TEST",
                    "device": {
                        "brand": "Apple",
                        "model": "iPhone",
                        "ios": "26.5.2",
                        "hardware": "V54AP",
                    },
                }
            )
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            report = build_comparison_html(build_run_comparison(run_a, run_b))

            metadata.update(
                {
                    "platform": "harmony",
                    "device_id": "harmony:TEST",
                    "device": {
                        "brand": "HUAWEI",
                        "model": "nova",
                        "harmony": "6.1.0",
                        "soc_model": "Kirin",
                    },
                }
            )
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            harmony_report = build_comparison_html(build_run_comparison(run_a, run_b))

        self.assertIn("设备标识", report)
        self.assertIn("系统 / 硬件", report)
        self.assertIn("ios:00008150-TEST", report)
        self.assertIn("iOS 26.5.2 / V54AP", report)
        self.assertNotIn("ADB serial", report)
        self.assertIn("HarmonyOS 6.1.0 / Kirin", harmony_report)

    def test_comparison_hides_power_and_winner_when_either_run_is_not_representative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_a = self._write_run(root, "phone-a", 1000.0, 16.67)
            run_b = self._write_run(root, "phone-b", 1200.0, 20.0)
            analysis_path = run_b / "analysis.json"
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            analysis["summary"]["power_valid_for_consumption"] = False
            analysis["summary"]["consumption_session_representative"] = False
            analysis["test_items"]["rows"][0]["power_valid_for_consumption"] = False
            analysis_path.write_text(json.dumps(analysis), encoding="utf-8")

            comparison = build_run_comparison(run_a, run_b, "A", "B")
            report = build_comparison_html(comparison)

        average = next(
            item for item in comparison["summary_rows"] if item["key"] == "average_power_mw"
        )
        self.assertFalse(comparison["power_comparison_available"])
        self.assertIsNone(average["a"])
        self.assertIsNone(average["b"])
        self.assertIsNone(average["delta"]["absolute"])
        self.assertFalse(comparison["test_items"][0]["power_comparison_available"])
        self.assertIsNone(comparison["test_items"][0]["lower_energy_rate"])
        self.assertIn("不比较", " ".join(comparison["warnings"]))
        self.assertNotIn("1000 / 1200 mW", report)


class ClockAndLogTests(unittest.TestCase):
    def test_clock_alignment_interpolates_offset(self) -> None:
        points = [
            ClockSyncPoint(1000.0, 10.0, 100.0, 10.0),
            ClockSyncPoint(1100.0, 110.0, 199.0, 12.0),
        ]
        self.assertAlmostEqual(host_epoch_to_device_uptime(1050.0, points), 149.5)

    def test_log_import_pairs_start_and_end(self) -> None:
        points = [ClockSyncPoint(1000.0, 10.0, 100.0, 10.0)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "run.log"
            rules_path = root / "rules.json"
            log_path.write_text(
                "1970-01-01 00:16:50 START video\n"
                "1970-01-01 00:17:00 END video\n",
                encoding="utf-8",
            )
            rules_path.write_text(
                json.dumps(
                    {
                        "timestamp": {
                            "regex": r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
                            "formats": ["%Y-%m-%d %H:%M:%S"],
                            "timezone": "UTC",
                        },
                        "rules": [
                            {
                                "regex": r"START (?P<name>\w+)",
                                "name": "{name}",
                                "phase": "test",
                                "kind": "start",
                                "key": "{name}",
                            },
                            {
                                "regex": r"END (?P<name>\w+)",
                                "name": "{name}",
                                "phase": "test",
                                "kind": "end",
                                "key": "{name}",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            events, stats = import_timestamped_log(log_path, rules_path, points)
        self.assertEqual(stats["event_count"], 1)
        self.assertEqual(events[0].kind, "span")
        self.assertAlmostEqual(events[0].device_uptime_s, 110.0)
        self.assertAlmostEqual(events[0].duration_s or 0.0, 10.0)

    def test_combined_btr2_events_can_be_filtered_by_phone_key(self) -> None:
        events = [
            ExternalEvent(100.0, "视频", "test", metadata={"phone_key": "phone1"}),
            ExternalEvent(100.0, "视频", "test", metadata={"phone_key": "phone2"}),
        ]
        filtered, filters = filter_events_by_metadata(events, ["phone_key=phone2"])
        self.assertEqual(filters, {"phone_key": "phone2"})
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].metadata["phone_key"], "phone2")


class StreamingTests(unittest.TestCase):
    class _FakeProcess:
        def __init__(self, stdout: str) -> None:
            self.stdout = io.StringIO(stdout)
            self.stderr = io.StringIO("")

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    def test_reconnect_keeps_stream_and_checkpoint(self) -> None:
        first = "S|0|10|300|3800|300|1|0|0|100|0|0|0|0\n"
        second = "S|0|11|320|3799|301|2|0|0|180|0|0|0|0\n"
        processes = [self._FakeProcess(first), self._FakeProcess(second)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RunJournal(root) as journal, patch(
                "mobile_profiler.collector._wait_for_device",
                side_effect=[True, True, False],
            ), patch(
                "mobile_profiler.collector.collect_clock_sync",
                return_value=None,
            ), patch(
                "mobile_profiler.collector._device_ready",
                return_value=False,
            ), patch(
                "mobile_profiler.collector.subprocess.Popen",
                side_effect=processes,
            ), patch(
                "mobile_profiler.collector.time.sleep",
                return_value=None,
            ):
                result = collect_streaming_session(
                    "adb",
                    "device",
                    60,
                    1.0,
                    [],
                    None,
                    journal,
                    reconnect_timeout_s=1.0,
                    performance_context_enabled=False,
                )
            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            stream = (root / "raw" / "sampler-stream.txt").read_text(encoding="utf-8")
        self.assertEqual(len(result.raw_samples), 2)
        self.assertGreaterEqual(result.reconnect_count, 1)
        self.assertEqual(checkpoint["sample_count"], 2)
        self.assertIn("S|0|10", stream)
        self.assertIn("S|0|11", stream)


class HarmonyAdapterTests(unittest.TestCase):
    def test_harmony_native_cpu_frequency_uses_independent_low_interference_cadence(self) -> None:
        self.assertEqual(HARMONY_NATIVE_CPU_FREQUENCY_INTERVAL_S, 30.0)
        self.assertEqual(harmony_native_cpu_frequency_schedule_s(5.0, False), 30.0)
        self.assertEqual(harmony_native_cpu_frequency_schedule_s(60.0, False), 30.0)
        self.assertEqual(harmony_native_cpu_frequency_schedule_s(5.0, True), 5.0)
        self.assertEqual(harmony_native_cpu_frequency_schedule_s(60.0, True), 30.0)

    def test_hdc_targets_ignore_uart_and_prefix_device_ids(self) -> None:
        devices = parse_hdc_targets(
            "192.168.21.8:8710\t\tTCP\tConnected\tlocalhost\thdc\n"
            "7DZ9K26524002289\t\tUSB\tOffline\tlocalhost\thdc\n"
            "COM3\t\tUART\tReady\tunknown\thdc\n"
        )
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["serial"], "harmony:192.168.21.8:8710")
        self.assertEqual(devices[0]["state"], "device")
        self.assertEqual(devices[0]["connection_type"], "wireless")
        self.assertEqual(devices[1]["state"], "offline")

    def test_harmony_battery_frequency_thermal_and_foreground_parsers(self) -> None:
        battery = parse_harmony_battery(
            "capacity: 90\nchargingStatus: 0\npluggedType: 0\n"
            "voltage: 4321032\nnowCurrent: -149\ncurrentAverage: -113\n"
            "temperature: 280\npresent: 1\ntechnology: Li-poly\n"
        )
        self.assertEqual(battery["level_pct"], 90.0)
        self.assertAlmostEqual(battery["voltage_mv"], 4321.032)
        self.assertEqual(battery["current_now_ma"], -149.0)
        self.assertEqual(battery["temperature_c"], 28.0)
        self.assertEqual(battery["powered"], [])
        self.assertEqual(battery["status"], "discharging")

        cpufreq = parse_harmony_cpufreq(
            "cmd is: cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq\n"
            "1133000\n"
            "cmd is: cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq\n"
            "1500000\n"
            "cmd is: cat /sys/devices/system/cpu/cpu1/cpufreq/cpuinfo_cur_freq\n"
            "1306000\n"
            "cmd is: cat /sys/devices/system/cpu/cpu1/cpufreq/cpuinfo_max_freq\n"
            "1500000\n"
            "cmd is: cat /sys/devices/system/cpu/cpu2/cpufreq/cpuinfo_cur_freq\n"
            "2050000\n"
            "cmd is: cat /sys/devices/system/cpu/cpu2/cpufreq/cpuinfo_max_freq\n"
            "2050000\n"
        )
        policies = harmony_cpu_policies(cpufreq)
        self.assertEqual([item.cores for item in policies], [[0, 1], [2]])
        frequencies = harmony_policy_frequencies_mhz(cpufreq, policies)
        self.assertAlmostEqual(frequencies["policy0"], 1219.5)
        self.assertAlmostEqual(frequencies["policy1"], 2050.0)

        temperatures = parse_harmony_thermal(
            "Type: Battery\nTemperature: 28000\n"
            "Type: modem\nTemperature: 30\n"
        )
        self.assertEqual(temperatures[0]["value_c"], 28.0)
        self.assertEqual(temperatures[1]["value_c"], 30.0)

        foreground = parse_harmony_foreground(
            "AbilityRecord ID #247\n"
            "  main name [EntryAbility]\n"
            "  bundle name [yylx.danmaku.bili]\n"
            "  state #FOREGROUND\n"
        )
        self.assertEqual(foreground["package"], "yylx.danmaku.bili")
        self.assertEqual(foreground["activity"], "EntryAbility")

    def test_harmony_display_frame_window_touch_and_gpu_parsers(self) -> None:
        screen = parse_harmony_render_screen(
            "screen[0]: id=0, powerStatus=POWER_STATUS_ON, backlight=23707, "
            "screenType=EXTERNAL_TYPE, render resolution=1320x2856, physical resolution=1320x2856\n"
            "supportedMode[0]: 1320x2856, refreshRate=120\n"
            "supportedMode[1]: 1320x2856, refreshRate=60\n"
            "activeMode: 1320x2856, refreshRate=60\n"
        )
        self.assertEqual(screen["display_width_px"], 1320)
        self.assertEqual(screen["refresh_rate_hz"], 60.0)
        self.assertEqual(screen["supported_refresh_rates_hz"], [60.0, 120.0])
        self.assertEqual(
            parse_harmony_refresh_counts(
                "Refresh Rate:60, Count:320565;\nRefresh Rate:120, Count:6597;"
            ),
            {"60": 320565, "120": 6597},
        )

        frame = parse_harmony_compositor_fps(
            "The fps of screen [Id:0] is:\n"
            "12066666668\n12050000001\n12033333334\n12000000000\n",
            60.0,
            display_active=True,
        )
        self.assertEqual(frame["frame_sample_count"], 3)
        self.assertEqual(frame["missed_vsync_interval_count"], 1)
        self.assertGreater(float(frame["frame_interval_p95_ms"]), 30.0)
        self.assertAlmostEqual(float(frame["one_percent_low_fps"]), 30.0, places=1)
        self.assertEqual(
            parse_harmony_compositor_fps(
                "12066666668\n12050000001\n",
                60.0,
                display_active=None,
            ),
            {},
        )

        window = parse_harmony_window_manager(
            "WindowName DisplayId Pid WinId Type Mode Flag ZOrd Orientation [ x y w h ]\n"
            "bili0 0 21799 108 1 1 0 101 0 [ 0 0 1320 2856 ]\n"
            "Focus window: 108\n"
        )
        self.assertEqual(window["foreground_window_name"], "bili0")
        self.assertEqual(window["foreground_window_pid"], 21799)
        self.assertEqual(
            parse_harmony_hitches(
                "more than 66 ms 1\nmore than 33 ms 2\nmore than 16.67 ms 3\n"
            )["hitch_over_16_67ms"],
            3,
        )

        touch = parse_harmony_input_events(
            "{eventType:pointer,actionTime:100,deviceId:1,sourceType:touch-screen,pointerAction:down}\n"
            "{eventType:pointer,actionTime:101,deviceId:1,sourceType:touch-screen,pointerAction:up}\n"
            "{eventType:pointer,actionTime:100,deviceId:1,sourceType:touch-screen,pointerId:20000,pointerAction:down}\n"
        )
        self.assertEqual(touch["touch_event_count"], 2)
        self.assertEqual(touch["touch_interaction_count"], 1)
        devices = parse_harmony_input_devices(
            "deviceId:1 | deviceName:input_mt_wrapper | deviceType:19 | bus:0\n"
            "axis: count=2\n"
            " axisType:POSITION_X | minimum:0 | maximum:10559 | fuzz:0\n"
            " axisType:POSITION_Y | minimum:0 | maximum:22847 | fuzz:0\n"
        )
        self.assertEqual(devices[0]["name"], "input_mt_wrapper")
        self.assertEqual(len(devices[0]["axes"]), 2)
        self.assertEqual(
            parse_harmony_gles("GL_VENDOR: HUAWEI\nGL_RENDERER: Maleoon 920C\n")["renderer"],
            "Maleoon 920C",
        )

    def test_harmony_performance_context_analysis_tracks_residency_frames_and_touches(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                screen_state="Awake",
                refresh_rate_hz=60.0,
                performance={
                    "refresh_rate_counts": {"60": 100, "120": 50},
                    "frame_sample_count": 100,
                    "compositor_fps": 59.8,
                    "frame_interval_average_ms": 16.72,
                    "frame_interval_p95_ms": 17.0,
                    "missed_vsync_interval_count": 1,
                    "touch_down_times_us": [1, 2],
                    "display_width_px": 1320,
                    "display_height_px": 2856,
                },
            ),
            ContextSample(
                110.0,
                screen_state="Awake",
                refresh_rate_hz=120.0,
                performance={
                    "refresh_rate_counts": {"60": 700, "120": 650},
                    "frame_sample_count": 100,
                    "compositor_fps": 118.0,
                    "frame_interval_average_ms": 8.47,
                    "frame_interval_p95_ms": 16.8,
                    "missed_vsync_interval_count": 2,
                    "touch_down_times_us": [1, 2, 3, 4],
                },
            ),
        ]
        result = analyze_performance_contexts(
            contexts,
            {"performance_probe": {"supported_refresh_rates_hz": [60, 120], "gpu_renderer": "Maleoon 920C"}},
        )
        self.assertEqual(result["current_refresh_rate_hz"], 120.0)
        self.assertEqual(result["touch_interaction_count"], 2)
        self.assertEqual(result["frame_sample_count"], 200)
        self.assertAlmostEqual(result["missed_vsync_interval_pct"], 1.5)
        residency = {row["refresh_rate_hz"]: row for row in result["refresh_residency"]}
        self.assertAlmostEqual(residency[60.0]["share_pct"], 66.666, places=2)
        self.assertEqual(result["gpu_renderer"], "Maleoon 920C")
        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(flow["app_submission"]["status"], "unavailable")
        self.assertEqual(flow["surface_present"]["status"], "primary")
        self.assertAlmostEqual(flow["surface_present"]["value"], 88.9, places=1)
        self.assertEqual(flow["display_scanout"]["status"], "reference")
        self.assertEqual(
            [(item["uptime_s"], item["value"]) for item in result["refresh_rate_timeline"]],
            [(100.0, 60.0), (110.0, 120.0)],
        )
        self.assertEqual(flow["app_submission"]["timeline"], [])
        self.assertEqual(flow["render_queue"]["timeline"], [])
        self.assertEqual(
            [item["value"] for item in flow["surface_present"]["timeline"]],
            [59.8, 118.0],
        )
        self.assertEqual(
            [item["value"] for item in flow["display_scanout"]["timeline"]],
            [60.0, 120.0],
        )
        self.assertEqual(result["frame_flow"]["timeline_stage_count"], 2)
        self.assertFalse(result["render_pipeline"]["available"])
        self.assertEqual(result["render_pipeline"]["stages"], [])
        self.assertEqual(result["render_pipeline"]["timeline"], [])
        self.assertNotIn("Android", result["render_pipeline"]["limitations"])
        self.assertNotIn("gfxinfo", result["render_pipeline"]["limitations"])

    def test_harmony_refresh_residency_uses_only_adjacent_active_counter_windows(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                screen_state="Awake",
                refresh_rate_hz=60.0,
                source="harmony_ability_render_service",
                performance={"refresh_rate_counts": {"60": 100, "120": 50}},
            ),
            ContextSample(
                110.0,
                screen_state="off",
                refresh_rate_hz=60.0,
                source="harmony_ability_render_service",
                performance={"refresh_rate_counts": {"60": 700, "120": 650}},
            ),
            ContextSample(
                120.0,
                screen_state="on",
                refresh_rate_hz=120.0,
                source="harmony_ability_render_service",
                performance={"refresh_rate_counts": {"60": 1300, "120": 1250}},
            ),
            ContextSample(
                125.0,
                screen_state="on",
                refresh_rate_hz=120.0,
                source="harmony_smartperf",
                performance={
                    "smartperf_source": "SP_daemon",
                    "refresh_rate_counts": {"60": 100, "120": 50},
                },
            ),
            ContextSample(
                130.0,
                screen_state="Awake",
                refresh_rate_hz=120.0,
                source="harmony_ability_render_service",
                performance={"refresh_rate_counts": {"60": 1600, "120": 1850}},
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "harmony"})

        residency = {row["refresh_rate_hz"]: row for row in result["refresh_residency"]}
        self.assertEqual(residency[60.0]["count"], 300)
        self.assertEqual(residency[120.0]["count"], 600)
        self.assertAlmostEqual(residency[60.0]["estimated_duration_s"], 5.0)
        self.assertAlmostEqual(residency[120.0]["estimated_duration_s"], 5.0)
        self.assertAlmostEqual(residency[60.0]["share_pct"], 50.0)
        self.assertAlmostEqual(residency[120.0]["share_pct"], 50.0)

    def test_harmony_analyze_run_clips_contexts_to_primary_sample_window(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=100.0,
                signed_current_ma=-100.0,
                voltage_mv=4000.0,
                power_mw=400.0,
                direction="discharging",
                cpu_pct=20.0,
                power_source="harmony_smartperf_battery",
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(5)
        ]
        contexts = [
            ContextSample(
                99.0,
                foreground_package="pre.window",
                screen_state="on",
                refresh_rate_hz=90.0,
                source="harmony_ability_render_service",
                performance={
                    "platform": "harmony",
                    "foreground_window_name": "pre-window",
                },
            ),
            ContextSample(
                100.0,
                foreground_package="com.example.game",
                screen_state="on",
                refresh_rate_hz=60.0,
                source="harmony_smartperf",
                performance={
                    "platform": "harmony",
                    "smartperf_source": "SP_daemon",
                    "foreground_window_name": "game-start",
                    "frame_sample_count": 60,
                    "compositor_fps": 60.0,
                },
            ),
            ContextSample(
                104.0,
                foreground_package="com.example.game",
                screen_state="on",
                refresh_rate_hz=60.0,
                source="harmony_smartperf",
                performance={
                    "platform": "harmony",
                    "smartperf_source": "SP_daemon",
                    "foreground_window_name": "game-end",
                    "frame_sample_count": 60,
                    "compositor_fps": 60.0,
                },
            ),
            ContextSample(
                105.0,
                foreground_package="com.ohos.sceneboard",
                screen_state="off",
                refresh_rate_hz=120.0,
                source="harmony_ability_render_service",
                performance={
                    "platform": "harmony",
                    "foreground_window_name": "post-window",
                    "frame_data_available": False,
                    "frame_unavailable_reason": "display is inactive",
                },
            ),
        ]
        metadata = {
            "platform": "harmony",
            "test_mode": "performance",
            "sample_interval_s": 1.0,
            "capture_configuration": resolve_capture_configuration(
                "performance",
                "harmony",
                "harmony-smartperf",
            ),
        }

        result = analyze_run(samples, metadata, {}, [], contexts=contexts)

        performance = result["performance"]
        self.assertEqual(performance["context_sample_count"], 2)
        self.assertEqual(performance["foreground_window_name"], "game-end")
        self.assertEqual(performance["current_refresh_rate_hz"], 60.0)
        self.assertEqual(
            [row["uptime_s"] for row in performance["frame_rate_timeline"]],
            [100.0, 104.0],
        )

    def test_android_performance_context_analysis_uses_counter_deltas(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                foreground_activity=".MainActivity",
                refresh_rate_hz=60.0,
                performance={
                    "foreground_window_name": "com.example/com.example.MainActivity",
                    "refresh_rate_durations_s": {"60": 100.0, "120": 50.0},
                    "frame_counter_total": 100,
                    "frame_counter_deadline_missed": 2,
                    "frame_counter_missed_vsync": 1,
                    "frame_counter_janky": 3,
                    "frame_histogram_ms": {"10": 80, "20": 20},
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                foreground_activity=".MainActivity",
                refresh_rate_hz=60.0,
                performance={
                    "foreground_window_name": "com.example/com.example.MainActivity",
                    "refresh_rate_durations_s": {"60": 108.0, "120": 52.0},
                    "frame_counter_total": 160,
                    "frame_counter_deadline_missed": 5,
                    "frame_counter_missed_vsync": 2,
                    "frame_counter_janky": 5,
                    "frame_histogram_ms": {"10": 120, "20": 40},
                    "display_width_px": 1260,
                    "display_height_px": 2800,
                },
            ),
        ]
        result = analyze_performance_contexts(
            contexts,
            {"performance_probe": {"supported_refresh_rates_hz": [60, 90, 120]}},
        )
        self.assertAlmostEqual(result["sampled_frame_rate_fps"], 6.0)
        self.assertEqual(result["frame_sample_count"], 60)
        self.assertEqual(result["frame_metric_p95_ms"], 20.0)
        self.assertEqual(result["frame_metric_p99_ms"], 20.0)
        self.assertEqual(result["one_percent_low_fps"], 50.0)
        self.assertEqual(result["one_percent_low_confidence"], "high")
        self.assertEqual(len(result["frame_rate_timeline"]), 1)
        self.assertAlmostEqual(result["frame_rate_timeline"][0]["frame_rate_fps"], 6.0)
        self.assertEqual(result["frame_deadline_missed_count"], 3)
        self.assertAlmostEqual(result["frame_issue_pct"], 5.0)
        self.assertEqual(result["touch_interaction_count"], None)
        residency = {row["refresh_rate_hz"]: row for row in result["refresh_residency"]}
        self.assertAlmostEqual(residency[60.0]["share_pct"], 80.0)
        self.assertAlmostEqual(residency[120.0]["share_pct"], 20.0)
        self.assertEqual(
            result["refresh_residency_source"],
            "Android SurfaceFlinger refresh-rate duration delta",
        )
        self.assertEqual(
            result["refresh_rate_timeline_source"],
            "Android DisplayManager active display mode",
        )
        self.assertTrue(
            all(
                item["source"] == "Android DisplayManager active display mode"
                for item in result["refresh_rate_timeline"]
            )
        )
        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(
            flow["display_scanout"]["source"],
            "Android DisplayManager active display mode",
        )
        self.assertIsNone(result["render_width_px"])
        self.assertIsNone(result["render_height_px"])
        self.assertFalse(result["render_resolution_available"])
        self.assertFalse(result["render_resolution_estimated"])
        self.assertEqual(result["frame_flow"]["primary_key"], "app_submission")
        self.assertEqual(flow["app_submission"]["status"], "primary")
        self.assertAlmostEqual(flow["app_submission"]["value"], 6.0)
        self.assertEqual(flow["surface_present"]["status"], "unavailable")
        self.assertEqual(flow["display_scanout"]["status"], "reference")
        self.assertEqual(
            [(item["uptime_s"], item["value"]) for item in flow["app_submission"]["timeline"]],
            [(110.0, 6.0)],
        )
        self.assertEqual(flow["render_queue"]["timeline"], [])
        self.assertEqual(flow["surface_present"]["timeline"], [])
        self.assertEqual(
            [item["value"] for item in flow["display_scanout"]["timeline"]],
            [60.0, 60.0],
        )
        self.assertEqual(result["frame_flow"]["timeline_stage_count"], 2)

    def test_performance_context_report_break_does_not_bridge_deleted_range(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                screen_state="on",
                refresh_rate_hz=120.0,
                performance={
                    "frame_counter_total": 100,
                    "refresh_rate_durations_s": {"60": 10.0, "120": 90.0},
                },
            ),
            ContextSample(
                105.0,
                foreground_package="com.example",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={
                    "frame_counter_total": 600,
                    "refresh_rate_durations_s": {"60": 12.0, "120": 93.0},
                    "report_break_before": True,
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={
                    "frame_counter_total": 700,
                    "refresh_rate_durations_s": {"60": 17.0, "120": 93.0},
                },
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "android"})

        self.assertEqual(result["frame_sample_count"], 100)
        self.assertAlmostEqual(result["sampled_frame_rate_fps"], 20.0)
        self.assertEqual(len(result["frame_rate_timeline"]), 1)
        self.assertTrue(result["frame_rate_timeline"][0]["report_break_before"])
        self.assertEqual(
            result["refresh_residency"],
            [
                {
                    "refresh_rate_hz": 60.0,
                    "count": None,
                    "estimated_duration_s": 5.0,
                    "share_pct": 100.0,
                }
            ],
        )

    def test_counter_window_fps_p1_is_not_reported_as_standard_one_percent_low(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={"frame_counter_total": 0},
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={"frame_counter_total": 600},
            ),
            ContextSample(
                120.0,
                foreground_package="com.example",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={"frame_counter_total": 900},
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "android"})

        self.assertIsNone(result["one_percent_low_fps"])
        self.assertIsNone(result["one_percent_low_source"])
        self.assertFalse(result["one_percent_low_standard"])
        self.assertAlmostEqual(result["window_frame_rate_p1_reference_fps"], 30.0, places=1)
        self.assertIn(
            "reference only",
            result["window_frame_rate_p1_reference_source"],
        )

    def test_harmony_native_compositor_windows_do_not_claim_application_one_percent_low(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.ohos.sceneboard",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={
                    "frame_sample_count": 60,
                    "compositor_fps": 60.0,
                    "render_service_compositor_source": True,
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.ohos.sceneboard",
                screen_state="on",
                refresh_rate_hz=60.0,
                performance={
                    "frame_sample_count": 60,
                    "compositor_fps": 45.0,
                    "render_service_compositor_source": True,
                },
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "harmony"})

        self.assertIsNone(result["one_percent_low_fps"])
        self.assertIsNone(result["one_percent_low_source"])
        self.assertFalse(result["one_percent_low_standard"])
        self.assertAlmostEqual(result["window_frame_rate_p1_reference_fps"], 45.0, places=1)

    def test_harmony_frame_flow_keeps_smartperf_and_render_service_timelines_separate(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                screen_state="on",
                refresh_rate_hz=60.0,
                source="harmony_smartperf",
                performance={
                    "platform": "harmony",
                    "smartperf_source": "SP_daemon",
                    "frame_sample_count": 58,
                    "compositor_fps": 58.0,
                },
            ),
            ContextSample(
                101.0,
                screen_state="on",
                refresh_rate_hz=60.0,
                source="harmony_ability_render_service",
                performance={
                    "platform": "harmony",
                    "render_service_compositor_source": True,
                    "frame_sample_count": 60,
                    "compositor_fps": 60.0,
                },
            ),
            ContextSample(
                102.0,
                screen_state="on",
                refresh_rate_hz=120.0,
                source="harmony_smartperf",
                performance={
                    "platform": "harmony",
                    "smartperf_source": "SP_daemon",
                    "frame_sample_count": 57,
                    "compositor_fps": 57.0,
                },
            ),
            ContextSample(
                103.0,
                screen_state="on",
                refresh_rate_hz=120.0,
                source="harmony_ability_render_service",
                performance={
                    "platform": "harmony",
                    "render_service_compositor_source": True,
                    "frame_sample_count": 59,
                    "compositor_fps": 59.0,
                },
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "harmony"})

        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(
            [item["value"] for item in flow["app_submission"]["timeline"]],
            [58.0, 57.0],
        )
        self.assertEqual(
            [item["value"] for item in flow["surface_present"]["timeline"]],
            [60.0, 59.0],
        )
        self.assertEqual(
            [item["frame_rate_fps"] for item in result["frame_rate_timeline"]],
            [58.0, 57.0],
        )
        self.assertEqual(
            [(item["uptime_s"], item["value"]) for item in result["refresh_rate_timeline"]],
            [(101.0, 60.0), (103.0, 120.0)],
        )
        self.assertEqual(flow["render_queue"]["timeline"], [])

    def test_harmony_unknown_screen_state_suppresses_cached_frame_and_refresh_data(self) -> None:
        result = analyze_performance_contexts(
            [
                ContextSample(
                    100.0,
                    screen_state=None,
                    refresh_rate_hz=120.0,
                    source="harmony_ability_render_service",
                    performance={
                        "platform": "harmony",
                        "render_service_compositor_source": True,
                        "frame_sample_count": 60,
                        "compositor_fps": 60.0,
                    },
                )
            ],
            {"platform": "harmony"},
        )

        self.assertIsNone(result["current_refresh_rate_hz"])
        self.assertEqual(result["refresh_rate_timeline"], [])
        self.assertEqual(result["frame_rate_timeline"], [])
        self.assertIn("无法确认当前屏幕状态", result["refresh_rate_unavailable_reason"])

    def test_harmony_frame_and_refresh_timelines_exclude_asleep_contexts(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                screen_state="Awake",
                refresh_rate_hz=60.0,
                source="harmony_ability_render_service",
                performance={
                    "platform": "harmony",
                    "render_service_compositor_source": True,
                    "frame_sample_count": 60,
                    "compositor_fps": 60.0,
                },
            ),
            ContextSample(
                105.0,
                screen_state="Asleep",
                refresh_rate_hz=120.0,
                source="harmony_ability_render_service",
                performance={
                    "platform": "harmony",
                    "render_service_compositor_source": True,
                    "frame_sample_count": 5,
                    "compositor_fps": 1.0,
                },
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "harmony"})

        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(result["current_refresh_rate_hz"], 60.0)
        self.assertEqual(result["observed_refresh_rates_hz"], [60.0])
        self.assertEqual(result["refresh_switch_count"], 0)
        self.assertEqual(
            [item["frame_rate_fps"] for item in result["frame_rate_timeline"]],
            [60.0],
        )
        self.assertEqual(
            [item["value"] for item in flow["surface_present"]["timeline"]],
            [60.0],
        )
        self.assertEqual(
            [item["value"] for item in flow["display_scanout"]["timeline"]],
            [60.0],
        )

    def test_refresh_timeline_compression_preserves_short_rate_switches(self) -> None:
        contexts = [
            ContextSample(
                float(uptime),
                screen_state="Awake",
                refresh_rate_hz=float(rate),
                source="harmony_ability_render_service",
                performance={"platform": "harmony"},
            )
            for uptime, rate in (
                (100, 60),
                (101, 60),
                (102, 120),
                (103, 60),
                (104, 60),
            )
        ]

        result = analyze_performance_contexts(contexts, {"platform": "harmony"})

        self.assertEqual(
            [(item["uptime_s"], item["value"]) for item in result["refresh_rate_timeline"]],
            [(100.0, 60.0), (102.0, 120.0), (103.0, 60.0), (104.0, 60.0)],
        )
        self.assertEqual(
            result["refresh_rate_timeline_source"],
            "HarmonyOS RenderService current refresh rate",
        )

    def test_android_surface_frames_override_static_gfxinfo_counter(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.hottagames.yh.laohu",
                foreground_activity="com.epicgames.unreal.GameActivity",
                screen_state="Awake",
                refresh_rate_hz=120.0,
                performance={
                    "platform": "android",
                    "foreground_window_name": "com.hottagames.yh.laohu/"
                    "com.epicgames.unreal.GameActivity",
                    "frame_counter_total": 35,
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.hottagames.yh.laohu",
                foreground_activity="com.epicgames.unreal.GameActivity",
                screen_state="Awake",
                refresh_rate_hz=120.0,
                performance={
                    "platform": "android",
                    "foreground_window_name": "com.hottagames.yh.laohu/"
                    "com.epicgames.unreal.GameActivity",
                    "frame_counter_total": 35,
                    "surface_frame_source": True,
                    "frame_sample_count": 600,
                    "compositor_fps": 60.07,
                    "frame_interval_average_ms": 16.647,
                    "frame_interval_p95_ms": 16.674,
                    "frame_interval_p99_ms": 16.744,
                    "one_percent_low_fps": 52.61,
                    "missed_vsync_interval_count": 2,
                },
            ),
        ]
        result = analyze_performance_contexts(contexts, {"platform": "android"})
        self.assertAlmostEqual(result["sampled_frame_rate_fps"], 60.07)
        self.assertAlmostEqual(result["one_percent_low_fps"], 52.61)
        self.assertEqual(result["frame_sample_count"], 600)
        self.assertEqual(result["frame_rate_label"], "应用呈现帧率")
        self.assertEqual(result["frame_rate_timeline"][0]["frame_issue_count"], 2)
        self.assertEqual(
            result["frame_source"],
            "Android SurfaceFlinger foreground application-layer present timestamps",
        )
        self.assertEqual(len(result["frame_rate_timeline"]), 1)
        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(result["frame_flow"]["primary_key"], "surface_present")
        self.assertEqual(flow["app_submission"]["status"], "invalid")
        self.assertIsNone(flow["app_submission"]["value"])
        self.assertEqual(flow["app_submission"]["metrics"], [])
        self.assertEqual(flow["app_submission"]["timeline"], [])
        self.assertEqual(flow["app_submission"]["confidence"], "")
        self.assertEqual(flow["surface_present"]["status"], "primary")
        self.assertAlmostEqual(flow["surface_present"]["value"], 60.07)
        self.assertEqual(
            [item["value"] for item in flow["surface_present"]["timeline"]],
            [60.07],
        )
        self.assertEqual(
            [item["value"] for item in flow["display_scanout"]["timeline"]],
            [120.0, 120.0],
        )

    def test_android_frame_flow_explains_disabled_framestats_without_hiding_base_frames(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                screen_state="on",
                refresh_rate_hz=60.0,
                source="android-performance-context",
                performance={
                    "platform": "android",
                    "surface_frame_source": True,
                    "frame_sample_count": 120,
                    "compositor_fps": 60.0,
                    "frame_interval_p95_ms": 16.8,
                    "frame_interval_p99_ms": 20.0,
                    "missed_vsync_interval_count": 1,
                },
            )
        ]
        result = analyze_performance_contexts(
            contexts,
            {
                "platform": "android",
                "capture_configuration": {
                    "features": {"frame_rate": True, "frame_details": False},
                },
            },
        )

        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(flow["surface_present"]["status"], "primary")
        self.assertIn("framestats 采集未启用", flow["render_queue"]["detail"])
        self.assertIn("P95/P99", flow["render_queue"]["detail"])
        self.assertNotIn("未产生新增", flow["render_queue"]["detail"])

    def test_realtime_one_percent_low_uses_rolling_ten_second_intervals(self) -> None:
        contexts = []
        for index in range(6):
            intervals = [16.666667] * 119 + ([50.0] if index == 5 else [16.666667])
            average_interval = sum(intervals) / len(intervals)
            contexts.append(
                ContextSample(
                    100.0 + index * 2.0,
                    screen_state="on",
                    refresh_rate_hz=60.0,
                    source="android-performance-context",
                    performance={
                        "platform": "android",
                        "surface_frame_source": True,
                        "frame_sample_count": len(intervals),
                        "compositor_fps": 1000.0 / average_interval,
                        "frame_interval_average_ms": average_interval,
                        "frame_interval_p95_ms": 16.666667,
                        "frame_interval_p99_ms": 16.666667,
                        "frame_intervals_ms": intervals,
                    },
                )
            )

        result = analyze_performance_contexts(contexts, {"platform": "android"})
        timeline = result["frame_rate_timeline"]

        self.assertIsNone(timeline[0]["one_percent_low_fps"])
        self.assertIsNone(timeline[2]["one_percent_low_fps"])
        self.assertIsNotNone(timeline[3]["one_percent_low_fps"])
        self.assertGreaterEqual(timeline[-1]["one_percent_low_window_s"], 9.9)
        self.assertGreaterEqual(timeline[-1]["one_percent_low_sample_count"], 500)
        self.assertEqual(result["one_percent_low_timeline_label"], "滚动 10 秒 1% Low")
        self.assertIn("rolling 10-second", result["one_percent_low_timeline_source"])

    def test_android_frame_pipeline_and_performance_test_items_follow_slow_frame_path(self) -> None:
        def frame_record(frame_id: int, base: int, completed_ms: int) -> dict[str, int]:
            return {
                "Flags": 0,
                "FrameTimelineVsyncId": frame_id,
                "IntendedVsync": base,
                "Vsync": base + 1_000_000,
                "HandleInputStart": base + 1_100_000,
                "AnimationStart": base + 2_000_000,
                "PerformTraversalsStart": base + 3_000_000,
                "DrawStart": base + 4_000_000,
                "SyncQueued": base + 6_000_000,
                "SyncStart": base + 6_200_000,
                "IssueDrawCommandsStart": base + 7_000_000,
                "SwapBuffers": base + 10_000_000,
                "GpuCompleted": base + 20_000_000,
                "FrameCompleted": base + completed_ms * 1_000_000,
                "FrameDeadline": base + 16_000_000,
                "DequeueBufferDuration": 500_000,
                "QueueBufferDuration": 300_000,
            }

        baseline = frame_record(1, 1_000_000_000, 15)
        slow = frame_record(2, 2_000_000_000, 24)
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                foreground_activity=".GameActivity",
                refresh_rate_hz=60.0,
                performance={
                    "foreground_window_name": "com.example/.GameActivity",
                    "frame_counter_total": 100,
                    "frame_counter_deadline_missed": 1,
                    "frame_histogram_ms": {"10": 80, "20": 20},
                    "frame_records": [baseline],
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                foreground_activity=".GameActivity",
                refresh_rate_hz=60.0,
                performance={
                    "foreground_window_name": "com.example/.GameActivity",
                    "frame_counter_total": 160,
                    "frame_counter_deadline_missed": 4,
                    "frame_histogram_ms": {"10": 120, "20": 40},
                    "frame_records": [baseline, slow],
                },
            ),
        ]
        pipeline = analyze_android_frame_pipeline(contexts)
        self.assertTrue(pipeline["available"])
        self.assertEqual(pipeline["frame_count"], 1)
        self.assertEqual(pipeline["deadline_missed_count"], 1)
        self.assertEqual(pipeline["dominant_stage"]["key"], "post_swap_ms")

        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=300.0 + index,
                signed_current_ma=-300.0 - index,
                voltage_mv=3800.0,
                power_mw=1140.0 + index * 20.0,
                direction="discharging",
                cpu_pct=30.0 + index,
                gpu_load_pct=50.0 + index,
                gpu_frequency_mhz=600.0 + index * 10,
                memory_frequency_mhz=800.0 + index * 20,
                battery_temperature_c=35.0 + index * 0.1,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(11)
        ]
        performance = analyze_performance_contexts(
            contexts,
            {"platform": "android"},
        )
        test_items = analyze_performance_test_items(
            samples,
            contexts,
            [ExternalEvent(100.0, "战斗场景", "游戏", duration_s=10.0)],
            performance,
            {
                "timeline": [
                    {"uptime_s": 105.0, "status": 1, "maximum_temperature_c": 42.0}
                ]
            },
            {"timeline": [{"uptime_s": 105.0, "hint_session_count": 1}]},
            3.0,
            1.0,
        )
        row = test_items["rows"][0]
        self.assertEqual(row["name"], "战斗场景")
        self.assertEqual(row["frame_count"], 60)
        self.assertAlmostEqual(row["average_fps"], 6.0)
        self.assertEqual(row["dominant_stage"]["key"], "post_swap_ms")
        self.assertTrue(row["throttling_observed"])
        self.assertIsNotNone(row["average_whole_device_power_mw"])

    def test_performance_test_item_aggregates_presented_intervals_for_one_percent_low(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=300.0,
                signed_current_ma=-300.0,
                voltage_mv=3800.0,
                power_mw=1140.0,
                direction="discharging",
                cpu_pct=30.0,
            )
            for index in range(11)
        ]
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                foreground_activity=".GameActivity",
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                foreground_activity=".GameActivity",
            ),
        ]
        performance = {
            "frame_rate_timeline": [
                {
                    "uptime_s": 105.0,
                    "duration_s": 5.0,
                    "frame_count": 300,
                    "frame_rate_fps": 60.0,
                    "one_percent_low_fps": 10.0,
                    "frame_issue_count": 12,
                    "frame_intervals_ms": [16.667] * 99 + [100.0],
                },
                {
                    "uptime_s": 110.0,
                    "duration_s": 5.0,
                    "frame_count": 300,
                    "frame_rate_fps": 60.0,
                    "one_percent_low_fps": 40.0,
                    "frame_issue_count": 13,
                    "frame_intervals_ms": [16.667] * 100,
                },
            ],
            "render_pipeline": {"timeline": [], "stages": []},
        }
        result = analyze_performance_test_items(
            samples,
            contexts,
            [],
            performance,
            {"timeline": []},
            {"timeline": []},
            3.0,
            1.0,
        )
        row = result["rows"][0]
        self.assertAlmostEqual(row["average_fps"], 60.0)
        self.assertAlmostEqual(row["one_percent_low_fps"], 17.14, places=2)
        self.assertEqual(row["frame_issue_count"], 25)
        self.assertAlmostEqual(row["frame_issue_pct"], 25 / 600 * 100.0)
        self.assertEqual(
            row["frame_metric_source"],
            "Android SurfaceFlinger presented-frame intervals",
        )

    def test_performance_test_item_power_uses_time_integration(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=uptime - 100.0,
                uptime_s=uptime,
                current_ma=power / 4.0,
                signed_current_ma=-power / 4.0,
                voltage_mv=4000.0,
                power_mw=power,
                direction="discharging",
                cpu_pct=None,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index, (uptime, power) in enumerate(
                ((100.0, 1000.0), (101.0, 3000.0), (110.0, 3000.0))
            )
        ]
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                foreground_activity=".GameActivity",
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                foreground_activity=".GameActivity",
            ),
        ]

        result = analyze_performance_test_items(
            samples,
            contexts,
            [ExternalEvent(100.0, "完整窗口", "游戏", duration_s=10.0)],
            {"frame_rate_timeline": [], "render_pipeline": {"timeline": [], "stages": []}},
            {"timeline": []},
            {"timeline": []},
            20.0,
            1.0,
        )

        row = result["rows"][0]
        self.assertAlmostEqual(row["power_covered_duration_s"], 10.0)
        self.assertAlmostEqual(row["average_whole_device_power_mw"], 2900.0)

    def test_performance_test_items_hide_unattributed_unknown_window(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=1000.0,
                direction="discharging",
                cpu_pct=20.0,
            )
            for index in range(4)
        ]

        result = analyze_performance_test_items(
            samples,
            [],
            [],
            {"frame_rate_timeline": [], "render_pipeline": {"timeline": [], "stages": []}},
            {"timeline": []},
            {"timeline": []},
            3.0,
            1.0,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["source_mode"], "unavailable")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["spans"], [])

    def test_performance_test_items_ignore_harmony_shell_and_split_screen_state(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=uptime - 100.0,
                uptime_s=uptime,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=1000.0,
                direction="discharging",
                cpu_pct=20.0,
            )
            for index, uptime in enumerate((100.0, 105.0, 110.0, 115.0))
        ]
        empty_performance = {
            "frame_rate_timeline": [],
            "render_pipeline": {"timeline": [], "stages": []},
        }
        shell_result = analyze_performance_test_items(
            samples,
            [
                ContextSample(
                    100.0,
                    "com.ohos.sceneboard",
                    "EngineServiceAbility",
                    screen_state="off",
                    source="harmony_smartperf",
                )
            ],
            [],
            empty_performance,
            {"timeline": []},
            {"timeline": []},
            15.0,
            1.0,
        )
        self.assertFalse(shell_result["available"])
        self.assertEqual(shell_result["source_mode"], "unavailable")
        self.assertEqual(shell_result["rows"], [])
        self.assertEqual(shell_result["spans"], [])

        contexts = [
            ContextSample(100.0, "com.example.game", ".Main", screen_state="on"),
            ContextSample(105.0, "com.example.game", ".Main", screen_state="off"),
            ContextSample(110.0, "com.example.game", ".Main", screen_state="on"),
        ]
        performance = {
            "frame_rate_timeline": [
                {"uptime_s": 102.0, "duration_s": 2.0, "frame_count": 120, "frame_rate_fps": 60.0},
                {"uptime_s": 112.0, "duration_s": 2.0, "frame_count": 120, "frame_rate_fps": 60.0},
            ],
            "render_pipeline": {"timeline": [], "stages": []},
        }
        split_result = analyze_performance_test_items(
            samples,
            contexts,
            [],
            performance,
            {"timeline": []},
            {"timeline": []},
            15.0,
            1.0,
        )
        self.assertTrue(split_result["available"])
        self.assertEqual(split_result["row_count"], 1)
        self.assertEqual(split_result["span_count"], 2)
        self.assertAlmostEqual(split_result["rows"][0]["duration_s"], 10.0)
        self.assertEqual(
            [(item["start_uptime_s"], item["end_uptime_s"]) for item in split_result["spans"]],
            [(100.0, 105.0), (110.0, 115.0)],
        )

    def test_explicit_performance_event_is_kept_when_screen_is_inactive(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=250.0,
                signed_current_ma=-250.0,
                voltage_mv=4000.0,
                power_mw=1000.0,
                direction="discharging",
                cpu_pct=20.0,
            )
            for index in range(5)
        ]
        result = analyze_performance_test_items(
            samples,
            [
                ContextSample(
                    100.0,
                    "com.ohos.sceneboard",
                    "EngineServiceAbility",
                    screen_state="off",
                )
            ],
            [ExternalEvent(100.0, "显式区间", "测试", duration_s=4.0)],
            {"frame_rate_timeline": [], "render_pipeline": {"timeline": [], "stages": []}},
            {"timeline": []},
            {"timeline": []},
            4.0,
            1.0,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["source_mode"], "external_events")
        self.assertEqual(result["rows"][0]["name"], "显式区间")

    def test_android_asleep_context_does_not_fake_refresh_residency_or_zero_fps(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                screen_state="Asleep",
                refresh_rate_hz=60.0,
                performance={
                    "platform": "android",
                    "frame_counter_total": 35,
                    "frame_data_available": False,
                    "frame_unavailable_reason": "screen asleep",
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.example",
                screen_state="Asleep",
                refresh_rate_hz=60.0,
                performance={
                    "platform": "android",
                    "frame_counter_total": 35,
                    "frame_data_available": False,
                    "frame_unavailable_reason": "screen asleep",
                },
            ),
        ]
        result = analyze_performance_contexts(contexts, {"platform": "android"})
        self.assertEqual(result["refresh_residency"], [])
        self.assertIsNone(result["sampled_frame_rate_fps"])
        self.assertEqual(result["frame_rate_label"], "应用 UI 帧提交速率")
        self.assertEqual(result["frame_unavailable_reason"], "screen asleep")
        self.assertIsNone(result["touch_interaction_count"])
        self.assertEqual(result["refresh_rate_timeline"], [])
        flow = {item["key"]: item for item in result["frame_flow"]["stages"]}
        self.assertEqual(flow["display_scanout"]["timeline"], [])
        for stage in flow.values():
            if stage["status"] not in {"invalid", "unavailable"}:
                continue
            self.assertIsNone(stage["value"])
            self.assertEqual(stage["metrics"], [])
            self.assertEqual(stage["confidence"], "")

    def test_harmony_inactive_display_has_separate_refresh_unavailable_reason(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example.game",
                screen_state="off",
                refresh_rate_hz=60.0,
                source="harmony_smartperf",
                performance={
                    "platform": "harmony",
                    "smartperf_source": "SP_daemon",
                    "frame_unavailable_reason": (
                        "Target package com.example.game is not foreground; SmartPerf FPS was suppressed."
                    ),
                },
            ),
            ContextSample(
                101.0,
                foreground_package="com.example.game",
                screen_state="off",
                refresh_rate_hz=60.0,
                source="harmony_smartperf",
                performance={
                    "platform": "harmony",
                    "smartperf_source": "SP_daemon",
                    "frame_unavailable_reason": (
                        "Target package com.example.game is not foreground; SmartPerf FPS was suppressed."
                    ),
                },
            ),
        ]

        result = analyze_performance_contexts(contexts, {"platform": "harmony"})

        self.assertIn("目标应用未处于前台", result["frame_unavailable_reason"])
        self.assertEqual(
            result["refresh_rate_unavailable_reason"],
            "屏幕未处于活动状态；保留的刷新配置不代表当前显示输出。",
        )
        self.assertEqual(result["refresh_rate_timeline"], [])
        self.assertEqual(result["refresh_residency"], [])

    def test_harmony_normalized_sample_preserves_signed_current_and_cpu_delta(self) -> None:
        policies = [
            CpuPolicy(
                name="policy0",
                path="hidumper --cpufreq",
                cluster_index=0,
                label="Performance",
                cores=[0, 1],
                max_khz=2_000_000,
            )
        ]
        previous = {
            "cpu": CpuTimes.from_values([100, 0, 50, 850, 0, 0, 0, 0]),
            "cpu0": CpuTimes.from_values([50, 0, 25, 425, 0, 0, 0, 0]),
            "cpu1": CpuTimes.from_values([50, 0, 25, 425, 0, 0, 0, 0]),
        }
        sample, counters = build_harmony_sample(
            1,
            1_783_934_231.0,
            "pluggedType: 0\nchargingStatus: 0\nvoltage: 4318299\n"
            "nowCurrent: -100\ntemperature: 280\n",
            "cpu  120 0 60 920 0 0 0 0\n"
            "cpu0 60 0 30 460 0 0 0 0\n"
            "cpu1 60 0 30 460 0 0 0 0\n",
            previous,
            policies,
            {"policy0": 1500.0},
            previous_timestamp_s=1_783_934_230.0,
            max_cpu_gap_s=3.0,
        )
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.current_ma, 100.0)
        self.assertEqual(sample.signed_current_ma, -100.0)
        self.assertEqual(sample.direction, "discharging")
        self.assertAlmostEqual(sample.power_mw, 431.8299)
        self.assertGreater(sample.cpu_pct or 0.0, 0.0)
        self.assertIn("policy0", sample.cluster_cpu_pct)
        self.assertEqual(sample.power_source, "harmony_battery_service")
        self.assertIn("cpu", counters)

    def test_capture_presets_apply_overrides_and_mode_boundaries(self) -> None:
        performance = resolve_capture_configuration(
            "performance",
            "harmony",
            "harmony-smartperf",
            enable_features=["touch_events", "power_attribution"],
            disable_features=["frame_details"],
        )
        features = performance["features"]
        self.assertTrue(features["frame_rate"])
        self.assertFalse(features["frame_details"])
        self.assertTrue(features["touch_events"])
        self.assertFalse(features["target_process"])
        self.assertFalse(features["power_attribution"])
        self.assertFalse(features["hot_threads"])
        self.assertEqual(performance["backend"], "harmony_smartperf")

        low = resolve_capture_configuration("power", "android", "low-overhead")
        self.assertTrue(low["features"]["cpu_usage"])
        self.assertFalse(low["features"]["process_snapshots"])
        self.assertFalse(low["features"]["power_attribution"])

        android = resolve_capture_configuration(
            "performance", "android", "performance-standard"
        )
        self.assertEqual(
            {name for name, enabled in android["features"].items() if enabled},
            {
                "cpu_usage",
                "cpu_frequency",
                "gpu_metrics",
                "foreground_window",
                "frame_rate",
                "thermal",
            },
        )
        power = resolve_capture_configuration("power", "android", "power-standard")
        self.assertEqual(
            {name for name, enabled in power["features"].items() if enabled},
            {
                "cpu_usage",
                "cpu_frequency",
                "gpu_metrics",
                "foreground_window",
                "thermal",
                "runtime_settings",
            },
        )
        self.assertEqual(
            power["base_channels"],
            ["battery_current", "battery_voltage", "device_timestamp"],
        )
        auto_power = resolve_capture_configuration("power", "android", "auto")
        self.assertEqual(auto_power["preset"], "power-standard")
        self.assertEqual(auto_power["features"], power["features"])
        auto_performance = resolve_capture_configuration(
            "performance", "android", "auto"
        )
        self.assertEqual(auto_performance["preset"], "performance-standard")
        self.assertEqual(auto_performance["features"], android["features"])
        harmony_native = resolve_capture_configuration(
            "performance", "harmony", "performance-standard"
        )
        self.assertEqual(
            {name for name, enabled in harmony_native["features"].items() if enabled},
            {
                "cpu_usage",
                "cpu_frequency",
                "foreground_window",
                "frame_rate",
                "thermal",
            },
        )
        ios = resolve_capture_configuration(
            "performance", "ios", "performance-standard"
        )
        self.assertEqual(
            {name for name, enabled in ios["features"].items() if enabled},
            {"cpu_usage", "gpu_metrics", "foreground_window", "thermal"},
        )
        ios_rows = {row["key"]: row for row in ios["feature_rows"]}
        self.assertEqual(ios_rows["gpu_metrics"]["label"], "GPU 利用率")
        self.assertIn("不含 GPU 频率", ios_rows["gpu_metrics"]["description"])
        self.assertEqual(ios_rows["foreground_window"]["label"], "前台应用状态")
        self.assertIn("当前前台保持未知", ios_rows["foreground_window"]["description"])
        self.assertEqual(ios_rows["thermal"]["label"], "电池温度")
        self.assertIn("不包含热严重度", ios_rows["thermal"]["description"])
        for name in (
            "cpu_frequency",
            "memory_frequency",
            "frame_rate",
            "frame_details",
            "harmony_hitches",
            "touch_events",
            "target_process",
            "process_snapshots",
            "hot_threads",
            "scheduler",
            "runtime_settings",
            "power_attribution",
        ):
            self.assertFalse(ios["features"][name], name)
        ios_required = resolve_capture_configuration(
            "power",
            "ios",
            "low-overhead",
            disable_features=["cpu_usage", "gpu_metrics", "foreground_window", "thermal"],
        )
        for name in ("cpu_usage", "gpu_metrics", "foreground_window", "thermal"):
            self.assertTrue(ios_required["features"][name], name)
        android_touch = resolve_capture_configuration(
            "performance",
            "android",
            "performance-standard",
            enable_features=["touch_events"],
        )
        self.assertFalse(android_touch["features"]["touch_events"])
        harmony_gpu = resolve_capture_configuration(
            "performance",
            "harmony",
            "performance-standard",
            enable_features=["gpu_metrics", "frame_details", "target_process"],
        )
        for name in ("gpu_metrics", "frame_details", "target_process"):
            self.assertFalse(harmony_gpu["features"][name], name)

    def test_analysis_data_sources_keep_only_enabled_and_observed_android_channels(self) -> None:
        capture = resolve_capture_configuration(
            "performance", "android", "performance-standard"
        )
        sources = _analysis_data_sources("android", "performance", capture)
        filtered = _filter_analysis_data_sources(
            sources,
            {
                "summary": {
                    "average_cpu_pct": 10.0,
                    "observed_power_average_mw": 500.0,
                },
                "cpu": {"clusters": [{"name": "policy0", "average_mhz": 1500.0}]},
                "gpu": {"frequency_available": True},
                "memory": {"available": False},
                "performance": {"frame_sample_count": 0},
                "system": {"available": False},
                "render_performance": {"stages": [], "render_threads": []},
                "scheduler": {"available": False},
                "thermal": {"available": True, "sensors": [{"name": "skin"}]},
                "brightness_throttling": {"available": True},
                "applications": {"rows": []},
                "external_events": {"rows": []},
            },
        )
        metrics = {item["metric"] for item in filtered}
        by_metric = {item["metric"]: item for item in filtered}

        self.assertIn("CPU utilization", metrics)
        self.assertIn("CPU frequency", metrics)
        self.assertIn("GPU activity", metrics)
        self.assertIn("Whole-device power recording", metrics)
        self.assertIn("Thermal context", metrics)
        self.assertIn("Brightness thermal limiting", metrics)
        self.assertNotIn("Memory frequency pressure", metrics)
        self.assertNotIn("CPU, GPU and memory frequency context", metrics)
        self.assertNotIn("Frame rate, 1% Low and frame latency", metrics)
        self.assertNotIn("Render pipeline stages", metrics)
        self.assertNotIn("Render and compositor thread activity", metrics)
        self.assertNotIn("Scheduler context", metrics)
        self.assertEqual(
            by_metric["CPU utilization"]["source"],
            "Android /proc/stat utilization deltas",
        )
        self.assertEqual(
            by_metric["CPU frequency"]["source"],
            "Android cpufreq core-group counters",
        )
        self.assertEqual(
            by_metric["GPU activity"]["source"],
            "Readable Android KGSL/OEM GPU frequency and load counters",
        )

    def test_analysis_data_sources_name_only_observed_memory_frequency_channel(self) -> None:
        capture = resolve_capture_configuration(
            "performance",
            "android",
            "performance-standard",
            enable_features=["memory_frequency"],
            disable_features=["cpu_usage", "cpu_frequency", "gpu_metrics", "thermal"],
        )
        sources = _analysis_data_sources("android", "performance", capture)
        filtered = _filter_analysis_data_sources(
            sources,
            {
                "summary": {"observed_power_average_mw": 500.0},
                "cpu": {"clusters": [], "timeline": []},
                "gpu": {"frequency_available": False, "load_available": False},
                "memory": {"available": True, "timeline": [{"frequency_mhz": 3200}]},
                "performance": {"frame_sample_count": 0},
                "system": {"available": False},
                "render_performance": {"stages": [], "render_threads": []},
                "scheduler": {"available": False},
                "thermal": {"available": False},
                "brightness_throttling": {"available": False},
                "applications": {"rows": []},
                "external_events": {"rows": []},
            },
        )

        metrics = {item["metric"] for item in filtered}
        self.assertEqual(
            metrics,
            {"Memory frequency pressure", "Whole-device power recording"},
        )
        memory_source = next(
            item for item in filtered if item["metric"] == "Memory frequency pressure"
        )
        self.assertEqual(
            memory_source["source"],
            "Readable DRAM/DMC/MIF devfreq clock",
        )

    def test_harmony_native_data_sources_do_not_claim_gpu_or_ddr(self) -> None:
        capture = resolve_capture_configuration(
            "performance", "harmony", "performance-standard"
        )
        filtered = _filter_analysis_data_sources(
            _analysis_data_sources("harmony", "performance", capture),
            {
                "summary": {
                    "average_cpu_pct": 24.0,
                    "observed_power_average_mw": 1800.0,
                },
                "cpu": {"clusters": [{"average_mhz": 1800.0}]},
                "gpu": {"frequency_available": False, "load_available": False},
                "memory": {"available": False, "timeline": []},
                "performance": {"frame_sample_count": 0, "refresh_rate_timeline": []},
                "system": {"top_processes": [], "hot_threads": []},
                "scheduler": {},
                "thermal": {"sensors": [{"name": "soc", "maximum_c": 48.0}]},
                "brightness_throttling": {"available": False},
                "applications": {"rows": [], "transitions": []},
                "external_events": {"rows": []},
            },
        )

        metrics = {item["metric"] for item in filtered}
        self.assertIn("CPU utilization", metrics)
        self.assertIn("CPU frequency", metrics)
        self.assertIn("Thermal sensors", metrics)
        self.assertIn("Whole-device power recording", metrics)
        self.assertNotIn("GPU activity", metrics)
        self.assertNotIn("Memory frequency pressure", metrics)
        self.assertNotIn("HarmonyOS CPU/GPU/DDR and thermal context", metrics)
        thermal_source = next(
            item for item in filtered if item["metric"] == "Thermal sensors"
        )
        self.assertEqual(
            thermal_source["source"],
            "HarmonyOS ThermalService via hidumper",
        )

    def test_ios_system_load_is_low_rate_telemetry_in_both_modes(self) -> None:
        for mode in ("power", "performance"):
            with self.subTest(mode=mode):
                capture = resolve_capture_configuration(mode, "ios", "auto")
                source = next(
                    item
                    for item in _analysis_data_sources("ios", mode, capture)
                    if item["metric"] == "Whole-device raw SystemLoad power"
                )
                self.assertEqual(source["kind"], "measured low-rate telemetry")

    def test_harmony_smartperf_omits_unobserved_ddr_channel(self) -> None:
        capture = resolve_capture_configuration(
            "performance",
            "harmony",
            "harmony-smartperf",
            enable_features=["memory_frequency"],
        )
        filtered = _filter_analysis_data_sources(
            _analysis_data_sources("harmony", "performance", capture),
            {
                "summary": {
                    "average_cpu_pct": 40.0,
                    "observed_power_average_mw": 2200.0,
                },
                "cpu": {"clusters": [{"average_mhz": 2100.0}]},
                "gpu": {"frequency_available": True, "load_available": True},
                "memory": {"available": False, "timeline": []},
                "performance": {
                    "frame_sample_count": 1,
                    "sampled_frame_rate_fps": 60.0,
                    "refresh_rate_timeline": [],
                },
                "system": {"top_processes": [], "hot_threads": []},
                "scheduler": {},
                "thermal": {
                    "available": True,
                    "sensors": [{"name": "soc_thermal", "maximum_c": 48.0}],
                },
                "applications": {"rows": [], "transitions": []},
                "external_events": {"rows": []},
            },
        )

        metrics = {item["metric"] for item in filtered}
        self.assertIn("GPU activity", metrics)
        self.assertIn("HarmonyOS application frame pacing", metrics)
        self.assertIn("Thermal sensors", metrics)
        self.assertNotIn("Memory frequency pressure", metrics)
        self.assertNotIn("CPU/GPU/DDR and target process resources", metrics)
        thermal_source = next(
            item for item in filtered if item["metric"] == "Thermal sensors"
        )
        self.assertEqual(
            thermal_source["source"],
            "HarmonyOS SmartPerf SP_daemon temperature fields",
        )

    def test_ios_data_sources_are_atomic_and_require_observed_events(self) -> None:
        capture = resolve_capture_configuration(
            "performance", "ios", "performance-standard"
        )
        sources = _analysis_data_sources("ios", "performance", capture)
        base = {
            "summary": {
                "average_cpu_pct": 18.0,
                "observed_power_average_mw": 1300.0,
                "observed_power_sources": ["ios_power_telemetry_system_load"],
                "power_valid_for_consumption": False,
                "average_current_ma": None,
                "observed_average_current_ma": 300.0,
                "average_voltage_mv": 4300.0,
            },
            "cpu": {"clusters": []},
            "gpu": {"frequency_available": False, "load_available": False},
            "memory": {"available": False},
            "performance": {
                "frame_sample_count": 0,
                "frame_rate_timeline": [],
                "refresh_rate_timeline": [],
            },
            "system": {"top_processes": [], "hot_threads": []},
            "scheduler": {},
            "thermal": {"available": False, "sensors": []},
            "brightness_throttling": {"available": False},
            "applications": {
                "rows": [{"package": "unknown"}],
                "transitions": [{"package": "unknown"}],
            },
            "external_events": {"rows": []},
        }
        filtered = _filter_analysis_data_sources(sources, base)
        metrics = {item["metric"] for item in filtered}

        self.assertIn("CPU utilization", metrics)
        self.assertIn("Whole-device raw SystemLoad power", metrics)
        self.assertIn("Battery current and voltage", metrics)
        for absent in (
            "GPU activity",
            "System processes",
            "Relative process power score",
            "Foreground application",
            "Observer-related process CPU upper bound",
            "Battery temperature",
            "Display refresh rate",
            "Frame rate, 1% Low and frame latency",
            "iOS CPU and GPU performance context",
        ):
            self.assertNotIn(absent, metrics)

        fallback_only = {
            **base,
            "summary": {
                **base["summary"],
                "observed_power_sources": ["ios_battery_current_voltage"],
            },
        }
        fallback_metrics = {
            item["metric"]
            for item in _filter_analysis_data_sources(sources, fallback_only)
        }
        self.assertNotIn("Whole-device raw SystemLoad power", fallback_metrics)
        self.assertIn("Battery current and voltage", fallback_metrics)

        observed = {
            **base,
            "summary": {**base["summary"], "average_collector_cpu_pct": 6.0},
            "gpu": {"frequency_available": False, "load_available": True},
            "system": {
                "top_processes": [
                    {
                        "name": "Example",
                        "average_cpu_pct": 4.0,
                        "average_relative_power_score": 7.5,
                    }
                ],
                "hot_threads": [],
            },
            "thermal": {
                "available": True,
                "sensors": [{"name": "battery", "maximum_c": 35.0}],
            },
            "applications": {
                "rows": [{"package": "com.example.game"}],
                "transitions": [{"package": "com.example.game"}],
            },
        }
        observed_metrics = {
            item["metric"]
            for item in _filter_analysis_data_sources(sources, observed)
        }
        for present in (
            "CPU utilization",
            "GPU activity",
            "System processes",
            "Relative process power score",
            "Foreground application",
            "Observer-related process CPU upper bound",
            "Battery temperature",
        ):
            self.assertIn(present, observed_metrics)
        self.assertNotIn("iOS CPU and GPU performance context", observed_metrics)

    def test_capture_metadata_partial_features_keep_legacy_channels_enabled(self) -> None:
        self.assertTrue(all(capture_features_from_metadata({}).values()))
        features = capture_features_from_metadata(
            {
                "capture_configuration": {
                    "features": {"frame_rate": False},
                }
            }
        )
        self.assertFalse(features["frame_rate"])
        self.assertTrue(features["cpu_usage"])
        self.assertTrue(features["gpu_metrics"])

        configuration = resolve_capture_configuration(
            "performance", "android", "performance-standard"
        )
        rows = {item["key"]: item for item in configuration["feature_rows"]}
        self.assertEqual(
            rows["cpu_usage"]["description"],
            "记录整机 CPU 整体负载；不重复展示逐核负载。",
        )
        self.assertIn("共享/分组频率", rows["cpu_frequency"]["description"])

    def test_report_capture_configuration_refreshes_legacy_feature_copy(self) -> None:
        rows = _capture_configuration_rows(
            {
                "capture_configuration": {
                    "preset_label": "历史性能配置",
                    "backend": "platform_native",
                    "feature_rows": [
                        {
                            "key": "cpu_usage",
                            "label": "逐核 CPU 利用率",
                            "description": "记录整体与每个 CPU 核心负载。",
                            "overhead": "high",
                            "enabled": False,
                            "reason": "历史会话中由用户关闭",
                        }
                    ],
                }
            }
        )

        self.assertIn("CPU 利用率", rows)
        self.assertIn("记录整机 CPU 整体负载；不重复展示逐核负载。", rows)
        self.assertIn("<td>低</td>", rows)
        self.assertIn("关闭", rows)
        self.assertIn("历史会话中由用户关闭", rows)
        self.assertNotIn("逐核 CPU 利用率", rows)
        self.assertNotIn("记录整体与每个 CPU 核心负载。", rows)

    def test_report_capture_configuration_uses_platform_specific_copy(self) -> None:
        harmony = resolve_capture_configuration(
            "performance", "harmony", "performance-standard"
        )
        rows = _capture_configuration_rows(
            {"platform": "harmony", "capture_configuration": harmony}
        )
        self.assertIn("温度传感器", rows)
        self.assertIn("不提供可验证的热严重度", rows)
        self.assertIn("RenderService 背光原始值", rows)
        self.assertNotIn("温度与热限制", rows)

        ios = resolve_capture_configuration(
            "performance", "ios", "performance-standard"
        )
        ios_rows = _capture_configuration_rows(
            {"platform": "ios", "capture_configuration": ios}
        )
        self.assertIn("GPU 利用率", ios_rows)
        self.assertIn("不含 GPU 频率", ios_rows)
        self.assertIn("前台应用状态", ios_rows)

    def test_frame_flow_history_omits_all_unavailable_empty_stages(self) -> None:
        fragment = _frame_flow_history_section(
            {
                "performance": {
                    "frame_flow": {
                        "stages": [
                            {
                                "key": "app_submission",
                                "status": "unavailable",
                                "timeline": [],
                            },
                            {
                                "key": "render_queue",
                                "status": "unavailable",
                                "timeline": [],
                            },
                            {
                                "key": "surface_present",
                                "status": "unavailable",
                                "timeline": [],
                            },
                            {
                                "key": "display_scanout",
                                "status": "unavailable",
                                "timeline": [],
                            },
                        ]
                    }
                }
            }
        )

        self.assertEqual(fragment, "")

    def test_smartperf_parser_builds_native_sample_and_frame_context(self) -> None:
        text = """
order:0 Battery=33.000000
order:1 ProcAppName=com.example.game
order:2 ProcCpuUsage=42.5
order:3 ProcId=40595
order:4 ProcSCpuUsage=2.5
order:5 ProcUCpuUsage=40.0
order:6 TotalcpuUsage=58.0
order:7 cpu0Frequency=1500000
order:8 cpu0Usage=60.0
order:9 cpu1Frequency=2050000
order:10 cpu1Usage=70.0
order:11 currentNow=-1000
order:12 ddrFrequency=3197000000
order:13 fps=60
order:14 fpsJitters=16666667;;33333334
order:15 gpuFrequency=800000000
order:16 gpuLoad=95.0
order:17 pss=2500000
order:18 refreshrate=60
order:19 soc_thermal=55.0
order:20 timestamp=1783995192882
order:21 voltageNow=4073546
command exec finished!
"""
        rows = parse_harmony_smartperf_output(text)
        self.assertEqual(len(rows), 1)
        policies = [
            CpuPolicy(
                name="policy0",
                path="hidumper --cpufreq",
                cluster_index=0,
                label="Performance",
                cores=[0, 1],
            )
        ]
        sample = build_harmony_smartperf_sample(rows[0], 0, policies)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.power_source, "harmony_smartperf_battery")
        self.assertAlmostEqual(sample.voltage_mv, 4073.546)
        self.assertAlmostEqual(sample.gpu_frequency_mhz or 0.0, 800.0)
        self.assertAlmostEqual(sample.memory_frequency_mhz or 0.0, 3197.0)
        context = build_harmony_smartperf_context(
            rows[0], sample, "com.example.game", screen_state="awake"
        )
        self.assertEqual(context.performance["compositor_fps"], 60.0)
        self.assertAlmostEqual(context.performance["frame_interval_p95_ms"], 33.333334)
        self.assertLess(context.performance["one_percent_low_fps"], 31.0)
        self.assertFalse(context.performance["smartperf_foreground_probe_used"])
        self.assertIsNone(context.performance["smartperf_target_foreground"])

        unknown_screen = build_harmony_smartperf_context(
            rows[0], sample, "com.example.game"
        )
        self.assertIsNone(unknown_screen.performance["compositor_fps"])
        self.assertIsNone(unknown_screen.refresh_rate_hz)
        self.assertIn(
            "screen state could not be verified",
            unknown_screen.performance["frame_unavailable_reason"],
        )

        background = build_harmony_smartperf_context(
            rows[0],
            sample,
            "com.example.game",
            actual_foreground_package="com.ohos.sceneboard",
            foreground_verified=True,
            screen_state="awake",
        )
        self.assertTrue(background.performance["smartperf_foreground_probe_used"])
        self.assertFalse(background.performance["smartperf_target_foreground"])
        self.assertIsNone(background.performance["compositor_fps"])
        self.assertIn("not foreground", background.performance["frame_unavailable_reason"])

    def test_harmony_native_sampler_uses_deadline_cadence(self) -> None:
        script = _sampler_script(1.0)

        self.assertIn("interval_ns=1000000000", script)
        self.assertIn("next_ns=$(date +%s%N)", script)
        self.assertIn("remain_ns=$((next_ns - now_ns))", script)
        self.assertIn("sleep \"$delay\"", script)
        self.assertNotIn("sleep 1.000", script)

    def test_harmony_power_mode_is_read_and_changed_with_verification(self) -> None:
        help_output = (
            "usage: power-shell setmode\n  602  :  performance mode\n"
            "Set Mode Failed, current mode is: 600\n"
        )
        with patch(
            "mobile_profiler.harmony.hdc_shell",
            return_value=CommandResult([], 0, help_output, "", 0.01),
        ):
            state = read_harmony_power_mode("hdc", "device")
        self.assertTrue(state["supported"])
        self.assertEqual(state["current_mode"], 600)

        with patch(
            "mobile_profiler.harmony.hdc_shell",
            side_effect=[
                CommandResult([], 0, "Set Mode Success!", "", 0.01),
                CommandResult(
                    [],
                    0,
                    "602 : performance mode\ncurrent mode is: 602",
                    "",
                    0.01,
                ),
            ],
        ):
            changed = set_harmony_power_mode("hdc", "device", 602)
        self.assertTrue(changed["success"])
        self.assertEqual(changed["current_mode"], 602)

    def test_harmony_error_report_is_finalized_after_power_mode_restore(self) -> None:
        observed_modes: list[dict[str, object]] = []
        observed_warnings: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "run"
            args = type(
                "Args",
                (),
                {
                    "output": output_dir,
                    "device": "harmony:USB123",
                    "hdc": "hdc",
                    "test_mode": "performance",
                    "capture_configuration": resolve_capture_configuration(
                        "performance",
                        "harmony",
                        "performance-standard",
                        disable_features=["cpu_frequency"],
                    ),
                    "package": "com.example.game",
                    "session_mode": False,
                    "harmony_high_performance": True,
                    "require_unplugged": False,
                    "title": "",
                    "start_context": "app",
                    "start_note": "",
                    "duration": 10.0,
                    "interval": 0.5,
                    "performance_interval": 2.0,
                    "process_interval": 2.0,
                    "thermal_interval": 5.0,
                    "scheduler_interval": 5.0,
                    "checkpoint_interval": 30.0,
                    "reconnect_timeout": 120.0,
                },
            )()
            probe = {
                "device": {"brand": "HUAWEI", "model": "nova"},
                "battery": {
                    "voltage_mv": 4000.0,
                    "powered": [],
                    "status": "DISCHARGING",
                },
                "connection": {"type": "usb"},
                "cpu_policies": [],
                "cpu_frequencies_mhz": {},
                "foreground_package": "com.example.game",
                "smartperf": {"available": False},
                "power_mode": {
                    "supported": True,
                    "current_mode": 600,
                    "current_label": "normal",
                },
                "performance": {},
                "current_command_ok": True,
            }

            def change_mode(_hdc: str, _device: str, mode: int) -> dict[str, object]:
                return {
                    "success": True,
                    "current_mode": mode,
                    "current_label": "performance" if mode == 602 else "normal",
                    "set_output": "Set Mode Success!",
                }

            def finalize_after_restore(
                run_dir: Path,
                _warnings: object = (),
                _status: object = None,
            ) -> tuple[dict[str, object], Path]:
                if isinstance(_warnings, (list, tuple)):
                    observed_warnings.extend(str(item) for item in _warnings)
                metadata = json.loads(
                    (run_dir / "metadata.json").read_text(encoding="utf-8")
                )
                observed_modes.append(dict(metadata["device_performance_mode"]))
                return {}, run_dir / "report.html"

            with (
                patch(
                    "mobile_profiler.cli.select_harmony_device",
                    return_value={
                        "serial": "harmony:USB123",
                        "hdc_target": "USB123",
                        "connection_type": "usb",
                    },
                ),
                patch("mobile_profiler.cli.probe_harmony_device", return_value=probe),
                patch(
                    "mobile_profiler.cli.read_harmony_power_mode",
                    return_value={
                        "supported": True,
                        "current_mode": 600,
                        "current_label": "normal",
                    },
                ),
                patch(
                    "mobile_profiler.cli.set_harmony_power_mode",
                    side_effect=change_mode,
                ) as set_mode,
                patch(
                    "mobile_profiler.cli.collect_harmony_session",
                    side_effect=RuntimeError("collector failed"),
                ),
                patch(
                    "mobile_profiler.cli.finalize_run",
                    side_effect=finalize_after_restore,
                ),
                patch("mobile_profiler.cli.print_run_summary"),
                patch("mobile_profiler.cli.sys.stderr", new=io.StringIO()),
            ):
                result = run_harmony_record(args)

        self.assertEqual(result, 6)
        self.assertEqual([call.args[2] for call in set_mode.call_args_list], [602, 600])
        self.assertEqual(len(observed_modes), 1)
        self.assertTrue(observed_modes[0]["applied"])
        self.assertTrue(observed_modes[0]["restored"])
        self.assertEqual(observed_modes[0]["restored_mode"], 600)
        self.assertFalse(
            any("hidumper --cpufreq" in warning for warning in observed_warnings)
        )


class IosAdapterTests(unittest.TestCase):
    def test_ios_bridge_rejects_stale_system_load_for_consumption(self) -> None:
        self.assertTrue(
            ios_bridge._power_valid_for_consumption(
                "discharging",
                False,
                "ios_power_telemetry_system_load",
                5.0,
            )
        )
        self.assertFalse(
            ios_bridge._power_valid_for_consumption(
                "discharging",
                False,
                "ios_power_telemetry_system_load",
                ios_bridge.SYSTEM_LOAD_SAMPLE_STALE_AFTER_S + 0.1,
            )
        )
        self.assertFalse(
            ios_bridge._power_valid_for_consumption(
                "discharging",
                False,
                "ios_power_telemetry_system_load",
                None,
            )
        )

    def test_ios_performance_limitations_do_not_claim_render_service_samples(self) -> None:
        result = analyze_performance_contexts([], {"platform": "ios"})

        self.assertIn("does not expose a general application FPS counter", result["limitations"])
        self.assertIn("No frame-rate, 1% Low", result["limitations"])
        self.assertNotIn("RenderService submissions", result["limitations"])

    def test_legacy_link_local_ios_connection_is_not_migrated_as_wifi(self) -> None:
        metadata: dict[str, object] = {
            "platform": "ios",
            "connection": {
                "type": "wireless",
                "host": "169.254.47.225",
                "port": 49152,
            },
        }

        warning = _migrate_legacy_ios_connection_metadata(metadata)

        self.assertIsNotNone(warning)
        connection = metadata["connection"]
        self.assertIsInstance(connection, dict)
        assert isinstance(connection, dict)
        self.assertEqual(connection["type"], "remote-pairing")
        self.assertTrue(connection["remote_xpc_ready"])
        self.assertFalse(connection["unplug_ready"])
        self.assertFalse(connection["wireless_lan_candidate"])
        self.assertEqual(connection["endpoint_scope"], "link-local")
        self.assertIn(
            "legacy_ios_link_local_connection_reclassified",
            metadata["metadata_migrations"],
        )

    def test_ios_collection_duration_starts_at_first_valid_sample(self) -> None:
        self.assertEqual(
            ios_bridge._ios_collection_target_coverage_s(0.0, 1.0),
            float("inf"),
        )
        self.assertFalse(
            ios_bridge._ios_collection_window_complete(100.0, 10000.0, 0.0, 1.0)
        )
        self.assertEqual(ios_bridge._ios_collection_target_coverage_s(16.0, 5.0), 15.0)
        self.assertFalse(
            ios_bridge._ios_collection_window_complete(100.0, 110.0, 16.0, 5.0)
        )
        self.assertTrue(
            ios_bridge._ios_collection_window_complete(100.0, 115.0, 16.0, 5.0)
        )

        self.assertEqual(ios_bridge._ios_collection_target_coverage_s(8.0, 1.0), 8.0)
        self.assertFalse(
            ios_bridge._ios_collection_window_complete(100.0, 107.0, 8.0, 1.0)
        )
        self.assertTrue(
            ios_bridge._ios_collection_window_complete(100.0, 108.0, 8.0, 1.0)
        )

    def test_ios_collection_duration_keeps_two_samples_when_shorter_than_interval(self) -> None:
        self.assertEqual(ios_bridge._ios_collection_target_coverage_s(2.0, 5.0), 5.0)

    def test_ios_sidecar_accepts_python313_native_psk_without_sslpsk(self) -> None:
        tunnel_service = type("TunnelService", (), {"SSLPSKContext": None})()
        ssl_context = type(
            "NativePskContext",
            (),
            {"set_psk_client_callback": lambda self, callback: None},
        )

        error = ios_bridge._pairing_tls_runtime_error(
            tunnel_service,
            python_version=(3, 13, 0),
            ssl_context_type=ssl_context,
        )

        self.assertIsNone(error)

    def test_ios_sidecar_explains_python313_requirement_for_legacy_runtime(self) -> None:
        tunnel_service = type("TunnelService", (), {"SSLPSKContext": None})()

        error = ios_bridge._pairing_tls_runtime_error(
            tunnel_service,
            python_version=(3, 12, 0),
        )

        self.assertIn("iOS 18.2+", error or "")
        self.assertIn("CPython 3.13+", error or "")

    def test_ios_sidecar_rejects_incompatible_async_pytcp_api(self) -> None:
        class AsyncStack:
            @staticmethod
            async def start() -> None:
                return None

        userspace_tunnel = type("UserspaceTunnel", (), {"stack": AsyncStack})()

        error = ios_bridge._userspace_tunnel_runtime_error(userspace_tunnel)

        self.assertIn("pmd-pytcp==0.0.6", error or "")

    def test_ios_sidecar_accepts_synchronous_pytcp_api(self) -> None:
        class SyncStack:
            @staticmethod
            def start() -> None:
                return None

        userspace_tunnel = type("UserspaceTunnel", (), {"stack": SyncStack})()

        self.assertIsNone(ios_bridge._userspace_tunnel_runtime_error(userspace_tunnel))

    def test_ios_sidecar_registers_conda_openssl11_dll_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            dll_dir = base / "pkgs" / "openssl-1.1.1-test" / "Library" / "bin"
            dll_dir.mkdir(parents=True)
            for name in ("libssl-1_1-x64.dll", "libcrypto-1_1-x64.dll"):
                (dll_dir / name).write_bytes(b"")
            handle = object()
            with (
                patch.object(ios_bridge.sys, "platform", "win32"),
                patch.object(ios_bridge.sys, "prefix", str(base / "venv")),
                patch.object(ios_bridge.sys, "base_prefix", str(base)),
                patch.dict(ios_bridge.os.environ, {"PATH": "", "CONDA_PREFIX": ""}, clear=False),
                patch.object(ios_bridge.os, "add_dll_directory", return_value=handle, create=True) as add_dll,
                patch.object(ios_bridge, "_WINDOWS_DLL_DIRECTORY_HANDLES", []),
            ):
                configured = ios_bridge._configure_windows_sslpsk_runtime()

        self.assertEqual(configured, [str(dll_dir)])
        add_dll.assert_called_once_with(str(dll_dir))

    def test_ios_sidecar_uses_windows_selector_event_loop_policy(self) -> None:
        policy = object()
        with (
            patch.object(ios_bridge.sys, "platform", "win32"),
            patch.object(
                ios_bridge.asyncio,
                "WindowsSelectorEventLoopPolicy",
                return_value=policy,
                create=True,
            ) as policy_factory,
            patch.object(ios_bridge.asyncio, "set_event_loop_policy") as set_policy,
        ):
            ios_bridge._configure_windows_event_loop()

        policy_factory.assert_called_once_with()
        set_policy.assert_called_once_with(policy)

    def test_ios_list_keeps_usb_devices_when_collection_or_network_is_unavailable(self) -> None:
        device = type("MuxDevice", (), {"serial": "00008150-TEST"})()
        client = type(
            "Lockdown",
            (),
            {
                "all_values": {
                    "DeviceName": "Test iPhone",
                    "ProductType": "iPhone18,2",
                    "ProductVersion": "19.0",
                    "BuildVersion": "23A000",
                },
                "close": AsyncMock(),
            },
        )()
        with (
            patch.object(ios_bridge, "DISCOVERY_IMPORT_ERROR", None),
            patch.object(
                ios_bridge,
                "COLLECTION_IMPORT_ERROR",
                RuntimeError("Graphics API changed"),
            ),
            patch.object(ios_bridge, "_usb_devices", AsyncMock(return_value=[device])),
            patch.object(
                ios_bridge,
                "_open_usb_lockdown",
                AsyncMock(return_value=client),
            ),
            patch.object(
                ios_bridge,
                "_network_devices",
                AsyncMock(side_effect=OSError("Bonjour socket failed")),
            ),
            patch.object(ios_bridge, "_remote_pair_record_path") as pair_path,
        ):
            pair_path.return_value.is_file.return_value = False
            payload = asyncio.run(ios_bridge.list_devices())

        self.assertEqual(payload["devices"][0]["udid"], "00008150-TEST")
        self.assertEqual(payload["devices"][0]["state"], "device")
        self.assertIn("Bonjour socket failed", payload["warnings"][0])

    def test_project_cache_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / ".mobile-profiler" / "ios-devices.json"
            current.parent.mkdir(parents=True)
            current.write_text(
                json.dumps({"00008150-TEST": {"host": "192.0.2.10", "port": 49152}}),
                encoding="utf-8",
            )
            with patch("mobile_profiler.ios.IOS_ENDPOINTS_PATH", current):
                endpoints = _load_endpoints()

        self.assertEqual(endpoints["00008150-TEST"]["host"], "192.0.2.10")

    def test_usb_transport_is_visible_even_when_cached_wireless_endpoint_is_ready(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "devices": [
                        {
                            "udid": "00008150-TEST",
                            "name": "Test iPhone",
                            "product_type": "iPhone18,2",
                            "connection_type": "usb",
                            "state": "device",
                            "remote_paired": True,
                        }
                    ]
                },
            ),
            patch(
                "mobile_profiler.ios._load_endpoints",
                return_value={
                    "00008150-TEST": {"host": "192.0.2.10", "port": 49152}
                },
            ),
            patch("mobile_profiler.ios._save_endpoint"),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=True),
        ):
            devices, error = list_ios_devices("ios-python")

        self.assertIsNone(error)
        self.assertEqual(devices[0]["connection_type"], "usb")
        self.assertEqual(devices[0]["transports"], "usb,remote-xpc")
        self.assertEqual(devices[0]["remote_xpc_ready"], "true")
        self.assertEqual(devices[0]["wireless_ready"], "false")
        self.assertEqual(devices[0]["unplug_ready"], "false")
        self.assertEqual(devices[0]["wireless_lan_candidate"], "true")
        self.assertEqual(devices[0]["host"], "192.0.2.10")

    def test_usb_transport_remains_visible_when_cached_wireless_endpoint_is_stale(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "devices": [
                        {
                            "udid": "00008150-TEST",
                            "name": "Test iPhone",
                            "connection_type": "usb",
                            "state": "device",
                            "remote_paired": True,
                        }
                    ]
                },
            ),
            patch(
                "mobile_profiler.ios._load_endpoints",
                return_value={
                    "00008150-TEST": {"host": "192.0.2.10", "port": 49152}
                },
            ),
            patch("mobile_profiler.ios._save_endpoint"),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=False),
        ):
            devices, error = list_ios_devices("ios-python")

        self.assertIsNone(error)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["connection_type"], "usb")
        self.assertEqual(devices[0]["transports"], "usb")
        self.assertEqual(devices[0]["remote_xpc_ready"], "false")
        self.assertEqual(devices[0]["wireless_ready"], "false")

    def test_link_local_remote_xpc_is_not_treated_as_unplug_ready(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "devices": [
                        {
                            "udid": "00008150-TEST",
                            "name": "Test iPhone",
                            "connection_type": "usb",
                            "state": "device",
                            "remote_paired": True,
                        }
                    ]
                },
            ),
            patch(
                "mobile_profiler.ios._load_endpoints",
                return_value={
                    "00008150-TEST": {"host": "169.254.47.225", "port": 49152}
                },
            ),
            patch("mobile_profiler.ios._save_endpoint"),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=True),
        ):
            devices, error = list_ios_devices("ios-python")

        self.assertIsNone(error)
        self.assertEqual(devices[0]["endpoint_scope"], "link-local")
        self.assertEqual(devices[0]["transports"], "usb,remote-xpc")
        self.assertEqual(devices[0]["remote_xpc_ready"], "true")
        self.assertEqual(devices[0]["wireless_ready"], "false")
        self.assertEqual(devices[0]["unplug_ready"], "false")
        self.assertEqual(devices[0]["wireless_lan_candidate"], "false")

    def test_endpoint_scope_and_reachability_retry_are_conservative(self) -> None:
        self.assertEqual(_endpoint_scope("169.254.47.225"), "link-local")
        self.assertEqual(_endpoint_scope("192.168.21.50"), "private-lan")
        self.assertEqual(_endpoint_scope("127.0.0.1"), "local-only")
        with patch(
            "mobile_profiler.ios.socket.create_connection",
            side_effect=[OSError("wrong USB-NCM route"), nullcontext()],
        ) as connect:
            self.assertTrue(_endpoint_reachable("169.254.47.225", 49152))
        self.assertEqual(connect.call_count, 2)

    def test_windows_route_adapter_distinguishes_bluetooth_pan_and_wifi(self) -> None:
        bluetooth = {
            "interface_alias": "蓝牙网络连接",
            "interface_description": "Bluetooth Device (Personal Area Network)",
            "physical_media_type": "BlueTooth",
            "ndis_physical_medium": 10,
            "pnp_device_id": r"BTH\MS_BTHPAN\TEST",
        }
        wifi = {
            "interface_alias": "WLAN",
            "interface_description": "MediaTek Wi-Fi 6 Wireless LAN Card",
            "physical_media_type": "Native 802.11",
            "ndis_physical_medium": 9,
        }
        ethernet = {
            "interface_alias": "Ethernet",
            "interface_description": "PCIe GbE Family Controller",
            "physical_media_type": "802.3",
            "ndis_physical_medium": 0,
        }

        self.assertEqual(_classify_windows_route_adapter(bluetooth), "bluetooth-pan")
        self.assertEqual(_classify_windows_route_adapter(wifi), "wifi")
        self.assertEqual(_classify_windows_route_adapter(ethernet), "unknown")

    def test_windows_endpoint_route_uses_best_route_without_ip_heuristics(self) -> None:
        completed = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "interface_alias": "WLAN",
                    "interface_description": "Wi-Fi adapter",
                    "interface_index": 23,
                    "physical_media_type": "Native 802.11",
                    "ndis_physical_medium": 9,
                    "local_address": "192.168.31.125",
                }
            ),
            stderr="",
        )
        with (
            patch("mobile_profiler.ios.sys.platform", "win32"),
            patch("mobile_profiler.ios.shutil.which", return_value="powershell.exe"),
            patch("mobile_profiler.ios._run_subprocess", return_value=completed) as run,
        ):
            route = _windows_endpoint_route("172.20.10.1")

        self.assertEqual(route["interface_alias"], "WLAN")
        command = run.call_args.args[0]
        self.assertIn("Find-NetRoute", command[-1])
        self.assertEqual(
            run.call_args.kwargs["env"]["MOBILE_PROFILER_IOS_ENDPOINT_HOST"],
            "172.20.10.1",
        )

    def test_live_route_transport_overrides_cached_bluetooth_transport(self) -> None:
        cached = {
            "host": "172.20.10.1",
            "wireless_transport": "bluetooth-pan",
        }
        with patch(
            "mobile_profiler.ios._windows_endpoint_route",
            return_value={
                "interface_alias": "WLAN",
                "physical_media_type": "Native 802.11",
                "ndis_physical_medium": 9,
                "local_address": "192.168.31.125",
            },
        ):
            details = _ios_wireless_transport_details("172.20.10.1", cached)

        self.assertEqual(details["wireless_transport"], "wifi")
        self.assertEqual(details["transport_source"], "windows-route")

    def test_unreachable_cached_wireless_endpoint_is_removed_from_device_list(self) -> None:
        cached = {
            "00008150-TEST": {
                "host": "192.0.2.10",
                "port": 49152,
                "model": "Test iPhone",
            }
        }
        with (
            patch("mobile_profiler.ios._run_bridge_json", return_value={"devices": []}),
            patch("mobile_profiler.ios._load_endpoints", return_value=cached),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=False),
        ):
            devices, error = list_ios_devices("ios-python")

        self.assertEqual(devices, [])
        self.assertIn("no longer reachable", error or "")
        self.assertIn("192.0.2.10:49152", error or "")

    def test_unreachable_discovered_wireless_endpoint_is_removed_from_device_list(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "devices": [
                        {
                            "udid": "00008150-TEST",
                            "name": "Test iPhone",
                            "connection_type": "wireless",
                            "state": "device",
                            "host": "192.0.2.10",
                            "port": 49152,
                        }
                    ]
                },
            ),
            patch("mobile_profiler.ios._load_endpoints", return_value={}),
            patch("mobile_profiler.ios._save_endpoint"),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=False),
        ):
            devices, error = list_ios_devices("ios-python")

        self.assertEqual(devices, [])
        self.assertIn("no longer reachable", error or "")

    def test_pair_requires_a_reachable_wifi_endpoint_before_reporting_success(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "udid": "00008150-TEST",
                    "endpoint": None,
                    "device": {"name": "Test iPhone"},
                },
            ),
            patch("mobile_profiler.ios._save_endpoint") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "no reachable RemoteXPC endpoint"):
                pair_ios_device("ios:00008150-TEST", "ios-python", 12.0)

        save.assert_not_called()

    def test_pair_reports_the_unreachable_endpoint_address(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "udid": "00008150-TEST",
                    "endpoint": {"host": "192.0.2.10", "port": 49152},
                },
            ),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=False),
            patch("mobile_profiler.ios._save_endpoint") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "192.0.2.10:49152"):
                pair_ios_device("ios:00008150-TEST", "ios-python", 12.0)

        save.assert_not_called()

    def test_pair_marks_link_local_endpoint_as_remote_xpc_not_unplug_ready(self) -> None:
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "udid": "00008150-TEST",
                    "endpoint": {"host": "169.254.47.225", "port": 49152},
                    "device": {"name": "Test iPhone"},
                },
            ),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=True),
            patch("mobile_profiler.ios._save_endpoint") as save,
        ):
            result = pair_ios_device("ios:00008150-TEST", "ios-python", 12.0)

        self.assertTrue(result["connected"])
        self.assertEqual(result["endpoint"]["scope"], "link-local")
        self.assertTrue(result["endpoint"]["remote_xpc_ready"])
        self.assertFalse(result["endpoint"]["wireless_lan_candidate"])
        self.assertFalse(result["endpoint"]["unplug_ready"])
        save.assert_called_once()

    def test_bluetooth_pan_updates_the_cached_remote_pairing_endpoint(self) -> None:
        cached = {
            "00008150-TEST": {
                "host": "169.254.47.225",
                "port": 49152,
                "model": "Test iPhone",
                "product_type": "iPhone18,2",
            }
        }
        with (
            patch("mobile_profiler.ios.sys.platform", "win32"),
            patch("mobile_profiler.ios._load_endpoints", return_value=cached),
            patch(
                "mobile_profiler.ios._run_windows_bluetooth_pan",
                return_value={
                    "adapter_name": "Bluetooth Device (Personal Area Network)",
                    "address": "172.20.10.3",
                    "gateway": "172.20.10.1",
                    "link_speed": "3 Mbps",
                    "already_connected": False,
                },
            ) as connect,
            patch("mobile_profiler.ios._endpoint_reachable", return_value=True),
            patch("mobile_profiler.ios._save_endpoint") as save,
        ):
            result = connect_ios_bluetooth(
                "ios:00008150-TEST",
                "ios-python",
                30.0,
            )

        connect.assert_called_once_with("Test iPhone", 30.0)
        save.assert_called_once_with(
            "00008150-TEST",
            "172.20.10.1",
            49152,
            cached["00008150-TEST"],
            wireless_transport="bluetooth-pan",
            transport_source="bluetooth-connect",
            local_address="172.20.10.3",
            adapter_name="Bluetooth Device (Personal Area Network)",
        )
        self.assertTrue(result["connected"])
        self.assertEqual(result["transport"], "bluetooth-pan")
        self.assertEqual(result["endpoint"]["wireless_transport"], "bluetooth-pan")
        self.assertEqual(result["address"], "172.20.10.3")
        self.assertEqual(result["endpoint"]["scope"], "private-lan")
        self.assertTrue(result["endpoint"]["remote_xpc_ready"])
        self.assertTrue(result["endpoint"]["wireless_lan_candidate"])

    def test_windows_bluetooth_helper_parses_the_powershell_result(self) -> None:
        completed = Mock(
            returncode=0,
            stdout=(
                "diagnostic\n"
                '{"adapter_name":"Bluetooth PAN","address":"172.20.10.3",'
                '"gateway":"172.20.10.1","already_connected":false}\n'
            ),
            stderr="",
        )
        with (
            patch("mobile_profiler.ios.sys.platform", "win32"),
            patch("mobile_profiler.ios.shutil.which", return_value="powershell.exe"),
            patch("mobile_profiler.ios._run_subprocess", return_value=completed) as run,
        ):
            result = _run_windows_bluetooth_pan("Test iPhone", 20.0)

        self.assertEqual(result["gateway"], "172.20.10.1")
        command = run.call_args.args[0]
        self.assertIn("[ScriptBlock]::Create", command[-1])
        self.assertIn("MOBILE_PROFILER_IOS_BLUETOOTH_NAME", run.call_args.kwargs["env"])
        self.assertEqual(
            run.call_args.kwargs["env"]["MOBILE_PROFILER_IOS_BLUETOOTH_NAME"],
            "Test iPhone",
        )

    def test_bluetooth_pan_requires_windows_and_existing_remote_pairing(self) -> None:
        with patch("mobile_profiler.ios.sys.platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "only on Windows"):
                connect_ios_bluetooth("ios:00008150-TEST", "ios-python")

        with (
            patch("mobile_profiler.ios.sys.platform", "win32"),
            patch("mobile_profiler.ios._load_endpoints", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "Create iOS RemotePairing"):
                connect_ios_bluetooth("ios:00008150-TEST", "ios-python")

    def test_bluetooth_pan_reports_an_unreachable_remote_pairing_port(self) -> None:
        cached = {
            "00008150-TEST": {
                "host": "169.254.47.225",
                "port": 49152,
                "model": "Test iPhone",
            }
        }
        with (
            patch("mobile_profiler.ios.sys.platform", "win32"),
            patch("mobile_profiler.ios._load_endpoints", return_value=cached),
            patch(
                "mobile_profiler.ios._run_windows_bluetooth_pan",
                return_value={
                    "address": "172.20.10.3",
                    "gateway": "172.20.10.1",
                },
            ),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=False),
            patch("mobile_profiler.ios._save_endpoint") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "172.20.10.1:49152"):
                connect_ios_bluetooth("ios:00008150-TEST", "ios-python")

        save.assert_not_called()

    def test_cached_wireless_endpoint_survives_sidecar_discovery_failure(self) -> None:
        cached = {
            "00008150-TEST": {
                "host": "192.0.2.10",
                "port": 49152,
                "model": "Test iPhone",
                "product_type": "iPhone18,2",
            }
        }
        with (
            patch(
                "mobile_profiler.ios._run_bridge_json",
                side_effect=RuntimeError("Bonjour unavailable"),
            ),
            patch("mobile_profiler.ios._load_endpoints", return_value=cached),
            patch("mobile_profiler.ios._endpoint_reachable", return_value=True),
            patch(
                "mobile_profiler.ios._ios_wireless_transport_details",
                return_value={
                    "wireless_transport": "bluetooth-pan",
                    "transport_source": "windows-route",
                    "local_address": "172.20.10.3",
                    "adapter_name": "Bluetooth PAN",
                    "adapter_index": 13,
                },
            ),
        ):
            devices, error = list_ios_devices("ios-python")
            selected = select_ios_device("ios:00008150-TEST", "ios-python")

        self.assertIn("Bonjour unavailable", error or "")
        self.assertEqual(devices[0]["state"], "device")
        self.assertEqual(devices[0]["connection_type"], "wireless")
        self.assertEqual(devices[0]["wireless_transport"], "bluetooth-pan")
        self.assertEqual(devices[0]["transport_adapter"], "Bluetooth PAN")
        self.assertEqual(devices[0]["remote_xpc_ready"], "true")
        self.assertEqual(devices[0]["unplug_ready"], "true")
        self.assertEqual(selected["serial"], "ios:00008150-TEST")

    def test_ios_probe_preserves_wireless_transport_for_run_metadata(self) -> None:
        selected = {
            "serial": "ios:00008150-TEST",
            "udid": "00008150-TEST",
            "host": "172.20.10.1",
            "port": "49152",
            "wireless_transport": "bluetooth-pan",
            "transport_source": "windows-route",
            "transport_local_address": "172.20.10.3",
            "transport_adapter": "Bluetooth PAN",
            "transport_adapter_index": "13",
            "remote_xpc_ready": "true",
            "unplug_ready": "true",
            "wireless_lan_candidate": "true",
            "endpoint_scope": "private-lan",
        }
        with (
            patch("mobile_profiler.ios.select_ios_device", return_value=selected),
            patch(
                "mobile_profiler.ios._run_bridge_json",
                return_value={
                    "connection": {"host": "172.20.10.1", "port": 49152},
                    "device": {"model": "Test iPhone"},
                },
            ),
            patch("mobile_profiler.ios._save_endpoint"),
        ):
            result = probe_ios_device("ios:00008150-TEST", "ios-python")

        connection = result["connection"]
        self.assertEqual(connection["transport"], "bluetooth-pan")
        self.assertEqual(connection["transport_label"], "Bluetooth PAN")
        self.assertEqual(connection["local_address"], "172.20.10.3")
        self.assertEqual(connection["adapter_name"], "Bluetooth PAN")

    def test_ios_sidecar_events_are_journaled_as_normalized_samples(self) -> None:
        events = [
            {
                "type": "ready",
                "clock": {
                    "host_epoch_s": 1000.0,
                    "host_monotonic_s": 10.0,
                    "device_uptime_s": 100.0,
                    "round_trip_ms": 0.0,
                },
            },
            {
                "type": "sample",
                "sample": {
                    "index": 0,
                    "elapsed_s": 0.0,
                    "uptime_s": 100.0,
                    "current_ma": 300.0,
                    "signed_current_ma": -300.0,
                    "voltage_mv": 4300.0,
                    "power_mw": 1290.0,
                    "direction": "discharging",
                    "cpu_pct": 20.0,
                    "power_source": "ios_power_telemetry_system_load",
                    "power_sample_age_s": 2.0,
                    "collector_cpu_pct": 6.0,
                },
            },
            {
                "type": "context",
                "context": {
                    "uptime_s": 100.0,
                    "foreground_package": "Example",
                    "foreground_activity": "Example",
                    "source": "ios_dvt_notifications",
                },
            },
            {
                "type": "end",
                "battery": {"level_pct": 80, "voltage_mv": 4290.0},
                "stats": {"sample_count": 1, "average_collector_cpu_pct": 6.0},
            },
        ]

        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO(
                    "".join(json.dumps(item) + "\n" for item in events)
                )
                self.stderr = io.StringIO("")

            def poll(self) -> int:
                return 0

            def wait(self, timeout: object = None) -> int:
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                RunJournal(root) as journal,
                patch("mobile_profiler.ios.subprocess.Popen", return_value=FakeProcess()),
            ):
                result = collect_ios_session(
                    "ios-python",
                    {
                        "udid": "00008150-TEST",
                        "host": "192.0.2.10",
                        "port": "49152",
                    },
                    2,
                    1.0,
                    journal,
                    checkpoint_interval_s=30.0,
                    reconnect_timeout_s=10.0,
                    system_monitor_enabled=True,
                    process_interval_s=10.0,
                )
            stream = (root / "raw" / "sampler-stream.txt").read_text(encoding="utf-8")
            contexts = load_contexts(root)

        self.assertEqual(result.sample_count, 1)
        self.assertEqual(result.stats["average_collector_cpu_pct"], 6.0)
        self.assertIn('N|{"index":0', stream)
        self.assertEqual(contexts[0].source, "ios_dvt_notifications")


class ReportTests(unittest.TestCase):
    def test_harmony_performance_context_labels_backlight_as_raw_not_nits(self) -> None:
        rows = _performance_context_rows(
            {
                "platform": "harmony",
                "performance": {
                    "display_width_px": 1320,
                    "display_height_px": 2856,
                    "brightness_raw": 23482,
                },
                "thermal": {},
            }
        )
        self.assertIn("RenderService 背光原始值 23482（非 nit、非热限亮）", rows)

    def test_harmony_capture_copy_does_not_claim_thermal_limit_semantics(self) -> None:
        configuration = resolve_capture_configuration(
            "performance",
            "harmony",
            "performance-standard",
        )
        rows = {item["key"]: item for item in configuration["feature_rows"]}
        self.assertEqual(rows["thermal"]["label"], "温度传感器")
        self.assertIn("不提供可验证的热严重度", rows["thermal"]["description"])
        self.assertIn("不是 nit 或热限亮上限", rows["foreground_window"]["description"])

    def test_harmony_report_copy_calls_temperature_context_not_thermal_status(self) -> None:
        metadata = {
            "platform": "harmony",
            "test_mode": "performance",
            "capture_configuration": {"backend": "harmony_smartperf"},
        }
        profile = _report_mode_profile(
            _report_platform_profile(metadata, {}),
            metadata,
        )
        self.assertEqual(profile["report_subtitle"], "HarmonyOS 帧节奏、资源与温度分析")
        self.assertIn("资源与温度上下文", profile["test_item_copy"])
        self.assertNotIn("热状态", profile["test_item_copy"])

    def test_report_does_not_render_missing_consumption_power_as_zero(self) -> None:
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "charging ios",
                    "platform": "ios",
                    "test_mode": "power",
                    "device": {},
                    "cpu_policies": [],
                },
                "analysis": {
                    "summary": {
                        "duration_s": 1.0,
                        "sample_count": 2,
                        "power_valid_for_consumption": False,
                        "average_power_mw": None,
                        "p95_power_mw": None,
                        "observed_power_average_mw": 5000.0,
                        "power_sources": ["ios_power_telemetry_system_load"],
                    },
                    "cpu": {"clusters": [], "timeline": []},
                    "gpu": {},
                    "thermal": {},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [],
                },
                "samples": [
                    {
                        "elapsed_s": float(index),
                        "uptime_s": 100.0 + index,
                        "power_mw": 5000.0,
                        "current_ma": 1000.0,
                        "voltage_mv": 4300.0,
                        "direction": "charging",
                        "external_power": True,
                        "power_valid_for_consumption": False,
                        "power_source": "ios_power_telemetry_system_load",
                    }
                    for index in range(2)
                ],
            }
        )

        self.assertIn('"average_power_mw":null', fragment)
        self.assertNotIn('"average_power_mw":0', fragment)
        self.assertIn("iOS 整机原始 SystemLoad 通道", fragment)
        self.assertIn("外部供电 · 仅原始流量", fragment)
        self.assertIn("本次无法评价耗电或续航", fragment)
        self.assertNotIn("本次没有形成可独立陈述的异常结论", fragment)

    def test_report_source_table_only_lists_sources_observed_in_this_run(self) -> None:
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "actual sources",
                    "platform": "ios",
                    "test_mode": "power",
                    "device": {},
                    "cpu_policies": [],
                },
                "analysis": {
                    "summary": {"duration_s": 1.0, "sample_count": 2},
                    "cpu": {"clusters": [], "timeline": []},
                    "gpu": {"frequency_available": False, "load_available": False},
                    "thermal": {},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [
                        {
                            "metric": "GPU activity",
                            "source": "UNSEEN_GPU_SOURCE",
                            "kind": "measured counters",
                        },
                        {
                            "metric": "Battery current and voltage",
                            "source": "OBSERVED_BATTERY_SOURCE",
                            "kind": "measured",
                        },
                        {
                            "metric": "Whole-device battery power",
                            "source": "UNSEEN_SYSTEMLOAD_SOURCE",
                            "kind": "measured low-rate telemetry",
                        },
                    ],
                },
                "samples": [
                    {
                        "elapsed_s": float(index),
                        "uptime_s": 100.0 + index,
                        "current_ma": 300.0,
                        "voltage_mv": 4300.0,
                        "power_mw": 1290.0,
                        "power_source": "ios_battery_current_voltage",
                    }
                    for index in range(2)
                ],
            }
        )

        rendered_markup = fragment.split("<script>", 1)[0]
        self.assertNotIn("UNSEEN_GPU_SOURCE", rendered_markup)
        self.assertNotIn("UNSEEN_SYSTEMLOAD_SOURCE", rendered_markup)
        self.assertIn("OBSERVED_BATTERY_SOURCE", rendered_markup)

    def test_report_exposes_persistent_time_range_delete_controls(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=300.0,
                signed_current_ma=-300.0,
                voltage_mv=3800.0,
                power_mw=1140.0,
                direction="discharging",
                cpu_pct=20.0,
            )
            for index in range(3)
        ]
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "range delete",
                    "platform": "android",
                    "test_mode": "power",
                    "device": {},
                    "cpu_policies": [],
                },
                "analysis": {
                    "summary": {"duration_s": 2.0, "sample_count": 3},
                    "cpu": {"clusters": [], "timeline": []},
                    "gpu": {},
                    "thermal": {},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [],
                },
                "samples": [sample.__dict__ for sample in samples],
            }
        )

        self.assertIn('id="report-range-start"', fragment)
        self.assertIn('id="report-range-end"', fragment)
        self.assertIn('id="report-range-summary"', fragment)
        self.assertIn('id="report-range-statistics"', fragment)
        self.assertIn('id="report-range-summary-note"', fragment)
        self.assertIn('id="report-range-status"', fragment)
        self.assertIn('id="delete-report-range" type="button" disabled', fragment)
        self.assertIn("在任意时间图上横向拖动框选", fragment)
        self.assertIn("function setExactReportRange", fragment)
        self.assertIn("function attachTimeRangeBrush", fragment)
        self.assertIn('class: "time-range-brush"', fragment)
        self.assertIn('overlay.addEventListener("pointerdown"', fragment)
        self.assertIn('overlay.addEventListener("pointermove"', fragment)
        self.assertIn('overlay.addEventListener("pointerup"', fragment)
        self.assertIn("overlay.setPointerCapture", fragment)
        self.assertIn("refreshReportRangeSelectionOverlays", fragment)
        self.assertIn('root.querySelectorAll("svg").forEach', fragment)
        self.assertIn('sourceKey: metric.key', fragment)
        self.assertIn('sourceKey: "frame_flow"', fragment)
        self.assertIn('/api/range-summary', fragment)
        self.assertIn('start_elapsed_s: selection.startElapsed', fragment)
        self.assertIn('end_elapsed_s: selection.endElapsed', fragment)
        self.assertIn('result.full_resolution === true', fragment)
        self.assertIn("已使用服务端全量数据重算", fragment)
        self.assertIn("删除后会用全量", fragment)
        self.assertIn('/api/report/delete-range', fragment)
        self.assertIn('start_uptime_s: Number(selection.startUptime)', fragment)
        self.assertIn('end_uptime_s: Number(selection.endUptime)', fragment)
        self.assertIn("raw-delete-window", fragment)
        self.assertIn("raw-excluded-window", fragment)

    def test_report_excluded_ranges_merge_and_split_duration_events(self) -> None:
        ranges = normalize_report_excluded_ranges(
            [
                {"start_uptime_s": 103.0, "end_uptime_s": 105.0},
                {"start_uptime_s": 104.0, "end_uptime_s": 106.0},
            ]
        )
        events = [
            ExternalEvent(100.0, "阶段", "运行", duration_s=10.0),
            ExternalEvent(104.0, "瞬时", "标记"),
            ExternalEvent(111.0, "保留", "标记"),
        ]

        filtered = _filter_report_events(events, ranges)

        self.assertEqual(ranges, [(103.0, 106.0)])
        self.assertEqual(
            [(event.device_uptime_s, event.duration_s) for event in filtered[:2]],
            [(100.0, 3.0), (106.0, 4.0)],
        )
        self.assertTrue(all(event.metadata.get("report_edit_split") for event in filtered[:2]))
        self.assertEqual(filtered[-1].name, "保留")

    def test_finalize_run_reapplies_report_edits_without_rewriting_source_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "raw"
            raw_dir.mkdir()
            raw_path = raw_dir / "sampler-stream.txt"
            raw_path.write_text("", encoding="utf-8")
            metadata = {
                "title": "edited report",
                "platform": "android",
                "test_mode": "power",
                "sample_interval_s": 1.0,
                "battery_start": {
                    "voltage_mv": 3800.0,
                    "status": "discharging",
                },
                "device": {},
                "cpu_policies": [],
            }
            (root / "metadata.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            samples = [
                Sample(
                    index=index,
                    elapsed_s=float(index),
                    uptime_s=100.0 + index,
                    current_ma=300.0,
                    signed_current_ma=-300.0,
                    voltage_mv=3800.0,
                    power_mw=1140.0,
                    direction="discharging",
                    cpu_pct=20.0,
                )
                for index in range(7)
            ]
            write_samples_csv(root / "samples.csv", samples, metadata)
            contexts = [
                ContextSample(101.0, foreground_package="deleted"),
                ContextSample(106.0, foreground_package="kept"),
            ]
            events = [ExternalEvent(101.0, "deleted", "marker"), ExternalEvent(106.0, "kept", "marker")]
            write_jsonl(root / "contexts.jsonl", contexts)
            write_jsonl(root / "events.jsonl", events)
            write_report_excluded_ranges(root, [(100.0, 101.0), (103.0, 104.0)])
            original_raw = raw_path.read_bytes()
            original_contexts = (root / "contexts.jsonl").read_bytes()
            original_events = (root / "events.jsonl").read_bytes()

            first_analysis, _ = finalize_run(root)
            first_samples = read_samples_csv(root / "samples.csv")
            second_analysis, _ = finalize_run(root)
            second_samples = read_samples_csv(root / "samples.csv")
            source_samples = read_samples_csv(root / "report-source-samples.csv")
            stored_metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
            final_raw = raw_path.read_bytes()
            final_contexts = (root / "contexts.jsonl").read_bytes()
            final_events = (root / "events.jsonl").read_bytes()

        self.assertEqual([sample.uptime_s for sample in first_samples], [102.0, 105.0, 106.0])
        self.assertEqual([sample.uptime_s for sample in second_samples], [102.0, 105.0, 106.0])
        self.assertEqual([sample.elapsed_s for sample in second_samples], [0.0, 3.0, 4.0])
        self.assertEqual([sample.uptime_s for sample in source_samples], [float(value) for value in range(100, 107)])
        self.assertAlmostEqual(first_analysis["summary"]["covered_duration_s"], 1.0)
        self.assertAlmostEqual(second_analysis["summary"]["covered_duration_s"], 1.0)
        self.assertEqual(stored_metadata["report_edits"]["time_origin_uptime_s"], 102.0)
        self.assertEqual(stored_metadata["report_edits"]["excluded_sample_count"], 4)
        self.assertEqual(final_raw, original_raw)
        self.assertEqual(final_contexts, original_contexts)
        self.assertEqual(final_events, original_events)

    def test_report_lists_and_marks_each_brightness_throttling_point(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index * 10),
                uptime_s=100.0 + index * 10,
                current_ma=300.0,
                signed_current_ma=-300.0,
                voltage_mv=3800.0,
                power_mw=1140.0,
                direction="discharging",
                cpu_pct=45.0,
            )
            for index in range(4)
        ]
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example.game",
                foreground_activity=".GameActivity",
                screen_state="Awake",
                refresh_rate_hz=60.0,
            )
        ]
        thermal_snapshots = [
            ThermalSnapshot(
                uptime_s=uptime_s,
                host_epoch_s=1000.0 + uptime_s,
                status=2 if applied else 0,
                temperatures=[{"name": "skin", "value_c": temperature}],
                cooling_devices=[{"name": "lcd-backlight", "value": cooling}],
                display_brightness={
                    "available": True,
                    "screen_state": "ON",
                    "setting_raw": 204.0,
                    "setting_float": 0.8,
                    "current_screen_brightness": 0.8,
                    "screen_brightness": effective,
                    "adjusted_brightness": effective,
                    "thermal_cap": cap,
                    "thermal_applied": applied,
                    "thermal_status": 3 if applied else 0,
                    "brightness_max_reason": 1 if applied else 0,
                },
            )
            for uptime_s, effective, cap, applied, cooling, temperature in (
                (100.0, 0.8, 1.0, False, 0.0, 38.0),
                (110.0, 0.8, 1.0, False, 1.0, 43.0),
                (120.0, 0.48, 0.48, True, 2.0, 45.0),
                (130.0, 0.8, 1.0, False, 0.0, 40.0),
            )
        ]
        metadata = {
            "platform": "android",
            "test_mode": "performance",
            "title": "Brightness throttle markers",
            "sample_interval_s": 10.0,
            "cpu_policies": [],
            "gpu_source": None,
            "battery_start": {"level_pct": 80, "temperature_c": 32.0},
            "battery_end": {"level_pct": 80, "temperature_c": 33.0},
            "device": {"brand": "Test", "model": "Phone", "android": "16"},
            "system_monitor": {"enabled": True},
            "capture_configuration": resolve_capture_configuration(
                "performance",
                "android",
                "performance-standard",
            ),
        }

        analysis = analyze_run(
            samples,
            metadata,
            {},
            [],
            contexts=contexts,
            thermal_snapshots=thermal_snapshots,
        )
        fragment = build_report_fragment(
            {
                "metadata": metadata,
                "analysis": analysis,
                "samples": [item.__dict__ for item in samples],
                "contexts": [item.__dict__ for item in contexts],
            }
        )

        self.assertEqual(analysis["brightness_throttling"]["point_count"], 2)
        self.assertEqual(
            [item["status"] for item in analysis["brightness_throttling"]["points"]],
            ["suspected", "confirmed"],
        )
        self.assertIn('data-panel="raw"', fragment)
        self.assertIn('key: "requested_brightness_pct"', fragment)
        self.assertIn('key: "effective_brightness_pct"', fragment)
        self.assertIn('key: "brightness_thermal_cap_pct"', fragment)
        self.assertIn('features: ["foreground_window", "thermal"]', fragment)
        self.assertIn("appendBrightnessMarkers", fragment)
        self.assertIn("确认热降亮", fragment)
        self.assertIn("疑似热降亮", fragment)

    def test_power_and_performance_modes_separate_analysis_and_report_focus(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=250.0 + index * 8,
                signed_current_ma=-250.0 - index * 8,
                voltage_mv=3800.0,
                power_mw=950.0 + index * 45.0,
                direction="discharging",
                cpu_pct=20.0 + index * 4,
                gpu_load_pct=30.0 + index * 3,
                gpu_frequency_mhz=500.0 + index * 25,
                memory_frequency_mhz=600.0 + index * 60,
                battery_temperature_c=32.0 + index * 0.2,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(31)
        ]
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example.game",
                foreground_activity=".GameActivity",
                refresh_rate_hz=60.0,
                performance={
                    "platform": "android",
                    "foreground_window_name": "com.example.game/.GameActivity",
                    "frame_counter_total": 100,
                    "frame_counter_deadline_missed": 1,
                    "frame_histogram_ms": {"10": 80, "20": 20},
                },
            ),
            ContextSample(
                110.0,
                foreground_package="com.example.game",
                foreground_activity=".GameActivity",
                refresh_rate_hz=60.0,
                performance={
                    "platform": "android",
                    "foreground_window_name": "com.example.game/.GameActivity",
                    "frame_counter_total": 700,
                    "frame_counter_deadline_missed": 7,
                    "frame_histogram_ms": {"10": 560, "20": 140},
                    "render_width_px": 1080,
                    "render_height_px": 2400,
                },
            ),
        ]
        base_metadata = {
            "platform": "android",
            "title": "Mode separation",
            "sample_interval_s": 1.0,
            "foreground_package": "com.example.game",
            "session_mode": False,
            "cpu_policies": [],
            "gpu_source": None,
            "battery_start": {"level_pct": 80, "temperature_c": 32.0},
            "battery_end": {"level_pct": 79, "temperature_c": 34.0},
            "runtime_settings_start": {
                "system.screen_brightness": 180,
                "system.peak_refresh_rate": 60,
                "global.low_power": 0,
            },
            "memory_source": {
                "name": "dmc",
                "frequency_path": "/sys/class/devfreq/dmc/cur_freq",
                "maximum_mhz": 1200.0,
            },
            "device": {"brand": "Test", "model": "Phone", "android": "16"},
            "system_monitor": {"enabled": True},
        }
        system_snapshots = [
            SystemSnapshot(
                uptime_s=105.0,
                host_epoch_s=1000.0,
                processes=[
                    {"pid": 10, "name": "com.example.game", "cpu_pct": 55.0},
                    {"pid": 20, "name": "background.worker", "cpu_pct": 25.0},
                ],
                threads=[
                    {
                        "pid": 10,
                        "tid": 11,
                        "name": "RenderThread",
                        "process": "com.example.game",
                        "cpu_pct": 35.0,
                    }
                ],
            )
        ]
        raw_outputs = {
            "runtime_settings_end": (
                "SETTING|system|screen_brightness|180\n"
                "SETTING|system|peak_refresh_rate|60\n"
                "SETTING|global|low_power|0\n"
            ),
            "batterystats_usage": "Uid u0a1: 10.0 mAh",
            "batterystats": "Kernel Wakelock fake 10s",
        }

        power_metadata = {**base_metadata, "test_mode": "power"}
        power = analyze_run(
            samples,
            power_metadata,
            raw_outputs,
            [],
            contexts=contexts,
            system_snapshots=system_snapshots,
        )
        self.assertEqual(power["test_mode"], "power")
        self.assertTrue(power["power_pressure"]["available"])
        self.assertTrue(power["memory"]["available"])
        self.assertTrue(power["runtime_settings"]["available"])
        self.assertTrue(power["render_performance"]["analysis_disabled"])

        performance_metadata = {
            **base_metadata,
            "test_mode": "performance",
            "capture_configuration": resolve_capture_configuration(
                "performance",
                "android",
                "performance-standard",
                enable_features=["process_snapshots"],
            ),
        }
        performance = analyze_run(
            samples,
            performance_metadata,
            raw_outputs,
            [],
            contexts=contexts,
            events=[ExternalEvent(100.0, "战斗", "游戏", duration_s=10.0)],
            system_snapshots=system_snapshots,
        )
        self.assertEqual(performance["test_mode"], "performance")
        self.assertEqual(performance["components"], [])
        self.assertEqual(performance["wakelocks"], [])
        self.assertTrue(performance["battery_usage"]["analysis_disabled"])
        self.assertTrue(performance["applications"]["analysis_disabled"])
        self.assertEqual(performance["target_app"], {"package": "com.example.game"})
        self.assertTrue(performance["power_pressure"]["analysis_disabled"])
        self.assertEqual(performance["test_items"]["analysis_mode"], "performance")
        self.assertNotIn("power_correlation", performance["system"]["top_processes"][0])
        self.assertNotIn(
            "BatteryStats",
            {item["source"] for item in performance["data_sources"]},
        )

        power_fragment = build_report_fragment(
            {
                "metadata": power_metadata,
                "analysis": power,
                "samples": [item.__dict__ for item in samples],
                "contexts": [item.__dict__ for item in contexts],
            }
        )
        performance_fragment = build_report_fragment(
            {
                "metadata": performance_metadata,
                "analysis": performance,
                "samples": [item.__dict__ for item in samples],
                "contexts": [item.__dict__ for item in contexts],
            }
        )
        self.assertIn('id="mobile-profiler"', power_fragment)
        self.assertIn('data-test-mode="power"', power_fragment)
        self.assertEqual(power_fragment.count('class="nav-tab"'), 2)
        self.assertEqual(power_fragment.count('class="app-view"'), 2)
        self.assertIn('data-view="raw"', power_fragment)
        self.assertIn('data-view="analysis"', power_fragment)
        self.assertIn('data-panel="raw"', power_fragment)
        self.assertIn('data-panel="analysis"', power_fragment)
        self.assertIn("原始数据随时间变化", power_fragment)
        self.assertIn("分析结论", power_fragment)
        self.assertIn("资源与整机功率相关性", power_fragment)
        self.assertNotIn("系统设置变化", power_fragment)
        self.assertIn('data-test-mode="performance"', performance_fragment)
        self.assertEqual(performance_fragment.count('class="nav-tab"'), 2)
        self.assertEqual(performance_fragment.count('class="app-view"'), 2)
        self.assertIn('data-view="raw"', performance_fragment)
        self.assertIn('data-view="analysis"', performance_fragment)
        self.assertIn('key: "frame_rate_fps"', performance_fragment)
        self.assertIn('key: "one_percent_low_fps"', performance_fragment)
        self.assertIn('key: `frame_stage:${stageKey}`', performance_fragment)
        self.assertIn('if (stageKey === primaryFrameStageKey || stageKey === "display_scanout") return;', performance_fragment)
        self.assertIn("按测试项形成的结论证据", performance_fragment)
        self.assertNotIn("功耗贡献模型证据", performance_fragment)
        self.assertNotIn("<td><strong>渲染分辨率</strong></td>", performance_fragment)
        self.assertNotIn("<td><strong>插帧 / MEMC</strong></td>", performance_fragment)
        self.assertNotIn("内存频率 平均 / P95", performance_fragment)
        self.assertNotIn('data-view="overview"', power_fragment)
        self.assertNotIn('data-view="timeline"', performance_fragment)
        self.assertNotIn("@@", power_fragment)
        self.assertNotIn("@@", performance_fragment)

    def test_report_raw_small_multiples_filter_by_capture_feature_and_valid_data(self) -> None:
        capture_configuration = resolve_capture_configuration(
            "performance",
            "android",
            "performance-standard",
            disable_features=["gpu_metrics", "memory_frequency", "thermal"],
        )
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "raw metric filtering",
                    "platform": "android",
                    "test_mode": "performance",
                    "device": {},
                    "cpu_policies": [],
                    "capture_configuration": capture_configuration,
                },
                "samples": [
                    {
                        "elapsed_s": 0.0,
                        "uptime_s": 100.0,
                        "power_mw": 0.0,
                        "current_ma": 0.0,
                        "voltage_mv": 3800.0,
                        "cpu_pct": 0.0,
                        "gpu_load_pct": 77.0,
                        "gpu_frequency_mhz": 777.0,
                        "memory_frequency_mhz": 933.0,
                        "battery_temperature_c": 36.5,
                        "frequencies_mhz": {"policy0": 0.0},
                    }
                ],
                "analysis": {
                    "test_mode": "performance",
                    "summary": {"duration_s": 0.0, "sample_count": 1},
                    "cpu": {
                        "clusters": [
                            {
                                "name": "policy0",
                                "label": "Little",
                                "cores": [0, 1, 2, 3],
                            }
                        ],
                        "timeline": [],
                    },
                    "gpu": {"frequency_available": True, "load_available": True},
                    "performance": {
                        "frame_sample_count": 1,
                        "frame_rate_timeline": [
                            {
                                "uptime_s": 100.0,
                                "frame_rate_fps": 60.0,
                                "one_percent_low_fps": 52.0,
                                "frame_time_p95_ms": 17.0,
                            }
                        ],
                        "refresh_rate_timeline": [
                            {"uptime_s": 100.0, "refresh_rate_hz": 60.0}
                        ],
                    },
                    "render_performance": {},
                    "thermal": {"sensors": [], "timeline": []},
                    "scheduler": {},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [],
                },
            }
        )

        self.assertIn('id="raw-metric-grid"', fragment)
        self.assertIn("function rawMetricDefinitions()", fragment)
        self.assertIn("const captureFeatures = captureConfiguration.features || {};", fragment)
        self.assertIn("return !keys.length || captureFeatures[name] === true;", fragment)
        self.assertIn(
            "if (features.length && !features.some(featureEnabled)) return;",
            fragment,
        )
        self.assertIn("if (!points.length) return;", fragment)
        self.assertIn(
            "if (definition.requiresPositive && !points.some(point => point.value > 0)) return;",
            fragment,
        )
        self.assertIn('key: "power_mw"', fragment)
        self.assertIn('const powerChannel = reportPowerChannelPresentation();', fragment)
        self.assertIn('label: powerChannel.label', fragment)
        self.assertIn('source: powerChannel.source', fragment)
        self.assertIn('label: "电池流量功率（电流×电压）"', fragment)
        self.assertNotIn('label: "整机功耗"', fragment)
        self.assertIn('key: "current_ma"', fragment)
        self.assertIn('key: "voltage_mv"', fragment)
        self.assertIn('key: "cpu_pct"', fragment)
        self.assertIn('feature: "cpu_usage"', fragment)
        self.assertIn('key: `cpu_frequency:${cluster.name}`', fragment)
        self.assertIn('feature: "cpu_frequency"', fragment)
        self.assertIn('feature: "frame_rate"', fragment)
        self.assertIn('features: ["foreground_window", "frame_rate"]', fragment)
        self.assertIn('feature: "gpu_metrics"', fragment)
        self.assertIn('feature: "memory_frequency"', fragment)
        self.assertIn('feature: "thermal"', fragment)
        self.assertIn('const smartPerfThermal =', fragment)
        self.assertIn('HarmonyOS SmartPerf SP_daemon 温度字段', fragment)
        self.assertIn('source: batteryTemperatureSource', fragment)
        self.assertIn('source: `${thermalTelemetrySource}', fragment)
        self.assertIn('requiresPositive: true', fragment)
        self.assertIn('"gpu_metrics":false', fragment)
        self.assertIn('"memory_frequency":false', fragment)
        self.assertIn('"thermal":false', fragment)
        self.assertIn("本次没有形成可独立陈述的异常结论", fragment)

    def test_performance_report_without_frame_evidence_uses_unavailable_callout(self) -> None:
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "screen-off performance",
                    "platform": "harmony",
                    "test_mode": "performance",
                    "device": {},
                    "cpu_policies": [],
                    "capture_configuration": resolve_capture_configuration(
                        "performance", "harmony", "harmony-smartperf"
                    ),
                },
                "samples": [
                    {
                        "elapsed_s": 0.0,
                        "uptime_s": 100.0,
                        "power_mw": 8.0,
                        "current_ma": 2.0,
                        "voltage_mv": 4000.0,
                        "external_power": True,
                        "power_valid_for_consumption": False,
                    }
                ],
                "analysis": {
                    "summary": {"power_valid_for_consumption": False},
                    "cpu": {"clusters": [], "timeline": []},
                    "gpu": {},
                    "performance": {
                        "available": False,
                        "frame_evidence_available": False,
                        "frame_sample_count": 0,
                        "frame_rate_timeline": [],
                        "refresh_rate_timeline": [],
                        "frame_unavailable_reason": "屏幕未处于活动状态；SmartPerf FPS 不代表当前输出。",
                    },
                    "render_performance": {},
                    "thermal": {},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [],
                },
            }
        )

        self.assertIn("本次无法评价帧表现", fragment)
        self.assertIn("屏幕未处于活动状态", fragment)
        self.assertNotIn("本次没有形成可独立陈述的异常结论", fragment)

    def test_report_analysis_renders_each_supported_finding_once(self) -> None:
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "finding placement",
                    "platform": "android",
                    "test_mode": "power",
                    "device": {},
                    "cpu_policies": [],
                },
                "samples": [
                    {
                        "elapsed_s": 0.0,
                        "uptime_s": 100.0,
                        "power_mw": 1000.0,
                        "current_ma": 250.0,
                        "voltage_mv": 4000.0,
                    }
                ],
                "analysis": {
                    "test_mode": "power",
                    "summary": {"duration_s": 0.0, "sample_count": 1},
                    "findings": [
                        {
                            "level": "measured",
                            "title": "唯一结论标题",
                            "detail": "唯一结论正文",
                        }
                    ],
                    "cpu": {"clusters": [], "timeline": []},
                    "performance": {"frame_rate_timeline": []},
                    "render_performance": {},
                    "thermal": {},
                    "scheduler": {},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "data_sources": [],
                },
            }
        )

        analysis_start = fragment.index('data-panel="analysis"')
        self.assertEqual(fragment.count("唯一结论标题"), 1)
        self.assertEqual(fragment.count("唯一结论正文"), 1)
        self.assertGreater(fragment.index("唯一结论标题"), analysis_start)
        self.assertEqual(fragment.count('class="finding-list"'), 1)
        self.assertNotIn("本次没有形成可独立陈述的异常结论", fragment)

    def test_report_records_capture_controls_and_harmony_performance_restore(self) -> None:
        capture_configuration = resolve_capture_configuration(
            "performance",
            "harmony",
            "harmony-smartperf",
            disable_features=["touch_events"],
        )
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "Harmony performance ceiling",
                    "platform": "harmony",
                    "test_mode": "performance",
                    "device": {
                        "brand": "HUAWEI",
                        "model": "nova",
                        "harmony": "6.1",
                    },
                    "cpu_policies": [],
                    "capture_configuration": capture_configuration,
                    "device_performance_mode": {
                        "requested": True,
                        "supported": True,
                        "original_mode": 600,
                        "requested_mode": 602,
                        "active_mode": 602,
                        "applied": True,
                        "restored": True,
                        "restored_mode": 600,
                    },
                },
                "samples": [],
                "analysis": {
                    "test_mode": "performance",
                    "summary": {"duration_s": 8.0, "sample_count": 0},
                    "cpu": {"clusters": [], "timeline": []},
                    "gpu": {
                        "frequency_available": False,
                        "load_available": False,
                        "work_by_uid": [],
                    },
                    "system": {
                        "priority_activities": {"rows": [], "monitored": []},
                        "top_processes": [],
                        "hot_threads": [],
                    },
                    "performance": {},
                    "render_performance": {},
                    "thermal": {"sensors": [], "cooling_devices": []},
                    "scheduler": {
                        "cpusets": [],
                        "cpu_policies": [],
                        "hint_sessions": [],
                        "process_states": [],
                    },
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [],
                },
            }
        )
        self.assertIn('data-panel="raw"', fragment)
        self.assertIn("查看采集配置与数据来源", fragment)
        self.assertIn("Harmony SmartPerf 采集", fragment)
        self.assertIn("用户显式关闭以降低采集干扰", fragment)
        self.assertIn("power-shell setmode 602", fragment)
        self.assertIn("已启用并恢复", fragment)
        self.assertIn("原模式 600，测试模式 602，结束后模式 600", fragment)
        self.assertNotIn("@@CAPTURE_CONFIGURATION_ROWS@@", fragment)

    def test_harmony_analysis_and_report_use_hdc_sources_without_android_attribution(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=1_783_934_230.0 + index,
                current_ma=100.0 + index * 10,
                signed_current_ma=-100.0 - index * 10,
                voltage_mv=4320.0 - index,
                power_mw=(100.0 + index * 10) * (4320.0 - index) / 1000.0,
                direction="discharging",
                cpu_pct=20.0 + index,
                core_cpu_pct={"0": 20.0 + index, "1": 25.0 + index},
                cluster_cpu_pct={"policy0": 22.5 + index},
                frequencies_mhz={"policy0": 1300.0 + index * 100},
                battery_temperature_c=28.0,
                power_source="harmony_battery_service",
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(3)
        ]
        metadata = {
            "platform": "harmony",
            "title": "HarmonyOS smoke",
            "sample_interval_s": 1.0,
            "session_mode": False,
            "foreground_package": "yylx.danmaku.bili",
            "cpu_policies": [
                {
                    "name": "policy0",
                    "path": "hidumper --cpufreq",
                    "cluster_index": 0,
                    "label": "Performance",
                    "cores": [0, 1],
                    "min_khz": None,
                    "max_khz": 1_500_000,
                    "available_frequencies_khz": [],
                    "governor": None,
                    "core_control": {},
                }
            ],
            "gpu_source": None,
            "gpu_probe": {
                "provider": "HarmonyOS production shell",
                "model": "Kirin9010S",
                "reason": "GPU sysfs is restricted",
            },
            "device": {
                "brand": "HUAWEI",
                "model": "nova 16 Pro",
                "hardware": "HL1CPLM",
                "soc_model": "Kirin9010S",
                "harmony": "6.1.0.130",
            },
            "battery_start": {"level_pct": 90, "temperature_c": 28.0},
            "battery_end": {"level_pct": 90, "temperature_c": 28.0},
            "system_monitor": {"enabled": True},
        }
        analysis = analyze_run(
            samples,
            metadata,
            {},
            [],
            thermal_snapshots=[
                ThermalSnapshot(
                    uptime_s=samples[1].uptime_s,
                    host_epoch_s=1_783_934_231.0,
                    status=None,
                    temperatures=[
                        {
                            "name": "Battery",
                            "type": "Battery",
                            "value_c": 28.0,
                            "status": 0,
                            "status_available": False,
                        }
                    ],
                )
            ],
        )
        fragment = build_report_fragment(
            {
                "metadata": metadata,
                "analysis": analysis,
                "samples": [item.__dict__ for item in samples],
            }
        )

        sources = {item["source"] for item in analysis["data_sources"]}
        self.assertIn("HarmonyOS BatteryService via hidumper", sources)
        self.assertEqual(analysis["cpu"]["source"], "HarmonyOS /proc/stat + hidumper --cpufreq")
        self.assertIsNone(analysis["thermal"]["maximum_status"])
        self.assertIn("HarmonyOS 6.1.0.130", fragment)
        self.assertIn("HarmonyOS BatteryService · hidumper", fragment)
        self.assertIn('key: `cpu_frequency:${cluster.name}`', fragment)
        self.assertNotIn("Android BatteryStats、ADPF", fragment)
        self.assertIn("GPU sysfs is restricted", fragment)

    def test_ios_analysis_and_report_keep_physical_power_separate_from_dvt_scores(self) -> None:
        samples = [
            Sample(
                index=index,
                elapsed_s=float(index),
                uptime_s=100.0 + index,
                current_ma=300.0 + index,
                signed_current_ma=-300.0 - index,
                voltage_mv=4370.0 - index,
                power_mw=1277.0 + index * 10,
                direction="discharging",
                cpu_pct=20.0 + index,
                gpu_load_pct=10.0 + index,
                battery_temperature_c=31.0,
                power_source="ios_power_telemetry_system_load",
                power_sample_age_s=4.0 + index,
                collector_cpu_pct=6.0 + index,
                power_valid_for_consumption=True,
                external_power=False,
            )
            for index in range(3)
        ]
        metadata = {
            "platform": "ios",
            "title": "iOS smoke",
            "sample_interval_s": 1.0,
            "session_mode": False,
            "cpu_policies": [],
            "gpu_source": {"name": "Apple GPU", "source_type": "ios_dvt_graphics"},
            "gpu_probe": {"provider": "ios_dvt_graphics"},
            "device": {
                "brand": "Apple",
                "model": "Test iPhone",
                "product_type": "iPhone18,2",
                "hardware": "D84AP",
                "ios": "26.5.2",
            },
            "battery_start": {
                "level_pct": 80,
                "temperature_c": 31.0,
                "full_charge_capacity_mah": 4500,
            },
            "battery_end": {"level_pct": 80, "temperature_c": 31.1},
            "system_monitor": {"enabled": True},
        }
        analysis = analyze_run(
            samples,
            metadata,
            {},
            [],
            system_snapshots=[
                SystemSnapshot(
                    uptime_s=101.0,
                    host_epoch_s=1000.0,
                    processes=[
                        {
                            "pid": 42,
                            "user": "501",
                            "name": "Example",
                            "command": "Example",
                            "category": "application",
                            "cpu_pct": 12.0,
                            "power_score": 7.5,
                        }
                    ],
                    process_count=1,
                )
            ],
            thermal_snapshots=[
                ThermalSnapshot(
                    uptime_s=101.0,
                    host_epoch_s=1000.0,
                    status=None,
                    temperatures=[
                        {
                            "name": "Battery",
                            "type": "BATTERY",
                            "value_c": 31.0,
                            "status": 0,
                            "status_available": False,
                        }
                    ],
                )
            ],
        )
        fragment = build_report_fragment(
            {
                "metadata": metadata,
                "analysis": analysis,
                "samples": [item.__dict__ for item in samples],
            }
        )

        sources = {item["source"] for item in analysis["data_sources"]}
        self.assertIn("iOS DiagnosticsService PowerTelemetryData.SystemLoad", sources)
        system_load_source = next(
            item
            for item in analysis["data_sources"]
            if item["source"] == "iOS DiagnosticsService PowerTelemetryData.SystemLoad"
        )
        self.assertEqual(system_load_source["kind"], "measured low-rate telemetry")
        self.assertEqual(analysis["summary"]["capacity_mah"], 4500.0)
        self.assertEqual(analysis["summary"]["average_collector_cpu_pct"], 7.0)
        self.assertFalse(analysis["thermal"]["severity_available"])
        self.assertIsNone(analysis["thermal"]["maximum_status"])
        self.assertIsNone(analysis["thermal"]["throttling_observed"])
        self.assertIn("battery temperature only", analysis["thermal"]["limitations"])
        self.assertFalse(any(item.get("title") == "未检测到热限制" for item in analysis["findings"]))
        self.assertEqual(
            analysis["system"]["top_processes"][0]["average_relative_power_score"],
            7.5,
        )
        self.assertIn("Mobile Profiler", fragment)
        self.assertIn("iOS 26.5.2", fragment)
        self.assertIn("iOS DVT sysmontap · powerScore（相对分数）", fragment)
        self.assertIn("iOS 整机原始 SystemLoad 通道", fragment)
        self.assertIn("低频实测遥测", fragment.split("<script>", 1)[0])
        self.assertIn("电池流量功率（电流×电压）", fragment)
        self.assertIn('key: "collector_cpu_pct"', fragment)
        self.assertIn('key: "power_sample_age_s"', fragment)
        self.assertIn('key: "gpu_load_pct"', fragment)
        self.assertIn('feature: "gpu_metrics"', fragment)
        self.assertIn("summaryValue: summary.observed_power_average_mw", fragment)
        self.assertIn("summaryValue: summary.battery_flow_average_power_mw", fragment)
        self.assertIn("全量样本算术均值", fragment)
        self.assertNotIn("@@PLATFORM_NAME@@", fragment)

    def test_report_payload_separates_mixed_ios_system_load_from_battery_flow(self) -> None:
        samples = [
            {
                "elapsed_s": 0.0,
                "uptime_s": 100.0,
                "power_mw": 100.0,
                "current_ma": 25.0,
                "voltage_mv": 4000.0,
                "power_source": "ios_battery_current_voltage",
                "power_valid_for_consumption": True,
            },
            {
                "elapsed_s": 1.0,
                "uptime_s": 101.0,
                "power_mw": 200.0,
                "current_ma": 50.0,
                "voltage_mv": 4000.0,
                "power_source": "ios_battery_current_voltage",
                "power_valid_for_consumption": True,
            },
            {
                "elapsed_s": 2.0,
                "uptime_s": 102.0,
                "power_mw": 1000.0,
                "current_ma": 75.0,
                "voltage_mv": 4000.0,
                "power_source": "ios_power_telemetry_system_load",
                "power_valid_for_consumption": True,
            },
            {
                "elapsed_s": 3.0,
                "uptime_s": 103.0,
                "power_mw": 1200.0,
                "current_ma": 100.0,
                "voltage_mv": 4000.0,
                "power_source": "ios_power_telemetry_system_load",
                "power_valid_for_consumption": True,
            },
        ]
        bundle = {
            "metadata": {"platform": "ios", "test_mode": "power"},
            "samples": samples,
            "analysis": {
                "summary": {
                    "power_sources": [
                        "ios_battery_current_voltage",
                        "ios_power_telemetry_system_load",
                    ],
                    "power_valid_for_consumption": True,
                    "average_power_mw": 625.0,
                    "observed_power_average_mw": 625.0,
                }
            },
        }

        prepared = _report_bundle(bundle)
        statistics_payload = prepared["analysis"]["report_payload"]["metric_statistics"]
        self.assertEqual(statistics_payload["power_mw"]["sample_count"], 2)
        self.assertEqual(statistics_payload["power_mw"]["average"], 1100.0)
        self.assertEqual(statistics_payload["battery_flow_mw"]["average"], 250.0)
        fragment = build_report_fragment(bundle)
        self.assertIn("sources.includes(\"ios_power_telemetry_system_load\")", fragment)
        self.assertIn(
            'sample?.power_source !== "ios_power_telemetry_system_load"',
            fragment,
        )
        self.assertIn("iOS 整机原始 SystemLoad 通道", fragment)
        self.assertIn("iOS 电池流量功率（电流×电压）", fragment)
        self.assertIn("外供时可能接近 SystemPowerIn", fragment)
        self.assertNotIn("sources.every(source => source ===", fragment)

    def test_report_payload_downsamples_long_session_with_cpu_alignment(self) -> None:
        samples = [
            {
                "elapsed_s": float(index + (100 if index >= 2000 else 0)),
                "power_mw": 1000.0 + index % 17,
                "cpu_pct": 99.0 if index == 1234 else 10.0,
            }
            for index in range(3600)
        ]
        bundle = {
            "samples": samples,
            "analysis": {
                "cpu": {
                    "timeline": [
                        {"elapsed_s": float(index), "modeled_power_mw": float(index)}
                        for index in range(3600)
                    ]
                },
                "performance": {
                    "frame_flow": {
                        "stages": [
                            {
                                "key": "display_scanout",
                                "timeline": [
                                    {"uptime_s": float(index), "value": 120.0}
                                    for index in range(3600)
                                ],
                            }
                        ]
                    }
                },
            },
        }
        prepared = _report_bundle(bundle)
        self.assertEqual(len(prepared["samples"]), 1200)
        self.assertEqual(len(prepared["analysis"]["cpu"]["timeline"]), 1200)
        flow_timeline = prepared["analysis"]["performance"]["frame_flow"]["stages"][0]["timeline"]
        self.assertEqual(len(flow_timeline), 1200)
        self.assertEqual(flow_timeline[0]["uptime_s"], 0.0)
        self.assertEqual(flow_timeline[-1]["uptime_s"], 3599.0)
        self.assertEqual(prepared["samples"][0]["elapsed_s"], 0.0)
        self.assertEqual(prepared["samples"][-1]["elapsed_s"], 3699.0)
        self.assertTrue(
            any(
                item.get("report_break_before") is True
                and item.get("elapsed_s") == 2100.0
                for item in prepared["samples"]
            )
        )
        self.assertTrue(any(item.get("cpu_pct") == 99.0 for item in prepared["samples"]))
        payload = prepared["analysis"]["report_payload"]
        self.assertTrue(payload["downsampled"])
        self.assertIn("cpu_pct", payload["sampled_metrics"])
        self.assertEqual(payload["metric_statistics"]["cpu_pct"]["maximum"], 99.0)
        self.assertIn("multi-metric stratified LTTB", payload["downsample_method"])

    def test_report_visualizes_frame_flow_and_omits_unavailable_performance_analysis(self) -> None:
        bundle = {
            "metadata": {
                "title": "performance coverage",
                "platform": "android",
                "test_mode": "performance",
                "device": {},
                "cpu_policies": [],
            },
            "samples": [
                {"elapsed_s": 0.0, "uptime_s": 10.0, "power_mw": 1000.0},
                {"elapsed_s": 1.0, "uptime_s": 11.0, "power_mw": 1100.0},
            ],
            "analysis": {
                "test_mode": "performance",
                "summary": {"duration_s": 1.0, "sample_count": 2},
                "cpu": {"clusters": [], "timeline": []},
                "gpu": {
                    "frequency_available": False,
                    "load_available": False,
                    "work_by_uid": [],
                    "memory": {"available": False},
                    "unavailable_reason": "GPU counters blocked",
                },
                "performance": {
                    "available": True,
                    "current_refresh_rate_hz": 60.0,
                    "sampled_frame_rate_fps": 59.8,
                    "frame_sample_count": 5,
                    "frame_issue_pct": 20.0,
                    "frame_rate_timeline": [
                        {
                            "uptime_s": 10.0,
                            "frame_rate_fps": 59.8,
                            "frame_intervals_ms": [16.4, 16.6, 16.8, 17.0, 40.0],
                            "refresh_rate_hz": 60.0,
                        },
                        {
                            "uptime_s": 11.0,
                            "frame_rate_fps": 119.0,
                            "frame_intervals_ms": [8.1, 8.3, 8.5, 12.8],
                            "refresh_rate_hz": 120.0,
                        },
                    ],
                    "frame_flow": {
                        "stages": [
                            {
                                "key": "app_submission",
                                "phase": "APP",
                                "label": "应用提交",
                                "status": "invalid",
                                "value": 0.0,
                                "unit": "帧/s",
                                "source": "gfxinfo",
                                "detail": "没有有效增量",
                                "timeline": [],
                                "timeline_unit": "帧/s",
                            },
                            {
                                "key": "surface_present",
                                "phase": "COMPOSITOR",
                                "label": "合成器呈现",
                                "status": "primary",
                                "value": 59.8,
                                "unit": "FPS",
                                "source": "SurfaceFlinger",
                                "sample_count": 5,
                                "timeline": [
                                    {"uptime_s": 10.0, "value": 59.8},
                                ],
                                "timeline_unit": "FPS",
                                "timeline_value_label": "呈现帧率",
                            },
                            {
                                "key": "display_scanout",
                                "phase": "DISPLAY",
                                "label": "屏幕刷新",
                                "status": "reference",
                                "value": 60.0,
                                "unit": "Hz",
                                "source": "active display mode",
                                "timeline": [
                                    {"uptime_s": 10.0, "value": 60.0},
                                ],
                                "timeline_unit": "Hz",
                                "timeline_value_label": "显示刷新率",
                            },
                        ]
                    },
                },
                "render_performance": {
                    "pipeline": {
                        "available": False,
                        "stages": [],
                        "slow_frames": [],
                    },
                    "render_threads": [],
                    "bottlenecks": [],
                },
                "system": {"top_processes": [], "priority_activities": {"rows": []}},
                "thermal": {"sensors": [], "cooling_devices": []},
                "scheduler": {"cpusets": [], "cpu_policies": [], "hint_sessions": [], "process_states": []},
                "applications": {},
                "external_events": {},
                "warnings": [],
                "findings": [],
                "data_sources": [],
            },
        }
        fragment = build_report_fragment(bundle)
        self.assertIn('data-panel="raw"', fragment)
        self.assertIn('data-panel="analysis"', fragment)
        self.assertIn('id="frame-flow-history-chart"', fragment)
        self.assertIn("完整链路节点帧率趋势", fragment)
        self.assertIn("renderFrameFlowHistory();", fragment)
        self.assertIn("未获得可解析的 RenderThread / BufferQueue 阶段数据", fragment)
        self.assertIn('key: `frame_stage:${stageKey}`', fragment)
        self.assertIn('if (stageKey === primaryFrameStageKey || stageKey === "display_scanout") return;', fragment)
        self.assertIn("points: metricPoints(stage.timeline", fragment)
        self.assertIn('feature: "frame_rate"', fragment)
        self.assertIn('id="frame-interval-chart"', fragment)
        self.assertIn('data-budget-lines=', fragment)
        self.assertIn("按窗口动态（60 Hz 16.67 ms / 120 Hz 8.33 ms）", fragment)
        self.assertIn("return [0, Math.max(1, maximum * 1.08)];", fragment)
        self.assertIn("单周期内 / 长帧（&gt;1.5×预算）", fragment)
        self.assertIn("与 SurfaceFlinger / 平台计数器报告的截止时间未命中或异常帧不是同一口径", fragment)
        self.assertIn("summaryValue: performance.sampled_frame_rate_fps", fragment)
        self.assertIn("summaryValue: performance.frame_metric_p95_ms", fragment)
        self.assertIn("summaryValue: performance.frame_issue_pct", fragment)
        self.assertIn("不对窗口统计求算术均值", fragment)
        self.assertIn("[6, 7, 8].includes(type)", fragment)
        self.assertIn('key: `thermal_sensor_group:${group.key}`', fragment)
        self.assertIn("本次没有形成可独立陈述的异常结论", fragment)
        self.assertNotIn('<h2>阶段耗时分布</h2>', fragment)
        self.assertNotIn('<h2>慢帧明细</h2>', fragment)
        self.assertNotIn('<h2>渲染与合成热点线程</h2>', fragment)
        self.assertNotIn('data-view="gpu"', fragment)

        bundle["analysis"]["gpu"].update(
            {
                "load_available": True,
                "average_load_pct": 70.0,
                "maximum_load_pct": 95.0,
            }
        )
        bundle["analysis"]["render_performance"] = {
            "pipeline": {
                "available": True,
                "stages": [
                    {
                        "label": "Draw",
                        "sample_count": 5,
                        "average_ms": 4.0,
                        "p95_ms": 6.0,
                        "p99_ms": 7.0,
                        "maximum_ms": 8.0,
                    }
                ],
                "slow_frames": [
                    {
                        "frame_id": 5,
                        "total_ms": 40.0,
                        "draw_ms": 30.0,
                        "deadline_missed": True,
                    }
                ],
            },
            "render_threads": [
                {
                    "name": "RenderThread",
                    "process": "game",
                    "pid": 1,
                    "tid": 2,
                    "average_when_visible_cpu_pct": 30.0,
                    "maximum_cpu_pct": 50.0,
                    "seen_snapshots": 3,
                }
            ],
            "bottlenecks": [],
        }
        bundle["analysis"]["performance"]["frame_issue_pct"] = 4.0
        valid_fragment = build_report_fragment(bundle)
        self.assertIn('<h2>阶段耗时分布</h2>', valid_fragment)
        self.assertIn('<h2>慢帧明细</h2>', valid_fragment)
        self.assertIn('<h2>渲染与合成热点线程</h2>', valid_fragment)
        self.assertNotIn('data-view="gpu"', valid_fragment)

    def test_performance_test_item_table_omits_empty_stage_and_thermal_columns(self) -> None:
        row = {
            "phase": "com.example.game",
            "name": "MainActivity",
            "duration_s": 10.0,
            "frame_count": 600,
            "average_fps": 60.0,
            "one_percent_low_fps": 50.0,
            "frame_p95_ms": 17.0,
            "frame_p99_ms": 20.0,
            "frame_issue_count": 2,
            "frame_issue_pct": 0.3,
            "dominant_stage": None,
            "average_cpu_pct": 40.0,
            "maximum_cpu_pct": 70.0,
            "average_gpu_load_pct": 60.0,
            "maximum_gpu_load_pct": 90.0,
            "maximum_temperature_c": None,
            "maximum_thermal_status": 0,
            "throttling_observed": False,
            "average_whole_device_power_mw": 2500.0,
            "occurrence_count": 1,
            "windows": [{"start_elapsed_s": 0.0, "end_elapsed_s": 10.0}],
            "confidence": "medium",
        }
        bundle = {
            "metadata": {
                "title": "optional test columns",
                "platform": "android",
                "test_mode": "performance",
                "device": {},
                "cpu_policies": [],
            },
            "samples": [
                {"elapsed_s": 0.0, "uptime_s": 10.0, "cpu_pct": 40.0},
                {"elapsed_s": 10.0, "uptime_s": 20.0, "cpu_pct": 45.0},
            ],
            "analysis": {
                "test_mode": "performance",
                "summary": {"duration_s": 10.0, "sample_count": 2},
                "cpu": {"clusters": [], "timeline": []},
                "gpu": {},
                "performance": {"frame_sample_count": 600},
                "render_performance": {},
                "thermal": {"sensors": [], "cooling_devices": []},
                "scheduler": {},
                "system": {},
                "test_items": {"available": True, "rows": [row], "spans": []},
                "applications": {},
                "external_events": {},
                "warnings": [],
                "findings": [],
                "data_sources": [],
            },
        }

        visible = build_report_fragment(bundle).split("<script>", 1)[0]
        self.assertNotIn("<th>主要延迟阶段</th>", visible)
        self.assertNotIn("<th>热限制</th>", visible)
        self.assertNotIn("P95 — ms", visible)
        self.assertNotIn("— °C", visible)

        row["dominant_stage"] = {"label": "GPU 完成", "p95_ms": 8.5}
        row["maximum_temperature_c"] = 43.0
        visible_with_optional = build_report_fragment(bundle).split("<script>", 1)[0]
        self.assertIn("<th>主要延迟阶段</th>", visible_with_optional)
        self.assertIn("<th>热限制</th>", visible_with_optional)
        self.assertIn("GPU 完成", visible_with_optional)
        self.assertIn("最高 43.0 °C", visible_with_optional)

    def test_report_visibly_explains_runtime_coverage_without_listing_disabled_actions(self) -> None:
        capture_configuration = {
            "backend": "harmony_smartperf",
            "features": {
                "cpu_usage": True,
                "cpu_frequency": True,
                "gpu_metrics": False,
                "memory_frequency": True,
                "foreground_window": True,
                "frame_rate": True,
                "frame_details": True,
                "process_snapshots": False,
                "hot_threads": False,
                "thermal": True,
                "scheduler": False,
                "runtime_settings": False,
            }
        }
        bundle = {
            "metadata": {
                "title": "inactive harmony",
                "platform": "harmony",
                "test_mode": "performance",
                "device": {},
                "cpu_policies": [],
                "capture_configuration": capture_configuration,
            },
            "samples": [
                {"elapsed_s": 0.0, "uptime_s": 10.0, "cpu_pct": 10.0},
                {"elapsed_s": 1.0, "uptime_s": 11.0, "cpu_pct": 11.0},
            ],
            "analysis": {
                "test_mode": "performance",
                "capture_configuration": capture_configuration,
                "summary": {"duration_s": 1.0, "sample_count": 2},
                "cpu": {"clusters": [], "timeline": []},
                "gpu": {
                    "frequency_available": False,
                    "load_available": False,
                    "work_by_uid": [],
                    "memory": {"available": False},
                },
                "memory": {
                    "available": False,
                    "timeline": [],
                    "limitations": "HarmonyOS SmartPerf SP_daemon -d did not return a usable DDR frequency field during this session.",
                },
                "performance": {
                    "frame_rate_timeline": [],
                    "frame_flow": {
                        "stages": [
                            {"phase": "APP", "status": "invalid"},
                            {"phase": "RENDER", "status": "unavailable"},
                            {"phase": "COMPOSITOR", "status": "unavailable"},
                            {"phase": "DISPLAY", "status": "invalid"},
                        ]
                    },
                    "frame_unavailable_reason": "目标应用未处于前台；该 SmartPerf FPS 不作为当前目标应用帧率。",
                    "refresh_rate_unavailable_reason": "屏幕未处于活动状态；保留的刷新配置不代表当前显示输出。",
                    "render_resolution_available": False,
                },
                "render_performance": {"pipeline": {"stages": []}, "render_threads": []},
                "system": {"top_processes": []},
                "thermal": {"sensors": [], "cooling_devices": []},
                "scheduler": {"cpusets": [], "cpu_policies": [], "hint_sessions": [], "process_states": []},
                "applications": {},
                "external_events": {},
                "warnings": [],
                "findings": [],
                "data_sources": [],
            },
        }

        visible = build_report_fragment(bundle).split("<script>", 1)[0]
        analysis_start = visible.index('data-panel="analysis"')
        coverage_start = visible.index("分析覆盖与省略项")
        self.assertGreater(coverage_start, analysis_start)
        self.assertIn("目标应用未处于前台；该 SmartPerf FPS 不作为当前目标应用帧率。", visible)
        self.assertIn("屏幕未处于活动状态；保留的刷新配置不代表当前显示输出。", visible)
        self.assertIn(
            '<strong>帧数据源有效性</strong></td><td><span class="source-tag low">未覆盖</span>',
            visible,
        )
        self.assertIn("空图表已省略", visible)
        self.assertIn("本次 SmartPerf 未获得可用 fpsJitters", visible)
        self.assertNotIn("目标窗口没有产生可解析的 framestats", visible)
        self.assertIn("游戏内部渲染分辨率", visible)
        self.assertIn("不展示推测值", visible)
        self.assertIn("内存频率", visible)
        self.assertIn("SmartPerf SP_daemon -d 未返回可用的 DDR 频率字段", visible)
        self.assertNotIn("GPU 实时遥测", visible)
        self.assertNotIn("进程 CPU 快照", visible)

    def test_report_hides_resource_only_performance_test_item_tables(self) -> None:
        bundle = {
            "metadata": {
                "title": "resource-only harmony",
                "platform": "harmony",
                "test_mode": "performance",
                "device": {},
                "cpu_policies": [],
            },
            "samples": [
                {"elapsed_s": 0.0, "uptime_s": 10.0, "cpu_pct": 20.0},
                {"elapsed_s": 1.0, "uptime_s": 11.0, "cpu_pct": 30.0},
            ],
            "analysis": {
                "test_mode": "performance",
                "summary": {"duration_s": 1.0, "sample_count": 2},
                "cpu": {"clusters": [], "timeline": []},
                "gpu": {},
                "performance": {"frame_sample_count": 0, "frame_rate_timeline": []},
                "render_performance": {},
                "thermal": {},
                "scheduler": {},
                "applications": {},
                "external_events": {},
                "test_items": {
                    "rows": [
                        {
                            "name": "Ability",
                            "duration_s": 1.0,
                            "average_cpu_pct": 25.0,
                            "maximum_cpu_pct": 30.0,
                            "confidence": "low",
                        }
                    ],
                    "spans": [
                        {
                            "name": "Ability",
                            "start_elapsed_s": 0.0,
                            "end_elapsed_s": 1.0,
                            "duration_s": 1.0,
                            "average_cpu_pct": 25.0,
                            "confidence": "low",
                        }
                    ],
                },
                "warnings": [],
                "findings": [],
                "data_sources": [],
            },
        }

        fragment = build_report_fragment(bundle)
        self.assertNotIn("按测试项形成的结论证据", fragment)
        bundle["analysis"]["test_items"]["rows"][0]["average_fps"] = 60.0
        bundle["analysis"]["test_items"]["spans"][0]["average_fps"] = 60.0
        frame_fragment = build_report_fragment(bundle)
        self.assertIn("按测试项形成的结论证据", frame_fragment)

    def test_power_report_visualizes_resource_correlation_and_hides_empty_gpu_page(self) -> None:
        fragment = build_report_fragment(
            {
                "metadata": {
                    "title": "power correlation",
                    "platform": "android",
                    "test_mode": "power",
                    "device": {},
                    "cpu_policies": [],
                },
                "samples": [],
                "analysis": {
                    "test_mode": "power",
                    "summary": {"duration_s": 0.0, "sample_count": 0},
                    "cpu": {"clusters": [], "timeline": []},
                    "gpu": {
                        "frequency_available": False,
                        "load_available": False,
                        "work_by_uid": [],
                        "memory": {"available": False},
                    },
                    "power_pressure": {
                        "drivers": [
                            {
                                "label": "CPU 总负载",
                                "correlation": 0.75,
                                "sample_count": 30,
                                "detail": "同期变化",
                            },
                            {
                                "label": "电池温度",
                                "correlation": -0.2,
                                "sample_count": 30,
                                "detail": "长期累积",
                            },
                        ],
                        "tasks": [],
                    },
                    "memory": {"available": False, "reason": "采集关闭"},
                    "runtime_settings": {"rows": []},
                    "system": {"priority_activities": {"rows": []}},
                    "performance": {},
                    "render_performance": {},
                    "thermal": {"sensors": [], "cooling_devices": []},
                    "scheduler": {"cpusets": [], "cpu_policies": [], "hint_sessions": [], "process_states": []},
                    "applications": {},
                    "external_events": {},
                    "warnings": [],
                    "findings": [],
                    "data_sources": [],
                },
            }
        )
        self.assertIn('class="correlation-chart"', fragment)
        self.assertIn("+0.75", fragment)
        self.assertIn("-0.20", fragment)
        self.assertIn("查看相关性计算证据", fragment)
        self.assertNotIn('data-view="gpu"', fragment)
        self.assertIn("资源与整机功率相关性", fragment)
        self.assertEqual(fragment.count('class="nav-tab"'), 2)

    def test_report_prioritizes_performance_context_and_keeps_only_compact_interference(self) -> None:
        bundle = {
            "metadata": {"title": "test", "device": {}, "cpu_policies": []},
            "samples": [
                {"elapsed_s": 0.0, "uptime_s": 1.0, "power_mw": 500.0},
                {"elapsed_s": 1.0, "uptime_s": 2.0, "power_mw": 600.0},
            ],
            "analysis": {
                "summary": {"duration_s": 1.0, "sample_count": 2},
                "cpu": {"clusters": [], "timeline": []},
                "gpu": {
                    "model": "Adreno830v2",
                    "frequency_available": False,
                    "load_available": False,
                    "work_by_uid": [],
                    "memory": {
                        "available": True,
                        "end_total_bytes": 764825600,
                        "change_bytes": 1024,
                        "processes": [
                            {"pid": 2035, "name": "surfaceflinger", "bytes": 408252416}
                        ],
                    },
                },
                "system": {
                    "priority_activities": {
                        "rows": [
                            {
                                "name": "dex2oat",
                                "label": "DEX AOT compilation",
                                "estimated_duration_s": 10.0,
                                "power_delta_mw": 100.0,
                            }
                        ],
                        "monitored": [],
                    },
                    "top_processes": [],
                    "hot_threads": [],
                },
                "performance": {
                    "available": True,
                    "current_refresh_rate_hz": 120.0,
                    "peak_refresh_rate_hz": 120.0,
                    "sampled_compositor_fps": 118.4,
                    "frame_interval_p95_ms": 16.7,
                    "frame_sample_count": 240,
                    "missed_vsync_interval_pct": 0.8,
                    "touch_interaction_count": 8,
                    "touch_interactions_per_minute": 4.0,
                    "refresh_residency": [
                        {"refresh_rate_hz": 120.0, "estimated_duration_s": 55.0, "share_pct": 91.7}
                    ],
                    "supported_refresh_rates_hz": [60.0, 120.0],
                    "foreground_window_name": "bili0",
                    "display_width_px": 1320,
                    "display_height_px": 2856,
                    "gpu_renderer": "Maleoon 920C",
                },
                "thermal": {"sensors": [], "cooling_devices": []},
                "scheduler": {"cpusets": [], "cpu_policies": [], "hint_sessions": [], "process_states": []},
                "applications": {},
                "external_events": {},
                "warnings": [],
                "findings": [],
                "data_sources": [],
            },
        }
        fragment = build_report_fragment(bundle)
        self.assertIn("原始数据随时间变化", fragment)
        self.assertIn("分析结论", fragment)
        self.assertEqual(fragment.count('class="nav-tab"'), 2)
        self.assertEqual(fragment.count('class="app-view"'), 2)
        self.assertIn('id="raw-metric-grid"', fragment)
        self.assertIn(
            "performance.refresh_rate_timeline_source || performance.refresh_residency_source",
            fragment,
        )
        self.assertIn("本次没有形成可独立陈述的异常结论", fragment)
        self.assertNotIn("硬件触控采样率未公开", fragment)
        self.assertNotIn("热控与调度状态", fragment)
        self.assertIn("DEX AOT compilation", fragment)
        self.assertIn("查看采集配置与数据来源", fragment)
        self.assertNotIn('data-view="data"', fragment)
        self.assertNotIn('data-view="test-items"', fragment)
        self.assertIn("Adreno830v2", fragment)
        self.assertIn("surfaceflinger", fragment)
        self.assertNotIn(">Session Overview<", fragment)
        self.assertNotIn("@@PRIORITY_ROWS@@", fragment)
        self.assertNotIn("@@TEST_ITEM_ROWS@@", fragment)


if __name__ == "__main__":
    unittest.main()
