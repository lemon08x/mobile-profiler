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
from .collector import (
    adb_shell,
    collect_cpu_policies,
    collect_streaming_session,
    collect_device_info,
    collect_foreground_package,
    collect_post_run_outputs,
    collect_text,
    detect_gpu_source,
    parse_context_samples,
    parse_raw_samples,
    select_device,
)
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
)
from .parsers import first_number, parse_battery
from .storage import (
    RunJournal,
    load_clock_sync,
    load_checkpoint,
    load_contexts,
    load_events,
    load_run_metadata,
    load_raw_outputs,
    read_samples_csv,
    write_jsonl,
    write_run_artifacts,
)


def default_output_dir() -> Path:
    return Path("power-runs") / datetime.now().strftime("android-power-%Y%m%d-%H%M%S")


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
    parsed_raw = _normalize_raw_samples(parse_raw_samples(sampler_text, policies, gpu_source))

    samples_path = output_dir / "samples.csv"
    conversion_warnings: list[str] = []
    battery_start = metadata.get("battery_start", {})
    battery_end = parse_battery(raw_outputs.get("battery_end", ""))
    if not battery_end:
        battery_end = dict(battery_start) if isinstance(battery_start, dict) else {}
        conversion_warnings.append(
            "End battery state was unavailable; start voltage and temperature were used as fallbacks."
        )
    metadata["battery_end"] = battery_end

    if len(parsed_raw) >= 2:
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
    analysis = analyze_run(samples, metadata, raw_outputs, warnings, contexts, events)
    report_path, _ = write_run_artifacts(
        output_dir,
        metadata,
        analysis,
        samples,
        raw_outputs,
        contexts,
        clock_sync,
        events,
    )
    with RunJournal(output_dir) as journal:
        journal.checkpoint(
            {
                "status": "complete",
                "sample_count": len(samples),
                "context_count": len(contexts),
                "clock_sync_count": len(clock_sync),
                "event_count": len(events),
                "last_device_uptime_s": samples[-1].uptime_s,
                "stop_reason": metadata.get("collection_stop_reason", "completed"),
            }
        )
    return analysis, report_path


