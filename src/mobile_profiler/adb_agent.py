"""Vision-language-model agent that operates Android devices through ADB.

The model protocol mirrors BTR2's OpenAI-compatible native tool calling, while
the execution layer is deliberately limited to a small, validated ADB action
surface.  No model-provided shell command is ever executed.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_BTR2_API_BASE_URL = os.environ.get(
    "BTR2_LLM_ENDPOINT",
    "http://192.168.31.237:8000",
).strip()
DEFAULT_BTR2_MODEL = os.environ.get("BTR2_LLM_MODEL", "qwen3.6-27b").strip()
DEFAULT_BTR2_API_KEY = os.environ.get("BTR2_LLM_TOKEN", "").strip()
MAX_AGENT_LOGS = 240
MAX_AGENT_HISTORY = 20
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")


ADB_AGENT_SYSTEM_PROMPT = """你是 Mobile Profiler 的 Android ADB 手机操作智能体。
你看到的是 adb exec-out screencap 获取的完整手机帧缓冲截图，截图包含状态栏和导航栏。
你的目标是根据用户任务观察当前屏幕，每轮只调用一次 phone_action 工具推进任务。

坐标与动作规则：
- 截图坐标统一归一化为 0 到 999，左上角为 (0,0)，右下角为 (999,999)。
- tap、double_tap、long_press 使用 element=[x,y]。
- swipe、swipe_fast 使用 start=[x1,y1] 和 end=[x2,y2]；滚动页面时滑动距离应超过屏幕高度 50%。
- back、home、recent、wake、enter、delete 会转换为 Android keyevent。
- input_text 会转换为 adb shell input text，仅适合 ASCII 文本；不要用它输入中文。
- launch_app 仅接受 Android 包名，不允许提供 shell 命令。
- 页面加载或动画未结束时使用 wait，通常等待 1 到 3 秒。
- 明确完成任务时立即 finish；需要验证码、账号授权、支付、隐私确认或人工判断时 take_over。

