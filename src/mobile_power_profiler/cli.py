from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Sequence

from .analysis import analyze_run, convert_samples
from .evidence import copy_evidence_attachment, create_evidence_archive
from .collector import (
    adb_shell,
    collect_cpu_policies,
    collect_scheduler_snapshot,
    collect_streaming_session,
    collect_device_info,
    collect_foreground_package,
    collect_post_run_outputs,
    collect_system_snapshot,
    collect_text,
    collect_thermal_snapshot,
    detect_gpu_source,
    parse_context_samples,
    parse_normalized_samples,
    parse_raw_samples,
    list_adb_devices,
    select_device,
)
from .ios import (
    DEFAULT_IOS_PYTHON,
    collect_ios_session,
    ios_device_id,
    ios_udid,
    list_ios_devices,
    pair_ios_device,
    probe_ios_device,
    select_ios_device,
)
from .comparison import build_run_comparison, write_comparison
from .log_import import import_timestamped_log
from .models import (
    APP_NAME,
    ClockSyncPoint,
    ContextSample,
    CpuPolicy,
    DEFAULT_ADB,
    DEFAULT_DURATION_S,
    DEFAULT_INTERVAL_S,
    ExternalEvent,
    GpuSource,
    RawSample,
    SCHEMA_VERSION,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
)
from .parsers import first_number, parse_battery, parse_gpu_dump
from .storage import (
    RunJournal,
    load_clock_sync,
    load_checkpoint,
    load_contexts,
    load_events,
    load_run_metadata,
    load_raw_outputs,
    load_scheduler_snapshots,
    load_system_snapshots,
    load_thermal_snapshots,
    read_samples_csv,
    write_jsonl,
    write_run_artifacts,
)


def default_output_dir(platform: str = "android") -> Path:
    return Path("power-runs") / datetime.now().strftime(f"{platform}-power-%Y%m%d-%H%M%S")


def requested_platform(args: argparse.Namespace) -> str:
    platform = str(getattr(args, "platform", "auto") or "auto")
    device = str(getattr(args, "device", "") or "")
    if platform != "auto":
        return platform
    if device.lower().startswith("ios:"):
        return "ios"
    if device:
        return "android"
    android_devices, _ = list_adb_devices(args.adb)
    android_ready = [item for item in android_devices if item.get("state") == "device"]
    ios_devices, _ = list_ios_devices(args.ios_python)
    ios_ready = [item for item in ios_devices if item.get("state") == "device"]
    if not android_ready and ios_ready:
        return "ios"
    return "android"


def filter_events_by_metadata(
    events: Sequence[ExternalEvent],
    expressions: Sequence[str],
) -> tuple[list[ExternalEvent], Dict[str, str]]:
    filters: Dict[str, str] = {}
    for expression in expressions:
        if "=" not in expression:
            raise ValueError(f"invalid --match {expression!r}; expected FIELD=VALUE")
        field, expected = expression.split("=", 1)
        field = field.strip()
        if not field or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise ValueError(f"invalid metadata field in --match: {field!r}")
        filters[field] = expected.strip()
    if not filters:
        return list(events), filters
    return (
        [
            event
            for event in events
            if all(str(event.metadata.get(field, "")) == expected for field, expected in filters.items())
        ],
        filters,
    )


def print_run_summary(output_dir: Path, analysis: Dict[str, object], report_path: Path) -> None:
    summary = analysis["summary"]
    print(f"\n{APP_NAME}")
    print("=" * len(APP_NAME))
    print(f"Output: {output_dir.resolve()}")
    print(f"Report: {report_path.resolve()}")
    print(f"Duration: {float(summary.get('duration_s') or 0.0):.1f}s")
    print(f"Average current: {float(summary.get('average_current_ma') or 0.0):.1f} mA (positive magnitude)")
    print(f"Average power: {float(summary.get('average_power_mw') or 0.0) / 1000.0:.3f} W")
    print(f"P95 power: {float(summary.get('p95_power_mw') or 0.0) / 1000.0:.3f} W")
    cpu = analysis.get("cpu", {})
    if isinstance(cpu, dict) and isinstance(cpu.get("modeled_power_mw"), (int, float)):
        print(f"Modeled CPU power: {float(cpu['modeled_power_mw']):.1f} mW")
    gpu = analysis.get("gpu", {})
    if isinstance(gpu, dict):
        if gpu.get("frequency_available"):
            print(f"Average GPU frequency: {float(gpu.get('average_frequency_mhz') or 0.0):.0f} MHz")
        elif gpu.get("load_available"):
            print(f"Average GPU load: {float(gpu.get('average_load_pct') or 0.0):.1f}%")
        elif gpu.get("work_source_available"):
            print("GPU frequency: unavailable; UID work-duration evidence captured")
    warnings = analysis.get("warnings", [])
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")


def _policies_from_metadata(metadata: Dict[str, object]) -> list[CpuPolicy]:
    policies: list[CpuPolicy] = []
    for item in metadata.get("cpu_policies", []):
        if isinstance(item, dict):
            policies.append(CpuPolicy(**item))
    return policies


def _gpu_from_metadata(metadata: Dict[str, object]) -> Optional[GpuSource]:
    value = metadata.get("gpu_source")
    return GpuSource(**value) if isinstance(value, dict) else None


def _normalize_raw_samples(raw_samples: Sequence[RawSample]) -> list[RawSample]:
    normalized: list[RawSample] = []
    last_uptime: Optional[float] = None
    for item in raw_samples:
        uptime = float(getattr(item, "uptime_s"))
        if last_uptime is not None and uptime <= last_uptime:
            continue
        setattr(item, "index", len(normalized))
        normalized.append(item)
        last_uptime = uptime
    return normalized


