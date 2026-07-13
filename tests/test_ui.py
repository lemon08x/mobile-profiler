from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from mobile_power_profiler.cli import build_parser
from mobile_power_profiler.ui import (
    DashboardHTTPServer,
    DashboardManager,
    LiveTelemetryReader,
    parse_device_ipv4_addresses,
    sanitize_run_name,
)


class LiveTelemetryTests(unittest.TestCase):
    def test_reader_converts_append_only_sampler_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            metadata = {
                "title": "UI test",
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
                json.dumps(
                    {
                        "uptime_s": 101.0,
                        "host_epoch_s": 1000.0,
                        "cpusets": {"background": "0-3"},
                        "hint_sessions": [{"uid": 1000, "pid": 123, "tids": [124]}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            snapshot = LiveTelemetryReader(root).snapshot()

            self.assertEqual(snapshot["sample_count"], 2)
            self.assertAlmostEqual(snapshot["latest"]["current_ma"], 320.0)
            self.assertAlmostEqual(snapshot["latest"]["power_mw"], 1215.36)
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


class UiServerTests(unittest.TestCase):
    def test_ui_merges_android_and_ios_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory), ios_python="ios-python")
            with (
                patch(
                    "mobile_power_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "ANDROID", "state": "device"}], None),
                ),
                patch(
                    "mobile_power_profiler.ui.list_ios_devices",
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
            ):
                devices, error = manager.devices(force=True)

        self.assertIsNone(error)
        self.assertEqual({item["serial"] for item in devices}, {"ANDROID", "ios:IPHONE"})
        self.assertEqual(
            next(item for item in devices if item["serial"] == "ANDROID")["platform"],
            "android",
        )

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
                with urlopen(base + "/api/state", timeout=5) as response:
                    state = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                manager.close()
                thread.join(timeout=5)

            self.assertIn("Mobile Power Profiler", html)
            self.assertIn("ADB IP", html)
            self.assertIn("无线 ADB", html)
            self.assertIn("iOS 无线", html)
            self.assertIn("断开无线", html)
            self.assertIn("系统活动", html)
            self.assertIn("热控与调度", html)
            self.assertIn("工具与交付", html)
            self.assertIn("导入 BTR2 日志", html)
            self.assertTrue(state["active"]["is_demo"])
            self.assertIn("portable_build_available", state["tooling"])
            self.assertEqual(len(state["active"]["series"]), 240)
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
            self.assertIn("comparison", comparison_html)
            manager.regenerate_run.assert_called_once_with({"run_name": "phone-a"})
            manager.enable_tcpip.assert_called_once_with({"device": "USB123", "port": 5555})
            manager.disconnect_device.assert_called_once_with(
                {"address": "192.168.21.90:5555"}
            )

    def test_start_record_launches_existing_cli_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            fake_active = Mock()
            fake_active.running = True
            fake_active.snapshot.return_value = {"running": True, "status": "starting"}
            with (
                patch(
                    "mobile_power_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "SERIAL", "state": "device"}], None),
                ),
                patch("mobile_power_profiler.ui.subprocess.Popen", return_value=object()) as popen,
                patch("mobile_power_profiler.ui.ActiveRun", return_value=fake_active),
            ):
                result = manager.start_record(
                    {
                        "device": "SERIAL",
                        "interval": 1,
                        "run_name": "UI smoke",
                        "session_mode": True,
                        "require_unplugged": True,
                    }
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[:5], [
                sys.executable,
                "-m",
                "mobile_power_profiler",
                "--adb",
                "custom-adb",
            ])
            self.assertIn("record", command)
            self.assertIn("--session-mode", command)
            self.assertIn("--require-unplugged", command)
            self.assertIn("--process-interval", command)
            self.assertIn("--thread-interval", command)
            self.assertNotIn("--no-system-monitor", command)
            duration_index = command.index("--duration")
            self.assertEqual(command[duration_index + 1], "3720")
            self.assertTrue(any(Path(value).name == "UI-smoke" for value in command))
            self.assertEqual(result["status"], "starting")

    def test_ui_can_run_adb_connect_and_refresh_device_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DashboardManager("custom-adb", Path(directory))
            completed = Mock(returncode=0, stdout="connected to 192.168.1.20:5555\n", stderr="")
            with (
                patch("mobile_power_profiler.ui.subprocess.run", return_value=completed) as run,
                patch(
                    "mobile_power_profiler.ui.list_adb_devices",
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
                patch("mobile_power_profiler.ui.subprocess.run", return_value=completed) as run,
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
                    "mobile_power_profiler.ui.list_adb_devices",
                    return_value=([{"serial": "USB123", "state": "device"}], None),
                ),
                patch("mobile_power_profiler.ui.adb_shell", return_value=ip_result) as shell,
                patch("mobile_power_profiler.ui.subprocess.run", return_value=tcpip_result) as run,
                patch.object(manager, "connect_device", return_value=connected) as connect,
                patch("mobile_power_profiler.ui.time.sleep", return_value=None),
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
            with patch("mobile_power_profiler.ui.subprocess.run", return_value=completed) as run:
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
            with patch("mobile_power_profiler.ui.subprocess.run", return_value=completed) as run:
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

            def fake_build(command, operation, **kwargs):
                output_dir.mkdir(parents=True)
                Path(f"{output_dir}.zip").write_bytes(b"zip")
                return "built"

            with (
                patch("mobile_power_profiler.ui.shutil.which", return_value="powershell.exe"),
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
    def test_run_name_is_safe_and_readable(self) -> None:
        self.assertEqual(sanitize_run_name(" BTR2 round 001 "), "BTR2-round-001")
        self.assertEqual(sanitize_run_name("../unsafe/name"), "unsafe-name")
        self.assertEqual(sanitize_run_name("续航 第 1 轮"), "续航-第-1-轮")

    def test_cli_parser_exposes_ui_options(self) -> None:
        args = build_parser().parse_args(
            ["ui", "--host", "127.0.0.1", "--port", "0", "--no-browser", "--demo"]
        )
        self.assertEqual(args.command, "ui")
        self.assertEqual(args.port, 0)
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
