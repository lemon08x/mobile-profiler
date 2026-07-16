from __future__ import annotations

import csv
import json
import math
import re
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, TextIO, Tuple, Type, TypeVar

from .models import (
    ClockSyncPoint,
    ContextSample,
    ExternalEvent,
    SCHEMA_VERSION,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
)


T = TypeVar("T")
REPORT_EDITS_FILENAME = "report-edits.json"
REPORT_SOURCE_SAMPLES_FILENAME = "report-source-samples.csv"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def normalize_report_excluded_ranges(value: object) -> List[Tuple[float, float]]:
    items = value if isinstance(value, list) else []
    ranges: List[Tuple[float, float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_uptime_s"))
            end = float(item.get("end_uptime_s"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        ranges.append((start, end))
    merged: List[Tuple[float, float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def load_report_excluded_ranges(output_dir: Path) -> List[Tuple[float, float]]:
    path = output_dir / REPORT_EDITS_FILENAME
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    return normalize_report_excluded_ranges(payload.get("excluded_ranges"))


def write_report_excluded_ranges(
    output_dir: Path,
    ranges: Sequence[Tuple[float, float]],
) -> Dict[str, object]:
    normalized = normalize_report_excluded_ranges(
        [
            {"start_uptime_s": start, "end_uptime_s": end}
            for start, end in ranges
        ]
    )
    payload: Dict[str, object] = {
        "schema_version": 1,
        "updated_at": _utc_now(),
        "excluded_ranges": [
            {"start_uptime_s": start, "end_uptime_s": end}
            for start, end in normalized
        ],
    }
    _write_json_atomic(output_dir / REPORT_EDITS_FILENAME, payload)
    return payload


def uptime_in_report_excluded_ranges(
    uptime_s: object,
    ranges: Sequence[Tuple[float, float]],
) -> bool:
    try:
        value = float(uptime_s)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    return any(start <= value <= end for start, end in ranges)


def write_jsonl(path: Path, values: Iterable[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            payload = asdict(value) if hasattr(value, "__dataclass_fields__") else value
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def read_jsonl(path: Path, model: Type[T], strict: bool = False) -> List[T]:
    if not path.exists():
        return []
    values: List[T] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            values.append(model(**payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if strict:
                raise RuntimeError(f"invalid {path.name} line {line_number}: {exc}") from exc
    return values


def load_contexts(output_dir: Path) -> List[ContextSample]:
    return read_jsonl(output_dir / "contexts.jsonl", ContextSample)


def load_clock_sync(output_dir: Path) -> List[ClockSyncPoint]:
    return read_jsonl(output_dir / "clock-sync.jsonl", ClockSyncPoint)


def load_events(output_dir: Path) -> List[ExternalEvent]:
    return read_jsonl(output_dir / "events.jsonl", ExternalEvent)


def load_system_snapshots(output_dir: Path) -> List[SystemSnapshot]:
    return read_jsonl(output_dir / "system-snapshots.jsonl", SystemSnapshot)


def load_thermal_snapshots(output_dir: Path) -> List[ThermalSnapshot]:
    return read_jsonl(output_dir / "thermal-snapshots.jsonl", ThermalSnapshot)


def load_scheduler_snapshots(output_dir: Path) -> List[SchedulerSnapshot]:
    return read_jsonl(output_dir / "scheduler-snapshots.jsonl", SchedulerSnapshot)


class RunJournal:
    """Append-only run journal used while a long ADB session is active."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.raw_dir = output_dir / "raw"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._sampler: Optional[TextIO] = None
        self._stderr: Optional[TextIO] = None
        self._contexts: Optional[TextIO] = None
        self._clock_sync: Optional[TextIO] = None
        self._system_snapshots: Optional[TextIO] = None
        self._thermal_snapshots: Optional[TextIO] = None
        self._scheduler_snapshots: Optional[TextIO] = None
        self._lock = threading.RLock()

    def __enter__(self) -> "RunJournal":
        with self._lock:
            self._sampler = (self.raw_dir / "sampler-stream.txt").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._stderr = (self.raw_dir / "sampler-stderr.txt").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._contexts = (self.output_dir / "contexts.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._clock_sync = (self.output_dir / "clock-sync.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._system_snapshots = (self.output_dir / "system-snapshots.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._thermal_snapshots = (self.output_dir / "thermal-snapshots.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._scheduler_snapshots = (
                self.output_dir / "scheduler-snapshots.jsonl"
            ).open("a", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        with self._lock:
            for handle in (
                self._sampler,
                self._stderr,
                self._contexts,
                self._clock_sync,
                self._system_snapshots,
                self._thermal_snapshots,
                self._scheduler_snapshots,
            ):
                if handle is not None:
                    handle.flush()
                    handle.close()

    def _append_line(self, handle: Optional[TextIO], value: str) -> None:
        with self._lock:
            if handle is None:
                raise RuntimeError("run journal is not open")
            handle.write(value.rstrip("\r\n"))
            handle.write("\n")
            handle.flush()

    def append_sampler_line(self, line: str) -> None:
        self._append_line(self._sampler, line)

    def append_stderr_line(self, line: str) -> None:
        self._append_line(self._stderr, line)

    def append_context(self, context: ContextSample) -> None:
        self._append_line(
            self._contexts,
            json.dumps(asdict(context), ensure_ascii=False, separators=(",", ":")),
        )

    def append_clock_sync(self, point: ClockSyncPoint) -> None:
        self._append_line(
            self._clock_sync,
            json.dumps(asdict(point), ensure_ascii=False, separators=(",", ":")),
        )

    def append_system_snapshot(self, snapshot: SystemSnapshot) -> None:
        self._append_line(
            self._system_snapshots,
            json.dumps(asdict(snapshot), ensure_ascii=False, separators=(",", ":")),
        )

    def append_thermal_snapshot(self, snapshot: ThermalSnapshot) -> None:
        self._append_line(
            self._thermal_snapshots,
            json.dumps(asdict(snapshot), ensure_ascii=False, separators=(",", ":")),
        )

    def append_scheduler_snapshot(self, snapshot: SchedulerSnapshot) -> None:
        self._append_line(
            self._scheduler_snapshots,
            json.dumps(asdict(snapshot), ensure_ascii=False, separators=(",", ":")),
        )

    def write_metadata(self, metadata: Dict[str, object]) -> None:
        with self._lock:
            _write_json_atomic(self.output_dir / "metadata.json", metadata)

    def write_raw_output(self, name: str, value: str) -> None:
        with self._lock:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            (self.raw_dir / f"{safe_name}.txt").write_text(
                value, encoding="utf-8", errors="replace"
            )

    def checkpoint(self, state: Dict[str, object]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now(),
            **state,
        }
        with self._lock:
            _write_json_atomic(self.output_dir / "checkpoint.json", payload)


def load_checkpoint(output_dir: Path) -> Dict[str, object]:
    path = output_dir / "checkpoint.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def sample_to_dict(sample: Sample) -> Dict[str, object]:
    return {
        "index": sample.index,
        "elapsed_s": round(sample.elapsed_s, 4),
        "uptime_s": round(sample.uptime_s, 4),
        "current_ma": round(sample.current_ma, 4),
        "signed_current_ma": round(sample.signed_current_ma, 4),
        "voltage_mv": round(sample.voltage_mv, 4),
        "power_mw": round(sample.power_mw, 4),
        "direction": sample.direction,
        "cpu_pct": round(sample.cpu_pct, 4) if sample.cpu_pct is not None else None,
        "core_cpu_pct": {name: round(value, 4) for name, value in sample.core_cpu_pct.items()},
        "cluster_cpu_pct": {
            name: round(value, 4) for name, value in sample.cluster_cpu_pct.items()
        },
        "frequencies_mhz": {
            name: round(value, 4) for name, value in sample.frequencies_mhz.items()
        },
        "gpu_frequency_mhz": (
            round(sample.gpu_frequency_mhz, 4) if sample.gpu_frequency_mhz is not None else None
        ),
        "gpu_load_pct": round(sample.gpu_load_pct, 4) if sample.gpu_load_pct is not None else None,
        "memory_frequency_mhz": (
            round(sample.memory_frequency_mhz, 4)
            if sample.memory_frequency_mhz is not None
            else None
        ),
        "battery_temperature_c": (
            round(sample.battery_temperature_c, 4)
            if sample.battery_temperature_c is not None
            else None
        ),
        "power_source": sample.power_source,
        "power_sample_age_s": (
            round(sample.power_sample_age_s, 4)
            if sample.power_sample_age_s is not None
            else None
        ),
        "collector_cpu_pct": (
            round(sample.collector_cpu_pct, 4)
            if sample.collector_cpu_pct is not None
            else None
        ),
    }


def write_samples_csv(path: Path, samples: Sequence[Sample], metadata: Dict[str, object]) -> None:
    policy_names = [
        str(item.get("name"))
        for item in metadata.get("cpu_policies", [])
        if isinstance(item, dict) and item.get("name")
    ]
    core_names = sorted(
        {name for sample in samples for name in sample.core_cpu_pct},
        key=lambda item: int(item),
    )
    fields = [
        "index",
        "elapsed_s",
        "uptime_s",
        "current_ma",
        "signed_current_ma",
        "voltage_mv",
        "power_mw",
        "direction",
        "cpu_pct",
        "battery_temperature_c",
        "gpu_frequency_mhz",
        "gpu_load_pct",
        "memory_frequency_mhz",
        "power_source",
        "power_sample_age_s",
        "collector_cpu_pct",
    ]
    fields.extend(f"core{core}_pct" for core in core_names)
    fields.extend(f"{name}_load_pct" for name in policy_names)
    fields.extend(f"{name}_mhz" for name in policy_names)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for sample in samples:
            row: Dict[str, object] = {
                "index": sample.index,
                "elapsed_s": f"{sample.elapsed_s:.4f}",
                "uptime_s": f"{sample.uptime_s:.4f}",
                "current_ma": f"{sample.current_ma:.4f}",
                "signed_current_ma": f"{sample.signed_current_ma:.4f}",
                "voltage_mv": f"{sample.voltage_mv:.4f}",
                "power_mw": f"{sample.power_mw:.4f}",
                "direction": sample.direction,
                "cpu_pct": "" if sample.cpu_pct is None else f"{sample.cpu_pct:.4f}",
                "battery_temperature_c": (
                    ""
                    if sample.battery_temperature_c is None
                    else f"{sample.battery_temperature_c:.4f}"
                ),
                "gpu_frequency_mhz": (
                    "" if sample.gpu_frequency_mhz is None else f"{sample.gpu_frequency_mhz:.4f}"
                ),
                "gpu_load_pct": "" if sample.gpu_load_pct is None else f"{sample.gpu_load_pct:.4f}",
                "memory_frequency_mhz": (
                    ""
                    if sample.memory_frequency_mhz is None
                    else f"{sample.memory_frequency_mhz:.4f}"
                ),
                "power_source": sample.power_source,
                "power_sample_age_s": (
                    "" if sample.power_sample_age_s is None else f"{sample.power_sample_age_s:.4f}"
                ),
                "collector_cpu_pct": (
                    "" if sample.collector_cpu_pct is None else f"{sample.collector_cpu_pct:.4f}"
                ),
            }
            row.update(
                {
                    f"core{core}_pct": (
                        f"{sample.core_cpu_pct[core]:.4f}" if core in sample.core_cpu_pct else ""
                    )
                    for core in core_names
                }
            )
            row.update(
                {
                    f"{name}_load_pct": (
                        f"{sample.cluster_cpu_pct[name]:.4f}"
                        if name in sample.cluster_cpu_pct
                        else ""
                    )
                    for name in policy_names
                }
            )
            row.update(
                {
                    f"{name}_mhz": f"{sample.frequencies_mhz.get(name, 0.0):.4f}"
                    for name in policy_names
                }
            )
            writer.writerow(row)


def _optional_float(row: Dict[str, str], key: str) -> Optional[float]:
    value = row.get(key)
    return float(value) if value not in (None, "") else None


def read_samples_csv(path: Path) -> List[Sample]:
    samples: List[Sample] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        frequency_fields = [
            name
            for name in fieldnames
            if name.endswith("_mhz")
            and name not in {"gpu_frequency_mhz", "memory_frequency_mhz"}
            and not name.startswith("signed_")
        ]
        cluster_fields = [name for name in fieldnames if name.endswith("_load_pct")]
        core_fields = [name for name in fieldnames if re.fullmatch(r"core\d+_pct", name)]
        for row in reader:
            stored_current = float(row["current_ma"])
            signed = _optional_float(row, "signed_current_ma")
            if signed is None:
                signed = stored_current
            current = abs(stored_current)
            direction = row.get("direction") or (
                "discharging" if signed < 0 else "charging" if signed > 0 else "idle"
            )
            samples.append(
                Sample(
                    index=int(row["index"]),
                    elapsed_s=float(row["elapsed_s"]),
                    uptime_s=float(row["uptime_s"]),
                    current_ma=current,
                    signed_current_ma=signed,
                    voltage_mv=float(row["voltage_mv"]),
                    power_mw=float(row["power_mw"]),
                    direction=direction,
                    cpu_pct=_optional_float(row, "cpu_pct"),
                    core_cpu_pct={
                        field[len("core") : -len("_pct")]: float(row[field])
                        for field in core_fields
                        if row.get(field)
                    },
                    cluster_cpu_pct={
                        field[: -len("_load_pct")]: float(row[field])
                        for field in cluster_fields
                        if row.get(field)
                    },
                    frequencies_mhz={
                        field[: -len("_mhz")]: float(row[field])
                        for field in frequency_fields
                        if row.get(field)
                    },
                    gpu_frequency_mhz=_optional_float(row, "gpu_frequency_mhz"),
                    gpu_load_pct=_optional_float(row, "gpu_load_pct"),
                    memory_frequency_mhz=_optional_float(row, "memory_frequency_mhz"),
                    battery_temperature_c=_optional_float(row, "battery_temperature_c"),
                    power_source=row.get("power_source") or "battery_current_voltage",
                    power_sample_age_s=_optional_float(row, "power_sample_age_s"),
                    collector_cpu_pct=_optional_float(row, "collector_cpu_pct"),
                )
            )
    return samples


def load_raw_outputs(output_dir: Path) -> Dict[str, str]:
    raw_dir = output_dir / "raw"
    if not raw_dir.exists():
        return {}
    return {path.stem: path.read_text(encoding="utf-8", errors="replace") for path in raw_dir.glob("*.txt")}


def write_run_artifacts(
    output_dir: Path,
    metadata: Dict[str, object],
    analysis: Dict[str, object],
    samples: Sequence[Sample],
    raw_outputs: Optional[Dict[str, str]] = None,
    contexts: Optional[Sequence[ContextSample]] = None,
    clock_sync: Optional[Sequence[ClockSyncPoint]] = None,
    events: Optional[Sequence[ExternalEvent]] = None,
    system_snapshots: Optional[Sequence[SystemSnapshot]] = None,
    thermal_snapshots: Optional[Sequence[ThermalSnapshot]] = None,
    scheduler_snapshots: Optional[Sequence[SchedulerSnapshot]] = None,
    persist_observation_streams: bool = True,
) -> Tuple[Path, Path]:
    from .report import write_report_files

    output_dir.mkdir(parents=True, exist_ok=True)
    write_samples_csv(output_dir / "samples.csv", samples, metadata)
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if persist_observation_streams and contexts is not None:
        write_jsonl(output_dir / "contexts.jsonl", contexts)
    if persist_observation_streams and clock_sync is not None:
        write_jsonl(output_dir / "clock-sync.jsonl", clock_sync)
    if persist_observation_streams and events is not None:
        write_jsonl(output_dir / "events.jsonl", events)
    if persist_observation_streams and system_snapshots is not None:
        write_jsonl(output_dir / "system-snapshots.jsonl", system_snapshots)
    if persist_observation_streams and thermal_snapshots is not None:
        write_jsonl(output_dir / "thermal-snapshots.jsonl", thermal_snapshots)
    if persist_observation_streams and scheduler_snapshots is not None:
        write_jsonl(output_dir / "scheduler-snapshots.jsonl", scheduler_snapshots)
    if persist_observation_streams and raw_outputs:
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(exist_ok=True)
        for key, value in raw_outputs.items():
            safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
            (raw_dir / f"{safe_key}.txt").write_text(
                value, encoding="utf-8", errors="replace"
            )
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "metadata": metadata,
        "analysis": analysis,
        "samples": [sample_to_dict(sample) for sample in samples],
        "contexts": [asdict(item) for item in (contexts or [])],
        "clock_sync": [asdict(item) for item in (clock_sync or [])],
        "events": [asdict(item) for item in (events or [])],
        "system_snapshots": [asdict(item) for item in (system_snapshots or [])],
        "thermal_snapshots": [asdict(item) for item in (thermal_snapshots or [])],
        "scheduler_snapshots": [asdict(item) for item in (scheduler_snapshots or [])],
    }
    return write_report_files(output_dir, bundle)


def load_existing_run(output_dir: Path) -> Tuple[Dict[str, object], Dict[str, object], List[Sample]]:
    metadata_path = output_dir / "metadata.json"
    analysis_path = output_dir / "analysis.json"
    samples_path = output_dir / "samples.csv"
    missing = [path.name for path in (metadata_path, analysis_path, samples_path) if not path.exists()]
    if missing:
        raise RuntimeError(f"run directory is missing: {', '.join(missing)}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    samples = read_samples_csv(samples_path)
    return metadata, analysis, samples


def load_run_metadata(output_dir: Path) -> Dict[str, object]:
    path = output_dir / "metadata.json"
    if not path.exists():
        raise RuntimeError("run directory is missing metadata.json")
    return json.loads(path.read_text(encoding="utf-8"))
