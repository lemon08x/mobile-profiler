#!/usr/bin/env python3
"""Run MaaEnd through MaaFramework's C API without starting an upstream UI.

This module is deliberately executed in a child Python process.  MaaFramework,
MaaAgentClient, and third-party Agent processes therefore never enter the
Mobile Profiler process.  The parent prepares a fully resolved request (entry
plus Pipeline overrides); this host owns only the runtime lifecycle and emits
structured evidence.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Mapping


_GENERIC_SCREENCAP_METHODS = 1 | 2 | 4
_GENERIC_INPUT_METHODS = 1 | 2 | 4

_TASK_TERMINAL_NODES: dict[str, frozenset[str]] = {
    "VisitFriends": frozenset(
        {
            "VisitFriendsFull",
            "VisitFriendsMenuScanFriendsFull",
            "VisitFriendsMenuScanScrollFinish",
            "VisitFriendsMenuScanScrollFinishSpecial",
        }
    ),
    "DijiangRewards": frozenset({"FinishDijiangRewards"}),
    "CreditShoppingN2": frozenset(
        {
            "CreditShoppingNothingToBuy",
            "CreditShoppingReserveCredit",
            "CreditShippingCanNotToBuy",
        }
    ),
    "DeliveryJobs": frozenset({"DeliveryJobsFinished"}),
    "SellProduct": frozenset({"SellProductScheduleEnd", "SellProductTaskEnd"}),
    "AutoStockpile": frozenset({"AutoStockpileDone"}),
    "AutoStockStaple": frozenset({"AutoStockStapleDone"}),
    "AutoSell": frozenset({"AutoSellFinish"}),
    "EnvironmentMonitoring": frozenset({"EnvironmentMonitoringFinish"}),
    "DailyRewards": frozenset({"DailyRewardEnd"}),
    "SeizeDeliveryJobs": frozenset({"SeizeDeliveryJobsTaskEnd"}),
    "AutoCollect": frozenset({"AutoCollectEnd"}),
}


def _with_native_adb_swipe(
    raw_config: Mapping[str, object], *, enabled: bool
) -> dict[str, object]:
    """Select ADB shell for swipes without giving up MaaTouch contacts."""

    config = dict(raw_config)
    raw_extras = config.get("extras")
    extras = dict(raw_extras) if isinstance(raw_extras, Mapping) else {}
    raw_native_swipe = extras.get("native_swipe")
    native_swipe = (
        dict(raw_native_swipe)
        if isinstance(raw_native_swipe, Mapping)
        else {}
    )
    native_swipe["enable"] = enabled
    extras["native_swipe"] = native_swipe
    config["extras"] = extras
    return config


class _TaskOutcomeTracker:
    """Collect semantic task outcomes that MaaStatus alone does not expose.

    MaaFramework reports a Tasker job as succeeded when a Pipeline finishes
    running, even if its terminal Pipeline node failed.  Headless automation
    must preserve that distinction or a fail-closed Agent can be mistaken for
    a completed game task.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[int, dict[str, object]] = {}

    def observe(self, message: str, details: Mapping[str, object]) -> None:
        raw_task_id = details.get("task_id")
        if not isinstance(raw_task_id, int):
            return
        with self._lock:
            row = self._tasks.setdefault(
                raw_task_id,
                {
                    "pipeline_failures": [],
                    "pipeline_successes": [],
                    "task_events": [],
                },
            )
            if message.startswith("Tasker.Task."):
                task_events = row["task_events"]
                assert isinstance(task_events, list)
                task_events.append(message)
            elif message == "Node.PipelineNode.Failed":
                failures = row["pipeline_failures"]
                assert isinstance(failures, list)
                failures.append(
                    {
                        "name": str(details.get("name") or ""),
                        "node_id": details.get("node_id"),
                    }
                )
            elif message == "Node.PipelineNode.Succeeded":
                node_details = details.get("node_details")
                resolved_name = ""
                if isinstance(node_details, Mapping):
                    resolved_name = str(node_details.get("name") or "")
                successes = row["pipeline_successes"]
                assert isinstance(successes, list)
                successes.append(
                    {
                        "name": str(details.get("name") or ""),
                        "resolved_name": resolved_name,
                        "node_id": details.get("node_id"),
                    }
                )

    def snapshot(self, task_id: int) -> dict[str, object]:
        with self._lock:
            row = self._tasks.get(task_id)
            if row is None:
                return {
                    "pipeline_failures": [],
                    "pipeline_successes": [],
                    "task_events": [],
                }
            return {
                key: list(value) if isinstance(value, list) else value
                for key, value in row.items()
            }


