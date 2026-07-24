from __future__ import annotations

import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from mobile_profiler.campaign import (
    AndroidCampaignRunner,
    CommandResult,
    _minimum_recording_sample_count,
    _recording_artifact_evidence,
)
from mobile_profiler.campaign_controller import CampaignController
from mobile_profiler.campaign_config import (
    AgentTaskConfig,
    InstallSetConfig,
    load_campaign_config,
)
from mobile_profiler.cli import build_parser


def write_config(root: Path, data: dict) -> Path:
    path = root / "campaign.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def minimal_config(*, source: str = "app.apk", cycle_duration_s: float = 10) -> dict:
    return {
        "version": 1,
        "campaign_id": "test-campaign",
        "device": "serial-1",
        "model": {
            "provider": "openai_compatible",
            "api_base_url": "http://model.test:8000",
            "model": "vision-model",
            "thinking_mode": "disabled",
            "api_key_mode": "none",
        },
        "preparation": {
            "settings": [
                {
                    "namespace": "global",
                    "key": "window_animation_scale",
                    "value": 0,
                }
            ],
            "install_sets": [
                {
                    "name": "Test app",
                    "package": "com.example.app",
                    "source": source,
                }
            ],
            "apps": [
                {
                    "name": "Test app",
                    "package": "com.example.app",
                    "permissions": ["android.permission.POST_NOTIFICATIONS"],
                    "allow_terms_acceptance": True,
                    "setup_tasks": [
                        {
                            "id": "setup",
                            "name": "Setup",
                            "prompt": "Reach the stable home screen and finish.",
                        }
                    ],
                }
            ],
        },
        "test": {
            "cycle_duration_s": cycle_duration_s,
            "offline_grace_s": 2,
            "device_poll_interval_s": 1,
            "recording_start_delay_s": 0,
            "record_finalize_timeout_s": 2,
            "shutdown_finalize_timeout_s": 1,
            "agent_poll_interval_s": 0.1,
            "recording": {"enabled": False},
            "workflows": [
                {
                    "id": "browse",
                    "name": "Browse",
                    "package": "com.example.app",
                    "launch_wait_s": 0,
                    "idle_after_s": 0,
                    "tasks": [
                        {
                            "id": "scroll",
                            "name": "Scroll",
                            "prompt": "Scroll once and verify visible state change.",
                        }
                    ],
                }
            ],
        },
    }


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, duration_s: float) -> None:
        self.value += max(0.0, duration_s)


class FakeAgent:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls
        self.state: dict = {"running": False, "status": "idle"}

    def start(self, payload: dict) -> dict:
        self.calls.append(payload)
        self.state = {
            "running": False,
            "status": "completed",
            "message": "done",
            "total_steps": 2,
            "output_dir": "agent-run",
        }
        return dict(self.state)

    def snapshot(self) -> dict:
        return dict(self.state)

    def stop(self) -> dict:
        self.state["running"] = False
        self.state["status"] = "stopped"
        return dict(self.state)


