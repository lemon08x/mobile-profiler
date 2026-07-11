from __future__ import annotations

import bisect
import math
import statistics
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .collector import frequency_to_mhz
from .models import ContextSample, CpuPolicy, ExternalEvent, GpuSource, RawSample, Sample
from .parsers import (
    extract_kernel_wakelocks,
    extract_stats_window,
    format_bytes,
    parse_battery_usage,
    parse_checkin_network,
    parse_cpu_processes,
    parse_display,
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
) -> Tuple[List[Sample], List[str]]:
    if len(raw_samples) < 2:
        raise RuntimeError("sampler returned fewer than two valid rows")
    warnings: List[str] = []
    base_uptime = raw_samples[0].uptime_s
    duration = max(0.001, raw_samples[-1].uptime_s - base_uptime)
    sign_corrected = False
    samples: List[Sample] = []
    previous_raw: Optional[RawSample] = None

    for raw in raw_samples:
        elapsed = raw.uptime_s - base_uptime
        fraction = max(0.0, min(1.0, elapsed / duration))
        fallback_voltage = start_voltage_mv + (end_voltage_mv - start_voltage_mv) * fraction
        voltage = raw.voltage_mv if raw.voltage_mv and raw.voltage_mv > 0 else fallback_voltage
        sensor_ma = normalize_current_ma(raw.current_raw, current_unit)
        signed_current_ma, corrected = _directed_current(sensor_ma, battery_status)
        sign_corrected = sign_corrected or corrected
        current_ma = abs(signed_current_ma)
        if signed_current_ma < 0:
            direction = "discharging"
        elif signed_current_ma > 0:
            direction = "charging"
        else:
            direction = battery_status if battery_status in {"charging", "discharging"} else "idle"

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
                battery_temperature_c=(
                    raw.temperature_tenths_c / 10.0
                    if raw.temperature_tenths_c is not None
                    else None
                ),
            )
        )
        previous_raw = raw

    if sign_corrected:
        warnings.append(
            "The vendor current sign disagreed with BatteryService status and was normalized. "
            "current_ma is always a positive magnitude; signed_current_ma preserves charge direction."
        )
    return samples, warnings


def percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def detect_spikes(samples: Sequence[Sample]) -> List[Dict[str, float]]:
    if len(samples) < 5:
        return []
    powers = [sample.power_mw for sample in samples]
    median = statistics.median(powers)
    mad = median_absolute_deviation(powers)
    threshold = median + max(150.0, 3.0 * mad)
    windows: List[Dict[str, float]] = []
    current: List[Sample] = []
    for sample in samples:
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
) -> List[Tuple[Sample, Sample, float]]:
    intervals: List[Tuple[Sample, Sample, float]] = []
    for previous, current in zip(samples, samples[1:]):
        delta_s = current.uptime_s - previous.uptime_s
        if delta_s <= 0:
            continue
        if max_gap_s is not None and delta_s > max_gap_s:
            continue
        intervals.append((previous, current, delta_s))
    return intervals


def integrate_values(
    samples: Sequence[Sample],
    getter: Callable[[Sample], float],
    max_gap_s: Optional[float] = None,
) -> float:
    total = 0.0
    for previous, current, delta_s in sample_intervals(samples, max_gap_s):
        total += (getter(previous) + getter(current)) * 0.5 * delta_s / 3600.0
    return total


def build_buckets(samples: Sequence[Sample], width_s: float = 10.0) -> List[Dict[str, object]]:
    bucket_map: Dict[int, List[Sample]] = {}
    for sample in samples:
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
                "measured_power_correlation": _pearson(modeled_values, measured_for_model),
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
    return {
        "clusters": cluster_rows,
        "timeline": timeline,
        "modeled_power_mw": statistics.fmean(total_modeled) if total_modeled else None,
        "source": "Power Profile estimate",
        "limitations": (
            "The estimate combines /proc/stat utilization with instantaneous cpufreq and Android's "
            "per-core current table. It is useful for same-device comparisons, not a hardware rail measurement."
        ),
    }


