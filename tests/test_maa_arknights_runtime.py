from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mobile_profiler.maa_arknights_runtime import (
    MAA_GUARD_POLICY,
    MAA_PACKAGED_RUNNERS,
    MaaArknightsRuntimeController,
)
from mobile_profiler.maa_roguelike_runner import resolve_roguelike_params


class FakeRun:
    def __init__(self, *, awake: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.awake = awake

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(command))
        if any(str(item).endswith("maa_viewport_smoke.py") for item in command):
            payload = {
                "connected": True,
                "image": "viewport.png",
                "image_size": [1280, 720],
                "viewport": "adaptive",
                "resolution_event": {
                    "what": "ResolutionInfo",
                    "why": "AdaptiveViewport",
                    "details": {
                        "width": 2800,
                        "height": 1260,
                        "logical_width": 1280,
                        "logical_height": 720,
                        "viewport": {
                            "x": 280,
                            "y": 0,
                            "width": 2240,
                            "height": 1260,
                        },
                    },
                },
            }
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(payload).encode("utf-8"),
            )
        if "dumpsys" in command:
            if "power" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    (
                        b"mWakefulness=Awake\nDisplay Power: state=ON"
                        if self.awake
                        else b"mWakefulness=Asleep\nDisplay Power: state=OFF"
                    ),
                )
            return subprocess.CompletedProcess(
                command,
                0,
                b"mCurrentFocus=com.hypergryph.arknights/com.u8.sdk.U8UnityContext",
            )
        return subprocess.CompletedProcess(command, 1, b"unexpected command")


class BlockingPopen:
    def __init__(self, command: list[str], **_kwargs: object) -> None:
        self.command = list(command)
        self.returncode: int | None = None
        self._finished = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(self.command, timeout)
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, _signal: int) -> None:
        self.returncode = 0
        self._finished.set()

    def terminate(self) -> None:
        self.returncode = 0
        self._finished.set()

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()


class PopenFactory:
    def __init__(self) -> None:
        self.processes: list[BlockingPopen] = []

    def __call__(self, command: list[str], **kwargs: object) -> BlockingPopen:
        process = BlockingPopen(command, **kwargs)
        self.processes.append(process)
        return process