def _effective_task_status(framework_status: str, outcome: Mapping[str, object]) -> str:
    if framework_status != "succeeded":
        return framework_status
    task_events = outcome.get("task_events")
    if isinstance(task_events, list) and "Tasker.Task.Failed" in task_events:
        return "failed"
    pipeline_failures = outcome.get("pipeline_failures")
    if isinstance(pipeline_failures, list) and pipeline_failures:
        return "failed"
    return "succeeded"


def _validate_task_terminal(
    name: str,
    outcome: Mapping[str, object],
    screenshot: Mapping[str, object],
) -> list[str]:
    """Return semantic terminal-contract violations for a completed task."""

    successes = outcome.get("pipeline_successes")
    resolved_names = {
        str(row.get("resolved_name") or row.get("name") or "")
        for row in successes
        if isinstance(row, Mapping)
    } if isinstance(successes, list) else set()
    errors: list[str] = []
    expected_terminals = _TASK_TERMINAL_NODES.get(name)
    if expected_terminals is not None:
        if not resolved_names.intersection(expected_terminals):
            errors.append(
                f"{name} did not reach a natural terminal node; "
                f"expected one of {sorted(expected_terminals)}, "
                f"successful nodes={sorted(resolved_names)}"
            )
    elif name != "AndroidOpenGame":
        return []

    # A Pipeline terminal alone is insufficient on a physical device.  Every
    # guarded daily task must leave behind a fresh, usable adaptive frame and
    # no pinned touch contacts.  Applying this shared contract per task makes
    # a stale screenshot or leaked joystick contact fail at the task that
    # caused it instead of being hidden by a later successful node.
    if screenshot.get("available") is not True:
        errors.append("terminal controller screenshot is unavailable")
        return errors
    if (screenshot.get("width"), screenshot.get("height")) != (1280, 720):
        errors.append(
            "terminal controller screenshot is not 1280x720: "
            f"{screenshot.get('width')}x{screenshot.get('height')}"
        )
    raw = screenshot.get("raw_resolution")
    if not (
        isinstance(raw, list)
        and len(raw) == 2
        and isinstance(raw[0], int)
        and isinstance(raw[1], int)
        and raw[0] > raw[1]
    ):
        errors.append(f"raw display is not landscape: {raw}")
    controller_info = screenshot.get("controller_info")
    viewport: Mapping[str, object] = {}
    if isinstance(controller_info, Mapping):
        raw_viewport = controller_info.get("screenshot_viewport")
        if isinstance(raw_viewport, Mapping):
            viewport = raw_viewport
    if viewport.get("active") is not True:
        errors.append("adaptive screenshot viewport is not active")
    if viewport.get("adaptive") is not True:
        errors.append("terminal screenshot viewport is not adaptive")
    frame_id = viewport.get("frame_id")
    if not isinstance(frame_id, int) or isinstance(frame_id, bool) or frame_id <= 0:
        errors.append(f"terminal screenshot frame_id is invalid: {frame_id}")
    active_contacts = viewport.get("active_contacts")
    if not isinstance(active_contacts, list):
        errors.append("terminal active contact state is unavailable")
    elif active_contacts:
        errors.append(f"terminal has active touch contacts: {active_contacts}")
    black_fraction = screenshot.get("black_fraction")
    if isinstance(black_fraction, (int, float)) and black_fraction >= 0.98:
        errors.append(f"terminal screenshot is black ({black_fraction:.3f})")
    if name == "AndroidOpenGame" and not resolved_names.intersection(
        {"EnterGame", "InWorld"}
    ):
        errors.append(
            "OpenGame did not reach the EnterGame/InWorld terminal; "
            f"successful nodes={sorted(resolved_names)}"
        )
    return errors


