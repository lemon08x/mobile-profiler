from __future__ import annotations

import json
import math
import queue
import re
import shlex
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, IO, List, Optional, Sequence, Tuple

from .models import (
    ClockSyncPoint,
    CommandResult,
    ContextSample,
    CpuPolicy,
    CpuTimes,
    GpuSource,
    MemorySource,
    RawSample,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
)
from .parsers import (
    merge_watched_processes,
    parse_activity_processes,
    parse_cpuset_policy_state,
    parse_performance_hint,
    parse_power_scheduler_state,
    parse_ps_processes,
    parse_display_brightness_state,
    parse_thermalservice,
    parse_top_processes,
    parse_top_threads,
)
from .storage import RunJournal


@dataclass
class StreamCollectionResult:
    raw_samples: List[RawSample] = field(default_factory=list)
    contexts: List[ContextSample] = field(default_factory=list)
    clock_sync: List[ClockSyncPoint] = field(default_factory=list)
    reconnect_count: int = 0
    sampler_launch_count: int = 0
    host_elapsed_s: float = 0.0
    stop_reason: str = "completed"
    warnings: List[str] = field(default_factory=list)
    system_snapshot_count: int = 0
    thermal_snapshot_count: int = 0
    scheduler_snapshot_count: int = 0


def decode_output(data: bytes) -> str:
    for encoding in ("utf-8", "mbcs"):
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def run_command(argv: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
        return CommandResult(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=decode_output(proc.stdout),
            stderr=decode_output(proc.stderr),
            elapsed_s=time.monotonic() - start,
        )
    except FileNotFoundError as exc:
        return CommandResult(list(argv), 127, "", str(exc), time.monotonic() - start)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            list(argv),
            124,
            decode_output(exc.stdout or b""),
            f"timeout after {timeout_s:.1f}s\n{decode_output(exc.stderr or b'')}".strip(),
            time.monotonic() - start,
        )


def adb_prefix(adb: str, device: Optional[str]) -> List[str]:
    args = [adb]
    if device:
        args.extend(["-s", device])
    return args


def adb_shell(
    adb: str,
    device: str,
    command: Sequence[str] | str,
    timeout_s: float = 30.0,
) -> CommandResult:
    args = adb_prefix(adb, device) + ["shell"]
    if isinstance(command, str):
        args.append(command)
    else:
        args.extend(command)
    return run_command(args, timeout_s=timeout_s)


def adb_connection_type(serial: str) -> str:
    normalized = str(serial or "").strip()
    lowered = normalized.lower()
    if re.fullmatch(r"emulator-\d+", lowered):
        return "emulator"
    if "._adb-tls-connect._tcp" in lowered or re.search(r":\d+$", normalized):
        return "wireless"
    return "usb" if normalized else "unknown"


def list_adb_devices(adb: str) -> Tuple[List[Dict[str, str]], Optional[str]]:
    result = run_command([adb, "devices", "-l"], timeout_s=10)
    if not result.ok:
        return [], result.stderr.strip() or result.stdout.strip() or "adb devices failed"
    devices: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices") or stripped.startswith("*"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        info = {
            "serial": parts[0],
            "state": parts[1],
            "connection_type": adb_connection_type(parts[0]),
        }
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                info[key] = value
        devices.append(info)
    return devices, None


def select_device(adb: str, requested: Optional[str]) -> str:
    devices, error = list_adb_devices(adb)
    if error:
        raise RuntimeError(error)
    ready = [item for item in devices if item.get("state") == "device"]
    if requested:
        if not any(item.get("serial") == requested for item in ready):
            connected = ", ".join(item.get("serial", "") for item in ready) or "none"
            raise RuntimeError(f"device {requested!r} is not ready; connected devices: {connected}")
        return requested
    if not ready:
        raise RuntimeError("no authorized ADB device is connected")
    if len(ready) > 1:
        connected = ", ".join(item.get("serial", "") for item in ready)
        raise RuntimeError(f"multiple ADB devices are connected; pass --device. Devices: {connected}")
    return ready[0]["serial"]


def first_number(value: str) -> Optional[float]:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else None


def get_prop(adb: str, device: str, name: str) -> str:
    result = adb_shell(adb, device, ["getprop", name], timeout_s=10)
    return result.stdout.strip() if result.ok else ""


def collect_device_info(adb: str, device: str) -> Dict[str, object]:
    soc_manufacturer = get_prop(adb, device, "ro.soc.manufacturer")
    hardware = get_prop(adb, device, "ro.hardware")
    board_platform = get_prop(adb, device, "ro.board.platform")
    platform_identity = " ".join((soc_manufacturer, hardware, board_platform)).lower()
    return {
        "serial": device,
        "brand": get_prop(adb, device, "ro.product.brand"),
        "model": get_prop(adb, device, "ro.product.model"),
        "device": get_prop(adb, device, "ro.product.device"),
        "soc_manufacturer": soc_manufacturer,
        "soc_model": get_prop(adb, device, "ro.soc.model"),
        "hardware": hardware,
        "board_platform": board_platform,
        "platform_family": (
            "qualcomm"
            if any(token in platform_identity for token in ("qti", "qualcomm", "qcom"))
            else "mediatek"
            if any(token in platform_identity for token in ("mediatek", "mtk"))
            else "generic"
        ),
        "android": get_prop(adb, device, "ro.build.version.release"),
        "sdk": get_prop(adb, device, "ro.build.version.sdk"),
        "fingerprint": get_prop(adb, device, "ro.build.fingerprint"),
    }


POWER_SETTING_KEYS: Tuple[Tuple[str, str], ...] = (
    ("system", "screen_brightness"),
    ("system", "screen_brightness_mode"),
    ("system", "screen_off_timeout"),
    ("system", "peak_refresh_rate"),
    ("system", "min_refresh_rate"),
    ("global", "low_power"),
    ("global", "adaptive_battery_management_enabled"),
    ("global", "app_standby_enabled"),
    ("global", "wifi_on"),
    ("global", "bluetooth_on"),
    ("global", "airplane_mode_on"),
    ("secure", "location_mode"),
    ("global", "window_animation_scale"),
    ("global", "transition_animation_scale"),
    ("global", "animator_duration_scale"),
    ("global", "stay_on_while_plugged_in"),
)


def collect_android_runtime_settings_text(adb: str, device: str) -> str:
    commands = []
    for namespace, key in POWER_SETTING_KEYS:
        commands.append(
            f"app_setting=$(settings get {namespace} {key} 2>/dev/null); "
            f"printf 'SETTING|{namespace}|{key}|%s\\n' \"$app_setting\""
        )
    result = adb_shell(adb, device, "\n".join(commands), timeout_s=25)
    return result.stdout if result.ok else (result.stdout + "\n" + result.stderr).strip()


def parse_android_runtime_settings(text: str) -> Dict[str, object]:
    values: Dict[str, object] = {}
    for line in text.splitlines():
        parts = line.strip().split("|", 3)
        if len(parts) != 4 or parts[0] != "SETTING":
            continue
        namespace, key, raw = parts[1], parts[2], parts[3].strip()
        if raw.lower() in {"null", "none", ""}:
            value: object = None
        elif re.fullmatch(r"[-+]?\d+(?:\.\d+)?", raw):
            parsed = float(raw)
            value = int(parsed) if parsed.is_integer() else parsed
        else:
            value = raw
        values[f"{namespace}.{key}"] = value
    return values


def _read_number(adb: str, device: str, path: str) -> Optional[float]:
    result = adb_shell(adb, device, ["cat", path], timeout_s=10)
    return first_number(result.stdout) if result.ok else None


def _read_numbers(adb: str, device: str, path: str) -> List[float]:
    result = adb_shell(adb, device, ["cat", path], timeout_s=10)
    if not result.ok:
        return []
    return [float(item) for item in re.findall(r"[-+]?\d+(?:\.\d+)?", result.stdout)]


def _read_text(adb: str, device: str, path: str) -> Optional[str]:
    result = adb_shell(adb, device, ["cat", path], timeout_s=10)
    value = result.stdout.strip()
    return value if result.ok and value else None


def collect_core_control(
    adb: str,
    device: str,
    cores: Sequence[int],
) -> Dict[str, object]:
    if not cores:
        return {}
    root = f"/sys/devices/system/cpu/cpu{min(cores)}/core_ctl"
    enabled = _read_number(adb, device, f"{root}/enable")
    minimum = _read_number(adb, device, f"{root}/min_cpus")
    maximum = _read_number(adb, device, f"{root}/max_cpus")
    if enabled is None and minimum is None and maximum is None:
        return {}
    return {
        "path": root,
        "enabled": bool(enabled) if enabled is not None else None,
        "min_cpus": int(minimum) if minimum is not None else None,
        "max_cpus": int(maximum) if maximum is not None else None,
        "busy_up_thresholds": [int(value) for value in _read_numbers(adb, device, f"{root}/busy_up_thres")],
        "busy_down_thresholds": [int(value) for value in _read_numbers(adb, device, f"{root}/busy_down_thres")],
        "offline_delay_ms": _read_number(adb, device, f"{root}/offline_delay_ms"),
        "task_threshold": _read_number(adb, device, f"{root}/task_thres"),
        "not_preferred": [int(value) for value in _read_numbers(adb, device, f"{root}/not_preferred")],
    }


def _cluster_labels(count: int) -> List[str]:
    if count == 1:
        return ["CPU"]
    if count == 2:
        return ["Little", "Big"]
    if count == 3:
        return ["Little", "Big", "Prime"]
    return [f"Cluster {index}" for index in range(count)]


def collect_cpu_policies(adb: str, device: str) -> List[CpuPolicy]:
    root = "/sys/devices/system/cpu/cpufreq"
    result = adb_shell(adb, device, ["ls", root], timeout_s=10)
    if not result.ok:
        return []
    names = sorted(
        set(re.findall(r"\bpolicy\d+\b", result.stdout)),
        key=lambda item: int(item[6:]),
    )
    labels = _cluster_labels(len(names))
    soc_model = get_prop(adb, device, "ro.soc.model").upper()
    if len(names) == 2 and soc_model.startswith(("SM8750", "SM8850")):
        labels = ["Performance", "Prime"]
    policies: List[CpuPolicy] = []
    for cluster_index, name in enumerate(names):
        path = f"{root}/{name}"
        cores = [int(value) for value in _read_numbers(adb, device, f"{path}/affected_cpus")]
        policies.append(
            CpuPolicy(
                name=name,
                path=path,
                cluster_index=cluster_index,
                label=labels[cluster_index],
                cores=cores,
                min_khz=_read_number(adb, device, f"{path}/cpuinfo_min_freq"),
                max_khz=_read_number(adb, device, f"{path}/cpuinfo_max_freq"),
                available_frequencies_khz=_read_numbers(
                    adb, device, f"{path}/scaling_available_frequencies"
                ),
                governor=_read_text(adb, device, f"{path}/scaling_governor"),
                core_control=collect_core_control(adb, device, cores),
            )
        )
    return policies


def frequency_to_mhz(raw_value: float) -> float:
    value = abs(float(raw_value))
    if value >= 10_000_000:
        return value / 1_000_000.0
    if value >= 10_000:
        return value / 1_000.0
    return value


def _readable_number(
    adb: str,
    device: str,
    path: str,
) -> Tuple[Optional[float], str]:
    result = adb_shell(adb, device, ["cat", path], timeout_s=10)
    value = first_number(result.stdout)
    if result.ok and value is not None:
        return value, "readable"
    reason = (result.stderr or result.stdout).strip().splitlines()
    return None, reason[-1] if reason else "unavailable"


def _root_shell(
    adb: str,
    device: str,
    command: Sequence[str] | str,
    timeout_s: float = 30.0,
) -> CommandResult:
    command_text = (
        command
        if isinstance(command, str)
        else " ".join(shlex.quote(str(part)) for part in command)
    )
    return adb_shell(
        adb,
        device,
        f"su -c {shlex.quote(command_text)}",
        timeout_s=timeout_s,
    )


def _has_su_access(adb: str, device: str) -> bool:
    result = _root_shell(adb, device, ["id", "-u"], timeout_s=3)
    return result.ok and first_number(result.stdout) == 0


def _readable_number_with_root(
    adb: str,
    device: str,
    path: str,
    *,
    allow_root: bool,
) -> Tuple[Optional[float], str, bool]:
    value, status = _readable_number(adb, device, path)
    if value is not None or not allow_root:
        return value, status, False
    root_result = _root_shell(adb, device, ["cat", path], timeout_s=10)
    root_value = first_number(root_result.stdout)
    if root_result.ok and root_value is not None:
        return root_value, "readable via su", True
    root_reason = (root_result.stderr or root_result.stdout).strip().splitlines()
    if root_reason:
        status = f"{status}; root: {root_reason[-1]}"
    return None, status, False


def gpu_load_from_text(text: str, load_format: str) -> Optional[float]:
    values = [float(item) for item in re.findall(r"[-+]?\d+(?:\.\d+)?", text or "")]
    if not values:
        return None
    if load_format == "busy_total":
        if len(values) < 2 or values[1] <= 0:
            return None
        return max(0.0, min(100.0, values[0] / values[1] * 100.0))
    return max(0.0, min(100.0, values[0]))


def _readable_gpu_load(
    adb: str,
    device: str,
    path: str,
    load_format: str,
) -> Tuple[Optional[float], str]:
    result = adb_shell(adb, device, ["cat", path], timeout_s=10)
    value = gpu_load_from_text(result.stdout, load_format)
    if result.ok and value is not None:
        return value, "readable"
    reason = (result.stderr or result.stdout).strip().splitlines()
    return None, reason[-1] if reason else "unavailable"


def _readable_gpu_load_with_root(
    adb: str,
    device: str,
    path: str,
    load_format: str,
    *,
    allow_root: bool,
) -> Tuple[Optional[float], str, bool]:
    value, status = _readable_gpu_load(adb, device, path, load_format)
    if value is not None or not allow_root:
        return value, status, False
    root_result = _root_shell(adb, device, ["cat", path], timeout_s=10)
    root_value = gpu_load_from_text(root_result.stdout, load_format)
    if root_result.ok and root_value is not None:
        return root_value, "readable via su", True
    root_reason = (root_result.stderr or root_result.stdout).strip().splitlines()
    if root_reason:
        status = f"{status}; root: {root_reason[-1]}"
    return None, status, False


def _read_number_with_access(
    adb: str,
    device: str,
    path: str,
    *,
    requires_root: bool,
) -> Optional[float]:
    if not requires_root:
        return _read_number(adb, device, path)
    result = _root_shell(adb, device, ["cat", path], timeout_s=10)
    return first_number(result.stdout) if result.ok else None


def _read_numbers_with_access(
    adb: str,
    device: str,
    path: str,
    *,
    requires_root: bool,
) -> List[float]:
    if not requires_root:
        return _read_numbers(adb, device, path)
    result = _root_shell(adb, device, ["cat", path], timeout_s=10)
    if not result.ok:
        return []
    return [float(item) for item in re.findall(r"[-+]?\d+(?:\.\d+)?", result.stdout)]


def _read_text_with_root(
    adb: str,
    device: str,
    path: str,
    *,
    allow_root: bool,
) -> Tuple[Optional[str], bool]:
    value = _read_text(adb, device, path)
    if value is not None or not allow_root:
        return value, False
    result = _root_shell(adb, device, ["cat", path], timeout_s=10)
    root_value = result.stdout.strip()
    return (root_value, True) if result.ok and root_value else (None, False)


def is_gpu_core_devfreq(identity: str) -> bool:
    value = identity.lower()
    if any(token in value for token in ("gpubw", "gpu-bw", "busmon", "memlat", "latfloor")):
        return False
    return any(
        token in value
        for token in ("kgsl-3d", "qcom,kgsl", "mali", "gpu0", "gpu@", "graphics")
    )


def is_memory_devfreq(identity: str) -> bool:
    value = identity.lower()
    if any(
        token in value
        for token in (
            "memlat",
            "latfloor",
            "gpubw",
            "gpu-bw",
            "bw_hwmon",
            "busmon",
            "cpubw",
            "llccbw",
            "l3",
        )
    ):
        return False
    return any(
        token in value
        for token in (
            "dmc",
            "dram",
            "ddr",
            "memory-controller",
            "mem_ctrl",
            "mif",
            "dvfsrc",
        )
    )


