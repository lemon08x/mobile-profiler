from __future__ import annotations

import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from mobile_profiler.adb_agent import (
    ActionExecution,
    AdbAgentController,
    ModelDecision,
    OpenAICompatibleVisionClient,
    PNG_SIGNATURE,
    chat_completions_url,
    execute_adb_action,
    normalize_agent_tasks,
    parse_model_decision,
    png_dimensions,
)
from mobile_profiler.adb_agent_prompts import DEFAULT_ADB_AGENT_SYSTEM_PROMPT
from mobile_profiler.ui import DashboardHTTPServer, DashboardManager


def sample_png(width: int = 1080, height: int = 2400) -> bytes:
    return (
        PNG_SIGNATURE
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"test-png"
    )


class FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class AdbAgentProtocolTests(unittest.TestCase):
    def test_btr2_base_url_is_normalized_to_chat_completions(self) -> None:
        self.assertEqual(
            chat_completions_url("http://192.168.31.237:8000"),
            "http://192.168.31.237:8000/v1/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("http://host:8000/v1"),
            "http://host:8000/v1/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("https://host/v1/chat/completions"),
            "https://host/v1/chat/completions",
        )
        with self.assertRaises(ValueError):
            chat_completions_url("file:///tmp/model")

    def test_native_phone_action_tool_call_is_parsed(self) -> None:
        decision = parse_model_decision(
            {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "设置入口在右上角。",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "phone_action",
                                        "arguments": json.dumps(
                                            {"action": "tap", "element": [900, 80]}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 18},
            }
        )
        self.assertEqual(decision.action, {"action": "tap", "element": [900, 80]})
        self.assertEqual(decision.prompt_tokens, 120)
        self.assertEqual(decision.completion_tokens, 18)

    def test_client_sends_screenshot_and_required_native_tool(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "phone_action",
                                    "arguments": '{"action":"finish","message":"done"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
        with patch(
            "mobile_profiler.adb_agent.urlopen",
            return_value=FakeHttpResponse(response),
        ) as opener:
            client = OpenAICompatibleVisionClient(
                "http://192.168.31.237:8000",
                "qwen3.6-27b",
                request_timeout_s=12,
                system_prompt="CUSTOM ADB SYSTEM PROMPT",
            )
            decision = client.decide(
                task="回到桌面",
                task_name="初始化桌面",
                attention_prompt="遇到锁屏就接管",
                workflow_summary="1. 打开设置 - completed: 已完成",
                task_elapsed_s=2.5,
                task_timeout_s=60,
                step=1,
                max_steps=5,
                screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
            )
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://192.168.31.237:8000/v1/chat/completions")
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(body["tools"][0]["function"]["name"], "phone_action")
        self.assertEqual(body["messages"][0]["content"], "CUSTOM ADB SYSTEM PROMPT")
        prompt_text = body["messages"][1]["content"][0]["text"]
        self.assertIn("当前测试子任务：初始化桌面", prompt_text)
        self.assertIn("遇到锁屏就接管", prompt_text)
        self.assertIn("finish 只表示当前子任务完成", prompt_text)
        image_url = body["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertEqual(decision.action["action"], "finish")


class AdbAgentTaskNormalizationTests(unittest.TestCase):
    def test_legacy_single_task_payload_is_normalized(self) -> None:
        tasks = normalize_agent_tasks(
            {
                "task": "回到桌面",
                "task_name": "准备测试",
                "max_steps": 8,
                "task_timeout_s": 45,
                "on_failure": "continue",
            }
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "准备测试")
        self.assertEqual(tasks[0]["prompt"], "回到桌面")
        self.assertEqual(tasks[0]["max_steps"], 8)
        self.assertEqual(tasks[0]["timeout_s"], 45)
        self.assertEqual(tasks[0]["on_failure"], "continue")

    def test_task_list_validates_content_and_deduplicates_ids(self) -> None:
        tasks = normalize_agent_tasks(
            {
                "tasks": [
                    {"id": "same", "prompt": "任务一"},
                    {"id": "same", "prompt": "任务二", "on_failure": "continue"},
                ]
            }
        )
        self.assertEqual([task["id"] for task in tasks], ["same", "same-2"])
        with self.assertRaisesRegex(ValueError, "至少编排一个"):
            normalize_agent_tasks({"tasks": []})
        with self.assertRaisesRegex(ValueError, "缺少任务目标"):
            normalize_agent_tasks({"tasks": [{"name": "空任务"}]})
        with self.assertRaisesRegex(ValueError, "失败策略"):
            normalize_agent_tasks({"tasks": [{"prompt": "test", "on_failure": "retry"}]})


class AdbAgentActionTests(unittest.TestCase):
    def test_png_dimensions_and_normalized_tap_are_mapped_to_device_pixels(self) -> None:
        png = sample_png(1080, 2400)
        self.assertEqual(png_dimensions(png), (1080, 2400))
        completed = Mock(returncode=0, stdout=b"", stderr=b"")
        with patch("mobile_profiler.adb_agent.subprocess.run", return_value=completed) as run:
            result = execute_adb_action(
                "adb",
                "SERIAL",
                {"action": "tap", "element": [999, 999]},
                1080,
                2400,
                threading.Event(),
            )
        self.assertEqual(result.summary, "点击 (1079, 2399)")
        self.assertEqual(
            run.call_args.args[0],
            ["adb", "-s", "SERIAL", "shell", "input", "tap", "1079", "2399"],
        )

    def test_model_cannot_execute_arbitrary_shell_or_unicode_input(self) -> None:
        with self.assertRaises(ValueError):
            execute_adb_action(
                "adb",
                "SERIAL",
                {"action": "shell", "command": "rm -rf /"},
                1080,
                2400,
                threading.Event(),
            )
        with self.assertRaises(ValueError):
            execute_adb_action(
                "adb",
                "SERIAL",
                {"action": "input_text", "text": "中文"},
                1080,
                2400,
                threading.Event(),
            )


class FakeDecisionClient:
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, **_kwargs: object) -> ModelDecision:
        self.calls += 1
        if self.calls == 1:
            return ModelDecision(
                {"action": "tap", "element": [500, 500]},
                reasoning="点击中央按钮",
                prompt_tokens=10,
                completion_tokens=2,
            )
        return ModelDecision(
            {"action": "finish", "message": "闭环完成"},
            reasoning="目标已经完成",
            prompt_tokens=11,
            completion_tokens=3,
        )


class AdbAgentControllerTests(unittest.TestCase):
    def test_background_loop_persists_screenshots_and_events_until_finish(self) -> None:
        client = FakeDecisionClient()

        def execute(
            _adb: str,
            _device: str,
            action: dict,
            _width: int,
            _height: int,
            _stop: threading.Event,
        ) -> ActionExecution:
            if action["action"] == "finish":
                return ActionExecution("模型确认任务完成", "闭环完成", "completed")
            return ActionExecution("点击 (540, 1200)")

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: client,
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=execute,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "完成一次闭环",
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "api_key": "secret-token",
                    "system_prompt": "CUSTOM CONTROLLER SYSTEM PROMPT",
                    "max_steps": 4,
                    "step_delay_s": 0.2,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.03)
                state = controller.snapshot()

            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["step"], 2)
            self.assertEqual(state["prompt_tokens"], 21)
            self.assertEqual(state["completion_tokens"], 5)
            self.assertTrue(state["screenshot_available"])
            self.assertEqual(len(state["history"]), 2)
            output_dir = Path(str(state["output_dir"]))
            self.assertTrue((output_dir / "task-01-step-001.png").is_file())
            self.assertTrue((output_dir / "task-01-step-002.png").is_file())
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([event["event_type"] for event in events], ["task_start", "action", "action", "task_end"])
            config_text = (output_dir / "config.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-token", config_text)
            self.assertIn('"api_key_configured": true', config_text)
            self.assertIn("CUSTOM CONTROLLER SYSTEM PROMPT", config_text)
            self.assertEqual(state["system_prompt_version"], "custom")
            self.assertEqual(
                state["defaults"]["system_prompt"], DEFAULT_ADB_AGENT_SYSTEM_PROMPT
            )
            self.assertNotIn("api_key", state)

    def test_two_tasks_run_sequentially_and_finish_is_task_local(self) -> None:
        calls: list[dict] = []

        class FinishEveryTaskClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                calls.append(dict(kwargs))
                return ModelDecision(
                    {"action": "finish", "message": f"{kwargs['task_name']} 完成"},
                    reasoning="当前子任务目标已满足",
                )

        def execute(
            _adb: str,
            _device: str,
            action: dict,
            _width: int,
            _height: int,
            _stop: threading.Event,
        ) -> ActionExecution:
            return ActionExecution("模型确认任务完成", str(action.get("message")), "completed")

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: FinishEveryTaskClient(),
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=execute,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "workflow_name": "two tasks",
                    "tasks": [
                        {"name": "任务 A", "prompt": "完成 A", "max_steps": 3},
                        {"name": "任务 B", "prompt": "完成 B", "max_steps": 3},
                    ],
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "step_delay_s": 0.2,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

            self.assertEqual(state["status"], "completed")
            self.assertEqual([call["task_name"] for call in calls], ["任务 A", "任务 B"])
            self.assertEqual([item["status"] for item in state["task_results"]], ["completed", "completed"])
            self.assertEqual(state["task_index"], 2)
            self.assertEqual(state["total_steps"], 2)
            self.assertIn("任务 A - completed", calls[1]["workflow_summary"])
            output_dir = Path(str(state["output_dir"]))
            self.assertTrue((output_dir / "task-01-step-001.png").is_file())
            self.assertTrue((output_dir / "task-02-step-001.png").is_file())

    def test_continue_policy_completes_workflow_with_warnings(self) -> None:
        class ContinueClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                if kwargs["task_name"] == "允许失败":
                    return ModelDecision({"action": "tap", "element": [500, 500]})
                return ModelDecision({"action": "finish", "message": "第二项完成"})

        def execute(
            _adb: str,
            _device: str,
            action: dict,
            _width: int,
            _height: int,
            _stop: threading.Event,
        ) -> ActionExecution:
            if action["action"] == "finish":
                return ActionExecution("模型确认任务完成", "第二项完成", "completed")
            return ActionExecution("点击完成")

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: ContinueClient(),
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=execute,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "tasks": [
                        {"name": "允许失败", "prompt": "不结束", "max_steps": 1, "on_failure": "continue"},
                        {"name": "后续任务", "prompt": "完成", "max_steps": 1},
                    ],
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "step_delay_s": 0.2,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

            self.assertEqual(state["status"], "completed_with_warnings")
            self.assertEqual([item["status"] for item in state["task_results"]], ["max_steps", "completed"])

    def test_dashboard_manager_requires_a_ready_android_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = DashboardManager("adb", Path(temporary))
            manager.adb_agent.start = Mock(return_value={"status": "starting"})  # type: ignore[method-assign]
            ready = [
                {"serial": "ANDROID", "state": "device", "platform": "android"},
                {"serial": "harmony:PHONE", "state": "device", "platform": "harmony"},
            ]
            with patch.object(manager, "devices", return_value=(ready, None)):
                result = manager.start_adb_agent({"device": "ANDROID", "task": "test"})
                self.assertEqual(result["status"], "starting")
                with self.assertRaisesRegex(RuntimeError, "is not ready"):
                    manager.start_adb_agent({"device": "harmony:PHONE", "task": "test"})
            manager.adb_agent.start.assert_called_once_with(  # type: ignore[attr-defined]
                {"device": "ANDROID", "task": "test"}
            )


class AgentRouteTests(unittest.TestCase):
    def test_http_routes_start_stop_and_serve_latest_screenshot(self) -> None:
        png = sample_png(320, 640)

        class FakeAgent:
            def latest_screenshot(self) -> bytes:
                return png

        class FakeManager:
            adb_agent = FakeAgent()

            def start_adb_agent(self, payload: dict) -> dict:
                return {"status": "starting", "task": payload["task"]}

            def stop_adb_agent(self) -> dict:
                return {"status": "stopping"}

        server = DashboardHTTPServer(("127.0.0.1", 0), FakeManager())  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            start_request = UrlRequest(
                base + "/api/ai-agent/start",
                data=json.dumps({"task": "test"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(start_request, timeout=5) as response:
                self.assertEqual(json.loads(response.read())["status"], "starting")
            stop_request = UrlRequest(
                base + "/api/ai-agent/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(stop_request, timeout=5) as response:
                self.assertEqual(json.loads(response.read())["status"], "stopping")
            with urlopen(base + "/api/ai-agent/screenshot", timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertEqual(response.read(), png)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