def analyze_gpu(
    samples: Sequence[Sample],
    metadata: Dict[str, object],
    raw_outputs: Dict[str, str],
    package_uids: Dict[int, List[str]],
    target_uid: Optional[int],
    duration_s: float,
) -> Dict[str, object]:
    frequency_values = [
        sample.gpu_frequency_mhz for sample in samples if sample.gpu_frequency_mhz is not None
    ]
    load_values = [sample.gpu_load_pct for sample in samples if sample.gpu_load_pct is not None]
    start = parse_gpu_work(raw_outputs.get("gpu_start", ""))
    end = parse_gpu_work(raw_outputs.get("gpu_end", ""))
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
    reason = None
    if not frequency_values and isinstance(gpu_probe, dict):
        reason = gpu_probe.get("reason")
    return {
        "frequency_available": bool(frequency_values),
        "source": source,
        "unavailable_reason": reason,
        "average_frequency_mhz": statistics.fmean(frequency_values) if frequency_values else None,
        "minimum_frequency_mhz": min(frequency_values) if frequency_values else None,
        "maximum_frequency_mhz": max(frequency_values) if frequency_values else None,
        "average_load_pct": statistics.fmean(load_values) if load_values else None,
        "work_by_uid": work_rows[:20],
        "target_work": target_work,
        "work_source_available": bool(start and end),
        "limitations": (
            "GPU frequency is reported only when a readable OEM sysfs/devfreq node exists. "
            "UID active durations are cumulative driver evidence and are not an electrical power rail."
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
                "energy_mwh": 0.0,
                "discharge_mah": 0.0,
                "transition_count": 0,
                "activities": set(),
            },
        )
        row["duration_s"] = float(row["duration_s"]) + delta_s
        row["energy_mwh"] = float(row["energy_mwh"]) + (
            (previous.power_mw + current.power_mw) * 0.5 * delta_s / 3600.0
        )
        average_current = (previous.current_ma + current.current_ma) * 0.5
        if previous.direction == "discharging" or current.direction == "discharging":
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
        energy_mwh = float(row["energy_mwh"])
        activities = row.pop("activities")
        row["average_power_mw"] = energy_mwh * 3600.0 / duration_s if duration_s > 0 else None
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
    intervals = sample_intervals(samples, max_gap_s)
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
                "energy_mwh": energy_mwh,
                "discharge_mah": discharge_mah,
                "average_power_mw": energy_mwh * 3600.0 / covered_s if covered_s > 0 else None,
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
    for previous, current, _ in sample_intervals(samples, max_gap_s):
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


