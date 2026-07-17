from __future__ import annotations

import bisect
import math
import re
import statistics
from collections import deque
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .collector import frequency_to_mhz, parse_android_runtime_settings
from .features import capture_features_from_metadata
from .models import (
    ContextSample,
    CpuPolicy,
    ExternalEvent,
    GpuSource,
    RawSample,
    SCHEMA_VERSION,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
    IOS_SYSTEM_LOAD_STALE_AFTER_S,
    is_consumption_power_sample,
    is_power_sample_fresh_for_consumption,
)
from .parsers import (
    classify_thread_activity,
    extract_kernel_wakelocks,
    extract_stats_window,
    format_bytes,
    parse_battery_usage,
    parse_checkin_network,
    parse_cpu_processes,
    parse_display,
    parse_gpu_dump,
    parse_gpu_work,
    parse_package_uids,
    parse_power_profile,
    parse_thermal,
)


def normalize_current_ma(raw_value: float, unit: str) -> float:
    if unit == "ua":
        return raw_value / 1000.0
    if unit == "ma":
        return raw_value
    if abs(raw_value) >= 20_000:
        return raw_value / 1000.0
    return raw_value


def _cpu_utilization(previous: object, current: object) -> Optional[float]:
    previous_total, previous_idle = previous.total_and_idle()  # type: ignore[attr-defined]
    current_total, current_idle = current.total_and_idle()  # type: ignore[attr-defined]
    delta_total = current_total - previous_total
    delta_idle = current_idle - previous_idle
    if delta_total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total))


def _directed_current(raw_ma: float, battery_status: str) -> Tuple[float, bool]:
    if battery_status == "discharging":
        return -abs(raw_ma), raw_ma > 0
    if battery_status == "charging":
        return abs(raw_ma), raw_ma < 0
    return raw_ma, False


def convert_samples(
    raw_samples: Sequence[RawSample],
    policies: Sequence[CpuPolicy],
    gpu_source: Optional[GpuSource],
    start_voltage_mv: float,
    end_voltage_mv: float,
    current_unit: str,
    battery_status: str,
    max_cpu_gap_s: Optional[float] = None,
    external_power: Optional[bool] = None,
) -> Tuple[List[Sample], List[str]]:
    if len(raw_samples) < 2:
        raise RuntimeError("sampler returned fewer than two valid rows")
    warnings: List[str] = []
    base_uptime = raw_samples[0].uptime_s
    duration = max(0.001, raw_samples[-1].uptime_s - base_uptime)
    sign_corrected = False
    samples: List[Sample] = []
    previous_raw: Optional[RawSample] = None
    legacy_external_power_state = not any(
        raw.external_power is not None or raw.battery_status is not None
        for raw in raw_samples
    )
    unknown_external_power_seen = False

    for raw in raw_samples:
        elapsed = raw.uptime_s - base_uptime
        fraction = max(0.0, min(1.0, elapsed / duration))
        fallback_voltage = start_voltage_mv + (end_voltage_mv - start_voltage_mv) * fraction
        voltage = raw.voltage_mv if raw.voltage_mv and raw.voltage_mv > 0 else fallback_voltage
        sensor_ma = normalize_current_ma(raw.current_raw, current_unit)
        sample_status = str(raw.battery_status or battery_status or "unknown").lower()
        sample_external_power = raw.external_power
        if sample_external_power is None and legacy_external_power_state:
            sample_external_power = external_power
        if sample_external_power is None:
            unknown_external_power_seen = True
        signed_current_ma, corrected = _directed_current(sensor_ma, sample_status)
        sign_corrected = sign_corrected or corrected
        current_ma = abs(signed_current_ma)
        if sample_external_power is True:
            direction = (
                "charging"
                if sample_status in {"charging", "full"} or signed_current_ma > 0
                else "external_power"
            )
        elif signed_current_ma < 0:
            direction = "discharging"
        elif signed_current_ma > 0:
            direction = "charging"
        else:
            direction = sample_status if sample_status in {"charging", "discharging"} else "idle"

        cpu_pct = None
        core_cpu_pct: Dict[str, float] = {}
        if previous_raw is not None and (
            max_cpu_gap_s is None or raw.uptime_s - previous_raw.uptime_s <= max_cpu_gap_s
        ):
            cpu_pct = _cpu_utilization(previous_raw.cpu, raw.cpu)
            for core, counters in raw.core_cpu.items():
                previous_counters = previous_raw.core_cpu.get(core)
                if previous_counters is None:
                    continue
                value = _cpu_utilization(previous_counters, counters)
                if value is not None:
                    core_cpu_pct[str(core)] = value

        cluster_cpu_pct: Dict[str, float] = {}
        for policy in policies:
            values = [core_cpu_pct[str(core)] for core in policy.cores if str(core) in core_cpu_pct]
            if values:
                cluster_cpu_pct[policy.name] = statistics.fmean(values)

        gpu_frequency_mhz = None
        if gpu_source is not None and raw.gpu_frequency_raw is not None:
            gpu_frequency_mhz = frequency_to_mhz(raw.gpu_frequency_raw)
        gpu_load_pct = None
        if raw.gpu_load_raw is not None:
            gpu_load_pct = max(0.0, min(100.0, raw.gpu_load_raw))
        memory_frequency_mhz = (
            frequency_to_mhz(raw.memory_frequency_raw)
            if raw.memory_frequency_raw is not None
            else None
        )

        samples.append(
            Sample(
                index=raw.index,
                elapsed_s=elapsed,
                uptime_s=raw.uptime_s,
                current_ma=current_ma,
                signed_current_ma=signed_current_ma,
                voltage_mv=voltage,
                power_mw=current_ma * voltage / 1000.0,
                direction=direction,
                cpu_pct=cpu_pct,
                core_cpu_pct=core_cpu_pct,
                cluster_cpu_pct=cluster_cpu_pct,
                frequencies_mhz={
                    name: value / 1000.0 for name, value in raw.frequencies_khz.items()
                },
                gpu_frequency_mhz=gpu_frequency_mhz,
                gpu_load_pct=gpu_load_pct,
                memory_frequency_mhz=memory_frequency_mhz,
                battery_temperature_c=(
                    raw.temperature_tenths_c / 10.0
                    if raw.temperature_tenths_c is not None
                    else None
                ),
                power_valid_for_consumption=(
                    signed_current_ma < 0 and sample_external_power is False
                ),
                external_power=sample_external_power,
            )
        )
        previous_raw = raw

    if sign_corrected:
        warnings.append(
            "厂商电流符号与 BatteryService 状态不一致，已自动标准化。"
            "current_ma 始终为正幅值，signed_current_ma 保留充放电方向。"
        )
    if unknown_external_power_seen:
        warnings.append(
            "部分样本未能确认是否接入外部电源；这些样本只保留原始电流、电压和功率，"
            "不会进入正式耗电、能量或相关性分析。"
        )
    return samples, warnings


def percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def histogram_percentile(
    histogram: Dict[float, int],
    quantile: float,
) -> Optional[float]:
    total = sum(max(0, count) for count in histogram.values())
    if total <= 0:
        return None
    threshold = total * max(0.0, min(1.0, quantile))
    cumulative = 0
    for bucket, count in sorted(histogram.items()):
        cumulative += max(0, count)
        if cumulative >= threshold:
            return bucket
    return max(histogram) if histogram else None


def histogram_slowest_average(
    histogram: Dict[float, int],
    fraction: float = 0.01,
) -> Optional[float]:
    """Return the weighted average duration of the slowest fraction of frames."""

    total = sum(max(0, count) for count in histogram.values())
    if total <= 0:
        return None
    remaining = max(1, math.ceil(total * max(0.0, min(1.0, fraction))))
    weighted = 0.0
    selected = 0
    for bucket, count in sorted(histogram.items(), reverse=True):
        take = min(max(0, count), remaining)
        if take <= 0:
            continue
        weighted += bucket * take
        selected += take
        remaining -= take
        if remaining <= 0:
            break
    return weighted / selected if selected > 0 else None


def median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def detect_spikes(samples: Sequence[Sample]) -> List[Dict[str, float]]:
    consumption_samples = [sample for sample in samples if is_consumption_power_sample(sample)]
    if len(consumption_samples) < 5:
        return []
    consumption_powers = [sample.power_mw for sample in consumption_samples]
    median = statistics.median(consumption_powers)
    mad = median_absolute_deviation(consumption_powers)
    threshold = median + max(150.0, 3.0 * mad)
    windows: List[Dict[str, float]] = []
    current: List[Sample] = []
    for sample in samples:
        if not is_consumption_power_sample(sample):
            if current:
                windows.append(_spike_window(current, threshold))
                current = []
            continue
        if sample.power_mw >= threshold:
            current.append(sample)
            continue
        if current:
            windows.append(_spike_window(current, threshold))
            current = []
    if current:
        windows.append(_spike_window(current, threshold))
    return sorted(windows, key=lambda item: item["peak_mw"], reverse=True)[:5]


def _spike_window(samples: Sequence[Sample], threshold: float) -> Dict[str, float]:
    return {
        "start_s": samples[0].elapsed_s,
        "end_s": samples[-1].elapsed_s,
        "peak_mw": max(item.power_mw for item in samples),
        "average_mw": statistics.fmean(item.power_mw for item in samples),
        "threshold_mw": threshold,
    }


def sample_intervals(
    samples: Sequence[Sample],
    max_gap_s: Optional[float] = None,
    require_consumption_power: bool = False,
) -> List[Tuple[Sample, Sample, float]]:
    intervals: List[Tuple[Sample, Sample, float]] = []
    for previous, current in zip(samples, samples[1:]):
        if bool(getattr(current, "_report_break_before", False)):
            continue
        delta_s = current.uptime_s - previous.uptime_s
        if delta_s <= 0:
            continue
        if max_gap_s is not None and delta_s > max_gap_s:
            continue
        if require_consumption_power and not (
            is_consumption_power_sample(previous)
            and is_consumption_power_sample(current)
        ):
            continue
        intervals.append((previous, current, delta_s))
    return intervals


def integrate_values(
    samples: Sequence[Sample],
    getter: Callable[[Sample], float],
    max_gap_s: Optional[float] = None,
    require_consumption_power: bool = False,
) -> float:
    total = 0.0
    for previous, current, delta_s in sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=require_consumption_power,
    ):
        total += (getter(previous) + getter(current)) * 0.5 * delta_s / 3600.0
    return total


def integrate_step_values(
    samples: Sequence[Sample],
    getter: Callable[[Sample], float],
    max_gap_s: Optional[float] = None,
    require_consumption_power: bool = False,
    transition_age_getter: Optional[Callable[[Sample], Optional[float]]] = None,
) -> Tuple[float, float]:
    """Integrate zero-order-held telemetry and return energy plus covered seconds.

    Low-rate channels such as iOS DiagnosticsService SystemLoad retain their
    most recent value until the next device update.  When the collector keeps
    the age of that update, place the step at the estimated update time rather
    than linearly interpolating between host observations.
    """

    total_mw_seconds = 0.0
    covered_duration_s = 0.0
    for previous, current, delta_s in sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=require_consumption_power,
    ):
        previous_value = float(getter(previous))
        current_value = float(getter(current))
        transition_uptime = current.uptime_s
        if transition_age_getter is not None and current_value != previous_value:
            age = transition_age_getter(current)
            if (
                isinstance(age, (int, float))
                and not isinstance(age, bool)
                and math.isfinite(float(age))
                and float(age) >= 0.0
            ):
                transition_uptime = current.uptime_s - float(age)
        transition_uptime = min(
            current.uptime_s,
            max(previous.uptime_s, transition_uptime),
        )
        previous_duration_s = transition_uptime - previous.uptime_s
        current_duration_s = current.uptime_s - transition_uptime
        total_mw_seconds += (
            previous_value * previous_duration_s
            + current_value * current_duration_s
        )
        covered_duration_s += delta_s
    return total_mw_seconds / 3600.0, covered_duration_s


def build_buckets(samples: Sequence[Sample], width_s: float = 10.0) -> List[Dict[str, object]]:
    bucket_map: Dict[int, List[Sample]] = {}
    for sample in samples:
        if not is_consumption_power_sample(sample):
            continue
        bucket_map.setdefault(int(sample.elapsed_s // width_s), []).append(sample)
    buckets: List[Dict[str, object]] = []
    for bucket in sorted(bucket_map):
        rows = bucket_map[bucket]
        powers = [item.power_mw for item in rows]
        currents = [item.current_ma for item in rows]
        cpus = [item.cpu_pct for item in rows if item.cpu_pct is not None]
        buckets.append(
            {
                "start_s": bucket * width_s,
                "end_s": (bucket + 1) * width_s,
                "average_power_mw": statistics.fmean(powers),
                "peak_power_mw": max(powers),
                "average_current_ma": statistics.fmean(currents),
                "average_cpu_pct": statistics.fmean(cpus) if cpus else None,
            }
        )
    return buckets


def _profile_array(profile: Dict[str, object], *keys: str) -> List[float]:
    for key in keys:
        value = profile.get(key)
        if isinstance(value, list):
            return [float(item) for item in value]
    return []


def _nearest_power_coefficient(
    frequency_mhz: float,
    speeds_mhz: Sequence[float],
    currents_ma: Sequence[float],
) -> Optional[float]:
    count = min(len(speeds_mhz), len(currents_ma))
    if count == 0:
        return None
    index = min(range(count), key=lambda item: abs(speeds_mhz[item] - frequency_mhz))
    return float(currents_ma[index])


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = math.sqrt(
        sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys)
    )
    return numerator / denominator if denominator > 0 else None


def analyze_cpu(
    samples: Sequence[Sample],
    policies: Sequence[Dict[str, object]],
    power_profile: Dict[str, object],
    max_gap_s: Optional[float] = None,
) -> Dict[str, object]:
    cluster_rows: List[Dict[str, object]] = []
    timeline: List[Dict[str, object]] = [
        {"elapsed_s": sample.elapsed_s, "clusters": {}, "modeled_power_mw": 0.0}
        for sample in samples
    ]

    for policy in policies:
        name = str(policy.get("name"))
        label = str(policy.get("label") or name)
        cluster_index = int(policy.get("cluster_index") or 0)
        cores = [int(value) for value in policy.get("cores", [])]
        max_khz = policy.get("max_khz")
        hardware_max_mhz = float(max_khz) / 1000.0 if isinstance(max_khz, (int, float)) else None
        frequencies = [sample.frequencies_mhz.get(name, 0.0) for sample in samples]
        loads = [sample.cluster_cpu_pct.get(name) for sample in samples]
        speed_values = _profile_array(
            power_profile,
            f"cpu.core_speeds.cluster{cluster_index}",
            f"cpu.speeds.cluster{cluster_index}",
        )
        speeds_mhz = [value / 1000.0 if value >= 10_000 else value for value in speed_values]
        current_curve = _profile_array(
            power_profile,
            f"cpu.core_power.cluster{cluster_index}",
            f"cpu.active.cluster{cluster_index}",
        )
        pair_count = min(len(speeds_mhz), len(current_curve))
        speeds_mhz = speeds_mhz[:pair_count]
        current_curve = current_curve[:pair_count]
        cluster_overhead = power_profile.get(f"cpu.cluster_power.cluster{cluster_index}")
        overhead_ma = float(cluster_overhead) if isinstance(cluster_overhead, (int, float)) else 0.0

        modeled_values: List[float] = []
        premium_values: List[float] = []
        active_core_values: List[float] = []
        valid_loads: List[float] = []
        modeled_for_correlation: List[float] = []
        measured_for_model: List[float] = []
        model_available = bool(pair_count)
        minimum_coefficient = current_curve[0] if current_curve else None

        for index, sample in enumerate(samples):
            load = loads[index]
            frequency = frequencies[index]
            entry: Dict[str, object] = {
                "frequency_mhz": frequency,
                "load_pct": load,
                "active_cores": None,
                "modeled_power_mw": None,
                "frequency_premium_mw": None,
            }
            if load is not None:
                active_cores = float(load) / 100.0 * max(1, len(cores))
                entry["active_cores"] = active_cores
                active_core_values.append(active_cores)
                valid_loads.append(float(load))
                coefficient = _nearest_power_coefficient(frequency, speeds_mhz, current_curve)
                if coefficient is not None and minimum_coefficient is not None:
                    max_core_load = max(
                        (sample.core_cpu_pct.get(str(core), 0.0) for core in cores),
                        default=float(load),
                    )
                    cluster_active_fraction = max_core_load / 100.0
                    modeled_current_ma = coefficient * active_cores + overhead_ma * cluster_active_fraction
                    baseline_current_ma = (
                        minimum_coefficient * active_cores + overhead_ma * cluster_active_fraction
                    )
                    modeled_power = modeled_current_ma * sample.voltage_mv / 1000.0
                    premium_power = max(
                        0.0,
                        (modeled_current_ma - baseline_current_ma) * sample.voltage_mv / 1000.0,
                    )
                    entry["modeled_power_mw"] = modeled_power
                    entry["frequency_premium_mw"] = premium_power
                    modeled_values.append(modeled_power)
                    premium_values.append(premium_power)
                    if is_consumption_power_sample(sample):
                        modeled_for_correlation.append(modeled_power)
                        measured_for_model.append(sample.power_mw)
                    timeline[index]["modeled_power_mw"] = (
                        float(timeline[index]["modeled_power_mw"]) + modeled_power
                    )
            timeline[index]["clusters"][name] = entry  # type: ignore[index]

        band_seconds = {"low": 0.0, "balanced": 0.0, "high": 0.0}
        band_load = {"low": 0.0, "balanced": 0.0, "high": 0.0}
        total_seconds = 0.0
        total_load_weight = 0.0
        reference_max = hardware_max_mhz or (max(frequencies) if frequencies else 1.0) or 1.0
        for index, (current, following) in enumerate(zip(samples, samples[1:])):
            if bool(getattr(following, "_report_break_before", False)):
                continue
            delta_s = max(0.0, following.uptime_s - current.uptime_s)
            if max_gap_s is not None and delta_s > max_gap_s:
                continue
            ratio = frequencies[index] / reference_max if reference_max else 0.0
            band = "low" if ratio < 0.5 else "balanced" if ratio < 0.8 else "high"
            load = float(loads[index] or 0.0) / 100.0 * max(1, len(cores))
            band_seconds[band] += delta_s
            band_load[band] += delta_s * load
            total_seconds += delta_s
            total_load_weight += delta_s * load

        load_weighted_frequency = None
        load_frequency_pairs = [
            (frequency, float(load))
            for frequency, load in zip(frequencies, loads)
            if load is not None and float(load) > 0
        ]
        if load_frequency_pairs:
            denominator = sum(load for _, load in load_frequency_pairs)
            load_weighted_frequency = sum(freq * load for freq, load in load_frequency_pairs) / denominator

        cluster_rows.append(
            {
                "name": name,
                "label": label,
                "cluster_index": cluster_index,
                "cores": cores,
                "core_count": len(cores),
                "average_mhz": statistics.fmean(frequencies) if frequencies else None,
                "load_weighted_mhz": load_weighted_frequency,
                "maximum_mhz": max(frequencies) if frequencies else None,
                "hardware_max_mhz": hardware_max_mhz,
                "average_load_pct": statistics.fmean(valid_loads) if valid_loads else None,
                "average_active_cores": statistics.fmean(active_core_values) if active_core_values else None,
                "modeled_power_mw": statistics.fmean(modeled_values) if modeled_values else None,
                "maximum_modeled_power_mw": max(modeled_values) if modeled_values else None,
                "frequency_premium_mw": statistics.fmean(premium_values) if premium_values else None,
                "frequency_premium_pct": (
                    statistics.fmean(premium_values) / statistics.fmean(modeled_values) * 100.0
                    if modeled_values and statistics.fmean(modeled_values) > 0
                    else None
                ),
                "measured_power_correlation": _pearson(
                    modeled_for_correlation,
                    measured_for_model,
                ),
                "model_available": model_available,
                "model_source": "Android Power Profile per-core frequency table" if model_available else None,
                "residency": [
                    {
                        "band": band,
                        "time_pct": band_seconds[band] / total_seconds * 100.0 if total_seconds else 0.0,
                        "load_weighted_pct": (
                            band_load[band] / total_load_weight * 100.0 if total_load_weight else 0.0
                        ),
                    }
                    for band in ("low", "balanced", "high")
                ],
                "power_curve": [
                    {"frequency_mhz": speed, "current_ma_per_core": current}
                    for speed, current in zip(speeds_mhz, current_curve)
                ],
            }
        )

    total_modeled = [
        float(item["modeled_power_mw"])
        for item in timeline
        if float(item["modeled_power_mw"]) > 0
    ]
    model_available = any(bool(item.get("model_available")) for item in cluster_rows)
    if not model_available:
        for item in timeline:
            item["modeled_power_mw"] = None
    return {
        "clusters": cluster_rows,
        "timeline": timeline,
        "modeled_power_mw": statistics.fmean(total_modeled) if total_modeled else None,
        "source": (
            "Power Profile estimate"
            if model_available
            else "CPU load and frequency counters"
            if cluster_rows
            else None
        ),
        "limitations": (
            "The estimate combines /proc/stat utilization with instantaneous cpufreq and Android's "
            "per-core current table. It is useful for same-device comparisons, not a hardware rail measurement."
            if model_available
            else "CPU load and frequency are retained as measured counters, but no per-core power curve was "
            "available, so CPU electrical power is not modeled."
            if cluster_rows
            else "No CPU frequency policy or per-cluster frequency source was available; only total CPU load "
            "can be retained and no CPU electrical power is modeled."
        ),
    }


def analyze_gpu(
    samples: Sequence[Sample],
    metadata: Dict[str, object],
    raw_outputs: Dict[str, str],
    package_uids: Dict[int, List[str]],
    target_uid: Optional[int],
    duration_s: float,
    system_snapshots: Sequence[SystemSnapshot] = (),
) -> Dict[str, object]:
    platform = str(metadata.get("platform") or "android").lower()
    frequency_values = [
        sample.gpu_frequency_mhz for sample in samples if sample.gpu_frequency_mhz is not None
    ]
    load_values = [sample.gpu_load_pct for sample in samples if sample.gpu_load_pct is not None]
    start = parse_gpu_work(raw_outputs.get("gpu_start", ""))
    end = parse_gpu_work(raw_outputs.get("gpu_end", ""))
    start_dump = parse_gpu_dump(raw_outputs.get("gpu_start", ""))
    end_dump = parse_gpu_dump(raw_outputs.get("gpu_end", ""))
    work_rows: List[Dict[str, object]] = []
    for uid, end_item in end.items():
        start_item = start.get(uid, {})
        active_delta = max(0, end_item["active_ns"] - int(start_item.get("active_ns", 0)))
        inactive_delta = max(0, end_item["inactive_ns"] - int(start_item.get("inactive_ns", 0)))
        if active_delta <= 0 and inactive_delta <= 0:
            continue
        work_rows.append(
            {
                "uid": uid,
                "packages": package_uids.get(uid, []),
                "active_ms": active_delta / 1_000_000.0,
                "inactive_ms": inactive_delta / 1_000_000.0,
                "active_ratio_pct": active_delta / (duration_s * 1_000_000_000.0) * 100.0
                if duration_s > 0
                else None,
                "source": "dumpsys gpu work duration",
            }
        )
    work_rows.sort(key=lambda item: float(item["active_ms"]), reverse=True)
    target_work = next((item for item in work_rows if item.get("uid") == target_uid), None)
    gpu_probe = metadata.get("gpu_probe", {})
    source = metadata.get("gpu_source")
    process_names: Dict[int, str] = {}
    for snapshot in system_snapshots:
        for item in [*snapshot.processes, *snapshot.watched_processes]:
            pid = item.get("pid")
            name = item.get("name") or item.get("command")
            if isinstance(pid, int) and name:
                process_names[pid] = str(name)
    memory_rows: List[Dict[str, object]] = []
    for item in end_dump.get("process_memory", []):
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        memory_rows.append(
            {
                **item,
                "name": process_names.get(pid) if isinstance(pid, int) else None,
            }
        )
    reason = None
    if not frequency_values and not load_values and isinstance(gpu_probe, dict):
        reason = gpu_probe.get("reason")
    if not reason and platform == "ios" and not load_values:
        reason = "本次会话未恢复到 DVT Graphics 利用率事件。"
    if not reason and platform == "harmony" and not frequency_values and not load_values:
        reason = (
            "HarmonyOS production builds restrict GPU frequency/load sysfs nodes from the HDC shell; "
            "no Android dumpsys GPU fallback exists."
        )
    limitations = (
        "iOS DVT Graphics reports relative GPU utilization counters. It does not expose an "
        "electrical GPU rail or a public per-application GPU energy measurement."
        if platform == "ios"
        else (
            (
                "HarmonyOS native HDC does not expose a readable GPU frequency/load node on this production "
                "build; the values in this session were supplied by SmartPerf. They are utilization/frequency "
                "counters, not an electrical GPU rail or per-application GPU energy measurement."
                if frequency_values or load_values
                else "HarmonyOS HDC does not expose a readable GPU frequency/load node or a public "
                "per-application GPU energy counter on this production build. GPU activity is therefore "
                "explicitly unavailable."
            )
            if platform == "harmony"
            else
            "GPU frequency/load is reported only when a readable OEM sysfs/devfreq node exists. "
            "Qualcomm KGSL is commonly permission-restricted on production builds. UID active "
            "durations and GPU memory are driver evidence, not an electrical power rail."
        )
    )
    return {
        "frequency_available": bool(frequency_values),
        "load_available": bool(load_values),
        "source": source,
        "provider": gpu_probe.get("provider") if isinstance(gpu_probe, dict) else None,
        "model": gpu_probe.get("model") if isinstance(gpu_probe, dict) else None,
        "unavailable_reason": reason,
        "average_frequency_mhz": statistics.fmean(frequency_values) if frequency_values else None,
        "minimum_frequency_mhz": min(frequency_values) if frequency_values else None,
        "maximum_frequency_mhz": max(frequency_values) if frequency_values else None,
        "average_load_pct": statistics.fmean(load_values) if load_values else None,
        "minimum_load_pct": min(load_values) if load_values else None,
        "maximum_load_pct": max(load_values) if load_values else None,
        "work_by_uid": work_rows[:20],
        "target_work": target_work,
        "work_source_available": bool(start and end),
        "memory": {
            "available": bool(end_dump.get("memory_available")),
            "start_total_bytes": start_dump.get("global_total_bytes"),
            "end_total_bytes": end_dump.get("global_total_bytes"),
            "change_bytes": (
                int(end_dump["global_total_bytes"]) - int(start_dump["global_total_bytes"])
                if isinstance(start_dump.get("global_total_bytes"), int)
                and isinstance(end_dump.get("global_total_bytes"), int)
                else None
            ),
            "processes": memory_rows[:20],
        },
        "driver": {
            "stable_game_driver": end_dump.get("stable_game_driver"),
            "prerelease_game_driver": end_dump.get("prerelease_game_driver"),
        },
        "limitations": limitations,
    }


def analyze_memory_frequency(
    samples: Sequence[Sample],
    metadata: Dict[str, object],
) -> Dict[str, object]:
    rows = [
        sample
        for sample in samples
        if isinstance(sample.memory_frequency_mhz, (int, float))
        and float(sample.memory_frequency_mhz) > 0
    ]
    source = metadata.get("memory_source")
    source = source if isinstance(source, dict) else {}
    probe = metadata.get("memory_probe")
    probe = probe if isinstance(probe, dict) else {}
    capture_configuration = metadata.get("capture_configuration", {})
    capture_configuration = (
        capture_configuration if isinstance(capture_configuration, dict) else {}
    )
    backend = str(capture_configuration.get("backend") or "")
    if not rows:
        default_limitation = (
            "HarmonyOS SmartPerf SP_daemon -d did not return a usable DDR frequency field "
            "during this session."
            if backend == "harmony_smartperf"
            else "No readable DRAM/DMC/MIF frequency source was exposed."
        )
        return {
            "available": False,
            "source": source or None,
            "probe": probe,
            "timeline": [],
            "limitations": probe.get("limitations") or default_limitation,
        }

    frequencies = [float(item.memory_frequency_mhz or 0.0) for item in rows]
    consumption_rows = [item for item in rows if is_consumption_power_sample(item)]
    consumption_frequencies = [
        float(item.memory_frequency_mhz or 0.0) for item in consumption_rows
    ]
    powers = [float(item.power_mw) for item in consumption_rows]
    currents = [float(item.current_ma) for item in consumption_rows]
    hardware_max = source.get("maximum_mhz")
    observed_min = min(frequencies)
    observed_max = max(frequencies)
    range_max = (
        float(hardware_max)
        if isinstance(hardware_max, (int, float)) and float(hardware_max) > 0
        else observed_max
    )
    low_threshold = observed_min + (range_max - observed_min) * 0.35
    high_threshold = observed_min + (range_max - observed_min) * 0.75
    high_rows = [
        item for item in rows if float(item.memory_frequency_mhz or 0.0) >= high_threshold
    ]
    lower_rows = [
        item for item in rows if float(item.memory_frequency_mhz or 0.0) < high_threshold
    ]
    high_power_rows = [item for item in high_rows if is_consumption_power_sample(item)]
    lower_power_rows = [item for item in lower_rows if is_consumption_power_sample(item)]
    high_power = (
        statistics.fmean(item.power_mw for item in high_power_rows)
        if high_power_rows
        else None
    )
    lower_power = (
        statistics.fmean(item.power_mw for item in lower_power_rows)
        if lower_power_rows
        else None
    )
    residency_counts = {"low": 0, "medium": 0, "high": 0}
    for value in frequencies:
        if value < low_threshold:
            residency_counts["low"] += 1
        elif value < high_threshold:
            residency_counts["medium"] += 1
        else:
            residency_counts["high"] += 1
    total = len(frequencies)
    return {
        "available": True,
        "source": source or None,
        "probe": probe,
        "average_frequency_mhz": statistics.fmean(frequencies),
        "minimum_frequency_mhz": observed_min,
        "p95_frequency_mhz": percentile(frequencies, 0.95),
        "maximum_frequency_mhz": observed_max,
        "hardware_maximum_mhz": hardware_max,
        "power_valid_for_consumption": bool(consumption_rows),
        "power_correlation": _pearson(consumption_frequencies, powers),
        "current_correlation": _pearson(consumption_frequencies, currents),
        "high_frequency_threshold_mhz": high_threshold,
        "high_frequency_share_pct": len(high_rows) / total * 100.0 if total else 0.0,
        "high_frequency_average_power_mw": high_power,
        "lower_frequency_average_power_mw": lower_power,
        "high_frequency_power_delta_mw": (
            high_power - lower_power
            if isinstance(high_power, (int, float))
            and isinstance(lower_power, (int, float))
            else None
        ),
        "residency": {
            key: value / total * 100.0 if total else 0.0
            for key, value in residency_counts.items()
        },
        "timeline": [
            {
                "elapsed_s": item.elapsed_s,
                "frequency_mhz": item.memory_frequency_mhz,
                "power_mw": item.power_mw,
                "power_valid_for_consumption": is_consumption_power_sample(item),
                "current_ma": item.current_ma,
                "cpu_pct": item.cpu_pct,
                "gpu_load_pct": item.gpu_load_pct,
            }
            for item in rows
        ],
        "limitations": (
            "The value is a readable DMC/DRAM/MIF devfreq clock. It does not expose "
            "per-channel bandwidth, cache hit rate, or an electrical memory rail."
        ),
    }


def component_power_estimates(
    usage: Dict[str, object],
    stats_window: Dict[str, Optional[float]],
    average_voltage_mv: float,
    power_profile: Dict[str, object],
    display: Dict[str, object],
) -> List[Dict[str, object]]:
    observation_s = stats_window.get("time_on_battery_s")
    components: List[Dict[str, object]] = []
    if observation_s and observation_s > 0:
        for item in usage.get("components", []):
            component = dict(item)
            mah = float(component.get("mah", 0.0))
            average_ma = mah / (observation_s / 3600.0)
            component["modeled_power_mw"] = average_ma * average_voltage_mv / 1000.0
            component["confidence"] = "medium"
            components.append(component)

    known_names = {str(item.get("name", "")).lower() for item in components}
    if "screen" not in known_names:
        screen_on = power_profile.get("screen.on.display0", power_profile.get("screen.on"))
        screen_full = power_profile.get("screen.full.display0", power_profile.get("screen.full"))
        brightness_raw = display.get("brightness_raw")
        if isinstance(screen_on, (float, int)) and isinstance(screen_full, (float, int)):
            brightness_fraction = 0.5
            if isinstance(brightness_raw, (float, int)) and 0 <= float(brightness_raw) <= 255:
                brightness_fraction = float(brightness_raw) / 255.0
            screen_ma = float(screen_on) + float(screen_full) * brightness_fraction
            components.append(
                {
                    "name": "screen",
                    "mah": None,
                    "modeled_power_mw": screen_ma * average_voltage_mv / 1000.0,
                    "duration_s": stats_window.get("screen_on_s"),
                    "source": "Power Profile brightness estimate",
                    "confidence": "low",
                }
            )
    components.sort(key=lambda item: float(item.get("modeled_power_mw") or 0.0), reverse=True)
    return components


def _context_at(
    contexts: Sequence[ContextSample],
    uptimes: Sequence[float],
    uptime_s: float,
) -> Optional[ContextSample]:
    index = bisect.bisect_right(uptimes, uptime_s) - 1
    return contexts[index] if index >= 0 else None


_EXPLICIT_INACTIVE_SCREEN_STATES = {
    "off",
    "asleep",
    "sleep",
    "doze",
    "dozing",
    "inactive",
    "suspend",
    "suspended",
    "standby",
    "stand_by",
    "freeze",
    "hibernate",
}
_HARMONY_SHELL_PACKAGES = {
    "com.ohos.sceneboard",
    "com.huawei.hmos.launcher",
}


def _context_screen_partition(context: Optional[ContextSample]) -> str:
    state = str(context.screen_state or "").strip().lower() if context else ""
    return "inactive" if state in _EXPLICIT_INACTIVE_SCREEN_STATES else "active-or-unknown"


def _auto_test_context_allowed(
    context: Optional[ContextSample],
    *,
    performance: bool,
) -> bool:
    if context is None or _context_screen_partition(context) == "inactive":
        return False
    package = str(context.foreground_package or "").strip()
    activity = str(context.foreground_activity or "").strip()
    unknown_labels = {"", "unknown", "none", "null", "--", "-"}
    if package.lower() in unknown_labels and activity.lower() in unknown_labels:
        return False
    if performance and package.lower() in _HARMONY_SHELL_PACKAGES:
        return False
    return True


def analyze_applications(
    samples: Sequence[Sample],
    contexts: Sequence[ContextSample],
    max_gap_s: float,
) -> Dict[str, object]:
    ordered = sorted(contexts, key=lambda item: item.uptime_s)
    uptimes = [item.uptime_s for item in ordered]
    rows: Dict[str, Dict[str, object]] = {}
    covered_s = 0.0
    known_s = 0.0
    for previous, current, delta_s in sample_intervals(samples, max_gap_s):
        midpoint = (previous.uptime_s + current.uptime_s) * 0.5
        context = _context_at(ordered, uptimes, midpoint)
        package = context.foreground_package if context and context.foreground_package else "unknown"
        row = rows.setdefault(
            package,
            {
                "package": package,
                "duration_s": 0.0,
                "consumption_covered_duration_s": 0.0,
                "energy_mwh": 0.0,
                "discharge_mah": 0.0,
                "transition_count": 0,
                "activities": set(),
            },
        )
        row["duration_s"] = float(row["duration_s"]) + delta_s
        interval_power_valid = (
            is_consumption_power_sample(previous)
            and is_consumption_power_sample(current)
        )
        if interval_power_valid:
            row["consumption_covered_duration_s"] = (
                float(row["consumption_covered_duration_s"]) + delta_s
            )
            row["energy_mwh"] = float(row["energy_mwh"]) + (
                (previous.power_mw + current.power_mw) * 0.5 * delta_s / 3600.0
            )
            average_current = (previous.current_ma + current.current_ma) * 0.5
            row["discharge_mah"] = float(row["discharge_mah"]) + average_current * delta_s / 3600.0
        if context and context.foreground_activity:
            activities = row["activities"]
            if isinstance(activities, set):
                activities.add(context.foreground_activity)
        covered_s += delta_s
        if package != "unknown":
            known_s += delta_s

    transitions: List[Dict[str, object]] = []
    previous_package: Optional[str] = None
    for context in ordered:
        if context.uptime_s < samples[0].uptime_s or context.uptime_s >= samples[-1].uptime_s:
            continue
        package = context.foreground_package or "unknown"
        if package == previous_package:
            continue
        transitions.append(
            {
                "elapsed_s": context.uptime_s - samples[0].uptime_s,
                "uptime_s": context.uptime_s,
                "package": package,
                "activity": context.foreground_activity,
            }
        )
        row = rows.get(package)
        if row is not None:
            row["transition_count"] = int(row["transition_count"]) + 1
        previous_package = package

    result_rows: List[Dict[str, object]] = []
    for row in rows.values():
        duration_s = float(row["duration_s"])
        consumption_duration_s = float(row["consumption_covered_duration_s"])
        energy_mwh = float(row["energy_mwh"])
        activities = row.pop("activities")
        row["power_valid_for_consumption"] = consumption_duration_s > 0
        row["average_power_mw"] = (
            energy_mwh * 3600.0 / consumption_duration_s
            if consumption_duration_s > 0
            else None
        )
        if consumption_duration_s <= 0:
            row["energy_mwh"] = None
            row["discharge_mah"] = None
        row["time_pct"] = duration_s / covered_s * 100.0 if covered_s > 0 else None
        row["activities"] = sorted(activities)[:8] if isinstance(activities, set) else []
        row["confidence"] = "medium" if row["package"] != "unknown" else "low"
        result_rows.append(row)
    result_rows.sort(key=lambda item: float(item.get("energy_mwh") or 0.0), reverse=True)

    context_deltas = [
        following.uptime_s - current.uptime_s
        for current, following in zip(ordered, ordered[1:])
        if following.uptime_s > current.uptime_s
    ]
    return {
        "available": bool(ordered),
        "power_valid_for_consumption": any(
            bool(item.get("power_valid_for_consumption")) for item in result_rows
        ),
        "context_sample_count": len(ordered),
        "coverage_pct": known_s / covered_s * 100.0 if covered_s > 0 else 0.0,
        "transition_count": max(0, len(transitions) - 1),
        "boundary_uncertainty_s": statistics.median(context_deltas) if context_deltas else None,
        "rows": result_rows,
        "transitions": transitions,
    }


def analyze_external_events(
    samples: Sequence[Sample],
    events: Sequence[ExternalEvent],
    max_gap_s: float,
    sample_interval_s: float,
) -> Dict[str, object]:
    minimum_reliable_duration_s = max(3.0 * sample_interval_s, sample_interval_s + 2.0)
    intervals = sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=True,
    )
    span_rows: List[Dict[str, object]] = []
    for event in sorted(events, key=lambda item: item.device_uptime_s):
        if event.duration_s is None or event.duration_s <= 0:
            continue
        start = event.device_uptime_s
        end = start + event.duration_s
        covered_s = 0.0
        energy_mwh = 0.0
        discharge_mah = 0.0
        for previous, current, _ in intervals:
            overlap_s = max(
                0.0,
                min(end, current.uptime_s) - max(start, previous.uptime_s),
            )
            if overlap_s <= 0:
                continue
            covered_s += overlap_s
            energy_mwh += (previous.power_mw + current.power_mw) * 0.5 * overlap_s / 3600.0
            if previous.direction == "discharging" or current.direction == "discharging":
                discharge_mah += (
                    (previous.current_ma + current.current_ma) * 0.5 * overlap_s / 3600.0
                )
        confidence = "medium"
        if event.duration_s < minimum_reliable_duration_s or covered_s < event.duration_s * 0.75:
            confidence = "low"
        span_rows.append(
            {
                "name": event.name,
                "phase": event.phase,
                "kind": event.kind,
                "start_elapsed_s": start - samples[0].uptime_s,
                "duration_s": event.duration_s,
                "covered_duration_s": covered_s,
                "energy_mwh": energy_mwh if covered_s > 0 else None,
                "discharge_mah": discharge_mah if covered_s > 0 else None,
                "average_power_mw": (
                    energy_mwh * 3600.0 / covered_s if covered_s > 0 else None
                ),
                "power_valid_for_consumption": covered_s > 0,
                "confidence": confidence,
                "source": event.source,
                "metadata": event.metadata,
            }
        )

    grouped: Dict[Tuple[str, str], Dict[str, object]] = {}
    for row in span_rows:
        key = (str(row["phase"]), str(row["name"]))
        aggregate = grouped.setdefault(
            key,
            {
                "phase": key[0],
                "name": key[1],
                "count": 0,
                "duration_s": 0.0,
                "covered_duration_s": 0.0,
                "energy_mwh": 0.0,
                "discharge_mah": 0.0,
                "confidence": "medium",
            },
        )
        aggregate["count"] = int(aggregate["count"]) + 1
        for field in ("duration_s", "covered_duration_s", "energy_mwh", "discharge_mah"):
            aggregate[field] = float(aggregate[field]) + float(row[field] or 0.0)
        if row["confidence"] == "low":
            aggregate["confidence"] = "low"
    aggregate_rows = list(grouped.values())
    for row in aggregate_rows:
        covered_s = float(row["covered_duration_s"])
        row["average_power_mw"] = (
            float(row["energy_mwh"]) * 3600.0 / covered_s if covered_s > 0 else None
        )
        row["power_valid_for_consumption"] = covered_s > 0
        if covered_s <= 0:
            row["energy_mwh"] = None
            row["discharge_mah"] = None
    aggregate_rows.sort(key=lambda item: float(item.get("energy_mwh") or 0.0), reverse=True)
    return {
        "available": bool(events),
        "event_count": len(events),
        "instant_count": sum(1 for event in events if not event.duration_s),
        "minimum_reliable_duration_s": minimum_reliable_duration_s,
        "spans": span_rows,
        "rows": aggregate_rows,
    }


