#!/usr/bin/env python3
"""Run an exact, guarded MaaEnd task set against one ADB device."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from .maaend_guard import load_maaend_guard_policy
from .maaend_runtime import (
    MAAEND_CONFIG_FILENAME,
    MAAEND_MANAGED_INSTANCE_NAME,
    MaaEndRuntimeController,
    configure_maaend_managed_profile,
)


_AUDITABLE_TASK_CONTRACTS = {
    "android_open_game",
    "close_game",
    "pipeline_terminal",
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalized_names(values: Sequence[str], label: str) -> list[str]:
    names = [str(item).strip() for item in values]
    if not names or any(not item for item in names):
        raise ValueError(f"{label} must contain at least one non-empty task name")
    if len(set(names)) != len(names):
        raise ValueError(f"{label} contains duplicate task names")
    return names


def build_task_payload(
    *,
    runtime_root: Path,
    device: str,
    tasks: Sequence[str],
    authorized_tasks: Sequence[str],
    allow_baker_entry: bool = False,
    policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate authorization before the managed profile or device is changed."""

    task_names = _normalized_names(tasks, "tasks")
    authorized = {
        str(item).strip() for item in authorized_tasks if str(item).strip()
    }
    unexpected_authorizations = authorized.difference(task_names)
    if unexpected_authorizations:
        raise ValueError(
            "authorization was supplied for an unselected task: "
            + ", ".join(sorted(unexpected_authorizations))
        )

    guard_policy = dict(policy or load_maaend_guard_policy())
    definitions = {
        str(row.get("name") or ""): row
        for row in guard_policy.get("tasks", [])
        if isinstance(row, Mapping)
    }
    for name in task_names:
        definition = definitions.get(name)
        if definition is None:
            raise ValueError(f"MaaEnd task is not present in the pinned guard policy: {name}")
        if definition.get("adb_supported") is not True:
            raise ValueError(f"MaaEnd task is not allowlisted for ADB: {name}")
        contract = str(definition.get("terminal_contract") or "unsupported")
        if contract not in _AUDITABLE_TASK_CONTRACTS:
            raise ValueError(
                f"MaaEnd task does not yet have an auditable natural terminal: {name}"
            )
        authorization = str(definition.get("authorization") or "unsupported")
        if authorization == "task" and name not in authorized:
            raise ValueError(f"MaaEnd task requires --authorize-task {name}")
        if authorization == "baker_entry" and not (
            name in authorized and allow_baker_entry
        ):
            raise ValueError(
                "BakerEntry requires both --authorize-task BakerEntry and "
                "--allow-baker-entry"
            )
        if authorization == "unsupported":
            raise ValueError(f"MaaEnd task is disabled by the guard policy: {name}")

    payload: dict[str, object] = {
        "device": str(device).strip(),
        "runtime_path": str(runtime_root),
        "instance_name": MAAEND_MANAGED_INSTANCE_NAME,
        "authorized_tasks": sorted(authorized),
        "trial_tasks": task_names,
    }
    if allow_baker_entry:
        payload["allow_baker_entry"] = True
    return payload


