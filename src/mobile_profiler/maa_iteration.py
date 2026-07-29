from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


POLICY_SCHEMA_VERSION = 1
ISSUE_SCHEMA_VERSION = 1
FIXTURE_SCHEMA_VERSION = 1
INCIDENT_SCHEMA_VERSION = 1

ERROR_MESSAGES = {"InternalError", "InitFailed", "TaskChainError", "SubTaskError"}
VALID_ISSUE_STATUSES = {"open", "mitigated", "fixed", "known_limit", "wont_fix"}
VALID_ALIGNMENTS = {"left", "center", "right"}

MAA_FEATURE_PROBES: dict[str, dict[str, object]] = {
    "StartUp": {
        "risk": "read_only",
        "defaults": {
            "enable": True,
            "client_type": "Official",
            "start_game_enabled": False,
        },
    },
    "Depot": {"risk": "read_only", "defaults": {"enable": True}},
    "OperBox": {"risk": "read_only", "defaults": {"enable": True}},
    "Award": {
        "risk": "account_mutation",
        "defaults": {
            "enable": True,
            "award": True,
            "mail": False,
            "recruit": False,
            "orundum": False,
            "mining": False,
            "specialaccess": False,
        },
    },
}


def prepare_maa_feature_probe(
    task: str,
    params: Mapping[str, object] | None = None,
    *,
    allow_account_mutation: bool = False,
) -> dict[str, object]:
    """Validate an allowlisted MAA feature probe and apply safe defaults."""

    definition = MAA_FEATURE_PROBES.get(task)
    if definition is None:
        allowed = ", ".join(sorted(MAA_FEATURE_PROBES))
        raise ValueError(f"unsupported feature probe {task!r}; allowed: {allowed}")

    risk = str(definition["risk"])
    if risk == "account_mutation" and not allow_account_mutation:
        raise ValueError(
            f"{task} changes account state; pass --allow-account-mutation explicitly"
        )

    merged = dict(definition["defaults"])
    if params:
        merged.update(params)
    return {"task": task, "risk": risk, "params": merged}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _as_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EvidenceImageWriter:
    """Write evidence images while suppressing byte-identical periodic frames."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._last_sha256 = ""
        self._observed = 0
        self._saved = 0
        self._skipped_duplicates = 0
        self._bytes_written = 0
        self._bytes_avoided = 0
        self._last_saved = ""

    def write(self, filename: str, payload: bytes, *, force: bool = False) -> bool:
        if not payload:
            raise ValueError("evidence image payload must not be empty")
        digest = sha256_bytes(payload)
        duplicate = digest == self._last_sha256
        self._last_sha256 = digest
        self._observed += 1
        if duplicate and not force:
            self._skipped_duplicates += 1
            self._bytes_avoided += len(payload)
            return False

        destination = self.root / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        self._saved += 1
        self._bytes_written += len(payload)
        self._last_saved = filename
        return True

    def state(self) -> dict[str, object]:
        return {
            "observed": self._observed,
            "saved": self._saved,
            "skipped_duplicates": self._skipped_duplicates,
            "bytes_written": self._bytes_written,
            "bytes_avoided": self._bytes_avoided,
            "last_saved": self._last_saved or None,
            "last_sha256": self._last_sha256 or None,
        }


def task_suffix(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = text.rsplit("@", 1)[-1]
    return text.split("#", 1)[0].strip("()")


def callback_task(details: object) -> str:
    outer = _as_dict(details)
    nested = _as_dict(outer.get("details"))
    return str(nested.get("task") or "")


def callback_summary(message_name: str, details: object) -> dict[str, object]:
    outer = _as_dict(details)
    result: dict[str, object] = {"message": message_name}
    for key in ("taskchain", "taskid", "subtask", "what", "why", "pre_task"):
        if key in outer:
            result[key] = outer[key]
    first = outer.get("first")
    if isinstance(first, Sequence) and not isinstance(first, (bytes, bytearray, str)):
        names = [str(item) for item in first]
        result["first"] = names
        if len(names) == 1:
            result["probe"] = names[0]
    task = callback_task(outer)
    if task:
        result["task"] = task
    return result


def summarize_feature_extra(details: object) -> dict[str, object] | None:
    """Reduce Depot/OperBox extra-info callbacks without retaining account data."""

    outer = _as_dict(details)
    what = outer.get("what")
    nested = _as_dict(outer.get("details"))
    if not isinstance(what, str) or not nested:
        return None

    summary: dict[str, object] = {"what": what}
    if "done" in nested:
        summary["done"] = bool(nested["done"])
    if what in {"Depot", "DepotInfo"}:
        data = nested.get("data")
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = None
            summary["item_types"] = len(parsed) if isinstance(parsed, dict) else None
    elif what in {"OperBox", "OperBoxInfo"}:
        all_opers = nested.get("all_opers")
        own_opers = nested.get("own_opers")
        summary["all_operators"] = len(all_opers) if isinstance(all_opers, list) else None
        summary["owned_operators"] = len(own_opers) if isinstance(own_opers, list) else None
    return summary


def validate_guard_policy(policy: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        errors.append(f"guard policy schema_version must be {POLICY_SCHEMA_VERSION}")
    destructive = policy.get("destructive_tasks")
    if not isinstance(destructive, list) or not destructive:
        errors.append("guard policy destructive_tasks must be a non-empty list")
    else:
        seen_resources: set[str] = set()
        for index, item in enumerate(destructive):
            if not isinstance(item, dict):
                errors.append(f"destructive_tasks[{index}] must be an object")
                continue
            resource_task = item.get("resource_task")
            callback_suffix = item.get("callback_suffix")
            if not isinstance(resource_task, str) or not resource_task:
                errors.append(f"destructive_tasks[{index}].resource_task is required")
            elif resource_task in seen_resources:
                errors.append(f"duplicate destructive resource task: {resource_task}")
            else:
                seen_resources.add(resource_task)
            if not isinstance(callback_suffix, str) or not callback_suffix:
                errors.append(f"destructive_tasks[{index}].callback_suffix is required")

    watchdogs = policy.get("watchdogs")
    if not isinstance(watchdogs, dict):
        errors.append("guard policy watchdogs must be an object")
    else:
        for key in (
            "no_progress_seconds",
            "same_task_start_limit",
            "static_snapshot_limit",
            "slow_screenshot_seconds",
        ):
            value = watchdogs.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                errors.append(f"watchdogs.{key} must be a non-negative number")
    return errors


def load_guard_policy(path: Path) -> dict[str, object]:
    policy = read_json_object(path)
    errors = validate_guard_policy(policy)
    if errors:
        raise ValueError("invalid MAA guard policy: " + "; ".join(errors))
    return policy


def _stable_basis(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value[key] for key in sorted(value)}


def incident_fingerprint(kind: str, basis: Mapping[str, object]) -> str:
    encoded = json.dumps(
        {"kind": kind, "basis": _stable_basis(basis)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"maa:{kind}:{sha256_bytes(encoded)[:16]}"


@dataclass(frozen=True)
class IncidentSignal:
    kind: str
    reason: str
    basis: dict[str, object]
    stop: bool
    fingerprint: str

    @classmethod
    def create(
        cls,
        kind: str,
        reason: str,
        basis: Mapping[str, object],
        *,
        stop: bool,
    ) -> "IncidentSignal":
        stable = _stable_basis(basis)
        return cls(
            kind=kind,
            reason=reason,
            basis=stable,
            stop=stop,
            fingerprint=incident_fingerprint(kind, stable),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "basis": self.basis,
            "stop": self.stop,
            "fingerprint": self.fingerprint,
        }


class CallbackMonitor:
    """Classify MAA callbacks without depending on MaaCore or a live device."""

    def __init__(
        self,
        policy: Mapping[str, object],
        *,
        guard_destructive_actions: bool = True,
        stop_after_settlement: bool | None = None,
        now: float | None = None,
    ) -> None:
        errors = validate_guard_policy(policy)
        if errors:
            raise ValueError("invalid MAA guard policy: " + "; ".join(errors))
        self.policy = dict(policy)
        self.guard_destructive_actions = guard_destructive_actions
        self.stop_after_settlement = (
            bool(policy.get("stop_after_settlement", True))
            if stop_after_settlement is None
            else stop_after_settlement
        )
        self.watchdogs = _as_dict(policy.get("watchdogs"))
        self.started_at = time.monotonic() if now is None else now
        self.last_progress_at = self.started_at
        self.event_count = 0
        self.last_event: dict[str, object] | None = None
        self.all_tasks_completed = False
        self.settlement: dict[str, object] | None = None
        self.errors: list[dict[str, object]] = []
        self.optional_errors: list[dict[str, object]] = []
        self.stop_requested = False
        self.stop_reason = ""
        self.guard_trigger: dict[str, object] | None = None
        self.screenshot_durations: list[float] = []
        self.snapshot_count = 0
        self._last_task_signature = ""
        self._same_task_starts = 0
        self._last_snapshot_sha256 = ""
        self._same_snapshot_count = 0
        self._emitted: set[str] = set()

    def reset_clock(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.started_at = current
        self.last_progress_at = current

    def _emit_once(self, signal: IncidentSignal) -> IncidentSignal | None:
        if signal.fingerprint in self._emitted:
            return None
        self._emitted.add(signal.fingerprint)
        if signal.stop and not self.stop_requested:
            self.stop_requested = True
            self.stop_reason = signal.kind
        return signal

    def _destructive_rule(self, task_name: str) -> dict[str, object] | None:
        suffix = task_suffix(task_name)
        rules = self.policy.get("destructive_tasks")
        if not isinstance(rules, list):
            return None
        for value in rules:
            rule = _as_dict(value)
            expected = str(rule.get("callback_suffix") or "")
            if expected and suffix == expected:
                return rule
        return None

    @staticmethod
    def _is_optional_error(message_name: str, details: Mapping[str, object]) -> bool:
        """Recognize MaaCore process probes that use failure as a branch condition."""

        first = details.get("first")
        return (
            message_name == "SubTaskError"
            and details.get("taskchain") == "Roguelike"
            and details.get("subtask") == "ProcessTask"
            and isinstance(first, Sequence)
            and not isinstance(first, (bytes, bytearray, str))
            and len(first) == 1
            and task_suffix(first[0]) == "WaitForStartButtonClicked"
        )

    def observe(
        self,
        message_id: int,
        message_name: str,
        details: object,
        *,
        now: float | None = None,
    ) -> list[IncidentSignal]:
        current = time.monotonic() if now is None else now
        outer = _as_dict(details)
        self.event_count += 1
        self.last_event = callback_summary(message_name, outer)
        emitted: list[IncidentSignal] = []

        if message_name == "AllTasksCompleted":
            self.all_tasks_completed = True
            self.last_progress_at = current

        if message_name in ERROR_MESSAGES:
            summary = callback_summary(message_name, outer)
            if self._is_optional_error(message_name, outer):
                summary["optional_reason"] = "expected_negative_probe"
                self.optional_errors.append(summary)
            else:
                self.errors.append(summary)
                signal = IncidentSignal.create(
                    "maa_error",
                    f"MAA callback reported {message_name}",
                    {
                        "message": message_name,
                        "task": task_suffix(callback_task(outer)),
                        "what": str(outer.get("what") or ""),
                        "why": str(outer.get("why") or ""),
                    },
                    stop=True,
                )
                unique = self._emit_once(signal)
                if unique:
                    emitted.append(unique)
            self.last_progress_at = current

        if message_name in {"TaskChainStart", "TaskChainStopped"}:
            self.last_progress_at = current

        if message_name == "SubTaskExtraInfo" and outer.get("what") == "RoguelikeSettlement":
            self.settlement = _as_dict(outer.get("details"))
            self.last_progress_at = current
            if self.stop_after_settlement:
                self.stop_requested = True
                game_pass = self.settlement.get("game_pass")
                if game_pass is True:
                    self.stop_reason = "natural_settlement_pass"
                elif game_pass is False:
                    self.stop_reason = "natural_settlement_fail"
                else:
                    self.stop_reason = "natural_settlement_unknown"

        if message_name == "SubTaskStart":
            task_name = callback_task(outer)
            nested = _as_dict(outer.get("details"))
            signature = "|".join(
                (
                    str(outer.get("taskchain") or ""),
                    str(outer.get("subtask") or ""),
                    task_suffix(task_name),
                    str(nested.get("action") or ""),
                )
            )
            if signature == self._last_task_signature:
                self._same_task_starts += 1
            else:
                self._last_task_signature = signature
                self._same_task_starts = 1
                self.last_progress_at = current

            rule = self._destructive_rule(task_name)
            if rule:
                basis = {
                    "task": task_suffix(task_name),
                    "pre_task": task_suffix(outer.get("pre_task")),
                    "taskchain": str(outer.get("taskchain") or ""),
                }
                kind = (
                    "destructive_task_guarded"
                    if self.guard_destructive_actions
                    else "destructive_task_observed"
                )
                signal = IncidentSignal.create(
                    kind,
                    str(rule.get("reason") or f"destructive task {task_suffix(task_name)} observed"),
                    basis,
                    stop=self.guard_destructive_actions,
                )
                unique = self._emit_once(signal)
                if unique:
                    emitted.append(unique)
                self.guard_trigger = signal.as_dict()

            same_task_limit = int(self.watchdogs.get("same_task_start_limit") or 0)
            if same_task_limit and self._same_task_starts >= same_task_limit:
                signal = IncidentSignal.create(
                    "callback_loop",
                    f"the same task started {self._same_task_starts} consecutive times",
                    {"task_signature": signature, "limit": same_task_limit},
                    stop=True,
                )
                unique = self._emit_once(signal)
                if unique:
                    emitted.append(unique)

        return emitted

    def poll(self, *, now: float | None = None) -> list[IncidentSignal]:
        current = time.monotonic() if now is None else now
        if self.stop_requested or self.settlement:
            return []
        timeout = float(self.watchdogs.get("no_progress_seconds") or 0)
        if not timeout or current - self.last_progress_at < timeout:
            return []
        last = self.last_event or {}
        signal = IncidentSignal.create(
            "no_progress",
            f"no semantic callback progress for {round(current - self.last_progress_at, 1)} seconds",
            {
                "last_message": str(last.get("message") or ""),
                "last_task": task_suffix(last.get("task")),
                "threshold_seconds": timeout,
            },
            stop=True,
        )
        unique = self._emit_once(signal)
        return [unique] if unique else []

    def observe_snapshot(
        self,
        payload: bytes,
        duration_seconds: float,
    ) -> list[IncidentSignal]:
        self.snapshot_count += 1
        self.screenshot_durations.append(duration_seconds)
        emitted: list[IncidentSignal] = []

        slow_threshold = float(self.watchdogs.get("slow_screenshot_seconds") or 0)
        if slow_threshold and duration_seconds >= slow_threshold:
            signal = IncidentSignal.create(
                "slow_screenshot",
                f"cached screenshot took {duration_seconds:.3f} seconds",
                {"threshold_seconds": slow_threshold},
                stop=bool(self.watchdogs.get("stop_on_slow_screenshot", False)),
            )
            unique = self._emit_once(signal)
            if unique:
                emitted.append(unique)

        digest = sha256_bytes(payload)
        if digest == self._last_snapshot_sha256:
            self._same_snapshot_count += 1
        else:
            self._last_snapshot_sha256 = digest
            self._same_snapshot_count = 1

        static_limit = int(self.watchdogs.get("static_snapshot_limit") or 0)
        if static_limit and self._same_snapshot_count >= static_limit:
            signal = IncidentSignal.create(
                "static_screen",
                f"the cached frame was byte-identical for {self._same_snapshot_count} snapshots",
                {"image_sha256": digest, "limit": static_limit},
                stop=True,
            )
            unique = self._emit_once(signal)
            if unique:
                emitted.append(unique)
        return emitted

    def snapshot_stats(self) -> dict[str, object]:
        values = sorted(self.screenshot_durations)
        if not values:
            return {"count": 0, "min_seconds": None, "p50_seconds": None, "p95_seconds": None, "max_seconds": None}

        def percentile(ratio: float) -> float:
            index = min(len(values) - 1, max(0, int(round((len(values) - 1) * ratio))))
            return round(values[index], 4)

        return {
            "count": len(values),
            "min_seconds": round(values[0], 4),
            "p50_seconds": percentile(0.50),
            "p95_seconds": percentile(0.95),
            "max_seconds": round(values[-1], 4),
        }

    def state(self) -> dict[str, object]:
        return {
            "event_count": self.event_count,
            "last_event": self.last_event,
            "all_tasks_completed": self.all_tasks_completed,
            "settlement": self.settlement,
            "errors": list(self.errors),
            "optional_errors": list(self.optional_errors),
            "stop_requested": self.stop_requested,
            "stop_reason": self.stop_reason,
            "guard_trigger": self.guard_trigger,
            "snapshot_stats": self.snapshot_stats(),
        }


def classify_exit(
    monitor: CallbackMonitor,
    *,
    timed_out: bool = False,
    interrupted: bool = False,
    runner_error: str = "",
) -> dict[str, object]:
    if runner_error:
        reason = "runner_error"
    elif interrupted:
        reason = "manual_stop"
    elif timed_out:
        reason = "timeout"
    elif monitor.stop_reason and not monitor.stop_reason.startswith("natural_settlement"):
        reason = monitor.stop_reason
    elif monitor.errors:
        reason = "maa_error"
    elif monitor.settlement is not None:
        game_pass = monitor.settlement.get("game_pass")
        if game_pass is True:
            reason = "natural_settlement_pass"
        elif game_pass is False:
            reason = "natural_settlement_fail"
        else:
            reason = "natural_settlement_unknown"
    elif monitor.all_tasks_completed:
        reason = "maa_tasks_completed_without_settlement"
    else:
        reason = "core_stopped_without_terminal_event"

    natural = reason in {
        "natural_settlement_pass",
        "natural_settlement_fail",
        "natural_settlement_unknown",
    }
    return {
        "exit_reason": reason,
        "one_round_completed": natural,
        "natural_settlement": natural,
        "game_pass": monitor.settlement.get("game_pass") if monitor.settlement else None,
        "maa_all_tasks_completed": monitor.all_tasks_completed,
        "successful": natural and not monitor.errors,
    }


def write_guard_overlay(
    root: Path,
    policy: Mapping[str, object],
) -> tuple[Path, Path]:
    errors = validate_guard_policy(policy)
    if errors:
        raise ValueError("invalid MAA guard policy: " + "; ".join(errors))
    overlay_root = root.resolve()
    task_path = overlay_root / "resource" / "tasks" / "mobile_profiler_guard.json"
    tasks: dict[str, object] = {}
    for value in policy.get("destructive_tasks", []):
        rule = _as_dict(value)
        resource_task = str(rule.get("resource_task") or "")
        if resource_task:
            tasks[resource_task] = {
                "action": "Stop",
                "Doc": "Mobile Profiler diagnostic guard: stop before destructive fallback",
            }
    write_json_atomic(task_path, tasks)
    write_json_atomic(overlay_root / "guard-policy.json", dict(policy))
    return overlay_root, task_path


def _tail_text(path: Path, limit: int = 256 * 1024) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - limit))
        return stream.read().decode("utf-8", errors="replace")


def _safe_fingerprint_path(fingerprint: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", fingerprint).strip("-")


def _safe_artifact_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    if not key:
        raise ValueError("incident artifact key must contain a safe character")
    return key


def _artifact_metadata(path: Path) -> dict[str, object]:
    return {
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def record_incident_bundle(
    run_dir: Path,
    signal: IncidentSignal,
    *,
    events_tail: Sequence[Mapping[str, object]] = (),
    screenshot: bytes | None = None,
    environment: Mapping[str, object] | None = None,
    asst_log: Path | None = None,
    artifact_files: Mapping[str, Path] | None = None,
    artifact_json: Mapping[str, object] | None = None,
    log_files: Mapping[str, Path] | None = None,
    observed_at: str | None = None,
) -> dict[str, object]:
    timestamp = observed_at or _now_iso()
    incident_dir = run_dir / "incidents" / _safe_fingerprint_path(signal.fingerprint)
    incident_dir.mkdir(parents=True, exist_ok=True)
    incident_path = incident_dir / "incident.json"
    if incident_path.is_file():
        incident = read_json_object(incident_path)
    else:
        incident = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "fingerprint": signal.fingerprint,
            "kind": signal.kind,
            "reason": signal.reason,
            "basis": signal.basis,
            "first_seen": timestamp,
            "last_seen": timestamp,
            "occurrence_count": 0,
            "occurrences": [],
        }

    occurrence_count = int(incident.get("occurrence_count") or 0) + 1
    stem = f"occurrence-{occurrence_count:04d}"
    artifacts: dict[str, str] = {}
    artifact_details: dict[str, dict[str, object]] = {}

    def remember(key: str, path: Path) -> None:
        artifacts[key] = path.name
        artifact_details[key] = _artifact_metadata(path)

    if screenshot:
        screenshot_path = incident_dir / f"{stem}.png"
        screenshot_path.write_bytes(screenshot)
        remember("screenshot", screenshot_path)
    if events_tail:
        events_path = incident_dir / f"{stem}-events.jsonl"
        events_path.write_text(
            "".join(json.dumps(dict(item), ensure_ascii=False) + "\n" for item in events_tail),
            encoding="utf-8",
        )
        remember("events", events_path)
    if asst_log:
        log_tail = _tail_text(asst_log)
        if log_tail:
            log_path = incident_dir / f"{stem}-asst.log.txt"
            log_path.write_text(log_tail, encoding="utf-8")
            remember("asst_log", log_path)
    for raw_key, source in sorted((artifact_files or {}).items()):
        if not source.is_file():
            continue
        key = _safe_artifact_key(str(raw_key))
        suffix = "".join(source.suffixes[-2:]) or ".bin"
        destination = incident_dir / f"{stem}-{key}{suffix}"
        shutil.copy2(source, destination)
        remember(key, destination)
    for raw_key, value in sorted((artifact_json or {}).items()):
        key = _safe_artifact_key(str(raw_key))
        destination = incident_dir / f"{stem}-{key}.json"
        write_json_atomic(destination, value)
        remember(key, destination)
    for raw_key, source in sorted((log_files or {}).items()):
        log_tail = _tail_text(source)
        if not log_tail:
            continue
        key = _safe_artifact_key(str(raw_key))
        destination = incident_dir / f"{stem}-{key}.log.txt"
        destination.write_text(log_tail, encoding="utf-8")
        remember(key, destination)
    if environment:
        environment_path = incident_dir / f"{stem}-environment.json"
        write_json_atomic(environment_path, dict(environment))
        # Keep a stable latest snapshot for humans while each occurrence points
        # at its own immutable environment evidence.
        write_json_atomic(incident_dir / "environment.json", dict(environment))
        remember("environment", environment_path)

    occurrences = incident.get("occurrences")
    if not isinstance(occurrences, list):
        occurrences = []
    occurrences.append(
        {
            "index": occurrence_count,
            "observed_at": timestamp,
            "reason": signal.reason,
            "artifacts": artifacts,
            "artifact_details": artifact_details,
        }
    )
    incident.update(
        {
            "last_seen": timestamp,
            "occurrence_count": occurrence_count,
            "occurrences": occurrences,
        }
    )
    write_json_atomic(incident_path, incident)

    index_path = run_dir / "incidents" / "index.json"
    entries: list[dict[str, object]] = []
    for candidate in sorted((run_dir / "incidents").glob("*/incident.json")):
        value = read_json_object(candidate)
        entries.append(
            {
                "fingerprint": value.get("fingerprint"),
                "kind": value.get("kind"),
                "occurrence_count": value.get("occurrence_count"),
                "last_seen": value.get("last_seen"),
                "path": candidate.relative_to(run_dir).as_posix(),
            }
        )
    write_json_atomic(index_path, {"schema_version": INCIDENT_SCHEMA_VERSION, "incidents": entries})
    return incident


def validate_issue_ledger(ledger: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if ledger.get("schema_version") != ISSUE_SCHEMA_VERSION:
        errors.append(f"issue ledger schema_version must be {ISSUE_SCHEMA_VERSION}")
    issues = ledger.get("issues")
    if not isinstance(issues, list):
        return errors + ["issue ledger issues must be a list"]
    ids: set[str] = set()
    fingerprints: set[str] = set()
    for index, item in enumerate(issues):
        if not isinstance(item, dict):
            errors.append(f"issues[{index}] must be an object")
            continue
        for key in ("id", "fingerprint", "title", "area", "status"):
            if not isinstance(item.get(key), str) or not item.get(key):
                errors.append(f"issues[{index}].{key} is required")
        issue_id = str(item.get("id") or "")
        fingerprint = str(item.get("fingerprint") or "")
        if issue_id in ids:
            errors.append(f"duplicate issue id: {issue_id}")
        ids.add(issue_id)
        if fingerprint in fingerprints:
            errors.append(f"duplicate issue fingerprint: {fingerprint}")
        fingerprints.add(fingerprint)
        if item.get("status") not in VALID_ISSUE_STATUSES:
            errors.append(f"issues[{index}].status is invalid: {item.get('status')}")
    return errors


def _incident_title(kind: str, basis: Mapping[str, object]) -> str:
    task = str(basis.get("task") or basis.get("last_task") or "")
    labels = {
        "destructive_task_guarded": "危险兜底动作已被护栏拦截",
        "destructive_task_observed": "运行中出现危险兜底动作",
        "maa_error": "MAA 回调报告错误",
        "callback_loop": "回调任务进入重复循环",
        "no_progress": "运行长时间没有语义进展",
        "static_screen": "运行画面长期静止",
        "slow_screenshot": "截图链路持续偏慢",
    }
    title = labels.get(kind, f"MAA 运行问题：{kind}")
    return f"{title}（{task}）" if task else title


def triage_run_incidents(run_dir: Path, ledger_path: Path) -> dict[str, object]:
    ledger = read_json_object(ledger_path)
    errors = validate_issue_ledger(ledger)
    if errors:
        raise ValueError("invalid MAA issue ledger: " + "; ".join(errors))
    issues = ledger.get("issues")
    assert isinstance(issues, list)
    by_fingerprint = {str(item["fingerprint"]): item for item in issues if isinstance(item, dict)}
    added = 0
    updated = 0

    for incident_path in sorted(run_dir.glob("incidents/*/incident.json")):
        incident = read_json_object(incident_path)
        fingerprint = str(incident.get("fingerprint") or "")
        if not fingerprint:
            continue
        source = f"{run_dir.name}/{incident_path.relative_to(run_dir).as_posix()}"
        occurrence_count = int(incident.get("occurrence_count") or 0)
        issue = by_fingerprint.get(fingerprint)
        if issue is None:
            kind = str(incident.get("kind") or "unknown")
            digest = fingerprint.rsplit(":", 1)[-1][:8].upper()
            issue = {
                "id": f"MAA-RUN-{digest}",
                "fingerprint": fingerprint,
                "title": _incident_title(kind, _as_dict(incident.get("basis"))),
                "area": "runtime",
                "status": "open",
                "severity": "high" if kind in {"destructive_task_guarded", "maa_error"} else "medium",
                "first_seen": incident.get("first_seen"),
                "last_seen": incident.get("last_seen"),
                "occurrences": 0,
                "symptoms": [str(incident.get("reason") or kind)],
                "observations": [],
                "fix": None,
                "regression": [],
            }
            issues.append(issue)
            by_fingerprint[fingerprint] = issue
            added += 1
        else:
            updated += 1

        observations = issue.get("observations")
        if not isinstance(observations, list):
            observations = []
        existing = next(
            (value for value in observations if isinstance(value, dict) and value.get("source") == source),
            None,
        )
        observation = {
            "source": source,
            "occurrence_count": occurrence_count,
            "last_seen": incident.get("last_seen"),
        }
        if existing is None:
            observations.append(observation)
        else:
            existing.update(observation)
        issue["observations"] = observations
        issue["occurrences"] = sum(
            int(value.get("occurrence_count") or 0)
            for value in observations
            if isinstance(value, dict)
        )
        seen_values = [
            str(value.get("last_seen"))
            for value in observations
            if isinstance(value, dict) and value.get("last_seen")
        ]
        if seen_values:
            issue["last_seen"] = max(seen_values)

    ledger["updated_at"] = _now_iso()
    write_json_atomic(ledger_path, ledger)
    return {"added": added, "updated": updated, "issue_count": len(issues)}


def validate_fixture_manifest(manifest: Mapping[str, object], manifest_path: Path) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        errors.append(f"fixture manifest schema_version must be {FIXTURE_SCHEMA_VERSION}")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        return errors + ["fixture manifest fixtures must be a list"]
    ids: set[str] = set()
    for index, item in enumerate(fixtures):
        if not isinstance(item, dict):
            errors.append(f"fixtures[{index}] must be an object")
            continue
        fixture_id = item.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            errors.append(f"fixtures[{index}].id is required")
        elif fixture_id in ids:
            errors.append(f"duplicate fixture id: {fixture_id}")
        else:
            ids.add(fixture_id)
        relative = item.get("image")
        if not isinstance(relative, str) or not relative:
            errors.append(f"fixtures[{index}].image is required")
        else:
            image_path = manifest_path.parent / relative
            if not image_path.is_file():
                errors.append(f"fixture image is missing: {relative}")
            elif item.get("sha256") != sha256_file(image_path):
                errors.append(f"fixture hash mismatch: {relative}")
        expected = _as_dict(item.get("expected"))
        if not isinstance(expected.get("task"), str) or not expected.get("task"):
            errors.append(f"fixtures[{index}].expected.task is required")
        if expected.get("alignment") not in VALID_ALIGNMENTS:
            errors.append(f"fixtures[{index}].expected.alignment is invalid")
    return errors


def promote_incident_fixture(
    incident_path: Path,
    manifest_path: Path,
    *,
    fixture_id: str,
    page: str,
    expected_task: str,
    expected_alignment: str,
    forbidden_tasks: Iterable[str] = (),
    privacy_review: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", fixture_id):
        raise ValueError("fixture id may contain only letters, digits, dot, underscore and dash")
    if expected_alignment not in VALID_ALIGNMENTS:
        raise ValueError(f"invalid alignment: {expected_alignment}")
    incident = read_json_object(incident_path)
    occurrences = incident.get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError("incident has no occurrences")
    screenshot_name = ""
    for occurrence in reversed(occurrences):
        artifacts = _as_dict(_as_dict(occurrence).get("artifacts"))
        if artifacts.get("screenshot"):
            screenshot_name = str(artifacts["screenshot"])
            break
    if not screenshot_name:
        raise ValueError("incident has no screenshot artifact")
    source = incident_path.parent / screenshot_name
    if not source.is_file():
        raise ValueError(f"incident screenshot is missing: {source}")

    manifest = read_json_object(manifest_path)
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError("fixture manifest fixtures must be a list")
    if any(isinstance(item, dict) and item.get("id") == fixture_id for item in fixtures):
        raise ValueError(f"fixture already exists: {fixture_id}")
    destination = manifest_path.parent / "images" / f"{fixture_id}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256_file(destination) != sha256_file(source):
        raise ValueError(f"fixture destination already contains different bytes: {destination}")
    shutil.copy2(source, destination)
    entry = {
        "id": fixture_id,
        "page": page,
        "image": destination.relative_to(manifest_path.parent).as_posix(),
        "sha256": sha256_file(destination),
        "source_incident": incident.get("fingerprint"),
        "expected": {
            "task": expected_task,
            "alignment": expected_alignment,
            "must_not_match": list(forbidden_tasks),
        },
    }
    if privacy_review is not None:
        entry["privacy_review"] = dict(privacy_review)
    fixtures.append(entry)
    manifest["fixtures"] = fixtures
    write_json_atomic(manifest_path, manifest)
    errors = validate_fixture_manifest(manifest, manifest_path)
    if errors:
        raise ValueError("promoted fixture is invalid: " + "; ".join(errors))
    return entry


def compare_resource_file(
    source_root: Path,
    runtime_root: Path,
    relative_path: Path,
) -> dict[str, object]:
    source = source_root.resolve() / relative_path
    runtime = runtime_root.resolve() / relative_path
    if not source.is_file():
        raise FileNotFoundError(f"source resource not found: {source}")
    if not runtime.is_file():
        raise FileNotFoundError(f"runtime resource not found: {runtime}")
    source_hash = sha256_file(source)
    runtime_hash = sha256_file(runtime)
    return {
        "relative_path": relative_path.as_posix(),
        "source": os.fspath(source),
        "runtime": os.fspath(runtime),
        "source_sha256": source_hash,
        "runtime_sha256": runtime_hash,
        "matches": source_hash == runtime_hash,
    }


def compare_worktree_patch(
    source_root: Path,
    patch_path: Path,
    *,
    include_untracked: Sequence[Path] = (),
) -> dict[str, object]:
    actual = _worktree_diff(source_root, include_untracked=include_untracked)
    expected = _normalize_patch(patch_path.read_bytes())
    return {
        "source_root": os.fspath(source_root.resolve()),
        "patch": os.fspath(patch_path.resolve()),
        "worktree_diff_sha256": sha256_bytes(actual),
        "patch_sha256": sha256_bytes(expected),
        "matches": actual == expected,
    }


def _normalize_patch(payload: bytes) -> bytes:
    return payload.replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n"


def _worktree_diff(
    source_root: Path,
    *,
    include_untracked: Sequence[Path] = (),
) -> bytes:
    root = source_root.resolve()
    process = subprocess.run(
        [
            "git",
            "-C",
            os.fspath(root),
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace").strip())
    chunks = [process.stdout]
    seen: set[str] = set()
    for raw_relative in include_untracked:
        relative = Path(raw_relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"untracked patch path must be relative: {relative}")
        normalized = relative.as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise FileNotFoundError(f"untracked patch file not found: {candidate}")
        tracked = subprocess.run(
            ["git", "-C", os.fspath(root), "ls-files", "--error-unmatch", "--", normalized],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if tracked.returncode == 0:
            # Tracked and staged additions are already included by `diff HEAD`.
            continue
        untracked = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", os.devnull, normalized],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if untracked.returncode not in {0, 1}:
            raise RuntimeError(untracked.stderr.decode("utf-8", errors="replace").strip())
        if not untracked.stdout:
            raise RuntimeError(f"failed to generate patch for untracked file: {normalized}")
        chunks.append(untracked.stdout)
    return _normalize_patch(b"".join(chunks))


def sync_worktree_patch(
    source_root: Path,
    patch_path: Path,
    *,
    expected_head: str,
    include_untracked: Sequence[Path] = (),
) -> dict[str, object]:
    head = _run_read_only(
        ["git", "-C", os.fspath(source_root.resolve()), "rev-parse", "HEAD"]
    )
    if not head.get("ok") or head.get("stdout") != expected_head:
        raise RuntimeError(
            f"refusing to sync patch: expected HEAD {expected_head}, got {head.get('stdout') or head.get('error')}"
        )
    payload = _worktree_diff(source_root, include_untracked=include_untracked)
    if payload == b"\n":
        raise RuntimeError("refusing to replace the patch with an empty worktree diff")
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = patch_path.with_name(f".{patch_path.name}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, patch_path)
    return compare_worktree_patch(
        source_root,
        patch_path,
        include_untracked=include_untracked,
    )


JIEGARDEN_BEGIN_RECOVERY_ROUTES = (
    "JieGarden@Roguelike@Continue",
    "JieGarden@Roguelike@MissionFailedFlag2",
    "JieGarden@Roguelike@GamePass",
    "JieGarden@RoguelikeSettlementConfirm",
    "JieGarden@Roguelike@OperationFailed",
    "JieGarden@Roguelike@Stages#next",
    "JieGarden@Roguelike@GetDrops#next",
    "JieGarden@Roguelike@EnterAfterRecruit",
    "JieGarden@Roguelike@NextLevel",
    "JieGarden@Roguelike@NextLevel#next",
)


def audit_roguelike_recovery_routes(resource: Mapping[str, object]) -> list[str]:
    """Validate the screens from which a guarded JieGarden run must resume."""

    failures: list[str] = []
    begin = _as_dict(resource.get("JieGarden@Roguelike@Begin"))
    begin_next = begin.get("next")
    if not isinstance(begin_next, list) or not begin_next:
        return ["JieGarden Begin.next is missing"]
    if len(begin_next) != len(set(str(item) for item in begin_next)):
        failures.append("JieGarden Begin.next contains duplicate recovery routes")
    for route in JIEGARDEN_BEGIN_RECOVERY_ROUTES:
        if route not in begin_next:
            failures.append(f"JieGarden Begin.next no longer resumes from {task_suffix(route)}")
    if begin_next[-1] != "JieGarden@Roguelike@ExitThenAbandon":
        failures.append("ExitThenAbandon must remain the final Begin fallback")
    continue_route = "JieGarden@Roguelike@Continue"
    if continue_route in begin_next and begin_next.index(continue_route) > 2:
        failures.append("Continue must remain near the front of Begin.next")
    settlement_page = "JieGarden@RoguelikeSettlementConfirm"
    for broad_flag in (
        "JieGarden@Roguelike@MissionFailedFlag2",
        "JieGarden@Roguelike@GamePass",
    ):
        if (
            settlement_page in begin_next
            and broad_flag in begin_next
            and begin_next.index(settlement_page) > begin_next.index(broad_flag)
        ):
            failures.append("final settlement-page recovery must precede broad pass/fail templates")
    navigation_route = "JieGarden@Roguelike@ChooseDifficulty"
    if navigation_route in begin_next:
        for settlement_route in (
            "JieGarden@Roguelike@MissionFailedFlag2",
            "JieGarden@Roguelike@GamePass",
            "JieGarden@RoguelikeSettlementConfirm",
            "JieGarden@Roguelike@OperationFailed",
        ):
            if settlement_route in begin_next and begin_next.index(settlement_route) > begin_next.index(navigation_route):
                failures.append(f"{task_suffix(settlement_route)} must be checked before new-run navigation")
    return failures


def audit_maa_source(source_root: Path) -> dict[str, object]:
    root = source_root.resolve()
    failures: list[str] = []

    controller_path = root / "src" / "MaaCore" / "Controller" / "Controller.cpp"
    proxy_path = root / "src" / "MaaCore" / "Controller" / "ControlScaleProxy.cpp"
    process_path = root / "src" / "MaaCore" / "Task" / "ProcessTask.cpp"
    battle_path = root / "src" / "MaaCore" / "Task" / "BattleHelper.cpp"
    rogue_battle_path = (
        root
        / "src"
        / "MaaCore"
        / "Task"
        / "Roguelike"
        / "RoguelikeBattleTaskPlugin.cpp"
    )
    settlement_path = (
        root
        / "src"
        / "MaaCore"
        / "Task"
        / "Roguelike"
        / "RoguelikeSettlementTaskPlugin.cpp"
    )
    depot_path = (
        root
        / "src"
        / "MaaCore"
        / "Task"
        / "Miscellaneous"
        / "DepotRecognitionTask.cpp"
    )
    resource_path = root / "resource" / "tasks" / "Roguelike" / "JieGarden.json"
    base_resource_path = root / "resource" / "tasks" / "Roguelike" / "base.json"
    required_paths = (
        controller_path,
        proxy_path,
        process_path,
        battle_path,
        rogue_battle_path,
        settlement_path,
        depot_path,
        resource_path,
        base_resource_path,
    )
    for path in required_paths:
        if not path.is_file():
            failures.append(f"required source file is missing: {path.relative_to(root)}")
    if failures:
        return {"valid": False, "source_root": os.fspath(root), "failures": failures}

    controller = controller_path.read_text(encoding="utf-8")
    proxy = proxy_path.read_text(encoding="utf-8")
    process = process_path.read_text(encoding="utf-8")
    battle = battle_path.read_text(encoding="utf-8")
    rogue_battle = rogue_battle_path.read_text(encoding="utf-8")
    settlement = settlement_path.read_text(encoding="utf-8")
    depot = depot_path.read_text(encoding="utf-8")
    if "return m_scale_proxy->inject_input_event(event);" not in controller:
        failures.append("Controller::inject_input_event no longer routes through ControlScaleProxy")
    if "event.point = m_viewport_transform.logical_to_display(event.point);" not in proxy:
        failures.append("ControlScaleProxy no longer transforms coordinate-bearing input events")
    if "m_cache_image(cv::Rect(viewport.x, viewport.y, viewport.width, viewport.height))" not in controller:
        failures.append("recognition screenshot no longer crops the active viewport")
    if "analyze_image(candidate.image, list)" not in process or "single_task" in process:
        failures.append("adaptive recognition is no longer bounded to one pass per viewport")
    if (
        "ViewportAlignment::Left" not in battle
        or "ViewportAlignment::Right" not in battle
        or "set_viewport_alignment(ViewportAlignment::Center)" not in rogue_battle
    ):
        failures.append("battle code no longer contains an explicit Center viewport invariant")
    if (
        "DepotRecognitionTask::analyze_basic_items" not in depot
        or "set_viewport_alignment(ViewportAlignment::Right)" not in depot
        or "set_viewport_alignment(ViewportAlignment::Left)" not in depot
    ):
        failures.append("depot basic-item recognition no longer uses Right-tab/Left-content viewports")
    if 'm_config->get_theme() + "@RoguelikeSettlementOcr-" + task_name' not in settlement:
        failures.append("settlement battle statistics no longer use theme-specific OCR tasks")
    if (
        "set_viewport_alignment(ViewportAlignment::Center)" not in settlement
        or "restore_original_alignment" not in settlement
    ):
        failures.append("settlement OCR no longer fixes Center alignment and restores the caller viewport")
    if (
        'task_name.ends_with("RoguelikeSettlementConfirm")' not in settlement
        or 'json_msg["details"]["resumed_page2"] = true' not in settlement
    ):
        failures.append("settlement plugin no longer resumes directly from the final statistics page")

    resource = json.loads(resource_path.read_text(encoding="utf-8"))
    base_resource = json.loads(base_resource_path.read_text(encoding="utf-8"))
    failures.extend(audit_roguelike_recovery_routes(resource))
    continued = _as_dict(resource.get("JieGarden@Roguelike@Continue"))
    if "JieGarden@Roguelike@Begin" not in (continued.get("next") or []):
        failures.append("Continue no longer reconnects to Begin")
    trader = _as_dict(resource.get("JieGarden@Roguelike@StageTrader"))
    if trader.get("templThreshold") != 0.68:
        failures.append("StageTrader threshold drifted from the physical-device value 0.68")
    drops = _as_dict(resource.get("JieGarden@Roguelike@DropsFlag_default"))
    drops_next = drops.get("next")
    if not isinstance(drops_next, list):
        failures.append("DropsFlag_default.next is missing")
    else:
        strategy = "JieGarden@Roguelike@StrategyChange"
        stages = "JieGarden@Roguelike@Stages#next"
        if strategy not in drops_next or stages not in drops_next or drops_next.index(strategy) > drops_next.index(stages):
            failures.append("StrategyChange must be checked before the normal Stages route")
        enter_after_recruit = "JieGarden@Roguelike@EnterAfterRecruit"
        if enter_after_recruit not in drops_next:
            failures.append("DropsFlag_default no longer handles the post-recruit entry screen")

    enter_after_recruit_task = _as_dict(resource.get("JieGarden@Roguelike@EnterAfterRecruit"))
    if enter_after_recruit_task.get("template") != "JieGarden@Roguelike@EnterAfterRecruit.png":
        failures.append("EnterAfterRecruit must use its dedicated visual template")

    integrated_strategies = _as_dict(base_resource.get("Roguelike@IntegratedStrategies"))
    if integrated_strategies.get("roi") != [884, 606, 250, 114]:
        failures.append("IntegratedStrategies ROI no longer covers the ultrawide terminal icon")

    settlement_rois = {
        "Floor": [490, 170, 90, 40],
        "Step": [490, 238, 90, 40],
        "Combat": [490, 304, 90, 40],
        "Recruit": [490, 374, 90, 40],
        "Collection": [1060, 170, 90, 40],
        "BOSS": [1060, 238, 90, 40],
        "Emergency": [1060, 304, 90, 40],
    }
    for field, roi in settlement_rois.items():
        task = _as_dict(resource.get(f"JieGarden@RoguelikeSettlementOcr-{field}"))
        if task.get("roi") != roi:
            failures.append(f"JieGarden settlement {field} ROI drifted from the verified Center layout")

    return {
        "valid": not failures,
        "source_root": os.fspath(root),
        "checks": 28,
        "failures": failures,
    }


def _run_read_only(command: Sequence[str], timeout: float = 8.0) -> dict[str, object]:
    try:
        process = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }


def collect_environment_manifest(
    *,
    core_path: Path,
    runtime_root: Path,
    adb_path: Path,
    address: str,
    viewport: str,
    policy_path: Path,
    maa_source_root: Path | None = None,
    probe_device: bool = True,
) -> dict[str, object]:
    resource_path = runtime_root / "resource" / "tasks" / "Roguelike" / "JieGarden.json"
    manifest: dict[str, object] = {
        "schema_version": 1,
        "captured_at": _now_iso(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
        },
        "core": {
            "path": os.fspath(core_path.resolve()),
            "sha256": sha256_file(core_path),
        },
        "runtime": {
            "path": os.fspath(runtime_root.resolve()),
            "jiegarden_resource": {
                "path": os.fspath(resource_path.resolve()),
                "sha256": sha256_file(resource_path) if resource_path.is_file() else None,
            },
        },
        "adb": {
            "path": os.fspath(adb_path.resolve()),
            "sha256": sha256_file(adb_path),
        },
        "device": {"address": address},
        "viewport": viewport,
        "guard_policy": {
            "path": os.fspath(policy_path.resolve()),
            "sha256": sha256_file(policy_path),
        },
    }
    if maa_source_root:
        source = maa_source_root.resolve()
        manifest["maa_source"] = {
            "path": os.fspath(source),
            "head": _run_read_only(["git", "-C", os.fspath(source), "rev-parse", "HEAD"]),
            "status": _run_read_only(["git", "-C", os.fspath(source), "status", "--short"]),
        }
    if probe_device:
        prefix = [os.fspath(adb_path.resolve()), "-s", address]
        manifest["device"] = {
            "address": address,
            "state": _run_read_only([*prefix, "get-state"]),
            "wm_size": _run_read_only([*prefix, "shell", "wm", "size"]),
            "model": _run_read_only([*prefix, "shell", "getprop", "ro.product.model"]),
            "build_fingerprint": _run_read_only(
                [*prefix, "shell", "getprop", "ro.build.fingerprint"]
            ),
        }
    return manifest


def recent_event_buffer(limit: int = 80) -> deque[dict[str, object]]:
    return deque(maxlen=max(1, limit))
