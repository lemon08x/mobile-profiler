"""Fail-closed policy and terminal contracts for the MaaEnd MXU adapter."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence


MAAEND_GUARD_SCHEMA_VERSION = 1
MAAEND_RESOURCE_FINGERPRINT_ALGORITHM = "sha256-tree-v1"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_VALID_AUTHORIZATIONS = {"none", "task", "baker_entry", "unsupported"}
_VALID_TERMINAL_CONTRACTS = {
    "android_open_game",
    "close_game",
    "pipeline_entry",
    "pipeline_terminal",
    "standard_pipeline",
    "unsupported",
}
_VALID_PROBE_AUTHORIZATIONS = {"none", "viewport_input_probe"}
_VALID_PROBE_TERMINAL_CONTRACTS = {
    "scene_identified",
    "uid_hash_captured",
    "input_reversed",
}
_EXPECTED_INTERNAL_PROBES = {
    "SceneProbe",
    "CaptureUidProbe",
    "ViewportInputProbe",
}
_COORDINATE_ACTIONS = {
    "click",
    "long_press",
    "swipe",
    "multi_swipe",
    "touch_down",
    "touch_move",
    "touch_up",
    "scroll",
}


class MaaEndGuardError(RuntimeError):
    """A policy decision rejected a profile before it reached MXU."""

    def __init__(self, code: str, message: str, evidence: Mapping[str, object]) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def default_maaend_guard_policy_path() -> Path:
    package_path = Path(__file__).with_name("maaend_guard_policy.json")
    repository_path = (
        Path(__file__).resolve().parents[2]
        / "integrations"
        / "maaend"
        / "guard-policy.json"
    )
    return repository_path if repository_path.is_file() else package_path


def validate_maaend_guard_policy(policy: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if policy.get("schema_version") != MAAEND_GUARD_SCHEMA_VERSION:
        errors.append(
            "guard policy schema_version must be "
            f"{MAAEND_GUARD_SCHEMA_VERSION}"
        )

    project = policy.get("project")
    if not isinstance(project, Mapping):
        errors.append("guard policy project must be an object")
        expected_task_count = 0
        expected_adb_task_count = 0
    else:
        if project.get("name") != "MaaEnd":
            errors.append("guard policy project.name must be MaaEnd")
        if project.get("version") != "v2.20.0":
            errors.append("guard policy project.version must be v2.20.0")
        expected_task_count = project.get("task_count", 0)
        expected_adb_task_count = project.get("adb_task_count", 0)
        if not isinstance(expected_task_count, int) or isinstance(
            expected_task_count, bool
        ) or expected_task_count <= 0:
            errors.append("guard policy project.task_count must be a positive integer")
            expected_task_count = 0
        if not isinstance(expected_adb_task_count, int) or isinstance(
            expected_adb_task_count, bool
        ) or expected_adb_task_count <= 0:
            errors.append(
                "guard policy project.adb_task_count must be a positive integer"
            )
            expected_adb_task_count = 0

    fingerprint = policy.get("resource_fingerprint")
    if not isinstance(fingerprint, Mapping):
        errors.append("guard policy resource_fingerprint must be an object")
    else:
        if fingerprint.get("algorithm") != MAAEND_RESOURCE_FINGERPRINT_ALGORITHM:
            errors.append(
                "guard policy resource_fingerprint.algorithm must be "
                f"{MAAEND_RESOURCE_FINGERPRINT_ALGORITHM}"
            )
        paths = fingerprint.get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(item, str) and item.strip() for item in paths
        ):
            errors.append(
                "guard policy resource_fingerprint.paths must be a non-empty string list"
            )
        expected = str(fingerprint.get("expected_sha256") or "").lower()
        if not _SHA256_PATTERN.fullmatch(expected):
            errors.append(
                "guard policy resource_fingerprint.expected_sha256 must be SHA-256"
            )

    tasks = policy.get("tasks")
    if not isinstance(tasks, list):
        errors.append("guard policy tasks must be a list")
        tasks = []
    names: set[str] = set()
    adb_count = 0
    for index, raw in enumerate(tasks):
        if not isinstance(raw, Mapping):
            errors.append(f"tasks[{index}] must be an object")
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            errors.append(f"tasks[{index}].name is required")
        elif name in names:
            errors.append(f"duplicate MaaEnd guard task: {name}")
        else:
            names.add(name)
        adb_supported = raw.get("adb_supported")
        if not isinstance(adb_supported, bool):
            errors.append(f"tasks[{index}].adb_supported must be boolean")
        elif adb_supported:
            adb_count += 1
        authorization = raw.get("authorization")
        if authorization not in _VALID_AUTHORIZATIONS:
            errors.append(f"tasks[{index}].authorization is invalid")
        if adb_supported is False and authorization != "unsupported":
            errors.append(
                f"tasks[{index}] must use unsupported authorization without ADB"
            )
        terminal = raw.get("terminal_contract")
        if terminal not in _VALID_TERMINAL_CONTRACTS:
            errors.append(f"tasks[{index}].terminal_contract is invalid")
        terminal_nodes = raw.get("terminal_nodes")
        if terminal == "pipeline_terminal":
            if not isinstance(terminal_nodes, list) or not terminal_nodes or not all(
                isinstance(item, str) and item.strip() for item in terminal_nodes
            ):
                errors.append(
                    f"tasks[{index}].terminal_nodes must be a non-empty string list"
                )
            elif len(set(terminal_nodes)) != len(terminal_nodes):
                errors.append(f"tasks[{index}].terminal_nodes contains duplicates")
        elif terminal_nodes is not None:
            errors.append(
                f"tasks[{index}].terminal_nodes requires pipeline_terminal contract"
            )
        entry = raw.get("entry")
        if adb_supported is True and terminal != "unsupported" and not (
            isinstance(entry, str) and entry.strip()
        ):
            errors.append(f"tasks[{index}].entry is required for an ADB contract")
        wave = raw.get("wave")
        if not (
            isinstance(wave, int)
            and not isinstance(wave, bool)
            and 0 <= wave <= 5
        ) and wave not in {"independent_high_risk", "unsupported"}:
            errors.append(f"tasks[{index}].wave is invalid")

    if expected_task_count and len(tasks) != expected_task_count:
        errors.append(
            f"guard policy task count is {len(tasks)}, expected {expected_task_count}"
        )
    if expected_adb_task_count and adb_count != expected_adb_task_count:
        errors.append(
            f"guard policy ADB task count is {adb_count}, "
            f"expected {expected_adb_task_count}"
        )

    probes = policy.get("internal_probes")
    if not isinstance(probes, list) or not probes:
        errors.append("guard policy internal_probes must be a non-empty list")
    else:
        probe_names: set[str] = set()
        selected_task_ids: set[str] = set()
        for index, raw in enumerate(probes):
            if not isinstance(raw, Mapping):
                errors.append(f"internal_probes[{index}] must be an object")
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                errors.append(f"internal_probes[{index}].name is required")
            elif name in probe_names:
                errors.append(f"duplicate internal probe: {name}")
            probe_names.add(name)
            entry = raw.get("entry")
            if not isinstance(entry, str) or not entry.strip():
                errors.append(f"internal_probes[{index}].entry is required")
            selected_task_id = raw.get("selected_task_id")
            if not isinstance(selected_task_id, str) or not selected_task_id.strip():
                errors.append(
                    f"internal_probes[{index}].selected_task_id is required"
                )
            elif selected_task_id in selected_task_ids:
                errors.append(f"duplicate internal probe selected_task_id: {selected_task_id}")
            else:
                selected_task_ids.add(selected_task_id)
            authorization = raw.get("authorization")
            if authorization not in _VALID_PROBE_AUTHORIZATIONS:
                errors.append(f"internal_probes[{index}].authorization is invalid")
            terminal = raw.get("terminal_contract")
            if terminal not in _VALID_PROBE_TERMINAL_CONTRACTS:
                errors.append(
                    f"internal_probes[{index}].terminal_contract is invalid"
                )
            if not isinstance(raw.get("input_allowed"), bool):
                errors.append(f"internal_probes[{index}].input_allowed must be boolean")
            pipeline_override = raw.get("pipeline_override")
            if not (
                isinstance(pipeline_override, list)
                and pipeline_override
                and all(isinstance(item, Mapping) and item for item in pipeline_override)
            ):
                errors.append(
                    f"internal_probes[{index}].pipeline_override must be a non-empty object list"
                )
        if probe_names != _EXPECTED_INTERNAL_PROBES:
            errors.append(
                "guard policy internal_probes must contain exactly "
                + ", ".join(sorted(_EXPECTED_INTERNAL_PROBES))
            )

    watchdogs = policy.get("watchdogs")
    if not isinstance(watchdogs, Mapping):
        errors.append("guard policy watchdogs must be an object")
    else:
        for key in (
            "poll_interval_seconds",
            "no_progress_seconds",
            "same_node_start_limit",
            "same_node_loop_seconds",
            "screenshot_interval_seconds",
            "static_snapshot_limit",
            "slow_screenshot_seconds",
            "slow_screenshot_consecutive_limit",
            "screenshot_backend_switch_limit",
            "tasker_idle_grace_seconds",
            "active_touch_grace_seconds",
            "api_error_limit",
            "runtime_integrity_interval_seconds",
        ):
            value = watchdogs.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                errors.append(f"watchdogs.{key} must be a non-negative number")
    return errors


def load_maaend_guard_policy(path: Path | None = None) -> dict[str, object]:
    selected = (path or default_maaend_guard_policy_path()).resolve()
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load MaaEnd guard policy {selected}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"MaaEnd guard policy must be an object: {selected}")
    errors = validate_maaend_guard_policy(value)
    if errors:
        raise ValueError("invalid MaaEnd guard policy: " + "; ".join(errors))
    return value


def _safe_policy_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"resource fingerprint path escapes MaaEnd root: {relative}") from exc
    return path


def fingerprint_maaend_resources(
    root: Path,
    policy: Mapping[str, object],
    *,
    file_hash_cache: dict[str, tuple[int, int, str]] | None = None,
) -> dict[str, object]:
    """Hash the fixed interface, task definitions, resources, and ADB overlay."""

    resolved_root = root.resolve()
    definition = policy.get("resource_fingerprint")
    if not isinstance(definition, Mapping):
        raise ValueError("MaaEnd guard policy has no resource_fingerprint")
    raw_paths = definition.get("paths")
    if not isinstance(raw_paths, list):
        raise ValueError("MaaEnd guard resource paths are invalid")

    candidates: dict[str, Path] = {}
    for raw in raw_paths:
        relative = str(raw).strip().replace("\\", "/")
        selected = _safe_policy_path(resolved_root, relative)
        if not selected.exists():
            raise ValueError(f"MaaEnd guarded resource path is missing: {relative}")
        if selected.is_symlink():
            raise ValueError(f"MaaEnd guarded resource path may not be a symlink: {relative}")
        paths = [selected] if selected.is_file() else sorted(selected.rglob("*"))
        for path in paths:
            if path.is_symlink():
                raise ValueError(
                    "MaaEnd guarded resource tree may not contain symlinks: "
                    f"{path.relative_to(resolved_root).as_posix()}"
                )
            if not path.is_file():
                continue
            relative_path = path.relative_to(resolved_root).as_posix()
            candidates[relative_path] = path

    manifest_hasher = hashlib.sha256()
    total_bytes = 0
    for relative_path in sorted(candidates):
        path = candidates[relative_path]
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError(f"cannot stat MaaEnd resource {relative_path}: {exc}") from exc
        cache_key = f"{resolved_root}|{relative_path}"
        cached = (file_hash_cache or {}).get(cache_key)
        file_sha256 = ""
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            file_sha256 = cached[2]
            total_bytes += stat.st_size
        else:
            file_hasher = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        file_hasher.update(chunk)
                        total_bytes += len(chunk)
            except OSError as exc:
                raise ValueError(f"cannot hash MaaEnd resource {relative_path}: {exc}") from exc
            file_sha256 = file_hasher.hexdigest()
            if file_hash_cache is not None:
                file_hash_cache[cache_key] = (
                    stat.st_size,
                    stat.st_mtime_ns,
                    file_sha256,
                )
        row = {
            "path": relative_path,
            "sha256": file_sha256,
            "size": stat.st_size,
        }
        manifest_hasher.update(_canonical_json(row))
        manifest_hasher.update(b"\n")

    return {
        "algorithm": MAAEND_RESOURCE_FINGERPRINT_ALGORITHM,
        "sha256": manifest_hasher.hexdigest(),
        "file_count": len(candidates),
        "total_bytes": total_bytes,
        "paths": [str(item) for item in raw_paths],
    }


def _authorized_task_names(payload: Mapping[str, object]) -> set[str]:
    raw = payload.get("authorized_tasks")
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _authorized_probe_names(payload: Mapping[str, object]) -> set[str]:
    raw = payload.get("authorized_probes")
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def requested_maaend_internal_probe(payload: Mapping[str, object]) -> str:
    if "internal_probe" not in payload:
        return ""
    raw = payload.get("internal_probe")
    if not isinstance(raw, str) or not raw.strip():
        raise MaaEndGuardError(
            "probe_denied",
            "MaaEnd internal_probe 必须是非空字符串",
            {"allowed": False, "reason": "invalid_internal_probe"},
        )
    return raw.strip()


def _internal_probe_definitions(
    policy: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    raw = policy.get("internal_probes")
    rows = raw if isinstance(raw, list) else []
    return {
        str(row.get("name") or "").strip(): dict(row)
        for row in rows
        if isinstance(row, Mapping) and str(row.get("name") or "").strip()
    }


def build_maaend_probe_request(
    policy: Mapping[str, object],
    probe_name: str,
) -> dict[str, object]:
    """Build the only MXU request accepted for a checked-in internal probe."""

    definition = _internal_probe_definitions(policy).get(probe_name)
    if definition is None:
        raise MaaEndGuardError(
            "probe_denied",
            f"未知 MaaEnd 内部探针: {probe_name or '<empty>'}",
            {
                "allowed": False,
                "reason": "unknown_internal_probe",
                "internal_probe": probe_name,
            },
        )
    pipeline_override = definition.get("pipeline_override")
    assert isinstance(pipeline_override, list)
    return {
        "entry": str(definition.get("entry") or ""),
        "pipeline_override": json.dumps(
            pipeline_override,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "selected_task_id": str(definition.get("selected_task_id") or ""),
    }


def _enabled_task_configurations(profile: Mapping[str, object]) -> list[dict[str, object]]:
    raw_rows = profile.get("task_configurations")
    if not isinstance(raw_rows, list):
        return []
    return [
        dict(row)
        for row in raw_rows
        if isinstance(row, Mapping) and row.get("enabled") is True
    ]


def evaluate_maaend_guard(
    policy: Mapping[str, object],
    profile: Mapping[str, object],
    payload: Mapping[str, object],
    *,
    resource_fingerprint: Mapping[str, object],
    task_requests: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Validate tasks, authorizations, options, resources, and PI overrides."""

    errors = validate_maaend_guard_policy(policy)
    if errors:
        raise MaaEndGuardError(
            "invalid_policy",
            "MaaEnd 风险策略无效: " + "; ".join(errors),
            {"allowed": False, "errors": errors},
        )
    definition = policy["resource_fingerprint"]
    assert isinstance(definition, Mapping)
    expected_resource = str(definition.get("expected_sha256") or "").lower()
    observed_resource = str(resource_fingerprint.get("sha256") or "").lower()
    if observed_resource != expected_resource:
        evidence = {
            "allowed": False,
            "code": "resource_drift",
            "resource_fingerprint": dict(resource_fingerprint),
            "expected_resource_sha256": expected_resource,
        }
        raise MaaEndGuardError(
            "resource_drift",
            "MaaEnd 上游资源指纹与 v2.20.0 风险策略不一致，已拒绝执行",
            evidence,
        )

    probe_name = requested_maaend_internal_probe(payload)
    if probe_name:
        definitions = _internal_probe_definitions(policy)
        definition = definitions.get(probe_name)
        expected_request = build_maaend_probe_request(policy, probe_name)
        configurations = _enabled_task_configurations(profile)
        request_rows = [dict(row) for row in task_requests]
        if configurations or request_rows != [expected_request]:
            evidence = {
                "allowed": False,
                "code": "request_mismatch",
                "mode": "internal_probe",
                "internal_probe": probe_name,
                "enabled_profile_tasks": [
                    str(row.get("name") or "") for row in configurations
                ],
                "expected_request_sha256": canonical_sha256(expected_request),
                "observed_request_sha256": canonical_sha256(request_rows),
            }
            raise MaaEndGuardError(
                "request_mismatch",
                "MaaEnd 内部探针必须使用空任务 Profile 和固定请求",
                evidence,
            )
        assert definition is not None
        authorized_probes = _authorized_probe_names(payload)
        authorization = str(definition.get("authorization") or "none")
        reason = ""
        if authorization == "viewport_input_probe" and probe_name not in authorized_probes:
            reason = "explicit_probe_authorization_required"
        elif authorization == "viewport_input_probe" and payload.get(
            "allow_viewport_input_probe"
        ) is not True:
            reason = "viewport_input_probe_flag_required"
        evaluation = {
            "name": probe_name,
            "entry": expected_request["entry"],
            "selected_task_id": expected_request["selected_task_id"],
            "wave": definition.get("wave"),
            "risk": definition.get("risk"),
            "authorization": authorization,
            "input_allowed": definition.get("input_allowed"),
            "terminal_contract": definition.get("terminal_contract"),
            "request_sha256": canonical_sha256(expected_request),
            "allowed": not reason,
        }
        if reason:
            evaluation["reason"] = reason
        evidence = {
            "allowed": not reason,
            "mode": "internal_probe",
            "policy_sha256": canonical_sha256(policy),
            "resource_fingerprint": dict(resource_fingerprint),
            "request_set_sha256": canonical_sha256([evaluation["request_sha256"]]),
            "authorized_probes": sorted(authorized_probes),
            "allow_viewport_input_probe": payload.get(
                "allow_viewport_input_probe"
            ) is True,
            "internal_probe": evaluation,
        }
        evidence["decision_sha256"] = canonical_sha256(evidence)
        if reason:
            raise MaaEndGuardError(
                "probe_denied",
                f"MaaEnd 风险策略拒绝内部探针: {probe_name}",
                evidence,
            )
        return evidence

    raw_tasks = policy.get("tasks")
    assert isinstance(raw_tasks, list)
    task_definitions = {
        str(row.get("name") or ""): dict(row)
        for row in raw_tasks
        if isinstance(row, Mapping)
    }
    authorized = _authorized_task_names(payload)
    evaluations: list[dict[str, object]] = []
    denied: list[dict[str, object]] = []
    configurations = _enabled_task_configurations(profile)
    if len(configurations) != len(task_requests):
        raise MaaEndGuardError(
            "request_mismatch",
            "MaaEnd 风险校验中的任务配置与待提交请求数量不一致",
            {
                "allowed": False,
                "configuration_count": len(configurations),
                "request_count": len(task_requests),
            },
        )

    for row, request in zip(configurations, task_requests):
        name = str(row.get("name") or "").strip()
        task_policy = task_definitions.get(name)
        option_values = row.get("option_values")
        canonical_options = option_values if isinstance(option_values, Mapping) else {}
        option_sha256 = canonical_sha256(canonical_options)
        request_sha256 = canonical_sha256(dict(request))
        request_entry = str(request.get("entry") or "").strip()
        evaluation: dict[str, object] = {
            "name": name,
            "request_entry": request_entry,
            "option_sha256": option_sha256,
            "request_sha256": request_sha256,
            "allowed": True,
        }
        if task_policy is None:
            evaluation.update(
                {"allowed": False, "reason": "unknown_task", "authorization": "unsupported"}
            )
        else:
            authorization = str(task_policy.get("authorization") or "unsupported")
            evaluation.update(
                {
                    "expected_entry": str(task_policy.get("entry") or ""),
                    "wave": task_policy.get("wave"),
                    "risk": task_policy.get("risk"),
                    "authorization": authorization,
                    "terminal_contract": task_policy.get("terminal_contract"),
                }
            )
            if task_policy.get("adb_supported") is not True or authorization == "unsupported":
                evaluation.update({"allowed": False, "reason": "adb_not_allowlisted"})
            elif request_entry != str(task_policy.get("entry") or ""):
                evaluation.update({"allowed": False, "reason": "task_entry_drift"})
            elif authorization == "task" and name not in authorized:
                evaluation.update({"allowed": False, "reason": "explicit_authorization_required"})
            elif authorization == "baker_entry" and not (
                name in authorized and payload.get("allow_baker_entry") is True
            ):
                evaluation.update({"allowed": False, "reason": "baker_entry_authorization_required"})
        evaluations.append(evaluation)
        if evaluation["allowed"] is not True:
            denied.append(evaluation)

    request_set_sha256 = canonical_sha256(
        [
            {
                "name": row["name"],
                "option_sha256": row["option_sha256"],
                "request_sha256": row["request_sha256"],
            }
            for row in evaluations
        ]
    )
    evidence = {
        "allowed": not denied,
        "mode": "tasks",
        "policy_sha256": canonical_sha256(policy),
        "resource_fingerprint": dict(resource_fingerprint),
        "request_set_sha256": request_set_sha256,
        "authorized_tasks": sorted(authorized),
        "tasks": evaluations,
    }
    evidence["decision_sha256"] = canonical_sha256(evidence)
    if denied:
        names = ", ".join(str(row["name"]) for row in denied)
        raise MaaEndGuardError(
            "task_denied",
            f"MaaEnd 风险策略拒绝任务: {names}",
            evidence,
        )
    return evidence


