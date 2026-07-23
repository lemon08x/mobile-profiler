"""Validated configuration for long-running two-stage Android campaigns."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adb_agent import (
    AUTOMATION_ENGINE_HYBRID,
    AUTOMATION_ENGINE_UIAUTOMATOR2,
    AUTOMATION_ENGINE_VISION,
    DEFAULT_AUTOMATION_ENGINE,
    DEFAULT_MODEL,
    DEFAULT_MODEL_API_BASE_URL,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODEL_THINKING_MODE,
    normalize_automation_engine,
)
from .features import capture_feature_names, capture_preset_names


CAMPAIGN_CONFIG_VERSION = 1

_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")
_PERMISSION_PATTERN = re.compile(r"[A-Za-z0-9_.]+")
_SETTING_VALUE_PATTERN = re.compile(r"[^\r\n\x00]{1,200}")

# The campaign runner deliberately does not expose arbitrary ``settings put``.
# New keys should be added only when they are useful for repeatable test setup.
ALLOWED_ANDROID_SETTINGS = frozenset(
    {
        ("global", "animator_duration_scale"),
        ("global", "auto_time"),
        ("global", "auto_time_zone"),
        ("global", "low_power"),
        ("global", "peak_refresh_rate"),
        ("global", "stay_on_while_plugged_in"),
        ("global", "transition_animation_scale"),
        ("global", "window_animation_scale"),
        ("secure", "location_mode"),
        ("system", "accelerometer_rotation"),
        ("system", "min_refresh_rate"),
        ("system", "peak_refresh_rate"),
        ("system", "screen_brightness"),
        ("system", "screen_brightness_mode"),
        ("system", "screen_off_timeout"),
        ("system", "user_rotation"),
    }
)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _text(value: object, label: str, *, required: bool = False, limit: int = 6000) -> str:
    text = ("" if value is None else str(value)).replace("\r\n", "\n").replace("\r", "\n").strip()
    if required and not text:
        raise ValueError(f"{label} cannot be empty")
    if len(text) > limit:
        raise ValueError(f"{label} cannot exceed {limit} characters")
    return text


def _bool(value: object, default: bool) -> bool:
    return default if value is None else bool(value)


def _choice(
    value: object,
    label: str,
    *,
    choices: set[str],
    default: str,
) -> str:
    selected = _text(value or default, label, required=True, limit=80).lower()
    if selected not in choices:
        raise ValueError(f"{label} must be one of {', '.join(sorted(choices))}")
    return selected


def _positive_float(value: object, label: str, default: float) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _non_negative_float(value: object, label: str, default: float) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"{label} cannot be negative")
    return parsed


def _positive_int(value: object, label: str, default: int) -> int:
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _package(value: object, label: str, *, required: bool = True) -> str:
    package = _text(value, label, required=required, limit=300)
    if package and not _PACKAGE_PATTERN.fullmatch(package):
        raise ValueError(f"{label} is not a valid Android package name")
    return package


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, item in enumerate(_sequence(value, label), 1):
        text = _text(item, f"{label}[{index}]", required=True, limit=200)
        if text not in result:
            result.append(text)
    return tuple(result)


def _resolve_path(value: object, label: str, base_dir: Path) -> Path:
    text = _text(value, label, required=True, limit=1000)
    path = Path(text).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


@dataclass(frozen=True)
class AgentTaskConfig:
    task_id: str
    name: str
    prompt: str
    attention_prompt: str = ""
    max_steps: int = 12
    timeout_s: float = 120.0
    on_failure: str = "stop"

    @classmethod
    def from_mapping(
        cls,
        value: object,
        index: int,
        *,
        default_on_failure: str,
    ) -> "AgentTaskConfig":
        data = _mapping(value, f"task {index}")
        on_failure = _text(
            data.get("on_failure") or default_on_failure,
            f"task {index} on_failure",
            required=True,
            limit=20,
        ).lower()
        if on_failure not in {"stop", "continue"}:
            raise ValueError(f"task {index} on_failure must be stop or continue")
        return cls(
            task_id=_text(data.get("id") or f"task-{index}", f"task {index} id", required=True, limit=64),
            name=_text(data.get("name") or f"Task {index}", f"task {index} name", required=True, limit=120),
            prompt=_text(data.get("prompt"), f"task {index} prompt", required=True),
            attention_prompt=_text(
                data.get("attention_prompt"),
                f"task {index} attention_prompt",
                limit=3000,
            ),
            max_steps=_positive_int(data.get("max_steps"), f"task {index} max_steps", 12),
            timeout_s=_positive_float(data.get("timeout_s"), f"task {index} timeout_s", 120.0),
            on_failure=on_failure,
        )

    def payload(self, prompt_prefix: str = "", attention_prefix: str = "") -> dict[str, object]:
        prompt = "\n\n".join(part for part in (prompt_prefix.strip(), self.prompt) if part.strip())
        attention = "\n\n".join(
            part for part in (attention_prefix.strip(), self.attention_prompt) if part.strip()
        )
        return {
            "id": self.task_id,
            "name": self.name,
            "prompt": prompt,
            "attention_prompt": attention,
            "max_steps": self.max_steps,
            "timeout_s": self.timeout_s,
            "on_failure": self.on_failure,
        }


@dataclass(frozen=True)
class ModelConfig:
    automation_engine: str = DEFAULT_AUTOMATION_ENGINE
    provider: str = DEFAULT_MODEL_PROVIDER
    api_base_url: str = DEFAULT_MODEL_API_BASE_URL
    model: str = DEFAULT_MODEL
    thinking_mode: str = DEFAULT_MODEL_THINKING_MODE
    api_key_mode: str = "none"
    api_key_env: str = "MOBILE_PROFILER_MODEL_API_KEY"
    api_base_url_env: str = "MOBILE_PROFILER_MODEL_ENDPOINT"
    model_env: str = "MOBILE_PROFILER_MODEL_NAME"
    system_prompt: str = ""
    system_prompt_file: Path | None = None
    step_delay_s: float = 0.8
    request_timeout_s: float = 60.0

    @classmethod
    def from_mapping(cls, value: object, base_dir: Path) -> "ModelConfig":
        data = _mapping(value, "model")
        prompt = _text(data.get("system_prompt"), "model system_prompt", limit=24000)
        prompt_file_value = data.get("system_prompt_file")
        if prompt and prompt_file_value:
            raise ValueError("model may specify only one of system_prompt and system_prompt_file")
        prompt_file = (
            _resolve_path(prompt_file_value, "model system_prompt_file", base_dir)
            if prompt_file_value
            else None
        )
        return cls(
            automation_engine=normalize_automation_engine(
                data.get("automation_engine")
                or data.get("engine")
                or DEFAULT_AUTOMATION_ENGINE
            ),
            provider=_text(data.get("provider") or DEFAULT_MODEL_PROVIDER, "model provider", required=True, limit=80),
            api_base_url=_text(
                data.get("api_base_url") or DEFAULT_MODEL_API_BASE_URL,
                "model api_base_url",
                required=True,
                limit=1000,
            ),
            model=_text(data.get("model") or DEFAULT_MODEL, "model name", required=True, limit=300),
            thinking_mode=_text(
                data.get("thinking_mode") or DEFAULT_MODEL_THINKING_MODE,
                "model thinking_mode",
                required=True,
                limit=20,
            ),
            api_key_mode=_text(data.get("api_key_mode") or "none", "model api_key_mode", required=True, limit=40),
            api_key_env=_text(data.get("api_key_env") or "MOBILE_PROFILER_MODEL_API_KEY", "model api_key_env", limit=200),
            api_base_url_env=_text(
                data.get("api_base_url_env") or "MOBILE_PROFILER_MODEL_ENDPOINT",
                "model api_base_url_env",
                limit=200,
            ),
            model_env=_text(data.get("model_env") or "MOBILE_PROFILER_MODEL_NAME", "model model_env", limit=200),
            system_prompt=prompt,
            system_prompt_file=prompt_file,
            step_delay_s=_positive_float(data.get("step_delay_s"), "model step_delay_s", 0.8),
            request_timeout_s=_positive_float(
                data.get("request_timeout_s"), "model request_timeout_s", 60.0
            ),
        )

    def payload(self) -> dict[str, object]:
        system_prompt = self.system_prompt
        if self.system_prompt_file is not None:
            system_prompt = self.system_prompt_file.read_text(encoding="utf-8")
        api_base_url = os.environ.get(self.api_base_url_env, "").strip() or self.api_base_url
        model = os.environ.get(self.model_env, "").strip() or self.model
        api_key = os.environ.get(self.api_key_env, "").strip() if self.api_key_env else ""
        return {
            "automation_engine": self.automation_engine,
            "model_provider": self.provider,
            "api_base_url": api_base_url,
            "model": model,
            "model_thinking_mode": self.thinking_mode,
            "api_key": api_key,
            "api_key_mode": self.api_key_mode,
            "system_prompt": system_prompt,
            "step_delay_s": self.step_delay_s,
            "request_timeout_s": self.request_timeout_s,
        }


@dataclass(frozen=True)
class SystemSettingConfig:
    namespace: str
    key: str
    value: str
    required: bool = True

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "SystemSettingConfig":
        data = _mapping(value, f"preparation setting {index}")
        namespace = _text(data.get("namespace"), f"setting {index} namespace", required=True, limit=20).lower()
        key = _text(data.get("key"), f"setting {index} key", required=True, limit=120)
        setting_value = _text(data.get("value"), f"setting {index} value", required=True, limit=200)
        if (namespace, key) not in ALLOWED_ANDROID_SETTINGS:
            raise ValueError(f"setting {namespace}.{key} is not allowlisted")
        if not _SETTING_VALUE_PATTERN.fullmatch(setting_value):
            raise ValueError(f"setting {namespace}.{key} has an invalid value")
        return cls(namespace, key, setting_value, _bool(data.get("required"), True))


@dataclass(frozen=True)
class InstallSetConfig:
    name: str
    package: str
    source: Path
    required: bool = True
    replace: bool = True
    allow_downgrade: bool = False
    timeout_s: float = 300.0

    @classmethod
    def from_mapping(cls, value: object, index: int, base_dir: Path) -> "InstallSetConfig":
        data = _mapping(value, f"install set {index}")
        return cls(
            name=_text(data.get("name") or f"Install set {index}", f"install set {index} name", required=True, limit=120),
            package=_package(data.get("package"), f"install set {index} package"),
            source=_resolve_path(data.get("source"), f"install set {index} source", base_dir),
            required=_bool(data.get("required"), True),
            replace=_bool(data.get("replace"), True),
            allow_downgrade=_bool(data.get("allow_downgrade"), False),
            timeout_s=_positive_float(data.get("timeout_s"), f"install set {index} timeout_s", 300.0),
        )


@dataclass(frozen=True)
class PermissionConfig:
    name: str
    required: bool = True

    @classmethod
    def from_value(cls, value: object, index: int, app_label: str) -> "PermissionConfig":
        if isinstance(value, str):
            name = value.strip()
            required = True
        else:
            data = _mapping(value, f"{app_label} permission {index}")
            name = _text(data.get("name"), f"{app_label} permission {index} name", required=True, limit=300)
            required = _bool(data.get("required"), True)
        if not _PERMISSION_PATTERN.fullmatch(name):
            raise ValueError(f"{app_label} permission {index} is invalid")
        return cls(name, required)


@dataclass(frozen=True)
class PreparationAppConfig:
    name: str
    package: str
    catalog_status: str = "supported"
    software_type: str = "app"
    install_mode: str = "project"
    install_channel: str = "project_apk"
    install_source: str = ""
    official_url: str = ""
    supported_engines: tuple[str, ...] = (
        AUTOMATION_ENGINE_VISION,
        AUTOMATION_ENGINE_UIAUTOMATOR2,
        AUTOMATION_ENGINE_HYBRID,
    )
    description: str = ""
    required: bool = True
    install_prompt: str = ""
    allow_terms_acceptance: bool = False
    permissions: tuple[PermissionConfig, ...] = ()
    setup_tasks: tuple[AgentTaskConfig, ...] = ()
    home_after: bool = True
    launch_wait_s: float = 2.0

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "PreparationAppConfig":
        data = _mapping(value, f"preparation app {index}")
        name = _text(data.get("name") or f"Preparation app {index}", f"preparation app {index} name", required=True, limit=120)
        permissions = tuple(
            PermissionConfig.from_value(item, permission_index, name)
            for permission_index, item in enumerate(
                _sequence(data.get("permissions"), f"{name} permissions"), 1
            )
        )
        raw_tasks = _sequence(data.get("setup_tasks"), f"{name} setup_tasks")
        tasks = tuple(
            AgentTaskConfig.from_mapping(item, task_index, default_on_failure="stop")
            for task_index, item in enumerate(raw_tasks, 1)
        )
        install_prompt = _text(data.get("install_prompt"), f"{name} install_prompt")
        install_mode = _choice(
            data.get("install_mode"),
            f"{name} install_mode",
            choices={"project", "external"},
            default="external" if install_prompt else "project",
        )
        install_channel = _choice(
            data.get("install_channel"),
            f"{name} install_channel",
            choices={
                "project_apk",
                "project_apks",
                "app_store",
                "official_website",
                "app_store_or_official",
            },
            default="app_store" if install_mode == "external" else "project_apk",
        )
        if install_mode == "project" and not install_channel.startswith("project_"):
            raise ValueError(f"{name} project install_mode requires a project install_channel")
        if install_mode == "external" and install_channel.startswith("project_"):
            raise ValueError(f"{name} external install_mode requires an external install_channel")
        raw_engines = _string_tuple(data.get("supported_engines"), f"{name} supported_engines")
        supported_engines = tuple(
            normalize_automation_engine(engine)
            for engine in (
                raw_engines
                or (
                    AUTOMATION_ENGINE_VISION,
                    AUTOMATION_ENGINE_UIAUTOMATOR2,
                    AUTOMATION_ENGINE_HYBRID,
                )
            )
        )
        return cls(
            name=name,
            package=_package(data.get("package"), f"preparation app {index} package"),
            catalog_status=_choice(
                data.get("catalog_status"),
                f"{name} catalog_status",
                choices={"supported", "pending_validation"},
                default="supported",
            ),
            software_type=_choice(
                data.get("software_type"),
                f"{name} software_type",
                choices={"app", "game"},
                default="app",
            ),
            install_mode=install_mode,
            install_channel=install_channel,
            install_source=_text(data.get("install_source"), f"{name} install_source", limit=300),
            official_url=_text(data.get("official_url"), f"{name} official_url", limit=1000),
            supported_engines=supported_engines,
            description=_text(data.get("description"), f"{name} description", limit=500),
            required=_bool(data.get("required"), True),
            install_prompt=install_prompt,
            allow_terms_acceptance=_bool(data.get("allow_terms_acceptance"), False),
            permissions=permissions,
            setup_tasks=tasks,
            home_after=_bool(data.get("home_after"), True),
            launch_wait_s=_non_negative_float(data.get("launch_wait_s"), f"{name} launch_wait_s", 2.0),
        )


@dataclass(frozen=True)
class PreparationStageConfig:
    settings: tuple[SystemSettingConfig, ...] = ()
    install_sets: tuple[InstallSetConfig, ...] = ()
    apps: tuple[PreparationAppConfig, ...] = ()
    store_package: str = ""
    prompt_prefix: str = ""
    attention_prompt: str = ""
    agent_poll_interval_s: float = 1.0

    @classmethod
    def from_mapping(cls, value: object, base_dir: Path) -> "PreparationStageConfig":
        data = _mapping(value, "preparation")
        return cls(
            settings=tuple(
                SystemSettingConfig.from_mapping(item, index)
                for index, item in enumerate(
                    _sequence(data.get("settings"), "preparation settings"), 1
                )
            ),
            install_sets=tuple(
                InstallSetConfig.from_mapping(item, index, base_dir)
                for index, item in enumerate(
                    _sequence(data.get("install_sets"), "preparation install_sets"), 1
                )
            ),
            apps=tuple(
                PreparationAppConfig.from_mapping(item, index)
                for index, item in enumerate(
                    _sequence(data.get("apps"), "preparation apps"), 1
                )
            ),
            store_package=_package(data.get("store_package"), "preparation store_package", required=False),
            prompt_prefix=_text(data.get("prompt_prefix"), "preparation prompt_prefix"),
            attention_prompt=_text(data.get("attention_prompt"), "preparation attention_prompt", limit=3000),
            agent_poll_interval_s=_positive_float(
                data.get("agent_poll_interval_s"), "preparation agent_poll_interval_s", 1.0
            ),
        )


@dataclass(frozen=True)
class WorkflowConfig:
    workflow_id: str
    name: str
    package: str
    tasks: tuple[AgentTaskConfig, ...]
    required: bool = True
    home_after: bool = True
    launch_wait_s: float = 2.0
    idle_after_s: float = 10.0

    @classmethod
    def from_mapping(cls, value: object, index: int) -> "WorkflowConfig":
        data = _mapping(value, f"test workflow {index}")
        name = _text(data.get("name") or f"Workflow {index}", f"workflow {index} name", required=True, limit=120)
        tasks = tuple(
            AgentTaskConfig.from_mapping(item, task_index, default_on_failure="continue")
            for task_index, item in enumerate(
                _sequence(data.get("tasks"), f"{name} tasks"), 1
            )
        )
        if not tasks:
            raise ValueError(f"{name} must contain at least one task")
        return cls(
            workflow_id=_text(data.get("id") or f"workflow-{index}", f"workflow {index} id", required=True, limit=64),
            name=name,
            package=_package(data.get("package"), f"workflow {index} package"),
            tasks=tasks,
            required=_bool(data.get("required"), True),
            home_after=_bool(data.get("home_after"), True),
            launch_wait_s=_non_negative_float(data.get("launch_wait_s"), f"{name} launch_wait_s", 2.0),
            idle_after_s=_non_negative_float(data.get("idle_after_s"), f"{name} idle_after_s", 10.0),
        )


@dataclass(frozen=True)
class RecordingConfig:
    enabled: bool = True
    test_mode: str = "power"
    capture_preset: str = "low-overhead"
    interval_s: float = 5.0
    session_mode: bool = True
    # Campaigns exercise app/game behavior and may run for hours while the
    # device is connected to a charger. Keep strict unplugged collection as an
    # explicit opt-in instead of blocking the functional test by default.
    require_unplugged: bool = False
    checkpoint_interval_s: float = 30.0
    reconnect_timeout_s: float = 120.0
    no_system_monitor: bool = False
    enable_features: tuple[str, ...] = ("thermal", "runtime_settings")
    disable_features: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> "RecordingConfig":
        data = _mapping(value, "test recording")
        test_mode = _text(data.get("test_mode") or "power", "recording test_mode", required=True, limit=20)
        if test_mode not in {"power", "performance"}:
            raise ValueError("recording test_mode must be power or performance")
        preset = _text(data.get("capture_preset") or "low-overhead", "recording capture_preset", required=True, limit=40)
        if preset not in capture_preset_names():
            raise ValueError(f"unknown recording capture_preset: {preset}")
        known_features = set(capture_feature_names())
        enable_features = _string_tuple(
            data.get("enable_features", ("thermal", "runtime_settings")),
            "recording enable_features",
        )
        disable_features = _string_tuple(data.get("disable_features"), "recording disable_features")
        unknown = (set(enable_features) | set(disable_features)) - known_features
        if unknown:
            raise ValueError(f"unknown recording features: {', '.join(sorted(unknown))}")
        if set(enable_features) & set(disable_features):
            raise ValueError("recording features cannot be both enabled and disabled")
        return cls(
            enabled=_bool(data.get("enabled"), True),
            test_mode=test_mode,
            capture_preset=preset,
            interval_s=_positive_float(data.get("interval_s"), "recording interval_s", 5.0),
            session_mode=_bool(data.get("session_mode"), True),
            require_unplugged=_bool(data.get("require_unplugged"), False),
            checkpoint_interval_s=_positive_float(
                data.get("checkpoint_interval_s"), "recording checkpoint_interval_s", 30.0
            ),
            reconnect_timeout_s=_positive_float(
                data.get("reconnect_timeout_s"), "recording reconnect_timeout_s", 120.0
            ),
            no_system_monitor=_bool(data.get("no_system_monitor"), False),
            enable_features=enable_features,
            disable_features=disable_features,
        )


@dataclass(frozen=True)
class TestStageConfig:
    cycle_duration_s: float
    workflows: tuple[WorkflowConfig, ...]
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    offline_grace_s: float = 120.0
    device_poll_interval_s: float = 2.0
    recording_start_delay_s: float = 8.0
    record_finalize_timeout_s: float = 300.0
    shutdown_finalize_timeout_s: float = 45.0
    agent_poll_interval_s: float = 1.0
    prompt_prefix: str = ""
    attention_prompt: str = ""

    @classmethod
    def from_mapping(cls, value: object) -> "TestStageConfig":
        data = _mapping(value, "test")
        workflows = tuple(
            WorkflowConfig.from_mapping(item, index)
            for index, item in enumerate(
                _sequence(data.get("workflows"), "test workflows"), 1
            )
        )
        if not workflows:
            raise ValueError("test must contain at least one workflow")
        return cls(
            cycle_duration_s=_positive_float(data.get("cycle_duration_s"), "test cycle_duration_s", 7200.0),
            workflows=workflows,
            recording=RecordingConfig.from_mapping(data.get("recording")),
            offline_grace_s=_positive_float(data.get("offline_grace_s"), "test offline_grace_s", 120.0),
            device_poll_interval_s=_positive_float(
                data.get("device_poll_interval_s"), "test device_poll_interval_s", 2.0
            ),
            recording_start_delay_s=_non_negative_float(
                data.get("recording_start_delay_s"), "test recording_start_delay_s", 8.0
            ),
            record_finalize_timeout_s=_positive_float(
                data.get("record_finalize_timeout_s"), "test record_finalize_timeout_s", 300.0
            ),
            shutdown_finalize_timeout_s=_positive_float(
                data.get("shutdown_finalize_timeout_s"), "test shutdown_finalize_timeout_s", 45.0
            ),
            agent_poll_interval_s=_positive_float(
                data.get("agent_poll_interval_s"), "test agent_poll_interval_s", 1.0
            ),
            prompt_prefix=_text(data.get("prompt_prefix"), "test prompt_prefix"),
            attention_prompt=_text(data.get("attention_prompt"), "test attention_prompt", limit=3000),
        )


@dataclass(frozen=True)
class CampaignConfig:
    version: int
    campaign_id: str
    device: str
    model: ModelConfig
    preparation: PreparationStageConfig
    test: TestStageConfig
    source_path: Path

    def with_device(self, device: str) -> "CampaignConfig":
        return CampaignConfig(
            self.version,
            self.campaign_id,
            _text(device, "device", required=True, limit=300),
            self.model,
            self.preparation,
            self.test,
            self.source_path,
        )


def load_campaign_config(path: Path) -> CampaignConfig:
    source_path = Path(path).expanduser().resolve()
    try:
        parsed = json.loads(source_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read campaign config {source_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid campaign JSON at line {exc.lineno}: {exc.msg}") from exc
    data = _mapping(parsed, "campaign config")
    version = _positive_int(data.get("version"), "campaign version", CAMPAIGN_CONFIG_VERSION)
    if version != CAMPAIGN_CONFIG_VERSION:
        raise ValueError(
            f"unsupported campaign config version {version}; expected {CAMPAIGN_CONFIG_VERSION}"
        )
    return CampaignConfig(
        version=version,
        campaign_id=_text(data.get("campaign_id"), "campaign_id", required=True, limit=120),
        device=_text(data.get("device"), "device", limit=300),
        model=ModelConfig.from_mapping(data.get("model"), source_path.parent),
        preparation=PreparationStageConfig.from_mapping(
            data.get("preparation"), source_path.parent
        ),
        test=TestStageConfig.from_mapping(data.get("test")),
        source_path=source_path,
    )
