"""Host-side lifecycle for the external Star Rail simulated-universe runtime."""

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

from .star_rail_asu_runner import validate_upstream_path
from .star_rail_bridge import STAR_RAIL_CN_PACKAGE


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


class StarRailAsuRuntimeController:
    """Run the AGPL upstream in a subprocess and expose auditable state."""

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
        self.output_root = (
            output_root / "open-source-automation" / "star-rail-simulated-universe"
        ).resolve()
        self.upstream_path = (
            upstream_path
            or output_root
            / "open-source-runtimes"
            / "Auto_Simulated_Universe"
        ).resolve()
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

    def _command(self, device: str, mode: str, payload: dict[str, object]) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "mobile_profiler.star_rail_asu_runner",
            "--upstream",
            str(self.upstream_path),
            "--serial",
            device,
            "--adb",
            self.adb,
            "--package",
            STAR_RAIL_CN_PACKAGE,
            mode,
        ]
        for key, flag in (
            ("speed", "--speed"),
            ("debug", "--debug"),
            ("bonus", "--bonus"),
        ):
            if _bool(payload.get(key)):
                command.append(flag)
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
        raise RuntimeError("external runtime did not return a preflight summary")

    def preflight(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Star Rail preflight requires device")
        with self._lock:
            if self._running:
                raise RuntimeError("Star Rail simulated universe is already running")
            self._refresh_install_status()
            if self._status == "not_installed":
                raise RuntimeError(self._last_error or "external runtime is not installed")
            self._status = "preflighting"
            self._last_error = ""
            self._device = device
        self._log("preflighting", f"开始检查真机 {device} 与外部模拟宇宙运行时")
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
            raise RuntimeError(f"Star Rail preflight failed: {exc}") from exc
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
        self._log(
            self._status,
            (
                "真机与模拟宇宙运行时已就绪"
                if ready
                else f"等待游戏就绪：{screen.get('screen_state') or 'unknown'}"
            ),
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
                    self._last_error = f"external runtime exited with code {exit_code}"
        self._log(self._status, self._last_error or "模拟宇宙外部运行时已结束")

    def start(self, payload: dict[str, object]) -> dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Star Rail run requires device")
        with self._lock:
            if self._running:
                raise RuntimeError("Star Rail simulated universe is already running")
            preflight = self._last_preflight or {}
            screen = preflight.get("screen") if isinstance(preflight.get("screen"), dict) else {}
            checked_device = preflight.get("device") if isinstance(preflight.get("device"), dict) else {}
            if checked_device.get("serial") != device or screen.get("game_ready") is not True:
                raise RuntimeError(
                    "run a successful game-ready preflight for this device before starting"
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
        self._log("running", f"已在真机 {device} 启动一轮模拟宇宙")
        watcher = threading.Thread(
            target=self._watch_process,
            args=(process, log_handle),
            daemon=True,
            name="star-rail-asu-watcher",
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
        self._log("stopping", "正在停止模拟宇宙外部运行时并释放触摸")
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
                "repository": "https://github.com/CHNZYX/Auto_Simulated_Universe",
                "license": "AGPL-3.0",
                "commit": "",
                "map_count": 0,
                "error": str(exc),
            }
            available = False
            disk_bytes = 0
        with self._lock:
            process = self._process
            running = self._running and process is not None and process.poll() is None
            return {
                "adapter_id": "star-rail-asu",
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
                        "id": "speed",
                        "type": "checkbox",
                        "label": "快速模式",
                        "description": "缩短部分等待时间",
                        "value": False,
                    },
                    {
                        "id": "bonus",
                        "type": "checkbox",
                        "label": "沉浸奖励",
                        "description": "按上游选项领取奖励",
                        "value": False,
                    },
                ],
                "capabilities": {
                    "preflight": True,
                    "start": True,
                    "stop": True,
                    "screenshot": True,
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
