from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from android_power_profiler.analysis import analyze_cpu, convert_samples
from android_power_profiler.collector import (
    collect_streaming_session,
    parse_sampler_line,
)
from android_power_profiler.log_import import (
    host_epoch_to_device_uptime,
    import_timestamped_log,
)
from android_power_profiler.models import (
    ClockSyncPoint,
    ContextSample,
    CpuPolicy,
    CpuTimes,
    RawSample,
    Sample,
)
from android_power_profiler.parsers import parse_gpu_work, parse_power_profile
from android_power_profiler.report import _report_bundle
from android_power_profiler.storage import (
    RunJournal,
    load_contexts,
    read_samples_csv,
    write_samples_csv,
)


class ParserTests(unittest.TestCase):
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
        )
        metadata = {"cpu_policies": [{"name": "policy0"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.csv"
            write_samples_csv(path, [sample], metadata)
            loaded = read_samples_csv(path)[0]
        self.assertEqual(loaded.current_ma, 250.0)
        self.assertEqual(loaded.signed_current_ma, -250.0)
        self.assertEqual(loaded.direction, "discharging")

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
                "android_power_profiler.collector._wait_for_device",
                side_effect=[True, True, False],
            ), patch(
                "android_power_profiler.collector.collect_clock_sync",
                return_value=None,
            ), patch(
                "android_power_profiler.collector._device_ready",
                return_value=False,
            ), patch(
                "android_power_profiler.collector.subprocess.Popen",
                side_effect=processes,
            ), patch(
                "android_power_profiler.collector.time.sleep",
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


class ReportTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
