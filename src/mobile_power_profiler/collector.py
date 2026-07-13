from __future__ import annotations

import json
import math
import queue
import re
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


def is_gpu_core_devfreq(identity: str) -> bool:
    value = identity.lower()
    if any(token in value for token in ("gpubw", "gpu-bw", "busmon", "memlat", "latfloor")):
        return False
    return any(
        token in value
        for token in ("kgsl-3d", "qcom,kgsl", "mali", "gpu0", "gpu@", "graphics")
    )


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
    kgsl_model = _read_text(adb, device, "/sys/class/kgsl/kgsl-3d0/gpu_model")
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
    for path in candidates:
        value, status = _readable_number(adb, device, path)
        attempts.append({"path": path, "status": status})
        if value is not None:
            frequency_path = path
            initial_raw = value
            break

    load_path: Optional[str] = None
    load_format = "percentage"
    initial_load_pct: Optional[float] = None
    load_attempts: List[Dict[str, str]] = []
    load_candidates = [
        ("/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage", "percentage"),
        ("/sys/class/kgsl/kgsl-3d0/gpubusy", "busy_total"),
        ("/sys/kernel/ged/hal/gpu_utilization", "percentage"),
    ]
    for path, candidate_format in load_candidates:
        value, status = _readable_gpu_load(adb, device, path, candidate_format)
        load_attempts.append({"path": path, "format": candidate_format, "status": status})
        if value is not None:
            load_path = path
            load_format = candidate_format
            initial_load_pct = value
            break

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
    min_raw = _read_number(adb, device, f"{parent}/min_freq") if parent else None
    max_raw = _read_number(adb, device, f"{parent}/max_freq") if parent else None
    available_raw = _read_numbers(adb, device, f"{parent}/available_frequencies") if parent else []
    if is_qualcomm or kgsl_model:
        kgsl_root = "/sys/class/kgsl/kgsl-3d0"
        if min_raw is None:
            min_raw = _read_number(adb, device, f"{kgsl_root}/min_gpuclk")
        if max_raw is None:
            max_raw = _read_number(adb, device, f"{kgsl_root}/max_gpuclk")
        if not available_raw:
            available_raw = _read_numbers(adb, device, f"{kgsl_root}/gpu_available_frequencies")
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
        r"mResumedActivity:.*?\s([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
        r"topResumedActivity=.*?\s([A-Za-z0-9_.$]+)/(?:[A-Za-z0-9_.$]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, result.stdout)
        if match:
            return match.group(1)
    return None