def detect_memory_source(
    adb: str,
    device: str,
) -> Tuple[Optional[MemorySource], Dict[str, object]]:
    listed = adb_shell(
        adb,
        device,
        "ls -d /sys/class/devfreq/* 2>/dev/null",
        timeout_s=10,
    )
    candidates: List[Tuple[str, str]] = []
    for directory in listed.stdout.split():
        resolved = adb_shell(adb, device, ["readlink", "-f", directory], timeout_s=10)
        identity = f"{directory.lower()} {resolved.stdout.lower()}"
        if is_memory_devfreq(identity):
            candidates.append((directory, identity.strip()))
    candidates.extend(
        [
            (
                "/sys/class/devfreq/dmc",
                "generic dmc",
            ),
            (
                "/sys/class/devfreq/mif",
                "generic mif",
            ),
        ]
    )

    attempts: List[Dict[str, object]] = []
    for directory, identity in dict.fromkeys(candidates):
        path = f"{directory}/cur_freq"
        value, status = _readable_number(adb, device, path)
        attempts.append({"path": path, "identity": identity, "status": status})
        if value is None:
            continue
        minimum = _read_number(adb, device, f"{directory}/min_freq")
        maximum = _read_number(adb, device, f"{directory}/max_freq")
        available = _read_numbers(adb, device, f"{directory}/available_frequencies")
        source = MemorySource(
            name=directory.rsplit("/", 1)[-1] or "memory",
            frequency_path=path,
            minimum_mhz=frequency_to_mhz(minimum) if minimum is not None else None,
            maximum_mhz=frequency_to_mhz(maximum) if maximum is not None else None,
            available_frequencies_mhz=sorted(
                {frequency_to_mhz(item) for item in available if item > 0}
            ),
        )
        return source, {
            "available": True,
            "source": source.name,
            "frequency_path": path,
            "identity": identity,
            "initial_frequency_mhz": frequency_to_mhz(value),
            "attempts": attempts,
        }
    return None, {
        "available": False,
        "source": None,
        "frequency_path": None,
        "attempts": attempts,
        "limitations": (
            "The production kernel did not expose a readable DRAM/DMC/MIF devfreq node. "
            "Bandwidth-vote and memlat devices are intentionally not mislabeled as memory clock."
        ),
    }


def detect_gpu_source(
    adb: str,
    device: str,
    frequency_override: Optional[str] = None,
) -> Tuple[Optional[GpuSource], Dict[str, object]]:
    soc_manufacturer = get_prop(adb, device, "ro.soc.manufacturer")
    hardware = get_prop(adb, device, "ro.hardware")
    board_platform = get_prop(adb, device, "ro.board.platform")
    platform_identity = " ".join((soc_manufacturer, hardware, board_platform)).lower()
    is_qualcomm = any(
        token in platform_identity for token in ("qti", "qualcomm", "qcom")
    )
    su_available = _has_su_access(adb, device)
    kgsl_model, model_requires_root = _read_text_with_root(
        adb,
        device,
        "/sys/class/kgsl/kgsl-3d0/gpu_model",
        allow_root=su_available,
    )
    provider = "qualcomm_kgsl" if is_qualcomm or kgsl_model else "generic_sysfs"
    profiler_support = get_prop(adb, device, "graphics.gpu.profiler.support")
    producer_help = adb_shell(adb, device, ["gpu_counter_producer", "-h"], timeout_s=10)
    perfetto_state = adb_shell(adb, device, ["perfetto", "--query"], timeout_s=20)
    perfetto_text = re.sub(r"\x1b\[[0-9;]*m", "", perfetto_state.stdout)
    perfetto_gpu_sources = [
        name
        for name in (
            "gpu.counters",
            "gpu.counter",
            "gpu.renderstages",
            "android.gpu.memory",
        )
        if re.search(rf"(?m)^\s*{re.escape(name)}\s+", perfetto_text)
    ]
    hardware_counter_sources = [
        name
        for name in perfetto_gpu_sources
        if "counter" in name.lower() and "memory" not in name.lower()
    ]
    candidates: List[str] = []
    if frequency_override:
        candidates.append(frequency_override)

    if is_qualcomm or kgsl_model:
        candidates.extend(
            [
                "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq",
                "/sys/class/kgsl/kgsl-3d0/gpuclk",
            ]
        )

    listed = adb_shell(
        adb,
        device,
        "ls -d /sys/class/devfreq/* 2>/dev/null",
        timeout_s=10,
    )
    for directory in listed.stdout.split():
        lower = directory.lower()
        resolved = adb_shell(adb, device, ["readlink", "-f", directory], timeout_s=10)
        identity = f"{lower} {resolved.stdout.lower()}"
        if is_gpu_core_devfreq(identity):
            candidates.append(f"{directory}/cur_freq")

    candidates.extend(
        [
            "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq",
            "/sys/class/kgsl/kgsl-3d0/gpuclk",
            "/sys/kernel/ged/hal/current_freqency",
            "/sys/kernel/ged/hal/gpu_cur_freq",
        ]
    )
    candidates = list(dict.fromkeys(candidates))
    attempts: List[Dict[str, str]] = []
    frequency_path: Optional[str] = None
    initial_raw: Optional[float] = None
    frequency_requires_root = False
    for path in candidates:
        value, status, requires_root = _readable_number_with_root(
            adb,
            device,
            path,
            allow_root=su_available,
        )
        attempts.append({"path": path, "status": status})
        if value is not None:
            frequency_path = path
            initial_raw = value
            frequency_requires_root = requires_root
            break

    load_path: Optional[str] = None
    load_format = "percentage"
    initial_load_pct: Optional[float] = None
    load_requires_root = False
    load_attempts: List[Dict[str, str]] = []
    load_candidates = [
        ("/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage", "percentage"),
        ("/sys/class/kgsl/kgsl-3d0/gpubusy", "busy_total"),
        ("/sys/kernel/ged/hal/gpu_utilization", "percentage"),
    ]
    for path, candidate_format in load_candidates:
        value, status, requires_root = _readable_gpu_load_with_root(
            adb,
            device,
            path,
            candidate_format,
            allow_root=su_available,
        )
        load_attempts.append({"path": path, "format": candidate_format, "status": status})
        if value is not None:
            load_path = path
            load_format = candidate_format
            initial_load_pct = value
            load_requires_root = requires_root
            break

    requires_root = model_requires_root or frequency_requires_root or load_requires_root

    probe: Dict[str, object] = {
        "provider": provider,
        "model": kgsl_model,
        "qualcomm_platform": is_qualcomm,
        "kgsl_detected": bool(kgsl_model) or is_qualcomm,
        "frequency_available": frequency_path is not None,
        "attempts": attempts,
        "load_available": load_path is not None,
        "load_attempts": load_attempts,
        "initial_load_pct": initial_load_pct,
        "graphics_gpu_profiler_support": profiler_support.lower() == "true",
        "gpu_counter_producer_present": "GPU hardware counter" in (
            producer_help.stdout + producer_help.stderr
        ),
        "perfetto_gpu_sources": perfetto_gpu_sources,
        "perfetto_hardware_counter_source_available": bool(hardware_counter_sources),
        "perfetto_query_available": perfetto_state.ok,
        "root_access_used": requires_root,
        "su_available": su_available,
    }
    if frequency_path is None and load_path is None:
        platform_note = (
            f"Qualcomm KGSL ({kgsl_model or 'Adreno'}) was detected, but its telemetry nodes "
            "are permission-restricted on this production build. "
            if is_qualcomm or kgsl_model
            else ""
        )
        probe["reason"] = (
            platform_note
            + "No readable GPU frequency/load node was exposed to the ADB shell, and Perfetto "
            "did not register a GPU hardware-counter data source. dumpsys gpu UID work and "
            "memory snapshots may still be available."
        )
        return None, probe

    parent = frequency_path.rsplit("/", 1)[0] if frequency_path else ""
    min_raw = (
        _read_number_with_access(
            adb,
            device,
            f"{parent}/min_freq",
            requires_root=requires_root,
        )
        if parent
        else None
    )
    max_raw = (
        _read_number_with_access(
            adb,
            device,
            f"{parent}/max_freq",
            requires_root=requires_root,
        )
        if parent
        else None
    )
    available_raw = (
        _read_numbers_with_access(
            adb,
            device,
            f"{parent}/available_frequencies",
            requires_root=requires_root,
        )
        if parent
        else []
    )
    if is_qualcomm or kgsl_model:
        kgsl_root = "/sys/class/kgsl/kgsl-3d0"
        if min_raw is None:
            min_raw = _read_number_with_access(
                adb,
                device,
                f"{kgsl_root}/min_gpuclk",
                requires_root=requires_root,
            )
        if max_raw is None:
            max_raw = _read_number_with_access(
                adb,
                device,
                f"{kgsl_root}/max_gpuclk",
                requires_root=requires_root,
            )
        if not available_raw:
            available_raw = _read_numbers_with_access(
                adb,
                device,
                f"{kgsl_root}/gpu_available_frequencies",
                requires_root=requires_root,
            )
    name = kgsl_model or (_read_text(adb, device, f"{parent}/name") if parent else None) or "GPU"
    source = GpuSource(
        name=name,
        frequency_path=frequency_path,
        load_path=load_path,
        load_format=load_format,
        minimum_mhz=frequency_to_mhz(min_raw) if min_raw is not None else None,
        maximum_mhz=frequency_to_mhz(max_raw) if max_raw is not None else None,
        available_frequencies_mhz=[frequency_to_mhz(value) for value in available_raw],
        source_type=provider,
        requires_root=requires_root,
    )
    probe.update(
        {
            "source": asdict(source),
            "initial_frequency_mhz": (
                frequency_to_mhz(initial_raw) if initial_raw is not None else None
            ),
        }
    )
    return source, probe


