from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mobile_profiler.maaend_runtime as maaend_runtime

from mobile_profiler.maaend_runtime import (
    MAAEND_ADAPTER_ID,
    MAAEND_MANAGED_INSTANCE_ID,
    MAAEND_MANAGED_INSTANCE_NAME,
    MAAEND_REPOSITORY,
    MaaEndRuntimeController,
    configure_maaend_managed_profile,
    load_maaend_game_catalog,
    load_maaend_profile_config,
    validate_maaend_runtime,
)
from mobile_profiler.maaend_guard import (
    fingerprint_maaend_resources,
    load_maaend_guard_policy,
)


class ControlledProcess:
    def __init__(
        self,
        *,
        stop_on_signal: bool = True,
        stop_on_terminate: bool = True,
        immediate_timeout: bool = False,
    ) -> None:
        self.returncode: int | None = None
        self._event = threading.Event()
        self.stop_on_signal = stop_on_signal
        self.stop_on_terminate = stop_on_terminate
        self.immediate_timeout = immediate_timeout
        self.signals: list[object] = []
        self.terminate_count = 0
        self.kill_count = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if timeout is not None and self.immediate_timeout:
            raise subprocess.TimeoutExpired("MaaEnd.exe", timeout)
        if not self._event.wait(timeout if timeout is not None else 5):
            raise subprocess.TimeoutExpired("MaaEnd.exe", timeout)
        return self.returncode

    def send_signal(self, value) -> None:
        self.signals.append(value)
        if self.stop_on_signal:
            self.returncode = 0
            self._event.set()

    def terminate(self) -> None:
        self.terminate_count += 1
        if self.stop_on_terminate:
            self.returncode = 0
            self._event.set()

    def kill(self) -> None:
        self.kill_count += 1
        self.returncode = -9
        self._event.set()


class MaaEndInteractionStateTests(unittest.TestCase):
    def test_auto_wakes_and_dismisses_insecure_keyguard_without_wm_size(self) -> None:
        states = iter(
            [
                {
                    "wakefulness": "Asleep",
                    "awake": False,
                    "keyguard_showing": True,
                    "keyguard_secure": False,
                },
                {
                    "wakefulness": "Awake",
                    "awake": True,
                    "keyguard_showing": True,
                    "keyguard_secure": False,
                },
                {
                    "wakefulness": "Awake",
                    "awake": True,
                    "keyguard_showing": False,
                    "keyguard_secure": False,
                },
            ]
        )
        commands: list[tuple[str, ...]] = []

        def command(_adb: str, _device: str, *arguments: str) -> str:
            commands.append(arguments)
            return ""

        with patch.object(
            maaend_runtime,
            "_read_maaend_interaction_state",
            side_effect=lambda *_args: next(states),
        ), patch.object(
            maaend_runtime,
            "_adb_text_command",
            side_effect=command,
        ), patch.object(maaend_runtime.time, "sleep"):
            result = maaend_runtime._ensure_maaend_device_interactive(
                "adb",
                "SERIAL-001",
            )

        self.assertTrue(result["succeeded"])
        self.assertEqual(result["actions"], ["wake", "dismiss_insecure_keyguard"])
        self.assertIn(("shell", "input", "keyevent", "224"), commands)
        self.assertIn(("shell", "wm", "dismiss-keyguard"), commands)
        self.assertFalse(any(command[:3] == ("shell", "wm", "size") for command in commands))

    def test_secure_keyguard_is_never_dismissed(self) -> None:
        state = {
            "wakefulness": "Awake",
            "awake": True,
            "keyguard_showing": True,
            "keyguard_secure": True,
        }
        with patch.object(
            maaend_runtime,
            "_read_maaend_interaction_state",
            return_value=state,
        ), patch.object(
            maaend_runtime,
            "_adb_text_command",
        ) as command:
            with self.assertRaises(maaend_runtime._DeviceStateError) as blocked:
                maaend_runtime._ensure_maaend_device_interactive(
                    "adb",
                    "SERIAL-001",
                )

        self.assertEqual(blocked.exception.screen_state, "secure_keyguard")
        command.assert_not_called()

    def test_insecure_keyguard_swipe_uses_initial_display_size(self) -> None:
        states = iter(
            [
                {
                    "wakefulness": "Awake",
                    "awake": True,
                    "keyguard_showing": True,
                    "keyguard_secure": False,
                },
                {
                    "wakefulness": "Awake",
                    "awake": True,
                    "keyguard_showing": True,
                    "keyguard_secure": False,
                },
                {
                    "wakefulness": "Awake",
                    "awake": True,
                    "keyguard_showing": True,
                    "keyguard_secure": False,
                },
                {
                    "wakefulness": "Awake",
                    "awake": True,
                    "keyguard_showing": False,
                    "keyguard_secure": False,
                },
            ]
        )
        commands: list[tuple[str, ...]] = []

        def command(_adb: str, _device: str, *arguments: str) -> str:
            commands.append(arguments)
            if arguments == ("shell", "dumpsys", "window", "displays"):
                return "init=1260x2800 560dpi cur=1260x2800"
            return ""

        with patch.object(
            maaend_runtime,
            "_read_maaend_interaction_state",
            side_effect=lambda *_args: next(states),
        ), patch.object(
            maaend_runtime,
            "_adb_text_command",
            side_effect=command,
        ), patch.object(maaend_runtime.time, "sleep"):
            result = maaend_runtime._ensure_maaend_device_interactive(
                "adb",
                "SERIAL-001",
            )

        swipe = next(command for command in commands if command[:3] == ("shell", "input", "swipe"))
        self.assertEqual(swipe[3:], ("630", "2408", "630", "672", "450"))
        self.assertEqual(
            result["actions"],
            ["dismiss_insecure_keyguard", "menu_key", "swipe_up"],
        )


class ImmediateProcess:
    def __init__(self, exit_code: int) -> None:
        self.returncode: int | None = None
        self.exit_code = exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.exit_code
        return self.exit_code


