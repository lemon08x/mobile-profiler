from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .models import ClockSyncPoint, ExternalEvent


@dataclass
class _PendingEvent:
    event: ExternalEvent
    key: str


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def host_epoch_to_device_uptime(
    host_epoch_s: float,
    sync_points: Sequence[ClockSyncPoint],
) -> float:
    if not sync_points:
        raise RuntimeError("clock-sync.jsonl is empty; host logs cannot be aligned")
    ordered = sorted(sync_points, key=lambda item: item.host_epoch_s)
    offsets = [item.host_epoch_s - item.device_uptime_s for item in ordered]
    if len(ordered) == 1 or host_epoch_s <= ordered[0].host_epoch_s:
        return host_epoch_s - offsets[0]
    if host_epoch_s >= ordered[-1].host_epoch_s:
        return host_epoch_s - offsets[-1]
    for left, right, left_offset, right_offset in zip(
        ordered,
        ordered[1:],
        offsets,
        offsets[1:],
    ):
        if left.host_epoch_s <= host_epoch_s <= right.host_epoch_s:
            span = right.host_epoch_s - left.host_epoch_s
            ratio = (host_epoch_s - left.host_epoch_s) / span if span > 0 else 0.0
            offset = left_offset + (right_offset - left_offset) * ratio
            return host_epoch_s - offset
    return host_epoch_s - offsets[-1]


def _timezone_from_value(value: object) -> tzinfo:
    text = str(value or "local").strip()
    if text.lower() == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    if text.upper() in {"UTC", "Z"}:
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if match:
        minutes = int(match.group(2)) * 60 + int(match.group(3))
        if match.group(1) == "-":
            minutes = -minutes
        return timezone(timedelta(minutes=minutes))
    try:
        return ZoneInfo(text)
    except Exception as exc:
        raise RuntimeError(f"unknown timezone in log rules: {text}") from exc


def _parse_timestamp(
    value: str,
    formats: Sequence[str],
    default_timezone: tzinfo,
) -> float:
    stripped = value.strip()
    for timestamp_format in formats:
        try:
            parsed = datetime.strptime(stripped, timestamp_format)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=default_timezone)
            return parsed.timestamp()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"timestamp {stripped!r} did not match configured formats") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.timestamp()


def _render_template(value: object, fields: Dict[str, str], fallback: str) -> str:
    template = str(value if value is not None else fallback)
    return template.format_map(_TemplateValues(fields))