def finalize_run(
    output_dir: Path,
    extra_warnings: Sequence[str] = (),
    collection_status: Optional[str] = None,
) -> tuple[Dict[str, object], Path]:
    metadata = load_run_metadata(output_dir)
    policies = _policies_from_metadata(metadata)
    gpu_source = _gpu_from_metadata(metadata)
    raw_outputs = load_raw_outputs(output_dir)
    sampler_text = raw_outputs.get("sampler-stream") or raw_outputs.get("sampler_stdout") or ""
    normalized_samples = parse_normalized_samples(sampler_text, policies, gpu_source)
    parsed_raw = _normalize_raw_samples(parse_raw_samples(sampler_text, policies, gpu_source))

    samples_path = output_dir / "samples.csv"
    conversion_warnings: list[str] = []
    battery_start = metadata.get("battery_start", {})
    platform = str(metadata.get("platform") or "android")
    stored_battery_end = metadata.get("battery_end")
    battery_end = (
        dict(stored_battery_end)
        if platform != "android" and isinstance(stored_battery_end, dict)
        else parse_battery(raw_outputs.get("battery_end", ""))
    )
    if not battery_end:
        battery_end = dict(battery_start) if isinstance(battery_start, dict) else {}
        conversion_warnings.append(
            "无法读取测试结束时的电池状态，已使用起始电压和温度作为回退值。"
        )
    metadata["battery_end"] = battery_end

    if len(normalized_samples) >= 2:
        samples = normalized_samples
    elif len(parsed_raw) >= 2:
        start_voltage = battery_start.get("voltage_mv") if isinstance(battery_start, dict) else None
        end_voltage = battery_end.get("voltage_mv") if isinstance(battery_end, dict) else None
        if not isinstance(start_voltage, (int, float)):
            start_voltage = next(
                (item.voltage_mv for item in parsed_raw if getattr(item, "voltage_mv", None)),
                None,
            )
        if not isinstance(start_voltage, (int, float)):
            raise RuntimeError("could not recover a valid battery voltage")
        if not isinstance(end_voltage, (int, float)):
            end_voltage = float(start_voltage)
        sample_interval = float(metadata.get("sample_interval_s") or DEFAULT_INTERVAL_S)
        samples, converted = convert_samples(
            parsed_raw,
            policies,
            gpu_source,
            float(start_voltage),
            float(end_voltage),
            str(metadata.get("current_unit") or "auto"),
            str(battery_start.get("status") or "unknown")
            if isinstance(battery_start, dict)
            else "unknown",
            max_cpu_gap_s=max(sample_interval * 3.0, sample_interval + 2.0),
        )
        conversion_warnings.extend(converted)
    elif samples_path.exists():
        samples = read_samples_csv(samples_path)
    else:
        raise RuntimeError("run contains fewer than two recoverable sampler rows")

    contexts = load_contexts(output_dir)
    if not contexts and sampler_text:
        contexts = parse_context_samples(sampler_text, policies, gpu_source)
    clock_sync = load_clock_sync(output_dir)
    events = load_events(output_dir)
    system_snapshots = load_system_snapshots(output_dir)
    thermal_snapshots = load_thermal_snapshots(output_dir)
    scheduler_snapshots = load_scheduler_snapshots(output_dir)
    stable_warnings = metadata.get("collection_warnings", [])
    warnings = [str(item) for item in stable_warnings] if isinstance(stable_warnings, list) else []
    warnings.extend(conversion_warnings)
    persisted_finalization = metadata.get("finalization_warnings", [])
    if not isinstance(persisted_finalization, list):
        persisted_finalization = []
    for item in extra_warnings:
        text = str(item)
        if text not in persisted_finalization:
            persisted_finalization.append(text)
    metadata["finalization_warnings"] = persisted_finalization
    warnings.extend(str(item) for item in persisted_finalization)
    metadata["schema_version"] = SCHEMA_VERSION
    metadata["report_generated_at"] = datetime.now().isoformat(timespec="seconds")
    metadata["actual_duration_s"] = samples[-1].uptime_s - samples[0].uptime_s
    if collection_status:
        metadata["collection_status"] = collection_status
    checkpoint = load_checkpoint(output_dir)
    if not metadata.get("collection_stop_reason") and checkpoint.get("stop_reason"):
        metadata["collection_stop_reason"] = checkpoint["stop_reason"]
    analysis = analyze_run(
        samples,
        metadata,
        raw_outputs,
        warnings,
        contexts,
        events,
        system_snapshots,
        thermal_snapshots,
        scheduler_snapshots,
    )
    report_path, _ = write_run_artifacts(
        output_dir,
        metadata,
        analysis,
        samples,
        raw_outputs,
        contexts,
        clock_sync,
        events,
        system_snapshots,
        thermal_snapshots,
        scheduler_snapshots,
    )
    with RunJournal(output_dir) as journal:
        journal.checkpoint(
            {
                "status": "complete",
                "sample_count": len(samples),
                "context_count": len(contexts),
                "clock_sync_count": len(clock_sync),
                "event_count": len(events),
                "system_snapshot_count": len(system_snapshots),
                "thermal_snapshot_count": len(thermal_snapshots),
                "scheduler_snapshot_count": len(scheduler_snapshots),
                "last_device_uptime_s": samples[-1].uptime_s,
                "stop_reason": metadata.get("collection_stop_reason", "completed"),
            }
        )
    return analysis, report_path