def evaluate_terminal_contracts(
    policy: Mapping[str, object],
    task_rows: Sequence[Mapping[str, object]],
    *,
    terminal_screenshot: Mapping[str, object] | None,
    viewport_evidence: Mapping[str, object] | None,
    structured_events: Sequence[Mapping[str, object]],
    active_contacts: Sequence[int],
    blocking_incidents: Sequence[Mapping[str, object]],
    device_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Evaluate business evidence without rewriting MXU per-task truth."""

    task_definitions = {
        str(row.get("name") or ""): dict(row)
        for row in policy.get("tasks", [])
        if isinstance(row, Mapping)
    }
    task_definitions.update(_internal_probe_definitions(policy))
    screenshot = dict(terminal_screenshot or {})
    viewport = dict(viewport_evidence or {})
    device = dict(device_state or {})
    event_names = {str(row.get("kind") or "") for row in structured_events}
    succeeded_task_events = [
        row
        for row in structured_events
        if isinstance(row, Mapping) and row.get("kind") == "task_succeeded"
    ]
    coordinate_events = [
        row
        for row in structured_events
        if isinstance(row, Mapping)
        and row.get("kind") == "controller_action"
        and str(row.get("status") or "") == "succeeded"
        and str(row.get("action") or "").replace("-", "_").casefold()
        in _COORDINATE_ACTIONS
    ]
    results: list[dict[str, object]] = []

    for raw in task_rows:
        name = str(raw.get("name") or "").strip()
        mxu_status = str(raw.get("status") or "")
        definition = task_definitions.get(name, {})
        contract = str(definition.get("terminal_contract") or "unsupported")
        checks: dict[str, bool] = {
            "mxu_succeeded": mxu_status == "succeeded",
            "no_active_contacts": not active_contacts,
            "no_blocking_incidents": not blocking_incidents,
        }
        if contract in {
            "android_open_game",
            "pipeline_entry",
            "pipeline_terminal",
            "standard_pipeline",
            "scene_identified",
            "uid_hash_captured",
            "input_reversed",
        }:
            checks.update(
                {
                    "terminal_screenshot": bool(screenshot),
                    "logical_1280x720": screenshot.get("width") == 1280
                    and screenshot.get("height") == 720,
                    "landscape": int(screenshot.get("width") or 0)
                    > int(screenshot.get("height") or 0),
                }
            )
        if contract in {
            "android_open_game",
            "close_game",
            "pipeline_entry",
            "pipeline_terminal",
            "scene_identified",
            "uid_hash_captured",
            "input_reversed",
        }:
            expected_entry = str(definition.get("entry") or "").strip()
            expected_task_id = raw.get("maa_task_id")
            matching_events = [
                row
                for row in succeeded_task_events
                if str(row.get("entry") or "") == expected_entry
            ]
            checks["entry_contract_declared"] = bool(expected_entry)
            checks["entry_succeeded_observed"] = bool(matching_events)
            if expected_task_id is not None:
                checks["task_id_correlated"] = any(
                    str(row.get("task_id")) == str(expected_task_id)
                    for row in matching_events
                )
            elif contract == "pipeline_terminal":
                checks["task_id_correlated"] = False
        if contract == "pipeline_terminal":
            expected_task_id = raw.get("maa_task_id")
            terminal_nodes = [
                str(item)
                for item in definition.get("terminal_nodes", [])
                if isinstance(item, str) and item.strip()
            ]
            named_terminal_events = [
                row
                for row in structured_events
                if isinstance(row, Mapping)
                and row.get("kind") == "node_succeeded"
                and str(row.get("name") or "") in terminal_nodes
            ]
            correlated_terminal_events = [
                row
                for row in named_terminal_events
                if expected_task_id is not None
                and str(row.get("task_id")) == str(expected_task_id)
            ]
            checks["terminal_nodes_declared"] = bool(terminal_nodes)
            checks["terminal_node_succeeded_observed"] = bool(named_terminal_events)
            checks["terminal_node_task_id_correlated"] = bool(
                correlated_terminal_events
            )
        if contract == "android_open_game":
            checks["viewport_active"] = viewport.get("active") is True
            checks["launch_gate_observed"] = (
                "viewport_activated" in event_names
                or viewport.get("gate_observed") is True
            )
        elif contract == "close_game":
            checks["stop_app_observed"] = "stop_app_succeeded" in event_names
            checks["foreground_evidence_available"] = device.get("available") is True
            checks["game_not_foreground"] = device.get("game_foreground") is False
        elif contract == "scene_identified":
            matching = _matching_recognition_events(
                structured_events,
                str(definition.get("entry") or ""),
            )
            checks["scene_recognition_labeled"] = bool(matching)
            checks["no_coordinate_input_observed"] = not coordinate_events
        elif contract == "uid_hash_captured":
            uid_events = [
                row
                for row in structured_events
                if isinstance(row, Mapping)
                and row.get("kind") == "agent_log"
                and row.get("component") == "captureuid"
                and row.get("uid_hash_present") is True
                and _valid_alignment_frame(row)
            ]
            checks["uid_hash_evidence"] = bool(uid_events)
            checks["no_coordinate_input_observed"] = not coordinate_events
        elif contract == "input_reversed":
            matching = _matching_recognition_events(
                structured_events,
                str(definition.get("precondition_entry") or "InWorld"),
            )
            agent_events = [
                row
                for row in structured_events
                if isinstance(row, Mapping)
                and row.get("kind") == "agent_log"
                and row.get("component") == "viewport_input_probe"
            ]
            joystick_agent = any(
                row.get("phase") == "joystick_center_released"
                and row.get("alignment") == "left"
                and row.get("contact") == 0
                and _valid_alignment_frame(row)
                for row in agent_events
            )
            camera_agent = any(
                row.get("phase") == "camera_reversed"
                and row.get("alignment") == "right"
                and row.get("contact") == 1
                and row.get("reversed") is True
                and _valid_alignment_frame(row)
                for row in agent_events
            )
            checks["safe_open_world_recognized"] = bool(matching)
            checks["joystick_agent_evidence"] = joystick_agent
            checks["camera_agent_evidence"] = camera_agent
            checks.update(_viewport_input_action_checks(coordinate_events))
        elif contract == "unsupported":
            checks["supported_contract"] = False

        passed = all(checks.values())
        results.append(
            {
                "name": name,
                "contract": contract,
                "mxu_status": mxu_status,
                "passed": passed,
                "checks": checks,
            }
        )

    return {
        "verified": bool(results) and all(row["passed"] is True for row in results),
        "tasks": results,
        "terminal_screenshot": screenshot,
        "viewport": viewport,
        "device_state": device,
        "active_contacts": list(active_contacts),
        "blocking_incident_count": len(blocking_incidents),
    }


def _valid_alignment_frame(row: Mapping[str, object]) -> bool:
    try:
        frame_id = int(row.get("frame_id") or 0)
    except (TypeError, ValueError):
        frame_id = 0
    return row.get("alignment") in {"left", "center", "right"} and frame_id > 0


def _matching_recognition_events(
    events: Sequence[Mapping[str, object]],
    node_name: str,
) -> list[Mapping[str, object]]:
    return [
        row
        for row in events
        if isinstance(row, Mapping)
        and row.get("kind") == "node_succeeded"
        and str(row.get("name") or "") == node_name
        and isinstance(row.get("recognition"), Mapping)
        and _valid_alignment_frame(row["recognition"])
    ]


def _action_contact(row: Mapping[str, object]) -> int | None:
    param = row.get("param")
    if not isinstance(param, Mapping):
        return None
    try:
        return int(param.get("contact"))
    except (TypeError, ValueError):
        return None


def _logical_point(row: Mapping[str, object]) -> tuple[int, int] | None:
    viewport = row.get("viewport")
    if not isinstance(viewport, Mapping):
        return None
    logical = viewport.get("logical_param")
    if not isinstance(logical, Mapping):
        return None
    point = logical.get("point")
    if not (
        isinstance(point, list)
        and len(point) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) for value in point)
    ):
        return None
    return point[0], point[1]


def _viewport_input_action_checks(
    events: Sequence[Mapping[str, object]],
) -> dict[str, bool]:
    rows = []
    for event in events:
        action = str(event.get("action") or "").replace("-", "_").casefold()
        # MaaFramework serializes TouchUp with a placeholder logical point
        # [0, 0], but TouchUp has no coordinate semantics. Keep validating its
        # contact and snapshotted alignment while ignoring that placeholder.
        point = None if action == "touch_up" else _logical_point(event)
        rows.append(
            {
                "action": action,
                "contact": _action_contact(event),
                "alignment": (
                    event.get("viewport", {}).get("alignment")
                    if isinstance(event.get("viewport"), Mapping)
                    else None
                ),
                "point": point,
            }
        )
    expected = [
        ("touch_down", 0, "left", (195, 551)),
        ("touch_up", 0, "left", None),
        ("touch_down", 1, "right", (640, 264)),
        ("touch_move", 1, "right", (664, 264)),
        ("touch_move", 1, "right", (640, 264)),
        ("touch_up", 1, "right", None),
    ]
    observed = [
        (row["action"], row["contact"], row["alignment"], row["point"])
        for row in rows
    ]
    exact_sequence = observed == expected
    return {
        "only_probe_touch_actions": bool(rows)
        and all(row["action"].startswith("touch_") for row in rows),
        "left_contact_released": exact_sequence,
        "right_camera_reversed": exact_sequence,
    }