def _run_adb(
    adb_path: Path,
    device: str,
    arguments: list[str],
    *,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(adb_path), "-s", device, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )


class _DeviceAwakeGuard:
    """Keep an attached Android device awake and restore its user settings."""

    def __init__(
        self,
        adb_path: Path,
        device: str,
        events: "_EventWriter",
        *,
        interval_seconds: float = 20.0,
    ) -> None:
        self.adb_path = adb_path
        self.device = device
        self.events = events
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._original_screen_timeout = ""
        self._original_stay_on = ""

    def _shell(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return _run_adb(
            self.adb_path,
            self.device,
            ["shell", *arguments],
        )

    def start(self) -> dict[str, object]:
        serial = self._shell("getprop", "ro.serialno")
        if serial.returncode != 0:
            raise RuntimeError(f"无法查询真机序列号: {serial.stdout.strip()}")
        physical_serial = serial.stdout.strip()
        self._original_screen_timeout = self._shell(
            "settings", "get", "system", "screen_off_timeout"
        ).stdout.strip()
        self._original_stay_on = self._shell(
            "settings", "get", "global", "stay_on_while_plugged_in"
        ).stdout.strip()
        commands = [
            ("input", "keyevent", "WAKEUP"),
            ("wm", "dismiss-keyguard"),
            ("svc", "power", "stayon", "true"),
            ("settings", "put", "system", "screen_off_timeout", "1800000"),
        ]
        failures: list[dict[str, object]] = []
        for command in commands:
            completed = self._shell(*command)
            if completed.returncode != 0:
                failures.append(
                    {
                        "command": list(command),
                        "returncode": completed.returncode,
                        "output": completed.stdout.strip(),
                    }
                )
        if failures:
            raise RuntimeError(f"无法建立真机唤醒保护: {failures}")
        state = {
            "physical_serial": physical_serial,
            "screen_off_timeout_before": self._original_screen_timeout,
            "stay_on_while_plugged_in_before": self._original_stay_on,
            "keepalive_interval_seconds": self.interval_seconds,
        }
        self.events.emit("host", "device.awake_guard_started", state)
        self._thread = threading.Thread(
            target=self._keepalive,
            name="maaend-device-keepalive",
            daemon=True,
        )
        self._thread.start()
        return state

    def _keepalive(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                completed = self._shell("input", "keyevent", "WAKEUP")
                if completed.returncode != 0:
                    self.events.emit(
                        "host",
                        "device.keepalive_failed",
                        {
                            "returncode": completed.returncode,
                            "output": completed.stdout.strip(),
                        },
                    )
            except Exception as exc:
                self.events.emit(
                    "host", "device.keepalive_failed", {"error": str(exc)}
                )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(self.interval_seconds + 2.0, 12.0))
        restored: dict[str, object] = {}
        self._shell("svc", "power", "stayon", "false")
        if self._original_screen_timeout and self._original_screen_timeout != "null":
            completed = self._shell(
                "settings",
                "put",
                "system",
                "screen_off_timeout",
                self._original_screen_timeout,
            )
            restored["screen_off_timeout"] = completed.returncode == 0
        if self._original_stay_on and self._original_stay_on != "null":
            completed = self._shell(
                "settings",
                "put",
                "global",
                "stay_on_while_plugged_in",
                self._original_stay_on,
            )
            restored["stay_on_while_plugged_in"] = completed.returncode == 0
        restored["screen_off_timeout_actual"] = self._shell(
            "settings", "get", "system", "screen_off_timeout"
        ).stdout.strip()
        restored["stay_on_while_plugged_in_actual"] = self._shell(
            "settings", "get", "global", "stay_on_while_plugged_in"
        ).stdout.strip()
        self.events.emit("host", "device.awake_guard_stopped", restored)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _status_name(job: object) -> str:
    status = getattr(job, "status")
    for name in ("succeeded", "failed", "running", "pending"):
        if getattr(status, name, False):
            return name
    return "unknown"


class _EventWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._sequence = 0

    def emit(self, source: str, message: str, details: object = None) -> None:
        with self._lock:
            self._sequence += 1
            row = {
                "sequence": self._sequence,
                "time": time.time(),
                "monotonic": time.monotonic(),
                "source": source,
                "message": message,
                "details": details if details is not None else {},
            }
            self._handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
                + "\n"
            )

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def _bootstrap_binding(binding_root: Path, binary_root: Path) -> dict[str, Any]:
    if not binding_root.is_dir():
        raise RuntimeError(f"MaaFramework Python binding 不存在: {binding_root}")
    if not (binary_root / "MaaFramework.dll").is_file():
        raise RuntimeError(f"MaaFramework 二进制目录无效: {binary_root}")

    # Python 3.11+ includes enum.StrEnum.  MaaFramework keeps the tiny
    # third-party compatibility package for Python 3.9/3.10; provide the same
    # symbol locally so a development host does not need a network install.
    try:
        import strenum  # type: ignore  # noqa: F401
    except ModuleNotFoundError:
        shim = types.ModuleType("strenum")
        shim.StrEnum = enum.StrEnum  # type: ignore[attr-defined]
        sys.modules["strenum"] = shim

    sys.path.insert(0, str(binding_root))
    os.environ["MAAFW_BINARY_PATH"] = str(binary_root)
    dll_cookie = None
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        dll_cookie = os.add_dll_directory(str(binary_root))

    from maa.agent_client import AgentClient
    from maa.context import ContextEventSink
    from maa.controller import AdbController, ControllerEventSink
    from maa.resource import Resource, ResourceEventSink
    from maa.tasker import Tasker, TaskerEventSink
    from maa.toolkit import Toolkit

    return {
        "AdbController": AdbController,
        "AgentClient": AgentClient,
        "ContextEventSink": ContextEventSink,
        "ControllerEventSink": ControllerEventSink,
        "Resource": Resource,
        "ResourceEventSink": ResourceEventSink,
        "Tasker": Tasker,
        "TaskerEventSink": TaskerEventSink,
        "Toolkit": Toolkit,
        "dll_cookie": dll_cookie,
    }


