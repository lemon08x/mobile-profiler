from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mobile_power_profiler.analysis import (
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
    parse_sampler_line,
    parse_normalized_samples,
)
from mobile_power_profiler.cli import filter_events_by_metadata
from mobile_power_profiler.comparison import build_comparison_html, build_run_comparison
from mobile_power_profiler.evidence import create_evidence_archive
from mobile_power_profiler.ios import (
    _load_endpoints,
    collect_ios_session,
    list_ios_devices,
    select_ios_device,
)
from mobile_power_profiler.log_import import (
    host_epoch_to_device_uptime,
    import_timestamped_log,
)
from mobile_power_profiler.models import (
    ClockSyncPoint,
    ContextSample,
    CpuPolicy,
    CpuTimes,
    ExternalEvent,
    GpuSource,
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
                )
            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            stream = (root / "raw" / "sampler-stream.txt").read_text(encoding="utf-8")
        self.assertEqual(len(result.raw_samples), 2)
        self.assertGreaterEqual(result.reconnect_count, 1)
        self.assertEqual(checkpoint["sample_count"], 2)
        self.assertIn("S|0|10", stream)
        self.assertIn("S|0|11", stream)


class IosAdapterTests(unittest.TestCase):
    def test_legacy_project_cache_is_loaded_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / ".android-power-profiler" / "ios-devices.json"
            current = root / ".mobile-power-profiler" / "ios-devices.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                json.dumps({"00008150-TEST": {"host": "192.0.2.10", "port": 49152}}),
                encoding="utf-8",
            )
            with (
                patch("mobile_power_profiler.ios.LEGACY_IOS_ENDPOINTS_PATH", legacy),
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

    def test_report_contains_system_and_thermal_views(self) -> None:
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
        self.assertIn("全系统活动", fragment)
        self.assertIn("热控与调度状态", fragment)
        self.assertIn("DEX AOT compilation", fragment)
        self.assertIn("数据质量", fragment)
        self.assertIn("测试项分析", fragment)
        self.assertIn("测试项矩阵", fragment)
        self.assertIn("GC / kworker / 内核与显示活动聚合", fragment)
        self.assertIn("Adreno830v2", fragment)
        self.assertIn("GPU 进程内存快照", fragment)
        self.assertIn("surfaceflinger", fragment)
        self.assertNotIn(">Session Overview<", fragment)
        self.assertNotIn("@@PRIORITY_ROWS@@", fragment)
        self.assertNotIn("@@TEST_ITEM_ROWS@@", fragment)


if __name__ == "__main__":
    unittest.main()
