from __future__ import annotations

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
        info = {"serial": parts[0], "state": parts[1]}
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
    return {
        "serial": device,
        "brand": get_prop(adb, device, "ro.product.brand"),
        "model": get_prop(adb, device, "ro.product.model"),
        "device": get_prop(adb, device, "ro.product.device"),
        "soc_manufacturer": get_prop(adb, device, "ro.soc.manufacturer"),
        "soc_model": get_prop(adb, device, "ro.soc.model"),
        "hardware": get_prop(adb, device, "ro.hardware"),
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


def detect_gpu_source(
    adb: str,
    device: str,
    frequency_override: Optional[str] = None,
) -> Tuple[Optional[GpuSource], Dict[str, object]]:
    profiler_support = get_prop(adb, device, "graphics.gpu.profiler.support")
    producer_help = adb_shell(adb, device, ["gpu_counter_producer", "-h"], timeout_s=10)
    perfetto_state = run_command(
        adb_prefix(adb, device) + ["exec-out", "perfetto", "--query-raw"],
        timeout_s=15,
    )
    perfetto_gpu_sources = [
        name
        for name in (
            "gpu.counters",
            "gpu.counter",
            "gpu.renderstages",
            "android.gpu.memory",
        )
        if name in perfetto_state.stdout
    ]
    hardware_counter_sources = [
        name
        for name in perfetto_gpu_sources
        if "counter" in name.lower() and "memory" not in name.lower()
    ]
    candidates: List[str] = []
    if frequency_override:
        candidates.append(frequency_override)

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
        if any(token in identity for token in ("gpu", "mali", "kgsl", "3d")):
            candidates.append(f"{directory}/cur_freq")

    candidates.extend(
        [
            "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq",
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

    probe: Dict[str, object] = {
        "frequency_available": frequency_path is not None,
        "attempts": attempts,
        "graphics_gpu_profiler_support": profiler_support.lower() == "true",
        "gpu_counter_producer_present": "GPU hardware counter" in (
            producer_help.stdout + producer_help.stderr
        ),
        "perfetto_gpu_sources": perfetto_gpu_sources,
        "perfetto_hardware_counter_source_available": bool(hardware_counter_sources),
    }
    if frequency_path is None or initial_raw is None:
        probe["reason"] = (
            "No readable GPU frequency node was exposed to the ADB shell, and Perfetto did not "
            "register a GPU hardware-counter data source."
        )
        return None, probe

    load_path: Optional[str] = None
    for path in (
        "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage",
        "/sys/kernel/ged/hal/gpu_utilization",
    ):
        value, _ = _readable_number(adb, device, path)
        if value is not None:
            load_path = path
            break

    parent = frequency_path.rsplit("/", 1)[0]
    min_raw = _read_number(adb, device, f"{parent}/min_freq")
    max_raw = _read_number(adb, device, f"{parent}/max_freq")
    available_raw = _read_numbers(adb, device, f"{parent}/available_frequencies")
    name_result = adb_shell(adb, device, ["cat", f"{parent}/name"], timeout_s=10)
    name = name_result.stdout.strip() if name_result.ok and name_result.stdout.strip() else "GPU"
    source = GpuSource(
        name=name,
        frequency_path=frequency_path,
        load_path=load_path,
        minimum_mhz=frequency_to_mhz(min_raw) if min_raw is not None else None,
        maximum_mhz=frequency_to_mhz(max_raw) if max_raw is not None else None,
        available_frequencies_mhz=[frequency_to_mhz(value) for value in available_raw],
    )
    probe.update({"source": asdict(source), "initial_frequency_mhz": frequency_to_mhz(initial_raw)})
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
        body.extend(
            [
                f"gpu_f=$(cat {gpu_source.frequency_path} 2>/dev/null)",
                "set -- $gpu_f; gpu_f=$1; [ -n \"$gpu_f\" ] || gpu_f=-1",
            ]
        )
        if gpu_source.load_path:
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
) -> RawSample | ContextSample | None:
    stripped = line.strip()
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


def _checkpoint_state(
    result: StreamCollectionResult,
    status: str,
    requested_duration_s: float,
    host_start_monotonic: float,
) -> Dict[str, object]:
    return {
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
) -> StreamCollectionResult:
    """Stream a wall-clock session while preserving every complete line on disk."""

    result = StreamCollectionResult()
    host_start = time.monotonic()
    deadline = host_start + duration_s
    next_checkpoint = host_start + checkpoint_interval_s
    fatal_stop = False
    journal.checkpoint(_checkpoint_state(result, "collecting", duration_s, host_start))

    try:
        while time.monotonic() < deadline and not fatal_stop:
            if not _wait_for_device(adb, device, deadline, reconnect_timeout_s):
                result.stop_reason = "reconnect_timeout"
                result.warnings.append(
                    "ADB did not reconnect before the collection window ended; partial data was retained."
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
                                        "Device uptime moved backwards, indicating a reboot; the session was stopped."
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
                            _checkpoint_state(result, "collecting", duration_s, host_start)
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
            )
        )
        return result
    except KeyboardInterrupt:
        result.stop_reason = "interrupted"
        result.host_elapsed_s = time.monotonic() - host_start
        journal.checkpoint(_checkpoint_state(result, "interrupted", duration_s, host_start))
        raise
    except Exception:
        result.stop_reason = "collector_error"
        result.host_elapsed_s = time.monotonic() - host_start
        journal.checkpoint(_checkpoint_state(result, "failed", duration_s, host_start))
        raise


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