def _wait_job(
    job: object,
    *,
    deadline: float,
    stop_requested: threading.Event,
    label: str,
    events: _EventWriter,
) -> str:
    # MaaFramework uses zero as MaaInvalidId. Its status query reports an
    # invalid job as non-terminal, so waiting on it otherwise burns the whole
    # run deadline and hides the actual Pipeline-override parse error.
    job_id = getattr(job, "job_id", None)
    if isinstance(job_id, int) and job_id == 0:
        raise RuntimeError(f"{label} was rejected by MaaFramework (invalid job id)")
    while not bool(getattr(job, "done")):
        if stop_requested.is_set():
            # A stop-file notification can race the MaaFramework terminal
            # callback by a few milliseconds.  Prefer an already-completed
            # business task over the operator stop signal, but keep the grace
            # window short so an intentional stop remains responsive.
            terminal_grace_deadline = min(deadline, time.monotonic() + 0.5)
            while time.monotonic() < terminal_grace_deadline:
                if bool(getattr(job, "done")):
                    break
                time.sleep(0.05)
            if bool(getattr(job, "done")):
                continue
            raise InterruptedError(f"{label} interrupted")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label} timed out")
        time.sleep(0.1)
    status = _status_name(job)
    events.emit("host", "job.completed", {"label": label, "status": status})
    return status