def build_long_windows(
    samples: Sequence[Sample],
    contexts: Sequence[ContextSample],
    max_gap_s: float,
    width_s: float = 300.0,
) -> List[Dict[str, object]]:
    ordered_contexts = sorted(contexts, key=lambda item: item.uptime_s)
    context_uptimes = [item.uptime_s for item in ordered_contexts]
    windows: Dict[int, Dict[str, object]] = {}
    base_uptime = samples[0].uptime_s
    for previous, current, _ in sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=True,
    ):
        cursor = previous.uptime_s
        while cursor < current.uptime_s:
            window_index = int(max(0.0, cursor - base_uptime) // width_s)
            window_end_uptime = base_uptime + (window_index + 1) * width_s
            segment_end = min(current.uptime_s, window_end_uptime)
            segment_s = segment_end - cursor
            if segment_s <= 0:
                break
            midpoint = (cursor + segment_end) * 0.5
            context = _context_at(ordered_contexts, context_uptimes, midpoint)
            package = context.foreground_package if context and context.foreground_package else "unknown"
            row = windows.setdefault(
                window_index,
                {
                    "start_s": window_index * width_s,
                    "end_s": (window_index + 1) * width_s,
                    "covered_duration_s": 0.0,
                    "energy_mwh": 0.0,
                    "app_duration_s": {},
                },
            )
            row["covered_duration_s"] = float(row["covered_duration_s"]) + segment_s
            row["energy_mwh"] = float(row["energy_mwh"]) + (
                (previous.power_mw + current.power_mw) * 0.5 * segment_s / 3600.0
            )
            app_duration = row["app_duration_s"]
            if isinstance(app_duration, dict):
                app_duration[package] = float(app_duration.get(package, 0.0)) + segment_s
            cursor = segment_end

    result: List[Dict[str, object]] = []
    for index in sorted(windows):
        row = windows[index]
        duration_s = float(row["covered_duration_s"])
        energy_mwh = float(row["energy_mwh"])
        app_duration = row.pop("app_duration_s")
        dominant_app = None
        if isinstance(app_duration, dict) and app_duration:
            dominant_app = max(app_duration, key=lambda key: float(app_duration[key]))
        row["average_power_mw"] = energy_mwh * 3600.0 / duration_s if duration_s else None
        row["dominant_app"] = dominant_app
        result.append(row)
    return result


def _nearest_sample(
    samples: Sequence[Sample],
    uptimes: Sequence[float],
    uptime_s: float,
) -> Optional[Sample]:
    if not samples:
        return None
    index = bisect.bisect_left(uptimes, uptime_s)
    candidates = []
    if index < len(samples):
        candidates.append(samples[index])
    if index > 0:
        candidates.append(samples[index - 1])
    return min(candidates, key=lambda item: abs(item.uptime_s - uptime_s)) if candidates else None


def _window_power_metrics(
    samples: Sequence[Sample],
    intervals: Sequence[Tuple[Sample, Sample, float]],
    start_uptime_s: float,
    end_uptime_s: float,
) -> Dict[str, object]:
    covered_s = 0.0
    energy_mwh = 0.0
    for previous, current, _ in intervals:
        overlap_s = max(
            0.0,
            min(end_uptime_s, current.uptime_s) - max(start_uptime_s, previous.uptime_s),
        )
        if overlap_s <= 0:
            continue
        covered_s += overlap_s
        energy_mwh += (previous.power_mw + current.power_mw) * 0.5 * overlap_s / 3600.0
    return {
        "covered_duration_s": covered_s,
        "energy_mwh": energy_mwh if covered_s > 0 else None,
        "average_power_mw": energy_mwh * 3600.0 / covered_s if covered_s > 0 else None,
        "power_valid_for_consumption": covered_s > 0,
    }


def _snapshot_cadence(uptimes: Sequence[float], fallback: float) -> float:
    deltas = [
        current - previous
        for previous, current in zip(uptimes, uptimes[1:])
        if current > previous
    ]
    return statistics.median(deltas) if deltas else fallback


def analyze_system_activity(
    samples: Sequence[Sample],
    snapshots: Sequence[SystemSnapshot],
    max_gap_s: float,
    thermal_snapshots: Sequence[ThermalSnapshot] = (),
) -> Dict[str, object]:
    ordered = sorted(
        (
            item
            for item in snapshots
            if samples[0].uptime_s - max_gap_s <= item.uptime_s <= samples[-1].uptime_s + max_gap_s
        ),
        key=lambda item: item.uptime_s,
    )
    if not ordered:
        return {
            "available": False,
            "snapshot_count": 0,
            "top_processes": [],
            "hot_threads": [],
            "timeline": [],
            "activity_groups": {
                "available": False,
                "rows": [],
                "timeline": [],
                "runtime": [],
                "kernel": [],
            },
            "runtime_activity": [],
            "kernel_activity": [],
            "priority_activities": {
                "available": False,
                "active": False,
                "rows": [],
                "timeline": [],
                "monitored": [],
            },
        }

    sample_uptimes = [item.uptime_s for item in samples]
    powers_by_snapshot: List[Optional[float]] = []
    process_values: Dict[Tuple[str, str, str], Dict[int, float]] = {}
    process_meta: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    process_powers: Dict[Tuple[str, str, str], List[float]] = {}
    process_relative_power_scores: Dict[Tuple[str, str, str], List[float]] = {}
    thread_values: Dict[Tuple[str, str, str], List[float]] = {}
    thread_meta: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    timeline: List[Dict[str, object]] = []
    active_detections: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
    monitored: Dict[Tuple[str, str], Dict[str, object]] = {}
    classified_values: Dict[Tuple[str, str, str], Dict[int, float]] = {}
    classified_meta: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    classified_detections: Dict[Tuple[str, str, str], List[Dict[str, object]]] = {}
    classified_sources: Dict[Tuple[str, str, str], set[str]] = {}
    classified_entities: Dict[Tuple[str, str, str], Dict[str, set[object]]] = {}
    thread_snapshot_indices: List[int] = []

    ordered_thermal = sorted(
        (
            item
            for item in thermal_snapshots
            if samples[0].uptime_s - 60.0 <= item.uptime_s <= samples[-1].uptime_s + 60.0
        ),
        key=lambda item: item.uptime_s,
    )
    thermal_uptimes = [item.uptime_s for item in ordered_thermal]

    def thermal_context(uptime_s: float) -> Tuple[Optional[float], Optional[str]]:
        if not ordered_thermal:
            return None, None
        index = bisect.bisect_left(thermal_uptimes, uptime_s)
        candidates: List[ThermalSnapshot] = []
        if index < len(ordered_thermal):
            candidates.append(ordered_thermal[index])
        if index > 0:
            candidates.append(ordered_thermal[index - 1])
        nearest = min(candidates, key=lambda item: abs(item.uptime_s - uptime_s))
        if abs(nearest.uptime_s - uptime_s) > max(60.0, max_gap_s * 2.0):
            return None, None
        values = [
            (float(item["value_c"]), str(item.get("name") or "unknown"))
            for item in nearest.temperatures
            if isinstance(item.get("value_c"), (int, float))
        ]
        return max(values) if values else (None, None)

    for snapshot_index, snapshot in enumerate(ordered):
        sample = _nearest_sample(samples, sample_uptimes, snapshot.uptime_s)
        power = (
            sample.power_mw
            if sample is not None and is_consumption_power_sample(sample)
            else None
        )
        temperature_c, temperature_sensor = thermal_context(snapshot.uptime_s)
        powers_by_snapshot.append(power)
        category_cpu: Dict[str, float] = {}
        active_names: List[str] = []
        active_classified: List[str] = []
        snapshot_classified: Dict[Tuple[str, str, str], Dict[str, object]] = {}

        def add_classified(item: Dict[str, object], source: str) -> None:
            descriptor = None
            if not item.get("activity_kind"):
                descriptor = classify_thread_activity(
                    str(item.get("name") or item.get("command") or ""),
                    str(item.get("process") or item.get("command") or ""),
                )
            classified_item = {**item, **descriptor} if descriptor else item
            kind = classified_item.get("activity_kind")
            if not kind:
                return
            key = (
                str(kind),
                str(classified_item.get("subsystem") or "other"),
                str(classified_item.get("activity_label") or kind),
            )
            state = snapshot_classified.setdefault(
                key,
                {
                    "process_cpu_pct": 0.0,
                    "thread_cpu_pct": 0.0,
                    "active": False,
                    "pids": set(),
                    "tids": set(),
                    "processes": set(),
                    "threads": set(),
                    "sources": set(),
                },
            )
            cpu = float(classified_item.get("cpu_pct") or 0.0)
            state[f"{source}_cpu_pct"] = float(state[f"{source}_cpu_pct"]) + cpu
            state["active"] = bool(state["active"]) or bool(
                classified_item.get("classified_activity_active")
                or cpu >= 0.5
                or str(classified_item.get("state") or "").upper() in {"R", "D"}
            )
            state["sources"].add(source)  # type: ignore[union-attr]
            if isinstance(classified_item.get("pid"), int):
                state["pids"].add(int(classified_item["pid"]))  # type: ignore[union-attr]
            if isinstance(classified_item.get("tid"), int):
                state["tids"].add(int(classified_item["tid"]))  # type: ignore[union-attr]
            process_name = (
                classified_item.get("process")
                or classified_item.get("command")
                or classified_item.get("name")
            )
            if process_name:
                state["processes"].add(str(process_name))  # type: ignore[union-attr]
            if source == "thread" and classified_item.get("name"):
                state["threads"].add(str(classified_item["name"]))  # type: ignore[union-attr]
            classified_meta.setdefault(
                key,
                {
                    "kind": str(kind),
                    "family": str(classified_item.get("activity_family") or kind),
                    "label": str(classified_item.get("activity_label") or kind),
                    "domain": str(classified_item.get("activity_domain") or "system"),
                    "subsystem": str(classified_item.get("subsystem") or "other"),
                    "impact": classified_item.get("impact_hint"),
                },
            )

        for process in snapshot.processes:
            name = str(process.get("name") or process.get("command") or "unknown")
            user = str(process.get("user") or "unknown")
            category = str(process.get("category") or "other")
            key = (name, user, category)
            cpu = float(process.get("cpu_pct") or 0.0)
            process_values.setdefault(key, {})[snapshot_index] = cpu
            process_meta.setdefault(key, dict(process))
            relative_power_score = process.get("power_score")
            if isinstance(relative_power_score, (int, float)):
                process_relative_power_scores.setdefault(key, []).append(
                    float(relative_power_score)
                )
            if power is not None:
                process_powers.setdefault(key, []).append(power)
            category_cpu[category] = category_cpu.get(category, 0.0) + cpu
            add_classified(process, "process")

        if snapshot.threads:
            thread_snapshot_indices.append(snapshot_index)
        for thread in snapshot.threads:
            key = (
                str(thread.get("name") or "unknown"),
                str(thread.get("process") or "unknown"),
                str(thread.get("user") or "unknown"),
            )
            thread_values.setdefault(key, []).append(float(thread.get("cpu_pct") or 0.0))
            thread_meta.setdefault(key, dict(thread))
            add_classified(thread, "thread")

        classified_cpu = 0.0
        for key, state in snapshot_classified.items():
            cpu = max(
                float(state.get("process_cpu_pct") or 0.0),
                float(state.get("thread_cpu_pct") or 0.0),
            )
            classified_values.setdefault(key, {})[snapshot_index] = cpu
            classified_sources.setdefault(key, set()).update(state["sources"])  # type: ignore[arg-type]
            entities = classified_entities.setdefault(
                key,
                {"pids": set(), "tids": set(), "processes": set(), "threads": set()},
            )
            for field in ("pids", "tids", "processes", "threads"):
                entities[field].update(state[field])  # type: ignore[arg-type]
            if not state.get("active"):
                continue
            classified_cpu += cpu
            meta = classified_meta[key]
            active_classified.append(str(meta.get("label") or key[0]))
            classified_detections.setdefault(key, []).append(
                {
                    "uptime_s": snapshot.uptime_s,
                    "elapsed_s": snapshot.uptime_s - samples[0].uptime_s,
                    "cpu_pct": cpu,
                    "device_cpu_pct": sample.cpu_pct if sample else None,
                    "power_mw": power,
                    "temperature_c": temperature_c,
                    "temperature_sensor": temperature_sensor,
                    "pids": sorted(state["pids"]),
                    "tids": sorted(state["tids"]),
                    "processes": sorted(str(value) for value in state["processes"]),
                    "threads": sorted(str(value) for value in state["threads"]),
                    "sources": sorted(str(value) for value in state["sources"]),
                }
            )

        for watched in snapshot.watched_processes:
            watch_kind = str(watched.get("watch_kind") or "system_activity")
            watch_name = str(watched.get("watch_name") or watched.get("name") or "unknown")
            key = (watch_kind, watch_name)
            state = monitored.setdefault(
                key,
                {
                    "kind": watch_kind,
                    "name": watch_name,
                    "label": watched.get("watch_label"),
                    "impact": watched.get("watch_impact"),
                    "trigger": watched.get("watch_trigger"),
                    "seen_snapshots": 0,
                    "active_snapshots": 0,
                    "maximum_cpu_pct": 0.0,
                    "latest_active": False,
                    "pids": set(),
                },
            )
            state["seen_snapshots"] = int(state["seen_snapshots"]) + 1
            if isinstance(watched.get("pid"), int):
                state["pids"].add(int(watched["pid"]))  # type: ignore[union-attr]
            cpu = float(watched.get("cpu_pct") or 0.0)
            state["maximum_cpu_pct"] = max(float(state["maximum_cpu_pct"]), cpu)
            is_active = bool(watched.get("activity_active"))
            state["latest_active"] = is_active
            if not is_active:
                continue
            state["active_snapshots"] = int(state["active_snapshots"]) + 1
            active_names.append(str(watched.get("watch_label") or watch_name))
            active_detections.setdefault(key, []).append(
                {
                    "uptime_s": snapshot.uptime_s,
                    "elapsed_s": snapshot.uptime_s - samples[0].uptime_s,
                    "pid": watched.get("pid"),
                    "cpu_pct": watched.get("cpu_pct"),
                    "power_mw": power,
                    "state": watched.get("state"),
                    "policy": watched.get("policy"),
                    "command": watched.get("command"),
                }
            )

        timeline.append(
            {
                "elapsed_s": snapshot.uptime_s - samples[0].uptime_s,
                "uptime_s": snapshot.uptime_s,
                "power_mw": power,
                "visible_cpu_pct": sum(category_cpu.values()),
                "background_cpu_pct": sum(
                    float(item.get("cpu_pct") or 0.0)
                    for item in snapshot.processes
                    if str(item.get("policy") or "").lower() in {"bg", "background"}
                ),
                "kernel_cpu_pct": category_cpu.get("kernel", 0.0),
                "android_system_cpu_pct": category_cpu.get("android_system", 0.0)
                + category_cpu.get("native_system", 0.0)
                + category_cpu.get("vendor_service", 0.0),
                "application_cpu_pct": category_cpu.get("application", 0.0),
                "priority_cpu_pct": sum(
                    float(item.get("cpu_pct") or 0.0)
                    for item in snapshot.watched_processes
                    if item.get("activity_active")
                ),
                "active_priority": active_names,
                "classified_activity_cpu_pct": classified_cpu,
                "active_classified": active_classified,
                "temperature_c": temperature_c,
                "process_count": snapshot.process_count,
                "thread_count": snapshot.thread_count,
                "collection_ms": snapshot.collection_ms,
            }
        )

    valid_power_indices = [index for index, value in enumerate(powers_by_snapshot) if value is not None]
    top_processes: List[Dict[str, object]] = []
    for key, values in process_values.items():
        meta = dict(process_meta[key])
        vector = [values.get(index, 0.0) for index in range(len(ordered))]
        visible = list(values.values())
        paired_cpu = [vector[index] for index in valid_power_indices]
        paired_power = [float(powers_by_snapshot[index]) for index in valid_power_indices]
        meta.update(
            {
                "seen_snapshots": len(visible),
                "average_cpu_pct": statistics.fmean(vector),
                "average_when_visible_cpu_pct": statistics.fmean(visible),
                "maximum_cpu_pct": max(visible),
                "power_correlation": _pearson(paired_cpu, paired_power),
                "average_power_when_visible_mw": (
                    statistics.fmean(process_powers.get(key, []))
                    if process_powers.get(key)
                    else None
                ),
                "average_relative_power_score": (
                    statistics.fmean(process_relative_power_scores[key])
                    if process_relative_power_scores.get(key)
                    else None
                ),
                "maximum_relative_power_score": (
                    max(process_relative_power_scores[key])
                    if process_relative_power_scores.get(key)
                    else None
                ),
            }
        )
        top_processes.append(meta)
    top_processes.sort(
        key=lambda item: (
            float(item.get("average_cpu_pct") or 0.0),
            float(item.get("maximum_cpu_pct") or 0.0),
        ),
        reverse=True,
    )

    hot_threads: List[Dict[str, object]] = []
    for key, values in thread_values.items():
        meta = dict(thread_meta[key])
        meta.update(
            {
                "seen_snapshots": len(values),
                "average_when_visible_cpu_pct": statistics.fmean(values),
                "maximum_cpu_pct": max(values),
            }
        )
        hot_threads.append(meta)
    hot_threads.sort(key=lambda item: float(item.get("maximum_cpu_pct") or 0.0), reverse=True)

    cadence = _snapshot_cadence([item.uptime_s for item in ordered], 10.0)
    intervals = sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=True,
    )
    total_metrics = _window_power_metrics(
        samples,
        intervals,
        samples[0].uptime_s,
        samples[-1].uptime_s,
    )
    baseline_power = total_metrics["average_power_mw"]
    for process in top_processes:
        visible_power = process.get("average_power_when_visible_mw")
        process["power_delta_when_visible_mw"] = (
            float(visible_power) - float(baseline_power)
            if isinstance(visible_power, (int, float))
            and isinstance(baseline_power, (int, float))
            else None
        )
    activity_rows: List[Dict[str, object]] = []
    activity_timeline: List[Dict[str, object]] = []
    for key, detections in active_detections.items():
        detections.sort(key=lambda item: float(item["uptime_s"]))
        windows: List[List[Dict[str, object]]] = []
        for detection in detections:
            if not windows or float(detection["uptime_s"]) - float(windows[-1][-1]["uptime_s"]) > max(
                15.0,
                cadence * 2.5,
            ):
                windows.append([detection])
            else:
                windows[-1].append(detection)
        covered_s = 0.0
        energy_mwh = 0.0
        estimated_s = 0.0
        activity_windows: List[Dict[str, object]] = []
        for window in windows:
            start = max(samples[0].uptime_s, float(window[0]["uptime_s"]) - cadence * 0.5)
            end = min(samples[-1].uptime_s, float(window[-1]["uptime_s"]) + cadence * 0.5)
            metrics = _window_power_metrics(samples, intervals, start, end)
            covered_s += float(metrics.get("covered_duration_s") or 0.0)
            energy_mwh += float(metrics.get("energy_mwh") or 0.0)
            estimated_s += max(0.0, end - start)
            activity_windows.append(
                {
                    "start_uptime_s": start,
                    "end_uptime_s": end,
                    "start_elapsed_s": start - samples[0].uptime_s,
                    "end_elapsed_s": end - samples[0].uptime_s,
                    "duration_s": max(0.0, end - start),
                    **metrics,
                }
            )
        average_power = energy_mwh * 3600.0 / covered_s if covered_s > 0 else None
        cpu_values = [
            float(item["cpu_pct"])
            for item in detections
            if isinstance(item.get("cpu_pct"), (int, float))
        ]
        monitored_state = monitored[key]
        row = {
            "kind": key[0],
            "name": key[1],
            "label": monitored_state.get("label"),
            "impact": monitored_state.get("impact"),
            "detection_count": len(detections),
            "window_count": len(windows),
            "first_elapsed_s": float(detections[0]["elapsed_s"]),
            "last_elapsed_s": float(detections[-1]["elapsed_s"]),
            "estimated_duration_s": estimated_s,
            "covered_duration_s": covered_s,
            "energy_mwh": energy_mwh if covered_s > 0 else None,
            "average_power_mw": average_power,
            "baseline_power_mw": baseline_power,
            "power_delta_mw": (
                average_power - float(baseline_power)
                if average_power is not None and isinstance(baseline_power, (int, float))
                else None
            ),
            "excess_energy_mwh": (
                max(0.0, average_power - float(baseline_power)) * covered_s / 3600.0
                if average_power is not None and isinstance(baseline_power, (int, float))
                else None
            ),
            "power_valid_for_consumption": covered_s > 0,
            "average_cpu_pct": statistics.fmean(cpu_values) if cpu_values else None,
            "maximum_cpu_pct": max(cpu_values) if cpu_values else None,
            "windows": activity_windows,
            "confidence": "medium" if len(detections) >= 2 and covered_s >= cadence else "low",
            "source": "periodic whole-system top/ps snapshots; power association is temporal, not causal",
        }
        activity_rows.append(row)
        for detection in detections:
            activity_timeline.append(
                {
                    **detection,
                    "kind": key[0],
                    "name": key[1],
                    "label": monitored_state.get("label"),
                }
            )
    activity_rows.sort(
        key=lambda item: (
            float(item.get("excess_energy_mwh") or 0.0),
            float(item.get("maximum_cpu_pct") or 0.0),
        ),
        reverse=True,
    )
    activity_timeline.sort(key=lambda item: float(item.get("elapsed_s") or 0.0))

    thread_cadence = _snapshot_cadence(
        [ordered[index].uptime_s for index in thread_snapshot_indices],
        max(cadence, 30.0),
    )
    classified_rows: List[Dict[str, object]] = []
    classified_timeline: List[Dict[str, object]] = []
    for key, detections in classified_detections.items():
        detections.sort(key=lambda item: float(item["uptime_s"]))
        sources = classified_sources.get(key, set())
        observation_cadence = thread_cadence if sources == {"thread"} else cadence
        observation_indices = (
            thread_snapshot_indices if sources == {"thread"} else list(range(len(ordered)))
        )
        values = classified_values.get(key, {})
        cpu_vector = [float(values.get(index, 0.0)) for index in observation_indices]
        paired = [
            (float(values.get(index, 0.0)), float(powers_by_snapshot[index]))
            for index in observation_indices
            if powers_by_snapshot[index] is not None
        ]
        windows: List[List[Dict[str, object]]] = []
        for detection in detections:
            if not windows or float(detection["uptime_s"]) - float(windows[-1][-1]["uptime_s"]) > max(
                15.0,
                observation_cadence * 2.5,
            ):
                windows.append([detection])
            else:
                windows[-1].append(detection)

        covered_s = 0.0
        energy_mwh = 0.0
        estimated_s = 0.0
        window_rows: List[Dict[str, object]] = []
        for window in windows:
            start = max(
                samples[0].uptime_s,
                float(window[0]["uptime_s"]) - observation_cadence * 0.5,
            )
            end = min(
                samples[-1].uptime_s,
                float(window[-1]["uptime_s"]) + observation_cadence * 0.5,
            )
            metrics = _window_power_metrics(samples, intervals, start, end)
            covered_s += float(metrics.get("covered_duration_s") or 0.0)
            energy_mwh += float(metrics.get("energy_mwh") or 0.0)
            estimated_s += max(0.0, end - start)
            window_rows.append(
                {
                    "start_uptime_s": start,
                    "end_uptime_s": end,
                    "start_elapsed_s": start - samples[0].uptime_s,
                    "end_elapsed_s": end - samples[0].uptime_s,
                    "duration_s": max(0.0, end - start),
                    **metrics,
                }
            )

        cpu_values = [float(item.get("cpu_pct") or 0.0) for item in detections]
        device_cpu_values = [
            float(item["device_cpu_pct"])
            for item in detections
            if isinstance(item.get("device_cpu_pct"), (int, float))
        ]
        temperature_values = [
            float(item["temperature_c"])
            for item in detections
            if isinstance(item.get("temperature_c"), (int, float))
        ]
        average_power = energy_mwh * 3600.0 / covered_s if covered_s > 0 else None
        meta = classified_meta[key]
        entities = classified_entities.get(
            key,
            {"pids": set(), "tids": set(), "processes": set(), "threads": set()},
        )
        row = {
            **meta,
            "detection_count": len(detections),
            "window_count": len(windows),
            "observed_snapshot_count": len(observation_indices),
            "first_elapsed_s": float(detections[0]["elapsed_s"]),
            "last_elapsed_s": float(detections[-1]["elapsed_s"]),
            "estimated_duration_s": estimated_s,
            "covered_duration_s": covered_s,
            "energy_mwh": energy_mwh if covered_s > 0 else None,
            "average_power_mw": average_power,
            "baseline_power_mw": baseline_power,
            "power_delta_mw": (
                average_power - float(baseline_power)
                if average_power is not None and isinstance(baseline_power, (int, float))
                else None
            ),
            "excess_energy_mwh": (
                max(0.0, average_power - float(baseline_power)) * covered_s / 3600.0
                if average_power is not None and isinstance(baseline_power, (int, float))
                else None
            ),
            "power_valid_for_consumption": covered_s > 0,
            "average_cpu_pct": statistics.fmean(cpu_values) if cpu_values else None,
            "maximum_cpu_pct": max(cpu_values) if cpu_values else None,
            "session_average_cpu_pct": statistics.fmean(cpu_vector) if cpu_vector else None,
            "average_device_cpu_pct": statistics.fmean(device_cpu_values) if device_cpu_values else None,
            "power_correlation": _pearson(
                [item[0] for item in paired],
                [item[1] for item in paired],
            ),
            "average_temperature_c": (
                statistics.fmean(temperature_values) if temperature_values else None
            ),
            "maximum_temperature_c": max(temperature_values) if temperature_values else None,
            "pids": sorted(int(value) for value in entities["pids"] if isinstance(value, int)),
            "tids": sorted(int(value) for value in entities["tids"] if isinstance(value, int)),
            "processes": sorted(str(value) for value in entities["processes"])[:12],
            "threads": sorted(str(value) for value in entities["threads"])[:12],
            "sources": sorted(sources),
            "observation_cadence_s": observation_cadence,
            "windows": window_rows,
            "confidence": (
                "medium"
                if len(detections) >= 2 and covered_s >= observation_cadence
                else "low"
            ),
            "source": (
                "periodic top process/thread snapshots; power and temperature associations are temporal, not causal"
            ),
        }
        classified_rows.append(row)
        for detection in detections:
            classified_timeline.append(
                {
                    **detection,
                    "kind": meta.get("kind"),
                    "family": meta.get("family"),
                    "label": meta.get("label"),
                    "domain": meta.get("domain"),
                    "subsystem": meta.get("subsystem"),
                }
            )
    classified_rows.sort(
        key=lambda item: (
            float(item.get("excess_energy_mwh") or 0.0),
            float(item.get("maximum_cpu_pct") or 0.0),
            int(item.get("detection_count") or 0),
        ),
        reverse=True,
    )
    classified_timeline.sort(key=lambda item: float(item.get("elapsed_s") or 0.0))
    runtime_activity = [item for item in classified_rows if item.get("domain") == "runtime"]
    kernel_activity = [item for item in classified_rows if item.get("domain") == "kernel"]

    latest_active_keys = {
        (
            str(item.get("watch_kind") or "system_activity"),
            str(item.get("watch_name") or item.get("name") or "unknown"),
        )
        for item in ordered[-1].watched_processes
        if item.get("activity_active")
    }
    monitored_rows = []
    for key, value in monitored.items():
        row = dict(value)
        row["latest_active"] = key in latest_active_keys
        pids = row.pop("pids")
        row["pids"] = sorted(pids) if isinstance(pids, set) else []
        monitored_rows.append(row)
    monitored_rows.sort(
        key=lambda item: (
            not bool(item.get("latest_active")),
            -int(item.get("active_snapshots") or 0),
            str(item.get("name") or ""),
        )
    )
    process_counts = [item.process_count for item in ordered if item.process_count is not None]
    thread_counts = [item.thread_count for item in ordered if item.thread_count is not None]
    collection_times = [
        float(item.collection_ms)
        for item in ordered
        if isinstance(item.collection_ms, (int, float))
    ]
    return {
        "available": True,
        "power_valid_for_consumption": bool(intervals),
        "snapshot_count": len(ordered),
        "thread_snapshot_count": sum(1 for item in ordered if item.threads),
        "average_process_count": statistics.fmean(process_counts) if process_counts else None,
        "maximum_process_count": max(process_counts) if process_counts else None,
        "average_thread_count": statistics.fmean(thread_counts) if thread_counts else None,
        "maximum_thread_count": max(thread_counts) if thread_counts else None,
        "average_collection_ms": statistics.fmean(collection_times) if collection_times else None,
        "maximum_collection_ms": max(collection_times) if collection_times else None,
        "top_processes": top_processes[:40],
        "hot_threads": hot_threads[:40],
        "timeline": timeline,
        "activity_groups": {
            "available": bool(classified_rows),
            "rows": classified_rows,
            "timeline": classified_timeline,
            "runtime": runtime_activity,
            "kernel": kernel_activity,
            "cadence_s": cadence,
            "thread_cadence_s": thread_cadence,
            "limitations": (
                "Activity groups are inferred from process/thread names in periodic top snapshots. "
                "They are sampling evidence rather than continuous scheduler traces or rail-level energy attribution."
            ),
        },
        "runtime_activity": runtime_activity,
        "kernel_activity": kernel_activity,
        "priority_activities": {
            "available": bool(monitored_rows),
            "active": bool(activity_rows),
            "rows": activity_rows,
            "timeline": activity_timeline,
            "monitored": monitored_rows,
            "latest_active": [
                item.get("label") or item.get("name")
                for item in monitored_rows
                if item.get("latest_active")
            ],
            "cadence_s": cadence,
        },
        "limitations": (
            "Process and thread CPU values are periodic top snapshots, not continuous scheduler traces. "
            "Power deltas show time association with whole-device battery output and must not be read as causal attribution."
        ),
    }


THERMAL_STATUS_LABELS = {
    0: "none",
    1: "light",
    2: "moderate",
    3: "severe",
    4: "critical",
    5: "emergency",
    6: "shutdown",
}

# Android TemperatureType values 6-8 are battery-current-limit (BCL)
# telemetry rather than temperature sensors. Their status values describe BCL
# conditions (for example low voltage), so folding them into ThermalService
# severity can incorrectly turn a vbat status of 6 into a "thermal shutdown".
BCL_SENSOR_UNITS = {
    6: "V",  # BCL_VOLTAGE
    7: "A",  # BCL_CURRENT
    8: "%",  # BCL_PERCENTAGE
}


