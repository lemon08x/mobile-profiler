#!/usr/bin/env python3
"""Run one guarded, evidence-producing MAA roguelike exploration on Windows."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path


from mobile_profiler.maa_iteration import (
    CallbackMonitor,
    IncidentSignal,
    callback_summary,
    classify_exit,
    collect_environment_manifest,
    load_guard_policy,
    recent_event_buffer,
    record_incident_bundle,
    write_guard_overlay,
    write_json_atomic,
)


ASST_OPTION_TOUCH_MODE = 2
ASST_OPTION_CLIENT_TYPE = 6
ASST_OPTION_VIEWPORT = 7

MSG_INTERNAL_ERROR = 0
MSG_INIT_FAILED = 1
MSG_CONNECTION_INFO = 2
MSG_ALL_TASKS_COMPLETED = 3
MSG_TASK_CHAIN_ERROR = 10000
MSG_TASK_CHAIN_START = 10001
MSG_TASK_CHAIN_COMPLETED = 10002
MSG_TASK_CHAIN_EXTRA = 10003
MSG_TASK_CHAIN_STOPPED = 10004
MSG_SUBTASK_ERROR = 20000
MSG_SUBTASK_START = 20001
MSG_SUBTASK_COMPLETED = 20002
MSG_SUBTASK_EXTRA = 20003

MESSAGE_NAMES = {
    MSG_INTERNAL_ERROR: "InternalError",
    MSG_INIT_FAILED: "InitFailed",
    MSG_CONNECTION_INFO: "ConnectionInfo",
    MSG_ALL_TASKS_COMPLETED: "AllTasksCompleted",
    MSG_TASK_CHAIN_ERROR: "TaskChainError",
    MSG_TASK_CHAIN_START: "TaskChainStart",
    MSG_TASK_CHAIN_COMPLETED: "TaskChainCompleted",
    MSG_TASK_CHAIN_EXTRA: "TaskChainExtraInfo",
    MSG_TASK_CHAIN_STOPPED: "TaskChainStopped",
    MSG_SUBTASK_ERROR: "SubTaskError",
    MSG_SUBTASK_START: "SubTaskStart",
    MSG_SUBTASK_COMPLETED: "SubTaskCompleted",
    MSG_SUBTASK_EXTRA: "SubTaskExtraInfo",
}

PROGRESS_TASK_TOKENS = (
    "StartExplore",
    "Stage",
    "Battle",
    "Mission",
    "Routing",
    "Recruit",
    "Trader",
    "GamePass",
    "Exit",
    "Abandon",
    "Settlement",
    "Difficulty",
    "Squad",
    "Roles",
)

ROGUELIKE_PARAM_DEFAULTS: dict[str, object] = {
    "theme": "JieGarden",
    "mode": 0,
    "starts_count": 1,
    "difficulty": 2_147_483_647,
    "investment_enabled": True,
    "investments_count": 999,
    "stop_when_investment_full": False,
    "stop_at_final_boss": False,
    "stop_at_max_level": False,
    "use_support": False,
    "use_nonfriend_support": False,
    "refresh_trader_with_dice": False,
}
ROGUELIKE_OPTIONAL_TEXT_PARAMS = frozenset({"squad", "roles", "core_char"})


def resolve_roguelike_params(theme: str, raw_params: object) -> dict[str, object]:
    """Validate and merge Web/runtime overrides into the guarded task params."""

    if raw_params in (None, ""):
        decoded: object = {}
    elif isinstance(raw_params, str):
        try:
            decoded = json.loads(raw_params)
        except json.JSONDecodeError as exc:
            raise ValueError("--params-json must be valid JSON") from exc
    else:
        decoded = raw_params
    if not isinstance(decoded, dict):
        raise ValueError("--params-json must be a JSON object")
    allowed = set(ROGUELIKE_PARAM_DEFAULTS) | set(ROGUELIKE_OPTIONAL_TEXT_PARAMS)
    unknown = set(str(key) for key in decoded) - allowed
    if unknown:
        raise ValueError(f"unsupported Roguelike option(s): {', '.join(sorted(unknown))}")

    params = dict(ROGUELIKE_PARAM_DEFAULTS)
    params["theme"] = str(theme or "JieGarden")
    for key, value in decoded.items():
        if key in ROGUELIKE_OPTIONAL_TEXT_PARAMS:
            text = str(value or "").strip()
            if len(text) > 200:
                raise ValueError(f"Roguelike {key} is too long")
            if text:
                params[key] = text
            continue
        default = ROGUELIKE_PARAM_DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"Roguelike {key} must be a boolean")
            params[key] = value
        elif isinstance(default, int):
            if isinstance(value, bool):
                raise ValueError(f"Roguelike {key} must be an integer")
            try:
                params[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Roguelike {key} must be an integer") from exc
        else:
            params[key] = str(value)
    if int(params["starts_count"]) != 1:
        raise ValueError("guarded Roguelike runner supports exactly one natural-settlement round")
    if int(params["mode"]) < 0 or int(params["mode"]) > 5:
        raise ValueError("Roguelike mode must be within 0..5")
    return params


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one JieGarden exploration with settlement stopping, destructive-action "
            "guards, watchdogs and incident evidence."
        )
    )
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--maa-source-root", type=Path)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).with_name("maa_guard_policy.json"),
    )
    parser.add_argument("--viewport", default="adaptive")
    parser.add_argument("--theme", default="JieGarden")
    parser.add_argument("--client-type", default="Official")
    parser.add_argument(
        "--params-json",
        help="validated JSON overrides for the MaaCore Roguelike task",
    )
    parser.add_argument("--max-seconds", type=float, default=3 * 60 * 60)
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    parser.add_argument("--snapshot-seconds", type=float, default=30)
    parser.add_argument("--skip-startup", action="store_true")
    parser.add_argument("--no-device-manifest", action="store_true")

    settlement = parser.add_mutually_exclusive_group()
    settlement.add_argument(
        "--stop-after-settlement",
        dest="stop_after_settlement",
        action="store_true",
    )
    settlement.add_argument(
        "--continue-after-settlement",
        dest="stop_after_settlement",
        action="store_false",
    )
    parser.set_defaults(stop_after_settlement=None)

    destructive = parser.add_mutually_exclusive_group()
    destructive.add_argument(
        "--guard-destructive-actions",
        dest="guard_destructive_actions",
        action="store_true",
    )
    destructive.add_argument(
        "--allow-destructive-actions",
        dest="guard_destructive_actions",
        action="store_false",
        help="Explicitly allow ExitThenAbandon; intended only for a deliberate reset run.",
    )
    parser.set_defaults(guard_destructive_actions=True)
    return parser.parse_args()


def _utf8(value: str | Path) -> bytes:
    return os.fspath(value).encode("utf-8")


def _task_name(details: object) -> str:
    if not isinstance(details, dict):
        return ""
    nested = details.get("details")
    if isinstance(nested, dict):
        task = nested.get("task")
        if isinstance(task, str):
            return task
    return ""


def _should_print(message: int, details: object) -> bool:
    if message in {
        MSG_INTERNAL_ERROR,
        MSG_INIT_FAILED,
        MSG_ALL_TASKS_COMPLETED,
        MSG_TASK_CHAIN_ERROR,
        MSG_TASK_CHAIN_START,
        MSG_TASK_CHAIN_COMPLETED,
        MSG_TASK_CHAIN_EXTRA,
        MSG_TASK_CHAIN_STOPPED,
        MSG_SUBTASK_ERROR,
        MSG_SUBTASK_EXTRA,
    }:
        return True
    if message == MSG_CONNECTION_INFO and isinstance(details, dict):
        return details.get("what") in {
            "Connected",
            "UnsupportedResolution",
            "ResolutionInfo",
            "Reconnecting",
            "Reconnected",
            "Disconnect",
            "ScreencapFailed",
        }
    if message in (MSG_SUBTASK_START, MSG_SUBTASK_COMPLETED):
        task = _task_name(details)
        return any(token in task for token in PROGRESS_TASK_TOKENS)
    return False


def _validate_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def main() -> int:
    args = _parse_args()
    _validate_positive("max-seconds", args.max_seconds)
    _validate_positive("heartbeat-seconds", args.heartbeat_seconds)
    _validate_positive("snapshot-seconds", args.snapshot_seconds)

    core_root = args.core_root.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    adb_path = args.adb.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    maa_source_root = args.maa_source_root.expanduser().resolve() if args.maa_source_root else None

    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory must be empty to keep evidence immutable: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    user_dir = run_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    core_path = core_root / "MaaCore.dll"
    if not core_path.is_file():
        raise RuntimeError(f"MaaCore.dll not found: {core_path}")
    if not (runtime_root / "resource").is_dir():
        raise RuntimeError(f"resource directory not found: {runtime_root / 'resource'}")
    if not adb_path.is_file():
        raise RuntimeError(f"adb executable not found: {adb_path}")
    if not hasattr(os, "add_dll_directory") or not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("the MaaCore runner requires Windows")

    policy = load_guard_policy(policy_path)
    stop_after_settlement = (
        bool(policy.get("stop_after_settlement", True))
        if args.stop_after_settlement is None
        else bool(args.stop_after_settlement)
    )
    monitor = CallbackMonitor(
        policy,
        guard_destructive_actions=args.guard_destructive_actions,
        stop_after_settlement=stop_after_settlement,
    )

    startup_params = {
        "client_type": args.client_type,
        "start_game_enabled": True,
    }
    roguelike_params = resolve_roguelike_params(args.theme, args.params_json)
    request = {
        "schema_version": 2,
        "core_root": os.fspath(core_root),
        "runtime_root": os.fspath(runtime_root),
        "maa_source_root": os.fspath(maa_source_root) if maa_source_root else None,
        "startup": startup_params,
        "roguelike": roguelike_params,
        "viewport": args.viewport,
        "address": args.address,
        "skip_startup": args.skip_startup,
        "stop_after_settlement": stop_after_settlement,
        "guard_destructive_actions": args.guard_destructive_actions,
        "policy": os.fspath(policy_path),
        "started_at": datetime.now().astimezone().isoformat(),
    }
    write_json_atomic(run_dir / "request.json", request)

    environment = collect_environment_manifest(
        core_path=core_path,
        runtime_root=runtime_root,
        adb_path=adb_path,
        address=args.address,
        viewport=args.viewport,
        policy_path=policy_path,
        maa_source_root=maa_source_root,
        probe_device=not args.no_device_manifest,
    )
    guard_overlay_root: Path | None = None
    if args.guard_destructive_actions:
        guard_overlay_root, guard_task_path = write_guard_overlay(run_dir / "guard-overlay", policy)
        environment["guard_overlay"] = {
            "enabled": True,
            "root": os.fspath(guard_overlay_root),
            "task_file": os.fspath(guard_task_path),
        }
    else:
        environment["guard_overlay"] = {"enabled": False}
    write_json_atomic(run_dir / "environment.json", environment)

    event_lock = threading.Lock()
    event_tail = recent_event_buffer(100)
    pending_signals: deque[IncidentSignal] = deque()
    event_file = (run_dir / "events.jsonl").open("a", encoding="utf-8")
    incident_summaries: list[dict[str, object]] = []
    latest_image: bytes | None = None

    dll_search = os.add_dll_directory(os.fspath(core_root))
    core = ctypes.WinDLL(os.fspath(core_path))
    callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p)

    core.AsstSetUserDir.argtypes = [ctypes.c_char_p]
    core.AsstSetUserDir.restype = ctypes.c_uint8
    core.AsstLoadResource.argtypes = [ctypes.c_char_p]
    core.AsstLoadResource.restype = ctypes.c_uint8
    core.AsstCreateEx.argtypes = [callback_type, ctypes.c_void_p]
    core.AsstCreateEx.restype = ctypes.c_void_p
    core.AsstDestroy.argtypes = [ctypes.c_void_p]
    core.AsstSetInstanceOption.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_char_p]
    core.AsstSetInstanceOption.restype = ctypes.c_uint8
    core.AsstConnect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    core.AsstConnect.restype = ctypes.c_uint8
    core.AsstConnected.argtypes = [ctypes.c_void_p]
    core.AsstConnected.restype = ctypes.c_uint8
    core.AsstAppendTask.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    core.AsstAppendTask.restype = ctypes.c_int32
    core.AsstStart.argtypes = [ctypes.c_void_p]
    core.AsstStart.restype = ctypes.c_uint8
    core.AsstStop.argtypes = [ctypes.c_void_p]
    core.AsstStop.restype = ctypes.c_uint8
    core.AsstRunning.argtypes = [ctypes.c_void_p]
    core.AsstRunning.restype = ctypes.c_uint8
    core.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
    core.AsstGetImage.restype = ctypes.c_uint64

    @callback_type
    def callback(message: int, details_raw: bytes | None, _custom: int) -> None:
        try:
            raw_text = details_raw.decode("utf-8") if details_raw else "{}"
            details: object = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            details = {"raw": repr(details_raw)}
        message_name = MESSAGE_NAMES.get(message, str(message))
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "message_id": message,
            "message": message_name,
            "details": details,
        }
        with event_lock:
            event_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            event_file.flush()
            event_tail.append(record)
            pending_signals.extend(monitor.observe(message, message_name, details))
        if _should_print(message, details):
            print(json.dumps(callback_summary(message_name, details), ensure_ascii=False), flush=True)

    def capture_cached_image(handle: int) -> tuple[bytes | None, float]:
        started = time.monotonic()
        buffer = ctypes.create_string_buffer(16 * 1024 * 1024)
        size = core.AsstGetImage(handle, buffer, len(buffer))
        duration = time.monotonic() - started
        if size <= 0 or size > len(buffer):
            return None, duration
        payload = bytes(buffer.raw[:size])
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return None, duration
        return payload, duration

    def save_periodic_image(handle: int, filename: str) -> bool:
        nonlocal latest_image
        payload, duration = capture_cached_image(handle)
        if not payload:
            return False
        latest_image = payload
        (run_dir / filename).write_bytes(payload)
        with event_lock:
            pending_signals.extend(monitor.observe_snapshot(payload, duration))
        return True

    def drain_signals(handle: int | None) -> bool:
        nonlocal latest_image
        with event_lock:
            signals = list(pending_signals)
            pending_signals.clear()
            tail = list(event_tail)
        if not signals:
            return False
        incident_image = latest_image
        if handle:
            captured, _duration = capture_cached_image(handle)
            if captured:
                incident_image = captured
                latest_image = captured
        should_stop = False
        for signal in signals:
            incident = record_incident_bundle(
                run_dir,
                signal,
                events_tail=tail,
                screenshot=incident_image,
                environment=environment,
                asst_log=user_dir / "debug" / "asst.log",
            )
            incident_summaries.append(
                {
                    "fingerprint": incident.get("fingerprint"),
                    "kind": incident.get("kind"),
                    "occurrence_count": incident.get("occurrence_count"),
                }
            )
            should_stop = should_stop or signal.stop
        if should_stop and handle and core.AsstRunning(handle):
            core.AsstStop(handle)
        return should_stop

    handle: int | None = None
    timed_out = False
    interrupted = False
    runner_error = ""
    started = time.monotonic()
    try:
        if not core.AsstSetUserDir(_utf8(user_dir)):
            raise RuntimeError("AsstSetUserDir failed")
        if not core.AsstLoadResource(_utf8(runtime_root)):
            raise RuntimeError("AsstLoadResource failed")
        cache_root = runtime_root / "cache"
        if (cache_root / "resource").is_dir():
            if not core.AsstLoadResource(_utf8(cache_root)):
                raise RuntimeError("AsstLoadResource failed for cache")
            print(
                json.dumps({"event": "cache_resource_loaded", "path": os.fspath(cache_root)}, ensure_ascii=False),
                flush=True,
            )
        if guard_overlay_root and not core.AsstLoadResource(_utf8(guard_overlay_root)):
            raise RuntimeError("AsstLoadResource failed for destructive-action guard overlay")

        handle = core.AsstCreateEx(callback, None)
        if not handle:
            raise RuntimeError("AsstCreateEx failed")
        for option, value in (
            (ASST_OPTION_TOUCH_MODE, "adb"),
            (ASST_OPTION_CLIENT_TYPE, args.client_type),
            (ASST_OPTION_VIEWPORT, args.viewport),
        ):
            if not core.AsstSetInstanceOption(handle, option, value.encode("utf-8")):
                raise RuntimeError(f"AsstSetInstanceOption failed for key {option}")
        if not core.AsstConnect(handle, _utf8(adb_path), args.address.encode("utf-8"), b"General"):
            raise RuntimeError("AsstConnect failed")
        if not core.AsstConnected(handle):
            raise RuntimeError("MaaCore did not remain connected")

        startup_id = 0
        if not args.skip_startup:
            startup_id = core.AsstAppendTask(
                handle,
                b"StartUp",
                json.dumps(startup_params, ensure_ascii=False).encode("utf-8"),
            )
        roguelike_id = core.AsstAppendTask(
            handle,
            b"Roguelike",
            json.dumps(roguelike_params, ensure_ascii=False).encode("utf-8"),
        )
        if (not args.skip_startup and startup_id <= 0) or roguelike_id <= 0:
            raise RuntimeError(f"failed to append tasks: StartUp={startup_id}, Roguelike={roguelike_id}")
        print(
            json.dumps(
                {
                    "event": "run_started",
                    "startup_task_id": startup_id,
                    "roguelike_task_id": roguelike_id,
                    "guard_destructive_actions": args.guard_destructive_actions,
                    "stop_after_settlement": stop_after_settlement,
                    "parameters": roguelike_params,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        monitor.reset_clock()
        started = time.monotonic()
        if not core.AsstStart(handle):
            raise RuntimeError("AsstStart failed")

        next_heartbeat = time.monotonic() + args.heartbeat_seconds
        next_snapshot = time.monotonic() + args.snapshot_seconds
        snapshot_index = 0
        while core.AsstRunning(handle):
            now = time.monotonic()
            with event_lock:
                pending_signals.extend(monitor.poll(now=now))
                stop_requested = monitor.stop_requested
                stop_reason = monitor.stop_reason
            if drain_signals(handle):
                break
            if stop_requested:
                filename = "settlement.png" if stop_reason.startswith("natural_settlement") else "terminal.png"
                save_periodic_image(handle, filename)
                core.AsstStop(handle)
                break
            if now - started >= args.max_seconds:
                timed_out = True
                signal = IncidentSignal.create(
                    "timeout",
                    f"run exceeded {args.max_seconds} seconds",
                    {"max_seconds": args.max_seconds},
                    stop=True,
                )
                with event_lock:
                    pending_signals.append(signal)
                drain_signals(handle)
                break
            if now >= next_heartbeat:
                with event_lock:
                    state = monitor.state()
                heartbeat = {
                    "event": "heartbeat",
                    "elapsed_seconds": round(now - started, 1),
                    **state,
                }
                write_json_atomic(run_dir / "status.json", heartbeat)
                print(json.dumps(heartbeat, ensure_ascii=False), flush=True)
                next_heartbeat = now + args.heartbeat_seconds
            if now >= next_snapshot:
                snapshot_index += 1
                save_periodic_image(handle, f"snapshot-{snapshot_index:04d}.png")
                next_snapshot = now + args.snapshot_seconds
            time.sleep(0.05)
        drain_signals(handle)
    except KeyboardInterrupt:
        interrupted = True
        if handle and core.AsstRunning(handle):
            core.AsstStop(handle)
    except Exception as exc:  # The result bundle is more useful than a traceback-only failure.
        runner_error = f"{type(exc).__name__}: {exc}"
        signal = IncidentSignal.create(
            "runner_error",
            runner_error,
            {"exception_type": type(exc).__name__, "message": str(exc)},
            stop=True,
        )
        with event_lock:
            pending_signals.append(signal)
        drain_signals(handle)
        if handle and core.AsstRunning(handle):
            core.AsstStop(handle)
    finally:
        elapsed = time.monotonic() - started
        if handle:
            payload, _duration = capture_cached_image(handle)
            if payload:
                latest_image = payload
                (run_dir / "final.png").write_bytes(payload)
            core.AsstDestroy(handle)
        with event_lock:
            state = monitor.state()
        outcome = classify_exit(
            monitor,
            timed_out=timed_out,
            interrupted=interrupted,
            runner_error=runner_error,
        )
        result = {
            "schema_version": 2,
            **outcome,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "runner_error": runner_error or None,
            "elapsed_seconds": round(elapsed, 1),
            **state,
            "incidents": incident_summaries,
            "finished_at": datetime.now().astimezone().isoformat(),
        }
        write_json_atomic(run_dir / "result.json", result)
        write_json_atomic(run_dir / "status.json", {"event": "finished", **result})
        event_file.close()
        dll_search.close()

    print(json.dumps({"event": "run_finished", **result}, ensure_ascii=False), flush=True)
    if interrupted:
        return 130
    return 0 if result["successful"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
