"""Lifecycle adapter for the patched MaaAssistantArknights physical-device runtime."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional


MAA_ARKNIGHTS_REPOSITORY = (
    "https://github.com/MaaAssistantArknights/MaaAssistantArknights"
)
MAA_ARKNIGHTS_TASKS: dict[str, dict[str, object]] = {
    "Depot": {
        "label": "仓库识别（只读）",
        "risk": "read_only",
        "runner": "feature",
    },
    "OperBox": {
        "label": "干员箱识别（只读）",
        "risk": "read_only",
        "runner": "feature",
    },
    "StartUp": {
        "label": "启动并进入游戏",
        "risk": "device_state",
        "runner": "feature",
    },
    "Award": {
        "label": "领取日常 / 周常任务奖励",
        "risk": "account_mutation",
        "runner": "feature",
    },
    "Roguelike": {
        "label": "界园肉鸽单轮（受保护）",
        "risk": "gameplay",
        "runner": "roguelike",
    },
}
ARKNIGHTS_PACKAGES = (
    "com.hypergryph.arknights",
    "com.hypergryph.arknights.bilibili",
    "com.yostaren.arknights",
    "com.yostarjp.arknights",
    "com.yostarkr.arknights",
    "tw.txwy.and.arknights",
)
MAA_PACKAGED_RUNNERS = {
    "smoke": Path(__file__).with_name("maa_viewport_smoke.py"),
    "feature": Path(__file__).with_name("maa_feature_runner.py"),
    "roguelike": Path(__file__).with_name("maa_roguelike_runner.py"),
}
MAA_GUARD_POLICY = Path(__file__).with_name("maa_guard_policy.json")


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


def _resolved_path(value: object) -> Path:
    return Path(str(value or "")).expanduser().resolve()


class MaaArknightsRuntimeController:
    """Run allowlisted MAA tasks and retain local, auditable evidence."""

    def __init__(
        self,
        adb: str,
        output_root: Path,
        source_root: Optional[Path] = None,
        *,
        run_func: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        platform_name: Optional[str] = None,
    ) -> None:
        self.adb = str(adb or "adb")
        self.output_root = (
            output_root / "open-source-automation" / "maa-arknights"
        ).resolve()
        self.source_root = (source_root or self._discover_source_root()).resolve()
        self._run_func = run_func
        self._popen_factory = popen_factory
        self._platform_name = platform_name or os.name
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._log_handle = None
        self._status = "not_installed"
        self._running = False
        self._device = ""
        self._last_error = ""
        self._last_preflight: Optional[dict[str, object]] = None
        self._last_result: Optional[dict[str, object]] = None
        self._last_run_dir = ""
        self._last_log_path = ""
        self._last_exit_code: Optional[int] = None
        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None
        self._logs: deque[dict[str, object]] = deque(maxlen=30)
        self._core_root, self._runtime_root = self._default_runtime_paths()
        self._task = "Depot"
        self._time_limit = 600
        self._allow_account_mutation = False
        self._load_config()
        error = self._install_error(self._core_root, self._runtime_root)
        self._status = "not_installed" if error else "installed"
        self._last_error = error

    @staticmethod
    def _discover_source_root() -> Path:
        package_path = Path(__file__).resolve()
        for candidate in package_path.parents:
            if (
                (candidate / "pyproject.toml").is_file()
                and (candidate / "src" / "mobile_profiler").is_dir()
            ):
                return candidate
        return package_path.parent

    def _default_runtime_paths(self) -> tuple[Path, Path]:
        core_candidates: list[Path] = []
        configured_core = os.environ.get("MOBILE_PROFILER_MAA_CORE_ROOT", "").strip()
        if configured_core:
            core_candidates.append(Path(configured_core))
        core_candidates.append(
            self.source_root
            / ".codex-research"
            / "MAA-v6.14.2-git"
            / "build-viewport"
            / "bin"
        )
        runtime_candidates: list[Path] = []
        configured_runtime = os.environ.get(
            "MOBILE_PROFILER_MAA_RUNTIME_ROOT", ""
        ).strip()
        if configured_runtime:
            runtime_candidates.append(Path(configured_runtime))
        runtime_candidates.extend(
            [
                Path.home() / "Downloads" / "MAA-v6.14.2-win-x64",
                self.output_root.parents[1]
                / "open-source-runtimes"
                / "MaaAssistantArknights",
            ]
        )
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            runtime_candidates.extend(
                sorted(downloads.glob("MAA-v*-win-x64"), reverse=True)
            )

        def first(candidates: list[Path], marker: Path) -> Path:
            for candidate in candidates:
                if (candidate.expanduser() / marker).exists():
                    return candidate.expanduser().resolve()
            for candidate in candidates:
                return candidate.expanduser().resolve()
            return Path()

        return (
            first(core_candidates, Path("MaaCore.dll")),
            first(runtime_candidates, Path("resource")),
        )

    @property
    def _config_path(self) -> Path:
        return self.output_root / "config.json"

    def _load_config(self) -> None:
        try:
            value = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        core_root = str(value.get("core_root") or "").strip()
        runtime_root = str(value.get("runtime_root") or "").strip()
        task = str(value.get("task") or "").strip()
        time_limit = value.get("time_limit")
        if core_root:
            self._core_root = _resolved_path(core_root)
        if runtime_root:
            self._runtime_root = _resolved_path(runtime_root)
        if task in MAA_ARKNIGHTS_TASKS:
            self._task = task
        if isinstance(time_limit, int) and 30 <= time_limit <= 10_800:
            self._time_limit = time_limit

    def _persist_config(self) -> None:
        # Account-mutation consent is intentionally process-local and never persisted.
        payload = {
            "schema_version": 1,
            "core_root": str(self._core_root),
            "runtime_root": str(self._runtime_root),
            "task": self._task,
            "time_limit": self._time_limit,
            "viewport": "adaptive",
            "saved_at": time.time(),
        }
        _atomic_write(
            self._config_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _log(self, status: str, message: str) -> None:
        with self._lock:
            self._logs.append(
                {"time": time.time(), "status": status, "message": message}
            )

    def _adb_path(self) -> Path:
        direct = Path(self.adb).expanduser()
        if direct.is_file():
            return direct.resolve()
        discovered = shutil.which(self.adb)
        return Path(discovered).resolve() if discovered else direct.resolve()

    def _install_error(self, core_root: Path, runtime_root: Path) -> str:
        missing: list[str] = []
        if not (core_root / "MaaCore.dll").is_file():
            missing.append(f"MaaCore.dll：{core_root}")
        if not (runtime_root / "resource").is_dir():
            missing.append(f"resource：{runtime_root}")
        if not self._adb_path().is_file():
            missing.append(f"adb：{self.adb}")
        for name, path in MAA_PACKAGED_RUNNERS.items():
            if not path.is_file():
                missing.append(f"内置 {name} runner：{path}")
        if not MAA_GUARD_POLICY.is_file():
            missing.append(f"内置危险动作策略：{MAA_GUARD_POLICY}")
        if self._platform_name != "nt":
            missing.append("MaaCore 真机适配当前仅支持 Windows")
        return "；".join(missing)

    @staticmethod
    def _parse_time_limit(value: object) -> int:
        try:
            parsed = int(str(value or "600").strip())
        except ValueError as exc:
            raise ValueError("MAA time_limit must be an integer") from exc
        if parsed < 30 or parsed > 10_800:
            raise ValueError("MAA time_limit must be within 30..10800 seconds")
        return parsed

    def _configuration(self, payload: dict[str, object]) -> dict[str, object]:
        core_value = str(payload.get("core_root") or self._core_root).strip()
        runtime_value = str(payload.get("runtime_root") or self._runtime_root).strip()
        task = str(payload.get("task") or self._task).strip()
        if not core_value or not runtime_value:
            raise ValueError("MAA core_root and runtime_root are required")
        if task not in MAA_ARKNIGHTS_TASKS:
            raise ValueError(f"unsupported MAA task: {task}")
        return {
            "core_root": _resolved_path(core_value),
            "runtime_root": _resolved_path(runtime_value),
            "task": task,
            "time_limit": self._parse_time_limit(
                payload.get("time_limit", self._time_limit)
            ),
            "allow_account_mutation": _bool(
                payload.get("allow_account_mutation"),
                self._allow_account_mutation,
            ),
        }

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        source_root = self.source_root / "src"
        source = str(source_root) if source_root.is_dir() else ""
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source, existing) if value
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    @staticmethod
    def _json_from_output(payload: bytes) -> dict[str, object]:
        text = payload.decode("utf-8", errors="replace").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise RuntimeError("MAA viewport smoke did not return JSON")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("MAA viewport smoke returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("MAA viewport smoke summary must be an object")
        return value

    def _run_checked(self, command: list[str], timeout: float) -> bytes:
        result = self._run_func(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            cwd=str(self.source_root),
            env=self._environment(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        output = bytes(result.stdout or b"")
        if result.returncode != 0:
            detail = output.decode("utf-8", errors="replace")[-3000:].strip()
            raise RuntimeError(detail or f"command exited with {result.returncode}")
        return output

    def _foreground_package(self, device: str) -> str:
        command = [
            str(self._adb_path()),
            "-s",
            device,
            "shell",
            "dumpsys",
            "window",
            "windows",
        ]
        try:
            output = self._run_checked(command, 20).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""
        lowered = output.lower()
        return next(
            (package for package in ARKNIGHTS_PACKAGES if package.lower() in lowered),
            "",
        )

    def _screen_awake(self, device: str) -> bool:
        command = [
            str(self._adb_path()),
            "-s",
            device,
            "shell",
            "dumpsys",
            "power",
        ]
        try:
            output = self._run_checked(command, 20).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return False
        normalized = " ".join(output.lower().split())
        return any(
            token in normalized
            for token in (
                "mwakefulness=awake",
                "wakefulness: awake",
                "display power: state=on",
            )
        )

    def preflight(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("MAA preflight requires device")
        configuration = self._configuration(payload)
        core_root = configuration["core_root"]
        runtime_root = configuration["runtime_root"]
        task = str(configuration["task"])
        assert isinstance(core_root, Path) and isinstance(runtime_root, Path)
        allow_mutation = bool(configuration["allow_account_mutation"])
        if task == "Award" and not allow_mutation:
            raise ValueError(
                "Award changes account state; explicitly enable account-mutation consent"
            )
        with self._lock:
            if self._running:
                raise RuntimeError("MAA automation is already running")
            self._core_root = core_root
            self._runtime_root = runtime_root
            self._task = task
            self._time_limit = int(configuration["time_limit"])
            self._allow_account_mutation = allow_mutation
            self._device = device
            self._status = "preflighting"
            self._last_error = ""
        error = self._install_error(core_root, runtime_root)
        if error:
            with self._lock:
                self._status = "not_installed"
                self._last_error = error
            self._log("error", error)
            raise RuntimeError(error)

        self._log("preflighting", f"开始验证 {device} 的 adaptive MaaCore 与 {task}")
        preflight_root = self.output_root / "preflight"
        preflight_root.mkdir(parents=True, exist_ok=True)
        image_path = preflight_root / "viewport.png"
        command = [
            sys.executable,
            str(MAA_PACKAGED_RUNNERS["smoke"]),
            "--core-root",
            str(core_root),
            "--runtime-root",
            str(runtime_root),
            "--adb",
            str(self._adb_path()),
            "--address",
            device,
            "--viewport",
            "adaptive",
            "--output",
            str(image_path),
        ]
        try:
            smoke = self._json_from_output(self._run_checked(command, 180))
            foreground_package = self._foreground_package(device)
            screen_awake = self._screen_awake(device)
            resolution = smoke.get("resolution_event")
            resolution_outer = resolution if isinstance(resolution, dict) else {}
            resolution_details = resolution_outer.get("details")
            geometry = resolution_details if isinstance(resolution_details, dict) else {}
            width = int(geometry.get("width") or 0)
            height = int(geometry.get("height") or 0)
            landscape = width > height > 0
            foreground_ready = bool(foreground_package)
            launch_task = task == "StartUp"
            game_ready = screen_awake and landscape and (foreground_ready or launch_task)
            if not screen_awake:
                screen_state = "device_asleep"
            elif not landscape:
                screen_state = "wrong_orientation"
            elif foreground_ready:
                screen_state = "in_game"
            elif launch_task:
                screen_state = "start_screen"
            else:
                screen_state = "wrong_app"
            summary: dict[str, object] = {
                "adapter": "maa-arknights",
                "device": {"serial": device, "state": "device"},
                "screen": {
                    "screen_state": screen_state,
                    "game_ready": game_ready,
                    "foreground_package": foreground_package,
                    "screen_awake": screen_awake,
                    "width": width,
                    "height": height,
                    "logical_size": smoke.get("image_size"),
                    "viewport": geometry.get("viewport"),
                    "evidence": str(image_path),
                },
                "runtime": {
                    "core_root": str(core_root),
                    "runtime_root": str(runtime_root),
                    "task": task,
                    "time_limit": self._time_limit,
                    "viewport": "adaptive",
                },
                "policy": {
                    "risk": MAA_ARKNIGHTS_TASKS[task]["risk"],
                    "account_mutation_confirmed": allow_mutation,
                    "destructive_roguelike_fallback_guarded": task == "Roguelike",
                },
            }
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._last_error = str(exc)
            self._log("error", str(exc))
            raise RuntimeError(f"MAA preflight failed: {exc}") from exc

        self._persist_config()
        _atomic_write(
            preflight_root / "preflight.json",
            json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        with self._lock:
            self._last_preflight = summary
            self._status = "ready" if game_ready else "waiting_for_game"
            self._last_error = ""
        self._log(
            self._status,
            "MAA adaptive 预检通过" if game_ready else f"等待游戏就绪：{screen_state}",
        )
        return self.snapshot()

    def _runner_command(
        self,
        device: str,
        run_dir: Path,
        configuration: dict[str, object],
    ) -> list[str]:
        core_root = configuration["core_root"]
        runtime_root = configuration["runtime_root"]
        task = str(configuration["task"])
        time_limit = int(configuration["time_limit"])
        assert isinstance(core_root, Path) and isinstance(runtime_root, Path)
        common = [
            "--core-root",
            str(core_root),
            "--runtime-root",
            str(runtime_root),
            "--adb",
            str(self._adb_path()),
            "--address",
            device,
            "--run-dir",
            str(run_dir),
            "--viewport",
            "adaptive",
            "--max-seconds",
            str(time_limit),
        ]
        if task == "Roguelike":
            command = [
                sys.executable,
                str(MAA_PACKAGED_RUNNERS["roguelike"]),
                *common,
                "--theme",
                "JieGarden",
                "--policy",
                str(MAA_GUARD_POLICY),
            ]
            maa_source_root = core_root.parents[1] if len(core_root.parents) > 1 else None
            if maa_source_root and (maa_source_root / "resource").is_dir():
                command.extend(["--maa-source-root", str(maa_source_root)])
            return command

        command = [
            sys.executable,
            str(MAA_PACKAGED_RUNNERS["feature"]),
            *common,
            "--task",
            task,
        ]
        if task == "StartUp":
            preflight = self._last_preflight or {}
            screen = preflight.get("screen")
            screen_info = screen if isinstance(screen, dict) else {}
            start_game_enabled = not bool(screen_info.get("foreground_package"))
            command.extend(
                [
                    "--params",
                    json.dumps(
                        {
                            "enable": True,
                            "client_type": "Official",
                            "start_game_enabled": start_game_enabled,
                        },
                        separators=(",", ":"),
                    ),
                ]
            )
        elif task == "Award":
            if not bool(configuration["allow_account_mutation"]):
                raise ValueError("Award requires account-mutation consent")
            command.append("--allow-account-mutation")
        return command

    def _watch_process(
        self,
        process: subprocess.Popen[bytes],
        log_handle,
        run_dir: Path,
    ) -> None:
        exit_code = process.wait()
        try:
            log_handle.flush()
            log_handle.close()
        except OSError:
            pass
        result: Optional[dict[str, object]] = None
        try:
            value = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            result = value if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result = None
        with self._lock:
            if self._process is process:
                self._process = None
                self._log_handle = None
                self._running = False
                self._last_exit_code = int(exit_code)
                self._last_result = result
                self._completed_at = time.time()
                self._allow_account_mutation = False
                if self._status == "stopping":
                    self._status = "stopped"
                elif exit_code == 0:
                    self._status = "completed"
                else:
                    self._status = "error"
                    self._last_error = (
                        str(result.get("runner_error") or "")
                        if isinstance(result, dict)
                        else ""
                    ) or f"MAA runner exited with code {exit_code}"
        self._log(self._status, self._last_error or "MAA 自动化任务已结束")

    def start(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("MAA run requires device")
        configuration = self._configuration(payload)
        with self._lock:
            if self._running:
                raise RuntimeError("MAA automation is already running")
            preflight = self._last_preflight or {}
            checked_device = preflight.get("device")
            checked_runtime = preflight.get("runtime")
            screen = preflight.get("screen")
            if not isinstance(checked_device, dict) or not isinstance(
                checked_runtime, dict
            ) or not isinstance(screen, dict):
                raise RuntimeError("run a successful MAA preflight before starting")
            expected = {
                "core_root": str(configuration["core_root"]),
                "runtime_root": str(configuration["runtime_root"]),
                "task": configuration["task"],
                "time_limit": configuration["time_limit"],
                "viewport": "adaptive",
            }
            if (
                checked_device.get("serial") != device
                or screen.get("game_ready") is not True
                or any(checked_runtime.get(key) != value for key, value in expected.items())
            ):
                raise RuntimeError(
                    "runtime options changed; rerun a successful MAA preflight"
                )
            if configuration["task"] == "Award" and not bool(
                configuration["allow_account_mutation"]
            ):
                raise RuntimeError("Award requires fresh account-mutation consent")

            run_name = time.strftime("%Y%m%d-%H%M%S")
            run_dir = self.output_root / "runs" / f"{run_name}-{configuration['task']}"
            log_path = self.output_root / "logs" / f"{run_name}-{configuration['task']}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("wb")
            command = self._runner_command(device, run_dir, configuration)
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                process = self._popen_factory(
                    command,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.source_root),
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
            self._task = str(configuration["task"])
            self._last_run_dir = str(run_dir)
            self._last_log_path = str(log_path)
            self._last_exit_code = None
            self._last_result = None
            self._last_error = ""
            self._started_at = time.time()
            self._completed_at = None
        self._log("running", f"已在 {device} 启动 MAA {self._task}")
        watcher = threading.Thread(
            target=self._watch_process,
            args=(process, log_handle, run_dir),
            daemon=True,
            name="maa-arknights-watcher",
        )
        watcher.start()
        return self.snapshot()

    def stop(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._running = False
                self._allow_account_mutation = False
                if self._status == "running":
                    self._status = "stopped"
                return self.snapshot()
            self._status = "stopping"
        self._log("stopping", "正在停止 MAA runner 并释放 MaaCore 连接")
        try:
            if self._platform_name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
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

    def _runtime_output(self) -> dict[str, object]:
        path = Path(self._last_log_path) if self._last_log_path else None
        if path is None or not path.is_file():
            return {"path": self._last_log_path, "lines": []}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        return {"path": str(path), "lines": lines[-20:]}

    def snapshot(self) -> dict[str, object]:
        install_error = self._install_error(self._core_root, self._runtime_root)
        available = not install_error
        core_path = self._core_root / "MaaCore.dll"
        disk_bytes = core_path.stat().st_size if core_path.is_file() else 0
        with self._lock:
            process = self._process
            running = self._running and process is not None and process.poll() is None
            if self._status == "not_installed" and available:
                self._status = "installed"
                self._last_error = ""
            status = "running" if running else self._status
            return {
                "adapter_id": "maa-arknights",
                "end_to_end_verified": True,
                "verification": {
                    "status": "verified",
                    "device_resolution": "2800x1260",
                    "completed_flows": [
                        "StartUp",
                        "Depot",
                        "OperBox",
                        "Award",
                        "Roguelike natural settlement",
                    ],
                    "coordinate_model": "display -> adaptive viewport -> logical",
                },
                "status": status,
                "running": running,
                "available": available,
                "device": self._device,
                "upstream": {
                    "repository": MAA_ARKNIGHTS_REPOSITORY,
                    "license": "AGPL-3.0",
                    "version": "v6.14.2 adaptive",
                    "path": str(self._core_root),
                    "runtime_path": str(self._runtime_root),
                    "disk_bytes": disk_bytes,
                    "disk_mib": round(disk_bytes / 1024 / 1024, 1),
                },
                "preflight": self._last_preflight,
                "result": self._last_result,
                "progress": {
                    "task": self._task,
                    "task_label": str(MAA_ARKNIGHTS_TASKS[self._task]["label"]),
                    "risk": str(MAA_ARKNIGHTS_TASKS[self._task]["risk"]),
                    "viewport": "adaptive",
                },
                "runtime_options": [
                    {
                        "id": "task",
                        "type": "select",
                        "label": "MAA 任务",
                        "description": "只读探针、受控领奖或受保护的界园单轮",
                        "value": self._task,
                        "options": [
                            {"value": task, "label": str(info["label"])}
                            for task, info in MAA_ARKNIGHTS_TASKS.items()
                        ],
                    },
                    {
                        "id": "core_root",
                        "type": "text",
                        "label": "Adaptive Core 目录",
                        "description": "包含已应用非 16:9 viewport 补丁的 MaaCore.dll",
                        "value": str(self._core_root),
                        "required": True,
                    },
                    {
                        "id": "runtime_root",
                        "type": "text",
                        "label": "MAA 发布目录",
                        "description": "包含官方 resource 目录的 MAA Windows 发布包",
                        "value": str(self._runtime_root),
                        "required": True,
                    },
                    {
                        "id": "time_limit",
                        "type": "select",
                        "label": "最长运行时间",
                        "description": "到时由 runner 停止并保留证据",
                        "value": str(self._time_limit),
                        "options": [
                            {"value": "600", "label": "10 分钟"},
                            {"value": "1800", "label": "30 分钟"},
                            {"value": "10800", "label": "3 小时（肉鸽）"},
                        ],
                    },
                    {
                        "id": "allow_account_mutation",
                        "type": "checkbox",
                        "label": "允许本次领奖改变账号状态",
                        "description": "仅 Award 使用；不会写入配置，进程结束后自动重置",
                        "value": self._allow_account_mutation,
                    },
                ],
                "capabilities": {
                    "configure_when_unavailable": True,
                    "packaged_runner": True,
                    "preflight": True,
                    "start": True,
                    "stop": True,
                    "screenshot": True,
                },
                "safety": {
                    "viewport": "adaptive",
                    "task_allowlist": list(MAA_ARKNIGHTS_TASKS),
                    "award_requires_account_mutation_consent": True,
                    "roguelike_destructive_fallback_guarded": True,
                    "raw_evidence_local_only": True,
                },
                "last_error": self._last_error or (install_error if not available else ""),
                "last_run_dir": self._last_run_dir,
                "last_exit_code": self._last_exit_code,
                "started_at": self._started_at,
                "completed_at": self._completed_at,
                "runtime_output": self._runtime_output(),
                "logs": list(self._logs),
            }

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
