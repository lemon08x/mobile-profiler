#!/usr/bin/env python3
"""Run one guarded MaaEnd viewport probe against an ADB device."""

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
    MAAEND_CONFIG_FILENAME,
    MAAEND_MANAGED_INSTANCE_NAME,
    MaaEndRuntimeController,
    configure_maaend_managed_profile,
)


PROBES = ("SceneProbe", "CaptureUidProbe", "ViewportInputProbe")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_probe_payload(
    *,
    runtime_root: Path,
    device: str,
    probe: str,
    authorize_input: bool,
) -> dict[str, object]:
    if probe not in PROBES:
        raise ValueError(f"unsupported MaaEnd probe: {probe}")
    if probe == "ViewportInputProbe" and not authorize_input:
        raise ValueError(
            "ViewportInputProbe requires the explicit --authorize-input flag"
        )
    payload: dict[str, object] = {
        "device": device,
        "runtime_path": str(runtime_root),
        "instance_name": MAAEND_MANAGED_INSTANCE_NAME,
        "internal_probe": probe,
    }
    if probe == "ViewportInputProbe":
        payload.update(
            {
                "authorized_probes": [probe],
                "allow_viewport_input_probe": True,
            }
        )
    return payload


def evaluate_probe_snapshot(
    snapshot: dict[str, object],
    probe: str,
) -> dict[str, object]:
    """Require MXU truth, the business terminal contract, and integrity."""

    raw_mxu = snapshot.get("mxu_api")
    mxu = raw_mxu if isinstance(raw_mxu, dict) else {}
    raw_tasks = mxu.get("tasks")
    tasks = (
        [row for row in raw_tasks if isinstance(row, dict)]
        if isinstance(raw_tasks, list)
        else []
    )
    task_statuses = {
        str(row.get("name") or row.get("id") or ""): str(
            row.get("status") or ""
        )
        for row in tasks
    }
    unique_probe_succeeded = (
        task_statuses == {probe: "succeeded"}
    )

    raw_contract = snapshot.get("terminal_contract")
    contract = raw_contract if isinstance(raw_contract, dict) else {}
    contract_rows = contract.get("tasks")
    matching_contracts = [
        row
        for row in contract_rows
        if isinstance(row, dict) and str(row.get("name") or "") == probe
    ] if isinstance(contract_rows, list) else []
    terminal_contract_verified = (
        contract.get("verified") is True
        and len(matching_contracts) == 1
        and matching_contracts[0].get("passed") is True
    )

    raw_integrity = snapshot.get("runtime_integrity")
    integrity = raw_integrity if isinstance(raw_integrity, dict) else {}
    runtime_integrity_verified = integrity.get("verified") is True
    controller_completed = snapshot.get("status") == "completed"
    successful = all(
        (
            controller_completed,
            unique_probe_succeeded,
            terminal_contract_verified,
            runtime_integrity_verified,
        )
    )
    reasons: list[str] = []
    if not controller_completed:
        reasons.append(
            f"controller status is {snapshot.get('status') or 'unknown'}"
        )
    if not unique_probe_succeeded:
        reasons.append(f"{probe} did not uniquely reach succeeded")
    if not terminal_contract_verified:
        reasons.append(f"{probe} terminal contract was not verified")
    if not runtime_integrity_verified:
        reasons.append("runtime integrity was not verified")
    return {
        "successful": successful,
        "controller_completed": controller_completed,
        "unique_probe_succeeded": unique_probe_succeeded,
        "terminal_contract_verified": terminal_contract_verified,
        "runtime_integrity_verified": runtime_integrity_verified,
        "task_statuses": task_statuses,
        "terminal_contract": matching_contracts[0] if matching_contracts else {},
        "reasons": reasons,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--probe", choices=PROBES, required=True)
    parser.add_argument("--max-seconds", type=float, default=180.0)
    parser.add_argument(
        "--configure-managed-profile",
        action="store_true",
        help=(
            "Back up mxu-MaaEnd.json and replace only the Mobile "
            "Profiler-owned instance with a probe-only empty task list."
        ),
    )
    parser.add_argument(
        "--authorize-input",
        action="store_true",
        help=(
            "Explicitly authorize the reversible touch sequence used only by "
            "ViewportInputProbe."
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
    payload = build_probe_payload(
        runtime_root=runtime_root,
        device=args.device,
        probe=args.probe,
        authorize_input=args.authorize_input,
    )
    request = {
        "schema_version": 1,
        "started_at": time.time(),
        "runtime_root": str(runtime_root),
        "adb": str(adb),
        "device": args.device,
        "instance_name": MAAEND_MANAGED_INSTANCE_NAME,
        "probe": args.probe,
        "max_seconds": args.max_seconds,
        "configure_managed_profile": args.configure_managed_profile,
        "coordinate_input_authorized": args.authorize_input,
        "changes_display_resolution": False,
    }
    _write_json(output_root / "request.json", request)

    if args.configure_managed_profile:
        config_path = runtime_root / "config" / MAAEND_CONFIG_FILENAME
        if config_path.is_file():
            backup = output_root / "profile-backup" / MAAEND_CONFIG_FILENAME
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_path, backup)
        configure_maaend_managed_profile(
            runtime_root,
            device=args.device,
            adb=str(adb),
            tasks=[],
            allow_empty_tasks=True,
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
        controller.preflight(payload)
        controller.start(payload)
        deadline = time.monotonic() + args.max_seconds
        while True:
            final_snapshot = controller.snapshot()
            if final_snapshot.get("running") is not True:
                break
            if stop_requested.wait(0.5):
                caught_error = "probe trial interrupted"
                controller.stop()
                final_snapshot = controller.snapshot()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                caught_error = (
                    f"probe trial exceeded {args.max_seconds:g} seconds"
                )
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
    evaluation = evaluate_probe_snapshot(final_snapshot, args.probe)
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