def run_probe(args: argparse.Namespace) -> int:
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
    info = {
        "device": collect_device_info(args.adb, device),
        "battery": parse_battery(battery_text),
        "current_command": current_result.stdout.strip(),
        "current_command_ok": current_result.ok,
        "cpu_policies": [asdict(item) for item in collect_cpu_policies(args.adb, device)],
        "gpu_source": asdict(gpu_source) if gpu_source else None,
        "gpu_probe": gpu_probe,
        "gpu_work_duration_available": "GPU work information" in gpu_dump.stdout,
        "perfetto_android_power": "android.power" in perfetto.stdout,
        "perfetto_sysfs_power": "linux.sysfs_power" in perfetto.stdout,
        "powerstats_dump_available": bool(powerstats.stdout.strip()),
        "foreground_package": collect_foreground_package(args.adb, device),
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
    print(
        "CPU policies: "
        + (
            ", ".join(
                f"{item['label']}={item['name']} cores {item['cores']} max {float(item.get('max_khz') or 0) / 1000:.0f} MHz"
                for item in info["cpu_policies"]
            )
            or "none"
        )
    )
    if info["gpu_source"]:
        print(f"GPU frequency: readable from {info['gpu_source']['frequency_path']}")
    else:
        print(f"GPU frequency: unavailable ({gpu_probe.get('reason')})")
    print(
        "GPU hardware counter source: "
        f"{gpu_probe.get('perfetto_hardware_counter_source_available', False)} "
        f"(profiler support property={gpu_probe.get('graphics_gpu_profiler_support', False)})"
    )
    print(f"GPU UID work duration: {info['gpu_work_duration_available']}")
    print(f"Foreground package: {info['foreground_package'] or 'unknown'}")
    return 0


def run_record(args: argparse.Namespace) -> int:
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
            "The device was externally powered. Fuel-gauge current is net battery flow, not total device input power."
        )
        if args.require_unplugged:
            print("ERROR: device is powered; unplug it or omit --require-unplugged", file=sys.stderr)
            return 4

    if not args.no_reset:
        reset = adb_shell(args.adb, device, ["dumpsys", "batterystats", "--reset"], timeout_s=20)
        if not reset.ok:
            warnings.append("BatteryStats reset failed; attribution may include activity from before the run.")
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
    if gpu_source is None:
        warnings.append(
            "GPU frequency was not readable on this build; the report uses dumpsys gpu UID active durations instead."
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
        "session_mode": bool(args.session_mode),
        "requested_duration_s": args.duration,
        "sample_interval_s": args.interval,
        "sampling_schedule_s": {
            "current_cpu_frequency": args.interval,
            "voltage": 5.0,
            "temperature_context": 10.0,
            "active_refresh_rate": 30.0,
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
            )
            metadata["collection_stop_reason"] = collection.stop_reason
            metadata["collection_host_elapsed_s"] = collection.host_elapsed_s
            metadata["reconnect_count"] = collection.reconnect_count
            metadata["sampler_launch_count"] = collection.sampler_launch_count
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
                [f"Collector stopped with an error: {exc}"],
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
                ["Collection was interrupted by the operator; the partial run was preserved."],
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
            ["This report was finalized from an interrupted or incomplete run journal."],
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
        existing = [] if args.replace else load_events(args.run_dir)
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


def build_demo_samples() -> list[Sample]:
    samples = []
    uptime = 100000.0
    for index in range(91):
        elapsed = float(index)
        voltage = 3705.0 - index * 0.18
        cpu = 31.0 + 6.0 * math.sin(index / 6.0) + (10.0 if 32 <= index <= 38 else 0.0)
        little_load = 35.0 + 9.0 * math.sin(index / 5.0)
        big_load = 18.0 + 12.0 * max(0.0, math.sin((index - 12) / 8.0))
        prime_load = 5.0 + (24.0 if 32 <= index <= 38 else 2.0 * math.sin(index / 9.0))
        little_freq = 1100.0 + 650.0 * (little_load / 100.0)
        big_freq = 900.0 + 1800.0 * (big_load / 100.0)
        prime_freq = 798.0 + 2700.0 * (prime_load / 100.0)
        gpu_load = 42.0 + 10.0 * math.sin(index / 8.0)
        gpu_freq = 420.0 + 480.0 * gpu_load / 100.0
        power = 1040.0 + cpu * 4.8 + gpu_load * 2.2 + 35.0 * math.sin(index / 2.7)
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
            name="Open video",
            phase="test",
            kind="span",
            duration_s=18.0,
            source="demo log",
        ),
        ExternalEvent(
            device_uptime_s=100018.0,
            name="Playback",
            phase="test",
            kind="span",
            duration_s=72.0,
            source="demo log",
        ),
        ExternalEvent(
            device_uptime_s=100035.0,
            name="Seek",
            phase="action",
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
    metadata: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": "Bilibili playback power test",
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
        "requested_duration_s": 90,
        "sample_interval_s": 1.0,
        "current_unit": "ma",
        "current_semantics": "current_ma is positive magnitude; signed_current_ma preserves direction",
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
    analysis = analyze_run(samples, metadata, raw_outputs, [], contexts, events)
    report_path, _ = write_run_artifacts(
        output_dir,
        metadata,
        analysis,
        samples,
        raw_outputs,
        contexts,
        clock_sync,
        events,
    )
    print_run_summary(output_dir, analysis, report_path)
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect Android power telemetry and generate an interactive analysis report."
    )
    parser.add_argument("--adb", default=DEFAULT_ADB, help="adb executable path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="show device power collection capabilities")
    probe.add_argument("--device", help="ADB serial")
    probe.add_argument("--gpu-frequency-path", help="override readable GPU frequency sysfs path")
    probe.add_argument("--json", action="store_true", help="print JSON")
    probe.set_defaults(handler=run_probe)

    record = subparsers.add_parser("record", help="record a test and generate a report")
    record.add_argument("--device", help="ADB serial")
    record.add_argument(
        "--duration", type=positive_int, default=DEFAULT_DURATION_S, help="test duration in seconds"
    )
    record.add_argument(
        "--interval", type=positive_float, default=DEFAULT_INTERVAL_S, help="sample interval in seconds"
    )
    record.add_argument("--package", help="target package; defaults to foreground outside session mode")
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
        "--checkpoint-interval",
        type=positive_float,
        default=30.0,
        help="checkpoint and clock-sync interval in seconds",
    )
    record.add_argument(
        "--reconnect-timeout",
        type=positive_float,
        default=120.0,
        help="maximum time to wait for ADB reconnection",
    )
    record.set_defaults(handler=run_record)

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
    import_log.set_defaults(handler=run_import_log)

    demo = subparsers.add_parser("demo", help="generate a report from built-in demonstration data")
    demo.add_argument("--output", type=Path, help="output directory")
    demo.set_defaults(handler=run_demo)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))