def collect_foreground_package(adb: str, device: str) -> Optional[str]:
    result = adb_shell(adb, device, ["dumpsys", "activity", "activities"], timeout_s=20)
    if not result.ok:
        return None
    patterns = [
        r"(?:mResumedActivity|ResumedActivity|mFocusedApp)[:=].*?\s([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
        r"topResumedActivity=.*?\s([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, result.stdout)
        if match:
            return match.group(1)
    return None


def _normalized_refresh_rate(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 1000:
        return None
    rounded = round(parsed)
    return float(rounded) if abs(parsed - rounded) < 0.05 else round(parsed, 3)


def parse_android_display_performance(text: str) -> Dict[str, object]:
    """Extract the active Android display mode and advertised refresh modes."""

    active_refresh: Optional[float] = None
    active_match = re.search(r"mActiveRenderFrameRate\s*=\s*([-+\d.]+)", text)
    if active_match:
        active_refresh = _normalized_refresh_rate(active_match.group(1))

    width = height = None
    active_mode = re.search(
        r"mActiveSfDisplayMode\s*=\s*DisplayMode\{[^}\n]*?width=(\d+),\s*height=(\d+),"
        r"[^}\n]*?(?:peakRefreshRate|fps)=([-+\d.]+)",
        text,
    )
    if active_mode:
        width = int(active_mode.group(1))
        height = int(active_mode.group(2))
        if active_refresh is None:
            active_refresh = _normalized_refresh_rate(active_mode.group(3))

    if width is None or height is None:
        display_info = re.search(
            r"\breal\s+(\d+)\s*x\s*(\d+).*?renderFrameRate\s+([-+\d.]+)",
            text,
        )
        if display_info:
            width = int(display_info.group(1))
            height = int(display_info.group(2))
            if active_refresh is None:
                active_refresh = _normalized_refresh_rate(display_info.group(3))

    supported: set[float] = set()
    for match in re.finditer(
        r"(?:DisplayMode|DisplayModeRecord)\{[^}\n]*?(?:peakRefreshRate|\bfps)=([-+\d.]+)",
        text,
    ):
        rate = _normalized_refresh_rate(match.group(1))
        if rate is not None:
            supported.add(rate)
    for match in re.finditer(r"supportedRefreshRates\s*\[([^\]]+)\]", text):
        for value in re.findall(r"[-+\d.]+", match.group(1)):
            rate = _normalized_refresh_rate(value)
            if rate is not None:
                supported.add(rate)

    return {
        "display_width_px": width,
        "display_height_px": height,
        "refresh_rate_hz": active_refresh,
        "supported_refresh_rates_hz": sorted(supported),
    }


def parse_android_surfaceflinger_performance(text: str) -> Dict[str, object]:
    """Parse non-mutating SurfaceFlinger refresh residency and renderer details."""

    durations: Dict[str, float] = {}
    duration_pattern = re.compile(
        r"^\s*([-+\d.]+)\s+Hz:\s*(\d+)d(\d+):(\d+):([-+\d.]+)\s*$",
        re.M,
    )
    for match in duration_pattern.finditer(text):
        rate = _normalized_refresh_rate(match.group(1))
        if rate is None:
            continue
        seconds = (
            int(match.group(2)) * 86400.0
            + int(match.group(3)) * 3600.0
            + int(match.group(4)) * 60.0
            + float(match.group(5))
        )
        key = f"{rate:g}"
        durations[key] = max(durations.get(key, 0.0), seconds)

    renderer_match = re.search(r"^\s*GLES:\s*([^,\n]+),\s*([^,\n]+),", text, re.M)
    return {
        "refresh_rate_durations_s": durations,
        "gpu_vendor": renderer_match.group(1).strip() if renderer_match else None,
        "gpu_renderer": renderer_match.group(2).strip() if renderer_match else None,
    }


def parse_android_window_performance(
    text: str,
    foreground_package: Optional[str] = None,
    foreground_activity: Optional[str] = None,
) -> Dict[str, object]:
    """Extract foreground window bounds without treating them as engine render size."""

    sections: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        header = re.search(r"Window\s+#?\d*\s*Window\{[^}]*\s([^}\s]+)\}", line)
        if header:
            current = {"window": header.group(1), "lines": []}
            sections.append(current)
        if current is not None:
            lines = current["lines"]
            assert isinstance(lines, list)
            lines.append(line)

    if not sections and text.strip():
        sections = [{"window": foreground_package, "lines": text.splitlines()}]

    activity_token = str(foreground_activity or "").rsplit(".", 1)[-1].lower()

    def score(item: Dict[str, object]) -> int:
        window = str(item.get("window") or "").lower()
        value = 0
        if foreground_package and foreground_package.lower() in window:
            value += 4
        if activity_token and activity_token in window:
            value += 2
        return value

    candidates: List[Dict[str, object]] = []
    patterns = (
        ("mFrame", 2, re.compile(r"\bmFrame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")),
        ("frame", 1, re.compile(r"\bframe=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", re.I)),
        (
            "mBounds",
            0,
            re.compile(
                r"\bmBounds=Rect\((-?\d+),\s*(-?\d+)\s*-\s*(-?\d+),\s*(-?\d+)\)"
            ),
        ),
    )
    for section in sections:
        lines = section.get("lines", [])
        body = "\n".join(str(item) for item in lines) if isinstance(lines, list) else ""
        for source, source_priority, pattern in patterns:
            for match in pattern.finditer(body):
                left, top, right, bottom = (int(value) for value in match.groups())
                width = right - left
                height = bottom - top
                if width <= 0 or height <= 0:
                    continue
                candidates.append(
                    {
                        "window": section.get("window"),
                        "render_width_px": width,
                        "render_height_px": height,
                        "render_resolution_source": f"WindowManager {source}",
                        "render_resolution_estimated": True,
                        "score": score(section) + source_priority,
                    }
                )

        requested = re.search(
            r"\b(?:mRequestedWidth|requestedWidth)=(\d+).*?"
            r"\b(?:mRequestedHeight|requestedHeight)=(\d+)",
            body,
            re.S,
        )
        if requested:
            width, height = int(requested.group(1)), int(requested.group(2))
            if width > 0 and height > 0:
                candidates.append(
                    {
                        "window": section.get("window"),
                        "render_width_px": width,
                        "render_height_px": height,
                        "render_resolution_source": "WindowManager requested size",
                        "render_resolution_estimated": True,
                        "score": score(section) + 1,
                    }
                )

    if not candidates:
        return {}
    selected = max(
        candidates,
        key=lambda item: (
            int(item.get("score") or 0),
            int(item.get("render_width_px") or 0) * int(item.get("render_height_px") or 0),
        ),
    )
    return {key: value for key, value in selected.items() if key != "score"}


def parse_android_surface_render_resolution(
    text: str,
    foreground_package: Optional[str] = None,
    surface_layer_name: Optional[str] = None,
) -> Dict[str, object]:
    """Read the active game SurfaceView GraphicBuffer size from SurfaceFlinger."""

    package = str(foreground_package or "").strip().lower()
    layer_token = str(surface_layer_name or "").strip().split(" ", 1)[0].lower()
    if not package and not layer_token:
        return {}

    pattern = re.compile(
        r"^\+\s+name:(?P<name>[^\r\n]+?),\s*"
        r"id:[^,\r\n]+,\s*size:[^,\r\n]+,\s*"
        r"w/h:(?P<width>\d+)x(?P<height>\d+),",
        re.M,
    )
    grouped: Dict[Tuple[int, int], Dict[str, object]] = {}
    for match in pattern.finditer(text or ""):
        name = match.group("name").strip()
        lowered = name.lower()
        if "surfaceview" not in lowered or "blast consumer" not in lowered:
            continue
        if package and package not in lowered and (not layer_token or layer_token not in lowered):
            continue
        width = int(match.group("width"))
        height = int(match.group("height"))
        if width < 64 or height < 64:
            continue
        score = 0
        if layer_token and layer_token in lowered:
            score += 8
        if package and package in lowered:
            score += 4
        if "(blast consumer)" in lowered:
            score += 2
        key = (width, height)
        entry = grouped.setdefault(
            key,
            {"count": 0, "score": 0, "name": name},
        )
        entry["count"] = int(entry["count"]) + 1
        if score >= int(entry["score"]):
            entry["score"] = score
            entry["name"] = name

    if not grouped:
        return {}
    (width, height), selected = max(
        grouped.items(),
        key=lambda item: (
            int(item[1]["score"]),
            int(item[1]["count"]),
            item[0][0] * item[0][1],
        ),
    )
    return {
        "render_width_px": width,
        "render_height_px": height,
        "render_resolution_source": "SurfaceFlinger GraphicBuffer",
        "render_resolution_estimated": False,
        "render_resolution_evidence": str(selected["name"]),
    }


def parse_android_frame_interpolation(
    text: str,
    foreground_package: Optional[str] = None,
    foreground_pid: Optional[int] = None,
) -> Dict[str, object]:
    """Classify explicit vendor MEMC/frame-interpolation switches without cadence guessing."""

    evidence: List[str] = []
    states: List[Tuple[int, bool, str, str]] = []
    key_pattern = re.compile(
        r"(?:memc(?!g)|motion[_ .-]*(?:compensation|smoothing)|"
        r"frame[_ .-]*interpolat|video[_ .-]*(?:motion|enhance)|iris.*memc)",
        re.I,
    )

    def key_value(line: str) -> Tuple[str, str]:
        bracketed = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$", line)
        if bracketed:
            return bracketed.group(1).strip().lower(), bracketed.group(2).strip()
        if "=" in line:
            key, value = line.split("=", 1)
            return key.strip().lower(), value.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            return key.strip().lower(), value.strip()
        return line.strip().lower(), ""

    def bool_value(value: str) -> Optional[bool]:
        token = value.strip("[],'\" ").split(":", 1)[0].lower()
        if token in {"1", "true", "on", "enabled", "enable", "open"}:
            return True
        if token in {"0", "false", "off", "disabled", "disable", "closed", "close"}:
            return False
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not key_pattern.search(line):
            continue
        if line not in evidence:
            evidence.append(line[:300])
        key, value = key_value(line)
        state = bool_value(value)
        if state is None:
            continue

        lowered_line = line.lower()
        package = str(foreground_package or "").strip().lower()
        if package and package in lowered_line:
            states.append((5, state, "current_app", line))
            continue
        if key == "gamecube_frame_interpolation":
            parts = [part.strip() for part in value.split(":")]
            pid_matches = (
                foreground_pid is not None
                and len(parts) >= 3
                and parts[2].isdigit()
                and int(parts[2]) == int(foreground_pid)
            )
            states.append((5 if pid_matches else 3, state, "current_session", line))
            continue
        if key in {
            "memc_main",
            "gpu_memc_switch_to_ic_memc",
            "gpu_memc_frame_rate",
            "cached_memc_sdk_game_target_fps",
        }:
            states.append((4, state, "current_session", line))
            continue
        if any(token in key for token in ("whitelist", "_apps", "mutex", "support", "capability")):
            continue
        if key.startswith("ro."):
            continue
        states.append((2, state, "device", line))

    status = "unavailable"
    label = "系统未公开可验证的插帧开关"
    confidence = "low"
    scope = "none"
    selected_evidence: List[str] = []
    if states:
        strongest = max(item[0] for item in states)
        selected = [item for item in states if item[0] == strongest]
        selected_evidence = [item[3] for item in selected]
        selected_values = {item[1] for item in selected}
        scope = selected[-1][2]
        if len(selected_values) > 1:
            status = "indeterminate"
            label = "插帧状态证据互相冲突，无法确认当前游戏是否启用"
            confidence = "medium"
        elif True in selected_values:
            status = "detected"
            label = "检测到当前插帧 / MEMC 开关已开启"
            confidence = "high" if strongest >= 4 else "medium"
        else:
            status = "disabled"
            label = "检测到当前插帧 / MEMC 开关已关闭"
            confidence = "high" if strongest >= 4 else "medium"
    elif evidence:
        status = "indeterminate"
        label = "发现插帧相关能力，但无法读取当前游戏的有效开关状态"
        confidence = "medium"

    if status == "detected" and scope == "device":
        label = "检测到设备级插帧 / MEMC 开关已开启，无法确认当前游戏是否生效"
    elif status == "disabled" and scope == "device":
        label = "检测到设备级插帧 / MEMC 开关已关闭"

    ordered_evidence = selected_evidence + [
        item for item in evidence if item not in selected_evidence
    ]
    return {
        "frame_interpolation_status": status,
        "frame_interpolation_label": label,
        "frame_interpolation_confidence": confidence,
        "frame_interpolation_scope": scope,
        "frame_interpolation_evidence": ordered_evidence[:20],
    }


def parse_android_gfxinfo(
    text: str,
    foreground_package: Optional[str] = None,
    foreground_activity: Optional[str] = None,
) -> Dict[str, object]:
    """Parse cumulative, reset-free Android gfxinfo counters for one foreground window."""

    sections: List[Dict[str, object]] = [{"window": None}]
    current = sections[0]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Window:"):
            current = {"window": line.split(":", 1)[1].strip()}
            sections.append(current)
            continue
        match = re.match(r"Total frames rendered:\s*(\d+)", line)
        if match:
            current["frame_counter_total"] = int(match.group(1))
            continue
        match = re.match(r"Janky frames:\s*(\d+)", line)
        if match:
            current["frame_counter_janky"] = int(match.group(1))
            continue
        match = re.match(r"Number Missed Vsync:\s*(\d+)\s*$", line)
        if match:
            current["frame_counter_missed_vsync"] = int(match.group(1))
            continue
        match = re.match(r"Number Frame deadline missed:\s*(\d+)\s*$", line)
        if match:
            current["frame_counter_deadline_missed"] = int(match.group(1))
            continue
        if line.startswith("HISTOGRAM:"):
            current["frame_histogram_ms"] = {
                key: int(value)
                for key, value in re.findall(r"(\d+)ms=(\d+)", line)
            }
            continue
        if line.startswith("Flags,"):
            current["frame_stats_columns"] = [
                value.strip() for value in line.split(",") if value.strip()
            ]
            current.setdefault("frame_records", [])
            continue
        if re.fullmatch(r"\d+(?:,\d*){10,},?", line):
            values = [value.strip() for value in line.rstrip(",").split(",")]
            columns = current.get("frame_stats_columns")
            if not isinstance(columns, list) or len(columns) != len(values):
                legacy_columns = [
                    "Flags",
                    "IntendedVsync",
                    "Vsync",
                    "OldestInputEvent",
                    "NewestInputEvent",
                    "HandleInputStart",
                    "AnimationStart",
                    "PerformTraversalsStart",
                    "DrawStart",
                    "SyncQueued",
                    "SyncStart",
                    "IssueDrawCommandsStart",
                    "SwapBuffers",
                    "FrameCompleted",
                    "DequeueBufferDuration",
                    "QueueBufferDuration",
                    "GpuCompleted",
                ]
                timeline_columns = [
                    "Flags",
                    "FrameTimelineVsyncId",
                    "IntendedVsync",
                    "Vsync",
                    "InputEventId",
                    "HandleInputStart",
                    "AnimationStart",
                    "PerformTraversalsStart",
                    "DrawStart",
                    "FrameDeadline",
                    "FrameStartTime",
                    "SyncQueued",
                    "SyncStart",
                    "IssueDrawCommandsStart",
                    "SwapBuffers",
                    "FrameCompleted",
                    "DequeueBufferDuration",
                    "QueueBufferDuration",
                    "GpuCompleted",
                    "SwapBuffersCompleted",
                    "DisplayPresentTime",
                    "CommandSubmissionCompleted",
                ]
                if len(values) == len(legacy_columns):
                    columns = legacy_columns
                elif len(values) == len(timeline_columns):
                    columns = timeline_columns
                else:
                    continue
            record: Dict[str, object] = {}
            invalid = False
            for key, raw_value in zip(columns, values):
                try:
                    record[str(key)] = int(raw_value or "0")
                except ValueError:
                    invalid = True
                    break
            if invalid:
                continue
            records = current.setdefault("frame_records", [])
            if isinstance(records, list):
                records.append(record)

    candidates = [item for item in sections if isinstance(item.get("frame_counter_total"), int)]
    if not candidates:
        return {}

    activity_token = str(foreground_activity or "").rsplit(".", 1)[-1].lower()

    def score(item: Dict[str, object]) -> int:
        window = str(item.get("window") or "")
        lowered = window.lower()
        relevance = 0
        if window:
            relevance += 2
        if foreground_package and foreground_package.lower() in lowered:
            relevance += 4
        if activity_token and activity_token in lowered:
            relevance += 2
        return relevance

    selected = max(enumerate(candidates), key=lambda item: (score(item[1]), -item[0]))[1]
    result = dict(selected)
    result["foreground_window_name"] = selected.get("window") or foreground_package
    result["frame_counter_source"] = "Android gfxinfo cumulative frame histogram"
    result.pop("window", None)
    return result


def parse_android_surface_layers(
    text: str,
    foreground_package: Optional[str] = None,
    foreground_activity: Optional[str] = None,
) -> Dict[str, object]:
    """Select the foreground app buffer layer from SurfaceFlinger --list.

    SurfaceView/BLAST remains the strongest signal for games.  Some Android 16
    builds expose an ordinary application buffer as a child of the window
    container instead; those layers are accepted only when their name starts
    with the foreground component/package and control-only layers are excluded.
    """

    package = str(foreground_package or "").strip().lower()
    if not package:
        return {}
    activity = str(foreground_activity or "").strip().lower()
    activity_token = activity.rsplit(".", 1)[-1]
    qualified_activity = (
        package + activity
        if activity.startswith(".")
        else f"{package}.{activity}"
        if activity and "." not in activity
        else activity
    )
    component_names = {
        f"{package}/{value}"
        for value in (activity, qualified_activity)
        if value
    }
    records: List[Dict[str, object]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name = line
        parent_match = re.search(r"\bparentId=(-?\d+)\b", line)
        if line.startswith("RequestedLayerState{"):
            name = line[len("RequestedLayerState{") :]
            if name.endswith("}"):
                name = name[:-1]
            name = re.split(
                r"\s+(?:parentId|relativeParentId|z)=-?\d+",
                name,
                maxsplit=1,
            )[0].strip()
        lowered = name.lower()
        layer_id_match = re.search(r"#(\d+)\b", name)
        records.append(
            {
                "name": name,
                "lowered": lowered,
                "layer_id": int(layer_id_match.group(1)) if layer_id_match else -1,
                "parent_id": (
                    int(parent_match.group(1)) if parent_match is not None else None
                ),
            }
        )

    records_by_id = {
        int(item["layer_id"]): item
        for item in records
        if isinstance(item.get("layer_id"), int) and int(item["layer_id"]) >= 0
    }

    def layer_depth(item: Dict[str, object]) -> int:
        depth = 0
        parent_id = item.get("parent_id")
        visited: set[int] = set()
        while isinstance(parent_id, int) and parent_id in records_by_id:
            if parent_id in visited:
                break
            visited.add(parent_id)
            depth += 1
            parent_id = records_by_id[parent_id].get("parent_id")
        return depth

    excluded_tokens = (
        "activityrecord",
        "activity record",
        "inputsink",
        "input sink",
        "background for",
        "bounds for",
        "dim layer",
        "windowtoken",
        "transition-leash",
        "animation-leash",
        "starting window",
        "splash screen",
        "snapshot",
        "input consumer",
        "gesture monitor",
        "surfacecontrolviewhost",
    )
    candidates: List[Tuple[Tuple[int, int, int, int, int, int], str]] = []
    for item in records:
        name = str(item["name"])
        lowered = str(item["lowered"])
        if package not in lowered:
            continue
        if any(token in lowered for token in excluded_tokens):
            continue
        is_surfaceview = "surfaceview" in lowered
        is_blast = "(blast)" in lowered
        exact_component = any(value in lowered for value in component_names)
        prefix_name = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", lowered)
        exact_prefix = any(prefix_name.startswith(value) for value in component_names)
        package_prefix = prefix_name.startswith(package + "/") or prefix_name.startswith(
            package + "#"
        )
        if not (is_surfaceview or is_blast) and not (exact_prefix or package_prefix):
            continue
        priority = (
            3
            if is_surfaceview and is_blast
            else 2
            if is_surfaceview or is_blast
            else 1
        )
        layer_id = int(item["layer_id"])
        rank = (
            priority,
            int(exact_prefix),
            int(exact_component),
            int(bool(activity_token and activity_token in lowered)),
            layer_depth(item),
            layer_id,
        )
        candidates.append((rank, name))
    if not candidates:
        return {}
    _, selected = max(candidates, key=lambda item: item[0])
    return {
        "surface_layer_name": selected,
        "surface_layer_type": (
            "blast_surfaceview"
            if "surfaceview" in selected.lower() and "(blast)" in selected.lower()
            else "surfaceview"
            if "surfaceview" in selected.lower()
            else "app_layer"
        ),
        "surface_layer_source": "SurfaceFlinger --list foreground application layer",
    }


def parse_android_surface_latency(text: str) -> Dict[str, object]:
    """Parse SurfaceFlinger --latency timestamps without clearing its ring buffer."""

    refresh_period_ns: Optional[int] = None
    records: List[Tuple[int, int, int]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if refresh_period_ns is None and re.fullmatch(r"\d{6,10}", line):
            refresh_period_ns = int(line)
            continue
        parts = line.split()
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            continue
        desired, actual, ready = (int(part) for part in parts)
        if not desired or not actual or not ready:
            continue
        if max(desired, actual, ready) >= 9_000_000_000_000_000_000:
            continue
        records.append((desired, actual, ready))
    timestamps = sorted({actual for _, actual, _ in records})
    result: Dict[str, object] = {
        "surface_frame_timestamps_ns": timestamps,
        "surface_latency_frame_count": len(timestamps),
    }
    if refresh_period_ns is not None and refresh_period_ns > 0:
        result["surface_refresh_period_ns"] = refresh_period_ns
    return result


def _percentile_float(values: Sequence[float], quantile: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def android_surface_frame_metrics(
    intervals_ms: Sequence[float],
    refresh_rate_hz: Optional[float] = None,
) -> Dict[str, object]:
    """Summarize newly presented SurfaceFlinger frames for one context window."""

    intervals = [float(value) for value in intervals_ms if 0.1 <= float(value) <= 2000.0]
    if not intervals:
        return {}
    average_ms = statistics.fmean(intervals)
    median_ms = statistics.median(intervals)
    display_budget_ms = (
        1000.0 / float(refresh_rate_hz)
        if isinstance(refresh_rate_hz, (int, float)) and float(refresh_rate_hz) > 0
        else None
    )
    cadence_divisor = 1
    if display_budget_ms is not None:
        candidate = max(1, min(4, int(round(median_ms / display_budget_ms))))
        if abs(median_ms - display_budget_ms * candidate) <= display_budget_ms * 0.18:
            cadence_divisor = candidate
    frame_budget_ms = (
        display_budget_ms * cadence_divisor
        if display_budget_ms is not None
        else median_ms
    )
    missed = [value for value in intervals if value > frame_budget_ms * 1.5]
    severe = [value for value in intervals if value > frame_budget_ms * 2.5]
    frozen = [value for value in intervals if value > max(700.0, frame_budget_ms * 4.5)]
    missed_slots = sum(
        max(0, int(round(value / frame_budget_ms)) - 1)
        for value in intervals
    )
    slowest_count = max(1, math.ceil(len(intervals) * 0.01))
    slowest_average = statistics.fmean(sorted(intervals, reverse=True)[:slowest_count])
    return {
        "compositor_fps": 1000.0 / average_ms if average_ms > 0 else None,
        "frame_intervals_ms": intervals,
        "frame_interval_average_ms": average_ms,
        "frame_interval_p95_ms": _percentile_float(intervals, 0.95),
        "frame_interval_p99_ms": _percentile_float(intervals, 0.99),
        "frame_interval_maximum_ms": max(intervals),
        "one_percent_low_fps": (
            1000.0 / slowest_average if slowest_average > 0 else None
        ),
        "frame_sample_count": len(intervals),
        "missed_vsync_interval_count": len(missed),
        "severe_frame_interval_count": len(severe),
        "frozen_frame_interval_count": len(frozen),
        "missed_vsync_slot_count": missed_slots,
        "frame_budget_ms": frame_budget_ms,
        "frame_cadence_divisor": cadence_divisor,
        "surface_frame_source": True,
        "frame_counter_source": (
            "Android SurfaceFlinger foreground application-layer present timestamps"
        ),
    }


def collect_android_surface_latency(
    adb: str,
    device: str,
    layer_name: str,
) -> Tuple[Dict[str, object], Optional[str]]:
    result = adb_shell(
        adb,
        device,
        f"dumpsys SurfaceFlinger --latency {shlex.quote(layer_name)}",
        timeout_s=12.0,
    )
    if not result.ok:
        return {}, (result.stderr or result.stdout).strip() or "SurfaceFlinger latency failed"
    return parse_android_surface_latency(result.stdout), None


def parse_android_touch_devices(text: str) -> Dict[str, object]:
    """Read touchscreen capabilities without inferring a hardware scan frequency."""

    devices: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        axes = current.get("axes", {})
        if current.get("direct") and isinstance(axes, dict) and {"x", "y"} <= set(axes):
            slot = axes.get("slot")
            if isinstance(slot, dict) and isinstance(slot.get("max"), int):
                current["max_touch_points"] = int(slot["max"]) + 1
            devices.append(current)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = re.match(r"add device\s+\d+:\s*(\S+)", line)
        if match:
            finish()
            current = {"path": match.group(1), "name": None, "direct": False, "axes": {}}
            continue
        if current is None:
            continue
        match = re.match(r'name:\s*"([^"]+)"', line)
        if match:
            current["name"] = match.group(1)
            continue
        if "INPUT_PROP_DIRECT" in line:
            current["direct"] = True
            continue
        match = re.search(
            r"ABS_MT_(SLOT|POSITION_X|POSITION_Y|TOUCH_MAJOR|TOUCH_MINOR)\s*:.*?"
            r"min\s+(-?\d+),\s*max\s+(-?\d+)",
            line,
        )
        if match:
            axis_name = {
                "SLOT": "slot",
                "POSITION_X": "x",
                "POSITION_Y": "y",
                "TOUCH_MAJOR": "touch_major",
                "TOUCH_MINOR": "touch_minor",
            }[match.group(1)]
            axes = current["axes"]
            assert isinstance(axes, dict)
            axes[axis_name] = {"min": int(match.group(2)), "max": int(match.group(3))}
    finish()
    return {
        "devices": devices,
        "sampling_rate_available": False,
        "sampling_rate_reason": (
            "Android input capabilities expose axes and touch slots, but not the panel controller "
            "hardware scan frequency."
        ),
    }


def build_sampler_script(
    duration_s: int,
    interval_s: float,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    memory_source: Optional[MemorySource] = None,
    voltage_period_s: float = 5.0,
    context_period_s: float = 10.0,
    refresh_period_s: float = 30.0,
    emit_context_samples: bool = True,
    session_has_root: bool = False,
) -> str:
    def cat_command(path: str, *, requires_root: bool = False) -> str:
        quoted_path = shlex.quote(path)
        if requires_root and not session_has_root:
            return f"su -c {shlex.quote(f'cat {quoted_path}')}"
        return f"cat {quoted_path}"

    core_ids = sorted({core for policy in policies for core in policy.cores})
    voltage_every = max(1, int(math.ceil(voltage_period_s / interval_s)))
    temperature_every = max(1, int(math.ceil(context_period_s / interval_s)))
    context_every = max(1, int(math.ceil(context_period_s / interval_s)))
    refresh_every = max(context_every, int(math.ceil(refresh_period_s / interval_s)))
    lines = [
        "emit_context() {",
        "ctx_up=$1",
        "ctx_refresh=$2",
        "component=$(dumpsys activity activities 2>/dev/null | awk '/ResumedActivity|mFocusedApp/ { for (n=1; n<=NF; n++) if ($n ~ /\\//) { print $n; exit } }' | tr -d '},')",
        "if [ -z \"$component\" ]; then component=$(dumpsys window windows 2>/dev/null | awk '/mCurrentFocus|mFocusedApp/ { for (n=1; n<=NF; n++) if ($n ~ /\\//) { print $n; exit } }' | tr -d '},'); fi",
        "[ -n \"$component\" ] || component=unknown",
        "screen=$(dumpsys power 2>/dev/null | awk -F= '/mWakefulnessRaw=/{print $2; exit} /mWakefulness=/{print $2; exit}')",
        "set -- $screen; screen=$1; [ -n \"$screen\" ] || screen=unknown",
        "brightness=$(settings get system screen_brightness 2>/dev/null)",
        "set -- $brightness; brightness=$1; [ -n \"$brightness\" ] || brightness=-1",
        "refresh=-1",
        "if [ \"$ctx_refresh\" -eq 1 ]; then",
        "refresh=$(dumpsys display 2>/dev/null | awk -F= '/mActiveRenderFrameRate=/{print $2; exit} /renderFrameRate=/{print $2; exit}' | awk '{print $1}')",
        "set -- $refresh; refresh=$1; [ -n \"$refresh\" ] || refresh=-1",
        "fi",
        "printf 'CTX|%s|%s|%s|%s|%s\\n' \"$ctx_up\" \"$component\" \"$screen\" \"$brightness\" \"$refresh\"",
        "}",
        "read first_up rest < /proc/uptime",
        "start=${first_up%%.*}",
        f"end=$((start+{int(duration_s)}+1))",
        "i=0",
        "volt=-1",
        "temp=-1",
        "powered=-1",
        "battery_status=-1",
    ]
    cpu_names = ["u", "n", "s", "idle", "io", "irq", "sirq", "steal"]
    body = [
        "cur=$(cmd battery get -f current_now 2>/dev/null)",
        "set -- $cur; cur=$1",
        "[ -n \"$cur\" ] || cur=0",
        (
            f"if [ $((i % {voltage_every})) -eq 0 ]; then "
            "battery_state=$(dumpsys battery 2>/dev/null); "
            "volt=$(printf '%s\\n' \"$battery_state\" | sed -n 's/^  voltage: *//p' | head -n 1); "
            "[ -n \"$volt\" ] || volt=-1; "
            "powered=$(printf '%s\\n' \"$battery_state\" | awk -F: "
            "'tolower($1) ~ /(ac|usb|wireless|dock) powered/ "
            "{seen=1; if (tolower($2) ~ /true/) p=1} "
            "END {if (seen) print p+0; else print -1}'); "
            "battery_status=$(printf '%s\\n' \"$battery_state\" | sed -n 's/^  status: *//p' | head -n 1); "
            "[ -n \"$battery_status\" ] || battery_status=-1; fi"
        ),
        f"if [ $((i % {temperature_every})) -eq 0 ]; then temp=$(cmd battery get -f temp 2>/dev/null); set -- $temp; temp=$1; [ -n \"$temp\" ] || temp=-1; fi",
        "read up rest < /proc/uptime",
    ]
    for prefix in ["g"] + [f"c{core}" for core in core_ids]:
        body.extend(f"{prefix}_{name}=0" for name in cpu_names)

    case_parts = []
    read_names = ["a", "b", "c", "d", "e", "f", "g", "h"]
    assignments = "; ".join(
        f"g_{name}=${read_names[index]}" for index, name in enumerate(cpu_names)
    )
    case_parts.append(f"cpu) {assignments} ;;")
    for core in core_ids:
        assignments = "; ".join(
            f"c{core}_{name}=${read_names[index]}" for index, name in enumerate(cpu_names)
        )
        case_parts.append(f"cpu{core}) {assignments} ;;")
    case_parts.append("intr) break ;;")
    body.append(
        "while read tag a b c d e f g h rest; do case \"$tag\" in "
        + " ".join(case_parts)
        + " esac; done < /proc/stat"
    )

    for policy in policies:
        var_name = f"f_{policy.name}"
        body.extend(
            [
                f"{var_name}=$(cat {policy.path}/scaling_cur_freq 2>/dev/null)",
                f"[ -n \"${var_name}\" ] || {var_name}=0",
            ]
        )

    if gpu_source:
        if gpu_source.frequency_path:
            body.extend(
                [
                    f"gpu_f=$({cat_command(gpu_source.frequency_path, requires_root=gpu_source.requires_root)} 2>/dev/null)",
                    "set -- $gpu_f; gpu_f=$1; [ -n \"$gpu_f\" ] || gpu_f=-1",
                ]
            )
        else:
            body.append("gpu_f=-1")
        if gpu_source.load_path:
            if gpu_source.load_format == "busy_total":
                body.extend(
                    [
                        f"gpu_l=$({cat_command(gpu_source.load_path, requires_root=gpu_source.requires_root)} 2>/dev/null | awk '{{ if ($2 > 0) printf \"%.3f\", 100 * $1 / $2; else print -1 }}')",
                        "set -- $gpu_l; gpu_l=$1; [ -n \"$gpu_l\" ] || gpu_l=-1",
                    ]
                )
            else:
                body.extend(
                    [
                        f"gpu_l=$({cat_command(gpu_source.load_path, requires_root=gpu_source.requires_root)} 2>/dev/null)",
                        "set -- $gpu_l; gpu_l=$1; gpu_l=$(echo \"$gpu_l\" | tr -d '%'); [ -n \"$gpu_l\" ] || gpu_l=-1",
                    ]
                )

    if memory_source is not None:
        body.extend(
            [
                f"mem_f=$(cat {memory_source.frequency_path} 2>/dev/null)",
                "set -- $mem_f; mem_f=$1; [ -n \"$mem_f\" ] || mem_f=-1",
            ]
        )

    fields = [
        "$i",
        "$up",
        "$cur",
        "$volt",
        "$temp",
        "$powered",
        "$battery_status",
    ]
    fields.extend(f"$g_{name}" for name in cpu_names)
    for core in core_ids:
        fields.extend(f"$c{core}_{name}" for name in cpu_names)
    fields.extend(f"$f_{policy.name}" for policy in policies)
    if gpu_source:
        fields.append("$gpu_f")
        if gpu_source.load_path:
            fields.append("$gpu_l")
    if memory_source is not None:
        fields.append("$mem_f")
    sample_format = "S|" + "|".join(["%s"] * len(fields)) + "\\n"
    sample_args = " ".join(f'\"{field}\"' for field in fields)
    body.append(f"printf '{sample_format}' {sample_args}")
    if emit_context_samples:
        body.append(
            f"if [ $((i % {context_every})) -eq 0 ]; then ctx_refresh=0; "
            f"if [ $((i % {refresh_every})) -eq 0 ]; then ctx_refresh=1; fi; "
            'emit_context "$up" "$ctx_refresh" & fi'
        )
    body.extend(
        [
            "now=${up%%.*}",
            "i=$((i+1))",
            "if [ \"$now\" -ge \"$end\" ] && [ \"$i\" -gt 1 ]; then break; fi",
            "read after_up rest < /proc/uptime",
            (
                "delay=$(awk -v sample=\"$up\" "
                f"-v step=\"{interval_s:.3f}\" "
                "-v now=\"$after_up\" 'BEGIN { due = sample + step; delay = due - now; "
                "if (delay < 0) { due = now + step; delay = step } "
                "printf \"%.3f\", delay }')"
            ),
            "if [ \"$delay\" != \"0.000\" ]; then sleep \"$delay\"; fi",
        ]
    )
    lines.extend(["while true; do", *body, "done", "wait"])
    return "\n".join(lines)


def _sampler_shell_command(script: str, use_root_session: bool) -> str:
    if not use_root_session:
        return script
    return f"su -c {shlex.quote(script)}"


def parse_sampler_line(
    line: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    memory_source: Optional[MemorySource] = None,
) -> RawSample | Sample | ContextSample | None:
    stripped = line.strip()
    if stripped.startswith("N|"):
        try:
            payload = json.loads(stripped[2:])
            if not isinstance(payload, dict):
                return None
            return Sample(
                index=int(payload.get("index") or 0),
                elapsed_s=float(payload.get("elapsed_s") or 0.0),
                uptime_s=float(payload["uptime_s"]),
                current_ma=abs(float(payload.get("current_ma") or 0.0)),
                signed_current_ma=float(payload.get("signed_current_ma") or 0.0),
                voltage_mv=float(payload.get("voltage_mv") or 0.0),
                power_mw=max(0.0, float(payload.get("power_mw") or 0.0)),
                direction=str(payload.get("direction") or "unknown"),
                cpu_pct=(
                    float(payload["cpu_pct"])
                    if isinstance(payload.get("cpu_pct"), (int, float))
                    else None
                ),
                core_cpu_pct={
                    str(name): float(value)
                    for name, value in dict(payload.get("core_cpu_pct") or {}).items()
                },
                cluster_cpu_pct={
                    str(name): float(value)
                    for name, value in dict(payload.get("cluster_cpu_pct") or {}).items()
                },
                frequencies_mhz={
                    str(name): float(value)
                    for name, value in dict(payload.get("frequencies_mhz") or {}).items()
                },
                gpu_frequency_mhz=(
                    float(payload["gpu_frequency_mhz"])
                    if isinstance(payload.get("gpu_frequency_mhz"), (int, float))
                    else None
                ),
                gpu_load_pct=(
                    float(payload["gpu_load_pct"])
                    if isinstance(payload.get("gpu_load_pct"), (int, float))
                    else None
                ),
                memory_frequency_mhz=(
                    float(payload["memory_frequency_mhz"])
                    if isinstance(payload.get("memory_frequency_mhz"), (int, float))
                    else None
                ),
                battery_temperature_c=(
                    float(payload["battery_temperature_c"])
                    if isinstance(payload.get("battery_temperature_c"), (int, float))
                    else None
                ),
                power_source=str(payload.get("power_source") or "platform_reported"),
                power_sample_age_s=(
                    float(payload["power_sample_age_s"])
                    if isinstance(payload.get("power_sample_age_s"), (int, float))
                    else None
                ),
                collector_cpu_pct=(
                    float(payload["collector_cpu_pct"])
                    if isinstance(payload.get("collector_cpu_pct"), (int, float))
                    else None
                ),
                power_valid_for_consumption=(
                    bool(payload["power_valid_for_consumption"])
                    if isinstance(payload.get("power_valid_for_consumption"), bool)
                    else None
                ),
                external_power=(
                    bool(payload["external_power"])
                    if isinstance(payload.get("external_power"), bool)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    if stripped.startswith("CTX|"):
        parts = stripped.split("|", 5)
        if len(parts) != 6:
            return None
        try:
            uptime_s = float(parts[1])
        except ValueError:
            return None
        component = parts[2].strip()
        package = None
        activity = None
        if component and component != "unknown":
            if "/" in component:
                package, activity = component.split("/", 1)
            else:
                package = component
        try:
            brightness = float(parts[4])
        except ValueError:
            brightness = -1.0
        try:
            refresh = float(parts[5])
        except ValueError:
            refresh = -1.0
        return ContextSample(
            uptime_s=uptime_s,
            foreground_package=package,
            foreground_activity=activity,
            screen_state=parts[3].strip() if parts[3].strip() != "unknown" else None,
            brightness_raw=brightness if brightness >= 0 else None,
            refresh_rate_hz=refresh if refresh >= 0 else None,
        )

    if stripped.startswith("S|"):
        parts = [part.strip() for part in stripped.split("|")[1:]]
    else:
        parts = [part.strip() for part in stripped.split(",")]
    core_ids = sorted({core for policy in policies for core in policy.cores})
    payload_field_count = 8 + len(core_ids) * 8 + len(policies)
    if gpu_source:
        payload_field_count += 1 + (1 if gpu_source.load_path else 0)
    if memory_source is not None:
        payload_field_count += 1
    base_field_count = len(parts) - payload_field_count
    if base_field_count not in {5, 6, 7}:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    external_power = (
        bool(int(values[5])) if base_field_count >= 6 and values[5] >= 0 else None
    )
    battery_status = None
    if base_field_count >= 7:
        battery_status = {
            1: "unknown",
            2: "charging",
            3: "discharging",
            4: "not_charging",
            5: "full",
        }.get(int(values[6]))
    offset = base_field_count
    global_cpu = CpuTimes.from_values(values[offset : offset + 8])
    offset += 8
    core_cpu: Dict[int, CpuTimes] = {}
    for core in core_ids:
        core_cpu[core] = CpuTimes.from_values(values[offset : offset + 8])
        offset += 8
    frequencies = {}
    for policy in policies:
        frequencies[policy.name] = values[offset]
        offset += 1
    gpu_frequency_raw = None
    gpu_load_raw = None
    memory_frequency_raw = None
    if gpu_source:
        gpu_frequency_raw = values[offset] if values[offset] >= 0 else None
        offset += 1
        if gpu_source.load_path:
            gpu_load_raw = values[offset] if values[offset] >= 0 else None
            offset += 1
    if memory_source is not None:
        memory_frequency_raw = values[offset] if values[offset] >= 0 else None
    return RawSample(
        index=int(values[0]),
        uptime_s=values[1],
        current_raw=values[2],
        voltage_mv=values[3] if values[3] >= 0 else None,
        temperature_tenths_c=values[4] if values[4] >= 0 else None,
        cpu=global_cpu,
        core_cpu=core_cpu,
        frequencies_khz=frequencies,
        gpu_frequency_raw=gpu_frequency_raw,
        gpu_load_raw=gpu_load_raw,
        memory_frequency_raw=memory_frequency_raw,
        external_power=external_power,
        battery_status=battery_status,
    )


def parse_raw_samples(
    text: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    memory_source: Optional[MemorySource] = None,
) -> List[RawSample]:
    rows: List[RawSample] = []
    for line in text.splitlines():
        parsed = parse_sampler_line(line, policies, gpu_source, memory_source)
        if isinstance(parsed, RawSample):
            rows.append(parsed)
    return rows


def parse_normalized_samples(
    text: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    memory_source: Optional[MemorySource] = None,
) -> List[Sample]:
    rows: List[Sample] = []
    base_uptime: Optional[float] = None
    last_uptime: Optional[float] = None
    for line in text.splitlines():
        parsed = parse_sampler_line(line, policies, gpu_source, memory_source)
        if not isinstance(parsed, Sample):
            continue
        if last_uptime is not None and parsed.uptime_s <= last_uptime:
            continue
        if base_uptime is None:
            base_uptime = parsed.uptime_s
        parsed.index = len(rows)
        parsed.elapsed_s = parsed.uptime_s - base_uptime
        rows.append(parsed)
        last_uptime = parsed.uptime_s
    return rows


def parse_context_samples(
    text: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    memory_source: Optional[MemorySource] = None,
) -> List[ContextSample]:
    rows: List[ContextSample] = []
    for line in text.splitlines():
        parsed = parse_sampler_line(line, policies, gpu_source, memory_source)
        if isinstance(parsed, ContextSample):
            rows.append(parsed)
    return rows


def collect_clock_sync(adb: str, device: str) -> Optional[ClockSyncPoint]:
    start_epoch = time.time()
    start_monotonic = time.monotonic()
    result = adb_shell(adb, device, ["cat", "/proc/uptime"], timeout_s=10)
    end_epoch = time.time()
    end_monotonic = time.monotonic()
    uptime = first_number(result.stdout)
    if not result.ok or uptime is None:
        return None
    return ClockSyncPoint(
        host_epoch_s=(start_epoch + end_epoch) * 0.5,
        host_monotonic_s=(start_monotonic + end_monotonic) * 0.5,
        device_uptime_s=uptime,
        round_trip_ms=(end_monotonic - start_monotonic) * 1000.0,
    )


def _device_ready(adb: str, device: str) -> bool:
    result = run_command(adb_prefix(adb, device) + ["get-state"], timeout_s=5)
    return result.ok and result.stdout.strip() == "device"


def _wait_for_device(
    adb: str,
    device: str,
    deadline_monotonic: float,
    timeout_s: float,
) -> bool:
    wait_deadline = min(deadline_monotonic, time.monotonic() + timeout_s)
    while time.monotonic() < wait_deadline:
        if _device_ready(adb, device):
            return True
        if ":" in device:
            run_command([adb, "connect", device], timeout_s=8)
            if _device_ready(adb, device):
                return True
        time.sleep(2.0)
    return False


def _read_process_stream(
    stream: IO[str],
    stream_name: str,
    messages: "queue.Queue[Tuple[str, Optional[str]]]",
) -> None:
    try:
        for line in stream:
            messages.put((stream_name, line))
    finally:
        messages.put((stream_name, None))


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


_UPTIME_MARKER = "__APP_UPTIME__|"
_PS_MARKER = "__APP_PS__"
_PROCESS_MARKER = "__APP_TOP_PROCESSES__"
_THREAD_MARKER = "__APP_TOP_THREADS__"
_THREAD_COUNT_MARKER = "__APP_THREAD_COUNT__|"
_POWER_STATE_MARKER = "__APP_POWER_STATE__"
_ANDROID_CONTEXT_MARKER = "__APP_ANDROID_CONTEXT__|"
_ANDROID_DISPLAY_MARKER = "__APP_ANDROID_DISPLAY__"
_ANDROID_SURFACE_MARKER = "__APP_ANDROID_SURFACE__"
_ANDROID_LAYER_MARKER = "__APP_ANDROID_LAYER__"
_ANDROID_WINDOW_MARKER = "__APP_ANDROID_WINDOW__"
_ANDROID_GFXINFO_MARKER = "__APP_ANDROID_GFXINFO__"
_BRIGHTNESS_SETTING_MARKER = "__APP_BRIGHTNESS_SETTING__|"
_BRIGHTNESS_FLOAT_MARKER = "__APP_BRIGHTNESS_FLOAT__|"
_BRIGHTNESS_DISPLAY_MARKER = "__APP_BRIGHTNESS_DISPLAY__"


def _timed_shell_output(
    adb: str,
    device: str,
    command: str,
    timeout_s: float,
) -> Tuple[Optional[float], float, str, float, Optional[str]]:
    script = (
        "read app_profiler_up app_profiler_rest < /proc/uptime; "
        f"printf '{_UPTIME_MARKER}%s\\n' \"$app_profiler_up\"; "
        + command
    )
    host_start = time.time()
    result = adb_shell(adb, device, script, timeout_s=timeout_s)
    host_end = time.time()
    uptime_s: Optional[float] = None
    output_lines: List[str] = []
    for line in result.stdout.splitlines():
        if line.startswith(_UPTIME_MARKER):
            uptime_s = first_number(line[len(_UPTIME_MARKER) :])
        else:
            output_lines.append(line)
    error = None
    if not result.ok or uptime_s is None:
        error = (result.stderr or result.stdout).strip().splitlines()
        error = error[-1] if error else f"command failed with exit code {result.returncode}"
    return (
        uptime_s,
        (host_start + host_end) * 0.5,
        "\n".join(output_lines),
        result.elapsed_s * 1000.0,
        error,
    )


def _android_performance_context_script(
    include_surface_flinger: bool,
    include_foreground: bool,
    include_frame_rate: bool,
    include_window: bool,
    include_frame_details: bool,
    include_gfxinfo_summary: bool = True,
) -> str:
    needs_component = include_foreground or include_window or include_frame_rate
    lines = [
        "app_ctx_screen=$(dumpsys power 2>/dev/null | "
        "awk -F= '/mWakefulnessRaw=/{print $2; exit} /mWakefulness=/{print $2; exit}')",
        "set -- $app_ctx_screen; app_ctx_screen=$1; "
        "[ -n \"$app_ctx_screen\" ] || app_ctx_screen=unknown",
        "case \"$app_ctx_screen\" in "
        "Asleep|asleep|OFF|Off|off|Doze*|DOZE*) app_ctx_screen_active=0 ;; "
        "*) app_ctx_screen_active=1 ;; esac",
    ]
    if needs_component:
        lines.extend(
            [
                "app_ctx_component=unknown",
                "if [ \"$app_ctx_screen_active\" = 1 ]; then",
                "app_ctx_component=$(dumpsys activity activities 2>/dev/null | "
                "awk '/ResumedActivity|mFocusedApp/ { for (n=1; n<=NF; n++) "
                "if ($n ~ /\\//) { print $n; exit } }' | tr -d '},')",
                "if [ -z \"$app_ctx_component\" ]; then app_ctx_component=$(dumpsys window windows 2>/dev/null | "
                "awk '/mCurrentFocus|mFocusedApp/ { for (n=1; n<=NF; n++) if ($n ~ /\\//) "
                "{ print $n; exit } }' | tr -d '},'); fi",
                "[ -n \"$app_ctx_component\" ] || app_ctx_component=unknown",
                "fi",
            ]
        )
    else:
        lines.append("app_ctx_component=unknown")
    lines.extend(
        [
        "app_ctx_package=${app_ctx_component%%/*}",
        "app_ctx_brightness=$(settings get system screen_brightness 2>/dev/null)",
        "set -- $app_ctx_brightness; app_ctx_brightness=$1; "
        "[ -n \"$app_ctx_brightness\" ] || app_ctx_brightness=-1",
        f"printf '{_ANDROID_CONTEXT_MARKER}%s|%s|%s\\n' "
        '"$app_ctx_component" "$app_ctx_screen" "$app_ctx_brightness"',
        f"printf '{_ANDROID_DISPLAY_MARKER}\\n'",
        "dumpsys display 2>/dev/null | grep -E "
        "'mActiveRenderFrameRate[[:space:]]*=|mActiveSfDisplayMode[[:space:]]*=|"
        "DisplayMode\\{id=|supportedRefreshRates' | head -n 120",
        f"printf '{_ANDROID_SURFACE_MARKER}\\n'",
        ]
    )
    if include_surface_flinger and include_frame_rate:
        lines.append(
            "if [ \"$app_ctx_screen_active\" = 1 ]; then "
            "dumpsys SurfaceFlinger 2>/dev/null | grep -E "
            "'^[[:space:]]*(ScreenOff:|[0-9]+([.][0-9]+)? Hz:|GLES:)'"
            "; fi"
        )
    lines.append(f"printf '{_ANDROID_LAYER_MARKER}\\n'")
    if include_frame_rate:
        lines.append(
            "if [ \"$app_ctx_screen_active\" = 1 ] && "
            "[ \"$app_ctx_package\" != unknown ]; then "
            "dumpsys SurfaceFlinger --list 2>/dev/null | "
            "grep -F \"$app_ctx_package\" | head -n 160; fi"
        )
    lines.append(f"printf '{_ANDROID_WINDOW_MARKER}\\n'")
    if include_window:
        lines.append(
            "if [ \"$app_ctx_screen_active\" = 1 ] && "
            "[ \"$app_ctx_package\" != unknown ]; then "
            "dumpsys window windows 2>/dev/null | awk -v pkg=\"$app_ctx_package\" "
            "'/^[[:space:]]*Window #[0-9]+ Window\\{/ "
            "{ capture=(index($0,pkg)>0); lines=0 } "
            "capture && lines<80 { print; lines++ }'; fi"
        )
    lines.append(f"printf '{_ANDROID_GFXINFO_MARKER}\\n'")
    if include_frame_rate and include_frame_details:
        lines.append(
            "if [ \"$app_ctx_screen_active\" = 1 ] && "
            "[ \"$app_ctx_package\" != unknown ]; then "
            "dumpsys gfxinfo \"$app_ctx_package\" framestats 2>/dev/null | grep -E "
            "'^[[:space:]]*(Window:|Total frames rendered:|Janky frames:|"
            "Number Missed Vsync:|Number Frame deadline missed:|HISTOGRAM:|"
            "Flags,|[0-9]+,)' | head -n 360; fi"
        )
    elif include_frame_rate and include_gfxinfo_summary:
        lines.append(
            "if [ \"$app_ctx_screen_active\" = 1 ] && "
            "[ \"$app_ctx_package\" != unknown ]; then "
            "dumpsys gfxinfo \"$app_ctx_package\" framestats 2>/dev/null | grep -E "
            "'^[[:space:]]*(Window:|Total frames rendered:|Janky frames:|"
            "Number Missed Vsync:|Number Frame deadline missed:|HISTOGRAM:)' | head -n 120; fi"
        )
    return "\n".join(lines)


def collect_android_performance_context(
    adb: str,
    device: str,
    include_surface_flinger: bool = True,
    include_foreground: bool = True,
    include_frame_rate: bool = True,
    include_window: bool = True,
    include_frame_details: bool = False,
    include_gfxinfo_summary: bool = True,
    cached_surface: Optional[Dict[str, object]] = None,
) -> Tuple[Optional[ContextSample], Dict[str, object], Optional[str]]:
    uptime_s, _, output, _, error = _timed_shell_output(
        adb,
        device,
        _android_performance_context_script(
            include_surface_flinger,
            include_foreground,
            include_frame_rate,
            include_window,
            include_frame_details,
            include_gfxinfo_summary,
        ),
        timeout_s=25.0,
    )
    if uptime_s is None:
        return None, {}, error

    sections: Dict[str, List[str]] = {
        _ANDROID_DISPLAY_MARKER: [],
        _ANDROID_SURFACE_MARKER: [],
        _ANDROID_LAYER_MARKER: [],
        _ANDROID_WINDOW_MARKER: [],
        _ANDROID_GFXINFO_MARKER: [],
    }
    current_section: Optional[str] = None
    component = "unknown"
    screen = "unknown"
    brightness = -1.0
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(_ANDROID_CONTEXT_MARKER):
            values = stripped[len(_ANDROID_CONTEXT_MARKER) :].split("|", 2)
            if values:
                component = values[0].strip() or "unknown"
            if len(values) >= 2:
                screen = values[1].strip() or "unknown"
            if len(values) >= 3:
                parsed_brightness = first_number(values[2])
                brightness = parsed_brightness if parsed_brightness is not None else -1.0
            current_section = None
            continue
        if stripped in sections:
            current_section = stripped
            continue
        if current_section is not None:
            sections[current_section].append(line)

    foreground_package = foreground_activity = None
    if component != "unknown":
        if "/" in component:
            foreground_package, foreground_activity = component.split("/", 1)
        else:
            foreground_package = component

    screen_awake = screen.lower() not in {"asleep", "off"} and not screen.lower().startswith("doze")
    display = parse_android_display_performance(
        "\n".join(sections[_ANDROID_DISPLAY_MARKER])
    )
    surface = parse_android_surfaceflinger_performance(
        "\n".join(sections[_ANDROID_SURFACE_MARKER])
    )
    surface_update = {
        key: value
        for key, value in surface.items()
        if value not in (None, {}, [])
    }
    performance: Dict[str, object] = dict(cached_surface or {}) if screen_awake else {}
    performance.update(surface_update)
    performance.update(display)
    surface_layer = parse_android_surface_layers(
        "\n".join(sections[_ANDROID_LAYER_MARKER]),
        foreground_package,
        foreground_activity,
    )
    performance.update(surface_layer)
    performance.update(
        parse_android_window_performance(
            "\n".join(sections[_ANDROID_WINDOW_MARKER]),
            foreground_package,
            foreground_activity,
        )
    )
    gfxinfo = parse_android_gfxinfo(
        "\n".join(sections[_ANDROID_GFXINFO_MARKER]),
        foreground_package,
        foreground_activity,
    )
    performance.update(gfxinfo)
    performance["screen_state"] = screen if screen != "unknown" else None
    performance["foreground_active"] = screen_awake and foreground_package is not None
    if not screen_awake:
        if foreground_package:
            performance["last_known_foreground_package"] = foreground_package
        if component != "unknown":
            performance["last_known_foreground_window_name"] = component
        last_refresh = display.get("refresh_rate_hz")
        if isinstance(last_refresh, (int, float)):
            performance["last_reported_refresh_rate_hz"] = float(last_refresh)
        performance["refresh_rate_hz"] = None
        performance["refresh_rate_live_valid"] = False
        performance["foreground_window_name"] = None
        foreground_package = None
        foreground_activity = None
    else:
        performance["refresh_rate_live_valid"] = isinstance(
            display.get("refresh_rate_hz"), (int, float)
        )
    performance["frame_data_available"] = (
        bool(gfxinfo or surface_layer) and screen_awake
        if include_frame_rate
        else False
    )
    if include_frame_rate and (not screen_awake or not (gfxinfo or surface_layer)):
        performance["frame_unavailable_reason"] = (
            "屏幕未处于亮屏交互状态，未采集前台应用帧数据。"
            if not screen_awake
            else "前台应用未向 Android gfxinfo 或 SurfaceFlinger 暴露可用帧数据。"
        )
    performance["brightness_raw"] = brightness if brightness >= 0 else None
    performance.setdefault("foreground_window_name", component if component != "unknown" else None)
    performance["platform"] = "android"

    refresh = performance.get("refresh_rate_hz")
    return (
        ContextSample(
            uptime_s=uptime_s,
            foreground_package=foreground_package,
            foreground_activity=foreground_activity,
            screen_state=screen if screen != "unknown" else None,
            brightness_raw=brightness if brightness >= 0 else None,
            refresh_rate_hz=float(refresh) if isinstance(refresh, (int, float)) else None,
            source="android-performance-context",
            performance=performance,
        ),
        surface_update,
        error,
    )


def probe_android_performance(
    adb: str,
    device: str,
    *,
    include_frame_rate: bool = True,
    include_frame_details: bool = True,
) -> Dict[str, object]:
    power_result = adb_shell(adb, device, ["dumpsys", "power"], timeout_s=20)
    wakefulness_match = re.search(
        r"^\s*mWakefulness(?:Raw)?\s*=\s*([^\s]+)",
        power_result.stdout,
        re.M,
    )
    screen_state = wakefulness_match.group(1).strip() if wakefulness_match else None
    screen_text = str(screen_state or "").strip().lower()
    screen_awake = not screen_text or (
        screen_text not in {"asleep", "off"} and not screen_text.startswith("doze")
    )
    display_result = adb_shell(adb, device, ["dumpsys", "display"], timeout_s=30)
    surface_result = adb_shell(adb, device, ["dumpsys", "SurfaceFlinger"], timeout_s=30)
    layer_result = adb_shell(
        adb,
        device,
        ["dumpsys", "SurfaceFlinger", "--list"],
        timeout_s=30,
    )
    window_result = adb_shell(adb, device, ["dumpsys", "window", "windows"], timeout_s=30)
    touch_result = adb_shell(adb, device, ["getevent", "-lp"], timeout_s=20)
    interpolation_result = adb_shell(
        adb,
        device,
        "{ getprop; settings list system; settings list secure; settings list global; "
        "} 2>/dev/null | grep -Ei "
        "'memc|motion[_ .-]*(compensation|smoothing)|frame[_ .-]*interpolat|"
        "video[_ .-]*(motion|enhance)|iris.*memc' | head -n 120",
        timeout_s=35,
    )
    brightness_result = adb_shell(
        adb,
        device,
        ["settings", "get", "system", "screen_brightness"],
        timeout_s=10,
    )
    display = parse_android_display_performance(display_result.stdout)
    surface = parse_android_surfaceflinger_performance(surface_result.stdout)
    last_known_foreground = collect_foreground_package(adb, device)
    foreground = last_known_foreground if screen_awake else None
    foreground_pid_result = (
        adb_shell(adb, device, ["pidof", last_known_foreground], timeout_s=10)
        if last_known_foreground
        else None
    )
    foreground_pid_value = (
        first_number(foreground_pid_result.stdout)
        if foreground_pid_result is not None and foreground_pid_result.ok
        else None
    )
    foreground_pid = int(foreground_pid_value) if foreground_pid_value is not None else None
    gfxinfo_result = (
        adb_shell(
            adb,
            device,
            ["dumpsys", "gfxinfo", last_known_foreground, "framestats"],
            timeout_s=30,
        )
        if include_frame_details and screen_awake and last_known_foreground
        else None
    )
    gfxinfo = (
        parse_android_gfxinfo(gfxinfo_result.stdout, last_known_foreground)
        if gfxinfo_result is not None
        else {}
    )
    layer = (
        parse_android_surface_layers(layer_result.stdout, last_known_foreground)
        if last_known_foreground
        else {}
    )
    latency: Dict[str, object] = {}
    latency_error: Optional[str] = None
    layer_name = layer.get("surface_layer_name")
    if (
        include_frame_rate
        and screen_awake
        and isinstance(layer_name, str)
        and layer_name
    ):
        latency, latency_error = collect_android_surface_latency(
            adb,
            device,
            layer_name,
        )
    window = parse_android_window_performance(window_result.stdout, last_known_foreground)
    render_resolution = parse_android_surface_render_resolution(
        surface_result.stdout,
        last_known_foreground,
        str(layer.get("surface_layer_name") or "") or None,
    )
    interpolation = parse_android_frame_interpolation(
        interpolation_result.stdout + "\n" + surface_result.stdout,
        last_known_foreground,
        foreground_pid,
    )
    touch = parse_android_touch_devices(touch_result.stdout)
    performance: Dict[str, object] = {
        **display,
        **surface,
        **window,
        **render_resolution,
        **interpolation,
        **gfxinfo,
        **layer,
    }
    performance["brightness_raw"] = first_number(brightness_result.stdout)
    performance["screen_state"] = screen_state
    performance["foreground_active"] = screen_awake and foreground is not None
    frame_probe_requested = include_frame_rate or include_frame_details
    if screen_awake:
        performance.setdefault("foreground_window_name", foreground)
        performance["refresh_rate_live_valid"] = isinstance(
            performance.get("refresh_rate_hz"), (int, float)
        )
    else:
        if last_known_foreground:
            performance["last_known_foreground_package"] = last_known_foreground
        last_window = performance.get("foreground_window_name")
        if isinstance(last_window, str) and last_window:
            performance["last_known_foreground_window_name"] = last_window
        last_refresh = performance.get("refresh_rate_hz")
        if isinstance(last_refresh, (int, float)):
            performance["last_reported_refresh_rate_hz"] = float(last_refresh)
        performance["foreground_window_name"] = None
        performance["refresh_rate_hz"] = None
        performance["refresh_rate_live_valid"] = False
        if frame_probe_requested:
            performance["frame_evidence_scope"] = "inactive screen; live frame probes skipped"
            performance["frame_unavailable_reason"] = (
                "屏幕未处于亮屏交互状态；已跳过 gfxinfo framestats 与 SurfaceFlinger "
                "应用层时间戳探测。保留显示模式仅用于确认接口能力，不代表当前 FPS 或刷新输出。"
            )
    if not frame_probe_requested:
        performance["frame_evidence_scope"] = "frame probes disabled by capture configuration"
        performance["frame_unavailable_reason"] = "本次采集配置未启用帧率或详细帧时间戳。"
    performance["platform"] = "android"
    performance["frame_data_available"] = bool(
        frame_probe_requested
        and screen_awake
        and (gfxinfo or latency.get("surface_latency_frame_count"))
    )
    performance["surface_latency_available"] = bool(
        latency.get("surface_latency_frame_count")
        or latency.get("surface_refresh_period_ns")
    )
    performance["touch_device_count"] = len(touch.get("devices", []))
    performance["touch_sampling_rate_available"] = False
    performance["touch_sampling_rate_reason"] = touch.get("sampling_rate_reason")
    warnings = []
    for label, result in (
        ("power", power_result),
        ("display", display_result),
        ("SurfaceFlinger", surface_result),
        ("SurfaceFlinger layer list", layer_result),
        ("window", window_result),
        ("input", touch_result),
    ):
        if not result.ok:
            warnings.append(
                f"Android {label} probe failed: "
                f"{(result.stderr or result.stdout).strip() or result.returncode}"
            )
    if gfxinfo_result is not None and not gfxinfo_result.ok:
        warnings.append(
            "Android gfxinfo probe failed: "
            f"{(gfxinfo_result.stderr or gfxinfo_result.stdout).strip() or gfxinfo_result.returncode}"
        )
    if latency_error:
        warnings.append(f"Android SurfaceFlinger latency probe failed: {latency_error}")
    return {
        "screen_state": screen_state,
        "foreground_package": foreground,
        "last_known_foreground_package": (
            last_known_foreground if not screen_awake else None
        ),
        "performance": performance,
        "touch": touch,
        "capabilities": {
            "display_modes": bool(display.get("supported_refresh_rates_hz")),
            "refresh_residency": bool(surface.get("refresh_rate_durations_s")),
            "gfxinfo_frame_counters": bool(gfxinfo),
            "surfaceflinger_frame_timestamps": bool(
                performance.get("surface_latency_available")
            ),
            "frame_rate": bool(
                gfxinfo or performance.get("surface_latency_available")
            ),
            "frame_probe_skipped_inactive_screen": bool(
                frame_probe_requested and not screen_awake
            ),
            "frame_probe_skipped_by_configuration": not frame_probe_requested,
            "render_resolution": bool(render_resolution.get("render_width_px")),
            "frame_interpolation_switch": bool(
                interpolation.get("frame_interpolation_evidence")
            ),
            "touch_devices": bool(touch.get("devices")),
            "touch_sampling_rate": False,
            "gpu_renderer": bool(surface.get("gpu_renderer")),
        },
        "warnings": warnings,
    }


def compact_android_performance_probe_for_metadata(
    performance: Dict[str, object],
) -> Dict[str, object]:
    """Keep probe summaries in metadata without duplicating raw frame history.

    The full awake-screen probe remains available in raw/android_performance_probe.txt.
    Metadata retains only small counters and an audit summary of any omitted bulk fields.
    """

    compact = dict(performance)
    record_count = 0
    histogram_bin_count = 0
    column_count = 0

    records = compact.pop("frame_records", None)
    if isinstance(records, list):
        record_count = len(records)
    histogram = compact.pop("frame_histogram_ms", None)
    if isinstance(histogram, dict):
        histogram_bin_count = len(histogram)
    columns = compact.pop("frame_stats_columns", None)
    if isinstance(columns, list):
        column_count = len(columns)

    for key in ("frame_intervals_ms", "frame_interval_values_ms"):
        compact.pop(key, None)

    if record_count or histogram_bin_count or column_count:
        compact["raw_frame_history_omitted_from_metadata"] = True
        compact["raw_frame_history_artifact"] = "raw/android_performance_probe.txt"
        compact["raw_frame_record_count"] = record_count
        compact["raw_frame_histogram_bin_count"] = histogram_bin_count
        compact["raw_frame_stats_column_count"] = column_count
    return compact


def _split_marked_sections(text: str) -> Tuple[Dict[str, str], Optional[int]]:
    sections: Dict[str, List[str]] = {}
    current = "preamble"
    thread_count: Optional[int] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {_PS_MARKER, _PROCESS_MARKER, _THREAD_MARKER, _POWER_STATE_MARKER}:
            current = stripped
            sections.setdefault(current, [])
            continue
        if stripped.startswith(_THREAD_COUNT_MARKER):
            value = first_number(stripped[len(_THREAD_COUNT_MARKER) :])
            thread_count = int(value) if value is not None else None
            continue
        sections.setdefault(current, []).append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}, thread_count


def collect_system_snapshot(
    adb: str,
    device: str,
    include_threads: bool,
) -> Tuple[Optional[SystemSnapshot], Optional[str]]:
    commands = [
        f"printf '{_PS_MARKER}\\n'",
        "ps -A -o PID,USER,PPID,NAME,ARGS 2>/dev/null",
        f"printf '{_PROCESS_MARKER}\\n'",
        (
            "top -b -q -n 1 -m 64 -s 9 "
            "-o PID,USER,PPID,PR,NI,PCY,RES,S,%CPU,%MEM,TIME+,CMDLINE 2>/dev/null"
        ),
        (
            f"printf '{_THREAD_COUNT_MARKER}'; "
            "ps -AT -o TID 2>/dev/null | tail -n +2 | wc -l"
        ),
    ]
    if include_threads:
        commands.extend(
            [
                f"printf '{_THREAD_MARKER}\\n'",
                (
                    "top -H -b -q -n 1 -m 40 -s 9 "
                    "-o PID,TID,USER,PR,NI,PCY,RES,S,%CPU,%MEM,TIME+,CMD,ARGS 2>/dev/null"
                ),
            ]
        )
    uptime_s, host_epoch_s, output, collection_ms, error = _timed_shell_output(
        adb,
        device,
        "; ".join(commands),
        timeout_s=12.0 if include_threads else 8.0,
    )
    if uptime_s is None:
        return None, error
    sections, thread_count = _split_marked_sections(output)
    ps_processes = parse_ps_processes(sections.get(_PS_MARKER, ""))
    processes = parse_top_processes(sections.get(_PROCESS_MARKER, ""))
    threads = parse_top_threads(sections.get(_THREAD_MARKER, "")) if include_threads else []
    watched = merge_watched_processes(ps_processes, processes)
    return (
        SystemSnapshot(
            uptime_s=uptime_s,
            host_epoch_s=host_epoch_s,
            processes=processes,
            threads=threads,
            watched_processes=watched,
            process_count=len(ps_processes) or None,
            thread_count=thread_count,
            collection_ms=collection_ms,
        ),
        error,
    )


def collect_thermal_snapshot(
    adb: str,
    device: str,
    include_display_brightness: bool = True,
) -> Tuple[Optional[ThermalSnapshot], Optional[str]]:
    uptime_s, host_epoch_s, output, collection_ms, error = _timed_shell_output(
        adb,
        device,
        "dumpsys thermalservice 2>/dev/null",
        timeout_s=8.0,
    )
    if uptime_s is None:
        return None, error
    parsed = parse_thermalservice(output)
    brightness_cooling_active = any(
        isinstance(item, dict)
        and re.search(r"lcd|backlight|display|screen", str(item.get("name") or ""), re.I)
        and float(item.get("value") or 0.0) > 0
        for item in parsed.get("cooling_devices", [])
        if isinstance(parsed.get("cooling_devices"), list)
    )
    display_brightness: Dict[str, object] = {}
    display_elapsed_ms = 0.0
    if include_display_brightness or brightness_cooling_active:
        display_script = (
            f"printf '{_BRIGHTNESS_SETTING_MARKER}'; "
            "settings get system screen_brightness 2>/dev/null; "
            f"printf '{_BRIGHTNESS_FLOAT_MARKER}'; "
            "settings get system screen_brightness_float 2>/dev/null; "
            f"printf '{_BRIGHTNESS_DISPLAY_MARKER}\\n'; "
            "dumpsys display 2>/dev/null"
        )
        display_result = adb_shell(adb, device, display_script, timeout_s=15.0)
        display_elapsed_ms = display_result.elapsed_s * 1000.0
        setting_text = ""
        setting_float_text = ""
        display_lines: List[str] = []
        in_display = False
        for line in display_result.stdout.splitlines():
            if line.startswith(_BRIGHTNESS_SETTING_MARKER):
                setting_text = line[len(_BRIGHTNESS_SETTING_MARKER) :]
            elif line.startswith(_BRIGHTNESS_FLOAT_MARKER):
                setting_float_text = line[len(_BRIGHTNESS_FLOAT_MARKER) :]
            elif line.strip() == _BRIGHTNESS_DISPLAY_MARKER:
                in_display = True
            elif in_display:
                display_lines.append(line)
        if display_result.ok:
            display_brightness = parse_display_brightness_state(
                "\n".join(display_lines),
                setting_text,
                setting_float_text,
            )
            display_brightness["probe_elapsed_ms"] = display_elapsed_ms
        else:
            display_brightness = {
                "available": False,
                "error": (
                    display_result.stderr.strip()
                    or display_result.stdout.strip()
                    or "dumpsys display brightness probe failed"
                ),
                "probe_elapsed_ms": display_elapsed_ms,
            }
    return (
        ThermalSnapshot(
            uptime_s=uptime_s,
            host_epoch_s=host_epoch_s,
            status=parsed.get("status") if isinstance(parsed.get("status"), int) else None,
            hal_ready=parsed.get("hal_ready") if isinstance(parsed.get("hal_ready"), bool) else None,
            temperatures=list(parsed.get("temperatures", [])),
            cooling_devices=list(parsed.get("cooling_devices", [])),
            thresholds=list(parsed.get("thresholds", [])),
            headroom_thresholds=list(parsed.get("headroom_thresholds", [])),
            display_brightness=display_brightness,
            collection_ms=collection_ms + display_elapsed_ms,
        ),
        error,
    )


def _brightness_tracking_required(snapshot: ThermalSnapshot) -> bool:
    relevant_cooling = any(
        re.search(
            r"lcd|backlight|display|screen",
            str(item.get("name") or ""),
            re.I,
        )
        and float(item.get("value") or 0.0) > 0
        for item in snapshot.cooling_devices
        if isinstance(item, dict)
    )
    brightness = snapshot.display_brightness
    framework_clamp = bool(brightness.get("thermal_applied")) or (
        isinstance(brightness.get("thermal_cap"), (int, float))
        and float(brightness["thermal_cap"]) < 0.999
        and int(brightness.get("thermal_status") or 0) > 0
    )
    # OEM tables only describe candidate limits.  Continue high-rate tracking
    # only when the vendor runtime explicitly reports that its clamp is active.
    vendor_clamp = brightness.get("vendor_thermal_active") is True
    return bool(relevant_cooling or framework_clamp or vendor_clamp)


def _scheduler_state_script() -> str:
    lines = [
        "for app_group in foreground background system-background restricted top-app; do",
        "app_value=$(cat /dev/cpuset/$app_group/cpus 2>/dev/null)",
        "if [ -n \"$app_value\" ]; then app_status=ok; else app_status=unavailable; fi",
        "printf 'CPUSET|%s|%s|%s\\n' \"$app_group\" \"$app_value\" \"$app_status\"",
        "done",
        "for app_policy in /sys/devices/system/cpu/cpufreq/policy*; do",
        "[ -d \"$app_policy\" ] || continue",
        "app_name=${app_policy##*/}",
        "app_gov=$(cat $app_policy/scaling_governor 2>/dev/null)",
        "app_min=$(cat $app_policy/scaling_min_freq 2>/dev/null)",
        "app_max=$(cat $app_policy/scaling_max_freq 2>/dev/null)",
        "app_hw_min=$(cat $app_policy/cpuinfo_min_freq 2>/dev/null)",
        "app_hw_max=$(cat $app_policy/cpuinfo_max_freq 2>/dev/null)",
        "app_related=$(cat $app_policy/affected_cpus 2>/dev/null)",
        "set -- $app_related; app_first_cpu=$1",
        "app_core_root=/sys/devices/system/cpu/cpu$app_first_cpu/core_ctl",
        "app_core_enable=$(cat $app_core_root/enable 2>/dev/null)",
        "app_core_min=$(cat $app_core_root/min_cpus 2>/dev/null)",
        "app_core_max=$(cat $app_core_root/max_cpus 2>/dev/null)",
        "if [ -n \"$app_gov$app_min$app_max\" ]; then app_status=ok; elif [ -n \"$app_hw_min$app_hw_max\" ]; then app_status=limits-only; else app_status=unavailable; fi",
        "printf 'POLICY|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \"$app_name\" \"$app_gov\" \"$app_min\" \"$app_max\" \"$app_hw_min\" \"$app_hw_max\" \"$app_status\" \"$app_related\" \"$app_core_enable\" \"$app_core_min\" \"$app_core_max\"",
        "done",
        f"printf '{_POWER_STATE_MARKER}\\n'",
        "dumpsys power 2>/dev/null",
    ]
    return "\n".join(lines)


def collect_scheduler_snapshot(
    adb: str,
    device: str,
    include_pids: Optional[set[int]] = None,
) -> Tuple[Optional[SchedulerSnapshot], List[str]]:
    warnings: List[str] = []
    uptime_s, host_epoch_s, output, collection_ms, error = _timed_shell_output(
        adb,
        device,
        _scheduler_state_script(),
        timeout_s=12.0,
    )
    if error:
        warnings.append(f"调度状态：{error}")
    sections, _ = _split_marked_sections(output)
    parsed_state = parse_cpuset_policy_state(sections.get("preamble", ""))
    power_state = parse_power_scheduler_state(sections.get(_POWER_STATE_MARKER, ""))

    hint_up, _, hint_output, hint_ms, hint_error = _timed_shell_output(
        adb,
        device,
        "dumpsys performance_hint 2>/dev/null",
        timeout_s=10.0,
    )
    if hint_error:
        warnings.append(f"ADPF performance_hint：{hint_error}")
    hint = parse_performance_hint(hint_output)

    activity_up, _, activity_output, activity_ms, activity_error = _timed_shell_output(
        adb,
        device,
        "dumpsys activity processes 2>/dev/null",
        timeout_s=15.0,
    )
    if activity_error:
        warnings.append(f"ActivityManager 进程状态：{activity_error}")
    process_states = parse_activity_processes(activity_output, include_pids)
    selected_uptime = uptime_s if uptime_s is not None else hint_up if hint_up is not None else activity_up
    if selected_uptime is None:
        return None, warnings
    availability = dict(parsed_state.get("availability", {}))
    availability.update(
        {
            "adpf_hint_sessions": hint_error is None,
            "hint_session_supported": hint.get("hint_session_supported"),
            "hint_session_preferred_rate_ns": hint.get("preferred_rate_ns"),
            "cpu_headroom_supported": hint.get("cpu_headroom_supported"),
            "gpu_headroom_supported": hint.get("gpu_headroom_supported"),
            "activity_process_state": activity_error is None,
            "power_state": bool(power_state),
        }
    )
    return (
        SchedulerSnapshot(
            uptime_s=selected_uptime,
            host_epoch_s=host_epoch_s,
            cpusets=dict(parsed_state.get("cpusets", {})),
            cpu_policies=list(parsed_state.get("cpu_policies", [])),
            hint_sessions=list(hint.get("sessions", [])),
            watched_processes=process_states,
            power_hal=power_state,
            availability=availability,
            collection_ms=collection_ms + hint_ms + activity_ms,
        ),
        warnings,
    )


class AndroidPerformanceContextWorker:
    """Collect Android display and frame counters outside the high-rate power sampler."""

    _SURFACE_LATENCY_INTERVAL_S = 0.5

    def __init__(
        self,
        adb: str,
        device: str,
        journal: RunJournal,
        contexts: List[ContextSample],
        interval_s: float = 10.0,
        surface_interval_s: float = 30.0,
        include_foreground: bool = True,
        include_frame_rate: bool = True,
        include_window: bool = True,
        include_frame_details: bool = False,
    ) -> None:
        self.adb = adb
        self.device = device
        self.journal = journal
        self.contexts = contexts
        self.interval_s = max(1.0, interval_s)
        self.surface_interval_s = max(self.interval_s, surface_interval_s)
        self.include_foreground = include_foreground
        self.include_frame_rate = include_frame_rate
        self.include_window = include_window
        self.include_frame_details = include_frame_details
        self.warnings: List[str] = []
        self._warning_keys: set[str] = set()
        self._surface_state: Dict[str, object] = {}
        self._last_surface_monotonic: Optional[float] = None
        self._last_context_monotonic: Optional[float] = None
        self._surface_layer_name: Optional[str] = None
        self._surface_layer_package: Optional[str] = None
        self._surface_last_timestamp_ns: Optional[int] = None
        self._surface_refresh_period_ns: Optional[int] = None
        self._surface_intervals_ms: List[float] = []
        self._stop_event = threading.Event()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="android-performance-context-monitor",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warning_keys:
            return
        self._warning_keys.add(key)
        self.warnings.append(message)

    def _reset_surface_latency(
        self,
        layer_name: Optional[str],
        foreground_package: Optional[str],
    ) -> None:
        self._surface_layer_name = layer_name
        self._surface_layer_package = foreground_package
        self._surface_last_timestamp_ns = None
        self._surface_refresh_period_ns = None
        self._surface_intervals_ms.clear()

    def _sample_surface_latency(self) -> None:
        layer_name = self._surface_layer_name
        if not self.include_frame_rate or not layer_name:
            return
        parsed, error = collect_android_surface_latency(
            self.adb,
            self.device,
            layer_name,
        )
        if error:
            self._warn_once(
                f"surface-latency:{layer_name}",
                f"Android SurfaceFlinger frame timestamps: {error}",
            )
            return
        refresh_period = parsed.get("surface_refresh_period_ns")
        if isinstance(refresh_period, (int, float)) and int(refresh_period) > 0:
            self._surface_refresh_period_ns = int(refresh_period)
        raw_timestamps = parsed.get("surface_frame_timestamps_ns")
        if not isinstance(raw_timestamps, list):
            return
        timestamps = sorted(
            {
                int(value)
                for value in raw_timestamps
                if isinstance(value, (int, float)) and int(value) > 0
            }
        )
        if not timestamps:
            return
        previous = self._surface_last_timestamp_ns
        latest = timestamps[-1]
        if previous is None or latest < previous:
            self._surface_last_timestamp_ns = latest
            return
        new_timestamps = [value for value in timestamps if value > previous]
        if not new_timestamps:
            return

        # A full SurfaceFlinger ring means the previous endpoint may already have
        # fallen out. In that case retain intervals inside the current ring but do
        # not invent one large gap across potentially dropped records.
        cursor: Optional[int] = previous if previous >= timestamps[0] else None
        for timestamp in new_timestamps:
            if cursor is not None:
                interval_ms = (timestamp - cursor) / 1_000_000.0
                if 0.1 <= interval_ms <= 2000.0:
                    self._surface_intervals_ms.append(interval_ms)
            cursor = timestamp
        self._surface_last_timestamp_ns = latest

    def _drain_surface_metrics(
        self,
        refresh_rate_hz: Optional[float],
    ) -> Dict[str, object]:
        intervals = list(self._surface_intervals_ms)
        self._surface_intervals_ms.clear()
        effective_refresh = refresh_rate_hz
        if (
            not isinstance(effective_refresh, (int, float))
            or float(effective_refresh) <= 0
        ) and isinstance(self._surface_refresh_period_ns, int):
            effective_refresh = 1_000_000_000.0 / self._surface_refresh_period_ns
        return android_surface_frame_metrics(intervals, effective_refresh)

    def _collect(self, include_surface_flinger: bool) -> None:
        surface_timestamps_confirmed = (
            self._surface_layer_name is not None
            and self._surface_last_timestamp_ns is not None
        )
        context, surface_update, error = collect_android_performance_context(
            self.adb,
            self.device,
            include_surface_flinger=include_surface_flinger,
            include_foreground=self.include_foreground,
            include_frame_rate=self.include_frame_rate,
            include_window=self.include_window,
            include_frame_details=self.include_frame_details,
            include_gfxinfo_summary=(
                self.include_frame_details or not surface_timestamps_confirmed
            ),
            cached_surface=self._surface_state,
        )
        if surface_update:
            self._surface_state.update(surface_update)
        if include_surface_flinger:
            self._last_surface_monotonic = time.monotonic()
        if context is not None:
            screen_state = str(context.screen_state or "").strip().lower()
            screen_awake = not screen_state or screen_state in {"awake", "on"}
            layer_name = context.performance.get("surface_layer_name")
            layer_name = layer_name if isinstance(layer_name, str) and layer_name else None
            layer_changed = (
                layer_name != self._surface_layer_name
                or context.foreground_package != self._surface_layer_package
            )
            if not screen_awake or layer_name is None:
                if self._surface_layer_name is not None:
                    self._reset_surface_latency(None, None)
            elif layer_changed:
                self._reset_surface_latency(layer_name, context.foreground_package)
                self._sample_surface_latency()
            else:
                frame_metrics = self._drain_surface_metrics(context.refresh_rate_hz)
                if frame_metrics:
                    context.performance.update(frame_metrics)
                    context.performance["frame_data_available"] = True
                    context.performance.pop("frame_unavailable_reason", None)
            self._last_context_monotonic = time.monotonic()
            self.contexts.append(context)
            self.journal.append_context(context)
        if error:
            self._warn_once("context", f"Android performance context: {error}")

    def _run(self) -> None:
        next_context = time.monotonic()
        next_surface = next_context
        next_surface_latency = next_context if self.include_frame_rate else math.inf
        while not self._stop_event.is_set():
            now = time.monotonic()
            if self.include_frame_rate and now >= next_surface_latency:
                try:
                    self._sample_surface_latency()
                except Exception as exc:
                    self._warn_once(
                        f"surface-worker:{type(exc).__name__}",
                        "Android SurfaceFlinger timestamp sampler recovered from "
                        f"{type(exc).__name__}: {exc}",
                    )
                completed = time.monotonic()
                while next_surface_latency <= completed:
                    next_surface_latency += self._SURFACE_LATENCY_INTERVAL_S
            if now >= next_context:
                include_surface = now >= next_surface
                try:
                    self._collect(include_surface)
                except Exception as exc:
                    self._warn_once(
                        f"worker:{type(exc).__name__}",
                        f"Android performance context recovered from {type(exc).__name__}: {exc}",
                    )
                completed = time.monotonic()
                while next_context <= completed:
                    next_context += self.interval_s
                if include_surface:
                    while next_surface <= completed:
                        next_surface += self.surface_interval_s
            next_deadline = min(next_context, next_surface_latency)
            self._stop_event.wait(
                max(0.05, min(0.5, next_deadline - time.monotonic()))
            )

    def stop(self) -> None:
        if self._stopped:
            return
        self._stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=30.0)
        if self._thread.is_alive():
            self._warn_once(
                "stop_timeout",
                "Android performance context worker did not stop before the command timeout.",
            )
            self._stopped = True
            return
        now = time.monotonic()
        surface_age = (
            now - self._last_surface_monotonic
            if self._last_surface_monotonic is not None
            else math.inf
        )
        context_age = (
            now - self._last_context_monotonic
            if self._last_context_monotonic is not None
            else math.inf
        )
        if _device_ready(self.adb, self.device) and (
            context_age >= 0.25 or self._surface_intervals_ms
        ):
            try:
                self._sample_surface_latency()
                self._collect(surface_age >= 1.0)
            except Exception as exc:
                self._warn_once(
                    f"final:{type(exc).__name__}",
                    f"Final Android performance context failed: {exc}",
                )
        self._stopped = True


class SystemMonitorWorker:
    """Low-frequency whole-system snapshots isolated from the battery sampler."""

    def __init__(
        self,
        adb: str,
        device: str,
        journal: RunJournal,
        process_interval_s: float = 10.0,
        thread_interval_s: float = 30.0,
        thermal_interval_s: float = 10.0,
        scheduler_interval_s: float = 30.0,
        process_enabled: bool = True,
        thread_enabled: bool = True,
        thermal_enabled: bool = True,
        scheduler_enabled: bool = True,
    ) -> None:
        self.adb = adb
        self.device = device
        self.journal = journal
        self.process_interval_s = max(2.0, process_interval_s)
        self.thread_interval_s = max(5.0, thread_interval_s)
        self.thermal_interval_s = max(2.0, thermal_interval_s)
        self.scheduler_interval_s = max(5.0, scheduler_interval_s)
        self.process_enabled = process_enabled
        self.thread_enabled = thread_enabled
        self.thermal_enabled = thermal_enabled
        self.scheduler_enabled = scheduler_enabled
        self.system_snapshot_count = 0
        self.thermal_snapshot_count = 0
        self.scheduler_snapshot_count = 0
        self.warnings: List[str] = []
        self.latest_system: Optional[SystemSnapshot] = None
        self._warning_keys: set[str] = set()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="mobile-profiler-system-monitor",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=20.0)
        if self._thread.is_alive():
            self._warn_once("stop_timeout", "全系统监控线程未能在命令超时前停止。")

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warning_keys:
            return
        self._warning_keys.add(key)
        self.warnings.append(message)

    @staticmethod
    def _advance_due(due: float, interval: float, now: float) -> float:
        while due <= now:
            due += interval
        return due

    def _run(self) -> None:
        now = time.monotonic()
        next_process = now if self.process_enabled else math.inf
        next_thread = now if self.thread_enabled else math.inf
        next_thermal = now if self.thermal_enabled else math.inf
        next_brightness_probe = now if self.thermal_enabled else math.inf
        brightness_tracking_active = False
        next_scheduler = now if self.scheduler_enabled else math.inf
        while not self._stop_event.is_set():
            now = time.monotonic()
            try:
                if (self.process_enabled and now >= next_process) or (
                    self.thread_enabled and now >= next_thread
                ):
                    process_due = now >= next_process
                    include_threads = self.thread_enabled and now >= next_thread
                    snapshot, error = collect_system_snapshot(
                        self.adb,
                        self.device,
                        include_threads,
                    )
                    if snapshot is not None:
                        self.journal.append_system_snapshot(snapshot)
                        self.latest_system = snapshot
                        self.system_snapshot_count += 1
                    if error:
                        self._warn_once("system", f"系统进程监控：{error}")
                    completed = time.monotonic()
                    if process_due and self.process_enabled:
                        next_process = self._advance_due(
                            next_process,
                            self.process_interval_s,
                            completed,
                        )
                    if include_threads and self.thread_enabled:
                        next_thread = self._advance_due(next_thread, self.thread_interval_s, completed)

                now = time.monotonic()
                if (
                    self.thermal_enabled
                    and now >= next_thermal
                    and not self._stop_event.is_set()
                ):
                    include_brightness = brightness_tracking_active or now >= next_brightness_probe
                    snapshot, error = collect_thermal_snapshot(
                        self.adb,
                        self.device,
                        include_display_brightness=include_brightness,
                    )
                    if snapshot is not None:
                        self.journal.append_thermal_snapshot(snapshot)
                        self.thermal_snapshot_count += 1
                        was_tracking = brightness_tracking_active
                        brightness_tracking_active = _brightness_tracking_required(snapshot)
                        if brightness_tracking_active or was_tracking:
                            next_brightness_probe = time.monotonic() + self.thermal_interval_s
                        elif snapshot.display_brightness:
                            next_brightness_probe = time.monotonic() + max(
                                30.0,
                                self.thermal_interval_s * 3.0,
                            )
                    if error:
                        self._warn_once("thermal", f"热状态监控：{error}")
                    next_thermal = self._advance_due(
                        next_thermal,
                        self.thermal_interval_s,
                        time.monotonic(),
                    )

                now = time.monotonic()
                if (
                    self.scheduler_enabled
                    and now >= next_scheduler
                    and not self._stop_event.is_set()
                ):
                    include_pids: set[int] = set()
                    if self.latest_system is not None:
                        include_pids.update(
                            int(item["pid"])
                            for item in self.latest_system.processes
                            if isinstance(item.get("pid"), int)
                        )
                        include_pids.update(
                            int(item["pid"])
                            for item in self.latest_system.watched_processes
                            if isinstance(item.get("pid"), int)
                        )
                    snapshot, warnings = collect_scheduler_snapshot(
                        self.adb,
                        self.device,
                        include_pids or None,
                    )
                    if snapshot is not None:
                        self.journal.append_scheduler_snapshot(snapshot)
                        self.scheduler_snapshot_count += 1
                    for index, warning in enumerate(warnings):
                        self._warn_once(f"scheduler:{index}:{warning.split(':', 1)[0]}", warning)
                    next_scheduler = self._advance_due(
                        next_scheduler,
                        self.scheduler_interval_s,
                        time.monotonic(),
                    )
            except Exception as exc:
                self._warn_once(
                    f"worker:{type(exc).__name__}",
                    f"全系统监控已从 {type(exc).__name__} 异常中恢复：{exc}",
                )

            next_due = min(next_process, next_thread, next_thermal, next_scheduler)
            self._stop_event.wait(max(0.05, min(1.0, next_due - time.monotonic())))

    def checkpoint_state(self) -> Dict[str, object]:
        return {
            "system_snapshot_count": self.system_snapshot_count,
            "thermal_snapshot_count": self.thermal_snapshot_count,
            "scheduler_snapshot_count": self.scheduler_snapshot_count,
            "system_monitor_warnings": list(self.warnings),
        }


def _copy_monitor_state(
    result: StreamCollectionResult,
    monitor: Optional[SystemMonitorWorker],
) -> None:
    if monitor is None:
        return
    result.system_snapshot_count = monitor.system_snapshot_count
    result.thermal_snapshot_count = monitor.thermal_snapshot_count
    result.scheduler_snapshot_count = monitor.scheduler_snapshot_count
    for warning in monitor.warnings:
        if warning not in result.warnings:
            result.warnings.append(warning)


def _copy_performance_monitor_state(
    result: StreamCollectionResult,
    monitor: Optional[AndroidPerformanceContextWorker],
) -> None:
    if monitor is None:
        return
    for warning in monitor.warnings:
        if warning not in result.warnings:
            result.warnings.append(warning)


def _checkpoint_state(
    result: StreamCollectionResult,
    status: str,
    requested_duration_s: float,
    host_start_monotonic: float,
    monitor: Optional[SystemMonitorWorker] = None,
) -> Dict[str, object]:
    state = {
        "status": status,
        "requested_duration_s": requested_duration_s,
        "host_elapsed_s": time.monotonic() - host_start_monotonic,
        "sample_count": len(result.raw_samples),
        "context_count": len(result.contexts),
        "clock_sync_count": len(result.clock_sync),
        "reconnect_count": result.reconnect_count,
        "sampler_launch_count": result.sampler_launch_count,
        "last_device_uptime_s": (
            result.raw_samples[-1].uptime_s if result.raw_samples else None
        ),
        "stop_reason": result.stop_reason,
    }
    if monitor is not None:
        state.update(monitor.checkpoint_state())
    return state


def collect_streaming_session(
    adb: str,
    device: str,
    duration_s: int,
    interval_s: float,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    journal: RunJournal,
    memory_source: Optional[MemorySource] = None,
    checkpoint_interval_s: float = 30.0,
    reconnect_timeout_s: float = 120.0,
    system_monitor_enabled: bool = False,
    process_interval_s: float = 10.0,
    thread_interval_s: float = 30.0,
    thermal_interval_s: float = 10.0,
    scheduler_interval_s: float = 30.0,
    performance_context_enabled: bool = True,
    performance_context_interval_s: float = 10.0,
    performance_surface_interval_s: float = 30.0,
    performance_foreground_enabled: bool = True,
    performance_frame_rate_enabled: bool = True,
    performance_window_enabled: bool = False,
    performance_frame_details_enabled: bool = False,
    process_snapshots_enabled: bool = True,
    hot_threads_enabled: bool = True,
    thermal_snapshots_enabled: bool = True,
    scheduler_snapshots_enabled: bool = True,
) -> StreamCollectionResult:
    """Stream a wall-clock session while preserving every complete line on disk."""

    result = StreamCollectionResult()
    use_root_sampler_session = bool(gpu_source is not None and gpu_source.requires_root)
    host_start = time.monotonic()
    deadline = host_start + duration_s
    next_checkpoint = host_start + checkpoint_interval_s
    fatal_stop = False
    monitor: Optional[SystemMonitorWorker] = None
    performance_monitor: Optional[AndroidPerformanceContextWorker] = None
    if performance_context_enabled:
        performance_monitor = AndroidPerformanceContextWorker(
            adb,
            device,
            journal,
            result.contexts,
            interval_s=performance_context_interval_s,
            surface_interval_s=performance_surface_interval_s,
            include_foreground=performance_foreground_enabled,
            include_frame_rate=performance_frame_rate_enabled,
            include_window=performance_window_enabled,
            include_frame_details=performance_frame_details_enabled,
        )
        performance_monitor.start()
    if system_monitor_enabled:
        monitor = SystemMonitorWorker(
            adb,
            device,
            journal,
            process_interval_s=process_interval_s,
            thread_interval_s=thread_interval_s,
            thermal_interval_s=thermal_interval_s,
            scheduler_interval_s=scheduler_interval_s,
            process_enabled=process_snapshots_enabled,
            thread_enabled=hot_threads_enabled,
            thermal_enabled=thermal_snapshots_enabled,
            scheduler_enabled=scheduler_snapshots_enabled,
        )
        monitor.start()
    journal.checkpoint(
        _checkpoint_state(result, "collecting", duration_s, host_start, monitor)
    )

    try:
        while time.monotonic() < deadline and not fatal_stop:
            if not _wait_for_device(adb, device, deadline, reconnect_timeout_s):
                result.stop_reason = "reconnect_timeout"
                result.warnings.append(
                    "ADB 未能在采集窗口结束前重连，已保留部分数据。"
                )
                break

            sync = collect_clock_sync(adb, device)
            if sync is not None:
                result.clock_sync.append(sync)
                journal.append_clock_sync(sync)

            remaining_s = max(2, int(math.ceil(deadline - time.monotonic())))
            script = build_sampler_script(
                remaining_s,
                interval_s,
                policies,
                gpu_source,
                memory_source,
                emit_context_samples=not performance_context_enabled,
                session_has_root=use_root_sampler_session,
            )
            remote_command = _sampler_shell_command(script, use_root_sampler_session)
            argv = adb_prefix(adb, device) + ["shell", remote_command]
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            result.sampler_launch_count += 1
            messages: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue()
            if process.stdout is None or process.stderr is None:
                _stop_process(process)
                raise RuntimeError("ADB sampler pipes were not created")
            stdout_thread = threading.Thread(
                target=_read_process_stream,
                args=(process.stdout, "stdout", messages),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_read_process_stream,
                args=(process.stderr, "stderr", messages),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            ended_streams = set()

            try:
                while time.monotonic() < deadline and len(ended_streams) < 2:
                    try:
                        stream_name, line = messages.get(timeout=0.25)
                    except queue.Empty:
                        stream_name = ""
                        line = ""
                    if line is None:
                        ended_streams.add(stream_name)
                    elif line:
                        if stream_name == "stderr":
                            journal.append_stderr_line(line)
                        else:
                            journal.append_sampler_line(line)
                            parsed = parse_sampler_line(
                                line,
                                policies,
                                gpu_source,
                                memory_source,
                            )
                            if isinstance(parsed, RawSample):
                                if result.raw_samples and parsed.uptime_s < (
                                    result.raw_samples[-1].uptime_s - 1.0
                                ):
                                    result.stop_reason = "device_rebooted"
                                    result.warnings.append(
                                        "设备 uptime 倒退，表明设备可能已重启；采集已停止。"
                                    )
                                    fatal_stop = True
                                    break
                                if result.raw_samples and parsed.uptime_s <= result.raw_samples[-1].uptime_s:
                                    continue
                                parsed.index = len(result.raw_samples)
                                result.raw_samples.append(parsed)
                            elif isinstance(parsed, ContextSample):
                                result.contexts.append(parsed)
                                journal.append_context(parsed)

                    if time.monotonic() >= next_checkpoint:
                        point = collect_clock_sync(adb, device)
                        if point is not None:
                            result.clock_sync.append(point)
                            journal.append_clock_sync(point)
                        journal.checkpoint(
                            _checkpoint_state(
                                result,
                                "collecting",
                                duration_s,
                                host_start,
                                monitor,
                            )
                        )
                        while next_checkpoint <= time.monotonic():
                            next_checkpoint += checkpoint_interval_s
            finally:
                _stop_process(process)
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)

            if fatal_stop or time.monotonic() >= deadline:
                break
            result.reconnect_count += 1
            result.stop_reason = "adb_disconnected"
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

        if monitor is not None:
            monitor.stop()
            _copy_monitor_state(result, monitor)
        if performance_monitor is not None:
            performance_monitor.stop()
            _copy_performance_monitor_state(result, performance_monitor)
        if _device_ready(adb, device):
            final_sync = collect_clock_sync(adb, device)
            if final_sync is not None:
                result.clock_sync.append(final_sync)
                journal.append_clock_sync(final_sync)
        result.contexts.sort(key=lambda item: item.uptime_s)
        if result.stop_reason in {"adb_disconnected", "completed"}:
            result.stop_reason = "completed"
        result.host_elapsed_s = time.monotonic() - host_start
        journal.checkpoint(
            _checkpoint_state(
                result,
                "collected" if len(result.raw_samples) >= 2 else "failed",
                duration_s,
                host_start,
                monitor,
            )
        )
        return result
    except KeyboardInterrupt:
        if monitor is not None:
            monitor.stop()
            _copy_monitor_state(result, monitor)
        if performance_monitor is not None:
            performance_monitor.stop()
            _copy_performance_monitor_state(result, performance_monitor)
        result.stop_reason = "interrupted"
        result.host_elapsed_s = time.monotonic() - host_start
        journal.checkpoint(
            _checkpoint_state(result, "interrupted", duration_s, host_start, monitor)
        )
        raise
    except Exception:
        if monitor is not None:
            monitor.stop()
            _copy_monitor_state(result, monitor)
        if performance_monitor is not None:
            performance_monitor.stop()
            _copy_performance_monitor_state(result, performance_monitor)
        result.stop_reason = "collector_error"
        result.host_elapsed_s = time.monotonic() - host_start
        journal.checkpoint(
            _checkpoint_state(result, "failed", duration_s, host_start, monitor)
        )
        raise
    finally:
        if monitor is not None:
            monitor.stop()
            _copy_monitor_state(result, monitor)
        if performance_monitor is not None:
            performance_monitor.stop()
            _copy_performance_monitor_state(result, performance_monitor)


def collect_text(
    adb: str,
    device: str,
    args: Sequence[str],
    timeout_s: float = 45.0,
) -> str:
    result = adb_shell(adb, device, list(args), timeout_s=timeout_s)
    if result.ok:
        return result.stdout
    return (result.stdout + "\n" + result.stderr).strip()


def collect_post_run_outputs(
    adb: str,
    device: str,
    test_mode: str = "power",
    capture_features: Optional[Dict[str, bool]] = None,
) -> Dict[str, str]:
    features = capture_features or {}

    def enabled(name: str, default: bool = True) -> bool:
        return bool(features.get(name, default))

    outputs: Dict[str, str] = {}
    commands: List[Tuple[str, List[str], float]] = [
        ("battery_end", ["dumpsys", "battery"], 15),
    ]
    if enabled("target_process") or enabled("process_snapshots"):
        commands.append(("cpuinfo", ["dumpsys", "cpuinfo"], 45))
    if enabled("thermal"):
        commands.append(("thermalservice", ["dumpsys", "thermalservice"], 30))
    if enabled("foreground_window") or enabled("frame_rate"):
        commands.extend(
            [
                ("display", ["dumpsys", "display"], 45),
                ("power", ["dumpsys", "power"], 30),
            ]
        )
    if test_mode == "power" and enabled("power_attribution"):
        commands.extend(
            [
                ("batterystats_usage", ["dumpsys", "batterystats", "--usage"], 60),
                *(([("gpu_end", ["dumpsys", "gpu"], 45)]) if enabled("gpu_metrics") else []),
                ("batterystats_checkin", ["dumpsys", "batterystats", "-c"], 60),
                ("batterystats", ["dumpsys", "batterystats"], 90),
                ("batterystats_wakeups", ["dumpsys", "batterystats", "--wakeups"], 45),
                ("power_profile", ["dumpsys", "batterystats", "--power-profile"], 45),
                ("packages", ["cmd", "package", "list", "packages", "-U"], 30),
            ]
        )
    for key, args, timeout_s in commands:
        outputs[key] = collect_text(adb, device, args, timeout_s=timeout_s)
    if enabled("foreground_window") or enabled("frame_rate"):
        outputs["screen_brightness"] = collect_text(
            adb, device, ["settings", "get", "system", "screen_brightness"], timeout_s=10
        ).strip()
        outputs["peak_refresh_rate"] = collect_text(
            adb, device, ["settings", "get", "system", "peak_refresh_rate"], timeout_s=10
        ).strip()
    if enabled("runtime_settings"):
        outputs["runtime_settings_end"] = collect_android_runtime_settings_text(adb, device)
    return outputs