def run_probe(args: argparse.Namespace) -> int:
    if requested_platform(args) == "ios":
        try:
            info = probe_ios_device(args.device, args.ios_python)
        except (RuntimeError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        device = info.get("device", {})
        battery = info.get("battery", {})
        gpu = info.get("gpu_probe", {})
        monitor = info.get("system_monitor", {})
        print(
            f"Device: Apple {device.get('model')} / {device.get('product_type')} / "
            f"iOS {device.get('ios')}"
        )
        print(f"Serial: {device.get('serial')}")
        print(f"Battery: {battery}")
        print(f"Power telemetry: {info.get('power_telemetry_available', False)}")
        print(
            "GPU: "
            f"device={gpu.get('device_utilization_pct')}%, "
            f"renderer={gpu.get('renderer_utilization_pct')}%, "
            f"tiler={gpu.get('tiler_utilization_pct')}%"
        )
        print(f"Processes: {monitor.get('process_count') or 'n/a'}")
        print(f"Connection: {info.get('connection')}")
        return 0
    try:
        device = select_device(args.adb, args.device)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    battery_text = collect_text(args.adb, device, ["dumpsys", "battery"], timeout_s=15)
    current_result = adb_shell(
        args.adb, device, ["cmd", "battery", "get", "-f", "current_now"], timeout_s=10
    )
    gpu_source, gpu_probe = detect_gpu_source(
        args.adb, device, getattr(args, "gpu_frequency_path", None)
    )
    perfetto = adb_shell(args.adb, device, ["perfetto", "--query"], timeout_s=20)
    powerstats = adb_shell(args.adb, device, ["dumpsys", "powerstats"], timeout_s=20)
    gpu_dump = adb_shell(args.adb, device, ["dumpsys", "gpu"], timeout_s=30)
    gpu_dump_details = parse_gpu_dump(gpu_dump.stdout)
    system_snapshot, system_error = collect_system_snapshot(args.adb, device, False)
    thermal_snapshot, thermal_error = collect_thermal_snapshot(args.adb, device)
    scheduler_snapshot, scheduler_warnings = collect_scheduler_snapshot(args.adb, device, set())
    info = {
        "device": collect_device_info(args.adb, device),
        "battery": parse_battery(battery_text),
        "current_command": current_result.stdout.strip(),
        "current_command_ok": current_result.ok,
        "cpu_policies": [asdict(item) for item in collect_cpu_policies(args.adb, device)],
        "gpu_source": asdict(gpu_source) if gpu_source else None,
        "gpu_probe": gpu_probe,
        "gpu_work_duration_available": "GPU work information" in gpu_dump.stdout,
        "gpu_memory_snapshot_available": bool(gpu_dump_details.get("memory_available")),
        "gpu_memory_total_bytes": gpu_dump_details.get("global_total_bytes"),
        "perfetto_android_power": "android.power" in perfetto.stdout,
        "perfetto_sysfs_power": "linux.sysfs_power" in perfetto.stdout,
        "powerstats_dump_available": bool(powerstats.stdout.strip()),
        "foreground_package": collect_foreground_package(args.adb, device),
        "system_monitor": {
            "process_top_available": system_snapshot is not None and bool(system_snapshot.processes),
            "process_count": system_snapshot.process_count if system_snapshot else None,
            "watched_services": [
                item.get("watch_name")
                for item in (system_snapshot.watched_processes if system_snapshot else [])
            ],
            "process_error": system_error,
            "thermalservice_available": thermal_snapshot is not None
            and bool(thermal_snapshot.temperatures),
            "thermal_sensor_count": len(thermal_snapshot.temperatures) if thermal_snapshot else 0,
            "thermal_threshold_count": len(thermal_snapshot.thresholds) if thermal_snapshot else 0,
            "thermal_error": thermal_error,
            "cpusets": scheduler_snapshot.cpusets if scheduler_snapshot else {},
            "cpu_policies": scheduler_snapshot.cpu_policies if scheduler_snapshot else [],
            "adpf_available": scheduler_snapshot is not None
            and bool(scheduler_snapshot.availability.get("adpf_hint_sessions")),
            "adpf_active_session_count": len(scheduler_snapshot.hint_sessions)
            if scheduler_snapshot
            else 0,
            "scheduler_warnings": scheduler_warnings,
        },
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    device_info = info["device"]
    print(
        f"Device: {device_info.get('brand')} {device_info.get('model')} / "
        f"{device_info.get('soc_model')} / Android {device_info.get('android')}"
    )
    print(f"Serial: {device}")
    print(f"Battery: {info['battery']}")
    print(f"Current command: {info['current_command'] or 'unavailable'}")
    policy_summaries = []
    for item in info["cpu_policies"]:
        core_control = item.get("core_control", {})
        core_control = core_control if isinstance(core_control, dict) else {}
        runtime = []
        if item.get("governor"):
            runtime.append(str(item["governor"]))
        if core_control.get("min_cpus") is not None:
            runtime.append(
                f"core_ctl {core_control.get('min_cpus')}-{core_control.get('max_cpus')}"
            )
        policy_summaries.append(
            f"{item['label']}={item['name']} cores {item['cores']} "
            f"max {float(item.get('max_khz') or 0) / 1000:.0f} MHz"
            + (f" ({', '.join(runtime)})" if runtime else "")
        )
    print("CPU policies: " + (", ".join(policy_summaries) or "none"))
    if info["gpu_source"] and info["gpu_source"].get("frequency_path"):
        print(f"GPU frequency: readable from {info['gpu_source']['frequency_path']}")
    elif info["gpu_source"] and info["gpu_source"].get("load_path"):
        print(f"GPU load: readable from {info['gpu_source']['load_path']}")
    else:
        print(f"GPU frequency: unavailable ({gpu_probe.get('reason')})")
    if gpu_probe.get("model"):
        print(
            f"GPU platform: {gpu_probe.get('model')} "
            f"({gpu_probe.get('provider', 'unknown provider')})"
        )
    print(
        "GPU hardware counter source: "
        f"{gpu_probe.get('perfetto_hardware_counter_source_available', False)} "
        f"(profiler support property={gpu_probe.get('graphics_gpu_profiler_support', False)})"
    )
    print(f"GPU UID work duration: {info['gpu_work_duration_available']}")
    print(
        "GPU memory snapshot: "
        f"{info['gpu_memory_snapshot_available']} "
        f"(total={info.get('gpu_memory_total_bytes')})"
    )
    print(f"Foreground package: {info['foreground_package'] or 'unknown'}")
    monitor = info["system_monitor"]
    print(
        "System monitor: "
        f"processes={monitor['process_count'] or 'n/a'}, "
        f"thermal sensors={monitor['thermal_sensor_count']}, "
        f"ADPF sessions={monitor['adpf_active_session_count']}"
    )
    print(
        "cpusets: "
        + (", ".join(f"{name}={cpus}" for name, cpus in monitor["cpusets"].items()) or "unavailable")
    )
    return 0


def run_ios_pair(args: argparse.Namespace) -> int:
    try:
        result = pair_ios_device(args.device, args.ios_python, args.timeout)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        endpoint = result.get("endpoint")
        endpoint = endpoint if isinstance(endpoint, dict) else {}
        print(f"RemotePairing ready: {result.get('serial')}")
        if endpoint.get("host") and endpoint.get("port"):
            print(f"Wi-Fi endpoint: {endpoint['host']}:{endpoint['port']}")
            print("The USB cable can now be removed.")
        else:
            print("Pairing succeeded, but no Wi-Fi endpoint was discovered; keep the phone unlocked and retry.")
    return 0


def run_ios_record(args: argparse.Namespace) -> int:
    output_dir = args.output or default_output_dir("ios")
    if output_dir.exists() and any(output_dir.iterdir()):
        print(
            f"ERROR: output directory is not empty: {output_dir}. Use recover/report for it.",
            file=sys.stderr,
        )
        return 2
    try:
        selected = select_ios_device(args.device, args.ios_python)
        probe = probe_ios_device(selected["serial"], args.ios_python)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    device_info = probe.get("device")
    device_info = device_info if isinstance(device_info, dict) else {}
    battery_start = probe.get("battery")
    battery_start = battery_start if isinstance(battery_start, dict) else {}
    connection = probe.get("connection")
    connection = connection if isinstance(connection, dict) else {}
    target_package = args.package if args.package else None
    warnings: list[str] = [
        "iOS whole-device PowerTelemetry is physically measured but typically refreshes about every 20 seconds; "
        "one-second CPU/GPU and power-score rows are diagnostic evidence, not one-second physical power rails.",
        "iOS DVT sysmond/DTServiceHub/remotepairingdeviced add measurable collection overhead; "
        "collector_cpu_pct is retained in samples and profiler processes are tagged in system snapshots.",
    ]
    powered = battery_start.get("powered")
    if powered:
        warnings.append(
            "The iPhone is externally powered. Battery current is not a clean unplugged discharge measurement."
        )
        if args.require_unplugged:
            print("ERROR: iPhone is externally powered; unplug it before recording", file=sys.stderr)
            return 4

    gpu_source = probe.get("gpu_source")
    gpu_source = gpu_source if isinstance(gpu_source, dict) else None
    metadata: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "platform": "ios",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": args.title
        or (
            "Multi-app iOS power session"
            if args.session_mode and not target_package
            else f"{target_package} power test" if target_package else "iOS Power Profiler"
        ),
        "device": device_info,
        "device_id": selected["serial"],
        "ios_udid": selected["udid"],
        "connection": connection,
        "target_package": target_package,
        "foreground_package": None,
        "capture_start": {
            "expected_context": args.start_context,
            "note": args.start_note,
            "host_epoch_s": datetime.now().timestamp(),
            "observed_foreground_package": None,
            "workflow_synchronization": "independent_start",
        },
        "session_mode": bool(args.session_mode),
        "requested_duration_s": args.duration,
        "sample_interval_s": args.interval,
        "power_observation_interval_s": 20.0,
        "sampling_schedule_s": {
            "cpu_gpu_process": args.interval,
            "battery_power": 5.0,
            "physical_power_update_hint": 20.0,
            "application_state": "event_driven",
            "system_processes": args.process_interval,
        },
        "system_monitor": {
            "enabled": not args.no_system_monitor,
            "process_interval_s": args.process_interval,
            "thread_interval_s": None,
            "thermal_interval_s": 5.0,
            "scheduler_interval_s": None,
            "priority_processes": [
                "sysmond",
                "DTServiceHub",
                "remotepairingdeviced",
            ],
        },
        "checkpoint_interval_s": args.checkpoint_interval,
        "reconnect_timeout_s": args.reconnect_timeout,
        "current_unit": "ma",
        "current_semantics": "current_ma is positive magnitude; signed_current_ma preserves iOS battery direction",
        "power_semantics": "power_mw prefers iOS PowerTelemetryData.SystemLoad; power_sample_age_s preserves its cadence",
        "cpu_semantics": "sum of DVT per-process cpuUsage divided by logical CPU count",
        "cpu_policies": [],
        "gpu_source": gpu_source,
        "gpu_probe": probe.get("gpu_probe"),
        "capabilities": probe.get("capabilities"),
        "battery_before": battery_start,
        "battery_start": battery_start,
        "collection_status": "collecting",
        "collection_warnings": warnings,
    }
    print(
        f"Recording {args.duration}s from {selected['serial']} over "
        f"{connection.get('host')}:{connection.get('port')}...",
        file=sys.stderr,
    )

    collection = None
    try:
        with RunJournal(output_dir) as journal:
            journal.write_metadata(metadata)
            journal.write_raw_output("ios_probe", json.dumps(probe, ensure_ascii=False, indent=2))
            collection = collect_ios_session(
                args.ios_python,
                selected,
                args.duration,
                args.interval,
                journal,
                checkpoint_interval_s=args.checkpoint_interval,
                reconnect_timeout_s=args.reconnect_timeout,
                system_monitor_enabled=not args.no_system_monitor,
                process_interval_s=args.process_interval,
            )
            metadata["collection_stop_reason"] = collection.stop_reason
            metadata["collection_host_elapsed_s"] = collection.host_elapsed_s
            metadata["reconnect_count"] = collection.reconnect_count
            metadata["sampler_launch_count"] = collection.sampler_launch_count
            metadata["system_snapshot_count"] = collection.system_snapshot_count
            metadata["thermal_snapshot_count"] = collection.thermal_snapshot_count
            metadata["scheduler_snapshot_count"] = 0
            metadata["battery_end"] = collection.battery_end or battery_start
            metadata["ios_collection_stats"] = collection.stats
            average_overhead = collection.stats.get("average_collector_cpu_pct")
            if isinstance(average_overhead, (int, float)):
                warnings.append(
                    f"Average normalized iOS collector CPU overhead was {float(average_overhead):.2f}% during this run."
                )
            metadata["collection_warnings"] = warnings + collection.warnings
            metadata["collection_status"] = (
                "collected" if collection.stop_reason == "completed" else "partial"
            )
            journal.write_metadata(metadata)
            journal.write_raw_output(
                "ios_battery_end",
                json.dumps(metadata["battery_end"], ensure_ascii=False, indent=2),
            )
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: iOS collector failed: {exc}", file=sys.stderr)
        try:
            analysis, report_path = finalize_run(
                output_dir,
                [f"iOS collection stopped because of an error: {exc}"],
                "partial",
            )
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError):
            return 6
        print_run_summary(output_dir, analysis, report_path)
        return 6
    except KeyboardInterrupt:
        print("\nCollection interrupted; finalizing the recoverable portion...", file=sys.stderr)
        try:
            analysis, report_path = finalize_run(
                output_dir,
                ["iOS collection was interrupted; recoverable data has been retained."],
                "interrupted",
            )
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Partial data kept in {output_dir.resolve()}: {exc}", file=sys.stderr)
            return 130
        print_run_summary(output_dir, analysis, report_path)
        return 130

    if collection is None or collection.sample_count < 2:
        print(
            f"ERROR: fewer than two iOS samples were collected; partial data is in {output_dir}",
            file=sys.stderr,
        )
        return 7
    final_status = "complete" if collection.stop_reason == "completed" else "partial"
    analysis, report_path = finalize_run(output_dir, collection_status=final_status)
    print_run_summary(output_dir, analysis, report_path)
    return 0 if final_status == "complete" else 6