def _thermal_sensor_type(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _contributes_to_thermal_status(sensor_type: object) -> bool:
    parsed = _thermal_sensor_type(sensor_type)
    return parsed not in BCL_SENSOR_UNITS


def _thermal_sensor_unit(sensor_type: object) -> str:
    parsed = _thermal_sensor_type(sensor_type)
    return BCL_SENSOR_UNITS.get(parsed, "°C")


def analyze_thermal_history(
    samples: Sequence[Sample],
    snapshots: Sequence[ThermalSnapshot],
) -> Dict[str, object]:
    ordered = sorted(snapshots, key=lambda item: item.uptime_s)
    if not ordered:
        return {
            "available": False,
            "snapshot_count": 0,
            "timeline": [],
            "sensors": [],
            "cooling_devices": [],
        }
    sample_uptimes = [item.uptime_s for item in samples]
    sensor_values: Dict[str, List[float]] = {}
    sensor_statuses: Dict[str, List[int]] = {}
    sensor_types: Dict[str, object] = {}
    sensor_powers: Dict[str, List[float]] = {}
    sensor_power_values: Dict[str, List[float]] = {}
    threshold_map: Dict[str, Dict[str, object]] = {}
    cooling: Dict[str, Dict[str, object]] = {}
    timeline: List[Dict[str, object]] = []
    statuses: List[int] = []
    for snapshot in ordered:
        sample = _nearest_sample(samples, sample_uptimes, snapshot.uptime_s)
        power = (
            sample.power_mw
            if sample is not None and is_consumption_power_sample(sample)
            else None
        )
        sensors: Dict[str, float] = {}
        sensor_severity: Dict[str, int] = {}
        if snapshot.status is not None:
            statuses.append(snapshot.status)
        for threshold in snapshot.thresholds:
            name = str(threshold.get("name") or "unknown")
            threshold_map[name] = threshold
        for item in snapshot.temperatures:
            name = str(item.get("name") or "unknown")
            if not isinstance(item.get("value_c"), (int, float)):
                continue
            value = float(item["value_c"])
            raw_severity = item.get("status")
            severity = (
                int(raw_severity)
                if item.get("status_available") is not False
                and isinstance(raw_severity, (int, float))
                and not isinstance(raw_severity, bool)
                else None
            )
            sensor_type = item.get("type")
            sensors[name] = value
            if severity is not None:
                sensor_severity[name] = severity
            sensor_values.setdefault(name, []).append(value)
            if severity is not None:
                sensor_statuses.setdefault(name, []).append(severity)
            if sensor_type is not None:
                sensor_types[name] = sensor_type
            if power is not None:
                sensor_powers.setdefault(name, []).append(power)
                sensor_power_values.setdefault(name, []).append(value)
        active_cooling = []
        for item in snapshot.cooling_devices:
            name = str(item.get("name") or "unknown")
            value = float(item.get("value") or 0.0)
            row = cooling.setdefault(
                name,
                {
                    "name": name,
                    "type": item.get("type"),
                    "maximum_value": 0.0,
                    "active_snapshots": 0,
                },
            )
            row["maximum_value"] = max(float(row["maximum_value"]), value)
            if value > 0:
                row["active_snapshots"] = int(row["active_snapshots"]) + 1
                active_cooling.append(name)
        timeline.append(
            {
                "elapsed_s": snapshot.uptime_s - samples[0].uptime_s,
                "uptime_s": snapshot.uptime_s,
                "status": snapshot.status,
                "status_label": THERMAL_STATUS_LABELS.get(snapshot.status, "unknown"),
                "sensors": sensors,
                "sensor_status": sensor_severity,
                "active_cooling": active_cooling,
                "power_mw": power,
            }
        )

    sensors_rows: List[Dict[str, object]] = []
    for name, values in sensor_values.items():
        threshold = threshold_map.get(name, {})
        sensor_type = sensor_types.get(name)
        if sensor_type is None and isinstance(threshold, dict):
            sensor_type = threshold.get("type")
        contributes_to_thermal_status = _contributes_to_thermal_status(sensor_type)
        known_sensor_statuses = sensor_statuses.get(name, [])
        maximum_sensor_status = max(known_sensor_statuses) if known_sensor_statuses else None
        hot = threshold.get("hot_c", []) if isinstance(threshold, dict) else []
        first_hot = next(
            (float(value) for value in hot if isinstance(value, (int, float))),
            None,
        )
        powers = sensor_powers.get(name, [])
        paired_values = sensor_power_values.get(name, [])
        sensors_rows.append(
            {
                "name": name,
                "type": sensor_type,
                "unit": _thermal_sensor_unit(sensor_type),
                "contributes_to_thermal_status": contributes_to_thermal_status,
                "minimum_value": min(values),
                "average_value": statistics.fmean(values),
                "maximum_value": max(values),
                "minimum_c": min(values),
                "average_c": statistics.fmean(values),
                "maximum_c": max(values),
                "maximum_status": maximum_sensor_status,
                "maximum_status_label": (
                    THERMAL_STATUS_LABELS.get(maximum_sensor_status, "unknown")
                    if contributes_to_thermal_status and maximum_sensor_status is not None
                    else "unknown"
                    if contributes_to_thermal_status
                    else "not_applicable"
                ),
                "first_hot_threshold_c": first_hot,
                "margin_to_first_threshold_c": first_hot - max(values) if first_hot is not None else None,
                "power_correlation": _pearson(paired_values, powers),
                "thresholds": threshold,
            }
        )
    sensors_rows.sort(
        key=lambda item: (
            bool(item.get("contributes_to_thermal_status")),
            float(item.get("maximum_value") or 0.0),
        ),
        reverse=True,
    )
    thermal_sensor_rows = [
        item for item in sensors_rows if bool(item.get("contributes_to_thermal_status"))
    ]
    status_candidates = statuses + [
        int(item["maximum_status"])
        for item in thermal_sensor_rows
        if isinstance(item.get("maximum_status"), (int, float))
    ]
    maximum_status = max(status_candidates) if status_candidates else None
    severity_available = maximum_status is not None
    latest_status = ordered[-1].status
    collection_times = [
        float(item.collection_ms)
        for item in ordered
        if isinstance(item.collection_ms, (int, float))
    ]
    cooling_rows = sorted(
        cooling.values(),
        key=lambda item: (int(item.get("active_snapshots") or 0), float(item.get("maximum_value") or 0.0)),
        reverse=True,
    )
    return {
        "available": True,
        "snapshot_count": len(ordered),
        "hal_ready": ordered[-1].hal_ready,
        "latest_status": latest_status,
        "latest_status_label": THERMAL_STATUS_LABELS.get(latest_status, "unknown"),
        "maximum_status": maximum_status,
        "maximum_status_label": THERMAL_STATUS_LABELS.get(maximum_status, "unknown"),
        "severity_available": severity_available,
        "throttling_observed": maximum_status > 0 if maximum_status is not None else None,
        "hottest_sensor": thermal_sensor_rows[0] if thermal_sensor_rows else None,
        "sensors": sensors_rows,
        "cooling_devices": cooling_rows,
        "timeline": timeline,
        "headroom_thresholds": ordered[-1].headroom_thresholds,
        "average_collection_ms": statistics.fmean(collection_times) if collection_times else None,
        "maximum_collection_ms": max(collection_times) if collection_times else None,
        "limitations": (
            "ThermalService exposes observed temperatures, severity, cooling states and static thresholds. "
            "BCL voltage/current/percentage sensors are retained as auxiliary telemetry but excluded from "
            "thermal severity aggregation. "
            "The OEM's internal thermal decision algorithm and all vendor configuration semantics remain opaque."
        ),
    }


def analyze_brightness_throttling(
    samples: Sequence[Sample],
    contexts: Sequence[ContextSample],
    snapshots: Sequence[ThermalSnapshot],
) -> Dict[str, object]:
    ordered = sorted(snapshots, key=lambda item: item.uptime_s)
    if not ordered:
        return {
            "available": False,
            "timeline": [],
            "points": [],
            "events": [],
            "point_count": 0,
            "event_count": 0,
        }

    sample_uptimes = [item.uptime_s for item in samples]
    ordered_contexts = sorted(contexts, key=lambda item: item.uptime_s)
    context_uptimes = [item.uptime_s for item in ordered_contexts]
    session_start = (
        samples[0].uptime_s
        if samples
        else ordered_contexts[0].uptime_s
        if ordered_contexts
        else ordered[0].uptime_s
    )

    def context_for(uptime_s: float) -> Optional[ContextSample]:
        if not ordered_contexts:
            return None
        index = bisect.bisect_right(context_uptimes, uptime_s) - 1
        return ordered_contexts[max(0, index)]

    def normalized(
        value: object,
        minimum: object = None,
        maximum: object = None,
    ) -> Optional[float]:
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        parsed = float(value)
        lower = (
            float(minimum)
            if isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and math.isfinite(float(minimum))
            else None
        )
        upper = (
            float(maximum)
            if isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and math.isfinite(float(maximum))
            else None
        )
        if upper is not None and upper > 1.001:
            effective_lower = lower if lower is not None else 0.0
            if effective_lower <= parsed <= upper and upper > effective_lower:
                return max(
                    0.0,
                    min(1.0, (parsed - effective_lower) / (upper - effective_lower)),
                )
            # Some vendor fields remain normalized even when the surrounding
            # DisplayManager state exposes a raw 0..N brightness range.  Keep
            # positive normalized values, but reject zero below a non-zero raw
            # minimum so an inactive HBM controller's placeholder cannot win.
            if 0.0 < parsed <= 1.001:
                return parsed
            return None
        return parsed if 0.0 <= parsed <= 1.001 else None

    def sensor_value(snapshot: ThermalSnapshot, name_pattern: str) -> Optional[float]:
        values = [
            float(item["value_c"])
            for item in snapshot.temperatures
            if isinstance(item, dict)
            and re.search(name_pattern, str(item.get("name") or ""), re.I)
            and isinstance(item.get("value_c"), (int, float))
        ]
        return max(values) if values else None

    baseline_setting: Optional[float] = None
    observations: List[Dict[str, object]] = []
    cooling_exposed = False
    display_exposed = False
    vendor_thermal_exposed = False
    vendor_thermal_providers: List[str] = []
    snapshot_intervals = [
        current.uptime_s - previous.uptime_s
        for previous, current in zip(ordered, ordered[1:])
        if 0 < current.uptime_s - previous.uptime_s <= 120.0
    ]
    median_snapshot_interval_s = (
        statistics.median(snapshot_intervals) if snapshot_intervals else 5.0
    )
    last_vendor_state: Dict[str, object] = {}
    last_vendor_observed_uptime_s: Optional[float] = None
    for snapshot in ordered:
        display = (
            snapshot.display_brightness
            if isinstance(snapshot.display_brightness, dict)
            else {}
        )
        display_exposed = display_exposed or bool(display.get("available"))
        relevant_cooling = [
            item
            for item in snapshot.cooling_devices
            if isinstance(item, dict)
            and re.search(r"lcd|backlight|display|screen", str(item.get("name") or ""), re.I)
        ]
        cooling_exposed = cooling_exposed or bool(relevant_cooling)
        vendor_active_raw = display.get("vendor_thermal_active")
        vendor_active = (
            vendor_active_raw if isinstance(vendor_active_raw, bool) else None
        )
        vendor_provider = str(display.get("vendor_thermal_provider") or "").strip() or None
        vendor_level = (
            int(display["vendor_thermal_level"])
            if isinstance(display.get("vendor_thermal_level"), (int, float))
            and not isinstance(display.get("vendor_thermal_level"), bool)
            and math.isfinite(float(display["vendor_thermal_level"]))
            else None
        )
        vendor_limit_nits = (
            float(display["vendor_thermal_limit_nits"])
            if isinstance(display.get("vendor_thermal_limit_nits"), (int, float))
            and not isinstance(display.get("vendor_thermal_limit_nits"), bool)
            and math.isfinite(float(display["vendor_thermal_limit_nits"]))
            else None
        )
        vendor_temperature_c = (
            float(display["vendor_thermal_temperature_c"])
            if isinstance(display.get("vendor_thermal_temperature_c"), (int, float))
            and not isinstance(display.get("vendor_thermal_temperature_c"), bool)
            and math.isfinite(float(display["vendor_thermal_temperature_c"]))
            else None
        )
        raw_candidate_caps = display.get("vendor_thermal_candidate_caps_nits")
        vendor_candidate_caps = (
            {
                str(level): float(cap)
                for level, cap in raw_candidate_caps.items()
                if isinstance(cap, (int, float))
                and not isinstance(cap, bool)
                and math.isfinite(float(cap))
            }
            if isinstance(raw_candidate_caps, dict)
            else {}
        )
        vendor_state_exposed = bool(
            vendor_provider
            or vendor_candidate_caps
            or any(
                value is not None
                for value in (
                    vendor_active,
                    vendor_level,
                    vendor_limit_nits,
                    vendor_temperature_c,
                )
            )
        )
        vendor_state_carried_forward = False
        vendor_last_known_active: Optional[bool] = None
        if vendor_state_exposed:
            last_vendor_state = {
                "provider": vendor_provider,
                "active": vendor_active,
                "level": vendor_level,
                "limit_nits": vendor_limit_nits,
                "temperature_c": vendor_temperature_c,
                "candidate_caps_nits": dict(vendor_candidate_caps),
            }
            last_vendor_observed_uptime_s = snapshot.uptime_s
        elif last_vendor_state:
            vendor_state_carried_forward = True
            vendor_provider = str(last_vendor_state.get("provider") or "").strip() or None
            vendor_last_known_active = (
                last_vendor_state.get("active")
                if isinstance(last_vendor_state.get("active"), bool)
                else None
            )
            vendor_level = (
                int(last_vendor_state["level"])
                if isinstance(last_vendor_state.get("level"), (int, float))
                else None
            )
            vendor_limit_nits = (
                float(last_vendor_state["limit_nits"])
                if isinstance(last_vendor_state.get("limit_nits"), (int, float))
                else None
            )
            vendor_temperature_c = (
                float(last_vendor_state["temperature_c"])
                if isinstance(last_vendor_state.get("temperature_c"), (int, float))
                else None
            )
            cached_caps = last_vendor_state.get("candidate_caps_nits")
            vendor_candidate_caps = dict(cached_caps) if isinstance(cached_caps, dict) else {}
        vendor_observed_age_s = (
            max(0.0, snapshot.uptime_s - last_vendor_observed_uptime_s)
            if last_vendor_observed_uptime_s is not None
            else None
        )
        vendor_stale_after_s = (
            max(15.0, median_snapshot_interval_s * 3.0)
            if vendor_last_known_active is True
            else max(35.0, median_snapshot_interval_s * 7.0)
        )
        vendor_state_stale = bool(
            vendor_state_carried_forward
            and isinstance(vendor_observed_age_s, (int, float))
            and vendor_observed_age_s > vendor_stale_after_s
        )
        vendor_thermal_exposed = vendor_thermal_exposed or vendor_state_exposed or bool(
            last_vendor_state
        )
        if vendor_provider and vendor_provider not in vendor_thermal_providers:
            vendor_thermal_providers.append(vendor_provider)

        if not display and not relevant_cooling and not last_vendor_state:
            continue

        context = context_for(snapshot.uptime_s)
        setting_raw = display.get("setting_raw")
        if not isinstance(setting_raw, (int, float)) and context is not None:
            setting_raw = context.brightness_raw
        if isinstance(setting_raw, (int, float)) and baseline_setting is None:
            baseline_setting = float(setting_raw)
        brightness_minimum = display.get("brightness_minimum")
        brightness_maximum = display.get("brightness_maximum")

        def display_normalized(value: object) -> Optional[float]:
            return normalized(value, brightness_minimum, brightness_maximum)

        setting_float = normalized(display.get("setting_float"))
        if setting_float is None:
            setting_float = display_normalized(setting_raw)
        requested = next(
            (
                value
                for value in (
                    display_normalized(display.get("current_screen_brightness")),
                    display_normalized(display.get("last_user_set_brightness")),
                    setting_float,
                    display_normalized(display.get("hbm_unthrottled_brightness")),
                )
                if value is not None
            ),
            None,
        )
        effective, effective_source = next(
            (
                (value, source)
                for value, source in (
                    (
                        display_normalized(display.get("screen_brightness")),
                        "Display Power Controller",
                    ),
                    (
                        display_normalized(display.get("adjusted_brightness")),
                        "BrightnessInfo adjustedBrightness",
                    ),
                    (
                        display_normalized(display.get("hbm_brightness")),
                        "HighBrightnessModeController",
                    ),
                    (
                        display_normalized(display.get("cached_brightness")),
                        "BrightnessInfo brightness",
                    ),
                )
                if value is not None
            ),
            (None, None),
        )
        thermal_cap = normalized(display.get("thermal_cap"))
        thermal_applied = display.get("thermal_applied") is True
        thermal_status = int(display.get("thermal_status") or 0)
        maximum_reason = (
            int(display["brightness_max_reason"])
            if isinstance(display.get("brightness_max_reason"), (int, float))
            else None
        )
        cooling_value = max(
            (float(item.get("value") or 0.0) for item in relevant_cooling),
            default=0.0,
        )
        if (
            thermal_cap is not None
            and requested is not None
            and thermal_applied
            and thermal_cap < requested
            and (effective is None or effective > thermal_cap)
        ):
            effective = thermal_cap
            effective_source = "DisplayManager thermal cap estimate"

        screen_text = str(
            display.get("screen_state")
            or (context.screen_state if context is not None else "")
            or ""
        ).strip().upper()
        screen_explicitly_off = bool(
            screen_text
            and (screen_text in {"OFF", "ASLEEP"} or screen_text.startswith("DOZE"))
        )
        cap_restricts = bool(
            thermal_cap is not None
            and requested is not None
            and thermal_cap < requested - 0.005
        )
        effective_drop = (
            max(0.0, requested - effective)
            if requested is not None and effective is not None
            else None
        )
        effective_drop_pct = (
            effective_drop / requested * 100.0
            if effective_drop is not None and requested and requested > 0
            else None
        )
        thermal_reason = maximum_reason == 1 or str(
            display.get("hbm_throttling_reason") or ""
        ).lower() == "thermal"
        framework_confirmed = bool(
            not screen_explicitly_off
            and (
                (
                    thermal_reason
                    and (cap_restricts or (effective_drop or 0.0) >= 0.005)
                )
                or (thermal_applied and cap_restricts)
            )
        )
        cooling_confirmed = bool(
            not screen_explicitly_off
            and cooling_value > 0
            and (effective_drop or 0.0) >= 0.005
        )
        # The OEM's candidate table, current level and limit value are descriptive
        # runtime fields.  Only an explicit active=true flag proves that the OEM
        # brightness clamp is engaged.
        vendor_confirmed = vendor_active is True and vendor_state_exposed
        confirmed = framework_confirmed or cooling_confirmed or vendor_confirmed
        suspected = bool(
            not screen_explicitly_off
            and not confirmed
            and (
                cooling_value > 0
                or (thermal_applied and cap_restricts)
                or (thermal_status > 0 and cap_restricts)
                or (thermal_reason and cap_restricts)
            )
        )
        status = "confirmed" if confirmed else "suspected" if suspected else "none"
        reasons: List[str] = []
        confirmation_sources: List[str] = []
        if vendor_confirmed:
            confirmation_sources.append("vendor_runtime_active")
            reasons.append("厂商运行时明确报告温控亮度限制已生效")
            if vendor_level is not None:
                reasons.append(f"当前档位 {vendor_level}")
            if vendor_limit_nits is not None:
                reasons.append(
                    f"系统标称上限 {vendor_limit_nits:g} nit（非亮度计实测）"
                )
        elif vendor_active is False:
            reasons.append("厂商运行时明确报告温控亮度限制未生效")
        elif vendor_state_carried_forward and vendor_last_known_active is not None:
            age_text = (
                f"{vendor_observed_age_s:.1f} 秒前"
                if isinstance(vendor_observed_age_s, (int, float))
                else "此前"
            )
            reasons.append(
                "厂商运行时最近一次报告温控亮度限制"
                f"{'已生效' if vendor_last_known_active else '未生效'}（{age_text}）"
            )
            if vendor_state_stale:
                reasons.append("该厂商状态已超过低频刷新有效期，不能代表当前状态")
        if framework_confirmed:
            confirmation_sources.append("android_framework_thermal_clamp")
        if cooling_confirmed:
            confirmation_sources.append("thermal_hal_lcd_backlight")
        if thermal_applied:
            reasons.append("DisplayManager 已应用热亮度限制")
        if thermal_reason:
            reasons.append("显示亮度最大值原因标记为 thermal")
        if thermal_cap is not None and thermal_cap < 0.999:
            reasons.append(f"亮度上限 {thermal_cap * 100.0:.1f}%")
        if cooling_value > 0:
            reasons.append(f"lcd-backlight 冷却档位 {cooling_value:g}")
        if thermal_status > 0:
            reasons.append(f"亮度热限制状态 {thermal_status}")
        if isinstance(effective_drop_pct, (int, float)) and effective_drop_pct >= 0.5:
            reasons.append(f"有效亮度较请求值低 {effective_drop_pct:.1f}%")

        effective_raw_estimate = None
        if (
            isinstance(setting_raw, (int, float))
            and setting_float is not None
            and setting_float > 0
            and effective is not None
        ):
            effective_raw_estimate = float(setting_raw) * effective / setting_float
        sample = _nearest_sample(samples, sample_uptimes, snapshot.uptime_s)
        observations.append(
            {
                "elapsed_s": max(0.0, snapshot.uptime_s - session_start),
                "uptime_s": snapshot.uptime_s,
                "status": status,
                "confidence": "high" if confirmed else "medium" if suspected else "none",
                "screen_state": screen_text or None,
                "setting_raw": float(setting_raw) if isinstance(setting_raw, (int, float)) else None,
                "setting_unchanged": (
                    abs(float(setting_raw) - baseline_setting) < 0.5
                    if isinstance(setting_raw, (int, float)) and baseline_setting is not None
                    else None
                ),
                "requested_brightness": requested,
                "effective_brightness": effective,
                "effective_brightness_source": effective_source,
                "effective_raw_estimate": effective_raw_estimate,
                "effective_drop_pct": effective_drop_pct,
                "thermal_cap": thermal_cap,
                "thermal_applied": thermal_applied,
                "thermal_status": thermal_status,
                "brightness_max_reason": maximum_reason,
                "lcd_backlight_cooling": cooling_value,
                "confirmation_sources": confirmation_sources,
                "vendor_thermal_provider": vendor_provider,
                "vendor_thermal_active": vendor_active,
                "vendor_thermal_runtime_observed": vendor_state_exposed,
                "vendor_thermal_state_carried_forward": vendor_state_carried_forward,
                "vendor_thermal_last_known_active": vendor_last_known_active,
                "vendor_thermal_observed_age_s": vendor_observed_age_s,
                "vendor_thermal_state_stale": vendor_state_stale,
                "vendor_thermal_level": vendor_level,
                "vendor_thermal_limit_nits": vendor_limit_nits,
                "vendor_thermal_cap_label": (
                    f"系统标称上限 {vendor_limit_nits:g} nit（非亮度计实测）"
                    if vendor_confirmed and vendor_limit_nits is not None
                    else None
                ),
                "vendor_thermal_temperature_c": vendor_temperature_c,
                "vendor_thermal_candidate_caps_nits": vendor_candidate_caps,
                "skin_temperature_c": sensor_value(snapshot, r"^skin$"),
                "battery_temperature_c": sensor_value(snapshot, r"battery"),
                "foreground_package": context.foreground_package if context is not None else None,
                "power_mw": (
                    sample.power_mw
                    if sample is not None and is_consumption_power_sample(sample)
                    else None
                ),
                "reason": "；".join(reasons) if reasons else "未观察到热降亮证据",
                "probe_elapsed_ms": display.get("probe_elapsed_ms"),
            }
        )

    available = display_exposed or cooling_exposed or vendor_thermal_exposed
    points = [item for item in observations if item.get("status") in {"suspected", "confirmed"}]
    events: List[Dict[str, object]] = []
    current_event: List[Dict[str, object]] = []

    def finish_event() -> None:
        nonlocal current_event
        if not current_event:
            return
        confirmed_event = any(item.get("status") == "confirmed" for item in current_event)
        settings = [
            float(item["setting_raw"])
            for item in current_event
            if isinstance(item.get("setting_raw"), (int, float))
        ]
        effective_values = [
            float(item["effective_brightness"])
            for item in current_event
            if isinstance(item.get("effective_brightness"), (int, float))
        ]
        raw_estimates = [
            float(item["effective_raw_estimate"])
            for item in current_event
            if isinstance(item.get("effective_raw_estimate"), (int, float))
        ]
        caps = [
            float(item["thermal_cap"])
            for item in current_event
            if isinstance(item.get("thermal_cap"), (int, float))
        ]
        skin_values = [
            float(item["skin_temperature_c"])
            for item in current_event
            if isinstance(item.get("skin_temperature_c"), (int, float))
        ]
        vendor_limits = [
            float(item["vendor_thermal_limit_nits"])
            for item in current_event
            if item.get("vendor_thermal_active") is True
            and isinstance(item.get("vendor_thermal_limit_nits"), (int, float))
        ]
        vendor_levels = list(
            dict.fromkeys(
                int(item["vendor_thermal_level"])
                for item in current_event
                if item.get("vendor_thermal_active") is True
                and isinstance(item.get("vendor_thermal_level"), (int, float))
            )
        )
        event_confirmation_sources = list(
            dict.fromkeys(
                str(source)
                for item in current_event
                for source in item.get("confirmation_sources", [])
                if source
            )
        )
        events.append(
            {
                "index": len(events) + 1,
                "status": "confirmed" if confirmed_event else "suspected",
                "confidence": "high" if confirmed_event else "medium",
                "start_elapsed_s": current_event[0]["elapsed_s"],
                "end_elapsed_s": current_event[-1]["elapsed_s"],
                "duration_s": max(
                    0.0,
                    float(current_event[-1]["elapsed_s"])
                    - float(current_event[0]["elapsed_s"]),
                ),
                "point_count": len(current_event),
                "setting_raw": settings[0] if settings else None,
                "setting_unchanged": bool(settings) and max(settings) - min(settings) < 0.5,
                "minimum_effective_brightness": min(effective_values) if effective_values else None,
                "minimum_effective_raw_estimate": min(raw_estimates) if raw_estimates else None,
                "minimum_thermal_cap": min(caps) if caps else None,
                "maximum_lcd_backlight_cooling": max(
                    float(item.get("lcd_backlight_cooling") or 0.0)
                    for item in current_event
                ),
                "maximum_skin_temperature_c": max(skin_values) if skin_values else None,
                "vendor_thermal_levels": vendor_levels,
                "minimum_vendor_thermal_limit_nits": (
                    min(vendor_limits) if vendor_limits else None
                ),
                "confirmation_sources": event_confirmation_sources,
                "foreground_packages": list(
                    dict.fromkeys(
                        str(item.get("foreground_package"))
                        for item in current_event
                        if item.get("foreground_package")
                    )
                ),
                "reason": current_event[0].get("reason"),
            }
        )
        current_event = []

    for observation in observations:
        if observation.get("status") in {"suspected", "confirmed"}:
            current_event.append(observation)
        else:
            finish_event()
    finish_event()
    current_state = observations[-1] if observations else None
    latest_flagged = points[-1] if points else None
    source_parts = [
        "Android DisplayManager BrightnessThermalClamper",
        "Thermal HAL lcd-backlight",
    ]
    if vendor_thermal_exposed:
        provider_text = ", ".join(vendor_thermal_providers) or "OEM vendor service"
        source_parts.append(f"{provider_text} runtime state")
    return {
        "available": available,
        "source": " + ".join(source_parts),
        "vendor_thermal_available": vendor_thermal_exposed,
        "vendor_thermal_providers": vendor_thermal_providers,
        "vendor_thermal_confirmed_point_count": sum(
            1
            for item in observations
            if item.get("vendor_thermal_active") is True
            and item.get("vendor_thermal_runtime_observed") is True
        ),
        "timeline": observations,
        "points": points,
        "events": events,
        "point_count": len(points),
        "confirmed_point_count": sum(1 for item in points if item.get("status") == "confirmed"),
        "suspected_point_count": sum(1 for item in points if item.get("status") == "suspected"),
        "event_count": len(events),
        "current_active": bool(
            current_state and current_state.get("status") in {"suspected", "confirmed"}
        ),
        "current_state": current_state,
        "latest_flagged_point": latest_flagged,
        "setting_baseline_raw": baseline_setting,
        "limitations": (
            "ADB can identify framework or Thermal HAL brightness limiting and estimate the effective normalized "
            "brightness. For an OEM channel, only vendor_thermal_active=true is treated as explicit activation; "
            "an inactive flag, a reported limit (including 9999 nit), or a candidate-cap table does not prove "
            "that dimming occurred. The OEM level and ‘system nominal cap’ are firmware-reported values, not "
            "photometer measurements, and the candidate table cannot establish a temperature-to-level mapping. "
            "Exact physical panel luminance still requires an external photometer."
        ),
    }


def _cpuset_cpu_count(value: object) -> Optional[int]:
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


def analyze_scheduler_history(
    samples: Sequence[Sample],
    snapshots: Sequence[SchedulerSnapshot],
) -> Dict[str, object]:
    ordered = sorted(snapshots, key=lambda item: item.uptime_s)
    if not ordered:
        return {
            "available": False,
            "snapshot_count": 0,
            "cpusets": [],
            "cpu_policies": [],
            "hint_sessions": [],
            "process_states": [],
            "timeline": [],
        }
    cpuset_values: Dict[str, List[str]] = {}
    policy_values: Dict[str, Dict[str, object]] = {}
    session_values: Dict[Tuple[object, object, Tuple[int, ...]], Dict[str, object]] = {}
    process_values: Dict[Tuple[object, object], Dict[str, object]] = {}
    timeline: List[Dict[str, object]] = []
    sample_uptimes = [item.uptime_s for item in samples]
    for snapshot in ordered:
        for name, value in snapshot.cpusets.items():
            cpuset_values.setdefault(name, []).append(value)
        for policy in snapshot.cpu_policies:
            name = str(policy.get("name") or "unknown")
            row = policy_values.setdefault(
                name,
                {
                    "name": name,
                    "governors": set(),
                    "scaling_min_khz": set(),
                    "scaling_max_khz": set(),
                    "cpuinfo_min_khz": set(),
                    "cpuinfo_max_khz": set(),
                    "related_cpus": set(),
                    "core_ctl_enabled": set(),
                    "core_ctl_min_cpus": set(),
                    "core_ctl_max_cpus": set(),
                    "statuses": set(),
                },
            )
            if policy.get("governor"):
                row["governors"].add(str(policy["governor"]))  # type: ignore[union-attr]
            for key in ("scaling_min_khz", "scaling_max_khz", "cpuinfo_min_khz", "cpuinfo_max_khz"):
                if isinstance(policy.get(key), (int, float)):
                    row[key].add(float(policy[key]))  # type: ignore[union-attr]
            if policy.get("related_cpus"):
                row["related_cpus"].add(str(policy["related_cpus"]))  # type: ignore[union-attr]
            if isinstance(policy.get("core_ctl_enabled"), bool):
                row["core_ctl_enabled"].add(bool(policy["core_ctl_enabled"]))  # type: ignore[union-attr]
            for key in ("core_ctl_min_cpus", "core_ctl_max_cpus"):
                if isinstance(policy.get(key), (int, float)):
                    row[key].add(int(policy[key]))  # type: ignore[union-attr]
            row["statuses"].add(str(policy.get("status") or "unknown"))  # type: ignore[union-attr]
        for session in snapshot.hint_sessions:
            tids = tuple(int(value) for value in session.get("tids", []) if isinstance(value, int))
            key = (session.get("uid"), session.get("pid"), tids)
            row = session_values.setdefault(
                key,
                {**session, "snapshot_count": 0},
            )
            row["snapshot_count"] = int(row["snapshot_count"]) + 1
            row.update(session)
        for process in snapshot.watched_processes:
            key = (process.get("pid"), process.get("name"))
            row = process_values.setdefault(
                key,
                {**process, "snapshot_count": 0},
            )
            row["snapshot_count"] = int(row["snapshot_count"]) + 1
            row.update(process)
        sample = _nearest_sample(samples, sample_uptimes, snapshot.uptime_s)
        cpuset_name = (
            "top-app"
            if snapshot.cpusets.get("top-app")
            else "foreground"
            if snapshot.cpusets.get("foreground")
            else next(iter(snapshot.cpusets), "")
        )
        cpuset_value = snapshot.cpusets.get(cpuset_name) if cpuset_name else None
        timeline.append(
            {
                "elapsed_s": snapshot.uptime_s - samples[0].uptime_s,
                "cpuset_name": cpuset_name or None,
                "cpuset_cpus": cpuset_value,
                "cpuset_cpu_count": _cpuset_cpu_count(cpuset_value),
                "hint_session_count": len(snapshot.hint_sessions),
                "graphics_session_count": sum(
                    1 for item in snapshot.hint_sessions if item.get("graphics_pipeline")
                ),
                "power_efficient_session_count": sum(
                    1 for item in snapshot.hint_sessions if item.get("power_efficient")
                ),
                "top_app_process_count": sum(
                    1
                    for item in snapshot.watched_processes
                    if item.get("current_sched_group") == 3
                    or str(item.get("adj_type") or "") == "top-activity"
                ),
                "foreground_process_count": sum(
                    1
                    for item in snapshot.watched_processes
                    if isinstance(item.get("current_sched_group"), (int, float))
                    and int(item.get("current_sched_group") or 0) >= 2
                ),
                "frozen_process_count": sum(
                    1 for item in snapshot.watched_processes if item.get("frozen")
                ),
                "power_mw": (
                    sample.power_mw
                    if sample is not None and is_consumption_power_sample(sample)
                    else None
                ),
                "collection_ms": snapshot.collection_ms,
            }
        )

    cpuset_rows = [
        {
            "name": name,
            "latest_cpus": values[-1],
            "observed_cpus": list(dict.fromkeys(values)),
            "changed": len(set(values)) > 1,
        }
        for name, values in sorted(cpuset_values.items())
    ]
    policy_rows: List[Dict[str, object]] = []
    for row in policy_values.values():
        policy_rows.append(
            {
                "name": row["name"],
                "governors": sorted(row["governors"]),
                "scaling_min_khz": sorted(row["scaling_min_khz"]),
                "scaling_max_khz": sorted(row["scaling_max_khz"]),
                "cpuinfo_min_khz": sorted(row["cpuinfo_min_khz"]),
                "cpuinfo_max_khz": sorted(row["cpuinfo_max_khz"]),
                "related_cpus": sorted(row["related_cpus"]),
                "core_ctl_enabled": sorted(row["core_ctl_enabled"]),
                "core_ctl_min_cpus": sorted(row["core_ctl_min_cpus"]),
                "core_ctl_max_cpus": sorted(row["core_ctl_max_cpus"]),
                "statuses": sorted(row["statuses"]),
                "runtime_controls_visible": bool(
                    row["governors"]
                    or row["scaling_min_khz"]
                    or row["scaling_max_khz"]
                    or row["core_ctl_min_cpus"]
                    or row["core_ctl_max_cpus"]
                ),
            }
        )
    policy_rows.sort(key=lambda item: str(item["name"]))
    hint_rows = sorted(
        session_values.values(),
        key=lambda item: (int(item.get("snapshot_count") or 0), bool(item.get("graphics_pipeline"))),
        reverse=True,
    )
    process_rows = sorted(
        process_values.values(),
        key=lambda item: (
            int(item.get("current_proc_state") or 99) * -1,
            int(item.get("snapshot_count") or 0),
        ),
        reverse=True,
    )
    availability: Dict[str, object] = {}
    for snapshot in ordered:
        availability.update(snapshot.availability)
    collection_times = [
        float(item.collection_ms)
        for item in ordered
        if isinstance(item.collection_ms, (int, float))
    ]
    return {
        "available": True,
        "snapshot_count": len(ordered),
        "cpusets": cpuset_rows,
        "cpu_policies": policy_rows,
        "hint_sessions": hint_rows,
        "maximum_hint_session_count": max((item["hint_session_count"] for item in timeline), default=0),
        "process_states": process_rows[:60],
        "timeline": timeline,
        "availability": availability,
        "latest_power_state": ordered[-1].power_hal,
        "average_collection_ms": statistics.fmean(collection_times) if collection_times else None,
        "maximum_collection_ms": max(collection_times) if collection_times else None,
        "limitations": (
            "cpuset membership, ActivityManager proc state and ADPF sessions are observable. "
            "Kernel sched_debug and OEM governor/uclamp controls may remain permission-restricted on production builds."
        ),
    }


def _merge_time_windows(windows: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    ordered = sorted((start, end) for start, end in windows if end > start)
    merged: List[Tuple[float, float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _overlap_segments(
    left: Sequence[Tuple[float, float]],
    right: Sequence[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    overlaps: List[Tuple[float, float]] = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if end > start:
                overlaps.append((start, end))
    return _merge_time_windows(overlaps)


def _time_in_windows(value: float, windows: Sequence[Tuple[float, float]]) -> bool:
    return any(start <= value <= end for start, end in windows)


def _analysis_windows(rows: Sequence[Dict[str, object]]) -> List[Tuple[float, float]]:
    windows: List[Tuple[float, float]] = []
    for row in rows:
        for item in row.get("windows", []):
            if not isinstance(item, dict):
                continue
            start = item.get("start_uptime_s")
            end = item.get("end_uptime_s")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                windows.append((float(start), float(end)))
    return windows


def analyze_test_items(
    samples: Sequence[Sample],
    contexts: Sequence[ContextSample],
    events: Sequence[ExternalEvent],
    system_snapshots: Sequence[SystemSnapshot],
    system_analysis: Dict[str, object],
    thermal_analysis: Dict[str, object],
    scheduler_analysis: Dict[str, object],
    max_gap_s: float,
    sample_interval_s: float,
) -> Dict[str, object]:
    """Aggregate long sessions by explicit test spans, falling back to foreground activity."""
    session_start = samples[0].uptime_s
    session_end = samples[-1].uptime_s
    intervals = sample_intervals(samples, max_gap_s)
    consumption_intervals = sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=True,
    )
    sample_uptimes = [item.uptime_s for item in samples]
    minimum_reliable_duration_s = max(3.0 * sample_interval_s, sample_interval_s + 2.0)
    definitions: Dict[Tuple[str, str], Dict[str, object]] = {}
    occurrences: List[Dict[str, object]] = []

    duration_events = [
        item
        for item in events
        if isinstance(item.duration_s, (int, float))
        and float(item.duration_s or 0.0) > 0
        and item.device_uptime_s < session_end
        and item.device_uptime_s + float(item.duration_s or 0.0) > session_start
    ]
    source_mode = "external_events" if duration_events else "foreground_activity"
    if duration_events:
        for event in sorted(duration_events, key=lambda item: item.device_uptime_s):
            start = max(session_start, event.device_uptime_s)
            end = min(session_end, event.device_uptime_s + float(event.duration_s or 0.0))
            if end <= start:
                continue
            key = (event.phase or "测试", event.name or "未命名测试项")
            occurrence = {
                "phase": key[0],
                "name": key[1],
                "start_uptime_s": start,
                "end_uptime_s": end,
                "source": event.source,
                "metadata": event.metadata,
            }
            occurrences.append(occurrence)
            row = definitions.setdefault(
                key,
                {
                    "phase": key[0],
                    "name": key[1],
                    "comparison_key": str(event.metadata.get("test") or key[1]),
                    "source": event.source,
                    "windows": [],
                },
            )
            row["windows"].append((start, end))  # type: ignore[union-attr]
    else:
        ordered_contexts = sorted(contexts, key=lambda item: item.uptime_s)
        context_uptimes = [item.uptime_s for item in ordered_contexts]
        current = _context_at(ordered_contexts, context_uptimes, session_start)
        cursor = session_start
        changes = [
            item for item in ordered_contexts if session_start < item.uptime_s < session_end
        ]

        def context_key(context: Optional[ContextSample]) -> Tuple[str, str]:
            package = context.foreground_package if context else None
            activity = context.foreground_activity if context else None
            return package or "前台活动", activity or package or "未知前台活动"

        def append_context_window(
            key: Tuple[str, str],
            start: float,
            end: float,
            context: Optional[ContextSample],
        ) -> None:
            if end <= start:
                return
            if not _auto_test_context_allowed(context, performance=False):
                return
            context_source = context.source if context and context.source else "platform_context_sampler"
            occurrence = {
                "phase": key[0],
                "name": key[1],
                "start_uptime_s": start,
                "end_uptime_s": end,
                "source": context_source,
                "metadata": {},
            }
            occurrences.append(occurrence)
            row = definitions.setdefault(
                key,
                {
                    "phase": key[0],
                    "name": key[1],
                    "comparison_key": key[1],
                    "source": context_source,
                    "windows": [],
                },
            )
            row["windows"].append((start, end))  # type: ignore[union-attr]

        current_key = context_key(current)
        current_partition = _context_screen_partition(current)
        for following in changes:
            next_key = context_key(following)
            next_partition = _context_screen_partition(following)
            if next_key == current_key and next_partition == current_partition:
                current = following
                continue
            append_context_window(current_key, cursor, following.uptime_s, current)
            current = following
            current_key = next_key
            current_partition = next_partition
            cursor = following.uptime_s
        append_context_window(current_key, cursor, session_end, current)

    activity_groups = system_analysis.get("activity_groups", {})
    classified_rows = (
        activity_groups.get("rows", []) if isinstance(activity_groups, dict) else []
    )
    classified_timeline = (
        activity_groups.get("timeline", []) if isinstance(activity_groups, dict) else []
    )
    priority = system_analysis.get("priority_activities", {})
    priority_rows = priority.get("rows", []) if isinstance(priority, dict) else []
    priority_timeline = priority.get("timeline", []) if isinstance(priority, dict) else []
    system_timeline = system_analysis.get("timeline", [])
    thermal_timeline = thermal_analysis.get("timeline", [])
    scheduler_timeline = scheduler_analysis.get("timeline", [])

    thermal_points = sorted(
        (
            item
            for item in thermal_timeline
            if isinstance(item.get("uptime_s"), (int, float))
        ),
        key=lambda item: float(item["uptime_s"]),
    )
    thermal_cadence = _snapshot_cadence(
        [float(item["uptime_s"]) for item in thermal_points],
        30.0,
    )
    throttled_windows: List[Tuple[float, float]] = []
    for index, item in enumerate(thermal_points):
        status = int(item.get("status") or 0)
        if status <= 0:
            continue
        previous_uptime = (
            float(thermal_points[index - 1]["uptime_s"])
            if index > 0
            else float(item["uptime_s"]) - thermal_cadence
        )
        next_uptime = (
            float(thermal_points[index + 1]["uptime_s"])
            if index + 1 < len(thermal_points)
            else float(item["uptime_s"]) + thermal_cadence
        )
        throttled_windows.append(
            (
                max(session_start, (previous_uptime + float(item["uptime_s"])) * 0.5),
                min(session_end, (float(item["uptime_s"]) + next_uptime) * 0.5),
            )
        )

    ordered_system_snapshots = sorted(
        (
            item
            for item in system_snapshots
            if session_start - max_gap_s <= item.uptime_s <= session_end + max_gap_s
        ),
        key=lambda item: item.uptime_s,
    )
    ordered_contexts = sorted(contexts, key=lambda item: item.uptime_s)
    context_uptimes = [item.uptime_s for item in ordered_contexts]

    def analyze_windows(windows: Sequence[Tuple[float, float]]) -> Dict[str, object]:
        valid_windows = [(float(start), float(end)) for start, end in windows if end > start]
        duration_s = sum(end - start for start, end in valid_windows)
        covered_s = 0.0
        energy_mwh = 0.0
        discharge_mah = 0.0
        power_values: List[float] = []
        cpu_values: List[float] = []
        battery_temperatures: List[float] = []
        gpu_load_values: List[float] = []
        gpu_frequency_values: List[float] = []
        foreground_duration: Dict[str, float] = {}
        for start, end in valid_windows:
            metrics = _window_power_metrics(samples, consumption_intervals, start, end)
            covered_s += float(metrics.get("covered_duration_s") or 0.0)
            energy_mwh += float(metrics.get("energy_mwh") or 0.0)
            for previous, current, _ in consumption_intervals:
                overlap_s = max(0.0, min(end, current.uptime_s) - max(start, previous.uptime_s))
                if overlap_s <= 0:
                    continue
                discharge_mah += (
                    (previous.current_ma + current.current_ma) * 0.5 * overlap_s / 3600.0
                )
                midpoint = max(start, previous.uptime_s) + overlap_s * 0.5
                context = _context_at(ordered_contexts, context_uptimes, midpoint)
                package = context.foreground_package if context and context.foreground_package else "unknown"
                foreground_duration[package] = foreground_duration.get(package, 0.0) + overlap_s
            for sample in samples:
                if start <= sample.uptime_s <= end:
                    if is_consumption_power_sample(sample):
                        power_values.append(sample.power_mw)
                    if sample.cpu_pct is not None:
                        cpu_values.append(float(sample.cpu_pct))
                    if sample.battery_temperature_c is not None:
                        battery_temperatures.append(float(sample.battery_temperature_c))
                    if sample.gpu_load_pct is not None:
                        gpu_load_values.append(float(sample.gpu_load_pct))
                    if sample.gpu_frequency_mhz is not None:
                        gpu_frequency_values.append(float(sample.gpu_frequency_mhz))

        first_start = min((item[0] for item in valid_windows), default=session_start)
        last_end = max((item[1] for item in valid_windows), default=session_start)
        start_sample = _nearest_sample(samples, sample_uptimes, first_start)
        end_sample = _nearest_sample(samples, sample_uptimes, last_end)

        relevant_system = [
            item for item in ordered_system_snapshots if _time_in_windows(item.uptime_s, valid_windows)
        ]
        process_aggregate: Dict[Tuple[str, str, str], Dict[str, object]] = {}
        for snapshot in relevant_system:
            for process in snapshot.processes:
                key = (
                    str(process.get("name") or process.get("command") or "unknown"),
                    str(process.get("user") or "unknown"),
                    str(process.get("category") or "other"),
                )
                row = process_aggregate.setdefault(
                    key,
                    {
                        "name": key[0],
                        "user": key[1],
                        "category": key[2],
                        "cpu_sum": 0.0,
                        "visible_snapshots": 0,
                        "maximum_cpu_pct": 0.0,
                        "pids": set(),
                    },
                )
                cpu = float(process.get("cpu_pct") or 0.0)
                row["cpu_sum"] = float(row["cpu_sum"]) + cpu
                row["visible_snapshots"] = int(row["visible_snapshots"]) + 1
                row["maximum_cpu_pct"] = max(float(row["maximum_cpu_pct"]), cpu)
                if isinstance(process.get("pid"), int):
                    row["pids"].add(int(process["pid"]))  # type: ignore[union-attr]
        top_processes: List[Dict[str, object]] = []
        for row in process_aggregate.values():
            cpu_sum = float(row.pop("cpu_sum"))
            visible = int(row["visible_snapshots"])
            pids = row.pop("pids")
            row["average_cpu_pct"] = (
                cpu_sum / len(relevant_system) if relevant_system else None
            )
            row["average_when_visible_cpu_pct"] = cpu_sum / visible if visible else None
            row["pids"] = sorted(pids) if isinstance(pids, set) else []
            top_processes.append(row)
        top_processes.sort(
            key=lambda item: (
                float(item.get("average_cpu_pct") or 0.0),
                float(item.get("maximum_cpu_pct") or 0.0),
            ),
            reverse=True,
        )

        classified_summaries: List[Dict[str, object]] = []
        for activity in classified_rows:
            activity_windows = _analysis_windows([activity])
            overlap = _overlap_segments(valid_windows, activity_windows)
            detections = [
                item
                for item in classified_timeline
                if item.get("kind") == activity.get("kind")
                and item.get("subsystem") == activity.get("subsystem")
                and _time_in_windows(float(item.get("uptime_s") or -1.0), valid_windows)
            ]
            if not overlap and not detections:
                continue
            cpu = [float(item.get("cpu_pct") or 0.0) for item in detections]
            classified_summaries.append(
                {
                    "kind": activity.get("kind"),
                    "family": activity.get("family"),
                    "label": activity.get("label"),
                    "subsystem": activity.get("subsystem"),
                    "overlap_s": sum(end - start for start, end in overlap),
                    "snapshot_count": len({float(item.get("uptime_s") or 0.0) for item in detections}),
                    "average_cpu_pct": statistics.fmean(cpu) if cpu else None,
                    "maximum_cpu_pct": max(cpu) if cpu else None,
                }
            )

        priority_summaries: List[Dict[str, object]] = []
        for activity in priority_rows:
            activity_windows = _analysis_windows([activity])
            overlap = _overlap_segments(valid_windows, activity_windows)
            detections = [
                item
                for item in priority_timeline
                if item.get("kind") == activity.get("kind")
                and item.get("name") == activity.get("name")
                and _time_in_windows(float(item.get("uptime_s") or -1.0), valid_windows)
            ]
            if not overlap and not detections:
                continue
            cpu = [
                float(item["cpu_pct"])
                for item in detections
                if isinstance(item.get("cpu_pct"), (int, float))
            ]
            priority_summaries.append(
                {
                    "kind": activity.get("kind"),
                    "family": activity.get("kind"),
                    "label": activity.get("label") or activity.get("name"),
                    "subsystem": activity.get("kind"),
                    "overlap_s": sum(end - start for start, end in overlap),
                    "snapshot_count": len({float(item.get("uptime_s") or 0.0) for item in detections}),
                    "average_cpu_pct": statistics.fmean(cpu) if cpu else None,
                    "maximum_cpu_pct": max(cpu) if cpu else None,
                }
            )

        def family_metrics(family: str) -> Dict[str, object]:
            matching = [item for item in classified_summaries if item.get("family") == family]
            detections = [
                item
                for item in classified_timeline
                if item.get("family") == family
                and _time_in_windows(float(item.get("uptime_s") or -1.0), valid_windows)
            ]
            cpu = [float(item.get("cpu_pct") or 0.0) for item in detections]
            matching_rows = [item for item in classified_rows if item.get("family") == family]
            overlap = _overlap_segments(valid_windows, _analysis_windows(matching_rows))
            return {
                "overlap_s": sum(end - start for start, end in overlap),
                "snapshot_count": len({float(item.get("uptime_s") or 0.0) for item in detections}),
                "average_cpu_pct": statistics.fmean(cpu) if cpu else None,
                "maximum_cpu_pct": max(cpu) if cpu else None,
                "labels": [str(item.get("label")) for item in matching],
            }

        gc = family_metrics("gc")
        kworker = family_metrics("kworker")
        dex_update_rows = [
            item
            for item in priority_rows
            if item.get("kind") in {"dex_optimization", "system_update"}
        ]
        dex_update_overlap = _overlap_segments(
            valid_windows,
            _analysis_windows(dex_update_rows),
        )
        package_rows = [item for item in priority_rows if item.get("kind") == "package_management"]
        package_overlap = _overlap_segments(valid_windows, _analysis_windows(package_rows))
        interference_activity_rows = [
            item for item in classified_rows if item.get("family") != "display"
        ]
        all_activity_overlap = _overlap_segments(
            valid_windows,
            _analysis_windows(interference_activity_rows) + _analysis_windows(priority_rows),
        )
        system_activity_overlap_s = sum(end - start for start, end in all_activity_overlap)
        system_activity_overlap_pct = (
            system_activity_overlap_s / duration_s * 100.0 if duration_s > 0 else 0.0
        )

        system_points = [
            item
            for item in system_timeline
            if _time_in_windows(
                session_start + float(item.get("elapsed_s") or 0.0),
                valid_windows,
            )
        ]
        visible_cpu = sum(float(item.get("visible_cpu_pct") or 0.0) for item in system_points)
        system_cpu = sum(
            float(item.get("kernel_cpu_pct") or 0.0)
            + float(item.get("android_system_cpu_pct") or 0.0)
            + float(item.get("priority_cpu_pct") or 0.0)
            for item in system_points
        )
        system_cpu_share_pct = (
            min(100.0, system_cpu / visible_cpu * 100.0) if visible_cpu > 0 else None
        )

        relevant_thermal = [
            item
            for item in thermal_points
            if _time_in_windows(float(item["uptime_s"]), valid_windows)
        ]
        thermal_values = [
            float(value)
            for item in relevant_thermal
            for value in (item.get("sensors") or {}).values()
            if isinstance(value, (int, float))
        ]
        thermal_max_status = max((int(item.get("status") or 0) for item in relevant_thermal), default=0)
        throttled_overlap = _overlap_segments(valid_windows, throttled_windows)
        throttled_overlap_s = sum(end - start for start, end in throttled_overlap)
        relevant_scheduler = [
            item
            for item in scheduler_timeline
            if _time_in_windows(
                session_start + float(item.get("elapsed_s") or 0.0),
                valid_windows,
            )
        ]
        maximum_hint_sessions = max(
            (int(item.get("hint_session_count") or 0) for item in relevant_scheduler),
            default=0,
        )

        dex_update_overlap_s = sum(end - start for start, end in dex_update_overlap)
        package_overlap_s = sum(end - start for start, end in package_overlap)
        if not system_analysis.get("available"):
            interference_level = "unknown"
        elif (
            system_activity_overlap_pct >= 40.0
            or (system_cpu_share_pct is not None and system_cpu_share_pct >= 50.0)
            or dex_update_overlap_s >= max(10.0, duration_s * 0.15)
        ):
            interference_level = "high"
        elif (
            system_activity_overlap_pct >= 10.0
            or (system_cpu_share_pct is not None and system_cpu_share_pct >= 25.0)
            or int(gc["snapshot_count"]) > 0
            or int(kworker["snapshot_count"]) > 0
            or dex_update_overlap_s > 0
            or package_overlap_s > 0
        ):
            interference_level = "medium"
        else:
            interference_level = "low"

        coverage_pct = covered_s / duration_s * 100.0 if duration_s > 0 else 0.0
        power_confidence = (
            "medium"
            if duration_s >= minimum_reliable_duration_s and coverage_pct >= 75.0
            else "low"
            if covered_s > 0
            else "unavailable"
        )
        interference_confidence = (
            "medium" if len(relevant_system) >= 2 else "low"
        )
        confidence = (
            "medium"
            if power_confidence == "medium" and interference_confidence == "medium"
            else "low"
        )
        activity_summaries = classified_summaries + priority_summaries
        activity_summaries.sort(
            key=lambda item: (
                float(item.get("overlap_s") or 0.0),
                float(item.get("average_cpu_pct") or 0.0),
            ),
            reverse=True,
        )
        foreground_rows = sorted(
            foreground_duration.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        all_temperatures = battery_temperatures + thermal_values
        return {
            "duration_s": duration_s,
            "covered_duration_s": covered_s,
            "coverage_pct": coverage_pct,
            "power_valid_for_consumption": covered_s > 0,
            "energy_mwh": energy_mwh if covered_s > 0 else None,
            "discharge_mah": discharge_mah if covered_s > 0 else None,
            "mwh_per_minute": energy_mwh * 60.0 / covered_s if covered_s > 0 else None,
            "average_power_mw": energy_mwh * 3600.0 / covered_s if covered_s > 0 else None,
            "p95_power_mw": percentile(power_values, 0.95),
            "maximum_power_mw": max(power_values) if power_values else None,
            "average_cpu_pct": statistics.fmean(cpu_values) if cpu_values else None,
            "maximum_cpu_pct": max(cpu_values) if cpu_values else None,
            "average_gpu_load_pct": statistics.fmean(gpu_load_values) if gpu_load_values else None,
            "maximum_gpu_load_pct": max(gpu_load_values) if gpu_load_values else None,
            "average_gpu_frequency_mhz": (
                statistics.fmean(gpu_frequency_values) if gpu_frequency_values else None
            ),
            "maximum_gpu_frequency_mhz": max(gpu_frequency_values) if gpu_frequency_values else None,
            "start_temperature_c": (
                start_sample.battery_temperature_c if start_sample else None
            ),
            "end_temperature_c": end_sample.battery_temperature_c if end_sample else None,
            "maximum_temperature_c": max(all_temperatures) if all_temperatures else None,
            "maximum_thermal_status": thermal_max_status,
            "thermal_throttling_overlap_s": throttled_overlap_s,
            "maximum_hint_session_count": maximum_hint_sessions,
            "foreground_packages": [item[0] for item in foreground_rows[:5]],
            "top_processes": top_processes[:8],
            "top_activities": activity_summaries[:8],
            "gc": gc,
            "kworker": kworker,
            "dex_update_overlap_s": dex_update_overlap_s,
            "package_management_overlap_s": package_overlap_s,
            "system_activity_overlap_s": system_activity_overlap_s,
            "system_activity_overlap_pct": system_activity_overlap_pct,
            "visible_system_cpu_share_pct": system_cpu_share_pct,
            "system_snapshot_count": len(relevant_system),
            "interference_level": interference_level,
            "power_confidence": power_confidence,
            "interference_confidence": interference_confidence,
            "confidence": confidence,
        }

    rows: List[Dict[str, object]] = []
    for definition in definitions.values():
        windows = definition.get("windows", [])
        metrics = analyze_windows(windows if isinstance(windows, list) else [])
        rows.append(
            {
                "phase": definition.get("phase"),
                "name": definition.get("name"),
                "comparison_key": definition.get("comparison_key") or definition.get("name"),
                "source": definition.get("source"),
                "count": len(windows) if isinstance(windows, list) else 0,
                "first_start_elapsed_s": (
                    min((item[0] for item in windows), default=session_start) - session_start
                    if isinstance(windows, list)
                    else 0.0
                ),
                "last_end_elapsed_s": (
                    max((item[1] for item in windows), default=session_start) - session_start
                    if isinstance(windows, list)
                    else 0.0
                ),
                **metrics,
            }
        )
    rows.sort(key=lambda item: float(item.get("first_start_elapsed_s") or 0.0))

    span_rows: List[Dict[str, object]] = []
    for occurrence in occurrences:
        start = float(occurrence["start_uptime_s"])
        end = float(occurrence["end_uptime_s"])
        metrics = analyze_windows([(start, end)])
        span_rows.append(
            {
                "phase": occurrence.get("phase"),
                "name": occurrence.get("name"),
                "source": occurrence.get("source"),
                "metadata": occurrence.get("metadata"),
                "start_uptime_s": start,
                "end_uptime_s": end,
                "start_elapsed_s": start - session_start,
                "end_elapsed_s": end - session_start,
                **metrics,
            }
        )
    span_rows.sort(key=lambda item: float(item.get("start_uptime_s") or 0.0))

    overlap_count = 0
    for index, current in enumerate(occurrences):
        for following in occurrences[index + 1 :]:
            if (
                float(current["start_uptime_s"]) < float(following["end_uptime_s"])
                and float(following["start_uptime_s"]) < float(current["end_uptime_s"])
                and (current.get("phase"), current.get("name"))
                != (following.get("phase"), following.get("name"))
            ):
                overlap_count += 1

    effective_source_mode = source_mode if rows else "unavailable"
    return {
        "available": bool(rows),
        "source_mode": effective_source_mode,
        "source_label": (
            "导入的持续测试事件"
            if effective_source_mode == "external_events"
            else "前台应用 / Ability 区间"
            if effective_source_mode == "foreground_activity"
            else "未获得可归属的测试区间"
        ),
        "minimum_reliable_duration_s": minimum_reliable_duration_s,
        "row_count": len(rows),
        "span_count": len(span_rows),
        "overlap_count": overlap_count,
        "rows": rows,
        "spans": span_rows,
        "instant_events": [
            {
                "name": item.name,
                "phase": item.phase,
                "elapsed_s": item.device_uptime_s - session_start,
                "source": item.source,
            }
            for item in events
            if not item.duration_s and session_start <= item.device_uptime_s <= session_end
        ],
        "limitations": (
            "Energy and power are whole-device measurements integrated inside each test window. "
            "GC, kworker, platform background activity and thermal columns report sampled temporal overlap and relative influence, "
            "not exclusive per-process rail power. Overlapping test rows are not additive."
        ),
    }


def analyze_performance_test_items(
    samples: Sequence[Sample],
    contexts: Sequence[ContextSample],
    events: Sequence[ExternalEvent],
    performance_analysis: Dict[str, object],
    thermal_analysis: Dict[str, object],
    scheduler_analysis: Dict[str, object],
    max_gap_s: float,
    sample_interval_s: float,
) -> Dict[str, object]:
    """Aggregate explicit test spans around frame behavior, not power attribution."""
    session_start = samples[0].uptime_s
    session_end = samples[-1].uptime_s
    minimum_reliable_duration_s = max(3.0 * sample_interval_s, sample_interval_s + 2.0)
    definitions: Dict[Tuple[str, str], Dict[str, object]] = {}
    occurrences: List[Dict[str, object]] = []

    duration_events = [
        item
        for item in events
        if isinstance(item.duration_s, (int, float))
        and float(item.duration_s or 0.0) > 0
        and item.device_uptime_s < session_end
        and item.device_uptime_s + float(item.duration_s or 0.0) > session_start
    ]
    source_mode = "external_events" if duration_events else "foreground_activity"
    if duration_events:
        for event in sorted(duration_events, key=lambda item: item.device_uptime_s):
            start = max(session_start, event.device_uptime_s)
            end = min(session_end, event.device_uptime_s + float(event.duration_s or 0.0))
            if end <= start:
                continue
            key = (event.phase or "测试", event.name or "未命名测试项")
            occurrence = {
                "phase": key[0],
                "name": key[1],
                "start_uptime_s": start,
                "end_uptime_s": end,
                "source": event.source,
                "metadata": event.metadata,
            }
            occurrences.append(occurrence)
            row = definitions.setdefault(
                key,
                {
                    "phase": key[0],
                    "name": key[1],
                    "comparison_key": str(event.metadata.get("test") or key[1]),
                    "source": event.source,
                    "windows": [],
                },
            )
            row["windows"].append((start, end))  # type: ignore[union-attr]
    else:
        ordered_contexts = sorted(contexts, key=lambda item: item.uptime_s)
        context_uptimes = [item.uptime_s for item in ordered_contexts]
        current = _context_at(ordered_contexts, context_uptimes, session_start)
        cursor = session_start

        def context_key(context: Optional[ContextSample]) -> Tuple[str, str]:
            package = context.foreground_package if context else None
            activity = context.foreground_activity if context else None
            return package or "前台活动", activity or package or "未知前台活动"

        def append_window(
            key: Tuple[str, str],
            start: float,
            end: float,
            context: Optional[ContextSample],
        ) -> None:
            if end <= start:
                return
            if not _auto_test_context_allowed(context, performance=True):
                return
            source = context.source if context and context.source else "platform_context_sampler"
            occurrences.append(
                {
                    "phase": key[0],
                    "name": key[1],
                    "start_uptime_s": start,
                    "end_uptime_s": end,
                    "source": source,
                    "metadata": {},
                }
            )
            row = definitions.setdefault(
                key,
                {
                    "phase": key[0],
                    "name": key[1],
                    "comparison_key": key[1],
                    "source": source,
                    "windows": [],
                },
            )
            row["windows"].append((start, end))  # type: ignore[union-attr]

        current_key = context_key(current)
        current_partition = _context_screen_partition(current)
        for following in (
            item for item in ordered_contexts if session_start < item.uptime_s < session_end
        ):
            next_key = context_key(following)
            next_partition = _context_screen_partition(following)
            if next_key == current_key and next_partition == current_partition:
                current = following
                continue
            append_window(current_key, cursor, following.uptime_s, current)
            current = following
            current_key = next_key
            current_partition = next_partition
            cursor = following.uptime_s
        append_window(current_key, cursor, session_end, current)

    frame_timeline = performance_analysis.get("frame_rate_timeline", [])
    frame_timeline = frame_timeline if isinstance(frame_timeline, list) else []
    render_pipeline = performance_analysis.get("render_pipeline", {})
    render_pipeline = render_pipeline if isinstance(render_pipeline, dict) else {}
    detailed_frames = render_pipeline.get("timeline", [])
    detailed_frames = detailed_frames if isinstance(detailed_frames, list) else []
    thermal_timeline = thermal_analysis.get("timeline", [])
    thermal_timeline = thermal_timeline if isinstance(thermal_timeline, list) else []
    scheduler_timeline = scheduler_analysis.get("timeline", [])
    scheduler_timeline = scheduler_timeline if isinstance(scheduler_timeline, list) else []
    consumption_intervals = sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=True,
    )

    def values_in_windows(
        field: str,
        windows: Sequence[Tuple[float, float]],
        *,
        require_consumption_power: bool = False,
    ) -> List[float]:
        return [
            float(getattr(sample, field))
            for sample in samples
            if _time_in_windows(sample.uptime_s, windows)
            and isinstance(getattr(sample, field), (int, float))
            and (
                not require_consumption_power
                or is_consumption_power_sample(sample)
            )
        ]

    def analyze_windows(windows: Sequence[Tuple[float, float]]) -> Dict[str, object]:
        valid_windows = _merge_time_windows(windows)
        duration_s = sum(end - start for start, end in valid_windows)
        period_rows = [
            item
            for item in frame_timeline
            if isinstance(item, dict)
            and isinstance(item.get("uptime_s"), (int, float))
            and _time_in_windows(float(item["uptime_s"]), valid_windows)
        ]
        detailed = [
            item
            for item in detailed_frames
            if isinstance(item, dict)
            and isinstance(item.get("context_uptime_s"), (int, float))
            and _time_in_windows(float(item["context_uptime_s"]), valid_windows)
        ]
        frame_count = sum(int(item.get("frame_count") or 0) for item in period_rows)
        frame_duration_s = sum(float(item.get("duration_s") or 0.0) for item in period_rows)
        average_fps = frame_count / frame_duration_s if frame_duration_s > 0 else None
        detailed_totals = [
            float(item["total_ms"])
            for item in detailed
            if isinstance(item.get("total_ms"), (int, float))
        ]
        presented_intervals = [
            float(value)
            for item in period_rows
            for value in (
                item.get("frame_intervals_ms", [])
                if isinstance(item.get("frame_intervals_ms"), list)
                else []
            )
            if isinstance(value, (int, float)) and float(value) > 0
        ]
        if presented_intervals:
            slow_count = max(1, math.ceil(len(presented_intervals) * 0.01))
            slow_average_ms = statistics.fmean(
                sorted(presented_intervals, reverse=True)[:slow_count]
            )
            one_percent_low = 1000.0 / slow_average_ms if slow_average_ms > 0 else None
            frame_p95 = percentile(presented_intervals, 0.95)
            frame_p99 = percentile(presented_intervals, 0.99)
            frame_metric_source = "Android SurfaceFlinger presented-frame intervals"
        elif detailed_totals:
            slow_count = max(1, math.ceil(len(detailed_totals) * 0.01))
            slow_average_ms = statistics.fmean(
                sorted(detailed_totals, reverse=True)[:slow_count]
            )
            one_percent_low = 1000.0 / slow_average_ms if slow_average_ms > 0 else None
            frame_p95 = percentile(detailed_totals, 0.95)
            frame_p99 = percentile(detailed_totals, 0.99)
            frame_metric_source = "Android gfxinfo detailed framestats"
        else:
            one_percent_low = None
            p95_values = [
                float(item["frame_time_p95_ms"])
                for item in period_rows
                if isinstance(item.get("frame_time_p95_ms"), (int, float))
            ]
            p99_values = [
                float(item["frame_time_p99_ms"])
                for item in period_rows
                if isinstance(item.get("frame_time_p99_ms"), (int, float))
            ]
            frame_p95 = max(p95_values) if p95_values else None
            frame_p99 = max(p99_values) if p99_values else None
            frame_metric_source = "periodic frame counter windows"

        frame_issue_count = 0
        for item in period_rows:
            explicit_count = next(
                (
                    item.get(key)
                    for key in (
                        "frame_issue_count",
                        "missed_vsync_interval_count",
                        "deadline_missed_count",
                    )
                    if isinstance(item.get(key), (int, float))
                ),
                None,
            )
            if isinstance(explicit_count, (int, float)):
                frame_issue_count += max(0, int(explicit_count))
                continue
            period_frame_count = item.get("frame_count")
            period_issue_pct = item.get("frame_issue_pct")
            if isinstance(period_frame_count, (int, float)) and isinstance(
                period_issue_pct, (int, float)
            ):
                frame_issue_count += max(
                    0,
                    int(round(float(period_frame_count) * float(period_issue_pct) / 100.0)),
                )
        if detailed and not period_rows:
            frame_issue_count = sum(1 for item in detailed if item.get("deadline_missed"))
            issue_denominator = len(detailed)
        else:
            issue_denominator = frame_count
        frame_issue_pct = (
            frame_issue_count / issue_denominator * 100.0
            if issue_denominator > 0
            else None
        )

        stage_rows: List[Dict[str, object]] = []
        stage_labels = {
            str(item.get("key")): str(item.get("label"))
            for item in render_pipeline.get("stages", [])
            if isinstance(item, dict) and item.get("key")
        }
        for key, label in stage_labels.items():
            stage_values = [
                float(item[key])
                for item in detailed
                if isinstance(item.get(key), (int, float))
            ]
            if stage_values:
                stage_rows.append(
                    {
                        "key": key,
                        "label": label,
                        "sample_count": len(stage_values),
                        "average_ms": statistics.fmean(stage_values),
                        "p95_ms": percentile(stage_values, 0.95),
                        "p99_ms": percentile(stage_values, 0.99),
                        "maximum_ms": max(stage_values),
                    }
                )
        stage_rows.sort(key=lambda item: float(item.get("p95_ms") or 0.0), reverse=True)

        cpu_values = values_in_windows("cpu_pct", valid_windows)
        gpu_load_values = values_in_windows("gpu_load_pct", valid_windows)
        gpu_frequency_values = values_in_windows("gpu_frequency_mhz", valid_windows)
        memory_values = values_in_windows("memory_frequency_mhz", valid_windows)
        power_values = values_in_windows(
            "power_mw",
            valid_windows,
            require_consumption_power=True,
        )
        power_covered_duration_s = 0.0
        power_energy_mwh = 0.0
        for start, end in valid_windows:
            power_metrics = _window_power_metrics(
                samples,
                consumption_intervals,
                start,
                end,
            )
            power_covered_duration_s += float(
                power_metrics.get("covered_duration_s") or 0.0
            )
            power_energy_mwh += float(power_metrics.get("energy_mwh") or 0.0)
        thermal_points = [
            item
            for item in thermal_timeline
            if isinstance(item, dict)
            and isinstance(item.get("uptime_s"), (int, float))
            and _time_in_windows(float(item["uptime_s"]), valid_windows)
        ]
        thermal_values = [
            float(item["maximum_temperature_c"])
            for item in thermal_points
            if isinstance(item.get("maximum_temperature_c"), (int, float))
        ]
        maximum_thermal_status = max(
            (int(item.get("status") or 0) for item in thermal_points),
            default=0,
        )
        scheduler_points = [
            item
            for item in scheduler_timeline
            if isinstance(item, dict)
            and isinstance(item.get("uptime_s"), (int, float))
            and _time_in_windows(float(item["uptime_s"]), valid_windows)
        ]
        return {
            "duration_s": duration_s,
            "frame_count": frame_count or len(detailed),
            "average_fps": average_fps,
            "one_percent_low_fps": one_percent_low,
            "frame_p95_ms": frame_p95,
            "frame_p99_ms": frame_p99,
            "frame_issue_count": frame_issue_count,
            "frame_issue_pct": frame_issue_pct,
            "frame_metric_source": frame_metric_source,
            "detailed_frame_count": len(detailed),
            "dominant_stage": stage_rows[0] if stage_rows else None,
            "stages": stage_rows,
            "average_cpu_pct": statistics.fmean(cpu_values) if cpu_values else None,
            "maximum_cpu_pct": max(cpu_values) if cpu_values else None,
            "average_gpu_load_pct": (
                statistics.fmean(gpu_load_values) if gpu_load_values else None
            ),
            "maximum_gpu_load_pct": max(gpu_load_values) if gpu_load_values else None,
            "average_gpu_frequency_mhz": (
                statistics.fmean(gpu_frequency_values) if gpu_frequency_values else None
            ),
            "maximum_gpu_frequency_mhz": (
                max(gpu_frequency_values) if gpu_frequency_values else None
            ),
            "average_memory_frequency_mhz": (
                statistics.fmean(memory_values) if memory_values else None
            ),
            "p95_memory_frequency_mhz": percentile(memory_values, 0.95),
            "maximum_temperature_c": max(thermal_values) if thermal_values else None,
            "maximum_thermal_status": maximum_thermal_status,
            "throttling_observed": maximum_thermal_status > 0,
            "scheduler": scheduler_points[-1] if scheduler_points else None,
            "average_whole_device_power_mw": (
                power_energy_mwh * 3600.0 / power_covered_duration_s
                if power_covered_duration_s > 0
                else None
            ),
            "power_covered_duration_s": power_covered_duration_s,
            "power_valid_for_consumption": power_covered_duration_s > 0,
        }

    rows: List[Dict[str, object]] = []
    accepted_foreground_keys: set[Tuple[str, str]] = set()
    for definition in definitions.values():
        windows = definition.get("windows", [])
        if not isinstance(windows, list):
            continue
        metrics = analyze_windows(windows)
        definition_key = (
            str(definition.get("phase") or ""),
            str(definition.get("name") or ""),
        )
        if (
            source_mode == "foreground_activity"
            and int(metrics.get("frame_count") or 0) <= 0
            and int(metrics.get("detailed_frame_count") or 0) <= 0
            and not isinstance(metrics.get("average_fps"), (int, float))
        ):
            continue
        if source_mode == "foreground_activity":
            accepted_foreground_keys.add(definition_key)
        rows.append(
            {
                **{key: value for key, value in definition.items() if key != "windows"},
                **metrics,
                "occurrence_count": len(windows),
                "windows": [
                    {
                        "start_uptime_s": start,
                        "end_uptime_s": end,
                        "start_elapsed_s": start - session_start,
                        "end_elapsed_s": end - session_start,
                    }
                    for start, end in windows
                ],
                "confidence": (
                    "low"
                    if float(metrics.get("duration_s") or 0.0) < minimum_reliable_duration_s
                    or int(metrics.get("frame_count") or 0) < 30
                    else "high"
                    if int(metrics.get("detailed_frame_count") or 0) >= 100
                    else "medium"
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            float(item.get("frame_issue_pct") or 0.0),
            float(item.get("frame_p99_ms") or 0.0),
        ),
        reverse=True,
    )

    span_rows: List[Dict[str, object]] = []
    for occurrence in occurrences:
        if source_mode == "foreground_activity" and (
            str(occurrence.get("phase") or ""),
            str(occurrence.get("name") or ""),
        ) not in accepted_foreground_keys:
            continue
        start = float(occurrence["start_uptime_s"])
        end = float(occurrence["end_uptime_s"])
        metrics = analyze_windows([(start, end)])
        span_rows.append(
            {
                **occurrence,
                **metrics,
                "start_elapsed_s": start - session_start,
                "end_elapsed_s": end - session_start,
                "confidence": (
                    "low"
                    if end - start < minimum_reliable_duration_s
                    or int(metrics.get("frame_count") or 0) < 30
                    else "medium"
                ),
            }
        )

    overlap_count = 0
    ordered_spans = sorted(span_rows, key=lambda item: float(item["start_uptime_s"]))
    for current, following in zip(ordered_spans, ordered_spans[1:]):
        if (
            float(following["start_uptime_s"]) < float(current["end_uptime_s"])
            and (current.get("phase"), current.get("name"))
            != (following.get("phase"), following.get("name"))
        ):
            overlap_count += 1

    effective_source_mode = source_mode if rows else "unavailable"
    frame_flow = performance_analysis.get("frame_flow", {})
    frame_flow = frame_flow if isinstance(frame_flow, dict) else {}
    platform = str(frame_flow.get("platform") or "android").lower()
    if platform == "harmony":
        limitations = (
            "测试窗口内的帧指标来自 SmartPerf 应用 FPS 与 fpsJitters，或原生 RenderService 合成节奏。"
            "fpsJitters 可用于 P95/P99 与 1% Low，但量产接口不拆分 RenderThread、BufferQueue、GPU 或 HWC 阶段；"
            "整机功率只作为同期上下文，不归因到进程或组件。"
        )
    elif platform == "ios":
        limitations = (
            "当前 iOS 后端不提供通用应用 FPS、1% Low 或逐帧阶段时间戳；"
            "测试窗口仅保留 CPU/GPU、SystemLoad、温度和前台状态等实际可观测上下文。"
        )
    else:
        limitations = (
            "测试窗口内聚合帧指标；专项开启且实际获得详细 framestats 时才用于阶段分析，"
            "否则使用 SurfaceFlinger / 周期帧计数窗口给出保守的 P95/P99 与 1% Low 上下文。"
            "整机功率只作为同期记录，不归因到进程、UID 或组件。"
        )
    return {
        "available": bool(rows),
        "analysis_mode": "performance",
        "source_mode": effective_source_mode,
        "source_label": (
            "导入的持续测试事件"
            if effective_source_mode == "external_events"
            else "前台应用 / Ability 区间"
            if effective_source_mode == "foreground_activity"
            else "未获得可归属的测试区间"
        ),
        "minimum_reliable_duration_s": minimum_reliable_duration_s,
        "row_count": len(rows),
        "span_count": len(span_rows),
        "overlap_count": overlap_count,
        "rows": rows,
        "spans": span_rows,
        "instant_events": [
            {
                "name": item.name,
                "phase": item.phase,
                "elapsed_s": item.device_uptime_s - session_start,
                "source": item.source,
            }
            for item in events
            if not item.duration_s and session_start <= item.device_uptime_s <= session_end
        ],
        "limitations": limitations,
    }


def analyze_android_frame_pipeline(
    contexts: Sequence[ContextSample],
) -> Dict[str, object]:
    seen_by_window: Dict[str, set[int]] = {}
    initialized_windows: set[str] = set()
    frames: List[Dict[str, object]] = []

    def timestamp(record: Dict[str, object], key: str) -> Optional[int]:
        value = record.get(key)
        if not isinstance(value, (int, float)):
            return None
        parsed = int(value)
        return parsed if 0 < parsed < 9_000_000_000_000_000_000 else None

    def duration_ms(
        record: Dict[str, object],
        end_key: str,
        start_key: str,
    ) -> Optional[float]:
        end = timestamp(record, end_key)
        start = timestamp(record, start_key)
        if end is None or start is None or end < start:
            return None
        value = (end - start) / 1_000_000.0
        return value if value <= 5000.0 else None

    for context in sorted(contexts, key=lambda item: item.uptime_s):
        records = context.performance.get("frame_records")
        if not isinstance(records, list) or not records:
            continue
        window = str(
            context.performance.get("foreground_window_name")
            or context.foreground_activity
            or context.foreground_package
            or "unknown"
        )
        seen = seen_by_window.setdefault(window, set())
        parsed_records: List[Tuple[int, Dict[str, object]]] = []
        for raw in records:
            if not isinstance(raw, dict):
                continue
            frame_id = timestamp(raw, "FrameTimelineVsyncId")
            intended = timestamp(raw, "IntendedVsync")
            key = frame_id or intended
            if key is None:
                continue
            parsed_records.append((key, raw))
        if window not in initialized_windows:
            seen.update(key for key, _ in parsed_records)
            initialized_windows.add(window)
            continue
        for key, record in parsed_records:
            if key in seen:
                continue
            seen.add(key)
            if int(record.get("Flags") or 0) != 0:
                continue
            total_ms = duration_ms(record, "FrameCompleted", "IntendedVsync")
            if total_ms is None:
                continue
            intended = timestamp(record, "IntendedVsync")
            deadline = timestamp(record, "FrameDeadline")
            completed = timestamp(record, "FrameCompleted")
            dequeue = record.get("DequeueBufferDuration")
            queue = record.get("QueueBufferDuration")
            stages = {
                "vsync_delay_ms": duration_ms(record, "Vsync", "IntendedVsync"),
                "input_ms": duration_ms(record, "AnimationStart", "HandleInputStart"),
                "animation_ms": duration_ms(
                    record,
                    "PerformTraversalsStart",
                    "AnimationStart",
                ),
                "traversal_ms": duration_ms(
                    record,
                    "DrawStart",
                    "PerformTraversalsStart",
                ),
                "draw_ms": duration_ms(record, "SyncQueued", "DrawStart"),
                "sync_ms": duration_ms(
                    record,
                    "IssueDrawCommandsStart",
                    "SyncStart",
                ),
                "command_ms": duration_ms(
                    record,
                    "SwapBuffers",
                    "IssueDrawCommandsStart",
                ),
                "gpu_wait_ms": duration_ms(record, "GpuCompleted", "SwapBuffers"),
                "post_swap_ms": duration_ms(
                    record,
                    "FrameCompleted",
                    "SwapBuffers",
                ),
                "dequeue_ms": (
                    float(dequeue) / 1_000_000.0
                    if isinstance(dequeue, (int, float)) and 0 <= float(dequeue) < 5e9
                    else None
                ),
                "queue_ms": (
                    float(queue) / 1_000_000.0
                    if isinstance(queue, (int, float)) and 0 <= float(queue) < 5e9
                    else None
                ),
            }
            frames.append(
                {
                    "frame_id": key,
                    "window": window,
                    "context_uptime_s": context.uptime_s,
                    "intended_vsync_ns": intended,
                    "total_ms": total_ms,
                    "deadline_missed": bool(
                        deadline is not None
                        and completed is not None
                        and completed > deadline
                    ),
                    **stages,
                }
            )

    stage_labels = {
        "vsync_delay_ms": "VSync / 调度起步",
        "input_ms": "输入处理",
        "animation_ms": "动画",
        "traversal_ms": "布局 / 遍历",
        "draw_ms": "UI 绘制",
        "sync_ms": "同步 / 上传准备",
        "command_ms": "渲染命令提交",
        "gpu_wait_ms": "GPU 完成等待",
        "post_swap_ms": "BufferQueue / 合成等待",
        "dequeue_ms": "DequeueBuffer",
        "queue_ms": "QueueBuffer",
    }
    stage_rows: List[Dict[str, object]] = []
    for key, label in stage_labels.items():
        values = [
            float(item[key])
            for item in frames
            if isinstance(item.get(key), (int, float))
        ]
        if not values:
            continue
        stage_rows.append(
            {
                "key": key,
                "label": label,
                "sample_count": len(values),
                "average_ms": statistics.fmean(values),
                "p95_ms": percentile(values, 0.95),
                "p99_ms": percentile(values, 0.99),
                "maximum_ms": max(values),
            }
        )
    stage_rows.sort(key=lambda item: float(item.get("p95_ms") or 0.0), reverse=True)
    totals = [float(item["total_ms"]) for item in frames]
    deadline_missed = sum(1 for item in frames if item.get("deadline_missed"))
    return {
        "available": bool(frames),
        "frame_count": len(frames),
        "average_total_ms": statistics.fmean(totals) if totals else None,
        "p95_total_ms": percentile(totals, 0.95),
        "p99_total_ms": percentile(totals, 0.99),
        "maximum_total_ms": max(totals) if totals else None,
        "deadline_missed_count": deadline_missed,
        "deadline_missed_pct": (
            deadline_missed / len(frames) * 100.0 if frames else None
        ),
        "dominant_stage": stage_rows[0] if stage_rows else None,
        "stages": stage_rows,
        "slow_frames": sorted(
            frames,
            key=lambda item: float(item.get("total_ms") or 0.0),
            reverse=True,
        )[:30],
        "timeline": sorted(
            frames,
            key=lambda item: int(item.get("intended_vsync_ns") or 0),
        ),
        "limitations": (
            "Stages are derived from Android gfxinfo framestats timestamps for newly observed "
            "foreground-window frames. They describe the app UI/RenderThread submission path; "
            "DisplayPresentTime and hardware-composer internals may remain unavailable."
        ),
    }


def analyze_performance_contexts(
    contexts: Sequence[ContextSample],
    metadata: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    ordered = sorted(contexts, key=lambda item: item.uptime_s)
    metadata = metadata or {}
    probe = metadata.get("performance_probe", {})
    probe = probe if isinstance(probe, dict) else {}
    touch_probe = metadata.get("touch", {})
    touch_probe = touch_probe if isinstance(touch_probe, dict) else {}

    performance_contexts = [
        item for item in ordered if isinstance(item.performance, dict) and item.performance
    ]
    latest_context = performance_contexts[-1] if performance_contexts else None
    latest = latest_context.performance if latest_context is not None else probe

    platform = str(metadata.get("platform") or latest.get("platform") or "").lower()
    if not platform:
        if "refresh_rate_counts" in latest or latest.get("smartperf_source"):
            platform = "harmony"
        elif "frame_counter_total" in latest or latest.get("surface_frame_source"):
            platform = "android"
    is_android = platform == "android"
    capture_features = capture_features_from_metadata(metadata)
    frame_details_enabled = bool(capture_features.get("frame_details", True))
    render_pipeline = (
        analyze_android_frame_pipeline(performance_contexts)
        if is_android
        else {
            "available": False,
            "frame_count": 0,
            "average_total_ms": None,
            "p95_total_ms": None,
            "p99_total_ms": None,
            "maximum_total_ms": None,
            "deadline_missed_count": 0,
            "deadline_missed_pct": None,
            "dominant_stage": None,
            "stages": [],
            "slow_frames": [],
            "timeline": [],
            "limitations": (
                "Detailed per-frame render-stage timestamps were not observed for this platform."
            ),
        }
    )

    def latest_value(key: str) -> object:
        value = latest.get(key) if isinstance(latest, dict) else None
        return probe.get(key) if value in (None, "", [], {}) else value

    smartperf_contexts = [
        item
        for item in performance_contexts
        if bool(item.performance.get("smartperf_source"))
    ]
    smartperf_unavailable_reason = next(
        (
            str(item.performance.get("frame_unavailable_reason") or "").strip()
            for item in reversed(smartperf_contexts)
            if str(item.performance.get("frame_unavailable_reason") or "").strip()
        ),
        "",
    )
    if "display is inactive" in smartperf_unavailable_reason.lower():
        smartperf_unavailable_reason = (
            "屏幕未处于活动状态；SmartPerf 报告的 FPS 不代表当前目标应用输出。"
        )
    elif "target" in smartperf_unavailable_reason.lower() and "foreground" in smartperf_unavailable_reason.lower():
        smartperf_unavailable_reason = (
            "目标应用未处于前台；该 SmartPerf FPS 不作为当前目标应用帧率。"
        )

    def context_screen_active(item: ContextSample) -> bool:
        screen_state = str(item.screen_state or "").strip().lower()
        if platform == "harmony":
            return screen_state in {"awake", "on"}
        return not screen_state or screen_state in {"awake", "on"}

    def context_report_break(item: ContextSample) -> bool:
        return bool(
            getattr(item, "_report_break_before", False)
            or item.performance.get("report_break_before") is True
        )

    harmony_display_contexts = [
        item
        for item in ordered
        if item.source == "harmony_ability_render_service"
        or bool(item.performance.get("render_service_compositor_source"))
    ]
    non_smartperf_refresh_contexts = [
        item
        for item in ordered
        if not bool(item.performance.get("smartperf_source"))
    ]
    refresh_contexts = (
        harmony_display_contexts
        if platform == "harmony" and harmony_display_contexts
        else non_smartperf_refresh_contexts
        if platform == "harmony" and non_smartperf_refresh_contexts
        else ordered
    )

    refresh_values = [
        float(item.refresh_rate_hz)
        for item in refresh_contexts
        if context_screen_active(item)
        and isinstance(item.refresh_rate_hz, (int, float))
        and item.refresh_rate_hz > 0
    ]
    current_refresh = refresh_values[-1] if refresh_values else None
    if (
        current_refresh is None
        and (
            (platform != "harmony" and latest_context is None)
            or (latest_context is not None and context_screen_active(latest_context))
        )
        and isinstance(latest.get("refresh_rate_hz"), (int, float))
    ):
        current_refresh = float(latest["refresh_rate_hz"])

    supported_refresh_rates = set()
    for source in [probe, *[item.performance for item in performance_contexts]]:
        values = source.get("supported_refresh_rates_hz", []) if isinstance(source, dict) else []
        if isinstance(values, list):
            supported_refresh_rates.update(
                float(value)
                for value in values
                if isinstance(value, (int, float)) and float(value) > 0
            )

    refresh_residency: List[Dict[str, object]] = []
    residency_source = None
    duration_counter_contexts = [
        item
        for item in performance_contexts
        if isinstance(item.performance.get("refresh_rate_durations_s"), dict)
        and item.performance.get("refresh_rate_durations_s")
    ]
    if len(duration_counter_contexts) >= 2:
        accumulated_durations: Dict[float, float] = {}
        for previous, current in zip(
            duration_counter_contexts,
            duration_counter_contexts[1:],
        ):
            if context_report_break(current):
                continue
            previous_durations = previous.performance.get("refresh_rate_durations_s")
            current_durations = current.performance.get("refresh_rate_durations_s")
            if not isinstance(previous_durations, dict) or not isinstance(
                current_durations, dict
            ):
                continue
            for key in set(previous_durations) | set(current_durations):
                try:
                    rate = float(key)
                    start_duration = float(previous_durations.get(key, 0.0))
                    end_duration = float(current_durations.get(key, 0.0))
                except (TypeError, ValueError):
                    continue
                delta = end_duration - start_duration
                if rate > 0 and delta > 0:
                    accumulated_durations[rate] = (
                        accumulated_durations.get(rate, 0.0) + delta
                    )
        duration_rows = sorted(accumulated_durations.items())
        total_duration = sum(item[1] for item in duration_rows)
        refresh_residency = [
            {
                "refresh_rate_hz": rate,
                "count": None,
                "estimated_duration_s": duration,
                "share_pct": duration / total_duration * 100.0 if total_duration > 0 else 0.0,
            }
            for rate, duration in sorted(duration_rows)
        ]
        if refresh_residency:
            residency_source = "Android SurfaceFlinger refresh-rate duration delta"

    counter_contexts = [
        item
        for item in refresh_contexts
        if isinstance(item.performance.get("refresh_rate_counts"), dict)
        and item.performance.get("refresh_rate_counts")
    ]
    if platform == "harmony" and not refresh_residency and len(counter_contexts) >= 2:
        accumulated_counts: Dict[float, int] = {}
        accumulated_durations: Dict[float, float] = {}
        for previous, current in zip(refresh_contexts, refresh_contexts[1:]):
            if context_report_break(current):
                continue
            previous_screen_state = str(previous.screen_state or "").strip().lower()
            current_screen_state = str(current.screen_state or "").strip().lower()
            if previous_screen_state not in {"awake", "on"} or current_screen_state not in {
                "awake",
                "on",
            }:
                continue
            previous_counts = previous.performance.get("refresh_rate_counts")
            current_counts = current.performance.get("refresh_rate_counts")
            if (
                not isinstance(previous_counts, dict)
                or not previous_counts
                or not isinstance(current_counts, dict)
                or not current_counts
            ):
                continue
            for key in sorted(
                set(previous_counts) & set(current_counts),
                key=lambda value: float(value),
            ):
                try:
                    rate = float(key)
                    start_count = int(previous_counts[key])
                    end_count = int(current_counts[key])
                except (TypeError, ValueError):
                    continue
                delta = end_count - start_count
                if rate <= 0 or delta <= 0:
                    continue
                accumulated_counts[rate] = accumulated_counts.get(rate, 0) + delta
                accumulated_durations[rate] = (
                    accumulated_durations.get(rate, 0.0) + delta / rate
                )
        weighted_rows = [
            (rate, accumulated_counts[rate], accumulated_durations[rate])
            for rate in sorted(accumulated_durations)
        ]
        total_weight = sum(row[2] for row in weighted_rows)
        for rate, count, estimated_duration in weighted_rows:
            refresh_residency.append(
                {
                    "refresh_rate_hz": rate,
                    "count": count,
                    "estimated_duration_s": estimated_duration,
                    "share_pct": (
                        estimated_duration / total_weight * 100.0 if total_weight > 0 else 0.0
                    ),
                }
            )
        if refresh_residency:
            residency_source = "HarmonyOS RenderService fpsCount delta"

    if not refresh_residency and len(ordered) >= 2:
        durations: Dict[float, float] = {}
        for current, following in zip(ordered, ordered[1:]):
            if context_report_break(following):
                continue
            if not isinstance(current.refresh_rate_hz, (int, float)) or current.refresh_rate_hz <= 0:
                continue
            if not context_screen_active(current):
                continue
            delta = following.uptime_s - current.uptime_s
            if 0 < delta <= 60.0:
                rate = float(current.refresh_rate_hz)
                durations[rate] = durations.get(rate, 0.0) + delta
        total_duration = sum(durations.values())
        refresh_residency = [
            {
                "refresh_rate_hz": rate,
                "count": None,
                "estimated_duration_s": duration,
                "share_pct": duration / total_duration * 100.0 if total_duration > 0 else 0.0,
            }
            for rate, duration in sorted(durations.items())
        ]
        if refresh_residency:
            residency_source = "context sample intervals"

    all_frame_rows = [
        item.performance
        for item in performance_contexts
        if context_screen_active(item)
        and isinstance(item.performance.get("frame_sample_count"), (int, float))
        and float(item.performance.get("frame_sample_count") or 0) > 0
    ]
    surface_frame_rows = [
        item for item in all_frame_rows if bool(item.get("surface_frame_source"))
    ]
    smartperf_frame_rows = [
        item for item in all_frame_rows if bool(item.get("smartperf_source"))
    ]
    legacy_render_service_frame_rows = [
        item
        for item in all_frame_rows
        if not bool(item.get("smartperf_source"))
        and not bool(item.get("surface_frame_source"))
    ]
    explicit_render_service_frame_rows = [
        item
        for item in legacy_render_service_frame_rows
        if bool(item.get("render_service_compositor_source"))
    ]
    render_service_frame_rows = (
        explicit_render_service_frame_rows or legacy_render_service_frame_rows
    )
    using_surface_frames = is_android and bool(surface_frame_rows)
    using_smartperf_frames = platform == "harmony" and bool(smartperf_frame_rows)
    frame_rows = (
        surface_frame_rows
        if using_surface_frames
        else smartperf_frame_rows
        if using_smartperf_frames
        else all_frame_rows
    )
    frame_sample_count = sum(int(item.get("frame_sample_count") or 0) for item in frame_rows)
    presented_intervals = [
        float(value)
        for item in frame_rows
        for value in (
            item.get("frame_intervals_ms", [])
            if isinstance(item.get("frame_intervals_ms"), list)
            else []
        )
        if isinstance(value, (int, float)) and float(value) > 0
    ]
    if presented_intervals:
        frame_sample_count = len(presented_intervals)

    counter_frame_contexts = [
        item
        for item in performance_contexts
        if isinstance(item.performance.get("frame_counter_total"), (int, float))
    ]
    counter_frame_count = 0
    counter_frame_duration_s = 0.0
    counter_frame_rates: List[float] = []
    counter_deadline_missed = 0
    counter_missed_vsync = 0
    counter_janky = 0
    counter_histogram: Dict[float, int] = {}
    counter_pair_count = 0
    counter_timeline: List[Dict[str, object]] = []

    for previous, current in zip(counter_frame_contexts, counter_frame_contexts[1:]):
        if context_report_break(current):
            continue
        previous_screen = str(previous.screen_state or "").strip().lower()
        current_screen = str(current.screen_state or "").strip().lower()
        if (
            (previous_screen and previous_screen not in {"awake", "on"})
            or (current_screen and current_screen not in {"awake", "on"})
        ):
            continue
        previous_package = str(previous.foreground_package or "")
        current_package = str(current.foreground_package or "")
        previous_window = str(previous.performance.get("foreground_window_name") or "")
        current_window = str(current.performance.get("foreground_window_name") or "")
        if previous_package != current_package:
            continue
        if previous_window and current_window and previous_window != current_window:
            continue
        elapsed = current.uptime_s - previous.uptime_s
        if elapsed <= 0 or elapsed > 60.0:
            continue
        previous_total = int(previous.performance.get("frame_counter_total") or 0)
        current_total = int(current.performance.get("frame_counter_total") or 0)
        delta_total = current_total - previous_total
        if delta_total < 0:
            continue
        counter_pair_count += 1
        counter_frame_count += delta_total
        counter_frame_duration_s += elapsed
        counter_frame_rates.append(delta_total / elapsed)
        pair_counters = {"deadline": 0, "vsync": 0, "janky": 0}

        for source_key, target_name in (
            ("frame_counter_deadline_missed", "deadline"),
            ("frame_counter_missed_vsync", "vsync"),
            ("frame_counter_janky", "janky"),
        ):
            previous_value = previous.performance.get(source_key)
            current_value = current.performance.get(source_key)
            if not isinstance(previous_value, (int, float)) or not isinstance(
                current_value, (int, float)
            ):
                continue
            delta = int(current_value) - int(previous_value)
            if delta < 0:
                continue
            pair_counters[target_name] = delta
            if target_name == "deadline":
                counter_deadline_missed += delta
            elif target_name == "vsync":
                counter_missed_vsync += delta
            else:
                counter_janky += delta

        previous_histogram = previous.performance.get("frame_histogram_ms")
        current_histogram = current.performance.get("frame_histogram_ms")
        pair_histogram: Dict[float, int] = {}
        if isinstance(previous_histogram, dict) and isinstance(current_histogram, dict):
            for key in set(previous_histogram) | set(current_histogram):
                try:
                    bucket = float(key)
                    delta = int(current_histogram.get(key, 0)) - int(
                        previous_histogram.get(key, 0)
                    )
                except (TypeError, ValueError):
                    continue
                if bucket >= 0 and delta > 0:
                    counter_histogram[bucket] = counter_histogram.get(bucket, 0) + delta
                    pair_histogram[bucket] = pair_histogram.get(bucket, 0) + delta

        pair_histogram_count = sum(pair_histogram.values())
        pair_average_ms = (
            sum(bucket * count for bucket, count in pair_histogram.items())
            / pair_histogram_count
            if pair_histogram_count > 0
            else None
        )
        pair_slowest_ms = histogram_slowest_average(pair_histogram, 0.01)
        counter_timeline.append(
            {
                "uptime_s": current.uptime_s,
                "duration_s": elapsed,
                "frame_count": delta_total,
                "frame_rate_fps": delta_total / elapsed,
                "frame_time_average_ms": pair_average_ms,
                "frame_time_p95_ms": histogram_percentile(pair_histogram, 0.95),
                "frame_time_p99_ms": histogram_percentile(pair_histogram, 0.99),
                "one_percent_low_fps": (
                    1000.0 / pair_slowest_ms
                    if isinstance(pair_slowest_ms, (int, float)) and pair_slowest_ms > 0
                    else None
                ),
                "deadline_missed_count": pair_counters["deadline"],
                "missed_vsync_count": pair_counters["vsync"],
                "janky_frame_count": pair_counters["janky"],
                "frame_issue_count": pair_counters["deadline"],
                "frame_issue_pct": (
                    pair_counters["deadline"] / delta_total * 100.0
                    if delta_total > 0
                    else 0.0
                ),
                "refresh_rate_hz": current.refresh_rate_hz,
                "report_break_before": context_report_break(previous),
            }
        )

    histogram_count = sum(counter_histogram.values())
    counter_frame_average_ms = (
        sum(bucket * count for bucket, count in counter_histogram.items()) / histogram_count
        if histogram_count > 0
        else None
    )
    counter_frame_p95_ms = histogram_percentile(counter_histogram, 0.95)
    counter_frame_p99_ms = histogram_percentile(counter_histogram, 0.99)
    counter_slowest_frame_average_ms = histogram_slowest_average(counter_histogram, 0.01)
    counter_one_percent_low_fps = (
        1000.0 / counter_slowest_frame_average_ms
        if isinstance(counter_slowest_frame_average_ms, (int, float))
        and counter_slowest_frame_average_ms > 0
        else None
    )
    window_frame_rate_p1_reference_fps = (
        percentile(counter_frame_rates, 0.01) if counter_frame_rates else None
    )
    window_frame_rate_p1_reference_source = (
        "Android gfxinfo counter-window FPS P1 (reference only)"
        if counter_frame_rates
        else None
    )

    def weighted_source_value(
        rows: Sequence[Dict[str, object]],
        key: str,
    ) -> Optional[float]:
        values = [
            (float(item[key]), int(item.get("frame_sample_count") or 0))
            for item in rows
            if isinstance(item.get(key), (int, float))
        ]
        weight = sum(item[1] for item in values)
        return sum(value * count for value, count in values) / weight if weight > 0 else None

    def weighted_frame_value(key: str) -> Optional[float]:
        return weighted_source_value(frame_rows, key)

    def source_frame_rate(rows: Sequence[Dict[str, object]]) -> Optional[float]:
        intervals = [
            float(value)
            for item in rows
            for value in (
                item.get("frame_intervals_ms", [])
                if isinstance(item.get("frame_intervals_ms"), list)
                else item.get("frame_interval_values_ms", [])
                if isinstance(item.get("frame_interval_values_ms"), list)
                else []
            )
            if isinstance(value, (int, float)) and float(value) > 0
        ]
        if intervals:
            average_interval = statistics.fmean(intervals)
            return 1000.0 / average_interval if average_interval > 0 else None
        return weighted_source_value(rows, "compositor_fps")

    surface_present_fps = source_frame_rate(surface_frame_rows)
    smartperf_app_fps = source_frame_rate(smartperf_frame_rows)
    render_service_compositor_fps = source_frame_rate(render_service_frame_rows)
    gfxinfo_submission_fps = (
        counter_frame_count / counter_frame_duration_s
        if counter_pair_count > 0 and counter_frame_duration_s > 0
        else None
    )

    fps_values = [
        float(item["compositor_fps"])
        for item in frame_rows
        if isinstance(item.get("compositor_fps"), (int, float))
    ]
    p95_values = [
        float(item["frame_interval_p95_ms"])
        for item in frame_rows
        if isinstance(item.get("frame_interval_p95_ms"), (int, float))
    ]
    missed_intervals = sum(
        int(item.get("missed_vsync_interval_count") or 0) for item in frame_rows
    )
    severe_intervals = sum(
        int(item.get("severe_frame_interval_count") or 0) for item in frame_rows
    )
    frozen_intervals = sum(
        int(item.get("frozen_frame_interval_count") or 0) for item in frame_rows
    )
    compositor_fps = (
        1000.0 / statistics.fmean(presented_intervals)
        if presented_intervals
        else weighted_frame_value("compositor_fps")
    )
    compositor_minimum_fps = min(fps_values) if fps_values else None
    compositor_frame_average_ms = (
        statistics.fmean(presented_intervals)
        if presented_intervals
        else weighted_frame_value("frame_interval_average_ms")
    )
    compositor_frame_p95_ms = (
        percentile(presented_intervals, 0.95)
        if presented_intervals
        else max(p95_values) if p95_values else None
    )
    compositor_frame_p99_ms = (
        percentile(presented_intervals, 0.99)
        if presented_intervals
        else weighted_frame_value("frame_interval_p99_ms")
    )
    if presented_intervals:
        slow_count = max(1, math.ceil(len(presented_intervals) * 0.01))
        slow_average_ms = statistics.fmean(
            sorted(presented_intervals, reverse=True)[:slow_count]
        )
        compositor_one_percent_low_fps = (
            1000.0 / slow_average_ms if slow_average_ms > 0 else None
        )
    else:
        compositor_one_percent_low_fps = weighted_frame_value("one_percent_low_fps")
    compositor_missed_pct = (
        missed_intervals / frame_sample_count * 100.0 if frame_sample_count > 0 else None
    )

    using_gfxinfo_counters = (
        counter_pair_count > 0
        and counter_frame_count > 0
        and not using_surface_frames
    )
    if using_gfxinfo_counters:
        sampled_frame_rate = (
            counter_frame_count / counter_frame_duration_s
            if counter_frame_duration_s > 0
            else 0.0
        )
        minimum_sampled_frame_rate = min(counter_frame_rates) if counter_frame_rates else 0.0
        reported_frame_sample_count = counter_frame_count
        frame_metric_average_ms = counter_frame_average_ms
        frame_metric_p95_ms = counter_frame_p95_ms
        frame_metric_p99_ms = counter_frame_p99_ms
        one_percent_low_fps = counter_one_percent_low_fps
        one_percent_low_source = (
            "Android gfxinfo frame-time histogram slowest 1%"
            if counter_one_percent_low_fps is not None
            else None
        )
        one_percent_low_confidence = (
            "high" if counter_one_percent_low_fps is not None else None
        )
        frame_issue_count = counter_deadline_missed
        frame_issue_pct = (
            counter_deadline_missed / counter_frame_count * 100.0
            if counter_frame_count > 0
            else 0.0
        )
        frame_rate_source = "Android gfxinfo frame counter delta"
        frame_rate_label = "应用 UI 帧提交速率"
        frame_rate_unit = "帧/s"
        frame_metric_label = "应用帧耗时 P95"
        frame_issue_label = "超出帧截止时间"
    else:
        sampled_frame_rate = compositor_fps
        minimum_sampled_frame_rate = compositor_minimum_fps
        reported_frame_sample_count = frame_sample_count
        frame_metric_average_ms = compositor_frame_average_ms
        frame_metric_p95_ms = compositor_frame_p95_ms
        frame_metric_p99_ms = compositor_frame_p99_ms
        one_percent_low_fps = compositor_one_percent_low_fps
        if window_frame_rate_p1_reference_fps is None and fps_values:
            window_frame_rate_p1_reference_fps = percentile(fps_values, 0.01)
            window_frame_rate_p1_reference_source = (
                "sampled-window FPS P1 (reference only)"
            )
        frame_issue_count = missed_intervals
        frame_issue_pct = compositor_missed_pct
        if is_android:
            if using_surface_frames:
                one_percent_low_source = (
                    "Android SurfaceFlinger presented-frame slowest 1%"
                    if compositor_one_percent_low_fps is not None
                    else None
                )
                one_percent_low_confidence = (
                    "high" if compositor_one_percent_low_fps is not None
                    else None
                )
                frame_rate_source = (
                    "Android SurfaceFlinger foreground application-layer present timestamps"
                )
                frame_rate_label = "应用呈现帧率"
                frame_rate_unit = "FPS"
                frame_metric_label = "呈现帧间隔 P95"
                frame_issue_label = "节奏校正后超出有效帧预算"
            else:
                one_percent_low_source = None
                one_percent_low_confidence = None
                frame_rate_source = None
                frame_rate_label = "应用 UI 帧提交速率"
                frame_rate_unit = "帧/s"
                frame_metric_label = "应用帧耗时 P95"
                frame_issue_label = "超出帧截止时间"
        else:
            smartperf_rows = any(item.get("smartperf_source") for item in frame_rows)
            if smartperf_rows and compositor_one_percent_low_fps is not None:
                one_percent_low_source = "HarmonyOS SmartPerf frame-jitter slowest 1%"
                one_percent_low_confidence = "high"
            else:
                one_percent_low_fps = None
                one_percent_low_source = None
                one_percent_low_confidence = None
            frame_rate_source = (
                "HarmonyOS SmartPerf SP_daemon app FPS and frame jitter"
                if smartperf_rows
                else "HarmonyOS RenderService composer fps sampled windows" if frame_rows else None
            )
            frame_rate_label = "应用 FPS" if smartperf_rows else "合成器 FPS"
            frame_rate_unit = "FPS"
            frame_metric_label = "帧间隔 P95"
            frame_issue_label = "跨越刷新槽位"

    hitch_totals = {
        "hitch_over_16_67ms": 0,
        "hitch_over_33ms": 0,
        "hitch_over_66ms": 0,
    }
    previous_hitches: Dict[str, Dict[str, int]] = {}
    for item in performance_contexts:
        values = item.performance
        window = str(values.get("foreground_window_name") or "unknown")
        current_hitches = {
            key: int(values.get(key) or 0)
            for key in hitch_totals
            if isinstance(values.get(key), (int, float))
        }
        previous = previous_hitches.get(window)
        if previous is not None:
            for key, value in current_hitches.items():
                delta = value - previous.get(key, 0)
                if delta > 0:
                    hitch_totals[key] += delta
        if current_hitches:
            previous_hitches[window] = current_hitches

    touch_contexts = [
        item
        for item in performance_contexts
        if isinstance(item.performance.get("touch_down_times_us"), list)
    ]
    new_touches = set()
    if touch_contexts:
        known_touches = {
            int(value)
            for value in touch_contexts[0].performance.get("touch_down_times_us", [])
            if isinstance(value, (int, float))
        }
        for item in touch_contexts[1:]:
            for value in item.performance.get("touch_down_times_us", []):
                if not isinstance(value, (int, float)):
                    continue
                parsed = int(value)
                if parsed not in known_touches:
                    new_touches.add(parsed)
                    known_touches.add(parsed)
    context_duration_s = (
        performance_contexts[-1].uptime_s - performance_contexts[0].uptime_s
        if len(performance_contexts) >= 2
        else 0.0
    )
    touch_interaction_count = len(new_touches) if len(touch_contexts) >= 2 else None
    frame_overproduction_ratio = (
        sampled_frame_rate / current_refresh
        if isinstance(sampled_frame_rate, (int, float))
        and isinstance(current_refresh, (int, float))
        and current_refresh > 0
        and using_gfxinfo_counters
        else None
    )
    display_to_frame_ratio = (
        current_refresh / sampled_frame_rate
        if isinstance(sampled_frame_rate, (int, float))
        and sampled_frame_rate >= 15.0
        and isinstance(current_refresh, (int, float))
        and current_refresh > 0
        else None
    )
    cadence_multiplier = None
    if isinstance(display_to_frame_ratio, (int, float)):
        rounded_multiplier = round(display_to_frame_ratio)
        if 2 <= rounded_multiplier <= 4 and abs(display_to_frame_ratio - rounded_multiplier) <= 0.12:
            cadence_multiplier = rounded_multiplier

    interpolation_status = str(latest_value("frame_interpolation_status") or "unavailable")
    interpolation_label = str(
        latest_value("frame_interpolation_label") or "系统未公开可验证的插帧开关"
    )
    interpolation_confidence = str(
        latest_value("frame_interpolation_confidence") or "low"
    )
    interpolation_scope = str(
        latest_value("frame_interpolation_scope") or "none"
    )
    interpolation_evidence = latest_value("frame_interpolation_evidence")
    interpolation_evidence = (
        list(interpolation_evidence)
        if isinstance(interpolation_evidence, list)
        else []
    )
    if interpolation_status == "unavailable" and cadence_multiplier is not None:
        interpolation_status = "indeterminate"
        interpolation_label = (
            f"显示刷新率约为应用帧率 {cadence_multiplier} 倍；"
            "仅凭系统计数无法区分重复帧与插帧"
        )

    display_width = latest_value("display_width_px")
    display_height = latest_value("display_height_px")
    render_width = render_height = render_resolution_source = None
    render_resolution_evidence = None
    render_resolution_estimated = False
    render_resolution_available = False
    render_sources = [
        item.performance
        for item in reversed(performance_contexts)
        if isinstance(item.performance, dict)
    ] + [probe]
    for source in render_sources:
        candidate_width = source.get("render_width_px")
        candidate_height = source.get("render_height_px")
        candidate_source = str(source.get("render_resolution_source") or "")
        candidate_estimated = bool(source.get("render_resolution_estimated"))
        unreliable_source = (
            not candidate_source
            or candidate_source == "active display mode fallback"
            or candidate_source.lower().startswith("windowmanager")
        )
        if (
            isinstance(candidate_width, (int, float))
            and float(candidate_width) > 0
            and isinstance(candidate_height, (int, float))
            and float(candidate_height) > 0
            and not candidate_estimated
            and not unreliable_source
        ):
            render_width = candidate_width
            render_height = candidate_height
            render_resolution_source = candidate_source
            render_resolution_evidence = source.get("render_resolution_evidence")
            render_resolution_available = True
            break
    if not render_resolution_available:
        render_width = None
        render_height = None
        render_resolution_source = None
        render_resolution_evidence = None
        render_resolution_estimated = False
    render_scale_pct = (
        max(
            min(
                float(render_width) / float(display_width),
                float(render_height) / float(display_height),
            ),
            min(
                float(render_width) / float(display_height),
                float(render_height) / float(display_width),
            ),
        )
        * 100.0
        if render_resolution_available
        and isinstance(display_width, (int, float))
        and isinstance(display_height, (int, float))
        and display_width > 0
        and display_height > 0
        else None
    )

    surface_contexts = [
        item
        for item in performance_contexts
        if bool(item.performance.get("surface_frame_source"))
    ]
    smartperf_contexts = [
        item
        for item in performance_contexts
        if bool(item.performance.get("smartperf_source"))
    ]
    legacy_render_service_contexts = [
        item
        for item in performance_contexts
        if not bool(item.performance.get("smartperf_source"))
        and not bool(item.performance.get("surface_frame_source"))
    ]
    explicit_render_service_contexts = [
        item
        for item in legacy_render_service_contexts
        if bool(item.performance.get("render_service_compositor_source"))
    ]
    render_service_contexts = (
        explicit_render_service_contexts or legacy_render_service_contexts
    )
    primary_frame_contexts = (
        surface_contexts
        if using_surface_frames
        else smartperf_contexts
        if using_smartperf_frames
        else render_service_contexts
    )

    frame_rate_timeline = list(counter_timeline) if using_gfxinfo_counters else []
    if not frame_rate_timeline:
        frame_rate_timeline = [
            {
                "uptime_s": item.uptime_s,
                "duration_s": (
                    float(item.performance.get("frame_sample_count") or 0)
                    * float(item.performance.get("frame_interval_average_ms") or 0)
                    / 1000.0
                    if isinstance(
                        item.performance.get("frame_interval_average_ms"),
                        (int, float),
                    )
                    else None
                ),
                "frame_count": item.performance.get("frame_sample_count"),
                "frame_rate_fps": item.performance.get("compositor_fps"),
                "frame_intervals_ms": item.performance.get("frame_intervals_ms", []),
                "frame_time_average_ms": item.performance.get("frame_interval_average_ms"),
                "frame_time_p95_ms": item.performance.get("frame_interval_p95_ms"),
                "frame_time_p99_ms": item.performance.get("frame_interval_p99_ms"),
                "one_percent_low_fps": item.performance.get("one_percent_low_fps"),
                "frame_budget_ms": item.performance.get("frame_budget_ms"),
                "frame_cadence_divisor": item.performance.get(
                    "frame_cadence_divisor"
                ),
                "frame_issue_count": int(
                    item.performance.get("frame_issue_count")
                    or item.performance.get("missed_vsync_interval_count")
                    or item.performance.get("deadline_missed_count")
                    or 0
                ),
                "frame_issue_pct": (
                    float(
                        item.performance.get("frame_issue_count")
                        or item.performance.get("missed_vsync_interval_count")
                        or item.performance.get("deadline_missed_count")
                        or 0
                    )
                    / float(item.performance.get("frame_sample_count") or 1)
                    * 100.0
                ),
                "refresh_rate_hz": item.refresh_rate_hz,
                "report_break_before": context_report_break(item),
            }
            for item in primary_frame_contexts
            if context_screen_active(item)
            and isinstance(item.performance.get("compositor_fps"), (int, float))
        ]

    one_percent_low_timeline_label: Optional[str] = None
    one_percent_low_timeline_source: Optional[str] = None
    rolling_one_percent_low_supported = using_surface_frames or using_smartperf_frames
    if frame_rate_timeline and rolling_one_percent_low_supported and any(
        isinstance(item.get("frame_intervals_ms"), list)
        and bool(item.get("frame_intervals_ms"))
        for item in frame_rate_timeline
    ):
        rolling_intervals: deque[float] = deque()
        rolling_coverage_s = 0.0
        previous_uptime: Optional[float] = None
        enriched_timeline: List[Dict[str, object]] = []
        for item in frame_rate_timeline:
            row = dict(item)
            uptime = (
                float(item["uptime_s"])
                if isinstance(item.get("uptime_s"), (int, float))
                else None
            )
            if (
                row.get("report_break_before") is True
                or (
                uptime is not None
                and previous_uptime is not None
                and uptime - previous_uptime > 10.0
                )
            ):
                rolling_intervals.clear()
                rolling_coverage_s = 0.0
            if uptime is not None:
                previous_uptime = uptime
            intervals = item.get("frame_intervals_ms", [])
            valid_intervals = [
                float(value)
                for value in intervals
                if isinstance(value, (int, float)) and 0.1 <= float(value) <= 2000.0
            ] if isinstance(intervals, list) else []
            for interval_ms in valid_intervals:
                rolling_intervals.append(interval_ms)
                rolling_coverage_s += interval_ms / 1000.0
            while (
                rolling_intervals
                and rolling_coverage_s - rolling_intervals[0] / 1000.0 >= 10.0
            ):
                rolling_coverage_s -= rolling_intervals.popleft() / 1000.0
            row["one_percent_low_fps"] = None
            row["one_percent_low_window_s"] = min(10.0, rolling_coverage_s)
            row["one_percent_low_sample_count"] = len(rolling_intervals)
            row["one_percent_low_timeline_source"] = "rolling_10s_presented_frame_intervals"
            if (
                valid_intervals
                and rolling_coverage_s >= 8.0
                and len(rolling_intervals) >= 120
            ):
                slow_count = max(1, math.ceil(len(rolling_intervals) * 0.01))
                slow_average_ms = statistics.fmean(
                    sorted(rolling_intervals, reverse=True)[:slow_count]
                )
                if slow_average_ms > 0:
                    row["one_percent_low_fps"] = 1000.0 / slow_average_ms
            enriched_timeline.append(row)
        frame_rate_timeline = enriched_timeline
        one_percent_low_timeline_label = "滚动 10 秒 1% Low"
        one_percent_low_timeline_source = "presented frame intervals, rolling 10-second window"
    elif frame_rate_timeline and platform == "harmony" and not using_smartperf_frames:
        frame_rate_timeline = [
            {**item, "one_percent_low_fps": None}
            for item in frame_rate_timeline
        ]
    elif any(
        isinstance(item.get("one_percent_low_fps"), (int, float))
        for item in frame_rate_timeline
    ):
        one_percent_low_timeline_label = "窗口 1% Low（参考）"
        one_percent_low_timeline_source = "per-sampled-window frame statistics"

    def context_frame_rate_timeline(
        source_contexts: Sequence[ContextSample],
    ) -> List[Dict[str, object]]:
        timeline: List[Dict[str, object]] = []
        for item in source_contexts:
            if not context_screen_active(item):
                continue
            value = item.performance.get("compositor_fps")
            if not isinstance(value, (int, float)):
                continue
            average_interval = item.performance.get("frame_interval_average_ms")
            frame_count = item.performance.get("frame_sample_count")
            duration_s = (
                float(frame_count) * float(average_interval) / 1000.0
                if isinstance(frame_count, (int, float))
                and isinstance(average_interval, (int, float))
                and float(average_interval) > 0
                else None
            )
            timeline.append(
                {
                    "uptime_s": item.uptime_s,
                    "duration_s": duration_s,
                    "frame_count": (
                        int(frame_count) if isinstance(frame_count, (int, float)) else None
                    ),
                    "value": float(value),
                    "frame_rate_fps": float(value),
                    "report_break_before": context_report_break(item),
                }
            )
        return timeline

    counter_flow_timeline = [
        {
            **item,
            "value": float(item["frame_rate_fps"]),
        }
        for item in counter_timeline
        if isinstance(item.get("frame_rate_fps"), (int, float))
    ]
    surface_flow_timeline = context_frame_rate_timeline(
        surface_contexts if using_surface_frames else render_service_contexts
    )
    smartperf_flow_timeline = context_frame_rate_timeline(smartperf_contexts)
    render_service_flow_timeline = context_frame_rate_timeline(
        render_service_contexts
    )

    def refresh_timeline_source(item: ContextSample) -> str:
        if platform == "android":
            return "Android DisplayManager active display mode"
        if platform == "harmony":
            if item.source == "harmony_ability_render_service":
                return "HarmonyOS RenderService current refresh rate"
            if bool(item.performance.get("smartperf_source")):
                return "HarmonyOS SmartPerf reported refresh rate"
            return "HarmonyOS display context current refresh rate"
        return str(item.source or "platform display context")

    refresh_points_by_uptime: Dict[float, Dict[str, object]] = {}
    for item in refresh_contexts:
        if (
            not isinstance(item.refresh_rate_hz, (int, float))
            or item.refresh_rate_hz <= 0
            or not context_screen_active(item)
        ):
            continue
        refresh_points_by_uptime[round(item.uptime_s, 6)] = {
            "uptime_s": item.uptime_s,
            "value": float(item.refresh_rate_hz),
            "refresh_rate_hz": float(item.refresh_rate_hz),
            "source": refresh_timeline_source(item),
            "report_break_before": context_report_break(item),
        }
    refresh_flow_points = [
        refresh_points_by_uptime[key] for key in sorted(refresh_points_by_uptime)
    ]
    refresh_flow_timeline: List[Dict[str, object]] = []
    for point in refresh_flow_points:
        if (
            not refresh_flow_timeline
            or point.get("report_break_before") is True
            or abs(float(point["value"]) - float(refresh_flow_timeline[-1]["value"]))
            > 0.1
        ):
            refresh_flow_timeline.append(point)
    if (
        refresh_flow_points
        and refresh_flow_timeline[-1]["uptime_s"]
        != refresh_flow_points[-1]["uptime_s"]
    ):
        refresh_flow_timeline.append(refresh_flow_points[-1])
    refresh_timeline_sources = list(
        dict.fromkeys(
            str(item.get("source") or "").strip()
            for item in refresh_flow_points
            if str(item.get("source") or "").strip()
        )
    )
    refresh_rate_timeline_source = (
        " + ".join(refresh_timeline_sources)
        if refresh_timeline_sources
        else "Android DisplayManager active display mode"
        if platform == "android"
        else "HarmonyOS RenderService current refresh rate"
        if platform == "harmony"
        else "platform active display mode"
    )

    def flow_metric(
        label: str,
        value: object,
        unit: str = "",
        digits: int = 1,
    ) -> Optional[Dict[str, object]]:
        if not isinstance(value, (int, float)):
            return None
        return {
            "label": label,
            "value": float(value),
            "unit": unit,
            "digits": digits,
        }

    def flow_stage(
        key: str,
        phase: str,
        label: str,
        status: str,
        *,
        value: object = None,
        unit: str = "",
        value_label: str = "",
        source: str = "",
        detail: str = "",
        sample_count: object = None,
        confidence: str = "",
        metrics: Sequence[Optional[Dict[str, object]]] = (),
        timeline: Sequence[Dict[str, object]] = (),
        timeline_unit: str = "FPS",
        timeline_value_label: str = "帧率",
    ) -> Dict[str, object]:
        parsed_sample_count = (
            int(sample_count) if isinstance(sample_count, (int, float)) else None
        )
        parsed_timeline = [item for item in timeline if isinstance(item, dict)]
        timeline_has_positive_value = any(
            isinstance(
                item.get("value", item.get("frame_rate_fps", item.get("refresh_rate_hz"))),
                (int, float),
            )
            and float(
                item.get("value", item.get("frame_rate_fps", item.get("refresh_rate_hz")))
                or 0.0
            )
            > 0
            for item in parsed_timeline
        )
        empty_invalid_stage = (
            status in {"invalid", "unavailable"}
            and not (isinstance(parsed_sample_count, int) and parsed_sample_count > 0)
            and not timeline_has_positive_value
        )
        return {
            "key": key,
            "phase": phase,
            "label": label,
            "status": status,
            "value": (
                None
                if empty_invalid_stage
                else float(value) if isinstance(value, (int, float)) else None
            ),
            "unit": unit,
            "value_label": value_label,
            "source": source,
            "detail": detail,
            "sample_count": parsed_sample_count,
            "confidence": "" if empty_invalid_stage else confidence,
            "metrics": (
                []
                if empty_invalid_stage
                else [item for item in metrics if isinstance(item, dict)]
            ),
            "timeline": (
                []
                if empty_invalid_stage
                else parsed_timeline
            ),
            "timeline_unit": timeline_unit,
            "timeline_value_label": timeline_value_label,
        }

    latest_screen_state = str(
        (latest_context.screen_state if latest_context is not None else "") or ""
    ).strip().lower()
    screen_active = (
        latest_screen_state in {"awake", "on"}
        if platform == "harmony"
        else not latest_screen_state or latest_screen_state in {"awake", "on"}
    )
    refresh_rate_unavailable_reason = (
        (
            "屏幕未处于活动状态；保留的刷新配置不代表当前显示输出。"
            if latest_screen_state
            else "无法确认当前屏幕状态；保留的刷新配置不作为当前显示输出。"
        )
        if platform == "harmony" and not screen_active
        else None
    )
    unavailable_reason = str(latest_value("frame_unavailable_reason") or "").strip()
    frame_flow_stages: List[Dict[str, object]] = []
    primary_flow_key: Optional[str] = None

    if is_android:
        if using_gfxinfo_counters:
            app_status = "primary"
            primary_flow_key = "app_submission"
            app_detail = (
                "前台窗口 gfxinfo 累计帧计数存在连续正向增量；当前作为应用提交速率主数据。"
            )
        elif counter_pair_count > 0 and counter_frame_count > 0:
            app_status = "reference"
            app_detail = (
                "该值只代表应用 UI 窗口提交；游戏前台应用缓冲层的真实呈现节奏以 "
                "SurfaceFlinger 时间戳为准。"
            )
        elif len(counter_frame_contexts) >= 2:
            app_status = "invalid"
            app_detail = unavailable_reason or (
                "gfxinfo 累计计数没有产生可用增量，可能是静态 UI、窗口切换或原生游戏渲染面未计入。"
            )
        else:
            app_status = "unavailable"
            app_detail = unavailable_reason or "前台窗口未公开连续 gfxinfo 帧计数。"
        frame_flow_stages.append(
            flow_stage(
                "app_submission",
                "APP",
                "应用 / UI 帧提交",
                app_status,
                value=gfxinfo_submission_fps,
                unit="帧/s",
                value_label="提交速率",
                source="Android gfxinfo cumulative frame counter delta",
                detail=app_detail,
                sample_count=counter_frame_count,
                confidence="high" if using_gfxinfo_counters else "low",
                metrics=(
                    flow_metric("1% Low", counter_one_percent_low_fps, "FPS", 1),
                    flow_metric("超时", counter_deadline_missed, "帧", 0),
                ),
                timeline=counter_flow_timeline,
                timeline_unit="帧/s",
                timeline_value_label="提交速率",
            )
        )

        pipeline_available = bool(render_pipeline.get("available"))
        frame_flow_stages.append(
            flow_stage(
                "render_queue",
                "RENDER",
                "RenderThread / BufferQueue",
                "valid" if pipeline_available else "unavailable",
                value=render_pipeline.get("p95_total_ms"),
                unit="ms",
                value_label="端到端 P95",
                source="Android gfxinfo framestats timestamps",
                detail=(
                    "逐帧时间戳用于拆分 UI、RenderThread、GPU 与 BufferQueue 等待；"
                    "它提供阶段延迟，不是独立 FPS 计数。"
                    if pipeline_available
                    else (
                        "详细 gfxinfo framestats 采集未启用；基础 SurfaceFlinger 呈现间隔仍用于 "
                        "FPS、P95/P99、异常帧与 1% Low，但不拆 RenderThread、BufferQueue 或 GPU 阶段。"
                    )
                    if not frame_details_enabled
                    else (
                        "当前前台窗口未产生新增且可解析的 gfxinfo framestats；"
                        "原生游戏 SurfaceView 通常不会在此公开独立 RenderThread 阶段。"
                    )
                ),
                sample_count=render_pipeline.get("frame_count"),
                confidence="high" if int(render_pipeline.get("frame_count") or 0) >= 100 else "medium",
                metrics=(
                    flow_metric(
                        "截止超时",
                        render_pipeline.get("deadline_missed_pct"),
                        "%",
                        1,
                    ),
                ),
                timeline=(),
                timeline_unit="FPS",
                timeline_value_label="无独立帧率计数",
            )
        )

        untargeted_compositor_fps = source_frame_rate(render_service_frame_rows)
        if using_surface_frames:
            surface_status = "primary"
            primary_flow_key = "surface_present"
            surface_detail = (
                "前台应用缓冲层实际 present 时间戳（优先 SurfaceView/BLAST）；"
                "用于游戏呈现 FPS、帧间隔和 1% Low。"
            )
        elif isinstance(untargeted_compositor_fps, (int, float)):
            surface_status = "invalid"
            surface_detail = (
                "检测到合成器窗口采样，但未绑定到当前前台应用缓冲层，不能作为游戏呈现 FPS。"
            )
        else:
            surface_status = "unavailable"
            surface_detail = unavailable_reason or (
                "当前应用未暴露可持续读取的 SurfaceFlinger 图层 present 时间戳。"
            )
        frame_flow_stages.append(
            flow_stage(
                "surface_present",
                "COMPOSITOR",
                "SurfaceFlinger 应用层呈现",
                surface_status,
                value=(
                    surface_present_fps
                    if using_surface_frames
                    else untargeted_compositor_fps
                ),
                unit="FPS",
                value_label="呈现帧率",
                source=(
                    "Android SurfaceFlinger foreground application-layer present timestamps"
                    if using_surface_frames
                    else "Android compositor sample without target-layer binding"
                ),
                detail=surface_detail,
                sample_count=sum(
                    int(item.get("frame_sample_count") or 0)
                    for item in surface_frame_rows
                ),
                confidence="high" if using_surface_frames else "low",
                metrics=(
                    flow_metric("1% Low", compositor_one_percent_low_fps, "FPS", 1),
                    flow_metric("P95 间隔", compositor_frame_p95_ms, "ms", 2),
                ),
                timeline=surface_flow_timeline,
                timeline_unit="FPS",
                timeline_value_label="呈现帧率",
            )
        )

        display_detail = (
            "屏幕刷新率描述扫描节奏，不等于应用唯一帧数；公开接口未提供可与目标应用逐帧对齐的 HWC present 计数。"
        )
        if interpolation_status in {"detected", "disabled"}:
            display_detail += f" {interpolation_label}。"
        frame_flow_stages.append(
            flow_stage(
                "display_scanout",
                "DISPLAY",
                "HWC / 屏幕扫描输出",
                "reference" if isinstance(current_refresh, (int, float)) and screen_active else "invalid" if isinstance(current_refresh, (int, float)) else "unavailable",
                value=current_refresh,
                unit="Hz",
                value_label="刷新率",
                source=refresh_rate_timeline_source,
                detail=(
                    display_detail
                    if screen_active
                    else f"屏幕状态为 {latest_screen_state or 'unknown'}；当前刷新率不能代表有效显示输出。"
                ),
                confidence="high" if residency_source else "medium",
                metrics=(
                    flow_metric("显示/应用倍率", display_to_frame_ratio, "×", 2),
                ),
                timeline=refresh_flow_timeline,
                timeline_unit="Hz",
                timeline_value_label="显示刷新率",
            )
        )
    elif platform == "harmony":
        smartperf_p95 = weighted_source_value(
            smartperf_frame_rows,
            "frame_interval_p95_ms",
        )
        smartperf_sample_count = sum(
            int(item.get("frame_sample_count") or 0) for item in smartperf_frame_rows
        )
        if isinstance(smartperf_app_fps, (int, float)):
            app_status = "primary"
            primary_flow_key = "app_submission"
            app_detail = "SmartPerf SP_daemon 绑定目标进程输出的应用 FPS。"
        else:
            app_status = "unavailable"
            app_detail = (
                smartperf_unavailable_reason
                or "SP_daemon 未返回可验证的目标应用 FPS。"
                if smartperf_contexts
                else "未启用 SmartPerf。"
            )
        frame_flow_stages.append(
            flow_stage(
                "app_submission",
                "APP",
                "应用产帧 / SmartPerf",
                app_status,
                value=smartperf_app_fps,
                unit="FPS",
                value_label="应用 FPS",
                source="HarmonyOS SmartPerf SP_daemon app FPS",
                detail=app_detail,
                sample_count=smartperf_sample_count,
                confidence="high" if app_status == "primary" else "low",
                metrics=(
                    flow_metric("P95 抖动", smartperf_p95, "ms", 2),
                ),
                timeline=smartperf_flow_timeline,
                timeline_unit="FPS",
                timeline_value_label="应用 FPS",
            )
        )
        frame_flow_stages.append(
            flow_stage(
                "render_queue",
                "RENDER",
                "应用渲染 / 帧抖动",
                "valid" if isinstance(smartperf_p95, (int, float)) else "unavailable",
                value=smartperf_p95,
                unit="ms",
                value_label="帧间隔 P95",
                source="HarmonyOS SmartPerf fpsJitters",
                detail=(
                    "帧抖动反映应用提交间隔，但量产接口不拆分 RenderThread、BufferQueue 与 GPU 内部阶段。"
                    if isinstance(smartperf_p95, (int, float))
                    else "未获得 SmartPerf 帧抖动数组，无法拆分应用渲染阶段。"
                ),
                sample_count=smartperf_sample_count,
                confidence="high" if isinstance(smartperf_p95, (int, float)) else "low",
                timeline=(),
                timeline_unit="FPS",
                timeline_value_label="无独立帧率计数",
            )
        )
        if isinstance(render_service_compositor_fps, (int, float)):
            composer_status = "valid" if primary_flow_key else "primary"
            if primary_flow_key is None:
                primary_flow_key = "surface_present"
            composer_detail = (
                "RenderService 最近合成提交时间戳窗口；可反映合成节奏，但不保证只包含目标应用。"
            )
        else:
            composer_status = "unavailable"
            composer_detail = "RenderService 未返回足够的合成时间戳。"
        frame_flow_stages.append(
            flow_stage(
                "surface_present",
                "COMPOSITOR",
                "RenderService 合成提交",
                composer_status,
                value=render_service_compositor_fps,
                unit="FPS",
                value_label="合成器 FPS",
                source="HarmonyOS RenderService composer fps sampled windows",
                detail=composer_detail,
                sample_count=sum(
                    int(item.get("frame_sample_count") or 0)
                    for item in render_service_frame_rows
                ),
                confidence="medium" if composer_status != "unavailable" else "low",
                timeline=render_service_flow_timeline,
                timeline_unit="FPS",
                timeline_value_label="合成器 FPS",
            )
        )
        frame_flow_stages.append(
            flow_stage(
                "display_scanout",
                "DISPLAY",
                "RenderService / 屏幕刷新",
                "reference" if isinstance(current_refresh, (int, float)) and screen_active else "invalid" if isinstance(current_refresh, (int, float)) else "unavailable",
                value=current_refresh,
                unit="Hz",
                value_label="刷新率",
                source=refresh_rate_timeline_source,
                detail=(
                    "fpsCount 与当前刷新率描述显示档位驻留，不是目标应用 FPS；HWC 最终 present 计数未公开。"
                    if screen_active
                    else f"屏幕状态为 {latest_screen_state or 'unknown'}；刷新率仅保留为无效上下文。"
                ),
                confidence="high" if residency_source else "medium",
                metrics=(
                    flow_metric("显示/应用倍率", display_to_frame_ratio, "×", 2),
                ),
                timeline=refresh_flow_timeline,
                timeline_unit="Hz",
                timeline_value_label="显示刷新率",
            )
        )

    frame_flow = {
        "available": any(
            item.get("status") != "unavailable" for item in frame_flow_stages
        ),
        "platform": platform,
        "primary_key": primary_flow_key,
        "valid_count": sum(
            1
            for item in frame_flow_stages
            if item.get("status") in {"primary", "valid"}
        ),
        "reference_count": sum(
            1 for item in frame_flow_stages if item.get("status") == "reference"
        ),
        "invalid_count": sum(
            1 for item in frame_flow_stages if item.get("status") == "invalid"
        ),
        "timeline_stage_count": sum(
            1
            for item in frame_flow_stages
            if isinstance(item.get("timeline"), list) and item.get("timeline")
        ),
        "stages": frame_flow_stages,
        "note": (
            "不同阶段的数值语义不同：应用提交速率、合成器呈现 FPS 与屏幕刷新率不能直接互换。"
            "时间轴按采样窗口展示实际可读节点；没有独立计数的内部阶段不会用延迟或刷新率冒充 FPS。"
        ),
    }

    gpu_probe = metadata.get("gpu_probe", {})
    gpu_probe = gpu_probe if isinstance(gpu_probe, dict) else {}
    touch_devices = touch_probe.get("devices", [])
    touch_devices = touch_devices if isinstance(touch_devices, list) else []
    switch_count = sum(
        1
        for previous, current in zip(refresh_values, refresh_values[1:])
        if abs(current - previous) > 0.1
    )
    observed_rates = sorted(set(refresh_values))
    context_available = bool(latest or probe or ordered)
    frame_evidence_available = bool(
        frame_rate_timeline
        or isinstance(sampled_frame_rate, (int, float))
        or reported_frame_sample_count > 0
        or render_pipeline.get("available") is True
    )
    available = bool(
        frame_evidence_available
        or refresh_values
        or refresh_residency
        or render_resolution_available
        or touch_interaction_count is not None
    )
    if platform == "ios":
        performance_limitations = (
            "The current iOS sidecar does not expose a general application FPS counter, per-frame Core Animation "
            "timestamps, a display refresh-rate timeline, or a verifiable frame-interpolation switch. No frame-rate, "
            "1% Low, refresh-switch, or render-stage conclusion is generated without those observations."
        )
    elif platform == "harmony":
        performance_limitations = (
            "Compositor FPS and frame intervals are periodic samples of recent RenderService submissions. "
            "Touch counts are delivered interactions; the panel hardware sampling rate is not exposed and is not "
            "inferred. Frame interpolation is reported as enabled or disabled only when an explicit vendor switch "
            "is readable; a refresh/application cadence ratio alone cannot distinguish MEMC from ordinary frame "
            "repetition."
        )
    elif is_android:
        performance_limitations = (
            (
                "Android game frame rate and frame intervals are derived from present timestamps on the "
                "foreground application buffer layer selected from SurfaceFlinger, with SurfaceView/BLAST "
                "preferred when available. They represent frames presented by SurfaceFlinger; "
                "the public interface does not expose every internal engine or hardware-composer stage. "
                if using_surface_frames
                else (
                    "Android frame rate and frame-duration statistics are deltas of cumulative gfxinfo counters "
                    "for the sampled foreground window. The rate is UI frame submissions per second, not visible "
                    "display FPS, and can exceed the panel refresh rate when an app submits redundant frames. "
                    if using_gfxinfo_counters
                    else "Android gfxinfo and SurfaceFlinger did not expose usable foreground frame deltas in this session. "
                )
            )
            + "Touch counts are delivered interactions; the panel hardware sampling rate is not exposed "
            "and is not inferred. Frame interpolation is reported as enabled or disabled only when an "
            "explicit vendor switch is readable; a refresh/application cadence ratio alone cannot "
            "distinguish MEMC from ordinary frame repetition."
        )
    else:
        performance_limitations = (
            "This platform did not expose enough frame, refresh-rate, touch-controller, or interpolation evidence "
            "to generate a verified rendering conclusion."
        )
    return {
        "available": available,
        "context_available": context_available,
        "frame_evidence_available": frame_evidence_available,
        "context_sample_count": len(ordered),
        "current_refresh_rate_hz": current_refresh,
        "peak_refresh_rate_hz": max(supported_refresh_rates) if supported_refresh_rates else None,
        "supported_refresh_rates_hz": sorted(supported_refresh_rates),
        "observed_refresh_rates_hz": observed_rates,
        "refresh_switch_count": switch_count,
        "refresh_residency": refresh_residency,
        "refresh_residency_source": residency_source,
        "sampled_compositor_fps": compositor_fps,
        "minimum_sampled_compositor_fps": compositor_minimum_fps,
        "frame_interval_average_ms": compositor_frame_average_ms,
        "frame_interval_p95_ms": compositor_frame_p95_ms,
        "sampled_frame_rate_fps": sampled_frame_rate,
        "minimum_sampled_frame_rate_fps": minimum_sampled_frame_rate,
        "frame_rate_label": frame_rate_label,
        "frame_rate_unit": frame_rate_unit,
        "frame_overproduction_ratio": frame_overproduction_ratio,
        "display_to_frame_ratio": display_to_frame_ratio,
        "cadence_multiplier": cadence_multiplier,
        "frame_metric_average_ms": frame_metric_average_ms,
        "frame_metric_p95_ms": frame_metric_p95_ms,
        "frame_metric_p99_ms": frame_metric_p99_ms,
        "frame_metric_label": frame_metric_label,
        "one_percent_low_fps": one_percent_low_fps,
        "one_percent_low_source": one_percent_low_source,
        "one_percent_low_confidence": one_percent_low_confidence,
        "one_percent_low_standard": bool(one_percent_low_fps is not None),
        "one_percent_low_timeline_label": one_percent_low_timeline_label,
        "one_percent_low_timeline_source": one_percent_low_timeline_source,
        "window_frame_rate_p1_reference_fps": window_frame_rate_p1_reference_fps,
        "window_frame_rate_p1_reference_source": window_frame_rate_p1_reference_source,
        "frame_rate_timeline": frame_rate_timeline,
        "refresh_rate_timeline": refresh_flow_timeline,
        "refresh_rate_timeline_source": refresh_rate_timeline_source,
        "refresh_rate_unavailable_reason": refresh_rate_unavailable_reason,
        "frame_flow": frame_flow,
        "gfxinfo_submission_fps": gfxinfo_submission_fps,
        "surface_present_fps": surface_present_fps,
        "smartperf_app_fps": smartperf_app_fps,
        "render_service_compositor_fps": render_service_compositor_fps,
        "render_pipeline": render_pipeline,
        "frame_issue_count": frame_issue_count,
        "frame_issue_pct": frame_issue_pct,
        "frame_issue_label": frame_issue_label,
        "frame_sample_count": reported_frame_sample_count,
        "missed_vsync_interval_count": missed_intervals,
        "missed_vsync_interval_pct": compositor_missed_pct,
        "frame_deadline_missed_count": (
            counter_deadline_missed if using_gfxinfo_counters else None
        ),
        "gfxinfo_janky_frame_count": counter_janky if using_gfxinfo_counters else None,
        "gfxinfo_missed_vsync_count": (
            counter_missed_vsync if using_gfxinfo_counters else None
        ),
        "severe_frame_interval_count": severe_intervals,
        "frozen_frame_interval_count": frozen_intervals,
        "missed_vsync_slot_count": sum(
            int(item.get("missed_vsync_slot_count") or 0) for item in frame_rows
        ),
        **hitch_totals,
        "touch_interaction_count": touch_interaction_count,
        "touch_interactions_per_minute": (
            touch_interaction_count / context_duration_s * 60.0
            if touch_interaction_count is not None and context_duration_s > 0
            else None
        ),
        "touch_sampling_rate_hz": None,
        "touch_sampling_rate_available": False,
        "touch_sampling_rate_reason": str(
            latest.get("touch_sampling_rate_reason")
            or touch_probe.get("sampling_rate_reason")
            or "The platform does not expose the touch controller hardware scan rate."
        ),
        "touch_devices": touch_devices,
        "foreground_window_name": latest_value("foreground_window_name"),
        "foreground_window_id": latest_value("foreground_window_id"),
        "foreground_window_pid": latest_value("foreground_window_pid"),
        "display_width_px": display_width,
        "display_height_px": display_height,
        "render_width_px": render_width,
        "render_height_px": render_height,
        "render_resolution_source": render_resolution_source,
        "render_resolution_evidence": render_resolution_evidence,
        "render_resolution_estimated": render_resolution_estimated,
        "render_resolution_available": render_resolution_available,
        "render_scale_pct": render_scale_pct,
        "brightness_raw": latest_value("brightness_raw"),
        "gpu_renderer": latest_value("gpu_renderer") or gpu_probe.get("model"),
        "gpu_vendor": latest_value("gpu_vendor") or gpu_probe.get("vendor"),
        "frame_interpolation_status": interpolation_status,
        "frame_interpolation_label": interpolation_label,
        "frame_interpolation_confidence": interpolation_confidence,
        "frame_interpolation_scope": interpolation_scope,
        "frame_interpolation_evidence": interpolation_evidence,
        "frame_interpolation_available": (
            interpolation_status in {"detected", "disabled"}
            and (
                not is_android
                or interpolation_scope in {"current_app", "current_session"}
            )
        ),
        "frame_source": frame_rate_source,
        "frame_unavailable_reason": (
            None
            if isinstance(sampled_frame_rate, (int, float))
            else smartperf_unavailable_reason
            if platform == "harmony" and smartperf_contexts and smartperf_unavailable_reason
            else latest_value("frame_unavailable_reason")
        ),
        "limitations": performance_limitations,
    }


def analyze_runtime_settings(
    metadata: Dict[str, object],
    raw_outputs: Dict[str, str],
) -> Dict[str, object]:
    start = metadata.get("runtime_settings_start")
    start = dict(start) if isinstance(start, dict) else parse_android_runtime_settings(
        raw_outputs.get("runtime_settings_start", "")
    )
    end = parse_android_runtime_settings(raw_outputs.get("runtime_settings_end", ""))
    labels = {
        "system.screen_brightness": ("屏幕亮度", "亮度越高通常越直接增加显示功耗。"),
        "system.screen_brightness_mode": ("自动亮度", "自动亮度会随环境改变显示负载。"),
        "system.screen_off_timeout": ("自动熄屏", "较长熄屏时间会延长显示与前台任务活动。"),
        "system.peak_refresh_rate": ("最高刷新率", "较高刷新率会增加显示、合成和应用提交压力。"),
        "system.min_refresh_rate": ("最低刷新率", "较高最低档位会减少低刷新率省电机会。"),
        "global.low_power": ("省电模式", "省电模式会改变调度、后台与显示策略。"),
        "global.adaptive_battery_management_enabled": (
            "自适应电池",
            "自适应电池会限制后台应用活动。",
        ),
        "global.app_standby_enabled": ("应用待机", "应用待机会影响后台任务频率。"),
        "global.wifi_on": ("Wi-Fi", "无线网络活动可能带来射频与系统任务压力。"),
        "global.bluetooth_on": ("蓝牙", "蓝牙扫描和连接会增加无线子系统活动。"),
        "global.airplane_mode_on": ("飞行模式", "飞行模式会显著改变蜂窝与无线压力。"),
        "secure.location_mode": ("定位模式", "定位会影响 GNSS、Wi-Fi 扫描与传感器任务。"),
        "global.window_animation_scale": ("窗口动画倍率", "动画倍率影响前台渲染持续时间。"),
        "global.transition_animation_scale": ("转场动画倍率", "转场动画影响合成与渲染持续时间。"),
        "global.animator_duration_scale": ("动画时长倍率", "动画时长会改变持续渲染时间。"),
        "global.stay_on_while_plugged_in": ("充电常亮", "常亮设置会影响显示持续时间。"),
    }
    rows = []
    for key in labels:
        start_value = start.get(key)
        end_value = end.get(key, start_value)
        if start_value is None and end_value is None:
            continue
        label, impact = labels[key]
        rows.append(
            {
                "key": key,
                "label": label,
                "start": start_value,
                "end": end_value,
                "changed": start_value != end_value,
                "impact": impact,
            }
        )
    return {
        "available": bool(rows),
        "start": start,
        "end": end,
        "changed_count": sum(1 for item in rows if item["changed"]),
        "rows": rows,
    }


def analyze_power_pressure(
    samples: Sequence[Sample],
    system_analysis: Dict[str, object],
    scheduler_analysis: Dict[str, object],
    thermal_analysis: Dict[str, object],
    memory_analysis: Dict[str, object],
    settings_analysis: Dict[str, object],
) -> Dict[str, object]:
    valid_samples = [item for item in samples if is_consumption_power_sample(item)]
    powers = [float(item.power_mw) for item in valid_samples]
    drivers: List[Dict[str, object]] = []

    def add_driver(
        key: str,
        label: str,
        values: Sequence[Optional[float]],
        detail: str,
    ) -> None:
        pairs = [
            (float(value), float(sample.power_mw))
            for sample, value in zip(samples, values)
            if isinstance(value, (int, float)) and is_consumption_power_sample(sample)
        ]
        if len(pairs) < 30:
            return
        paired_samples = [
            sample
            for sample, value in zip(samples, values)
            if isinstance(value, (int, float)) and is_consumption_power_sample(sample)
        ]
        if paired_samples[-1].uptime_s - paired_samples[0].uptime_s < 30.0:
            return
        driver_values = [item[0] for item in pairs]
        power_values = [item[1] for item in pairs]
        if len(set(driver_values)) < 3 or len(set(power_values)) < 3:
            return
        correlation = _pearson(driver_values, power_values)
        if not isinstance(correlation, (int, float)) or abs(correlation) < 0.5:
            return
        drivers.append(
            {
                "key": key,
                "label": label,
                "correlation": correlation,
                "sample_count": len(pairs),
                "detail": detail,
            }
        )

    add_driver(
        "cpu",
        "CPU 总负载",
        [item.cpu_pct for item in samples],
        "观察整机 CPU 活动与电池侧功率是否同步。",
    )
    cluster_names = sorted(
        {name for item in samples for name in item.frequencies_mhz}
    )
    for name in cluster_names:
        add_driver(
            f"cpu_frequency:{name}",
            f"{name} 频率",
            [item.frequencies_mhz.get(name) for item in samples],
            "高频驻留会提高 CPU 电压/频率压力，但仍需结合负载判断。",
        )
    add_driver(
        "gpu_load",
        "GPU 负载",
        [item.gpu_load_pct for item in samples],
        "GPU 活动与功率同步时，图形负载可能是主要压力来源。",
    )
    add_driver(
        "gpu_frequency",
        "GPU 频率",
        [item.gpu_frequency_mhz for item in samples],
        "GPU 高频驻留用于解释图形负载阶段的功率抬升。",
    )
    add_driver(
        "memory_frequency",
        "内存 / DMC 频率",
        [item.memory_frequency_mhz for item in samples],
        "内存频率同步抬升通常表示带宽、缓存未命中或数据搬运压力增加。",
    )
    add_driver(
        "temperature",
        "电池温度",
        [item.battery_temperature_c for item in samples],
        "温度与功率同升更多表示长期负载累积，不代表瞬时因果。",
    )
    drivers.sort(
        key=lambda item: abs(float(item.get("correlation") or 0.0)),
        reverse=True,
    )

    processes = system_analysis.get("top_processes", [])
    tasks = []
    if isinstance(processes, list):
        tasks = sorted(
            (
                {
                    "name": item.get("name") or item.get("command"),
                    "category": item.get("category"),
                    "average_cpu_pct": item.get("average_cpu_pct"),
                    "maximum_cpu_pct": item.get("maximum_cpu_pct"),
                    "power_delta_when_visible_mw": item.get(
                        "power_delta_when_visible_mw"
                    ),
                    "power_correlation": item.get("power_correlation"),
                    "seen_snapshots": item.get("seen_snapshots"),
                }
                for item in processes
                if isinstance(item, dict)
            ),
            key=lambda item: (
                float(item.get("power_delta_when_visible_mw") or 0.0),
                float(item.get("average_cpu_pct") or 0.0),
            ),
            reverse=True,
        )[:20]

    leading = drivers[0] if drivers else None
    explanations = []
    if leading and isinstance(leading.get("correlation"), (int, float)):
        correlation = float(leading["correlation"])
        explanations.append(
            {
                "level": "measured",
                "title": f"电流变化与{leading['label']}最同步",
                "detail": (
                    f"相关系数 {correlation:.2f}；{leading['detail']}"
                    "相关性用于解释同时变化，不等同于独立电源轨归因。"
                ),
            }
        )
    if memory_analysis.get("available") and memory_analysis.get(
        "power_valid_for_consumption"
    ):
        delta = memory_analysis.get("high_frequency_power_delta_mw")
        explanations.append(
            {
                "level": "counter",
                "title": "内存频率压力",
                "detail": (
                    f"高频驻留 {float(memory_analysis.get('high_frequency_share_pct') or 0.0):.1f}%；"
                    + (
                        f"高频样本平均功率较低频样本高 {float(delta):.1f} mW。"
                        if isinstance(delta, (int, float))
                        else "当前样本不足以计算高低频功率差。"
                    )
                ),
            }
        )
    return {
        "available": bool(drivers or tasks),
        "power_valid_for_consumption": bool(valid_samples),
        "power_distribution": {
            "median_mw": statistics.median(powers) if powers else None,
            "p95_mw": percentile(powers, 0.95),
            "maximum_mw": max(powers) if powers else None,
        },
        "drivers": drivers,
        "tasks": tasks,
        "scheduler": scheduler_analysis,
        "thermal": thermal_analysis,
        "memory": memory_analysis,
        "settings": settings_analysis,
        "explanations": explanations,
        "limitations": (
            "Drivers are ranked by time-aligned correlation with whole-device battery power. "
            "They explain pressure patterns but are not independent rail measurements and must not be added."
        ),
    }


def analyze_render_performance(
    performance: Dict[str, object],
    system_analysis: Dict[str, object],
    scheduler_analysis: Dict[str, object],
    thermal_analysis: Dict[str, object],
    cpu_analysis: Dict[str, object],
    gpu_analysis: Dict[str, object],
    memory_analysis: Dict[str, object],
    summary: Dict[str, object],
) -> Dict[str, object]:
    pipeline = performance.get("render_pipeline")
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    hot_threads = system_analysis.get("hot_threads", [])
    hot_threads = hot_threads if isinstance(hot_threads, list) else []
    render_threads = [
        item
        for item in hot_threads
        if isinstance(item, dict)
        and re.search(
            r"renderthread|surfaceflinger|renderengine|composer|hwc|gpu|main",
            f"{item.get('name') or ''} {item.get('process') or ''}",
            re.I,
        )
    ][:20]
    bottlenecks: List[Dict[str, object]] = []
    dominant = pipeline.get("dominant_stage")
    if isinstance(dominant, dict):
        key = str(dominant.get("key") or "")
        stage_hints = {
            "vsync_delay_ms": "帧在 VSync 后才开始，优先检查主线程/RenderThread 调度、cpuset 与系统抢占。",
            "input_ms": "输入处理阶段偏长，检查主线程事件处理和同步阻塞。",
            "animation_ms": "动画阶段偏长，检查动画计算、属性更新与主线程工作量。",
            "traversal_ms": "布局/遍历阶段偏长，检查 View 层级、布局重算和 UI 主线程。",
            "draw_ms": "UI 绘制阶段偏长，检查过度绘制、DisplayList 构建和复杂 Canvas 操作。",
            "sync_ms": "同步/上传准备偏长，检查 RenderThread、纹理上传和资源创建。",
            "command_ms": "渲染命令提交偏长，检查 RenderThread 与驱动提交压力。",
            "gpu_wait_ms": "GPU 完成等待偏长，优先检查着色器、分辨率、带宽和 GPU 饱和。",
            "post_swap_ms": "Swap 后等待偏长，检查 BufferQueue、SurfaceFlinger、HWC 和显示合成背压。",
            "dequeue_ms": "DequeueBuffer 偏长，可能存在缓冲区不足或下游合成背压。",
            "queue_ms": "QueueBuffer 偏长，可能存在 SurfaceFlinger/HWC 提交等待。",
        }
        bottlenecks.append(
            {
                "stage": dominant.get("label"),
                "p95_ms": dominant.get("p95_ms"),
                "severity": (
                    "high"
                    if float(dominant.get("p95_ms") or 0.0) >= 8.0
                    else "medium"
                ),
                "detail": stage_hints.get(key, "该阶段在慢帧中占用时间最高。"),
            }
        )
    if float(gpu_analysis.get("average_load_pct") or 0.0) >= 85.0:
        bottlenecks.append(
            {
                "stage": "GPU",
                "severity": "high",
                "detail": "GPU 平均负载超过 85%，慢帧更可能受着色、分辨率或带宽限制。",
            }
        )
    if thermal_analysis.get("throttling_observed"):
        bottlenecks.append(
            {
                "stage": "Thermal",
                "severity": "high",
                "detail": "测试期间出现热限制，CPU/GPU 频率上限变化可能扩大帧延迟。",
            }
        )
    return {
        "available": bool(pipeline.get("available") or render_threads or bottlenecks),
        "pipeline": pipeline,
        "bottlenecks": bottlenecks,
        "render_threads": render_threads,
        "scheduler": {
            "maximum_hint_session_count": scheduler_analysis.get(
                "maximum_hint_session_count"
            ),
            "cpusets": scheduler_analysis.get("cpusets"),
            "process_states": scheduler_analysis.get("process_states"),
        },
        "cpu": {
            "clusters": cpu_analysis.get("clusters"),
        },
        "gpu": {
            "frequency_available": gpu_analysis.get("frequency_available"),
            "load_available": gpu_analysis.get("load_available"),
            "average_frequency_mhz": gpu_analysis.get("average_frequency_mhz"),
            "maximum_frequency_mhz": gpu_analysis.get("maximum_frequency_mhz"),
            "average_load_pct": gpu_analysis.get("average_load_pct"),
            "maximum_load_pct": gpu_analysis.get("maximum_load_pct"),
        },
        "memory": memory_analysis,
        "thermal": {
            "throttling_observed": thermal_analysis.get("throttling_observed"),
            "maximum_status": thermal_analysis.get("maximum_status"),
            "hottest_sensor": thermal_analysis.get("hottest_sensor"),
        },
        "power_recording": {
            "power_valid_for_consumption": summary.get(
                "power_valid_for_consumption"
            ),
            "average_power_mw": summary.get("average_power_mw"),
            "p95_power_mw": summary.get("p95_power_mw"),
            "maximum_power_mw": summary.get("maximum_power_mw"),
            "energy_mwh": summary.get("energy_mwh"),
            "note": (
                "仅记录整机功耗，不执行组件、UID 或第三方任务功耗归因。"
                if summary.get("power_valid_for_consumption")
                else "当前功率通道仅作为原始观测展示，不生成耗电结论。"
            ),
        },
    }


def build_performance_findings(analysis: Dict[str, object]) -> List[Dict[str, str]]:
    platform = str(analysis.get("platform") or "android").lower()
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    brightness = analysis.get("brightness_throttling", {})
    brightness = brightness if isinstance(brightness, dict) else {}
    findings: List[Dict[str, str]] = []
    if platform == "ios":
        summary = analysis.get("summary", {})
        summary = summary if isinstance(summary, dict) else {}
        # CPU/GPU/SystemLoad averages already appear in the aligned raw-data view.
        # Findings should add interpretation instead of duplicating raw statistics.
        observer_cpu = summary.get("average_collector_cpu_pct")
        if isinstance(observer_cpu, (int, float)):
            findings.append(
                {
                    "level": "context",
                    "title": "观察者相关进程 CPU 上界",
                    "detail": (
                        f"sysmond、DTServiceHub 与配对服务同期归一化 CPU 平均为 {float(observer_cpu):.2f}%。"
                        "该值包含这些进程的本底活动，不等于采集工具造成的净增量。"
                    ),
                }
            )
        return findings
    fps = performance.get("sampled_frame_rate_fps")
    one_low = performance.get("one_percent_low_fps")
    p99 = performance.get("frame_metric_p99_ms")
    frame_issue_pct = performance.get("frame_issue_pct")
    frame_issue_count = performance.get("frame_issue_count")
    frame_sample_count = performance.get("frame_sample_count")
    if (
        isinstance(fps, (int, float))
        and float(fps) > 0
        and isinstance(one_low, (int, float))
        and float(one_low) >= 0
    ):
        frame_rate_label = str(performance.get("frame_rate_label") or "帧率").strip()
        frame_rate_unit = str(performance.get("frame_rate_unit") or "FPS").strip()
        low_ratio = float(one_low) / float(fps)
        evidence = (
            f"平均{frame_rate_label} {float(fps):.1f} {frame_rate_unit}，"
            f"1% Low {float(one_low):.1f} FPS（平均值的 {low_ratio * 100.0:.1f}%）"
        )
        if isinstance(p99, (int, float)):
            evidence += f"，P99 帧耗时 {float(p99):.2f} ms"
        if isinstance(frame_issue_pct, (int, float)):
            evidence += f"，节奏校正后异常帧占比 {float(frame_issue_pct):.2f}%"
            if isinstance(frame_issue_count, (int, float)):
                evidence += f"（{int(frame_issue_count)} 帧）"
        evidence += "。"

        if low_ratio < 0.75:
            findings.append(
                {
                    "level": "high" if low_ratio < 0.5 else "medium",
                    "title": "尾部帧稳定性不足",
                    "detail": (
                        evidence
                        + "1% Low 明显低于全程平均，说明少量慢帧或停顿显著影响尾部体验；"
                        "P99 是最慢 1% 的入口分位点，而 1% Low 会平均最慢 1%，两者口径不同。"
                    ),
                }
            )
        elif isinstance(frame_issue_pct, (int, float)) and float(frame_issue_pct) >= 2.0:
            findings.append(
                {
                    "level": "medium",
                    "title": "异常帧占比偏高",
                    "detail": evidence + "异常帧已形成可重复的节奏问题，建议结合时间轴定位同段资源或热状态变化。",
                }
            )
        elif (
            isinstance(frame_issue_pct, (int, float))
            and float(frame_issue_pct) < 1.0
            and isinstance(frame_sample_count, (int, float))
            and int(frame_sample_count) >= 300
            and low_ratio >= 0.85
        ):
            findings.append(
                {
                    "level": "measured",
                    "title": "帧节奏整体稳定",
                    "detail": evidence + "样本量充足，1% Low 保持率和异常帧占比均未显示明显尾部劣化。",
                }
            )
    for item in render.get("bottlenecks", []) if isinstance(render.get("bottlenecks"), list) else []:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "level": str(item.get("severity") or "counter"),
                "title": f"可能的帧延迟来源：{item.get('stage') or '渲染链路'}",
                "detail": str(item.get("detail") or ""),
            }
        )
    if int(brightness.get("point_count") or 0) > 0:
        events = brightness.get("events", [])
        events = events if isinstance(events, list) else []
        leading = events[0] if events and isinstance(events[0], dict) else {}
        findings.append(
            {
                "level": "high" if int(brightness.get("confirmed_point_count") or 0) else "medium",
                "title": "检测到疑似屏幕热降亮",
                "detail": (
                    f"共标记 {int(brightness.get('point_count') or 0)} 个采样点、"
                    f"{int(brightness.get('event_count') or 0)} 段事件；"
                    f"首次发生在 {float(leading.get('start_elapsed_s') or 0.0):.1f} s。"
                    + (
                        "系统亮度设定在事件内保持不变，显示侧有效亮度发生下降。"
                        if leading.get("setting_unchanged")
                        else "请结合事件表中的设置值和有效亮度判断。"
                    )
                ),
            }
        )
    return findings


def build_findings(analysis: Dict[str, object]) -> List[Dict[str, str]]:
    test_mode = str(analysis.get("test_mode") or "power")
    if test_mode == "performance":
        return build_performance_findings(analysis)
    platform = str(analysis.get("platform") or "android").lower()
    summary = analysis["summary"]
    cpu = analysis.get("cpu", {})
    gpu = analysis.get("gpu", {})
    display = analysis.get("display", {})
    performance = analysis.get("performance", {})
    thermal = analysis.get("thermal", {})
    system = analysis.get("system", {})
    scheduler = analysis.get("scheduler", {})
    brightness = analysis.get("brightness_throttling", {})
    brightness = brightness if isinstance(brightness, dict) else {}
    target = analysis.get("target_app")
    if not bool(summary.get("power_valid_for_consumption")):
        return []
    # Average power/current/P95 are first-class raw-data statistics.  Keep the
    # analysis page for conclusions that add causal, comparative, or validity
    # information instead of repeating those values.
    findings: List[Dict[str, str]] = []
    pressure = analysis.get("power_pressure", {})
    pressure = pressure if isinstance(pressure, dict) else {}
    for item in pressure.get("explanations", []) if isinstance(pressure.get("explanations"), list) else []:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "level": str(item.get("level") or "counter"),
                "title": str(item.get("title") or "功耗压力解释"),
                "detail": str(item.get("detail") or ""),
            }
        )
    runtime_settings = analysis.get("runtime_settings", {})
    runtime_settings = runtime_settings if isinstance(runtime_settings, dict) else {}
    if int(runtime_settings.get("changed_count") or 0) > 0:
        findings.append(
            {
                "level": "context",
                "title": "测试期间系统设置发生变化",
                "detail": (
                    f"亮度、刷新率、省电或无线等设置中有 "
                    f"{int(runtime_settings.get('changed_count') or 0)} 项前后不一致，"
                    "续航对比时应先固定这些变量。"
                ),
            }
        )

    clusters = cpu.get("clusters", []) if isinstance(cpu, dict) else []
    modeled_clusters = [
        item for item in clusters if isinstance(item.get("frequency_premium_mw"), (int, float))
    ]
    if modeled_clusters:
        leading = max(modeled_clusters, key=lambda item: float(item.get("frequency_premium_mw") or 0.0))
        cluster_label = {
            "Little": "小核",
            "Big": "大核",
            "Performance": "性能核",
            "Prime": "超大核",
        }.get(str(leading.get("label") or ""), str(leading.get("label") or "CPU"))
        findings.append(
            {
                "level": "model",
                "title": f"{cluster_label}频率影响",
                "detail": (
                    f"Android CPU Power Profile 估算：在相同负载下，相对最低频率基线，"
                    f"平均增加 {float(leading.get('frequency_premium_mw') or 0.0):.1f} mW。"
                ),
            }
        )

    if isinstance(gpu, dict):
        target_work = gpu.get("target_work")
        if isinstance(target_work, dict):
            findings.append(
                {
                    "level": "driver",
                    "title": "目标 UID 的 GPU 活动",
                    "detail": (
                        f"GPU 驱动在本次测试中向目标 UID 记录了 "
                        f"{float(target_work.get('active_ms') or 0.0):.1f} ms 活跃工作时长。"
                    ),
                }
            )
        elif not gpu.get("frequency_available") and not gpu.get("load_available"):
            findings.append(
                {
                    "level": "context",
                    "title": "GPU 实时计数器不可用",
                    "detail": (
                        "本次会话未恢复到 iOS DVT Graphics 利用率事件；报告不会据此推断 GPU 电源轨功耗。"
                        if platform == "ios"
                        else (
                            "HarmonyOS 量产系统未向 HDC shell 暴露可读 GPU 频率或负载节点，且不存在 Android dumpsys GPU 回退证据。"
                            if platform == "harmony"
                            else "OEM 系统未向 ADB shell 暴露可读的 GPU 频率节点，报告使用可获得的 UID 工作时长证据。"
                        )
                    ),
                }
            )

    if isinstance(target, dict):
        uid_usage = target.get("usage")
        network = target.get("network")
        details = []
        if isinstance(uid_usage, dict):
            details.append(f"BatteryStats 模型归因 {float(uid_usage.get('mah') or 0.0):.3f} mAh")
        if isinstance(network, dict):
            details.append(
                f"Wi-Fi 接收 {format_bytes(network.get('wifi_rx_bytes'))} / "
                f"发送 {format_bytes(network.get('wifi_tx_bytes'))}"
            )
        if details:
            findings.append(
                {
                    "level": "model",
                    "title": f"目标应用：{target.get('package')}",
                    "detail": "；".join(details) + "。",
                }
            )

    applications = analysis.get("applications", {})
    if isinstance(applications, dict):
        known_apps = [
            item
            for item in applications.get("rows", [])
            if item.get("package") != "unknown"
        ]
        if known_apps:
            leading_app = max(known_apps, key=lambda item: float(item.get("energy_mwh") or 0.0))
            findings.append(
                {
                    "level": "measured",
                    "title": f"前台能耗最高：{leading_app.get('package')}",
                    "detail": (
                        f"观测到的前台时间为 {float(leading_app.get('duration_s') or 0.0):.1f} s，"
                        f"期间分配的整机实测能量为 {float(leading_app.get('energy_mwh') or 0.0):.2f} mWh。"
                    ),
                }
            )

    if isinstance(system, dict):
        priority = system.get("priority_activities", {})
        rows = priority.get("rows", []) if isinstance(priority, dict) else []
        if rows:
            leading = max(
                rows,
                key=lambda item: float(item.get("excess_energy_mwh") or 0.0),
            )
            delta = leading.get("power_delta_mw")
            delta_text = (
                f"，相对会话基线 {float(delta):+.0f} mW"
                if isinstance(delta, (int, float))
                else ""
            )
            findings.append(
                {
                    "level": "measured",
                    "title": f"重点后台活动：{leading.get('label') or leading.get('name')}",
                    "detail": (
                        f"在 {int(leading.get('detection_count') or 0)} 个系统快照中被检测到，"
                        f"估算持续 {float(leading.get('estimated_duration_s') or 0.0):.1f} s{delta_text}。"
                        "该结果仅表示与整机功率的时间相关性，不代表因果归因。"
                    ),
                }
            )
        groups = system.get("activity_groups", {})
        group_rows = groups.get("rows", []) if isinstance(groups, dict) else []
        meaningful_group_rows = [
            item
            for item in group_rows
            if float(item.get("maximum_cpu_pct") or 0.0) >= 5.0
            and (
                abs(float(item.get("power_delta_mw") or 0.0)) >= 100.0
                or float(item.get("estimated_duration_s") or 0.0) >= 10.0
            )
        ]
        if meaningful_group_rows:
            leading_group = max(
                meaningful_group_rows,
                key=lambda item: (
                    float(item.get("excess_energy_mwh") or 0.0),
                    float(item.get("maximum_cpu_pct") or 0.0),
                ),
            )
            findings.append(
                {
                    "level": "measured",
                    "title": f"系统活动关联：{leading_group.get('label')}",
                    "detail": (
                        f"在 {int(leading_group.get('detection_count') or 0)} 个热点快照中可见，"
                        f"CPU 平均 / 峰值为 {float(leading_group.get('average_cpu_pct') or 0.0):.1f}% / "
                        f"{float(leading_group.get('maximum_cpu_pct') or 0.0):.1f}%，"
                        f"同期整机功率相对会话基线 {float(leading_group.get('power_delta_mw') or 0.0):+.0f} mW。"
                        "该结果是采样时间关联，不是独占功耗归因。"
                    ),
                }
            )

    external = analysis.get("external_events", {})
    if isinstance(external, dict) and external.get("rows"):
        leading_phase = max(
            external["rows"],
            key=lambda item: float(item.get("energy_mwh") or 0.0),
        )
        findings.append(
            {
                "level": "measured",
                "title": f"导入阶段能耗最高：{leading_phase.get('name')}",
                "detail": (
                    f"根据 {leading_phase.get('phase')} 日志事件对齐得到 "
                    f"{float(leading_phase.get('energy_mwh') or 0.0):.2f} mWh。"
                ),
            }
        )

    test_items = analysis.get("test_items", {})
    if isinstance(test_items, dict) and test_items.get("rows"):
        rank = {"unknown": -1, "low": 0, "medium": 1, "high": 2}
        leading_test = max(
            test_items["rows"],
            key=lambda item: (
                rank.get(str(item.get("interference_level") or "unknown"), -1),
                float(item.get("system_activity_overlap_pct") or 0.0),
                float(item.get("energy_mwh") or 0.0),
            ),
        )
        findings.append(
            {
                "level": "context",
                "title": f"系统干扰最高的测试项：{leading_test.get('name')}",
                "detail": (
                    f"系统活动窗口重叠 {float(leading_test.get('system_activity_overlap_pct') or 0.0):.1f}%，"
                    f"GC {int((leading_test.get('gc') or {}).get('snapshot_count') or 0)} 个采样点，"
                    f"kworker {int((leading_test.get('kworker') or {}).get('snapshot_count') or 0)} 个采样点，"
                    f"{'更新/安装/编译' if platform == 'harmony' else '系统/采集器' if platform == 'ios' else 'DEX/更新'}"
                    f"重叠 {float(leading_test.get('dex_update_overlap_s') or 0.0):.1f} s。"
                ),
            }
        )

    active_refresh = display.get("active_refresh_hz") if isinstance(display, dict) else None
    if isinstance(performance, dict) and performance.get("available"):
        residency = performance.get("refresh_residency", [])
        if isinstance(residency, list) and residency:
            leading_refresh = max(
                residency,
                key=lambda item: float(item.get("share_pct") or 0.0),
            )
            findings.append(
                {
                    "level": "context",
                    "title": "刷新率驻留",
                    "detail": (
                        f"当前 {float(performance.get('current_refresh_rate_hz') or 0.0):.0f} Hz；"
                        f"会话内以 {float(leading_refresh.get('refresh_rate_hz') or 0.0):.0f} Hz 为主，"
                        f"占已观测刷新档位时间的 {float(leading_refresh.get('share_pct') or 0.0):.1f}%。"
                    ),
                }
            )
        elif isinstance(active_refresh, (int, float)):
            findings.append(
                {
                    "level": "context",
                    "title": "显示模式",
                    "detail": f"采集期间显示渲染刷新率为 {active_refresh:.0f} Hz。",
                }
            )
    elif isinstance(active_refresh, (int, float)):
        findings.append(
            {
                "level": "context",
                "title": "显示模式",
                "detail": f"采集期间显示渲染刷新率为 {active_refresh:.0f} Hz。",
            }
        )
    thermal_status = None
    if int(brightness.get("point_count") or 0) > 0:
        events = brightness.get("events", [])
        events = events if isinstance(events, list) else []
        leading = events[0] if events and isinstance(events[0], dict) else {}
        findings.append(
            {
                "level": "high" if int(brightness.get("confirmed_point_count") or 0) else "medium",
                "title": "检测到疑似屏幕热降亮",
                "detail": (
                    f"共标记 {int(brightness.get('point_count') or 0)} 个采样点、"
                    f"{int(brightness.get('event_count') or 0)} 段事件；"
                    f"首次发生在 {float(leading.get('start_elapsed_s') or 0.0):.1f} s。"
                    + (
                        "系统亮度设定保持不变，因此屏幕变暗不能用设置值下降解释。"
                        if leading.get("setting_unchanged")
                        else "设置值同期也有变化，报告会分别展示设置与显示侧限制。"
                    )
                ),
            }
        )
    if isinstance(thermal, dict):
        thermal_status = thermal.get("maximum_status")
        if thermal_status is None:
            thermal_status = thermal.get("status")
    thermal_severity_available = bool(
        isinstance(thermal, dict) and thermal.get("severity_available") is True
    )
    if (
        thermal_severity_available
        and isinstance(thermal_status, (int, float))
        and thermal_status > 0
    ):
        thermal_label = {
            "light": "轻度",
            "moderate": "中度",
            "severe": "严重",
            "critical": "危急",
            "emergency": "紧急",
            "shutdown": "关机",
        }.get(str(thermal.get("maximum_status_label") or ""), "级别升高")
        findings.append(
            {
                "level": "measured",
                "title": "检测到热限制",
                "detail": (
                    f"ThermalService 最高达到状态 {int(thermal_status)} "
                    f"（{thermal_label}）。"
                ),
            }
        )
    elif thermal_severity_available and thermal_status == 0:
        findings.append(
            {
                "level": "low",
                "title": "未检测到热限制",
                "detail": "所有已采集的 ThermalService 状态均保持为 0。",
            }
        )
    if isinstance(scheduler, dict) and scheduler.get("maximum_hint_session_count"):
        findings.append(
            {
                "level": "context",
                "title": "存在活跃的 ADPF Performance Hint 会话",
                "detail": (
                    f"最多观察到 {int(scheduler.get('maximum_hint_session_count') or 0)} 个活跃 ADPF 会话，"
                    "报告保留了对应 PID/TID 与目标时长状态。"
                ),
            }
        )
    return findings


def _analysis_data_sources(
    platform: str,
    test_mode: str = "power",
    capture_configuration: Optional[Dict[str, object]] = None,
) -> List[Dict[str, str]]:
    capture_configuration = capture_configuration or {}
    backend = str(capture_configuration.get("backend") or "")
    feature_flags = capture_configuration.get("features")
    feature_flags = feature_flags if isinstance(feature_flags, dict) else {}

    def feature_enabled(name: str, default: bool = True) -> bool:
        value = feature_flags.get(name)
        return bool(value) if isinstance(value, bool) else default
    sources: List[Dict[str, str]] = []

    def add(metric: str, source: str, kind: str) -> None:
        sources.append({"metric": metric, "source": source, "kind": kind})

    if platform == "ios":
        add(
            "Whole-device raw SystemLoad power",
            "iOS DiagnosticsService PowerTelemetryData.SystemLoad",
            "measured low-rate telemetry",
        )
        add(
            "Battery current and voltage",
            "iOS DiagnosticsService battery properties",
            "measured",
        )
        if feature_enabled("cpu_usage"):
            add("CPU utilization", "iOS DVT sysmontap", "measured counters")
        # sysmontap process rows and powerScore are opportunistic even though the
        # base CPU stream is always enabled. The observation filter below keeps
        # each channel only when a corresponding event was actually received.
        add(
            "System processes",
            "iOS DVT sysmontap process snapshots",
            "measured counters",
        )
        add(
            "Relative process power score",
            "iOS DVT sysmontap powerScore",
            "diagnostic score",
        )
        if feature_enabled("gpu_metrics"):
            add("GPU activity", "iOS DVT Graphics utilization", "measured counters")
        if feature_enabled("foreground_window"):
            add(
                "Foreground application",
                "iOS DVT application-state notifications when observed",
                "event context",
            )
        add(
            "Observer-related process CPU upper bound",
            "sysmond + DTServiceHub + remotepairingdeviced concurrent CPU",
            "interference context, not net overhead",
        )
        if feature_enabled("thermal"):
            add(
                "Battery temperature",
                "iOS DiagnosticsService battery temperature",
                "measured counters",
            )
        add(
            "Test phases and actions",
            "Imported timestamped logs aligned to device uptime",
            "context",
        )
        return sources

    if platform == "harmony":
        smartperf = backend == "harmony_smartperf"
        counter_source = (
            "HarmonyOS SmartPerf SP_daemon"
            if smartperf
            else "HarmonyOS /proc/stat + hidumper --cpufreq"
        )
        add(
            "Whole-device power recording",
            "HarmonyOS SmartPerf SP_daemon current and voltage fields"
            if smartperf
            else "HarmonyOS BatteryService via hidumper",
            "battery flow; consumption-valid only while discharging",
        )
        if feature_enabled("cpu_usage"):
            add("CPU utilization", counter_source, "measured counters")
        if feature_enabled("cpu_frequency"):
            add("CPU frequency", counter_source, "measured counters")
        if feature_enabled("gpu_metrics"):
            add(
                "GPU activity",
                "HarmonyOS SmartPerf SP_daemon GPU fields",
                "measured counters",
            )
        if feature_enabled("memory_frequency"):
            add(
                "Memory frequency pressure",
                "HarmonyOS SmartPerf SP_daemon DDR fields",
                "measured counters",
            )
        if feature_enabled("target_process"):
            add(
                "Target process resources",
                "HarmonyOS SmartPerf SP_daemon target process fields",
                "measured counters",
            )
        if test_mode == "performance" and feature_enabled("frame_rate"):
            add(
                "HarmonyOS application frame pacing"
                if smartperf
                else "HarmonyOS compositor cadence context",
                "HarmonyOS SmartPerf SP_daemon target-foreground FPS and raw frame jitter"
                if smartperf
                else "HarmonyOS RenderService fresh active-screen compositor timestamps",
                "measured counters" if smartperf else "system compositor context",
            )
        if feature_enabled("foreground_window"):
            add(
                "Foreground application",
                "HarmonyOS AbilityManager + WindowManager",
                "measured counters",
            )
            add(
                "Display refresh rate",
                "HarmonyOS RenderService screen refresh-rate counters",
                "measured counters",
            )
        if feature_enabled("touch_events", False):
            add(
                "Delivered touch interactions",
                "HarmonyOS MultimodalInput delivered touch events",
                "measured counters",
            )
        if feature_enabled("process_snapshots", False):
            add("System processes", "HarmonyOS top + ps over HDC", "measured counters")
        if feature_enabled("thermal"):
            add(
                "Thermal sensors",
                "HarmonyOS SmartPerf SP_daemon temperature fields"
                if smartperf
                else "HarmonyOS ThermalService via hidumper",
                "measured counters",
            )
        if feature_enabled("scheduler", False):
            add(
                "Scheduler context",
                "HarmonyOS PowerManagerService + cpufreq capability snapshots",
                "context",
            )
        add(
            "Test phases and actions",
            "Imported timestamped logs aligned to HarmonyOS device realtime",
            "context",
        )
        return sources

    frame_source = (
        "Android SurfaceFlinger foreground application-layer present timestamps "
        "with gfxinfo fallback and detailed framestats"
    )
    add(
        "Whole-device power recording",
        "Battery current and voltage telemetry",
        "measured",
    )
    if feature_enabled("cpu_usage"):
        add(
            "CPU utilization",
            "Android /proc/stat utilization deltas",
            "measured counters",
        )
    if feature_enabled("cpu_frequency"):
        add(
            "CPU frequency",
            "Android cpufreq core-group counters",
            "measured counters",
        )
        if test_mode == "power":
            add("CPU frequency impact", "Android Power Profile", "model")
    if feature_enabled("gpu_metrics"):
        add(
            "GPU activity",
            "Readable Android KGSL/OEM GPU frequency and load counters",
            "measured counters",
        )
    if feature_enabled("memory_frequency"):
        add(
            "Memory frequency pressure",
            "Readable DRAM/DMC/MIF devfreq clock",
            "measured counters",
        )
    if feature_enabled("foreground_window"):
        add(
            "Foreground application",
            "ActivityManager context sampler",
            "measured counters",
        )
        add(
            "Display refresh rate",
            "Android DisplayManager context sampler",
            "measured counters",
        )
    if test_mode == "performance" and feature_enabled("frame_rate"):
        add(
            "Frame rate, 1% Low and frame latency",
            frame_source,
            "measured counters",
        )
    if test_mode == "performance" and feature_enabled("frame_details"):
        add("Render pipeline stages", frame_source, "measured counters")
    if feature_enabled("process_snapshots", False):
        add(
            "System processes",
            "Periodic toybox top/ps snapshots",
            "measured counters",
        )
    if feature_enabled("hot_threads", False):
        add(
            "Render and compositor thread activity",
            "Periodic toybox top/ps thread snapshots",
            "measured counters",
        )
    if feature_enabled("scheduler", False):
        add(
            "Scheduler context",
            "cgroup files + ActivityManager + performance_hint",
            "context",
        )
    if feature_enabled("thermal"):
        add(
            "Thermal context",
            "Android ThermalService sensors, severity and cooling devices",
            "measured context",
        )
        add(
            "Brightness thermal limiting",
            "DisplayManager BrightnessThermalClamper + Thermal HAL lcd-backlight",
            "measured counters",
        )
    if test_mode == "power" and feature_enabled("runtime_settings"):
        add(
            "Runtime settings pressure",
            "Android settings snapshot at test start/end",
            "context",
        )
    if test_mode == "power" and feature_enabled("power_attribution"):
        add("Component/app attribution", "BatteryStats", "model")
    if test_mode == "power":
        add(
            "Per-test power and system interference",
            "Whole-device telemetry + aligned process/thread/thermal snapshots",
            "measured counters",
        )
    add(
        "Test phases and actions",
        "Imported timestamped logs aligned by /proc/uptime",
        "context",
    )
    return sources


def _filter_analysis_data_sources(
    sources: Sequence[Dict[str, str]],
    analysis: Dict[str, object],
) -> List[Dict[str, str]]:
    summary = analysis.get("summary", {})
    summary = summary if isinstance(summary, dict) else {}
    performance = analysis.get("performance", {})
    performance = performance if isinstance(performance, dict) else {}
    cpu = analysis.get("cpu", {})
    cpu = cpu if isinstance(cpu, dict) else {}
    gpu = analysis.get("gpu", {})
    gpu = gpu if isinstance(gpu, dict) else {}
    memory = analysis.get("memory", {})
    memory = memory if isinstance(memory, dict) else {}
    system = analysis.get("system", {})
    system = system if isinstance(system, dict) else {}
    render = analysis.get("render_performance", {})
    render = render if isinstance(render, dict) else {}
    scheduler = analysis.get("scheduler", {})
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    thermal = analysis.get("thermal", {})
    thermal = thermal if isinstance(thermal, dict) else {}
    brightness = analysis.get("brightness_throttling", {})
    brightness = brightness if isinstance(brightness, dict) else {}
    applications = analysis.get("applications", {})
    applications = applications if isinstance(applications, dict) else {}
    external = analysis.get("external_events", {})
    external = external if isinstance(external, dict) else {}
    runtime_settings = analysis.get("runtime_settings", {})
    runtime_settings = runtime_settings if isinstance(runtime_settings, dict) else {}
    battery_usage = analysis.get("battery_usage", {})
    battery_usage = battery_usage if isinstance(battery_usage, dict) else {}
    test_items = analysis.get("test_items", {})
    test_items = test_items if isinstance(test_items, dict) else {}

    def rows_present(value: Dict[str, object], *keys: str) -> bool:
        return any(isinstance(value.get(key), list) and bool(value.get(key)) for key in keys)

    def numeric(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    def known_foreground(value: object) -> bool:
        text = str(value or "").strip().lower()
        return bool(text and text not in {"unknown", "none", "null", "--", "-"})

    process_rows = (
        system.get("top_processes", [])
        if isinstance(system.get("top_processes"), list)
        else []
    )
    thread_rows = (
        system.get("hot_threads", [])
        if isinstance(system.get("hot_threads"), list)
        else []
    )
    application_rows = (
        applications.get("rows", [])
        if isinstance(applications.get("rows"), list)
        else []
    )
    application_transitions = (
        applications.get("transitions", [])
        if isinstance(applications.get("transitions"), list)
        else []
    )

    frame_observed = bool(
        performance.get("frame_sample_count")
        or numeric(performance.get("sampled_frame_rate_fps"))
        or numeric(performance.get("sampled_compositor_fps"))
        or performance.get("frame_rate_timeline")
    )
    refresh_observed = bool(
        numeric(performance.get("current_refresh_rate_hz"))
        or rows_present(performance, "refresh_rate_timeline", "refresh_residency")
    )
    cpu_usage_observed = numeric(summary.get("average_cpu_pct"))
    cpu_frequency_observed = any(
        isinstance(item, dict)
        and any(
            numeric(item.get(key))
            for key in ("average_mhz", "maximum_mhz", "load_weighted_mhz")
        )
        for item in (cpu.get("clusters", []) if isinstance(cpu.get("clusters"), list) else [])
    )
    cpu_model_observed = any(
        isinstance(item, dict)
        and (
            item.get("model_available") is True
            or numeric(item.get("modeled_power_mw"))
            or numeric(item.get("frequency_premium_mw"))
        )
        for item in (cpu.get("clusters", []) if isinstance(cpu.get("clusters"), list) else [])
    )
    gpu_observed = bool(
        gpu.get("frequency_available")
        or gpu.get("load_available")
        or gpu.get("work_by_uid")
        or (
            isinstance(gpu.get("memory"), dict)
            and gpu.get("memory", {}).get("available")
        )
    )
    memory_observed = bool(memory.get("available") or memory.get("timeline"))
    target_process_observed = any(
        isinstance(item, dict)
        and (
            str(item.get("source") or "") == "harmony_smartperf_target"
            or str(item.get("watch_name") or "") == "target_app"
        )
        for item in process_rows
    )
    system_process_observed = bool(
        thread_rows
        or any(
            isinstance(item, dict)
            and str(item.get("source") or "") != "harmony_smartperf_target"
            for item in process_rows
        )
    )
    relative_power_score_observed = any(
        isinstance(item, dict)
        and any(
            numeric(item.get(key))
            for key in ("average_relative_power_score", "maximum_relative_power_score")
        )
        for item in process_rows
    )
    scheduler_observed = any(
        scheduler.get(key)
        for key in ("cpusets", "cpu_policies", "hint_sessions", "process_states", "timeline")
    )
    thermal_observed = bool(
        thermal.get("available")
        or thermal.get("sensors")
        or thermal.get("cooling_devices")
        or thermal.get("timeline")
        or numeric(thermal.get("maximum_status"))
    )
    battery_temperature_observed = any(
        isinstance(item, dict)
        and "battery" in str(item.get("name") or "").lower()
        and any(numeric(item.get(key)) for key in ("value_c", "maximum_c", "average_c"))
        for item in (
            thermal.get("sensors", [])
            if isinstance(thermal.get("sensors"), list)
            else []
        )
    )
    foreground_observed = bool(
        any(
            isinstance(item, dict) and known_foreground(item.get("package"))
            for item in [*application_rows, *application_transitions]
        )
        or known_foreground(performance.get("foreground_window_name"))
    )
    power_observed = any(
        numeric(summary.get(key))
        for key in (
            "observed_power_average_mw",
            "battery_flow_average_power_mw",
            "average_power_mw",
        )
    )
    observed_power_sources = {
        str(item)
        for item in (
            summary.get("observed_power_sources", [])
            if isinstance(summary.get("observed_power_sources"), list)
            else summary.get("power_sources", [])
            if isinstance(summary.get("power_sources"), list)
            else []
        )
        if str(item)
    }
    ios_system_load_observed = (
        "ios_power_telemetry_system_load" in observed_power_sources
    )
    battery_channels_observed = bool(
        (
            numeric(summary.get("observed_average_current_ma"))
            or numeric(summary.get("average_current_ma"))
        )
        and numeric(summary.get("average_voltage_mv"))
    )
    observer_cpu_observed = numeric(summary.get("average_collector_cpu_pct"))
    render_threads_observed = bool(render.get("render_threads"))
    touch_observed = numeric(performance.get("touch_interaction_count"))
    runtime_settings_observed = bool(
        runtime_settings.get("available") or rows_present(runtime_settings, "rows")
    )
    attribution_observed = bool(
        battery_usage.get("available")
        or rows_present(battery_usage, "components", "uids")
        or analysis.get("components")
    )
    test_items_observed = bool(
        test_items.get("available") or rows_present(test_items, "rows", "timeline", "spans")
    )
    external_observed = bool(
        external.get("event_count") or rows_present(external, "rows", "spans")
    )

    def observed(metric: str) -> bool:
        if metric in {
            "Frame rate, 1% Low and frame latency",
            "HarmonyOS application frame pacing",
            "HarmonyOS compositor cadence context",
            "Application FPS and frame jitter",
        }:
            return frame_observed
        if metric == "Display refresh rate":
            return refresh_observed
        if metric == "Render pipeline stages":
            pipeline = render.get("pipeline", {})
            return bool(
                render.get("stages")
                or (isinstance(pipeline, dict) and pipeline.get("stages"))
            )
        if metric == "Render and compositor thread activity":
            return render_threads_observed or bool(thread_rows)
        if metric in {"System processes", "System processes and hot threads"}:
            return system_process_observed
        if metric == "Target process resources":
            return target_process_observed
        if metric == "Relative process power score":
            return relative_power_score_observed
        if metric in {"Scheduler context", "Scheduler and thermal context"}:
            return scheduler_observed
        if metric in {
            "Thermal context",
            "Thermal sensors",
            "Thermal severity, sensors and cooling devices",
        }:
            return thermal_observed
        if metric == "Battery temperature":
            return battery_temperature_observed
        if metric == "Brightness thermal limiting":
            return bool(brightness.get("available"))
        if metric in {
            "Whole-device raw SystemLoad power",
            "Whole-device battery power",  # legacy analysis files
        }:
            return ios_system_load_observed
        if metric == "Whole-device power recording":
            return power_observed
        if metric in {
            "Battery current and voltage",
            "Battery current, voltage and temperature",
            "Battery current",
            "Battery voltage",
        }:
            return battery_channels_observed
        if metric in {
            "Foreground application",
            "Foreground application state",
            "Foreground application and screen state",
        }:
            return foreground_observed
        if metric == "Observer-related process CPU upper bound":
            return observer_cpu_observed
        if metric == "GPU activity":
            return gpu_observed
        if metric == "CPU utilization":
            return cpu_usage_observed
        if metric == "CPU frequency":
            return cpu_frequency_observed
        if metric == "CPU frequency impact":
            return cpu_model_observed
        if metric == "Memory frequency pressure":
            return memory_observed
        if metric == "Runtime settings pressure":
            return runtime_settings_observed
        if metric == "Component/app attribution":
            return attribution_observed
        if metric == "Per-test power and system interference":
            return test_items_observed
        if metric == "Delivered touch interactions":
            return touch_observed
        if metric == "Test phases and actions":
            return external_observed

        # Legacy compound names are accepted only when every named channel was
        # observed. New reports no longer generate these ambiguous rows.
        if metric == "iOS CPU and GPU performance context":
            return cpu_usage_observed and gpu_observed
        if metric == "CPU and process activity":
            return cpu_usage_observed and system_process_observed
        if metric == "System processes and collector overhead":
            return system_process_observed and observer_cpu_observed
        if metric == "HarmonyOS CPU/GPU/DDR and thermal context":
            return (
                cpu_usage_observed
                and cpu_frequency_observed
                and gpu_observed
                and memory_observed
                and thermal_observed
            )
        if metric == "CPU/GPU/DDR and target process resources":
            return (
                cpu_usage_observed
                and cpu_frequency_observed
                and gpu_observed
                and memory_observed
                and target_process_observed
            )
        if metric == "CPU utilization/frequency":
            return cpu_usage_observed and cpu_frequency_observed
        if metric == "Refresh-rate residency and sampled compositor frame pacing":
            return refresh_observed and frame_observed
        if metric == "Foreground window and display context":
            return foreground_observed and refresh_observed
        if metric == "Foreground window and delivered touch interactions":
            return foreground_observed and touch_observed
        if metric in {"Power and scheduler context", "cpuset, process state and ADPF hints"}:
            return scheduler_observed
        return False

    return [item for item in sources if observed(str(item.get("metric") or ""))]


def _apply_legacy_consumption_power_inference(
    samples: Sequence[Sample],
    metadata: Dict[str, object],
) -> bool:
    """Recover old unplugged artifacts only when both endpoints prove discharge."""

    if any(
        sample.power_valid_for_consumption is not None
        or sample.external_power is not None
        for sample in samples
    ):
        return False
    try:
        schema_version = int(metadata.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version >= SCHEMA_VERSION:
        return False
    start = metadata.get("battery_start")
    end = metadata.get("battery_end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return False

    def explicitly_unplugged(value: Dict[str, object]) -> bool:
        return (
            "powered" in value
            and value.get("powered") in (False, [], ())
            and str(value.get("status") or "").strip().lower() == "discharging"
        )

    if not explicitly_unplugged(start) or not explicitly_unplugged(end):
        return False
    if any(
        str(sample.direction or "").strip().lower() != "discharging"
        for sample in samples
    ):
        return False
    for sample in samples:
        sample.external_power = False
        sample.power_valid_for_consumption = True
    return True


def analyze_run(
    samples: Sequence[Sample],
    metadata: Dict[str, object],
    raw_outputs: Dict[str, str],
    warnings: Sequence[str],
    contexts: Sequence[ContextSample] = (),
    events: Sequence[ExternalEvent] = (),
    system_snapshots: Sequence[SystemSnapshot] = (),
    thermal_snapshots: Sequence[ThermalSnapshot] = (),
    scheduler_snapshots: Sequence[SchedulerSnapshot] = (),
) -> Dict[str, object]:
    if len(samples) < 2:
        raise RuntimeError("at least two samples are required")
    capture_configuration = metadata.get("capture_configuration", {})
    capture_configuration = (
        capture_configuration if isinstance(capture_configuration, dict) else {}
    )
    capture_features = capture_features_from_metadata(metadata)

    def feature(name: str) -> bool:
        return bool(capture_features.get(name, True))

    legacy_power_validity_inferred = _apply_legacy_consumption_power_inference(
        samples,
        metadata,
    )
    platform = str(metadata.get("platform") or "android").lower()
    if platform == "harmony":
        sample_window_start = samples[0].uptime_s
        sample_window_end = samples[-1].uptime_s
        contexts = tuple(
            item
            for item in contexts
            if sample_window_start <= item.uptime_s <= sample_window_end
        )
    power_sources = sorted({sample.power_source for sample in samples if sample.power_source})
    primary_power_source: Optional[str] = None
    if platform == "ios":
        if "ios_power_telemetry_system_load" in power_sources:
            primary_power_source = "ios_power_telemetry_system_load"
        elif "ios_battery_current_voltage" in power_sources:
            primary_power_source = "ios_battery_current_voltage"
    primary_power_samples = [
        sample
        for sample in samples
        if primary_power_source is None or sample.power_source == primary_power_source
    ]
    if not primary_power_samples:
        primary_power_samples = list(samples)
        primary_power_source = None
    excluded_power_sources = sorted(
        {
            sample.power_source
            for sample in samples
            if primary_power_source is not None
            and sample.power_source
            and sample.power_source != primary_power_source
        }
    )
    # iOS can expose battery I×V before the lower-cadence SystemLoad channel first
    # arrives.  They are different physical domains, so never integrate them into
    # one whole-device average.  The discarded samples remain available through
    # the dedicated battery-flow statistics and raw timeline.
    if platform == "ios" and excluded_power_sources:
        for sample in samples:
            if sample.power_source != primary_power_source:
                sample.power_valid_for_consumption = False

    stale_system_load_samples = []
    if platform == "ios":
        stale_system_load_samples = [
            sample
            for sample in primary_power_samples
            if sample.power_source == "ios_power_telemetry_system_load"
            and not is_power_sample_fresh_for_consumption(sample)
        ]
        for sample in stale_system_load_samples:
            sample.power_valid_for_consumption = False

    powers = [sample.power_mw for sample in primary_power_samples]
    consumption_samples = [
        sample for sample in samples if is_consumption_power_sample(sample)
    ]
    consumption_powers = [sample.power_mw for sample in consumption_samples]
    consumption_currents = [sample.current_ma for sample in consumption_samples]
    battery_flow_powers = [
        sample.current_ma * sample.voltage_mv / 1000.0 for sample in samples
    ]
    currents = [sample.current_ma for sample in samples]
    signed_currents = [sample.signed_current_ma for sample in samples]
    cpus = (
        [sample.cpu_pct for sample in samples if sample.cpu_pct is not None]
        if feature("cpu_usage")
        else []
    )
    power_sample_ages = [
        sample.power_sample_age_s
        for sample in primary_power_samples
        if sample.power_sample_age_s is not None
    ]
    collector_cpu_values = [
        sample.collector_cpu_pct
        for sample in samples
        if sample.collector_cpu_pct is not None
    ]
    test_mode = str(metadata.get("test_mode") or "power").strip().lower()
    if test_mode not in {"power", "performance"}:
        test_mode = "power"
    power_mode = test_mode == "power"
    invalid_power_observation_text = (
        "SystemLoad 仅作为整机原始功率通道，电流×电压另作为电池流量"
        if platform == "ios"
        else "电流和功率仅作为电池侧原始流量"
    )
    invalid_power_pressure_text = (
        "外部供电或充电时只保留整机 SystemLoad 原始通道与独立的电池 I×V 流量，"
        "不分析耗电压力来源。"
        if platform == "ios"
        else "充电或外部供电样本只展示电池侧原始流量，不分析功耗压力来源。"
    )
    duration_s = samples[-1].uptime_s - samples[0].uptime_s
    configured_interval = metadata.get("sample_interval_s")
    observed_intervals = [
        current.uptime_s - previous.uptime_s
        for previous, current in zip(samples, samples[1:])
        if current.uptime_s > previous.uptime_s
    ]
    sample_interval_s = (
        float(configured_interval)
        if isinstance(configured_interval, (int, float)) and float(configured_interval) > 0
        else statistics.median(observed_intervals) if observed_intervals else 1.0
    )
    cadence_target_s = sample_interval_s
    cadence_on_time_limit_s = max(
        cadence_target_s * 1.5,
        cadence_target_s + 0.25,
    )
    cadence_on_time_count = sum(
        1 for value in observed_intervals if value <= cadence_on_time_limit_s
    )
    cadence_on_time_pct = (
        cadence_on_time_count / len(observed_intervals) * 100.0
        if observed_intervals
        else None
    )
    cadence_p95_s = percentile(observed_intervals, 0.95)
    cadence_assessment_available = len(observed_intervals) >= 10
    sampling_cadence_stable = (
        bool(
            cadence_on_time_pct is not None
            and cadence_on_time_pct >= 90.0
            and cadence_p95_s is not None
            and cadence_p95_s <= max(cadence_target_s * 2.0, cadence_target_s + 1.0)
        )
        if cadence_assessment_available
        else None
    )
    estimated_missed_sample_slots = sum(
        max(0, int(round(value / cadence_target_s)) - 1)
        for value in observed_intervals
        if cadence_target_s > 0
    )
    max_gap_s = max(sample_interval_s * 3.0, sample_interval_s + 2.0)
    valid_intervals = sample_intervals(samples, max_gap_s)
    covered_duration_s = sum(item[2] for item in valid_intervals)
    primary_power_intervals = [
        item
        for item in valid_intervals
        if primary_power_source is None
        or (
            item[0].power_source == primary_power_source
            and item[1].power_source == primary_power_source
        )
    ]
    ios_system_load_primary = (
        platform == "ios"
        and primary_power_source == "ios_power_telemetry_system_load"
    )
    if ios_system_load_primary:
        (
            observed_power_energy_mwh,
            observed_power_covered_duration_s,
        ) = integrate_step_values(
            primary_power_samples,
            lambda sample: sample.power_mw,
            max_gap_s,
            transition_age_getter=lambda sample: sample.power_sample_age_s,
        )
    else:
        observed_power_covered_duration_s = sum(item[2] for item in primary_power_intervals)
        observed_power_energy_mwh = sum(
            (previous.power_mw + current.power_mw) * 0.5 * delta_s / 3600.0
            for previous, current, delta_s in primary_power_intervals
        )
    consumption_intervals = sample_intervals(
        samples,
        max_gap_s,
        require_consumption_power=True,
    )
    consumption_covered_duration_s = sum(item[2] for item in consumption_intervals)
    missing_duration_s = max(0.0, duration_s - covered_duration_s)
    average_voltage_mv = statistics.fmean(sample.voltage_mv for sample in samples)
    observed_power_average_mw = (
        observed_power_energy_mwh * 3600.0 / observed_power_covered_duration_s
        if observed_power_covered_duration_s > 0
        else statistics.fmean(powers)
    )
    battery_flow_energy_mwh = integrate_values(
        samples,
        lambda sample: sample.current_ma * sample.voltage_mv / 1000.0,
        max_gap_s,
    )
    battery_flow_average_power_mw = (
        battery_flow_energy_mwh * 3600.0 / covered_duration_s
        if covered_duration_s > 0
        else statistics.fmean(battery_flow_powers)
    )
    if consumption_intervals and ios_system_load_primary:
        energy_mwh, consumption_covered_duration_s = integrate_step_values(
            primary_power_samples,
            lambda sample: sample.power_mw,
            max_gap_s,
            require_consumption_power=True,
            transition_age_getter=lambda sample: sample.power_sample_age_s,
        )
    else:
        energy_mwh = (
            integrate_values(
                samples,
                lambda sample: sample.power_mw,
                max_gap_s,
                require_consumption_power=True,
            )
            if consumption_intervals
            else None
        )
    discharge_mah = (
        integrate_values(
            samples,
            lambda sample: sample.current_ma,
            max_gap_s,
            require_consumption_power=True,
        )
        if consumption_intervals
        else None
    )
    time_weighted_power_mw = (
        float(energy_mwh) * 3600.0 / consumption_covered_duration_s
        if energy_mwh is not None and consumption_covered_duration_s > 0
        else None
    )
    observed_time_weighted_current_ma = (
        integrate_values(samples, lambda sample: sample.current_ma, max_gap_s)
        * 3600.0
        / covered_duration_s
        if covered_duration_s > 0
        else statistics.fmean(currents)
    )
    time_weighted_current_ma = (
        float(discharge_mah) * 3600.0 / consumption_covered_duration_s
        if discharge_mah is not None and consumption_covered_duration_s > 0
        else None
    )
    observed_directions = {
        str(sample.direction or "unknown").strip().lower() for sample in samples
    }
    if samples and all(sample.external_power is True for sample in samples):
        power_flow_direction = "external_power"
    elif observed_directions == {"discharging"}:
        power_flow_direction = "discharging"
    elif observed_directions <= {"charging", "full"}:
        power_flow_direction = "charging"
    elif observed_directions <= {"idle", "unknown"}:
        power_flow_direction = "idle"
    else:
        power_flow_direction = "mixed"
    power_valid_for_consumption = bool(consumption_intervals)
    consumption_session_representative = bool(
        duration_s > 0
        and covered_duration_s / duration_s >= 0.9
        and primary_power_samples
        and all(is_consumption_power_sample(sample) for sample in primary_power_samples)
        and consumption_covered_duration_s / duration_s >= 0.9
    )
    analysis_warnings = list(warnings)
    if platform == "ios" and excluded_power_sources:
        analysis_warnings.append(
            "iOS 会话中同时观测到 SystemLoad 与电池 I×V fallback；"
            "整机功率统计只使用 SystemLoad，电池 I×V 仅保留在独立电池流量通道，"
            "两者不会合并求平均。"
        )
    if legacy_power_validity_inferred:
        analysis_warnings.append(
            "旧版记录未保存逐样本外部供电标志；因起止状态均明确为未插电放电，"
            "本次以低置信度恢复耗电分析。"
        )
    if stale_system_load_samples:
        analysis_warnings.append(
            f"iOS SystemLoad 有 {len(stale_system_load_samples)} 个样本因超过 "
            f"{IOS_SYSTEM_LOAD_STALE_AFTER_S:.0f} 秒未刷新或缺少刷新年龄而被排除；"
            "耗电与能量积分会在这些区间断开，不会沿用旧功率值。"
        )
    if not power_valid_for_consumption:
        analysis_warnings.append(
            "采集期间未获得连续的电池放电样本；"
            f"{invalid_power_observation_text}，"
            "不会用于设备耗电、测试项能量、平均功耗或续航结论。"
        )
    elif not consumption_session_representative:
        analysis_warnings.append(
            "采集期间存在充电、外部供电或较大的数据缺口；正式功耗和能量只统计连续有效的放电区间，"
            "BatteryStats、UID/组件/网络/Wakelock 归因及续航推算不会用于本次全会话结论。"
        )
    if duration_s > 0 and missing_duration_s > max_gap_s:
        analysis_warnings.append(
            f"遥测覆盖率为 {covered_duration_s / duration_s * 100.0:.1f}%；"
            "能量积分会排除连接或采集缺口，不会跨缺口插值。"
        )
    if metadata.get("session_mode") and not contexts:
        analysis_warnings.append(
            "会话模式没有采集到前台窗口上下文，因此无法按测试项聚合帧表现。"
            if not power_mode
            else "会话模式没有采集到前台应用上下文，因此无法按应用拆分整机实测能量。"
        )
    monitor_config = metadata.get("system_monitor", {})
    if (
        isinstance(monitor_config, dict)
        and monitor_config.get("enabled")
        and feature("process_snapshots")
        and not system_snapshots
    ):
        analysis_warnings.append(
            "已启用全系统监控，但未恢复到任何进程快照。"
        )

    package_uids, package_to_uid = (
        parse_package_uids(raw_outputs.get("packages", ""))
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else ({}, {})
    )
    usage = (
        parse_battery_usage(raw_outputs.get("batterystats_usage", ""), package_uids)
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else {
            "available": False,
            "analysis_disabled": True,
            "reason": (
                "性能模式不执行 BatteryStats 组件或 UID 功耗归因。"
                if not power_mode
                else "功耗来源归因已在采集配置中关闭。"
            ),
            "capacity_mah": None,
            "components": [],
            "uids": [],
        }
    )
    if power_mode and feature("power_attribution") and not consumption_session_representative:
        usage["reason"] = (
            "采集期间设备未持续由电池放电；BatteryStats 不用于本次设备耗电或 UID 归因。"
        )
    if cadence_assessment_available and sampling_cadence_stable is False:
        analysis_warnings.append(
            f"主采样节奏未达到设定的 {cadence_target_s:g} 秒："
            f"{float(cadence_on_time_pct or 0.0):.1f}% 的间隔在 "
            f"{cadence_on_time_limit_s:.2f} 秒内，P95 间隔为 {float(cadence_p95_s or 0.0):.2f} 秒。"
            "所有积分仍使用真实设备时间戳，但短时尖峰的时间分辨率会下降。"
        )
    stats_window = (
        extract_stats_window(raw_outputs.get("batterystats", ""))
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else {}
    )
    power_profile = (
        parse_power_profile(raw_outputs.get("power_profile", ""))
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else {}
    )
    display = parse_display(
        raw_outputs.get("display", ""),
        raw_outputs.get("screen_brightness", ""),
        raw_outputs.get("peak_refresh_rate", ""),
    )
    performance_analysis = analyze_performance_contexts(contexts, metadata)
    if not feature("frame_rate"):
        frame_unavailable_reason = (
            "当前 iOS sidecar 不提供通用应用 FPS、逐帧 Core Animation 时间戳或刷新率时间线；"
            "该采集项默认关闭且不生成帧率结论。"
            if platform == "ios"
            else "帧率与帧间隔采集已关闭。"
        )
        performance_analysis.update(
            {
                "sampled_compositor_fps": None,
                "minimum_sampled_compositor_fps": None,
                "sampled_frame_rate_fps": None,
                "minimum_sampled_frame_rate_fps": None,
                "frame_metric_average_ms": None,
                "frame_metric_p95_ms": None,
                "frame_metric_p99_ms": None,
                "one_percent_low_fps": None,
                "frame_rate_timeline": [],
                "frame_sample_count": 0,
                "frame_source": None,
                "frame_unavailable_reason": frame_unavailable_reason,
            }
        )
    if not isinstance(display.get("active_refresh_hz"), (int, float)) and isinstance(
        performance_analysis.get("current_refresh_rate_hz"), (int, float)
    ):
        display["active_refresh_hz"] = performance_analysis["current_refresh_rate_hz"]
    if not isinstance(display.get("peak_refresh_hz"), (int, float)) and isinstance(
        performance_analysis.get("peak_refresh_rate_hz"), (int, float)
    ):
        display["peak_refresh_hz"] = performance_analysis["peak_refresh_rate_hz"]
    if not isinstance(display.get("brightness_raw"), (int, float)) and isinstance(
        performance_analysis.get("brightness_raw"), (int, float)
    ):
        display["brightness_raw"] = performance_analysis["brightness_raw"]
    thermal_post_run = parse_thermal(raw_outputs.get("thermalservice", ""))
    processes = parse_cpu_processes(raw_outputs.get("cpuinfo", ""))
    wakelocks = (
        extract_kernel_wakelocks(raw_outputs.get("batterystats", ""))
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else []
    )

    policies = metadata.get("cpu_policies", [])
    cpu_analysis = analyze_cpu(
        samples,
        policies if isinstance(policies, list) else [],
        power_profile,
        max_gap_s,
    )
    if not feature("cpu_usage") and not feature("cpu_frequency"):
        cpu_analysis = {
            "available": False,
            "analysis_disabled": True,
            "reason": "CPU 利用率与频率采集均已关闭。",
            "clusters": [],
            "timeline": [],
        }
    if platform == "harmony":
        cpu_analysis["source"] = (
            "HarmonyOS SmartPerf SP_daemon"
            if capture_configuration.get("backend") == "harmony_smartperf"
            else "HarmonyOS /proc/stat + hidumper --cpufreq"
        )
        cpu_analysis["limitations"] = (
            "CPU utilization and cluster frequency are measured counters. HarmonyOS does not expose an "
            "Android Power Profile equivalent here, so no CPU rail power is modeled."
        )

    target_package = metadata.get("target_package")
    if not target_package and not metadata.get("session_mode"):
        target_package = metadata.get("foreground_package")
    target_uid = (
        package_to_uid.get(str(target_package))
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        and target_package
        else None
    )
    target_usage = (
        next(
            (item for item in usage.get("uids", []) if item.get("uid") == target_uid),
            None,
        )
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else None
    )
    target_network = (
        parse_checkin_network(raw_outputs.get("batterystats_checkin", ""), target_uid)
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else None
    )
    gpu_analysis = analyze_gpu(
        samples,
        metadata,
        (
            raw_outputs
            if power_mode
            and feature("power_attribution")
            and consumption_session_representative
            else {}
        ),
        package_uids,
        target_uid,
        duration_s,
        system_snapshots,
    )
    if not feature("gpu_metrics"):
        gpu_analysis.update(
            {
                "frequency_available": False,
                "load_available": False,
                "analysis_disabled": True,
                "reason": "GPU 指标采集已关闭。",
                "timeline": [],
            }
        )
    if not power_mode:
        gpu_analysis["work_by_uid"] = []
        gpu_analysis["target_work"] = None
        gpu_analysis["work_source_available"] = False
        gpu_analysis["memory"] = {
            "available": False,
            "analysis_disabled": True,
            "reason": "性能模式不采集或分析进程级 GPU 内存功耗归因。",
            "processes": [],
        }

    start_battery = metadata.get("battery_start", {})
    end_battery = metadata.get("battery_end", {})
    capacity_mah = usage.get("capacity_mah")
    if not isinstance(capacity_mah, (int, float)):
        profile_capacity = power_profile.get("battery.capacity")
        capacity_mah = profile_capacity if isinstance(profile_capacity, (int, float)) else None
    if not isinstance(capacity_mah, (int, float)) and isinstance(start_battery, dict):
        capacity_mah = next(
            (
                float(start_battery[key])
                for key in (
                    "full_charge_capacity_mah",
                    "nominal_charge_capacity_mah",
                    "design_capacity_mah",
                )
                if isinstance(start_battery.get(key), (int, float))
                and float(start_battery[key]) > 0
            ),
            None,
        )
    average_discharge_ma = (
        float(discharge_mah) * 3600.0 / consumption_covered_duration_s
        if discharge_mah is not None and consumption_covered_duration_s > 0
        else None
    )
    drain_pct_per_hour = (
        float(average_discharge_ma) / float(capacity_mah) * 100.0
        if consumption_session_representative
        and isinstance(average_discharge_ma, (int, float))
        and isinstance(capacity_mah, (int, float))
        and capacity_mah > 0
        else None
    )
    full_runtime_h = (
        float(capacity_mah) / float(average_discharge_ma)
        if consumption_session_representative
        and isinstance(average_discharge_ma, (int, float))
        and average_discharge_ma > 0
        and isinstance(capacity_mah, (int, float))
        else None
    )
    end_level = end_battery.get("level_pct") if isinstance(end_battery, dict) else None
    remaining_runtime_h = (
        full_runtime_h * float(end_level) / 100.0
        if full_runtime_h is not None and isinstance(end_level, (int, float))
        else None
    )

    components = (
        component_power_estimates(
            usage,
            stats_window,
            average_voltage_mv,
            power_profile,
            display,
        )
        if power_mode
        and feature("power_attribution")
        and consumption_session_representative
        else []
    )
    application_analysis = (
        analyze_applications(samples, contexts, max_gap_s)
        if power_mode
        and feature("foreground_window")
        and power_valid_for_consumption
        else {
            "available": False,
            "analysis_disabled": True,
            "reason": (
                "性能模式不按前台应用分配整机实测能量。"
                if not power_mode
                else "前台应用与窗口采集已关闭。"
                if not feature("foreground_window")
                else "没有连续有效的电池放电区间；前台上下文仅保留在原始时间序列中。"
            ),
            "rows": [],
            "transitions": [],
        }
    )
    event_analysis = (
        analyze_external_events(
            samples,
            events,
            max_gap_s,
            sample_interval_s,
        )
        if power_mode and power_valid_for_consumption
        else {
            "available": bool(events),
            "analysis_mode": (
                "power_unavailable" if power_mode else "performance"
            ),
            "analysis_disabled": bool(power_mode),
            "power_valid_for_consumption": False if power_mode else None,
            "reason": (
                "设备未持续由电池放电，外部事件仅保留时间标记，不计算能量。"
                if power_mode
                else None
            ),
            "event_count": len(events),
            "instant_count": sum(1 for event in events if not event.duration_s),
            "rows": [],
            "spans": [],
        }
    )
    system_analysis = analyze_system_activity(
        samples,
        system_snapshots,
        max_gap_s,
        thermal_snapshots,
    )
    if not feature("process_snapshots") and not feature("target_process"):
        system_analysis.update(
            {
                "available": False,
                "analysis_disabled": True,
                "reason": "进程与目标应用资源采集均已关闭。",
                "top_processes": [],
                "hot_threads": [],
            }
        )
    thermal_history = analyze_thermal_history(samples, thermal_snapshots)
    if thermal_history.get("available"):
        thermal: Dict[str, object] = {
            **thermal_post_run,
            **thermal_history,
            "status": thermal_history.get("latest_status"),
            "post_run": thermal_post_run,
        }
    else:
        thermal = {**thermal_post_run, **thermal_history, "post_run": thermal_post_run}
    brightness_throttling = (
        analyze_brightness_throttling(samples, contexts, thermal_snapshots)
        if platform == "android" and feature("thermal")
        else {
            "available": False,
            "analysis_disabled": platform != "android" or not feature("thermal"),
            "reason": (
                "当前平台未提供 Android DisplayManager 热亮度限制语义。"
                if platform != "android"
                else "温度与热限制采集已关闭。"
            ),
            "timeline": [],
            "points": [],
            "events": [],
            "point_count": 0,
            "event_count": 0,
        }
    )
    thermal["brightness_throttling"] = brightness_throttling
    if isinstance(display, dict):
        display["brightness_throttling"] = brightness_throttling
    scheduler_analysis = analyze_scheduler_history(samples, scheduler_snapshots)
    if not feature("thermal"):
        thermal = {
            "available": False,
            "analysis_disabled": True,
            "reason": "温度与热限制采集已关闭。",
            "sensors": [],
            "post_run": {},
        }
    if not feature("scheduler"):
        scheduler_analysis = {
            "available": False,
            "analysis_disabled": True,
            "reason": "调度与资源分配快照已关闭。",
            "cpusets": {},
            "cpu_policies": [],
            "hint_sessions": [],
            "watched_processes": [],
        }
    if platform == "ios" and thermal.get("available"):
        thermal["status"] = None
        thermal["latest_status"] = None
        thermal["maximum_status"] = None
        thermal["latest_status_label"] = "unavailable"
        thermal["maximum_status_label"] = "unavailable"
        thermal["severity_available"] = False
        thermal["throttling_observed"] = None
        thermal["source"] = "iOS DiagnosticsService battery temperature"
        thermal["limitations"] = (
            "The current iOS sidecar exposes battery temperature only. It does not provide a public thermal "
            "severity, throttling state, cooling-device state, threshold table, or thermal brightness cap; "
            "unknown severity must not be interpreted as status 0."
        )
        for sensor in thermal.get("sensors", []) if isinstance(thermal.get("sensors"), list) else []:
            if isinstance(sensor, dict):
                sensor["maximum_status"] = None
                sensor["maximum_status_label"] = "unavailable"
        for point in thermal.get("timeline", []) if isinstance(thermal.get("timeline"), list) else []:
            if isinstance(point, dict):
                point["status"] = None
                point["status_label"] = "unavailable"
                point["sensor_status"] = {}
    if platform == "harmony":
        smartperf_thermal = (
            str(capture_configuration.get("backend") or "") == "harmony_smartperf"
        )
        thermal["status"] = None
        thermal["latest_status"] = None
        thermal["maximum_status"] = None
        thermal["latest_status_label"] = "unavailable"
        thermal["maximum_status_label"] = "unavailable"
        thermal["severity_available"] = False
        thermal["throttling_observed"] = None
        for sensor in thermal.get("sensors", []) if isinstance(thermal.get("sensors"), list) else []:
            if isinstance(sensor, dict):
                sensor["maximum_status"] = None
                sensor["maximum_status_label"] = "unavailable"
        if feature("thermal"):
            thermal["source"] = (
                "HarmonyOS SmartPerf SP_daemon temperature fields"
                if smartperf_thermal
                else "HarmonyOS ThermalService via hidumper"
            )
            thermal["limitations"] = (
                "HarmonyOS SmartPerf exposes sampled temperature fields through SP_daemon, but it does not "
                "provide Android thermal severity, cooling-device or threshold semantics."
                if smartperf_thermal
                else "HarmonyOS ThermalService exposes sensor temperatures through hidumper, but this adapter does "
                "not receive Android thermal severity, cooling-device or threshold semantics."
            )
        scheduler_analysis["limitations"] = (
            "HarmonyOS snapshots retain cpufreq and PowerManager state. Android cpuset, ActivityManager and "
            "ADPF HintSession concepts are not present and remain explicitly unavailable."
        )
    if not power_mode or not power_valid_for_consumption:
        invalid_power_fields = {
            "power_mw",
            "energy_mwh",
            "average_power_mw",
            "baseline_power_mw",
            "power_delta_mw",
            "excess_energy_mwh",
            "power_correlation",
            "average_power_when_visible_mw",
            "power_delta_when_visible_mw",
            "average_relative_power_score",
            "maximum_relative_power_score",
        }

        def strip_invalid_power_metrics(value: object) -> None:
            if isinstance(value, dict):
                for key in list(value):
                    if key in invalid_power_fields:
                        value.pop(key, None)
                    else:
                        strip_invalid_power_metrics(value[key])
            elif isinstance(value, list):
                for item in value:
                    strip_invalid_power_metrics(item)

        strip_invalid_power_metrics(system_analysis)
        for collection_name in ("top_processes", "hot_threads"):
            collection = system_analysis.get(collection_name, [])
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for key in (
                    "average_power_when_visible_mw",
                    "power_delta_when_visible_mw",
                    "power_correlation",
                    "average_relative_power_score",
                    "maximum_relative_power_score",
                ):
                    item.pop(key, None)
        system_analysis["power_attribution_disabled"] = True
        system_analysis["power_attribution_note"] = (
            "采集期间未获得可用于耗电结论的连续放电样本；仅保留进程和线程 CPU 活动。"
            if power_mode
            else "性能模式仅把进程和线程 CPU 活动作为调度竞争证据，不分析其功耗来源。"
        )

    summary_analysis: Dict[str, object] = {
        "duration_s": duration_s,
        "covered_duration_s": covered_duration_s,
        "consumption_covered_duration_s": consumption_covered_duration_s,
        "consumption_coverage_pct": (
            consumption_covered_duration_s / duration_s * 100.0
            if duration_s > 0
            else 0.0
        ),
        "missing_duration_s": missing_duration_s,
        "coverage_pct": covered_duration_s / duration_s * 100.0 if duration_s > 0 else 0.0,
        "sample_count": len(samples),
        "configured_sample_interval_s": cadence_target_s,
        "observed_sample_interval_median_s": (
            statistics.median(observed_intervals) if observed_intervals else None
        ),
        "observed_sample_interval_p95_s": cadence_p95_s,
        "observed_sample_interval_maximum_s": (
            max(observed_intervals) if observed_intervals else None
        ),
        "sampling_interval_on_time_pct": cadence_on_time_pct,
        "sampling_cadence_assessment_available": cadence_assessment_available,
        "sampling_cadence_stable": sampling_cadence_stable,
        "estimated_missed_sample_slots": estimated_missed_sample_slots,
        "current_semantics": "positive magnitude",
        "average_current_ma": time_weighted_current_ma,
        "minimum_current_ma": min(consumption_currents) if power_valid_for_consumption else None,
        "maximum_current_ma": max(consumption_currents) if power_valid_for_consumption else None,
        "observed_average_current_ma": observed_time_weighted_current_ma,
        "observed_minimum_current_ma": min(currents),
        "observed_maximum_current_ma": max(currents),
        "average_signed_current_ma": statistics.fmean(signed_currents),
        "average_voltage_mv": average_voltage_mv,
        "power_flow_direction": power_flow_direction,
        "power_valid_for_consumption": power_valid_for_consumption,
        "consumption_session_representative": consumption_session_representative,
        "power_consumption_unavailable_reason": (
            None
            if power_valid_for_consumption
            else "采集期间设备未持续由电池放电；"
            f"{invalid_power_observation_text}。"
        ),
        "full_session_attribution_unavailable_reason": (
            None
            if consumption_session_representative
            else "会话不是连续、完整且可代表整场测试的电池放电窗口。"
        ),
        "observed_power_average_mw": observed_power_average_mw,
        "observed_power_median_mw": statistics.median(powers),
        "observed_power_p95_mw": percentile(powers, 0.95),
        "observed_power_minimum_mw": min(powers),
        "observed_power_maximum_mw": max(powers),
        "observed_power_energy_mwh": observed_power_energy_mwh,
        "observed_power_covered_duration_s": observed_power_covered_duration_s,
        "observed_power_primary_source": primary_power_source,
        "observed_power_excluded_sources": excluded_power_sources,
        "observed_power_sources": power_sources,
        "battery_flow_average_power_mw": battery_flow_average_power_mw,
        "battery_flow_median_power_mw": statistics.median(battery_flow_powers),
        "battery_flow_p95_power_mw": percentile(battery_flow_powers, 0.95),
        "battery_flow_minimum_power_mw": min(battery_flow_powers),
        "battery_flow_maximum_power_mw": max(battery_flow_powers),
        "battery_flow_energy_mwh": battery_flow_energy_mwh,
        "battery_flow_power_source": "battery_current_voltage",
        "average_power_mw": time_weighted_power_mw,
        "median_power_mw": (
            statistics.median(consumption_powers)
            if power_valid_for_consumption and consumption_powers
            else None
        ),
        "p95_power_mw": (
            percentile(consumption_powers, 0.95)
            if power_valid_for_consumption
            else None
        ),
        "minimum_power_mw": (
            min(consumption_powers)
            if power_valid_for_consumption and consumption_powers
            else None
        ),
        "maximum_power_mw": (
            max(consumption_powers)
            if power_valid_for_consumption and consumption_powers
            else None
        ),
        "energy_mwh": energy_mwh,
        "discharge_mah": discharge_mah,
        "energy_per_minute_mwh": (
            float(energy_mwh) / consumption_covered_duration_s * 60.0
            if energy_mwh is not None and consumption_covered_duration_s > 0
            else None
        ),
        "mah_per_minute": (
            float(discharge_mah) / consumption_covered_duration_s * 60.0
            if discharge_mah is not None and consumption_covered_duration_s > 0
            else None
        ),
        "average_cpu_pct": statistics.fmean(cpus) if cpus else None,
        "maximum_cpu_pct": max(cpus) if cpus else None,
        "power_sources": power_sources,
        "average_power_sample_age_s": (
            statistics.fmean(power_sample_ages) if power_sample_ages else None
        ),
        "maximum_power_sample_age_s": max(power_sample_ages) if power_sample_ages else None,
        "average_collector_cpu_pct": (
            statistics.fmean(collector_cpu_values) if collector_cpu_values else None
        ),
        "maximum_collector_cpu_pct": (
            max(collector_cpu_values) if collector_cpu_values else None
        ),
        "capacity_mah": capacity_mah,
        "drain_pct_per_hour": drain_pct_per_hour,
        "full_runtime_h": full_runtime_h,
        "remaining_runtime_h": remaining_runtime_h,
        "temperature_delta_c": (
            float(end_battery.get("temperature_c")) - float(start_battery.get("temperature_c"))
            if isinstance(start_battery, dict)
            and isinstance(end_battery, dict)
            and isinstance(start_battery.get("temperature_c"), (int, float))
            and isinstance(end_battery.get("temperature_c"), (int, float))
            else None
        ),
    }
    memory_analysis = analyze_memory_frequency(samples, metadata)
    if not feature("memory_frequency"):
        memory_analysis = {
            "available": False,
            "analysis_disabled": True,
            "reason": "内存频率采集已关闭。",
            "timeline": [],
        }
    settings_analysis = (
        analyze_runtime_settings(metadata, raw_outputs)
        if feature("runtime_settings")
        else {
            "available": False,
            "analysis_disabled": True,
            "reason": "系统设置快照已关闭。",
            "rows": [],
        }
    )
    if power_mode and power_valid_for_consumption:
        test_item_analysis = analyze_test_items(
            samples,
            contexts,
            events,
            system_snapshots,
            system_analysis,
            thermal,
            scheduler_analysis,
            max_gap_s,
            sample_interval_s,
        )
        power_pressure = analyze_power_pressure(
            samples,
            system_analysis,
            scheduler_analysis,
            thermal,
            memory_analysis,
            settings_analysis,
        )
        render_performance: Dict[str, object] = {
            "available": False,
            "analysis_disabled": True,
            "reason": "功耗模式不展开帧延迟和渲染链路归因。",
        }
    elif power_mode:
        test_item_analysis = {
            "available": False,
            "analysis_disabled": True,
            "power_valid_for_consumption": False,
            "reason": "设备未持续由电池放电，测试项耗电、平均功耗和能量排名均不生成。",
            "rows": [],
            "timeline": [],
        }
        power_pressure = {
            "available": False,
            "analysis_disabled": True,
            "power_valid_for_consumption": False,
            "reason": invalid_power_pressure_text,
        }
        render_performance = {
            "available": False,
            "analysis_disabled": True,
            "reason": "功耗模式不展开帧延迟和渲染链路归因。",
        }
    else:
        test_item_analysis = analyze_performance_test_items(
            samples,
            contexts,
            events,
            performance_analysis,
            thermal,
            scheduler_analysis,
            max_gap_s,
            sample_interval_s,
        )
        power_pressure = {
            "available": False,
            "analysis_disabled": True,
            "reason": "性能模式只记录整机功耗，不分析任务、组件或 UID 功耗来源。",
        }
        render_performance = analyze_render_performance(
            performance_analysis,
            system_analysis,
            scheduler_analysis,
            thermal,
            cpu_analysis,
            gpu_analysis,
            memory_analysis,
            summary_analysis,
        )
    analysis: Dict[str, object] = {
        "platform": platform,
        "test_mode": test_mode,
        "capture_configuration": capture_configuration,
        "summary": summary_analysis,
        "buckets": (
            build_buckets(samples) if power_mode and power_valid_for_consumption else []
        ),
        "long_windows": (
            build_long_windows(samples, contexts, max_gap_s)
            if power_mode and power_valid_for_consumption
            else []
        ),
        "spikes": (
            detect_spikes(samples) if power_mode and power_valid_for_consumption else []
        ),
        "cpu": cpu_analysis,
        "frequency_summary": cpu_analysis.get("clusters", []),
        "gpu": gpu_analysis,
        "battery_usage": usage,
        "components": components,
        "target_app": (
            {
                "package": target_package,
                "uid": target_uid,
                "usage": target_usage,
                "network": target_network,
            }
            if power_mode
            else {"package": target_package}
        )
        if target_package
        else None,
        "processes": processes,
        "system": system_analysis,
        "wakelocks": wakelocks,
        "display": display,
        "performance": performance_analysis,
        "memory": memory_analysis,
        "runtime_settings": settings_analysis,
        "power_pressure": power_pressure,
        "render_performance": render_performance,
        "thermal": thermal,
        "brightness_throttling": brightness_throttling,
        "scheduler": scheduler_analysis,
        "applications": application_analysis,
        "external_events": event_analysis,
        "test_items": test_item_analysis,
        "stats_window": stats_window,
        "data_sources": [],
        "warnings": analysis_warnings,
    }
    analysis["data_sources"] = _filter_analysis_data_sources(
        _analysis_data_sources(platform, test_mode, capture_configuration),
        analysis,
    )
    analysis["findings"] = build_findings(analysis)
    return analysis