class MaaArknightsRuntimeTests(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path, Path, Path]:
        source = root / "source"
        source.mkdir(parents=True)
        core = source / ".codex-research" / "MAA-v6.14.2-git" / "build-viewport" / "bin"
        core.mkdir(parents=True)
        (core / "MaaCore.dll").write_bytes(b"patched-core")
        runtime = root / "MAA-v6.14.2-win-x64"
        (runtime / "resource").mkdir(parents=True)
        adb = root / "adb.exe"
        adb.write_bytes(b"adb")
        return source, core, runtime, adb

    def test_snapshot_exposes_allowlisted_tasks_and_adaptive_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            config_path = (
                root
                / "output"
                / "open-source-automation"
                / "maa-arknights"
                / "config.json"
            )
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "core_root": str(core),
                        "runtime_root": str(runtime),
                        "task": "Depot",
                        "time_limit": 600,
                    }
                ),
                encoding="utf-8",
            )
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                platform_name="nt",
            )
            snapshot = controller.snapshot()
            controller.close()

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["adapter_id"], "maa-arknights")
        self.assertEqual(snapshot["safety"]["viewport"], "adaptive")
        self.assertEqual(
            snapshot["safety"]["task_allowlist"],
            ["Daily", "Depot", "OperBox", "StartUp", "Award", "Roguelike"],
        )
        self.assertEqual(snapshot["upstream"]["path"], str(core.resolve()))
        self.assertEqual(snapshot["upstream"]["runtime_path"], str(runtime.resolve()))
        self.assertTrue(snapshot["capabilities"]["packaged_runner"])

    def test_packaged_runners_and_guard_policy_are_self_contained(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        source_policy = json.loads(
            (repository_root / "integrations" / "maa" / "guard-policy.json").read_text(
                encoding="utf-8"
            )
        )
        packaged_policy = json.loads(MAA_GUARD_POLICY.read_text(encoding="utf-8"))

        self.assertEqual(packaged_policy, source_policy)
        self.assertEqual(set(MAA_PACKAGED_RUNNERS), {"smoke", "feature", "daily", "roguelike"})
        self.assertTrue(all(path.is_file() for path in MAA_PACKAGED_RUNNERS.values()))

    def test_runtime_options_separate_task_parameters_from_install_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _core, _runtime, adb = self._layout(root)
            controller = MaaArknightsRuntimeController(
                str(adb), root / "output", source, platform_name="nt"
            )
            options = {
                row["id"]: row for row in controller.snapshot()["runtime_options"]
            }
            controller.close()

        self.assertEqual(options["task"]["scope"], "task")
        self.assertEqual(options["fight_stage"]["visible_when"], {"id": "task", "equals": "Daily"})
        self.assertEqual(options["roguelike_theme"]["visible_when"], {"id": "task", "equals": "Roguelike"})
        self.assertEqual(options["roguelike_strategy_preset"]["value"], "stable")
        self.assertEqual(options["core_root"]["scope"], "environment")
        self.assertFalse(options["allow_account_mutation"]["persisted"])

    def test_guarded_roguelike_parameter_overrides_are_validated(self) -> None:
        params = resolve_roguelike_params(
            "Sami",
            {"mode": 1, "squad": "指挥分队", "investment_enabled": False},
            strategy_preset="custom",
        )
        self.assertEqual(params["theme"], "Sami")
        self.assertEqual(params["mode"], 1)
        self.assertEqual(params["squad"], "指挥分队")
        self.assertFalse(params["investment_enabled"])
        stable = resolve_roguelike_params(
            "JieGarden",
            {"mode": 2, "investment_enabled": True, "investments_count": 999},
        )
        self.assertEqual(stable["mode"], 0)
        self.assertFalse(stable["investment_enabled"])
        self.assertEqual(stable["investments_count"], 0)
        with self.assertRaisesRegex(ValueError, "unsupported Roguelike option"):
            resolve_roguelike_params("Sami", {"unsafe": True})
        with self.assertRaisesRegex(ValueError, "strategy preset"):
            resolve_roguelike_params("Sami", {}, strategy_preset="unknown")

    def test_preflight_uses_patched_viewport_and_does_not_persist_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            run = FakeRun()
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                run_func=run,
                platform_name="nt",
            )
            snapshot = controller.preflight(
                {
                    "device": "USB-DEVICE",
                    "task": "Award",
                    "core_root": str(core),
                    "runtime_root": str(runtime),
                    "time_limit": "600",
                    "allow_account_mutation": True,
                }
            )
            config = json.loads(
                (root / "output" / "open-source-automation" / "maa-arknights" / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            controller.close()

        self.assertTrue(snapshot["preflight"]["screen"]["game_ready"])
        self.assertEqual(snapshot["preflight"]["screen"]["width"], 2800)
        smoke_command = next(
            command
            for command in run.calls
            if any(item.endswith("maa_viewport_smoke.py") for item in command)
        )
        self.assertIn("adaptive", smoke_command)
        self.assertNotIn("allow_account_mutation", config)

    def test_award_requires_explicit_session_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                run_func=FakeRun(),
                platform_name="nt",
            )
            with self.assertRaisesRegex(ValueError, "account-mutation"):
                controller.preflight(
                    {
                        "device": "USB-DEVICE",
                        "task": "Award",
                        "core_root": str(core),
                        "runtime_root": str(runtime),
                    }
                )
            controller.close()

    def test_startup_does_not_restart_a_game_already_confirmed_in_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                run_func=FakeRun(),
                platform_name="nt",
            )
            payload = {
                "device": "USB-DEVICE",
                "task": "StartUp",
                "core_root": str(core),
                "runtime_root": str(runtime),
                "time_limit": "600",
            }
            controller.preflight(payload)
            configuration = controller._configuration(payload)
            command = controller._runner_command(
                "USB-DEVICE", root / "run", configuration
            )
            params = json.loads(command[command.index("--params") + 1])
            controller.close()

        self.assertFalse(params["start_game_enabled"])

    def test_preflight_rejects_a_sleeping_screen_even_when_game_is_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                run_func=FakeRun(awake=False),
                platform_name="nt",
            )
            snapshot = controller.preflight(
                {
                    "device": "USB-DEVICE",
                    "task": "StartUp",
                    "core_root": str(core),
                    "runtime_root": str(runtime),
                }
            )
            controller.close()

        self.assertEqual(snapshot["status"], "waiting_for_game")
        self.assertEqual(
            snapshot["preflight"]["screen"]["screen_state"],
            "device_asleep",
        )
        self.assertFalse(snapshot["preflight"]["screen"]["game_ready"])

    def test_roguelike_start_uses_guarded_runner_and_can_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            # Let the adapter derive the MAA source root from build-viewport/bin.
            (core.parents[1] / "resource").mkdir(exist_ok=True)
            run = FakeRun()
            popen = PopenFactory()
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                run_func=run,
                popen_factory=popen,
                platform_name="nt",
            )
            payload = {
                "device": "USB-DEVICE",
                "task": "Roguelike",
                "core_root": str(core),
                "runtime_root": str(runtime),
                "time_limit": "600",
            }
            controller.preflight(payload)
            started = controller.start(payload)
            self.assertTrue(started["running"])
            command = popen.processes[0].command
            self.assertTrue(any(item.endswith("maa_roguelike_runner.py") for item in command))
            self.assertNotIn("--allow-destructive-actions", command)
            self.assertEqual(command[command.index("--strategy-preset") + 1], "stable")
            self.assertIn("--maa-source-root", command)
            self.assertIn("--policy", command)
            controller.stop()
            deadline = time.monotonic() + 2
            while controller.snapshot()["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            snapshot = controller.snapshot()
            controller.close()

        self.assertFalse(snapshot["running"])
        self.assertIn(snapshot["status"], {"stopped", "completed"})

    def test_daily_start_passes_selected_tasks_and_web_parameters_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, core, runtime, adb = self._layout(root)
            run = FakeRun()
            popen = PopenFactory()
            controller = MaaArknightsRuntimeController(
                str(adb),
                root / "output",
                source,
                run_func=run,
                popen_factory=popen,
                platform_name="nt",
            )
            payload = {
                "device": "USB-DEVICE",
                "task": "Daily",
                "core_root": str(core),
                "runtime_root": str(runtime),
                "time_limit": "1800",
                "daily_startup": True,
                "daily_fight": True,
                "daily_infrast": False,
                "daily_recruit": False,
                "daily_mall": True,
                "daily_award": False,
                "fight_stage": "1-7",
                "fight_medicine": "2",
                "fight_times": "5",
                "mall_buy_first": "招聘许可;技巧概要",
                "qwen_enabled": False,
                "allow_account_mutation": True,
            }
            controller.preflight(payload)
            controller.start(payload)
            command = popen.processes[0].command
            controller.stop()

        self.assertTrue(any(item.endswith("maa_daily_runner.py") for item in command))
        self.assertEqual(command[command.index("--tasks") + 1], "StartUp,Fight,Mall")
        task_options = json.loads(command[command.index("--task-options") + 1])
        self.assertEqual(task_options["Fight"]["stage"], "1-7")
        self.assertEqual(task_options["Fight"]["medicine"], 2)
        self.assertEqual(task_options["Fight"]["times"], 5)
        self.assertEqual(task_options["Mall"]["buy_first"], ["招聘许可", "技巧概要"])
        self.assertIn("--no-qwen", command)
        self.assertIn("--allow-account-mutation", command)


if __name__ == "__main__":
    unittest.main()