def run_record(args: argparse.Namespace) -> int:
    if requested_platform(args) == "ios":
        return run_ios_record(args)
    output_dir = args.output or default_output_dir()
    if output_dir.exists() and any(output_dir.iterdir()):
        print(
            f"ERROR: output directory is not empty: {output_dir}. Use recover/report for it.",
            file=sys.stderr,
        )
        return 2
    try:
        device = select_device(args.adb, args.device)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    device_info = collect_device_info(args.adb, device)
    policies = collect_cpu_policies(args.adb, device)
    gpu_source, gpu_probe = detect_gpu_source(args.adb, device, args.gpu_frequency_path)
    foreground = collect_foreground_package(args.adb, device)
    target_package = args.package if args.session_mode else (args.package or foreground)
    battery_before_text = collect_text(args.adb, device, ["dumpsys", "battery"], timeout_s=15)
    battery_before = parse_battery(battery_before_text)
    current_probe = adb_shell(
        args.adb, device, ["cmd", "battery", "get", "-f", "current_now"], timeout_s=10
    )
    if not current_probe.ok or first_number(current_probe.stdout) is None:
        print(
            "ERROR: the device does not expose 'cmd battery get -f current_now'. "
            "Use a phone-side BatteryManager agent on this model.",
            file=sys.stderr,
        )
        return 3

    warnings: list[str] = []
    if battery_before.get("powered"):
        warnings.append(
            "设备处于外部供电状态。电量计电流是电池净流量，不代表设备总输入功率。"
        )
        if args.require_unplugged:
            print("ERROR: device is powered; unplug it or omit --require-unplugged", file=sys.stderr)
            return 4

    if not args.no_reset:
        reset = adb_shell(args.adb, device, ["dumpsys", "batterystats", "--reset"], timeout_s=20)
        if not reset.ok:
            warnings.append("BatteryStats 重置失败，归因结果可能包含测试开始前的活动。")
    if args.full_history:
        adb_shell(
            args.adb,
            device,
            ["dumpsys", "batterystats", "enable", "full-history"],
            timeout_s=15,
        )

    battery_start_text = collect_text(args.adb, device, ["dumpsys", "battery"], timeout_s=15)
    battery_start = parse_battery(battery_start_text)
    if not isinstance(battery_start.get("voltage_mv"), (int, float)):
        print("ERROR: could not read battery voltage", file=sys.stderr)
        return 5
    gpu_start = collect_text(args.adb, device, ["dumpsys", "gpu"], timeout_s=45)
    if gpu_source is None or not gpu_source.frequency_path:
        platform_label = str(gpu_probe.get("model") or gpu_probe.get("provider") or "GPU")
        warnings.append(
            f"当前系统无法读取 {platform_label} 频率，报告将使用可用的 GPU 负载、"
            "dumpsys gpu UID 活跃时长和内存快照作为回退证据。"
        )

    metadata: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": args.title
        or (
            "Multi-app Android power session"
            if args.session_mode and not target_package
            else f"{target_package} power test" if target_package else APP_NAME
        ),
        "device": device_info,
        "adb_serial": device,
        "target_package": target_package,
        "foreground_package": foreground,
        "capture_start": {
            "expected_context": args.start_context,
            "note": args.start_note,
            "host_epoch_s": datetime.now().timestamp(),
            "observed_foreground_package": foreground,
            "workflow_synchronization": "independent_start",
        },
        "session_mode": bool(args.session_mode),
        "requested_duration_s": args.duration,
        "sample_interval_s": args.interval,
        "sampling_schedule_s": {
            "current_cpu_frequency": args.interval,
            "voltage": 5.0,
            "temperature_context": 10.0,
            "active_refresh_rate": 30.0,
            "system_processes": args.process_interval,
            "hot_threads": args.thread_interval,
            "thermalservice": args.thermal_interval,
            "scheduler_adpf": args.scheduler_interval,
        },
        "system_monitor": {
            "enabled": not args.no_system_monitor,
            "process_interval_s": args.process_interval,
            "thread_interval_s": args.thread_interval,
            "thermal_interval_s": args.thermal_interval,
            "scheduler_interval_s": args.scheduler_interval,
            "priority_processes": [
                "dex2oat",
                "dexopt",
                "artd",
                "installd",
                "profman",
                "odrefresh",
                "otapreopt",
                "update_engine",
                "update_verifier",
                "apexd",
            ],
        },
        "checkpoint_interval_s": args.checkpoint_interval,
        "reconnect_timeout_s": args.reconnect_timeout,
        "current_unit": args.current_unit,
        "current_semantics": "current_ma is positive magnitude; signed_current_ma preserves direction",
        "cpu_policies": [asdict(item) for item in policies],
        "gpu_source": asdict(gpu_source) if gpu_source else None,
        "gpu_probe": gpu_probe,
        "battery_before": battery_before,
        "battery_start": battery_start,
        "collection_status": "collecting",
        "collection_warnings": warnings,
    }
    print(
        f"Recording {args.duration}s from {device}"
        + (
            " in multi-app session mode"
            if args.session_mode
            else f" for {target_package}" if target_package else ""
        )
        + "...",
        file=sys.stderr,
    )

    collection = None
    try:
        with RunJournal(output_dir) as journal:
            journal.write_metadata(metadata)
            journal.write_raw_output("battery_before", battery_before_text)
            journal.write_raw_output("battery_start", battery_start_text)
            journal.write_raw_output("gpu_start", gpu_start)
            collection = collect_streaming_session(
                args.adb,
                device,
                args.duration,
                args.interval,
                policies,
                gpu_source,
                journal,
                checkpoint_interval_s=args.checkpoint_interval,
                reconnect_timeout_s=args.reconnect_timeout,
                system_monitor_enabled=not args.no_system_monitor,
                process_interval_s=args.process_interval,
                thread_interval_s=args.thread_interval,
                thermal_interval_s=args.thermal_interval,
                scheduler_interval_s=args.scheduler_interval,
            )
            metadata["collection_stop_reason"] = collection.stop_reason
            metadata["collection_host_elapsed_s"] = collection.host_elapsed_s
            metadata["reconnect_count"] = collection.reconnect_count
            metadata["sampler_launch_count"] = collection.sampler_launch_count
            metadata["system_snapshot_count"] = collection.system_snapshot_count
            metadata["thermal_snapshot_count"] = collection.thermal_snapshot_count
            metadata["scheduler_snapshot_count"] = collection.scheduler_snapshot_count
            metadata["collection_warnings"] = warnings + collection.warnings
            metadata["collection_status"] = (
                "collected" if collection.stop_reason == "completed" else "partial"
            )
            journal.write_metadata(metadata)
            for name, value in collect_post_run_outputs(args.adb, device).items():
                journal.write_raw_output(name, value)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: collector failed: {exc}", file=sys.stderr)
        try:
            analysis, report_path = finalize_run(
                output_dir,
                [f"采集器因错误停止：{exc}"],
                "partial",
            )
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError):
            return 6
        print_run_summary(output_dir, analysis, report_path)
        return 6
    except KeyboardInterrupt:
        print("\nCollection interrupted; finalizing the recoverable portion...", file=sys.stderr)
        try:
            analysis, report_path = finalize_run(
                output_dir,
                ["采集被操作员中断，已保留可恢复的部分数据。"],
                "interrupted",
            )
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Partial data kept in {output_dir.resolve()}: {exc}", file=sys.stderr)
            return 130
        print_run_summary(output_dir, analysis, report_path)
        return 130

    if collection is None or len(collection.raw_samples) < 2:
        print(
            f"ERROR: fewer than two samples were collected; partial data is in {output_dir}",
            file=sys.stderr,
        )
        return 7
    final_status = "complete" if collection.stop_reason == "completed" else "partial"
    analysis, report_path = finalize_run(output_dir, collection_status=final_status)
    print_run_summary(output_dir, analysis, report_path)
    return 0 if final_status == "complete" else 6


