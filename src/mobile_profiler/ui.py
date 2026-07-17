from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import quote, unquote, urlparse

from . import __version__ as APP_VERSION
from .analysis import (
    analyze_brightness_throttling,
    analyze_memory_frequency,
    analyze_performance_contexts,
    analyze_runtime_settings,
    convert_samples,
)
from .collector import adb_connection_type, adb_shell, list_adb_devices, parse_sampler_line
from .evidence import create_evidence_archive
from .features import capture_feature_names, capture_preset_names
from .ios import DEFAULT_IOS_PYTHON, list_ios_devices, pair_ios_device
from .harmony import (
    DEFAULT_HDC,
    connect_harmony_device,
    enable_harmony_tcp,
    list_harmony_devices,
)
from .models import (
    ContextSample,
    CpuPolicy,
    GpuSource,
    MemorySource,
    RawSample,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
    IOS_SYSTEM_LOAD_STALE_AFTER_S,
    is_consumption_power_sample,
    is_power_sample_fresh_for_consumption,
)
from .messages import localize_collection_warning
from .storage import (
    REPORT_EDITS_FILENAME,
    REPORT_SOURCE_SAMPLES_FILENAME,
    load_contexts,
    load_report_excluded_ranges,
    read_samples_csv,
    write_report_excluded_ranges,
)


MAX_LIVE_POINTS = 900
MAX_LOG_LINES = 500
MAX_REQUEST_BYTES = 1_000_000
DEFAULT_UI_DURATION_S = 62 * 60
DEFAULT_PERFORMANCE_UI_DURATION_S = 32 * 60
ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
ANDROID_COMPONENT_RE = re.compile(
    r"^(?P<package>[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/(?P<activity>\S+)$"
)
ANDROID_BRIGHTNESS_SCALES = (255, 1023, 2047, 4095, 8191, 16383, 32767, 65535)


def _read_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _cpu_set_count(value: object) -> Optional[int]:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "unavailable", "unknown", "-"}:
        return None
    cores = set()
    for token in re.split(r"[\s,]+", text):
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            if left.isdigit() and right.isdigit():
                start, end = int(left), int(right)
                if 0 <= start <= end <= 4096:
                    cores.update(range(start, end + 1))
            continue
        if token.isdigit():
            cores.add(int(token))
    return len(cores) if cores else None


def _optional_float(value: object) -> Optional[float]:
    match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", str(value or ""))
    if not match:
        return None
    try:
        parsed = float(match.group(0))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_android_brightness_capability(
    current_text: str,
    float_text: str,
    mode_text: str,
    power_text: str,
    display_text: str = "",
    display_id: Optional[int] = None,
    display_dump_text: str = "",
) -> Dict[str, object]:
    current_number = _optional_float(current_text)
    if current_number is None or current_number < 0:
        raise RuntimeError("Android did not expose a valid screen_brightness value")
    current = int(round(current_number))
    current_float = _optional_float(float_text)
    minimum_match = re.search(
        r"mScreenBrightnessMinimum\s*=\s*([-+]?\d+(?:\.\d+)?)",
        power_text or "",
    )
    maximum_match = re.search(
        r"mScreenBrightnessMaximum\s*=\s*([-+]?\d+(?:\.\d+)?)",
        power_text or "",
    )
    normalized_minimum = (
        float(minimum_match.group(1)) if minimum_match else 0.0
    )
    normalized_maximum = (
        float(maximum_match.group(1)) if maximum_match else 1.0
    )
    if not math.isfinite(normalized_minimum):
        normalized_minimum = 0.0
    if not math.isfinite(normalized_maximum) or normalized_maximum <= normalized_minimum:
        normalized_maximum = 1.0

    inferred_ratio: Optional[float] = None
    if (
        current_float is not None
        and current_float > normalized_minimum
        and current > 0
    ):
        inferred_ratio = current / max(1e-9, current_float - normalized_minimum)
    if inferred_ratio is not None:
        integer_scale = min(
            ANDROID_BRIGHTNESS_SCALES,
            key=lambda candidate: abs(float(candidate) - inferred_ratio),
        )
        if abs(float(integer_scale) - inferred_ratio) / integer_scale > 0.12:
            integer_scale = next(
                (candidate for candidate in ANDROID_BRIGHTNESS_SCALES if candidate >= current),
                max(current, ANDROID_BRIGHTNESS_SCALES[-1]),
            )
    else:
        integer_scale = next(
            (candidate for candidate in ANDROID_BRIGHTNESS_SCALES if candidate >= current),
            max(current, ANDROID_BRIGHTNESS_SCALES[-1]),
        )

    raw_minimum_match = re.search(
        r"mScreenBrightnessRangeMinimum\s*=\s*([-+]?\d+(?:\.\d+)?)",
        display_dump_text or "",
    )
    raw_normal_maximum_match = re.search(
        r"mScreenBrightnessNormalMaximum\s*=\s*([-+]?\d+(?:\.\d+)?)",
        display_dump_text or "",
    )
    raw_maximum_match = re.search(
        r"mScreenBrightnessRangeMaximum\s*=\s*([-+]?\d+(?:\.\d+)?)",
        display_dump_text or "",
    )
    raw_minimum = (
        float(raw_minimum_match.group(1)) if raw_minimum_match else None
    )
    raw_maximum = (
        float(raw_normal_maximum_match.group(1))
        if raw_normal_maximum_match
        else float(raw_maximum_match.group(1))
        if raw_maximum_match
        else None
    )
    raw_range_available = (
        raw_minimum is not None
        and raw_maximum is not None
        and raw_maximum > max(1.0, raw_minimum)
    )
    if raw_range_available:
        minimum = max(0, int(round(raw_minimum)))
        maximum = max(minimum + 1, int(round(raw_maximum)), current)
        integer_scale = maximum
    else:
        minimum = max(0, int(round(normalized_minimum * integer_scale)))
        maximum = max(minimum + 1, int(round(normalized_maximum * integer_scale)), current)
    step = 1
    mode_number = _optional_float(mode_text)
    mode = int(round(mode_number)) if mode_number is not None else None
    normalized_step = (normalized_maximum - normalized_minimum) / max(1, maximum - minimum)
    normalized_current = (
        current_float
        if current_float is not None
        else normalized_minimum
        + (current - minimum) / max(1, maximum - minimum)
        * (normalized_maximum - normalized_minimum)
    )
    display_current = _optional_float(display_text)
    display_value_format = (
        "raw"
        if display_current is not None and display_current > normalized_maximum + 1e-6
        else "normalized"
        if display_current is not None
        else None
    )
    return {
        "supported": True,
        "current": current,
        "current_normalized": max(
            normalized_minimum,
            min(normalized_maximum, float(normalized_current)),
        ),
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
        "normalized_minimum": normalized_minimum,
        "normalized_maximum": normalized_maximum,
        "normalized_step": normalized_step,
        "mode": mode,
        "automatic": mode == 1,
        "display_id": display_id,
        "display_current": display_current,
        "display_value_format": display_value_format,
        "range_source": (
            "dumpsys display raw brightness range"
            if raw_range_available
            else "dumpsys power + screen_brightness_float"
            if current_float is not None
            else "dumpsys power + Android integer fallback"
        ),
    }