class FakeCommands:
    def __init__(
        self,
        *,
        installed: set[str] | None = None,
        install_package: str = "com.example.app",
        online_calls: int | None = None,
        foreground_package: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.installed = set(installed or ())
        self.install_package = install_package
        self.online_calls = online_calls
        self.foreground_package = foreground_package
        self.get_state_calls = 0
        self.settings: dict[tuple[str, str], str] = {}

    def __call__(self, command, timeout_s: float) -> CommandResult:
        args = [str(item) for item in command]
        self.calls.append(args)
        tail = args[3:]
        if tail == ["get-state"]:
            self.get_state_calls += 1
            online = self.online_calls is None or self.get_state_calls <= self.online_calls
            return CommandResult(0 if online else 1, "device\n" if online else "", "")
        if tail[:3] == ["shell", "settings", "put"]:
            self.settings[(tail[3], tail[4])] = tail[5]
            return CommandResult(0)
        if tail[:3] == ["shell", "settings", "get"]:
            return CommandResult(0, self.settings.get((tail[3], tail[4]), "null") + "\n")
        if tail[:3] == ["shell", "pm", "path"]:
            package = tail[3]
            if package in self.installed:
                return CommandResult(0, f"package:/data/app/{package}/base.apk\n")
            return CommandResult(1, "", "not installed")
        if tail == ["shell", "dumpsys", "activity", "activities"]:
            if self.foreground_package:
                return CommandResult(
                    0,
                    "  ResumedActivity: ActivityRecord{123 u0 "
                    f"{self.foreground_package}/.MainActivity t1}}\n",
                )
            return CommandResult(0, "ok\n")
        if tail and tail[0] in {"install", "install-multiple"}:
            self.installed.add(self.install_package)
            return CommandResult(0, "Success\n")
        if tail[:3] == ["shell", "pm", "grant"]:
            return CommandResult(0)
        if tail[:3] == ["shell", "cmd", "package"]:
            return CommandResult(0, "granted\n")
        return CommandResult(0, "ok\n")


class CampaignConfigTests(unittest.TestCase):
    def test_example_config_has_two_hour_round_and_independent_stages(self) -> None:
        config = load_campaign_config(Path("examples/android-two-stage-campaign.json"))
        self.assertEqual(config.test.cycle_duration_s, 7200)
        self.assertGreaterEqual(len(config.preparation.install_sets), 4)
        self.assertGreaterEqual(len(config.preparation.apps), 8)
        self.assertGreaterEqual(len(config.test.workflows), 8)
        self.assertFalse(config.test.recording.require_unplugged)
        by_package = {item.package: item for item in config.preparation.apps}
        self.assertEqual(by_package["com.italankin.fifteen"].install_mode, "project")
        self.assertEqual(by_package["com.miHoYo.hkrpg"].install_mode, "external")
        self.assertEqual(by_package["com.miHoYo.hkrpg"].software_type, "game")
        self.assertIn("vision", by_package["com.miHoYo.hkrpg"].supported_engines)
        self.assertEqual(by_package["com.baidu.BaiduMap"].catalog_status, "pending_validation")
        self.assertEqual(by_package["com.tencent.map"].catalog_status, "pending_validation")
        self.assertNotIn("com.tpcstld.twozerogame", by_package)
        self.assertNotIn(
            "com.tpcstld.twozerogame",
            {item.package for item in config.preparation.install_sets},
        )
        self.assertNotIn(
            "com.tpcstld.twozerogame",
            {item.package for item in config.test.workflows},
        )
        self.assertIn(
            "baidu-map-pan",
            {item.workflow_id for item in config.test.workflows},
        )
        self.assertIn(
            "tencent-map-pan",
            {item.workflow_id for item in config.test.workflows},
        )
        workflows = {item.workflow_id: item for item in config.test.workflows}
        dungeon = workflows["shattered-pixel-dungeon-move"]
        self.assertEqual(dungeon.automation_engine, "vision")
        self.assertEqual(dungeon.initialization_tasks[0].task_id, "dungeon-map-ready")
        self.assertIn("英雄", dungeon.contract.entry_state)
        self.assertEqual(
            dungeon.contract.allowed_foreground_packages,
            ("com.shatteredpixel.shatteredpixeldungeon",),
        )
        self.assertEqual(
            workflows["opencalculator-arithmetic"].automation_engine,
            "hybrid",
        )
        self.assertEqual(
            workflows["fossify-calculator-arithmetic"].automation_engine,
            "hybrid",
        )
        quark = workflows["quark-search-editor-roundtrip"]
        self.assertNotIn("input_text", {
            action
            for requirement in quark.contract.required_actions
            for action in requirement.actions
        })
        self.assertIn("软键盘", quark.contract.success_evidence)
        material = workflows["material-files-directory-roundtrip"]
        self.assertEqual(
            material.tasks[0].action_limits[0].actions,
            ("tap", "tap_element"),
        )
        self.assertEqual(material.tasks[0].action_limits[0].maximum, 1)
        self.assertEqual(material.tasks[0].action_limits[1].actions, ("back",))
        self.assertIn(
            "back",
            material.initialization_tasks[0].action_limits[0].actions,
        )
        self.assertEqual(material.initialization_tasks[0].action_limits[0].maximum, 0)
        self.assertEqual(
            workflows["minesweeper-compose-reveal"].automation_engine,
            "hybrid",
        )
        self.assertEqual(
            workflows["star-rail-safe-camera"].contract.login_policy,
            "existing_session_only",
        )
        self.assertTrue(workflows["sgtpuzzles-light-up-toggle"].repeat_after_success)
        self.assertEqual(
            workflows["sgtpuzzles-light-up-toggle"].tasks[0].action_limits[0].maximum,
            3,
        )
        self.assertEqual(
            workflows["super-snake-turn"].tasks[0].action_limits[0].maximum,
            2,
        )
        for workflow_id in (
            "baidu-public-feed",
            "zhihu-public-feed",
            "tencent-news-public-feed",
            "sohu-news-public-feed",
            "bilibili-public-feed",
            "xiaohongshu-public-feed",
        ):
            task = workflows[workflow_id].tasks[0]
            self.assertIn('"start":[500,800]', task.prompt)
            self.assertEqual(task.action_limits[0].actions, ("swipe", "swipe_fast"))
            self.assertEqual(task.action_limits[0].maximum, 2)
        fossil = workflows["fossify-calculator-arithmetic"].tasks[0]
        self.assertEqual(fossil.action_limits[2].maximum, 4)
        self.assertEqual(fossil.action_limits[2].maximum_per_signature, 1)
        self.assertFalse(workflows["star-rail-safe-camera"].repeat_after_success)
    def test_campaign_recording_allows_external_power_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config()
            data["test"]["recording"] = {"enabled": True}
            config = load_campaign_config(write_config(root, data)).with_device("serial-1")
            runner = AndroidCampaignRunner("adb", config, root / "output")

            command = runner.build_record_command(root / "recording", 60)

        self.assertFalse(config.test.recording.require_unplugged)
        self.assertIn("--allow-external-power", command)
        self.assertNotIn("--require-unplugged", command)

    def test_campaign_can_explicitly_require_unplugged_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config()
            data["test"]["recording"] = {
                "enabled": True,
                "require_unplugged": True,
            }
            config = load_campaign_config(write_config(root, data)).with_device("serial-1")
            runner = AndroidCampaignRunner("adb", config, root / "output")

            command = runner.build_record_command(root / "recording", 60)

        self.assertTrue(config.test.recording.require_unplugged)
        self.assertIn("--require-unplugged", command)
        self.assertNotIn("--allow-external-power", command)

    def test_numeric_zero_setting_is_not_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
        self.assertEqual(config.preparation.settings[0].value, "0")

    def test_unknown_android_setting_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = minimal_config()
            data["preparation"]["settings"][0]["key"] = "dangerous_unknown_key"
            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                load_campaign_config(write_config(root, data))

    def test_unknown_required_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config()
            data["test"]["workflows"][0]["contract"] = {
                "required_actions": [
                    {"actions": ["shell"], "minimum": 1}
                ]
            }

            with self.assertRaisesRegex(ValueError, "unknown actions: shell"):
                load_campaign_config(write_config(root, data))

    def test_cli_parser_exposes_independent_prepare_and_test_commands(self) -> None:
        parser = build_parser()
        prepare = parser.parse_args(
            ["campaign", "prepare", "config.json", "--device", "serial-1", "--dry-run"]
        )
        test = parser.parse_args(
            ["campaign", "test", "config.json", "--device", "serial-1", "--max-rounds", "1"]
        )
        self.assertEqual(prepare.handler.__name__, "run_campaign_prepare")
        self.assertEqual(test.handler.__name__, "run_campaign_test")
        self.assertEqual(test.max_rounds, 1)


class CampaignRunnerTests(unittest.TestCase):
    def test_recording_evidence_requires_final_samples_analysis_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recording_dir = Path(temporary)
            missing = _recording_artifact_evidence(recording_dir)
            self.assertFalse(missing["artifacts_complete"])
            self.assertIn("checkpoint.json is missing", missing["artifact_errors"])

            (recording_dir / "checkpoint.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "sample_count": 59,
                        "context_count": 31,
                        "thermal_snapshot_count": 31,
                        "reconnect_count": 0,
                        "stop_reason": "completed",
                    }
                ),
                encoding="utf-8",
            )
            for name in ("samples.csv", "analysis.json", "report.html"):
                (recording_dir / name).write_text("usable", encoding="utf-8")
            complete = _recording_artifact_evidence(recording_dir)

        self.assertTrue(complete["artifacts_complete"])
        self.assertEqual(complete["sample_count"], 59)
        self.assertEqual(complete["checkpoint_status"], "complete")
        self.assertEqual(_minimum_recording_sample_count(300, 5), 48)
        self.assertGreaterEqual(59, _minimum_recording_sample_count(300, 5))

    def test_zero_artifact_recorder_cannot_pass_round_acceptance(self) -> None:
        class ExitZeroRecorder:
            def __init__(self, clock: FakeClock, deadline: float) -> None:
                self.clock = clock
                self.deadline = deadline

            def poll(self):
                return 0 if self.clock() >= self.deadline else None

            def wait(self, timeout=None):
                return 0

            def terminate(self) -> None:
                return None

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=5)
            data["test"]["workflows"][0]["repeat_after_success"] = False
            data["test"]["recording"] = {"enabled": True, "interval_s": 1}
            config = load_campaign_config(write_config(root, data))
            clock = FakeClock()
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.example.app",
                ),
                recorder_factory=lambda _command, _log: ExitZeroRecorder(clock, 5),
                agent_factory=lambda _adb, _root: FakeAgent([]),
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)
            round_result = result["rounds"][0]

        self.assertEqual(round_result["recording"]["exit_code"], 0)
        self.assertFalse(round_result["recording"]["artifacts_complete"])
        self.assertFalse(round_result["acceptance"]["recording_ok"])
        self.assertFalse(round_result["acceptance"]["passed"])

    def test_rotation_setting_uses_window_service_fallback_when_vendor_reverts_it(self) -> None:
        class RotationCommands(FakeCommands):
            def __call__(self, command, timeout_s: float) -> CommandResult:
                args = [str(item) for item in command]
                tail = args[3:]
                if tail[:5] == [
                    "shell",
                    "settings",
                    "put",
                    "system",
                    "accelerometer_rotation",
                ]:
                    self.calls.append(args)
                    self.settings[("system", "accelerometer_rotation")] = "1"
                    self.settings[("system", "user_rotation")] = "0"
                    return CommandResult(0)
                if tail[:5] == [
                    "shell",
                    "cmd",
                    "window",
                    "user-rotation",
                    "lock",
                ]:
                    self.calls.append(args)
                    self.settings[("system", "accelerometer_rotation")] = "0"
                    self.settings[("system", "user_rotation")] = tail[5]
                    return CommandResult(0)
                return super().__call__(command, timeout_s)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = minimal_config()
            data["preparation"]["settings"] = [
                {
                    "namespace": "system",
                    "key": "accelerometer_rotation",
                    "value": "0",
                    "required": True,
                }
            ]
            config = load_campaign_config(write_config(root, data))
            commands = RotationCommands()
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
            )

            result = runner._apply_setting(config.preparation.settings[0])

        self.assertTrue(result["succeeded"])
        self.assertEqual(result["actual"], "0")
        self.assertIn("cmd window user-rotation lock 0", result["compatibility_fallback"])

    def test_workflow_stops_when_an_action_opens_a_forbidden_foreground_package(self) -> None:
        class RunningAgent:
            def __init__(self) -> None:
                self.stopped = False
                self.state = {"running": False, "status": "idle"}

            def start(self, payload: dict) -> dict:
                self.state = {
                    "running": True,
                    "status": "running",
                    "output_dir": "agent-run",
                }
                return dict(self.state)

            def snapshot(self) -> dict:
                return dict(self.state)

            def stop(self) -> dict:
                self.stopped = True
                self.state.update({"running": False, "status": "stopped"})
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
            commands = FakeCommands(
                installed={"com.example.app"},
                foreground_package="com.android.packageinstaller",
            )
            agent = RunningAgent()
            clock = FakeClock()
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: agent,
                repeat_workflows=False,
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)

        workflow = result["rounds"][0]["workflow_results"][0]
        self.assertTrue(agent.stopped)
        self.assertEqual(workflow["status"], "forbidden_foreground")
        self.assertEqual(
            workflow["agent"]["foreground_package"],
            "com.android.packageinstaller",
        )
        self.assertEqual(
            result["rounds"][0]["quarantined_workflows"],
            {"browse": "forbidden_foreground"},
        )

    def test_workflow_can_force_stop_before_each_launcher_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=100)
            data["test"]["workflows"][0]["force_stop_before_launch"] = True
            config = load_campaign_config(write_config(root, data))
            commands = FakeCommands(
                installed={"com.example.app"},
                foreground_package="com.example.app",
            )
            clock = FakeClock()
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: FakeAgent([]),
                repeat_workflows=False,
                clock=clock,
                sleep=clock.sleep,
            )

            runner.run_test(max_rounds=1)

        tails = [call[3:] for call in commands.calls]
        force_stop = ["shell", "am", "force-stop", "com.example.app"]
        launcher = [
            "shell",
            "monkey",
            "-p",
            "com.example.app",
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ]
        self.assertIn(force_stop, tails)
        self.assertIn(launcher, tails)
        self.assertLess(tails.index(force_stop), tails.index(launcher))

    def test_workflow_can_stop_repeating_after_its_first_strict_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=5)
            data["test"]["workflows"][0]["repeat_after_success"] = False
            config = load_campaign_config(write_config(root, data))
            commands = FakeCommands(
                installed={"com.example.app"},
                foreground_package="com.example.app",
            )
            clock = FakeClock()
            agent_calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: FakeAgent(agent_calls),
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)

        self.assertEqual(len(agent_calls), 1)
        self.assertFalse(config.test.workflows[0].repeat_after_success)
        self.assertTrue(result["rounds"][0]["acceptance"]["passed"])
        self.assertTrue(result["rounds"][0]["acceptance"]["duration_reached"])

    def test_preparation_applies_setting_installs_grants_and_runs_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
            commands = FakeCommands(foreground_package="com.example.app")
            agent_calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: FakeAgent(agent_calls),
                task_overrides={
                    "setup": AgentTaskConfig(
                        task_id="setup",
                        name="Edited preparation task",
                        prompt="Edited preparation prompt",
                        attention_prompt="Edited preparation attention",
                        max_steps=29,
                        timeout_s=345,
                        on_failure="stop",
                    )
                },
            )

            result = runner.prepare()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(commands.settings[("global", "window_animation_scale")], "0")
            self.assertIn("com.example.app", commands.installed)
            self.assertEqual(len(agent_calls), 2)
            task = agent_calls[0]["tasks"][0]
            self.assertIn("用户已明确授权", task["prompt"])
            self.assertIn("android.permission.POST_NOTIFICATIONS", task["prompt"])
            self.assertIn("Edited preparation prompt", task["prompt"])
            self.assertIn("Edited preparation attention", task["attention_prompt"])
            self.assertEqual(task["max_steps"], 29)
            self.assertEqual(task["timeout_s"], 345)
            validation_task = agent_calls[1]["tasks"][0]
            self.assertEqual(validation_task["id"], "scroll")
            self.assertIn("正式流程支持验证", validation_task["prompt"])
            self.assertTrue(result["app_results"][0]["normal_flow_supported"])
            validation = result["app_results"][0]["workflow_validations"][0]
            self.assertTrue(validation["foreground_verified"])
            self.assertTrue(validation["foreground_matches"])
            self.assertTrue(Path(result["output_dir"], "state.json").exists())

    def test_preparation_rejects_completed_validation_in_wrong_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
            commands = FakeCommands(foreground_package="com.android.settings")
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: FakeAgent([]),
            )

            result = runner.prepare()

            app_result = result["app_results"][0]
            validation = app_result["workflow_validations"][0]
            self.assertFalse(app_result["normal_flow_supported"])
            self.assertFalse(app_result["succeeded"])
            self.assertEqual(validation["status"], "wrong_foreground")
            self.assertEqual(validation["agent_status"], "completed")
            self.assertEqual(validation["foreground_package"], "com.android.settings")
            self.assertFalse(validation["foreground_matches"])

    def test_preparation_reuses_an_already_installed_archive_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
            commands = FakeCommands(installed={"com.example.app"})
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: FakeAgent([]),
            )

            result = runner.prepare()

            install_result = result["install_results"][0]
            self.assertTrue(install_result["succeeded"])
            self.assertTrue(install_result["already_installed"])
            self.assertTrue(install_result["skipped"])
            self.assertFalse(
                any(call[3] in {"install", "install-multiple"} for call in commands.calls)
            )

    def test_secure_keyguard_blocks_test_before_recording_or_agent_actions(self) -> None:
        class SecureLockedCommands(FakeCommands):
            def __call__(self, command, timeout_s: float) -> CommandResult:
                args = [str(item) for item in command]
                tail = args[3:]
                if tail == ["shell", "dumpsys", "power"]:
                    self.calls.append(args)
                    return CommandResult(0, "  mWakefulness=Awake\n")
                if tail == ["shell", "dumpsys", "window", "policy"]:
                    self.calls.append(args)
                    return CommandResult(0, "      showing=true\n      secure=true\n")
                return super().__call__(command, timeout_s)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
            calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=SecureLockedCommands(installed={"com.example.app"}),
                agent_factory=lambda _adb, _root: FakeAgent(calls),
            )

            result = runner.run_test(max_rounds=1)

            self.assertEqual(result["status"], "device_locked")
            self.assertEqual(result["round_count"], 0)
            self.assertEqual(calls, [])

    def test_record_command_allows_external_power_for_two_hour_campaign(self) -> None:
        config = load_campaign_config(Path("examples/android-two-stage-campaign.json")).with_device(
            "serial-1"
        )
        runner = AndroidCampaignRunner("adb", config, Path("out"))
        command = runner.build_record_command(Path("round/recording"), 7200)
        self.assertIn("7200", command)
        self.assertIn("--session-mode", command)
        self.assertIn("--allow-external-power", command)
        self.assertNotIn("--require-unplugged", command)
        self.assertIn("low-overhead", command)
        self.assertIn("thermal", command)
        self.assertIn("runtime_settings", command)

    def test_test_stage_stops_after_device_offline_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(
                write_config(root, minimal_config(cycle_duration_s=100))
            )
            commands = FakeCommands(
                installed={"com.example.app"},
                online_calls=3,
            )
            clock = FakeClock()
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                agent_factory=lambda _adb, _root: FakeAgent([]),
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test()

            self.assertEqual(result["status"], "device_shutdown_or_unavailable")
            self.assertEqual(result["round_count"], 1)
            self.assertEqual(result["rounds"][0]["status"], "device_unavailable")
            self.assertGreaterEqual(clock.value, 2)

    def test_agent_short_device_outage_recovers_without_stopping_workflow(self) -> None:
        class IntermittentCommands(FakeCommands):
            def __init__(self) -> None:
                super().__init__(installed={"com.example.app"})
                self.states = iter([True, True, False, False, True, True])

            def __call__(self, command, timeout_s: float) -> CommandResult:
                args = [str(item) for item in command]
                tail = args[3:]
                if tail == ["get-state"]:
                    self.calls.append(args)
                    online = next(self.states, True)
                    return CommandResult(
                        0 if online else 1,
                        "device\n" if online else "",
                        "" if online else "temporarily offline",
                    )
                return super().__call__(command, timeout_s)

        class RecoveringAgent:
            def __init__(self, calls: list[dict]) -> None:
                self.calls = calls
                self.snapshot_calls = 0
                self.stop_calls = 0
                self.state = {"running": False, "status": "idle"}

            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                self.state = {"running": True, "status": "running"}
                return dict(self.state)

            def snapshot(self) -> dict:
                self.snapshot_calls += 1
                if self.snapshot_calls >= 3:
                    self.state = {
                        "running": False,
                        "status": "completed",
                        "message": "recovered",
                    }
                return dict(self.state)

            def stop(self) -> dict:
                self.stop_calls += 1
                self.state = {"running": False, "status": "stopped"}
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(
                write_config(root, minimal_config(cycle_duration_s=100))
            )
            clock = FakeClock()
            agent_calls: list[dict] = []
            agents: list[RecoveringAgent] = []

            def agent_factory(_adb: str, _root: Path) -> RecoveringAgent:
                agent = RecoveringAgent(agent_calls)
                agents.append(agent)
                return agent

            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=IntermittentCommands(),
                agent_factory=agent_factory,
                repeat_workflows=False,
                clock=clock,
                sleep=clock.sleep,
            )
            runner._command_runner.foreground_package = "com.example.app"  # type: ignore[attr-defined]

            result = runner.run_test(max_rounds=1)
            events = [
                json.loads(line)
                for line in Path(result["output_dir"], "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["status"], "max_rounds")
        self.assertEqual(result["rounds"][0]["workflow_results"][0]["status"], "completed")
        self.assertEqual(len(agent_calls), 1)
        self.assertEqual(agent_calls[0]["screenshot_retry_timeout_s"], 2)
        self.assertEqual(agents[0].stop_calls, 0)
        event_types = [event["event_type"] for event in events]
        self.assertIn("agent_device_offline", event_types)
        self.assertIn("agent_device_reconnected", event_types)
        self.assertNotIn("agent_device_offline_timeout", event_types)

    def test_take_over_workflow_is_disabled_for_rest_of_round(self) -> None:
        class TakeOverAgent(FakeAgent):
            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                self.state = {
                    "running": False,
                    "status": "take_over",
                    "message": "verification required",
                }
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(
                write_config(root, minimal_config(cycle_duration_s=3))
            )
            clock = FakeClock()
            agent_calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(installed={"com.example.app"}),
                agent_factory=lambda _adb, _root: TakeOverAgent(agent_calls),
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)
            events = [
                json.loads(line)
                for line in Path(result["output_dir"], "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["status"], "max_rounds")
        self.assertEqual(len(agent_calls), 1)
        disabled = [
            event
            for event in events
            if event.get("event_type") == "workflow_disabled"
        ]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0]["reason"], "take_over")

    def test_wrong_foreground_workflow_is_disabled_for_rest_of_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(
                write_config(root, minimal_config(cycle_duration_s=3))
            )
            clock = FakeClock()
            agent_calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.android.settings",
                ),
                agent_factory=lambda _adb, _root: FakeAgent(agent_calls),
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)
            round_result = result["rounds"][0]
            workflow_result = round_result["workflow_results"][0]
            events = [
                json.loads(line)
                for line in Path(result["output_dir"], "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["status"], "max_rounds")
        self.assertEqual(len(agent_calls), 1)
        self.assertEqual(workflow_result["status"], "wrong_foreground")
        self.assertEqual(workflow_result["agent_status"], "completed")
        self.assertEqual(
            workflow_result["foreground_package"], "com.android.settings"
        )
        self.assertFalse(workflow_result["foreground_matches"])
        disabled = [
            event
            for event in events
            if event.get("event_type") == "workflow_disabled"
        ]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0]["reason"], "wrong_foreground")

    def test_workflow_runs_initialization_before_validation_with_its_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=100)
            workflow = data["test"]["workflows"][0]
            workflow["automation_engine"] = "hybrid"
            workflow["contract"] = {
                "entry_state": "stable app home",
                "success_evidence": "fresh card change",
                "forbidden_states": ["login page"],
            }
            workflow["initialization_tasks"] = [
                {
                    "id": "initialize-home",
                    "name": "Initialize home",
                    "prompt": "Reach the stable app home.",
                    "on_failure": "stop",
                }
            ]
            config = load_campaign_config(write_config(root, data))
            calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.example.app",
                ),
                agent_factory=lambda _adb, _root: FakeAgent(calls),
                repeat_workflows=False,
            )

            result = runner.run_test(max_rounds=1)
            workflow_result = result["rounds"][0]["workflow_results"][0]

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tasks"][0]["id"], "initialize-home")
        self.assertEqual(calls[1]["tasks"][0]["id"], "scroll")
        self.assertEqual(calls[0]["automation_engine"], "hybrid")
        self.assertEqual(calls[1]["automation_engine"], "hybrid")
        self.assertIn("独立初始化阶段", calls[0]["tasks"][0]["prompt"])
        self.assertIn("stable app home", calls[1]["tasks"][0]["prompt"])
        self.assertIn("fresh card change", calls[1]["tasks"][0]["prompt"])
        self.assertTrue(workflow_result["initialization_evidence_complete"])
        self.assertTrue(workflow_result["evidence_complete"])
        self.assertTrue(result["rounds"][0]["acceptance"]["passed"])
        self.assertTrue(result["acceptance"]["passed"])

    def test_incomplete_evidence_is_retried_then_quarantined(self) -> None:
        class SkippingAgent(FakeAgent):
            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                self.state = {
                    "running": False,
                    "status": "completed_with_warnings",
                    "message": "skipped",
                    "task_results": [
                        {
                            "id": payload["tasks"][0]["id"],
                            "status": "skipped",
                            "message": "blocked",
                        }
                    ],
                }
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=4)
            workflow = data["test"]["workflows"][0]
            workflow["quarantine_after_failures"] = 2
            workflow["retry_cooldown_s"] = 0
            config = load_campaign_config(write_config(root, data))
            clock = FakeClock()
            calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.example.app",
                ),
                agent_factory=lambda _adb, _root: SkippingAgent(calls),
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)
            round_result = result["rounds"][0]
            events = [
                json.loads(line)
                for line in Path(result["output_dir"], "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [item["status"] for item in round_result["workflow_results"]],
            ["incomplete_evidence", "incomplete_evidence"],
        )
        self.assertEqual(
            round_result["quarantined_workflows"],
            {"browse": "incomplete_evidence"},
        )
        self.assertFalse(round_result["acceptance"]["passed"])
        self.assertFalse(result["acceptance"]["passed"])
        self.assertIn(
            "workflow_quarantined",
            [event["event_type"] for event in events],
        )

    def test_completed_agent_must_satisfy_host_action_evidence(self) -> None:
        class OneSwipeAgent(FakeAgent):
            def __init__(self, calls: list[dict], output_root: Path) -> None:
                super().__init__(calls)
                self.output_dir = output_root / "synthetic-agent"

            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                self.output_dir.mkdir(parents=True, exist_ok=True)
                (self.output_dir / "events.jsonl").write_text(
                    json.dumps(
                        {
                            "event_type": "action",
                            "action": {"action": "swipe_fast"},
                            "action_valid": True,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.state = {
                    "running": False,
                    "status": "completed",
                    "message": "claimed complete",
                    "output_dir": str(self.output_dir),
                    "task_results": [
                        {
                            "id": payload["tasks"][0]["id"],
                            "status": "completed",
                            "message": "claimed complete",
                        }
                    ],
                }
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=100)
            data["test"]["workflows"][0]["contract"] = {
                "required_actions": [
                    {
                        "actions": ["swipe", "swipe_fast"],
                        "minimum": 2,
                        "label": "two scrolls",
                    }
                ]
            }
            config = load_campaign_config(write_config(root, data))
            calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.example.app",
                ),
                agent_factory=lambda _adb, output_root: OneSwipeAgent(
                    calls, output_root
                ),
                repeat_workflows=False,
            )

            result = runner.run_test(max_rounds=1)
            workflow_result = result["rounds"][0]["workflow_results"][0]

        self.assertEqual(workflow_result["status"], "incomplete_action_evidence")
        self.assertFalse(workflow_result["evidence_complete"])
        requirement = workflow_result["action_evidence"]["requirements"][0]
        self.assertEqual(requirement["observed"], 1)
        self.assertFalse(requirement["satisfied"])
        self.assertFalse(result["acceptance"]["passed"])

    def test_completed_agent_with_failure_message_is_not_accepted(self) -> None:
        class ContradictingAgent(FakeAgent):
            def __init__(self, calls: list[dict], output_root: Path) -> None:
                super().__init__(calls)
                self.output_dir = output_root / "synthetic-agent"

            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                self.output_dir.mkdir(parents=True, exist_ok=True)
                (self.output_dir / "events.jsonl").write_text(
                    json.dumps(
                        {
                            "event_type": "action",
                            "action": {"action": "tap"},
                            "action_valid": True,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                message = "游戏已结束，无法完成横向移动测试"
                self.state = {
                    "running": False,
                    "status": "completed",
                    "message": "claimed complete",
                    "output_dir": str(self.output_dir),
                    "task_results": [
                        {
                            "id": payload["tasks"][0]["id"],
                            "status": "completed",
                            "message": message,
                        }
                    ],
                    "latest_action": {"action": "finish", "message": message},
                }
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=100)
            data["test"]["workflows"][0]["contract"] = {
                "required_actions": [
                    {"actions": ["tap"], "minimum": 1, "label": "one tap"}
                ]
            }
            config = load_campaign_config(write_config(root, data))
            calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.example.app",
                ),
                agent_factory=lambda _adb, output_root: ContradictingAgent(
                    calls, output_root
                ),
                repeat_workflows=False,
            )

            result = runner.run_test(max_rounds=1)
            workflow_result = result["rounds"][0]["workflow_results"][0]

        self.assertEqual(
            workflow_result["status"], "contradicted_completion_claim"
        )
        self.assertFalse(workflow_result["evidence_complete"])
        self.assertFalse(workflow_result["completion_claim"]["satisfied"])
        self.assertTrue(workflow_result["action_evidence"]["satisfied"])
        self.assertFalse(result["acceptance"]["passed"])

    def test_agent_is_stopped_at_round_deadline(self) -> None:
        class NeverEndingAgent:
            def __init__(self, calls: list[dict]) -> None:
                self.calls = calls
                self.stop_calls = 0
                self.state = {"running": False, "status": "idle"}

            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                self.state = {"running": True, "status": "running"}
                return dict(self.state)

            def snapshot(self) -> dict:
                return dict(self.state)

            def stop(self) -> dict:
                self.stop_calls += 1
                self.state = {"running": False, "status": "stopped"}
                return dict(self.state)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(
                write_config(root, minimal_config(cycle_duration_s=3))
            )
            clock = FakeClock()
            calls: list[dict] = []
            agents: list[NeverEndingAgent] = []

            def agent_factory(_adb: str, _root: Path) -> NeverEndingAgent:
                agent = NeverEndingAgent(calls)
                agents.append(agent)
                return agent

            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(
                    installed={"com.example.app"},
                    foreground_package="com.example.app",
                ),
                agent_factory=agent_factory,
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)
            round_result = result["rounds"][0]
            progress = json.loads(
                Path(round_result["round_dir"], "round-progress.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(agents[0].stop_calls, 1)
        self.assertEqual(
            round_result["workflow_results"][0]["status"],
            "round_deadline",
        )
        self.assertLess(round_result["active_duration_s"], 4)
        self.assertFalse(round_result["acceptance"]["passed"])
        self.assertIn("coverage", progress)
        self.assertIn("acceptance", progress)

    def test_keyboard_interrupt_finalizes_and_recovers_active_round(self) -> None:
        class InterruptingAgent(FakeAgent):
            def __init__(self, calls: list[dict]) -> None:
                super().__init__(calls)
                self.stop_calls = 0

            def start(self, payload: dict) -> dict:
                self.calls.append(payload)
                raise KeyboardInterrupt

            def stop(self) -> dict:
                self.stop_calls += 1
                return super().stop()

        class FakeRecorder:
            def __init__(self) -> None:
                self.terminated = False
                self.closed = False

            def poll(self):
                return None

            def wait(self, timeout=None):
                return -15

            def terminate(self) -> None:
                self.terminated = True

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=100)
            data["test"]["recording"] = {"enabled": True}
            config = load_campaign_config(write_config(root, data))
            commands = FakeCommands(installed={"com.example.app"})
            recorders: list[FakeRecorder] = []

            def recorder_factory(command, _log_path: Path) -> FakeRecorder:
                output_index = list(command).index("--output") + 1
                recording_dir = Path(str(command[output_index]))
                recording_dir.mkdir(parents=True, exist_ok=True)
                (recording_dir / "partial.jsonl").write_text(
                    "{}\n", encoding="utf-8"
                )
                recorder = FakeRecorder()
                recorders.append(recorder)
                return recorder

            agent_calls: list[dict] = []
            agents: list[InterruptingAgent] = []

            def agent_factory(_adb: str, _root: Path) -> InterruptingAgent:
                agent = InterruptingAgent(agent_calls)
                agents.append(agent)
                return agent

            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=commands,
                recorder_factory=recorder_factory,
                agent_factory=agent_factory,
            )

            result = runner.run_test(max_rounds=1)
            round_result = result["rounds"][0]
            summary = json.loads(
                Path(round_result["round_dir"], "round-summary.json").read_text(
                    encoding="utf-8"
                )
            )
            events = [
                json.loads(line)
                for line in Path(result["output_dir"], "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["status"], "operator_stopped")
        self.assertEqual(result["round_count"], 1)
        self.assertEqual(round_result["status"], "stopped")
        self.assertTrue(round_result["operator_interrupted"])
        self.assertEqual(summary["status"], "stopped")
        self.assertTrue(recorders[0].terminated)
        self.assertTrue(recorders[0].closed)
        self.assertGreaterEqual(agents[0].stop_calls, 1)
        self.assertTrue(any("recover" in call for call in commands.calls))
        self.assertIn("record_recover", [event["event_type"] for event in events])

    def test_test_stage_refuses_to_start_without_required_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(write_config(root, minimal_config()))
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(installed=set()),
                agent_factory=lambda _adb, _root: FakeAgent([]),
            )

            result = runner.run_test(max_rounds=1)

            self.assertEqual(result["status"], "missing_required_packages")
            self.assertEqual(result["missing_required_packages"], ["com.example.app"])
            self.assertEqual(result["round_count"], 0)

    def test_single_pass_uses_ui_task_overrides_and_does_not_repeat_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            config = load_campaign_config(
                write_config(root, minimal_config(cycle_duration_s=7200))
            )
            agent_calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(installed={"com.example.app"}),
                agent_factory=lambda _adb, _root: FakeAgent(agent_calls),
                task_overrides={
                    "scroll": AgentTaskConfig(
                        task_id="scroll",
                        name="UI edited task",
                        prompt="UI edited prompt",
                        attention_prompt="UI edited attention",
                        max_steps=37,
                        timeout_s=456,
                        on_failure="continue",
                    )
                },
                repeat_workflows=False,
            )

            result = runner.run_test(max_rounds=1)

            self.assertEqual(result["status"], "max_rounds")
            self.assertEqual(len(agent_calls), 1)
            task = agent_calls[0]["tasks"][0]
            self.assertEqual(task["name"], "UI edited task")
            self.assertIn("UI edited prompt", task["prompt"])
            self.assertIn("UI edited attention", task["attention_prompt"])
            self.assertEqual(task["max_steps"], 37)
            self.assertEqual(task["timeout_s"], 456)
            self.assertTrue(result["rounds"][0]["workflow_pass_complete"])

    def test_ui_task_order_reorders_campaign_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config(cycle_duration_s=100)
            data["test"]["workflows"].append(
                {
                    "id": "second-workflow",
                    "name": "Second workflow",
                    "package": "com.example.app",
                    "launch_wait_s": 0,
                    "idle_after_s": 0,
                    "tasks": [
                        {
                            "id": "second-task",
                            "name": "Second task",
                            "prompt": "Run second task.",
                        }
                    ],
                }
            )
            config = load_campaign_config(write_config(root, data))
            clock = FakeClock()
            agent_calls: list[dict] = []
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(installed={"com.example.app"}),
                agent_factory=lambda _adb, _root: FakeAgent(agent_calls),
                task_order=("second-task", "scroll"),
                repeat_workflows=False,
                clock=clock,
                sleep=clock.sleep,
            )

            result = runner.run_test(max_rounds=1)

        self.assertEqual(result["status"], "max_rounds")
        self.assertEqual(
            [call["tasks"][0]["id"] for call in agent_calls],
            ["second-task", "scroll"],
        )
        self.assertEqual(
            [item["workflow_id"] for item in result["rounds"][0]["workflow_results"]],
            ["second-workflow", "browse"],
        )

    def test_apks_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "bad.apks"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../base.apk", b"apk")
            data = minimal_config(source="bad.apks")
            config = load_campaign_config(write_config(root, data))
            runner = AndroidCampaignRunner(
                "adb",
                config,
                root / "output",
                command_runner=FakeCommands(),
                agent_factory=lambda _adb, _root: FakeAgent([]),
            )
            install_set = InstallSetConfig(
                "bad",
                "com.example.app",
                archive_path,
            )
            with self.assertRaisesRegex(ValueError, "unsafe APK entry"):
                with runner._apk_files(install_set):
                    pass


