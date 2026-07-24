"""Product-level two-stage Android preparation and endurance orchestration."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Optional, Protocol, Sequence

from .adb_agent import AdbAgentController
from .adb_agent_prompts import finish_message_contradiction
from .campaign_config import (
    ActionRequirementConfig,
    AgentTaskConfig,
    CampaignConfig,
    InstallSetConfig,
    PermissionConfig,
    PreparationAppConfig,
    RecordingConfig,
    SystemSettingConfig,
    WorkflowConfig,
)


PREPARATION_DEFAULT_PROMPT = """这是正式测试前的设备预备阶段。目标是让指定应用完成首次启动初始化并停在稳定主界面：从上到下检查完整屏幕，关闭更新、活动、广告、通知引导和功能介绍等弹窗，并处理当前应用的运行时权限。每个动作后都要依据下一张截图确认状态变化。"""

PREPARATION_DEFAULT_ATTENTION = """只处理配置中明确指定的应用和权限。不得登录账号、输入手机号/验证码、实名认证、支付、下单、发送消息、清除数据或操作其他应用；遇到这些边界立即 take_over。"""

TEST_DEFAULT_PROMPT = """这是两小时续航轮次中的实际操作任务。只在当前目标应用内完成可逆、可重复的主功能操作；每次输入后必须用下一张截图确认页面、卡片、棋子或角色状态确实发生变化，再根据当前子任务决定 finish。当前页面处于可安全导航的前置状态时应继续完成最小导航，不能仅因还没到主功能页面就 skip。"""

TEST_DEFAULT_ATTENTION = """不要登录、注册、实名、购买、支付、点赞、评论、关注、发送消息、修改账号或授予任务未声明的敏感权限。离开目标应用、出现验证码或无法判断的风险提示时立即 take_over。"""

_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class RecorderProcess(Protocol):
    def poll(self) -> Optional[int]: ...

    def wait(self, timeout: Optional[float] = None) -> int: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class _ManagedRecorder:
    def __init__(self, command: Sequence[str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = log_path.open("ab")
        self._process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=self._stream,
            stderr=subprocess.STDOUT,
        )

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        self._process.terminate()

    def close(self) -> None:
        self._stream.close()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _default_command_runner(command: Sequence[str], timeout_s: float) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            124,
            _decode(exc.stdout or b""),
            f"timeout after {timeout_s:.1f}s\n{_decode(exc.stderr or b'')}".strip(),
        )
    return CommandResult(
        completed.returncode,
        _decode(completed.stdout),
        _decode(completed.stderr),
    )


def _default_recorder_factory(command: Sequence[str], log_path: Path) -> RecorderProcess:
    return _ManagedRecorder(command, log_path)


def _default_agent_factory(adb: str, output_root: Path) -> AdbAgentController:
    return AdbAgentController(adb, output_root)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:80]
    return slug or "campaign"


def _json_safe(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(_json_safe(value), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _recording_artifact_evidence(recording_dir: Path) -> dict[str, object]:
    """Return strict, user-consumable evidence for a completed recording."""

    checkpoint_path = recording_dir / "checkpoint.json"
    errors: list[str] = []
    checkpoint: Mapping[str, object] = {}
    try:
        decoded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if isinstance(decoded, Mapping):
            checkpoint = decoded
        else:
            errors.append("checkpoint.json is not a JSON object")
    except FileNotFoundError:
        errors.append("checkpoint.json is missing")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"checkpoint.json is unreadable: {exc}")

    def checkpoint_count(name: str) -> int:
        try:
            return max(0, int(checkpoint.get(name) or 0))
        except (TypeError, ValueError):
            errors.append(f"checkpoint {name} is not an integer")
            return 0

    checkpoint_status = str(checkpoint.get("status") or "").strip().lower()
    sample_count = checkpoint_count("sample_count")
    if checkpoint_status != "complete":
        errors.append(
            f"checkpoint status is {checkpoint_status or 'missing'}, expected complete"
        )
    if sample_count <= 0:
        errors.append("checkpoint sample_count is zero")

    artifact_sizes: dict[str, int] = {}
    for name in ("samples.csv", "analysis.json", "report.html"):
        path = recording_dir / name
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        artifact_sizes[name] = size
        if size <= 0:
            errors.append(f"{name} is missing or empty")

    return {
        "checkpoint_status": checkpoint_status or "missing",
        "sample_count": sample_count,
        "context_count": checkpoint_count("context_count"),
        "thermal_snapshot_count": checkpoint_count("thermal_snapshot_count"),
        "reconnect_count": checkpoint_count("reconnect_count"),
        "stop_reason": str(checkpoint.get("stop_reason") or ""),
        "artifact_sizes": artifact_sizes,
        "artifacts_complete": not errors,
        "artifact_errors": errors,
    }


def _minimum_recording_sample_count(duration_s: float, interval_s: float) -> int:
    """Require at least 80% of the nominal fixed-interval sample count."""

    expected = max(1, math.floor(max(0.0, duration_s) / max(0.1, interval_s)))
    return max(1, math.floor(expected * 0.8))


class CampaignJournal:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = output_dir / "events.jsonl"
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload: object) -> None:
        event = {
            "event_type": event_type,
            "time": time.time(),
            **payload,
        }
        line = json.dumps(_json_safe(event), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def state(self, **payload: object) -> None:
        _write_json(self.output_dir / "state.json", payload)


class AndroidCampaignRunner:
    """Runs preparation independently, then repeated fixed-duration test rounds."""

    def __init__(
        self,
        adb: str,
        config: CampaignConfig,
        output_root: Path,
        *,
        command_runner: Callable[[Sequence[str], float], CommandResult] = _default_command_runner,
        recorder_factory: Callable[[Sequence[str], Path], RecorderProcess] = _default_recorder_factory,
        agent_factory: Callable[[str, Path], object] = _default_agent_factory,
        model_payload_overrides: Optional[Mapping[str, object]] = None,
        task_overrides: Optional[Mapping[str, AgentTaskConfig]] = None,
        task_order: Optional[Sequence[str]] = None,
        repeat_workflows: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not str(config.device or "").strip():
            raise ValueError("campaign device is empty; set config.device or pass --device")
        self.adb = str(adb or "adb")
        self.config = config
        self.output_root = Path(output_root)
        self._command_runner = command_runner
        self._recorder_factory = recorder_factory
        self._agent_factory = agent_factory
        self._model_payload_overrides = dict(model_payload_overrides or {})
        self._task_overrides = dict(task_overrides or {})
        self._task_order = {
            str(task_id): index
            for index, task_id in enumerate(task_order or ())
            if str(task_id).strip()
        }
        self.repeat_workflows = bool(repeat_workflows)
        self._clock = clock
        self._sleep = sleep
        self._stop_event = threading.Event()
        self._active_agent: Optional[object] = None
        self._active_recorder: Optional[RecorderProcess] = None

    @property
    def device(self) -> str:
        return self.config.device

    def request_stop(self) -> None:
        self._stop_event.set()
        agent = self._active_agent
        if agent is not None:
            try:
                agent.stop()  # type: ignore[attr-defined]
            except Exception:
                pass

    def active_agent_snapshot(self) -> dict[str, object]:
        """Return the nested ADB Agent state used by the current campaign workflow."""

        agent = self._active_agent
        if agent is None:
            return {}
        try:
            state = agent.snapshot()  # type: ignore[attr-defined]
        except Exception:
            return {}
        return dict(state) if isinstance(state, Mapping) else {}

    def latest_screenshot(self) -> Optional[bytes]:
        """Return the latest screenshot from the nested ADB Agent, if available."""

        agent = self._active_agent
        if agent is None:
            return None
        try:
            screenshot = agent.latest_screenshot()  # type: ignore[attr-defined]
        except Exception:
            return None
        return screenshot if isinstance(screenshot, bytes) else None

    def _stage_output(self, stage: str) -> Path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        base_name = f"{_slug(self.config.campaign_id)}-{stage}-{timestamp}"
        for suffix in range(1, 10000):
            name = base_name if suffix == 1 else f"{base_name}-{suffix}"
            candidate = self.output_root / name
            try:
                candidate.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return candidate
        raise RuntimeError(f"cannot reserve a unique {stage} campaign output directory")

    def _adb(self, arguments: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
        return self._command_runner(
            [self.adb, "-s", self.device, *[str(item) for item in arguments]],
            timeout_s,
        )

    def device_available(self) -> bool:
        result = self._adb(["get-state"], 8.0)
        return result.returncode == 0 and result.stdout.strip().lower() == "device"

    def package_installed(self, package: str) -> bool:
        if not _PACKAGE_PATTERN.fullmatch(package):
            raise ValueError(f"invalid package: {package}")
        result = self._adb(["shell", "pm", "path", package], 20.0)
        return result.returncode == 0 and "package:" in result.stdout

    def _foreground_package(self) -> Optional[str]:
        """Return the observed resumed Android package, or None when unavailable."""

        result = self._adb(["shell", "dumpsys", "activity", "activities"], 20.0)
        if result.returncode != 0:
            return None
        patterns = (
            r"topResumedActivity=.*?\s(?:u\d+\s+)?([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
            r"(?:mResumedActivity|ResumedActivity)[:=].*?\s(?:u\d+\s+)?([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
            r"mFocusedApp[:=].*?\s(?:u\d+\s+)?([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, result.stdout)
            if match is not None:
                return match.group(1)
        return None

    @staticmethod
    def _command_output(result: CommandResult) -> str:
        return "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if str(part or "").strip()
        )

    def _interaction_state(self) -> dict[str, object]:
        power = self._adb(["shell", "dumpsys", "power"], 20.0)
        policy = self._adb(["shell", "dumpsys", "window", "policy"], 20.0)
        wakefulness_match = re.search(
            r"mWakefulness=(Awake|Asleep|Dozing|Dreaming)",
            power.stdout,
            re.IGNORECASE,
        )
        showing_match = re.search(
            r"^\s+showing=(true|false)\s*$",
            policy.stdout,
            re.IGNORECASE | re.MULTILINE,
        )
        secure_match = re.search(
            r"^\s+secure=(true|false)\s*$",
            policy.stdout,
            re.IGNORECASE | re.MULTILINE,
        )
        wakefulness = wakefulness_match.group(1).lower() if wakefulness_match else "unknown"
        return {
            "wakefulness": wakefulness,
            "awake": None if wakefulness == "unknown" else wakefulness == "awake",
            "keyguard_showing": (
                None if showing_match is None else showing_match.group(1).lower() == "true"
            ),
            "keyguard_secure": (
                None if secure_match is None else secure_match.group(1).lower() == "true"
            ),
            "power_error": self._command_output(power) if power.returncode else "",
            "policy_error": self._command_output(policy) if policy.returncode else "",
        }

    def _ensure_interactive(self) -> dict[str, object]:
        before = self._interaction_state()
        actions: list[str] = []
        if before.get("awake") is False:
            wake = self._adb(["shell", "input", "keyevent", "224"], 10.0)
            actions.append("wake")
            if wake.returncode == 0:
                self._sleep(0.5)

        current = self._interaction_state()
        if current.get("keyguard_showing") is True:
            if current.get("keyguard_secure") is True:
                return {
                    "succeeded": False,
                    "message": "device is protected by a secure keyguard",
                    "actions": actions,
                    "before": before,
                    "after": current,
                }
            menu = self._adb(["shell", "input", "keyevent", "82"], 10.0)
            actions.append("dismiss_insecure_keyguard")
            size_result = self._adb(["shell", "wm", "size"], 10.0)
            size_match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", size_result.stdout)
            width = int(size_match.group(1)) if size_match else 1080
            height = int(size_match.group(2)) if size_match else 2400
            swipe = self._adb(
                [
                    "shell",
                    "input",
                    "swipe",
                    str(width // 2),
                    str(round(height * 0.86)),
                    str(width // 2),
                    str(round(height * 0.24)),
                    "450",
                ],
                10.0,
            )
            actions.append("swipe_up")
            if menu.returncode == 0 or swipe.returncode == 0:
                self._sleep(0.8)
            current = self._interaction_state()

        succeeded = (
            current.get("awake") is not False
            and current.get("keyguard_showing") is not True
        )
        return {
            "succeeded": succeeded,
            "message": "device is interactive" if succeeded else "device did not become interactive",
            "actions": actions,
            "before": before,
            "after": current,
        }

    def _home(self) -> CommandResult:
        return self._adb(
            [
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.HOME",
            ],
            20.0,
        )

    def _launch(
        self,
        package: str,
        *,
        force_stop_before_launch: bool = False,
    ) -> CommandResult:
        if force_stop_before_launch:
            stopped = self._adb(
                ["shell", "am", "force-stop", package],
                20.0,
            )
            if stopped.returncode != 0:
                return stopped
        return self._adb(
            [
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            30.0,
        )

    @staticmethod
    def _setting_matches(expected: str, actual: str) -> bool:
        if expected.strip() == actual.strip():
            return True
        try:
            return math.isclose(float(expected), float(actual), rel_tol=0.0, abs_tol=1e-6)
        except ValueError:
            return False

    def _apply_setting(self, setting: SystemSettingConfig) -> dict[str, object]:
        put = self._adb(
            ["shell", "settings", "put", setting.namespace, setting.key, setting.value],
            20.0,
        )
        get = self._adb(
            ["shell", "settings", "get", setting.namespace, setting.key],
            20.0,
        )
        actual = get.stdout.strip()
        fallback: Optional[CommandResult] = None
        fallback_command: list[str] = []
        if (
            put.returncode == 0
            and get.returncode == 0
            and not self._setting_matches(setting.value, actual)
            and setting.namespace == "system"
        ):
            if setting.key == "accelerometer_rotation" and setting.value in {"0", "1"}:
                if setting.value == "0":
                    rotation_get = self._adb(
                        ["shell", "settings", "get", "system", "user_rotation"],
                        20.0,
                    )
                    rotation = rotation_get.stdout.strip()
                    if rotation not in {"0", "1", "2", "3"}:
                        rotation = "0"
                    fallback_command = [
                        "shell",
                        "cmd",
                        "window",
                        "user-rotation",
                        "lock",
                        rotation,
                    ]
                else:
                    fallback_command = [
                        "shell",
                        "cmd",
                        "window",
                        "user-rotation",
                        "free",
                    ]
            elif setting.key == "user_rotation" and setting.value in {"0", "1", "2", "3"}:
                fallback_command = [
                    "shell",
                    "cmd",
                    "window",
                    "user-rotation",
                    "lock",
                    setting.value,
                ]
            if fallback_command:
                fallback = self._adb(fallback_command, 20.0)
                get = self._adb(
                    ["shell", "settings", "get", setting.namespace, setting.key],
                    20.0,
                )
                actual = get.stdout.strip()
        succeeded = (
            put.returncode == 0
            and (fallback is None or fallback.returncode == 0)
            and get.returncode == 0
            and self._setting_matches(setting.value, actual)
        )
        errors = [put.stderr, get.stderr]
        if fallback is not None:
            errors.append(fallback.stderr)
        return {
            "namespace": setting.namespace,
            "key": setting.key,
            "expected": setting.value,
            "actual": actual,
            "required": setting.required,
            "succeeded": succeeded,
            "compatibility_fallback": " ".join(fallback_command),
            "error": "\n".join(value.strip() for value in errors if value.strip()),
        }

    @contextmanager
    def _apk_files(self, install_set: InstallSetConfig) -> Iterator[tuple[Path, ...]]:
        source = install_set.source
        if not source.exists():
            raise FileNotFoundError(f"install source does not exist: {source}")
        if source.is_file() and source.suffix.lower() == ".apk":
            yield (source,)
            return
        if source.is_dir():
            apks = sorted(
                (path for path in source.iterdir() if path.is_file() and path.suffix.lower() == ".apk"),
                key=lambda path: (path.name != "base.apk", path.name.lower()),
            )
            if not apks:
                raise ValueError(f"install directory contains no APK files: {source}")
            yield tuple(apks)
            return
        if not source.is_file() or source.suffix.lower() != ".apks":
            raise ValueError(f"install source must be an APK, APKS archive, or directory: {source}")

        with tempfile.TemporaryDirectory(prefix="mobile-profiler-apks-") as temporary:
            target_root = Path(temporary)
            extracted: list[Path] = []
            names: set[str] = set()
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".apk"):
                        continue
                    pure = PurePosixPath(info.filename)
                    if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 1:
                        raise ValueError(f"unsafe APK entry in {source.name}: {info.filename}")
                    if pure.name in names:
                        raise ValueError(f"duplicate APK entry in {source.name}: {pure.name}")
                    names.add(pure.name)
                    target = target_root / pure.name
                    with archive.open(info) as source_handle, target.open("wb") as target_handle:
                        shutil.copyfileobj(source_handle, target_handle)
                    extracted.append(target)
            if not extracted:
                raise ValueError(f"APKS archive contains no APK files: {source}")
            extracted.sort(key=lambda path: (path.name != "base.apk", path.name.lower()))
            yield tuple(extracted)

    def _install(self, install_set: InstallSetConfig) -> dict[str, object]:
        if self.package_installed(install_set.package):
            return {
                "name": install_set.name,
                "package": install_set.package,
                "source": str(install_set.source),
                "apk_count": 0,
                "required": install_set.required,
                "succeeded": True,
                "already_installed": True,
                "skipped": True,
                "output": "package already installed; archive installation skipped",
            }
        try:
            with self._apk_files(install_set) as apks:
                command = ["install" if len(apks) == 1 else "install-multiple"]
                if install_set.replace:
                    command.append("-r")
                if install_set.allow_downgrade:
                    command.append("-d")
                command.extend(str(path) for path in apks)
                result = self._adb(command, install_set.timeout_s)
                installed = result.returncode == 0 and self.package_installed(
                    install_set.package
                )
                return {
                    "name": install_set.name,
                    "package": install_set.package,
                    "source": str(install_set.source),
                    "apk_count": len(apks),
                    "required": install_set.required,
                    "succeeded": installed,
                    "already_installed": False,
                    "skipped": False,
                    "output": self._command_output(result),
                }
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return {
                "name": install_set.name,
                "package": install_set.package,
                "source": str(install_set.source),
                "apk_count": 0,
                "required": install_set.required,
                "succeeded": False,
                "output": str(exc),
            }

    def _grant_permission(self, package: str, permission: PermissionConfig) -> dict[str, object]:
        grant = self._adb(
            ["shell", "pm", "grant", package, permission.name],
            30.0,
        )
        check = self._adb(
            ["shell", "cmd", "package", "check-permission", permission.name, package],
            20.0,
        )
        checked = check.stdout.strip().lower()
        verification = "cmd package check-permission"
        explicitly_granted: Optional[bool] = None
        if check.returncode == 0 and checked and "unknown command" not in checked:
            explicitly_granted = "granted" in checked or checked == "0"
        else:
            dump = self._adb(["shell", "dumpsys", "package", package], 30.0)
            match = re.search(
                rf"{re.escape(permission.name)}:\s+granted=(true|false)",
                dump.stdout,
                re.IGNORECASE,
            )
            verification = "dumpsys package"
            if match is not None:
                explicitly_granted = match.group(1).lower() == "true"
                checked = f"granted={str(explicitly_granted).lower()}"
            else:
                checked = "unverified after successful pm grant"
        succeeded = grant.returncode == 0 and explicitly_granted is not False
        return {
            "package": package,
            "permission": permission.name,
            "required": permission.required,
            "succeeded": succeeded,
            "check": checked,
            "verification": verification,
            "error": self._command_output(grant) if grant.returncode else "",
        }

    def _agent_payload(
        self,
        workflow_name: str,
        tasks: Sequence[AgentTaskConfig],
        prompt_prefix: str,
        attention_prefix: str,
        payload_overrides: Optional[Mapping[str, object]] = None,
        finish_action_requirements: Sequence[ActionRequirementConfig] = (),
    ) -> dict[str, object]:
        task_payloads = [
            task.payload(prompt_prefix, attention_prefix) for task in tasks
        ]
        if finish_action_requirements:
            requirements_payload = [
                {
                    "actions": list(requirement.actions),
                    "minimum": requirement.minimum,
                    "label": requirement.label,
                }
                for requirement in finish_action_requirements
            ]
            for task_payload in task_payloads:
                task_payload["finish_action_requirements"] = requirements_payload
        payload = {
            "device": self.device,
            "workflow_name": workflow_name,
            "tasks": task_payloads,
        }
        payload.update(self.config.model.payload())
        payload.update(payload_overrides or {})
        payload.update(self._model_payload_overrides)
        return payload

    def _overridden_tasks(
        self, tasks: Sequence[AgentTaskConfig]
    ) -> tuple[AgentTaskConfig, ...]:
        indexed = list(enumerate(tasks))
        indexed.sort(
            key=lambda item: (
                self._task_order.get(
                    item[1].task_id,
                    len(self._task_order) + item[0],
                ),
                item[0],
            )
        )
        return tuple(
            self._task_overrides.get(task.task_id, task)
            for _index, task in indexed
        )

    def _ordered_preparation_apps(self) -> tuple[PreparationAppConfig, ...]:
        indexed = list(enumerate(self.config.preparation.apps))

        def rank(item: tuple[int, PreparationAppConfig]) -> tuple[int, int]:
            index, app = item
            task_ids = [task.task_id for task in app.setup_tasks]
            if app.install_prompt:
                task_ids.append(f"store-install-{app.package}")
            known = [self._task_order[task_id] for task_id in task_ids if task_id in self._task_order]
            return (min(known) if known else len(self._task_order) + index, index)

        indexed.sort(key=rank)
        return tuple(app for _index, app in indexed)

    def _ordered_test_workflows(self) -> tuple[WorkflowConfig, ...]:
        indexed = list(enumerate(self.config.test.workflows))

        def rank(item: tuple[int, WorkflowConfig]) -> tuple[int, int]:
            index, workflow = item
            known = [
                self._task_order[task.task_id]
                for task in workflow.tasks
                if task.task_id in self._task_order
            ]
            return (min(known) if known else len(self._task_order) + index, index)

        indexed.sort(key=rank)
        return tuple(workflow for _index, workflow in indexed)

    @staticmethod
    def _compact_agent_state(state: Mapping[str, object]) -> dict[str, object]:
        keys = (
            "session_id",
            "workflow_name",
            "status",
            "message",
            "error",
            "running",
            "total_steps",
            "elapsed_s",
            "output_dir",
            "task_results",
            "latest_action",
            "latest_action_result",
        )
        return {key: state.get(key) for key in keys if key in state}

    def _run_agent(
        self,
        output_root: Path,
        workflow_name: str,
        tasks: Sequence[AgentTaskConfig],
        prompt_prefix: str,
        attention_prefix: str,
        poll_interval_s: float,
        journal: CampaignJournal,
        *,
        device_offline_grace_s: float,
        payload_overrides: Optional[Mapping[str, object]] = None,
        deadline: Optional[float] = None,
        progress_callback: Optional[Callable[[], None]] = None,
        allowed_foreground_packages: Optional[Sequence[str]] = None,
        finish_action_requirements: Sequence[ActionRequirementConfig] = (),
    ) -> dict[str, object]:
        agent = self._agent_factory(self.adb, output_root)
        self._active_agent = agent
        payload = self._agent_payload(
            workflow_name,
            tasks,
            prompt_prefix,
            attention_prefix,
            payload_overrides,
            finish_action_requirements,
        )
        offline_grace_s = max(0.0, float(device_offline_grace_s))
        payload["screenshot_retry_timeout_s"] = offline_grace_s
        allowed_foreground = {
            str(package).strip()
            for package in (allowed_foreground_packages or ())
            if str(package).strip()
        }
        journal.emit(
            "agent_start",
            workflow_name=workflow_name,
            device_offline_grace_s=offline_grace_s,
        )
        try:
            state = agent.start(payload)  # type: ignore[attr-defined]
            offline_since: Optional[float] = None
            next_progress_at = self._clock()
            while bool(state.get("running")):
                if self._stop_event.is_set():
                    agent.stop()  # type: ignore[attr-defined]
                    break
                now = self._clock()
                if progress_callback is not None and now >= next_progress_at:
                    progress_callback()
                    next_progress_at = now + 5.0
                if deadline is not None and now >= deadline:
                    agent.stop()  # type: ignore[attr-defined]
                    state = agent.snapshot()  # type: ignore[attr-defined]
                    compact = self._compact_agent_state(state)
                    compact.update(
                        {
                            "status": "round_deadline",
                            "running": False,
                            "message": "two-hour round deadline reached during workflow",
                        }
                    )
                    journal.emit(
                        "agent_round_deadline",
                        workflow_name=workflow_name,
                    )
                    journal.emit(
                        "agent_end", workflow_name=workflow_name, state=compact
                    )
                    return compact
                if not self.device_available():
                    if offline_since is None:
                        offline_since = now
                        journal.emit(
                            "agent_device_offline",
                            workflow_name=workflow_name,
                            grace_s=offline_grace_s,
                        )
                    unavailable_s = max(0.0, now - offline_since)
                    if unavailable_s >= offline_grace_s:
                        agent.stop()  # type: ignore[attr-defined]
                        compact = self._compact_agent_state(state)
                        compact.update(
                            {
                                "status": "device_unavailable",
                                "running": False,
                                "message": (
                                    "device remained unavailable during agent workflow "
                                    f"for {unavailable_s:.1f}s"
                                ),
                            }
                        )
                        journal.emit(
                            "agent_device_offline_timeout",
                            workflow_name=workflow_name,
                            unavailable_s=unavailable_s,
                            grace_s=offline_grace_s,
                        )
                        journal.emit(
                            "agent_end", workflow_name=workflow_name, state=compact
                        )
                        return compact
                    self._sleep(
                        min(
                            poll_interval_s,
                            max(0.01, offline_grace_s - unavailable_s),
                        )
                    )
                    state = agent.snapshot()  # type: ignore[attr-defined]
                    continue
                if offline_since is not None:
                    journal.emit(
                        "agent_device_reconnected",
                        workflow_name=workflow_name,
                        unavailable_s=max(0.0, now - offline_since),
                    )
                    offline_since = None
                if allowed_foreground:
                    foreground_package = self._foreground_package()
                    if (
                        foreground_package is not None
                        and foreground_package not in allowed_foreground
                    ):
                        agent.stop()  # type: ignore[attr-defined]
                        state = agent.snapshot()  # type: ignore[attr-defined]
                        compact = self._compact_agent_state(state)
                        compact.update(
                            {
                                "status": "forbidden_foreground",
                                "running": False,
                                "message": (
                                    "workflow left its allowed foreground packages; "
                                    f"observed {foreground_package}"
                                ),
                                "foreground_package": foreground_package,
                            }
                        )
                        journal.emit(
                            "agent_forbidden_foreground",
                            workflow_name=workflow_name,
                            foreground_package=foreground_package,
                            allowed_foreground_packages=sorted(allowed_foreground),
                        )
                        journal.emit(
                            "agent_end", workflow_name=workflow_name, state=compact
                        )
                        return compact
                sleep_for = poll_interval_s
                if deadline is not None:
                    sleep_for = min(sleep_for, max(0.01, deadline - now))
                self._sleep(sleep_for)
                state = agent.snapshot()  # type: ignore[attr-defined]
            state = agent.snapshot()  # type: ignore[attr-defined]
            compact = self._compact_agent_state(state)
            journal.emit("agent_end", workflow_name=workflow_name, state=compact)
            return compact
        except KeyboardInterrupt:
            try:
                agent.stop()  # type: ignore[attr-defined]
            finally:
                journal.emit("agent_interrupted", workflow_name=workflow_name)
            raise
        except Exception as exc:
            compact = {
                "status": "error",
                "running": False,
                "message": "agent workflow failed",
                "error": str(exc),
            }
            journal.emit("agent_end", workflow_name=workflow_name, state=compact)
            return compact
        finally:
            self._active_agent = None

    def _preparation_policy(self, app: PreparationAppConfig) -> tuple[str, str]:
        permissions = ", ".join(permission.name for permission in app.permissions) or "无"
        terms = (
            "用户已明确授权本次测试设备接受该应用自身的用户协议和隐私政策；可以点击同意/接受。"
            if app.allow_terms_acceptance
            else "配置未授权接受该应用协议；若协议是进入主功能的前置条件，立即 take_over。"
        )
        prompt = "\n\n".join(
            part
            for part in (
                self.config.preparation.prompt_prefix or PREPARATION_DEFAULT_PROMPT,
                f"目标应用：{app.name}（{app.package}）。",
                f"配置允许的运行时权限：{permissions}。只允许这些权限。",
                terms,
            )
            if part
        )
        attention = "\n\n".join(
            part
            for part in (
                self.config.preparation.attention_prompt
                or PREPARATION_DEFAULT_ATTENTION,
                "即使配置允许接受应用协议，也仍然禁止账号登录、验证码和实名认证。",
            )
            if part
        )
        return prompt, attention

    @staticmethod
    def _default_setup_task(app: PreparationAppConfig) -> AgentTaskConfig:
        return AgentTaskConfig(
            task_id="first-launch-setup",
            name=f"{app.name} 首次启动初始化",
            prompt=(
                "目标应用已经由宿主启动。处理完整首屏和后续有限数量的首次启动弹窗，"
                "直到应用停在可重复进入的稳定主界面。确认界面仍可交互后 finish。"
            ),
            attention_prompt=(
                "关闭非必要营销、更新和功能介绍弹窗；权限与协议严格遵守预备阶段策略。"
            ),
            max_steps=20,
            timeout_s=240.0,
            on_failure="stop",
        )

    def _preparation_validation_policy(
        self,
        workflow: WorkflowConfig,
        *,
        phase: str = "validation",
    ) -> tuple[str, str]:
        contract_prompt, contract_attention = self._workflow_contract_policy(workflow)
        phase_prompt = (
            "当前是独立初始化阶段，只把应用恢复到契约入口，不执行主功能验证动作。"
            if phase == "initialization"
            else "当前是主验证阶段，必须完成本次新的功能动作和动作后证据。"
        )
        prompt = "\n\n".join(
            part
            for part in (
                self.config.test.prompt_prefix or TEST_DEFAULT_PROMPT,
                "这是预备阶段安装和初始化之后的正式流程支持验证。必须按实际测试阶段相同的成功标准完成当前任务，不能只确认应用能够打开。",
                f"目标应用是 {workflow.name}（{workflow.package}），应用已由宿主启动。",
                phase_prompt,
                contract_prompt,
            )
            if part
        )
        attention = "\n\n".join(
            part
            for part in (
                self.config.test.attention_prompt or TEST_DEFAULT_ATTENTION,
                contract_attention,
            )
            if part
        )
        return prompt, attention

    def _validate_prepared_app(
        self,
        app: PreparationAppConfig,
        output_dir: Path,
        journal: CampaignJournal,
    ) -> list[dict[str, object]]:
        workflows = tuple(
            workflow
            for workflow in self._ordered_test_workflows()
            if workflow.package == app.package
        )
        if not workflows:
            return [
                {
                    "workflow_id": "",
                    "name": app.name,
                    "package": app.package,
                    "status": "no_workflow",
                    "succeeded": False,
                    "message": "没有配置实际测试 workflow，无法判定 Qwen 正常流程支持",
                }
            ]

        results: list[dict[str, object]] = []
        for workflow in workflows:
            if self._stop_event.is_set():
                results.append(
                    {
                        "workflow_id": workflow.workflow_id,
                        "name": workflow.name,
                        "package": workflow.package,
                        "status": "operator_stopped",
                        "succeeded": False,
                    }
                )
                break
            interaction = self._ensure_interactive()
            if not interaction["succeeded"]:
                result = {
                    "workflow_id": workflow.workflow_id,
                    "name": workflow.name,
                    "package": workflow.package,
                    "status": "device_locked",
                    "succeeded": False,
                    "interaction": interaction,
                }
                results.append(result)
                journal.emit("support_validation_end", result=result)
                continue
            launch = self._launch(
                workflow.package,
                force_stop_before_launch=workflow.force_stop_before_launch,
            )
            if workflow.launch_wait_s:
                self._sleep(workflow.launch_wait_s)
            journal.emit(
                "support_validation_start",
                workflow_id=workflow.workflow_id,
                package=workflow.package,
            )
            initialization_state: dict[str, object] = {}
            initialization_ok = launch.returncode == 0
            initialization_agent_complete = True
            initialization_foreground_package: Optional[str] = None
            allowed_packages = set(
                workflow.contract.allowed_foreground_packages
                or (workflow.package,)
            )
            if launch.returncode == 0 and workflow.initialization_tasks:
                init_prompt, init_attention = self._preparation_validation_policy(
                    workflow,
                    phase="initialization",
                )
                initialization_state = self._run_agent(
                    output_dir,
                    f"prepare-initialize-{_slug(workflow.workflow_id)}",
                    self._overridden_tasks(workflow.initialization_tasks),
                    init_prompt,
                    init_attention,
                    self.config.preparation.agent_poll_interval_s,
                    journal,
                    device_offline_grace_s=30.0,
                    payload_overrides=self._workflow_payload_overrides(workflow),
                    allowed_foreground_packages=tuple(allowed_packages),
                )
                initialization_agent_complete = self._agent_evidence_complete(
                    initialization_state
                )
                if self.device_available():
                    initialization_foreground_package = self._foreground_package()
                initialization_ok = (
                    initialization_agent_complete
                    and initialization_foreground_package in allowed_packages
                )
            prompt, attention = self._preparation_validation_policy(workflow)
            agent_state = (
                self._run_agent(
                    output_dir,
                    f"prepare-validate-{_slug(workflow.workflow_id)}",
                    self._overridden_tasks(workflow.tasks),
                    prompt,
                    attention,
                    self.config.preparation.agent_poll_interval_s,
                    journal,
                    device_offline_grace_s=30.0,
                    payload_overrides=self._workflow_payload_overrides(workflow),
                    allowed_foreground_packages=tuple(allowed_packages),
                    finish_action_requirements=workflow.contract.required_actions,
                )
                if launch.returncode == 0 and initialization_ok
                else {}
            )
            agent_status = str(agent_state.get("status") or "")
            initialization_status = str(initialization_state.get("status") or "")
            agent_result_complete = self._agent_result_complete(agent_state)
            completion_claim = self._agent_completion_claim(agent_state)
            initialization_completion_claim = self._agent_completion_claim(
                initialization_state
            )
            action_evidence = self._agent_action_evidence(agent_state, workflow)
            foreground_package = (
                self._foreground_package()
                if self.device_available()
                and (agent_status or initialization_state)
                else None
            )
            foreground_verified = bool(foreground_package)
            allowed_packages = set(
                workflow.contract.allowed_foreground_packages
                or (workflow.package,)
            )
            foreground_matches = foreground_package in allowed_packages
            effective_status = (
                "launch_failed"
                if launch.returncode != 0
                else "device_unavailable"
                if initialization_status == "device_unavailable"
                else "take_over"
                if initialization_status == "take_over"
                else "forbidden_foreground"
                if initialization_status == "forbidden_foreground"
                else "wrong_foreground"
                if workflow.initialization_tasks
                and initialization_agent_complete
                and initialization_foreground_package not in allowed_packages
                else "initialization_failed"
                if not initialization_ok
                else "forbidden_foreground"
                if agent_status == "forbidden_foreground"
                else "wrong_foreground"
                if foreground_verified and not foreground_matches
                else "contradicted_completion_claim"
                if agent_result_complete
                and completion_claim["satisfied"] is not True
                else "foreground_unverified"
                if self._agent_evidence_complete(agent_state) and not foreground_verified
                else "incomplete_action_evidence"
                if self._agent_evidence_complete(agent_state)
                and action_evidence["satisfied"] is not True
                else (
                    "incomplete_evidence"
                    if agent_status == "completed_with_warnings"
                    else agent_status
                )
            )
            if workflow.home_after and self.device_available():
                self._home()
            succeeded = (
                launch.returncode == 0
                and initialization_ok
                and self._agent_evidence_complete(agent_state)
                and action_evidence["satisfied"] is True
                and foreground_matches
            )
            result = {
                "workflow_id": workflow.workflow_id,
                "name": workflow.name,
                "package": workflow.package,
                "status": effective_status,
                "succeeded": succeeded,
                "launch_returncode": launch.returncode,
                "agent_status": agent_status,
                "initialization_status": initialization_status,
                "initialization_agent": initialization_state,
                "initialization_evidence_complete": initialization_ok,
                "initialization_completion_claim": initialization_completion_claim,
                "initialization_foreground_package": initialization_foreground_package,
                "foreground_package": foreground_package,
                "foreground_verified": foreground_verified,
                "foreground_matches": foreground_matches,
                "agent": agent_state,
                "completion_claim": completion_claim,
                "action_evidence": action_evidence,
                "evidence_complete": succeeded,
            }
            results.append(result)
            journal.emit("support_validation_end", result=result)
        return results

    def _install_from_store(
        self,
        app: PreparationAppConfig,
        output_dir: Path,
        journal: CampaignJournal,
    ) -> dict[str, object]:
        store_package = self.config.preparation.store_package
        if store_package and self.package_installed(store_package):
            self._launch(store_package)
            self._sleep(2.0)
        task = AgentTaskConfig(
            task_id=f"store-install-{app.package}",
            name=f"安装 {app.name}",
            prompt=app.install_prompt,
            attention_prompt=(
                f"只安装目标应用 {app.name}（期望包名 {app.package}）。"
                "允许确认系统安装器和应用商店的安装按钮；不得登录、付费或安装推荐应用。"
            ),
            max_steps=40,
            timeout_s=600.0,
            on_failure="stop",
        )
        task = self._task_overrides.get(task.task_id, task)
        state = self._run_agent(
            output_dir,
            f"prepare-install-{_slug(app.name)}",
            (task,),
            "这是预备阶段的软件安装任务。",
            PREPARATION_DEFAULT_ATTENTION,
            self.config.preparation.agent_poll_interval_s,
            journal,
            device_offline_grace_s=30.0,
        )
        installed = self.package_installed(app.package)
        return {
            "name": app.name,
            "package": app.package,
            "succeeded": installed,
            "agent": state,
        }

    def prepare(self, *, dry_run: bool = False) -> dict[str, object]:
        plan = {
            "stage": "preparation",
            "campaign_id": self.config.campaign_id,
            "device": self.device,
            "settings": [
                {
                    "namespace": item.namespace,
                    "key": item.key,
                    "value": item.value,
                    "required": item.required,
                }
                for item in self.config.preparation.settings
            ],
            "install_sets": [
                {
                    "name": item.name,
                    "package": item.package,
                    "source": str(item.source),
                    "required": item.required,
                }
                for item in self.config.preparation.install_sets
            ],
            "apps": [
                {
                    "name": item.name,
                    "package": item.package,
                    "catalog_status": item.catalog_status,
                    "software_type": item.software_type,
                    "install_mode": item.install_mode,
                    "install_channel": item.install_channel,
                    "install_source": item.install_source,
                    "official_url": item.official_url,
                    "supported_engines": list(item.supported_engines),
                    "description": item.description,
                    "required": item.required,
                    "permissions": [permission.name for permission in item.permissions],
                    "allow_terms_acceptance": item.allow_terms_acceptance,
                }
                for item in self._ordered_preparation_apps()
            ],
        }
        if dry_run:
            return {**plan, "status": "dry_run"}

        output_dir = self._stage_output("prepare")
        journal = CampaignJournal(output_dir)
        journal.emit("stage_start", stage="preparation", device=self.device)
        results: dict[str, object] = {
            **plan,
            "output_dir": str(output_dir),
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "current_app": None,
            "setting_results": [],
            "install_results": [],
            "app_results": [],
        }
        journal.state(**results)
        required_failures = 0
        optional_failures = 0
        stopped = False
        if not self.device_available():
            results.update(
                {
                    "status": "device_unavailable",
                    "message": "Android device is not available",
                    "finished_at": time.time(),
                }
            )
            journal.state(**results)
            return results
        interaction = self._ensure_interactive()
        results["interaction"] = interaction
        journal.emit("interaction_ready", result=interaction)
        if not interaction["succeeded"]:
            results.update(
                {
                    "status": "device_locked",
                    "message": str(interaction.get("message") or "device is not interactive"),
                    "finished_at": time.time(),
                }
            )
            journal.state(**results)
            return results

        for setting in self.config.preparation.settings:
            if self._stop_event.is_set():
                stopped = True
                break
            result = self._apply_setting(setting)
            results["setting_results"].append(result)  # type: ignore[union-attr]
            journal.emit("setting", result=result)
            journal.state(**results)
            if not result["succeeded"]:
                if setting.required:
                    required_failures += 1
                else:
                    optional_failures += 1

        for install_set in (
            self.config.preparation.install_sets if not stopped else ()
        ):
            if self._stop_event.is_set():
                stopped = True
                break
            result = self._install(install_set)
            results["install_results"].append(result)  # type: ignore[union-attr]
            journal.emit("install", result=result)
            journal.state(**results)
            if not result["succeeded"]:
                if install_set.required:
                    required_failures += 1
                else:
                    optional_failures += 1

        for app in (self._ordered_preparation_apps() if not stopped else ()):
            if self._stop_event.is_set():
                stopped = True
                break
            app_result: dict[str, object] = {
                "name": app.name,
                "package": app.package,
                "catalog_status": app.catalog_status,
                "software_type": app.software_type,
                "install_mode": app.install_mode,
                "install_channel": app.install_channel,
                "install_source": app.install_source,
                "official_url": app.official_url,
                "supported_engines": list(app.supported_engines),
                "description": app.description,
                "required": app.required,
                "permissions": [],
            }
            results["current_app"] = {
                "name": app.name,
                "package": app.package,
            }
            journal.state(**results)
            interaction = self._ensure_interactive()
            app_result["interaction"] = interaction
            journal.emit(
                "interaction_ready",
                app=app.name,
                package=app.package,
                result=interaction,
            )
            if not interaction["succeeded"]:
                app_result.update(
                    {
                        "status": "device_locked",
                        "succeeded": False,
                        "message": interaction.get("message"),
                    }
                )
                if app.required:
                    required_failures += 1
                else:
                    optional_failures += 1
                results["app_results"].append(app_result)  # type: ignore[union-attr]
                journal.emit("app_setup", result=app_result)
                results["current_app"] = None
                journal.state(**results)
                continue
            if not self.package_installed(app.package) and app.install_prompt:
                app_result["store_install"] = self._install_from_store(
                    app, output_dir, journal
                )
            if not self.package_installed(app.package):
                app_result.update(
                    {"status": "missing", "succeeded": False, "message": "package is not installed"}
                )
                if app.required:
                    required_failures += 1
                else:
                    optional_failures += 1
                results["app_results"].append(app_result)  # type: ignore[union-attr]
                journal.emit("app_setup", result=app_result)
                results["current_app"] = None
                journal.state(**results)
                continue

            permission_failed = False
            for permission in app.permissions:
                permission_result = self._grant_permission(app.package, permission)
                app_result["permissions"].append(permission_result)  # type: ignore[union-attr]
                journal.emit("permission", result=permission_result)
                if not permission_result["succeeded"] and permission.required:
                    permission_failed = True

            launch = self._launch(app.package)
            if app.launch_wait_s:
                self._sleep(app.launch_wait_s)
            prompt, attention = self._preparation_policy(app)
            tasks = self._overridden_tasks(
                app.setup_tasks or (self._default_setup_task(app),)
            )
            agent_state = self._run_agent(
                output_dir,
                f"prepare-{_slug(app.name)}",
                tasks,
                prompt,
                attention,
                self.config.preparation.agent_poll_interval_s,
                journal,
                device_offline_grace_s=30.0,
            )
            if app.home_after:
                self._home()
            agent_ok = agent_state.get("status") in {
                "completed",
                "completed_with_warnings",
            }
            workflow_validations = (
                self._validate_prepared_app(app, output_dir, journal)
                if launch.returncode == 0 and not permission_failed and agent_ok
                else []
            )
            normal_flow_supported = bool(workflow_validations) and all(
                validation.get("succeeded") is True
                for validation in workflow_validations
            )
            succeeded = (
                launch.returncode == 0
                and not permission_failed
                and agent_ok
                and normal_flow_supported
            )
            failed_validation = next(
                (
                    validation
                    for validation in workflow_validations
                    if validation.get("succeeded") is not True
                ),
                None,
            )
            final_status = (
                "completed"
                if succeeded
                else str(
                    (failed_validation or {}).get("status")
                    or agent_state.get("status")
                    or "failed"
                )
            )
            app_result.update(
                {
                    "status": final_status,
                    "succeeded": succeeded,
                    "agent": agent_state,
                    "setup_status": agent_state.get("status"),
                    "setup_succeeded": agent_ok,
                    "workflow_validations": workflow_validations,
                    "normal_flow_supported": normal_flow_supported,
                    "launch_output": self._command_output(launch),
                }
            )
            if not succeeded:
                if app.required:
                    required_failures += 1
                else:
                    optional_failures += 1
            results["app_results"].append(app_result)  # type: ignore[union-attr]
            journal.emit("app_setup", result=app_result)
            results["current_app"] = None
            journal.state(**results)

        status = (
            "operator_stopped"
            if stopped or self._stop_event.is_set()
            else "failed"
            if required_failures
            else "completed_with_warnings"
            if optional_failures
            else "completed"
        )
        results.update(
            {
                "status": status,
                "required_failures": required_failures,
                "optional_failures": optional_failures,
                "current_app": None,
                "finished_at": time.time(),
                "message": (
                    "campaign stopped by operator"
                    if status == "operator_stopped"
                    else ""
                ),
            }
        )
        journal.emit("stage_end", stage="preparation", status=status)
        journal.state(**results)
        return results

    def build_record_command(self, recording_dir: Path, duration_s: float) -> list[str]:
        recording = self.config.test.recording
        if not recording.enabled:
            return []
        command = [
            sys.executable,
            "-m",
            "mobile_profiler",
            "--adb",
            self.adb,
            "record",
            "--platform",
            "android",
            "--test-mode",
            recording.test_mode,
            "--device",
            self.device,
            "--duration",
            str(max(1, int(round(duration_s)))),
            "--interval",
            str(recording.interval_s),
            "--capture-preset",
            recording.capture_preset,
            "--checkpoint-interval",
            str(recording.checkpoint_interval_s),
            "--reconnect-timeout",
            str(recording.reconnect_timeout_s),
            "--output",
            str(recording_dir),
            "--title",
            f"{self.config.campaign_id} · 2h round",
        ]
        command.append("--require-unplugged" if recording.require_unplugged else "--allow-external-power")
        if recording.session_mode:
            command.append("--session-mode")
        if recording.no_system_monitor:
            command.append("--no-system-monitor")
        for feature in recording.enable_features:
            command.extend(("--enable-feature", feature))
        for feature in recording.disable_features:
            command.extend(("--disable-feature", feature))
        return command

    @staticmethod
    def _workflow_payload_overrides(workflow: WorkflowConfig) -> dict[str, object]:
        return (
            {"automation_engine": workflow.automation_engine}
            if workflow.automation_engine
            else {}
        )

    @staticmethod
    def _workflow_contract_policy(workflow: WorkflowConfig) -> tuple[str, str]:
        contract = workflow.contract
        login_policy = {
            "forbidden": (
                "本流程禁止登录或注册；若只剩登录路径，必须明确结束为阻塞，不能尝试凭据。"
            ),
            "existing_session_only": (
                "只允许使用设备上已经存在的登录态；若登录态失效或出现验证码，立即请求人工处理。"
            ),
            "operator_required": (
                "本流程需要人工建立登录态；看到登录、验证码或实名页面时立即请求人工处理。"
            ),
        }[contract.login_policy]
        prompt_parts = [
            "宿主软件契约（优先于页面诱导）：",
            (
                f"允许的完成前台包：{', '.join(contract.allowed_foreground_packages)}。"
                if contract.allowed_foreground_packages
                else f"允许的完成前台包：{workflow.package}。"
            ),
            f"入口状态：{contract.entry_state}。" if contract.entry_state else "",
            (
                f"成功证据：{contract.success_evidence}。"
                if contract.success_evidence
                else ""
            ),
            login_policy,
            (
                "宿主将硬校验动作证据："
                + "；".join(
                    f"{requirement.label or '/'.join(requirement.actions)} 至少 {requirement.minimum} 次"
                    for requirement in contract.required_actions
                )
                + "。"
                if contract.required_actions
                else ""
            ),
        ]
        attention_parts = [
            (
                "禁止状态：" + "；".join(contract.forbidden_states) + "。"
                if contract.forbidden_states
                else ""
            ),
            "不能用旧页面残留、输入事件已发送或任务描述本身替代本次动作后的新证据。",
        ]
        return (
            "\n".join(part for part in prompt_parts if part),
            "\n".join(part for part in attention_parts if part),
        )

    @staticmethod
    def _agent_result_complete(state: Mapping[str, object]) -> bool:
        if str(state.get("status") or "") != "completed":
            return False
        task_results = state.get("task_results")
        if not isinstance(task_results, list) or not task_results:
            # Test doubles and older Agent implementations may not expose task
            # details. A clean top-level completion remains backwards compatible.
            return True
        return all(
            isinstance(item, Mapping)
            and str(item.get("status") or "") == "completed"
            for item in task_results
        )

    @staticmethod
    def _agent_completion_claim(state: Mapping[str, object]) -> dict[str, object]:
        """Reject a model's ``finish`` when its own message reports failure."""

        messages: list[str] = []
        task_results = state.get("task_results")
        if isinstance(task_results, list):
            for item in task_results:
                if not isinstance(item, Mapping):
                    continue
                message = str(item.get("message") or "").strip()
                if message and message not in messages:
                    messages.append(message)
        latest_action = state.get("latest_action")
        if (
            isinstance(latest_action, Mapping)
            and str(latest_action.get("action") or "").strip().lower() == "finish"
        ):
            message = str(latest_action.get("message") or "").strip()
            if message and message not in messages:
                messages.append(message)
        contradictions = [
            {"message": message, "reason": reason}
            for message in messages
            if (reason := finish_message_contradiction(message))
        ]
        return {
            "satisfied": not contradictions,
            "messages": messages,
            "contradictions": contradictions,
        }

    @classmethod
    def _agent_evidence_complete(cls, state: Mapping[str, object]) -> bool:
        return (
            cls._agent_result_complete(state)
            and cls._agent_completion_claim(state)["satisfied"] is True
        )

    @staticmethod
    def _agent_action_evidence(
        state: Mapping[str, object],
        workflow: WorkflowConfig,
    ) -> dict[str, object]:
        requirements = workflow.contract.required_actions
        counts: dict[str, int] = {}
        events_path = Path(str(state.get("output_dir") or "")) / "events.jsonl"
        if requirements:
            try:
                lines = events_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, Mapping) or event.get("event_type") != "action":
                    continue
                if event.get("action_valid") is False:
                    continue
                action = event.get("action")
                if not isinstance(action, Mapping):
                    continue
                action_name = str(action.get("action") or "")
                if action_name:
                    counts[action_name] = counts.get(action_name, 0) + 1
        checks = [
            {
                "label": requirement.label or "/".join(requirement.actions),
                "actions": list(requirement.actions),
                "minimum": requirement.minimum,
                "observed": sum(counts.get(action, 0) for action in requirement.actions),
            }
            for requirement in requirements
        ]
        for check in checks:
            check["satisfied"] = check["observed"] >= check["minimum"]
        return {
            "satisfied": all(check["satisfied"] for check in checks),
            "counts": counts,
            "requirements": checks,
            "events_path": str(events_path) if requirements else "",
        }

    @staticmethod
    def _workflow_status_counts(
        results: Sequence[Mapping[str, object]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _round_coverage(
        self,
        results: Sequence[Mapping[str, object]],
        available_workflow_ids: set[str],
        quarantined: Mapping[str, str],
    ) -> dict[str, object]:
        ordered = self._ordered_test_workflows()
        required_ids = {
            workflow.workflow_id
            for workflow in ordered
            if workflow.required and workflow.workflow_id in available_workflow_ids
        }
        attempted_ids = {
            str(result.get("workflow_id") or "")
            for result in results
            if str(result.get("workflow_id") or "") in available_workflow_ids
        }
        successful_ids = {
            str(result.get("workflow_id") or "")
            for result in results
            if result.get("evidence_complete") is True
            and str(result.get("workflow_id") or "") in available_workflow_ids
        }
        available_count = len(available_workflow_ids)
        return {
            "configured_count": len(ordered),
            "available_count": available_count,
            "attempted_count": len(attempted_ids),
            "successful_count": len(successful_ids),
            "successful_ratio": (
                len(successful_ids) / available_count if available_count else 1.0
            ),
            "required_count": len(required_ids),
            "required_successful_count": len(required_ids & successful_ids),
            "all_available_attempted": attempted_ids >= available_workflow_ids,
            "all_available_completed": successful_ids >= available_workflow_ids,
            "all_required_completed": successful_ids >= required_ids,
            "successful_workflow_ids": sorted(successful_ids),
            "quarantined": dict(quarantined),
            "status_counts": self._workflow_status_counts(results),
        }

    def _test_policy(
        self,
        workflow: WorkflowConfig,
        round_index: int,
        *,
        phase: str = "validation",
    ) -> tuple[str, str]:
        contract_prompt, contract_attention = self._workflow_contract_policy(workflow)
        phase_prompt = (
            "当前是独立初始化阶段：只把应用恢复到软件契约的确定入口，不执行主验证动作。"
            if phase == "initialization"
            else (
                "当前是主验证阶段：宿主已单独完成配置中的初始化。必须执行并验证本次新的主功能动作；"
                "若仍看到可安全处理的普通前置页，先做最小修正，不能直接 skip。"
            )
        )
        prompt = "\n\n".join(
            part
            for part in (
                self.config.test.prompt_prefix or TEST_DEFAULT_PROMPT,
                f"当前是第 {round_index} 个两小时轮次，目标应用是 {workflow.name}（{workflow.package}）。",
                phase_prompt,
                contract_prompt,
            )
            if part
        )
        attention = "\n\n".join(
            part
            for part in (
                self.config.test.attention_prompt or TEST_DEFAULT_ATTENTION,
                contract_attention,
            )
            if part
        )
        return prompt, attention

    def _wait_recorder(self, recorder: RecorderProcess, timeout_s: float) -> Optional[int]:
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            exit_code = recorder.poll()
            if exit_code is not None:
                return exit_code
            self._sleep(min(1.0, max(0.01, deadline - self._clock())))
        return recorder.poll()

    def _recover_recording(self, recording_dir: Path, journal: CampaignJournal) -> None:
        if not recording_dir.exists() or not any(recording_dir.iterdir()):
            return
        command = [sys.executable, "-m", "mobile_profiler", "recover", str(recording_dir)]
        result = self._command_runner(command, 300.0)
        journal.emit(
            "record_recover",
            command=command,
            returncode=result.returncode,
            output=(result.stdout or result.stderr).strip(),
        )

    def _finalize_recorder(
        self,
        recorder: Optional[RecorderProcess],
        recording_dir: Path,
        journal: CampaignJournal,
        *,
        device_unavailable: bool,
        completed_early: bool = False,
    ) -> dict[str, object]:
        if recorder is None:
            return {"enabled": False, "exit_code": None, "terminated": False}
        timeout = 0.0 if completed_early else (
            self.config.test.shutdown_finalize_timeout_s
            if device_unavailable
            else self.config.test.record_finalize_timeout_s
        )
        exit_code = self._wait_recorder(recorder, timeout)
        terminated = False
        if exit_code is None:
            recorder.terminate()
            terminated = True
            try:
                exit_code = recorder.wait(timeout=15.0)
            except Exception:
                exit_code = recorder.poll()
        recorder.close()
        self._active_recorder = None
        if terminated or (exit_code not in (0, None)):
            self._recover_recording(recording_dir, journal)
        return {
            "enabled": True,
            "exit_code": exit_code,
            "terminated": terminated,
            "output_dir": str(recording_dir),
            **_recording_artifact_evidence(recording_dir),
        }

    @staticmethod
    def _journal_workflow_results(
        journal: CampaignJournal,
        round_index: int,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        try:
            lines = journal.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return results
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("event_type") == "workflow_end"
                and event.get("round_index") == round_index
                and isinstance(event.get("result"), dict)
            ):
                results.append(dict(event["result"]))
        return results

    def _run_round(
        self,
        campaign_dir: Path,
        round_index: int,
        journal: CampaignJournal,
    ) -> dict[str, object]:
        started = self._clock()
        try:
            return self._run_round_active(campaign_dir, round_index, journal)
        except KeyboardInterrupt:
            self.request_stop()
            round_dir = campaign_dir / f"round-{round_index:04d}"
            round_dir.mkdir(parents=True, exist_ok=True)
            recording_dir = round_dir / "recording"
            journal.emit("round_interrupted", round_index=round_index)
            record_result = self._finalize_recorder(
                self._active_recorder,
                recording_dir,
                journal,
                device_unavailable=False,
                completed_early=True,
            )
            result = {
                "round_index": round_index,
                "status": "stopped",
                "round_dir": str(round_dir),
                "duration_s": max(0.0, self._clock() - started),
                "workflow_results": self._journal_workflow_results(
                    journal, round_index
                ),
                "recording": record_result,
                "device_unavailable": False,
                "interaction_failed": False,
                "record_failed": False,
                "workflow_pass_complete": False,
                "operator_interrupted": True,
            }
            _write_json(round_dir / "round-summary.json", result)
            journal.emit("round_end", result=result)
            return result

    def _run_round_active(
        self,
        campaign_dir: Path,
        round_index: int,
        journal: CampaignJournal,
    ) -> dict[str, object]:
        round_dir = campaign_dir / f"round-{round_index:04d}"
        round_dir.mkdir(parents=True, exist_ok=False)
        recording_dir = round_dir / "recording"
        record_command = self.build_record_command(
            recording_dir, self.config.test.cycle_duration_s
        )
        recorder: Optional[RecorderProcess] = None
        if record_command:
            recorder = self._recorder_factory(record_command, round_dir / "record.log")
            self._active_recorder = recorder
        journal.emit(
            "round_start",
            round_index=round_index,
            round_dir=str(round_dir),
            record_command=record_command,
        )
        started = self._clock()
        deadline = started + self.config.test.cycle_duration_s
        ordered_workflows = self._ordered_test_workflows()
        workflow_results: list[dict[str, object]] = []
        disabled_workflows: set[str] = set()
        attempted_workflows: set[str] = set()
        successful_workflows: set[str] = set()
        failure_counts: dict[str, int] = {}
        cooldown_until: dict[str, float] = {}
        quarantined: dict[str, str] = {}
        workflow_index = 0
        offline_since: Optional[float] = None
        device_unavailable = False
        record_failed = False
        interaction_failed = False
        workflow_pass_complete = False

        package_available = {
            workflow.workflow_id: self.package_installed(workflow.package)
            for workflow in ordered_workflows
        }
        available_workflow_ids = {
            workflow_id
            for workflow_id, available in package_available.items()
            if available
        }
        for workflow in ordered_workflows:
            if package_available[workflow.workflow_id]:
                continue
            disabled_workflows.add(workflow.workflow_id)
            result = {
                "workflow_id": workflow.workflow_id,
                "name": workflow.name,
                "package": workflow.package,
                "status": "missing",
                "required": workflow.required,
                "evidence_complete": False,
            }
            workflow_results.append(result)
            journal.emit("workflow_end", round_index=round_index, result=result)

        def persist_progress(
            phase: str,
            current_workflow: Optional[WorkflowConfig] = None,
        ) -> None:
            now = self._clock()
            coverage = self._round_coverage(
                workflow_results,
                available_workflow_ids,
                quarantined,
            )
            recent_results = [
                {
                    "workflow_id": result.get("workflow_id"),
                    "status": result.get("status"),
                    "evidence_complete": result.get("evidence_complete", False),
                    "attempt": result.get("attempt"),
                }
                for result in workflow_results[-50:]
            ]
            progress = {
                "stage": "test",
                "status": "running",
                "campaign_id": self.config.campaign_id,
                "device": self.device,
                "output_dir": str(campaign_dir),
                "round_index": round_index,
                "round_dir": str(round_dir),
                "phase": phase,
                "current_workflow": (
                    {
                        "id": current_workflow.workflow_id,
                        "name": current_workflow.name,
                        "package": current_workflow.package,
                    }
                    if current_workflow is not None
                    else None
                ),
                "round_elapsed_s": max(0.0, now - started),
                "round_remaining_s": max(0.0, deadline - now),
                "coverage": coverage,
                "failure_counts": dict(failure_counts),
                "cooldown_workflow_ids": sorted(
                    workflow_id
                    for workflow_id, until in cooldown_until.items()
                    if until > now
                ),
                "recent_workflow_results": recent_results,
                "updated_at": time.time(),
            }
            journal.state(**progress)
            _write_json(round_dir / "round-progress.json", progress)

        persist_progress("recording_start_delay")

        if self.config.test.recording_start_delay_s:
            delay_deadline = min(
                deadline, started + self.config.test.recording_start_delay_s
            )
            while self._clock() < delay_deadline and not self._stop_event.is_set():
                self._sleep(
                    min(
                        self.config.test.device_poll_interval_s,
                        max(0.01, delay_deadline - self._clock()),
                    )
                )

        while self._clock() < deadline and not self._stop_event.is_set():
            now = self._clock()
            available = self.device_available()
            if not available:
                if offline_since is None:
                    offline_since = now
                    journal.emit("device_offline", round_index=round_index)
                if now - offline_since >= self.config.test.offline_grace_s:
                    device_unavailable = True
                    break
                self._sleep(self.config.test.device_poll_interval_s)
                continue
            if offline_since is not None:
                journal.emit(
                    "device_reconnected",
                    round_index=round_index,
                    unavailable_s=now - offline_since,
                )
                offline_since = None

            if recorder is not None:
                exit_code = recorder.poll()
                early_margin = max(5.0, self.config.test.device_poll_interval_s * 2.0)
                if exit_code is not None and now < deadline - early_margin:
                    record_failed = True
                    journal.emit(
                        "record_ended_early",
                        round_index=round_index,
                        exit_code=exit_code,
                        remaining_s=deadline - now,
                    )
                    break

            selected: Optional[WorkflowConfig] = None
            for offset in range(len(ordered_workflows)):
                candidate_index = (workflow_index + offset) % len(ordered_workflows)
                candidate = ordered_workflows[candidate_index]
                workflow_id = candidate.workflow_id
                if workflow_id in disabled_workflows:
                    continue
                if not package_available[workflow_id]:
                    continue
                if not self.repeat_workflows and workflow_id in attempted_workflows:
                    continue
                if (
                    self.repeat_workflows
                    and not candidate.repeat_after_success
                    and workflow_id in successful_workflows
                ):
                    continue
                if cooldown_until.get(workflow_id, 0.0) > now:
                    continue
                selected = candidate
                workflow_index = (candidate_index + 1) % len(ordered_workflows)
                break
            if selected is None:
                if not self.repeat_workflows:
                    workflow_pass_complete = True
                    break
                persist_progress("waiting_for_retry")
                self._sleep(self.config.test.device_poll_interval_s)
                continue
            workflow = selected
            attempted_workflows.add(workflow.workflow_id)
            persist_progress("ensuring_interactive", workflow)
            interaction = self._ensure_interactive()
            journal.emit(
                "interaction_ready",
                round_index=round_index,
                workflow_id=workflow.workflow_id,
                package=workflow.package,
                result=interaction,
            )
            if not interaction["succeeded"]:
                interaction_failed = True
                workflow_results.append(
                    {
                        "workflow_id": workflow.workflow_id,
                        "name": workflow.name,
                        "package": workflow.package,
                        "status": "device_locked",
                        "required": workflow.required,
                        "interaction": interaction,
                    }
                )
                break

            launch = self._launch(
                workflow.package,
                force_stop_before_launch=workflow.force_stop_before_launch,
            )
            if workflow.launch_wait_s:
                self._sleep(workflow.launch_wait_s)
            payload_overrides = self._workflow_payload_overrides(workflow)
            initialization_state: dict[str, object] = {}
            initialization_evidence_complete = not workflow.initialization_tasks
            initialization_agent_complete = not workflow.initialization_tasks
            initialization_foreground_package: Optional[str] = None
            allowed_packages = set(
                workflow.contract.allowed_foreground_packages
                or (workflow.package,)
            )
            if launch.returncode == 0 and workflow.initialization_tasks:
                persist_progress("initializing", workflow)
                init_prompt, init_attention = self._test_policy(
                    workflow,
                    round_index,
                    phase="initialization",
                )
                initialization_state = self._run_agent(
                    round_dir,
                    f"round-{round_index:04d}-{_slug(workflow.workflow_id)}-initialize",
                    self._overridden_tasks(workflow.initialization_tasks),
                    init_prompt,
                    init_attention,
                    self.config.test.agent_poll_interval_s,
                    journal,
                    device_offline_grace_s=self.config.test.offline_grace_s,
                    payload_overrides=payload_overrides,
                    deadline=deadline,
                    progress_callback=lambda: persist_progress(
                        "initializing", workflow
                    ),
                    allowed_foreground_packages=tuple(allowed_packages),
                )
                initialization_agent_complete = self._agent_evidence_complete(
                    initialization_state
                )
                if self.device_available():
                    initialization_foreground_package = self._foreground_package()
                initialization_evidence_complete = (
                    initialization_agent_complete
                    and initialization_foreground_package in allowed_packages
                )

            agent_state: dict[str, object] = {}
            if launch.returncode == 0 and initialization_evidence_complete:
                persist_progress("validating", workflow)
                prompt, attention = self._test_policy(workflow, round_index)
                agent_state = self._run_agent(
                    round_dir,
                    f"round-{round_index:04d}-{_slug(workflow.workflow_id)}-validate",
                    self._overridden_tasks(workflow.tasks),
                    prompt,
                    attention,
                    self.config.test.agent_poll_interval_s,
                    journal,
                    device_offline_grace_s=self.config.test.offline_grace_s,
                    payload_overrides=payload_overrides,
                    deadline=deadline,
                    progress_callback=lambda: persist_progress(
                        "validating", workflow
                    ),
                    allowed_foreground_packages=tuple(allowed_packages),
                    finish_action_requirements=workflow.contract.required_actions,
                )
            agent_status = str(agent_state.get("status") or "")
            foreground_package = (
                self._foreground_package()
                if self.device_available()
                and agent_state
                else None
            )
            foreground_verified = bool(foreground_package)
            foreground_matches = foreground_package in allowed_packages
            agent_result_complete = self._agent_result_complete(agent_state)
            completion_claim = self._agent_completion_claim(agent_state)
            initialization_completion_claim = self._agent_completion_claim(
                initialization_state
            )
            agent_status_complete = self._agent_evidence_complete(agent_state)
            action_evidence = self._agent_action_evidence(agent_state, workflow)
            agent_evidence_complete = (
                agent_status_complete and action_evidence["satisfied"] is True
            )
            initialization_status = str(initialization_state.get("status") or "")
            effective_status = (
                "launch_failed"
                if launch.returncode != 0
                else "device_unavailable"
                if initialization_status == "device_unavailable"
                else "take_over"
                if initialization_status == "take_over"
                else "forbidden_foreground"
                if initialization_status == "forbidden_foreground"
                else "wrong_foreground"
                if workflow.initialization_tasks
                and initialization_agent_complete
                and initialization_foreground_package not in allowed_packages
                else "initialization_failed"
                if not initialization_evidence_complete
                else "device_unavailable"
                if agent_status == "device_unavailable"
                else "take_over"
                if agent_status == "take_over"
                else "forbidden_foreground"
                if agent_status == "forbidden_foreground"
                else "wrong_foreground"
                if foreground_verified and not foreground_matches
                else "contradicted_completion_claim"
                if agent_result_complete
                and completion_claim["satisfied"] is not True
                else "foreground_unverified"
                if agent_status_complete and not foreground_verified
                else "incomplete_action_evidence"
                if agent_status_complete and action_evidence["satisfied"] is not True
                else "completed"
                if agent_evidence_complete and foreground_matches
                else "incomplete_evidence"
                if agent_status in {"completed", "completed_with_warnings"}
                else agent_status or "error"
            )
            evidence_complete = effective_status == "completed"
            if workflow.home_after and self.device_available():
                self._home()
            attempt = 1 + sum(
                str(item.get("workflow_id") or "") == workflow.workflow_id
                for item in workflow_results
            )
            result = {
                "workflow_id": workflow.workflow_id,
                "name": workflow.name,
                "package": workflow.package,
                "required": workflow.required,
                "attempt": attempt,
                "automation_engine": (
                    workflow.automation_engine
                    or self.config.model.automation_engine
                ),
                "launch_returncode": launch.returncode,
                "status": effective_status,
                "agent_status": agent_status,
                "initialization_status": initialization_status,
                "initialization_evidence_complete": initialization_evidence_complete,
                "initialization_completion_claim": initialization_completion_claim,
                "initialization_foreground_package": initialization_foreground_package,
                "initialization_agent": initialization_state,
                "foreground_package": foreground_package,
                "foreground_verified": foreground_verified,
                "foreground_matches": foreground_matches,
                "agent": agent_state,
                "completion_claim": completion_claim,
                "action_evidence": action_evidence,
                "evidence_complete": evidence_complete,
            }
            workflow_results.append(result)
            journal.emit("workflow_end", round_index=round_index, result=result)
            if evidence_complete:
                successful_workflows.add(workflow.workflow_id)
                failure_counts[workflow.workflow_id] = 0
                cooldown_until.pop(workflow.workflow_id, None)
            else:
                failure_count = failure_counts.get(workflow.workflow_id, 0) + 1
                failure_counts[workflow.workflow_id] = failure_count
                terminal_failure = effective_status in {
                    "take_over",
                    "forbidden_foreground",
                    "wrong_foreground",
                    "foreground_unverified",
                }
                if terminal_failure or failure_count >= workflow.quarantine_after_failures:
                    disabled_workflows.add(workflow.workflow_id)
                    quarantined[workflow.workflow_id] = effective_status
                    journal.emit(
                        "workflow_quarantined",
                        round_index=round_index,
                        workflow_id=workflow.workflow_id,
                        reason=effective_status,
                        failure_count=failure_count,
                    )
                else:
                    retry_at = self._clock() + workflow.retry_cooldown_s
                    cooldown_until[workflow.workflow_id] = retry_at
                    journal.emit(
                        "workflow_cooldown",
                        round_index=round_index,
                        workflow_id=workflow.workflow_id,
                        reason=effective_status,
                        failure_count=failure_count,
                        cooldown_s=workflow.retry_cooldown_s,
                    )
            if effective_status in {"take_over", "wrong_foreground"}:
                disabled_workflows.add(workflow.workflow_id)
                journal.emit(
                    "workflow_disabled",
                    round_index=round_index,
                    workflow_id=workflow.workflow_id,
                    reason=effective_status,
                )
            persist_progress("workflow_complete", workflow)
            if effective_status == "device_unavailable":
                device_unavailable = True
                break
            idle_deadline = min(deadline, self._clock() + workflow.idle_after_s)
            while self._clock() < idle_deadline and not self._stop_event.is_set():
                self._sleep(
                    min(
                        self.config.test.device_poll_interval_s,
                        max(0.01, idle_deadline - self._clock()),
                    )
                )

        active_finished = self._clock()
        persist_progress("finalizing_recording")
        record_result = self._finalize_recorder(
            recorder,
            recording_dir,
            journal,
            device_unavailable=device_unavailable,
            completed_early=(
                workflow_pass_complete
                or interaction_failed
                or self._stop_event.is_set()
            ),
        )
        coverage = self._round_coverage(
            workflow_results,
            available_workflow_ids,
            quarantined,
        )
        active_duration_s = max(0.0, active_finished - started)
        duration_reached = active_finished >= deadline
        duration_requirement_met = duration_reached or workflow_pass_complete
        minimum_recording_samples = _minimum_recording_sample_count(
            active_duration_s,
            self.config.test.recording.interval_s,
        )
        recording_sample_coverage_ok = (
            not record_result.get("enabled")
            or int(record_result.get("sample_count") or 0)
            >= minimum_recording_samples
        )
        recording_ok = (
            not record_result.get("enabled")
            or (
                record_result.get("exit_code") == 0
                and record_result.get("terminated") is not True
                and record_result.get("artifacts_complete") is True
                and recording_sample_coverage_ok
            )
        )
        no_operator_takeover = not any(
            str(item.get("status") or "") == "take_over"
            for item in workflow_results
        )
        acceptance = {
            "passed": bool(
                duration_requirement_met
                and recording_ok
                and coverage["all_available_completed"]
                and coverage["all_required_completed"]
                and no_operator_takeover
                and not quarantined
                and not device_unavailable
                and not interaction_failed
                and not record_failed
                and not self._stop_event.is_set()
            ),
            "duration_reached": duration_reached,
            "duration_requirement_met": duration_requirement_met,
            "recording_ok": recording_ok,
            "recording_artifacts_complete": (
                not record_result.get("enabled")
                or record_result.get("artifacts_complete") is True
            ),
            "recording_sample_coverage_ok": recording_sample_coverage_ok,
            "recording_sample_count": int(record_result.get("sample_count") or 0),
            "minimum_recording_sample_count": (
                minimum_recording_samples if record_result.get("enabled") else 0
            ),
            "all_available_completed": coverage["all_available_completed"],
            "all_required_completed": coverage["all_required_completed"],
            "agent_only": no_operator_takeover,
            "no_quarantined_workflows": not quarantined,
        }
        status = (
            "device_unavailable"
            if device_unavailable
            else "interaction_failed"
            if interaction_failed
            else "record_failed"
            if record_failed
            else "stopped"
            if self._stop_event.is_set()
            else "completed"
            if acceptance["passed"]
            else "completed_with_warnings"
        )
        result = {
            "round_index": round_index,
            "status": status,
            "round_dir": str(round_dir),
            "duration_s": max(0.0, self._clock() - started),
            "active_duration_s": active_duration_s,
            "workflow_results": workflow_results,
            "recording": record_result,
            "coverage": coverage,
            "acceptance": acceptance,
            "quarantined_workflows": dict(quarantined),
            "failure_counts": dict(failure_counts),
            "device_unavailable": device_unavailable,
            "interaction_failed": interaction_failed,
            "record_failed": record_failed,
            "workflow_pass_complete": workflow_pass_complete,
        }
        _write_json(round_dir / "round-summary.json", result)
        _write_json(round_dir / "round-progress.json", result)
        journal.emit("round_end", result=result)
        return result

    def test_plan(self) -> dict[str, object]:
        placeholder = self.output_root / "round-0001" / "recording"
        return {
            "stage": "test",
            "status": "dry_run",
            "campaign_id": self.config.campaign_id,
            "device": self.device,
            "cycle_duration_s": self.config.test.cycle_duration_s,
            "stop_condition": "device unavailable for offline_grace_s",
            "offline_grace_s": self.config.test.offline_grace_s,
            "repeat_workflows": self.repeat_workflows,
            "record_command": self.build_record_command(
                placeholder, self.config.test.cycle_duration_s
            ),
            "workflows": [
                {
                    "id": workflow.workflow_id,
                    "name": workflow.name,
                    "package": workflow.package,
                    "required": workflow.required,
                    "automation_engine": (
                        workflow.automation_engine
                        or self.config.model.automation_engine
                    ),
                    "initialization_task_count": len(workflow.initialization_tasks),
                    "task_count": len(workflow.tasks),
                    "idle_after_s": workflow.idle_after_s,
                    "quarantine_after_failures": workflow.quarantine_after_failures,
                    "retry_cooldown_s": workflow.retry_cooldown_s,
                    "contract": {
                        "entry_state": workflow.contract.entry_state,
                        "success_evidence": workflow.contract.success_evidence,
                        "forbidden_states": list(workflow.contract.forbidden_states),
                        "login_policy": workflow.contract.login_policy,
                        "allowed_foreground_packages": list(
                            workflow.contract.allowed_foreground_packages
                            or (workflow.package,)
                        ),
                        "required_actions": [
                            {
                                "actions": list(requirement.actions),
                                "minimum": requirement.minimum,
                                "label": requirement.label,
                            }
                            for requirement in workflow.contract.required_actions
                        ],
                    },
                }
                for workflow in self._ordered_test_workflows()
            ],
        }

    def run_test(
        self,
        *,
        dry_run: bool = False,
        max_rounds: Optional[int] = None,
    ) -> dict[str, object]:
        if max_rounds is not None and max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        if dry_run:
            return self.test_plan()

        campaign_dir = self._stage_output("test")
        journal = CampaignJournal(campaign_dir)
        journal.emit(
            "stage_start",
            stage="test",
            device=self.device,
            cycle_duration_s=self.config.test.cycle_duration_s,
        )
        rounds: list[dict[str, object]] = []
        status = "running"
        message = ""
        missing_required_packages: list[str] = []
        interaction: dict[str, object] = {}
        try:
            if not self.device_available():
                status = "device_unavailable"
                message = "Android device is not available at test start"
            else:
                interaction = self._ensure_interactive()
                journal.emit("interaction_ready", result=interaction)
                if not interaction["succeeded"]:
                    status = "device_locked"
                    message = str(
                        interaction.get("message") or "device is not interactive"
                    )
                else:
                    missing_required_packages = [
                        workflow.package
                        for workflow in self._ordered_test_workflows()
                        if workflow.required and not self.package_installed(workflow.package)
                    ]
                if status == "running" and missing_required_packages:
                    status = "missing_required_packages"
                    message = (
                        "required workflow packages are not installed: "
                        + ", ".join(missing_required_packages)
                    )
                elif status == "running":
                    round_index = 1
                    while not self._stop_event.is_set():
                        if max_rounds is not None and round_index > max_rounds:
                            status = "max_rounds"
                            message = f"completed requested {max_rounds} rounds"
                            break
                        result = self._run_round(campaign_dir, round_index, journal)
                        rounds.append(result)
                        if result["device_unavailable"]:
                            status = "device_shutdown_or_unavailable"
                            message = (
                                "device remained unavailable for the configured grace period; "
                                "campaign stopped as the observable shutdown condition"
                            )
                            break
                        if result["record_failed"]:
                            status = "record_failed"
                            message = "profiler recording ended before the two-hour round"
                            break
                        if result["interaction_failed"]:
                            status = "device_locked"
                            message = "device became non-interactive during the round"
                            break
                        if result["status"] == "stopped":
                            status = "operator_stopped"
                            message = "campaign stopped by operator"
                            break
                        round_index += 1
        except KeyboardInterrupt:
            self.request_stop()
            status = "operator_stopped"
            message = "campaign interrupted by operator"
        finally:
            self.request_stop()
            recorder = self._active_recorder
            if recorder is not None:
                try:
                    recorder.terminate()
                except Exception:
                    pass
                try:
                    recorder.close()
                except Exception:
                    pass
                self._active_recorder = None

        accepted_rounds = sum(
            isinstance(round_result.get("acceptance"), Mapping)
            and round_result["acceptance"].get("passed") is True
            for round_result in rounds
        )
        strict_acceptance = {
            "passed": bool(rounds) and accepted_rounds == len(rounds),
            "round_count": len(rounds),
            "accepted_round_count": accepted_rounds,
        }
        result = {
            "stage": "test",
            "campaign_id": self.config.campaign_id,
            "device": self.device,
            "status": status,
            "message": message,
            "output_dir": str(campaign_dir),
            "round_count": len(rounds),
            "rounds": rounds,
            "acceptance": strict_acceptance,
            "repeat_workflows": self.repeat_workflows,
            "interaction": interaction,
            "missing_required_packages": missing_required_packages,
        }
        journal.emit("stage_end", stage="test", status=status, message=message)
        journal.state(**result)
        return result
