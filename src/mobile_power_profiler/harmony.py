from __future__ import annotations

import ipaddress
import json
import math
import os
import queue
import re
import shutil
import signal
import statistics
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, TextIO, Tuple

from .collector import run_command
from .models import (
    ClockSyncPoint,
    CommandResult,
    ContextSample,
    CpuPolicy,
    CpuTimes,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
)
from .storage import RunJournal


HARMONY_DEVICE_PREFIX = "harmony:"
_SAMPLE_BEGIN = "__MPP_HARMONY_SAMPLE_BEGIN__"
_SAMPLE_BATTERY = "__MPP_HARMONY_BATTERY__"
_SAMPLE_CPU = "__MPP_HARMONY_CPU__"
_SAMPLE_END = "__MPP_HARMONY_SAMPLE_END__"


def _discover_hdc() -> str:
    explicit = os.environ.get("HDC")
    if explicit:
        return explicit
    executable = shutil.which("hdc")
    if executable:
        return executable

    candidates: List[Path] = []
    for name in ("DEVECO_SDK_HOME", "OHOS_SDK_HOME", "HARMONY_SDK_HOME"):
        value = os.environ.get(name)
        if not value:
            continue
        root = Path(value).expanduser()
        candidates.extend(
            [
                root / "openharmony" / "toolchains" / "hdc.exe",
                root / "toolchains" / "hdc.exe",
                root / "hdc.exe",
            ]
        )
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates.extend(
        [
            program_files
            / "Huawei"
            / "DevEco Studio"
            / "sdk"
            / "default"
            / "openharmony"
            / "toolchains"
            / "hdc.exe",
            program_files
            / "Huawei"
            / "DevEco Studio"
            / "sdk"
            / "default"
            / "toolchains"
            / "hdc.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "hdc"


DEFAULT_HDC = _discover_hdc()
HARMONY_POWER_MODES = {
    600: "normal",
    601: "power_save",
    602: "performance",
    603: "extreme_power_save",
}


@dataclass
class HarmonyCollectionResult:
    sample_count: int = 0
    context_count: int = 0
    system_snapshot_count: int = 0
    thermal_snapshot_count: int = 0
    scheduler_snapshot_count: int = 0
    clock_sync_count: int = 0
    reconnect_count: int = 0
    sampler_launch_count: int = 0
    host_elapsed_s: float = 0.0
    stop_reason: str = "completed"
    warnings: List[str] = field(default_factory=list)
    battery_end: Dict[str, object] = field(default_factory=dict)
    last_device_uptime_s: Optional[float] = None


def harmony_target(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().startswith(HARMONY_DEVICE_PREFIX):
        return text[len(HARMONY_DEVICE_PREFIX) :]
    return text


def harmony_device_id(target: str) -> str:
    return f"{HARMONY_DEVICE_PREFIX}{harmony_target(target)}"


def harmony_connection_type(target: str) -> str:
    value = harmony_target(target)
    return "wireless" if re.search(r":\d+$", value) else "usb"


def read_harmony_power_mode(
    hdc: str,
    device: str,
) -> Dict[str, object]:
    result = hdc_shell(
        hdc,
        device,
        "power-shell setmode --help; power-shell setmode help",
        timeout_s=10,
    )
    output = "\n".join(value for value in (result.stdout, result.stderr) if value).strip()
    match = re.search(r"current mode is:\s*(\d+)", output, re.IGNORECASE)
    current = int(match.group(1)) if match else None
    supported = "602" in output and "performance mode" in output.lower()
    return {
        "supported": supported,
        "current_mode": current,
        "current_label": HARMONY_POWER_MODES.get(current, "unknown"),
        "performance_mode": 602,
        "command": "power-shell setmode",
        "output": output,
    }


def set_harmony_power_mode(
    hdc: str,
    device: str,
    mode: int,
) -> Dict[str, object]:
    if mode not in HARMONY_POWER_MODES:
        raise ValueError(f"unsupported HarmonyOS power mode: {mode}")
    result = hdc_shell(hdc, device, f"power-shell setmode {mode}", timeout_s=15)
    output = "\n".join(value for value in (result.stdout, result.stderr) if value).strip()
    state = read_harmony_power_mode(hdc, device)
    success = result.ok and (
        "set mode success" in output.lower() or state.get("current_mode") == mode
    )
    return {
        **state,
        "requested_mode": mode,
        "requested_label": HARMONY_POWER_MODES[mode],
        "success": success,
        "set_output": output,
    }


def parse_hdc_targets(text: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    seen = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        target, transport, status = parts[:3]
        transport_upper = transport.upper()
        if transport_upper not in {"USB", "TCP"}:
            continue
        serial = harmony_device_id(target)
        if serial in seen:
            continue
        seen.add(serial)
        ready = status.lower() in {"connected", "ready", "device"}
        devices.append(
            {
                "serial": serial,
                "hdc_target": target,
                "state": "device" if ready else status.lower(),
                "platform": "harmony",
                "connection_type": "wireless" if transport_upper == "TCP" else "usb",
                "transport": transport_upper.lower(),
                "status": status,
                "model": "HarmonyOS device",
            }
        )
    return devices


def list_harmony_devices(hdc: str = DEFAULT_HDC) -> Tuple[List[Dict[str, str]], Optional[str]]:
    result = run_command([hdc, "list", "targets", "-v"], timeout_s=15)
    if not result.ok:
        message = result.stderr.strip() or result.stdout.strip() or "hdc device discovery failed"
        return [], message
    return parse_hdc_targets(result.stdout), None


def connect_harmony_device(
    hdc: str,
    address: str,
    *,
    timeout_s: float = 20.0,
) -> Dict[str, object]:
    target = harmony_target(address)
    if not target or len(target) > 255 or any(character.isspace() for character in target):
        raise ValueError("Enter a valid HarmonyOS HDC address, for example 192.168.1.20:8710")
    result = run_command([hdc, "tconn", target], timeout_s=timeout_s)
    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value.strip()
    ).strip()
    devices, error = list_harmony_devices(hdc)
    serial = harmony_device_id(target)
    selected = next(
        (
            item
            for item in devices
            if item.get("serial") == serial and item.get("state") == "device"
        ),
        None,
    )
    connected = selected is not None
    if not result.ok and not connected:
        raise RuntimeError(output or f"hdc tconn {target} failed")
    return {
        "address": target,
        "serial": serial,
        "connected": connected,
        "output": output or (f"HDC connected to {target}" if connected else "HDC command completed"),
        "device": selected,
        "devices": devices,
        "device_error": error,
    }


def select_harmony_device(
    requested: Optional[str],
    hdc: str = DEFAULT_HDC,
) -> Dict[str, str]:
    requested_target = harmony_target(requested)
    if requested_target and harmony_connection_type(requested_target) == "wireless":
        devices, _ = list_harmony_devices(hdc)
        existing = next(
            (
                item
                for item in devices
                if harmony_target(item.get("serial")) == requested_target
                and item.get("state") == "device"
            ),
            None,
        )
        if existing is None:
            with suppress(RuntimeError, ValueError):
                connect_harmony_device(hdc, requested_target)

    devices, error = list_harmony_devices(hdc)
    ready = [item for item in devices if item.get("state") == "device"]
    if requested_target:
        selected = next(
            (
                item
                for item in devices
                if harmony_target(item.get("serial")) == requested_target
                or item.get("hdc_target") == requested_target
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(error or f"HarmonyOS HDC target {requested_target!r} was not found")
        if selected.get("state") != "device":
            raise RuntimeError(
                f"HarmonyOS HDC target {requested_target!r} is {selected.get('state') or 'offline'}"
            )
        return selected
    if not ready:
        raise RuntimeError(error or "no connected HarmonyOS HDC device was found")
    if len(ready) > 1:
        values = ", ".join(str(item.get("serial")) for item in ready)
        raise RuntimeError(f"multiple HarmonyOS HDC targets are ready; select one with --device: {values}")
    return ready[0]


def hdc_shell(
    hdc: str,
    device: str,
    command: Sequence[str] | str,
    *,
    timeout_s: float = 30.0,
) -> CommandResult:
    argv = [hdc, "-t", harmony_target(device), "shell"]
    if isinstance(command, str):
        argv.append(command)
    else:
        argv.extend(str(value) for value in command)
    return run_command(argv, timeout_s=timeout_s)


def _key_values(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _number(value: object) -> Optional[float]:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_harmony_battery(text: str) -> Dict[str, object]:
    values = _key_values(text)
    level = _number(values.get("capacity"))
    plugged = _number(values.get("pluggedType"))
    charging = _number(values.get("chargingStatus"))
    voltage_uv = _number(values.get("voltage"))
    temperature_tenths = _number(values.get("temperature"))
    current_ma = _number(values.get("nowCurrent"))
    current_average_ma = _number(values.get("currentAverage"))
    plugged_code = int(plugged or 0)
    powered_names = {1: "AC", 2: "USB", 3: "wireless"}
    powered = [powered_names.get(plugged_code, "external")] if plugged_code else []
    charging_code = int(charging or 0)
    if not powered:
        status = "discharging"
    elif charging_code == 1:
        status = "charging"
    elif charging_code == 3:
        status = "full"
    else:
        status = "not_charging"
    result: Dict[str, object] = {
        "level_pct": level,
        "status": status,
        "powered": powered,
        "plugged_type": plugged_code,
        "charging_status": charging_code,
        "present": int(_number(values.get("present")) or 0) != 0,
        "technology": values.get("technology"),
        "current_now_ma": current_ma,
        "current_average_ma": current_average_ma,
        "voltage_mv": voltage_uv / 1000.0 if voltage_uv is not None else None,
        "temperature_c": temperature_tenths / 10.0 if temperature_tenths is not None else None,
        "total_energy_raw": _number(values.get("totalEnergy")),
        "remaining_energy_raw": _number(values.get("remainingEnergy")),
        "remaining_charge_time_raw": _number(values.get("remainingChargeTime")),
        "health_state": _number(values.get("healthState")),
        "charge_type": _number(values.get("chargeType")),
    }
    return {key: value for key, value in result.items() if value is not None}


def parse_harmony_device_info(text: str, serial: Optional[str] = None) -> Dict[str, object]:
    values = _key_values(text)
    cmdline = text.split("/proc/cmdline", 1)[-1] if "/proc/cmdline" in text else text
    soc_match = re.search(r"(?:ohos\.boot\.hardware|ohos\.boot\.chiptype)=([^\s]+)", cmdline)
    build_id = values.get("BuildId", "")
    product_model = values.get("ProductModel", "")
    harmony_version = build_id
    if product_model and build_id.startswith(product_model):
        harmony_version = build_id[len(product_model) :].strip()
    os_full_name = values.get("OSFullName", "")
    openharmony = os_full_name.removeprefix("OpenHarmony-") if os_full_name else None
    return {
        "brand": values.get("Brand") or values.get("Manufacture") or "HUAWEI",
        "manufacturer": values.get("Manufacture") or values.get("Brand"),
        "model": values.get("MarketName") or product_model or "HarmonyOS device",
        "product": product_model or values.get("ProductSeries"),
        "product_model": product_model or None,
        "hardware": values.get("HardwareModel"),
        "soc_model": soc_match.group(1) if soc_match else None,
        "soc_manufacturer": "Huawei",
        "harmony": harmony_version or openharmony,
        "openharmony": openharmony,
        "os_full_name": os_full_name or None,
        "build_id": build_id or None,
        "api_level": _number(values.get("SDKAPIVersion")),
        "security_patch": values.get("SecurityPatch"),
        "device_type": values.get("DeviceType"),
        "abi": values.get("ABIList"),
        "kernel": "HongMeng Kernel" if "Hongmeng version:" in text else None,
        "serial": serial,
    }


def parse_harmony_cpufreq(text: str) -> Dict[int, Dict[str, float]]:
    cores: Dict[int, Dict[str, float]] = {}
    pending: Optional[Tuple[int, str]] = None
    for line in text.splitlines():
        command = re.search(
            r"/cpu/cpu(\d+)/cpufreq/(cpuinfo_cur_freq|cpuinfo_max_freq)", line
        )
        if command:
            pending = (int(command.group(1)), command.group(2))
            continue
        if pending is None:
            continue
        stripped = line.strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?", stripped):
            continue
        core, key = pending
        field = "current_khz" if key == "cpuinfo_cur_freq" else "max_khz"
        cores.setdefault(core, {})[field] = float(stripped)
        pending = None
    return cores


def harmony_cpu_policies(cores: Dict[int, Dict[str, float]]) -> List[CpuPolicy]:
    grouped: Dict[float, List[int]] = {}
    for core, values in cores.items():
        maximum = values.get("max_khz")
        if maximum is None:
            continue
        grouped.setdefault(maximum, []).append(core)
    maxima = sorted(grouped)
    count = len(maxima)
    labels_by_count = {
        1: ["Performance"],
        2: ["Little", "Big"],
        3: ["Little", "Big", "Prime"],
        4: ["Little", "Middle", "Big", "Prime"],
    }
    labels = labels_by_count.get(count, [f"Cluster {index + 1}" for index in range(count)])
    return [
        CpuPolicy(
            name=f"policy{index}",
            path="hidumper --cpufreq",
            cluster_index=index,
            label=labels[index],
            cores=sorted(grouped[maximum]),
            max_khz=maximum,
        )
        for index, maximum in enumerate(maxima)
    ]


def harmony_policy_frequencies_mhz(
    cores: Dict[int, Dict[str, float]],
    policies: Sequence[CpuPolicy],
) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for policy in policies:
        values = [
            cores[core]["current_khz"]
            for core in policy.cores
            if core in cores and "current_khz" in cores[core]
        ]
        if values:
            result[policy.name] = statistics.fmean(values) / 1000.0
    return result


def _percentile_value(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def parse_harmony_render_screen(text: str) -> Dict[str, object]:
    result: Dict[str, object] = {}
    screen_match = re.search(
        r"screen\[(\d+)\]:.*?powerStatus=([A-Z_]+),\s*backlight=([-+]?\d+(?:\.\d+)?).*?"
        r"render resolution=(\d+)x(\d+)",
        text,
    )
    if screen_match:
        result.update(
            {
                "screen_id": int(screen_match.group(1)),
                "display_power_state": screen_match.group(2),
                "brightness_raw": float(screen_match.group(3)),
                "display_width_px": int(screen_match.group(4)),
                "display_height_px": int(screen_match.group(5)),
            }
        )
    active_match = re.search(
        r"activeMode:\s*(\d+)x(\d+),\s*refreshRate=([-+]?\d+(?:\.\d+)?)",
        text,
    )
    if active_match:
        result["display_width_px"] = int(active_match.group(1))
        result["display_height_px"] = int(active_match.group(2))
        result["refresh_rate_hz"] = float(active_match.group(3))
    supported = sorted(
        {
            float(match.group(1))
            for match in re.finditer(
                r"supportedMode\[\d+\]:\s*\d+x\d+,\s*refreshRate=([-+]?\d+(?:\.\d+)?)",
                text,
            )
        }
    )
    if supported:
        result["supported_refresh_rates_hz"] = supported
    return result


def parse_harmony_refresh_counts(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for match in re.finditer(
        r"Refresh\s+Rate:\s*([-+]?\d+(?:\.\d+)?),\s*Count:\s*(\d+)",
        text,
        re.IGNORECASE,
    ):
        rate = float(match.group(1))
        key = str(int(rate)) if rate.is_integer() else f"{rate:g}"
        counts[key] = int(match.group(2))
    return counts


def parse_harmony_compositor_fps(
    text: str,
    refresh_rate_hz: Optional[float] = None,
) -> Dict[str, object]:
    timestamps = [
        int(line.strip())
        for line in text.splitlines()
        if re.fullmatch(r"\d{8,20}", line.strip())
    ]
    if len(timestamps) < 2:
        return {}
    rate = float(refresh_rate_hz) if refresh_rate_hz and refresh_rate_hz > 0 else 60.0
    # RenderService returns newest-first history. Keep about four seconds so
    # successive context samples do not substantially overlap.
    limit = max(16, min(481, int(rate * 4.0) + 1))
    ordered = sorted(set(timestamps[:limit]))
    intervals_ms = [
        (current - previous) / 1_000_000.0
        for previous, current in zip(ordered, ordered[1:])
        if current > previous
    ]
    intervals_ms = [value for value in intervals_ms if 0.1 <= value <= 2000.0]
    if not intervals_ms:
        return {}
    average_ms = statistics.fmean(intervals_ms)
    budget_ms = 1000.0 / rate
    missed = [value for value in intervals_ms if value > budget_ms * 1.5]
    severe = [value for value in intervals_ms if value > budget_ms * 2.5]
    frozen = [value for value in intervals_ms if value > budget_ms * 4.5]
    missed_slots = sum(
        max(0, int(round(value / budget_ms)) - 1)
        for value in intervals_ms
    )
    return {
        "compositor_fps": 1000.0 / average_ms if average_ms > 0 else None,
        "frame_interval_average_ms": average_ms,
        "frame_interval_p95_ms": _percentile_value(intervals_ms, 0.95),
        "frame_interval_maximum_ms": max(intervals_ms),
        "frame_sample_count": len(intervals_ms),
        "missed_vsync_interval_count": len(missed),
        "severe_frame_interval_count": len(severe),
        "frozen_frame_interval_count": len(frozen),
        "missed_vsync_slot_count": missed_slots,
        "frame_budget_ms": budget_ms,
    }


def parse_harmony_window_manager(text: str) -> Dict[str, object]:
    focus_match = re.search(r"^Focus window:\s*(\d+)\s*$", text, re.MULTILINE)
    if not focus_match:
        return {}
    focus_id = int(focus_match.group(1))
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 9 or not parts[2].isdigit() or not parts[3].isdigit():
            continue
        if int(parts[3]) != focus_id:
            continue
        result: Dict[str, object] = {
            "foreground_window_name": parts[0],
            "foreground_window_pid": int(parts[2]),
            "foreground_window_id": focus_id,
        }
        geometry = re.search(r"\[\s*(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s*\]", line)
        if geometry:
            result["foreground_window_width_px"] = int(geometry.group(3))
            result["foreground_window_height_px"] = int(geometry.group(4))
        return result
    return {"foreground_window_id": focus_id}


def parse_harmony_hitches(text: str) -> Dict[str, int]:
    thresholds = {
        "66": "hitch_over_66ms",
        "33": "hitch_over_33ms",
        "16.67": "hitch_over_16_67ms",
    }
    result: Dict[str, int] = {}
    for threshold, key in thresholds.items():
        match = re.search(
            rf"more\s+than\s+{re.escape(threshold)}\s+ms\s+(\d+)",
            text,
            re.IGNORECASE,
        )
        if match:
            result[key] = int(match.group(1))
    return result


def parse_harmony_input_events(text: str) -> Dict[str, object]:
    events: Dict[Tuple[int, str], Dict[str, object]] = {}
    for line in text.splitlines():
        if "eventType:pointer" not in line or "sourceType:touch-screen" not in line:
            continue
        time_match = re.search(r"actionTime:(\d+)", line)
        action_match = re.search(r"pointerAction:([a-zA-Z_-]+)", line)
        if not time_match or not action_match:
            continue
        action_time = int(time_match.group(1))
        action = action_match.group(1).lower()
        events[(action_time, action)] = {"action_time_us": action_time, "action": action}
    ordered = sorted(events.values(), key=lambda item: int(item["action_time_us"]))
    downs = [int(item["action_time_us"]) for item in ordered if item["action"] == "down"]
    moves = [int(item["action_time_us"]) for item in ordered if item["action"] == "move"]
    return {
        "touch_event_count": len(ordered),
        "touch_interaction_count": len(downs),
        "touch_move_event_count": len(moves),
        "touch_down_times_us": downs,
        "latest_touch_action_time_us": (
            int(ordered[-1]["action_time_us"]) if ordered else None
        ),
    }


def parse_harmony_input_devices(text: str) -> List[Dict[str, object]]:
    devices: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    for line in text.splitlines():
        device_match = re.search(
            r"deviceId:(\d+)\s*\|\s*deviceName:([^|]+?)\s*\|\s*deviceType:(\d+)",
            line,
        )
        if device_match:
            if current is not None:
                devices.append(current)
            current = {
                "device_id": int(device_match.group(1)),
                "name": device_match.group(2).strip(),
                "device_type": int(device_match.group(3)),
                "axes": [],
            }
            continue
        axis_match = re.search(
            r"axisType:([A-Z0-9_]+)\s*\|\s*minimum:([-+]?\d+(?:\.\d+)?)\s*\|\s*maximum:([-+]?\d+(?:\.\d+)?)",
            line,
        )
        if current is not None and axis_match:
            axes = current["axes"]
            assert isinstance(axes, list)
            axes.append(
                {
                    "type": axis_match.group(1),
                    "minimum": float(axis_match.group(2)),
                    "maximum": float(axis_match.group(3)),
                }
            )
    if current is not None:
        devices.append(current)
    return [
        item
        for item in devices
        if int(item.get("device_type") or 0) in {17, 19, 35}
        or re.search(r"(?:touch|input_mt|thp|pen)", str(item.get("name") or ""), re.IGNORECASE)
    ]


def parse_harmony_gles(text: str) -> Dict[str, Optional[str]]:
    def value(name: str) -> Optional[str]:
        match = re.search(rf"^{re.escape(name)}:\s*(.+?)\s*$", text, re.MULTILINE)
        return match.group(1).strip() if match else None

    return {
        "vendor": value("GL_VENDOR"),
        "renderer": value("GL_RENDERER"),
        "version": value("GL_VERSION"),
    }


def parse_harmony_thermal(text: str) -> List[Dict[str, object]]:
    temperatures: List[Dict[str, object]] = []
    current_type: Optional[str] = None
    for line in text.splitlines():
        type_match = re.match(r"^\s*Type:\s*(.+?)\s*$", line)
        if type_match:
            current_type = type_match.group(1).strip()
            continue
        value_match = re.match(r"^\s*Temperature:\s*([-+]?\d+(?:\.\d+)?)\s*$", line)
        if not value_match or current_type is None:
            continue
        raw = float(value_match.group(1))
        value_c = raw / 1000.0 if abs(raw) >= 1000.0 else raw
        temperatures.append(
            {
                "name": current_type,
                "type": current_type,
                "value_c": value_c,
                "status": 0,
                "status_available": False,
                "raw_value": raw,
            }
        )
        current_type = None
    return temperatures


def parse_harmony_power_state(text: str) -> Dict[str, object]:
    match = re.search(r"Current State:\s*([A-Z_]+)(?:\s+Reason:\s*([^\r\n]+?))?\s+Time:", text)
    if not match:
        return {}
    state = match.group(1)
    reason = match.group(2).strip() if match.group(2) else None
    if state in {"AWAKE", "DIM"}:
        screen_state = "on"
    elif state in {"DOZE", "STAND_BY", "INACTIVE"}:
        screen_state = "doze"
    elif state in {"SLEEP", "HIBERNATE", "SHUTDOWN"}:
        screen_state = "off"
    else:
        screen_state = state.lower()
    return {"state": state, "reason": reason, "screen_state": screen_state}


def parse_harmony_foreground(text: str) -> Dict[str, Optional[str]]:
    candidates: List[Dict[str, Optional[str]]] = []
    ability_blocks = re.split(r"(?=\n\s*AbilityRecord ID #)", "\n" + text)
    for block in ability_blocks:
        if "#FOREGROUND" not in block:
            continue
        bundle_match = re.search(r"bundle name \[([^\]]+)\]", block)
        main_match = re.search(r"main name \[([^\]]+)\]", block)
        if bundle_match:
            candidates.append(
                {
                    "package": bundle_match.group(1),
                    "activity": main_match.group(1) if main_match else None,
                }
            )
    app_blocks = re.split(r"(?=\n\s*AppRunningRecord ID #)", "\n" + text)
    for block in app_blocks:
        if re.search(r"^\s*state #FOREGROUND\s*$", block, re.MULTILINE) is None:
            continue
        process_match = re.search(r"process name \[([^\]]+)\]", block)
        if process_match:
            candidates.append({"package": process_match.group(1).split(":", 1)[0], "activity": None})
    if not candidates:
        return {"package": None, "activity": None}
    preferred = next(
        (
            item
            for item in candidates
            if item.get("package") not in {"com.ohos.sceneboard", "com.huawei.hmos.launcher"}
        ),
        candidates[0],
    )
    return preferred


def parse_harmony_proc_stat(text: str) -> Dict[str, CpuTimes]:
    counters: Dict[str, CpuTimes] = {}
    for line in text.splitlines():
        match = re.match(r"^(cpu\d*|cpu)\s+(.+)$", line.strip())
        if not match:
            continue
        try:
            values = [float(value) for value in match.group(2).split()[:8]]
        except ValueError:
            continue
        counters[match.group(1)] = CpuTimes.from_values(values)
    return counters


def _cpu_utilization(previous: CpuTimes, current: CpuTimes) -> Optional[float]:
    previous_total, previous_idle = previous.total_and_idle()
    current_total, current_idle = current.total_and_idle()
    delta_total = current_total - previous_total
    delta_idle = current_idle - previous_idle
    if delta_total <= 0:
        return None
    return max(0.0, min(100.0, (delta_total - delta_idle) / delta_total * 100.0))


def build_harmony_sample(
    index: int,
    timestamp_s: float,
    battery_text: str,
    cpu_text: str,
    previous_cpu: Optional[Dict[str, CpuTimes]],
    policies: Sequence[CpuPolicy],
    frequencies_mhz: Dict[str, float],
    *,
    previous_timestamp_s: Optional[float] = None,
    max_cpu_gap_s: Optional[float] = None,
) -> Tuple[Optional[Sample], Dict[str, CpuTimes]]:
    battery = parse_harmony_battery(battery_text)
    counters = parse_harmony_proc_stat(cpu_text)
    signed_current = battery.get("current_now_ma")
    if not isinstance(signed_current, (int, float)):
        signed_current = battery.get("current_average_ma")
    voltage_mv = battery.get("voltage_mv")
    if not isinstance(signed_current, (int, float)) or not isinstance(voltage_mv, (int, float)):
        return None, counters

    can_compare = previous_cpu is not None
    if (
        can_compare
        and max_cpu_gap_s is not None
        and previous_timestamp_s is not None
        and timestamp_s - previous_timestamp_s > max_cpu_gap_s
    ):
        can_compare = False
    cpu_pct: Optional[float] = None
    core_cpu_pct: Dict[str, float] = {}
    if can_compare and previous_cpu is not None:
        if "cpu" in counters and "cpu" in previous_cpu:
            cpu_pct = _cpu_utilization(previous_cpu["cpu"], counters["cpu"])
        for name, current in counters.items():
            match = re.fullmatch(r"cpu(\d+)", name)
            if not match or name not in previous_cpu:
                continue
            value = _cpu_utilization(previous_cpu[name], current)
            if value is not None:
                core_cpu_pct[match.group(1)] = value
    cluster_cpu_pct: Dict[str, float] = {}
    for policy in policies:
        values = [core_cpu_pct[str(core)] for core in policy.cores if str(core) in core_cpu_pct]
        if values:
            cluster_cpu_pct[policy.name] = statistics.fmean(values)

    signed = float(signed_current)
    status = str(battery.get("status") or "unknown")
    if status == "discharging" or signed < 0:
        direction = "discharging"
    elif status in {"charging", "full"} or signed > 0:
        direction = "charging"
    else:
        direction = "idle"
    current_ma = abs(signed)
    sample = Sample(
        index=index,
        elapsed_s=0.0,
        uptime_s=float(timestamp_s),
        current_ma=current_ma,
        signed_current_ma=signed,
        voltage_mv=float(voltage_mv),
        power_mw=current_ma * float(voltage_mv) / 1000.0,
        direction=direction,
        cpu_pct=cpu_pct,
        core_cpu_pct=core_cpu_pct,
        cluster_cpu_pct=cluster_cpu_pct,
        frequencies_mhz=dict(frequencies_mhz),
        battery_temperature_c=(
            float(battery["temperature_c"])
            if isinstance(battery.get("temperature_c"), (int, float))
            else None
        ),
        power_source="harmony_battery_service",
    )
    return sample, counters


class HarmonyFrameParser:
    def __init__(self) -> None:
        self.active = False
        self.section = ""
        self.timestamp: List[str] = []
        self.battery: List[str] = []
        self.cpu: List[str] = []

    def feed_line(self, line: str) -> Optional[Dict[str, str]]:
        stripped = line.rstrip("\r\n")
        marker = stripped.strip()
        if marker == _SAMPLE_BEGIN:
            self.active = True
            self.section = "timestamp"
            self.timestamp = []
            self.battery = []
            self.cpu = []
            return None
        if not self.active:
            return None
        if marker == _SAMPLE_BATTERY:
            self.section = "battery"
            return None
        if marker == _SAMPLE_CPU:
            self.section = "cpu"
            return None
        if marker == _SAMPLE_END:
            self.active = False
            values = {
                "timestamp": "\n".join(self.timestamp),
                "battery": "\n".join(self.battery),
                "cpu": "\n".join(self.cpu),
            }
            self.section = ""
            return values
        if self.section == "timestamp":
            self.timestamp.append(stripped)
        elif self.section == "battery":
            self.battery.append(stripped)
        elif self.section == "cpu":
            self.cpu.append(stripped)
        return None


def parse_harmony_ipv4_addresses(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    interface = "unknown"
    for line in text.splitlines():
        header = re.match(r"^([A-Za-z0-9_.:-]+)\s+Link", line)
        if header:
            interface = header.group(1)
        matches = re.findall(r"(?:inet addr:|\binet\s+)(\d+\.\d+\.\d+\.\d+)", line)
        for value in matches:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not isinstance(address, ipaddress.IPv4Address) or address.is_loopback:
                continue
            rows.append(
                {
                    "interface": interface,
                    "address": value,
                    "wifi": interface.lower().startswith(("wlan", "wifi")),
                    "private": address.is_private,
                }
            )
    rows.sort(key=lambda item: (not bool(item.get("wifi")), not bool(item.get("private"))))
    return rows


def enable_harmony_tcp(
    hdc: str,
    device: str,
    port: int = 8710,
    *,
    auto_connect: bool = True,
) -> Dict[str, object]:
    selected = select_harmony_device(device, hdc)
    target = str(selected["hdc_target"])
    if selected.get("connection_type") != "usb":
        raise ValueError("Select a USB-connected HarmonyOS device before enabling wireless HDC")
    if port < 1 or port > 65535:
        raise ValueError("HDC TCP port must be between 1 and 65535")
    ip_result = hdc_shell(hdc, target, "ifconfig wlan0", timeout_s=15)
    addresses = parse_harmony_ipv4_addresses(ip_result.stdout if ip_result.ok else "")
    result = run_command([hdc, "-t", target, "tmode", "port", str(port)], timeout_s=30)
    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value.strip()
    ).strip()
    if not result.ok:
        raise RuntimeError(output or f"hdc tmode port {port} failed")
    network = next((item for item in addresses if item.get("wifi")), None)
    suggested = f"{network['address']}:{port}" if network else None
    connection: Optional[Dict[str, object]] = None
    connect_error: Optional[str] = None
    if auto_connect and suggested:
        time.sleep(0.8)
        try:
            connection = connect_harmony_device(hdc, suggested)
        except RuntimeError as exc:
            connect_error = str(exc)
    elif not suggested:
        connect_error = "Wireless HDC was enabled, but wlan0 did not expose an IPv4 address"
    devices, error = list_harmony_devices(hdc)
    return {
        "device": selected["serial"],
        "port": port,
        "tcpip_enabled": True,
        "tcpip_output": output or f"HDC is listening on TCP port {port}",
        "addresses": addresses,
        "suggested_address": suggested,
        "suggested_device": harmony_device_id(suggested) if suggested else None,
        "auto_connect": auto_connect,
        "connected": bool(connection and connection.get("connected")),
        "connect_output": connection.get("output") if connection else None,
        "connect_error": connect_error,
        "devices": devices,
        "device_error": error,
    }


def _extract_timestamp(text: str) -> Optional[float]:
    for line in text.splitlines():
        value = _number(line)
        if value is not None and value > 0:
            return value
    return None


def _split_sections(text: str, markers: Sequence[str]) -> Dict[str, str]:
    values: Dict[str, List[str]] = {marker: [] for marker in markers}
    current: Optional[str] = None
    for line in text.splitlines():
        marker = line.strip()
        if marker in values:
            current = marker
            continue
        if current is not None:
            values[current].append(line)
    return {key: "\n".join(lines) for key, lines in values.items()}


def collect_harmony_clock_sync(
    hdc: str,
    device: str,
) -> Optional[ClockSyncPoint]:
    started_epoch = time.time()
    started_monotonic = time.monotonic()
    result = hdc_shell(hdc, device, "date +%s.%N", timeout_s=10)
    ended_epoch = time.time()
    ended_monotonic = time.monotonic()
    timestamp = _extract_timestamp(result.stdout) if result.ok else None
    if timestamp is None:
        return None
    return ClockSyncPoint(
        host_epoch_s=(started_epoch + ended_epoch) / 2.0,
        host_monotonic_s=(started_monotonic + ended_monotonic) / 2.0,
        device_uptime_s=timestamp,
        round_trip_ms=(ended_monotonic - started_monotonic) * 1000.0,
    )


def collect_harmony_context(
    hdc: str,
    device: str,
    *,
    include_power_state: bool = True,
    include_foreground: bool = True,
    include_window: bool = True,
    include_display: bool = True,
    include_frame_rate: bool = True,
    include_hitches: bool = True,
    include_touch: bool = True,
) -> Tuple[Optional[ContextSample], Optional[str]]:
    commands = ["echo __TIME__; date +%s.%N"]
    commands.append(
        "echo __POWER__; hidumper -s PowerManagerService -a '-s'"
        if include_power_state
        else "echo __POWER__"
    )
    commands.append("echo __ABILITY__; aa dump -a" if include_foreground else "echo __ABILITY__")
    commands.append(
        "echo __WINDOW__; hidumper -s WindowManagerService -a '-a'"
        if include_window or include_hitches
        else "echo __WINDOW__"
    )
    commands.append(
        "echo __SCREEN__; hidumper -s RenderService -a 'screen'"
        if include_display
        else "echo __SCREEN__"
    )
    commands.append(
        "echo __FPSCOUNT__; hidumper -s RenderService -a 'fpsCount'"
        if include_frame_rate
        else "echo __FPSCOUNT__"
    )
    commands.append(
        "echo __COMPOSITOR_FPS__; hidumper -s RenderService -a 'composer fps'"
        if include_frame_rate
        else "echo __COMPOSITOR_FPS__"
    )
    commands.append(
        "echo __INPUT__; hidumper -s MultimodalInput -a '-e'"
        if include_touch
        else "echo __INPUT__"
    )
    script = "; ".join(commands)
    result = hdc_shell(hdc, device, script, timeout_s=35)
    if not result.ok:
        return None, result.stderr.strip() or "HarmonyOS context collection failed"
    sections = _split_sections(
        result.stdout,
        (
            "__TIME__",
            "__POWER__",
            "__ABILITY__",
            "__WINDOW__",
            "__SCREEN__",
            "__FPSCOUNT__",
            "__COMPOSITOR_FPS__",
            "__INPUT__",
        ),
    )
    timestamp = _extract_timestamp(sections["__TIME__"])
    if timestamp is None:
        return None, "HarmonyOS context did not include a device timestamp"
    power = parse_harmony_power_state(sections["__POWER__"])
    foreground = parse_harmony_foreground(sections["__ABILITY__"])
    window = parse_harmony_window_manager(sections["__WINDOW__"])
    screen = parse_harmony_render_screen(sections["__SCREEN__"])
    refresh_rate = screen.get("refresh_rate_hz")
    frame_pacing = parse_harmony_compositor_fps(
        sections["__COMPOSITOR_FPS__"],
        float(refresh_rate) if isinstance(refresh_rate, (int, float)) else None,
    )
    touch = parse_harmony_input_events(sections["__INPUT__"])
    performance: Dict[str, object] = {
        **screen,
        **window,
        **frame_pacing,
        **touch,
        "refresh_rate_counts": parse_harmony_refresh_counts(sections["__FPSCOUNT__"]),
        "touch_sampling_rate_hz": None,
        "touch_sampling_rate_available": False,
        "touch_sampling_rate_reason": (
            "HarmonyOS MultimodalInput exposes delivered touch events and axis capabilities, "
            "not the panel controller's hardware scan rate."
        ),
    }
    window_name = str(window.get("foreground_window_name") or "")
    if include_hitches and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", window_name):
        hitch_result = hdc_shell(
            hdc,
            device,
            f"hidumper -s RenderService -a '{window_name} hitchs'",
            timeout_s=12,
        )
        if hitch_result.ok:
            performance.update(parse_harmony_hitches(hitch_result.stdout))
    return (
        ContextSample(
            uptime_s=timestamp,
            foreground_package=foreground.get("package"),
            foreground_activity=foreground.get("activity"),
            screen_state=(
                str(power.get("screen_state"))
                if power.get("screen_state")
                else str(screen.get("display_power_state") or "").lower() or None
            ),
            brightness_raw=(
                float(screen["brightness_raw"])
                if isinstance(screen.get("brightness_raw"), (int, float))
                else None
            ),
            refresh_rate_hz=(
                float(refresh_rate) if isinstance(refresh_rate, (int, float)) else None
            ),
            performance=performance,
            source="harmony_ability_render_service",
        ),
        None,
    )


def _memory_bytes(value: str) -> Optional[int]:
    match = re.fullmatch(r"([0-9.]+)([KMG])?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    scale = {None: 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2).upper() if match.group(2) else None]
    return int(number * scale)


def parse_harmony_ps(text: str) -> Dict[int, Dict[str, object]]:
    rows: Dict[int, Dict[str, object]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("PID"):
            continue
        parts = stripped.split(maxsplit=4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            uid = int(parts[1])
            ppid = int(parts[2])
        except ValueError:
            continue
        rows[pid] = {
            "pid": pid,
            "uid": uid,
            "ppid": ppid,
            "name": parts[3],
            "command": parts[4] if len(parts) > 4 else parts[3],
        }
    return rows


def _process_category(user: str, command: str, ppid: Optional[int]) -> str:
    if command.startswith("["):
        return "kernel"
    if user.isdigit() and int(user) >= 20_000_000:
        return "application"
    if ppid is not None and ppid in {1, 604}:
        return "native_system"
    if user in {"root", "system"}:
        return "native_system"
    return "service"


def _harmony_watch_descriptor(command: str) -> Optional[Dict[str, object]]:
    lower = command.lower()
    definitions = (
        ("system_update", "HarmonyOS system update", ("update_service", "updater", "ouc")),
        ("package_install", "HarmonyOS package installation", ("installs", "bundle_daemon", "appgallery")),
        ("runtime_compile", "Ark/runtime compilation", ("compiler", "aot", "dex2oat")),
    )
    for name, label, tokens in definitions:
        if any(token in lower for token in tokens):
            return {
                "watch_name": name,
                "watch_label": label,
                "watch_impact": "HarmonyOS background platform work can overlap CPU, storage, thermal and power measurements.",
                "activity_kind": name,
                "activity_family": name,
                "activity_label": label,
                "activity_domain": "system",
            }
    return None


def parse_harmony_top(
    text: str,
    ps_rows: Optional[Dict[int, Dict[str, object]]] = None,
) -> List[Dict[str, object]]:
    processes: List[Dict[str, object]] = []
    ps_rows = ps_rows or {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("Tasks:", "Mem:", "Swap:", "PID ")) or "%cpu" in stripped:
            continue
        parts = stripped.split(maxsplit=11)
        if len(parts) != 12:
            continue
        try:
            pid = int(parts[0])
            cpu_pct = float(parts[8].rstrip("%"))
            mem_pct = float(parts[9].rstrip("%"))
        except ValueError:
            continue
        command = parts[11]
        if command.startswith("top ") or command == "top" or "sh -c top -b" in command:
            continue
        ps = ps_rows.get(pid, {})
        name = str(ps.get("name") or command.split(maxsplit=1)[0]).rsplit("/", 1)[-1]
        ppid = ps.get("ppid") if isinstance(ps.get("ppid"), int) else None
        row: Dict[str, object] = {
            "pid": pid,
            "uid": ps.get("uid"),
            "user": parts[1],
            "ppid": ppid,
            "priority": parts[2],
            "nice": _number(parts[3]),
            "policy": None,
            "resident_bytes": _memory_bytes(parts[5]),
            "state": parts[7],
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "name": name,
            "command": str(ps.get("command") or command),
            "category": _process_category(parts[1], command, ppid),
        }
        descriptor = _harmony_watch_descriptor(str(row["command"]))
        if descriptor:
            row.update(descriptor)
            row["activity_active"] = cpu_pct >= 0.5 or str(parts[7]).upper() in {"R", "D"}
        processes.append(row)
    return sorted(processes, key=lambda item: float(item.get("cpu_pct") or 0.0), reverse=True)


def collect_harmony_system_snapshot(
    hdc: str,
    device: str,
) -> Tuple[Optional[SystemSnapshot], Optional[str]]:
    started = time.monotonic()
    script = (
        "echo __TIME__; date +%s.%N; "
        "echo __TOP__; top -b -n 1; "
        "echo __PS__; ps -A -o PID,UID,PPID,NAME,ARGS"
    )
    result = hdc_shell(hdc, device, script, timeout_s=30)
    if not result.ok:
        return None, result.stderr.strip() or "HarmonyOS process snapshot failed"
    sections = _split_sections(result.stdout, ("__TIME__", "__TOP__", "__PS__"))
    timestamp = _extract_timestamp(sections["__TIME__"])
    if timestamp is None:
        return None, "HarmonyOS process snapshot did not include a device timestamp"
    ps_rows = parse_harmony_ps(sections["__PS__"])
    processes = parse_harmony_top(sections["__TOP__"], ps_rows)
    count_match = re.search(r"Tasks:\s*(\d+)\s+total", sections["__TOP__"])
    watched = [item for item in processes if item.get("watch_name")]
    return (
        SystemSnapshot(
            uptime_s=timestamp,
            host_epoch_s=time.time(),
            processes=processes,
            threads=[],
            watched_processes=watched,
            process_count=int(count_match.group(1)) if count_match else len(ps_rows),
            thread_count=None,
            collection_ms=(time.monotonic() - started) * 1000.0,
        ),
        None,
    )


def collect_harmony_thermal_snapshot(
    hdc: str,
    device: str,
) -> Tuple[Optional[ThermalSnapshot], Optional[str]]:
    started = time.monotonic()
    script = "echo __TIME__; date +%s.%N; echo __THERMAL__; hidumper -s ThermalService -a '-t'"
    result = hdc_shell(hdc, device, script, timeout_s=20)
    if not result.ok:
        return None, result.stderr.strip() or "HarmonyOS thermal snapshot failed"
    sections = _split_sections(result.stdout, ("__TIME__", "__THERMAL__"))
    timestamp = _extract_timestamp(sections["__TIME__"])
    if timestamp is None:
        return None, "HarmonyOS thermal snapshot did not include a device timestamp"
    temperatures = parse_harmony_thermal(sections["__THERMAL__"])
    return (
        ThermalSnapshot(
            uptime_s=timestamp,
            host_epoch_s=time.time(),
            status=None,
            hal_ready=bool(temperatures),
            temperatures=temperatures,
            cooling_devices=[],
            thresholds=[],
            headroom_thresholds=[],
            collection_ms=(time.monotonic() - started) * 1000.0,
        ),
        None,
    )


def _cpu_range(cores: Sequence[int]) -> str:
    if not cores:
        return ""
    ordered = sorted(cores)
    if ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{ordered[0]}-{ordered[-1]}" if len(ordered) > 1 else str(ordered[0])
    return ",".join(str(value) for value in ordered)


def collect_harmony_scheduler_snapshot(
    hdc: str,
    device: str,
    policies: Sequence[CpuPolicy],
) -> Tuple[Optional[SchedulerSnapshot], Dict[str, float], Optional[str]]:
    started = time.monotonic()
    script = (
        "echo __TIME__; date +%s.%N; "
        "echo __CPUFREQ__; hidumper --cpufreq; "
        "echo __POWER__; hidumper -s PowerManagerService -a '-s'"
    )
    result = hdc_shell(hdc, device, script, timeout_s=30)
    if not result.ok:
        return None, {}, result.stderr.strip() or "HarmonyOS scheduler snapshot failed"
    sections = _split_sections(result.stdout, ("__TIME__", "__CPUFREQ__", "__POWER__"))
    timestamp = _extract_timestamp(sections["__TIME__"])
    if timestamp is None:
        return None, {}, "HarmonyOS scheduler snapshot did not include a device timestamp"
    cores = parse_harmony_cpufreq(sections["__CPUFREQ__"])
    frequencies = harmony_policy_frequencies_mhz(cores, policies)
    policy_rows = []
    for policy in policies:
        current_values = [
            cores[core]["current_khz"]
            for core in policy.cores
            if core in cores and "current_khz" in cores[core]
        ]
        policy_rows.append(
            {
                "name": policy.name,
                "related_cpus": _cpu_range(policy.cores),
                "cpuinfo_max_khz": policy.max_khz,
                "scaling_max_khz": policy.max_khz,
                "current_khz": statistics.fmean(current_values) if current_values else None,
                "status": "hidumper_cpufreq",
                "governor": None,
            }
        )
    power = parse_harmony_power_state(sections["__POWER__"])
    power_state = []
    if power.get("state"):
        power_state.append(
            f"Harmony PowerManager: {power['state']}"
            + (f" ({power.get('reason')})" if power.get("reason") else "")
        )
    snapshot = SchedulerSnapshot(
        uptime_s=timestamp,
        host_epoch_s=time.time(),
        cpusets={},
        cpu_policies=policy_rows,
        hint_sessions=[],
        watched_processes=[],
        power_hal=power_state,
        availability={
            "harmony_cpufreq": bool(cores),
            "harmony_power_manager": bool(power),
            "adpf_hint_sessions": False,
            "activity_manager": False,
            "reason": "HarmonyOS does not expose Android ADPF or ActivityManager services",
        },
        collection_ms=(time.monotonic() - started) * 1000.0,
    )
    return snapshot, frequencies, None


def probe_harmony_device(
    requested: Optional[str],
    hdc: str = DEFAULT_HDC,
) -> Dict[str, object]:
    selected = select_harmony_device(requested, hdc)
    target = str(selected["hdc_target"])
    power_mode = read_harmony_power_mode(hdc, target)
    battery_result = hdc_shell(hdc, target, "hidumper -s BatteryService -a '-i'", timeout_s=15)
    base_result = hdc_shell(hdc, target, "hidumper -c base", timeout_s=20)
    cpufreq_result = hdc_shell(hdc, target, "hidumper --cpufreq", timeout_s=20)
    thermal_result = hdc_shell(hdc, target, "hidumper -s ThermalService -a '-t'", timeout_s=15)
    power_result = hdc_shell(hdc, target, "hidumper -s PowerManagerService -a '-s'", timeout_s=15)
    ability_result = hdc_shell(hdc, target, "aa dump -a", timeout_s=20)
    stat_result = hdc_shell(hdc, target, "grep '^cpu' /proc/stat", timeout_s=10)
    smartperf_result = hdc_shell(
        hdc,
        target,
        "command -v SP_daemon 2>/dev/null",
        timeout_s=10,
    )
    smartperf_available = smartperf_result.ok and bool(smartperf_result.stdout.strip())
    performance_result = hdc_shell(
        hdc,
        target,
        "echo __SCREEN__; hidumper -s RenderService -a 'screen'; "
        "echo __FPSCOUNT__; hidumper -s RenderService -a 'fpsCount'; "
        "echo __COMPOSITOR_FPS__; hidumper -s RenderService -a 'composer fps'; "
        "echo __WINDOW__; hidumper -s WindowManagerService -a '-a'; "
        "echo __GLES__; hidumper -s RenderService -a 'gles'; "
        "echo __INPUT_DEVICES__; hidumper -s MultimodalInput -a '-d'",
        timeout_s=35,
    )
    if not battery_result.ok:
        raise RuntimeError(battery_result.stderr.strip() or "HarmonyOS BatteryService is unavailable")
    battery = parse_harmony_battery(battery_result.stdout)
    device_info = parse_harmony_device_info(base_result.stdout, selected["serial"])
    cores = parse_harmony_cpufreq(cpufreq_result.stdout)
    policies = harmony_cpu_policies(cores)
    frequencies = harmony_policy_frequencies_mhz(cores, policies)
    temperatures = parse_harmony_thermal(thermal_result.stdout)
    power = parse_harmony_power_state(power_result.stdout)
    foreground = parse_harmony_foreground(ability_result.stdout)
    performance_sections = _split_sections(
        performance_result.stdout if performance_result.ok else "",
        (
            "__SCREEN__",
            "__FPSCOUNT__",
            "__COMPOSITOR_FPS__",
            "__WINDOW__",
            "__GLES__",
            "__INPUT_DEVICES__",
        ),
    )
    display = parse_harmony_render_screen(performance_sections["__SCREEN__"])
    refresh_rate = display.get("refresh_rate_hz")
    frame_pacing = parse_harmony_compositor_fps(
        performance_sections["__COMPOSITOR_FPS__"],
        float(refresh_rate) if isinstance(refresh_rate, (int, float)) else None,
    )
    window = parse_harmony_window_manager(performance_sections["__WINDOW__"])
    gles = parse_harmony_gles(performance_sections["__GLES__"])
    touch_devices = parse_harmony_input_devices(performance_sections["__INPUT_DEVICES__"])
    performance = {
        **display,
        **frame_pacing,
        **window,
        "refresh_rate_counts": parse_harmony_refresh_counts(
            performance_sections["__FPSCOUNT__"]
        ),
        "gpu_vendor": gles.get("vendor"),
        "gpu_renderer": gles.get("renderer"),
        "gpu_api_version": gles.get("version"),
        "touch_device_count": len(touch_devices),
        "touch_sampling_rate_hz": None,
        "touch_sampling_rate_available": False,
        "touch_sampling_rate_reason": (
            "The production MultimodalInput service exposes touch devices and delivered events, "
            "but not the controller hardware scan frequency."
        ),
    }
    system_snapshot, system_error = collect_harmony_system_snapshot(hdc, target)
    connection = {
        "type": selected.get("connection_type"),
        "transport": selected.get("transport"),
        "target": target,
    }
    if selected.get("connection_type") == "wireless" and ":" in target:
        host, port = target.rsplit(":", 1)
        connection.update({"host": host, "port": int(port) if port.isdigit() else port})
    current = battery.get("current_now_ma")
    return {
        "platform": "harmony",
        "device": device_info,
        "device_id": selected["serial"],
        "connection": connection,
        "battery": battery,
        "current_command": f"{current} mA" if isinstance(current, (int, float)) else "unavailable",
        "current_command_ok": isinstance(current, (int, float)),
        "cpu_policies": [asdict(item) for item in policies],
        "cpu_frequencies_mhz": frequencies,
        "display": display,
        "performance": performance,
        "touch": {
            "devices": touch_devices,
            "sampling_rate_hz": None,
            "sampling_rate_available": False,
            "sampling_rate_reason": performance["touch_sampling_rate_reason"],
        },
        "gpu_source": None,
        "gpu_probe": {
            "provider": (
                "HarmonyOS SmartPerf SP_daemon"
                if smartperf_available
                else "HarmonyOS production shell"
            ),
            "model": gles.get("renderer") or device_info.get("soc_model") or "HarmonyOS GPU",
            "vendor": gles.get("vendor"),
            "api_version": gles.get("version"),
            "reason": (
                "GPU sysfs is permission-restricted, but the Harmony SmartPerf preset can request "
                "GPU frequency/load from SP_daemon."
                if smartperf_available
                else "RenderService exposes the GPU renderer, but frequency and load sysfs nodes are "
                "permission-restricted on this production HarmonyOS build"
            ),
        },
        "smartperf": {
            "available": smartperf_available,
            "command": smartperf_result.stdout.strip() or None,
            "provider": "OpenHarmony SmartPerf SP_daemon",
            "fixed_interval_s": 1.0,
        },
        "gpu_work_duration_available": False,
        "gpu_memory_snapshot_available": False,
        "foreground_package": foreground.get("package"),
        "foreground_activity": foreground.get("activity"),
        "foreground_activity": foreground.get("activity"),
        "power_state": power,
        "power_mode": power_mode,
        "capabilities": {
            "battery_service": bool(battery),
            "proc_stat": bool(parse_harmony_proc_stat(stat_result.stdout)),
            "cpufreq": bool(cores),
            "thermal_service": bool(temperatures),
            "power_manager": bool(power),
            "ability_manager": bool(ability_result.stdout.strip()),
            "process_top": system_snapshot is not None and bool(system_snapshot.processes),
            "display_modes": bool(display.get("supported_refresh_rates_hz")),
            "render_service_fps": bool(frame_pacing.get("frame_sample_count")),
            "window_manager": bool(window.get("foreground_window_id")),
            "touch_devices": bool(touch_devices),
            "touch_sampling_rate": False,
            "gpu_renderer": bool(gles.get("renderer")),
            "gpu_frequency": False,
            "gpu_load": False,
            "smartperf_daemon": smartperf_available,
            "smartperf_gpu_metrics": smartperf_available,
            "smartperf_ddr_frequency": smartperf_available,
            "smartperf_app_fps": smartperf_available,
            "performance_power_mode": bool(power_mode.get("supported")),
            "android_batterystats": False,
            "android_adpf": False,
        },
        "system_monitor": {
            "process_top_available": system_snapshot is not None and bool(system_snapshot.processes),
            "process_count": system_snapshot.process_count if system_snapshot else None,
            "process_error": system_error,
            "thermalservice_available": bool(temperatures),
            "thermal_sensor_count": len(temperatures),
            "thermal_threshold_count": 0,
            "thermal_error": None if thermal_result.ok else thermal_result.stderr.strip(),
            "cpusets": {},
            "cpu_policies": [asdict(item) for item in policies],
            "adpf_available": False,
            "adpf_active_session_count": 0,
            "scheduler_warnings": [
                "Android ADPF and ActivityManager services are not present on HarmonyOS; HDC cpufreq and PowerManager snapshots are used instead."
            ],
        },
    }


def _smartperf_number(value: object) -> Optional[float]:
    text = str(value or "").strip()
    if not text or text.upper() == "NA":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _smartperf_percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _smartperf_frequency_mhz(value: object, *, cpu: bool = False) -> Optional[float]:
    parsed = _smartperf_number(value)
    if parsed is None or parsed <= 0:
        return None
    if cpu:
        return parsed / 1000.0 if parsed >= 10_000 else parsed
    if parsed >= 10_000_000:
        return parsed / 1_000_000.0
    if parsed >= 10_000:
        return parsed / 1000.0
    return parsed


def _smartperf_frame_intervals_ms(value: object) -> List[float]:
    intervals: List[float] = []
    for token in re.split(r"[;,|\s]+", str(value or "").strip()):
        parsed = _smartperf_number(token)
        if parsed is None or parsed <= 0:
            continue
        intervals.append(parsed / 1_000_000.0 if parsed >= 100_000 else parsed)
    return intervals


class HarmonySmartPerfParser:
    """Turn SP_daemon's ordered key/value stream into one dictionary per sample."""

    def __init__(self) -> None:
        self.current: Dict[str, str] = {}

    def feed_line(self, line: str) -> Optional[Dict[str, str]]:
        stripped = line.strip()
        match = re.match(r"order:\d+\s+([^=]+)=(.*)$", stripped)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()
            if key == "Battery" and self.current:
                completed = self.current
                self.current = {key: value}
                return completed
            self.current[key] = value
            return None
        if "command exec finished" in stripped.lower():
            return self.finish()
        return None

    def finish(self) -> Optional[Dict[str, str]]:
        if not self.current:
            return None
        completed = self.current
        self.current = {}
        return completed


def parse_harmony_smartperf_output(text: str) -> List[Dict[str, str]]:
    parser = HarmonySmartPerfParser()
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        row = parser.feed_line(line)
        if row:
            rows.append(row)
    final = parser.finish()
    if final:
        rows.append(final)
    return rows


def build_harmony_smartperf_sample(
    record: Dict[str, str],
    index: int,
    policies: Sequence[CpuPolicy],
    *,
    cpu_usage_enabled: bool = True,
    cpu_frequency_enabled: bool = True,
    gpu_metrics_enabled: bool = True,
    memory_frequency_enabled: bool = True,
) -> Optional[Sample]:
    timestamp_ms = _smartperf_number(record.get("timestamp"))
    signed_current = _smartperf_number(record.get("currentNow"))
    voltage_raw = _smartperf_number(record.get("voltageNow"))
    if timestamp_ms is None or signed_current is None or voltage_raw is None:
        return None
    timestamp_s = timestamp_ms / 1000.0 if timestamp_ms > 100_000_000_000 else timestamp_ms
    if abs(signed_current) >= 100_000:
        signed_current /= 1000.0
    voltage_mv = voltage_raw / 1000.0 if voltage_raw >= 100_000 else voltage_raw

    core_cpu_pct: Dict[str, float] = {}
    if cpu_usage_enabled:
        for key, value in record.items():
            match = re.fullmatch(r"cpu(\d+)Usage", key)
            parsed = _smartperf_number(value)
            if match and parsed is not None:
                core_cpu_pct[match.group(1)] = max(0.0, min(100.0, parsed))
    cluster_cpu_pct: Dict[str, float] = {}
    for policy in policies:
        values = [core_cpu_pct[str(core)] for core in policy.cores if str(core) in core_cpu_pct]
        if values:
            cluster_cpu_pct[policy.name] = statistics.fmean(values)

    frequencies_mhz: Dict[str, float] = {}
    if cpu_frequency_enabled:
        for policy in policies:
            values = [
                _smartperf_frequency_mhz(record.get(f"cpu{core}Frequency"), cpu=True)
                for core in policy.cores
            ]
            available = [value for value in values if value is not None]
            if available:
                frequencies_mhz[policy.name] = statistics.fmean(available)

    cpu_pct = _smartperf_number(record.get("TotalcpuUsage")) if cpu_usage_enabled else None
    gpu_frequency = (
        _smartperf_frequency_mhz(record.get("gpuFrequency"))
        if gpu_metrics_enabled
        else None
    )
    gpu_load = _smartperf_number(record.get("gpuLoad")) if gpu_metrics_enabled else None
    memory_frequency = (
        _smartperf_frequency_mhz(record.get("ddrFrequency"))
        if memory_frequency_enabled
        else None
    )
    signed = float(signed_current)
    current_ma = abs(signed)
    return Sample(
        index=index,
        elapsed_s=0.0,
        uptime_s=float(timestamp_s),
        current_ma=current_ma,
        signed_current_ma=signed,
        voltage_mv=float(voltage_mv),
        power_mw=current_ma * float(voltage_mv) / 1000.0,
        direction="discharging" if signed < 0 else "charging" if signed > 0 else "idle",
        cpu_pct=(max(0.0, min(100.0, float(cpu_pct))) if cpu_pct is not None else None),
        core_cpu_pct=core_cpu_pct,
        cluster_cpu_pct=cluster_cpu_pct,
        frequencies_mhz=frequencies_mhz,
        gpu_frequency_mhz=gpu_frequency,
        gpu_load_pct=(max(0.0, min(100.0, gpu_load)) if gpu_load is not None else None),
        memory_frequency_mhz=memory_frequency,
        battery_temperature_c=_smartperf_number(record.get("Battery")),
        power_source="harmony_smartperf_battery",
    )


def build_harmony_smartperf_context(
    record: Dict[str, str],
    sample: Sample,
    target_package: str,
    *,
    foreground_activity: Optional[str] = None,
    base_performance: Optional[Dict[str, object]] = None,
    frame_rate_enabled: bool = True,
    frame_details_enabled: bool = True,
) -> ContextSample:
    refresh_rate = _smartperf_number(record.get("refreshrate"))
    fps = _smartperf_number(record.get("fps")) if frame_rate_enabled else None
    intervals = (
        _smartperf_frame_intervals_ms(record.get("fpsJitters"))
        if frame_rate_enabled and frame_details_enabled
        else []
    )
    refresh_interval_ms = (
        1000.0 / refresh_rate
        if isinstance(refresh_rate, (int, float)) and refresh_rate > 0
        else None
    )
    missed = (
        sum(1 for value in intervals if value > refresh_interval_ms * 1.5)
        if refresh_interval_ms is not None
        else 0
    )
    severe = (
        sum(1 for value in intervals if value > refresh_interval_ms * 2.5)
        if refresh_interval_ms is not None
        else 0
    )
    frozen = sum(1 for value in intervals if value >= 700.0)
    slowest_count = max(1, math.ceil(len(intervals) * 0.01)) if intervals else 0
    slowest_average = (
        statistics.fmean(sorted(intervals, reverse=True)[:slowest_count])
        if slowest_count
        else None
    )
    performance = dict(base_performance or {})
    performance.update(
        {
            "platform": "harmony",
            "foreground_window_name": performance.get("foreground_window_name") or target_package,
            "frame_counter_source": "HarmonyOS SmartPerf SP_daemon app FPS",
            "compositor_fps": fps,
            "frame_sample_count": len(intervals) or (max(0, int(round(fps))) if fps is not None else 0),
            "frame_interval_average_ms": statistics.fmean(intervals) if intervals else None,
            "frame_interval_p95_ms": _smartperf_percentile(intervals, 0.95),
            "frame_interval_p99_ms": _smartperf_percentile(intervals, 0.99),
            "one_percent_low_fps": (
                1000.0 / slowest_average
                if isinstance(slowest_average, (int, float)) and slowest_average > 0
                else None
            ),
            "missed_vsync_interval_count": missed,
            "severe_frame_interval_count": severe,
            "frozen_frame_interval_count": frozen,
            "frame_interval_values_ms": intervals,
            "smartperf_process_id": (
                int(value)
                if (value := _smartperf_number(record.get("ProcId"))) is not None
                else None
            ),
            "smartperf_process_cpu_pct": _smartperf_number(record.get("ProcCpuUsage")),
            "smartperf_process_pss_kb": _smartperf_number(record.get("pss")),
            "smartperf_source": "SP_daemon",
        }
    )
    return ContextSample(
        uptime_s=sample.uptime_s,
        foreground_package=target_package,
        foreground_activity=foreground_activity,
        screen_state="awake",
        brightness_raw=(
            float(performance["brightness_raw"])
            if isinstance(performance.get("brightness_raw"), (int, float))
            else None
        ),
        refresh_rate_hz=(
            float(refresh_rate) if isinstance(refresh_rate, (int, float)) else None
        ),
        source="harmony_smartperf",
        performance=performance,
    )


def _smartperf_thermal_snapshot(record: Dict[str, str], sample: Sample) -> ThermalSnapshot:
    sensors: List[Dict[str, object]] = []
    for key, value in record.items():
        lowered = key.lower()
        if not (
            lowered.endswith("thermal")
            or lowered.startswith("shell_")
            or lowered in {"battery", "system_h"}
        ):
            continue
        parsed = _smartperf_number(value)
        if parsed is None or not -20.0 <= parsed <= 150.0:
            continue
        sensors.append(
            {
                "name": key,
                "type": key,
                "value_c": parsed,
                "status": None,
                "status_available": False,
            }
        )
    return ThermalSnapshot(
        uptime_s=sample.uptime_s,
        host_epoch_s=time.time(),
        status=None,
        hal_ready=bool(sensors),
        temperatures=sensors,
        cooling_devices=[],
        thresholds=[],
        headroom_thresholds=[],
        collection_ms=None,
    )


def _smartperf_target_snapshot(
    record: Dict[str, str], sample: Sample, target_package: str
) -> Optional[SystemSnapshot]:
    pid_value = _smartperf_number(record.get("ProcId"))
    cpu_value = _smartperf_number(record.get("ProcCpuUsage"))
    if pid_value is None:
        return None
    pss_kb = _smartperf_number(record.get("pss"))
    process = {
        "pid": int(pid_value),
        "uid": None,
        "ppid": None,
        "user_pct": _smartperf_number(record.get("ProcUCpuUsage")),
        "system_pct": _smartperf_number(record.get("ProcSCpuUsage")),
        "cpu_pct": cpu_value,
        "mem_pct": None,
        "resident_bytes": int(pss_kb * 1024.0) if pss_kb is not None else None,
        "state": None,
        "name": target_package,
        "command": target_package,
        "category": "application",
        "watch_name": "target_app",
        "activity_active": bool(cpu_value and cpu_value >= 0.5),
        "source": "harmony_smartperf_target",
    }
    return SystemSnapshot(
        uptime_s=sample.uptime_s,
        host_epoch_s=time.time(),
        processes=[process],
        threads=[],
        watched_processes=[process],
        process_count=1,
        thread_count=None,
        collection_ms=None,
    )


def _smartperf_command(
    sample_count: int,
    target_package: str,
    features: Dict[str, bool],
) -> str:
    command = [
        "SP_daemon",
        "-N",
        str(max(2, sample_count)),
        "-PKG",
        target_package,
        "-p",
    ]
    if features.get("cpu_usage") or features.get("cpu_frequency") or features.get("target_process"):
        command.append("-c")
    if features.get("gpu_metrics"):
        command.append("-g")
    if features.get("frame_rate"):
        command.append("-f")
    if features.get("thermal"):
        command.append("-t")
    if features.get("target_process"):
        command.append("-r")
    if features.get("memory_frequency"):
        command.append("-d")
    return " ".join(command)


def collect_harmony_smartperf_session(
    hdc: str,
    device: str,
    duration_s: int,
    target_package: str,
    policies: Sequence[CpuPolicy],
    journal: RunJournal,
    *,
    features: Dict[str, bool],
    foreground_activity: Optional[str],
    base_performance: Optional[Dict[str, object]],
    checkpoint_interval_s: float,
    reconnect_timeout_s: float,
    process_interval_s: float,
    scheduler_interval_s: float,
    context_interval_s: float,
) -> HarmonyCollectionResult:
    target = harmony_target(device)
    result = HarmonyCollectionResult()
    started = time.monotonic()
    deadline = started + float(duration_s)
    last_checkpoint = started
    next_process = started
    next_scheduler = started
    next_extra_context = started
    warned: set[str] = set()

    def warn_once(key: str, message: str) -> None:
        if key in warned:
            return
        warned.add(key)
        result.warnings.append(message)
        journal.append_stderr_line(message)

    def checkpoint(status: str) -> None:
        journal.checkpoint(
            {
                "status": status,
                "sample_count": result.sample_count,
                "context_count": result.context_count,
                "clock_sync_count": result.clock_sync_count,
                "system_snapshot_count": result.system_snapshot_count,
                "thermal_snapshot_count": result.thermal_snapshot_count,
                "scheduler_snapshot_count": result.scheduler_snapshot_count,
                "last_device_uptime_s": result.last_device_uptime_s,
                "reconnect_count": result.reconnect_count,
                "sampler_launch_count": result.sampler_launch_count,
                "stop_reason": result.stop_reason,
                "capture_backend": "harmony_smartperf",
            }
        )

    initial_clock = collect_harmony_clock_sync(hdc, target)
    if initial_clock is not None:
        journal.append_clock_sync(initial_clock)
        result.clock_sync_count += 1

    remaining_s = max(2, int(math.ceil(deadline - time.monotonic())) + 1)
    command = [hdc, "-t", target, "shell", _smartperf_command(remaining_s, target_package, features)]
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start HarmonyOS SP_daemon: {exc}") from exc
    result.sampler_launch_count = 1
    messages: queue.Queue[Tuple[str, Optional[str]]] = queue.Queue()

    def read_stream(name: str, stream: Optional[TextIO]) -> None:
        if stream is not None:
            for line in stream:
                messages.put((name, line))
        messages.put((name, None))

    stdout_thread = threading.Thread(
        target=read_stream, args=("stdout", process.stdout), daemon=True
    )
    stderr_thread = threading.Thread(
        target=read_stream, args=("stderr", process.stderr), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    parser = HarmonySmartPerfParser()
    streams_ended: set[str] = set()

    def accept_record(record: Dict[str, str]) -> None:
        nonlocal last_checkpoint, next_process, next_scheduler, next_extra_context
        sample = build_harmony_smartperf_sample(
            record,
            result.sample_count,
            policies,
            cpu_usage_enabled=bool(features.get("cpu_usage")),
            cpu_frequency_enabled=bool(features.get("cpu_frequency")),
            gpu_metrics_enabled=bool(features.get("gpu_metrics")),
            memory_frequency_enabled=bool(features.get("memory_frequency")),
        )
        if sample is None:
            warn_once("invalid_record", "SP_daemon emitted a record without usable timestamp/current/voltage.")
            return
        if result.last_device_uptime_s is not None and sample.uptime_s <= result.last_device_uptime_s:
            return
        journal.append_sampler_line(
            "SP|" + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        )
        journal.append_sampler_line(
            "N|" + json.dumps(asdict(sample), ensure_ascii=False, separators=(",", ":"))
        )
        result.sample_count += 1
        result.last_device_uptime_s = sample.uptime_s

        if features.get("foreground_window") or features.get("frame_rate"):
            context = build_harmony_smartperf_context(
                record,
                sample,
                target_package,
                foreground_activity=foreground_activity,
                base_performance=base_performance,
                frame_rate_enabled=bool(features.get("frame_rate")),
                frame_details_enabled=bool(features.get("frame_details")),
            )
            journal.append_context(context)
            result.context_count += 1

        if features.get("thermal"):
            journal.append_thermal_snapshot(_smartperf_thermal_snapshot(record, sample))
            result.thermal_snapshot_count += 1

        if features.get("target_process"):
            target_snapshot = _smartperf_target_snapshot(record, sample, target_package)
            if target_snapshot is not None:
                journal.append_system_snapshot(target_snapshot)
                result.system_snapshot_count += 1

        now = time.monotonic()
        if features.get("process_snapshots") and now >= next_process:
            snapshot, error = collect_harmony_system_snapshot(hdc, target)
            if snapshot is not None:
                journal.append_system_snapshot(snapshot)
                result.system_snapshot_count += 1
            elif error:
                warn_once("process", error)
            next_process = _advance_due(next_process, process_interval_s, now)

        if features.get("scheduler") and now >= next_scheduler:
            snapshot, _, error = collect_harmony_scheduler_snapshot(hdc, target, policies)
            if snapshot is not None:
                journal.append_scheduler_snapshot(snapshot)
                result.scheduler_snapshot_count += 1
            elif error:
                warn_once("scheduler", error)
            next_scheduler = _advance_due(next_scheduler, max(5.0, scheduler_interval_s), now)

        if (
            (features.get("harmony_hitches") or features.get("touch_events"))
            and now >= next_extra_context
        ):
            context, error = collect_harmony_context(
                hdc,
                target,
                include_power_state=False,
                include_foreground=False,
                include_window=bool(features.get("harmony_hitches")),
                include_display=False,
                include_frame_rate=False,
                include_hitches=bool(features.get("harmony_hitches")),
                include_touch=bool(features.get("touch_events")),
            )
            if context is not None:
                journal.append_context(context)
                result.context_count += 1
            elif error:
                warn_once("extra_context", error)
            next_extra_context = _advance_due(
                next_extra_context, max(5.0, context_interval_s), now
            )

        if now - last_checkpoint >= checkpoint_interval_s:
            clock = collect_harmony_clock_sync(hdc, target)
            if clock is not None:
                journal.append_clock_sync(clock)
                result.clock_sync_count += 1
            checkpoint("collecting")
            last_checkpoint = now

    try:
        while time.monotonic() < deadline and len(streams_ended) < 2:
            try:
                name, line = messages.get(timeout=0.5)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                streams_ended.add(name)
                continue
            if name == "stderr":
                text = line.rstrip("\r\n")
                if text:
                    journal.append_stderr_line(text)
                continue
            record = parser.feed_line(line)
            if record:
                accept_record(record)
        final_record = parser.finish()
        if final_record:
            accept_record(final_record)
    except KeyboardInterrupt:
        result.stop_reason = "interrupted"
        _stop_process(process)
        checkpoint("interrupted")
        raise
    finally:
        if process.poll() is None:
            _stop_process(process)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

    if result.sample_count < 2:
        raise RuntimeError("HarmonyOS SP_daemon did not produce at least two usable samples")
    result.stop_reason = "completed" if time.monotonic() >= deadline or process.returncode == 0 else "partial"
    battery_result = hdc_shell(hdc, target, "hidumper -s BatteryService -a '-i'", timeout_s=15)
    if battery_result.ok:
        result.battery_end = parse_harmony_battery(battery_result.stdout)
    result.host_elapsed_s = time.monotonic() - started
    checkpoint("collected" if result.stop_reason == "completed" else "partial")
    return result


def _sampler_script(interval_s: float) -> str:
    return (
        "while true; do "
        f"echo {_SAMPLE_BEGIN}; "
        "date +%s.%N; "
        f"echo {_SAMPLE_BATTERY}; "
        "hidumper -s BatteryService -a '-i'; "
        f"echo {_SAMPLE_CPU}; "
        "grep '^cpu' /proc/stat; "
        f"echo {_SAMPLE_END}; "
        f"sleep {max(0.05, interval_s):.3f}; "
        "done"
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[arg-type]
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=5)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    with suppress(OSError, ValueError, subprocess.TimeoutExpired):
        process.terminate()
        process.wait(timeout=3)
        return
    with suppress(OSError, ValueError, subprocess.TimeoutExpired):
        process.kill()


def _advance_due(due: float, interval_s: float, now: float) -> float:
    while due <= now:
        due += interval_s
    return due


def collect_harmony_session(
    hdc: str,
    device: str,
    duration_s: int,
    interval_s: float,
    policies: Sequence[CpuPolicy],
    journal: RunJournal,
    *,
    initial_frequencies_mhz: Optional[Dict[str, float]] = None,
    checkpoint_interval_s: float,
    reconnect_timeout_s: float,
    system_monitor_enabled: bool,
    process_interval_s: float,
    thermal_interval_s: float,
    scheduler_interval_s: float,
    context_enabled: bool = True,
    context_sample_interval_s: Optional[float] = None,
    foreground_enabled: bool = True,
    frame_rate_enabled: bool = True,
    hitches_enabled: bool = True,
    touch_enabled: bool = True,
    process_snapshots_enabled: bool = True,
    thermal_snapshots_enabled: bool = True,
    scheduler_snapshots_enabled: bool = True,
    cpu_frequency_enabled: bool = True,
) -> HarmonyCollectionResult:
    target = harmony_target(device)
    result = HarmonyCollectionResult()
    started = time.monotonic()
    deadline = started + float(duration_s)
    last_checkpoint = started
    last_frame_at = started
    outage_started: Optional[float] = None
    previous_cpu: Optional[Dict[str, CpuTimes]] = None
    previous_timestamp: Optional[float] = None
    frequencies_mhz = dict(initial_frequencies_mhz or {})
    warned = set()
    context_interval_s = max(
        1.0,
        float(context_sample_interval_s)
        if context_sample_interval_s is not None
        else min(10.0, process_interval_s),
    )
    process_monitor_enabled = system_monitor_enabled and process_snapshots_enabled
    thermal_monitor_enabled = system_monitor_enabled and thermal_snapshots_enabled
    scheduler_monitor_enabled = system_monitor_enabled and scheduler_snapshots_enabled
    next_context = started if context_enabled else float("inf")
    next_process = started if process_monitor_enabled else float("inf")
    next_thermal = started if thermal_monitor_enabled else float("inf")
    next_scheduler = (
        started
        if scheduler_monitor_enabled or cpu_frequency_enabled
        else float("inf")
    )
    max_cpu_gap_s = max(interval_s * 3.0, interval_s + 2.0)

    def warn_once(key: str, message: str) -> None:
        if key in warned:
            return
        warned.add(key)
        result.warnings.append(message)
        journal.append_stderr_line(message)

    def checkpoint(status: str) -> None:
        journal.checkpoint(
            {
                "status": status,
                "sample_count": result.sample_count,
                "context_count": result.context_count,
                "clock_sync_count": result.clock_sync_count,
                "system_snapshot_count": result.system_snapshot_count,
                "thermal_snapshot_count": result.thermal_snapshot_count,
                "scheduler_snapshot_count": result.scheduler_snapshot_count,
                "last_device_uptime_s": result.last_device_uptime_s,
                "reconnect_count": result.reconnect_count,
                "sampler_launch_count": result.sampler_launch_count,
                "stop_reason": result.stop_reason,
            }
        )

    initial_clock = collect_harmony_clock_sync(hdc, target)
    if initial_clock is not None:
        journal.append_clock_sync(initial_clock)
        result.clock_sync_count += 1

    while time.monotonic() < deadline:
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        command = [hdc, "-t", target, "shell", _sampler_script(interval_s)]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"could not start HarmonyOS HDC sampler: {exc}") from exc
        result.sampler_launch_count += 1
        line_queue: queue.Queue[Optional[str]] = queue.Queue()

        def read_stdout(stream: Optional[TextIO]) -> None:
            if stream is not None:
                for line in stream:
                    line_queue.put(line)
            line_queue.put(None)

        def read_stderr(stream: Optional[TextIO]) -> None:
            if stream is None:
                return
            for line in stream:
                text = line.rstrip("\r\n")
                if text:
                    journal.append_stderr_line(text)

        stdout_thread = threading.Thread(target=read_stdout, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        parser = HarmonyFrameParser()
        ended = False
        try:
            while time.monotonic() < deadline:
                try:
                    line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None:
                        ended = True
                        break
                    if time.monotonic() - last_frame_at >= reconnect_timeout_s:
                        warn_once(
                            "sampler_stalled",
                            "HarmonyOS HDC sampler stopped producing framed data and will be reconnected.",
                        )
                        _stop_process(process)
                        ended = True
                        break
                    continue
                if line is None:
                    ended = True
                    break
                frame = parser.feed_line(line)
                if frame is None:
                    continue
                timestamp = _extract_timestamp(frame["timestamp"])
                if timestamp is None:
                    warn_once("missing_timestamp", "HarmonyOS sampler emitted a frame without a timestamp.")
                    continue
                sample, current_cpu = build_harmony_sample(
                    result.sample_count,
                    timestamp,
                    frame["battery"],
                    frame["cpu"],
                    previous_cpu,
                    policies,
                    frequencies_mhz,
                    previous_timestamp_s=previous_timestamp,
                    max_cpu_gap_s=max_cpu_gap_s,
                )
                previous_cpu = current_cpu or previous_cpu
                previous_timestamp = timestamp
                if sample is None:
                    warn_once(
                        "invalid_battery_frame",
                        "HarmonyOS BatteryService frame did not contain usable current and voltage values.",
                    )
                    continue
                if result.last_device_uptime_s is not None and sample.uptime_s <= result.last_device_uptime_s:
                    continue
                journal.append_sampler_line(
                    "N|" + json.dumps(asdict(sample), ensure_ascii=False, separators=(",", ":"))
                )
                result.sample_count += 1
                result.last_device_uptime_s = sample.uptime_s
                last_frame_at = time.monotonic()
                outage_started = None

                now = time.monotonic()
                if context_enabled and now >= next_context:
                    context, error = collect_harmony_context(
                        hdc,
                        target,
                        include_power_state=foreground_enabled,
                        include_foreground=foreground_enabled,
                        include_window=foreground_enabled or hitches_enabled,
                        include_display=foreground_enabled or frame_rate_enabled,
                        include_frame_rate=frame_rate_enabled,
                        include_hitches=hitches_enabled,
                        include_touch=touch_enabled,
                    )
                    if context is not None:
                        journal.append_context(context)
                        result.context_count += 1
                    elif error:
                        warn_once("context", error)
                    next_context = _advance_due(next_context, context_interval_s, now)

                if process_monitor_enabled and now >= next_process:
                    snapshot, error = collect_harmony_system_snapshot(hdc, target)
                    if snapshot is not None:
                        journal.append_system_snapshot(snapshot)
                        result.system_snapshot_count += 1
                    elif error:
                        warn_once("process", error)
                    next_process = _advance_due(next_process, process_interval_s, now)

                if thermal_monitor_enabled and now >= next_thermal:
                    snapshot, error = collect_harmony_thermal_snapshot(hdc, target)
                    if snapshot is not None:
                        journal.append_thermal_snapshot(snapshot)
                        result.thermal_snapshot_count += 1
                    elif error:
                        warn_once("thermal", error)
                    next_thermal = _advance_due(next_thermal, thermal_interval_s, now)

                if (scheduler_monitor_enabled or cpu_frequency_enabled) and now >= next_scheduler:
                    snapshot, latest_frequencies, error = collect_harmony_scheduler_snapshot(
                        hdc, target, policies
                    )
                    if latest_frequencies:
                        frequencies_mhz.update(latest_frequencies)
                    if snapshot is not None and scheduler_monitor_enabled:
                        journal.append_scheduler_snapshot(snapshot)
                        result.scheduler_snapshot_count += 1
                    elif error:
                        warn_once("scheduler", error)
                    next_scheduler = _advance_due(
                        next_scheduler, max(10.0, scheduler_interval_s), now
                    )

                if now - last_checkpoint >= checkpoint_interval_s:
                    clock = collect_harmony_clock_sync(hdc, target)
                    if clock is not None:
                        journal.append_clock_sync(clock)
                        result.clock_sync_count += 1
                    checkpoint("collecting")
                    last_checkpoint = now
        except KeyboardInterrupt:
            result.stop_reason = "interrupted"
            _stop_process(process)
            checkpoint("interrupted")
            raise
        finally:
            if process.poll() is None:
                _stop_process(process)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)

        if time.monotonic() >= deadline:
            result.stop_reason = "completed"
            break
        if not ended and process.returncode == 0:
            result.stop_reason = "completed"
            break
        if outage_started is None:
            outage_started = time.monotonic()
        if time.monotonic() - outage_started >= reconnect_timeout_s:
            result.stop_reason = "harmony_disconnected"
            warn_once(
                "reconnect_timeout",
                "HarmonyOS HDC target did not recover before the reconnect timeout.",
            )
            break
        checkpoint("reconnecting")
        if harmony_connection_type(target) == "wireless":
            with suppress(RuntimeError, ValueError):
                connect_harmony_device(hdc, target, timeout_s=min(20.0, reconnect_timeout_s))
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        result.reconnect_count += 1

    battery_result = hdc_shell(hdc, target, "hidumper -s BatteryService -a '-i'", timeout_s=15)
    if battery_result.ok:
        result.battery_end = parse_harmony_battery(battery_result.stdout)
    result.host_elapsed_s = time.monotonic() - started
    if result.stop_reason == "completed" and result.sample_count < 2:
        result.stop_reason = "collector_error"
    checkpoint("collected" if result.stop_reason == "completed" else "partial")
    return result
