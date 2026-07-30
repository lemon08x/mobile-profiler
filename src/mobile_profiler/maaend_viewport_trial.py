#!/usr/bin/env python3
"""Run the guarded MaaEnd AndroidOpenGame viewport transition trial."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from .maaend_runtime import (
    MAAEND_MANAGED_INSTANCE_NAME,
    MaaEndRuntimeController,
    configure_maaend_managed_profile,
)


EXPECTED_LOGICAL_SIZE = (1280, 720)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def evaluate_trial_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Evaluate task truth and viewport evidence independently."""

    mxu = snapshot.get("mxu_api")
    api = mxu if isinstance(mxu, dict) else {}
    raw_tasks = api.get("tasks")
    tasks = [row for row in raw_tasks if isinstance(row, dict)] if isinstance(raw_tasks, list) else []
    task_statuses = {
        str(row.get("name") or row.get("id") or ""): str(row.get("status") or "")
        for row in tasks
    }
    android_open_game_succeeded = (
        task_statuses.get("AndroidOpenGame") == "succeeded"
        and len(task_statuses) == 1
    )

    raw_screenshots = api.get("screenshots")
    screenshots = (
        [row for row in raw_screenshots if isinstance(row, dict)]
        if isinstance(raw_screenshots, list)
        else []
    )
    terminal = next(
        (
            row
            for row in reversed(screenshots)
            if str(row.get("label") or "") == "terminal"
        ),
        None,
    )
    terminal_size = (
        [int(terminal.get("width") or 0), int(terminal.get("height") or 0)]
        if terminal is not None
        else [0, 0]
    )
    viewport_verified = tuple(terminal_size) == EXPECTED_LOGICAL_SIZE
    controller_completed = snapshot.get("status") == "completed"
    successful = (
        controller_completed
        and android_open_game_succeeded
        and viewport_verified
    )

    reasons: list[str] = []
    if not controller_completed:
        reasons.append(f"controller status is {snapshot.get('status') or 'unknown'}")
    if not android_open_game_succeeded:
        reasons.append("AndroidOpenGame did not uniquely reach succeeded")
    if not viewport_verified:
        reasons.append(
            "terminal controller screenshot is not 1280x720"
            f" ({terminal_size[0]}x{terminal_size[1]})"
        )
    return {
        "successful": successful,
        "controller_completed": controller_completed,
        "android_open_game_succeeded": android_open_game_succeeded,
        "viewport_verified": viewport_verified,
        "expected_logical_size": list(EXPECTED_LOGICAL_SIZE),
        "terminal_screenshot": terminal,
        "task_statuses": task_statuses,
        "reasons": reasons,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=180.0)
    parser.add_argument(
        "--configure-managed-profile",
        action="store_true",
        help=(
            "Back up mxu-MaaEnd.json and replace only the Mobile Profiler-owned "
            "instance with one AndroidOpenGame task."
        ),
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _parser().parse_args()
    if args.max_seconds <= 0:
        raise ValueError("--max-seconds must be positive")
    runtime_root = args.runtime_root.expanduser().resolve()
    adb = args.adb.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    request = {
        "schema_version": 1,
        "started_at": time.time(),
        "runtime_root": str(runtime_root),
        "adb": str(adb),
        "device": args.device,
        "instance_name": MAAEND_MANAGED_INSTANCE_NAME,
        "task": "AndroidOpenGame",
        "expected_logical_size": list(EXPECTED_LOGICAL_SIZE),
        "max_seconds": args.max_seconds,
        "configure_managed_profile": args.configure_managed_profile,
        "changes_display_resolution": False,
    }
    _write_json(output_root / "request.json", request)

    if args.configure_managed_profile:
        config_path = runtime_root / "config" / "mxu-MaaEnd.json"
        if config_path.is_file():
            backup = output_root / "profile-backup" / "mxu-MaaEnd.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_path, backup)
        configure_maaend_managed_profile(
            runtime_root,
            device=args.device,
            adb=str(adb),
            tasks=[{"name": "AndroidOpenGame", "enabled": True}],
        )

    controller = MaaEndRuntimeController(
        output_root,
        runtime_path=runtime_root,
        instance_name=MAAEND_MANAGED_INSTANCE_NAME,
        adb=str(adb),
    )
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    final_snapshot: dict[str, object] = {}
    caught_error = ""
    timed_out = False
    try:
        payload = {
            "device": args.device,
            "runtime_path": str(runtime_root),
            "instance_name": MAAEND_MANAGED_INSTANCE_NAME,
        }
        controller.preflight(payload)
        controller.start(payload)
        deadline = time.monotonic() + args.max_seconds
        while True:
            final_snapshot = controller.snapshot()
            if final_snapshot.get("running") is not True:
                break
            if stop_requested.wait(0.5):
                caught_error = "trial interrupted"
                controller.stop()
                final_snapshot = controller.snapshot()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                caught_error = f"trial exceeded {args.max_seconds:g} seconds"
                controller.stop()
                final_snapshot = controller.snapshot()
                break
    except Exception as exc:
        caught_error = str(exc)
        try:
            final_snapshot = controller.stop()
        except Exception:
            final_snapshot = controller.snapshot()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        controller.close()

    if not final_snapshot:
        final_snapshot = controller.snapshot()
    evaluation = evaluate_trial_snapshot(final_snapshot)
    result = {
        "schema_version": 1,
        "completed_at": time.time(),
        "successful": evaluation["successful"],
        "timed_out": timed_out,
        "error": caught_error or str(final_snapshot.get("last_error") or ""),
        "evaluation": evaluation,
        "snapshot": final_snapshot,
    }
    _write_json(output_root / "result.json", result)
    console_result = {
        "successful": result["successful"],
        "timed_out": result["timed_out"],
        "status": final_snapshot.get("status"),
        "error": result["error"],
        "evaluation": result["evaluation"],
        "last_run_dir": final_snapshot.get("last_run_dir"),
        "result": str(output_root / "result.json"),
    }
    print(json.dumps(console_result, ensure_ascii=False, indent=2))
    return 0 if evaluation["successful"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