def import_timestamped_log(
    log_path: Path,
    rules_path: Path,
    sync_points: Sequence[ClockSyncPoint],
    session_end_uptime_s: Optional[float] = None,
) -> Tuple[List[ExternalEvent], Dict[str, object]]:
    config = json.loads(rules_path.read_text(encoding="utf-8"))
    timestamp_config = config.get("timestamp")
    if not isinstance(timestamp_config, dict):
        raise RuntimeError("log rules require a timestamp object")
    timestamp_regex = timestamp_config.get("regex")
    if not timestamp_regex:
        raise RuntimeError("log rules require timestamp.regex")
    timestamp_pattern = re.compile(str(timestamp_regex))
    raw_formats = timestamp_config.get("formats", [])
    if isinstance(raw_formats, str):
        raw_formats = [raw_formats]
    formats = [str(value) for value in raw_formats]
    default_timezone = _timezone_from_value(timestamp_config.get("timezone", "local"))

    compiled_rules: List[Tuple[Dict[str, object], re.Pattern[str]]] = []
    for index, raw_rule in enumerate(config.get("rules", [])):
        if not isinstance(raw_rule, dict) or not raw_rule.get("regex"):
            raise RuntimeError(f"log rule {index} requires regex")
        kind = str(raw_rule.get("kind", "instant"))
        if kind not in {"start", "end", "instant", "state"}:
            raise RuntimeError(f"log rule {index} has unsupported kind {kind!r}")
        compiled_rules.append((raw_rule, re.compile(str(raw_rule["regex"]))))
    if not compiled_rules:
        raise RuntimeError("log rules contain no event rules")

    events: List[ExternalEvent] = []
    pending_starts: Dict[Tuple[str, str], List[_PendingEvent]] = {}
    active_states: Dict[str, _PendingEvent] = {}
    matched_lines = 0
    timestamped_lines = 0
    unmatched_ends = 0
    parse_errors: List[str] = []

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line_number, line in enumerate(lines, 1):
        timestamp_match = timestamp_pattern.search(line)
        if not timestamp_match:
            continue
        timestamp_value = (
            timestamp_match.groupdict().get("timestamp")
            or (timestamp_match.group(1) if timestamp_match.groups() else timestamp_match.group(0))
        )
        try:
            host_epoch_s = _parse_timestamp(timestamp_value, formats, default_timezone)
        except RuntimeError as exc:
            if len(parse_errors) < 10:
                parse_errors.append(f"line {line_number}: {exc}")
            continue
        timestamped_lines += 1
        device_uptime_s = host_epoch_to_device_uptime(host_epoch_s, sync_points)

        for rule, pattern in compiled_rules:
            match = pattern.search(line)
            if not match:
                continue
            matched_lines += 1
            fields = {key: str(value) for key, value in match.groupdict().items() if value is not None}
            fields.update({"line": line, "line_number": str(line_number)})
            phase = _render_template(rule.get("phase"), fields, "event")
            name = _render_template(rule.get("name"), fields, phase)
            key = _render_template(rule.get("key"), fields, name)
            kind = str(rule.get("kind", "instant"))
            source = str(rule.get("source") or f"log:{log_path.name}")
            metadata: Dict[str, object] = {
                "line_number": line_number,
                "log_file": str(log_path),
                "rule_regex": str(rule.get("regex")),
                **fields,
            }
            event = ExternalEvent(
                device_uptime_s=device_uptime_s,
                host_epoch_s=host_epoch_s,
                name=name,
                phase=phase,
                kind=kind,
                source=source,
                metadata=metadata,
            )
            pending = _PendingEvent(event=event, key=key)

            if kind == "start":
                pending_starts.setdefault((phase, key), []).append(pending)
            elif kind == "end":
                candidates = pending_starts.get((phase, key), [])
                if candidates:
                    started = candidates.pop()
                    started.event.kind = "span"
                    started.event.duration_s = max(
                        0.0, device_uptime_s - started.event.device_uptime_s
                    )
                    started.event.metadata["end_line_number"] = line_number
                    events.append(started.event)
                else:
                    unmatched_ends += 1
                    events.append(event)
            elif kind == "state":
                previous = active_states.get(phase)
                if previous is not None:
                    previous.event.duration_s = max(
                        0.0, device_uptime_s - previous.event.device_uptime_s
                    )
                    events.append(previous.event)
                active_states[phase] = pending
            else:
                events.append(event)
            if not bool(rule.get("continue", False)):
                break

    unmatched_starts = 0
    for candidates in pending_starts.values():
        for pending in candidates:
            unmatched_starts += 1
            events.append(pending.event)
    for pending in active_states.values():
        if session_end_uptime_s is not None and session_end_uptime_s > pending.event.device_uptime_s:
            pending.event.duration_s = session_end_uptime_s - pending.event.device_uptime_s
        events.append(pending.event)

    events.sort(key=lambda item: item.device_uptime_s)
    stats = {
        "line_count": len(lines),
        "timestamped_line_count": timestamped_lines,
        "matched_line_count": matched_lines,
        "event_count": len(events),
        "span_count": sum(1 for item in events if item.duration_s),
        "unmatched_start_count": unmatched_starts,
        "unmatched_end_count": unmatched_ends,
        "timestamp_errors": parse_errors,
        "rules": str(rules_path),
        "log": str(log_path),
    }
    return events, stats