def _controller_snapshot(
    controller: object,
    output_root: Path,
    stem: str,
    *,
    deadline: float,
    stop_requested: threading.Event,
    events: _EventWriter,
) -> dict[str, object]:
    result: dict[str, object] = {"stem": stem, "available": False}
    try:
        job = controller.post_screencap()
        status = _wait_job(
            job,
            deadline=deadline,
            stop_requested=stop_requested,
            label=f"screenshot:{stem}",
            events=events,
        )
        if status != "succeeded":
            result["status"] = status
            return result
        image = job.get()
        import cv2

        path = output_root / "screenshots" / f"{stem}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), image):
            raise RuntimeError("OpenCV imwrite returned false")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        info = controller.info
        result.update(
            {
                "available": True,
                "path": str(path),
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "pixel_mean": float(gray.mean()),
                "pixel_stddev": float(gray.std()),
                "black_fraction": float((gray <= 5).mean()),
                "raw_resolution": list(controller.resolution),
                "controller_info": info,
            }
        )
        events.emit("host", "screenshot.saved", result)
    except Exception as exc:
        result["error"] = str(exc)
        events.emit("host", "screenshot.failed", result)
    return result


def _terminate_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def run_request(request: Mapping[str, object]) -> dict[str, object]:
    runtime_root = Path(str(request.get("runtime_root") or "")).resolve()
    binary_root = Path(str(request.get("binary_root") or runtime_root / "maafw")).resolve()
    binding_root = Path(str(request.get("binding_root") or "")).resolve()
    output_root = Path(str(request.get("output_root") or "")).resolve()
    adb_path = Path(str(request.get("adb") or "")).resolve()
    device = str(request.get("device") or "").strip()
    max_seconds = float(request.get("max_seconds") or 0.0)
    continue_on_failure = request.get("continue_on_failure") is not False
    agent_transport = str(request.get("agent_transport") or "ipc").strip().lower()
    if not runtime_root.is_dir() or not output_root or not adb_path.is_file() or not device:
        raise RuntimeError("headless request 缺少有效的 runtime/output/adb/device")
    if max_seconds <= 0:
        raise RuntimeError("headless request 的 max_seconds 必须为正数")
    output_root.mkdir(parents=True, exist_ok=True)

    if agent_transport not in {"ipc", "tcp"}:
        raise RuntimeError(f"invalid headless agent transport: {agent_transport}")

    binding = _bootstrap_binding(binding_root, binary_root)
    Toolkit = binding["Toolkit"]
    AdbController = binding["AdbController"]
    Resource = binding["Resource"]
    Tasker = binding["Tasker"]
    AgentClient = binding["AgentClient"]
    events = _EventWriter(output_root / "events.jsonl")
    outcomes = _TaskOutcomeTracker()
    stop_requested = threading.Event()
    stop_path = output_root / "stop.requested"
    if stop_path.exists():
        stop_path.unlink()
    deadline = time.monotonic() + max_seconds
    previous_handlers: dict[int, object] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)

    def watch_stop_file() -> None:
        while not stop_requested.wait(0.25):
            if stop_path.is_file():
                events.emit(
                    "host",
                    "run.stop_file_detected",
                    {"path": str(stop_path)},
                )
                stop_requested.set()
                return

    stop_watcher = threading.Thread(
        target=watch_stop_file,
        name="maaend-stop-file-watcher",
        daemon=True,
    )
    stop_watcher.start()

    class TaskSink(binding["TaskerEventSink"]):
        def on_raw_notification(self, _tasker: object, msg: str, details: dict[str, Any]) -> None:
            outcomes.observe(msg, details)
            events.emit("tasker", msg, details)

    class ContextSink(binding["ContextEventSink"]):
        def on_raw_notification(self, _context: object, msg: str, details: dict[str, Any]) -> None:
            outcomes.observe(msg, details)
            events.emit("context", msg, details)

    class ControllerSink(binding["ControllerEventSink"]):
        def on_raw_notification(self, _controller: object, msg: str, details: dict[str, Any]) -> None:
            events.emit("controller", msg, details)

    class ResourceSink(binding["ResourceEventSink"]):
        def on_raw_notification(self, _resource: object, msg: str, details: dict[str, Any]) -> None:
            events.emit("resource", msg, details)

    tasker = None
    controller = None
    resource = None
    agent_clients: list[object] = []
    agent_processes: list[subprocess.Popen[bytes]] = []
    agent_log_handles: list[object] = []
    awake_guard: _DeviceAwakeGuard | None = None
    screenshots: list[dict[str, object]] = []
    task_results: list[dict[str, object]] = []
    result: dict[str, object] = {
        "schema_version": 1,
        "started_at": time.time(),
        "status": "starting",
        "device": device,
        "runtime_root": str(runtime_root),
        "ui_started": False,
        "tasks": task_results,
        "screenshots": screenshots,
    }
    try:
        events.emit("host", "run.starting", {"device": device})
        if request.get("keep_device_awake") is not False:
            awake_guard = _DeviceAwakeGuard(adb_path, device, events)
            result["device_awake_guard"] = awake_guard.start()
        Toolkit.init_option(output_root / "framework")
        devices = Toolkit.find_adb_devices(adb_path)
        matched = next((item for item in devices if item.address == device), None)
        if matched is None:
            discovered = [item.address for item in devices]
            raise RuntimeError(f"MaaToolkit 未找到 USB 设备 {device}; discovered={discovered}")
        screencap_methods = int(matched.screencap_methods)
        input_methods = int(matched.input_methods)
        generic_fallback = False
        if not screencap_methods & _GENERIC_SCREENCAP_METHODS:
            screencap_methods = _GENERIC_SCREENCAP_METHODS
            generic_fallback = True
        if not input_methods & _GENERIC_INPUT_METHODS:
            input_methods = _GENERIC_INPUT_METHODS
            generic_fallback = True
        # This physical-device fallback keeps MaaTouch for explicit contacts
        # and multi-touch, but routes ordinary swipes through Android's native
        # `input swipe`. Endfield accepts the latter for camera/joystick drags
        # while silently ignoring the otherwise successful MaaTouch sequence.
        native_swipe_enabled = generic_fallback and bool(input_methods & 1)
        controller_config = _with_native_adb_swipe(
            matched.config,
            enabled=native_swipe_enabled,
        )
        result["adb"] = {
            "name": matched.name,
            "path": str(matched.adb_path),
            "address": matched.address,
            "discovered_screencap_methods": int(matched.screencap_methods),
            "discovered_input_methods": int(matched.input_methods),
            "screencap_methods": screencap_methods,
            "input_methods": input_methods,
            "generic_fallback": generic_fallback,
            "native_swipe_enabled": native_swipe_enabled,
        }
        events.emit("host", "adb.selected", result["adb"])

        controller = AdbController(
            adb_path=matched.adb_path,
            address=matched.address,
            screencap_methods=screencap_methods,
            input_methods=input_methods,
            config=controller_config,
            agent_path=binary_root / "MaaAgentBinary",
        )
        if not controller.set_screenshot_target_short_side(720):
            raise RuntimeError("无法设置 720p 逻辑截图短边")
        resource = Resource()
        tasker = Tasker()
        controller.add_sink(ControllerSink())
        resource.add_sink(ResourceSink())
        tasker.add_sink(TaskSink())
        tasker.add_context_sink(ContextSink())
        if not tasker.bind(resource, controller):
            raise RuntimeError("Tasker 绑定 Resource/Controller 失败")

        connection = controller.post_connection()
        resource_jobs: list[tuple[str, object]] = []
        for raw_path in request.get("resource_paths", []):
            path = Path(str(raw_path)).resolve()
            resource_jobs.append((str(path), resource.post_bundle(path)))
        if _wait_job(
            connection,
            deadline=deadline,
            stop_requested=stop_requested,
            label="controller.connection",
            events=events,
        ) != "succeeded":
            raise RuntimeError("ADB Controller 连接失败")
        for path, job in resource_jobs:
            if _wait_job(
                job,
                deadline=deadline,
                stop_requested=stop_requested,
                label=f"resource:{path}",
                events=events,
            ) != "succeeded":
                raise RuntimeError(f"资源加载失败: {path}")
        if not tasker.inited:
            raise RuntimeError("Tasker 在资源和控制器就绪后仍未初始化")
        screenshots.append(
            _controller_snapshot(
                controller,
                output_root,
                "connected",
                deadline=deadline,
                stop_requested=stop_requested,
                events=events,
            )
        )

        pi_env = {
            str(key): str(value)
            for key, value in dict(request.get("pi_env") or {}).items()
        }
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        for index, raw_agent in enumerate(request.get("agents", [])):
            if not isinstance(raw_agent, Mapping):
                continue
            executable = Path(str(raw_agent.get("path") or "")).resolve()
            if not executable.is_file():
                raise RuntimeError(f"Agent 可执行文件不存在: {executable}")
            client = (
                AgentClient.create_tcp()
                if agent_transport == "tcp"
                else AgentClient()
            )
            if not client.bind(resource):
                raise RuntimeError(f"AgentClient 无法绑定资源: {executable.name}")
            if not client.register_sink(resource, controller, tasker):
                raise RuntimeError(f"AgentClient 无法注册事件 sink: {executable.name}")
            client.set_timeout(30_000)
            identifier = client.identifier
            if not identifier:
                raise RuntimeError(f"AgentClient 缺少 identifier: {executable.name}")
            args = [str(item) for item in raw_agent.get("args", [])]
            args.append(identifier)
            agent_env = os.environ.copy()
            agent_env.update(pi_env)
            agent_env["MAA_SCREENSHOT_VIEWPORT"] = "1280x720:adaptive"
            log_path = output_root / "agents" / f"{index + 1}-{executable.stem}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("wb")
            process = subprocess.Popen(
                [str(executable), *args],
                cwd=str(runtime_root),
                env=agent_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            agent_processes.append(process)
            agent_log_handles.append(log_handle)
            events.emit(
                "host",
                "agent.started",
                {
                    "path": str(executable),
                    "pid": process.pid,
                    "identifier": identifier,
                    "transport": agent_transport,
                },
            )
            if not client.connect():
                raise RuntimeError(f"AgentClient 连接失败: {executable.name}")
            agent_clients.append(client)
            events.emit(
                "host",
                "agent.connected",
                {
                    "path": str(executable),
                    "recognitions": client.custom_recognition_list,
                    "actions": client.custom_action_list,
                },
            )

        result["status"] = "running"
        raw_tasks = request.get("tasks")
        tasks = raw_tasks if isinstance(raw_tasks, list) else []
        for index, raw_task in enumerate(tasks):
            if not isinstance(raw_task, Mapping):
                continue
            name = str(raw_task.get("name") or f"task-{index + 1}")
            entry = str(raw_task.get("entry") or "")
            raw_override = raw_task.get("pipeline_override", {})
            if isinstance(raw_override, str):
                pipeline_override = json.loads(raw_override)
            elif isinstance(raw_override, (dict, list)):
                pipeline_override = raw_override
            else:
                raise RuntimeError(f"任务 {name} 的 Pipeline override 类型无效")
            task_row: dict[str, object] = {
                "name": name,
                "entry": entry,
                "status": "running",
                "started_at": time.time(),
            }
            task_results.append(task_row)
            events.emit("host", "task.posted", {"name": name, "entry": entry})
            job = tasker.post_task(entry, pipeline_override)
            task_row["task_id"] = job.job_id
            if job.job_id == 0:
                raise RuntimeError(
                    f"task {name} was rejected by MaaFramework; check entry and Pipeline overrides"
                )
            try:
                framework_status = _wait_job(
                    job,
                    deadline=deadline,
                    stop_requested=stop_requested,
                    label=f"task:{name}",
                    events=events,
                )
            except (InterruptedError, TimeoutError):
                if tasker.running:
                    tasker.post_stop().wait()
                raise
            outcome = outcomes.snapshot(job.job_id)
            status = _effective_task_status(framework_status, outcome)
            task_row["framework_status"] = framework_status
            task_row.update(outcome)
            task_row["status"] = status
            task_row["completed_at"] = time.time()
            detail = job.get()
            if detail is not None:
                task_row["node_ids"] = list(detail.node_id_list)
                node_names: list[str] = []
                try:
                    node_names = [node.name for node in detail.nodes]
                except Exception as exc:
                    task_row["node_detail_error"] = str(exc)
                task_row["node_names"] = node_names
            terminal_screenshot = _controller_snapshot(
                controller,
                output_root,
                f"{index + 1:02d}-{name}",
                deadline=deadline,
                stop_requested=stop_requested,
                events=events,
            )
            screenshots.append(terminal_screenshot)
            terminal_errors = _validate_task_terminal(
                name,
                outcome,
                terminal_screenshot,
            )
            task_row["terminal_contract_errors"] = terminal_errors
            if terminal_errors:
                task_row["status"] = "failed"
                status = "failed"
                events.emit(
                    "host",
                    "task.terminal_contract_failed",
                    {"name": name, "errors": terminal_errors},
                )
            if status != "succeeded" and not continue_on_failure:
                break

        succeeded = bool(task_results) and all(
            row.get("status") == "succeeded" for row in task_results
        ) and len(task_results) == len(tasks)
        result["status"] = "completed" if succeeded else "failed"
        result["successful"] = succeeded
    except InterruptedError as exc:
        result.update({"status": "interrupted", "successful": False, "error": str(exc)})
    except TimeoutError as exc:
        result.update({"status": "timed_out", "successful": False, "error": str(exc)})
    except Exception as exc:
        result.update({"status": "error", "successful": False, "error": str(exc)})
        events.emit("host", "run.error", {"error": str(exc), "type": type(exc).__name__})
    finally:
        stop_requested.set()
        if tasker is not None:
            try:
                if tasker.running:
                    tasker.post_stop().wait()
            except Exception as exc:
                events.emit("host", "tasker.stop_failed", {"error": str(exc)})
        for client in reversed(agent_clients):
            try:
                client.disconnect()
            except Exception as exc:
                events.emit("host", "agent.disconnect_failed", {"error": str(exc)})
        for process in reversed(agent_processes):
            try:
                _terminate_child(process)
            except Exception as exc:
                events.emit("host", "agent.terminate_failed", {"error": str(exc)})
        for handle in agent_log_handles:
            try:
                handle.close()
            except Exception:
                pass
        if awake_guard is not None:
            try:
                awake_guard.stop()
            except Exception as exc:
                events.emit("host", "device.awake_guard_stop_failed", {"error": str(exc)})
        stop_watcher.join(timeout=1.0)
        result["completed_at"] = time.time()
        result["elapsed_seconds"] = result["completed_at"] - float(result["started_at"])
        events.emit(
            "host",
            "run.completed",
            {"status": result.get("status"), "successful": result.get("successful")},
        )
        events.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    _write_json(output_root / "host-result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _parser().parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise RuntimeError("headless request 根节点必须是对象")
    result = run_request(request)
    print(
        json.dumps(
            {
                "successful": result.get("successful"),
                "status": result.get("status"),
                "error": result.get("error", ""),
                "result": str(Path(str(request["output_root"])) / "host-result.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.get("successful") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
