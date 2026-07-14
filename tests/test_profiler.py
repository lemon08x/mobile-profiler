from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mobile_power_profiler.analysis import (
    analyze_android_frame_pipeline,
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
from mobile_power_profiler.collector import (
    adb_connection_type,
    build_sampler_script,
    collect_streaming_session,
    gpu_load_from_text,
    is_gpu_core_devfreq,
    is_memory_devfreq,
    parse_android_display_performance,
    parse_android_frame_interpolation,
    parse_android_gfxinfo,
    parse_android_runtime_settings,
    parse_android_surfaceflinger_performance,
    parse_android_touch_devices,
    parse_android_window_performance,
    parse_sampler_line,
    parse_normalized_samples,
)
from mobile_power_profiler.cli import filter_events_by_metadata, run_harmony_record
from mobile_power_profiler.comparison import build_comparison_html, build_run_comparison
from mobile_power_profiler.evidence import create_evidence_archive
from mobile_power_profiler.features import resolve_capture_configuration
from mobile_power_profiler.ios import (
    _load_endpoints,
    collect_ios_session,
    list_ios_devices,
    select_ios_device,
)
from mobile_power_profiler.harmony import (
    build_harmony_smartperf_context,
    build_harmony_smartperf_sample,
    build_harmony_sample,
    harmony_cpu_policies,
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
from mobile_power_profiler.log_import import (
    host_epoch_to_device_uptime,
    import_timestamped_log,
)
from mobile_power_profiler.models import (
    ClockSyncPoint,
    CommandResult,
    ContextSample,
    CpuPolicy,
    CpuTimes,
    ExternalEvent,
    GpuSource,
    MemorySource,
    RawSample,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
)
from mobile_power_profiler.parsers import (
    parse_activity_processes,
    parse_cpuset_policy_state,
    parse_gpu_dump,
    parse_gpu_work,
    parse_performance_hint,
    parse_power_profile,
    parse_thermalservice,
    parse_top_processes,
    parse_top_threads,
)
from mobile_power_profiler.report import _report_bundle, build_report_fragment
from mobile_power_profiler.storage import (
    RunJournal,
    load_contexts,
    load_scheduler_snapshots,
    load_system_snapshots,
    load_thermal_snapshots,
    read_samples_csv,
    write_samples_csv,
)


class ParserTests(unittest.TestCase):
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

        interpolation = parse_android_frame_interpolation(
            "[persist.vendor.display.memc.enable]: [1]\n"
        )
        self.assertEqual(interpolation["frame_interpolation_status"], "detected")
        self.assertEqual(interpolation["frame_interpolation_confidence"], "high")
        self.assertTrue(interpolation["frame_interpolation_evidence"])

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
        )
        self.assertEqual(samples[0].current_ma, 300.0)
        self.assertEqual(samples[0].signed_current_ma, -300.0)
        self.assertEqual(samples[0].direction, "discharging")
        self.assertGreater(samples[0].power_mw, 0.0)
        self.assertTrue(warnings)


class CpuAnalysisTests(unittest.TestCase):
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
                Sample(0, 0.0, 100.0, 300.0, -300.0, 3800.0, power, "discharging", 30.0),
                Sample(1, 1.0, 101.0, 300.0, -300.0, 3800.0, power, "discharging", 30.0),
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

        self.assertIn("设备标识", report)
        self.assertIn("系统 / 硬件", report)
        self.assertIn("ios:00008150-TEST", report)
        self.assertIn("iOS 26.5.2 / V54AP", report)
        self.assertNotIn("ADB serial", report)


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
                "mobile_power_profiler.collector._wait_for_device",
                side_effect=[True, True, False],
            ), patch(
                "mobile_power_profiler.collector.collect_clock_sync",
                return_value=None,
            ), patch(
                "mobile_power_profiler.collector._device_ready",
                return_value=False,
            ), patch(
                "mobile_power_profiler.collector.subprocess.Popen",
                side_effect=processes,
            ), patch(
                "mobile_power_profiler.collector.time.sleep",
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
        )
        self.assertEqual(frame["frame_sample_count"], 3)
        self.assertEqual(frame["missed_vsync_interval_count"], 1)
        self.assertGreater(float(frame["frame_interval_p95_ms"]), 30.0)

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
        self.assertEqual(result["render_width_px"], 1260)
        self.assertEqual(result["render_height_px"], 2800)
        self.assertTrue(result["render_resolution_estimated"])

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

    def test_android_asleep_context_does_not_fake_refresh_residency_or_zero_fps(self) -> None:
        contexts = [
            ContextSample(
                100.0,
                foreground_package="com.example",
                screen_state="Asleep",
                refresh_rate_hz=60.0,
                performance={
                    "platform": "android",
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
        self.assertFalse(android["features"]["harmony_hitches"])
        ios = resolve_capture_configuration(
            "performance", "ios", "performance-standard"
        )
        self.assertTrue(ios["features"]["cpu_usage"])
        self.assertTrue(ios["features"]["gpu_metrics"])
        self.assertTrue(ios["features"]["target_process"])
        self.assertTrue(ios["features"]["process_snapshots"])
        self.assertTrue(ios["features"]["thermal"])
        for name in (
            "cpu_frequency",
            "memory_frequency",
            "frame_rate",
            "frame_details",
            "harmony_hitches",
            "touch_events",
            "hot_threads",
            "scheduler",
            "runtime_settings",
            "power_attribution",
        ):
            self.assertFalse(ios["features"][name], name)

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
            rows[0], sample, "com.example.game"
        )
        self.assertEqual(context.performance["compositor_fps"], 60.0)
        self.assertAlmostEqual(context.performance["frame_interval_p95_ms"], 33.333334)
        self.assertLess(context.performance["one_percent_low_fps"], 31.0)

    def test_harmony_power_mode_is_read_and_changed_with_verification(self) -> None:
        help_output = (
            "usage: power-shell setmode\n  602  :  performance mode\n"
            "Set Mode Failed, current mode is: 600\n"
        )
        with patch(
            "mobile_power_profiler.harmony.hdc_shell",
            return_value=CommandResult([], 0, help_output, "", 0.01),
        ):
            state = read_harmony_power_mode("hdc", "device")
        self.assertTrue(state["supported"])
        self.assertEqual(state["current_mode"], 600)

        with patch(
            "mobile_power_profiler.harmony.hdc_shell",
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
                        "performance", "harmony", "performance-standard"
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
                metadata = json.loads(
                    (run_dir / "metadata.json").read_text(encoding="utf-8")
                )
                observed_modes.append(dict(metadata["device_performance_mode"]))
                return {}, run_dir / "report.html"

            with (
                patch(
                    "mobile_power_profiler.cli.select_harmony_device",
                    return_value={
                        "serial": "harmony:USB123",
                        "hdc_target": "USB123",
                        "connection_type": "usb",
                    },
                ),
                patch("mobile_power_profiler.cli.probe_harmony_device", return_value=probe),
                patch(
                    "mobile_power_profiler.cli.read_harmony_power_mode",
                    return_value={
                        "supported": True,
                        "current_mode": 600,
                        "current_label": "normal",
                    },
                ),
                patch(
                    "mobile_power_profiler.cli.set_harmony_power_mode",
                    side_effect=change_mode,
                ) as set_mode,
                patch(
                    "mobile_power_profiler.cli.collect_harmony_session",
                    side_effect=RuntimeError("collector failed"),
                ),
                patch(
                    "mobile_power_profiler.cli.finalize_run",
                    side_effect=finalize_after_restore,
                ),
                patch("mobile_power_profiler.cli.print_run_summary"),
                patch("mobile_power_profiler.cli.sys.stderr", new=io.StringIO()),
            ):
                result = run_harmony_record(args)

        self.assertEqual(result, 6)
        self.assertEqual([call.args[2] for call in set_mode.call_args_list], [602, 600])
        self.assertEqual(len(observed_modes), 1)
        self.assertTrue(observed_modes[0]["applied"])
        self.assertTrue(observed_modes[0]["restored"])
        self.assertEqual(observed_modes[0]["restored_mode"], 600)


class IosAdapterTests(unittest.TestCase):
    def test_legacy_project_cache_is_loaded_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            android_legacy = root / ".android-power-profiler" / "ios-devices.json"
            mobile_power_legacy = root / ".mobile-power-profiler" / "ios-devices.json"
            current = root / ".mobile-profiler" / "ios-devices.json"
            android_legacy.parent.mkdir(parents=True)
            android_legacy.write_text(
                json.dumps({"00008150-TEST": {"host": "192.0.2.10", "port": 49152}}),
                encoding="utf-8",
            )
            with (
                patch("mobile_power_profiler.ios.LEGACY_IOS_ENDPOINTS_PATH", android_legacy),
                patch(
                    "mobile_power_profiler.ios.LEGACY_MOBILE_POWER_IOS_ENDPOINTS_PATH",
                    mobile_power_legacy,
                ),
                patch("mobile_power_profiler.ios.IOS_ENDPOINTS_PATH", current),
            ):
                endpoints = _load_endpoints()

        self.assertEqual(endpoints["00008150-TEST"]["host"], "192.0.2.10")

    def test_usb_transport_is_visible_even_when_cached_wireless_endpoint_is_ready(self) -> None:
        with (
            patch(
                "mobile_power_profiler.ios._run_bridge_json",
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
                "mobile_power_profiler.ios._load_endpoints",
                return_value={
                    "00008150-TEST": {"host": "192.0.2.10", "port": 49152}
                },
            ),
            patch("mobile_power_profiler.ios._save_endpoint"),
            patch("mobile_power_profiler.ios._endpoint_reachable", return_value=True),
        ):
            devices, error = list_ios_devices("ios-python")

        self.assertIsNone(error)
        self.assertEqual(devices[0]["connection_type"], "usb")
        self.assertEqual(devices[0]["transports"], "usb,wireless")
        self.assertEqual(devices[0]["wireless_ready"], "true")
        self.assertEqual(devices[0]["host"], "192.0.2.10")

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
                "mobile_power_profiler.ios._run_bridge_json",
                side_effect=RuntimeError("Bonjour unavailable"),
            ),
            patch("mobile_power_profiler.ios._load_endpoints", return_value=cached),
            patch("mobile_power_profiler.ios._endpoint_reachable", return_value=True),
        ):
            devices, error = list_ios_devices("ios-python")
            selected = select_ios_device("ios:00008150-TEST", "ios-python")

        self.assertIn("Bonjour unavailable", error or "")
        self.assertEqual(devices[0]["state"], "device")
        self.assertEqual(devices[0]["connection_type"], "wireless")
        self.assertEqual(selected["serial"], "ios:00008150-TEST")

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
                patch("mobile_power_profiler.ios.subprocess.Popen", return_value=FakeProcess()),
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
            )
            for index in range(11)
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

        performance_metadata = {**base_metadata, "test_mode": "performance"}
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
        self.assertNotIn('id="mobile-power-profiler"', power_fragment)
        self.assertIn('data-test-mode="power"', power_fragment)
        self.assertIn("功耗压力分析", power_fragment)
        self.assertIn("系统设置快照", power_fragment)
        self.assertIn('data-test-mode="performance"', performance_fragment)
        self.assertIn("渲染链路与帧延迟", performance_fragment)
        self.assertIn("性能测试项矩阵", performance_fragment)
        self.assertIn("性能模式不继续拆分组件、UID、Wakelock", performance_fragment)
        self.assertNotIn("@@", performance_fragment)

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
        self.assertIn("采集项与干扰控制", fragment)
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
        self.assertIn("HDC 实测计数器", fragment)
        self.assertIn("Android BatteryStats、ADPF", fragment)
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
        self.assertEqual(analysis["summary"]["capacity_mah"], 4500.0)
        self.assertEqual(analysis["summary"]["average_collector_cpu_pct"], 7.0)
        self.assertEqual(
            analysis["system"]["top_processes"][0]["average_relative_power_score"],
            7.5,
        )
        self.assertIn("PowerScope Mobile", fragment)
        self.assertIn("iOS 26.5.2", fragment)
        self.assertIn("相对分数 ≠ mW", fragment)
        self.assertIn('data-overview-metric="gpu_load_pct"', fragment)
        self.assertNotIn("@@PLATFORM_NAME@@", fragment)

    def test_report_payload_downsamples_long_session_with_cpu_alignment(self) -> None:
        samples = [
            {"elapsed_s": float(index), "power_mw": 1000.0 + index % 17}
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
                }
            },
        }
        prepared = _report_bundle(bundle)
        self.assertEqual(len(prepared["samples"]), 1200)
        self.assertEqual(len(prepared["analysis"]["cpu"]["timeline"]), 1200)
        self.assertEqual(prepared["samples"][0]["elapsed_s"], 0.0)
        self.assertEqual(prepared["samples"][-1]["elapsed_s"], 3599.0)

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
        self.assertIn("功耗测试概览", fragment)
        self.assertIn("功耗压力分析", fragment)
        self.assertIn("刷新档位驻留", fragment)
        self.assertIn("合成器抽样 FPS", fragment)
        self.assertIn("硬件触控采样率未公开", fragment)
        self.assertNotIn("热控与调度状态", fragment)
        self.assertIn("DEX AOT compilation", fragment)
        self.assertIn("数据质量", fragment)
        self.assertIn("测试项分析", fragment)
        self.assertIn("功耗测试项矩阵", fragment)
        self.assertIn("Adreno830v2", fragment)
        self.assertIn("GPU 进程内存快照", fragment)
        self.assertIn("surfaceflinger", fragment)
        self.assertNotIn(">Session Overview<", fragment)
        self.assertNotIn("@@PRIORITY_ROWS@@", fragment)
        self.assertNotIn("@@TEST_ITEM_ROWS@@", fragment)


if __name__ == "__main__":
    unittest.main()