def build_sampler_script(
    duration_s: int,
    interval_s: float,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    voltage_period_s: float = 5.0,
    context_period_s: float = 10.0,
    refresh_period_s: float = 30.0,
) -> str:
    core_ids = sorted({core for policy in policies for core in policy.cores})
    voltage_every = max(1, int(math.ceil(voltage_period_s / interval_s)))
    temperature_every = max(1, int(math.ceil(context_period_s / interval_s)))
    context_every = max(1, int(math.ceil(context_period_s / interval_s)))
    refresh_every = max(context_every, int(math.ceil(refresh_period_s / interval_s)))
    lines = [
        "emit_context() {",
        "ctx_up=$1",
        "ctx_refresh=$2",
        "component=$(dumpsys activity activities 2>/dev/null | awk '/mResumedActivity|topResumedActivity|mFocusedApp|mLastPausedActivity/ { for (n=1; n<=NF; n++) if ($n ~ /\\//) { print $n; exit } }' | tr -d '},')",
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
    ]
    cpu_names = ["u", "n", "s", "idle", "io", "irq", "sirq", "steal"]
    body = [
        "cur=$(cmd battery get -f current_now 2>/dev/null)",
        "set -- $cur; cur=$1",
        "[ -n \"$cur\" ] || cur=0",
        f"if [ $((i % {voltage_every})) -eq 0 ]; then volt=$(dumpsys battery 2>/dev/null | sed -n 's/^  voltage: *//p' | head -n 1); [ -n \"$volt\" ] || volt=-1; fi",
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
                    f"gpu_f=$(cat {gpu_source.frequency_path} 2>/dev/null)",
                    "set -- $gpu_f; gpu_f=$1; [ -n \"$gpu_f\" ] || gpu_f=-1",
                ]
            )
        else:
            body.append("gpu_f=-1")
        if gpu_source.load_path:
            if gpu_source.load_format == "busy_total":
                body.extend(
                    [
                        f"gpu_l=$(cat {gpu_source.load_path} 2>/dev/null | awk '{{ if ($2 > 0) printf \"%.3f\", 100 * $1 / $2; else print -1 }}')",
                        "set -- $gpu_l; gpu_l=$1; [ -n \"$gpu_l\" ] || gpu_l=-1",
                    ]
                )
            else:
                body.extend(
                    [
                        f"gpu_l=$(cat {gpu_source.load_path} 2>/dev/null)",
                        "set -- $gpu_l; gpu_l=$1; gpu_l=$(echo \"$gpu_l\" | tr -d '%'); [ -n \"$gpu_l\" ] || gpu_l=-1",
                    ]
                )

    fields = ["$i", "$up", "$cur", "$volt", "$temp"]
    fields.extend(f"$g_{name}" for name in cpu_names)
    for core in core_ids:
        fields.extend(f"$c{core}_{name}" for name in cpu_names)
    fields.extend(f"$f_{policy.name}" for policy in policies)
    if gpu_source:
        fields.append("$gpu_f")
        if gpu_source.load_path:
            fields.append("$gpu_l")
    sample_format = "S|" + "|".join(["%s"] * len(fields)) + "\\n"
    sample_args = " ".join(f'\"{field}\"' for field in fields)
    body.extend(
        [
            f"printf '{sample_format}' {sample_args}",
            f"if [ $((i % {context_every})) -eq 0 ]; then ctx_refresh=0; if [ $((i % {refresh_every})) -eq 0 ]; then ctx_refresh=1; fi; emit_context \"$up\" \"$ctx_refresh\" & fi",
            "now=${up%%.*}",
            "i=$((i+1))",
            "if [ \"$now\" -ge \"$end\" ] && [ \"$i\" -gt 1 ]; then break; fi",
            f"sleep {interval_s:.3f}",
        ]
    )
    lines.extend(["while true; do", *body, "done", "wait"])
    return "\n".join(lines)


def parse_sampler_line(
    line: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
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
    expected = 5 + 8 + len(core_ids) * 8 + len(policies)
    if gpu_source:
        expected += 1 + (1 if gpu_source.load_path else 0)
    if len(parts) != expected:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    offset = 5
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
    if gpu_source:
        gpu_frequency_raw = values[offset] if values[offset] >= 0 else None
        offset += 1
        if gpu_source.load_path:
            gpu_load_raw = values[offset] if values[offset] >= 0 else None
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
    )


def parse_raw_samples(
    text: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
) -> List[RawSample]:
    rows: List[RawSample] = []
    for line in text.splitlines():
        parsed = parse_sampler_line(line, policies, gpu_source)
        if isinstance(parsed, RawSample):
            rows.append(parsed)
    return rows