def run_report(args: argparse.Namespace) -> int:
    try:
        _, report_path = finalize_run(args.run_dir)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Report regenerated: {report_path.resolve()}")
    return 0


def run_recover(args: argparse.Namespace) -> int:
    try:
        analysis, report_path = finalize_run(
            args.run_dir,
            ["本报告由中断或未完整结束的采集日志恢复生成。"],
            "recovered",
        )
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print_run_summary(args.run_dir, analysis, report_path)
    return 0


def run_import_log(args: argparse.Namespace) -> int:
    try:
        finalize_run(args.run_dir)
        metadata = load_run_metadata(args.run_dir)
        samples = read_samples_csv(args.run_dir / "samples.csv")
        sync_points = load_clock_sync(args.run_dir)
        imported, stats = import_timestamped_log(
            args.log,
            args.rules,
            sync_points,
            session_end_uptime_s=samples[-1].uptime_s,
        )
        before_filter = len(imported)
        imported, metadata_filters = filter_events_by_metadata(imported, args.match or ())
        if metadata_filters:
            stats["pre_filter_event_count"] = before_filter
            stats["event_count"] = len(imported)
            stats["metadata_filters"] = metadata_filters
        copied_log = copy_evidence_attachment(args.run_dir, args.log, "btr2")
        copied_rules = copy_evidence_attachment(args.run_dir, args.rules, "btr2")
        stats["archived_log"] = str(copied_log.relative_to(args.run_dir))
        stats["archived_rules"] = str(copied_rules.relative_to(args.run_dir))
        existing_events = load_events(args.run_dir)
        existing = (
            [item for item in existing_events if item.source == "runtime_ui"]
            if args.replace
            else existing_events
        )
        combined = existing + imported
        deduplicated: list[ExternalEvent] = []
        seen = set()
        for event in sorted(combined, key=lambda item: item.device_uptime_s):
            key = (
                round(event.device_uptime_s, 4),
                event.name,
                event.phase,
                event.kind,
                event.source,
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(event)
        write_jsonl(args.run_dir / "events.jsonl", deduplicated)
        imports = metadata.get("log_imports", [])
        if not isinstance(imports, list):
            imports = []
        imports.append(stats)
        metadata["log_imports"] = imports
        with RunJournal(args.run_dir) as journal:
            journal.write_metadata(metadata)
        _, report_path = finalize_run(args.run_dir)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError, re.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"Imported {stats['event_count']} events from {args.log} "
        f"({stats['matched_line_count']} matched lines)."
    )
    print(f"Report regenerated: {report_path.resolve()}")
    return 0


def run_archive(args: argparse.Namespace) -> int:
    try:
        archive_path, manifest = create_evidence_archive(
            args.run_dir,
            args.output,
            args.attach or (),
            force=args.force,
        )
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Evidence archive: {archive_path}")
    print(f"Archived entries: {manifest.get('entry_count', 0)}")
    return 0


def run_compare(args: argparse.Namespace) -> int:
    try:
        finalize_run(args.run_a)
        finalize_run(args.run_b)
        output = args.output or (
            args.run_a.parent
            / f"compare-{args.run_a.name}-vs-{args.run_b.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        comparison = build_run_comparison(
            args.run_a,
            args.run_b,
            args.label_a,
            args.label_b,
            args.title,
        )
        json_path, report_path = write_comparison(output, comparison)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Comparison JSON: {json_path.resolve()}")
    print(f"Comparison report: {report_path.resolve()}")
    print(f"Matched test items: {comparison.get('matched_test_item_count', 0)}")
    return 0


def build_demo_samples() -> list[Sample]:
    samples = []
    uptime = 100000.0
    for index in range(91):
        elapsed = float(index)
        voltage = 3705.0 - index * 0.18
        kworker_load = 7.0 if 10 <= index <= 30 else 0.0
        gc_load = 9.0 if 55 <= index <= 78 else 0.0
        cpu = (
            31.0
            + 6.0 * math.sin(index / 6.0)
            + (10.0 if 32 <= index <= 38 else 0.0)
            + kworker_load
            + gc_load
        )
        little_load = 35.0 + 9.0 * math.sin(index / 5.0)
        big_load = 18.0 + 12.0 * max(0.0, math.sin((index - 12) / 8.0))
        prime_load = 5.0 + (24.0 if 32 <= index <= 38 else 2.0 * math.sin(index / 9.0))
        little_freq = 1100.0 + 650.0 * (little_load / 100.0)
        big_freq = 900.0 + 1800.0 * (big_load / 100.0)
        prime_freq = 798.0 + 2700.0 * (prime_load / 100.0)
        gpu_load = 42.0 + 10.0 * math.sin(index / 8.0)
        gpu_freq = 420.0 + 480.0 * gpu_load / 100.0
        power = 1040.0 + cpu * 4.8 + gpu_load * 2.2 + 35.0 * math.sin(index / 2.7)
        power += kworker_load * 13.0 + gc_load * 15.0
        if 32 <= index <= 38:
            power += 360.0 * math.sin((index - 31) / 8.0 * math.pi)
        current = power / (voltage / 1000.0)
        core_loads = {
            "0": little_load + 4,
            "1": little_load + 2,
            "2": little_load - 2,
            "3": little_load - 4,
            "4": big_load + 5,
            "5": big_load,
            "6": max(0.0, big_load - 5),
            "7": prime_load,
        }
        samples.append(
            Sample(
                index=index,
                elapsed_s=elapsed,
                uptime_s=uptime + elapsed,
                current_ma=current,
                signed_current_ma=-current,
                voltage_mv=voltage,
                power_mw=power,
                direction="discharging",
                cpu_pct=cpu if index else None,
                core_cpu_pct=core_loads if index else {},
                cluster_cpu_pct=(
                    {"policy0": little_load, "policy4": big_load, "policy7": prime_load}
                    if index
                    else {}
                ),
                frequencies_mhz={
                    "policy0": little_freq,
                    "policy4": big_freq,
                    "policy7": prime_freq,
                },
                gpu_frequency_mhz=gpu_freq,
                gpu_load_pct=gpu_load,
                battery_temperature_c=30.6 + index * 0.003,
            )
        )
    return samples


