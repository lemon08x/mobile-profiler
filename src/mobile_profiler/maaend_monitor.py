"""Structured MaaEnd runtime evidence and watchdog state."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Mapping, Sequence

from .maa_iteration import IncidentSignal


_MAA_EVENT_PATTERN = re.compile(
    r"\[message=(?P<message>[^\]]+)\].*?"
    r"\[details_json=(?P<details>\{.*\})\]\s*"
    r"\[trans_arg="
)
_NODE_MESSAGE_PATTERN = re.compile(
    r"Node\.(?P<event>PipelineNode|Recognition|Action)\."
    r"(?P<status>Starting|Succeeded|Failed)"
)
_TASK_MESSAGE_PATTERN = re.compile(r"Tasker\.Task\.(Starting|Succeeded|Failed)")
_BACKEND_SWITCH_MARKER = "Switched active screencap method after runtime failure"
_VIEWPORT_GATE_MARKER = "landscape screenshot viewport activated"


def _as_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_signature(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_maaend_log_line(line: str) -> list[dict[str, object]]:
    """Extract only audit-safe semantic fields from Framework and Agent logs."""

    text = line.strip()
    if not text:
        return []
    events: list[dict[str, object]] = []

    parsed_json: dict[str, object] = {}
    if text.startswith("{") and text.endswith("}"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            parsed_json = value

    if parsed_json:
        message = str(parsed_json.get("message") or "")
        compact: dict[str, object] = {
            "kind": "agent_log",
            "message": message,
        }
        for key in (
            "level",
            "task_id",
            "entry",
            "name",
            "component",
            "alignment",
            "frame_id",
            "phase",
            "contact",
            "reversed",
            "use_cache",
            "stay_on_current_screen",
            "allow_unknown",
            "raw_width",
            "raw_height",
            "logical_width",
            "logical_height",
            "viewport_x",
            "viewport_y",
            "viewport_width",
            "viewport_height",
            "adaptive",
        ):
            if key in parsed_json:
                compact[key] = parsed_json[key]
        if str(parsed_json.get("component") or "") == "captureuid":
            uid_hash = str(parsed_json.get("uid") or "")
            if uid_hash:
                compact["uid_hash_present"] = uid_hash != "unknown"
                compact["uid_hash_sha256"] = hashlib.sha256(
                    uid_hash.encode("utf-8")
                ).hexdigest()
        if _VIEWPORT_GATE_MARKER in message:
            compact["kind"] = "viewport_activated"
            compact["active"] = True
            compact["gate_observed"] = True
        elif "viewport" in message.casefold() and not parsed_json.get("component"):
            compact["kind"] = "viewport_event"
        events.append(compact)

    match = _MAA_EVENT_PATTERN.search(text)
    if match:
        try:
            details = json.loads(match.group("details"))
        except json.JSONDecodeError:
            details = {}
        if isinstance(details, dict):
            message = match.group("message")
            event: dict[str, object] = {
                "kind": "framework_event",
                "message": message,
            }
            name = details.get("name") or details.get("task") or details.get("entry")
            if name:
                event["name"] = str(name)
            for key in (
                "task_id",
                "node_id",
                "reco_id",
                "action_id",
                "ctrl_id",
                "uuid",
            ):
                if key in details:
                    event[key] = details[key]
            node_match = _NODE_MESSAGE_PATTERN.fullmatch(message)
            if node_match:
                status = node_match.group("status").casefold()
                event_name = {
                    "PipelineNode": "node",
                    "Recognition": "recognition",
                    "Action": "action",
                }[node_match.group("event")]
                status_name = {
                    "starting": "start",
                    "succeeded": "succeeded",
                    "failed": "failed",
                }[status]
                event["kind"] = f"{event_name}_{status_name}"
                event["status"] = status
            task_match = _TASK_MESSAGE_PATTERN.fullmatch(message)
            if task_match:
                status = task_match.group(1).casefold()
                event["kind"] = {
                    "starting": "task_started",
                    "succeeded": "task_succeeded",
                    "failed": "task_failed",
                }[status]
                event["status"] = status
                event["entry"] = str(details.get("entry") or "")
                if "task_id" in details:
                    event["task_id"] = details["task_id"]
                if "uuid" in details:
                    event["uuid"] = details["uuid"]
            if message.startswith("Controller.Action."):
                event["kind"] = "controller_action"
                event["status"] = message.rsplit(".", 1)[-1].casefold()
                event["action"] = str(details.get("action") or "")
                event["param"] = _as_dict(details.get("param"))
                event["viewport"] = _as_dict(details.get("viewport"))
                info = _as_dict(details.get("info"))
                event["controller_info"] = {
                    "type": info.get("type"),
                    "screenshot_viewport": _as_dict(info.get("screenshot_viewport")),
                }
            recognition = _as_dict(details.get("reco_details"))
            if recognition:
                event["recognition"] = {
                    "name": recognition.get("name"),
                    "algorithm": recognition.get("algorithm"),
                    "box": recognition.get("box"),
                    "alignment": recognition.get("viewport_alignment"),
                    "frame_id": recognition.get("frame_id"),
                }
            events.append(event)

    if _BACKEND_SWITCH_MARKER in text:
        events.append(
            {
                "kind": "screencap_backend_switch",
                "message": _BACKEND_SWITCH_MARKER,
            }
        )
    if _VIEWPORT_GATE_MARKER in text and not any(
        row.get("kind") == "viewport_activated" for row in events
    ):
        events.append(
            {
                "kind": "viewport_activated",
                "message": _VIEWPORT_GATE_MARKER,
                "active": True,
                "gate_observed": True,
            }
        )
    return events


class MaaEndRunMonitor:
    """Track semantic progress without treating process liveness as success."""

    def __init__(
        self,
        watchdogs: Mapping[str, object],
        *,
        now: float | None = None,
        event_limit: int = 200,
    ) -> None:
        self.watchdogs = dict(watchdogs)
        current = time.monotonic() if now is None else now
        self.started_at = current
        self.last_progress_at = current
        self.last_state_signature = ""
        self.last_node = ""
        self.same_node_starts = 0
        self.same_node_first_started_at: float | None = None
        self.screenshot_count = 0
        self.screenshot_durations: list[float] = []
        self.last_screenshot_sha256 = ""
        self.same_screenshot_count = 0
        self.consecutive_slow_screenshots = 0
        self.backend_switch_count = 0
        self.active_contacts: dict[int, float] = {}
        self.viewport_evidence: dict[str, object] = {}
        self.alignment_trace: deque[dict[str, object]] = deque(maxlen=event_limit)
        self.events: deque[dict[str, object]] = deque(maxlen=event_limit)
        self.signals: list[IncidentSignal] = []
        self._emitted: set[str] = set()
        self._log_offsets: dict[str, int] = {}
        self._seen_framework_events: set[tuple[object, ...]] = set()

    def _emit(self, signal: IncidentSignal) -> IncidentSignal | None:
        if signal.fingerprint in self._emitted:
            return None
        self._emitted.add(signal.fingerprint)
        self.signals.append(signal)
        return signal

    def emit_signal(self, signal: IncidentSignal) -> IncidentSignal | None:
        return self._emit(signal)

    def mark_log_baseline(self, paths: Sequence[Path]) -> None:
        for path in paths:
            try:
                self._log_offsets[str(path.resolve())] = path.stat().st_size
            except OSError:
                self._log_offsets[str(path.resolve())] = 0

    def read_new_log_lines(
        self,
        paths: Sequence[Path],
        *,
        maximum_bytes: int = 2 * 1024 * 1024,
    ) -> list[str]:
        lines: list[str] = []
        for raw_path in paths:
            path = raw_path.resolve()
            key = str(path)
            offset = self._log_offsets.get(key, 0)
            try:
                size = path.stat().st_size
                if size < offset:
                    offset = 0
                start = max(offset, size - maximum_bytes)
                with path.open("rb") as stream:
                    stream.seek(start)
                    payload = stream.read(maximum_bytes)
                self._log_offsets[key] = size
            except OSError:
                self._log_offsets.setdefault(key, 0)
                continue
            text = payload.decode("utf-8", errors="replace")
            if start > offset:
                text = text.partition("\n")[2]
            lines.extend(text.splitlines())
        return lines

    def ingest_log_lines(
        self,
        lines: Sequence[str],
        *,
        now: float | None = None,
    ) -> list[IncidentSignal]:
        emitted: list[IncidentSignal] = []
        current = time.monotonic() if now is None else now
        for line in lines:
            for event in parse_maaend_log_line(line):
                event = dict(event)
                if self._is_duplicate_framework_event(event):
                    continue
                event["observed_monotonic"] = current
                self.events.append(event)
                kind = str(event.get("kind") or "")
                if kind == "node_start":
                    name = str(event.get("name") or "")
                    if name and name == self.last_node:
                        self.same_node_starts += 1
                        if self.same_node_first_started_at is None:
                            self.same_node_first_started_at = current
                    else:
                        self.last_node = name
                        self.same_node_starts = 1
                        self.same_node_first_started_at = current
                        self.last_progress_at = current
                    limit = int(self.watchdogs.get("same_node_start_limit") or 0)
                    minimum_seconds = float(
                        self.watchdogs.get("same_node_loop_seconds") or 0
                    )
                    loop_seconds = max(
                        0.0,
                        current
                        - (
                            self.same_node_first_started_at
                            if self.same_node_first_started_at is not None
                            else current
                        ),
                    )
                    if (
                        limit
                        and self.same_node_starts >= limit
                        and loop_seconds >= minimum_seconds
                    ):
                        signal = IncidentSignal.create(
                            "maaend_node_loop",
                            f"MaaEnd node {name or '<unknown>'} started "
                            f"{self.same_node_starts} consecutive times over "
                            f"{loop_seconds:.1f} seconds",
                            {
                                "node": name,
                                "limit": limit,
                                "minimum_seconds": minimum_seconds,
                                "observed_seconds": round(loop_seconds, 3),
                            },
                            stop=True,
                        )
                        unique = self._emit(signal)
                        if unique:
                            emitted.append(unique)
                elif kind in {
                    "node_succeeded",
                    "recognition_succeeded",
                    "action_succeeded",
                    "task_started",
                    "task_succeeded",
                }:
                    self.last_progress_at = current
                    recognition = _as_dict(event.get("recognition"))
                    alignment = str(recognition.get("alignment") or "")
                    if kind == "node_succeeded" and alignment in {
                        "left",
                        "center",
                        "right",
                    }:
                        trace = {
                            "kind": "recognition",
                            "node": event.get("name"),
                            "alignment": alignment,
                            "frame_id": recognition.get("frame_id"),
                            "box": recognition.get("box"),
                            "observed_monotonic": current,
                        }
                        self.alignment_trace.append(trace)
                        self.viewport_evidence.update(
                            {
                                "alignment": alignment,
                                "frame_id": recognition.get("frame_id"),
                            }
                        )
                elif kind == "screencap_backend_switch":
                    self.backend_switch_count += 1
                    limit = int(
                        self.watchdogs.get("screenshot_backend_switch_limit") or 0
                    )
                    if limit and self.backend_switch_count >= limit:
                        signal = IncidentSignal.create(
                            "maaend_screencap_failover_loop",
                            "MaaEnd screenshot backend switched repeatedly",
                            {
                                "switch_count": self.backend_switch_count,
                                "limit": limit,
                            },
                            stop=True,
                        )
                        unique = self._emit(signal)
                        if unique:
                            emitted.append(unique)
                elif kind == "viewport_activated":
                    self.viewport_evidence.update(event)
                    self.viewport_evidence["active"] = True
                    self.last_progress_at = current
                elif kind == "viewport_event":
                    self.viewport_evidence.update(event)
                elif kind == "controller_action":
                    self._observe_controller_action(event, current)
                elif kind == "agent_log" and event.get("component"):
                    self.last_progress_at = current
        return emitted

    def _is_duplicate_framework_event(self, event: Mapping[str, object]) -> bool:
        if event.get("kind") in {"agent_log", "viewport_event", "viewport_activated"}:
            return False
        identifiers = tuple(
            event.get(key)
            for key in (
                "task_id",
                "node_id",
                "reco_id",
                "action_id",
                "ctrl_id",
                "uuid",
            )
        )
        if not any(value is not None for value in identifiers):
            return False
        key = (
            event.get("message"),
            event.get("name"),
            *identifiers,
        )
        if key in self._seen_framework_events:
            return True
        self._seen_framework_events.add(key)
        return False

    def _observe_controller_action(
        self,
        event: Mapping[str, object],
        current: float,
    ) -> None:
        if event.get("status") != "succeeded":
            return
        action = str(event.get("action") or "").casefold()
        param = _as_dict(event.get("param"))
        viewport = _as_dict(event.get("viewport"))
        controller_info = _as_dict(event.get("controller_info"))
        screenshot_viewport = _as_dict(controller_info.get("screenshot_viewport"))
        if screenshot_viewport:
            self.viewport_evidence.update(screenshot_viewport)
            if "active" in screenshot_viewport:
                self.viewport_evidence["active"] = screenshot_viewport["active"]
        if viewport:
            trace = {
                "action": action,
                "alignment": viewport.get("alignment"),
                "frame_id": viewport.get("frame_id"),
                "contact": param.get("contact"),
                "logical_param": viewport.get("logical_param"),
                "display_param": viewport.get("display_param"),
                "observed_monotonic": current,
            }
            self.alignment_trace.append(trace)
        try:
            contact = int(param.get("contact", 0))
        except (TypeError, ValueError):
            contact = 0
        normalized_action = action.replace("-", "_")
        if normalized_action == "touch_down":
            self.active_contacts[contact] = current
        elif normalized_action == "touch_up":
            self.active_contacts.pop(contact, None)
        if normalized_action in {
            "click",
            "long_press",
            "swipe",
            "multi_swipe",
            "touch_down",
            "touch_move",
            "touch_up",
            "scroll",
        }:
            self.last_progress_at = current
        if normalized_action == "stop_app":
            self.events.append(
                {
                    "kind": "stop_app_succeeded",
                    "observed_monotonic": current,
                }
            )

    def observe_state(
        self,
        state: Mapping[str, object],
        task_rows: Sequence[Mapping[str, object]],
        *,
        now: float | None = None,
    ) -> list[IncidentSignal]:
        current = time.monotonic() if now is None else now
        run_state = _as_dict(state.get("task_run_state"))
        semantic = {
            "overall_status": run_state.get("overall_status"),
            "current_task_index": run_state.get("current_task_index"),
            "statuses": {
                str(row.get("id") or ""): str(row.get("status") or "")
                for row in task_rows
            },
        }
        signature = _canonical_signature(semantic)
        if signature != self.last_state_signature:
            self.last_state_signature = signature
            self.last_progress_at = current
            self.events.append(
                {
                    "kind": "mxu_semantic_state",
                    "signature": signature,
                    "state": semantic,
                    "observed_monotonic": current,
                }
            )
            return []
        return self.poll(now=current)

    def poll(self, *, now: float | None = None) -> list[IncidentSignal]:
        current = time.monotonic() if now is None else now
        threshold = float(self.watchdogs.get("no_progress_seconds") or 0)
        if not threshold or current - self.last_progress_at < threshold:
            return []
        signal = IncidentSignal.create(
            "maaend_no_progress",
            "MaaEnd produced no semantic state or structured-event progress",
            {
                "last_node": self.last_node,
                "state_signature": self.last_state_signature,
                "threshold_seconds": threshold,
            },
            stop=True,
        )
        unique = self._emit(signal)
        return [unique] if unique else []

    def observe_screenshot(
        self,
        payload: bytes,
        duration_seconds: float,
        *,
        label: str,
    ) -> list[IncidentSignal]:
        self.screenshot_count += 1
        self.screenshot_durations.append(duration_seconds)
        emitted: list[IncidentSignal] = []
        slow_threshold = float(self.watchdogs.get("slow_screenshot_seconds") or 0)
        if slow_threshold and duration_seconds >= slow_threshold:
            self.consecutive_slow_screenshots += 1
        else:
            self.consecutive_slow_screenshots = 0
        slow_limit = int(
            self.watchdogs.get("slow_screenshot_consecutive_limit") or 0
        )
        if slow_limit and self.consecutive_slow_screenshots >= slow_limit:
            signal = IncidentSignal.create(
                "maaend_slow_screenshot",
                "MaaEnd controller screenshot remained slow",
                {
                    "threshold_seconds": slow_threshold,
                    "consecutive_limit": slow_limit,
                },
                stop=bool(self.watchdogs.get("stop_on_slow_screenshot", False)),
            )
            unique = self._emit(signal)
            if unique:
                emitted.append(unique)

        digest = hashlib.sha256(payload).hexdigest()
        if label.startswith("watchdog-"):
            if digest == self.last_screenshot_sha256:
                self.same_screenshot_count += 1
            else:
                self.last_screenshot_sha256 = digest
                self.same_screenshot_count = 1
            static_limit = int(self.watchdogs.get("static_snapshot_limit") or 0)
            if static_limit and self.same_screenshot_count >= static_limit:
                signal = IncidentSignal.create(
                    "maaend_static_screen",
                    "MaaEnd controller screenshot remained byte-identical",
                    {"image_sha256": digest, "limit": static_limit},
                    stop=True,
                )
                unique = self._emit(signal)
                if unique:
                    emitted.append(unique)
        return emitted

    def terminal_signals(self, *, now: float | None = None) -> list[IncidentSignal]:
        if not self.active_contacts:
            return []
        current = time.monotonic() if now is None else now
        grace = float(self.watchdogs.get("active_touch_grace_seconds") or 0)
        stale = [
            contact
            for contact, started in self.active_contacts.items()
            if not grace or current - started >= grace
        ]
        if not stale:
            stale = sorted(self.active_contacts)
        signal = IncidentSignal.create(
            "maaend_active_touch_leak",
            "MaaEnd reached a terminal state with active touch contacts",
            {"contacts": sorted(stale)},
            stop=True,
        )
        unique = self._emit(signal)
        return [unique] if unique else []

    def state(self) -> dict[str, object]:
        values = sorted(self.screenshot_durations)

        def percentile(ratio: float) -> float | None:
            if not values:
                return None
            index = min(
                len(values) - 1,
                max(0, int(round((len(values) - 1) * ratio))),
            )
            return round(values[index], 4)

        return {
            "last_progress_at": self.last_progress_at,
            "last_state_signature": self.last_state_signature,
            "last_node": self.last_node,
            "same_node_starts": self.same_node_starts,
            "same_node_first_started_at": self.same_node_first_started_at,
            "backend_switch_count": self.backend_switch_count,
            "active_contacts": sorted(self.active_contacts),
            "viewport_evidence": dict(self.viewport_evidence),
            "alignment_trace": list(self.alignment_trace),
            "events": list(self.events),
            "incidents": [signal.as_dict() for signal in self.signals],
            "screenshot_stats": {
                "count": len(values),
                "p50_seconds": percentile(0.5),
                "p95_seconds": percentile(0.95),
                "max_seconds": round(values[-1], 4) if values else None,
                "same_snapshot_count": self.same_screenshot_count,
            },
        }