def build_task_configurations(
    tasks: Sequence[str],
    options_by_task: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    task_names = _normalized_names(tasks, "tasks")
    raw_options = dict(options_by_task or {})
    unexpected = set(raw_options).difference(task_names)
    if unexpected:
        raise ValueError(
            "options were supplied for an unselected task: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    rows: list[dict[str, object]] = []
    for name in task_names:
        values = raw_options.get(name, {})
        if not isinstance(values, Mapping):
            raise ValueError(f"options for {name} must be a JSON object")
        rows.append(
            {
                "name": name,
                "enabled": True,
                "option_values": dict(values),
            }
        )
    return rows


def evaluate_task_snapshot(
    snapshot: Mapping[str, object],
    expected_tasks: Sequence[str],
) -> dict[str, object]:
    """Require exact MXU truth, per-task terminals, integrity, and guard truth."""

    expected = _normalized_names(expected_tasks, "expected_tasks")
    raw_mxu = snapshot.get("mxu_api")
    mxu = raw_mxu if isinstance(raw_mxu, Mapping) else {}
    raw_tasks = mxu.get("tasks")
    mxu_tasks = (
        [row for row in raw_tasks if isinstance(row, Mapping)]
        if isinstance(raw_tasks, list)
        else []
    )
    observed_names = [
        str(row.get("name") or row.get("id") or "") for row in mxu_tasks
    ]
    exact_task_set = observed_names == expected
    all_mxu_succeeded = exact_task_set and all(
        str(row.get("status") or "") == "succeeded" for row in mxu_tasks
    )

    raw_contract = snapshot.get("terminal_contract")
    contract = raw_contract if isinstance(raw_contract, Mapping) else {}
    raw_contract_rows = contract.get("tasks")
    contract_rows = (
        [row for row in raw_contract_rows if isinstance(row, Mapping)]
        if isinstance(raw_contract_rows, list)
        else []
    )
    contract_names = [str(row.get("name") or "") for row in contract_rows]
    exact_contract_set = contract_names == expected
    all_contracts_passed = (
        contract.get("verified") is True
        and exact_contract_set
        and all(row.get("passed") is True for row in contract_rows)
    )

    raw_integrity = snapshot.get("runtime_integrity")
    integrity = raw_integrity if isinstance(raw_integrity, Mapping) else {}
    raw_guard = snapshot.get("guard")
    guard = raw_guard if isinstance(raw_guard, Mapping) else {}
    raw_decision = guard.get("last_decision")
    decision = raw_decision if isinstance(raw_decision, Mapping) else {}
    raw_preflight = snapshot.get("preflight")
    preflight = raw_preflight if isinstance(raw_preflight, Mapping) else {}
    raw_preflight_guard = preflight.get("guard")
    preflight_guard = (
        raw_preflight_guard if isinstance(raw_preflight_guard, Mapping) else {}
    )
    guard_allowed = (
        decision.get("allowed") is True or preflight_guard.get("allowed") is True
    )

    checks = {
        "controller_completed": snapshot.get("status") == "completed",
        "exact_mxu_task_set": exact_task_set,
        "all_mxu_tasks_succeeded": all_mxu_succeeded,
        "exact_terminal_contract_set": exact_contract_set,
        "all_terminal_contracts_passed": all_contracts_passed,
        "runtime_integrity_verified": integrity.get("verified") is True,
        "guard_allowed": guard_allowed,
        "guarded_flow_verified": snapshot.get("guarded_flow_verified") is True,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "successful": all(checks.values()),
        "checks": checks,
        "expected_tasks": expected,
        "observed_mxu_tasks": [
            {
                "name": str(row.get("name") or row.get("id") or ""),
                "status": str(row.get("status") or ""),
            }
            for row in mxu_tasks
        ],
        "terminal_contracts": [dict(row) for row in contract_rows],
        "reasons": reasons,
    }


def _load_options_file(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("--options-file must contain a JSON object keyed by task name")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--task", action="append", required=True)
    parser.add_argument("--authorize-task", action="append", default=[])
    parser.add_argument("--allow-baker-entry", action="store_true")
    parser.add_argument("--options-file", type=Path)
    parser.add_argument("--max-seconds", type=float, default=900.0)
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
    options_by_task = _load_options_file(
        args.options_file.expanduser().resolve() if args.options_file else None
    )
    task_rows = build_task_configurations(args.task, options_by_task)
    payload = build_task_payload(
        runtime_root=runtime_root,
        device=args.device,
        tasks=args.task,
        authorized_tasks=args.authorize_task,
        allow_baker_entry=args.allow_baker_entry,
    )

    request = {
        "schema_version": 1,
        "started_at": time.time(),
        "runtime_root": str(runtime_root),
        "adb": str(adb),
        "device": args.device,
        "instance_name": MAAEND_MANAGED_INSTANCE_NAME,
        "tasks": task_rows,
        "authorized_tasks": sorted(set(args.authorize_task)),
        "allow_baker_entry": args.allow_baker_entry,
        "max_seconds": args.max_seconds,
        "changes_display_resolution": False,
        "installs_device_client": False,
    }
    _write_json(output_root / "request.json", request)

    config_path = runtime_root / "config" / MAAEND_CONFIG_FILENAME
    if config_path.is_file():
        backup = output_root / "profile-backup" / MAAEND_CONFIG_FILENAME
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path, backup)
    configured = configure_maaend_managed_profile(
        runtime_root,
        device=args.device,
        adb=str(adb),
        tasks=task_rows,
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
        preflight = controller.preflight(payload)
        preflight_profile = preflight.get("preflight")
        profile = (
            preflight_profile.get("profile", {})
            if isinstance(preflight_profile, Mapping)
            else {}
        )
        if not isinstance(profile, Mapping) or profile.get("task_names") != args.task:
            raise RuntimeError("managed MaaEnd profile did not retain the exact trial task set")
        controller.start(payload)
        deadline = time.monotonic() + args.max_seconds
        while True:
            final_snapshot = controller.snapshot()
            if final_snapshot.get("running") is not True:
                break
            if stop_requested.wait(0.5):
                caught_error = "task trial interrupted"
                controller.stop()
                final_snapshot = controller.snapshot()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                caught_error = f"task trial exceeded {args.max_seconds:g} seconds"
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
    evaluation = evaluate_task_snapshot(final_snapshot, args.task)
    result = {
        "schema_version": 1,
        "completed_at": time.time(),
        "successful": evaluation["successful"],
        "timed_out": timed_out,
        "error": caught_error or str(final_snapshot.get("last_error") or ""),
        "configured_profile": configured,
        "evaluation": evaluation,
        "snapshot": final_snapshot,
    }
    _write_json(output_root / "result.json", result)
    console_result = {
        "successful": result["successful"],
        "timed_out": timed_out,
        "status": final_snapshot.get("status"),
        "error": result["error"],
        "evaluation": evaluation,
        "last_run_dir": final_snapshot.get("last_run_dir"),
        "result": str(output_root / "result.json"),
    }
    print(json.dumps(console_result, ensure_ascii=False, indent=2))
    return 0 if evaluation["successful"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