def build_findings(analysis: Dict[str, object]) -> List[Dict[str, str]]:
    summary = analysis["summary"]
    cpu = analysis.get("cpu", {})
    gpu = analysis.get("gpu", {})
    display = analysis.get("display", {})
    thermal = analysis.get("thermal", {})
    target = analysis.get("target_app")
    findings: List[Dict[str, str]] = [
        {
            "level": "measured",
            "title": "Measured battery output",
            "detail": (
                f"Average {float(summary.get('average_power_mw') or 0.0) / 1000.0:.3f} W at "
                f"{float(summary.get('average_current_ma') or 0.0):.1f} mA discharge magnitude; "
                f"P95 {float(summary.get('p95_power_mw') or 0.0) / 1000.0:.3f} W."
            ),
        }
    ]

    clusters = cpu.get("clusters", []) if isinstance(cpu, dict) else []
    modeled_clusters = [
        item for item in clusters if isinstance(item.get("frequency_premium_mw"), (int, float))
    ]
    if modeled_clusters:
        leading = max(modeled_clusters, key=lambda item: float(item.get("frequency_premium_mw") or 0.0))
        findings.append(
            {
                "level": "model",
                "title": f"{leading.get('label')} frequency effect",
                "detail": (
                    f"The Android CPU table estimates {float(leading.get('frequency_premium_mw') or 0.0):.1f} mW "
                    f"average above the same-load minimum-frequency baseline."
                ),
            }
        )

    if isinstance(gpu, dict):
        target_work = gpu.get("target_work")
        if isinstance(target_work, dict):
            findings.append(
                {
                    "level": "driver",
                    "title": "Target GPU activity",
                    "detail": (
                        f"The GPU driver attributed {float(target_work.get('active_ms') or 0.0):.1f} ms "
                        f"of active work to the target UID during this run."
                    ),
                }
            )
        elif not gpu.get("frequency_available"):
            findings.append(
                {
                    "level": "context",
                    "title": "GPU frequency unavailable",
                    "detail": str(gpu.get("unavailable_reason") or "The OEM did not expose a readable frequency node."),
                }
            )

    if isinstance(target, dict):
        uid_usage = target.get("usage")
        network = target.get("network")
        details = []
        if isinstance(uid_usage, dict):
            details.append(f"BatteryStats attributed {float(uid_usage.get('mah') or 0.0):.3f} mAh")
        if isinstance(network, dict):
            details.append(
                f"Wi-Fi {format_bytes(network.get('wifi_rx_bytes'))} received / "
                f"{format_bytes(network.get('wifi_tx_bytes'))} sent"
            )
        if details:
            findings.append(
                {
                    "level": "model",
                    "title": f"Target app: {target.get('package')}",
                    "detail": "; ".join(details) + ".",
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
                    "title": f"Largest foreground energy: {leading_app.get('package')}",
                    "detail": (
                        f"{float(leading_app.get('energy_mwh') or 0.0):.2f} mWh over "
                        f"{float(leading_app.get('duration_s') or 0.0):.1f} s of observed foreground time."
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
                "title": f"Largest imported phase: {leading_phase.get('name')}",
                "detail": (
                    f"{float(leading_phase.get('energy_mwh') or 0.0):.2f} mWh aligned from "
                    f"{leading_phase.get('phase')} log events."
                ),
            }
        )

    active_refresh = display.get("active_refresh_hz") if isinstance(display, dict) else None
    if isinstance(active_refresh, (int, float)):
        findings.append(
            {
                "level": "context",
                "title": "Display mode",
                "detail": f"The display rendered at {active_refresh:.0f} Hz during collection.",
            }
        )
    thermal_status = thermal.get("status") if isinstance(thermal, dict) else None
    if thermal_status == 0:
        findings.append(
            {
                "level": "low",
                "title": "No thermal throttling",
                "detail": "Android reported thermal status 0 after the run.",
            }
        )
    return findings


def analyze_run(
    samples: Sequence[Sample],
    metadata: Dict[str, object],
    raw_outputs: Dict[str, str],
    warnings: Sequence[str],
    contexts: Sequence[ContextSample] = (),
    events: Sequence[ExternalEvent] = (),
) -> Dict[str, object]:
    if len(samples) < 2:
        raise RuntimeError("at least two samples are required")
    powers = [sample.power_mw for sample in samples]
    currents = [sample.current_ma for sample in samples]
    signed_currents = [sample.signed_current_ma for sample in samples]
    cpus = [sample.cpu_pct for sample in samples if sample.cpu_pct is not None]
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
    max_gap_s = max(sample_interval_s * 3.0, sample_interval_s + 2.0)
    valid_intervals = sample_intervals(samples, max_gap_s)
    covered_duration_s = sum(item[2] for item in valid_intervals)
    missing_duration_s = max(0.0, duration_s - covered_duration_s)
    average_voltage_mv = statistics.fmean(sample.voltage_mv for sample in samples)
    energy_mwh = integrate_values(samples, lambda sample: sample.power_mw, max_gap_s)
    discharge_mah = integrate_values(
        samples,
        lambda sample: sample.current_ma if sample.direction == "discharging" else 0.0,
        max_gap_s,
    )
    time_weighted_power_mw = (
        energy_mwh * 3600.0 / covered_duration_s if covered_duration_s > 0 else statistics.fmean(powers)
    )
    time_weighted_current_ma = (
        integrate_values(samples, lambda sample: sample.current_ma, max_gap_s)
        * 3600.0
        / covered_duration_s
        if covered_duration_s > 0
        else statistics.fmean(currents)
    )
    analysis_warnings = list(warnings)
    if duration_s > 0 and missing_duration_s > max_gap_s:
        analysis_warnings.append(
            f"Telemetry coverage was {covered_duration_s / duration_s * 100.0:.1f}%; "
            "energy integration excludes ADB gaps instead of interpolating across them."
        )
    if metadata.get("session_mode") and not contexts:
        analysis_warnings.append(
            "Session mode had no foreground context samples, so measured energy could not be split by app."
        )

    package_uids, package_to_uid = parse_package_uids(raw_outputs.get("packages", ""))
    usage = parse_battery_usage(raw_outputs.get("batterystats_usage", ""), package_uids)
    stats_window = extract_stats_window(raw_outputs.get("batterystats", ""))
    power_profile = parse_power_profile(raw_outputs.get("power_profile", ""))
    display = parse_display(
        raw_outputs.get("display", ""),
        raw_outputs.get("screen_brightness", ""),
        raw_outputs.get("peak_refresh_rate", ""),
    )
    thermal = parse_thermal(raw_outputs.get("thermalservice", ""))
    processes = parse_cpu_processes(raw_outputs.get("cpuinfo", ""))
    wakelocks = extract_kernel_wakelocks(raw_outputs.get("batterystats", ""))

    policies = metadata.get("cpu_policies", [])
    cpu_analysis = analyze_cpu(
        samples,
        policies if isinstance(policies, list) else [],
        power_profile,
        max_gap_s,
    )

    target_package = metadata.get("target_package")
    if not target_package and not metadata.get("session_mode"):
        target_package = metadata.get("foreground_package")
    target_uid = package_to_uid.get(str(target_package)) if target_package else None
    target_usage = next(
        (item for item in usage.get("uids", []) if item.get("uid") == target_uid),
        None,
    )
    target_network = parse_checkin_network(raw_outputs.get("batterystats_checkin", ""), target_uid)
    gpu_analysis = analyze_gpu(
        samples,
        metadata,
        raw_outputs,
        package_uids,
        target_uid,
        duration_s,
    )

    start_battery = metadata.get("battery_start", {})
    end_battery = metadata.get("battery_end", {})
    capacity_mah = usage.get("capacity_mah")
    if not isinstance(capacity_mah, (int, float)):
        profile_capacity = power_profile.get("battery.capacity")
        capacity_mah = profile_capacity if isinstance(profile_capacity, (int, float)) else None
    average_discharge_ma = (
        discharge_mah * 3600.0 / covered_duration_s if covered_duration_s > 0 else 0.0
    )
    drain_pct_per_hour = (
        average_discharge_ma / float(capacity_mah) * 100.0
        if isinstance(capacity_mah, (int, float)) and capacity_mah > 0
        else None
    )
    full_runtime_h = (
        float(capacity_mah) / average_discharge_ma
        if average_discharge_ma > 0 and isinstance(capacity_mah, (int, float))
        else None
    )
    end_level = end_battery.get("level_pct") if isinstance(end_battery, dict) else None
    remaining_runtime_h = (
        full_runtime_h * float(end_level) / 100.0
        if full_runtime_h is not None and isinstance(end_level, (int, float))
        else None
    )

    components = component_power_estimates(
        usage,
        stats_window,
        average_voltage_mv,
        power_profile,
        display,
    )
    application_analysis = analyze_applications(samples, contexts, max_gap_s)
    event_analysis = analyze_external_events(
        samples,
        events,
        max_gap_s,
        sample_interval_s,
    )
    analysis: Dict[str, object] = {
        "summary": {
            "duration_s": duration_s,
            "covered_duration_s": covered_duration_s,
            "missing_duration_s": missing_duration_s,
            "coverage_pct": covered_duration_s / duration_s * 100.0 if duration_s > 0 else 0.0,
            "sample_count": len(samples),
            "current_semantics": "positive magnitude",
            "average_current_ma": time_weighted_current_ma,
            "minimum_current_ma": min(currents),
            "maximum_current_ma": max(currents),
            "average_signed_current_ma": statistics.fmean(signed_currents),
            "average_voltage_mv": average_voltage_mv,
            "average_power_mw": time_weighted_power_mw,
            "median_power_mw": statistics.median(powers),
            "p95_power_mw": percentile(powers, 0.95),
            "minimum_power_mw": min(powers),
            "maximum_power_mw": max(powers),
            "energy_mwh": energy_mwh,
            "discharge_mah": discharge_mah,
            "energy_per_minute_mwh": energy_mwh / duration_s * 60.0 if duration_s > 0 else None,
            "mah_per_minute": discharge_mah / duration_s * 60.0 if duration_s > 0 else None,
            "average_cpu_pct": statistics.fmean(cpus) if cpus else None,
            "maximum_cpu_pct": max(cpus) if cpus else None,
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
        },
        "buckets": build_buckets(samples),
        "long_windows": build_long_windows(samples, contexts, max_gap_s),
        "spikes": detect_spikes(samples),
        "cpu": cpu_analysis,
        "frequency_summary": cpu_analysis.get("clusters", []),
        "gpu": gpu_analysis,
        "battery_usage": usage,
        "components": components,
        "target_app": {
            "package": target_package,
            "uid": target_uid,
            "usage": target_usage,
            "network": target_network,
        }
        if target_package
        else None,
        "processes": processes,
        "wakelocks": wakelocks,
        "display": display,
        "thermal": thermal,
        "applications": application_analysis,
        "external_events": event_analysis,
        "stats_window": stats_window,
        "data_sources": [
            {
                "metric": "Battery current",
                "source": "BatteryService / fuel gauge",
                "kind": "measured",
            },
            {
                "metric": "Battery voltage",
                "source": "BatteryService state",
                "kind": "measured",
            },
            {
                "metric": "CPU utilization/frequency",
                "source": "/proc/stat + cpufreq",
                "kind": "measured counters",
            },
            {
                "metric": "CPU frequency impact",
                "source": "Android Power Profile",
                "kind": "model",
            },
            {
                "metric": "GPU activity",
                "source": "devfreq when readable; dumpsys gpu otherwise",
                "kind": "measured counters",
            },
            {
                "metric": "Component/app attribution",
                "source": "BatteryStats",
                "kind": "model",
            },
            {
                "metric": "Foreground application",
                "source": "ActivityManager context sampler",
                "kind": "measured counters",
            },
            {
                "metric": "Test phases and actions",
                "source": "Imported timestamped logs aligned by /proc/uptime",
                "kind": "context",
            },
        ],
        "warnings": analysis_warnings,
    }
    analysis["findings"] = build_findings(analysis)
    return analysis
