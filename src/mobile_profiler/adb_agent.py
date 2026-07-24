"""Provider-neutral vision-language-model agent that operates Android via ADB.

The orchestration and validated ADB executor are independent from the selected
multimodal-model provider.  Native adapters translate one ``phone_action``
contract to OpenAI-compatible, Anthropic, or Gemini request/response formats.
No model-provided shell command is ever executed.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlparse, urlunparse
from urllib.request import Request, urlopen

from .automation import (
    Observation,
    ObservationRequest,
    Uiautomator2Provider,
    format_ui_hierarchy,
    uiautomator2_dependency_status,
)
from .adb_agent_prompts import (
    ADB_AGENT_SYSTEM_PROMPT_VERSION,
    DEFAULT_ADB_AGENT_SYSTEM_PROMPT,
    finish_message_contradiction,
    task_templates_snapshot,
)


MODEL_PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
MODEL_PROVIDER_ANTHROPIC = "anthropic"
MODEL_PROVIDER_GEMINI = "gemini"
SUPPORTED_MODEL_PROVIDERS = {
    MODEL_PROVIDER_OPENAI_COMPATIBLE,
    MODEL_PROVIDER_ANTHROPIC,
    MODEL_PROVIDER_GEMINI,
}
DEFAULT_MODEL_PROVIDER = (
    os.environ.get("MOBILE_PROFILER_MODEL_PROVIDER")
    or os.environ.get("BTR2_LLM_PROVIDER")
    or MODEL_PROVIDER_OPENAI_COMPATIBLE
).strip().lower()
if DEFAULT_MODEL_PROVIDER not in SUPPORTED_MODEL_PROVIDERS:
    DEFAULT_MODEL_PROVIDER = MODEL_PROVIDER_OPENAI_COMPATIBLE
DEFAULT_MODEL_API_BASE_URL = (
    os.environ.get("MOBILE_PROFILER_MODEL_ENDPOINT")
    or os.environ.get("BTR2_LLM_ENDPOINT")
    or "http://192.168.31.237:8000"
).strip()
DEFAULT_MODEL = (
    os.environ.get("MOBILE_PROFILER_MODEL_NAME")
    or os.environ.get("BTR2_LLM_MODEL")
    or "qwen3.6-27b"
).strip()
DEFAULT_MODEL_API_KEY = (
    os.environ.get("MOBILE_PROFILER_MODEL_API_KEY")
    or os.environ.get("BTR2_LLM_TOKEN")
    or ""
).strip()
MODEL_THINKING_AUTO = "auto"
MODEL_THINKING_DISABLED = "disabled"
MODEL_THINKING_ENABLED = "enabled"
SUPPORTED_MODEL_THINKING_MODES = {
    MODEL_THINKING_AUTO,
    MODEL_THINKING_DISABLED,
    MODEL_THINKING_ENABLED,
}
_configured_thinking_mode = os.environ.get("MOBILE_PROFILER_MODEL_THINKING_MODE")
if _configured_thinking_mode:
    DEFAULT_MODEL_THINKING_MODE = _configured_thinking_mode.strip().lower()
elif (
    DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_OPENAI_COMPATIBLE
    and re.search(r"(?:^|[/_.-])(qwen|deepseek)", DEFAULT_MODEL, re.IGNORECASE)
):
    DEFAULT_MODEL_THINKING_MODE = MODEL_THINKING_DISABLED
else:
    DEFAULT_MODEL_THINKING_MODE = MODEL_THINKING_AUTO
if DEFAULT_MODEL_THINKING_MODE not in SUPPORTED_MODEL_THINKING_MODES:
    DEFAULT_MODEL_THINKING_MODE = MODEL_THINKING_AUTO

AUTOMATION_ENGINE_VISION = "vision"
AUTOMATION_ENGINE_UIAUTOMATOR2 = "uiautomator2"
AUTOMATION_ENGINE_HYBRID = "hybrid"
SUPPORTED_AUTOMATION_ENGINES = {
    AUTOMATION_ENGINE_VISION,
    AUTOMATION_ENGINE_UIAUTOMATOR2,
    AUTOMATION_ENGINE_HYBRID,
}
DEFAULT_AUTOMATION_ENGINE = (
    os.environ.get("MOBILE_PROFILER_AUTOMATION_ENGINE")
    or AUTOMATION_ENGINE_VISION
).strip().lower()
if DEFAULT_AUTOMATION_ENGINE not in SUPPORTED_AUTOMATION_ENGINES:
    DEFAULT_AUTOMATION_ENGINE = AUTOMATION_ENGINE_VISION

# Backward-compatible configuration exports.
DEFAULT_BTR2_API_BASE_URL = DEFAULT_MODEL_API_BASE_URL
DEFAULT_BTR2_MODEL = DEFAULT_MODEL
DEFAULT_BTR2_API_KEY = DEFAULT_MODEL_API_KEY
MAX_AGENT_LOGS = 240
MAX_AGENT_HISTORY = 20
MAX_AGENT_TASKS = 100
MAX_AGENT_REASONING_CHARS = 120
MAX_AGENT_FINISH_MESSAGE_CHARS = 80
MAX_AGENT_TAKE_OVER_MESSAGE_CHARS = 120
MAX_AGENT_SKIP_MESSAGE_CHARS = 120
MAX_OPENAI_COMPATIBLE_OUTPUT_TOKENS = 1000
MAX_OPENAI_COMPATIBLE_REPAIR_TOKENS = 512
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")

# Backward-compatible export for callers that imported the original constant.
ADB_AGENT_SYSTEM_PROMPT = DEFAULT_ADB_AGENT_SYSTEM_PROMPT


PHONE_ACTION_TOOL: Dict[str, object] = {
    "type": "function",
    "function": {
        "name": "phone_action",
        "description": (
            "Perform exactly one validated Android UI action through ADB. "
            "Coordinates use the normalized 0-999 screenshot space."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "tap",
                        "double_tap",
                        "long_press",
                        "swipe",
                        "swipe_fast",
                        "back",
                        "home",
                        "recent",
                        "wake",
                        "enter",
                        "delete",
                        "input_text",
                        "input_secret",
                        "launch_app",
                        "wait",
                        "finish",
                        "skip",
                        "take_over",
                    ],
                },
                "element": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 999},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "start": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 999},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "end": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 999},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "duration_ms": {
                    "type": "integer",
                    "minimum": 50,
                    "maximum": 5000,
                },
                "duration_seconds": {
                    "type": "number",
                    "minimum": 0.2,
                    "maximum": 30,
                },
                "text": {"type": "string", "maxLength": 500},
                "secret_id": {
                    "type": "string",
                    "pattern": "^[A-Z][A-Z0-9_]{0,63}$",
                    "maxLength": 64,
                },
                "package": {"type": "string", "maxLength": 200},
                "message": {
                    "type": "string",
                    "maxLength": MAX_AGENT_TAKE_OVER_MESSAGE_CHARS,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}

# ``tap_element`` is injected only for semantic/hybrid engines, but task-level
# action limits must be validated independently of the selected engine.
SUPPORTED_PHONE_ACTIONS = frozenset(
    {
        "tap",
        "tap_element",
        "double_tap",
        "long_press",
        "swipe",
        "swipe_fast",
        "back",
        "home",
        "recent",
        "wake",
        "enter",
        "delete",
        "input_text",
        "input_secret",
        "launch_app",
        "wait",
        "finish",
        "skip",
        "take_over",
    }
)


def automation_engine_definitions_snapshot() -> List[Dict[str, object]]:
    uia_available, uia_detail = uiautomator2_dependency_status()
    return [
        {
            "id": AUTOMATION_ENGINE_VISION,
            "label": "视觉截图",
            "description": "模型比较前后 ADB 截图并使用归一化坐标操作",
            "available": True,
            "detail": "内置 ADB screencap + 坐标动作",
        },
        {
            "id": AUTOMATION_ENGINE_UIAUTOMATOR2,
            "label": "uiautomator2 语义控件",
            "description": "模型读取控件树并按 revision 绑定的元素编号操作；不向模型发送截图",
            "available": uia_available,
            "detail": uia_detail,
        },
        {
            "id": AUTOMATION_ENGINE_HYBRID,
            "label": "视觉 + uiautomator2",
            "description": "模型同时比较截图与控件树；语义元素缺失时可使用视觉坐标",
            "available": uia_available,
            "detail": uia_detail,
        },
    ]


def normalize_automation_engine(value: object) -> str:
    text = str(value or DEFAULT_AUTOMATION_ENGINE).strip().lower().replace("-", "_")
    aliases = {
        "adb": AUTOMATION_ENGINE_VISION,
        "screenshot": AUTOMATION_ENGINE_VISION,
        "visual": AUTOMATION_ENGINE_VISION,
        "uia": AUTOMATION_ENGINE_UIAUTOMATOR2,
        "ui_automator2": AUTOMATION_ENGINE_UIAUTOMATOR2,
        "semantic": AUTOMATION_ENGINE_UIAUTOMATOR2,
        "combined": AUTOMATION_ENGINE_HYBRID,
        "mixed": AUTOMATION_ENGINE_HYBRID,
        "vision_uiautomator2": AUTOMATION_ENGINE_HYBRID,
    }
    normalized = aliases.get(text, text)
    if normalized not in SUPPORTED_AUTOMATION_ENGINES:
        raise ValueError(f"不支持的手机操作引擎：{value}")
    return normalized


def phone_action_tool(automation_engine: object = AUTOMATION_ENGINE_VISION) -> Dict[str, object]:
    engine = normalize_automation_engine(automation_engine)
    tool = deepcopy(PHONE_ACTION_TOOL)
    function = tool.get("function")
    if not isinstance(function, dict):
        raise RuntimeError("phone_action schema is invalid")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise RuntimeError("phone_action parameter schema is invalid")
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("phone_action properties are invalid")
    action = properties.get("action")
    if not isinstance(action, dict) or not isinstance(action.get("enum"), list):
        raise RuntimeError("phone_action action enum is invalid")
    if engine in {AUTOMATION_ENGINE_UIAUTOMATOR2, AUTOMATION_ENGINE_HYBRID}:
        action["enum"] = ["tap_element", *action["enum"]]
        properties["element_id"] = {
            "type": "string",
            "pattern": "^e[0-9]+$",
            "maxLength": 32,
        }
        properties["observation_revision"] = {
            "type": "string",
            "maxLength": 80,
        }
        function["description"] = (
            "Perform exactly one validated Android UI action through uiautomator2. "
            "Prefer tap_element with the exact current observation_revision; "
            "normalized 0-999 coordinates remain available as a fallback."
        )
    return tool


MODEL_PROVIDER_DEFINITIONS: List[Dict[str, str]] = [
    {
        "id": MODEL_PROVIDER_OPENAI_COMPATIBLE,
        "label": "OpenAI-compatible",
        "description": "OpenAI、Azure 完整端点、vLLM、Ollama 与兼容网关；当前默认局域网千问",
        "default_api_base_url": (
            DEFAULT_MODEL_API_BASE_URL
            if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_OPENAI_COMPATIBLE
            else "https://api.openai.com"
        ),
        "default_model": (
            DEFAULT_MODEL
            if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_OPENAI_COMPATIBLE
            else ""
        ),
        "api_placeholder": "http://192.168.31.237:8000 或完整 chat/completions URL",
        "model_placeholder": "支持图像和工具调用的模型名称",
        "default_api_key_mode": "bearer",
        "default_thinking_mode": (
            DEFAULT_MODEL_THINKING_MODE
            if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_OPENAI_COMPATIBLE
            else MODEL_THINKING_AUTO
        ),
    },
    {
        "id": MODEL_PROVIDER_ANTHROPIC,
        "label": "Anthropic Claude",
        "description": "Anthropic Messages API 原生图像与 tool_use",
        "default_api_base_url": (
            DEFAULT_MODEL_API_BASE_URL
            if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_ANTHROPIC
            else "https://api.anthropic.com"
        ),
        "default_model": (
            DEFAULT_MODEL if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_ANTHROPIC else ""
        ),
        "api_placeholder": "https://api.anthropic.com",
        "model_placeholder": "支持视觉与工具调用的 Claude 模型",
        "default_api_key_mode": "x-api-key",
        "default_thinking_mode": MODEL_THINKING_AUTO,
    },
    {
        "id": MODEL_PROVIDER_GEMINI,
        "label": "Google Gemini",
        "description": "Google Generative Language generateContent 原生函数调用",
        "default_api_base_url": (
            DEFAULT_MODEL_API_BASE_URL
            if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_GEMINI
            else "https://generativelanguage.googleapis.com/v1beta"
        ),
        "default_model": (
            DEFAULT_MODEL if DEFAULT_MODEL_PROVIDER == MODEL_PROVIDER_GEMINI else ""
        ),
        "api_placeholder": "https://generativelanguage.googleapis.com/v1beta",
        "model_placeholder": "支持视觉与函数调用的 Gemini 模型",
        "default_api_key_mode": "x-goog-api-key",
        "default_thinking_mode": MODEL_THINKING_AUTO,
    },
]


def model_provider_definitions_snapshot() -> List[Dict[str, str]]:
    return [dict(item) for item in MODEL_PROVIDER_DEFINITIONS]


def model_provider_definition(provider: str) -> Dict[str, str]:
    normalized = normalize_model_provider(provider)
    for item in MODEL_PROVIDER_DEFINITIONS:
        if item["id"] == normalized:
            return item
    raise ValueError(f"不支持的多模态模型协议：{provider}")


@dataclass
class ModelDecision:
    action: Dict[str, object]
    reasoning: str = ""
    content: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None


@dataclass
class ActionExecution:
    summary: str
    message: str = ""
    terminal_status: Optional[str] = None


def _integer(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _number(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bounded_int(value: object, minimum: int, maximum: int, default: int) -> int:
    return max(minimum, min(maximum, _integer(value, default)))


def _bounded_float(value: object, minimum: float, maximum: float, default: float) -> float:
    return max(minimum, min(maximum, _number(value, default)))


def _short_text(value: object, limit: int = 1000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _multiline_text(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        raise ValueError(f"文本不能超过 {limit} 个字符")
    return text


def normalize_agent_tasks(payload: Dict[str, object]) -> List[Dict[str, object]]:
    """Validate the orchestration task list, with legacy single-task support."""

    raw_tasks = payload.get("tasks")
    legacy = raw_tasks is None
    if legacy:
        raw_tasks = [
            {
                "name": str(payload.get("task_name") or "任务 1"),
                "prompt": payload.get("task"),
                "attention_prompt": payload.get("attention_prompt"),
                "max_steps": payload.get("max_steps"),
                "timeout_s": payload.get("task_timeout_s"),
                "on_failure": payload.get("on_failure"),
            }
        ]
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("请至少编排一个测试任务")
    if len(raw_tasks) > MAX_AGENT_TASKS:
        raise ValueError(f"测试任务不能超过 {MAX_AGENT_TASKS} 个")

    default_max_steps = _bounded_int(payload.get("max_steps"), 1, 200, 30)
    normalized: List[Dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, raw_task in enumerate(raw_tasks, 1):
        if not isinstance(raw_task, dict):
            raise ValueError(f"第 {index} 个测试任务必须是 JSON 对象")
        prompt = _multiline_text(raw_task.get("prompt"), 6000)
        if not prompt:
            raise ValueError(f"第 {index} 个测试任务缺少任务目标")
        attention_prompt = _multiline_text(raw_task.get("attention_prompt"), 3000)
        name = _short_text(
            raw_task.get("name") or raw_task.get("label") or f"任务 {index}",
            120,
        )
        raw_id = str(raw_task.get("id") or f"task-{index}").strip().lower()
        task_id = re.sub(r"[^a-z0-9._-]+", "-", raw_id).strip("-.")[:64]
        if not task_id:
            task_id = f"task-{index}"
        base_id = task_id
        suffix = 2
        while task_id in seen_ids:
            task_id = f"{base_id[: max(1, 61 - len(str(suffix)))]}-{suffix}"
            suffix += 1
        seen_ids.add(task_id)
        on_failure = str(raw_task.get("on_failure") or "stop").strip().lower()
        if on_failure not in {"stop", "continue"}:
            raise ValueError(f"第 {index} 个测试任务的失败策略必须是 stop 或 continue")
        raw_action_limits = raw_task.get("action_limits")
        if raw_action_limits is None:
            raw_action_limits = []
        if not isinstance(raw_action_limits, list):
            raise ValueError(f"第 {index} 个测试任务的 action_limits 必须是数组")
        action_limits: List[Dict[str, object]] = []
        for limit_index, raw_limit in enumerate(raw_action_limits, 1):
            if not isinstance(raw_limit, dict):
                raise ValueError(
                    f"第 {index} 个测试任务的第 {limit_index} 个动作上限必须是对象"
                )
            raw_actions = raw_limit.get("actions")
            if not isinstance(raw_actions, list) or not raw_actions:
                raise ValueError(
                    f"第 {index} 个测试任务的第 {limit_index} 个动作上限缺少 actions"
                )
            actions: List[str] = []
            for raw_action in raw_actions:
                action_name = str(raw_action or "").strip().lower()
                if action_name not in SUPPORTED_PHONE_ACTIONS:
                    raise ValueError(
                        f"第 {index} 个测试任务的动作上限包含不支持的动作：{raw_action}"
                    )
                if action_name not in actions:
                    actions.append(action_name)
            raw_maximum = raw_limit.get("maximum", 1)
            try:
                maximum = int(raw_maximum)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"第 {index} 个测试任务的第 {limit_index} 个 maximum 必须是整数"
                ) from exc
            if maximum < 0 or maximum > 200:
                raise ValueError(
                    f"第 {index} 个测试任务的第 {limit_index} 个 maximum 必须在 0 到 200 之间"
                )
            raw_maximum_per_signature = raw_limit.get("maximum_per_signature")
            maximum_per_signature: Optional[int] = None
            if raw_maximum_per_signature is not None:
                try:
                    maximum_per_signature = int(raw_maximum_per_signature)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"第 {index} 个测试任务的第 {limit_index} 个 "
                        "maximum_per_signature 必须是整数"
                    ) from exc
                if maximum_per_signature <= 0 or maximum_per_signature > 200:
                    raise ValueError(
                        f"第 {index} 个测试任务的第 {limit_index} 个 "
                        "maximum_per_signature 必须在 1 到 200 之间"
                    )
            normalized_limit: Dict[str, object] = {
                "actions": actions,
                "maximum": maximum,
                "label": _short_text(
                    raw_limit.get("label") or "/".join(actions),
                    120,
                ),
            }
            if maximum_per_signature is not None:
                normalized_limit["maximum_per_signature"] = maximum_per_signature
            action_limits.append(normalized_limit)
        raw_finish_requirements = raw_task.get("finish_action_requirements")
        if raw_finish_requirements is None:
            raw_finish_requirements = []
        if not isinstance(raw_finish_requirements, list):
            raise ValueError(
                f"第 {index} 个测试任务的 finish_action_requirements 必须是数组"
            )
        finish_action_requirements: List[Dict[str, object]] = []
        for requirement_index, raw_requirement in enumerate(
            raw_finish_requirements,
            1,
        ):
            if not isinstance(raw_requirement, dict):
                raise ValueError(
                    f"第 {index} 个测试任务的第 {requirement_index} 个 finish 动作要求必须是对象"
                )
            raw_actions = raw_requirement.get("actions")
            if not isinstance(raw_actions, list) or not raw_actions:
                raise ValueError(
                    f"第 {index} 个测试任务的第 {requirement_index} 个 finish 动作要求缺少 actions"
                )
            actions: List[str] = []
            for raw_action in raw_actions:
                action_name = str(raw_action or "").strip().lower()
                if action_name not in SUPPORTED_PHONE_ACTIONS:
                    raise ValueError(
                        f"第 {index} 个测试任务的 finish 动作要求包含不支持的动作：{raw_action}"
                    )
                if action_name not in actions:
                    actions.append(action_name)
            raw_minimum = raw_requirement.get("minimum", 1)
            try:
                minimum = int(raw_minimum)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"第 {index} 个测试任务的第 {requirement_index} 个 minimum 必须是整数"
                ) from exc
            if minimum <= 0 or minimum > 200:
                raise ValueError(
                    f"第 {index} 个测试任务的第 {requirement_index} 个 minimum 必须在 1 到 200 之间"
                )
            finish_action_requirements.append(
                {
                    "actions": actions,
                    "minimum": minimum,
                    "label": _short_text(
                        raw_requirement.get("label") or "/".join(actions),
                        120,
                    ),
                }
            )
        normalized.append(
            {
                "id": task_id,
                "name": name or f"任务 {index}",
                "prompt": prompt,
                "attention_prompt": attention_prompt,
                "max_steps": _bounded_int(
                    raw_task.get("max_steps"), 1, 200, default_max_steps
                ),
                "timeout_s": _bounded_float(
                    raw_task.get("timeout_s"), 5.0, 7200.0, 300.0
                ),
                "on_failure": on_failure,
                "action_limits": action_limits,
                "finish_action_requirements": finish_action_requirements,
            }
        )
    return normalized


def _workflow_summary(results: Sequence[Dict[str, object]]) -> str:
    if not results:
        return "尚无已完成的子任务。"
    lines = []
    for result in results[-12:]:
        lines.append(
            f"{result.get('index')}. {result.get('name')} - "
            f"{result.get('status')}: {_short_text(result.get('message'), 160)}"
        )
    return "\n".join(lines)


def _device_identity_attention(config: Dict[str, object]) -> str:
    fields = [
        ("品牌", _short_text(config.get("device_brand"), 80)),
        ("型号", _short_text(config.get("device_model"), 120)),
        ("产品", _short_text(config.get("device_product"), 120)),
        ("代号", _short_text(config.get("device_codename"), 120)),
    ]
    identity = "，".join(f"{label} {value}" for label, value in fields if value)
    if not identity:
        return ""
    return (
        f"当前真机设备标识（宿主已通过 ADB 确认）：{identity}。"
        "选择厂商专属应用、应用商店包名或系统设置路径时必须以此为准；"
        "禁止猜测或尝试其他厂商的包名。"
    )


def _normalize_input_secrets(value: object) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("input_secrets must be an object of secret aliases")
    normalized: Dict[str, str] = {}
    for raw_name, raw_secret in value.items():
        name = str(raw_name or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
            raise ValueError(
                "input secret aliases must use uppercase letters, numbers, and underscores"
            )
        secret = str(raw_secret or "")
        if not secret or len(secret) > 500 or any(ord(character) < 32 for character in secret):
            raise ValueError(f"input secret {name} is empty or contains unsupported characters")
        normalized[name] = secret
    return normalized


def _secret_alias_attention(config: Dict[str, object]) -> str:
    secret_ids = config.get("input_secret_ids")
    if not isinstance(secret_ids, list) or not secret_ids:
        return ""
    aliases = "、".join(str(value) for value in secret_ids)
    return (
        f"宿主为本次会话配置了敏感输入别名：{aliases}。"
        "需要填写对应字段时只能调用 input_secret 并原样使用 secret_id；"
        "不要猜测、复述或使用 input_text 输入这些值。"
    )


def normalize_model_provider(value: object) -> str:
    provider = str(value or DEFAULT_MODEL_PROVIDER).strip().lower().replace("-", "_")
    aliases = {
        "openai": MODEL_PROVIDER_OPENAI_COMPATIBLE,
        "openai_compatible": MODEL_PROVIDER_OPENAI_COMPATIBLE,
        "vllm": MODEL_PROVIDER_OPENAI_COMPATIBLE,
        "claude": MODEL_PROVIDER_ANTHROPIC,
        "anthropic": MODEL_PROVIDER_ANTHROPIC,
        "google": MODEL_PROVIDER_GEMINI,
        "gemini": MODEL_PROVIDER_GEMINI,
    }
    normalized = aliases.get(provider, provider)
    if normalized not in SUPPORTED_MODEL_PROVIDERS:
        raise ValueError(f"不支持的多模态模型协议：{value}")
    return normalized


def default_model_thinking_mode(provider: str, model: str) -> str:
    normalized_provider = normalize_model_provider(provider)
    if normalized_provider != MODEL_PROVIDER_OPENAI_COMPATIBLE:
        return MODEL_THINKING_AUTO
    if re.search(r"(?:^|[/_.-])(qwen|deepseek)", str(model or ""), re.IGNORECASE):
        return MODEL_THINKING_DISABLED
    return MODEL_THINKING_AUTO


def normalize_model_thinking_mode(value: object) -> str:
    mode = str(value or MODEL_THINKING_AUTO).strip().lower().replace("-", "_")
    aliases = {
        "auto": MODEL_THINKING_AUTO,
        "default": MODEL_THINKING_AUTO,
        "off": MODEL_THINKING_DISABLED,
        "disable": MODEL_THINKING_DISABLED,
        "disabled": MODEL_THINKING_DISABLED,
        "false": MODEL_THINKING_DISABLED,
        "on": MODEL_THINKING_ENABLED,
        "enable": MODEL_THINKING_ENABLED,
        "enabled": MODEL_THINKING_ENABLED,
        "true": MODEL_THINKING_ENABLED,
    }
    normalized = aliases.get(mode, mode)
    if normalized not in SUPPORTED_MODEL_THINKING_MODES:
        raise ValueError(f"不支持的模型思考模式：{value}")
    return normalized


def _parsed_http_endpoint(api_base_url: str):
    value = str(api_base_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API 地址必须是有效的 http:// 或 https:// URL")
    sensitive_query_keys = {"key", "api_key", "apikey", "token", "access_token"}
    if any(
        name.strip().lower() in sensitive_query_keys
        for name, _value in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError("不要把 API Key 或 Token 写入 URL；请使用独立的 API Key 字段")
    return parsed._replace(path=parsed.path.rstrip("/"), fragment="")


def chat_completions_url(api_base_url: str) -> str:
    """Return an OpenAI-compatible chat-completions URL, preserving queries."""

    parsed = _parsed_http_endpoint(api_base_url)
    path = parsed.path
    if not path.endswith("/chat/completions"):
        path = (
            path + "/chat/completions"
            if path.endswith("/v1")
            else path + "/v1/chat/completions"
        )
    return urlunparse(parsed._replace(path=path))


def anthropic_messages_url(api_base_url: str) -> str:
    parsed = _parsed_http_endpoint(api_base_url)
    path = parsed.path
    if not path.endswith("/messages"):
        path = path + "/messages" if path.endswith("/v1") else path + "/v1/messages"
    return urlunparse(parsed._replace(path=path))


def gemini_generate_content_url(api_base_url: str, model: str) -> str:
    parsed = _parsed_http_endpoint(api_base_url)
    path = parsed.path
    if not path.endswith(":generateContent"):
        model_name = str(model or "").strip()
        if model_name.startswith("models/"):
            model_name = model_name[len("models/") :]
        if not model_name:
            raise ValueError("模型名称不能为空")
        encoded_model = quote(model_name, safe="._-/")
        if path.endswith("/models"):
            path = f"{path}/{encoded_model}:generateContent"
        elif re.search(r"/v\d+(?:beta\d*)?$", path):
            path = f"{path}/models/{encoded_model}:generateContent"
        else:
            path = f"{path}/v1beta/models/{encoded_model}:generateContent"
    return urlunparse(parsed._replace(path=path))


def model_endpoint_url(provider: str, api_base_url: str, model: str) -> str:
    normalized = normalize_model_provider(provider)
    if normalized == MODEL_PROVIDER_ANTHROPIC:
        return anthropic_messages_url(api_base_url)
    if normalized == MODEL_PROVIDER_GEMINI:
        return gemini_generate_content_url(api_base_url, model)
    return chat_completions_url(api_base_url)


def png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or not payload.startswith(PNG_SIGNATURE):
        raise RuntimeError("ADB screencap did not return a valid PNG image")
    width, height = struct.unpack(">II", payload[16:24])
    if width <= 0 or height <= 0 or width > 20000 or height > 20000:
        raise RuntimeError("ADB screencap returned invalid image dimensions")
    return width, height


def _json_object_from_text(content: str) -> Optional[Dict[str, object]]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match and match.group(0) != text:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    texts: List[str] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(str(item["text"]))
    return "\n".join(texts)


def parse_openai_model_decision(payload: Dict[str, object]) -> ModelDecision:
    """Parse one native ``phone_action`` call from Chat Completions JSON."""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("模型响应缺少 choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("模型响应缺少 message")
    reasoning = _short_text(
        message.get("reasoning_content") or message.get("reasoning") or "",
        MAX_AGENT_REASONING_CHARS,
    )
    reasoning_content = str(
        message.get("reasoning_content") or message.get("reasoning") or ""
    )
    content = _content_text(message.get("content"))
    action: Optional[Dict[str, object]] = None
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict) or function.get("name") != "phone_action":
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                action = dict(arguments)
            elif isinstance(arguments, str):
                decoded = _json_object_from_text(arguments)
                if decoded is not None:
                    action = decoded
            if action is not None:
                break
    if action is None:
        for candidate in (content, reasoning_content):
            fallback = _json_object_from_text(candidate)
            if fallback is None:
                continue
            if fallback.get("name") == "phone_action" and isinstance(
                fallback.get("arguments"), dict
            ):
                action = dict(fallback["arguments"])  # type: ignore[arg-type]
            elif "action" in fallback:
                action = fallback
            if action is not None:
                break
    if action is None or not str(action.get("action") or "").strip():
        raise RuntimeError("模型没有返回 phone_action 工具调用")
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return ModelDecision(
        action=action,
        reasoning=reasoning,
        content=_short_text(content, 3000),
        prompt_tokens=(
            _integer(usage.get("prompt_tokens"), 0)
            if usage.get("prompt_tokens") is not None
            else None
        ),
        completion_tokens=(
            _integer(usage.get("completion_tokens"), 0)
            if usage.get("completion_tokens") is not None
            else None
        ),
    )


def parse_model_decision(payload: Dict[str, object]) -> ModelDecision:
    """Backward-compatible alias for the OpenAI-compatible response parser."""

    return parse_openai_model_decision(payload)


def parse_anthropic_model_decision(payload: Dict[str, object]) -> ModelDecision:
    content_blocks = payload.get("content")
    if not isinstance(content_blocks, list):
        raise RuntimeError("Anthropic 响应缺少 content")
    action: Optional[Dict[str, object]] = None
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "tool_use" and block.get("name") == "phone_action":
            tool_input = block.get("input")
            if isinstance(tool_input, dict) and action is None:
                action = dict(tool_input)
        elif block_type in {"thinking", "redacted_thinking"}:
            thinking = block.get("thinking") or block.get("text")
            if isinstance(thinking, str):
                reasoning_parts.append(thinking)
        elif block_type == "text" and isinstance(block.get("text"), str):
            text_parts.append(str(block["text"]))
    content = "\n".join(text_parts)
    if action is None:
        fallback = _json_object_from_text(content)
        if fallback is not None and "action" in fallback:
            action = fallback
    if action is None or not str(action.get("action") or "").strip():
        raise RuntimeError("Anthropic 模型没有返回 phone_action 工具调用")
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return ModelDecision(
        action=action,
        reasoning=_short_text(
            "\n".join(reasoning_parts) or content,
            MAX_AGENT_REASONING_CHARS,
        ),
        content=_short_text(content, 3000),
        prompt_tokens=(
            _integer(usage.get("input_tokens"), 0)
            if usage.get("input_tokens") is not None
            else None
        ),
        completion_tokens=(
            _integer(usage.get("output_tokens"), 0)
            if usage.get("output_tokens") is not None
            else None
        ),
    )


def parse_gemini_model_decision(payload: Dict[str, object]) -> ModelDecision:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise RuntimeError("Gemini 响应缺少 candidates[0]")
    content = candidates[0].get("content")
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        raise RuntimeError("Gemini 响应缺少 content.parts")
    action: Optional[Dict[str, object]] = None
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    for part in content["parts"]:  # type: ignore[index]
        if not isinstance(part, dict):
            continue
        function_call = part.get("functionCall") or part.get("function_call")
        if isinstance(function_call, dict) and function_call.get("name") == "phone_action":
            arguments = function_call.get("args") or function_call.get("arguments")
            if isinstance(arguments, dict) and action is None:
                action = dict(arguments)
        text = part.get("text")
        if isinstance(text, str):
            text_parts.append(text)
            if part.get("thought") is True:
                reasoning_parts.append(text)
    content_text = "\n".join(text_parts)
    if action is None:
        fallback = _json_object_from_text(content_text)
        if fallback is not None and "action" in fallback:
            action = fallback
    if action is None or not str(action.get("action") or "").strip():
        raise RuntimeError("Gemini 模型没有返回 phone_action 函数调用")
    usage = payload.get("usageMetadata") or payload.get("usage_metadata")
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = usage.get("promptTokenCount")
    if prompt_tokens is None:
        prompt_tokens = usage.get("prompt_token_count")
    completion_tokens = usage.get("candidatesTokenCount")
    if completion_tokens is None:
        completion_tokens = usage.get("candidates_token_count")
    return ModelDecision(
        action=action,
        reasoning=_short_text(
            "\n".join(reasoning_parts) or content_text,
            MAX_AGENT_REASONING_CHARS,
        ),
        content=_short_text(content_text, 3000),
        prompt_tokens=(
            _integer(prompt_tokens, 0) if prompt_tokens is not None else None
        ),
        completion_tokens=(
            _integer(completion_tokens, 0) if completion_tokens is not None else None
        ),
    )


def _phone_action_function(
    automation_engine: object = AUTOMATION_ENGINE_VISION,
) -> Dict[str, object]:
    function = phone_action_tool(automation_engine).get("function")
    if not isinstance(function, dict):
        raise RuntimeError("phone_action schema is invalid")
    return function


def _history_text(history: Sequence[Dict[str, object]]) -> str:
    if not history:
        return "尚无历史动作。"
    lines: List[str] = []
    for item in history[-12:]:
        action_text = json.dumps(
            item.get("action"), ensure_ascii=False, separators=(",", ":")
        )
        result = _short_text(item.get("result"), 180)
        lines.append(f"步骤 {item.get('step')}: {action_text} -> {result}")
    return "\n".join(lines)


def _valid_action_counts(history: Sequence[Dict[str, object]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in history:
        if item.get("action_valid") is False:
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        name = str(action.get("action") or "").strip().lower()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _action_limits_attention(
    action_limits: object,
    history: Sequence[Dict[str, object]],
) -> str:
    if not isinstance(action_limits, list) or not action_limits:
        return ""
    counts = _valid_action_counts(history)
    lines = ["宿主动作上限（硬约束，超过上限的动作不会执行）："]
    for limit in action_limits:
        if not isinstance(limit, dict):
            continue
        actions = [
            str(action).strip().lower()
            for action in limit.get("actions", [])
            if str(action).strip()
        ]
        maximum = _integer(limit.get("maximum"), 0)
        observed = sum(counts.get(action, 0) for action in actions)
        label = _short_text(limit.get("label") or "/".join(actions), 120)
        signature_limit = _integer(limit.get("maximum_per_signature"), 0)
        signature_note = (
            f"；同一动作参数最多 {signature_limit} 次"
            if signature_limit > 0
            else ""
        )
        lines.append(
            f"- {label}：已执行 {observed}/{maximum}（{', '.join(actions)}）"
            f"{signature_note}"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def _action_limit_violation(
    action_limits: object,
    history: Sequence[Dict[str, object]],
    action: Dict[str, object],
) -> str:
    if not isinstance(action_limits, list) or not action_limits:
        return ""
    action_name = str(action.get("action") or "").strip().lower()
    if not action_name:
        return ""
    counts = _valid_action_counts(history)
    for limit in action_limits:
        if not isinstance(limit, dict):
            continue
        actions = {
            str(item).strip().lower()
            for item in limit.get("actions", [])
            if str(item).strip()
        }
        if action_name not in actions:
            continue
        maximum = _integer(limit.get("maximum"), 0)
        observed = sum(counts.get(item, 0) for item in actions)
        if maximum >= 0 and observed >= maximum:
            label = _short_text(limit.get("label") or "/".join(sorted(actions)), 120)
            return f"动作上限已达到：{label} 已执行 {observed}/{maximum}，本次 {action_name} 被拒绝"
        maximum_per_signature = _integer(limit.get("maximum_per_signature"), 0)
        if maximum_per_signature > 0:
            signature = _repeatable_action_signature(action)
            signature_count = sum(
                1
                for item in history
                if item.get("action_valid") is not False
                and isinstance(item.get("action"), dict)
                and _repeatable_action_signature(item["action"]) == signature
            )
            if signature and signature_count >= maximum_per_signature:
                label = _short_text(
                    limit.get("label") or "/".join(sorted(actions)),
                    120,
                )
                return (
                    f"相同动作参数上限已达到：{label} 的当前 {action_name} "
                    f"已执行 {signature_count}/{maximum_per_signature}，本次动作被拒绝"
                )
    return ""


def _finish_requirements_attention(
    requirements: object,
    history: Sequence[Dict[str, object]],
) -> str:
    if not isinstance(requirements, list) or not requirements:
        return ""
    counts = _valid_action_counts(history)
    lines = ["finish 前置动作（宿主硬校验，未满足时 finish 不会执行）："]
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        actions = [
            str(action).strip().lower()
            for action in requirement.get("actions", [])
            if str(action).strip()
        ]
        minimum = _integer(requirement.get("minimum"), 0)
        observed = sum(counts.get(action, 0) for action in actions)
        label = _short_text(requirement.get("label") or "/".join(actions), 120)
        lines.append(f"- {label}：已执行 {observed}/{minimum}（{', '.join(actions)}）")
    return "\n".join(lines) if len(lines) > 1 else ""


def _finish_requirement_violation(
    requirements: object,
    history: Sequence[Dict[str, object]],
) -> str:
    if not isinstance(requirements, list) or not requirements:
        return ""
    counts = _valid_action_counts(history)
    missing: List[str] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        actions = [
            str(action).strip().lower()
            for action in requirement.get("actions", [])
            if str(action).strip()
        ]
        minimum = _integer(requirement.get("minimum"), 0)
        observed = sum(counts.get(action, 0) for action in actions)
        if observed < minimum:
            label = _short_text(requirement.get("label") or "/".join(actions), 120)
            missing.append(f"{label} {observed}/{minimum}")
    return (
        "finish 前置动作尚未满足：" + "；".join(missing)
        if missing
        else ""
    )


def _repeatable_action_signature(action: Dict[str, object]) -> str:
    name = str(action.get("action") or "").strip().lower()
    if name not in {
        "tap",
        "tap_element",
        "double_tap",
        "long_press",
        "swipe",
        "swipe_fast",
        "back",
        "home",
        "recent",
        "enter",
        "delete",
        "input_text",
        "input_secret",
        "launch_app",
    }:
        return ""
    stable = {
        key: value
        for key, value in action.items()
        if key not in {"message", "observation_revision"}
    }
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _observation_state_token(revision: object) -> str:
    text = str(revision or "").strip()
    return text.rsplit("-", 1)[-1] if "-" in text else text


def _matching_action_state_count(
    history: Sequence[Dict[str, object]],
    action: Dict[str, object],
    observation_revision: object,
) -> int:
    signature = _repeatable_action_signature(action)
    state_token = _observation_state_token(observation_revision)
    if not signature or not state_token:
        return 0
    count = 0
    for item in history[-8:]:
        historical_action = item.get("action")
        if not isinstance(historical_action, dict):
            continue
        if (
            _observation_state_token(item.get("observation_revision")) == state_token
            and _repeatable_action_signature(historical_action) == signature
        ):
            count += 1
    return count


def build_agent_user_text(
    *,
    task: str,
    step: int,
    max_steps: int,
    width: int,
    height: int,
    history: Sequence[Dict[str, object]],
    task_name: str = "",
    attention_prompt: str = "",
    workflow_summary: str = "",
    task_elapsed_s: float = 0.0,
    task_timeout_s: float = 0.0,
    previous_frame_available: bool = False,
    automation_engine: str = AUTOMATION_ENGINE_VISION,
    ui_hierarchy_text: str = "",
    previous_ui_hierarchy_text: str = "",
    observation_revision: str = "",
) -> str:
    engine = normalize_automation_engine(automation_engine)
    attention_block = (
        f"\n\n当前子任务注意事项：\n{attention_prompt}"
        if str(attention_prompt or "").strip()
        else ""
    )
    timeout_block = (
        f"{task_elapsed_s:.1f}/{task_timeout_s:.1f} 秒"
        if task_timeout_s > 0
        else f"{task_elapsed_s:.1f} 秒"
    )
    if engine == AUTOMATION_ENGINE_HYBRID:
        semantic_current = str(ui_hierarchy_text or "").strip() or "当前没有可用语义控件。"
        semantic_previous = str(previous_ui_hierarchy_text or "").strip()
        frame_block = (
            "本次附带上一动作前和动作后的两张截图。"
            if previous_frame_available
            else "本次只附带最新截图；动作后必须等待下一轮截图再判断变化。"
        )
        evidence_block = (
            "操作引擎：视觉 + uiautomator2 混合辅助。模型同时收到截图与语义控件树。\n"
            f"当前 observation_revision：{observation_revision or 'unknown'}。"
            "有准确语义元素时优先 tap_element 并原样填写当前 revision；"
            "标记为 canvas 的大画布元素只代表整块渲染区域，不能代替内部棋子或按钮；"
            "操作画布内部目标时必须依据截图使用归一化坐标。\n"
            f"截图证据：{frame_block} 必须让截图和语义证据相互校验；冲突时不能 finish。\n\n"
            + (
                f"上一动作前语义树：\n{semantic_previous}\n\n"
                if semantic_previous
                else "上一动作前没有语义树。\n\n"
            )
            + f"最新语义树：\n{semantic_current}"
        )
    elif engine == AUTOMATION_ENGINE_UIAUTOMATOR2:
        semantic_current = str(ui_hierarchy_text or "").strip() or "当前没有可用语义控件。"
        semantic_previous = str(previous_ui_hierarchy_text or "").strip()
        evidence_block = (
            "操作引擎：uiautomator2 语义控件；本轮不向模型发送截图。\n"
            f"当前 observation_revision：{observation_revision or 'unknown'}。"
            "优先使用 tap_element，并原样填写元素编号和当前 observation_revision；"
            "只有语义树没有合适元素时才使用归一化坐标。\n"
            + (
                "本次附带上一动作前和动作后的两份语义树，必须比较文字、选中状态、"
                "控件数量、bounds 或前台 Activity 的变化；没有变化就不能 finish。\n\n"
                f"上一动作前语义树：\n{semantic_previous}\n\n"
                if semantic_previous
                else "本次只有最新语义树；动作后必须等待下一轮语义树再判断变化。\n\n"
            )
            + f"最新语义树：\n{semantic_current}"
        )
    else:
        frame_block = (
            "本次附带两张截图：第一张是上一动作执行前的画面，第二张是执行后的最新画面。"
            "涉及位置、棋子、数值或列表变化时必须逐项比较两张图；目标未变化就不能 finish。"
            if previous_frame_available
            else "本次只附带最新截图；执行动作后必须等待下一轮截图再判断变化。"
        )
        evidence_block = f"操作引擎：视觉截图。\n截图证据：{frame_block}"
    return (
        f"当前测试子任务：{task_name or '未命名任务'}\n"
        f"任务目标：\n{task}{attention_block}\n\n"
        f"当前子任务步骤：{step}/{max_steps}\n"
        f"当前子任务用时：{timeout_block}\n"
        f"ADB 截图尺寸：{width}x{height}；工具坐标仍使用 0-999。\n\n"
        f"{evidence_block}\n\n"
        f"已完成子任务摘要：\n{workflow_summary or '尚无已完成的子任务。'}\n\n"
        f"最近动作与结果：\n{_history_text(history)}\n\n"
        "只处理当前子任务。观察最新截图，调用一次 phone_action；"
        "finish 只表示当前子任务完成。"
    )


def _api_key_headers(provider: str, api_key: str, api_key_mode: str) -> Dict[str, str]:
    key = str(api_key or "").strip()
    if not key or key == "EMPTY":
        return {}
    mode = str(api_key_mode or "auto").strip().lower().replace("_", "-")
    if mode == "auto":
        mode = {
            MODEL_PROVIDER_ANTHROPIC: "x-api-key",
            MODEL_PROVIDER_GEMINI: "x-goog-api-key",
        }.get(provider, "bearer")
    if mode == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if mode in {"api-key", "x-api-key", "x-goog-api-key"}:
        return {mode: key}
    if mode == "none":
        return {}
    raise ValueError(f"不支持的 API Key 认证方式：{api_key_mode}")


def _request_json(
    url: str,
    payload: Dict[str, object],
    headers: Dict[str, str],
    timeout_s: float,
) -> Dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **headers,
    }
    request = Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            response_body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:4000]
        raise RuntimeError(f"模型 API 返回 HTTP {exc.code}: {detail or exc.reason}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"无法连接模型 API: {exc}") from exc
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("模型 API 返回了无效 JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("模型 API 响应必须是 JSON 对象")
    if isinstance(decoded.get("error"), dict):
        raise RuntimeError(
            f"模型 API 错误: {_short_text(decoded['error'].get('message'), 2000)}"
        )
    return decoded


class VisionModelClient:
    provider = "base"

    def __init__(
        self,
        api_base_url: str,
        model: str,
        api_key: str = "",
        request_timeout_s: float = 90.0,
        system_prompt: str = DEFAULT_ADB_AGENT_SYSTEM_PROMPT,
        api_key_mode: str = "auto",
        model_thinking_mode: str = MODEL_THINKING_AUTO,
        automation_engine: str = AUTOMATION_ENGINE_VISION,
    ) -> None:
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("模型名称不能为空")
        self.url = model_endpoint_url(self.provider, api_base_url, self.model)
        self.api_key = str(api_key or "").strip()
        self.api_key_mode = str(api_key_mode or "auto").strip()
        self.model_thinking_mode = normalize_model_thinking_mode(model_thinking_mode)
        self.automation_engine = normalize_automation_engine(automation_engine)
        self.phone_action_tool = phone_action_tool(self.automation_engine)
        self.request_timeout_s = request_timeout_s
        self.system_prompt = str(system_prompt or DEFAULT_ADB_AGENT_SYSTEM_PROMPT).strip()

    def decide(self, **kwargs: object) -> ModelDecision:
        raise NotImplementedError


class OpenAICompatibleVisionClient(VisionModelClient):
    """OpenAI Chat Completions compatible multimodal/tool adapter."""

    provider = MODEL_PROVIDER_OPENAI_COMPATIBLE

    def __init__(
        self,
        api_base_url: str,
        model: str,
        api_key: str = "",
        request_timeout_s: float = 90.0,
        system_prompt: str = DEFAULT_ADB_AGENT_SYSTEM_PROMPT,
        api_key_mode: str = "auto",
        model_thinking_mode: str = MODEL_THINKING_AUTO,
        automation_engine: str = AUTOMATION_ENGINE_VISION,
    ) -> None:
        super().__init__(
            api_base_url,
            model,
            api_key,
            request_timeout_s,
            system_prompt,
            api_key_mode,
            model_thinking_mode,
            automation_engine,
        )

    def decide(
        self,
        *,
        task: str,
        step: int,
        max_steps: int,
        screenshot_png: bytes,
        previous_screenshot_png: bytes = b"",
        width: int,
        height: int,
        history: Sequence[Dict[str, object]],
        task_name: str = "",
        attention_prompt: str = "",
        workflow_summary: str = "",
        task_elapsed_s: float = 0.0,
        task_timeout_s: float = 0.0,
        ui_hierarchy_text: str = "",
        previous_ui_hierarchy_text: str = "",
        observation_revision: str = "",
    ) -> ModelDecision:
        include_images = self.automation_engine != AUTOMATION_ENGINE_UIAUTOMATOR2
        encoded = (
            base64.b64encode(screenshot_png).decode("ascii") if include_images else ""
        )
        previous_encoded = (
            base64.b64encode(previous_screenshot_png).decode("ascii")
            if include_images and previous_screenshot_png
            else ""
        )
        user_text = build_agent_user_text(
            task=task,
            step=step,
            max_steps=max_steps,
            width=width,
            height=height,
            history=history,
            task_name=task_name,
            attention_prompt=attention_prompt,
            workflow_summary=workflow_summary,
            task_elapsed_s=task_elapsed_s,
            task_timeout_s=task_timeout_s,
            previous_frame_available=bool(previous_encoded),
            automation_engine=self.automation_engine,
            ui_hierarchy_text=ui_hierarchy_text,
            previous_ui_hierarchy_text=previous_ui_hierarchy_text,
            observation_revision=observation_revision,
        )
        def message_content(text: str) -> list[dict[str, object]]:
            content: list[dict[str, object]] = [{"type": "text", "text": text}]
            if previous_encoded:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{previous_encoded}"},
                    }
                )
            if encoded:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    }
                )
            return content

        request_payload: Dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": message_content(user_text),
                },
            ],
            "tools": [self.phone_action_tool],
            "tool_choice": {
                "type": "function",
                "function": {"name": "phone_action"},
            },
            "max_tokens": MAX_OPENAI_COMPATIBLE_OUTPUT_TOKENS,
            "stream": False,
        }
        if self.model_thinking_mode != MODEL_THINKING_AUTO:
            request_payload["chat_template_kwargs"] = {
                "enable_thinking": self.model_thinking_mode == MODEL_THINKING_ENABLED
            }
        decoded = _request_json(
            self.url,
            request_payload,
            _api_key_headers(self.provider, self.api_key, self.api_key_mode),
            self.request_timeout_s,
        )
        try:
            return parse_openai_model_decision(decoded)
        except RuntimeError as exc:
            if "没有返回 phone_action" not in str(exc):
                raise
        repair_payload = dict(request_payload)
        repair_payload["messages"] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": message_content(
                    "协议修复：上一响应没有调用 phone_action。"
                    "不要输出正文或分析；现在必须且只能调用一次 phone_action。"
                    "若无法在不违反安全边界的情况下明确选择动作，调用 take_over。\n\n"
                    f"{user_text}"
                ),
            },
        ]
        repair_payload["max_tokens"] = MAX_OPENAI_COMPATIBLE_REPAIR_TOKENS
        repaired = _request_json(
            self.url,
            repair_payload,
            _api_key_headers(self.provider, self.api_key, self.api_key_mode),
            self.request_timeout_s,
        )
        try:
            return parse_openai_model_decision(repaired)
        except RuntimeError as exc:
            if "没有返回 phone_action" not in str(exc):
                raise
        choices = repaired.get("choices")
        repaired_message = (
            choices[0].get("message")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        content = (
            _content_text(repaired_message.get("content"))
            if isinstance(repaired_message, dict)
            else ""
        )
        usage = repaired.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return ModelDecision(
            action={
                "action": "take_over",
                "message": "模型连续两次未返回 phone_action，已按安全策略停止并请求人工接管。",
            },
            content=_short_text(content, 3000),
            prompt_tokens=(
                _integer(usage.get("prompt_tokens"), 0)
                if usage.get("prompt_tokens") is not None
                else None
            ),
            completion_tokens=(
                _integer(usage.get("completion_tokens"), 0)
                if usage.get("completion_tokens") is not None
                else None
            ),
        )


class AnthropicVisionClient(VisionModelClient):
    provider = MODEL_PROVIDER_ANTHROPIC

    def decide(
        self,
        *,
        task: str,
        step: int,
        max_steps: int,
        screenshot_png: bytes,
        previous_screenshot_png: bytes = b"",
        width: int,
        height: int,
        history: Sequence[Dict[str, object]],
        task_name: str = "",
        attention_prompt: str = "",
        workflow_summary: str = "",
        task_elapsed_s: float = 0.0,
        task_timeout_s: float = 0.0,
        ui_hierarchy_text: str = "",
        previous_ui_hierarchy_text: str = "",
        observation_revision: str = "",
    ) -> ModelDecision:
        include_images = self.automation_engine != AUTOMATION_ENGINE_UIAUTOMATOR2
        encoded = (
            base64.b64encode(screenshot_png).decode("ascii") if include_images else ""
        )
        previous_encoded = (
            base64.b64encode(previous_screenshot_png).decode("ascii")
            if include_images and previous_screenshot_png
            else ""
        )
        user_text = build_agent_user_text(
            task=task,
            step=step,
            max_steps=max_steps,
            width=width,
            height=height,
            history=history,
            task_name=task_name,
            attention_prompt=attention_prompt,
            workflow_summary=workflow_summary,
            task_elapsed_s=task_elapsed_s,
            task_timeout_s=task_timeout_s,
            previous_frame_available=bool(previous_encoded),
            automation_engine=self.automation_engine,
            ui_hierarchy_text=ui_hierarchy_text,
            previous_ui_hierarchy_text=previous_ui_hierarchy_text,
            observation_revision=observation_revision,
        )
        function = _phone_action_function(self.automation_engine)
        message_content: list[dict[str, object]] = [{"type": "text", "text": user_text}]
        if previous_encoded:
            message_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": previous_encoded,
                    },
                }
            )
        if encoded:
            message_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": encoded,
                    },
                }
            )
        request_payload: Dict[str, object] = {
            "model": self.model,
            "max_tokens": 1000,
            "temperature": 0.1,
            "system": self.system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": message_content,
                }
            ],
            "tools": [
                {
                    "name": function["name"],
                    "description": function.get("description"),
                    "input_schema": function["parameters"],
                }
            ],
            "tool_choice": {"type": "tool", "name": "phone_action"},
        }
        headers = _api_key_headers(self.provider, self.api_key, self.api_key_mode)
        headers["anthropic-version"] = "2023-06-01"
        decoded = _request_json(
            self.url, request_payload, headers, self.request_timeout_s
        )
        return parse_anthropic_model_decision(decoded)


def _gemini_schema(value: object) -> object:
    if isinstance(value, dict):
        converted: Dict[str, object] = {}
        for key, item in value.items():
            if key == "additionalProperties":
                continue
            if key == "type" and isinstance(item, str):
                converted[key] = item.upper()
            else:
                converted[key] = _gemini_schema(item)
        return converted
    if isinstance(value, list):
        return [_gemini_schema(item) for item in value]
    return value


class GeminiVisionClient(VisionModelClient):
    provider = MODEL_PROVIDER_GEMINI

    def decide(
        self,
        *,
        task: str,
        step: int,
        max_steps: int,
        screenshot_png: bytes,
        previous_screenshot_png: bytes = b"",
        width: int,
        height: int,
        history: Sequence[Dict[str, object]],
        task_name: str = "",
        attention_prompt: str = "",
        workflow_summary: str = "",
        task_elapsed_s: float = 0.0,
        task_timeout_s: float = 0.0,
        ui_hierarchy_text: str = "",
        previous_ui_hierarchy_text: str = "",
        observation_revision: str = "",
    ) -> ModelDecision:
        include_images = self.automation_engine != AUTOMATION_ENGINE_UIAUTOMATOR2
        encoded = (
            base64.b64encode(screenshot_png).decode("ascii") if include_images else ""
        )
        previous_encoded = (
            base64.b64encode(previous_screenshot_png).decode("ascii")
            if include_images and previous_screenshot_png
            else ""
        )
        user_text = build_agent_user_text(
            task=task,
            step=step,
            max_steps=max_steps,
            width=width,
            height=height,
            history=history,
            task_name=task_name,
            attention_prompt=attention_prompt,
            workflow_summary=workflow_summary,
            task_elapsed_s=task_elapsed_s,
            task_timeout_s=task_timeout_s,
            previous_frame_available=bool(previous_encoded),
            automation_engine=self.automation_engine,
            ui_hierarchy_text=ui_hierarchy_text,
            previous_ui_hierarchy_text=previous_ui_hierarchy_text,
            observation_revision=observation_revision,
        )
        function = _phone_action_function(self.automation_engine)
        parts: list[dict[str, object]] = [{"text": user_text}]
        if previous_encoded:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": previous_encoded,
                    }
                }
            )
        if encoded:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": encoded,
                    }
                }
            )
        request_payload: Dict[str, object] = {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": function["name"],
                            "description": function.get("description"),
                            "parameters": _gemini_schema(function["parameters"]),
                        }
                    ]
                }
            ],
            "toolConfig": {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": ["phone_action"],
                }
            },
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1000},
        }
        decoded = _request_json(
            self.url,
            request_payload,
            _api_key_headers(self.provider, self.api_key, self.api_key_mode),
            self.request_timeout_s,
        )
        return parse_gemini_model_decision(decoded)


def create_vision_model_client(config: Dict[str, object]) -> VisionModelClient:
    provider = normalize_model_provider(config.get("model_provider"))
    client_type = {
        MODEL_PROVIDER_OPENAI_COMPATIBLE: OpenAICompatibleVisionClient,
        MODEL_PROVIDER_ANTHROPIC: AnthropicVisionClient,
        MODEL_PROVIDER_GEMINI: GeminiVisionClient,
    }[provider]
    return client_type(
        str(config["api_base_url"]),
        str(config["model"]),
        str(config.get("api_key") or ""),
        _number(config.get("request_timeout_s"), 90.0),
        str(config.get("system_prompt") or DEFAULT_ADB_AGENT_SYSTEM_PROMPT),
        str(config.get("api_key_mode") or "auto"),
        str(config.get("model_thinking_mode") or MODEL_THINKING_AUTO),
        str(config.get("automation_engine") or DEFAULT_AUTOMATION_ENGINE),
    )


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def capture_adb_screenshot(adb: str, device: str) -> tuple[bytes, int, int]:
    try:
        result = subprocess.run(
            [adb, "-s", device, "exec-out", "screencap", "-p"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            creationflags=_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"ADB executable not found: {adb}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ADB screenshot timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "adb exec-out screencap failed")
    width, height = png_dimensions(result.stdout)
    return result.stdout, width, height


def _run_adb_command(adb: str, device: str, command: Sequence[str], timeout_s: float = 20) -> str:
    try:
        result = subprocess.run(
            [adb, "-s", device, *command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
            creationflags=_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"ADB executable not found: {adb}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ADB action timed out: {' '.join(command[:3])}") from exc
    output = "\n".join(
        value.decode("utf-8", errors="replace").strip()
        for value in (result.stdout, result.stderr)
        if value
    ).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"ADB action failed: {' '.join(command[:3])}")
    return output


def _normalized_pair(
    action: Dict[str, object],
    key: str,
    width: int,
    height: int,
) -> tuple[int, int]:
    value = action.get(key)
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        action_name = str(action.get("action") or "").strip().lower()
        if action_name in {"tap", "double_tap", "long_press"}:
            raise ValueError(
                f'{action_name} requires element=[x,y], not start/end; '
                f'example: {{"action":"{action_name}","element":[500,500]}}'
            )
        if action_name in {"swipe", "swipe_fast"}:
            raise ValueError(
                f'{action_name} requires both start=[x1,y1] and end=[x2,y2]; '
                f'example: {{"action":"{action_name}",'
                '"start":[500,800],"end":[500,200]}'
            )
        raise ValueError(f"{action_name or 'phone_action'} requires {key}=[x,y]")
    x = _bounded_int(value[0], 0, 999, 500)
    y = _bounded_int(value[1], 0, 999, 500)
    return (
        int(round(x / 999 * max(0, width - 1))),
        int(round(y / 999 * max(0, height - 1))),
    )


def _terminal_message(action: Dict[str, object], name: str, limit: int) -> str:
    message = _short_text(action.get("message"), limit).strip()
    meaningful = re.findall(r"[A-Za-z0-9\u3400-\u9fff]", message)
    if len(meaningful) < 2:
        raise ValueError(f"{name} requires a meaningful evidence message")
    return message


def execute_adb_action(
    adb: str,
    device: str,
    action: Dict[str, object],
    width: int,
    height: int,
    stop_event: threading.Event,
) -> ActionExecution:
    """Validate and execute one model action without allowing arbitrary shell."""

    name = str(action.get("action") or "").strip().lower()
    if name == "finish":
        message = _terminal_message(action, "finish", MAX_AGENT_FINISH_MESSAGE_CHARS)
        return ActionExecution("模型确认任务完成", message, "completed")
    if name == "skip":
        message = _terminal_message(action, "skip", MAX_AGENT_SKIP_MESSAGE_CHARS)
        return ActionExecution("模型跳过当前检查项", message, "skipped")
    if name == "take_over":
        message = _terminal_message(
            action,
            "take_over",
            MAX_AGENT_TAKE_OVER_MESSAGE_CHARS,
        )
        return ActionExecution("模型请求人工接管", message, "take_over")
    if name == "wait":
        duration = _bounded_float(action.get("duration_seconds"), 0.2, 30.0, 1.0)
        stop_event.wait(duration)
        return ActionExecution(f"等待 {duration:.1f} 秒")
    if name in {"tap", "double_tap", "long_press"}:
        x, y = _normalized_pair(action, "element", width, height)
        if name == "tap":
            _run_adb_command(adb, device, ["shell", "input", "tap", str(x), str(y)])
            return ActionExecution(f"点击 ({x}, {y})")
        if name == "double_tap":
            _run_adb_command(adb, device, ["shell", "input", "tap", str(x), str(y)])
            if not stop_event.wait(0.12):
                _run_adb_command(adb, device, ["shell", "input", "tap", str(x), str(y)])
            return ActionExecution(f"双击 ({x}, {y})")
        duration = _bounded_int(action.get("duration_ms"), 300, 5000, 800)
        _run_adb_command(
            adb,
            device,
            ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration)],
        )
        return ActionExecution(f"长按 ({x}, {y}) {duration} ms")
    if name in {"swipe", "swipe_fast"}:
        start_x, start_y = _normalized_pair(action, "start", width, height)
        end_x, end_y = _normalized_pair(action, "end", width, height)
        default_duration = 220 if name == "swipe_fast" else 600
        duration = _bounded_int(action.get("duration_ms"), 50, 5000, default_duration)
        _run_adb_command(
            adb,
            device,
            [
                "shell",
                "input",
                "swipe",
                str(start_x),
                str(start_y),
                str(end_x),
                str(end_y),
                str(duration),
            ],
        )
        return ActionExecution(
            f"滑动 ({start_x}, {start_y}) → ({end_x}, {end_y}) {duration} ms"
        )
    keycodes = {
        "back": 4,
        "home": 3,
        "recent": 187,
        "wake": 224,
        "enter": 66,
        "delete": 67,
    }
    if name in keycodes:
        _run_adb_command(
            adb,
            device,
            ["shell", "input", "keyevent", str(keycodes[name])],
        )
        return ActionExecution(f"按键 {name} (KEYCODE_{keycodes[name]})")
    if name == "input_text":
        text = str(action.get("text") or "")
        if not text:
            raise ValueError("input_text requires text")
        if len(text) > 500 or any(ord(character) < 32 or ord(character) > 126 for character in text):
            raise ValueError("ADB input_text currently supports printable ASCII only")
        encoded = text.replace("%", "%%").replace(" ", "%s")
        command = f"input text {shlex.quote(encoded)}"
        _run_adb_command(adb, device, ["shell", command])
        return ActionExecution(f"输入文本：{_short_text(text, 80)}")
    if name == "launch_app":
        package = str(action.get("package") or "").strip()
        if not ANDROID_PACKAGE_RE.fullmatch(package):
            raise ValueError("launch_app requires a valid Android package name")
        output = _run_adb_command(
            adb,
            device,
            [
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout_s=30,
        )
        if "No activities found" in output or "monkey aborted" in output.lower():
            raise RuntimeError(output or f"Unable to launch {package}")
        return ActionExecution(f"启动应用 {package}")
    raise ValueError(f"Unsupported phone_action: {name or '<empty>'}")


class AdbAgentController:
    """Own one background ADB agent session and expose polling-friendly state."""

    def __init__(
        self,
        adb: str,
        output_root: Path,
        *,
        client_factory: Optional[Callable[[Dict[str, object]], object]] = None,
        semantic_provider_factory: Optional[Callable[[str], object]] = None,
        screenshot_capture: Callable[[str, str], tuple[bytes, int, int]] = capture_adb_screenshot,
        action_executor: Callable[
            [str, str, Dict[str, object], int, int, threading.Event], ActionExecution
        ] = execute_adb_action,
    ) -> None:
        self.adb = adb
        self.output_root = output_root
        self._client_factory = client_factory or self._default_client_factory
        self._semantic_provider_factory = (
            semantic_provider_factory or Uiautomator2Provider
        )
        self._screenshot_capture = screenshot_capture
        self._action_executor = action_executor
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session: Optional[Dict[str, object]] = None
        self._logs: Deque[Dict[str, object]] = deque(maxlen=MAX_AGENT_LOGS)
        self._history: Deque[Dict[str, object]] = deque(maxlen=MAX_AGENT_HISTORY)
        self._latest_screenshot: Optional[bytes] = None
        self._screenshot_revision = 0

    @staticmethod
    def _default_client_factory(config: Dict[str, object]) -> VisionModelClient:
        return create_vision_model_client(config)

    def _log_locked(self, level: str, message: str) -> None:
        self._logs.append(
            {"time": time.time(), "level": level, "message": _short_text(message, 2000)}
        )

    def _log(self, level: str, message: str) -> None:
        with self._lock:
            self._log_locked(level, message)

    def _update(self, session_id: str, **values: object) -> None:
        with self._lock:
            if self._session is None or self._session.get("session_id") != session_id:
                return
            self._session.update(values)

    def _finish(
        self,
        session_id: str,
        status: str,
        message: str,
        *,
        error: str = "",
    ) -> None:
        now = time.time()
        with self._lock:
            if self._session is None or self._session.get("session_id") != session_id:
                return
            started_at = _number(self._session.get("started_at"), now)
            self._session.update(
                {
                    "running": False,
                    "status": status,
                    "phase": "finished",
                    "message": _short_text(message, 2000),
                    "error": _short_text(error, 3000),
                    "finished_at": now,
                    "elapsed_s": max(0.0, now - started_at),
                }
            )
            self._log_locked("error" if status == "error" else "info", message)

    @staticmethod
    def _session_directory(output_root: Path, workflow_name: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", workflow_name).strip("-.")[:42]
        slug = slug or "workflow"
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{uuid.uuid4().hex[:6]}"
        directory = output_root / "agent-runs" / name
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    @staticmethod
    def _persist_config(directory: Path, config: Dict[str, object]) -> None:
        public_config = {
            key: value
            for key, value in config.items()
            if key not in {"api_key", "input_secrets"}
        }
        public_config["api_key_configured"] = bool(config.get("api_key"))
        (directory / "config.json").write_text(
            json.dumps(public_config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _persist_event(directory: Path, event: Dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def start(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("请选择已授权的 Android ADB 设备")
        tasks = normalize_agent_tasks(payload)
        workflow_name = _short_text(
            payload.get("workflow_name") or tasks[0].get("name") or "ADB 测试流程",
            160,
        )
        temporary_task = payload.get("temporary_task") is True
        system_prompt = _multiline_text(payload.get("system_prompt"), 24000)
        if not system_prompt:
            system_prompt = DEFAULT_ADB_AGENT_SYSTEM_PROMPT
        system_prompt_version = (
            ADB_AGENT_SYSTEM_PROMPT_VERSION
            if system_prompt == DEFAULT_ADB_AGENT_SYSTEM_PROMPT
            else "custom"
        )
        model_provider = normalize_model_provider(payload.get("model_provider"))
        provider_definition = model_provider_definition(model_provider)
        provider_is_default = model_provider == DEFAULT_MODEL_PROVIDER
        api_base_url = str(
            payload.get("api_base_url")
            or (
                DEFAULT_MODEL_API_BASE_URL
                if provider_is_default
                else provider_definition["default_api_base_url"]
            )
        ).strip()
        model = str(
            payload.get("model") or (DEFAULT_MODEL if provider_is_default else "")
        ).strip()
        if not model or len(model) > 300:
            raise ValueError("请输入有效的模型名称")
        model_endpoint_url(model_provider, api_base_url, model)
        model_thinking_mode = normalize_model_thinking_mode(
            payload.get("model_thinking_mode")
            or default_model_thinking_mode(model_provider, model)
        )
        if (
            model_provider != MODEL_PROVIDER_OPENAI_COMPATIBLE
            and model_thinking_mode != MODEL_THINKING_AUTO
        ):
            raise ValueError("模型思考模式仅适用于 OpenAI-compatible 协议")
        automation_engine = normalize_automation_engine(
            payload.get("automation_engine") or DEFAULT_AUTOMATION_ENGINE
        )
        if automation_engine in {
            AUTOMATION_ENGINE_UIAUTOMATOR2,
            AUTOMATION_ENGINE_HYBRID,
        }:
            available, detail = uiautomator2_dependency_status()
            if not available and self._semantic_provider_factory is Uiautomator2Provider:
                raise ValueError(f"uiautomator2 引擎不可用：{detail}")
        api_key_mode = str(
            payload.get("api_key_mode")
            or provider_definition["default_api_key_mode"]
            or "auto"
        ).strip()
        _api_key_headers(model_provider, "validation-key", api_key_mode)
        input_secrets = _normalize_input_secrets(payload.get("input_secrets"))
        config: Dict[str, object] = {
            "device": device,
            "device_brand": _short_text(payload.get("device_brand"), 80),
            "device_model": _short_text(payload.get("device_model"), 120),
            "device_product": _short_text(payload.get("device_product"), 120),
            "device_codename": _short_text(payload.get("device_codename"), 120),
            "device_connection_type": _short_text(
                payload.get("device_connection_type"), 40
            ),
            "workflow_name": workflow_name,
            "temporary_task": temporary_task,
            "tasks": tasks,
            "task": tasks[0]["prompt"],
            "system_prompt": system_prompt,
            "system_prompt_version": system_prompt_version,
            "model_provider": model_provider,
            "api_base_url": api_base_url,
            "model": model,
            "model_thinking_mode": model_thinking_mode,
            "automation_engine": automation_engine,
            "api_key": str(
                payload.get("api_key")
                or (DEFAULT_MODEL_API_KEY if provider_is_default else "")
            ).strip(),
            "api_key_mode": api_key_mode,
            "input_secrets": input_secrets,
            "input_secret_ids": sorted(input_secrets),
            "step_delay_s": _bounded_float(payload.get("step_delay_s"), 0.2, 30.0, 1.2),
            "request_timeout_s": _bounded_float(
                payload.get("request_timeout_s"), 5.0, 600.0, 90.0
            ),
            "screenshot_retry_timeout_s": _bounded_float(
                payload.get("screenshot_retry_timeout_s"), 0.0, 300.0, 30.0
            ),
            "max_ui_elements": _bounded_int(
                payload.get("max_ui_elements"), 20, 1000, 240
            ),
            "created_at": time.time(),
        }
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("已有 ADB Agent 任务正在运行，请先停止")
            directory = self._session_directory(self.output_root, workflow_name)
            try:
                self._persist_config(directory, config)
            except OSError as exc:
                raise RuntimeError(f"无法创建 Agent 运行目录: {exc}") from exc
            session_id = uuid.uuid4().hex
            started_at = time.time()
            self._logs.clear()
            self._history.clear()
            self._latest_screenshot = None
            self._screenshot_revision = 0
            self._stop_event = threading.Event()
            self._session = {
                "session_id": session_id,
                "device": device,
                "device_brand": config["device_brand"],
                "device_model": config["device_model"],
                "device_product": config["device_product"],
                "device_codename": config["device_codename"],
                "device_connection_type": config["device_connection_type"],
                "workflow_name": workflow_name,
                "temporary_task": temporary_task,
                "task": tasks[0]["prompt"],
                "tasks": [dict(task) for task in tasks],
                "task_count": len(tasks),
                "task_index": 0,
                "current_task": None,
                "current_task_status": "pending",
                "current_task_started_at": None,
                "current_task_timeout_s": None,
                "task_results": [],
                "total_steps": 0,
                "system_prompt": system_prompt,
                "system_prompt_version": system_prompt_version,
                "model_provider": model_provider,
                "api_base_url": api_base_url,
                "model": model,
                "model_thinking_mode": model_thinking_mode,
                "automation_engine": automation_engine,
                "api_key_mode": api_key_mode,
                "api_key_configured": bool(config.get("api_key")),
                "input_secret_ids": list(config["input_secret_ids"]),
                "max_steps": tasks[0]["max_steps"],
                "step_delay_s": config["step_delay_s"],
                "request_timeout_s": config["request_timeout_s"],
                "screenshot_retry_timeout_s": config["screenshot_retry_timeout_s"],
                "max_ui_elements": config["max_ui_elements"],
                "step": 0,
                "status": "starting",
                "phase": "starting",
                "running": True,
                "started_at": started_at,
                "finished_at": None,
                "elapsed_s": 0.0,
                "message": "正在启动 ADB Agent",
                "error": "",
                "latest_action": None,
                "latest_action_result": "",
                "latest_reasoning": "",
                "latest_request_s": None,
                "screenshot_width": None,
                "screenshot_height": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "output_dir": str(directory),
            }
            self._log_locked(
                "info", f"测试流程已创建：{workflow_name} · {len(tasks)} 个子任务"
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(session_id, config, directory, self._stop_event),
                name=f"adb-agent-{session_id[:8]}",
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def stop(self) -> Dict[str, object]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return self.snapshot()
            self._stop_event.set()
            if self._session is not None:
                self._session.update(
                    {"status": "stopping", "phase": "stopping", "message": "正在停止流程"}
                )
            self._log_locked("warning", "用户请求停止流程")
        return self.snapshot()

    def latest_screenshot(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_screenshot

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            defaults = {
                "automation_engine": DEFAULT_AUTOMATION_ENGINE,
                "automation_engines": automation_engine_definitions_snapshot(),
                "model_provider": DEFAULT_MODEL_PROVIDER,
                "model_providers": model_provider_definitions_snapshot(),
                "api_base_url": DEFAULT_MODEL_API_BASE_URL,
                "model": DEFAULT_MODEL,
                "model_thinking_mode": DEFAULT_MODEL_THINKING_MODE,
                "api_key_mode": model_provider_definition(DEFAULT_MODEL_PROVIDER)[
                    "default_api_key_mode"
                ],
                "max_steps": 30,
                "task_timeout_s": 300,
                "step_delay_s": 1.2,
                "request_timeout_s": 90,
                "screenshot_retry_timeout_s": 30,
                "max_ui_elements": 240,
                "api_key_configured": bool(DEFAULT_MODEL_API_KEY),
                "system_prompt": DEFAULT_ADB_AGENT_SYSTEM_PROMPT,
                "system_prompt_version": ADB_AGENT_SYSTEM_PROMPT_VERSION,
                "task_templates": task_templates_snapshot(),
            }
            if self._session is None:
                return {
                    "available": True,
                    "running": False,
                    "status": "idle",
                    "phase": "idle",
                    "defaults": defaults,
                    "logs": [],
                    "history": [],
                    "screenshot_revision": 0,
                    "screenshot_available": False,
                }
            state = dict(self._session)
            if state.get("running"):
                state["elapsed_s"] = max(
                    0.0, time.time() - _number(state.get("started_at"), time.time())
                )
                if state.get("current_task_started_at"):
                    state["task_elapsed_s"] = max(
                        0.0,
                        time.time()
                        - _number(state.get("current_task_started_at"), time.time()),
                    )
            state.update(
                {
                    "available": True,
                    "defaults": defaults,
                    "logs": list(self._logs),
                    "history": list(self._history),
                    "screenshot_revision": self._screenshot_revision,
                    "screenshot_available": self._latest_screenshot is not None,
                    "screenshot_url": (
                        "/api/ai-agent/screenshot"
                        if self._latest_screenshot is not None
                        else None
                    ),
                }
            )
            return state

    def _run(
        self,
        session_id: str,
        config: Dict[str, object],
        directory: Path,
        stop_event: threading.Event,
    ) -> None:
        try:
            client = self._client_factory(config)
            provider = model_provider_definition(str(config["model_provider"]))
            automation_engine = normalize_automation_engine(
                config.get("automation_engine")
            )
            semantic_provider: Optional[object] = None
            if automation_engine in {
                AUTOMATION_ENGINE_UIAUTOMATOR2,
                AUTOMATION_ENGINE_HYBRID,
            }:
                semantic_provider = self._semantic_provider_factory(
                    str(config["device"])
                )
            self._log(
                "info",
                f"已连接模型：{provider['label']} · {config['model']} · "
                + {
                    AUTOMATION_ENGINE_UIAUTOMATOR2: "uiautomator2 语义控件",
                    AUTOMATION_ENGINE_HYBRID: "视觉 + uiautomator2",
                }.get(automation_engine, "视觉截图"),
            )
            tasks = config.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                raise RuntimeError("Agent workflow has no tasks")
            task_results: List[Dict[str, object]] = []
            device_identity_attention = _device_identity_attention(config)
            secret_alias_attention = _secret_alias_attention(config)
            had_warnings = False
            total_steps = 0

            for task_index, raw_task in enumerate(tasks, 1):
                if stop_event.is_set():
                    self._finish(session_id, "stopped", "测试流程已由用户停止")
                    return
                if not isinstance(raw_task, dict):
                    raise RuntimeError(f"Agent workflow task {task_index} is invalid")
                task = dict(raw_task)
                task_name = str(task.get("name") or f"任务 {task_index}")
                max_steps = _integer(task.get("max_steps"), 30)
                timeout_s = _number(task.get("timeout_s"), 300.0)
                task_started_at = time.time()
                task_history: List[Dict[str, object]] = []
                previous_screenshot = b""
                previous_ui_hierarchy_text = ""
                task_status: Optional[str] = None
                task_message = ""

                self._update(
                    session_id,
                    task_index=task_index,
                    current_task=task,
                    current_task_status="running",
                    current_task_started_at=task_started_at,
                    current_task_timeout_s=timeout_s,
                    step=0,
                    max_steps=max_steps,
                    status="running",
                    phase="task_start",
                    message=f"开始子任务 {task_index}/{len(tasks)}：{task_name}",
                )
                self._log(
                    "task",
                    f"开始子任务 {task_index}/{len(tasks)}：{task_name} · "
                    f"{max_steps} 步 / {timeout_s:g} 秒",
                )
                try:
                    self._persist_event(
                        directory,
                        {
                            "event_type": "task_start",
                            "time": task_started_at,
                            "task_index": task_index,
                            "task_id": task.get("id"),
                            "task_name": task_name,
                            "max_steps": max_steps,
                            "timeout_s": timeout_s,
                            "on_failure": task.get("on_failure"),
                        },
                    )
                except OSError as exc:
                    self._log("warning", f"保存子任务开始事件失败：{exc}")

                for step in range(1, max_steps + 1):
                    task_elapsed = time.time() - task_started_at
                    if task_elapsed >= timeout_s:
                        task_status = "timeout"
                        task_message = f"子任务超过 {timeout_s:g} 秒超时"
                        break
                    if stop_event.is_set():
                        self._finish(session_id, "stopped", "测试流程已由用户停止")
                        return
                    self._update(
                        session_id,
                        step=step,
                        phase="capturing",
                        task_elapsed_s=task_elapsed,
                        message=f"子任务 {task_index}/{len(tasks)} · 正在获取第 {step} 步截图",
                    )
                    retry_timeout_s = _number(
                        config.get("screenshot_retry_timeout_s"), 30.0
                    )
                    retry_started = time.monotonic()
                    retry_count = 0
                    while True:
                        try:
                            screenshot, width, height = self._screenshot_capture(
                                self.adb, str(config["device"])
                            )
                            if retry_count:
                                self._log(
                                    "info",
                                    f"ADB 截图已恢复，重试 {retry_count} 次",
                                )
                                try:
                                    self._persist_event(
                                        directory,
                                        {
                                            "event_type": "screenshot_recovered",
                                            "time": time.time(),
                                            "task_index": task_index,
                                            "step": step,
                                            "retry_count": retry_count,
                                            "unavailable_s": time.monotonic() - retry_started,
                                        },
                                    )
                                except OSError:
                                    pass
                            break
                        except RuntimeError as exc:
                            retry_count += 1
                            elapsed = time.monotonic() - retry_started
                            if stop_event.is_set():
                                self._finish(session_id, "stopped", "测试流程已由用户停止")
                                return
                            if elapsed >= retry_timeout_s:
                                raise RuntimeError(
                                    f"ADB screenshot unavailable for {elapsed:.1f}s: {exc}"
                                ) from exc
                            if retry_count == 1:
                                self._log(
                                    "warning",
                                    f"ADB 截图暂时不可用，最多重试 {retry_timeout_s:g} 秒：{exc}",
                                )
                                try:
                                    self._persist_event(
                                        directory,
                                        {
                                            "event_type": "screenshot_retry",
                                            "time": time.time(),
                                            "task_index": task_index,
                                            "step": step,
                                            "error": _short_text(exc, 500),
                                            "timeout_s": retry_timeout_s,
                                        },
                                    )
                                except OSError:
                                    pass
                            self._update(
                                session_id,
                                phase="reconnecting",
                                message=(
                                    f"子任务 {task_index}/{len(tasks)} · "
                                    f"ADB 暂时离线，正在重试截图（{elapsed:.0f}/{retry_timeout_s:g} 秒）"
                                ),
                            )
                            stop_event.wait(min(2.0, max(0.1, retry_timeout_s - elapsed)))
                    screenshot_name = f"task-{task_index:02d}-step-{step:03d}.png"
                    with self._lock:
                        if (
                            self._session is None
                            or self._session.get("session_id") != session_id
                        ):
                            return
                        self._latest_screenshot = screenshot
                        self._screenshot_revision += 1
                        self._session.update(
                            {
                                "screenshot_width": width,
                                "screenshot_height": height,
                            }
                        )
                    try:
                        (directory / screenshot_name).write_bytes(screenshot)
                    except OSError as exc:
                        self._log("warning", f"保存第 {task_index}.{step} 步截图失败：{exc}")
                    observation: Optional[Observation] = None
                    ui_hierarchy_text = ""
                    ui_hierarchy_name = ""
                    if semantic_provider is not None:
                        self._update(
                            session_id,
                            phase="observing_ui",
                            message=(
                                f"子任务 {task_index}/{len(tasks)} · "
                                f"正在读取第 {step} 步语义控件树"
                            ),
                        )
                        observation = semantic_provider.observe(  # type: ignore[attr-defined]
                            ObservationRequest(
                                channels=frozenset(
                                    {
                                        "ui_hierarchy",
                                        "foreground_activity",
                                        "device_context",
                                    }
                                ),
                                timeout_s=_number(config.get("request_timeout_s"), 90.0),
                                max_ui_elements=_bounded_int(
                                    config.get("max_ui_elements"),
                                    20,
                                    1000,
                                    240,
                                ),
                            )
                        )
                        if not isinstance(observation, Observation):
                            raise RuntimeError(
                                "uiautomator2 provider returned an invalid observation"
                            )
                        ui_hierarchy_text = format_ui_hierarchy(observation)
                        if observation.ui is not None:
                            ui_hierarchy_name = (
                                f"task-{task_index:02d}-step-{step:03d}.xml"
                            )
                            raw_artifact = observation.ui.raw_artifact
                            if raw_artifact is not None and raw_artifact.data:
                                try:
                                    (directory / ui_hierarchy_name).write_bytes(
                                        raw_artifact.data
                                    )
                                except OSError as exc:
                                    self._log(
                                        "warning",
                                        f"保存第 {task_index}.{step} 步控件树失败：{exc}",
                                    )
                        self._update(
                            session_id,
                            observation_revision=observation.revision,
                            ui_element_count=(
                                len(observation.ui.elements)
                                if observation.ui is not None
                                else 0
                            ),
                            foreground_package=observation.context.foreground_package,
                            foreground_activity=observation.context.foreground_activity,
                        )
                    self._update(
                        session_id,
                        phase="thinking",
                        message=f"子任务 {task_index}/{len(tasks)} · 模型正在决策第 {step} 步",
                    )
                    request_started = time.monotonic()
                    decision = client.decide(  # type: ignore[attr-defined]
                        task=str(task.get("prompt") or ""),
                        task_name=task_name,
                        attention_prompt="\n".join(
                            value
                            for value in (
                                device_identity_attention,
                                secret_alias_attention,
                                _action_limits_attention(
                                    task.get("action_limits"),
                                    task_history,
                                ),
                                _finish_requirements_attention(
                                    task.get("finish_action_requirements"),
                                    task_history,
                                ),
                                str(task.get("attention_prompt") or ""),
                            )
                            if value
                        ),
                        workflow_summary=_workflow_summary(task_results),
                        task_elapsed_s=time.time() - task_started_at,
                        task_timeout_s=timeout_s,
                        step=step,
                        max_steps=max_steps,
                        screenshot_png=screenshot,
                        previous_screenshot_png=previous_screenshot,
                        width=width,
                        height=height,
                        history=task_history,
                        ui_hierarchy_text=ui_hierarchy_text,
                        previous_ui_hierarchy_text=previous_ui_hierarchy_text,
                        observation_revision=(
                            observation.revision if observation is not None else ""
                        ),
                    )
                    request_duration = time.monotonic() - request_started
                    if not isinstance(decision, ModelDecision):
                        raise RuntimeError("Agent client returned an invalid decision")
                    action = dict(decision.action)
                    if (
                        str(task.get("id") or "").startswith("phone-config-")
                        and str(action.get("action") or "").strip().lower()
                        == "take_over"
                    ):
                        takeover_message = _short_text(
                            action.get("message") or "当前检查项需要人工处理",
                            96,
                        )
                        action = {
                            "action": "skip",
                            "message": _short_text(
                                f"{takeover_message}；手机配置检查已记录并继续后续项目",
                                MAX_AGENT_TAKE_OVER_MESSAGE_CHARS,
                            ),
                        }
                    if (
                        str(task.get("id") or "").startswith("phone-config-")
                        and observation is not None
                        and _matching_action_state_count(
                            task_history,
                            action,
                            observation.revision,
                        )
                        >= 2
                    ):
                        action = {
                            "action": "skip",
                            "message": _short_text(
                                "同一界面上的同一操作已执行两次且状态未变化；"
                                "手机配置检查已停止重试并记录该项",
                                MAX_AGENT_TAKE_OVER_MESSAGE_CHARS,
                            ),
                        }
                    with self._lock:
                        if (
                            self._session is None
                            or self._session.get("session_id") != session_id
                        ):
                            return
                        self._session["latest_reasoning"] = decision.reasoning
                        self._session["latest_action"] = dict(action)
                        self._session["latest_request_s"] = request_duration
                        self._session["prompt_tokens"] = _integer(
                            self._session.get("prompt_tokens"), 0
                        ) + (decision.prompt_tokens or 0)
                        self._session["completion_tokens"] = _integer(
                            self._session.get("completion_tokens"), 0
                        ) + (decision.completion_tokens or 0)
                    self._log(
                        "model",
                        f"子任务 {task_index} 第 {step} 步决策："
                        f"{json.dumps(action, ensure_ascii=False)}",
                    )
                    if stop_event.is_set():
                        self._finish(session_id, "stopped", "测试流程已由用户停止")
                        return
                    if time.time() - task_started_at >= timeout_s:
                        task_status = "timeout"
                        task_message = f"模型返回时子任务已超过 {timeout_s:g} 秒超时"
                        break
                    self._update(
                        session_id,
                        phase="acting",
                        message=f"子任务 {task_index}/{len(tasks)} · 正在执行第 {step} 步",
                    )
                    action_validation_error = (
                        finish_message_contradiction(action.get("message"))
                        if str(action.get("action") or "").strip().lower()
                        == "finish"
                        else ""
                    )
                    if (
                        not action_validation_error
                        and str(action.get("action") or "").strip().lower()
                        == "finish"
                    ):
                        action_validation_error = _finish_requirement_violation(
                            task.get("finish_action_requirements"),
                            task_history,
                        )
                    if not action_validation_error:
                        action_validation_error = _action_limit_violation(
                            task.get("action_limits"),
                            task_history,
                            action,
                        )
                    action_execution_error = ""
                    try:
                        if action_validation_error:
                            raise ValueError(action_validation_error)
                        execution_action = dict(action)
                        secret_input_id = ""
                        if str(execution_action.get("action") or "").strip().lower() == (
                            "input_secret"
                        ):
                            secret_input_id = str(
                                execution_action.get("secret_id") or ""
                            ).strip()
                            input_secrets = config.get("input_secrets")
                            if (
                                not secret_input_id
                                or not isinstance(input_secrets, dict)
                                or secret_input_id not in input_secrets
                            ):
                                raise ValueError(
                                    "input_secret requires a configured secret_id from the current session"
                                )
                            execution_action = {
                                "action": "input_text",
                                "text": str(input_secrets[secret_input_id]),
                            }
                        if (
                            semantic_provider is not None
                            and observation is not None
                            and str(action.get("action") or "").strip().lower()
                            not in {"finish", "skip", "take_over"}
                        ):
                            semantic_action = dict(execution_action)
                            if str(task.get("id") or "").startswith(
                                "phone-config-a0-store-"
                            ):
                                semantic_action["_forbid_recommendation_controls"] = True
                                if str(task.get("id") or "") != (
                                    "phone-config-a0-store-wechat"
                                ):
                                    semantic_action["_forbid_notification_allow"] = True
                            if str(task.get("id") or "") == (
                                "phone-config-a0-genshin-full-package"
                            ):
                                semantic_action["_forbid_launch_packages"] = (
                                    "com.bbk.appstore",
                                    "com.heytap.market",
                                    "com.xiaomi.market",
                                    "com.huawei.appmarket",
                                    "com.hihonor.appmarket",
                                    "com.sec.android.app.samsungapps",
                                )
                            semantic_summary = semantic_provider.execute_action(  # type: ignore[attr-defined]
                                semantic_action,
                                observation,
                                stop_event,
                            )
                            execution = ActionExecution(
                                f"uiautomator2 输入密钥 {secret_input_id}（内容已脱敏）"
                                if secret_input_id
                                else semantic_summary
                            )
                        else:
                            execution = self._action_executor(
                                self.adb,
                                str(config["device"]),
                                execution_action,
                                width,
                                height,
                                stop_event,
                            )
                            if secret_input_id:
                                execution = ActionExecution(
                                    f"输入密钥 {secret_input_id}（内容已脱敏）"
                                )
                    except ValueError as exc:
                        action_validation_error = _short_text(exc, 400)
                        execution = ActionExecution(
                            f"动作未执行：参数校验失败（{action_validation_error}）"
                        )
                    except RuntimeError as exc:
                        action_execution_error = _short_text(exc, 400)
                        execution = ActionExecution(
                            f"动作未执行：引擎执行失败（{action_execution_error}）"
                        )
                    total_steps += 1
                    event = {
                        "event_type": "action",
                        "time": time.time(),
                        "task_index": task_index,
                        "task_id": task.get("id"),
                        "task_name": task_name,
                        "step": step,
                        "workflow_step": total_steps,
                        "screenshot": screenshot_name,
                        "ui_hierarchy": ui_hierarchy_name or None,
                        "observation_revision": (
                            observation.revision if observation is not None else None
                        ),
                        "ui_element_count": (
                            len(observation.ui.elements)
                            if observation is not None and observation.ui is not None
                            else None
                        ),
                        "automation_engine": automation_engine,
                        "reasoning": decision.reasoning,
                        "action": action,
                        "result": execution.summary,
                        "request_s": request_duration,
                        "prompt_tokens": decision.prompt_tokens,
                        "completion_tokens": decision.completion_tokens,
                        "action_valid": not bool(
                            action_validation_error or action_execution_error
                        ),
                    }
                    with self._lock:
                        self._history.append(event)
                        if self._session is not None:
                            self._session["total_steps"] = total_steps
                    task_history.append(event)
                    previous_screenshot = screenshot
                    previous_ui_hierarchy_text = ui_hierarchy_text
                    try:
                        self._persist_event(directory, event)
                    except OSError as exc:
                        self._log("warning", f"保存第 {task_index}.{step} 步事件失败：{exc}")
                    self._update(session_id, latest_action_result=execution.summary)
                    self._log(
                        (
                            "warning"
                            if action_validation_error or action_execution_error
                            else "action"
                        ),
                        execution.summary,
                    )
                    if action_validation_error or action_execution_error:
                        if stop_event.is_set():
                            self._finish(session_id, "stopped", "测试流程已由用户停止")
                            return
                        self._update(
                            session_id,
                            phase="settling",
                            message=(
                                f"子任务 {task_index}/{len(tasks)} · "
                                "动作无效或执行失败，等待模型修正"
                            ),
                        )
                        stop_event.wait(_number(config.get("step_delay_s"), 1.2))
                        continue
                    if execution.terminal_status == "completed":
                        task_status = "completed"
                        task_message = execution.message or execution.summary
                        break
                    if execution.terminal_status == "take_over":
                        task_status = "take_over"
                        task_message = execution.message or execution.summary
                        break
                    if execution.terminal_status:
                        task_status = execution.terminal_status
                        task_message = execution.message or execution.summary
                        break
                    if stop_event.is_set():
                        self._finish(session_id, "stopped", "测试流程已由用户停止")
                        return
                    self._update(
                        session_id,
                        phase="settling",
                        message=f"子任务 {task_index}/{len(tasks)} · 等待界面稳定",
                    )
                    stop_event.wait(_number(config.get("step_delay_s"), 1.2))

                if task_status is None:
                    task_status = "max_steps"
                    task_message = f"达到子任务步骤上限 {max_steps}，未确认完成"
                task_finished_at = time.time()
                task_result = {
                    "index": task_index,
                    "id": task.get("id"),
                    "name": task_name,
                    "status": task_status,
                    "message": task_message,
                    "steps": len(task_history),
                    "started_at": task_started_at,
                    "finished_at": task_finished_at,
                    "duration_s": max(0.0, task_finished_at - task_started_at),
                    "on_failure": task.get("on_failure"),
                }
                task_results.append(task_result)
                self._update(
                    session_id,
                    task_results=list(task_results),
                    current_task_status=task_status,
                    task_elapsed_s=task_result["duration_s"],
                )
                try:
                    self._persist_event(
                        directory,
                        {"event_type": "task_end", "time": task_finished_at, **task_result},
                    )
                except OSError as exc:
                    self._log("warning", f"保存子任务结束事件失败：{exc}")
                self._log(
                    "info" if task_status == "completed" else "warning",
                    f"子任务 {task_index}/{len(tasks)} {task_status}：{task_message}",
                )

                if task_status == "completed":
                    continue
                if task_status == "skipped":
                    had_warnings = True
                    continue
                if task_status == "take_over":
                    self._finish(
                        session_id,
                        "take_over",
                        f"子任务“{task_name}”请求人工接管：{task_message}",
                    )
                    return
                if str(task.get("on_failure") or "stop") == "continue":
                    had_warnings = True
                    continue
                self._finish(
                    session_id,
                    "task_failed",
                    f"子任务“{task_name}”未完成：{task_message}",
                )
                return

            skipped_count = sum(
                str(item.get("status") or "") == "skipped" for item in task_results
            )
            completed_count = sum(
                str(item.get("status") or "") == "completed" for item in task_results
            )
            failed_results = [
                item
                for item in task_results
                if str(item.get("status") or "") not in {"completed", "skipped"}
            ]
            final_status = "completed_with_warnings" if had_warnings else "completed"
            if had_warnings:
                reason_summary = "；".join(
                    f"{item.get('name')}：{_short_text(item.get('message'), 80)}"
                    for item in task_results
                    if str(item.get("status") or "") != "completed"
                )
                final_message = (
                    f"检查完成：完成 {completed_count} 项，跳过 {skipped_count} 项，"
                    f"未完成 {len(failed_results)} 项。"
                    + (f"原因：{_short_text(reason_summary, 600)}" if reason_summary else "")
                )
            else:
                final_message = f"测试流程完成，共 {len(tasks)} 个子任务全部通过"
            self._finish(session_id, final_status, final_message)
        except Exception as exc:
            self._finish(
                session_id,
                "error",
                "ADB Agent 运行失败",
                error=str(exc),
            )