def _bounded_float(
    value: object,
    name: str,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    parsed = _number(value, default)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _bounded_int(
    value: object,
    name: str,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    parsed = _integer(value, default)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def sanitize_run_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now().strftime("mobile-profile-%Y%m%d-%H%M%S")
    normalized: List[str] = []
    for character in text:
        if character.isalnum() or character in {"-", "_", "."}:
            normalized.append(character)
        elif character.isspace():
            normalized.append("-")
        else:
            normalized.append("-")
    result = re.sub(r"-+", "-", "".join(normalized)).strip("-. ")
    if not result or result in {".", ".."}:
        return datetime.now().strftime("mobile-profile-%Y%m%d-%H%M%S")
    return result[:96]


def _reserve_unique_run_directory(output_root: Path, run_name: str) -> Path:
    sequence = 1
    while True:
        suffix = "" if sequence == 1 else f"-{sequence}"
        stem = run_name[: max(1, 96 - len(suffix))].rstrip("-. ")
        candidate = output_root / f"{stem}{suffix}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            sequence += 1
            continue
        return candidate


def parse_device_ipv4_addresses(text: str) -> List[Dict[str, object]]:
    addresses: List[Dict[str, object]] = []
    for line in (text or "").splitlines():
        match = re.search(
            r"^\s*\d+:\s+([^\s:]+).*?\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/\d+",
            line,
        )
        if not match:
            continue
        interface, value = match.groups()
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not isinstance(parsed, ipaddress.IPv4Address) or parsed.is_loopback or parsed.is_link_local:
            continue
        lower_interface = interface.lower()
        wifi = bool(re.match(r"^(?:wlan|wifi|swlan)", lower_interface))
        mobile = bool(re.match(r"^(?:rmnet|ccmni|pdp)", lower_interface))
        addresses.append(
            {
                "interface": interface,
                "address": value,
                "wifi": wifi,
                "mobile": mobile,
                "private": parsed.is_private,
                "priority": 0 if wifi else 2 if mobile else 1,
            }
        )
    addresses.sort(key=lambda item: (int(item["priority"]), str(item["interface"])))
    for item in addresses:
        item.pop("priority", None)
    return addresses


def parse_android_package_list(text: str) -> List[str]:
    packages = set()
    for line in (text or "").splitlines():
        value = line.strip()
        if value.startswith("package:"):
            value = value[len("package:") :].strip()
        if "=" in value:
            _, package = value.rsplit("=", 1)
            if ANDROID_PACKAGE_RE.fullmatch(package.strip()):
                value = package.strip()
        if ANDROID_PACKAGE_RE.fullmatch(value):
            packages.add(value)
    return sorted(packages, key=str.casefold)


def parse_android_package_paths(text: str) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("package:") or "=" not in line:
            continue
        path, package = line[len("package:") :].rsplit("=", 1)
        package = package.strip()
        path = path.strip()
        if ANDROID_PACKAGE_RE.fullmatch(package) and path.endswith(".apk"):
            paths[package] = path
    return paths


def parse_android_apk_icon_candidates(text: str) -> List[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    excluded = (
        ".9.png",
        "notification",
        "google_signin",
        "btn_",
        "button",
        "close",
        "delete",
        "retry",
        "loading",
        "rotate",
        "camera",
        "album",
        "qr_",
    )
    for raw_line in (text or "").splitlines():
        match = re.match(
            r"^\s*(?P<size>\d+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(?P<name>res/\S+)$",
            raw_line,
        )
        if match is None:
            continue
        size = int(match.group("size"))
        name = match.group("name")
        lowered = name.lower()
        if not lowered.endswith((".png", ".webp", ".jpg", ".jpeg")):
            continue
        if size < 512 or size > 750_000 or any(token in lowered for token in excluded):
            continue
        basename = lowered.rsplit("/", 1)[-1]
        score = 0
        if basename in {"ic_launcher.png", "ic_launcher.webp", "app_icon.png", "app_icon.webp"}:
            score += 220
        elif basename in {"icon.png", "icon.webp"}:
            score += 200
        elif "launcher" in basename:
            score += 150
        elif "app_icon" in basename or basename.startswith("icon"):
            score += 120
        elif "icon" in basename or "logo" in basename:
            score += 45
        else:
            continue
        density_scores = {
            "xxxhdpi": 50,
            "xxhdpi": 42,
            "xhdpi": 34,
            "hdpi": 26,
            "mdpi": 18,
        }
        score += next(
            (value for density, value in density_scores.items() if density in lowered),
            0,
        )
        score += min(35, int(math.log2(max(1, size))))
        candidates.append({"name": name, "size": size, "score": score})
    candidates.sort(
        key=lambda item: (int(item["score"]), int(item["size"])),
        reverse=True,
    )
    return candidates


def android_icon_data_uri(encoded: str, resource_name: str) -> Optional[str]:
    compact = re.sub(r"\s+", "", encoded or "")
    if not compact or len(compact) > 1_100_000:
        return None
    try:
        payload = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        return None
    mime = None
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        mime = "image/webp"
    elif payload.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    if mime is None or len(payload) < 512:
        return None
    return f"data:{mime};base64,{compact}"


def parse_android_launcher_activities(
    text: str,
    third_party_packages: Sequence[str] = (),
) -> List[Dict[str, object]]:
    third_party = set(third_party_packages)
    by_package: Dict[str, Dict[str, object]] = {}
    for line in (text or "").splitlines():
        value = line.strip()
        match = ANDROID_COMPONENT_RE.fullmatch(value)
        if match is None:
            continue
        package = match.group("package")
        activity = match.group("activity")
        component = f"{package}/{activity}"
        entry = by_package.get(package)
        if entry is None:
            entry = {
                "package": package,
                "activity": activity,
                "component": component,
                "activities": [],
                "user_app": package in third_party,
                "launchable": True,
            }
            by_package[package] = entry
        activities = entry["activities"]
        if isinstance(activities, list) and component not in activities:
            activities.append(component)
    return sorted(
        by_package.values(),
        key=lambda item: (
            not bool(item.get("user_app")),
            str(item.get("package") or "").casefold(),
        ),
    )


def _policies_from_metadata(metadata: Dict[str, object]) -> List[CpuPolicy]:
    policies: List[CpuPolicy] = []
    raw = metadata.get("cpu_policies", [])
    if not isinstance(raw, list):
        return policies
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            policies.append(CpuPolicy(**item))
        except TypeError:
            continue
    return policies


def _gpu_from_metadata(metadata: Dict[str, object]) -> Optional[GpuSource]:
    raw = metadata.get("gpu_source")
    if not isinstance(raw, dict):
        return None
    try:
        return GpuSource(**raw)
    except TypeError:
        return None


def _memory_from_metadata(metadata: Dict[str, object]) -> Optional[MemorySource]:
    raw = metadata.get("memory_source")
    if not isinstance(raw, dict):
        return None
    try:
        return MemorySource(**raw)
    except TypeError:
        return None


def _finite_timeline_value(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _distributed_positions(size: int) -> List[int]:
    """Return indexes in a centre-first, progressively distributed order."""

    if size <= 0:
        return []
    positions: List[int] = []
    pending = deque([(0, size)])
    while pending:
        start, end = pending.popleft()
        if start >= end:
            continue
        middle = (start + end) // 2
        positions.append(middle)
        pending.append((start, middle))
        pending.append((middle + 1, end))
    return positions


def _add_boundary_groups(
    selected: set[int],
    groups: Sequence[Sequence[int]],
    limit: int,
) -> None:
    if not groups or len(selected) >= limit:
        return
    all_indexes = {
        index
        for group in groups
        for index in group
        if index >= 0
    }
    if len(selected | all_indexes) <= limit:
        selected.update(all_indexes)
        return

    # When pathological input contains more boundaries than the point budget,
    # keep a time-distributed subset instead of silently favouring the start.
    for position in _distributed_positions(len(groups)):
        group = {index for index in groups[position] if index >= 0}
        cost = len(group - selected)
        if cost <= limit - len(selected):
            selected.update(group)


def _decimation_indexes(
    value_series: Sequence[Sequence[object]],
    length: int,
    limit: int,
    *,
    break_before: Sequence[bool] = (),
    step_values: Sequence[object] = (),
) -> List[int]:
    """Select a bounded set of timeline indexes without erasing short events.

    The selector keeps the first/last point, discontinuity boundaries and short
    step edges, then spends the remaining budget on bucket-local extrema.  The
    first value series is treated as the primary metric; for live samples that
    is power, while frame/refresh timelines contain only their displayed value.
    """

    if length <= 0 or limit <= 0:
        return []
    limit = min(length, int(limit))
    if limit == 1:
        return [length - 1]
    if length <= limit:
        return list(range(length))
    if limit == 2:
        return [0, length - 1]

    prepared_series: List[List[Optional[float]]] = []
    for series in value_series:
        if len(series) != length:
            continue
        prepared = [_finite_timeline_value(value) for value in series]
        if any(value is not None for value in prepared):
            prepared_series.append(prepared)

    selected = {0, length - 1}
    break_groups = [
        (index - 1, index)
        for index in range(1, min(length, len(break_before)))
        if bool(break_before[index])
    ]
    _add_boundary_groups(selected, break_groups, limit)

    if len(step_values) == length:
        numeric_steps = [_finite_timeline_value(value) for value in step_values]
        step_groups = []
        for index in range(1, length):
            previous = numeric_steps[index - 1]
            current = numeric_steps[index]
            if previous is None or current is None:
                continue
            tolerance = max(0.01, abs(previous) * 0.0001)
            if abs(current - previous) > tolerance:
                step_groups.append((index - 1, index))
        _add_boundary_groups(selected, step_groups, limit)

    remaining = limit - len(selected)
    if remaining <= 0:
        return sorted(selected)

    if not prepared_series:
        candidates = [index for index in range(1, length - 1) if index not in selected]
        if candidates:
            for offset in range(remaining):
                position = min(
                    len(candidates) - 1,
                    math.floor((offset + 0.5) * len(candidates) / remaining),
                )
                selected.add(candidates[position])
        return sorted(selected)

    global_ranges: List[tuple[float, float]] = []
    for series in prepared_series:
        values = [value for value in series if value is not None]
        global_ranges.append((min(values), max(values)))

    bucket_slots = min(6, max(2, len(prepared_series) * 2))
    bucket_count = max(1, remaining // bucket_slots)
    base_quota, extra_quota = divmod(remaining, bucket_count)

    for bucket in range(bucket_count):
        quota = base_quota + (1 if bucket < extra_quota else 0)
        start = math.floor(bucket * length / bucket_count)
        end = math.floor((bucket + 1) * length / bucket_count)
        bucket_indexes = [
            index
            for index in range(start, end)
            if 0 < index < length - 1 and index not in selected
        ]
        if not bucket_indexes or quota <= 0:
            continue

        candidates: Dict[int, tuple[int, float, int]] = {}
        for series_index, series in enumerate(prepared_series):
            values = [
                (index, series[index])
                for index in range(start, end)
                if series[index] is not None
            ]
            if not values:
                continue
            minimum_index, minimum_value = min(values, key=lambda item: float(item[1]))
            maximum_index, maximum_value = max(values, key=lambda item: float(item[1]))
            local_mean = statistics.fmean(float(item[1]) for item in values)
            global_minimum, global_maximum = global_ranges[series_index]
            scale = max(global_maximum - global_minimum, 1e-12)
            for candidate_index, candidate_value in {
                minimum_index: minimum_value,
                maximum_index: maximum_value,
            }.items():
                if candidate_index in selected:
                    continue
                local_salience = abs(float(candidate_value) - local_mean) / scale
                priority, score, nominations = candidates.get(
                    candidate_index,
                    (0, 0.0, 0),
                )
                is_primary = int(series_index == 0)
                candidates[candidate_index] = (
                    max(priority, is_primary),
                    max(score, local_salience),
                    nominations + 1,
                )

        bucket_middle = (start + end - 1) / 2.0

        def candidate_rank(index: int) -> tuple[int, float, int, float]:
            priority, score, nominations = candidates[index]
            return (
                priority,
                score,
                nominations,
                -abs(index - bucket_middle),
            )

        ordered_candidates = sorted(candidates, key=candidate_rank, reverse=True)
        added = 0
        for candidate_index in ordered_candidates:
            if added >= quota or len(selected) >= limit:
                break
            selected.add(candidate_index)
            added += 1

        if added < quota and len(selected) < limit:
            fill = [index for index in bucket_indexes if index not in selected]
            wanted = min(quota - added, limit - len(selected), len(fill))
            if wanted:
                for offset in range(wanted):
                    position = min(
                        len(fill) - 1,
                        math.floor((offset + 0.5) * len(fill) / wanted),
                    )
                    selected.add(fill[position])

    if len(selected) < limit:
        fill = [index for index in range(1, length - 1) if index not in selected]
        wanted = min(limit - len(selected), len(fill))
        for offset in range(wanted):
            position = min(
                len(fill) - 1,
                math.floor((offset + 0.5) * len(fill) / wanted),
            )
            selected.add(fill[position])
    return sorted(selected)


def _decimate_timeline_rows(
    rows: Sequence[Dict[str, object]],
    *,
    limit: int = MAX_LIVE_POINTS,
    value_keys: Sequence[str] = ("value",),
    preserve_steps: bool = False,
) -> List[Dict[str, object]]:
    if not rows or limit <= 0:
        return []
    value_series = [[row.get(key) for row in rows] for key in value_keys]
    breaks = [
        bool(
            row.get("report_break_before")
            or row.get("_report_break_before")
            or row.get("break_before")
        )
        for row in rows
    ]
    step_values = (
        next(
            (
                series
                for series in value_series
                if any(_finite_timeline_value(value) is not None for value in series)
            ),
            [],
        )
        if preserve_steps
        else []
    )
    indexes = _decimation_indexes(
        value_series,
        len(rows),
        limit,
        break_before=breaks,
        step_values=step_values,
    )

    displayed: List[Dict[str, object]] = []
    previous_index: Optional[int] = None
    for index in indexes:
        row = dict(rows[index])
        if previous_index is not None and any(breaks[previous_index + 1 : index + 1]):
            row["report_break_before"] = True
        displayed.append(row)
        previous_index = index
    return displayed


def _sample_break_flags(samples: Sequence[Sample]) -> List[bool]:
    breaks = [False]
    for previous, current in zip(samples, samples[1:]):
        breaks.append(
            previous.power_source != current.power_source
            or bool(getattr(current, "_report_break_before", False))
        )
    return breaks


def _decimate(samples: Sequence[Sample], limit: int = MAX_LIVE_POINTS) -> List[Sample]:
    if not samples or limit <= 0:
        return []
    if len(samples) <= limit:
        return list(samples)
    value_series: List[List[object]] = [
        [sample.power_mw for sample in samples],
        [sample.current_ma for sample in samples],
        [sample.voltage_mv for sample in samples],
        [sample.cpu_pct for sample in samples],
        [sample.gpu_load_pct for sample in samples],
        [sample.gpu_frequency_mhz for sample in samples],
        [sample.memory_frequency_mhz for sample in samples],
        [sample.battery_temperature_c for sample in samples],
    ]
    cluster_keys = sorted(
        {key for sample in samples for key in sample.cluster_cpu_pct}
    )
    frequency_keys = sorted(
        {key for sample in samples for key in sample.frequencies_mhz}
    )
    value_series.extend(
        [sample.cluster_cpu_pct.get(key) for sample in samples]
        for key in cluster_keys
    )
    value_series.extend(
        [sample.frequencies_mhz.get(key) for sample in samples]
        for key in frequency_keys
    )
    breaks = _sample_break_flags(samples)
    indexes = _decimation_indexes(
        value_series,
        len(samples),
        limit,
        break_before=breaks,
    )
    return [samples[index] for index in indexes]


def _sample_intervals(
    samples: Sequence[Sample],
    max_gap_s: float,
    *,
    require_consumption_power: bool = False,
) -> List[tuple[Sample, Sample, float]]:
    intervals: List[tuple[Sample, Sample, float]] = []
    for previous, current in zip(samples, samples[1:]):
        delta = current.uptime_s - previous.uptime_s
        if delta <= 0 or delta > max_gap_s:
            continue
        if bool(getattr(current, "_report_break_before", False)):
            continue
        if str(previous.power_source or "") != str(current.power_source or ""):
            continue
        if require_consumption_power and not (
            is_consumption_power_sample(previous)
            and is_consumption_power_sample(current)
        ):
            continue
        intervals.append((previous, current, delta))
    return intervals


def _interval_samples(
    intervals: Sequence[tuple[Sample, Sample, float]],
) -> List[Sample]:
    rows: List[Sample] = []
    seen: set[int] = set()
    for previous, current, _ in intervals:
        for sample in (previous, current):
            identity = id(sample)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(sample)
    return rows


def _integrate_sample_values(
    samples: Sequence[Sample],
    value: Callable[[Sample], float],
    max_gap_s: float,
    *,
    require_consumption_power: bool = False,
) -> Optional[float]:
    intervals = _sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=require_consumption_power,
    )
    if not intervals:
        return None
    return sum(
        (float(value(previous)) + float(value(current))) * 0.5 * delta / 3600.0
        for previous, current, delta in intervals
    )


def _integrate_energy(samples: Sequence[Sample], max_gap_s: float) -> Optional[float]:
    """Integrate only explicit, continuous battery-discharge intervals."""

    return _integrate_sample_values(
        samples,
        lambda sample: sample.power_mw,
        max_gap_s,
        require_consumption_power=True,
    )


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(
            len(ordered) - 1,
            math.ceil(len(ordered) * max(0.0, min(1.0, percentile))) - 1,
        ),
    )
    return ordered[index]


def _range_crosses_exclusion(
    start_uptime_s: float,
    end_uptime_s: float,
    excluded_ranges: Sequence[tuple[float, float]],
) -> bool:
    return any(
        start_uptime_s <= excluded_start and end_uptime_s >= excluded_end
        for excluded_start, excluded_end in excluded_ranges
    )


def _range_numeric_statistics(
    samples: Sequence[Sample],
    value: Callable[[Sample], object],
    start_uptime_s: float,
    end_uptime_s: float,
    max_gap_s: float,
    excluded_ranges: Sequence[tuple[float, float]] = (),
) -> Optional[Dict[str, object]]:
    selected_values: List[float] = []
    for sample in samples:
        if sample.uptime_s < start_uptime_s or sample.uptime_s > end_uptime_s:
            continue
        raw_value = value(sample)
        if isinstance(raw_value, (int, float)) and math.isfinite(float(raw_value)):
            selected_values.append(float(raw_value))

    weighted_total = 0.0
    covered_duration_s = 0.0
    boundary_values: List[float] = []
    for previous, current in zip(samples, samples[1:]):
        delta = float(current.uptime_s) - float(previous.uptime_s)
        if delta <= 0 or delta > max_gap_s:
            continue
        if bool(getattr(current, "_report_break_before", False)):
            continue
        if _range_crosses_exclusion(
            float(previous.uptime_s),
            float(current.uptime_s),
            excluded_ranges,
        ):
            continue
        interval_start = max(float(previous.uptime_s), start_uptime_s)
        interval_end = min(float(current.uptime_s), end_uptime_s)
        if interval_end <= interval_start:
            continue
        previous_value = value(previous)
        current_value = value(current)
        if not isinstance(previous_value, (int, float)) or not isinstance(
            current_value, (int, float)
        ):
            continue
        left_value = float(previous_value)
        right_value = float(current_value)
        if not math.isfinite(left_value) or not math.isfinite(right_value):
            continue
        left_ratio = (interval_start - float(previous.uptime_s)) / delta
        right_ratio = (interval_end - float(previous.uptime_s)) / delta
        clipped_left = left_value + (right_value - left_value) * left_ratio
        clipped_right = left_value + (right_value - left_value) * right_ratio
        clipped_duration = interval_end - interval_start
        weighted_total += (clipped_left + clipped_right) * 0.5 * clipped_duration
        covered_duration_s += clipped_duration
        boundary_values.extend((clipped_left, clipped_right))

    observed_values = [*selected_values, *boundary_values]
    if not observed_values:
        return None
    average = (
        weighted_total / covered_duration_s
        if covered_duration_s > 0
        else statistics.fmean(observed_values)
    )
    return {
        "average": average,
        "minimum": min(observed_values),
        "maximum": max(observed_values),
        "sample_count": len(selected_values),
        "covered_duration_s": covered_duration_s,
        "calculation": (
            "time_weighted_full_resolution"
            if covered_duration_s > 0
            else "sample_average_full_resolution"
        ),
    }


def _range_scalar_statistics(
    value: object,
    *,
    sample_count: object = 0,
    calculation: str = "range_recomputed",
) -> Optional[Dict[str, object]]:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    number = float(value)
    return {
        "average": number,
        "minimum": number,
        "maximum": number,
        "sample_count": max(0, _integer(sample_count, 0)),
        "covered_duration_s": None,
        "calculation": calculation,
    }


def _pearson(values: Sequence[float], powers: Sequence[float]) -> Optional[float]:
    if len(values) != len(powers) or len(values) < 3:
        return None
    mean_values = statistics.fmean(values)
    mean_powers = statistics.fmean(powers)
    numerator = sum(
        (value - mean_values) * (power - mean_powers)
        for value, power in zip(values, powers)
    )
    left = math.sqrt(sum((value - mean_values) ** 2 for value in values))
    right = math.sqrt(sum((power - mean_powers) ** 2 for power in powers))
    return numerator / (left * right) if left > 0 and right > 0 else None


class LiveTelemetryReader:
    """Incrementally parse an append-only run journal for the dashboard."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.metadata: Dict[str, object] = {}
        self.policies: List[CpuPolicy] = []
        self.gpu_source: Optional[GpuSource] = None
        self.memory_source: Optional[MemorySource] = None
        self.raw_samples: List[RawSample] = []
        self.normalized_samples: List[Sample] = []
        self.stream_contexts: List[ContextSample] = []
        self.contexts: List[ContextSample] = []
        self.system_snapshots: List[SystemSnapshot] = []
        self.thermal_snapshots: List[ThermalSnapshot] = []
        self.scheduler_snapshots: List[SchedulerSnapshot] = []
        self._metadata_mtime_ns: Optional[int] = None
        self._stream_offset = 0
        self._stream_remainder = b""
        self._jsonl_offsets: Dict[str, int] = {}
        self._jsonl_remainders: Dict[str, bytes] = {}
        self._lock = threading.RLock()

    def _refresh_metadata(self) -> None:
        path = self.output_dir / "metadata.json"
        try:
            stat = path.stat()
        except OSError:
            return
        if self._metadata_mtime_ns == stat.st_mtime_ns:
            return
        metadata = _read_json(path)
        if not metadata:
            return
        self.metadata = metadata
        self.policies = _policies_from_metadata(metadata)
        self.gpu_source = _gpu_from_metadata(metadata)
        self.memory_source = _memory_from_metadata(metadata)
        self._metadata_mtime_ns = stat.st_mtime_ns

    def _refresh_stream(self) -> None:
        if not self.metadata:
            return
        path = self.output_dir / "raw" / "sampler-stream.txt"
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._stream_offset:
            self._stream_offset = 0
            self._stream_remainder = b""
            self.raw_samples.clear()
            self.normalized_samples.clear()
            self.stream_contexts.clear()
        if size == self._stream_offset:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(self._stream_offset)
                chunk = handle.read()
        except OSError:
            return
        self._stream_offset += len(chunk)
        payload = self._stream_remainder + chunk
        lines = payload.split(b"\n")
        self._stream_remainder = lines.pop() if lines else b""
        for raw_line in lines:
            line = raw_line.decode("utf-8", errors="replace")
            parsed = parse_sampler_line(
                line,
                self.policies,
                self.gpu_source,
                self.memory_source,
            )
            if isinstance(parsed, RawSample):
                if self.raw_samples and parsed.uptime_s <= self.raw_samples[-1].uptime_s:
                    continue
                self.raw_samples.append(parsed)
            elif isinstance(parsed, Sample):
                if (
                    self.normalized_samples
                    and parsed.uptime_s <= self.normalized_samples[-1].uptime_s
                ):
                    continue
                parsed.index = len(self.normalized_samples)
                parsed.elapsed_s = (
                    parsed.uptime_s - self.normalized_samples[0].uptime_s
                    if self.normalized_samples
                    else 0.0
                )
                self.normalized_samples.append(parsed)
            elif isinstance(parsed, ContextSample):
                if (
                    self.stream_contexts
                    and parsed.uptime_s <= self.stream_contexts[-1].uptime_s
                ):
                    continue
                self.stream_contexts.append(parsed)

    def _refresh_jsonl(self, name: str, target: List[object], model: object) -> None:
        path = self.output_dir / name
        offset = self._jsonl_offsets.get(name, 0)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < offset:
            offset = 0
            self._jsonl_remainders[name] = b""
            target.clear()
        if size == offset:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            return
        self._jsonl_offsets[name] = offset + len(chunk)
        payload = self._jsonl_remainders.get(name, b"") + chunk
        lines = payload.split(b"\n")
        self._jsonl_remainders[name] = lines.pop() if lines else b""
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line.decode("utf-8", errors="replace"))
                target.append(model(**value))  # type: ignore[operator]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

    def refresh(self) -> None:
        self._refresh_metadata()
        self._refresh_stream()
        self._refresh_jsonl("contexts.jsonl", self.contexts, ContextSample)
        self._refresh_jsonl("system-snapshots.jsonl", self.system_snapshots, SystemSnapshot)
        self._refresh_jsonl("thermal-snapshots.jsonl", self.thermal_snapshots, ThermalSnapshot)
        self._refresh_jsonl(
            "scheduler-snapshots.jsonl",
            self.scheduler_snapshots,
            SchedulerSnapshot,
        )

    def _converted_samples(self) -> tuple[List[Sample], List[str]]:
        if len(self.normalized_samples) >= 2:
            return list(self.normalized_samples), []
        if len(self.raw_samples) < 2:
            return [], []
        battery = self.metadata.get("battery_start", {})
        battery = battery if isinstance(battery, dict) else {}
        first_voltage = self.raw_samples[0].voltage_mv
        latest_voltage = self.raw_samples[-1].voltage_mv
        start_voltage = _number(battery.get("voltage_mv"), first_voltage or 3800.0)
        end_voltage = latest_voltage or start_voltage
        current_unit = str(self.metadata.get("current_unit") or "auto")
        battery_status = str(battery.get("status") or "unknown")
        sample_interval = max(0.05, _number(self.metadata.get("sample_interval_s"), 1.0))
        legacy_power_state_format = all(
            sample.external_power is None and sample.battery_status is None
            for sample in self.raw_samples
        )
        try:
            return convert_samples(
                self.raw_samples,
                self.policies,
                self.gpu_source,
                start_voltage,
                end_voltage,
                current_unit,
                battery_status,
                max_cpu_gap_s=max(sample_interval * 3.0, sample_interval + 2.0),
                external_power=(
                    bool(battery.get("powered"))
                    if legacy_power_state_format and "powered" in battery
                    else None
                ),
            )
        except RuntimeError:
            return [], []

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> Dict[str, object]:
        self.refresh()
        samples, conversion_warnings = self._converted_samples()
        requested_duration = _number(self.metadata.get("requested_duration_s"), 0.0)
        sample_interval = max(0.05, _number(self.metadata.get("sample_interval_s"), 1.0))
        max_gap = max(sample_interval * 3.0, sample_interval + 2.0)
        latest = samples[-1] if samples else None
        elapsed = latest.elapsed_s if latest else 0.0
        progress = elapsed / requested_duration if requested_duration > 0 else 0.0
        displayed = _decimate(samples)
        sample_positions = {id(sample): index for index, sample in enumerate(samples)}
        sample_breaks = _sample_break_flags(samples) if samples else []
        displayed_break_before_ids: set[int] = set()
        previous_displayed_index: Optional[int] = None
        for displayed_sample in displayed:
            displayed_index = sample_positions[id(displayed_sample)]
            if (
                previous_displayed_index is not None
                and any(sample_breaks[previous_displayed_index + 1 : displayed_index + 1])
            ):
                displayed_break_before_ids.add(id(displayed_sample))
            previous_displayed_index = displayed_index
        observed_powers = [sample.power_mw for sample in samples]
        ios_system_load_source = "ios_power_telemetry_system_load"
        system_load_samples = [
            sample for sample in samples if sample.power_source == ios_system_load_source
        ]
        system_load_powers = [sample.power_mw for sample in system_load_samples]
        system_load_intervals = [
            interval
            for interval in _sample_intervals(samples, max_gap)
            if interval[0].power_source == ios_system_load_source
            and interval[1].power_source == ios_system_load_source
        ]
        system_load_consumption_intervals = [
            interval
            for interval in _sample_intervals(
                samples,
                max_gap,
                require_consumption_power=True,
            )
            if interval[0].power_source == ios_system_load_source
            and interval[1].power_source == ios_system_load_source
        ]
        system_load_consumption_samples = _interval_samples(
            system_load_consumption_intervals
        )
        system_load_consumption_powers = [
            sample.power_mw for sample in system_load_consumption_samples
        ]
        system_load_covered_duration_s = sum(
            interval[2] for interval in system_load_intervals
        )
        system_load_consumption_covered_duration_s = sum(
            interval[2] for interval in system_load_consumption_intervals
        )
        system_load_observed_energy_mwh = (
            sum(
                (previous.power_mw + current.power_mw) * 0.5 * delta / 3600.0
                for previous, current, delta in system_load_intervals
            )
            if system_load_intervals
            else None
        )
        system_load_consumption_energy_mwh = (
            sum(
                (previous.power_mw + current.power_mw) * 0.5 * delta / 3600.0
                for previous, current, delta in system_load_consumption_intervals
            )
            if system_load_consumption_intervals
            else None
        )
        system_load_observed_average_power_mw = (
            system_load_observed_energy_mwh * 3600.0 / system_load_covered_duration_s
            if system_load_observed_energy_mwh is not None
            and system_load_covered_duration_s > 0
            else statistics.fmean(system_load_powers) if system_load_powers else None
        )
        system_load_consumption_average_power_mw = (
            system_load_consumption_energy_mwh
            * 3600.0
            / system_load_consumption_covered_duration_s
            if system_load_consumption_energy_mwh is not None
            and system_load_consumption_covered_duration_s > 0
            else (
                statistics.fmean(system_load_consumption_powers)
                if system_load_consumption_powers
                else None
            )
        )
        latest_system_load = system_load_samples[-1] if system_load_samples else None
        latest_system_load_age_s = (
            max(0.0, (latest.uptime_s - latest_system_load.uptime_s))
            + max(0.0, float(latest_system_load.power_sample_age_s or 0.0))
            if latest is not None and latest_system_load is not None
            else None
        )
        battery_flow_powers = [
            sample.current_ma * sample.voltage_mv / 1000.0 for sample in samples
        ]
        battery_flow_currents = [sample.current_ma for sample in samples]
        telemetry_intervals = _sample_intervals(samples, max_gap)
        telemetry_covered_duration_s = sum(item[2] for item in telemetry_intervals)
        consumption_intervals = _sample_intervals(
            samples,
            max_gap,
            require_consumption_power=True,
        )
        consumption_samples = _interval_samples(consumption_intervals)
        consumption_powers = [sample.power_mw for sample in consumption_samples]
        consumption_covered_duration_s = sum(item[2] for item in consumption_intervals)
        observed_power_energy_mwh = _integrate_sample_values(
            samples,
            lambda sample: sample.power_mw,
            max_gap,
        )
        battery_flow_energy_mwh = _integrate_sample_values(
            samples,
            lambda sample: sample.current_ma * sample.voltage_mv / 1000.0,
            max_gap,
        )
        battery_flow_charge_mah = _integrate_sample_values(
            samples,
            lambda sample: sample.current_ma,
            max_gap,
        )
        energy_mwh = _integrate_energy(samples, max_gap)
        discharge_mah = _integrate_sample_values(
            samples,
            lambda sample: sample.current_ma,
            max_gap,
            require_consumption_power=True,
        )
        average_power_mw = (
            energy_mwh * 3600.0 / consumption_covered_duration_s
            if energy_mwh is not None and consumption_covered_duration_s > 0
            else None
        )
        average_current_ma = (
            discharge_mah * 3600.0 / consumption_covered_duration_s
            if discharge_mah is not None and consumption_covered_duration_s > 0
            else None
        )
        observed_power_average_mw = (
            observed_power_energy_mwh * 3600.0 / telemetry_covered_duration_s
            if observed_power_energy_mwh is not None and telemetry_covered_duration_s > 0
            else statistics.fmean(observed_powers) if observed_powers else None
        )
        battery_flow_average_power_mw = (
            battery_flow_energy_mwh * 3600.0 / telemetry_covered_duration_s
            if battery_flow_energy_mwh is not None and telemetry_covered_duration_s > 0
            else statistics.fmean(battery_flow_powers) if battery_flow_powers else None
        )
        battery_flow_average_current_ma = (
            battery_flow_charge_mah * 3600.0 / telemetry_covered_duration_s
            if battery_flow_charge_mah is not None and telemetry_covered_duration_s > 0
            else statistics.fmean(battery_flow_currents) if battery_flow_currents else None
        )
        session_duration_s = (
            samples[-1].uptime_s - samples[0].uptime_s if len(samples) >= 2 else 0.0
        )
        power_valid_for_consumption = bool(consumption_intervals)
        stale_system_load_observed = any(
            sample.power_source == ios_system_load_source
            and not is_power_sample_fresh_for_consumption(sample)
            for sample in samples
        )
        external_power_observed = any(sample.external_power is True for sample in samples)
        observed_directions = {
            str(sample.direction or "unknown").strip().lower() for sample in samples
        }
        if observed_directions and observed_directions <= {"charging", "full"}:
            power_flow_direction = "charging"
        elif samples and all(sample.external_power is True for sample in samples):
            power_flow_direction = "external_power"
        elif observed_directions == {"discharging"}:
            power_flow_direction = "discharging"
        elif observed_directions and observed_directions <= {"idle", "unknown"}:
            power_flow_direction = "idle"
        elif observed_directions:
            power_flow_direction = "mixed"
        else:
            power_flow_direction = str(
                (self.metadata.get("battery_start") or {}).get("status")
                if isinstance(self.metadata.get("battery_start"), dict)
                else "unknown"
            )
        cpu_values = [sample.cpu_pct for sample in samples if sample.cpu_pct is not None]
        context_map: Dict[tuple[object, ...], ContextSample] = {}
        for context in sorted(
            [*self.stream_contexts, *self.contexts],
            key=lambda item: item.uptime_s,
        ):
            key = (
                round(context.uptime_s, 4),
                context.source,
                context.foreground_package,
                context.foreground_activity,
            )
            context_map[key] = context
        contexts = sorted(context_map.values(), key=lambda item: item.uptime_s)

        latest_context: Optional[ContextSample] = None
        if latest is not None:
            for context in contexts:
                if context.uptime_s > latest.uptime_s:
                    break
                latest_context = context
        elif contexts:
            latest_context = contexts[-1]

        clusters: List[Dict[str, object]] = []
        if latest is not None:
            for policy in self.policies:
                clusters.append(
                    {
                        "name": policy.name,
                        "label": policy.label,
                        "load_pct": latest.cluster_cpu_pct.get(policy.name),
                        "frequency_mhz": latest.frequencies_mhz.get(policy.name),
                        "maximum_mhz": (
                            float(policy.max_khz) / 1000.0 if policy.max_khz else None
                        ),
                        "cores": policy.cores,
                    }
                )

        battery = self.metadata.get("battery_start", {})
        battery = battery if isinstance(battery, dict) else {}
        battery_status = str(battery.get("status") or "unknown")
        warnings = [localize_collection_warning(value) for value in conversion_warnings]
        raw_warnings = self.metadata.get("collection_warnings", [])
        if isinstance(raw_warnings, list):
            warnings.extend(localize_collection_warning(value) for value in raw_warnings)

        latest_system = self.system_snapshots[-1] if self.system_snapshots else None
        latest_thread_system = next(
            (item for item in reversed(self.system_snapshots) if item.threads),
            None,
        )
        latest_thermal = self.thermal_snapshots[-1] if self.thermal_snapshots else None
        latest_scheduler = self.scheduler_snapshots[-1] if self.scheduler_snapshots else None
        active_priority = (
            [
                item
                for item in latest_system.watched_processes
                if item.get("activity_active")
            ]
            if latest_system is not None
            else []
        )
        platform = str(self.metadata.get("platform") or "android").lower()
        performance = analyze_performance_contexts(contexts, self.metadata)
        if platform == "ios":
            ios_stats = self.metadata.get("ios_collection_stats", {})
            ios_stats = ios_stats if isinstance(ios_stats, dict) else {}
            notifications_observed = (
                ios_stats.get("application_state_notifications_observed") is True
            )
            current_foreground_known = bool(
                latest_context is not None and latest_context.foreground_package
            )
            latest_notification_was_suspend = bool(
                latest_context is not None
                and latest_context.source == "ios_dvt_notifications"
                and not latest_context.foreground_package
            )
            if current_foreground_known:
                foreground_state_status = "observed"
                foreground_state_reason = "来自测试期间实际收到的 DVT Running 通知。"
            elif latest_notification_was_suspend:
                foreground_state_status = "unknown"
                foreground_state_reason = (
                    "最近收到 DVT Suspended 通知，但接口没有提供随后当前前台应用的初始快照。"
                )
            elif notifications_observed:
                foreground_state_status = "unknown"
                foreground_state_reason = (
                    "采集期间观察到应用状态通知，但没有获得 Running 上下文，不能确认当前前台应用。"
                )
            else:
                foreground_state_status = "not_observed"
                foreground_state_reason = (
                    "DVT 通知流只报告测试期间发生的 Running / Suspended 变化；"
                    "没有变化时不能据此确定采集开始时的当前前台应用。"
                )
            performance = {
                **performance,
                "frame_unavailable_reason": (
                    "当前 iOS 后端不提供通用应用 FPS、标准 1% Low、逐帧 Core Animation "
                    "时间戳或刷新率时间线。"
                ),
                "refresh_rate_unavailable_reason": str(
                    performance.get("refresh_rate_unavailable_reason")
                    or "当前 iOS 后端没有可验证的屏幕刷新率时间线。"
                ),
                "foreground_state_status": foreground_state_status,
                "foreground_state_reason": foreground_state_reason,
                "foreground_state_available": current_foreground_known,
                "application_state_change_observed": notifications_observed,
            }
        brightness_throttling = (
            analyze_brightness_throttling(samples, contexts, self.thermal_snapshots)
            if platform == "android"
            else {
                "available": False,
                "timeline": [],
                "points": [],
                "events": [],
                "point_count": 0,
                "event_count": 0,
            }
        )
        session_start_uptime = samples[0].uptime_s if samples else None
        timeline_start_uptime = (
            session_start_uptime
            if session_start_uptime is not None
            else contexts[0].uptime_s if contexts else None
        )

        def live_timeline_points(
            value: object,
            *,
            value_keys: Sequence[str] = ("value",),
            preserve_steps: bool = False,
        ) -> List[Dict[str, object]]:
            rows = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
            if timeline_start_uptime is not None:
                rows = [
                    {
                        **item,
                        "elapsed_s": max(
                            0.0,
                            float(item.get("uptime_s") or 0.0) - timeline_start_uptime,
                        ),
                    }
                    for item in rows
                    if isinstance(item.get("uptime_s"), (int, float))
                ]
            return _decimate_timeline_rows(
                rows,
                value_keys=value_keys,
                preserve_steps=preserve_steps,
            )

        performance = {
            **performance,
            "frame_rate_timeline": live_timeline_points(
                performance.get("frame_rate_timeline", []),
                value_keys=(
                    "frame_rate_fps",
                    "one_percent_low_fps",
                    "frame_time_average_ms",
                    "frame_time_p95_ms",
                    "frame_time_p99_ms",
                    "frame_issue_pct",
                    "value",
                ),
            ),
            "refresh_rate_timeline": live_timeline_points(
                performance.get("refresh_rate_timeline", []),
                value_keys=("value", "refresh_rate_hz"),
                preserve_steps=True,
            ),
        }
        frame_flow = performance.get("frame_flow", {})
        if isinstance(frame_flow, dict):
            flow_stages = frame_flow.get("stages", [])
            if isinstance(flow_stages, list):
                performance = {
                    **performance,
                    "frame_flow": {
                        **frame_flow,
                        "stages": [
                            {
                                **stage,
                                "timeline": live_timeline_points(
                                    stage.get("timeline", []),
                                    value_keys=(
                                        "value",
                                        "frame_rate_fps",
                                        "duration_ms",
                                        "latency_ms",
                                    ),
                                    preserve_steps=(
                                        str(stage.get("key") or "") == "display_scanout"
                                    ),
                                ),
                            }
                            for stage in flow_stages
                            if isinstance(stage, dict)
                        ],
                    },
                }
        performance_timeline = performance.get("frame_rate_timeline", [])
        performance_series = [
            {
                **item,
                "elapsed_s": max(
                    0.0,
                    float(item.get("uptime_s") or 0.0) - session_start_uptime,
                ),
            }
            for item in performance_timeline
            if isinstance(item, dict)
            and session_start_uptime is not None
            and isinstance(item.get("uptime_s"), (int, float))
        ] if isinstance(performance_timeline, list) else []

        target_package = str(
            self.metadata.get("target_package")
            or self.metadata.get("foreground_package")
            or (latest_context.foreground_package if latest_context is not None else "")
            or ""
        )
        scheduler_source = self.scheduler_snapshots
        if len(scheduler_source) > MAX_LIVE_POINTS:
            stride = max(1, math.ceil(len(scheduler_source) / MAX_LIVE_POINTS))
            scheduler_source = scheduler_source[::stride]
            if scheduler_source[-1] is not self.scheduler_snapshots[-1]:
                scheduler_source = [*scheduler_source, self.scheduler_snapshots[-1]]
        scheduler_start_uptime = (
            session_start_uptime
            if session_start_uptime is not None
            else scheduler_source[0].uptime_s if scheduler_source else None
        )
        scheduler_series: List[Dict[str, object]] = []
        for snapshot in scheduler_source:
            if scheduler_start_uptime is None:
                continue
            cpuset_name = (
                "top-app"
                if snapshot.cpusets.get("top-app")
                else "foreground"
                if snapshot.cpusets.get("foreground")
                else next(iter(snapshot.cpusets), "")
            )
            cpuset_value = snapshot.cpusets.get(cpuset_name) if cpuset_name else None
            watched = [item for item in snapshot.watched_processes if isinstance(item, dict)]
            foreground_process = next(
                (
                    item
                    for item in watched
                    if target_package
                    and (
                        str(item.get("name") or "") == target_package
                        or str(item.get("name") or "").startswith(target_package + ":")
                    )
                ),
                None,
            )
            if foreground_process is None:
                foreground_process = next(
                    (
                        item
                        for item in watched
                        if str(item.get("adj_type") or "") == "top-activity"
                        or item.get("current_sched_group") == 3
                    ),
                    None,
                )
            scheduler_series.append(
                {
                    "elapsed_s": max(0.0, snapshot.uptime_s - scheduler_start_uptime),
                    "uptime_s": snapshot.uptime_s,
                    "cpuset_name": cpuset_name or None,
                    "cpuset_cpus": cpuset_value,
                    "cpuset_cpu_count": _cpu_set_count(cpuset_value),
                    "hint_session_count": len(snapshot.hint_sessions),
                    "graphics_session_count": sum(
                        1 for item in snapshot.hint_sessions if item.get("graphics_pipeline")
                    ),
                    "foreground_sched_group": (
                        foreground_process.get("current_sched_group")
                        if foreground_process is not None
                        else None
                    ),
                    "foreground_proc_state": (
                        foreground_process.get("current_proc_state")
                        if foreground_process is not None
                        else None
                    ),
                    "foreground_process": (
                        foreground_process.get("name")
                        if foreground_process is not None
                        else target_package or None
                    ),
                    "top_app_process_count": sum(
                        1
                        for item in watched
                        if item.get("current_sched_group") == 3
                        or str(item.get("adj_type") or "") == "top-activity"
                    ),
                    "frozen_process_count": sum(1 for item in watched if item.get("frozen")),
                    "collection_ms": snapshot.collection_ms,
                }
            )

        memory_analysis = analyze_memory_frequency(samples, self.metadata) if samples else {
            "available": False,
            "timeline": [],
        }
        settings_analysis = analyze_runtime_settings(self.metadata, {})
        pressure_drivers: List[Dict[str, object]] = []
        consumption_sample_ids = {id(sample) for sample in consumption_samples}

        def add_pressure_driver(
            key: str,
            label: str,
            values: Sequence[Optional[float]],
        ) -> None:
            pairs = [
                (float(value), float(sample.power_mw))
                for sample, value in zip(samples, values)
                if id(sample) in consumption_sample_ids
                and isinstance(value, (int, float))
            ]
            if len(pairs) < 30 or consumption_covered_duration_s < 30.0:
                return
            correlation = _pearson(
                [item[0] for item in pairs],
                [item[1] for item in pairs],
            )
            if correlation is None or abs(correlation) < 0.5:
                return
            pressure_drivers.append(
                {
                    "key": key,
                    "label": label,
                    "correlation": correlation,
                    "sample_count": len(pairs),
                    "covered_duration_s": consumption_covered_duration_s,
                }
            )

        add_pressure_driver("cpu", "CPU 总负载", [item.cpu_pct for item in samples])
        add_pressure_driver("gpu_load", "GPU 负载", [item.gpu_load_pct for item in samples])
        add_pressure_driver(
            "gpu_frequency",
            "GPU 频率",
            [item.gpu_frequency_mhz for item in samples],
        )
        add_pressure_driver(
            "memory_frequency",
            "内存 / DMC 频率",
            [item.memory_frequency_mhz for item in samples],
        )
        add_pressure_driver(
            "temperature",
            "电池温度",
            [item.battery_temperature_c for item in samples],
        )
        pressure_drivers.sort(
            key=lambda item: abs(float(item.get("correlation") or 0.0)),
            reverse=True,
        )
        live_tasks = []
        if latest_system is not None:
            live_tasks = [
                {
                    "pid": item.get("pid"),
                    "name": item.get("name") or item.get("command"),
                    "cpu_pct": item.get("cpu_pct"),
                    "memory_kb": item.get("memory_kb") or item.get("rss_kb"),
                    "state": item.get("state"),
                }
                for item in latest_system.processes[:8]
                if isinstance(item, dict)
            ]
        power_pressure = {
            "available": bool(pressure_drivers or live_tasks or settings_analysis.get("available")),
            "drivers": pressure_drivers,
            "leading_driver": pressure_drivers[0] if pressure_drivers else None,
            "power_valid_for_consumption": power_valid_for_consumption,
            "power_unavailable_reason": (
                None
                if power_valid_for_consumption
                else "采集期间没有连续且明确未接外部电源的放电区间，不生成负载与功耗相关性。"
            ),
            "tasks": live_tasks,
            "memory": memory_analysis,
            "settings": settings_analysis,
            "scheduler": asdict(latest_scheduler) if latest_scheduler else None,
        }

        render_threads = []
        if latest_thread_system is not None:
            render_threads = [
                item
                for item in latest_thread_system.threads
                if isinstance(item, dict)
                and re.search(
                    r"renderthread|surfaceflinger|renderengine|composer|hwc|gpu|main",
                    f"{item.get('name') or ''} {item.get('process') or ''}",
                    re.I,
                )
            ][:12]
        render_pipeline = performance.get("render_pipeline", {})
        render_pipeline = render_pipeline if isinstance(render_pipeline, dict) else {}
        render_performance = {
            "available": bool(render_pipeline.get("available") or render_threads),
            "pipeline": render_pipeline,
            "dominant_stage": render_pipeline.get("dominant_stage"),
            "render_threads": render_threads,
            "power_recording": {
                "power_valid_for_consumption": power_valid_for_consumption,
                "average_power_mw": average_power_mw,
                "p95_power_mw": _percentile(consumption_powers, 0.95),
                "energy_mwh": energy_mwh,
                "consumption_covered_duration_s": consumption_covered_duration_s,
                "observed_power_average_mw": observed_power_average_mw,
                "observed_power_p95_mw": _percentile(observed_powers, 0.95),
                "observed_power_energy_mwh": observed_power_energy_mwh,
                "battery_flow_average_power_mw": battery_flow_average_power_mw,
                "battery_flow_p95_power_mw": _percentile(battery_flow_powers, 0.95),
                "battery_flow_energy_mwh": battery_flow_energy_mwh,
                "note": (
                    "正式功耗统计只使用连续、明确未接外部电源的放电区间；性能模式不分析组件或 UID 功耗来源。"
                    if power_valid_for_consumption
                    else "当前没有连续、明确未接外部电源的放电区间；仅保留观测功率与电池流量原始统计。"
                ),
            },
        }

        return {
            "metadata": self.metadata,
            "test_mode": str(self.metadata.get("test_mode") or "power"),
            "sample_count": len(samples),
            "elapsed_s": elapsed,
            "requested_duration_s": requested_duration,
            "progress": max(0.0, min(1.0, progress)),
            "latest": (
                {
                    "elapsed_s": latest.elapsed_s,
                    "uptime_s": latest.uptime_s,
                    "current_ma": latest.current_ma,
                    "signed_current_ma": latest.signed_current_ma,
                    "power_mw": latest.power_mw,
                    "direction": latest.direction,
                    "power_valid_for_consumption": is_consumption_power_sample(latest),
                    "external_power": latest.external_power,
                    "voltage_mv": latest.voltage_mv,
                    "cpu_pct": latest.cpu_pct,
                    "cluster_cpu_pct": dict(latest.cluster_cpu_pct),
                    "frequencies_mhz": dict(latest.frequencies_mhz),
                    "temperature_c": latest.battery_temperature_c,
                    "gpu_frequency_mhz": latest.gpu_frequency_mhz,
                    "gpu_load_pct": latest.gpu_load_pct,
                    "memory_frequency_mhz": latest.memory_frequency_mhz,
                    "power_source": latest.power_source,
                    "power_sample_age_s": latest.power_sample_age_s,
                    "power_sample_stale": (
                        latest.power_source == ios_system_load_source
                        and not is_power_sample_fresh_for_consumption(latest)
                    ),
                    "collector_cpu_pct": latest.collector_cpu_pct,
                }
                if latest is not None
                else None
            ),
            "summary": {
                "average_power_mw": average_power_mw,
                "median_power_mw": (
                    statistics.median(consumption_powers) if consumption_powers else None
                ),
                "p95_power_mw": _percentile(consumption_powers, 0.95),
                "average_current_ma": average_current_ma,
                "energy_mwh": energy_mwh,
                "discharge_mah": discharge_mah,
                "direction": power_flow_direction if samples else battery_status,
                "power_flow_direction": power_flow_direction,
                "power_valid_for_consumption": power_valid_for_consumption,
                "power_consumption_unavailable_reason": (
                    None
                    if power_valid_for_consumption
                    else (
                        f"iOS SystemLoad 超过 {IOS_SYSTEM_LOAD_STALE_AFTER_S:.0f} 秒未刷新；"
                        "过期区间不生成平均功耗或能量结论。"
                        if stale_system_load_observed
                        else "采集期间没有连续、明确未接外部电源的放电区间；不生成平均功耗、能量或相关性结论。"
                    )
                ),
                "consumption_covered_duration_s": consumption_covered_duration_s,
                "consumption_coverage_pct": (
                    consumption_covered_duration_s / session_duration_s * 100.0
                    if session_duration_s > 0
                    else 0.0
                ),
                "external_power_observed": external_power_observed,
                "power_sources": sorted(
                    {sample.power_source for sample in samples if sample.power_source}
                ),
                "observed_power_primary_source": (
                    ios_system_load_source
                    if system_load_samples
                    else latest.power_source if latest is not None else None
                ),
                "system_load_available": bool(system_load_samples),
                "system_load_sample_count": len(system_load_samples),
                "system_load_latest_power_mw": (
                    latest_system_load.power_mw if latest_system_load is not None else None
                ),
                "system_load_latest_elapsed_s": (
                    latest_system_load.elapsed_s if latest_system_load is not None else None
                ),
                "system_load_latest_sample_age_s": latest_system_load_age_s,
                "system_load_observed_average_power_mw": (
                    system_load_observed_average_power_mw
                ),
                "system_load_observed_p95_power_mw": _percentile(
                    system_load_powers,
                    0.95,
                ),
                "system_load_consumption_average_power_mw": (
                    system_load_consumption_average_power_mw
                ),
                "system_load_consumption_p95_power_mw": _percentile(
                    system_load_consumption_powers,
                    0.95,
                ),
                "system_load_consumption_energy_mwh": (
                    system_load_consumption_energy_mwh
                ),
                "system_load_consumption_covered_duration_s": (
                    system_load_consumption_covered_duration_s
                ),
                "observed_power_average_mw": observed_power_average_mw,
                "observed_power_median_mw": (
                    statistics.median(observed_powers) if observed_powers else None
                ),
                "observed_power_p95_mw": _percentile(observed_powers, 0.95),
                "observed_power_energy_mwh": observed_power_energy_mwh,
                "battery_flow_average_power_mw": battery_flow_average_power_mw,
                "battery_flow_median_power_mw": (
                    statistics.median(battery_flow_powers) if battery_flow_powers else None
                ),
                "battery_flow_p95_power_mw": _percentile(battery_flow_powers, 0.95),
                "battery_flow_energy_mwh": battery_flow_energy_mwh,
                "battery_flow_charge_mah": battery_flow_charge_mah,
                "battery_flow_average_current_ma": battery_flow_average_current_ma,
                "covered_duration_s": telemetry_covered_duration_s,
                "average_cpu_pct": statistics.fmean(cpu_values) if cpu_values else None,
                "average_collector_cpu_pct": statistics.fmean(
                    [
                        sample.collector_cpu_pct
                        for sample in samples
                        if sample.collector_cpu_pct is not None
                    ]
                )
                if any(sample.collector_cpu_pct is not None for sample in samples)
                else None,
            },
            "series": [
                {
                    "elapsed_s": sample.elapsed_s,
                    "uptime_s": sample.uptime_s,
                    "power_mw": sample.power_mw,
                    "battery_flow_power_mw": (
                        sample.current_ma * sample.voltage_mv / 1000.0
                    ),
                    "direction": sample.direction,
                    "power_valid_for_consumption": is_consumption_power_sample(sample),
                    "external_power": sample.external_power,
                    "power_source": sample.power_source,
                    "current_ma": sample.current_ma,
                    "signed_current_ma": sample.signed_current_ma,
                    "voltage_mv": sample.voltage_mv,
                    "cpu_pct": sample.cpu_pct,
                    "cluster_cpu_pct": dict(sample.cluster_cpu_pct),
                    "frequencies_mhz": dict(sample.frequencies_mhz),
                    "temperature_c": sample.battery_temperature_c,
                    "gpu_frequency_mhz": sample.gpu_frequency_mhz,
                    "gpu_load_pct": sample.gpu_load_pct,
                    "memory_frequency_mhz": sample.memory_frequency_mhz,
                    "power_sample_age_s": sample.power_sample_age_s,
                    "power_sample_stale": (
                        sample.power_source == ios_system_load_source
                        and not is_power_sample_fresh_for_consumption(sample)
                    ),
                    "collector_cpu_pct": sample.collector_cpu_pct,
                    "report_break_before": bool(
                        id(sample) in displayed_break_before_ids
                        or getattr(sample, "_report_break_before", False)
                    ),
                }
                for sample in displayed
            ],
            "performance_series": performance_series,
            "scheduler_series": scheduler_series,
            "clusters": clusters,
            "context": asdict(latest_context) if latest_context is not None else None,
            "performance": performance,
            "brightness_throttling": brightness_throttling,
            "memory": memory_analysis,
            "runtime_settings": settings_analysis,
            "power_pressure": power_pressure,
            "render_performance": render_performance,
            "battery": battery,
            "warnings": list(dict.fromkeys(warnings)),
            "system_monitor": {
                "enabled": bool(
                    isinstance(self.metadata.get("system_monitor"), dict)
                    and self.metadata.get("system_monitor", {}).get("enabled")
                ),
                "system_snapshot_count": len(self.system_snapshots),
                "thermal_snapshot_count": len(self.thermal_snapshots),
                "scheduler_snapshot_count": len(self.scheduler_snapshots),
                "processes": latest_system.processes[:20] if latest_system else [],
                "threads": latest_thread_system.threads[:20] if latest_thread_system else [],
                "watched_processes": latest_system.watched_processes if latest_system else [],
                "active_priority": active_priority,
                "process_count": latest_system.process_count if latest_system else None,
                "thread_count": (
                    latest_thread_system.thread_count if latest_thread_system else None
                ),
                "system_uptime_s": latest_system.uptime_s if latest_system else None,
                "thermal": asdict(latest_thermal) if latest_thermal else None,
                "scheduler": asdict(latest_scheduler) if latest_scheduler else None,
            },
        }


class ActiveRun:
    def __init__(
        self,
        process: subprocess.Popen[str],
        output_dir: Path,
        config: Dict[str, object],
        command: Sequence[str],
    ) -> None:
        self.process = process
        self.output_dir = output_dir
        self.config = config
        self.command = list(command)
        self.reader = LiveTelemetryReader(output_dir)
        self.logs: deque[Dict[str, object]] = deque(maxlen=MAX_LOG_LINES)
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.stop_requested = False
        self._lock = threading.RLock()
        self._threads: List[threading.Thread] = []

        if process.stdout is not None:
            self._start_reader(process.stdout, "stdout")
        if process.stderr is not None:
            self._start_reader(process.stderr, "stderr")
        waiter = threading.Thread(target=self._wait_for_process, daemon=True)
        waiter.start()
        self._threads.append(waiter)

    def _start_reader(self, stream: object, source: str) -> None:
        thread = threading.Thread(
            target=self._drain_stream,
            args=(stream, source),
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)

    def _drain_stream(self, stream: object, source: str) -> None:
        try:
            for line in stream:  # type: ignore[union-attr]
                text = str(line).rstrip("\r\n")
                if not text:
                    continue
                with self._lock:
                    self.logs.append(
                        {
                            "time": time.time(),
                            "source": source,
                            "line": text,
                        }
                    )
        finally:
            try:
                stream.close()  # type: ignore[union-attr]
            except Exception:
                pass

    def _wait_for_process(self) -> None:
        returncode = self.process.wait()
        with self._lock:
            self.returncode = returncode
            self.finished_at = time.time()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def request_stop(self) -> None:
        if not self.running:
            return
        with self._lock:
            if self.stop_requested:
                return
            self.stop_requested = True
            self.logs.append(
                {
                    "time": time.time(),
                    "source": "ui",
                    "line": "Stop requested; finalizing the recoverable portion...",
                }
            )
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                self.process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[arg-type]
            else:
                self.process.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            self.process.terminate()
        watchdog = threading.Thread(target=self._stop_watchdog, daemon=True)
        watchdog.start()

    def _stop_watchdog(self) -> None:
        try:
            self.process.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except OSError:
                pass

    def snapshot(self) -> Dict[str, object]:
        live = self.reader.snapshot()
        metadata = live.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        checkpoint = _read_json(self.output_dir / "checkpoint.json")
        running = self.running
        if running:
            status = "stopping" if self.stop_requested else (
                "recording" if _integer(live.get("sample_count"), 0) else "starting"
            )
        else:
            collection_status = str(metadata.get("collection_status") or "")
            final_statuses = {"complete", "collected", "partial", "interrupted", "recovered"}
            observed_returncode = self.returncode
            if observed_returncode is None:
                observed_returncode = self.process.poll()
            if collection_status in final_statuses:
                status = collection_status
            elif observed_returncode == 0:
                status = "complete"
            elif self.stop_requested or observed_returncode == 130:
                status = "interrupted"
            else:
                status = "failed"
        if checkpoint.get("status") and running and status == "starting":
            status = str(checkpoint["status"])

        test_mode = str(
            metadata.get("test_mode") or self.config.get("test_mode") or "power"
        )
        default_title = "Performance session" if test_mode == "performance" else "Power session"
        title = str(metadata.get("title") or self.config.get("title") or default_title)
        device = metadata.get("device", {})
        report_exists = (self.output_dir / "report.html").exists() and not running
        with self._lock:
            logs = list(self.logs)
        return {
            **live,
            "status": status,
            "running": running,
            "stop_requested": self.stop_requested,
            "returncode": self.returncode,
            "title": title,
            "output_dir": str(self.output_dir),
            "run_name": self.output_dir.name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "device": device if isinstance(device, dict) else {},
            "platform": str(metadata.get("platform") or self.config.get("platform") or "android"),
            "test_mode": test_mode,
            "device_serial": str(
                metadata.get("device_id")
                or metadata.get("adb_serial")
                or self.config.get("device")
                or ""
            ),
            "checkpoint": checkpoint,
            "config": dict(self.config),
            "logs": logs,
            "report_ready": report_exists,
            "command": self.command,
        }


class DashboardManager:
    def __init__(
        self,
        adb: str,
        output_root: Path,
        demo_mode: bool = False,
        ios_python: Optional[str] = None,
        hdc: Optional[str] = None,
    ) -> None:
        self.adb = adb
        self.hdc = hdc or DEFAULT_HDC
        self.ios_python = ios_python or DEFAULT_IOS_PYTHON
        self.output_root = output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.demo_mode = demo_mode
        self.active: Optional[ActiveRun] = None
        self.probe_cache: Dict[str, Dict[str, object]] = {}
        self._android_icon_cache: Dict[str, Optional[str]] = {}
        self._starting = False
        self._device_cache: List[Dict[str, str]] = []
        self._android_device_cache: List[Dict[str, str]] = []
        self._harmony_device_cache: List[Dict[str, str]] = []
        self._ios_device_cache: List[Dict[str, str]] = []
        self._device_error: Optional[str] = None
        self._android_error: Optional[str] = None
        self._harmony_error: Optional[str] = None
        self._ios_error: Optional[str] = None
        self._device_cache_at = 0.0
        self._android_device_cache_at = 0.0
        self._harmony_device_cache_at = 0.0
        self._ios_device_cache_at = 0.0
        self._maintenance_operation: Optional[str] = None
        self._lock = threading.RLock()
        self.source_root = self._discover_source_root()
        self.default_rules_path = self._discover_default_rules_path()

    def _base_command(self) -> List[str]:
        return [
            sys.executable,
            "-m",
            "mobile_profiler",
            "--adb",
            self.adb,
            "--hdc",
            self.hdc,
            "--ios-python",
            self.ios_python,
        ]

    def _discover_source_root(self) -> Optional[Path]:
        package_dir = Path(__file__).resolve().parent
        candidates = list(package_dir.parents)
        seen = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if (
                (resolved / "pyproject.toml").is_file()
                and (resolved / "tools" / "build-portable.ps1").is_file()
                and (resolved / "src" / "mobile_profiler").is_dir()
                and (resolved / "src" / "mobile_profiler").resolve() == package_dir
            ):
                return resolved
        return None

    def _discover_default_rules_path(self) -> Optional[Path]:
        candidates: List[Path] = []
        if self.source_root is not None:
            candidates.append(self.source_root)
        candidates.extend([*Path(__file__).resolve().parents, Path.cwd()])
        seen = set()
        for root in candidates:
            try:
                candidate = (root / "examples" / "btr2-log-rules.json").resolve()
            except OSError:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
        return None

    def _resolve_run_dir(self, payload: Dict[str, object], key: str = "run_name") -> Path:
        raw_value = payload.get(key)
        if raw_value is None and key == "run_name":
            raw_value = payload.get("run_dir")
        value = str(raw_value or "").strip()
        if not value:
            raise ValueError(f"{key} is required")
        path = Path(value).expanduser()
        candidate = path.resolve() if path.is_absolute() else (self.output_root / path).resolve()
        try:
            candidate.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError("Run directory must stay inside the configured output root") from exc
        if not candidate.is_dir() or not (candidate / "metadata.json").is_file():
            raise ValueError(f"Run directory not found or invalid: {candidate}")
        return candidate

    def _resolve_local_path(
        self,
        value: object,
        label: str,
        *,
        require_file: bool = False,
    ) -> Path:
        text = str(value or "").strip().strip('"')
        if not text:
            raise ValueError(f"{label} is required")
        path = Path(text).expanduser()
        candidates = [path] if path.is_absolute() else []
        if self.source_root is not None and not path.is_absolute():
            candidates.append(self.source_root / path)
        if not path.is_absolute():
            candidates.append(Path.cwd() / path)
        resolved = next((item.resolve() for item in candidates if item.exists()), path.resolve())
        if not resolved.exists():
            raise ValueError(f"{label} does not exist: {resolved}")
        if require_file and not resolved.is_file():
            raise ValueError(f"{label} must be a file: {resolved}")
        return resolved

    @staticmethod
    def _payload_lines(value: object) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [line.strip() for line in str(value or "").splitlines() if line.strip()]

    def _ensure_run_idle(self, run_dir: Path) -> None:
        with self._lock:
            active = self.active
        if active is not None and active.running and active.output_dir.resolve() == run_dir:
            raise RuntimeError("Stop and finalize the active recording before maintaining this run")

    def _begin_maintenance(self, operation: str) -> None:
        with self._lock:
            if self._maintenance_operation:
                raise RuntimeError(
                    f"Another maintenance task is still running: {self._maintenance_operation}"
                )
            self._maintenance_operation = operation

    def _finish_maintenance(self) -> None:
        with self._lock:
            self._maintenance_operation = None

    def _run_command(
        self,
        command: Sequence[str],
        operation: str,
        *,
        timeout: float = 600.0,
        cwd: Optional[Path] = None,
    ) -> str:
        self._begin_maintenance(operation)
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            try:
                result = subprocess.run(
                    list(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                    cwd=str(cwd) if cwd is not None else None,
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"{operation} timed out") from exc
            output = "\n".join(
                value.strip() for value in (result.stdout, result.stderr) if value.strip()
            ).strip()
            if result.returncode != 0:
                raise RuntimeError(output or f"{operation} failed with exit code {result.returncode}")
            return output
        finally:
            self._finish_maintenance()

    def _run_cli(
        self,
        arguments: Sequence[str],
        operation: str,
        *,
        timeout: float = 600.0,
    ) -> str:
        return self._run_command(
            self._base_command() + list(arguments),
            operation,
            timeout=timeout,
        )

    def devices(
        self,
        force: bool = False,
        *,
        refresh_android: Optional[bool] = None,
        refresh_ios: Optional[bool] = None,
        refresh_harmony: Optional[bool] = None,
    ) -> tuple[List[Dict[str, str]], Optional[str]]:
        now = time.time()
        with self._lock:
            refresh_android_now = (
                bool(refresh_android)
                if refresh_android is not None
                else force or now - self._android_device_cache_at >= 3.0
            )
            refresh_harmony_now = (
                bool(refresh_harmony)
                if refresh_harmony is not None
                else force or now - self._harmony_device_cache_at >= 3.0
            )
            refresh_ios_now = (
                bool(refresh_ios)
                if refresh_ios is not None
                else force or now - self._ios_device_cache_at >= 15.0
            )
            if not refresh_android_now and not refresh_harmony_now and not refresh_ios_now:
                return list(self._device_cache), self._device_error

        if refresh_android_now:
            android_devices, android_error = list_adb_devices(self.adb)
            android_cache = [{**item, "platform": "android"} for item in android_devices]
        else:
            with self._lock:
                android_cache = list(self._android_device_cache)
                android_error = self._android_error

        if refresh_harmony_now:
            try:
                harmony_cache, harmony_error = list_harmony_devices(self.hdc)
            except Exception as exc:  # HDC is optional for Android/iOS-only hosts
                harmony_cache, harmony_error = [], str(exc)
        else:
            with self._lock:
                harmony_cache = list(self._harmony_device_cache)
                harmony_error = self._harmony_error

        if refresh_ios_now:
            try:
                ios_cache, ios_error = list_ios_devices(self.ios_python)
            except Exception as exc:  # optional sidecar must not break Android discovery
                ios_cache, ios_error = [], str(exc)
        else:
            with self._lock:
                ios_cache = list(self._ios_device_cache)
                ios_error = self._ios_error

        devices = [*android_cache, *harmony_cache, *ios_cache]
        error = android_error if android_error and not devices else None
        if not devices and (harmony_error or ios_error):
            error = " | ".join(
                value for value in (android_error, harmony_error, ios_error) if value
            )
        refreshed_at = time.time()
        with self._lock:
            if refresh_android_now:
                self._android_device_cache = android_cache
                self._android_error = android_error
                self._android_device_cache_at = refreshed_at
            if refresh_harmony_now:
                self._harmony_device_cache = harmony_cache
                self._harmony_error = harmony_error
                self._harmony_device_cache_at = refreshed_at
            if refresh_ios_now:
                self._ios_device_cache = ios_cache
                self._ios_error = ios_error
                self._ios_device_cache_at = refreshed_at
            self._device_cache = devices
            self._device_error = error
            self._device_cache_at = refreshed_at
        return list(devices), error

    def connect_device(self, payload: Dict[str, object]) -> Dict[str, object]:
        address = str(payload.get("address") or "").strip()
        if not address:
            raise ValueError("Enter an ADB IP address, for example 192.168.1.20:5555")
        if len(address) > 255 or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]%]+", address):
            raise ValueError("ADB address contains unsupported characters")
        try:
            result = subprocess.run(
                [self.adb, "connect", address],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("adb connect timed out") from exc
        output = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        ).strip()
        lower = output.lower()
        if result.returncode != 0 or any(
            token in lower for token in ("unable to connect", "failed to connect", "cannot connect")
        ):
            raise RuntimeError(output or f"adb connect {address} failed")
        devices, error = self.devices(force=True, refresh_ios=False, refresh_harmony=False)
        connected_device = next(
            (
                item
                for item in devices
                if item.get("serial") == address and item.get("state") == "device"
            ),
            None,
        )
        connected = connected_device is not None or "connected to" in lower or "already connected" in lower
        return {
            "address": address,
            "output": output or f"adb connect {address} completed",
            "connected": connected,
            "device": connected_device,
            "devices": devices,
            "device_error": error,
        }

    def connect_harmony(self, payload: Dict[str, object]) -> Dict[str, object]:
        address = str(payload.get("address") or "").strip()
        if address.lower().startswith("harmony:"):
            address = address[len("harmony:") :]
        with self._lock:
            active = self.active
        if active is not None and active.running:
            raise RuntimeError("Stop the active recording before changing the HarmonyOS HDC connection")
        result = connect_harmony_device(self.hdc, address)
        devices, error = self.devices(force=True, refresh_ios=False, refresh_harmony=True)
        result["devices"] = devices
        result["device_error"] = error
        return result

    def enable_harmony_tcpip(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Select a USB-connected HarmonyOS device before enabling wireless HDC")
        port = _bounded_int(payload.get("port"), "HDC TCP port", 1, 65535, 8710)
        with self._lock:
            active = self.active
        if active is not None and active.running:
            raise RuntimeError("Stop the active recording before changing HarmonyOS HDC transport")
        result = enable_harmony_tcp(
            self.hdc,
            device,
            port,
            auto_connect=bool(payload.get("auto_connect", True)),
        )
        devices, error = self.devices(force=True, refresh_ios=False, refresh_harmony=True)
        result["devices"] = devices
        result["device_error"] = error
        return result

    def disconnect_device(self, payload: Dict[str, object]) -> Dict[str, object]:
        address = str(payload.get("address") or "").strip()
        if not address:
            raise ValueError("Select a wireless ADB device before disconnecting")
        if len(address) > 255 or any(character.isspace() for character in address):
            raise ValueError("ADB address contains unsupported characters")
        if adb_connection_type(address) != "wireless":
            raise ValueError("Only wireless ADB devices can be disconnected")
        with self._lock:
            active = self.active
        if active is not None and active.running:
            raise RuntimeError("Stop the active recording before disconnecting wireless ADB")
        try:
            result = subprocess.run(
                [self.adb, "disconnect", address],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("adb disconnect timed out") from exc
        output = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        ).strip()
        if result.returncode != 0:
            raise RuntimeError(output or f"adb disconnect {address} failed")
        devices, error = self.devices(force=True, refresh_ios=False, refresh_harmony=False)
        return {
            "address": address,
            "disconnected": not any(item.get("serial") == address for item in devices),
            "output": output or f"adb disconnect {address} completed",
            "devices": devices,
            "device_error": error,
        }

    def enable_tcpip(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Select an authorized ADB device before enabling wireless ADB")
        port = _bounded_int(payload.get("port"), "ADB TCP port", 1, 65535, 5555)
        devices, error = self.devices(force=True, refresh_ios=False, refresh_harmony=False)
        if error:
            raise RuntimeError(error)
        selected_device = next(
            (
                item
                for item in devices
                if item.get("serial") == device and item.get("state") == "device"
            ),
            None,
        )
        if selected_device is None:
            raise RuntimeError(f"ADB device {device!r} is not ready")
        connection_type = str(
            selected_device.get("connection_type") or adb_connection_type(device)
        )
        if connection_type != "usb":
            raise ValueError("Select a USB-connected device before enabling wireless ADB")
        with self._lock:
            active = self.active
        if active is not None and active.running:
            active_device = str(active.config.get("device") or "")
            if not active_device or active_device == device:
                raise RuntimeError(
                    "Stop the active recording before restarting adbd in TCP mode"
                )

        ip_result = adb_shell(
            self.adb,
            device,
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            timeout_s=15,
        )
        addresses = parse_device_ipv4_addresses(ip_result.stdout if ip_result.ok else "")
        try:
            result = subprocess.run(
                [self.adb, "-s", device, "tcpip", str(port)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("adb tcpip timed out") from exc
        tcpip_output = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        ).strip()
        if result.returncode != 0:
            raise RuntimeError(tcpip_output or f"adb tcpip {port} failed")

        network_address = next(
            (item for item in addresses if item.get("wifi")),
            next(
                (
                    item
                    for item in addresses
                    if item.get("private") and not item.get("mobile")
                ),
                None,
            ),
        )
        suggested_address = (
            f"{network_address['address']}:{port}" if network_address else None
        )
        connection: Optional[Dict[str, object]] = None
        connect_error: Optional[str] = None
        if bool(payload.get("auto_connect", True)) and suggested_address:
            time.sleep(1.0)
            try:
                connection = self.connect_device({"address": suggested_address})
            except RuntimeError as exc:
                connect_error = str(exc)
        elif not suggested_address:
            connect_error = (
                "Wireless ADB was enabled, but no reachable Wi-Fi IPv4 address was detected"
            )

        refreshed_devices, refreshed_error = self.devices(
            force=True, refresh_ios=False, refresh_harmony=False
        )
        return {
            "device": device,
            "port": port,
            "tcpip_enabled": True,
            "tcpip_output": tcpip_output or f"adbd restarted on TCP port {port}",
            "addresses": addresses,
            "suggested_address": suggested_address,
            "auto_connect": bool(payload.get("auto_connect", True)),
            "connected": bool(connection and connection.get("connected")),
            "connect_output": connection.get("output") if connection else None,
            "connect_error": connect_error,
            "devices": refreshed_devices,
            "device_error": refreshed_error,
        }

    def _android_brightness_capability(self, device: str) -> Dict[str, object]:
        current_result = adb_shell(
            self.adb,
            device,
            ["settings", "get", "system", "screen_brightness"],
            timeout_s=10,
        )
        if not current_result.ok:
            raise RuntimeError(
                current_result.stderr.strip()
                or current_result.stdout.strip()
                or "Unable to read Android screen brightness"
            )
        float_result = adb_shell(
            self.adb,
            device,
            ["settings", "get", "system", "screen_brightness_float"],
            timeout_s=10,
        )
        mode_result = adb_shell(
            self.adb,
            device,
            ["settings", "get", "system", "screen_brightness_mode"],
            timeout_s=10,
        )
        power_result = adb_shell(
            self.adb,
            device,
            ["dumpsys", "power"],
            timeout_s=20,
        )
        display_id: Optional[int] = None
        display_text = ""
        display_ids_result = adb_shell(
            self.adb,
            device,
            ["cmd", "display", "get-displays", "-i"],
            timeout_s=10,
        )
        display_dump_result = adb_shell(
            self.adb,
            device,
            ["dumpsys", "display"],
            timeout_s=20,
        )
        display_ids = (
            [int(value) for value in re.findall(r"(?m)^\s*(\d+)\s*$", display_ids_result.stdout)]
            if display_ids_result.ok
            else []
        )
        for candidate_id in list(dict.fromkeys([*display_ids, 0])):
            display_result = adb_shell(
                self.adb,
                device,
                ["cmd", "display", "get-brightness", str(candidate_id)],
                timeout_s=10,
            )
            if display_result.ok and _optional_float(display_result.stdout) is not None:
                display_id = candidate_id
                display_text = display_result.stdout
                break
        capability = _parse_android_brightness_capability(
            current_result.stdout,
            float_result.stdout if float_result.ok else "",
            mode_result.stdout if mode_result.ok else "",
            power_result.stdout if power_result.ok else "",
            display_text,
            display_id,
            display_dump_result.stdout if display_dump_result.ok else "",
        )
        capability["device"] = device
        capability["platform"] = "android"
        capability["writable"] = True
        return capability

    def brightness(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Select an Android device before reading brightness")
        requested_platform = str(payload.get("platform") or "android").strip().lower()
        if requested_platform != "android":
            raise ValueError("Numeric brightness control is currently available only for Android")
        devices, error = self.devices(
            force=True,
            refresh_android=True,
            refresh_ios=False,
            refresh_harmony=False,
        )
        if error:
            raise RuntimeError(error)
        selected = next(
            (
                item
                for item in devices
                if item.get("serial") == device
                and item.get("state") == "device"
                and str(item.get("platform") or "android") == "android"
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Android device {device!r} is not ready")

        action = str(payload.get("action") or "read").strip().lower()
        if action not in {"read", "set"}:
            raise ValueError("brightness action must be read or set")
        if action == "set":
            with self._lock:
                active = self.active
            if active is not None and active.running:
                raise RuntimeError("Stop the active recording before changing device brightness")
        capability = self._android_brightness_capability(device)
        if action == "read":
            return capability
        raw_value = payload.get("value")
        try:
            numeric_value = float(raw_value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("brightness value must be an integer") from exc
        if not math.isfinite(numeric_value) or not numeric_value.is_integer():
            raise ValueError("brightness value must be an integer")
        value = int(numeric_value)
        minimum = int(capability["minimum"])
        maximum = int(capability["maximum"])
        if value < minimum or value > maximum:
            raise ValueError(
                f"brightness value must be between {minimum} and {maximum}"
            )

        previous_mode = capability.get("mode")
        mode_changed = previous_mode != 0
        if mode_changed:
            mode_result = adb_shell(
                self.adb,
                device,
                ["settings", "put", "system", "screen_brightness_mode", "0"],
                timeout_s=10,
            )
            if not mode_result.ok:
                raise RuntimeError(
                    mode_result.stderr.strip()
                    or mode_result.stdout.strip()
                    or "Unable to switch Android to manual brightness mode"
                )

        value_result = adb_shell(
            self.adb,
            device,
            ["settings", "put", "system", "screen_brightness", str(value)],
            timeout_s=10,
        )
        if not value_result.ok:
            if mode_changed and previous_mode is not None:
                adb_shell(
                    self.adb,
                    device,
                    [
                        "settings",
                        "put",
                        "system",
                        "screen_brightness_mode",
                        str(previous_mode),
                    ],
                    timeout_s=10,
                )
            raise RuntimeError(
                value_result.stderr.strip()
                or value_result.stdout.strip()
                or "Unable to write Android screen brightness"
            )

        normalized_minimum = float(capability["normalized_minimum"])
        normalized_maximum = float(capability["normalized_maximum"])
        normalized = normalized_minimum + (
            (value - minimum) / max(1, maximum - minimum)
            * (normalized_maximum - normalized_minimum)
        )
        display_normalized = max(0.0, min(1.0, normalized))
        display_value_format = str(
            capability.get("display_value_format") or "normalized"
        )
        # Some OEMs return their raw integer scale from get-brightness, while
        # set-brightness still follows Android's normalized 0..1 shell contract.
        display_value = f"{display_normalized:.7f}"
        display_result = adb_shell(
            self.adb,
            device,
            ["cmd", "display", "set-brightness", display_value],
            timeout_s=10,
        )
        time.sleep(0.1)
        refreshed = self._android_brightness_capability(device)
        warnings: List[str] = []
        if not display_result.ok:
            warnings.append(
                display_result.stderr.strip()
                or display_result.stdout.strip()
                or "The persistent value was written, but DisplayManager did not apply it immediately"
            )
        setting_applied = _integer(refreshed.get("current"), -1) == value
        display_current = _optional_float(refreshed.get("display_current"))
        display_applied: Optional[bool] = None
        if display_current is not None:
            if display_value_format == "raw":
                display_applied = abs(display_current - value) <= 0.5
            else:
                display_applied = (
                    abs(display_current - display_normalized)
                    <= max(
                        1e-4,
                        float(capability.get("normalized_step") or 0.0) * 1.5,
                    )
                )
        refreshed.update(
            {
                "requested": value,
                "display_requested": display_normalized,
                "display_applied": display_applied,
                "applied": setting_applied and display_applied is not False,
                "previous_mode": previous_mode,
                "manual_mode_changed": mode_changed,
                "warnings": warnings,
            }
        )
        return refreshed

    def probe(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Select a device before probing")
        devices, error = self.devices(
            force=True,
            refresh_ios=device.lower().startswith("ios:"),
            refresh_harmony=device.lower().startswith("harmony:"),
        )
        selected = next((item for item in devices if item.get("serial") == device), None)
        if selected is None:
            raise RuntimeError(error or f"Device {device!r} is not available")
        platform = str(selected.get("platform") or "android")
        requested_platform = str(payload.get("platform") or platform).strip().lower()
        if requested_platform not in {"android", "ios", "harmony"}:
            raise ValueError("platform must be android, ios, or harmony")
        if requested_platform != platform:
            raise ValueError(
                f"Selected {platform} device does not match the requested {requested_platform} UI platform"
            )
        command = self._base_command() + [
            "probe",
            "--platform",
            platform,
            "--device",
            device,
            "--json",
        ]
        gpu_path = str(payload.get("gpu_frequency_path") or "").strip()
        if gpu_path and platform == "android":
            command.extend(["--gpu-frequency-path", gpu_path])
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Device probe timed out") from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Probe failed")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Probe returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Probe returned an unexpected response")
        entry = {"device": device, "probed_at": time.time(), "data": data}
        with self._lock:
            self.probe_cache[device] = entry
        return entry

    def scan_android_apps(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Select an Android device before scanning applications")
        requested_platform = str(payload.get("platform") or "android").strip().lower()
        if requested_platform != "android":
            raise ValueError("Application scanning is currently available only for Android")
        devices, error = self.devices(
            force=True,
            refresh_android=True,
            refresh_ios=False,
            refresh_harmony=False,
        )
        if error:
            raise RuntimeError(error)
        selected = next(
            (
                item
                for item in devices
                if item.get("serial") == device and item.get("state") == "device"
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Android device {device!r} is not ready")
        if str(selected.get("platform") or "android") != "android":
            raise ValueError("Selected device is not an Android device")

        third_party_result = adb_shell(
            self.adb,
            device,
            ["pm", "list", "packages", "-3", "-f"],
            timeout_s=20,
        )
        third_party_packages = parse_android_package_list(
            third_party_result.stdout if third_party_result.ok else ""
        )
        package_paths = parse_android_package_paths(
            third_party_result.stdout if third_party_result.ok else ""
        )

        launcher_commands = [
            [
                "cmd",
                "package",
                "query-activities",
                "--brief",
                "--components",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
            ],
            [
                "pm",
                "query-activities",
                "--brief",
                "--components",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
            ],
        ]
        launcher_result = None
        apps: List[Dict[str, object]] = []
        source = "launcher-activities"
        for command in launcher_commands:
            launcher_result = adb_shell(self.adb, device, command, timeout_s=30)
            if not launcher_result.ok:
                continue
            apps = parse_android_launcher_activities(
                launcher_result.stdout,
                third_party_packages,
            )
            if apps:
                break

        warnings: List[str] = []
        if not third_party_result.ok:
            warnings.append(
                third_party_result.stderr.strip()
                or third_party_result.stdout.strip()
                or "Unable to classify third-party packages"
            )
        if not apps:
            if not third_party_packages:
                detail = ""
                if launcher_result is not None:
                    detail = launcher_result.stderr.strip() or launcher_result.stdout.strip()
                raise RuntimeError(detail or "No Android applications were found")
            source = "third-party-packages-fallback"
            apps = [
                {
                    "package": package,
                    "activity": None,
                    "component": None,
                    "activities": [],
                    "user_app": True,
                    "launchable": False,
                }
                for package in third_party_packages
            ]
            warnings.append(
                "The device did not expose launcher activities; showing third-party packages instead"
            )

        icon_count = 0
        icon_attempt_count = 0
        for item in apps:
            if icon_attempt_count >= 48 or not bool(item.get("user_app")):
                continue
            package = str(item.get("package") or "")
            apk_path = package_paths.get(package)
            if not apk_path:
                continue
            icon_attempt_count += 1
            cache_key = f"{device}|{package}|{apk_path}"
            with self._lock:
                cached = self._android_icon_cache.get(cache_key)
                cached_known = cache_key in self._android_icon_cache
            if cached_known:
                if cached:
                    item["icon_data_uri"] = cached
                    icon_count += 1
                continue
            listing = adb_shell(
                self.adb,
                device,
                ["unzip", "-l", apk_path],
                timeout_s=12,
            )
            icon_uri: Optional[str] = None
            if listing.ok:
                candidates = parse_android_apk_icon_candidates(listing.stdout)
                if candidates:
                    resource_name = str(candidates[0]["name"])
                    encoded = adb_shell(
                        self.adb,
                        device,
                        "unzip -p "
                        f"{shlex.quote(apk_path)} {shlex.quote(resource_name)} "
                        "2>/dev/null | base64",
                        timeout_s=12,
                    )
                    if encoded.ok:
                        icon_uri = android_icon_data_uri(
                            encoded.stdout,
                            resource_name,
                        )
            with self._lock:
                self._android_icon_cache[cache_key] = icon_uri
            if icon_uri:
                item["icon_data_uri"] = icon_uri
                icon_count += 1

        return {
            "device": device,
            "platform": "android",
            "source": source,
            "count": len(apps),
            "icon_count": icon_count,
            "apps": apps,
            "warnings": warnings,
            "scanned_at": time.time(),
        }

    def pair_ios(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip() or None
        with self._lock:
            active = self.active
        if active is not None and active.running:
            raise RuntimeError("Stop the active recording before changing iOS RemotePairing")
        devices, error = self.devices(
            force=True,
            refresh_android=False,
            refresh_harmony=False,
            refresh_ios=True,
        )
        if device:
            selected = next(
                (
                    item
                    for item in devices
                    if item.get("serial") == device and item.get("platform") == "ios"
                ),
                None,
            )
            if selected is None:
                raise RuntimeError(error or f"iPhone {device!r} is no longer available")
            if str(selected.get("connection_type") or "").lower() != "usb":
                raise ValueError(
                    "Creating or repairing iOS wireless pairing requires a USB-connected iPhone"
                )
        result = pair_ios_device(device, self.ios_python, 12.0)
        refreshed_devices, refreshed_error = self.devices(
            force=True,
            refresh_android=False,
            refresh_harmony=False,
            refresh_ios=True,
        )
        paired = next(
            (
                item
                for item in refreshed_devices
                if item.get("serial") == result.get("serial")
            ),
            None,
        )
        paired_remote_ready = bool(paired) and str(
            paired.get("remote_xpc_ready")
            if "remote_xpc_ready" in paired
            else paired.get("wireless_ready")
        ).lower() == "true"
        if paired is None or not paired_remote_ready:
            raise RuntimeError(
                refreshed_error
                or "RemotePairing completed, but the iPhone RemoteXPC endpoint failed validation"
            )
        result["device"] = paired
        result["devices"] = refreshed_devices
        return result

    def start_record(self, payload: Dict[str, object]) -> Dict[str, object]:
        with self._lock:
            if self._starting or (self.active is not None and self.active.running):
                raise RuntimeError("A recording is already running")
            self._starting = True

        try:
            return self._start_record(payload)
        finally:
            with self._lock:
                self._starting = False

    def _start_record(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip()
        if not device:
            raise ValueError("Select a device before recording")
        devices, error = self.devices(
            force=True,
            refresh_ios=device.lower().startswith("ios:"),
            refresh_harmony=device.lower().startswith("harmony:"),
        )
        if error:
            raise RuntimeError(error)
        selected = next(
            (
                item
                for item in devices
                if item.get("serial") == device and item.get("state") == "device"
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Device {device!r} is not ready")
        platform = str(selected.get("platform") or "android")
        requested_platform = str(payload.get("platform") or platform).strip().lower()
        if requested_platform not in {"android", "ios", "harmony"}:
            raise ValueError("platform must be android, ios, or harmony")
        if requested_platform != platform:
            raise ValueError(
                f"Selected {platform} device does not match the requested {requested_platform} UI platform"
            )
        test_mode = str(payload.get("test_mode") or "power").strip().lower()
        if test_mode not in {"power", "performance"}:
            raise ValueError("test mode must be power or performance")
        require_unplugged = bool(payload.get("require_unplugged", True))
        if platform == "ios":
            remote_xpc_ready = str(
                selected.get("remote_xpc_ready")
                if "remote_xpc_ready" in selected
                else selected.get("wireless_ready")
            ).strip().lower() == "true"
            unplug_ready = str(
                selected.get("unplug_ready")
                if "unplug_ready" in selected
                else selected.get("wireless_ready")
            ).strip().lower() == "true"
            if not remote_xpc_ready:
                raise ValueError(
                    "iOS 正式录制需要当前可达的 RemotePairing RemoteXPC 端点。请保持 USB 连接并解锁，"
                    "先完成配对；性能或允许外供的测试可使用当前可达端点。"
                )
            if require_unplugged and not unplug_ready:
                endpoint_scope = str(selected.get("endpoint_scope") or "unknown")
                raise ValueError(
                    "当前 iOS RemotePairing 端点尚未证明拔掉 USB 后仍可达"
                    f"（链路范围：{endpoint_scope}）。169.254/16 或 IPv6 link-local 可能来自 USB-NCM，"
                    "不能作为断电功耗测试的无线就绪证据。请拔掉 USB 后刷新设备；只有非链路本地 LAN "
                    "端点仍在线时才能开始，或明确关闭“要求断开外部供电”。"
                )
        capture_preset = str(payload.get("capture_preset") or "auto").strip().lower()
        if capture_preset not in set(capture_preset_names()):
            raise ValueError("unknown capture preset")
        if capture_preset == "harmony-smartperf" and platform != "harmony":
            raise ValueError("Harmony SmartPerf preset requires a HarmonyOS HDC device")
        if capture_preset == "harmony-smartperf" and test_mode != "performance":
            raise ValueError("Harmony SmartPerf preset is available only in performance mode")
        raw_capture_features = payload.get("capture_features")
        capture_features: Dict[str, bool] = {}
        if raw_capture_features is not None:
            if not isinstance(raw_capture_features, dict):
                raise ValueError("capture features must be an object")
            allowed_features = set(capture_feature_names())
            unknown = sorted(set(str(key) for key in raw_capture_features) - allowed_features)
            if unknown:
                raise ValueError(f"unknown capture feature: {unknown[0]}")
            capture_features = {
                name: bool(raw_capture_features[name])
                for name in capture_feature_names()
                if name in raw_capture_features
            }
        harmony_high_performance = bool(payload.get("harmony_high_performance", False))
        if harmony_high_performance and (platform != "harmony" or test_mode != "performance"):
            raise ValueError(
                "HarmonyOS high-performance mode requires a HarmonyOS performance test"
            )

        minimum_duration = (
            4
            if platform == "harmony" and capture_preset == "harmony-smartperf"
            else 8
            if platform == "harmony"
            else 2
        )
        duration = _bounded_int(
            payload.get("duration"),
            "duration",
            minimum_duration,
            604800,
            DEFAULT_PERFORMANCE_UI_DURATION_S
            if test_mode == "performance"
            else DEFAULT_UI_DURATION_S,
        )
        default_interval = 5.0 if platform == "ios" and test_mode == "power" else 1.0
        interval = _bounded_float(
            payload.get("interval"), "interval", 0.2, 60.0, default_interval
        )
        if platform == "harmony" and capture_preset == "harmony-smartperf":
            interval = 1.0
        checkpoint = _bounded_float(
            payload.get("checkpoint_interval"),
            "checkpoint interval",
            5.0,
            3600.0,
            30.0,
        )
        reconnect = _bounded_float(
            payload.get("reconnect_timeout"),
            "reconnect timeout",
            5.0,
            3600.0,
            120.0,
        )
        process_interval = _bounded_float(
            payload.get("process_interval"),
            "process interval",
            2.0,
            600.0,
            10.0,
        )
        thread_interval = _bounded_float(
            payload.get("thread_interval"),
            "thread interval",
            5.0,
            1800.0,
            30.0,
        )
        thermal_interval = (
            5.0
            if platform == "ios"
            else 1.0
            if platform == "harmony" and capture_preset == "harmony-smartperf"
            else _bounded_float(
                payload.get("thermal_interval"),
                "thermal interval",
                2.0,
                600.0,
                10.0,
            )
        )
        scheduler_interval = _bounded_float(
            payload.get("scheduler_interval"),
            "scheduler interval",
            5.0,
            1800.0,
            30.0,
        )
        performance_interval = _bounded_float(
            payload.get("performance_interval"),
            "performance interval",
            5.0 if platform == "harmony" else 1.0,
            60.0,
            5.0
            if platform == "harmony" and test_mode == "performance"
            else 2.0 if test_mode == "performance" else 10.0,
        )
        current_unit = str(payload.get("current_unit") or "auto")
        if current_unit not in {"auto", "ma", "ua"}:
            raise ValueError("current unit must be auto, ma, or ua")

        requested_run_name = sanitize_run_name(payload.get("run_name"))
        requested_output_dir = (self.output_root / requested_run_name).resolve()
        try:
            requested_output_dir.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError("Output directory must stay inside the configured run root") from exc

        title = str(payload.get("title") or "").strip()[:200]
        package = str(payload.get("package") or "").strip()[:200]
        package_required = test_mode == "performance" and (
            platform == "android" or capture_preset == "harmony-smartperf"
        )
        if package_required and not package:
            raise ValueError(
                "Performance tests require a target game or application package"
            )
        start_context = str(payload.get("start_context") or "desktop").strip()
        if start_context not in {"desktop", "app", "other", "unknown"}:
            raise ValueError("start context must be desktop, app, other, or unknown")
        start_note = str(payload.get("start_note") or "").strip()[:500]
        gpu_path = str(payload.get("gpu_frequency_path") or "").strip()[:500]
        session_mode = test_mode == "power" and bool(payload.get("session_mode", True))
        no_reset = bool(payload.get("no_reset", False))
        full_history = bool(payload.get("full_history", False))
        system_monitor = bool(payload.get("system_monitor", True))

        output_dir = _reserve_unique_run_directory(
            self.output_root,
            requested_run_name,
        )
        run_name = output_dir.name

        config: Dict[str, object] = {
            "device": device,
            "platform": platform,
            "test_mode": test_mode,
            "capture_preset": capture_preset,
            "capture_features": capture_features,
            "harmony_high_performance": harmony_high_performance,
            "duration": duration,
            "interval": interval,
            "checkpoint_interval": checkpoint,
            "reconnect_timeout": reconnect,
            "current_unit": current_unit,
            "run_name": run_name,
            "title": title,
            "package": package,
            "start_context": start_context,
            "start_note": start_note,
            "gpu_frequency_path": gpu_path,
            "session_mode": session_mode,
            "require_unplugged": require_unplugged,
            "no_reset": no_reset,
            "full_history": full_history,
            "system_monitor": system_monitor,
            "process_interval": process_interval,
            "thread_interval": thread_interval,
            "thermal_interval": thermal_interval,
            "scheduler_interval": scheduler_interval,
            "performance_interval": performance_interval,
        }
        command = self._base_command() + [
            "record",
            "--platform",
            platform,
            "--test-mode",
            test_mode,
            "--capture-preset",
            capture_preset,
            "--device",
            device,
            "--duration",
            str(duration),
            "--interval",
            str(interval),
            "--checkpoint-interval",
            str(checkpoint),
            "--reconnect-timeout",
            str(reconnect),
            "--current-unit",
            current_unit,
            "--output",
            str(output_dir),
            "--start-context",
            start_context,
            "--process-interval",
            str(process_interval),
            "--thread-interval",
            str(thread_interval),
            "--thermal-interval",
            str(thermal_interval),
            "--scheduler-interval",
            str(scheduler_interval),
            "--performance-interval",
            str(performance_interval),
        ]
        if title:
            command.extend(["--title", title])
        if package:
            command.extend(["--package", package])
        if start_note:
            command.extend(["--start-note", start_note])
        if gpu_path:
            command.extend(["--gpu-frequency-path", gpu_path])
        if session_mode:
            command.append("--session-mode")
        if require_unplugged:
            command.append("--require-unplugged")
        else:
            command.append("--allow-external-power")
        if no_reset:
            command.append("--no-reset")
        if full_history:
            command.append("--full-history")
        for name, enabled in capture_features.items():
            command.extend(
                ["--enable-feature" if enabled else "--disable-feature", name]
            )
        if harmony_high_performance:
            command.append("--harmony-high-performance")
        if not system_monitor and not capture_features:
            command.append("--no-system-monitor")

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=creationflags,
            )
        except Exception:
            try:
                output_dir.rmdir()
            except OSError:
                pass
            raise
        active = ActiveRun(process, output_dir, config, command)
        with self._lock:
            self.active = active
        return self.active_snapshot() or {}

    def stop_record(self) -> Dict[str, object]:
        with self._lock:
            active = self.active
        if active is None or not active.running:
            raise RuntimeError("No recording is currently running")
        active.request_stop()
        return active.snapshot()

    def add_marker(self, payload: Dict[str, object]) -> Dict[str, object]:
        with self._lock:
            active = self.active
        if active is None or not active.running:
            raise RuntimeError("No recording is currently running")
        name = str(payload.get("name") or "").strip()[:120]
        if not name:
            raise ValueError("Marker name is required")
        note = str(payload.get("note") or "").strip()[:500]
        snapshot = active.snapshot()
        latest = snapshot.get("latest", {})
        latest = latest if isinstance(latest, dict) else {}
        uptime_s = latest.get("uptime_s")
        if not isinstance(uptime_s, (int, float)):
            raise RuntimeError("No device uptime sample is available yet")
        context = snapshot.get("context", {})
        context = context if isinstance(context, dict) else {}
        event = {
            "device_uptime_s": float(uptime_s),
            "name": name,
            "phase": "手工标记",
            "kind": "instant",
            "host_epoch_s": time.time(),
            "duration_s": None,
            "source": "runtime_ui",
            "metadata": {
                "note": note,
                "foreground_package": context.get("foreground_package"),
                "foreground_activity": context.get("foreground_activity"),
            },
        }
        path = active.output_dir / "events.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
        with active._lock:
            active.logs.append(
                {
                    "time": time.time(),
                    "source": "ui",
                    "line": f"Marker recorded at device uptime {float(uptime_s):.3f}s: {name}",
                }
            )
        return event

    def active_snapshot(self) -> Optional[Dict[str, object]]:
        with self._lock:
            active = self.active
        if active is None:
            return self._demo_snapshot() if self.demo_mode else None
        snapshot = active.snapshot()
        if snapshot.get("report_ready"):
            snapshot["report_url"] = self.report_url(active.output_dir.name)
        return snapshot

    def report_url(self, run_name: str) -> str:
        return f"/runs/{quote(run_name, safe='')}/report.html"

    def report_path(self, run_name: str) -> Optional[Path]:
        decoded = unquote(run_name)
        candidate = (self.output_root / decoded / "report.html").resolve()
        try:
            candidate.relative_to(self.output_root)
        except ValueError:
            return None
        return candidate if candidate.exists() and candidate.is_file() else None

    def range_summary(self, payload: Dict[str, object]) -> Dict[str, object]:
        requested_run = str(payload.get("run_name") or "").strip()
        excluded_ranges: List[tuple[float, float]] = []
        if requested_run:
            run_dir = self._resolve_run_dir(payload)
            metadata = _read_json(run_dir / "metadata.json")
            excluded_ranges = load_report_excluded_ranges(run_dir)
            source_path = run_dir / REPORT_SOURCE_SAMPLES_FILENAME
            if not source_path.exists():
                source_path = run_dir / "samples.csv"
            samples = read_samples_csv(source_path)
            contexts = load_contexts(run_dir)
            run_name = run_dir.name
        else:
            with self._lock:
                active = self.active
            if active is None:
                raise RuntimeError("No active recording is available for range analysis")
            with active.reader._lock:
                active.reader.refresh()
                samples, _ = active.reader._converted_samples()
                context_map = {
                    (float(item.uptime_s), str(item.source or "")): item
                    for item in [
                        *active.reader.stream_contexts,
                        *active.reader.contexts,
                    ]
                }
                contexts = sorted(
                    context_map.values(),
                    key=lambda item: float(item.uptime_s),
                )
                metadata = dict(active.reader.metadata)
            run_name = active.output_dir.name

        visible_samples = [
            sample
            for sample in samples
            if not any(start <= sample.uptime_s <= end for start, end in excluded_ranges)
        ]
        if len(visible_samples) < 1:
            raise RuntimeError("Run does not contain range-analyzable samples")
        report_edits = metadata.get("report_edits", {})
        report_edits = report_edits if isinstance(report_edits, dict) else {}
        origin_uptime_s = _number(
            report_edits.get("time_origin_uptime_s"),
            float(visible_samples[0].uptime_s),
        )
        try:
            if payload.get("start_uptime_s") is not None:
                start_uptime_s = float(payload.get("start_uptime_s"))
                end_uptime_s = float(payload.get("end_uptime_s"))
            else:
                start_uptime_s = origin_uptime_s + float(payload.get("start_elapsed_s"))
                end_uptime_s = origin_uptime_s + float(payload.get("end_elapsed_s"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Range boundaries must be numbers") from exc
        if not math.isfinite(start_uptime_s) or not math.isfinite(end_uptime_s):
            raise ValueError("Range boundaries must be finite")
        if end_uptime_s <= start_uptime_s:
            raise ValueError("Range end must be greater than range start")

        session_start = float(visible_samples[0].uptime_s)
        session_end = float(visible_samples[-1].uptime_s)
        start_uptime_s = max(session_start, start_uptime_s)
        end_uptime_s = min(session_end, end_uptime_s)
        if end_uptime_s <= start_uptime_s:
            raise ValueError("Selected range does not overlap the recorded session")

        sample_interval_s = max(0.05, _number(metadata.get("sample_interval_s"), 1.0))
        max_gap_s = max(sample_interval_s * 3.0, sample_interval_s + 2.0)
        metrics: Dict[str, Dict[str, object]] = {}

        def add_sample_metric(
            keys: Sequence[str],
            extractor: Callable[[Sample], object],
        ) -> None:
            result = _range_numeric_statistics(
                visible_samples,
                extractor,
                start_uptime_s,
                end_uptime_s,
                max_gap_s,
                excluded_ranges,
            )
            if result is None:
                return
            for key in keys:
                metrics[key] = dict(result)

        ios_system_load_source = "ios_power_telemetry_system_load"
        system_load_observed = any(
            sample.power_source == ios_system_load_source for sample in visible_samples
        )
        add_sample_metric(
            ("power_mw",),
            lambda sample: (
                sample.power_mw
                if not system_load_observed
                or sample.power_source == ios_system_load_source
                else None
            ),
        )
        add_sample_metric(
            ("battery_flow_mw", "battery-flow-power"),
            lambda sample: (
                sample.current_ma * sample.voltage_mv / 1000.0
                if isinstance(sample.current_ma, (int, float))
                and isinstance(sample.voltage_mv, (int, float))
                else None
            ),
        )
        add_sample_metric(("current_ma",), lambda sample: sample.current_ma)
        add_sample_metric(("voltage_mv",), lambda sample: sample.voltage_mv)
        add_sample_metric(("cpu_pct",), lambda sample: sample.cpu_pct)
        add_sample_metric(("gpu_load_pct",), lambda sample: sample.gpu_load_pct)
        add_sample_metric(
            ("gpu_frequency_mhz",),
            lambda sample: sample.gpu_frequency_mhz,
        )
        add_sample_metric(
            ("memory_frequency_mhz",),
            lambda sample: sample.memory_frequency_mhz,
        )
        add_sample_metric(
            ("battery_temperature_c", "temperature_c"),
            lambda sample: sample.battery_temperature_c,
        )
        frequency_keys = sorted(
            {key for sample in visible_samples for key in sample.frequencies_mhz}
        )
        for frequency_key in frequency_keys:
            add_sample_metric(
                (
                    f"cpu_frequency:{frequency_key}",
                    f"cpu-frequency-{frequency_key}",
                ),
                lambda sample, key=frequency_key: sample.frequencies_mhz.get(key),
            )

        selected_contexts = [
            item
            for item in contexts
            if start_uptime_s <= float(item.uptime_s) <= end_uptime_s
            and not any(start <= item.uptime_s <= end for start, end in excluded_ranges)
        ]
        for previous, current in zip(selected_contexts, selected_contexts[1:]):
            if _range_crosses_exclusion(
                float(previous.uptime_s),
                float(current.uptime_s),
                excluded_ranges,
            ):
                current.performance = {
                    **current.performance,
                    "report_break_before": True,
                }
                setattr(current, "_report_break_before", True)
        performance = analyze_performance_contexts(selected_contexts, metadata)

        def add_scalar_metric(
            keys: Sequence[str],
            value: object,
            *,
            sample_count: object = 0,
            calculation: str = "range_recomputed",
            minimum: object = None,
            maximum: object = None,
        ) -> None:
            result = _range_scalar_statistics(
                value,
                sample_count=sample_count,
                calculation=calculation,
            )
            if result is None:
                return
            if isinstance(minimum, (int, float)) and math.isfinite(float(minimum)):
                result["minimum"] = float(minimum)
            if isinstance(maximum, (int, float)) and math.isfinite(float(maximum)):
                result["maximum"] = float(maximum)
            for key in keys:
                metrics[key] = dict(result)

        frame_sample_count = performance.get("frame_sample_count", 0)
        add_scalar_metric(
            ("frame_rate_fps",),
            performance.get("sampled_frame_rate_fps"),
            sample_count=frame_sample_count,
            calculation="frame_rate_recomputed",
            minimum=performance.get("minimum_sampled_frame_rate_fps"),
        )
        add_scalar_metric(
            ("one_percent_low_fps", "frame_rate_fps-overlay"),
            performance.get("one_percent_low_fps"),
            sample_count=frame_sample_count,
            calculation="one_percent_low_recomputed",
        )
        add_scalar_metric(
            ("frame_time_p95_ms", "frame-time-p95"),
            performance.get("frame_metric_p95_ms"),
            sample_count=frame_sample_count,
            calculation="frame_quantile_recomputed",
        )
        add_scalar_metric(
            ("frame_time_p99_ms", "frame-time-p99"),
            performance.get("frame_metric_p99_ms"),
            sample_count=frame_sample_count,
            calculation="frame_quantile_recomputed",
        )
        add_scalar_metric(
            ("frame_issue_pct", "frame-issue"),
            performance.get("frame_issue_pct"),
            sample_count=frame_sample_count,
            calculation="frame_issue_ratio_recomputed",
        )

        refresh_rows = performance.get("refresh_residency", [])
        refresh_rows = refresh_rows if isinstance(refresh_rows, list) else []
        refresh_weights = [
            (
                float(row.get("refresh_rate_hz")),
                float(row.get("estimated_duration_s")),
            )
            for row in refresh_rows
            if isinstance(row, dict)
            and isinstance(row.get("refresh_rate_hz"), (int, float))
            and isinstance(row.get("estimated_duration_s"), (int, float))
            and float(row.get("estimated_duration_s")) > 0
        ]
        total_refresh_duration = sum(duration for _, duration in refresh_weights)
        if total_refresh_duration > 0:
            refresh_rates = [rate for rate, _ in refresh_weights]
            refresh_average = sum(
                rate * duration for rate, duration in refresh_weights
            ) / total_refresh_duration
            refresh_result = {
                "average": refresh_average,
                "minimum": min(refresh_rates),
                "maximum": max(refresh_rates),
                "sample_count": len(refresh_weights),
                "covered_duration_s": total_refresh_duration,
                "calculation": "refresh_residency_weighted",
            }
            metrics["refresh_rate_hz"] = refresh_result

        frame_flow = performance.get("frame_flow", {})
        frame_flow = frame_flow if isinstance(frame_flow, dict) else {}
        stages = frame_flow.get("stages", [])
        stages = stages if isinstance(stages, list) else []
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            stage_key = str(stage.get("key") or index)
            timeline = stage.get("timeline", [])
            timeline = timeline if isinstance(timeline, list) else []
            values = []
            for row in timeline:
                if not isinstance(row, dict):
                    continue
                raw_value = next(
                    (
                        row.get(key)
                        for key in ("value", "frame_rate_fps", "duration_ms", "latency_ms")
                        if isinstance(row.get(key), (int, float))
                    ),
                    None,
                )
                if isinstance(raw_value, (int, float)) and math.isfinite(float(raw_value)):
                    values.append(float(raw_value))
            if not values:
                continue
            stage_result = {
                "average": statistics.fmean(values),
                "minimum": min(values),
                "maximum": max(values),
                "sample_count": len(values),
                "covered_duration_s": None,
                "calculation": "frame_stage_full_resolution",
            }
            metrics[f"frame_stage:{stage_key}"] = dict(stage_result)
            metrics[f"frame-flow-{stage_key}-series"] = dict(stage_result)

        selected_sample_count = sum(
            1
            for sample in visible_samples
            if start_uptime_s <= sample.uptime_s <= end_uptime_s
        )
        return {
            "run_name": run_name,
            "start_elapsed_s": start_uptime_s - origin_uptime_s,
            "end_elapsed_s": end_uptime_s - origin_uptime_s,
            "start_uptime_s": start_uptime_s,
            "end_uptime_s": end_uptime_s,
            "duration_s": end_uptime_s - start_uptime_s,
            "sample_count": selected_sample_count,
            "context_sample_count": len(selected_contexts),
            "full_resolution": True,
            "metrics": metrics,
        }

    def regenerate_run(self, payload: Dict[str, object]) -> Dict[str, object]:
        run_dir = self._resolve_run_dir(payload)
        self._ensure_run_idle(run_dir)
        output = self._run_cli(
            ["report", str(run_dir)],
            f"Regenerating report for {run_dir.name}",
        )
        return {
            "run_name": run_dir.name,
            "report_path": str(run_dir / "report.html"),
            "report_url": self.report_url(run_dir.name),
            "output": output,
        }

    def delete_report_range(self, payload: Dict[str, object]) -> Dict[str, object]:
        run_dir = self._resolve_run_dir(payload)
        self._ensure_run_idle(run_dir)
        try:
            start_uptime_s = float(payload.get("start_uptime_s"))
            end_uptime_s = float(payload.get("end_uptime_s"))
        except (TypeError, ValueError) as exc:
            raise ValueError("start_uptime_s and end_uptime_s must be numbers") from exc
        if not math.isfinite(start_uptime_s) or not math.isfinite(end_uptime_s):
            raise ValueError("Report range boundaries must be finite")
        if end_uptime_s <= start_uptime_s:
            raise ValueError("end_uptime_s must be greater than start_uptime_s")

        samples = read_samples_csv(run_dir / "samples.csv")
        if len(samples) < 2:
            raise RuntimeError("Run does not contain enough report samples")
        existing_ranges = load_report_excluded_ranges(run_dir)
        deleted_sample_count = sum(
            1
            for sample in samples
            if start_uptime_s <= float(sample.uptime_s) <= end_uptime_s
        )
        contexts = load_contexts(run_dir)
        deleted_context_count = sum(
            1
            for context in contexts
            if start_uptime_s <= float(context.uptime_s) <= end_uptime_s
            and not any(
                excluded_start <= float(context.uptime_s) <= excluded_end
                for excluded_start, excluded_end in existing_ranges
            )
        )
        if deleted_sample_count == 0 and deleted_context_count == 0:
            raise ValueError("Selected range does not contain report samples or frame/context data")
        if len(samples) - deleted_sample_count < 2:
            raise ValueError("Deleting this range would leave fewer than two samples")

        edits_path = run_dir / REPORT_EDITS_FILENAME
        previous_edits = edits_path.read_bytes() if edits_path.exists() else None
        try:
            edits = write_report_excluded_ranges(
                run_dir,
                [*existing_ranges, (start_uptime_s, end_uptime_s)],
            )
            output = self._run_cli(
                ["report", str(run_dir)],
                f"Deleting report range for {run_dir.name}",
            )
        except Exception:
            if previous_edits is None:
                edits_path.unlink(missing_ok=True)
            else:
                edits_path.write_bytes(previous_edits)
            raise
        return {
            "run_name": run_dir.name,
            "report_path": str(run_dir / "report.html"),
            "report_url": self.report_url(run_dir.name),
            "deleted_sample_count": deleted_sample_count,
            "deleted_context_count": deleted_context_count,
            "excluded_range_count": len(edits.get("excluded_ranges", [])),
            "output": output,
        }

    def recover_run(self, payload: Dict[str, object]) -> Dict[str, object]:
        run_dir = self._resolve_run_dir(payload)
        self._ensure_run_idle(run_dir)
        output = self._run_cli(
            ["recover", str(run_dir)],
            f"Recovering run {run_dir.name}",
        )
        return {
            "run_name": run_dir.name,
            "report_path": str(run_dir / "report.html"),
            "report_url": self.report_url(run_dir.name),
            "output": output,
        }

    def import_run_log(self, payload: Dict[str, object]) -> Dict[str, object]:
        run_dir = self._resolve_run_dir(payload)
        self._ensure_run_idle(run_dir)
        log_path = self._resolve_local_path(payload.get("log_path"), "BTR2 log", require_file=True)
        rules_value = payload.get("rules_path")
        if not str(rules_value or "").strip() and self.default_rules_path is not None:
            rules_value = str(self.default_rules_path)
        rules_path = self._resolve_local_path(rules_value, "log rule file", require_file=True)
        matches = self._payload_lines(payload.get("match"))
        arguments = [
            "import-log",
            str(run_dir),
            str(log_path),
            "--rules",
            str(rules_path),
        ]
        if bool(payload.get("replace", False)):
            arguments.append("--replace")
        for expression in matches:
            if "=" not in expression:
                raise ValueError(f"Metadata filter must use FIELD=VALUE: {expression}")
            arguments.extend(["--match", expression])
        output = self._run_cli(
            arguments,
            f"Importing BTR2 log into {run_dir.name}",
            timeout=900.0,
        )
        metadata = _read_json(run_dir / "metadata.json")
        imports = metadata.get("log_imports", [])
        latest_import = imports[-1] if isinstance(imports, list) and imports else {}
        return {
            "run_name": run_dir.name,
            "log_path": str(log_path),
            "rules_path": str(rules_path),
            "report_path": str(run_dir / "report.html"),
            "report_url": self.report_url(run_dir.name),
            "import": latest_import if isinstance(latest_import, dict) else {},
            "output": output,
        }

    def comparison_url(self, name: str) -> str:
        return f"/comparisons/{quote(name, safe='')}/comparison.html"

    def comparison_path(self, name: str) -> Optional[Path]:
        decoded = unquote(name)
        root = (self.output_root / "comparisons").resolve()
        candidate = (root / decoded / "comparison.html").resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.exists() and candidate.is_file() else None

    def compare_history_runs(self, payload: Dict[str, object]) -> Dict[str, object]:
        run_a = self._resolve_run_dir(payload, "run_a")
        run_b = self._resolve_run_dir(payload, "run_b")
        if run_a == run_b:
            raise ValueError("Select two different runs for comparison")
        self._ensure_run_idle(run_a)
        self._ensure_run_idle(run_b)
        label_a = str(payload.get("label_a") or run_a.name).strip()[:120]
        label_b = str(payload.get("label_b") or run_b.name).strip()[:120]
        title = str(payload.get("title") or "双机续航与系统活动对比").strip()[:200]
        raw_name = str(payload.get("output_name") or "").strip()
        if not raw_name:
            raw_name = (
                f"compare-{run_a.name}-vs-{run_b.name}-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
        output_name = sanitize_run_name(raw_name)
        comparisons_root = (self.output_root / "comparisons").resolve()
        output_dir = (comparisons_root / output_name).resolve()
        try:
            output_dir.relative_to(comparisons_root)
        except ValueError as exc:
            raise ValueError("Comparison output must stay inside the comparisons directory") from exc
        if output_dir.exists() and any(output_dir.iterdir()):
            raise RuntimeError(f"Comparison output directory is not empty: {output_dir}")
        output = self._run_cli(
            [
                "compare",
                str(run_a),
                str(run_b),
                "--label-a",
                label_a,
                "--label-b",
                label_b,
                "--title",
                title,
                "--output",
                str(output_dir),
            ],
            f"Comparing {run_a.name} and {run_b.name}",
            timeout=1200.0,
        )
        return {
            "run_a": run_a.name,
            "run_b": run_b.name,
            "output_name": output_name,
            "comparison_dir": str(output_dir),
            "comparison_path": str(output_dir / "comparison.html"),
            "comparison_url": self.comparison_url(output_name),
            "output": output,
        }

    def comparisons(self) -> List[Dict[str, object]]:
        root = self.output_root / "comparisons"
        if not root.is_dir():
            return []
        rows: List[Dict[str, object]] = []
        for directory in root.iterdir():
            report = directory / "comparison.html"
            if not directory.is_dir() or not report.is_file():
                continue
            payload = _read_json(directory / "comparison.json")
            try:
                modified = report.stat().st_mtime
            except OSError:
                modified = 0.0
            rows.append(
                {
                    "name": directory.name,
                    "title": str(payload.get("title") or directory.name),
                    "modified_at": modified,
                    "comparison_url": self.comparison_url(directory.name),
                    "output_dir": str(directory),
                }
            )
        rows.sort(key=lambda item: _number(item.get("modified_at")), reverse=True)
        return rows[:20]

    def build_portable_bundle(self, payload: Dict[str, object]) -> Dict[str, object]:
        if self.source_root is None:
            raise RuntimeError("Portable packages can only be rebuilt from the source project")
        with self._lock:
            active = self.active
        if active is not None and active.running:
            raise RuntimeError("Stop the active phone recording before building software")
        dist_root = (self.source_root / "dist").resolve()
        raw_output = str(payload.get("output_directory") or "").strip().strip('"')
        output_dir = (
            Path(raw_output).expanduser().resolve()
            if raw_output and Path(raw_output).expanduser().is_absolute()
            else (
                self.source_root
                / (raw_output or f"dist/mobile-profiler-v{APP_VERSION}-portable")
            ).resolve()
        )
        try:
            output_dir.relative_to(dist_root)
        except ValueError as exc:
            raise ValueError(f"Portable output must stay inside {dist_root}") from exc
        if output_dir == dist_root:
            raise ValueError("Choose a package directory inside dist, not the dist directory itself")
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("PowerShell was not found; portable packaging is available on Windows")
        script = self.source_root / "tools" / "build-portable.ps1"
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-OutputDirectory",
            str(output_dir),
            "-PythonVersion",
            version,
        ]
        include_adb = bool(payload.get("include_adb", True))
        if include_adb:
            adb_path = shutil.which(self.adb)
            if adb_path:
                command.extend(["-AdbPath", adb_path])
        else:
            command.append("-SkipAdb")
        output = self._run_command(
            command,
            "Building portable Windows package",
            timeout=1800.0,
            cwd=self.source_root,
        )
        zip_path = Path(f"{output_dir}.zip")
        if not output_dir.is_dir() or not zip_path.is_file():
            raise RuntimeError("Portable build finished without the expected directory and ZIP")
        return {
            "version": APP_VERSION,
            "bundle_dir": str(output_dir),
            "zip_path": str(zip_path),
            "include_adb": include_adb,
            "output": output,
        }

    def archive_history_run(self, payload: Dict[str, object]) -> Dict[str, object]:
        run_dir = self._resolve_run_dir(payload)
        self._ensure_run_idle(run_dir)
        attachment_values = self._payload_lines(payload.get("attachments") or payload.get("attach"))
        attachments = [
            self._resolve_local_path(value, "attachment") for value in attachment_values
        ]
        archive_dir = self.output_root / "archives"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_output = str(payload.get("output_path") or "").strip().strip('"')
        output = Path(raw_output).expanduser().resolve() if raw_output else (
            archive_dir / f"{run_dir.name}-evidence-{stamp}.zip"
        )
        self._begin_maintenance(f"Archiving {run_dir.name}")
        try:
            archive_path, manifest = create_evidence_archive(
                run_dir,
                output,
                attachments,
                force=bool(payload.get("force", False)),
            )
        finally:
            self._finish_maintenance()
        return {
            "run_name": run_dir.name,
            "archive_path": str(archive_path),
            "entry_count": manifest.get("entry_count", 0),
            "attachment_count": len(attachments),
        }

    def tooling_state(self) -> Dict[str, object]:
        default_output = (
            self.source_root / "dist" / f"mobile-profiler-v{APP_VERSION}-portable"
            if self.source_root is not None
            else None
        )
        with self._lock:
            maintenance_operation = self._maintenance_operation
        return {
            "source_mode": self.source_root is not None,
            "source_root": str(self.source_root) if self.source_root is not None else None,
            "portable_build_available": self.source_root is not None,
            "portable_output_default": str(default_output) if default_output is not None else None,
            "default_rules_path": (
                str(self.default_rules_path) if self.default_rules_path is not None else None
            ),
            "comparisons_root": str(self.output_root / "comparisons"),
            "comparisons": self.comparisons(),
            "busy": maintenance_operation is not None,
            "operation": maintenance_operation,
        }

    def history(self) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        try:
            directories = [item for item in self.output_root.iterdir() if item.is_dir()]
        except OSError:
            return rows
        for directory in directories:
            metadata = _read_json(directory / "metadata.json")
            if not metadata:
                continue
            analysis = _read_json(directory / "analysis.json")
            summary = analysis.get("summary", {}) if analysis else {}
            summary = summary if isinstance(summary, dict) else {}
            device = metadata.get("device", {})
            device = device if isinstance(device, dict) else {}
            try:
                modified = (directory / "metadata.json").stat().st_mtime
            except OSError:
                modified = 0.0
            collection_status = str(metadata.get("collection_status") or "unknown")
            report_ready = (
                (directory / "report.html").exists()
                and collection_status not in {"collecting", "unknown"}
            )
            rows.append(
                {
                    "run_name": directory.name,
                    "title": str(metadata.get("title") or directory.name),
                    "status": collection_status,
                    "device": " ".join(
                        value
                        for value in [str(device.get("brand") or ""), str(device.get("model") or "")]
                        if value
                    ).strip(),
                    "serial": str(metadata.get("device_id") or metadata.get("adb_serial") or ""),
                    "platform": str(metadata.get("platform") or "android"),
                    "duration_s": summary.get("duration_s") or metadata.get("requested_duration_s"),
                    "average_power_mw": summary.get("average_power_mw"),
                    "energy_mwh": summary.get("energy_mwh"),
                    "coverage_pct": summary.get("coverage_pct"),
                    "sample_count": summary.get("sample_count"),
                    "modified_at": modified,
                    "report_ready": report_ready,
                    "report_url": self.report_url(directory.name) if report_ready else None,
                    "output_dir": str(directory),
                }
            )
        rows.sort(key=lambda item: _number(item.get("modified_at")), reverse=True)
        return rows[:30]

    def snapshot(self) -> Dict[str, object]:
        devices, error = self.devices()
        with self._lock:
            probes = dict(self.probe_cache)
        return {
            "version": APP_VERSION,
            "server_time": time.time(),
            "adb": self.adb,
            "hdc": self.hdc,
            "ios_python": self.ios_python,
            "output_root": str(self.output_root),
            "devices": devices,
            "device_error": error,
            "harmony_error": self._harmony_error,
            "ios_error": self._ios_error,
            "probes": probes,
            "active": self.active_snapshot(),
            "history": self.history(),
            "tooling": self.tooling_state(),
            "demo_mode": self.demo_mode,
        }

    def _demo_snapshot(self) -> Dict[str, object]:
        series: List[Dict[str, object]] = []
        for index in range(240):
            baseline = 1480.0 + 170.0 * math.sin(index / 18.0)
            burst = 620.0 * math.exp(-((index - 122.0) / 13.0) ** 2)
            power = baseline + burst + 55.0 * math.sin(index / 3.7)
            cpu = 27.0 + 13.0 * math.sin(index / 15.0) + burst / 45.0
            little_cpu = max(0.0, min(100.0, cpu * 0.82 + 8.0))
            middle_cpu = max(0.0, min(100.0, cpu * 0.58 + burst / 70.0))
            big_cpu = max(0.0, min(100.0, cpu * 0.32 + burst / 95.0))
            temperature = 31.2 + index * 0.007 + 0.15 * math.sin(index / 24.0)
            voltage = 3898.0 - index * 0.12
            series.append(
                {
                    "elapsed_s": float(index),
                    "power_mw": power,
                    "current_ma": power / max(3.6, voltage / 1000.0),
                    "voltage_mv": voltage,
                    "cpu_pct": min(100.0, cpu),
                    "cluster_cpu_pct": {
                        "policy0": little_cpu,
                        "policy4": middle_cpu,
                        "policy7": big_cpu,
                    },
                    "frequencies_mhz": {
                        "policy0": 1050.0 + 360.0 * max(0.0, math.sin(index / 17.0)),
                        "policy4": 1420.0 + 620.0 * max(0.0, math.sin(index / 19.0)),
                        "policy7": 980.0 + 980.0 * max(0.0, math.sin(index / 23.0)),
                    },
                    "temperature_c": temperature,
                    "gpu_frequency_mhz": 420.0 + 220.0 * max(0.0, math.sin(index / 21.0)),
                }
            )
        latest = series[-1]
        powers = [float(item["power_mw"]) for item in series]
        performance_series = [
            {
                "elapsed_s": float(index),
                "uptime_s": 100000.0 + index,
                "frame_rate_fps": max(
                    52.0,
                    118.0
                    - 12.0 * math.exp(-((index - 122.0) / 16.0) ** 2)
                    - 3.0 * max(0.0, math.sin(index / 9.0)),
                ),
                "one_percent_low_fps": max(
                    32.0,
                    104.0
                    - 30.0 * math.exp(-((index - 122.0) / 13.0) ** 2)
                    - 8.0 * max(0.0, math.sin(index / 7.0)),
                ),
                "frame_time_average_ms": 8.5,
                "frame_time_p95_ms": (
                    9.2 + 15.0 * math.exp(-((index - 122.0) / 12.0) ** 2)
                ),
                "frame_time_p99_ms": (
                    12.0 + 26.0 * math.exp(-((index - 122.0) / 10.0) ** 2)
                ),
                "frame_issue_pct": (
                    0.4 + 3.2 * math.exp(-((index - 122.0) / 14.0) ** 2)
                ),
                "refresh_rate_hz": 60.0 if 92 <= index < 124 else 120.0,
            }
            for index in range(2, 240, 2)
        ]
        demo_present_timeline = [
            {
                "elapsed_s": float(item["elapsed_s"]),
                "uptime_s": float(item["uptime_s"]),
                "value": float(item["frame_rate_fps"]),
                "frame_rate_fps": float(item["frame_rate_fps"]),
            }
            for item in performance_series
        ]
        demo_refresh_timeline = [
            {
                "elapsed_s": float(item["elapsed_s"]),
                "uptime_s": float(item["uptime_s"]),
                "value": float(item["refresh_rate_hz"]),
                "refresh_rate_hz": float(item["refresh_rate_hz"]),
            }
            for item in performance_series
        ]
        return {
            "status": "demo",
            "running": False,
            "is_demo": True,
            "test_mode": "performance",
            "title": "Runtime UI preview",
            "run_name": "demo-preview",
            "output_dir": str(self.output_root / "demo-preview"),
            "device_serial": "192.168.21.179:5555",
            "device": {
                "brand": "vivo",
                "model": "V2458A",
                "soc_model": "MT6991",
                "android": "16",
            },
            "sample_count": len(series),
            "elapsed_s": 239.0,
            "requested_duration_s": 360.0,
            "progress": 239.0 / 360.0,
            "metadata": {
                "platform": "android",
                "test_mode": "performance",
                "capture_configuration": {
                    "preset": "performance",
                    "features": {
                        "cpu_usage": True,
                        "cpu_frequency": True,
                        "gpu_metrics": True,
                        "memory_frequency": False,
                        "foreground_window": True,
                        "frame_rate": True,
                        "frame_details": True,
                        "harmony_hitches": False,
                        "touch_events": True,
                        "target_process": True,
                        "process_snapshots": True,
                        "hot_threads": True,
                        "thermal": True,
                        "scheduler": True,
                        "runtime_settings": True,
                        "power_attribution": False,
                    },
                },
            },
            "latest": {
                **latest,
                "uptime_s": 100239.0,
                "signed_current_ma": -float(latest["current_ma"]),
                "gpu_load_pct": 42.0,
            },
            "summary": {
                "average_power_mw": statistics.fmean(powers),
                "p95_power_mw": _percentile(powers, 0.95),
                "average_current_ma": statistics.fmean(
                    [float(item["current_ma"]) for item in series]
                ),
                "average_cpu_pct": statistics.fmean(
                    [float(item["cpu_pct"]) for item in series]
                ),
                "energy_mwh": sum(powers) / 3600.0,
            },
            "series": series,
            "performance_series": performance_series,
            "clusters": [
                {
                    "name": "policy0",
                    "label": "Little",
                    "load_pct": 34.2,
                    "frequency_mhz": 1300.0,
                    "maximum_mhz": 2400.0,
                    "cores": [0, 1, 2, 3],
                },
                {
                    "name": "policy4",
                    "label": "Big",
                    "load_pct": 22.8,
                    "frequency_mhz": 1700.0,
                    "maximum_mhz": 3300.0,
                    "cores": [4, 5, 6],
                },
                {
                    "name": "policy7",
                    "label": "Prime",
                    "load_pct": 8.6,
                    "frequency_mhz": 1200.0,
                    "maximum_mhz": 3730.0,
                    "cores": [7],
                },
            ],
            "context": {
                "uptime_s": 100230.0,
                "foreground_package": "tv.danmaku.bili",
                "foreground_activity": ".MainActivityV2",
                "screen_state": "Awake",
                "brightness_raw": 118.0,
                "refresh_rate_hz": 120.0,
                "performance": {},
                "source": "demo",
            },
            "performance": {
                "available": True,
                "context_sample_count": 24,
                "current_refresh_rate_hz": 120.0,
                "peak_refresh_rate_hz": 120.0,
                "supported_refresh_rates_hz": [60.0, 90.0, 120.0],
                "observed_refresh_rates_hz": [60.0, 120.0],
                "refresh_switch_count": 2,
                "refresh_residency": [
                    {"refresh_rate_hz": 60.0, "estimated_duration_s": 32.0, "share_pct": 13.4},
                    {"refresh_rate_hz": 120.0, "estimated_duration_s": 207.0, "share_pct": 86.6},
                ],
                "refresh_rate_timeline": demo_refresh_timeline,
                "sampled_compositor_fps": 117.8,
                "minimum_sampled_compositor_fps": 92.4,
                "sampled_frame_rate_fps": 117.8,
                "minimum_sampled_frame_rate_fps": 92.4,
                "one_percent_low_fps": 88.6,
                "one_percent_low_source": "demo frame-time histogram slowest 1%",
                "one_percent_low_confidence": "high",
                "frame_metric_p95_ms": 16.72,
                "frame_metric_p99_ms": 28.4,
                "frame_issue_pct": 0.84,
                "frame_issue_label": "超出帧截止时间",
                "frame_source": "Android SurfaceFlinger BLAST layer present timestamps",
                "frame_flow": {
                    "available": True,
                    "platform": "android",
                    "primary_key": "surface_present",
                    "valid_count": 2,
                    "reference_count": 1,
                    "invalid_count": 1,
                    "timeline_stage_count": 2,
                    "note": (
                        "不同阶段的数值语义不同：应用提交速率、合成器呈现 FPS 与屏幕刷新率"
                        "不能直接互换。没有独立计数的节点不会用延迟或刷新率冒充 FPS。"
                    ),
                    "stages": [
                        {
                            "key": "app_submission",
                            "phase": "APP",
                            "label": "应用 / UI 帧提交",
                            "status": "invalid",
                            "value": 0.0,
                            "unit": "帧/s",
                            "value_label": "提交速率",
                            "source": "Android gfxinfo cumulative frame counter delta",
                            "detail": "gfxinfo 累计计数没有增长；原生游戏渲染面未计入该 UI 窗口，不能作为游戏 FPS。",
                            "sample_count": 0,
                            "confidence": "low",
                            "metrics": [],
                            "timeline": [],
                            "timeline_unit": "帧/s",
                            "timeline_value_label": "提交速率",
                        },
                        {
                            "key": "render_queue",
                            "phase": "RENDER",
                            "label": "RenderThread / BufferQueue",
                            "status": "valid",
                            "value": 16.72,
                            "unit": "ms",
                            "value_label": "端到端 P95",
                            "source": "Android gfxinfo framestats timestamps",
                            "detail": "提供 UI、RenderThread、GPU 和 BufferQueue 阶段延迟，不作为独立 FPS。",
                            "sample_count": 864,
                            "confidence": "high",
                            "metrics": [{"label": "截止超时", "value": 0.84, "unit": "%", "digits": 2}],
                            "timeline": [],
                            "timeline_unit": "FPS",
                            "timeline_value_label": "无独立帧率计数",
                        },
                        {
                            "key": "surface_present",
                            "phase": "COMPOSITOR",
                            "label": "SurfaceFlinger 应用层呈现",
                            "status": "primary",
                            "value": 117.8,
                            "unit": "FPS",
                            "value_label": "呈现帧率",
                            "source": "Android SurfaceFlinger BLAST layer present timestamps",
                            "detail": "目标应用 BLAST 层实际 present 时间戳，是当前主 FPS 与 1% Low 数据源。",
                            "sample_count": 2140,
                            "confidence": "high",
                            "metrics": [
                                {"label": "1% Low", "value": 88.6, "unit": "FPS", "digits": 1},
                                {"label": "P95 间隔", "value": 16.72, "unit": "ms", "digits": 2},
                            ],
                            "timeline": demo_present_timeline,
                            "timeline_unit": "FPS",
                            "timeline_value_label": "呈现帧率",
                        },
                        {
                            "key": "display_scanout",
                            "phase": "DISPLAY",
                            "label": "HWC / 屏幕扫描输出",
                            "status": "reference",
                            "value": 120.0,
                            "unit": "Hz",
                            "value_label": "刷新率",
                            "source": "Android SurfaceFlinger refresh-rate duration delta",
                            "detail": "刷新率是屏幕扫描节奏，不等于应用唯一帧；HWC 逐帧 present 计数未公开。",
                            "sample_count": None,
                            "confidence": "high",
                            "metrics": [{"label": "显示/应用倍率", "value": 1.02, "unit": "×", "digits": 2}],
                            "timeline": demo_refresh_timeline,
                            "timeline_unit": "Hz",
                            "timeline_value_label": "显示刷新率",
                        },
                    ],
                },
                "render_pipeline": {
                    "available": True,
                    "frame_count": 864,
                    "p95_total_ms": 16.72,
                    "p99_total_ms": 28.4,
                    "deadline_missed_pct": 0.84,
                    "dominant_stage": {
                        "key": "post_swap_ms",
                        "label": "BufferQueue / 合成等待",
                        "sample_count": 864,
                        "p95_ms": 7.8,
                        "p99_ms": 13.4,
                    },
                    "stages": [
                        {"key": "post_swap_ms", "label": "BufferQueue / 合成等待", "sample_count": 864, "p95_ms": 7.8, "p99_ms": 13.4},
                        {"key": "gpu_wait_ms", "label": "GPU 完成等待", "sample_count": 864, "p95_ms": 5.6, "p99_ms": 9.2},
                        {"key": "command_ms", "label": "渲染命令提交", "sample_count": 864, "p95_ms": 2.7, "p99_ms": 4.1},
                    ],
                    "limitations": "演示数据用于预览渲染链路界面。",
                },
                "frame_interval_average_ms": 8.49,
                "frame_interval_p95_ms": 16.72,
                "frame_sample_count": 2140,
                "missed_vsync_interval_count": 18,
                "missed_vsync_interval_pct": 0.84,
                "severe_frame_interval_count": 2,
                "frozen_frame_interval_count": 0,
                "touch_interaction_count": 37,
                "touch_interactions_per_minute": 9.3,
                "touch_sampling_rate_hz": None,
                "touch_sampling_rate_available": False,
                "touch_sampling_rate_reason": "硬件触控扫描率未通过系统接口公开。",
                "foreground_window_name": "bili0",
                "foreground_window_id": 108,
                "display_width_px": 1320,
                "display_height_px": 2856,
                "render_width_px": 1260,
                "render_height_px": 2736,
                "render_resolution_source": "演示数据 · 模拟 SurfaceFlinger GraphicBuffer",
                "render_resolution_evidence": "SurfaceView demo-preview (BLAST Consumer)",
                "render_resolution_estimated": False,
                "render_resolution_available": True,
                "render_scale_pct": 95.5,
                "frame_interpolation_status": "indeterminate",
                "frame_interpolation_label": "发现显示倍率，但无显式 MEMC 开关证据",
                "frame_interpolation_confidence": "low",
                "frame_interpolation_evidence": [],
                "frame_interpolation_available": False,
                "brightness_raw": 23707,
                "gpu_renderer": "Maleoon 920C",
            },
            "battery": {
                "level_pct": 82.0,
                "voltage_mv": 3898.0,
                "temperature_c": 31.2,
                "status": "discharging",
                "powered": [],
            },
            "warnings": [],
            "checkpoint": {"reconnect_count": 0},
            "system_monitor": {
                "enabled": True,
                "system_snapshot_count": 24,
                "thermal_snapshot_count": 24,
                "scheduler_snapshot_count": 8,
                "process_count": 1042,
                "thread_count": 9968,
                "system_uptime_s": 100239.0,
                "active_priority": [
                    {
                        "pid": 22340,
                        "name": "dex2oat64",
                        "command": "/apex/com.android.art/bin/dex2oat64 --dex-file=/data/app/demo/base.apk",
                        "cpu_pct": 68.4,
                        "policy": "bg",
                        "state": "R",
                        "watch_name": "dex2oat",
                        "watch_kind": "dex_optimization",
                        "watch_label": "DEX AOT 编译",
                        "watch_impact": "ART 正在系统更新后把字节码编译为本地代码。",
                        "activity_active": True,
                    }
                ],
                "watched_processes": [
                    {
                        "pid": 22340,
                        "name": "dex2oat64",
                        "command": "/apex/com.android.art/bin/dex2oat64 --dex-file=/data/app/demo/base.apk",
                        "cpu_pct": 68.4,
                        "policy": "bg",
                        "state": "R",
                        "watch_name": "dex2oat",
                        "watch_kind": "dex_optimization",
                        "watch_label": "DEX AOT 编译",
                        "watch_impact": "ART 正在系统更新后把字节码编译为本地代码。",
                        "activity_active": True,
                    },
                    {
                        "pid": 651,
                        "name": "installd",
                        "command": "/system/bin/installd",
                        "cpu_pct": 0.0,
                        "policy": "fg",
                        "state": "S",
                        "watch_name": "installd",
                        "watch_kind": "package_management",
                        "watch_label": "安装包服务活动",
                        "activity_active": False,
                    },
                ],
                "processes": [
                    {"pid": 22340, "user": "root", "name": "dex2oat64", "cpu_pct": 68.4, "mem_pct": 0.6, "policy": "bg", "state": "R", "category": "dex_optimization"},
                    {"pid": 88, "user": "root", "name": "kworker/u25:2-ufs", "cpu_pct": 18.4, "mem_pct": 0.0, "policy": "bg", "state": "R", "category": "kernel", "activity_label": "kworker · 存储 I/O", "activity_family": "kworker", "subsystem": "storage"},
                    {"pid": 1764, "user": "system", "name": "system_server", "cpu_pct": 18.2, "mem_pct": 4.2, "policy": "fg", "state": "S", "category": "android_system"},
                    {"pid": 1142, "user": "system", "name": "surfaceflinger", "cpu_pct": 12.5, "mem_pct": 0.8, "policy": "fg", "state": "S", "category": "android_system"},
                    {"pid": 13646, "user": "u0_a287", "name": "tv.danmaku.bili", "cpu_pct": 9.8, "mem_pct": 2.4, "policy": "fg", "state": "S", "category": "application"},
                ],
                "threads": [
                    {"pid": 22340, "tid": 22340, "user": "root", "name": "dex2oat64", "process": "dex2oat64", "cpu_pct": 66.1, "policy": "bg", "state": "R"},
                    {"pid": 13646, "tid": 14321, "user": "u0_a287", "name": "HeapTaskDaemon", "process": "tv.danmaku.bili", "cpu_pct": 24.8, "policy": "fg", "state": "R", "activity_label": "ART / GC", "activity_family": "gc", "subsystem": "art_runtime"},
                    {"pid": 88, "tid": 88, "user": "root", "name": "kworker/u25:2-ufs", "process": "[kworker/u25:2-ufs]", "cpu_pct": 18.4, "policy": "bg", "state": "R", "activity_label": "kworker · 存储 I/O", "activity_family": "kworker", "subsystem": "storage"},
                    {"pid": 1764, "tid": 3379, "user": "system", "name": "binder:1764_1E", "process": "system_server", "cpu_pct": 8.1, "policy": "fg", "state": "S"},
                ],
                "thermal": {
                    "uptime_s": 100239.0,
                    "status": 0,
                    "hal_ready": True,
                    "temperatures": [
                        {"name": "CPU", "value_c": 42.6, "type": 0, "status": 0},
                        {"name": "GPU", "value_c": 40.2, "type": 1, "status": 0},
                        {"name": "SKIN", "value_c": 36.8, "type": 3, "status": 0},
                        {"name": "BATTERY", "value_c": 32.9, "type": 2, "status": 0},
                    ],
                    "cooling_devices": [
                        {"name": "lcd-backlight", "value": 0.0, "type": 6},
                        {"name": "wifi-cooler", "value": 0.0, "type": 6},
                    ],
                    "thresholds": [
                        {"name": "CPU", "hot_c": [None, None, None, 95.0, 100.0, 110.0, 117.0]},
                        {"name": "SKIN", "hot_c": [None, 40.0, 43.0, 45.0, 47.0, 60.0, 80.0]},
                    ],
                },
                "scheduler": {
                    "uptime_s": 100230.0,
                    "cpusets": {"top-app": "0-7", "foreground": "0-7", "background": "0-3", "system-background": "0-3", "restricted": "0-7"},
                    "cpu_policies": [
                        {"name": "policy0", "governor": None, "cpuinfo_min_khz": 339000, "cpuinfo_max_khz": 2400000, "status": "limits-only"},
                        {"name": "policy4", "governor": None, "cpuinfo_min_khz": 622000, "cpuinfo_max_khz": 3300000, "status": "limits-only"},
                        {"name": "policy7", "governor": None, "cpuinfo_min_khz": 798000, "cpuinfo_max_khz": 3730000, "status": "limits-only"},
                    ],
                    "hint_sessions": [
                        {"uid": 10287, "pid": 13646, "tids": [14207], "target_duration_ns": 16666666, "allowed_by_proc_state": True, "power_efficient": False, "graphics_pipeline": True}
                    ],
                    "watched_processes": [
                        {"pid": 13646, "uid": 10287, "name": "tv.danmaku.bili", "current_sched_group": 3, "current_proc_state": 2, "adj_type": "top-app", "frozen": False}
                    ],
                    "availability": {"hint_session_supported": True, "cpuset:top-app": "unavailable"},
                    "power_hal": ["mWakefulness=Awake", "mIsPowered=false", "mDeviceIdleMode=false"],
                },
            },
            "logs": [
                {
                    "time": time.time() - 6,
                    "source": "ui",
                    "line": "Demo telemetry loaded. Start a real session when a device is ready.",
                },
                {
                    "time": time.time() - 3,
                    "source": "stderr",
                    "line": "Recording 360s in multi-app session mode...",
                },
            ],
            "report_ready": False,
            "returncode": None,
        }

    def close(self) -> None:
        with self._lock:
            active = self.active
        if active is None or not active.running:
            return
        active.request_stop()
        try:
            active.process.wait(timeout=18)
        except subprocess.TimeoutExpired:
            try:
                active.process.terminate()
            except OSError:
                pass


def _asset_bytes(name: str) -> bytes:
    return files("mobile_profiler").joinpath("web", name).read_bytes()


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], manager: DashboardManager) -> None:
        super().__init__(address, DashboardHandler)
        self.manager = manager


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self, "_response_status", 200) >= 400:
            super().log_message(format, *args)

    def _send_bytes(
        self,
        payload: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache: bool = False,
    ) -> None:
        self._response_status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=300" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Browser polling and tab navigation can cancel a response after the
            # headers were sent. This is a normal client disconnect, not a server
            # failure, and should not fill the long-running UI log with tracebacks.
            return

    def _send_json(self, value: object, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(payload, "application/json; charset=utf-8", status=status)

    def _read_json_body(self) -> Dict[str, object]:
        length = _integer(self.headers.get("Content-Length"), 0)
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send_bytes(_asset_bytes("index.html"), "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._send_bytes(_asset_bytes("app.css"), "text/css; charset=utf-8", cache=True)
            return
        if path == "/app.js":
            self._send_bytes(
                _asset_bytes("app.js"),
                "application/javascript; charset=utf-8",
                cache=True,
            )
            return
        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", status=HTTPStatus.NO_CONTENT, cache=True)
            return
        if path == "/api/state":
            self._send_json(self.server.manager.snapshot())
            return
        if path == "/api/health":
            self._send_json({"ok": True, "time": time.time()})
            return
        match = re.fullmatch(r"/runs/([^/]+)/report\.html", path)
        if match:
            report = self.server.manager.report_path(match.group(1))
            if report is None:
                self._send_json({"error": "Report not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                payload = report.read_bytes()
            except OSError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_bytes(payload, "text/html; charset=utf-8")
            return
        match = re.fullmatch(r"/comparisons/([^/]+)/comparison\.html", path)
        if match:
            report = self.server.manager.comparison_path(match.group(1))
            if report is None:
                self._send_json({"error": "Comparison report not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                payload = report.read_bytes()
            except OSError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_bytes(payload, "text/html; charset=utf-8")
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json_body()
            if path == "/api/probe":
                result = self.server.manager.probe(payload)
            elif path == "/api/apps":
                result = self.server.manager.scan_android_apps(payload)
            elif path == "/api/connect":
                result = self.server.manager.connect_device(payload)
            elif path == "/api/harmony/connect":
                result = self.server.manager.connect_harmony(payload)
            elif path == "/api/disconnect":
                result = self.server.manager.disconnect_device(payload)
            elif path == "/api/tcpip":
                result = self.server.manager.enable_tcpip(payload)
            elif path == "/api/harmony/tcpip":
                result = self.server.manager.enable_harmony_tcpip(payload)
            elif path == "/api/brightness":
                result = self.server.manager.brightness(payload)
            elif path == "/api/ios/pair":
                result = self.server.manager.pair_ios(payload)
            elif path == "/api/record":
                result = self.server.manager.start_record(payload)
            elif path == "/api/stop":
                result = self.server.manager.stop_record()
            elif path == "/api/marker":
                result = self.server.manager.add_marker(payload)
            elif path == "/api/archive":
                result = self.server.manager.archive_history_run(payload)
            elif path == "/api/import-log":
                result = self.server.manager.import_run_log(payload)
            elif path == "/api/report":
                result = self.server.manager.regenerate_run(payload)
            elif path == "/api/range-summary":
                result = self.server.manager.range_summary(payload)
            elif path == "/api/report/delete-range":
                result = self.server.manager.delete_report_range(payload)
            elif path == "/api/recover":
                result = self.server.manager.recover_run(payload)
            elif path == "/api/compare":
                result = self.server.manager.compare_history_runs(payload)
            elif path == "/api/build-portable":
                result = self.server.manager.build_portable_bundle(payload)
            elif path == "/api/devices/refresh":
                platform = str(payload.get("platform") or "").strip().lower()
                if platform and platform not in {"android", "ios", "harmony"}:
                    raise ValueError("platform must be android, ios, or harmony")
                devices, error = self.server.manager.devices(
                    force=True,
                    refresh_android=platform == "android" if platform else None,
                    refresh_ios=platform == "ios" if platform else None,
                    refresh_harmony=platform == "harmony" if platform else None,
                )
                result = {"devices": devices, "error": error}
            else:
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(result)


def serve_dashboard(
    adb: str,
    host: str,
    port: int,
    output_root: Path,
    open_browser: bool = True,
    demo_mode: bool = False,
    ios_python: Optional[str] = None,
    hdc: Optional[str] = None,
) -> int:
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    manager = DashboardManager(
        adb,
        output_root,
        demo_mode=demo_mode,
        ios_python=ios_python,
        hdc=hdc,
    )
    try:
        server = DashboardHTTPServer((host, port), manager)
    except OSError as exc:
        print(f"ERROR: could not start UI server: {exc}", file=sys.stderr)
        return 2
    actual_port = int(server.server_address[1])
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{actual_port}/"
    print("Mobile Profiler UI")
    print(f"Dashboard: {url}")
    print(f"Run root: {manager.output_root}")
    print("Press Ctrl+C to stop the UI server.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        manager.close()
        server.server_close()
    return 0