def parse_normalized_samples(
    text: str,
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
) -> List[Sample]:
    rows: List[Sample] = []
    base_uptime: Optional[float] = None
    last_uptime: Optional[float] = None
    for line in text.splitlines():
        parsed = parse_sampler_line(line, policies, gpu_source)
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
) -> List[ContextSample]:
    rows: List[ContextSample] = []
    for line in text.splitlines():
        parsed = parse_sampler_line(line, policies, gpu_source)
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
            collection_ms=collection_ms,
        ),
        error,
    )


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
    ) -> None:
        self.adb = adb
        self.device = device
        self.journal = journal
        self.process_interval_s = max(2.0, process_interval_s)
        self.thread_interval_s = max(5.0, thread_interval_s)
        self.thermal_interval_s = max(2.0, thermal_interval_s)
        self.scheduler_interval_s = max(5.0, scheduler_interval_s)
        self.system_snapshot_count = 0
        self.thermal_snapshot_count = 0
        self.scheduler_snapshot_count = 0
        self.warnings: List[str] = []
        self.latest_system: Optional[SystemSnapshot] = None
        self._warning_keys: set[str] = set()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="android-power-system-monitor",
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
        next_process = now
        next_thread = now
        next_thermal = now
        next_scheduler = now
        while not self._stop_event.is_set():
            now = time.monotonic()
            try:
                if now >= next_process or now >= next_thread:
                    process_due = now >= next_process
                    include_threads = now >= next_thread
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
                    if process_due:
                        next_process = self._advance_due(
                            next_process,
                            self.process_interval_s,
                            completed,
                        )
                    if include_threads:
                        next_thread = self._advance_due(next_thread, self.thread_interval_s, completed)

                now = time.monotonic()
                if now >= next_thermal and not self._stop_event.is_set():
                    snapshot, error = collect_thermal_snapshot(self.adb, self.device)
                    if snapshot is not None:
                        self.journal.append_thermal_snapshot(snapshot)
                        self.thermal_snapshot_count += 1
                    if error:
                        self._warn_once("thermal", f"热状态监控：{error}")
                    next_thermal = self._advance_due(
                        next_thermal,
                        self.thermal_interval_s,
                        time.monotonic(),
                    )

                now = time.monotonic()
                if now >= next_scheduler and not self._stop_event.is_set():
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
    checkpoint_interval_s: float = 30.0,
    reconnect_timeout_s: float = 120.0,
    system_monitor_enabled: bool = False,
    process_interval_s: float = 10.0,
    thread_interval_s: float = 30.0,
    thermal_interval_s: float = 10.0,
    scheduler_interval_s: float = 30.0,
) -> StreamCollectionResult:
    """Stream a wall-clock session while preserving every complete line on disk."""

    result = StreamCollectionResult()
    host_start = time.monotonic()
    deadline = host_start + duration_s
    next_checkpoint = host_start + checkpoint_interval_s
    fatal_stop = False
    monitor: Optional[SystemMonitorWorker] = None
    if system_monitor_enabled:
        monitor = SystemMonitorWorker(
            adb,
            device,
            journal,
            process_interval_s=process_interval_s,
            thread_interval_s=thread_interval_s,
            thermal_interval_s=thermal_interval_s,
            scheduler_interval_s=scheduler_interval_s,
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
            )
            argv = adb_prefix(adb, device) + ["shell", script]
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
                            parsed = parse_sampler_line(line, policies, gpu_source)
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


def collect_post_run_outputs(adb: str, device: str) -> Dict[str, str]:
    outputs: Dict[str, str] = {}
    commands: List[Tuple[str, List[str], float]] = [
        ("battery_end", ["dumpsys", "battery"], 15),
        ("gpu_end", ["dumpsys", "gpu"], 45),
        ("batterystats_usage", ["dumpsys", "batterystats", "--usage"], 60),
        ("batterystats_checkin", ["dumpsys", "batterystats", "-c"], 60),
        ("batterystats", ["dumpsys", "batterystats"], 90),
        ("batterystats_wakeups", ["dumpsys", "batterystats", "--wakeups"], 45),
        ("power_profile", ["dumpsys", "batterystats", "--power-profile"], 45),
        ("cpuinfo", ["dumpsys", "cpuinfo"], 45),
        ("thermalservice", ["dumpsys", "thermalservice"], 30),
        ("display", ["dumpsys", "display"], 45),
        ("power", ["dumpsys", "power"], 30),
        ("packages", ["cmd", "package", "list", "packages", "-U"], 30),
    ]
    for key, args, timeout_s in commands:
        outputs[key] = collect_text(adb, device, args, timeout_s=timeout_s)
    outputs["screen_brightness"] = collect_text(
        adb, device, ["settings", "get", "system", "screen_brightness"], timeout_s=10
    ).strip()
    outputs["peak_refresh_rate"] = collect_text(
        adb, device, ["settings", "get", "system", "peak_refresh_rate"], timeout_s=10
    ).strip()
    return outputs