def run_demo(args: argparse.Namespace) -> int:
    output_dir = args.output or Path("power-runs") / "android-power-demo"
    samples = build_demo_samples()
    contexts = [
        ContextSample(
            uptime_s=100000.0,
            foreground_package="tv.danmaku.bili",
            foreground_activity=".MainActivityV2",
            screen_state="Awake",
            brightness_raw=101.0,
            refresh_rate_hz=60.0,
        ),
        ContextSample(
            uptime_s=100033.0,
            foreground_package="com.android.systemui",
            foreground_activity=".statusbar.phone.CentralSurfaces",
            screen_state="Awake",
            brightness_raw=101.0,
            refresh_rate_hz=60.0,
        ),
        ContextSample(
            uptime_s=100040.0,
            foreground_package="tv.danmaku.bili",
            foreground_activity=".video.videodetail.VideoDetailsActivity",
            screen_state="Awake",
            brightness_raw=101.0,
            refresh_rate_hz=60.0,
        ),
    ]
    events = [
        ExternalEvent(
            device_uptime_s=100000.0,
            name="打开视频",
            phase="测试",
            kind="span",
            duration_s=18.0,
            source="demo log",
        ),
        ExternalEvent(
            device_uptime_s=100018.0,
            name="视频播放",
            phase="测试",
            kind="span",
            duration_s=37.0,
            source="demo log",
        ),
        ExternalEvent(
            device_uptime_s=100055.0,
            name="评论滚动",
            phase="测试",
            kind="span",
            duration_s=35.0,
            source="demo log",
        ),
        ExternalEvent(
            device_uptime_s=100035.0,
            name="拖动进度",
            phase="动作",
            kind="instant",
            source="demo log",
        ),
    ]
    clock_sync = [
        ClockSyncPoint(
            host_epoch_s=1_800_000_000.0,
            host_monotonic_s=5000.0,
            device_uptime_s=100000.0,
            round_trip_ms=12.0,
        )
    ]
    system_snapshots: list[SystemSnapshot] = []
    for snapshot_index, elapsed in enumerate(range(0, 91, 10)):
        dex_active = 30 <= elapsed <= 40
        kworker_active = 10 <= elapsed <= 30
        gc_active = elapsed in {60, 90}
        processes = [
            {
                "pid": 13646,
                "user": "u0_a287",
                "ppid": 1021,
                "policy": "fg",
                "state": "S",
                "cpu_pct": 32.0 + 4.0 * math.sin(snapshot_index),
                "mem_pct": 2.4,
                "name": "tv.danmaku.bili",
                "command": "tv.danmaku.bili",
                "category": "application",
            },
            {
                "pid": 1142,
                "user": "system",
                "ppid": 1,
                "policy": "fg",
                "state": "S",
                "cpu_pct": 12.0,
                "mem_pct": 0.7,
                "name": "surfaceflinger",
                "command": "/system/bin/surfaceflinger",
                "category": "android_system",
            },
        ]
        watched = [
            {
                "pid": 651,
                "user": "root",
                "ppid": 1,
                "name": "installd",
                "command": "/system/bin/installd",
                "category": "package_management",
                "watch_name": "installd",
                "watch_kind": "package_management",
                "watch_label": "安装包服务活动",
                "watch_impact": "应用产物维护可能占用 CPU 与存储带宽。",
                "watch_trigger": "cpu",
                "activity_active": False,
            }
        ]
        if dex_active:
            dex = {
                "pid": 22340,
                "user": "root",
                "ppid": 651,
                "policy": "bg",
                "state": "R",
                "cpu_pct": 72.0 if elapsed == 30 else 48.0,
                "mem_pct": 0.5,
                "name": "dex2oat64",
                "command": "/apex/com.android.art/bin/dex2oat64 --dex-file=/data/app/demo/base.apk",
                "category": "dex_optimization",
                "watch_name": "dex2oat",
                "watch_kind": "dex_optimization",
                "watch_label": "DEX AOT 编译",
                "watch_impact": "ART 正在系统更新后把字节码编译为本地代码。",
                "watch_trigger": "presence",
                "activity_active": True,
            }
            processes.append(dex)
            watched.append(dex)
        if kworker_active:
            processes.append(
                {
                    "pid": 88,
                    "user": "root",
                    "ppid": 2,
                    "policy": "bg",
                    "state": "R",
                    "cpu_pct": 24.0 if elapsed == 20 else 16.0,
                    "mem_pct": 0.0,
                    "name": "kworker/u25:2-ufs",
                    "command": "[kworker/u25:2-ufs]",
                    "category": "kernel",
                }
            )
        threads = []
        if elapsed % 30 == 0:
            threads.append(
                {
                    "pid": 22340 if dex_active else 13646,
                    "tid": 22340 if dex_active else 14207,
                    "user": "root" if dex_active else "u0_a287",
                    "policy": "bg" if dex_active else "fg",
                    "state": "R",
                    "cpu_pct": 68.0 if dex_active else 26.0,
                    "name": "dex2oat64" if dex_active else "ijk-worker",
                    "process": "dex2oat64" if dex_active else "tv.danmaku.bili:ijkservice",
                    "category": "dex_optimization" if dex_active else "application",
                }
            )
        if kworker_active and elapsed % 30 == 0:
            threads.append(
                {
                    "pid": 88,
                    "tid": 88,
                    "user": "root",
                    "policy": "bg",
                    "state": "R",
                    "cpu_pct": 18.0,
                    "name": "kworker/u25:2-ufs",
                    "process": "[kworker/u25:2-ufs]",
                    "category": "kernel",
                }
            )
        if gc_active:
            threads.append(
                {
                    "pid": 13646,
                    "tid": 14321,
                    "user": "u0_a287",
                    "policy": "fg",
                    "state": "R",
                    "cpu_pct": 27.0 if elapsed == 60 else 19.0,
                    "name": "HeapTaskDaemon",
                    "process": "tv.danmaku.bili",
                    "category": "application",
                }
            )
        system_snapshots.append(
            SystemSnapshot(
                uptime_s=100000.0 + elapsed,
                host_epoch_s=1_800_000_000.0 + elapsed,
                processes=processes,
                threads=threads,
                watched_processes=watched,
                process_count=532 + snapshot_index,
                thread_count=2860 + snapshot_index * 3 if elapsed % 30 == 0 else None,
                collection_ms=420.0 if elapsed % 30 else 1510.0,
            )
        )
    thermal_snapshots = [
        ThermalSnapshot(
            uptime_s=100000.0 + elapsed,
            host_epoch_s=1_800_000_000.0 + elapsed,
            status=0,
            hal_ready=True,
            temperatures=[
                {"name": "CPU", "value_c": 35.0 + elapsed * 0.035, "type": 0, "status": 0, "source": "HAL current"},
                {"name": "GPU", "value_c": 34.0 + elapsed * 0.025, "type": 1, "status": 0, "source": "HAL current"},
                {"name": "SKIN", "value_c": 31.0 + elapsed * 0.018, "type": 3, "status": 0, "source": "HAL current"},
                {"name": "BATTERY", "value_c": 30.6 + elapsed * 0.003, "type": 2, "status": 0, "source": "HAL current"},
            ],
            cooling_devices=[{"name": "lcd-backlight", "value": 0.0, "type": 6}],
            thresholds=[
                {"name": "CPU", "type": 0, "hot_c": [None, None, None, 95.0, 100.0, 110.0, 117.0], "cold_c": [None] * 7},
                {"name": "SKIN", "type": 3, "hot_c": [None, 40.0, 43.0, 45.0, 47.0, 60.0, 80.0], "cold_c": [None] * 7},
            ],
            headroom_thresholds=[None, 0.83, 0.93, 1.0, 1.07, 1.5, 2.17],
            collection_ms=75.0,
        )
        for elapsed in (0, 30, 60, 90)
    ]
    scheduler_snapshots = [
        SchedulerSnapshot(
            uptime_s=100000.0 + elapsed,
            host_epoch_s=1_800_000_000.0 + elapsed,
            cpusets={
                "foreground": "0-7",
                "background": "0-3",
                "system-background": "0-3",
                "restricted": "0-7",
            },
            cpu_policies=[
                {"name": "policy0", "governor": None, "cpuinfo_min_khz": 339000.0, "cpuinfo_max_khz": 2400000.0, "status": "limits-only"},
                {"name": "policy4", "governor": None, "cpuinfo_min_khz": 622000.0, "cpuinfo_max_khz": 3300000.0, "status": "limits-only"},
                {"name": "policy7", "governor": None, "cpuinfo_min_khz": 798000.0, "cpuinfo_max_khz": 3730000.0, "status": "limits-only"},
            ],
            hint_sessions=[
                {
                    "uid": 10287,
                    "pid": 13646,
                    "tids": [14207],
                    "target_duration_ns": 16666666,
                    "allowed_by_proc_state": True,
                    "force_paused": False,
                    "power_efficient": False,
                    "graphics_pipeline": True,
                }
            ],
            watched_processes=[
                {
                    "pid": 13646,
                    "uid": 10287,
                    "name": "tv.danmaku.bili",
                    "current_sched_group": 3,
                    "current_proc_state": 2,
                    "adj_type": "top-app",
                    "frozen": False,
                }
            ],
            power_hal=["mWakefulness=Awake", "mIsPowered=false", "mDeviceIdleMode=false"],
            availability={
                "adpf_hint_sessions": True,
                "hint_session_supported": True,
                "cpuset:top-app": "unavailable",
            },
            collection_ms=760.0,
        )
        for elapsed in (0, 30, 60, 90)
    ]
    metadata: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": "哔哩哔哩播放功耗测试",
        "device": {
            "serial": "demo-device",
            "brand": "vivo",
            "model": "V2458A",
            "soc_manufacturer": "Mediatek",
            "soc_model": "MT6991",
            "hardware": "mt6991",
            "android": "16",
            "sdk": "36",
        },
        "target_package": "tv.danmaku.bili",
        "foreground_package": "tv.danmaku.bili",
        "capture_start": {
            "expected_context": "desktop",
            "note": "演示：采集先开始，测试流程稍后进入。",
            "observed_foreground_package": "tv.danmaku.bili",
            "workflow_synchronization": "independent_start",
        },
        "requested_duration_s": 90,
        "sample_interval_s": 1.0,
        "current_unit": "ma",
        "current_semantics": "current_ma is positive magnitude; signed_current_ma preserves direction",
        "system_monitor": {
            "enabled": True,
            "process_interval_s": 10.0,
            "thread_interval_s": 30.0,
            "thermal_interval_s": 30.0,
            "scheduler_interval_s": 30.0,
        },
        "cpu_policies": [
            {
                "name": "policy0",
                "path": "demo",
                "cluster_index": 0,
                "label": "Little",
                "cores": [0, 1, 2, 3],
                "min_khz": 339000,
                "max_khz": 2400000,
                "available_frequencies_khz": [339000, 1200000, 1800000, 2400000],
            },
            {
                "name": "policy4",
                "path": "demo",
                "cluster_index": 1,
                "label": "Big",
                "cores": [4, 5, 6],
                "min_khz": 622000,
                "max_khz": 3300000,
                "available_frequencies_khz": [622000, 1600000, 2400000, 3300000],
            },
            {
                "name": "policy7",
                "path": "demo",
                "cluster_index": 2,
                "label": "Prime",
                "cores": [7],
                "min_khz": 798000,
                "max_khz": 3730000,
                "available_frequencies_khz": [798000, 1800000, 2800000, 3730000],
            },
        ],
        "gpu_source": {
            "name": "demo-mali",
            "frequency_path": "/sys/class/devfreq/demo/cur_freq",
            "load_path": "/sys/class/devfreq/demo/load",
            "minimum_mhz": 220,
            "maximum_mhz": 1200,
            "available_frequencies_mhz": [220, 420, 600, 800, 1000, 1200],
            "source_type": "sysfs",
        },
        "gpu_probe": {"frequency_available": True},
        "battery_start": {
            "level_pct": 60,
            "voltage_mv": 3705,
            "temperature_c": 30.6,
            "status": "discharging",
            "powered": [],
        },
        "battery_end": {
            "level_pct": 60,
            "voltage_mv": 3689,
            "temperature_c": 30.9,
            "status": "discharging",
            "powered": [],
        },
    }
    raw_outputs = {
        "packages": "package:tv.danmaku.bili uid:10287\npackage:com.android.shell uid:2000\n",
        "batterystats_usage": """
    Estimated power use (mAh):
      Capacity: 6200, Computed drain: 2.25, actual drain: 2.25
    Global
      screen: 0.780 apps: 0.780
      cpu: 0.520 apps: 0.420
      audio: 0.052 apps: 0 duration: 1m 30s
      wifi: 0.240 apps: 0.210
      wakelock: 0.030 apps: 0.030 duration: 9s
    UID u0a287: 0.950 fg: 1.20
      cpu=0.390 screen=0.410 wifi=0.150
    UID 1000: 0.180
      cpu=0.150 wakelock=0.030
    UID 2000: 0.030
      cpu=0.020 wifi=0.010
""",
        "batterystats": "Time on battery: 1m 30s (100.0%) realtime\nScreen on: 1m 30s (100.0%)\nKernel Wake lock WLAN Timer: 4s (30 times) realtime\n",
        "batterystats_checkin": "9,10287,l,nt,0,0,25165824,1048576,0,0,0,0,0,0\n",
        "power_profile": """
battery.capacity=6200.0
screen.on=43.0
screen.full=240.0
audio=32.0
cpu.cluster_power.cluster0=0.1
cpu.cluster_power.cluster1=0.1
cpu.cluster_power.cluster2=0.1
cpu.core_speeds.cluster0=[339000.0, 1200000.0, 1800000.0, 2400000.0]
cpu.core_power.cluster0=[9.0, 52.0, 98.0, 152.0]
cpu.core_speeds.cluster1=[622000.0, 1600000.0, 2400000.0, 3300000.0]
cpu.core_power.cluster1=[28.0, 113.0, 253.0, 520.0]
cpu.core_speeds.cluster2=[798000.0, 1800000.0, 2800000.0, 3626000.0]
cpu.core_power.cluster2=[48.0, 138.0, 437.0, 770.0]
""",
        "cpuinfo": """
  62% 13646/tv.danmaku.bili: 26% user + 36% kernel
  35% 14207/tv.danmaku.bili:ijkservice: 12% user + 23% kernel
  24% 1142/surfaceflinger: 10% user + 14% kernel
  11% 1051/android.hardware.media.c2-mediatek-64b: 3% user + 8% kernel
""",
        "gpu_start": "GPU work information.\ngpu_id uid total_active_duration_ns total_inactive_duration_ns\n0 10287 1000000000 2000000000\n0 1000 500000000 5000000000\n",
        "gpu_end": "GPU work information.\ngpu_id uid total_active_duration_ns total_inactive_duration_ns\n0 10287 33500000000 59500000000\n0 1000 2200000000 85000000000\n",
        "thermalservice": "Thermal Status: 0\nTemperature{mValue=36.3, mType=0, mName=CPU, mStatus=0}\nTemperature{mValue=30.9, mType=2, mName=BATTERY, mStatus=0}\n",
        "display": 'DisplayDeviceInfo{"Built-in": uniqueId="local:1", 1260 x 2800, modeId 3, renderFrameRate 60.0\nmActiveRenderFrameRate=60.0\n',
        "screen_brightness": "101",
        "peak_refresh_rate": "120.0",
    }
    analysis = analyze_run(
        samples,
        metadata,
        raw_outputs,
        [],
        contexts,
        events,
        system_snapshots,
        thermal_snapshots,
        scheduler_snapshots,
    )
    report_path, _ = write_run_artifacts(
        output_dir,
        metadata,
        analysis,
        samples,
        raw_outputs,
        contexts,
        clock_sync,
        events,
        system_snapshots,
        thermal_snapshots,
        scheduler_snapshots,
    )
    print_run_summary(output_dir, analysis, report_path)
    return 0


