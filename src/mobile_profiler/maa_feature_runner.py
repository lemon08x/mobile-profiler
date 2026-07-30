#!/usr/bin/env python3
"""Run one allowlisted MAA feature and retain callbacks, screenshots and status."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path


from mobile_profiler.maa_iteration import (
    callback_summary,
    prepare_maa_feature_probe,
    sha256_file,
    summarize_feature_extra,
    write_json_atomic,
)


ASST_OPTION_TOUCH_MODE = 2
ASST_OPTION_CLIENT_TYPE = 6
ASST_OPTION_VIEWPORT = 7

MESSAGE_NAMES = {
    0: "InternalError",
    1: "InitFailed",
    2: "ConnectionInfo",
    3: "AllTasksCompleted",
    10000: "TaskChainError",
    10001: "TaskChainStart",
    10002: "TaskChainCompleted",
    10003: "TaskChainExtraInfo",
    10004: "TaskChainStopped",
    20000: "SubTaskError",
    20001: "SubTaskStart",
    20002: "SubTaskCompleted",
    20003: "SubTaskExtraInfo",
}


def _utf8(value: str | Path) -> bytes:
    return os.fspath(value).encode("utf-8")


def _parse_json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("params must be a JSON object")
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--params", type=_parse_json_object, default={})
    parser.add_argument("--viewport", default="adaptive")
    parser.add_argument("--config", default="General")
    parser.add_argument("--max-seconds", type=float, default=10 * 60)
    parser.add_argument("--allow-account-mutation", action="store_true")
    return parser.parse_args()


def _capture(core: ctypes.WinDLL, handle: int) -> bytes | None:
    async_id = core.AsstAsyncScreencap(handle, 1)
    if async_id <= 0:
        return None
    buffer = ctypes.create_string_buffer(16 * 1024 * 1024)
    size = core.AsstGetImage(handle, buffer, len(buffer))
    if size <= 0 or size > len(buffer):
        return None
    payload = bytes(buffer.raw[:size])
    return payload if payload.startswith(b"\x89PNG\r\n\x1a\n") else None


def main() -> int:
    args = _parse_args()
    core_root = args.core_root.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    adb_path = args.adb.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    core_path = core_root / "MaaCore.dll"

    if not core_path.is_file():
        raise RuntimeError(f"MaaCore.dll not found: {core_path}")
    if not (runtime_root / "resource").is_dir():
        raise RuntimeError(f"resource directory not found: {runtime_root / 'resource'}")
    if not adb_path.is_file():
        raise RuntimeError(f"adb executable not found: {adb_path}")
    if args.max_seconds <= 0:
        raise RuntimeError("max-seconds must be positive")

    request = prepare_maa_feature_probe(
        args.task,
        args.params,
        allow_account_mutation=args.allow_account_mutation,
    )
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    user_dir = run_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    write_json_atomic(
        run_dir / "request.json",
        {
            "schema_version": 1,
            **request,
            "viewport": args.viewport,
            "address": args.address,
            "max_seconds": args.max_seconds,
            "started_at": datetime.now().astimezone().isoformat(),
        },
    )
    write_json_atomic(
        run_dir / "environment.json",
        {
            "schema_version": 1,
            "core": {"path": os.fspath(core_path), "sha256": sha256_file(core_path)},
            "runtime": {"path": os.fspath(runtime_root)},
            "adb": {"path": os.fspath(adb_path), "sha256": sha256_file(adb_path)},
            "device": {"address": args.address},
        },
    )

    event_lock = threading.Lock()
    event_file = (run_dir / "events.jsonl").open("a", encoding="utf-8")
    chain_completed = threading.Event()
    chain_error = threading.Event()
    stopped = threading.Event()
    extras: list[dict[str, object]] = []

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
    core.AsstAsyncScreencap.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    core.AsstAsyncScreencap.restype = ctypes.c_int32
    core.AsstGetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64]
    core.AsstGetImage.restype = ctypes.c_uint64

    @callback_type
    def callback(message: int, details_raw: bytes | None, _custom: int) -> None:
        try:
            details: object = json.loads(details_raw.decode("utf-8")) if details_raw else {}
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
            if message == 20003:
                summary = summarize_feature_extra(details)
                if summary:
                    extras.append(summary)
        taskchain = details.get("taskchain") if isinstance(details, dict) else None
        if taskchain == args.task:
            if message == 10002:
                chain_completed.set()
            elif message in (10000, 20000):
                chain_error.set()
            elif message == 10004:
                stopped.set()
        if message in (10000, 10001, 10002, 10004, 20000, 20003):
            print(json.dumps(callback_summary(message_name, details), ensure_ascii=False), flush=True)

    handle: int | None = None
    task_id = 0
    timed_out = False
    runner_error = ""
    started = time.monotonic()
    try:
        if not core.AsstSetUserDir(_utf8(user_dir)):
            raise RuntimeError("AsstSetUserDir failed")
        if not core.AsstLoadResource(_utf8(runtime_root)):
            raise RuntimeError("AsstLoadResource failed")
        cache_root = runtime_root / "cache"
        if (cache_root / "resource").is_dir() and not core.AsstLoadResource(_utf8(cache_root)):
            raise RuntimeError("AsstLoadResource failed for cache")

        handle = core.AsstCreateEx(callback, None)
        if not handle:
            raise RuntimeError("AsstCreateEx failed")
        for option, value in (
            (ASST_OPTION_TOUCH_MODE, "adb"),
            (ASST_OPTION_CLIENT_TYPE, "Official"),
            (ASST_OPTION_VIEWPORT, args.viewport),
        ):
            if not core.AsstSetInstanceOption(handle, option, value.encode("utf-8")):
                raise RuntimeError(f"AsstSetInstanceOption failed for key {option}")
        if not core.AsstConnect(
            handle,
            _utf8(adb_path),
            args.address.encode("utf-8"),
            args.config.encode("utf-8"),
        ):
            raise RuntimeError("AsstConnect failed")
        if not core.AsstConnected(handle):
            raise RuntimeError("MaaCore did not remain connected")

        initial = _capture(core, handle)
        if initial:
            (run_dir / "initial.png").write_bytes(initial)
        task_id = core.AsstAppendTask(
            handle,
            args.task.encode("utf-8"),
            json.dumps(request["params"], ensure_ascii=False).encode("utf-8"),
        )
        if task_id <= 0:
            raise RuntimeError(f"AsstAppendTask failed for {args.task}")
        if not core.AsstStart(handle):
            raise RuntimeError("AsstStart failed")
        started = time.monotonic()
        while core.AsstRunning(handle):
            if time.monotonic() - started >= args.max_seconds:
                timed_out = True
                core.AsstStop(handle)
                break
            time.sleep(0.05)
    except Exception as exc:
        runner_error = f"{type(exc).__name__}: {exc}"
        if handle and core.AsstRunning(handle):
            core.AsstStop(handle)
    finally:
        elapsed = time.monotonic() - started
        if handle:
            final = _capture(core, handle)
            if final:
                (run_dir / "final.png").write_bytes(final)
            core.AsstDestroy(handle)
        event_file.close()
        dll_search.close()

    successful = (
        chain_completed.is_set()
        and not chain_error.is_set()
        and not timed_out
        and not runner_error
    )
    result = {
        "schema_version": 1,
        "task": args.task,
        "task_id": task_id,
        "successful": successful,
        "chain_completed": chain_completed.is_set(),
        "chain_error": chain_error.is_set(),
        "chain_stopped": stopped.is_set(),
        "timed_out": timed_out,
        "runner_error": runner_error or None,
        "elapsed_seconds": round(elapsed, 1),
        "extra_info": extras,
        "finished_at": datetime.now().astimezone().isoformat(),
    }
    write_json_atomic(run_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if successful else 2


if __name__ == "__main__":
    raise SystemExit(main())