安全与决策规则：
1. 执行下一步前核对截图是否符合上一动作预期。
2. 不要连续三次执行完全相同的点击或滑动；两次无效后必须换方式。
3. 不要自行执行购买、支付、删除账号/数据、发送消息等不可逆操作；遇到这些步骤必须 take_over。
4. 不要输出或请求任意 adb shell；只能使用 phone_action 中列出的动作。
5. 每轮必须产生一个 phone_action 工具调用，不要只输出分析文字。
"""


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
                        "launch_app",
                        "wait",
                        "finish",
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
                "package": {"type": "string", "maxLength": 200},
                "message": {"type": "string", "maxLength": 1000},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


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


def chat_completions_url(api_base_url: str) -> str:
    """Return the chat-completions URL for a BTR2/OpenAI-compatible base URL."""

    value = str(api_base_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API 地址必须是有效的 http:// 或 https:// URL")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


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


def parse_model_decision(payload: Dict[str, object]) -> ModelDecision:
    """Parse one native ``phone_action`` tool call from a chat response."""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("模型响应缺少 choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("模型响应缺少 message")
    reasoning = _short_text(
        message.get("reasoning_content") or message.get("reasoning") or "",
        3000,
    )
    content = str(message.get("content") or "")
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
        fallback = _json_object_from_text(content)
        if fallback is not None:
            if fallback.get("name") == "phone_action" and isinstance(
                fallback.get("arguments"), dict
            ):
                action = dict(fallback["arguments"])  # type: ignore[arg-type]
            elif "action" in fallback:
                action = fallback
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


class OpenAICompatibleVisionClient:
    """Small stdlib-only client for BTR2's local vLLM endpoint."""

    def __init__(
        self,
        api_base_url: str,
        model: str,
        api_key: str = "",
        request_timeout_s: float = 90.0,
    ) -> None:
        self.url = chat_completions_url(api_base_url)
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("模型名称不能为空")
        self.api_key = str(api_key or "").strip()
        self.request_timeout_s = request_timeout_s

    @staticmethod
    def _history_text(history: Sequence[Dict[str, object]]) -> str:
        if not history:
            return "尚无历史动作。"
        lines: List[str] = []
        for item in history[-12:]:
            action = item.get("action")
            action_text = json.dumps(action, ensure_ascii=False, separators=(",", ":"))
            result = _short_text(item.get("result"), 180)
            lines.append(f"步骤 {item.get('step')}: {action_text} -> {result}")
        return "\n".join(lines)

    def decide(
        self,
        *,
        task: str,
        step: int,
        max_steps: int,
        screenshot_png: bytes,
        width: int,
        height: int,
        history: Sequence[Dict[str, object]],
    ) -> ModelDecision:
        encoded = base64.b64encode(screenshot_png).decode("ascii")
        user_text = (
            f"用户任务：\n{task}\n\n"
            f"当前步骤：{step}/{max_steps}\n"
            f"ADB 截图尺寸：{width}x{height}；工具坐标仍使用 0-999。\n\n"
            f"最近动作与结果：\n{self._history_text(history)}\n\n"
            "观察最新截图，调用一次 phone_action。"
        )
        request_payload: Dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": ADB_AGENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
            "tools": [PHONE_ACTION_TOOL],
            "tool_choice": "required",
            "max_tokens": 1000,
            "temperature": 0.1,
            "top_p": 0.8,
            "frequency_penalty": 0.2,
            "stream": False,
            "chat_template_kwargs": {"reasoning_budget": 96},
        }
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key and self.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.request_timeout_s) as response:
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
        return parse_model_decision(decoded)


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
        raise ValueError(f"{action.get('action')} requires {key}=[x,y]")
    x = _bounded_int(value[0], 0, 999, 500)
    y = _bounded_int(value[1], 0, 999, 500)
    return (
        int(round(x / 999 * max(0, width - 1))),
        int(round(y / 999 * max(0, height - 1))),
    )


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
        message = _short_text(action.get("message") or "任务已完成", 1000)
        return ActionExecution("模型确认任务完成", message, "completed")
    if name == "take_over":
        message = _short_text(action.get("message") or "模型请求人工接管", 1000)
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
        screenshot_capture: Callable[[str, str], tuple[bytes, int, int]] = capture_adb_screenshot,
        action_executor: Callable[
            [str, str, Dict[str, object], int, int, threading.Event], ActionExecution
        ] = execute_adb_action,
    ) -> None:
        self.adb = adb
        self.output_root = output_root
        self._client_factory = client_factory or self._default_client_factory
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
    def _default_client_factory(config: Dict[str, object]) -> OpenAICompatibleVisionClient:
        return OpenAICompatibleVisionClient(
            str(config["api_base_url"]),
            str(config["model"]),
            str(config.get("api_key") or ""),
            _number(config.get("request_timeout_s"), 90.0),
        )

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
    def _session_directory(output_root: Path, task: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task).strip("-.")[:42] or "task"
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{uuid.uuid4().hex[:6]}"
        directory = output_root / "agent-runs" / name
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    @staticmethod
    def _persist_config(directory: Path, config: Dict[str, object]) -> None:
        public_config = {key: value for key, value in config.items() if key != "api_key"}
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
        task = str(payload.get("task") or "").strip()
        if not device:
            raise ValueError("请选择已授权的 Android ADB 设备")
        if not task:
            raise ValueError("请输入 Agent 任务")
        if len(task) > 6000:
            raise ValueError("Agent 任务不能超过 6000 个字符")
        api_base_url = str(
            payload.get("api_base_url") or DEFAULT_BTR2_API_BASE_URL
        ).strip()
        chat_completions_url(api_base_url)
        model = str(payload.get("model") or DEFAULT_BTR2_MODEL).strip()
        if not model or len(model) > 300:
            raise ValueError("请输入有效的模型名称")
        config: Dict[str, object] = {
            "device": device,
            "task": task,
            "api_base_url": api_base_url,
            "model": model,
            "api_key": str(payload.get("api_key") or DEFAULT_BTR2_API_KEY).strip(),
            "max_steps": _bounded_int(payload.get("max_steps"), 1, 200, 30),
            "step_delay_s": _bounded_float(payload.get("step_delay_s"), 0.2, 30.0, 1.2),
            "request_timeout_s": _bounded_float(
                payload.get("request_timeout_s"), 5.0, 600.0, 90.0
            ),
            "created_at": time.time(),
        }
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("已有 ADB Agent 任务正在运行，请先停止")
            directory = self._session_directory(self.output_root, task)
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
                "task": task,
                "api_base_url": api_base_url,
                "model": model,
                "api_key_configured": bool(config.get("api_key")),
                "max_steps": config["max_steps"],
                "step_delay_s": config["step_delay_s"],
                "request_timeout_s": config["request_timeout_s"],
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
            self._log_locked("info", f"任务已创建：{task}")
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
                    {"status": "stopping", "phase": "stopping", "message": "正在停止任务"}
                )
            self._log_locked("warning", "用户请求停止任务")
        return self.snapshot()

    def latest_screenshot(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_screenshot

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            defaults = {
                "api_base_url": DEFAULT_BTR2_API_BASE_URL,
                "model": DEFAULT_BTR2_MODEL,
                "max_steps": 30,
                "step_delay_s": 1.2,
                "request_timeout_s": 90,
                "api_key_configured": bool(DEFAULT_BTR2_API_KEY),
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
            self._log("info", f"已连接 BTR2 模型协议：{config['model']}")
            max_steps = _integer(config.get("max_steps"), 30)
            for step in range(1, max_steps + 1):
                if stop_event.is_set():
                    self._finish(session_id, "stopped", "任务已由用户停止")
                    return
                self._update(
                    session_id,
                    step=step,
                    status="running",
                    phase="capturing",
                    message="正在通过 ADB 获取截图",
                )
                screenshot, width, height = self._screenshot_capture(
                    self.adb, str(config["device"])
                )
                with self._lock:
                    if self._session is None or self._session.get("session_id") != session_id:
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
                    (directory / f"step-{step:03d}.png").write_bytes(screenshot)
                except OSError as exc:
                    self._log("warning", f"保存第 {step} 步截图失败：{exc}")
                self._update(
                    session_id,
                    phase="thinking",
                    message=f"模型正在决策第 {step} 步",
                )
                request_started = time.monotonic()
                decision = client.decide(  # type: ignore[attr-defined]
                    task=str(config["task"]),
                    step=step,
                    max_steps=max_steps,
                    screenshot_png=screenshot,
                    width=width,
                    height=height,
                    history=list(self._history),
                )
                request_duration = time.monotonic() - request_started
                if not isinstance(decision, ModelDecision):
                    raise RuntimeError("Agent client returned an invalid decision")
                with self._lock:
                    if self._session is None or self._session.get("session_id") != session_id:
                        return
                    self._session["latest_reasoning"] = decision.reasoning
                    self._session["latest_action"] = dict(decision.action)
                    self._session["latest_request_s"] = request_duration
                    self._session["prompt_tokens"] = _integer(
                        self._session.get("prompt_tokens"), 0
                    ) + (decision.prompt_tokens or 0)
                    self._session["completion_tokens"] = _integer(
                        self._session.get("completion_tokens"), 0
                    ) + (decision.completion_tokens or 0)
                self._log(
                    "model",
                    f"第 {step} 步决策：{json.dumps(decision.action, ensure_ascii=False)}",
                )
                if stop_event.is_set():
                    self._finish(session_id, "stopped", "任务已由用户停止")
                    return
                self._update(
                    session_id,
                    phase="acting",
                    message=f"正在通过 ADB 执行第 {step} 步",
                )
                execution = self._action_executor(
                    self.adb,
                    str(config["device"]),
                    decision.action,
                    width,
                    height,
                    stop_event,
                )
                event = {
                    "time": time.time(),
                    "step": step,
                    "screenshot": f"step-{step:03d}.png",
                    "reasoning": decision.reasoning,
                    "action": decision.action,
                    "result": execution.summary,
                    "request_s": request_duration,
                    "prompt_tokens": decision.prompt_tokens,
                    "completion_tokens": decision.completion_tokens,
                }
                with self._lock:
                    self._history.append(event)
                try:
                    self._persist_event(directory, event)
                except OSError as exc:
                    self._log("warning", f"保存第 {step} 步事件失败：{exc}")
                self._update(session_id, latest_action_result=execution.summary)
                self._log("action", execution.summary)
                if execution.terminal_status:
                    self._finish(
                        session_id,
                        execution.terminal_status,
                        execution.message or execution.summary,
                    )
                    return
                if stop_event.is_set():
                    self._finish(session_id, "stopped", "任务已由用户停止")
                    return
                self._update(
                    session_id,
                    phase="settling",
                    message="等待界面稳定后继续观察",
                )
                stop_event.wait(_number(config.get("step_delay_s"), 1.2))
            self._finish(
                session_id,
                "max_steps",
                f"已达到最大步骤数 {max_steps}，任务未确认完成",
            )
        except Exception as exc:
            self._finish(
                session_id,
                "error",
                "ADB Agent 运行失败",
                error=str(exc),
            )