class CampaignControllerTests(unittest.TestCase):
    def test_controller_task_results_include_host_action_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = CampaignController("adb", Path(temporary), None)
            tasks = [
                {
                    "id": "scroll",
                    "name": "Scroll",
                    "prompt": "Scroll twice",
                }
            ]
            results = controller._completed_task_results(
                "test",
                {
                    "status": "max_rounds",
                    "rounds": [
                        {
                            "workflow_results": [
                                {
                                    "status": "incomplete_action_evidence",
                                    "evidence_complete": False,
                                    "initialization_evidence_complete": True,
                                    "action_evidence": {
                                        "requirements": [
                                            {
                                                "label": "two scrolls",
                                                "observed": 1,
                                                "minimum": 2,
                                                "satisfied": False,
                                            }
                                        ]
                                    },
                                    "agent": {
                                        "task_results": [
                                            {
                                                "id": "scroll",
                                                "status": "completed",
                                                "message": "model claimed complete",
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                },
                tasks,
            )

        self.assertEqual(results[0]["status"], "incomplete_action_evidence")
        self.assertIn("1/2", results[0]["message"])

    def test_controller_surfaces_failed_strict_round_acceptance(self) -> None:
        status, message = CampaignController._presentation_status(
            "test",
            {
                "status": "max_rounds",
                "message": "completed requested 1 rounds",
                "acceptance": {
                    "passed": False,
                    "accepted_round_count": 0,
                    "round_count": 1,
                },
            },
        )

        self.assertEqual(status, "completed_with_warnings")
        self.assertIn("0/1", message)

    def test_controller_exposes_json_derived_stage_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = CampaignController(
                "adb",
                Path(temporary),
                Path("examples/android-two-stage-campaign.json"),
            )

            overview = controller.stage_config_snapshot()
            snapshot = controller.snapshot()
            controller.close()

        self.assertTrue(overview["available"])
        self.assertTrue(str(overview["source_path"]).endswith("android-two-stage-campaign.json"))
        preparation = overview["stages"]["prepare"]
        test = overview["stages"]["test"]
        preparation_metrics = {
            item["label"]: item["value"] for item in preparation["metrics"]
        }
        self.assertEqual(preparation_metrics["系统设置"], 10)
        self.assertEqual(preparation_metrics["固定安装包"], 7)
        self.assertEqual(preparation_metrics["预备应用"], 23)
        self.assertEqual(preparation_metrics["已映射 workflow"], 23)
        self.assertEqual(len(preparation["flow"]), 6)
        warning_titles = {item["title"] for item in preparation["warnings"]}
        self.assertNotIn("预备应用没有正式 workflow", warning_titles)
        self.assertIn("存在浏览器下载 / APK 安装路径", warning_titles)

        test_metrics = {item["label"]: item["value"] for item in test["metrics"]}
        self.assertEqual(test_metrics["单轮时长"], "120")
        self.assertEqual(test_metrics["workflow"], 23)
        self.assertEqual(test_metrics["成功后继续循环"], 5)
        self.assertEqual(len(test["flow"]), 6)
        groups = {item["id"]: item for item in test["workflow_groups"]}
        self.assertEqual(groups["required_baseline"]["count"], 4)
        self.assertEqual(
            sum(group["count"] for group in test["workflow_groups"]),
            23,
        )
        workflow_ids = {
            workflow["id"]
            for group in test["workflow_groups"]
            for workflow in group["items"]
        }
        self.assertIn("material-files-directory-roundtrip", workflow_ids)
        material = next(
            workflow
            for group in test["workflow_groups"]
            for workflow in group["items"]
            if workflow["id"] == "material-files-directory-roundtrip"
        )
        self.assertEqual(
            material["initialization_tasks"][0]["action_limits"][0]["maximum"],
            0,
        )
        self.assertEqual(material["tasks"][0]["action_limits"][0]["maximum"], 1)
        self.assertIs(snapshot["stage_config"]["available"], True)

    def test_project_software_install_uses_only_configured_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            controller = CampaignController(
                "adb",
                root / "runs",
                write_config(root, minimal_config()),
            )
            runner = Mock()
            runner._install.return_value = {
                "package": "com.example.app",
                "succeeded": True,
                "already_installed": False,
            }
            with patch(
                "mobile_profiler.campaign_controller.AndroidCampaignRunner",
                return_value=runner,
            ) as runner_class:
                result = controller.install_project_software(
                    "serial-1",
                    "com.example.app",
                )

            install_set = runner._install.call_args.args[0]
            self.assertEqual(install_set.package, "com.example.app")
            self.assertEqual(install_set.source, (root / "app.apk").resolve())
            self.assertTrue(result["succeeded"])
            self.assertEqual(result["install_mode"], "project")
            runner_class.assert_called_once()
            controller.close()

    def test_software_catalog_merges_three_stage_preparation_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.apk").write_bytes(b"apk")
            data = minimal_config()
            data["preparation"]["apps"][0].update(
                {
                    "catalog_status": "pending_validation",
                    "software_type": "game",
                    "install_mode": "project",
                    "install_channel": "project_apk",
                    "install_source": "Project APK",
                    "supported_engines": ["vision", "hybrid"],
                }
            )
            config_path = write_config(root, data)
            controller = CampaignController("adb", root / "runs", config_path)
            state_dir = (
                root
                / "runs"
                / "campaigns"
                / "test-campaign-prepare-20260721-120000"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "stage": "preparation",
                        "status": "completed",
                        "device": "serial-1",
                        "started_at": 1.0,
                        "finished_at": 2.0,
                        "output_dir": str(state_dir),
                        "install_results": [
                            {"package": "com.example.app", "succeeded": True}
                        ],
                        "app_results": [
                            {
                                "name": "Test app",
                                "package": "com.example.app",
                                "status": "completed",
                                "succeeded": True,
                                "setup_status": "completed",
                                "setup_succeeded": True,
                                "normal_flow_supported": True,
                                "workflow_validations": [
                                    {
                                        "workflow_id": "browse",
                                        "name": "Browse",
                                        "status": "completed",
                                        "succeeded": True,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            catalog = controller.snapshot()["software_catalog"]

            self.assertEqual(catalog["total"], 1)
            self.assertEqual(catalog["verified_count"], 1)
            self.assertEqual(catalog["pending_count"], 0)
            item = catalog["items"][0]
            self.assertEqual(item["catalog_status"], "supported")
            self.assertEqual(item["configured_catalog_status"], "pending_validation")
            self.assertEqual(item["installation_status"], "installed")
            self.assertEqual(item["setup_status"], "verified")
            self.assertEqual(item["normal_flow_status"], "verified")
            self.assertEqual(item["validation_status"], "verified")
            self.assertEqual(item["install_actions"], ["project"])
            self.assertEqual(item["install_prompt"], "")
            self.assertIn("store_package", catalog)
            controller.close()

    def test_dashboard_controller_runs_each_stage_in_background(self) -> None:
        calls: list[tuple[str, dict]] = []

        class FakeRunner:
            def __init__(self, _adb, _config, output_root, **kwargs) -> None:
                calls.append(
                    (
                        "init",
                        {
                            "model": dict(kwargs.get("model_payload_overrides") or {}),
                            "repeat_workflows": kwargs.get("repeat_workflows"),
                            "task_overrides": dict(kwargs.get("task_overrides") or {}),
                            "task_order": list(kwargs.get("task_order") or []),
                        },
                    )
                )
                self.output_root = Path(output_root)

            def prepare(self) -> dict:
                return {
                    "status": "completed_with_warnings",
                    "output_dir": str(self.output_root / "prepare-run"),
                    "app_results": [],
                }

            def run_test(self, *, max_rounds=None) -> dict:
                calls.append(("run_test", {"max_rounds": max_rounds}))
                return {
                    "status": "device_shutdown_or_unavailable",
                    "message": "device remained unavailable",
                    "output_dir": str(self.output_root / "test-run"),
                    "round_count": 2,
                }

            def request_stop(self) -> None:
                calls.append(("stop", {}))

            def active_agent_snapshot(self) -> dict:
                return {}

            def latest_screenshot(self):
                return None

        with tempfile.TemporaryDirectory() as temporary, patch(
            "mobile_profiler.campaign_controller.AndroidCampaignRunner",
            FakeRunner,
        ):
            controller = CampaignController(
                "adb",
                Path(temporary),
                Path("examples/android-two-stage-campaign.json"),
            )
            state = controller.start(
                {
                    "stage": "prepare",
                    "device": "serial-1",
                    "model": "ui-model",
                    "api_key": "secret",
                    "system_prompt": "stale browser prompt",
                    "tasks": [
                        {
                            "id": "puzzle-home-ready",
                            "name": "stale",
                            "prompt": "stale browser task",
                        }
                    ],
                }
            )
            deadline = time.time() + 2
            while state["running"] and time.time() < deadline:
                time.sleep(0.01)
                state = controller.snapshot()
            self.assertEqual(state["status"], "completed_with_warnings")
            self.assertEqual(state["campaign_stage"], "prepare")
            self.assertGreaterEqual(len(state["tasks"]), 8)
            self.assertEqual(calls[0][1]["model"]["model"], "ui-model")
            self.assertEqual(calls[0][1]["model"]["api_key"], "secret")
            self.assertNotIn("system_prompt", calls[0][1]["model"])
            self.assertEqual(calls[0][1]["task_overrides"], {})
            self.assertEqual(calls[0][1]["task_order"], [])
            self.assertNotEqual(state["tasks"][0]["prompt"], "stale browser task")

            state = controller.start(
                {
                    "stage": "test",
                    "device": "serial-1",
                    "tasks": [
                        {
                            "id": "light-up-toggle",
                            "name": "stale browser task",
                            "prompt": "must be ignored without explicit override",
                        }
                    ],
                }
            )
            deadline = time.time() + 2
            while state["running"] and time.time() < deadline:
                time.sleep(0.01)
                state = controller.snapshot()
            default_test_init = [item for item in calls if item[0] == "init"][-1][1]
            self.assertTrue(default_test_init["repeat_workflows"])
            self.assertEqual(default_test_init["task_overrides"], {})
            self.assertEqual(default_test_init["task_order"], [])
            self.assertTrue(state["repeat_workflows"])
            self.assertEqual(state["max_rounds"], 1)
            self.assertEqual(calls[-1][1]["max_rounds"], 1)

            state = controller.start(
                {
                    "stage": "test",
                    "device": "serial-1",
                    "workflow_name": "UI custom workflow",
                    "repeat_workflows": False,
                    "max_rounds": 1,
                    "runtime_task_overrides": True,
                    "tasks": [
                        {
                            "id": "light-up-toggle",
                            "name": "UI task name",
                            "prompt": "UI custom prompt",
                            "attention_prompt": "UI custom attention",
                            "max_steps": 33,
                            "timeout_s": 444,
                            "on_failure": "continue",
                            "action_limits": [
                                {
                                    "actions": ["tap", "tap_element"],
                                    "maximum": 1,
                                    "maximum_per_signature": 1,
                                    "label": "UI unique tap",
                                }
                            ],
                        }
                    ],
                }
            )
            deadline = time.time() + 2
            while state["running"] and time.time() < deadline:
                time.sleep(0.01)
                state = controller.snapshot()
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["round_count"], 2)
            self.assertIn("关机", state["message"])
            self.assertFalse(state["loop_enabled"])
            self.assertEqual(state["workflow_name"], "UI custom workflow")
            test_init = [item for item in calls if item[0] == "init"][-1][1]
            self.assertFalse(test_init["repeat_workflows"])
            override = test_init["task_overrides"]["light-up-toggle"]
            self.assertEqual(override.prompt, "UI custom prompt")
            self.assertEqual(override.max_steps, 33)
            self.assertEqual(override.timeout_s, 444)
            self.assertEqual(override.action_limits[0].actions, ("tap", "tap_element"))
            self.assertEqual(override.action_limits[0].maximum, 1)
            self.assertEqual(override.action_limits[0].maximum_per_signature, 1)
            self.assertEqual(test_init["task_order"], ["light-up-toggle"])
            self.assertEqual(calls[-1][1]["max_rounds"], 1)
            controller.close()


if __name__ == "__main__":
    unittest.main()
