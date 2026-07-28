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
        self._refresh_install_status()

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
        use_immersifier = _bool(payload.get("use_immersifier"), True)
        double_event = _bool(payload.get("double_event"), True)
        weekly_farming = _bool(payload.get("weekly_farming"), False)
        use_stamina = _bool(payload.get("use_stamina"), False)
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
            _choice(payload, "server", "CN-Official"),
            "--screenshot-method",
            _choice(payload, "screenshot_method", "scrcpy"),
            "--scrcpy-max-size",
            str(_scrcpy_max_size(payload)),
            "--control-method",
            _choice(payload, "control_method", "MaaTouch"),
            "--world",
            _choice(payload, "world", ROGUE_WORLDS[-1]),
            "--path",
            _choice(payload, "path", "The_Hunt"),
            "--domain-strategy",
            _choice(payload, "domain_strategy", "combat"),
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
        with self._lock:
            if self._running:
                raise RuntimeError("StarRailCopilot Rogue is already running")
            self._refresh_install_status()
            if self._status == "not_installed":
                raise RuntimeError(self._last_error or "StarRailCopilot is not installed")
            self._status = "preflighting"
            self._last_error = ""
            self._device = device
        self._log("preflighting", f"开始检查真机 {device} 与 StarRailCopilot")
        command = self._command(device, "--preflight", payload)
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
        screen = summary.get("screen") if isinstance(summary.get("screen"), dict) else {}
        ready = screen.get("game_ready") is True
        with self._lock:
            self._last_preflight = summary
            self._status = "ready" if ready else "waiting_for_game"
            self._last_error = ""
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
            if (
                checked_device.get("serial") != device
                or screen.get("game_ready") is not True
            ):
                raise RuntimeError(
                    "run a successful SRC game-ready preflight for this device before starting"
                )
            run_name = time.strftime("%Y%m%d-%H%M%S")
            run_dir = self.output_root / "runs" / run_name
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path = run_dir / "runtime.log"
            log_handle = log_path.open("wb")
            command = self._command(device, "--run", payload)
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
    ) -> dict[str, object]:
        return {
            "id": option_id,
            "type": "select",
            "label": label,
            "description": description,
            "value": value,
            "options": [{"value": item, "label": item} for item in choices],
        }

    @staticmethod
    def _checkbox_option(
        option_id: str,
        label: str,
        description: str,
        value: bool,
    ) -> dict[str, object]:
        return {
            "id": option_id,
            "type": "checkbox",
            "label": label,
            "description": description,
            "value": value,
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
                    self._select_option(
                        "server",
                        "游戏服务器",
                        "决定 SRC 使用的安卓包名与资源语言。",
                        "CN-Official",
                        tuple(SERVER_PACKAGES),
                    ),
                    self._select_option(
                        "screenshot_method",
                        "截图后端",
                        "优先验证 scrcpy，异常时可切换 ADB 或 uiautomator2。",
                        "scrcpy",
                        SCREENSHOT_METHODS,
                    ),
                    self._select_option(
                        "control_method",
                        "触控后端",
                        "使用 SRC 自带的 MaaTouch 或 minitouch。",
                        "MaaTouch",
                        CONTROL_METHODS,
                    ),
                    self._select_option(
                        "scrcpy_max_size",
                        "scrcpy 长边",
                        "超宽屏建议 1920；性能受限设备可先使用 1600。",
                        str(DEFAULT_SCRCPY_MAX_SIZE),
                        [str(item) for item in SCRCPY_MAX_SIZES],
                    ),
                    self._select_option(
                        "world",
                        "模拟宇宙世界",
                        "进入前请先在游戏内准备对应队伍。",
                        ROGUE_WORLDS[-1],
                        ROGUE_WORLDS,
                    ),
                    self._select_option(
                        "path",
                        "命途",
                        "SRC Rogue 的命途选择。",
                        "The_Hunt",
                        ROGUE_PATHS,
                    ),
                    self._select_option(
                        "domain_strategy",
                        "区域策略",
                        "优先战斗区域或事件区域。",
                        "combat",
                        DOMAIN_STRATEGIES,
                    ),
                    self._checkbox_option(
                        "use_immersifier",
                        "使用沉浸器",
                        "允许 SRC 在结算时消耗沉浸器。",
                        True,
                    ),
                    self._checkbox_option(
                        "double_event",
                        "双倍事件",
                        "按 SRC 配置处理双倍位面饰品奖励。",
                        True,
                    ),
                    self._checkbox_option(
                        "weekly_farming",
                        "每周积分刷取",
                        "按每周积分目标继续运行。",
                        False,
                    ),
                    self._checkbox_option(
                        "use_stamina",
                        "允许使用开拓力",
                        "开启后可能消耗账号资源，默认关闭。",
                        False,
                    ),
                ],
                "capabilities": {
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