def run_ui(args: argparse.Namespace) -> int:
    from .ui import serve_dashboard

    try:
        return serve_dashboard(
            args.adb,
            args.host,
            args.port,
            args.output_root,
            open_browser=not args.no_browser,
            demo_mode=args.demo,
            ios_python=args.ios_python,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def float_at_least_two(value: str) -> float:
    parsed = positive_float(value)
    if parsed < 2.0:
        raise argparse.ArgumentTypeError("must be at least 2 seconds")
    return parsed


def float_at_least_five(value: str) -> float:
    parsed = positive_float(value)
    if parsed < 5.0:
        raise argparse.ArgumentTypeError("must be at least 5 seconds")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect mobile-device power telemetry and generate an interactive analysis report."
    )
    parser.add_argument("--adb", default=DEFAULT_ADB, help="adb executable path")
    parser.add_argument(
        "--ios-python",
        default=DEFAULT_IOS_PYTHON,
        help="Python interpreter containing pymobiledevice3 for the optional iOS sidecar",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="show device power collection capabilities")
    probe.add_argument("--platform", choices=("auto", "android", "ios"), default="auto")
    probe.add_argument("--device", help="device identifier; iPhones use ios:UDID")
    probe.add_argument("--gpu-frequency-path", help="override readable GPU frequency sysfs path")
    probe.add_argument("--json", action="store_true", help="print JSON")
    probe.set_defaults(handler=run_probe)

    record = subparsers.add_parser("record", help="record a test and generate a report")
    record.add_argument("--platform", choices=("auto", "android", "ios"), default="auto")
    record.add_argument("--device", help="device identifier; iPhones use ios:UDID")
    record.add_argument(
        "--duration", type=positive_int, default=DEFAULT_DURATION_S, help="test duration in seconds"
    )
    record.add_argument(
        "--interval", type=positive_float, default=DEFAULT_INTERVAL_S, help="sample interval in seconds"
    )
    record.add_argument("--package", help="target package; defaults to foreground outside session mode")
    record.add_argument(
        "--start-context",
        choices=("desktop", "app", "other", "unknown"),
        default="desktop",
        help="expected scene when capture starts; stored as audit metadata",
    )
    record.add_argument(
        "--start-note",
        default="",
        help="free-form note describing the capture start or delayed workflow launch",
    )
    record.add_argument(
        "--session-mode",
        action="store_true",
        help="track foreground app changes instead of assuming one target app",
    )
    record.add_argument("--output", type=Path, help="output directory")
    record.add_argument("--title", help="report title")
    record.add_argument("--current-unit", choices=("auto", "ma", "ua"), default="auto")
    record.add_argument("--gpu-frequency-path", help="override readable GPU frequency sysfs path")
    record.add_argument("--no-reset", action="store_true", help="do not reset BatteryStats")
    record.add_argument("--full-history", action="store_true", help="enable detailed BatteryStats history")
    record.add_argument("--require-unplugged", action="store_true", help="fail when external power is connected")
    record.add_argument(
        "--no-system-monitor",
        action="store_true",
        help="disable periodic process, thread, thermal and scheduler snapshots",
    )
    record.add_argument(
        "--process-interval",
        type=float_at_least_two,
        default=10.0,
        help="whole-system process snapshot interval in seconds",
    )
    record.add_argument(
        "--thread-interval",
        type=float_at_least_five,
        default=30.0,
        help="hot-thread snapshot interval in seconds",
    )
    record.add_argument(
        "--thermal-interval",
        type=float_at_least_two,
        default=10.0,
        help="ThermalService snapshot interval in seconds",
    )
    record.add_argument(
        "--scheduler-interval",
        type=float_at_least_five,
        default=30.0,
        help="cpuset, ActivityManager and ADPF snapshot interval in seconds",
    )
    record.add_argument(
        "--checkpoint-interval",
        type=positive_float,
        default=30.0,
        help="checkpoint and clock-sync interval in seconds",
    )
    record.add_argument(
        "--reconnect-timeout",
        type=positive_float,
        default=120.0,
        help="maximum time to wait for device reconnection",
    )
    record.set_defaults(handler=run_record)

    ios_pair = subparsers.add_parser(
        "ios-pair",
        help="create iOS RemotePairing over trusted USB and cache the Wi-Fi endpoint",
    )
    ios_pair.add_argument("--device", help="iPhone UDID or ios:UDID; defaults to the only USB iPhone")
    ios_pair.add_argument("--timeout", type=positive_float, default=12.0)
    ios_pair.add_argument("--json", action="store_true")
    ios_pair.set_defaults(handler=run_ios_pair)

    report = subparsers.add_parser("report", help="regenerate and migrate an existing run report")
    report.add_argument("run_dir", type=Path)
    report.set_defaults(handler=run_report)

    recover = subparsers.add_parser("recover", help="finalize an interrupted run journal")
    recover.add_argument("run_dir", type=Path)
    recover.set_defaults(handler=run_recover)

    import_log = subparsers.add_parser(
        "import-log",
        help="align a timestamped external log and add phase/action events",
    )
    import_log.add_argument("run_dir", type=Path)
    import_log.add_argument("log", type=Path)
    import_log.add_argument("--rules", required=True, type=Path, help="JSON regex rule file")
    import_log.add_argument(
        "--replace",
        action="store_true",
        help="replace existing imported events instead of appending",
    )
    import_log.add_argument(
        "--match",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="keep only events whose regex metadata matches; useful for one phone in a combined BTR2 log",
    )
    import_log.set_defaults(handler=run_import_log)

    archive = subparsers.add_parser(
        "archive",
        help="package a complete run directory and optional logs into a hashed evidence ZIP",
    )
    archive.add_argument("run_dir", type=Path, help="completed run directory")
    archive.add_argument("--output", type=Path, help="output ZIP path")
    archive.add_argument(
        "--attach",
        type=Path,
        action="append",
        default=[],
        help="additional file or directory to include; may be repeated",
    )
    archive.add_argument("--force", action="store_true", help="overwrite an existing ZIP")
    archive.set_defaults(handler=run_archive)

    compare = subparsers.add_parser(
        "compare",
        help="compare two completed phone runs and generate a Chinese HTML report",
    )
    compare.add_argument("run_a", type=Path, help="baseline or phone A run directory")
    compare.add_argument("run_b", type=Path, help="phone B run directory")
    compare.add_argument("--label-a", default="", help="display label for phone/run A")
    compare.add_argument("--label-b", default="", help="display label for phone/run B")
    compare.add_argument("--title", default="双机续航与系统活动对比", help="comparison title")
    compare.add_argument("--output", type=Path, help="output directory")
    compare.set_defaults(handler=run_compare)

    demo = subparsers.add_parser("demo", help="generate a report from built-in demonstration data")
    demo.add_argument("--output", type=Path, help="output directory")
    demo.set_defaults(handler=run_demo)

    ui = subparsers.add_parser("ui", help="launch the local runtime dashboard")
    ui.add_argument("--host", default="127.0.0.1", help="dashboard bind address")
    ui.add_argument("--port", type=int, default=8765, help="dashboard port; use 0 for any free port")
    ui.add_argument(
        "--output-root",
        type=Path,
        default=Path("power-runs"),
        help="root directory for UI-created runs",
    )
    ui.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    ui.add_argument("--demo", action="store_true", help="show synthetic telemetry until a real run starts")
    ui.set_defaults(handler=run_ui)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))
