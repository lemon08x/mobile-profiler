"""Host-side lifecycle for an external StarRailCopilot runtime."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from .star_rail_copilot_runner import (
    CONTROL_METHODS,
    DEFAULT_SCRCPY_MAX_SIZE,
    DOMAIN_STRATEGIES,
    ROGUE_PATHS,
    ROGUE_WORLDS,
    SCREENSHOT_METHODS,
    SCRCPY_MAX_SIZES,
    SERVER_PACKAGES,
    UPSTREAM_LICENSE,
    UPSTREAM_REPOSITORY,
    validate_upstream_path,
)


STAR_RAIL_OPTION_DEFAULTS: dict[str, object] = {
    "server": "CN-Official",
    "screenshot_method": "scrcpy",
    "scrcpy_max_size": DEFAULT_SCRCPY_MAX_SIZE,
    "control_method": "MaaTouch",
    "world": ROGUE_WORLDS[-1],
    "path": "The_Hunt",
    "domain_strategy": "combat",
    "use_immersifier": True,
    "double_event": True,
    "weekly_farming": False,
    "use_stamina": False,
}


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _choice(payload: dict[str, object], key: str, default: str) -> str:
    value = str(payload.get(key) or "").strip()
    return value or default


def _scrcpy_max_size(payload: dict[str, object]) -> int:
    try:
        value = int(payload.get("scrcpy_max_size") or DEFAULT_SCRCPY_MAX_SIZE)
    except (TypeError, ValueError):
        return DEFAULT_SCRCPY_MAX_SIZE
    return value if value in SCRCPY_MAX_SIZES else DEFAULT_SCRCPY_MAX_SIZE


class StarRailCopilotRuntimeController:
    """Launch SRC in a subprocess and expose auditable lifecycle state."""

    def __init__(
        self,
        adb: str,
        output_root: Path,
        upstream_path: Optional[Path] = None,
        *,
        run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.adb = str(adb or "adb")
        runtime_root = output_root.resolve()
        self.output_root = (
            runtime_root / "open-source-automation" / "star-rail-copilot"
        ).resolve()
        configured_root = os.environ.get(
            "MOBILE_PROFILER_STAR_RAIL_COPILOT_ROOT", ""
        ).strip()
        self.upstream_path = (
            upstream_path
            or (Path(configured_root) if configured_root else None)
            or runtime_root / "open-source-runtimes" / "StarRailCopilot"
        ).expanduser().resolve()
        self._run_func = run_func
        self._popen_factory = popen_factory
        self._lock = threading.RLock()
        self._status = "not_installed"
        self._running = False
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._log_handle = None
        self._device = ""
        self._last_error = ""
        self._last_preflight: Optional[dict[str, object]] = None
        self._last_run_dir = ""
        self._last_exit_code: Optional[int] = None
        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None
        self._logs: deque[dict[str, object]] = deque(maxlen=30)
        self._options = dict(STAR_RAIL_OPTION_DEFAULTS)
        self._load_config()
        self._refresh_install_status()

    @property
    def _config_path(self) -> Path:
        return self.output_root / "config.json"

    def _load_config(self) -> None:
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        upstream_path = str(payload.get("upstream_path") or "").strip()
        if upstream_path:
            self.upstream_path = Path(upstream_path).expanduser().resolve()
        raw_options = payload.get("options")
        if isinstance(raw_options, dict):
            try:
                self._options = self._validated_options(raw_options)
            except ValueError:
                pass

    def _persist_config(self) -> None:
        _atomic_write(
            self._config_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "upstream_path": str(self.upstream_path),
                    "options": dict(self._options),
                    "saved_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )

    def _validated_options(self, payload: dict[str, object]) -> dict[str, object]:
        values = dict(self._options)
        for key, default in STAR_RAIL_OPTION_DEFAULTS.items():
            if key not in payload:
                continue
            if isinstance(default, bool):
                values[key] = _bool(payload[key], bool(default))
            elif isinstance(default, int):
                try:
                    values[key] = int(str(payload[key]).strip())
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"StarRailCopilot {key} must be an integer") from exc
            else:
                values[key] = str(payload[key] or "").strip()
        choices = {
            "server": set(SERVER_PACKAGES),
            "screenshot_method": set(SCREENSHOT_METHODS),
            "control_method": set(CONTROL_METHODS),
            "world": set(ROGUE_WORLDS),
            "path": set(ROGUE_PATHS),
            "domain_strategy": set(DOMAIN_STRATEGIES),
        }
        for key, allowed in choices.items():
            if values[key] not in allowed:
                raise ValueError(f"unsupported StarRailCopilot {key}: {values[key]}")
        if int(values["scrcpy_max_size"]) not in SCRCPY_MAX_SIZES:
            raise ValueError("unsupported StarRailCopilot scrcpy_max_size")
        return values

    def _configuration(self, payload: dict[str, object]) -> dict[str, object]:
        raw_root = payload.get("upstream_path", str(self.upstream_path))
        root_text = str(raw_root or "").strip()
        if not root_text:
            raise ValueError("StarRailCopilot upstream_path is required")
        option_payload = {
            key: payload[key]
            for key in STAR_RAIL_OPTION_DEFAULTS
            if key in payload
        }
        return {
            "upstream_path": str(Path(root_text).expanduser().resolve()),
            **self._validated_options(option_payload),
        }

    def _apply_configuration(self, configuration: dict[str, object]) -> None:
        self.upstream_path = Path(str(configuration["upstream_path"])).resolve()
        self._options = {
            key: configuration[key] for key in STAR_RAIL_OPTION_DEFAULTS
        }

    def configure(self, payload: dict[str, object]) -> dict[str, object]:
        configuration = self._configuration(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("StarRailCopilot Rogue is already running")
            self._apply_configuration(configuration)
            self._last_preflight = None
            self._last_error = ""
        self._persist_config()
        self._refresh_install_status()
        self._log("configured", "已保存 StarRailCopilot 模拟宇宙参数")
        return self.snapshot()

    @property
    def python_executable(self) -> str:
        bundled = self.upstream_path / "toolkit" / "python.exe"
        return str(bundled) if bundled.is_file() else sys.executable

    def _log(self, status: str, message: str) -> None:
        with self._lock:
            self._logs.append(
                {"time": time.time(), "status": status, "message": message}
            )

    def _refresh_install_status(self) -> None:
        try:
            validate_upstream_path(self.upstream_path)
        except Exception as exc:
            self._status = "not_installed"
            self._last_error = str(exc)
        else:
            self._status = "installed"
            self._last_error = ""

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[1])
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source_root, existing) if value
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    def _command(
        self,
        device: str,
        mode: str,
        payload: dict[str, object],
    ) -> list[str]:
        configuration = self._configuration(payload)
        use_immersifier = bool(configuration["use_immersifier"])
        double_event = bool(configuration["double_event"])
        weekly_farming = bool(configuration["weekly_farming"])
        use_stamina = bool(configuration["use_stamina"])
        command = [
            self.python_executable,
            "-m",
            "mobile_profiler.star_rail_copilot_runner",
            "--upstream",
            str(self.upstream_path),
            "--serial",
            device,
            "--adb",
            self.adb,
            "--server",
            str(configuration["server"]),
            "--screenshot-method",
            str(configuration["screenshot_method"]),
            "--scrcpy-max-size",
            str(configuration["scrcpy_max_size"]),
            "--control-method",
            str(configuration["control_method"]),
            "--world",
            str(configuration["world"]),
            "--path",
            str(configuration["path"]),
            "--domain-strategy",
            str(configuration["domain_strategy"]),
            "--use-immersifier" if use_immersifier else "--no-use-immersifier",
            "--double-event" if double_event else "--no-double-event",
            "--weekly-farming" if weekly_farming else "--no-weekly-farming",
            "--use-stamina" if use_stamina else "--no-use-stamina",
            mode,
        ]
        return command

    @staticmethod
    def _summary_from_output(payload: bytes) -> dict[str, object]:
        text = payload.decode("utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "adapter" in value:
                return value
        raise RuntimeError("StarRailCopilot did not return a preflight summary")

    def preflight(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("StarRailCopilot preflight requires device")
        configuration = self._configuration(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("StarRailCopilot Rogue is already running")
            self._apply_configuration(configuration)
            self._refresh_install_status()
            if self._status == "not_installed":
                raise RuntimeError(self._last_error or "StarRailCopilot is not installed")
            self._status = "preflighting"
            self._last_error = ""
            self._device = device
        self._log("preflighting", f"开始检查真机 {device} 与 StarRailCopilot")
        command = self._command(device, "--preflight", configuration)
        try:
            result = self._run_func(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=150,
                check=False,
                cwd=str(self.upstream_path),
                env=self._environment(),
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            with self._lock:
                self._status = "error"
                self._last_error = str(exc)
            self._log("error", str(exc))
            raise RuntimeError(f"StarRailCopilot preflight failed: {exc}") from exc
        output = bytes(result.stdout or b"")
        _atomic_write(self.output_root / "preflight.log", output)
        if result.returncode != 0:
            detail = output.decode("utf-8", errors="replace")[-3000:].strip()
            with self._lock:
                self._status = "error"
                self._last_error = detail or f"preflight exited {result.returncode}"
            self._log("error", self._last_error)
            raise RuntimeError(self._last_error)
        summary = self._summary_from_output(output)
        summary["runtime"] = dict(configuration)
        screen = summary.get("screen") if isinstance(summary.get("screen"), dict) else {}
        ready = screen.get("game_ready") is True
        with self._lock:
            self._last_preflight = summary
            self._status = "ready" if ready else "waiting_for_game"
            self._last_error = ""
        self._persist_config()
        _atomic_write(
            self.output_root / "preflight.json",
            json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        state = str(screen.get("screen_state") or "unknown")
        self._log(
            self._status,
            "SRC 真机运行条件已满足" if ready else f"SRC 预检未就绪：{state}",
        )
        return self.snapshot()

    def _watch_process(
        self,
        process: subprocess.Popen[bytes],
        log_handle,
    ) -> None:
        exit_code = process.wait()
        try:
            log_handle.flush()
            log_handle.close()
        except OSError:
            pass
        with self._lock:
            if self._process is process:
                self._process = None
                self._log_handle = None
                self._running = False
                self._last_exit_code = int(exit_code)
                self._completed_at = time.time()
                if self._status == "stopping":
                    self._status = "stopped"
                elif exit_code == 0:
                    self._status = "completed"
                else:
                    self._status = "error"
                    self._last_error = (
                        f"StarRailCopilot exited with code {exit_code}"
                    )
        self._log(self._status, self._last_error or "SRC Rogue 运行已结束")

    def start(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("StarRailCopilot run requires device")
        configuration = self._configuration(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("StarRailCopilot Rogue is already running")
            preflight = self._last_preflight or {}
            screen = (
                preflight.get("screen")
                if isinstance(preflight.get("screen"), dict)
                else {}
            )
            checked_device = (
                preflight.get("device")
                if isinstance(preflight.get("device"), dict)
                else {}
            )
            checked_runtime = (
                preflight.get("runtime")
                if isinstance(preflight.get("runtime"), dict)
                else {}
            )
            if (
                checked_device.get("serial") != device
                or screen.get("game_ready") is not True
                or checked_runtime != configuration
            ):
                raise RuntimeError(
                    "run a successful SRC game-ready preflight for this device before starting"
                )
            run_name = time.strftime("%Y%m%d-%H%M%S")
            run_dir = self.output_root / "runs" / run_name
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path = run_dir / "runtime.log"
            log_handle = log_path.open("wb")
            self._apply_configuration(configuration)
            command = self._command(device, "--run", configuration)
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                process = self._popen_factory(
                    command,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.upstream_path),
                    env=self._environment(),
                    creationflags=creationflags,
                )
            except Exception:
                log_handle.close()
                raise
            self._process = process
            self._log_handle = log_handle
            self._running = True
            self._status = "running"
            self._device = device
            self._last_run_dir = str(run_dir)
            self._last_exit_code = None
            self._last_error = ""
            self._started_at = time.time()
            self._completed_at = None
        self._log("running", f"已在真机 {device} 启动 SRC Rogue")
        watcher = threading.Thread(
            target=self._watch_process,
            args=(process, log_handle),
            daemon=True,
            name="star-rail-copilot-watcher",
        )
        watcher.start()
        return self.snapshot()

    def stop(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._running = False
                if self._status == "running":
                    self._status = "stopped"
                return self.snapshot()
            self._status = "stopping"
        self._log("stopping", "正在停止 SRC Rogue 子进程")
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        return self.snapshot()

    @staticmethod
    def _select_option(
        option_id: str,
        label: str,
        description: str,
        value: str,
        choices: tuple[str, ...] | list[str],
        *,
        group: str,
        scope: str = "task",
    ) -> dict[str, object]:
        return {
            "id": option_id,
            "type": "select",
            "label": label,
            "description": description,
            "value": value,
            "options": [{"value": item, "label": item} for item in choices],
            "group": group,
            "scope": scope,
        }

    @staticmethod
    def _checkbox_option(
        option_id: str,
        label: str,
        description: str,
        value: bool,
        *,
        group: str,
    ) -> dict[str, object]:
        return {
            "id": option_id,
            "type": "checkbox",
            "label": label,
            "description": description,
            "value": value,
            "group": group,
            "scope": "task",
        }

    def snapshot(self) -> dict[str, object]:
        try:
            upstream = validate_upstream_path(self.upstream_path)
            available = True
            disk_bytes = sum(
                item.stat().st_size
                for item in self.upstream_path.rglob("*")
                if item.is_file()
            )
        except Exception as exc:
            upstream = {
                "path": str(self.upstream_path),
                "repository": UPSTREAM_REPOSITORY,
                "license": UPSTREAM_LICENSE,
                "commit": "",
                "route_count": 0,
                "scrcpy_server_versions": [],
                "logical_resolution": [1280, 720],
                "error": str(exc),
            }
            available = False
            disk_bytes = 0
        with self._lock:
            process = self._process
            running = (
                self._running and process is not None and process.poll() is None
            )
            adaptive_geometry = dict(upstream.get("adaptive_geometry") or {})
            return {
                "adapter_id": "star-rail-copilot",
                "end_to_end_verified": False,
                "verification": {
                    "status": "pending",
                    "reason": (
                        "StarRailCopilot 多分辨率自适应几何补丁已接入；识别消费者审计、"
                        "完整模拟宇宙流程和真机矩阵尚未验收。"
                        if adaptive_geometry.get("available") is True
                        else "崩铁底层已更换为 StarRailCopilot；当前 checkout 未安装"
                        "完整多分辨率自适应几何补丁。"
                    ),
                },
                "status": "running" if running else self._status,
                "running": running,
                "available": available,
                "device": self._device,
                "upstream": {
                    **upstream,
                    "disk_bytes": disk_bytes,
                    "disk_mib": round(disk_bytes / 1024 / 1024, 1),
                },
                "preflight": self._last_preflight,
                "runtime_options": [
                    {
                        "id": "upstream_path",
                        "type": "text",
                        "label": "StarRailCopilot 目录",
                        "description": "包含 SRC 原生 Android 设备栈与路线资源的 checkout。",
                        "value": str(self.upstream_path),
                        "required": True,
                        "group": "安装路径",
                        "scope": "environment",
                    },
                    self._select_option(
                        "server",
                        "游戏服务器",
                        "决定 SRC 使用的安卓包名与资源语言。",
                        str(self._options["server"]),
                        tuple(SERVER_PACKAGES),
                        group="游戏账号",
                    ),
                    self._select_option(
                        "screenshot_method",
                        "截图后端",
                        "优先验证 scrcpy，异常时可切换 ADB 或 uiautomator2。",
                        str(self._options["screenshot_method"]),
                        SCREENSHOT_METHODS,
                        group="设备后端",
                        scope="environment",
                    ),
                    self._select_option(
                        "control_method",
                        "触控后端",
                        "使用 SRC 自带的 MaaTouch 或 minitouch。",
                        str(self._options["control_method"]),
                        CONTROL_METHODS,
                        group="设备后端",
                        scope="environment",
                    ),
                    self._select_option(
                        "scrcpy_max_size",
                        "scrcpy 长边",
                        "超宽屏建议 1920；性能受限设备可先使用 1600。",
                        str(self._options["scrcpy_max_size"]),
                        [str(item) for item in SCRCPY_MAX_SIZES],
                        group="设备后端",
                        scope="environment",
                    ),
                    self._select_option(
                        "world",
                        "模拟宇宙世界",
                        "进入前请先在游戏内准备对应队伍。",
                        str(self._options["world"]),
                        ROGUE_WORLDS,
                        group="模拟宇宙目标",
                    ),
                    self._select_option(
                        "path",
                        "命途",
                        "SRC Rogue 的命途选择。",
                        str(self._options["path"]),
                        ROGUE_PATHS,
                        group="模拟宇宙目标",
                    ),
                    self._select_option(
                        "domain_strategy",
                        "区域策略",
                        "优先战斗区域或事件区域。",
                        str(self._options["domain_strategy"]),
                        DOMAIN_STRATEGIES,
                        group="模拟宇宙策略",
                    ),
                    self._checkbox_option(
                        "use_immersifier",
                        "使用沉浸器",
                        "允许 SRC 在结算时消耗沉浸器。",
                        bool(self._options["use_immersifier"]),
                        group="奖励消耗",
                    ),
                    self._checkbox_option(
                        "double_event",
                        "双倍事件",
                        "按 SRC 配置处理双倍位面饰品奖励。",
                        bool(self._options["double_event"]),
                        group="奖励消耗",
                    ),
                    self._checkbox_option(
                        "weekly_farming",
                        "每周积分刷取",
                        "按每周积分目标继续运行。",
                        bool(self._options["weekly_farming"]),
                        group="模拟宇宙策略",
                    ),
                    self._checkbox_option(
                        "use_stamina",
                        "允许使用开拓力",
                        "开启后可能消耗账号资源，默认关闭。",
                        bool(self._options["use_stamina"]),
                        group="奖励消耗",
                    ),
                ],
                "capabilities": {
                    "configure": True,
                    "configure_when_unavailable": True,
                    "preflight": True,
                    "start": True,
                    "stop": True,
                    "screenshot": True,
                    "native_android_stack": True,
                    "adaptive_geometry": adaptive_geometry.get("available") is True,
                },
                "last_error": self._last_error,
                "last_run_dir": self._last_run_dir,
                "last_exit_code": self._last_exit_code,
                "started_at": self._started_at,
                "completed_at": self._completed_at,
                "logs": list(self._logs),
            }

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


# Transitional import compatibility for local callers while the catalog and
# saved selections migrate from the former Auto_Simulated_Universe adapter.
StarRailAsuRuntimeController = StarRailCopilotRuntimeController
