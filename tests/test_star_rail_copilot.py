from __future__ import annotations

import argparse
import contextlib
import io
import json
import struct
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_profiler import star_rail_copilot_runner as runner
from mobile_profiler.star_rail_runtime import (
    StarRailAsuRuntimeController,
    StarRailCopilotRuntimeController,
)


def _result(
    command: list[str],
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


class FakeAdbRun:
    def __init__(
        self,
        *,
        width: int = 1280,
        height: int = 720,
        package: str = "com.miHoYo.hkrpg",
        installed: bool = True,
        state: str = "device",
    ) -> None:
        self.width = width
        self.height = height
        self.package = package
        self.installed = installed
        self.state = state
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object):
        self.commands.append(list(command))
        arguments = command[3:]
        if arguments == ["get-state"]:
            return _result(command, stdout=self.state.encode("utf-8"))
        if arguments[:3] == ["shell", "pm", "path"]:
            output = b"package:/data/app/base.apk\n" if self.installed else b""
            return _result(command, stdout=output)
        if arguments == ["shell", "dumpsys", "activity", "activities"]:
            output = (
                "mResumedActivity: ActivityRecord{123 u0 "
                f"{self.package}/com.mihoyo.combosdk.ComboSDKActivity t42}}"
            ).encode("utf-8")
            return _result(command, stdout=output)
        if arguments == ["exec-out", "screencap", "-p"]:
            return _result(command, stdout=_png(self.width, self.height))
        raise AssertionError(f"unexpected ADB command: {command}")


