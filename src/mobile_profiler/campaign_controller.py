"""Background controller used by the dashboard for two-stage Android campaigns."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, Mapping, Optional

from .adb_agent import normalize_agent_tasks
from .adb_agent_prompts import task_templates_snapshot
from .campaign import AndroidCampaignRunner
from .campaign_config import AgentTaskConfig, CampaignConfig, load_campaign_config


_MODEL_OVERRIDE_KEYS = {
    "automation_engine",
    "model_provider",
    "api_base_url",
    "model",
    "model_thinking_mode",
    "api_key",
    "api_key_mode",
    "system_prompt",
    "step_delay_s",
    "request_timeout_s",
}


def _campaign_template(stage: str) -> Dict[str, object]:
    for template in task_templates_snapshot():
        if template.get("kind") == "campaign" and template.get("campaign_stage") == stage:
            return template
    return {}


def _boolean(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


class CampaignController:
    """Run one preparation or endurance stage in a stoppable background thread."""

    def __init__(self, adb: str, output_root: Path, config_path: Optional[Path]) -> None:
        self.adb = str(adb or "adb")
        self.output_root = Path(output_root).resolve() / "campaigns"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path).resolve() if config_path is not None else None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[AndroidCampaignRunner] = None
        self._session: Optional[Dict[str, object]] = None
        self._logs: deque[Dict[str, object]] = deque(maxlen=400)
        self._screenshot_revision = 0
        self._screenshot_key = ""
        self._catalog_state_key = ""
        self._catalog_state_cache: Dict[str, object] = {}
        self._stage_config_cache_key = ""
        self._stage_config_cache: Dict[str, object] = {}
        self._installing_package = ""

    @property
    def available(self) -> bool:
        return self.config_path is not None and self.config_path.is_file()

    def _log_locked(self, level: str, message: str) -> None:
        self._logs.append(
            {"time": time.time(), "level": str(level or "info"), "message": str(message)}
        )

    def _load_config(self, device: str) -> CampaignConfig:
        if not self.available or self.config_path is None:
            raise RuntimeError("内置两阶段 Campaign 配置不可用")
        return load_campaign_config(self.config_path).with_device(device)

    def _catalog_config(self) -> CampaignConfig:
        if not self.available or self.config_path is None:
            raise RuntimeError("内置两阶段 Campaign 配置不可用")
        return load_campaign_config(self.config_path)

    def _latest_preparation_state(self) -> Dict[str, object]:
        candidates: list[tuple[int, Path]] = []
        try:
            directories = list(self.output_root.glob("*-prepare-*"))
        except OSError:
            directories = []
        for directory in directories:
            state_path = directory / "state.json"
            try:
                stat = state_path.stat()
            except OSError:
                continue
            candidates.append((stat.st_mtime_ns, state_path))
        if not candidates:
            return {}
        modified_ns, state_path = max(candidates, key=lambda item: item[0])
        cache_key = f"{state_path}:{modified_ns}"
        with self._lock:
            if cache_key == self._catalog_state_key:
                return dict(self._catalog_state_cache)
        try:
            parsed = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, Mapping) or parsed.get("stage") != "preparation":
            return {}
        state = dict(parsed)
        state["state_path"] = str(state_path)
        state["state_modified_at"] = modified_ns / 1_000_000_000
        with self._lock:
            self._catalog_state_key = cache_key
            self._catalog_state_cache = dict(state)
        return state

    @staticmethod
    def _catalog_validation_status(
        result: Optional[Mapping[str, object]],
        *,
        running: bool,
    ) -> str:
        if running:
            return "running"
        if result is None:
            return "not_checked"
        if (
            result.get("succeeded") is True
            and result.get("normal_flow_supported") is True
        ):
            return "verified"
        if result.get("normal_flow_supported") is None and result.get("succeeded") is True:
            return "not_checked"
        raw_status = str(result.get("status") or "").strip().lower()
        if raw_status == "missing":
            return "missing"
        if raw_status == "take_over":
            return "needs_attention"
        if raw_status in {"operator_stopped", "stopped"}:
            return "stopped"
        return "failed"

    @staticmethod
    def _task_config_snapshot(task: AgentTaskConfig) -> Dict[str, object]:
        return {
            "id": task.task_id,
            "name": task.name,
            "max_steps": task.max_steps,
            "timeout_s": task.timeout_s,
            "on_failure": task.on_failure,
            "action_limits": [limit.payload() for limit in task.action_limits],
        }

    @staticmethod
    def _task_payload_snapshot(task: AgentTaskConfig) -> Dict[str, object]:
        return {
            "id": task.task_id,
            "name": task.name,
            "prompt": task.prompt,
            "attention_prompt": task.attention_prompt,
            "max_steps": task.max_steps,
            "timeout_s": task.timeout_s,
            "on_failure": task.on_failure,
            "action_limits": [limit.payload() for limit in task.action_limits],
        }

    @staticmethod
    def _workflow_group_id(
        workflow_id: str,
        *,
        required: bool,
        software_type: str,
    ) -> str:
        if required:
            return "required_baseline"
        key = workflow_id.lower()
        if software_type == "game":
            return "games"
        if any(token in key for token in ("calculator", "wps", "edge")):
            return "tools"
        if "feed" in key:
            return "public_content"
        if any(token in key for token in ("map", "navigation", "browser")):
            return "browser_maps"
        return "other_scenarios"

    def stage_config_snapshot(self) -> Dict[str, object]:
        """Return a UI-oriented, read-only view derived from campaign JSON."""

        if not self.available or self.config_path is None:
            return {
                "available": False,
                "source_path": str(self.config_path or ""),
                "stages": {},
                "error": "内置两阶段 Campaign 配置不可用",
            }
        try:
            stat = self.config_path.stat()
            cache_key = f"{self.config_path}:{stat.st_mtime_ns}:{stat.st_size}"
            config_revision = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
        except OSError:
            cache_key = str(self.config_path)
            config_revision = "unavailable"
        with self._lock:
            if cache_key == self._stage_config_cache_key:
                return dict(self._stage_config_cache)

        try:
            config = self._catalog_config()
        except Exception as exc:
            return {
                "available": False,
                "source_path": str(self.config_path),
                "stages": {},
                "error": str(exc),
            }

        workflows_by_package: dict[str, list[str]] = {}
        for workflow in config.test.workflows:
            workflows_by_package.setdefault(workflow.package, []).append(
                workflow.workflow_id
            )

        setting_labels = {
            "window_animation_scale": "窗口动画",
            "transition_animation_scale": "过渡动画",
            "animator_duration_scale": "动画时长",
            "stay_on_while_plugged_in": "外部供电保持亮屏",
            "low_power": "低电量模式",
            "screen_brightness_mode": "亮度模式",
            "screen_brightness": "屏幕亮度",
            "screen_off_timeout": "自动锁屏",
            "accelerometer_rotation": "自动旋转",
            "user_rotation": "固定方向",
        }
        setting_group_meta = {
            "global": (
                "系统全局基线",
                "关闭动画、禁用省电并保持外部供电时亮屏。",
            ),
            "system": (
                "显示与交互基线",
                "固定亮度、锁屏时间和屏幕方向，减少轮次间变量。",
            ),
        }
        setting_groups: list[Dict[str, object]] = []
        for namespace in ("global", "system"):
            items = [
                {
                    "id": f"{setting.namespace}.{setting.key}",
                    "name": setting_labels.get(setting.key, setting.key),
                    "key": setting.key,
                    "value": setting.value,
                    "required": setting.required,
                }
                for setting in config.preparation.settings
                if setting.namespace == namespace
            ]
            if not items:
                continue
            label, purpose = setting_group_meta.get(
                namespace,
                (namespace, "写入白名单设置并回读验证。"),
            )
            setting_groups.append(
                {
                    "id": namespace,
                    "label": label,
                    "purpose": purpose,
                    "items": items,
                    "count": len(items),
                }
            )

        install_sets = [
            {
                "name": item.name,
                "package": item.package,
                "source": str(item.source),
                "source_name": item.source.name,
                "required": item.required,
                "replace": item.replace,
            }
            for item in config.preparation.install_sets
        ]

        preparation_apps: list[Dict[str, object]] = []
        for app in config.preparation.apps:
            workflow_ids = workflows_by_package.get(app.package, [])
            preparation_apps.append(
                {
                    "name": app.name,
                    "package": app.package,
                    "required": app.required,
                    "software_type": app.software_type,
                    "install_mode": app.install_mode,
                    "install_channel": app.install_channel,
                    "install_source": app.install_source,
                    "allow_terms_acceptance": app.allow_terms_acceptance,
                    "permissions": [
                        {
                            "name": permission.name,
                            "required": permission.required,
                        }
                        for permission in app.permissions
                    ],
                    "setup_tasks": [
                        self._task_config_snapshot(task)
                        for task in app.setup_tasks
                    ],
                    "workflow_ids": workflow_ids,
                    "workflow_mapped": bool(workflow_ids),
                }
            )

        preparation_group_meta = {
            "required_project": (
                "必需项目安装包",
                "固定版本的开源基线；任一项失败都会使预备阶段失败。",
            ),
            "optional_project": (
                "可选项目安装包",
                "固定版本的扩展游戏场景；失败记录为告警。",
            ),
            "external_apps": (
                "商店 / 官网应用",
                "缺失时按配置安装，随后完成首启和主流程复验。",
            ),
            "external_games": (
                "大型外部游戏",
                "只建立已有安装与登录态入口，不处理账号、实名或支付。",
            ),
        }

        def preparation_group_id(app: Mapping[str, object]) -> str:
            if app.get("install_mode") == "project":
                return "required_project" if app.get("required") else "optional_project"
            return (
                "external_games"
                if app.get("software_type") == "game"
                else "external_apps"
            )

        app_groups: list[Dict[str, object]] = []
        for group_id in (
            "required_project",
            "optional_project",
            "external_apps",
            "external_games",
        ):
            items = [
                item
                for item in preparation_apps
                if preparation_group_id(item) == group_id
            ]
            if not items:
                continue
            label, purpose = preparation_group_meta[group_id]
            app_groups.append(
                {
                    "id": group_id,
                    "label": label,
                    "purpose": purpose,
                    "items": items,
                    "count": len(items),
                }
            )

        unmapped_apps = [
            app for app in preparation_apps if not app["workflow_mapped"]
        ]
        raw_installer_apps = [
            app
            for app, configured in zip(
                preparation_apps,
                config.preparation.apps,
            )
            if configured.install_prompt
            and any(
                token in configured.install_prompt
                for token in ("APK", "官方下载", "官方页面下载", "官网下载")
            )
        ]
        all_files_apps = [
            app
            for app, configured in zip(
                preparation_apps,
                config.preparation.apps,
            )
            if "管理所有文件"
            in "\n".join(
                task.prompt + "\n" + task.attention_prompt
                for task in configured.setup_tasks
            )
        ]
        preparation_warnings: list[Dict[str, object]] = []
        if unmapped_apps:
            preparation_warnings.append(
                {
                    "severity": "warning",
                    "title": "预备应用没有正式 workflow",
                    "detail": "仍会安装和首启，但无法完成正常流程复验。",
                    "items": [app["name"] for app in unmapped_apps],
                }
            )
        if raw_installer_apps:
            preparation_warnings.append(
                {
                    "severity": "danger",
                    "title": "存在浏览器下载 / APK 安装路径",
                    "detail": "这些安装提示可能离开应用商店并进入系统安装器。",
                    "items": [app["name"] for app in raw_installer_apps],
                }
            )
        if all_files_apps:
            preparation_warnings.append(
                {
                    "severity": "danger",
                    "title": "存在“管理所有文件”权限流程",
                    "detail": "该权限会扩大 Agent 可见和可操作的本机文件范围。",
                    "items": [app["name"] for app in all_files_apps],
                }
            )

        app_type_by_package = {
            app.package: app.software_type for app in config.preparation.apps
        }
        workflow_items: list[Dict[str, object]] = []
        for workflow in config.test.workflows:
            workflow_items.append(
                {
                    "id": workflow.workflow_id,
                    "name": workflow.name,
                    "package": workflow.package,
                    "required": workflow.required,
                    "automation_engine": (
                        workflow.automation_engine
                        or config.model.automation_engine
                    ),
                    "repeat_after_success": workflow.repeat_after_success,
                    "force_stop_before_launch": workflow.force_stop_before_launch,
                    "initialization_tasks": [
                        self._task_config_snapshot(task)
                        for task in workflow.initialization_tasks
                    ],
                    "tasks": [
                        self._task_config_snapshot(task) for task in workflow.tasks
                    ],
                    "contract": {
                        "entry_state": workflow.contract.entry_state,
                        "success_evidence": workflow.contract.success_evidence,
                        "forbidden_states": list(
                            workflow.contract.forbidden_states
                        ),
                        "login_policy": workflow.contract.login_policy,
                        "allowed_foreground_packages": list(
                            workflow.contract.allowed_foreground_packages
                        ),
                        "required_actions": [
                            {
                                "label": requirement.label,
                                "actions": list(requirement.actions),
                                "minimum": requirement.minimum,
                            }
                            for requirement in workflow.contract.required_actions
                        ],
                    },
                    "quarantine_after_failures": workflow.quarantine_after_failures,
                    "retry_cooldown_s": workflow.retry_cooldown_s,
                    "idle_after_s": workflow.idle_after_s,
                    "group_id": self._workflow_group_id(
                        workflow.workflow_id,
                        required=workflow.required,
                        software_type=app_type_by_package.get(
                            workflow.package,
                            "app",
                        ),
                    ),
                }
            )

        workflow_group_meta = {
            "required_baseline": (
                "必需开源基线",
                "决定整轮是否满足 required 覆盖的四个核心场景。",
            ),
            "games": (
                "单机与大型游戏",
                "使用视觉状态变化验证移动、转向和镜头操作。",
            ),
            "tools": (
                "工具与文档",
                "验证计算、公开网页导航和临时文档放弃保存。",
            ),
            "public_content": (
                "免登录公开内容",
                "只滚动公开信息流，不点赞、搜索、发送或购买。",
            ),
            "browser_maps": (
                "浏览器与地图",
                "执行可撤销的输入、取消或地图平移，不提交外部内容。",
            ),
            "other_scenarios": (
                "其他受限场景",
                "按软件契约和登录策略执行最小可验证动作。",
            ),
        }
        workflow_groups: list[Dict[str, object]] = []
        for group_id in (
            "required_baseline",
            "games",
            "tools",
            "public_content",
            "browser_maps",
            "other_scenarios",
        ):
            items = [
                item for item in workflow_items if item["group_id"] == group_id
            ]
            if not items:
                continue
            label, purpose = workflow_group_meta[group_id]
            workflow_groups.append(
                {
                    "id": group_id,
                    "label": label,
                    "purpose": purpose,
                    "items": items,
                    "count": len(items),
                }
            )

        required_workflow_count = sum(
            workflow.required for workflow in config.test.workflows
        )
        repeat_workflow_count = sum(
            workflow.repeat_after_success for workflow in config.test.workflows
        )
        mapped_app_count = len(preparation_apps) - len(unmapped_apps)
        snapshot: Dict[str, object] = {
            "available": True,
            "campaign_id": config.campaign_id,
            "version": config.version,
            "revision": config_revision,
            "source_path": str(config.source_path),
            "source_name": config.source_path.name,
            "stages": {
                "prepare": {
                    "id": "prepare",
                    "label": "阶段 1 · 预备环境",
                    "kicker": "PREPARATION",
                    "purpose": "建立可重复的设备与软件基线，并在正式计时前证明每个场景可进入、可操作、可验收。",
                    "metrics": [
                        {
                            "label": "系统设置",
                            "value": len(config.preparation.settings),
                            "detail": "逐项写入并回读",
                        },
                        {
                            "label": "固定安装包",
                            "value": len(config.preparation.install_sets),
                            "detail": "本地 APK / APKS",
                        },
                        {
                            "label": "预备应用",
                            "value": len(config.preparation.apps),
                            "detail": "安装、首启、权限",
                        },
                        {
                            "label": "已映射 workflow",
                            "value": mapped_app_count,
                            "detail": f"{len(unmapped_apps)} 项未映射",
                        },
                    ],
                    "flow": [
                        {
                            "id": "device_ready",
                            "label": "设备就绪",
                            "purpose": "确认在线、解锁并可交互；设备号由启动时显式选择。",
                        },
                        {
                            "id": "settings",
                            "label": "系统基线",
                            "purpose": f"写入并回读 {len(config.preparation.settings)} 项白名单设置。",
                        },
                        {
                            "id": "install_sets",
                            "label": "固定版本",
                            "purpose": f"检查并安装 {len(config.preparation.install_sets)} 个项目 APK/APKS。",
                        },
                        {
                            "id": "app_setup",
                            "label": "逐应用初始化",
                            "purpose": f"处理 {len(config.preparation.apps)} 个应用的安装、权限和首启入口。",
                        },
                        {
                            "id": "validation",
                            "label": "功能复验",
                            "purpose": f"按阶段 2 的契约复验 {mapped_app_count} 个已映射场景。",
                        },
                        {
                            "id": "result",
                            "label": "预备结论",
                            "purpose": "必需项失败则阻断；可选项失败记录告警。",
                        },
                    ],
                    "setting_groups": setting_groups,
                    "install_sets": install_sets,
                    "app_groups": app_groups,
                    "warnings": preparation_warnings,
                    "policies": [
                        "JSON 不保存设备号；启动时必须显式选择 USB ADB 设备。",
                        "运行时权限只允许配置中逐项声明的权限。",
                        "允许接受协议不等于允许登录、验证码、实名、支付或发送。",
                        "每个应用首启后必须再通过对应阶段 2 workflow 的严格复验。",
                    ],
                    "acceptance": [
                        "所有 required 设置、安装包和应用均成功。",
                        "应用完成稳定入口初始化，并通过正常流程动作证据验证。",
                        "optional 失败只产生 completed_with_warnings，不伪装为全部通过。",
                    ],
                    "tasks": self._tasks_for_stage("prepare", config),
                },
                "test": {
                    "id": "test",
                    "label": "阶段 2 · 两小时测试",
                    "kicker": "TWO-HOUR TEST",
                    "purpose": "在统一录制窗口内循环执行可验证的真实软件动作，持续两小时并以宿主严格条件验收整轮。",
                    "metrics": [
                        {
                            "label": "单轮时长",
                            "value": f"{config.test.cycle_duration_s / 60:g}",
                            "detail": "分钟",
                        },
                        {
                            "label": "workflow",
                            "value": len(config.test.workflows),
                            "detail": f"{required_workflow_count} required",
                        },
                        {
                            "label": "成功后继续循环",
                            "value": repeat_workflow_count,
                            "detail": "其余严格成功一次后停",
                        },
                        {
                            "label": "录制采样",
                            "value": f"{config.test.recording.interval_s:g}",
                            "detail": "秒 / sample",
                        },
                    ],
                    "flow": [
                        {
                            "id": "round_start",
                            "label": "轮次与录制",
                            "purpose": "建立 7200 秒硬截止并启动低开销 profiler 录制。",
                        },
                        {
                            "id": "select_workflow",
                            "label": "选择场景",
                            "purpose": "按 JSON 顺序跳过已隔离、冷却或已满足一次成功的场景。",
                        },
                        {
                            "id": "initialize",
                            "label": "入口初始化",
                            "purpose": "只恢复契约入口，不提前执行主验证动作。",
                        },
                        {
                            "id": "validate",
                            "label": "主动作验证",
                            "purpose": "制造本轮新状态变化，并保留动作前后证据。",
                        },
                        {
                            "id": "host_guard",
                            "label": "宿主验收",
                            "purpose": "核对动作下限、动作上限与前台白名单；失败进入冷却或隔离。",
                        },
                        {
                            "id": "round_end",
                            "label": "循环与收尾",
                            "purpose": "回桌面继续编排，截止后验证录制、覆盖和 Agent-only 条件。",
                        },
                    ],
                    "workflow_groups": workflow_groups,
                    "warnings": [],
                    "policies": [
                        f"设备持续离线 {config.test.offline_grace_s:g} 秒后按关机条件收尾。",
                        "required workflow 默认循环；optional 默认严格成功一次后停止。",
                        "每个 workflow 有独立失败计数、冷却时间和隔离阈值。",
                        "Agent 运行中离开允许前台包会立即停止并隔离该场景。",
                    ],
                    "acceptance": [
                        "达到两小时硬截止，或在单遍模式下完成全部可用 workflow。",
                        "profiler 正常退出，最终工件完整且样本数达到理论值的 80%。",
                        "所有可用与 required workflow 均至少严格完成一次。",
                        "无人工接管、无隔离 workflow、设备与交互状态正常。",
                    ],
                    "recording": {
                        "enabled": config.test.recording.enabled,
                        "test_mode": config.test.recording.test_mode,
                        "capture_preset": config.test.recording.capture_preset,
                        "interval_s": config.test.recording.interval_s,
                        "checkpoint_interval_s": config.test.recording.checkpoint_interval_s,
                        "require_unplugged": config.test.recording.require_unplugged,
                    },
                    "tasks": self._tasks_for_stage("test", config),
                },
            },
        }
        with self._lock:
            self._stage_config_cache_key = cache_key
            self._stage_config_cache = dict(snapshot)
        return snapshot

    def software_catalog_snapshot(self) -> Dict[str, object]:
        try:
            config = self._catalog_config()
        except Exception as exc:
            return {
                "available": False,
                "items": [],
                "error": str(exc),
            }

        latest = self._latest_preparation_state()
        app_results = latest.get("app_results")
        result_by_package = {
            str(item.get("package") or ""): item
            for item in (app_results if isinstance(app_results, list) else [])
            if isinstance(item, Mapping) and str(item.get("package") or "")
        }
        install_results = latest.get("install_results")
        install_result_by_package = {
            str(item.get("package") or ""): item
            for item in (install_results if isinstance(install_results, list) else [])
            if isinstance(item, Mapping) and str(item.get("package") or "")
        }
        install_set_by_package = {
            item.package: item for item in config.preparation.install_sets
        }
        current_app = latest.get("current_app")
        current_package = (
            str(current_app.get("package") or "")
            if isinstance(current_app, Mapping)
            else ""
        )
        run_status = str(latest.get("status") or "not_checked")
        items: list[Dict[str, object]] = []
        for app in config.preparation.apps:
            result = result_by_package.get(app.package)
            install_result = install_result_by_package.get(app.package)
            install_set = install_set_by_package.get(app.package)
            validation_status = self._catalog_validation_status(
                result,
                running=run_status == "running" and current_package == app.package,
            )
            effective_catalog_status = (
                "supported"
                if app.catalog_status == "pending_validation"
                and validation_status == "verified"
                else app.catalog_status
            )
            workflow_validations = (
                result.get("workflow_validations")
                if result is not None
                and isinstance(result.get("workflow_validations"), list)
                else []
            )
            setup_agent = result.get("agent") if result is not None else None
            setup_raw_status = str(
                result.get("setup_status")
                if result is not None and result.get("setup_status") is not None
                else (
                    setup_agent.get("status")
                    if isinstance(setup_agent, Mapping)
                    else ""
                )
            )
            if validation_status == "running" and result is None:
                setup_status = "running"
            elif result is None:
                setup_status = "not_checked"
            elif result.get("setup_succeeded") is True or setup_raw_status in {
                "completed",
                "completed_with_warnings",
            }:
                setup_status = "verified"
            else:
                setup_status = "failed"
            normal_flow_status = validation_status
            if result is not None:
                agent = result.get("agent")
                failed_flow = next(
                    (
                        item
                        for item in workflow_validations
                        if isinstance(item, Mapping) and item.get("succeeded") is not True
                    ),
                    None,
                )
                failed_flow_agent = (
                    failed_flow.get("agent")
                    if isinstance(failed_flow, Mapping)
                    else None
                )
                validation_message = str(
                    result.get("message")
                    or (
                        failed_flow_agent.get("message")
                        if isinstance(failed_flow_agent, Mapping)
                        else ""
                    )
                    or (
                        failed_flow.get("message")
                        if isinstance(failed_flow, Mapping)
                        else ""
                    )
                    or (
                        agent.get("message")
                        if isinstance(agent, Mapping)
                        else ""
                    )
                    or (
                        "Qwen 正常测试流程验证通过"
                        if validation_status == "verified"
                        else result.get("status")
                        or ""
                    )
                )
            elif validation_status == "running":
                validation_message = "正在执行安装、首启和主界面验证"
            else:
                validation_message = "尚未运行预备验证"

            if app.install_mode == "project":
                if install_result is not None:
                    installation_status = (
                        "installed" if install_result.get("succeeded") is True else "failed"
                    )
                elif result is not None and str(result.get("status") or "") != "missing":
                    installation_status = "installed"
                else:
                    installation_status = "not_checked"
            else:
                store_install = result.get("store_install") if result is not None else None
                if isinstance(store_install, Mapping):
                    installation_status = (
                        "installed" if store_install.get("succeeded") is True else "failed"
                    )
                elif result is not None and str(result.get("status") or "") == "missing":
                    installation_status = "missing"
                elif result is not None:
                    installation_status = "installed"
                else:
                    installation_status = "not_checked"

            source_path = str(install_set.source) if install_set is not None else ""
            install_actions = (
                ["project"]
                if app.install_mode == "project"
                else [
                    action
                    for action, enabled in (
                        (
                            "app_store",
                            app.install_channel in {"app_store", "app_store_or_official"},
                        ),
                        (
                            "official_website",
                            app.install_channel in {"official_website", "app_store_or_official"}
                            and bool(app.official_url),
                        ),
                    )
                    if enabled
                ]
            )
            items.append(
                {
                    "name": app.name,
                    "package": app.package,
                    "catalog_status": effective_catalog_status,
                    "configured_catalog_status": app.catalog_status,
                    "software_type": app.software_type,
                    "install_mode": app.install_mode,
                    "install_channel": app.install_channel,
                    "install_source": app.install_source,
                    "official_url": app.official_url,
                    "source_path": source_path,
                    "source_available": (
                        install_set.source.exists() if install_set is not None else None
                    ),
                    "install_prompt": app.install_prompt,
                    "install_actions": install_actions,
                    "supported_engines": list(app.supported_engines),
                    "description": app.description,
                    "required": app.required,
                    "installation_status": installation_status,
                    "setup_status": setup_status,
                    "normal_flow_status": normal_flow_status,
                    "validation_status": validation_status,
                    "validation_message": validation_message,
                    "workflow_validations": [
                        {
                            "workflow_id": item.get("workflow_id"),
                            "name": item.get("name"),
                            "status": item.get("status"),
                            "succeeded": item.get("succeeded"),
                        }
                        for item in workflow_validations
                        if isinstance(item, Mapping)
                    ],
                    "raw_validation_status": (
                        str(result.get("status") or "") if result is not None else ""
                    ),
                }
            )

        return {
            "available": True,
            "items": items,
            "store_package": config.preparation.store_package,
            "total": len(items),
            "project_count": sum(item["install_mode"] == "project" for item in items),
            "external_count": sum(item["install_mode"] == "external" for item in items),
            "pending_count": sum(
                item["catalog_status"] == "pending_validation" for item in items
            ),
            "verified_count": sum(item["validation_status"] == "verified" for item in items),
            "failed_count": sum(
                item["validation_status"] in {"failed", "missing", "needs_attention"}
                for item in items
            ),
            "validation_run": {
                "status": run_status,
                "started_at": latest.get("started_at"),
                "finished_at": latest.get("finished_at"),
                "device": latest.get("device"),
                "output_dir": latest.get("output_dir"),
                "state_path": latest.get("state_path"),
            },
        }

    def install_project_software(self, device: str, package: str) -> Dict[str, object]:
        config = self._load_config(device)
        install_set = next(
            (
                item
                for item in config.preparation.install_sets
                if item.package == package
            ),
            None,
        )
        app = next(
            (item for item in config.preparation.apps if item.package == package),
            None,
        )
        if app is None or app.install_mode != "project" or install_set is None:
            raise ValueError("目标软件没有配置项目安装包")

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("已有 Campaign 阶段正在运行")
            if self._installing_package:
                raise RuntimeError(f"正在安装 {self._installing_package}")
            self._installing_package = package
            self._log_locked("info", f"开始从项目安装包安装 {app.name} ({package})")

        try:
            runner = AndroidCampaignRunner(
                self.adb,
                config,
                self.output_root,
            )
            result = runner._install(install_set)
            payload: Dict[str, object] = {
                **result,
                "install_mode": "project",
                "install_channel": app.install_channel,
            }
            with self._lock:
                self._log_locked(
                    "info" if result.get("succeeded") else "error",
                    (
                        f"{app.name} 项目安装包安装完成"
                        if result.get("succeeded")
                        else f"{app.name} 项目安装包安装失败"
                    ),
                )
            return payload
        finally:
            with self._lock:
                self._installing_package = ""

    def _tasks_for_stage(
        self,
        stage: str,
        config: Optional[CampaignConfig] = None,
    ) -> list[Dict[str, object]]:
        """Build the dashboard task list from the current campaign JSON.

        The prompt templates module still supplies campaign labels and fallback
        metadata, but it must never become a second source of campaign prompts.
        """

        selected = config or self._catalog_config()
        tasks: list[AgentTaskConfig] = []
        if stage == "prepare":
            workflows_by_package: dict[str, list[object]] = {}
            for workflow in selected.test.workflows:
                workflows_by_package.setdefault(workflow.package, []).append(workflow)
            for app in selected.preparation.apps:
                if app.install_prompt:
                    tasks.append(
                        AgentTaskConfig(
                            task_id=f"store-install-{app.package}",
                            name=f"安装 {app.name}",
                            prompt=app.install_prompt,
                            attention_prompt=(
                                f"只安装目标应用 {app.name}（期望包名 {app.package}）。"
                                "允许确认系统安装器和应用商店的安装按钮；"
                                "不得登录、付费或安装推荐应用。"
                            ),
                            max_steps=40,
                            timeout_s=600.0,
                            on_failure="stop",
                        )
                    )
                tasks.extend(app.setup_tasks)
                for workflow in workflows_by_package.get(app.package, []):
                    tasks.extend(workflow.initialization_tasks)
                    tasks.extend(workflow.tasks)
        elif stage == "test":
            for workflow in selected.test.workflows:
                tasks.extend(workflow.initialization_tasks)
                tasks.extend(workflow.tasks)
        else:
            raise ValueError("campaign stage must be prepare or test")

        # A setup task can intentionally be reused as a workflow initializer.
        # Show and override the first occurrence only; the runner resolves the
        # same task id consistently wherever it is used.
        snapshots: list[Dict[str, object]] = []
        seen: set[str] = set()
        for task in tasks:
            if task.task_id in seen:
                continue
            seen.add(task.task_id)
            snapshots.append(self._task_payload_snapshot(task))
        return snapshots

    def start(self, payload: Dict[str, object]) -> Dict[str, object]:
        stage = str(payload.get("stage") or "").strip().lower()
        if stage not in {"prepare", "test"}:
            raise ValueError("campaign stage must be prepare or test")
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("请选择 Android ADB 设备")

        config = self._load_config(device)
        model_overrides = {
            key: payload[key]
            for key in _MODEL_OVERRIDE_KEYS
            if key in payload and payload[key] is not None
        }
        runtime_system_prompt_override = _boolean(
            payload.get("runtime_system_prompt_override"),
            False,
        )
        if not runtime_system_prompt_override:
            model_overrides.pop("system_prompt", None)
        template = _campaign_template(stage)
        baseline_tasks = self._tasks_for_stage(stage, config)
        runtime_task_overrides = _boolean(
            payload.get("runtime_task_overrides"),
            False,
        )
        raw_tasks = payload.get("tasks")
        if runtime_task_overrides:
            if not isinstance(raw_tasks, list):
                raise ValueError("runtime task overrides require a tasks array")
            tasks = normalize_agent_tasks({"tasks": raw_tasks})
            task_overrides = {
                str(task["id"]): AgentTaskConfig.from_mapping(
                    task,
                    index,
                    default_on_failure=str(task["on_failure"]),
                )
                for index, task in enumerate(tasks, 1)
            }
            task_order = [str(task["id"]) for task in tasks]
        else:
            tasks = baseline_tasks
            task_overrides = {}
            task_order = []

        repeat_workflows = (
            _boolean(
                payload.get("repeat_workflows", payload.get("loop_enabled")),
                True,
            )
            if stage == "test"
            else False
        )
        if stage == "test":
            run_until_shutdown = _boolean(payload.get("run_until_shutdown"), False)
            if run_until_shutdown:
                max_rounds: Optional[int] = None
            else:
                raw_max_rounds = payload.get("max_rounds", 1)
                try:
                    max_rounds = int(raw_max_rounds)
                except (TypeError, ValueError) as exc:
                    raise ValueError("max_rounds must be a positive integer") from exc
                if max_rounds <= 0:
                    raise ValueError("max_rounds must be a positive integer")
        else:
            max_rounds = None
        now = time.time()
        session_id = uuid.uuid4().hex

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("已有 Campaign 阶段正在运行")
            if self._installing_package:
                raise RuntimeError(f"正在安装 {self._installing_package}")
            self._logs.clear()
            self._screenshot_revision = 0
            self._screenshot_key = ""
            self._runner = AndroidCampaignRunner(
                self.adb,
                config,
                self.output_root,
                model_payload_overrides=model_overrides,
                task_overrides=task_overrides,
                task_order=task_order,
                repeat_workflows=repeat_workflows,
            )
            self._session = {
                "session_id": session_id,
                "campaign_stage": stage,
                "workflow_name": str(
                    payload.get("workflow_name")
                    or template.get("workflow_name")
                    or ("阶段 1：Android 测试预备环境" if stage == "prepare" else "阶段 2：Android 两小时循环实际测试")
                ),
                "device": device,
                "tasks": tasks,
                "task_count": len(tasks),
                "task_index": 1 if tasks else 0,
                "current_task": tasks[0] if tasks else None,
                "task_results": [],
                "loop_enabled": repeat_workflows,
                "repeat_workflows": repeat_workflows,
                "max_rounds": max_rounds,
                "runtime_task_overrides": runtime_task_overrides,
                "runtime_system_prompt_override": runtime_system_prompt_override,
                "step": 0,
                "max_steps": tasks[0].get("max_steps", 0) if tasks else 0,
                "status": "starting",
                "phase": f"campaign_{stage}",
                "running": True,
                "started_at": now,
                "finished_at": None,
                "elapsed_s": 0.0,
                "message": "正在启动预备阶段" if stage == "prepare" else "正在启动两小时循环实际测试",
                "error": "",
                "latest_action": None,
                "latest_action_result": "",
                "latest_reasoning": "",
                "latest_request_s": None,
                "screenshot_width": None,
                "screenshot_height": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "output_dir": "",
                "model_provider": str(model_overrides.get("model_provider") or config.model.provider),
                "automation_engine": str(
                    model_overrides.get("automation_engine")
                    or config.model.automation_engine
                ),
                "api_base_url": str(model_overrides.get("api_base_url") or config.model.api_base_url),
                "model": str(model_overrides.get("model") or config.model.model),
                "model_thinking_mode": str(
                    model_overrides.get("model_thinking_mode") or config.model.thinking_mode
                ),
                "api_key_mode": str(model_overrides.get("api_key_mode") or config.model.api_key_mode),
                "step_delay_s": model_overrides.get("step_delay_s", config.model.step_delay_s),
                "request_timeout_s": model_overrides.get(
                    "request_timeout_s", config.model.request_timeout_s
                ),
                "system_prompt": str(model_overrides.get("system_prompt") or ""),
                "system_prompt_version": "custom" if model_overrides.get("system_prompt") else "campaign-default",
                "cycle_duration_s": config.test.cycle_duration_s if stage == "test" else None,
                "offline_grace_s": config.test.offline_grace_s if stage == "test" else None,
            }
            self._log_locked(
                "info",
                "预备阶段已创建"
                if stage == "prepare"
                else (
                    (
                        "实际测试阶段已创建：单轮内循环 workflow，"
                        f"最多 {max_rounds} 轮"
                    )
                    if repeat_workflows and max_rounds is not None
                    else "实际测试阶段已创建：循环至设备关机"
                    if repeat_workflows
                    else f"实际测试阶段已创建：每轮整套任务只执行一遍，最多 {max_rounds} 轮"
                ),
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(session_id, stage),
                name=f"android-campaign-{stage}-{session_id[:8]}",
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def _completed_task_results(
        self,
        stage: str,
        result: Mapping[str, object],
        tasks: list[Dict[str, object]],
    ) -> list[Dict[str, object]]:
        actual_by_id: dict[str, Mapping[str, object]] = {}

        def collect_agent(
            agent: object,
            *,
            host_status: str = "",
            host_message: str = "",
        ) -> None:
            if not isinstance(agent, Mapping):
                return
            task_results = agent.get("task_results")
            if not isinstance(task_results, list):
                return
            for item in task_results:
                if isinstance(item, Mapping) and item.get("id"):
                    effective = dict(item)
                    if host_status and str(effective.get("status") or "") == "completed":
                        effective["status"] = host_status
                        effective["message"] = (
                            host_message
                            or f"宿主严格验收未通过：{host_status}"
                        )
                    actual_by_id[str(item["id"])] = effective

        def workflow_failure_message(workflow_result: Mapping[str, object]) -> str:
            status = str(workflow_result.get("status") or "")
            action_evidence = workflow_result.get("action_evidence")
            if status == "incomplete_action_evidence" and isinstance(
                action_evidence, Mapping
            ):
                requirements = action_evidence.get("requirements")
                if isinstance(requirements, list):
                    missing = [
                        (
                            f"{item.get('label')}: "
                            f"{item.get('observed', 0)}/{item.get('minimum', 0)}"
                        )
                        for item in requirements
                        if isinstance(item, Mapping) and item.get("satisfied") is not True
                    ]
                    if missing:
                        return "宿主动作证据不足：" + "；".join(missing)
            agent = workflow_result.get("agent")
            initialization_agent = workflow_result.get("initialization_agent")
            return str(
                (agent.get("message") if isinstance(agent, Mapping) else "")
                or (
                    initialization_agent.get("message")
                    if isinstance(initialization_agent, Mapping)
                    else ""
                )
                or status
            )

        if stage == "prepare":
            app_results = result.get("app_results")
            if isinstance(app_results, list):
                for app_result in app_results:
                    if not isinstance(app_result, Mapping):
                        continue
                    store_install = app_result.get("store_install")
                    if isinstance(store_install, Mapping):
                        collect_agent(store_install.get("agent"))
                    collect_agent(app_result.get("agent"))
                    workflow_validations = app_result.get("workflow_validations")
                    if isinstance(workflow_validations, list):
                        for validation in workflow_validations:
                            if isinstance(validation, Mapping):
                                host_status = (
                                    str(validation.get("status") or "")
                                    if validation.get("succeeded") is not True
                                    else ""
                                )
                                host_message = (
                                    workflow_failure_message(validation)
                                    if host_status
                                    else ""
                                )
                                collect_agent(
                                    validation.get("initialization_agent"),
                                    host_status=(
                                        host_status
                                        if validation.get(
                                            "initialization_evidence_complete"
                                        )
                                        is False
                                        else ""
                                    ),
                                    host_message=host_message,
                                )
                                collect_agent(
                                    validation.get("agent"),
                                    host_status=host_status,
                                    host_message=host_message,
                                )
        else:
            rounds = result.get("rounds")
            if isinstance(rounds, list):
                for round_result in rounds:
                    if not isinstance(round_result, Mapping):
                        continue
                    workflow_results = round_result.get("workflow_results")
                    if not isinstance(workflow_results, list):
                        continue
                    for workflow_result in workflow_results:
                        if isinstance(workflow_result, Mapping):
                            host_status = (
                                str(workflow_result.get("status") or "")
                                if workflow_result.get("evidence_complete") is False
                                else ""
                            )
                            host_message = (
                                workflow_failure_message(workflow_result)
                                if host_status
                                else ""
                            )
                            collect_agent(
                                workflow_result.get("initialization_agent"),
                                host_status=(
                                    host_status
                                    if workflow_result.get(
                                        "initialization_evidence_complete"
                                    )
                                    is False
                                    else ""
                                ),
                                host_message=host_message,
                            )
                            collect_agent(
                                workflow_result.get("agent"),
                                host_status=host_status,
                                host_message=host_message,
                            )

        results: list[Dict[str, object]] = []
        overall_status = str(result.get("status") or "")
        completed = overall_status in {
            "completed",
            "completed_with_warnings",
            "device_shutdown_or_unavailable",
            "max_rounds",
        }
        for index, task in enumerate(tasks, 1):
            task_id = str(task.get("id") or "")
            actual = actual_by_id.get(task_id)
            if actual is not None:
                task_status = str(actual.get("status") or "error")
                message = str(actual.get("message") or task_status)
                steps = actual.get("steps", 0)
                duration_s = actual.get("duration_s", 0)
            else:
                task_status = "completed" if completed else "error"
                message = (
                    "目标包已存在，条件安装任务未触发"
                    if completed and task_id.startswith("store-install-")
                    else "阶段任务已完成"
                    if completed
                    else str(result.get("message") or "阶段未完成")
                )
                steps = 0
                duration_s = 0
            results.append(
                {
                    "index": index,
                    "id": task_id,
                    "name": task.get("name"),
                    "status": task_status,
                    "message": message,
                    "steps": steps,
                    "duration_s": duration_s,
                }
            )
        return results

    @staticmethod
    def _presentation_status(stage: str, result: Mapping[str, object]) -> tuple[str, str]:
        raw_status = str(result.get("status") or "error")
        raw_message = str(result.get("message") or "").strip()
        if stage == "prepare":
            if raw_status in {"completed", "completed_with_warnings"}:
                message = raw_message or (
                    "预备阶段完成，存在可选应用告警"
                    if raw_status == "completed_with_warnings"
                    else "预备阶段已完成"
                )
                return raw_status, message
            if raw_status == "operator_stopped":
                return "stopped", raw_message or "预备阶段已由用户停止"
            return "error", raw_message or f"预备阶段未完成：{raw_status}"

        if raw_status == "device_shutdown_or_unavailable":
            detail = f"（{raw_message}）" if raw_message else ""
            return "completed", f"设备持续离线，已按关机条件完成测试并收尾{detail}"
        if raw_status == "max_rounds":
            acceptance = result.get("acceptance")
            if (
                isinstance(acceptance, Mapping)
                and acceptance.get("passed") is False
            ):
                accepted = acceptance.get("accepted_round_count", 0)
                total = acceptance.get("round_count", 0)
                return (
                    "completed_with_warnings",
                    f"指定轮次已结束，但严格验收仅通过 {accepted}/{total} 轮",
                )
            return "completed", raw_message or "已完成指定测试轮次"
        if raw_status == "operator_stopped":
            return "stopped", raw_message or "实际测试阶段已由用户停止"
        if raw_status == "completed_with_warnings":
            return "completed_with_warnings", raw_message or "实际测试完成，存在告警"
        if raw_status == "completed":
            return "completed", raw_message or "实际测试阶段已完成"
        return "error", raw_message or f"实际测试阶段未完成：{raw_status}"

    def _run(self, session_id: str, stage: str) -> None:
        with self._lock:
            runner = self._runner
            max_rounds = (
                self._session.get("max_rounds")
                if self._session is not None
                else None
            )
            if self._session is not None and self._session.get("session_id") == session_id:
                self._session.update(
                    {
                        "status": "running",
                        "phase": f"campaign_{stage}",
                        "message": "正在执行预备阶段" if stage == "prepare" else "正在执行两小时循环实际测试",
                    }
                )
        if runner is None:
            return
        try:
            result = (
                runner.prepare()
                if stage == "prepare"
                else runner.run_test(
                    max_rounds=(int(max_rounds) if max_rounds is not None else None)
                )
            )
            status, message = self._presentation_status(stage, result)
            with self._lock:
                if self._session is None or self._session.get("session_id") != session_id:
                    return
                tasks = self._session.get("tasks")
                task_list = [dict(item) for item in tasks] if isinstance(tasks, list) else []
                self._session.update(
                    {
                        "status": status,
                        "phase": "finished",
                        "running": False,
                        "finished_at": time.time(),
                        "message": message,
                        "error": "" if status not in {"error"} else message,
                        "output_dir": str(result.get("output_dir") or ""),
                        "task_index": len(task_list),
                        "current_task": None,
                        "task_results": self._completed_task_results(stage, result, task_list),
                        "result": dict(result),
                        "round_count": result.get("round_count", 0),
                    }
                )
                self._log_locked(
                    "error" if status == "error" else ("warning" if status in {"stopped", "completed_with_warnings"} else "info"),
                    message,
                )
        except Exception as exc:
            with self._lock:
                if self._session is None or self._session.get("session_id") != session_id:
                    return
                self._session.update(
                    {
                        "status": "error",
                        "phase": "finished",
                        "running": False,
                        "finished_at": time.time(),
                        "message": "Campaign 阶段运行失败",
                        "error": str(exc),
                    }
                )
                self._log_locked("error", f"Campaign 阶段运行失败：{exc}")

    def stop(self) -> Dict[str, object]:
        with self._lock:
            thread = self._thread
            runner = self._runner
            if thread is None or not thread.is_alive():
                return self.snapshot()
            if self._session is not None:
                self._session.update(
                    {
                        "status": "stopping",
                        "phase": "stopping",
                        "message": "正在停止 Campaign 阶段并收尾",
                    }
                )
            self._log_locked("warning", "用户请求停止 Campaign 阶段")
        if runner is not None:
            runner.request_stop()
        return self.snapshot()

    def latest_screenshot(self) -> Optional[bytes]:
        with self._lock:
            runner = self._runner
        return runner.latest_screenshot() if runner is not None else None

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            session = dict(self._session) if self._session is not None else None
            runner = self._runner
            logs = list(self._logs)
        if session is None:
            return {
                "available": self.available,
                "running": False,
                "status": "idle",
                "phase": "idle",
                "logs": [],
                "screenshot_revision": 0,
                "screenshot_available": False,
                "config_path": str(self.config_path or ""),
                "stage_config": self.stage_config_snapshot(),
                "software_catalog": self.software_catalog_snapshot(),
            }

        if session.get("running"):
            session["elapsed_s"] = max(
                0.0, time.time() - float(session.get("started_at") or time.time())
            )
        nested = runner.active_agent_snapshot() if runner is not None else {}
        if nested:
            for key in (
                "phase",
                "step",
                "max_steps",
                "current_task",
                "latest_action",
                "latest_action_result",
                "latest_reasoning",
                "latest_request_s",
                "screenshot_width",
                "screenshot_height",
                "prompt_tokens",
                "completion_tokens",
            ):
                if key in nested:
                    session[key] = nested[key]
            nested_logs = nested.get("logs")
            if isinstance(nested_logs, list):
                logs.extend(item for item in nested_logs[-120:] if isinstance(item, Mapping))
            nested_key = f"{nested.get('session_id')}:{nested.get('screenshot_revision')}"
            if nested.get("screenshot_available") and nested_key != self._screenshot_key:
                with self._lock:
                    if nested_key != self._screenshot_key:
                        self._screenshot_key = nested_key
                        self._screenshot_revision += 1

        screenshot_available = bool(nested.get("screenshot_available"))
        session.update(
            {
                "available": self.available,
                "logs": logs[-300:],
                "screenshot_revision": self._screenshot_revision,
                "screenshot_available": screenshot_available,
                "screenshot_url": "/api/campaign/screenshot" if screenshot_available else None,
                "config_path": str(self.config_path or ""),
                "stage_config": self.stage_config_snapshot(),
                "software_catalog": self.software_catalog_snapshot(),
            }
        )
        return session

    def close(self) -> None:
        self.stop()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=8.0)
