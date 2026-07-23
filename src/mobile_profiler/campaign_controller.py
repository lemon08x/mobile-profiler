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

    @staticmethod
    def _tasks_for_stage(stage: str) -> list[Dict[str, object]]:
        template = _campaign_template(stage)
        raw_tasks = template.get("tasks")
        if not isinstance(raw_tasks, list):
            return []
        return [dict(item) for item in raw_tasks if isinstance(item, Mapping)]

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
        template = _campaign_template(stage)
        raw_tasks = payload.get("tasks")
        tasks = normalize_agent_tasks(
            {"tasks": raw_tasks if isinstance(raw_tasks, list) else self._tasks_for_stage(stage)}
        )
        task_overrides = {
            str(task["id"]): AgentTaskConfig(
                task_id=str(task["id"]),
                name=str(task["name"]),
                prompt=str(task["prompt"]),
                attention_prompt=str(task["attention_prompt"]),
                max_steps=int(task["max_steps"]),
                timeout_s=float(task["timeout_s"]),
                on_failure=str(task["on_failure"]),
            )
            for task in tasks
        }
        loop_enabled = (
            _boolean(payload.get("loop_enabled"), True) if stage == "test" else False
        )
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
                task_order=[str(task["id"]) for task in tasks],
                repeat_workflows=loop_enabled,
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
                "loop_enabled": loop_enabled,
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
                    "实际测试阶段已创建：循环执行至设备关机"
                    if loop_enabled
                    else "实际测试阶段已创建：整套任务只执行一遍"
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

        def collect_agent(agent: object) -> None:
            if not isinstance(agent, Mapping):
                return
            task_results = agent.get("task_results")
            if not isinstance(task_results, list):
                return
            for item in task_results:
                if isinstance(item, Mapping) and item.get("id"):
                    actual_by_id[str(item["id"])] = item

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
                                collect_agent(validation.get("agent"))
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
                            collect_agent(workflow_result.get("agent"))

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
            loop_enabled = bool(
                self._session.get("loop_enabled")
                if self._session is not None
                else False
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
                else runner.run_test(max_rounds=None if loop_enabled else 1)
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