class StarRailCopilotRunnerTests(unittest.TestCase):
    def _checkout(self, root: Path) -> Path:
        files = {
            "src.py": "class StarRailCopilot: pass\n",
            "module/alas.py": "class AzurLaneAutoScript: pass\n",
            "module/device/screenshot.py": "# screenshot\n",
            "tasks/rogue/rogue.py": "class Rogue: pass\n",
            "tasks/map/control/joystick.py": "# joystick\n",
            "config/template.json": "{}\n",
            "route/rogue/route.json": "{}\n",
            "route/rogue/Combat/example.py": "# route\n",
            "bin/scrcpy/scrcpy-server-v1.20.jar": "jar\n",
            "bin/scrcpy/scrcpy-server-v1.25.jar": "jar\n",
            "LICENSE": "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def test_checkout_validation_reports_routes_commit_and_scrcpy_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = self._checkout(Path(directory))
            with patch.object(runner, "_git_commit", return_value="abc123"):
                metadata = runner.validate_upstream_path(checkout)

        self.assertEqual(metadata["repository"], runner.UPSTREAM_REPOSITORY)
        self.assertEqual(metadata["license"], "GPL-3.0")
        self.assertEqual(metadata["commit"], "abc123")
        self.assertEqual(metadata["route_count"], 1)
        self.assertEqual(metadata["scrcpy_server_versions"], ["1.20", "1.25"])

    def test_parser_defaults_to_daily_without_ai_configuration(self) -> None:
        args = runner.build_parser().parse_args(
            ["--upstream", ".", "--serial", "phone-1", "--preflight"]
        )

        self.assertEqual(args.workflow, "daily")
        self.assertFalse(
            any("model" in option.dest or "api" in option.dest for option in runner.build_parser()._actions)
        )
        self.assertIn("rewards", runner.WORKFLOWS)

    def test_runtime_probe_reports_missing_src_modules_and_bundled_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "toolkit" / "python.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"python")

            def find_spec(name: str):
                return None if name == "adbutils" else object()

            with patch.object(runner.importlib.util, "find_spec", side_effect=find_spec):
                result = runner.probe_src_python_runtime(root)

        self.assertFalse(result["available"])
        self.assertEqual(result["missing_modules"], ["adbutils"])
        self.assertIn(str(bundled), result["detail"])
        self.assertFalse(result["requires_ai_server"])

    def test_checkout_validation_rejects_missing_native_device_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = self._checkout(Path(directory))
            (checkout / "module" / "device" / "screenshot.py").unlink()
            with self.assertRaisesRegex(RuntimeError, "module.device.screenshot.py"):
                runner.validate_upstream_path(checkout)

    def test_preflight_accepts_only_foreground_1280_by_720_game(self) -> None:
        adb = FakeAdbRun()

        result = runner.preflight_device(
            adb="host-adb",
            serial="phone-1",
            server="CN-Official",
            run_func=adb,
        )

        self.assertEqual(result["screen_state"], "in_game")
        self.assertTrue(result["game_ready"])
        self.assertTrue(result["resolution_supported"])
        self.assertEqual((result["width"], result["height"]), (1280, 720))
        self.assertEqual(len(adb.commands), 4)

    def test_preflight_exposes_unsupported_phone_resolution_without_touching(self) -> None:
        adb = FakeAdbRun(width=2800, height=1260)

        result = runner.preflight_device(
            adb="host-adb",
            serial="phone-1",
            server="CN-Official",
            run_func=adb,
        )

        self.assertEqual(result["screen_state"], "unsupported_resolution")
        self.assertFalse(result["game_ready"])
        self.assertEqual(result["required_resolution"], [1280, 720])
        allowed = {
            ("get-state",),
            ("shell", "pm", "path", "com.miHoYo.hkrpg"),
            ("shell", "dumpsys", "activity", "activities"),
            ("exec-out", "screencap", "-p"),
        }
        self.assertTrue(all(tuple(command[3:]) in allowed for command in adb.commands))

    def test_preflight_reports_missing_server_package_before_screenshot(self) -> None:
        adb = FakeAdbRun(installed=False)

        result = runner.preflight_device(
            adb="host-adb",
            serial="phone-1",
            server="CN-Official",
            run_func=adb,
        )

        self.assertEqual(result["screen_state"], "package_missing")
        self.assertFalse(result["package_installed"])
        self.assertEqual(len(adb.commands), 2)

    def test_dedicated_config_uses_src_native_backends_and_rogue_options(self) -> None:
        template = {
            "Alas": {"Emulator": {}},
            "Rogue": {"RogueWorld": {}},
        }
        args = argparse.Namespace(
            serial="phone-1",
            server="OVERSEA-Asia",
            screenshot_method="ADB",
            control_method="minitouch",
            world="Simulated_Universe_World_6",
            path="Nihility",
            domain_strategy="occurrence",
            use_immersifier=False,
            double_event=False,
            weekly_farming=True,
            use_stamina=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "template.json").write_text(
                json.dumps(template), encoding="utf-8"
            )
            path = runner.build_src_config(root, args)
            config = json.loads(path.read_text(encoding="utf-8"))

        emulator = config["Alas"]["Emulator"]
        rogue = config["Rogue"]["RogueWorld"]
        self.assertEqual(emulator["Serial"], "phone-1")
        self.assertEqual(emulator["PackageName"], "OVERSEA-Asia")
        self.assertEqual(emulator["ScreenshotMethod"], "ADB")
        self.assertEqual(emulator["ControlMethod"], "minitouch")
        self.assertEqual(rogue["Path"], "Nihility")
        self.assertEqual(rogue["DomainStrategy"], "occurrence")
        self.assertTrue(rogue["WeeklyFarming"])

    def test_transport_restart_only_accepts_dead_scrcpy_or_offline_adb(self) -> None:
        class Thread:
            def __init__(self, alive: bool) -> None:
                self.alive = alive

            def is_alive(self) -> bool:
                return self.alive

        args = argparse.Namespace(
            adb="host-adb",
            serial="phone-1",
            screenshot_method="scrcpy",
        )
        online = FakeAdbRun()
        live = types.SimpleNamespace(
            _scrcpy_stream_loop_thread=Thread(True),
            _scrcpy_alive=True,
        )
        dead = types.SimpleNamespace(
            _scrcpy_stream_loop_thread=Thread(False),
            _scrcpy_alive=True,
        )

        self.assertFalse(
            runner._src_transport_needs_restart(live, args, run_func=online)
        )
        self.assertTrue(
            runner._src_transport_needs_restart(dead, args, run_func=online)
        )
        self.assertTrue(
            runner._src_transport_needs_restart(
                live,
                args,
                run_func=FakeAdbRun(state="offline"),
            )
        )

    def test_rogue_recreates_device_after_recoverable_transport_failure(self) -> None:
        class HandledError(Exception):
            pass

        class RequestHumanTakeover(Exception):
            pass

        devices: list[object] = []

        class Application:
            def __init__(self, _name: str) -> None:
                self.device = types.SimpleNamespace()
                devices.append(self.device)

        class Rogue:
            calls = 0

            def __init__(self, *, config: object, device: object) -> None:
                self.config = config
                self.device = device

            def rogue_once(self) -> bool:
                type(self).calls += 1
                if type(self).calls == 1:
                    raise RequestHumanTakeover
                return True

        modules = {
            "module.config.config": types.SimpleNamespace(
                AzurLaneConfig=lambda *_args, **_kwargs: types.SimpleNamespace()
            ),
            "module.exception": types.SimpleNamespace(
                HandledError=HandledError,
                RequestHumanTakeover=RequestHumanTakeover,
            ),
            "src": types.SimpleNamespace(StarRailCopilot=Application),
            "tasks.rogue.rogue": types.SimpleNamespace(Rogue=Rogue),
        }
        args = argparse.Namespace(
            adb="host-adb",
            serial="phone-1",
            screenshot_method="scrcpy",
            scrcpy_max_size=1920,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", modules),
            patch.object(runner.os, "chdir"),
            patch.object(runner, "_prepend_adb_binary"),
            patch.object(runner, "_src_transport_needs_restart", return_value=True),
            patch.object(runner, "_wait_for_adb_device", return_value=True),
            patch.object(runner, "_stop_failed_scrcpy") as stop_scrcpy,
        ):
            result = runner.run_src_rogue(Path(directory), args)

        self.assertTrue(result)
        self.assertEqual(len(devices), 2)
        stop_scrcpy.assert_called_once_with(devices[0], "scrcpy")

    def test_rewards_workflow_excludes_resource_consuming_tasks(self) -> None:
        commands = {command for command, _method in runner.REWARD_TASKS}

        self.assertEqual(commands, {"BattlePass", "DailyQuest", "Freebies"})
        self.assertTrue(
            commands.isdisjoint({"Dungeon", "Ornament", "Weekly", "Rogue"})
        )

    def test_run_mode_refuses_unsupported_resolution_before_loading_src(self) -> None:
        device = {
            "serial": "phone-1",
            "server": "CN-Official",
            "package": "com.miHoYo.hkrpg",
            "device_state": "device",
            "package_installed": True,
            "screen_state": "unsupported_resolution",
            "game_ready": False,
            "width": 2800,
            "height": 1260,
            "required_resolution": [1280, 720],
        }
        output = io.StringIO()
        with (
            patch.object(runner, "validate_upstream_path", return_value={"route_count": 67}),
            patch.object(runner, "preflight_device", return_value=device),
            patch.object(runner, "build_src_config") as build_config,
            patch.object(runner, "run_src_rogue") as run_rogue,
            contextlib.redirect_stdout(output),
        ):
            return_code = runner.main(
                ["--upstream", ".", "--serial", "phone-1", "--run"]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(return_code, 2)
        self.assertEqual(summary["screen"]["screen_state"], "unsupported_resolution")
        build_config.assert_not_called()
        run_rogue.assert_not_called()


class StarRailCopilotRuntimeTests(unittest.TestCase):
    def _metadata(self, root: Path) -> dict[str, object]:
        return {
            "path": str(root),
            "repository": runner.UPSTREAM_REPOSITORY,
            "license": "GPL-3.0",
            "commit": "abc123",
            "route_count": 67,
            "scrcpy_server_versions": ["1.20", "1.25"],
            "logical_resolution": [1280, 720],
        }

    def test_runtime_snapshot_identifies_src_and_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "StarRailCopilot"
            checkout.mkdir()
            with patch(
                "mobile_profiler.star_rail_runtime.validate_upstream_path",
                return_value=self._metadata(checkout),
            ):
                controller = StarRailCopilotRuntimeController(
                    "host-adb", root, upstream_path=checkout
                )
                snapshot = controller.snapshot()

        self.assertEqual(snapshot["adapter_id"], "star-rail-copilot")
        self.assertFalse(snapshot["end_to_end_verified"])
        self.assertIn("多分辨率", snapshot["verification"]["reason"])
        self.assertEqual(snapshot["upstream"]["route_count"], 67)
        self.assertTrue(snapshot["capabilities"]["native_android_stack"])
        options = {row["id"]: row for row in snapshot["runtime_options"]}
        self.assertEqual(options["world"]["scope"], "task")
        self.assertEqual(options["upstream_path"]["scope"], "environment")
        self.assertEqual(options["control_method"]["scope"], "environment")
        self.assertEqual(options["workflow"]["value"], "daily")
        self.assertTrue(snapshot["capabilities"]["configure"])
        self.assertFalse(snapshot["requires_ai_server"])
        self.assertFalse(snapshot["capabilities"]["requires_ai_server"])
        self.assertIs(StarRailAsuRuntimeController, StarRailCopilotRuntimeController)

    def test_runtime_configuration_is_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "StarRailCopilot"
            checkout.mkdir()
            with patch(
                "mobile_profiler.star_rail_runtime.validate_upstream_path",
                return_value=self._metadata(checkout),
            ):
                controller = StarRailCopilotRuntimeController(
                    "host-adb", root, upstream_path=checkout
                )
                configured = controller.configure(
                    {
                        "world": "Simulated_Universe_World_6",
                        "path": "Nihility",
                        "domain_strategy": "occurrence",
                        "weekly_farming": True,
                        "use_stamina": True,
                    }
                )
                controller.close()
                restored_controller = StarRailCopilotRuntimeController(
                    "host-adb", root, upstream_path=checkout
                )
                restored = restored_controller.snapshot()
                restored_controller.close()
                invalid_controller = StarRailCopilotRuntimeController(
                    "host-adb", root, upstream_path=checkout
                )
                with self.assertRaisesRegex(ValueError, "unsupported StarRailCopilot world"):
                    invalid_controller.configure({"world": "Not_A_World"})
                invalid_controller.close()

        configured_options = {
            row["id"]: row["value"] for row in configured["runtime_options"]
        }
        restored_options = {
            row["id"]: row["value"] for row in restored["runtime_options"]
        }
        self.assertEqual(configured_options["world"], "Simulated_Universe_World_6")
        self.assertEqual(configured_options["path"], "Nihility")
        self.assertTrue(configured_options["weekly_farming"])
        self.assertTrue(restored_options["use_stamina"])

    def test_command_prefers_src_toolkit_and_has_no_m7a_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "StarRailCopilot"
            toolkit = checkout / "toolkit" / "python.exe"
            toolkit.parent.mkdir(parents=True)
            toolkit.write_bytes(b"python")
            with patch(
                "mobile_profiler.star_rail_runtime.validate_upstream_path",
                return_value=self._metadata(checkout),
            ):
                controller = StarRailCopilotRuntimeController(
                    "host-adb", root, upstream_path=checkout
                )
                command = controller._command(
                    "phone-1",
                    "--preflight",
                    {
                        "server": "CN-Bilibili",
                        "screenshot_method": "ADB",
                        "control_method": "minitouch",
                        "use_immersifier": False,
                    },
                )

        self.assertEqual(command[0], str(toolkit))
        self.assertIn("mobile_profiler.star_rail_copilot_runner", command)
        self.assertIn("CN-Bilibili", command)
        self.assertIn("daily", command)
        self.assertIn("--no-use-immersifier", command)
        self.assertFalse(any("model" in item.lower() for item in command))
        self.assertNotIn("--speed", command)
        self.assertNotIn("--bonus", command)

    def test_preflight_persists_src_summary_and_resolution_gate(self) -> None:
        summary = {
            "status": "waiting_for_game",
            "upstream": {"route_count": 67},
            "device": {"serial": "phone-1"},
            "screen": {
                "screen_state": "unsupported_resolution",
                "game_ready": False,
                "width": 2800,
                "height": 1260,
            },
            "adapter": {"native_android_stack": True},
        }

        def run_func(command: list[str], **_kwargs: object):
            return _result(command, stdout=json.dumps(summary).encode("utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "StarRailCopilot"
            checkout.mkdir()
            with patch(
                "mobile_profiler.star_rail_runtime.validate_upstream_path",
                return_value=self._metadata(checkout),
            ):
                controller = StarRailCopilotRuntimeController(
                    "host-adb",
                    root,
                    upstream_path=checkout,
                    run_func=run_func,
                )
                snapshot = controller.preflight({"device": "phone-1"})
                persisted = json.loads(
                    (
                        root
                        / "open-source-automation"
                        / "star-rail-copilot"
                        / "preflight.json"
                    ).read_text(encoding="utf-8")
                )

        self.assertEqual(snapshot["status"], "waiting_for_game")
        self.assertEqual(
            snapshot["preflight"]["screen"]["screen_state"],
            "unsupported_resolution",
        )
        self.assertEqual(persisted["device"]["serial"], "phone-1")


if __name__ == "__main__":
    unittest.main()
