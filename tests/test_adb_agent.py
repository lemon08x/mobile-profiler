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
    AUTOMATION_ENGINE_HYBRID,
    AUTOMATION_ENGINE_UIAUTOMATOR2,
    ActionExecution,
    AdbAgentController,
    AnthropicVisionClient,
    GeminiVisionClient,
    MAX_AGENT_TASKS,
    ModelDecision,
    OpenAICompatibleVisionClient,
    PNG_SIGNATURE,
    anthropic_messages_url,
    chat_completions_url,
    create_vision_model_client,
    execute_adb_action,
    gemini_generate_content_url,
    normalize_agent_tasks,
    normalize_automation_engine,
    normalize_model_provider,
    normalize_model_thinking_mode,
    parse_model_decision,
    phone_action_tool,
    png_dimensions,
)
from mobile_profiler.adb_agent_prompts import (
    ADB_AGENT_SYSTEM_PROMPT_VERSION,
    DEFAULT_ADB_AGENT_SYSTEM_PROMPT,
    task_templates_snapshot,
)
from mobile_profiler.automation import (
    Artifact,
    Bounds,
    DeviceContext,
    Observation,
    UiElement,
    UiHierarchy,
)
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
    def test_default_prompt_requires_visible_state_verification(self) -> None:
        self.assertEqual(ADB_AGENT_SYSTEM_PROMPT_VERSION, "adb-phone-agent-v13")
        self.assertIn("目标控件已可见时直接操作，不要先滚动", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("只代表输入事件已发送，不代表应用状态已经改变", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("缺少中间状态证据时调用 `take_over`", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("目标仍在原位", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("最新截图仍显示锁屏时禁止 `launch_app`", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("uiautomator2 语义控件", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("视觉 + uiautomator2", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("`tap_element`", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("`input_secret`", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("禁止对 canvas 元素使用 `tap_element`", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)
        self.assertIn("`skip` 表示当前检查项不适用", DEFAULT_ADB_AGENT_SYSTEM_PROMPT)

    def test_two_campaign_stages_are_exposed_as_complete_workflow_templates(self) -> None:
        templates = task_templates_snapshot()
        preparation = next(item for item in templates if item["id"] == "android-campaign-preparation")
        endurance = next(item for item in templates if item["id"] == "android-campaign-two-hour-test")
        phone_configuration = next(
            item for item in templates if item["id"] == "phone-configuration-endurance-5"
        )
        self.assertEqual(preparation["campaign_stage"], "prepare")
        self.assertEqual(endurance["campaign_stage"], "test")
        self.assertFalse(preparation["loop_enabled"])
        self.assertTrue(endurance["loop_enabled"])
        self.assertEqual(phone_configuration["kind"], "phone_configuration")
        self.assertEqual(phone_configuration["revision"], "phone-config-20260722-v9")
        self.assertFalse(phone_configuration["loop_enabled"])
        self.assertGreaterEqual(len(phone_configuration["tasks"]), 40)
        self.assertTrue(all(
            task["on_failure"] == "continue" for task in phone_configuration["tasks"]
        ))
        phone_configuration_text = json.dumps(phone_configuration, ensure_ascii=False)
        self.assertIn("192.168.31.150", phone_configuration_text)
        self.assertIn("杭州西湖", phone_configuration_text)
        self.assertIn("微博与网易云登录要求冲突", phone_configuration_text)
        self.assertIn("调用 skip", phone_configuration_text)
        self.assertIn("com.bbk.appstore", phone_configuration_text)
        self.assertIn("不要使用不存在的 com.vivo.appstore", phone_configuration_text)
        self.assertIn("search_result_list", phone_configuration_text)
        self.assertIn("search_input 已有 focus", phone_configuration_text)
        self.assertIn("recommend_download_list_layout", phone_configuration_text)
        self.assertIn("download_area", phone_configuration_text)
        self.assertIn("通知权限必须选择", phone_configuration_text)
        self.assertIn("com.vivo.browser", phone_configuration_text)
        self.assertIn("禁止打开任何应用商店", phone_configuration_text)
        self.assertIn("GENSHIN_ACCOUNT", phone_configuration_text)
        self.assertIn("不要因此 take_over", phone_configuration_text)
        self.assertGreaterEqual(len(preparation["tasks"]), 8)
        self.assertGreaterEqual(len(endurance["tasks"]), 5)
        self.assertIn("2 小时/轮", endurance["label"])
        self.assertIn("store-install-tv.danmaku.bili", {
            task["id"] for task in preparation["tasks"]
        })
        self.assertIn("light-up-toggle", {
            task["id"] for task in endurance["tasks"]
        })
        self.assertIn("store-install-com.baidu.BaiduMap", {
            task["id"] for task in preparation["tasks"]
        })
        self.assertIn("store-install-com.tencent.map", {
            task["id"] for task in preparation["tasks"]
        })
        self.assertIn("baidu-map-pan", {
            task["id"] for task in endurance["tasks"]
        })
        self.assertIn("tencent-map-pan", {
            task["id"] for task in endurance["tasks"]
        })
        self.assertNotIn("two-zero-four-eight-swipe", {
            task["id"] for template in (preparation, endurance) for task in template["tasks"]
        })

        preparation["tasks"][0]["name"] = "mutated"
        fresh = task_templates_snapshot()
        self.assertNotEqual(fresh[0]["tasks"][0]["name"], "mutated")

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
        self.assertEqual(
            chat_completions_url(
                "https://azure.example/openai/deployments/vision/chat/completions?api-version=2025-01-01"
            ),
            "https://azure.example/openai/deployments/vision/chat/completions?api-version=2025-01-01",
        )
        self.assertEqual(
            anthropic_messages_url("https://api.anthropic.com"),
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(
            gemini_generate_content_url(
                "https://generativelanguage.googleapis.com/v1beta",
                "gemini-vision-model",
            ),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-vision-model:generateContent",
        )
        with self.assertRaises(ValueError):
            chat_completions_url("file:///tmp/model")
        with self.assertRaisesRegex(ValueError, "独立的 API Key 字段"):
            gemini_generate_content_url(
                "https://generativelanguage.googleapis.com/v1beta?key=secret",
                "gemini-vision-model",
            )

    def test_model_provider_aliases_are_normalized(self) -> None:
        self.assertEqual(normalize_model_provider("openai"), "openai_compatible")
        self.assertEqual(normalize_model_provider("claude"), "anthropic")
        self.assertEqual(normalize_model_provider("google"), "gemini")
        with self.assertRaisesRegex(ValueError, "不支持"):
            normalize_model_provider("unknown-provider")
        self.assertEqual(normalize_model_thinking_mode("off"), "disabled")
        self.assertEqual(normalize_model_thinking_mode("on"), "enabled")
        self.assertEqual(normalize_model_thinking_mode("default"), "auto")
        with self.assertRaisesRegex(ValueError, "思考模式"):
            normalize_model_thinking_mode("turbo")
        self.assertEqual(normalize_automation_engine("semantic"), "uiautomator2")
        self.assertEqual(normalize_automation_engine("combined"), "hybrid")
        self.assertEqual(normalize_automation_engine("screenshot"), "vision")
        with self.assertRaisesRegex(ValueError, "操作引擎"):
            normalize_automation_engine("airtest")

    def test_uiautomator2_tool_exposes_revision_bound_element_action(self) -> None:
        tool = phone_action_tool(AUTOMATION_ENGINE_UIAUTOMATOR2)
        parameters = tool["function"]["parameters"]
        self.assertIn("tap_element", parameters["properties"]["action"]["enum"])
        self.assertIn("skip", parameters["properties"]["action"]["enum"])
        self.assertIn("element_id", parameters["properties"])
        self.assertIn("observation_revision", parameters["properties"])
        self.assertIn("input_secret", parameters["properties"]["action"]["enum"])
        self.assertIn("secret_id", parameters["properties"])
        vision_tool = phone_action_tool("vision")
        self.assertNotIn(
            "tap_element",
            vision_tool["function"]["parameters"]["properties"]["action"]["enum"],
        )
        hybrid_tool = phone_action_tool(AUTOMATION_ENGINE_HYBRID)
        self.assertIn(
            "tap_element",
            hybrid_tool["function"]["parameters"]["properties"]["action"]["enum"],
        )

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
                api_key="openai-secret",
                request_timeout_s=12,
                system_prompt="CUSTOM ADB SYSTEM PROMPT",
                api_key_mode="api-key",
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
        self.assertEqual(request.get_header("Api-key"), "openai-secret")
        self.assertEqual(
            body["tool_choice"],
            {"type": "function", "function": {"name": "phone_action"}},
        )
        self.assertEqual(body["max_tokens"], 1000)
        self.assertEqual(body["tools"][0]["function"]["name"], "phone_action")
        self.assertNotIn("chat_template_kwargs", body)
        self.assertNotIn("frequency_penalty", body)
        self.assertEqual(body["messages"][0]["content"], "CUSTOM ADB SYSTEM PROMPT")
        prompt_text = body["messages"][1]["content"][0]["text"]
        self.assertIn("当前测试子任务：初始化桌面", prompt_text)
        self.assertIn("遇到锁屏就接管", prompt_text)
        self.assertIn("finish 只表示当前子任务完成", prompt_text)
        image_url = body["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertEqual(decision.action["action"], "finish")

    def test_client_sends_previous_and_current_frames_for_visual_comparison(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "phone_action",
                                    "arguments": '{"action":"finish","message":"位置确已变化"}',
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
                "http://127.0.0.1:8000",
                "qwen3.6-27b",
                model_thinking_mode="disabled",
            )
            client.decide(
                task="比较角色位置",
                step=2,
                max_steps=4,
                screenshot_png=sample_png(),
                previous_screenshot_png=sample_png(width=720, height=1600),
                width=1080,
                height=2400,
                history=[],
            )

        body = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        content = body["messages"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image_url", "image_url"])
        self.assertIn("第一张是上一动作执行前", content[0]["text"])
        self.assertNotEqual(
            content[1]["image_url"]["url"],
            content[2]["image_url"]["url"],
        )

    def test_uiautomator2_client_sends_semantic_tree_without_images(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "phone_action",
                                    "arguments": json.dumps(
                                        {
                                            "action": "tap_element",
                                            "element_id": "e002",
                                            "observation_revision": "u2-000002-new",
                                        }
                                    ),
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
                "http://127.0.0.1:8000",
                "qwen3.6-27b",
                automation_engine="uiautomator2",
            )
            decision = client.decide(
                task="点击开始",
                step=2,
                max_steps=4,
                screenshot_png=sample_png(),
                previous_screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
                observation_revision="u2-000002-new",
                previous_ui_hierarchy_text="revision=u2-000001-old [e001] text=Menu",
                ui_hierarchy_text="revision=u2-000002-new [e002] text=Play flags=click",
            )

        body = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        content = body["messages"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["text"])
        self.assertIn("本轮不向模型发送截图", content[0]["text"])
        self.assertIn("u2-000002-new", content[0]["text"])
        actions = body["tools"][0]["function"]["parameters"]["properties"]["action"]["enum"]
        self.assertIn("tap_element", actions)
        self.assertEqual(decision.action["element_id"], "e002")

    def test_hybrid_client_sends_semantic_tree_and_images(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "phone_action",
                                    "arguments": '{"action":"tap","element":[500,500]}',
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
                "http://127.0.0.1:8000",
                "qwen3.6-27b",
                automation_engine="hybrid",
            )
            client.decide(
                task="操作游戏画布",
                step=2,
                max_steps=4,
                screenshot_png=sample_png(),
                previous_screenshot_png=sample_png(width=720, height=1600),
                width=1080,
                height=2400,
                history=[],
                observation_revision="u2-000002-new",
                previous_ui_hierarchy_text="revision=u2-000001-old [e001] class=View",
                ui_hierarchy_text="revision=u2-000002-new [e001] class=View",
            )

        body = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        content = body["messages"][1]["content"]
        self.assertEqual(
            [item["type"] for item in content],
            ["text", "image_url", "image_url"],
        )
        self.assertIn("视觉 + uiautomator2 混合辅助", content[0]["text"])
        self.assertIn("最新语义树", content[0]["text"])
        actions = body["tools"][0]["function"]["parameters"]["properties"]["action"]["enum"]
        self.assertIn("tap_element", actions)

    def test_openai_parser_recovers_action_json_from_reasoning_content(self) -> None:
        decision = parse_model_decision(
            {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": '{"action":"wait","duration_seconds":2}'
                        }
                    }
                ]
            }
        )
        self.assertEqual(decision.action, {"action": "wait", "duration_seconds": 2})

    def test_openai_compatible_client_can_disable_template_thinking(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "phone_action",
                                    "arguments": '{"action":"take_over","message":"blocked"}',
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
                "http://127.0.0.1:8000",
                "qwen3.6-27b",
                model_thinking_mode="disabled",
            )
            client.decide(
                task="检查阻塞",
                step=1,
                max_steps=3,
                screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
            )
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_openai_compatible_client_repairs_missing_tool_call_once(self) -> None:
        missing_tool = {"choices": [{"message": {"content": "I will inspect it."}}]}
        repaired = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "phone_action",
                                    "arguments": '{"action":"take_over","message":"blocked"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
        with patch(
            "mobile_profiler.adb_agent.urlopen",
            side_effect=[FakeHttpResponse(missing_tool), FakeHttpResponse(repaired)],
        ) as opener:
            client = OpenAICompatibleVisionClient(
                "http://127.0.0.1:8000",
                "qwen3.6-27b",
                model_thinking_mode="disabled",
            )
            decision = client.decide(
                task="检查当前页面",
                step=1,
                max_steps=3,
                screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
            )
        self.assertEqual(decision.action["action"], "take_over")
        self.assertEqual(opener.call_count, 2)
        repair_request = opener.call_args_list[1].args[0]
        repair_body = json.loads(repair_request.data.decode("utf-8"))
        self.assertEqual(repair_body["max_tokens"], 512)
        self.assertIn(
            "上一响应没有调用 phone_action",
            repair_body["messages"][1]["content"][0]["text"],
        )

    def test_openai_compatible_client_safely_takes_over_when_repair_also_fails(
        self,
    ) -> None:
        missing_tool = {
            "choices": [{"message": {"content": "I cannot emit the tool call."}}],
            "usage": {"prompt_tokens": 21, "completion_tokens": 8},
        }
        with patch(
            "mobile_profiler.adb_agent.urlopen",
            side_effect=[FakeHttpResponse(missing_tool), FakeHttpResponse(missing_tool)],
        ) as opener:
            client = OpenAICompatibleVisionClient(
                "http://127.0.0.1:8000",
                "qwen3.6-27b",
                model_thinking_mode="disabled",
            )
            decision = client.decide(
                task="检查当前页面",
                step=3,
                max_steps=3,
                screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
            )

        self.assertEqual(opener.call_count, 2)
        self.assertEqual(decision.action["action"], "take_over")
        self.assertIn("连续两次未返回", decision.action["message"])
        self.assertEqual(decision.content, "I cannot emit the tool call.")
        self.assertEqual(decision.prompt_tokens, 21)
        self.assertEqual(decision.completion_tokens, 8)

    def test_anthropic_adapter_translates_image_tool_and_response(self) -> None:
        response = {
            "content": [
                {"type": "text", "text": "目标已经完成"},
                {
                    "type": "tool_use",
                    "name": "phone_action",
                    "input": {"action": "finish", "message": "设置页可见"},
                },
            ],
            "usage": {"input_tokens": 41, "output_tokens": 7},
        }
        with patch(
            "mobile_profiler.adb_agent.urlopen",
            return_value=FakeHttpResponse(response),
        ) as opener:
            client = AnthropicVisionClient(
                "https://api.anthropic.com",
                "claude-vision-model",
                api_key="anthropic-secret",
            )
            decision = client.decide(
                task="打开设置",
                step=1,
                max_steps=3,
                screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
            )
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "anthropic-secret")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(body["tools"][0]["name"], "phone_action")
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "phone_action"})
        image = body["messages"][0]["content"][1]
        self.assertEqual(image["source"]["media_type"], "image/png")
        self.assertEqual(decision.action["action"], "finish")
        self.assertEqual(decision.prompt_tokens, 41)

    def test_gemini_adapter_translates_image_function_and_response(self) -> None:
        response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "截图显示桌面", "thought": True},
                            {
                                "functionCall": {
                                    "name": "phone_action",
                                    "args": {"action": "finish", "message": "桌面可见"},
                                }
                            },
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 33, "candidatesTokenCount": 6},
        }
        with patch(
            "mobile_profiler.adb_agent.urlopen",
            return_value=FakeHttpResponse(response),
        ) as opener:
            client = GeminiVisionClient(
                "https://generativelanguage.googleapis.com/v1beta",
                "gemini-vision-model",
                api_key="gemini-secret",
            )
            decision = client.decide(
                task="回到桌面",
                step=1,
                max_steps=3,
                screenshot_png=sample_png(),
                width=1080,
                height=2400,
                history=[],
            )
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertTrue(request.full_url.endswith("/models/gemini-vision-model:generateContent"))
        self.assertEqual(request.get_header("X-goog-api-key"), "gemini-secret")
        declaration = body["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["name"], "phone_action")
        self.assertNotIn("additionalProperties", declaration["parameters"])
        self.assertEqual(declaration["parameters"]["type"], "OBJECT")
        self.assertEqual(
            declaration["parameters"]["properties"]["action"]["type"], "STRING"
        )
        self.assertEqual(
            body["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"],
            ["phone_action"],
        )
        self.assertEqual(decision.action["action"], "finish")
        self.assertEqual(decision.reasoning, "截图显示桌面")
        self.assertEqual(decision.completion_tokens, 6)

    def test_client_factory_selects_native_provider_adapter(self) -> None:
        base = {
            "api_base_url": "https://api.anthropic.com",
            "model": "claude-vision-model",
            "model_provider": "anthropic",
        }
        self.assertIsInstance(create_vision_model_client(base), AnthropicVisionClient)
        gemini = {
            "api_base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-vision-model",
            "model_provider": "gemini",
        }
        self.assertIsInstance(create_vision_model_client(gemini), GeminiVisionClient)


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

    def test_task_list_supports_large_application_catalogs(self) -> None:
        tasks = normalize_agent_tasks(
            {"tasks": [{"prompt": f"任务 {index}"} for index in range(MAX_AGENT_TASKS)]}
        )
        self.assertEqual(len(tasks), MAX_AGENT_TASKS)
        with self.assertRaisesRegex(ValueError, str(MAX_AGENT_TASKS)):
            normalize_agent_tasks(
                {
                    "tasks": [
                        {"prompt": f"任务 {index}"}
                        for index in range(MAX_AGENT_TASKS + 1)
                    ]
                }
            )


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

    def test_reasoning_and_terminal_messages_are_hard_limited(self) -> None:
        decision = parse_model_decision(
            {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "判" * 300,
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "phone_action",
                                        "arguments": '{"action":"finish","message":"done"}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(len(decision.reasoning), 120)
        self.assertTrue(decision.reasoning.endswith("…"))
        finished = execute_adb_action(
            "adb",
            "SERIAL",
            {"action": "finish", "message": "完" * 200},
            1080,
            2400,
            threading.Event(),
        )
        takeover = execute_adb_action(
            "adb",
            "SERIAL",
            {"action": "take_over", "message": "阻" * 200},
            1080,
            2400,
            threading.Event(),
        )
        skipped = execute_adb_action(
            "adb",
            "SERIAL",
            {"action": "skip", "message": "需要人工短信验证" * 20},
            1080,
            2400,
            threading.Event(),
        )
        self.assertEqual(len(finished.message), 80)
        self.assertEqual(len(takeover.message), 120)
        self.assertEqual(len(skipped.message), 120)
        self.assertEqual(skipped.terminal_status, "skipped")
        with self.assertRaisesRegex(ValueError, "meaningful evidence message"):
            execute_adb_action(
                "adb",
                "SERIAL",
                {"action": "finish", "message": "}'"},
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
    def test_input_secret_is_ephemeral_and_redacted_from_state_and_artifacts(self) -> None:
        secret_value = "private-value-that-must-not-be-persisted"
        model_calls: list[dict] = []
        executed: list[dict] = []

        class SecretClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                model_calls.append(dict(kwargs))
                if len(model_calls) == 1:
                    return ModelDecision(
                        {
                            "action": "input_secret",
                            "secret_id": "GENSHIN_ACCOUNT",
                        }
                    )
                return ModelDecision(
                    {"action": "finish", "message": "密钥输入后界面已验证"}
                )

        class SemanticProvider:
            def __init__(self) -> None:
                self.revision = 0

            def observe(self, _request: object) -> Observation:
                self.revision += 1
                revision = f"u2-{self.revision:06d}-secret"
                return Observation(
                    revision,
                    time.time(),
                    frozenset({"ui_hierarchy"}),
                    DeviceContext("SERIAL", foreground_package="com.example.app"),
                    ui=UiHierarchy(
                        revision,
                        1080,
                        2400,
                        (
                            UiElement(
                                "e001",
                                Bounds(100, 400, 980, 560),
                                class_name="android.widget.EditText",
                                focusable=True,
                                attributes={"focused": True},
                            ),
                        ),
                        "uiautomator2",
                    ),
                )

            def execute_action(
                self,
                action: dict,
                _observation: Observation,
                _stop: threading.Event,
            ) -> str:
                executed.append(dict(action))
                return f"provider would otherwise expose {action.get('text')}"

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: SecretClient(),
                semantic_provider_factory=lambda _serial: SemanticProvider(),
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=lambda *_args: ActionExecution(
                    "模型确认任务完成", "密钥输入后界面已验证", "completed"
                ),
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "使用会话密钥完成登录",
                    "automation_engine": "hybrid",
                    "input_secrets": {"GENSHIN_ACCOUNT": secret_value},
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "max_steps": 3,
                    "step_delay_s": 0.01,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

            output_dir = Path(str(state["output_dir"]))
            persisted = (
                (output_dir / "config.json").read_text(encoding="utf-8")
                + (output_dir / "events.jsonl").read_text(encoding="utf-8")
            )

        self.assertEqual(state["status"], "completed")
        self.assertEqual(
            executed,
            [{"action": "input_text", "text": secret_value}],
        )
        self.assertEqual(
            state["history"][0]["action"],
            {"action": "input_secret", "secret_id": "GENSHIN_ACCOUNT"},
        )
        self.assertIn("内容已脱敏", state["history"][0]["result"])
        self.assertIn("GENSHIN_ACCOUNT", model_calls[0]["attention_prompt"])
        self.assertNotIn(secret_value, model_calls[0]["attention_prompt"])
        self.assertNotIn(secret_value, json.dumps(state, ensure_ascii=False))
        self.assertNotIn(secret_value, persisted)
        self.assertIn('"input_secret_ids": [', persisted)

    def test_uiautomator2_controller_persists_tree_and_uses_semantic_executor(self) -> None:
        calls: list[dict] = []
        executed: list[dict] = []

        class SemanticClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                calls.append(dict(kwargs))
                if len(calls) == 1:
                    return ModelDecision(
                        {
                            "action": "tap_element",
                            "element_id": "e001",
                            "observation_revision": kwargs["observation_revision"],
                        }
                    )
                return ModelDecision(
                    {"action": "finish", "message": "语义树显示已进入游戏"}
                )

        class SemanticProvider:
            def __init__(self) -> None:
                self.revision = 0

            def observe(self, _request: object) -> Observation:
                self.revision += 1
                revision = f"u2-{self.revision:06d}-test"
                hierarchy = UiHierarchy(
                    revision,
                    1080,
                    2400,
                    (
                        UiElement(
                            "e001",
                            Bounds(120, 1600, 960, 1780),
                            text="Play" if self.revision == 1 else "Game board",
                            clickable=self.revision == 1,
                        ),
                    ),
                    "uiautomator2",
                    raw_artifact=Artifact(
                        f"ui-{revision}",
                        "application/xml",
                        data=f"<hierarchy revision='{revision}'/>".encode(),
                    ),
                )
                return Observation(
                    revision,
                    time.time(),
                    frozenset({"ui_hierarchy"}),
                    DeviceContext(
                        "SERIAL",
                        foreground_package="com.example.game",
                        foreground_activity=".MainActivity",
                    ),
                    ui=hierarchy,
                )

            def execute_action(
                self,
                action: dict,
                _observation: Observation,
                _stop: threading.Event,
            ) -> str:
                executed.append(dict(action))
                return "uiautomator2 点击 e001"

        semantic_provider = SemanticProvider()
        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: SemanticClient(),
                semantic_provider_factory=lambda _serial: semantic_provider,
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=lambda *_args: ActionExecution(
                    "模型确认任务完成", "语义树显示已进入游戏", "completed"
                ),
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "点击 Play 后确认进入游戏",
                    "automation_engine": "uiautomator2",
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "max_steps": 3,
                    "step_delay_s": 0.01,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()
            output_dir = Path(str(state["output_dir"]))
            xml_files = (
                (output_dir / "task-01-step-001.xml").is_file(),
                (output_dir / "task-01-step-002.xml").is_file(),
            )

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["automation_engine"], "uiautomator2")
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["element_id"], "e001")
        self.assertIn("[e001]", calls[0]["ui_hierarchy_text"])
        self.assertEqual(calls[0]["previous_ui_hierarchy_text"], "")
        self.assertIn("[e001]", calls[1]["previous_ui_hierarchy_text"])
        self.assertEqual(xml_files, (True, True))
        self.assertEqual(state["ui_element_count"], 1)

    def test_screenshot_retries_recover_without_consuming_model_steps(self) -> None:
        screenshot_attempts = 0
        model_calls = 0

        class FinishClient:
            def decide(self, **_kwargs: object) -> ModelDecision:
                nonlocal model_calls
                model_calls += 1
                return ModelDecision(
                    {"action": "finish", "message": "截图恢复后确认完成"}
                )

        def capture(_adb: str, _device: str) -> tuple[bytes, int, int]:
            nonlocal screenshot_attempts
            screenshot_attempts += 1
            if screenshot_attempts <= 2:
                raise RuntimeError("device temporarily offline")
            return sample_png(), 1080, 2400

        def execute(
            _adb: str,
            _device: str,
            action: dict,
            _width: int,
            _height: int,
            _stop: threading.Event,
        ) -> ActionExecution:
            return ActionExecution(
                "模型确认任务完成",
                str(action.get("message")),
                "completed",
            )

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: FinishClient(),
                screenshot_capture=capture,
                action_executor=execute,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "等待截图恢复后完成",
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "max_steps": 2,
                    "step_delay_s": 0.2,
                    "screenshot_retry_timeout_s": 10,
                }
            )
            deadline = time.time() + 8
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.03)
                state = controller.snapshot()

            events = [
                json.loads(line)
                for line in (
                    Path(str(state["output_dir"])) / "events.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(state["status"], "completed")
        self.assertEqual(screenshot_attempts, 3)
        self.assertEqual(model_calls, 1)
        self.assertEqual(state["step"], 1)
        self.assertEqual(state["total_steps"], 1)
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "task_start",
                "screenshot_retry",
                "screenshot_recovered",
                "action",
                "task_end",
            ],
        )

    def test_controller_supplies_previous_frame_after_first_action(self) -> None:
        calls: list[dict] = []

        class ComparingClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                calls.append(dict(kwargs))
                if len(calls) == 1:
                    return ModelDecision({"action": "tap", "element": [500, 500]})
                return ModelDecision(
                    {"action": "finish", "message": "前后截图确认位置变化"}
                )

        def execute(
            _adb: str,
            _device: str,
            action: dict,
            _width: int,
            _height: int,
            _stop: threading.Event,
        ) -> ActionExecution:
            if action["action"] == "finish":
                return ActionExecution("模型确认任务完成", "前后截图确认位置变化", "completed")
            return ActionExecution("点击完成")

        screenshot = sample_png()
        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: ComparingClient(),
                screenshot_capture=lambda _adb, _device: (screenshot, 1080, 2400),
                action_executor=execute,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "比较动作前后截图",
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "max_steps": 3,
                    "step_delay_s": 0.01,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(calls[0]["previous_screenshot_png"], b"")
        self.assertEqual(calls[1]["previous_screenshot_png"], screenshot)

    def test_invalid_model_action_is_recorded_and_repaired_next_step(self) -> None:
        class RepairingClient:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, **_kwargs: object) -> ModelDecision:
                self.calls += 1
                if self.calls == 1:
                    return ModelDecision(
                        {"action": "swipe_fast", "end": [500, 200]}
                    )
                return ModelDecision(
                    {"action": "finish", "message": "已修正并完成"}
                )

        def execute(
            _adb: str,
            _device: str,
            action: dict,
            _width: int,
            _height: int,
            _stop: threading.Event,
        ) -> ActionExecution:
            if action["action"] == "swipe_fast":
                raise ValueError("swipe_fast requires start=[x,y]")
            return ActionExecution(
                "模型确认任务完成",
                str(action.get("message")),
                "completed",
            )

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: RepairingClient(),
                screenshot_capture=lambda _adb, _device: (
                    sample_png(),
                    1080,
                    2400,
                ),
                action_executor=execute,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "修复一次无效动作",
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "max_steps": 3,
                    "step_delay_s": 0.2,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["total_steps"], 2)
            self.assertEqual(len(state["history"]), 2)
            self.assertFalse(state["history"][0]["action_valid"])
            self.assertIn("参数校验失败", state["history"][0]["result"])
            self.assertTrue(state["history"][1]["action_valid"])

    def test_default_prompt_version_ignores_transport_whitespace(self) -> None:
        class FinishClient:
            def decide(self, **_kwargs: object) -> ModelDecision:
                return ModelDecision({"action": "finish", "message": "已完成"})

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: FinishClient(),
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=lambda *_args: ActionExecution(
                    "模型确认任务完成", "已完成", "completed"
                ),
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "task": "验证默认规则版本",
                    "system_prompt": DEFAULT_ADB_AGENT_SYSTEM_PROMPT + "\r\n",
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
            self.assertEqual(
                state["system_prompt_version"], ADB_AGENT_SYSTEM_PROMPT_VERSION
            )
            config = json.loads(
                (Path(str(state["output_dir"])) / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(config["system_prompt"], DEFAULT_ADB_AGENT_SYSTEM_PROMPT)

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
                    "temporary_task": True,
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
            self.assertEqual(state["model_provider"], "openai_compatible")
            self.assertEqual(
                [item["id"] for item in state["defaults"]["model_providers"]],
                ["openai_compatible", "anthropic", "gemini"],
            )
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
            self.assertTrue(state["temporary_task"])
            self.assertEqual(
                state["defaults"]["system_prompt"], DEFAULT_ADB_AGENT_SYSTEM_PROMPT
            )
            self.assertNotIn("api_key", state)
            config = json.loads(config_text)
            self.assertTrue(config["temporary_task"])

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
                    "device_brand": "vivo",
                    "device_model": "V2458A",
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
            self.assertIn("品牌 vivo", calls[0]["attention_prompt"])
            self.assertIn("型号 V2458A", calls[0]["attention_prompt"])
            self.assertIn("禁止猜测或尝试其他厂商", calls[0]["attention_prompt"])
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

    def test_skip_records_reason_and_continues_even_with_stop_policy(self) -> None:
        calls: list[dict] = []

        class SkipThenFinishClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                calls.append(dict(kwargs))
                if kwargs["task_name"] == "人工检查项":
                    return ModelDecision(
                        {"action": "skip", "message": "包装检查需要人工视频记录"}
                    )
                return ModelDecision({"action": "finish", "message": "后续设置已确认"})

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: SkipThenFinishClient(),
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=execute_adb_action,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "workflow_name": "手机配置检查",
                    "tasks": [
                        {"name": "人工检查项", "prompt": "无法执行则跳过", "max_steps": 1},
                        {"name": "后续设置", "prompt": "完成检查", "max_steps": 1},
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
            self.assertEqual(
                [item["status"] for item in state["task_results"]],
                ["skipped", "completed"],
            )
            self.assertIn("跳过 1 项", state["message"])
            self.assertIn("人工检查项 - skipped", calls[1]["workflow_summary"])

    def test_phone_configuration_takeover_is_recorded_as_skip_and_continues(self) -> None:
        class TakeoverThenFinishClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                if kwargs["task_name"] == "需人工项目":
                    return ModelDecision(
                        {"action": "take_over", "message": "当前前台不是目标应用"}
                    )
                return ModelDecision({"action": "finish", "message": "后续项目完成"})

        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: TakeoverThenFinishClient(),
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=execute_adb_action,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "workflow_name": "手机配置接管降级",
                    "tasks": [
                        {
                            "id": "phone-config-manual-item",
                            "name": "需人工项目",
                            "prompt": "无法执行则记录",
                            "max_steps": 1,
                        },
                        {
                            "id": "phone-config-follow-up",
                            "name": "后续项目",
                            "prompt": "完成后续检查",
                            "max_steps": 1,
                        },
                    ],
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "step_delay_s": 0.01,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(
            [item["status"] for item in state["task_results"]],
            ["skipped", "completed"],
        )
        self.assertEqual(state["history"][0]["action"]["action"], "skip")
        self.assertIn("已记录并继续", state["history"][0]["action"]["message"])

    def test_phone_configuration_stops_repeating_same_action_on_unchanged_state(self) -> None:
        class RepeatingClient:
            def decide(self, **kwargs: object) -> ModelDecision:
                return ModelDecision(
                    {
                        "action": "tap_element",
                        "element_id": "e001",
                        "observation_revision": kwargs["observation_revision"],
                    }
                )

        class UnchangedProvider:
            def __init__(self) -> None:
                self.revision = 0
                self.executions = 0

            def observe(self, _request: object) -> Observation:
                self.revision += 1
                revision = f"u2-{self.revision:06d}-unchanged"
                return Observation(
                    revision,
                    time.time(),
                    frozenset({"ui_hierarchy"}),
                    DeviceContext("SERIAL", foreground_package="com.example.app"),
                    ui=UiHierarchy(
                        revision,
                        1080,
                        2400,
                        (
                            UiElement(
                                "e001",
                                Bounds(100, 100, 500, 300),
                                text="Download",
                                clickable=True,
                                focusable=True,
                            ),
                        ),
                        "uiautomator2",
                    ),
                )

            def execute_action(
                self,
                _action: dict,
                _observation: Observation,
                _stop: threading.Event,
            ) -> str:
                self.executions += 1
                return "uiautomator2 点击 e001"

        provider = UnchangedProvider()
        with tempfile.TemporaryDirectory() as temporary:
            controller = AdbAgentController(
                "adb",
                Path(temporary),
                client_factory=lambda _config: RepeatingClient(),
                semantic_provider_factory=lambda _serial: provider,
                screenshot_capture=lambda _adb, _device: (sample_png(), 1080, 2400),
                action_executor=execute_adb_action,
            )
            controller.start(
                {
                    "device": "SERIAL",
                    "tasks": [
                        {
                            "id": "phone-config-repeat-guard",
                            "name": "重复保护",
                            "prompt": "点击下载",
                            "max_steps": 5,
                            "on_failure": "continue",
                        }
                    ],
                    "automation_engine": "hybrid",
                    "api_base_url": "http://127.0.0.1:8000",
                    "model": "test-model",
                    "step_delay_s": 0.01,
                }
            )
            deadline = time.time() + 3
            state = controller.snapshot()
            while state["running"] and time.time() < deadline:
                time.sleep(0.02)
                state = controller.snapshot()

        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(provider.executions, 2)
        self.assertEqual(
            [item["action"]["action"] for item in state["history"]],
            ["tap_element", "tap_element", "skip"],
        )
        self.assertIn("状态未变化", state["history"][-1]["action"]["message"])

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

    def test_dashboard_manager_starts_campaign_for_ready_android_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = DashboardManager("adb", Path(temporary))
            manager.campaign.start = Mock(  # type: ignore[method-assign]
                return_value={"status": "starting", "campaign_stage": "prepare"}
            )
            ready = [{"serial": "ANDROID", "state": "device", "platform": "android"}]
            with patch.object(manager, "devices", return_value=(ready, None)):
                result = manager.start_campaign(
                    {"device": "ANDROID", "stage": "prepare"}
                )
            self.assertEqual(result["campaign_stage"], "prepare")
            self.assertEqual(manager.snapshot()["automation_surface"], "campaign")
            manager.campaign.start.assert_called_once_with(  # type: ignore[attr-defined]
                {"device": "ANDROID", "stage": "prepare"}
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

    def test_http_routes_start_stop_and_serve_campaign_screenshot(self) -> None:
        png = sample_png(360, 720)

        class FakeCampaign:
            def latest_screenshot(self) -> bytes:
                return png

        class FakeManager:
            campaign = FakeCampaign()

            def start_campaign(self, payload: dict) -> dict:
                return {"status": "starting", "campaign_stage": payload["stage"]}

            def stop_campaign(self) -> dict:
                return {"status": "stopping"}

        server = DashboardHTTPServer(("127.0.0.1", 0), FakeManager())  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            start_request = UrlRequest(
                base + "/api/campaign/start",
                data=json.dumps({"stage": "test"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(start_request, timeout=5) as response:
                self.assertEqual(json.loads(response.read())["campaign_stage"], "test")
            stop_request = UrlRequest(
                base + "/api/campaign/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(stop_request, timeout=5) as response:
                self.assertEqual(json.loads(response.read())["status"], "stopping")
            with urlopen(base + "/api/campaign/screenshot", timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertEqual(response.read(), png)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