class FakeMxuApi:
    def __init__(
        self,
        runtime: Path,
        *,
        terminal: str = "running",
        accepted_task_count: int | None = None,
    ) -> None:
        self.runtime = runtime.resolve()
        self.terminal = terminal
        self.accepted_task_count = accepted_task_count
        self.calls: list[dict[str, object]] = []
        self.connected = False
        self.resource_loaded = False
        self.started_tasks: list[dict[str, object]] = []

    def __call__(
        self,
        base_url: str,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout: float = 15.0,
    ) -> object:
        self.calls.append(
            {
                "base_url": base_url,
                "method": method,
                "path": path,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if path == "/interface":
            return {
                "interface": {"name": "MaaEnd", "version": "v2.20.0"},
                "basePath": str(self.runtime),
            }
        if path == "/maa/initialized":
            return {"initialized": True, "version": "v5.10.5"}
        if path == "/maa/devices":
            return [
                {
                    "name": "SERIAL-001-C:\\platform-tools\\adb.exe",
                    "adb_path": "C:/platform-tools/adb.exe",
                    "address": "SERIAL-001",
                    "screencap_methods": "64",
                    "input_methods": "8",
                    "config": '{"extras":{"androws":{"enable":true}}}',
                },
                {
                    "name": "192.0.2.1:5555-C:/platform-tools/adb.exe",
                    "adb_path": "C:/platform-tools/adb.exe",
                    "address": "192.0.2.1:5555",
                    "screencap_methods": "11",
                    "input_methods": "22",
                    "config": "{}",
                },
            ]
        if path.endswith("/connect"):
            self.connected = True
            return {"connId": 100000001}
        if path.endswith("/resource/load"):
            self.resource_loaded = True
            return {"resIds": [100000001, 100000002]}
        if path.endswith("/tasks/start"):
            assert isinstance(payload, dict)
            tasks = payload.get("tasks")
            self.started_tasks = list(tasks) if isinstance(tasks, list) else []
            count = (
                self.accepted_task_count
                if self.accepted_task_count is not None
                else len(self.started_tasks)
            )
            return {"taskIds": [200000001 + index for index in range(count)]}
        if path == "/maa/state":
            statuses: dict[str, str] = {}
            mappings: dict[str, str] = {}
            for index, task in enumerate(self.started_tasks):
                selected_id = str(task.get("selected_task_id") or "")
                status = {
                    "running": "running",
                    "succeeded": "succeeded",
                    "failed": "failed",
                }[self.terminal]
                statuses[selected_id] = status
                mappings[str(200000001 + index)] = selected_id
            overall = {
                "running": "Running",
                "succeeded": "Succeeded",
                "failed": "Failed",
            }[self.terminal] if self.started_tasks else None
            return {
                "instances": {
                    "daily-id": {
                        "connected": self.connected,
                        "resource_loaded": self.resource_loaded,
                        "tasker_inited": bool(self.started_tasks),
                        "is_running": self.terminal == "running" and bool(self.started_tasks),
                        "task_run_state": {
                            "statuses": statuses,
                            "mappings": mappings,
                            "pending_task_ids": [
                                200000001 + index
                                for index in range(len(self.started_tasks))
                            ],
                            "current_task_index": 0,
                            "overall_status": overall,
                        },
                    }
                }
            }
        if path.endswith("/tasks/stop") or path.endswith("/agent/stop"):
            return {"ok": True}
        if method == "PUT" and path.endswith("/daily-id"):
            return {"ok": True}
        raise AssertionError(f"unexpected MXU API call: {method} {path}")


class MaaEndRuntimeTests(unittest.TestCase):
    @staticmethod
    def _fake_mxu_screenshot(*args, **kwargs) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + (b"\x00" * 8) + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big")

    @staticmethod
    def _task_success_popen(
        process: ControlledProcess,
        entry: str,
        terminal_node: str | None = None,
    ):
        def popen(command, **kwargs):
            details = {
                "task_id": 200000001,
                "entry": entry,
                "uuid": "test-task",
                "hash": "test-hash",
            }
            lines: list[str] = []
            if terminal_node:
                node_details = {
                    "task_id": 200000001,
                    "node_id": 200000099,
                    "name": terminal_node,
                }
                lines.append(
                    "[TRC] [message=Node.PipelineNode.Succeeded] "
                    f"[details_json={json.dumps(node_details, separators=(',', ':'))}] "
                    "[trans_arg=true]"
                )
            lines.append(
                "[TRC] [message=Tasker.Task.Succeeded] "
                f"[details_json={json.dumps(details, separators=(',', ':'))}] "
                "[trans_arg=true]"
            )
            kwargs["stdout"].write(("\n".join(lines) + "\n").encode("utf-8"))
            kwargs["stdout"].flush()
            return process

        return popen

    @staticmethod
    def _test_guard_policy(runtime: Path) -> dict[str, object]:
        policy = load_maaend_guard_policy()
        fingerprint = fingerprint_maaend_resources(runtime, policy)
        resource = policy["resource_fingerprint"]
        assert isinstance(resource, dict)
        resource["expected_sha256"] = fingerprint["sha256"]
        watchdogs = policy["watchdogs"]
        assert isinstance(watchdogs, dict)
        watchdogs.update(
            {
                "poll_interval_seconds": 0.01,
                "screenshot_interval_seconds": 0,
                "no_progress_seconds": 30,
                "tasker_idle_grace_seconds": 0.05,
            }
        )
        return policy

    def _make_runtime(self, root: Path) -> tuple[Path, Path]:
        runtime = root / "MaaEnd"
        for directory in (
            "resource",
            "resource_adb",
            "maafw/MaaAgentBinary",
            "agent",
            "config",
            "tasks",
            "locales/interface",
        ):
            (runtime / directory).mkdir(parents=True, exist_ok=True)
        for relative in (
            "MaaEnd.exe",
            "maafw/MaaFramework.dll",
            "maafw/MaaAdbControlUnit.dll",
            "maafw/MaaUtils.dll",
            "maafw/MaaToolkit.dll",
            "maafw/MaaAgentClient.dll",
            "maafw/MaaAgentServer.dll",
            "agent/go-service.exe",
            "agent/cpp-algo.exe",
        ):
            (runtime / relative).write_bytes(b"test")
        (runtime / "LICENSE").write_text(
            "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n",
            encoding="utf-8",
        )
        interface = {
            "interface_version": 2,
            "name": "MaaEnd",
            "version": "v2.20.0",
            "github": MAAEND_REPOSITORY,
            "languages": {"zh_cn": "locales/interface/zh_cn.json"},
            "controller": [
                {
                    "name": "ADB",
                    "type": "Adb",
                    "attach_resource_path": ["./resource_adb"],
                }
            ],
            "resource": [{"name": "官服", "path": ["./resource"]}],
            "agent": [
                {"child_exec": "agent/go-service", "child_args": []},
                {"child_exec": "agent/cpp-algo", "child_args": []},
            ],
            "group": [
                {
                    "name": "other_menu",
                    "label": "$group.other_menu.label",
                    "default_expand": True,
                },
                {
                    "name": "sanity_sink",
                    "label": "$group.sanity_sink.label",
                    "default_expand": True,
                },
            ],
            "task": [],
            "import": ["tasks/runtime-tasks.json"],
        }
        imported_tasks = {
            "task": [
                {
                    "name": "DailyRewards",
                    "label": "$task.DailyRewards.label",
                    "entry": "DailyRewardStart",
                    "description": "$task.DailyRewards.description",
                    "controller": ["ADB", "Win32-Front"],
                    "group": ["other_menu"],
                    "option": [
                        "RewardMode",
                        "RewardFlags",
                        "RewardLimit",
                        "RewardHotkey",
                        "DesktopOnlyOption",
                    ],
                },
                {
                    "name": "AutoEssence",
                    "label": "$task.AutoEssence.label",
                    "entry": "AutoEssenceMain",
                    "description": "$task.AutoEssence.description",
                    "controller": ["ADB", "Win32-Front"],
                    "group": ["sanity_sink"],
                },
                {
                    "name": "DesktopOnly",
                    "label": "$task.DesktopOnly.label",
                    "entry": "DesktopOnlyStart",
                    "controller": ["Win32-Front"],
                    "group": ["other_menu"],
                },
            ],
            "preset": [
                {
                    "name": "QuickDaily",
                    "label": "$preset.QuickDaily.label",
                    "description": "$preset.QuickDaily.description",
                    "task": [
                        {
                            "name": "DailyRewards",
                            "option": {
                                "RewardMode": "Fast",
                                "RewardFlags": ["Mail"],
                                "RewardLimit": {"Limit": "8"},
                                "RewardHotkey": {"Key": "Ctrl+R"},
                            },
                        },
                        {"name": "AutoEssence"},
                    ],
                },
                {
                    "name": "MixedDesktop",
                    "label": "混合预设",
                    "task": [
                        {"name": "DailyRewards"},
                        {"name": "DesktopOnly"},
                    ],
                },
            ],
            "option": {
                "RewardMode": {
                    "type": "select",
                    "label": "$option.RewardMode.label",
                    "cases": [
                        {"name": "Safe", "label": "安全"},
                        {
                            "name": "Fast",
                            "label": "快速",
                            "option": ["NestedSwitch"],
                            "pipeline_override": {"RewardMode": {"mode": "fast"}},
                        },
                    ],
                    "default_case": "Safe",
                },
                "NestedSwitch": {
                    "type": "switch",
                    "label": "嵌套开关",
                    "cases": [
                        {
                            "name": "Yes",
                            "pipeline_override": {"NestedSwitch": {"enabled": True}},
                        },
                        {"name": "No"},
                    ],
                    "default_case": "No",
                },
                "RewardFlags": {
                    "type": "checkbox",
                    "label": "奖励类别",
                    "cases": [
                        {
                            "name": "Mail",
                            "label": "邮件",
                            "pipeline_override": {"RewardFlags": {"mail": True}},
                        },
                        {"name": "Task"},
                    ],
                    "default_case": ["Task"],
                },
                "RewardLimit": {
                    "type": "input",
                    "label": "领取上限",
                    "inputs": [
                        {
                            "name": "Limit",
                            "label": "数量",
                            "default": "3",
                            "pipeline_type": "int",
                        }
                    ],
                    "pipeline_override": {"RewardLimit": {"limit": "{Limit}"}},
                },
                "RewardHotkey": {
                    "type": "hotkey",
                    "label": "快捷键",
                    "hotkeys": [{"name": "Key", "default": "R"}],
                    "pipeline_override": {"RewardHotkey": {"key": "{Key.primary}"}},
                },
                "DesktopOnlyOption": {
                    "type": "switch",
                    "label": "桌面选项",
                    "controller": ["Win32-Front"],
                    "cases": [{"name": "Yes"}, {"name": "No"}],
                },
            },
        }
        (runtime / "interface.json").write_text(
            json.dumps(interface, ensure_ascii=False),
            encoding="utf-8",
        )
        # Official releases retain interface imports. Exercise JSONC comments and
        # a trailing comma as used by MaaEnd's imported task definitions.
        imported_text = json.dumps(imported_tasks, ensure_ascii=False, indent=2)
        imported_text = imported_text.replace(
            '"task": [',
            '// imported by the release interface\n  "task": [',
            1,
        ).replace("\n  ]\n}", "\n  ],\n}")
        (runtime / "tasks" / "runtime-tasks.json").write_text(
            imported_text,
            encoding="utf-8",
        )
        (runtime / "locales" / "interface" / "zh_cn.json").write_text(
            json.dumps(
                {
                    "group.other_menu.label": "其他菜单",
                    "group.sanity_sink.label": "理智消耗",
                    "task.DailyRewards.label": "📅日常奖励领取",
                    "task.DailyRewards.description": "领取每日奖励",
                    "task.AutoEssence.label": "🎱基质刷取",
                    "task.AutoEssence.description": "自动挑战重度淤积点",
                    "task.DesktopOnly.label": "桌面任务",
                    "preset.QuickDaily.label": "快速日常",
                    "preset.QuickDaily.description": "核心日常任务",
                    "option.RewardMode.label": "奖励领取模式",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config = {
            "version": "1.0",
            "instances": [
                {
                    "id": "daily-id",
                    "name": "日常任务",
                    "controllerName": "ADB",
                    "resourceName": "官服",
                    "savedDevice": {
                        "adbDeviceName": "SERIAL-001-C:\\platform-tools\\adb.exe"
                    },
                    "tasks": [
                        {
                            "id": "task-1",
                            "taskName": "DailyRewards",
                            "enabled": True,
                            "optionValues": {},
                        }
                    ],
                }
            ],
        }
        config_path = runtime / "config" / "mxu-MaaEnd.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        return runtime, config_path

    @staticmethod
    def _read_config(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_config(path: Path, config: dict[str, object]) -> None:
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def test_validate_complete_official_release_and_load_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = self._make_runtime(Path(directory))
            metadata = validate_maaend_runtime(runtime)
            config = load_maaend_profile_config(runtime)

        self.assertEqual(metadata["repository"], MAAEND_REPOSITORY)
        self.assertEqual(metadata["license"], "AGPL-3.0")
        self.assertEqual(metadata["version"], "v2.20.0")
        self.assertEqual(metadata["task_count"], 3)
        self.assertEqual(metadata["adb_task_count"], 2)
        self.assertEqual(config["instances"][0]["name"], "日常任务")

    def test_game_catalog_uses_installed_localization_and_adb_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = self._make_runtime(Path(directory))
            catalog = load_maaend_game_catalog(runtime)

        tasks = {task["name"]: task for task in catalog["tasks"]}
        presets = {preset["name"]: preset for preset in catalog["presets"]}
        self.assertEqual(catalog["task_count"], 3)
        self.assertEqual(catalog["adb_task_count"], 2)
        self.assertEqual(tasks["DailyRewards"]["label"], "📅日常奖励领取")
        self.assertTrue(tasks["AutoEssence"]["adb_supported"])
        self.assertFalse(tasks["DesktopOnly"]["adb_supported"])
        self.assertEqual(tasks["DailyRewards"]["option_count"], 4)
        self.assertEqual(catalog["options"]["RewardMode"]["type"], "select")
        self.assertEqual(catalog["options"]["RewardFlags"]["type"], "checkbox")
        self.assertEqual(catalog["options"]["RewardLimit"]["type"], "input")
        self.assertEqual(catalog["options"]["RewardHotkey"]["type"], "hotkey")
        self.assertFalse(catalog["options"]["DesktopOnlyOption"]["adb_applicable"])
        quick_task = presets["QuickDaily"]["adb_task_configurations"][0]
        self.assertEqual(quick_task["option_values"]["RewardMode"]["caseName"], "Fast")
        self.assertEqual(quick_task["option_values"]["RewardLimit"]["values"]["Limit"], "8")
        self.assertTrue(presets["QuickDaily"]["adb_compatible"])
        self.assertEqual(
            presets["MixedDesktop"]["unsupported_task_names"],
            ["DesktopOnly"],
        )

    def test_configure_managed_profile_preserves_other_instances_and_native_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, config_path = self._make_runtime(Path(directory))
            original = self._read_config(config_path)["instances"][0]
            result = configure_maaend_managed_profile(
                runtime,
                device="SERIAL-001",
                adb="adb",
                tasks=[
                    {
                        "name": "DailyRewards",
                        "enabled": True,
                        "option_values": {
                            "RewardMode": {"type": "select", "caseName": "Fast"},
                            "RewardFlags": {"type": "checkbox", "caseNames": ["Mail"]},
                            "RewardLimit": {"type": "input", "values": {"Limit": "12"}},
                            "RewardHotkey": {"type": "hotkey", "values": {"Key": "Ctrl+R"}},
                            "NestedSwitch": {"type": "switch", "value": True},
                        },
                    },
                    {
                        "name": "AutoEssence",
                        "enabled": False,
                        "option_values": {},
                    },
                ],
            )
            config = self._read_config(config_path)

        self.assertEqual(config["instances"][0], original)
        managed = next(
            item for item in config["instances"] if item["id"] == MAAEND_MANAGED_INSTANCE_ID
        )
        self.assertEqual(managed["name"], MAAEND_MANAGED_INSTANCE_NAME)
        self.assertEqual(managed["controllerName"], "ADB")
        self.assertTrue(managed["savedDevice"]["adbDeviceName"].startswith("SERIAL-001-"))
        self.assertEqual([task["taskName"] for task in managed["tasks"]], ["DailyRewards", "AutoEssence"])
        self.assertEqual(
            managed["tasks"][0]["optionValues"]["NestedSwitch"],
            {"type": "switch", "value": True},
        )
        self.assertEqual(result["task_names"], ["DailyRewards"])
        self.assertTrue(result["managed"])

    def test_configure_managed_profile_can_create_probe_only_empty_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, config_path = self._make_runtime(Path(directory))
            result = configure_maaend_managed_profile(
                runtime,
                device="SERIAL-001",
                adb="adb",
                tasks=[],
                allow_empty_tasks=True,
            )
            config = self._read_config(config_path)

        managed = next(
            item
            for item in config["instances"]
            if item["id"] == MAAEND_MANAGED_INSTANCE_ID
        )
        self.assertEqual(managed["tasks"], [])
        self.assertEqual(result["task_names"], [])
        self.assertEqual(result["task_count"], 0)

    def test_configure_managed_profile_still_rejects_empty_regular_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = self._make_runtime(Path(directory))
            with self.assertRaisesRegex(ValueError, "至少配置"):
                configure_maaend_managed_profile(
                    runtime,
                    device="SERIAL-001",
                    adb="adb",
                    tasks=[],
                )

    def test_validate_rejects_incomplete_or_non_agpl_release(self) -> None:
        for relative in (
            "MaaEnd.exe",
            "interface.json",
            "maafw/MaaAdbControlUnit.dll",
            "agent/go-service.exe",
            "agent/cpp-algo.exe",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                runtime, _ = self._make_runtime(Path(directory))
                (runtime / relative).unlink()
                with self.assertRaisesRegex(RuntimeError, "缺少文件"):
                    validate_maaend_runtime(runtime)

        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = self._make_runtime(Path(directory))
            (runtime / "LICENSE").write_text("MIT", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "AGPL-3.0"):
                validate_maaend_runtime(runtime)

        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = self._make_runtime(Path(directory))
            interface_path = runtime / "interface.json"
            interface = json.loads(interface_path.read_text(encoding="utf-8"))
            interface["version"] = "v2.13.0"
            interface_path.write_text(
                json.dumps(interface, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "v2.20.0"):
                validate_maaend_runtime(runtime)

        with tempfile.TemporaryDirectory() as directory:
            runtime, _ = self._make_runtime(Path(directory))
            interface_path = runtime / "interface.json"
            interface = json.loads(interface_path.read_text(encoding="utf-8"))
            interface["mirrorchyan_rid"] = "MaaEnd"
            interface_path.write_text(
                json.dumps(interface, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "自动更新"):
                validate_maaend_runtime(runtime)

    def test_api_host_patch_is_exact_and_keeps_official_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "MaaEnd.exe"
            original = bytearray(b"official-mxu-v2.20-fixture")
            offset = 8
            elevation_branch = bytes(original[offset : offset + 6])
            run_branch = b"api-ok"
            patched = bytearray(original)
            patched[offset : offset + 6] = run_branch
            source.write_bytes(original)

            with patch.multiple(
                maaend_runtime,
                _MXU_V220_EXECUTABLE_SHA256=hashlib.sha256(original).hexdigest(),
                _MXU_V220_API_HOST_SHA256=hashlib.sha256(patched).hexdigest(),
                _MXU_V220_ELEVATION_BRANCH_OFFSET=offset,
                _MXU_V220_ELEVATION_BRANCH=elevation_branch,
                _MXU_V220_RUN_BRANCH=run_branch,
            ):
                host = maaend_runtime._prepare_mxu_v220_api_host(root)
                self.assertEqual(host.read_bytes(), patched)
                self.assertEqual(source.read_bytes(), original)

                host.write_bytes(b"corrupt")
                self.assertEqual(
                    maaend_runtime._prepare_mxu_v220_api_host(root).read_bytes(),
                    patched,
                )

                source.write_bytes(b"unknown-build")
                with self.assertRaisesRegex(RuntimeError, "指纹"):
                    maaend_runtime._prepare_mxu_v220_api_host(root)

    def test_visit_friends_adb_override_expands_v220_clipped_rois(self) -> None:
        encoded = maaend_runtime._task_pipeline_override(
            {"option": {}},
            {"name": "VisitFriends"},
            {},
            {},
            controller_name="ADB",
            resource_name="官服",
        )
        overrides = json.loads(encoded)
        compatibility = overrides[-1]
        self.assertEqual(
            compatibility["__ScenePrivateMenuFriendsEnterMenuFriendsList"]["roi"],
            [0, 130, 160, 215],
        )
        self.assertEqual(
            compatibility["InFriendsList"]["roi"],
            [75, 60, 250, 100],
        )
        self.assertEqual(
            compatibility["VisitFriendsRecognitionItemEnterButton"]["threshold"],
            0.75,
        )
        self.assertEqual(
            compatibility["__ScenePrivateMenuFriendsEnterMenuFriendsListSuccess"]
            ["recognition"]["param"]["roi"],
            [0, 0, 350, 70],
        )
        self.assertEqual(
            compatibility["VisitFriendsMenuTerminalExitToWorldShip"]["target"],
            [1135, 5, 65, 65],
        )
        self.assertEqual(
            compatibility["VisitFriendsWorldShipExitToMenuFriends"]["target"],
            [70, 10, 150, 60],
        )

    def test_preflight_accepts_adb_profile_and_persists_adapter_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            controller = MaaEndRuntimeController(
                output,
                guard_policy=self._test_guard_policy(runtime),
            )
            snapshot = controller.preflight(
                {
                    "device": "SERIAL-001",
                    "runtime_path": str(runtime),
                    "instance_name": "日常任务",
                    "authorized_tasks": ["DailyRewards"],
                }
            )
            persisted = json.loads(
                (
                    output
                    / "open-source-automation"
                    / "maaend"
                    / "config.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(snapshot["adapter_id"], MAAEND_ADAPTER_ID)
        self.assertEqual(snapshot["status"], "ready")
        self.assertTrue(snapshot["preflight"]["screen"]["game_ready"])
        self.assertEqual(
            snapshot["preflight"]["profile"]["task_names"],
            ["DailyRewards"],
        )
        self.assertEqual(snapshot["game_catalog"]["adb_task_count"], 2)
        self.assertEqual(snapshot["configured_profile"]["name"], "日常任务")
        self.assertEqual(snapshot["profiles"][0]["task_names"], ["DailyRewards"])
        instance_option = next(
            option
            for option in snapshot["runtime_options"]
            if option["id"] == "instance_name"
        )
        self.assertEqual(instance_option["type"], "select")
        self.assertEqual(persisted["runtime_path"], str(runtime.resolve()))
        self.assertEqual(persisted["instance_name"], "日常任务")
        self.assertEqual(
            set(persisted),
            {"schema_version", "runtime_path", "instance_name"},
        )

    def test_preflight_guard_denies_unapproved_account_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                guard_policy=self._test_guard_policy(runtime),
            )
            with self.assertRaisesRegex(RuntimeError, "风险策略拒绝任务"):
                controller.preflight({"device": "SERIAL-001"})
            snapshot = controller.snapshot()

        self.assertEqual(snapshot["preflight"]["screen"]["screen_state"], "guard_denied")
        self.assertFalse(snapshot["preflight"]["guard"]["allowed"])
        denied = snapshot["preflight"]["guard"]["tasks"][0]
        self.assertEqual(denied["name"], "DailyRewards")
        self.assertEqual(denied["reason"], "explicit_authorization_required")

    def test_start_rejects_resource_drift_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            policy = self._test_guard_policy(runtime)
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                guard_policy=policy,
                popen_factory=lambda *args, **kwargs: self.fail(
                    "resource drift must be rejected before process launch"
                ),
            )
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards"],
            }
            controller.preflight(request)
            (runtime / "resource_adb" / "drift.json").write_text(
                '{"unsafe":true}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "资源指纹"):
                controller.start(request)

        self.assertEqual(controller.snapshot()["status"], "error")

    def test_preflight_rejects_non_adb_empty_mismatched_and_unsafe_profiles(self) -> None:
        cases = (
            (
                "non-adb",
                lambda instance: instance.update({"controllerName": "Win32-Front"}),
                "没有使用 ADB",
            ),
            (
                "empty",
                lambda instance: instance.update({"tasks": []}),
                "没有启用的 ADB 任务",
            ),
            (
                "desktop-task",
                lambda instance: instance.update(
                    {
                        "tasks": [
                            {
                                "id": "desktop",
                                "taskName": "DesktopOnly",
                                "enabled": True,
                                "optionValues": {},
                            }
                        ]
                    }
                ),
                "未声明支持 ADB",
            ),
            (
                "device-mismatch",
                lambda instance: instance.update(
                    {"savedDevice": {"adbDeviceName": "OTHER-adb.exe"}}
                ),
                "设备与当前真机",
            ),
            (
                "pre-action",
                lambda instance: instance.update(
                    {
                        "preActions": [
                            {
                                "id": "unsafe",
                                "enabled": True,
                                "program": "cmd.exe",
                            }
                        ]
                    }
                ),
                "前置程序",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                runtime, config_path = self._make_runtime(output)
                config = self._read_config(config_path)
                mutate(config["instances"][0])
                self._write_config(config_path, config)
                controller = MaaEndRuntimeController(
                    output,
                    runtime_path=runtime,
                    instance_name="日常任务",
                    guard_policy=self._test_guard_policy(runtime),
                )
                with self.assertRaisesRegex(RuntimeError, message):
                    controller.preflight({"device": "SERIAL-001"})
                self.assertFalse(
                    controller.snapshot()["preflight"]["screen"]["game_ready"]
                )

    def test_start_requires_preflight_and_uses_mxu_http_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            calls: list[tuple[list[str], dict[str, object]]] = []
            process = ControlledProcess()
            api = FakeMxuApi(runtime)

            def popen(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                return process

            api_host = runtime / "MaaEnd.mobile-profiler-api-v2.20.exe"

            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=popen,
                http_json=api,
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda _root: api_host,
                guard_policy=self._test_guard_policy(runtime),
            )
            with self.assertRaisesRegex(RuntimeError, "预检"):
                controller.start({"device": "SERIAL-001"})
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards"],
            }
            controller.preflight(request)
            snapshot = controller.start(request)

            self.assertTrue(snapshot["running"])
            self.assertEqual(
                calls[0][0],
                [str(api_host)],
            )
            self.assertEqual(calls[0][1]["cwd"], str(runtime))
            self.assertEqual(
                calls[0][1]["env"]["MAA_SCREENSHOT_VIEWPORT"],
                "1280x720:adaptive",
            )
            self.assertNotIn("shell", calls[0][1])
            self.assertEqual(snapshot["mxu_api"]["phase"], "running")
            self.assertEqual(snapshot["mxu_api"]["truth_source"], "GET /api/maa/state")
            self.assertEqual(
                snapshot["mxu_api"]["resource_paths"],
                [
                    str(runtime / "resource").replace("\\", "/"),
                    str(runtime / "resource_adb").replace("\\", "/"),
                ],
            )
            connect_call = next(call for call in api.calls if call["path"].endswith("/connect"))
            self.assertEqual(connect_call["payload"]["address"], "SERIAL-001")
            self.assertEqual(connect_call["payload"]["screencap_methods"], "7")
            self.assertEqual(connect_call["payload"]["input_methods"], "7")
            start_call = next(call for call in api.calls if call["path"].endswith("/tasks/start"))
            start_payload = start_call["payload"]
            self.assertEqual(len(start_payload["agent_configs"]), 2)
            self.assertEqual(start_payload["pi_envs"]["PI_VERSION"], "v2.20.0")
            submitted = start_payload["tasks"]
            self.assertEqual(submitted[0]["entry"], "DailyRewardStart")
            overrides = json.loads(submitted[0]["pipeline_override"])
            self.assertIn({"RewardLimit": {"limit": 3}}, overrides)
            self.assertIn({"RewardHotkey": {"key": 46}}, overrides)
            controller.stop()

    def test_runtime_integrity_watchdog_stops_on_binary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            policy = self._test_guard_policy(runtime)
            watchdogs = policy["watchdogs"]
            assert isinstance(watchdogs, dict)
            watchdogs["runtime_integrity_interval_seconds"] = 0.01
            process = ControlledProcess()
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=lambda *args, **kwargs: process,
                http_json=FakeMxuApi(runtime, terminal="running"),
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=policy,
            )
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards"],
            }
            controller.preflight(request)
            started = controller.start(request)
            (runtime / "maafw" / "MaaFramework.dll").write_bytes(
                b"binary-drift"
            )

            deadline = time.time() + 2
            snapshot = started
            while snapshot["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
                snapshot = controller.snapshot()
            watchdog_evidence_exists = (
                Path(snapshot["last_run_dir"])
                / "runtime-integrity-watchdog.json"
            ).is_file()

        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["runtime_integrity"]["status"], "drifted")
        self.assertFalse(snapshot["runtime_integrity"]["verified"])
        self.assertTrue(
            any(
                row.get("kind") == "maaend_runtime_drift"
                for row in snapshot["incidents"]
            )
        )
        self.assertGreaterEqual(process.terminate_count, 1)
        self.assertFalse(snapshot["guarded_flow_verified"])
        self.assertFalse(snapshot["end_to_end_verified"])
        self.assertTrue(watchdog_evidence_exists)

    def test_terminal_full_rehash_detects_metadata_preserving_resource_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            guarded_file = runtime / "resource_adb" / "metadata.bin"
            guarded_file.write_bytes(b"before")
            original = guarded_file.stat()
            policy = self._test_guard_policy(runtime)
            watchdogs = policy["watchdogs"]
            assert isinstance(watchdogs, dict)
            watchdogs["runtime_integrity_interval_seconds"] = 60
            process = ControlledProcess()
            api = FakeMxuApi(runtime, terminal="running")
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=lambda *args, **kwargs: process,
                http_json=api,
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=policy,
            )
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards"],
            }
            controller.preflight(request)
            controller.start(request)
            guarded_file.write_bytes(b"after!")
            os.utime(
                guarded_file,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            api.terminal = "succeeded"

            deadline = time.time() + 2
            snapshot = controller.snapshot()
            while snapshot["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
                snapshot = controller.snapshot()

        integrity = snapshot["runtime_integrity"]
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(integrity["stage"], "terminal")
        self.assertTrue(integrity["full_resource_rehash"])
        self.assertEqual(integrity["status"], "drifted")
        self.assertTrue(
            any(row.get("path") == "<resource_tree>" for row in integrity["drift"])
        )
        self.assertFalse(snapshot["pipeline_verified"])
        self.assertFalse(snapshot["end_to_end_verified"])

    def test_internal_scene_probe_requires_empty_profile_and_submits_fixed_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, config_path = self._make_runtime(output)
            config = self._read_config(config_path)
            config["instances"][0]["tasks"] = []
            self._write_config(config_path, config)
            process = ControlledProcess()
            api = FakeMxuApi(runtime)
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=lambda *args, **kwargs: process,
                http_json=api,
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=self._test_guard_policy(runtime),
            )
            request = {"device": "SERIAL-001", "internal_probe": "SceneProbe"}
            preflight = controller.preflight(request)
            started = controller.start(request)
            start_call = next(
                call for call in api.calls if call["path"].endswith("/tasks/start")
            )
            submitted = start_call["payload"]["tasks"]
            override = json.loads(submitted[0]["pipeline_override"])
            controller.stop()

        self.assertTrue(preflight["preflight"]["profile"]["probe_only"])
        self.assertEqual(preflight["preflight"]["profile"]["task_count"], 0)
        self.assertTrue(started["running"])
        self.assertEqual(submitted[0]["entry"], "InWorld")
        self.assertEqual(submitted[0]["selected_task_id"], "internal-scene-probe")
        self.assertEqual(override, [{"InWorld": {"next": []}}])

    def test_internal_probe_rejects_enabled_profile_and_regular_run_rejects_empty_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, config_path = self._make_runtime(output)
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                guard_policy=self._test_guard_policy(runtime),
            )
            with self.assertRaisesRegex(RuntimeError, "专用 ADB Profile"):
                controller.preflight(
                    {"device": "SERIAL-001", "internal_probe": "SceneProbe"}
                )

            config = self._read_config(config_path)
            config["instances"][0]["tasks"] = []
            self._write_config(config_path, config)
            with self.assertRaisesRegex(RuntimeError, "没有启用的 ADB 任务"):
                controller.preflight({"device": "SERIAL-001"})

    def test_viewport_input_probe_runtime_verifies_exact_reversible_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, config_path = self._make_runtime(output)
            config = self._read_config(config_path)
            config["instances"][0]["tasks"] = []
            self._write_config(config_path, config)
            process = ControlledProcess()

            def popen(command, **kwargs):
                lines = [
                    (
                        "[TRC] [message=Node.PipelineNode.Succeeded] "
                        '[details_json={"name":"InWorld","reco_details":'
                        '{"name":"InWorld","algorithm":"Or","box":[0,0,1,1],'
                        '"viewport_alignment":"center","frame_id":10}}] '
                        "[trans_arg=true]"
                    ),
                    json.dumps(
                        {
                            "level": "info",
                            "component": "viewport_input_probe",
                            "phase": "joystick_center_released",
                            "alignment": "left",
                            "frame_id": 10,
                            "contact": 0,
                            "message": "ViewportInputProbe: joystick verified",
                        }
                    ),
                    json.dumps(
                        {
                            "level": "info",
                            "component": "viewport_input_probe",
                            "phase": "camera_reversed",
                            "alignment": "right",
                            "frame_id": 10,
                            "contact": 1,
                            "reversed": True,
                            "message": "ViewportInputProbe: done",
                        }
                    ),
                ]
                for action, contact, alignment, point in (
                    ("touch_down", 0, "left", [195, 551]),
                    ("touch_up", 0, "left", None),
                    ("touch_down", 1, "right", [640, 264]),
                    ("touch_move", 1, "right", [664, 264]),
                    ("touch_move", 1, "right", [640, 264]),
                    ("touch_up", 1, "right", None),
                ):
                    logical = {"contact": contact}
                    if point is not None:
                        logical["point"] = point
                    details = {
                        "action": action,
                        "param": {"contact": contact},
                        "viewport": {
                            "alignment": alignment,
                            "frame_id": 10,
                            "logical_param": logical,
                            "display_param": logical,
                        },
                    }
                    lines.append(
                        "[TRC] [message=Controller.Action.Succeeded] "
                        f"[details_json={json.dumps(details, separators=(',', ':'))}] "
                        "[trans_arg=true]"
                    )
                task_details = {
                    "task_id": 200000001,
                    "entry": "InWorld",
                    "uuid": "input-probe",
                    "hash": "test-hash",
                }
                lines.append(
                    "[TRC] [message=Tasker.Task.Succeeded] "
                    f"[details_json={json.dumps(task_details, separators=(',', ':'))}] "
                    "[trans_arg=true]"
                )
                kwargs["stdout"].write(("\n".join(lines) + "\n").encode("utf-8"))
                kwargs["stdout"].flush()
                return process

            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=popen,
                http_json=FakeMxuApi(runtime, terminal="succeeded"),
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=self._test_guard_policy(runtime),
            )
            request = {
                "device": "SERIAL-001",
                "internal_probe": "ViewportInputProbe",
                "authorized_probes": ["ViewportInputProbe"],
                "allow_viewport_input_probe": True,
            }
            controller.preflight(request)
            controller.start(request)
            deadline = time.time() + 2
            snapshot = controller.snapshot()
            while snapshot["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
                snapshot = controller.snapshot()

        self.assertEqual(
            snapshot["status"],
            "completed",
            json.dumps(snapshot.get("terminal_contract"), ensure_ascii=False),
        )
        self.assertTrue(snapshot["pipeline_verified"])
        self.assertTrue(snapshot["custom_agent_verified"])
        self.assertTrue(snapshot["input_verified"])
        self.assertTrue(snapshot["guarded_flow_verified"])
        self.assertFalse(snapshot["end_to_end_verified"])

    def test_start_revalidates_profile_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, config_path = self._make_runtime(output)
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=lambda *args, **kwargs: ControlledProcess(),
                guard_policy=self._test_guard_policy(runtime),
            )
            controller.preflight(
                {"device": "SERIAL-001", "authorized_tasks": ["DailyRewards"]}
            )
            config = self._read_config(config_path)
            config["instances"][0]["preActions"] = [
                {"id": "unsafe", "enabled": True, "program": "cmd.exe"}
            ]
            self._write_config(config_path, config)
            with self.assertRaisesRegex(RuntimeError, "前置程序"):
                controller.start({"device": "SERIAL-001"})
            snapshot = controller.snapshot()
            self.assertEqual(snapshot["status"], "error")
            self.assertIsNone(snapshot["preflight"])

    def test_watcher_uses_per_task_api_status_not_process_exit(self) -> None:
        for terminal, expected_status in (("succeeded", "completed"), ("failed", "error")):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                runtime, _ = self._make_runtime(output)
                process = ControlledProcess()
                api = FakeMxuApi(runtime, terminal=terminal)
                controller = MaaEndRuntimeController(
                    output,
                    runtime_path=runtime,
                    instance_name="日常任务",
                    popen_factory=self._task_success_popen(
                        process,
                        "DailyRewardStart",
                        "DailyRewardEnd",
                    ),
                    http_json=api,
                    http_bytes=self._fake_mxu_screenshot,
                    api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                    guard_policy=self._test_guard_policy(runtime),
                )
                request = {
                    "device": "SERIAL-001",
                    "authorized_tasks": ["DailyRewards"],
                }
                controller.preflight(request)
                controller.start(request)
                deadline = time.time() + 2
                snapshot = controller.snapshot()
                while snapshot["status"] == "running" and time.time() < deadline:
                    time.sleep(0.01)
                    snapshot = controller.snapshot()
                self.assertEqual(snapshot["status"], expected_status)
                self.assertEqual(snapshot["last_exit_code"], 0)
                self.assertEqual(
                    snapshot["mxu_api"]["tasks"][0]["status"],
                    terminal,
                )
                screenshots = snapshot["mxu_api"]["screenshots"]
                self.assertEqual(
                    [row["label"] for row in screenshots],
                    ["connection", "terminal"],
                )
                self.assertEqual(
                    [(row["width"], row["height"]) for row in screenshots],
                    [(1280, 720), (1280, 720)],
                )
                self.assertTrue(Path(screenshots[1]["path"]).is_file())
                if terminal == "failed":
                    self.assertIn("DailyRewards", snapshot["last_error"])
                    self.assertFalse(snapshot["pipeline_verified"])
                else:
                    self.assertTrue(snapshot["pipeline_verified"])
                    self.assertTrue(snapshot["guarded_flow_verified"])
                    self.assertFalse(snapshot["end_to_end_verified"])

    def test_terminal_evidence_failure_does_not_rewrite_task_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            process = ControlledProcess()
            api = FakeMxuApi(runtime, terminal="succeeded")
            screenshot_calls = 0

            def screenshot(*args, **kwargs) -> bytes:
                nonlocal screenshot_calls
                screenshot_calls += 1
                if screenshot_calls == 1:
                    return self._fake_mxu_screenshot()
                raise RuntimeError("late screenshot unavailable")

            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=self._task_success_popen(
                    process,
                    "DailyRewardStart",
                    "DailyRewardEnd",
                ),
                http_json=api,
                http_bytes=screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=self._test_guard_policy(runtime),
            )
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards"],
            }
            controller.preflight(request)
            controller.start(request)
            deadline = time.time() + 2
            snapshot = controller.snapshot()
            while snapshot["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
                snapshot = controller.snapshot()
            incident_path = next(
                path
                for path in Path(snapshot["last_run_dir"]).glob(
                    "incidents/*/incident.json"
                )
                if json.loads(path.read_text(encoding="utf-8")).get("kind")
                == "maaend_terminal_contract_failed"
            )
            incident = json.loads(incident_path.read_text(encoding="utf-8"))
            artifacts = incident["occurrences"][-1]["artifacts"]
            request_evidence = json.loads(
                (incident_path.parent / artifacts["request"]).read_text(
                    encoding="utf-8"
                )
            )
            mxu_evidence = json.loads(
                (incident_path.parent / artifacts["mxu-state"]).read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["mxu_api"]["tasks"][0]["status"], "succeeded")
        self.assertFalse(snapshot["pipeline_verified"])
        self.assertFalse(snapshot["guarded_flow_verified"])
        self.assertFalse(snapshot["terminal_contract"]["verified"])
        self.assertEqual(
            [row["label"] for row in snapshot["mxu_api"]["screenshots"]],
            ["connection"],
        )
        self.assertTrue(
            any(row["status"] == "evidence_warning" for row in snapshot["logs"])
        )
        self.assertTrue(
            any(
                row["kind"] == "maaend_terminal_contract_failed"
                for row in snapshot["incidents"]
            )
        )
        self.assertIn("logical-connection", artifacts)
        self.assertIn("alignment-trace", artifacts)
        self.assertIn("structured-events", artifacts)
        self.assertIn("runtime", artifacts)
        self.assertEqual(request_evidence["tasks"][0]["entry"], "DailyRewardStart")
        self.assertEqual(mxu_evidence["tasks"][0]["status"], "succeeded")

    def test_android_open_game_requires_and_accepts_viewport_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, config_path = self._make_runtime(output)
            interface_path = runtime / "interface.json"
            interface = json.loads(interface_path.read_text(encoding="utf-8"))
            interface["task"].append(
                {
                    "name": "AndroidOpenGame",
                    "entry": "AndroidOpenGame",
                    "controller": ["ADB"],
                    "group": ["other_menu"],
                }
            )
            interface_path.write_text(
                json.dumps(interface, ensure_ascii=False),
                encoding="utf-8",
            )
            config = self._read_config(config_path)
            config["instances"][0]["tasks"] = [
                {
                    "id": "open-game",
                    "taskName": "AndroidOpenGame",
                    "enabled": True,
                    "optionValues": {},
                }
            ]
            self._write_config(config_path, config)
            process = ControlledProcess()

            def popen(command, **kwargs):
                marker = {
                    "level": "info",
                    "task_id": 200000001,
                    "entry": "AndroidOpenGame",
                    "raw_width": 2800,
                    "raw_height": 1260,
                    "viewport_width": 2240,
                    "viewport_height": 1260,
                    "message": (
                        "landscape screenshot viewport activated; "
                        "continuing AndroidOpenGame"
                    ),
                }
                kwargs["stdout"].write(
                    (json.dumps(marker, ensure_ascii=False) + "\n").encode("utf-8")
                )
                details = {
                    "task_id": 200000001,
                    "entry": "AndroidOpenGame",
                    "uuid": "open-game",
                    "hash": "test-hash",
                }
                task_line = (
                    "[TRC] [message=Tasker.Task.Succeeded] "
                    f"[details_json={json.dumps(details, separators=(',', ':'))}] "
                    "[trans_arg=true]\n"
                )
                kwargs["stdout"].write(task_line.encode("utf-8"))
                kwargs["stdout"].flush()
                return process

            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=popen,
                http_json=FakeMxuApi(runtime, terminal="succeeded"),
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=self._test_guard_policy(runtime),
            )
            request = {"device": "SERIAL-001"}
            controller.preflight(request)
            controller.start(request)
            deadline = time.time() + 2
            snapshot = controller.snapshot()
            while snapshot["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)
                snapshot = controller.snapshot()

        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(snapshot["launch_verified"])
        self.assertTrue(snapshot["pipeline_verified"])
        self.assertTrue(snapshot["custom_agent_verified"])
        self.assertTrue(snapshot["guarded_flow_verified"])
        self.assertFalse(snapshot["input_verified"])
        self.assertFalse(snapshot["end_to_end_verified"])

    def test_start_rejects_partially_accepted_task_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, config_path = self._make_runtime(output)
            config = self._read_config(config_path)
            config["instances"][0]["tasks"].append(
                {
                    "id": "task-2",
                    "taskName": "AutoEssence",
                    "enabled": True,
                    "optionValues": {},
                }
            )
            self._write_config(config_path, config)
            api = FakeMxuApi(runtime, accepted_task_count=1)
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=lambda *args, **kwargs: ControlledProcess(),
                http_json=api,
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=self._test_guard_policy(runtime),
            )
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards", "AutoEssence"],
            }
            controller.preflight(request)
            with self.assertRaisesRegex(RuntimeError, "1/2"):
                controller.start(request)
            self.assertEqual(controller.snapshot()["status"], "error")

    def test_stop_escalates_to_kill_and_snapshot_has_no_arbitrary_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runtime, _ = self._make_runtime(output)
            process = ControlledProcess(
                stop_on_signal=False,
                stop_on_terminate=False,
                immediate_timeout=True,
            )
            controller = MaaEndRuntimeController(
                output,
                runtime_path=runtime,
                instance_name="日常任务",
                popen_factory=lambda *args, **kwargs: process,
                http_json=FakeMxuApi(runtime),
                http_bytes=self._fake_mxu_screenshot,
                api_host_preparer=lambda root: root / "MaaEnd.api-host.exe",
                guard_policy=self._test_guard_policy(runtime),
            )
            request = {
                "device": "SERIAL-001",
                "authorized_tasks": ["DailyRewards"],
            }
            controller.preflight(request)
            controller.start(request)
            snapshot = controller.stop()
            serialized = json.dumps(snapshot, ensure_ascii=False)

        self.assertEqual(process.kill_count, 1)
        self.assertFalse(snapshot["running"])
        self.assertNotIn("--autostart", serialized)
        self.assertNotIn("command", serialized)


if __name__ == "__main__":
    unittest.main()
