from __future__ import annotations

import ipaddress
import json
import math
import os
import re
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
from typing import Dict, List, Optional, Sequence
from urllib.parse import quote, unquote, urlparse

from .analysis import analyze_performance_contexts, convert_samples
from .collector import adb_connection_type, adb_shell, list_adb_devices, parse_sampler_line
from .evidence import create_evidence_archive
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
    RawSample,
    Sample,
    SchedulerSnapshot,
    SystemSnapshot,
    ThermalSnapshot,
)


MAX_LIVE_POINTS = 900
MAX_LOG_LINES = 500
MAX_REQUEST_BYTES = 1_000_000
DEFAULT_UI_DURATION_S = 62 * 60


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
        return datetime.now().strftime("android-power-%Y%m%d-%H%M%S")
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
        return datetime.now().strftime("android-power-%Y%m%d-%H%M%S")
    return result[:96]


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


def _decimate(samples: Sequence[Sample], limit: int = MAX_LIVE_POINTS) -> List[Sample]:
    if len(samples) <= limit:
        return list(samples)
    if limit <= 2:
        return [samples[0], samples[-1]]
    step = (len(samples) - 1) / float(limit - 1)
    indexes = sorted({min(len(samples) - 1, round(index * step)) for index in range(limit)})
    return [samples[index] for index in indexes]


def _integrate_energy(samples: Sequence[Sample], max_gap_s: float) -> float:
    energy_mwh = 0.0
    for previous, current in zip(samples, samples[1:]):
        delta = current.uptime_s - previous.uptime_s
        if delta <= 0 or delta > max_gap_s:
            continue
        energy_mwh += (previous.power_mw + current.power_mw) * 0.5 * delta / 3600.0
    return energy_mwh


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class LiveTelemetryReader:
    """Incrementally parse an append-only run journal for the dashboard."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.metadata: Dict[str, object] = {}
        self.policies: List[CpuPolicy] = []
        self.gpu_source: Optional[GpuSource] = None
        self.raw_samples: List[RawSample] = []
        self.normalized_samples: List[Sample] = []
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
            self.contexts.clear()
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
            parsed = parse_sampler_line(line, self.policies, self.gpu_source)
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
                if self.contexts and parsed.uptime_s <= self.contexts[-1].uptime_s:
                    continue
                self.contexts.append(parsed)

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
        powers = [sample.power_mw for sample in samples]
        currents = [sample.current_ma for sample in samples]
        cpu_values = [sample.cpu_pct for sample in samples if sample.cpu_pct is not None]

        latest_context: Optional[ContextSample] = None
        if latest is not None:
            for context in self.contexts:
                if context.uptime_s > latest.uptime_s:
                    break
                latest_context = context
        elif self.contexts:
            latest_context = self.contexts[-1]

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
        warnings = list(conversion_warnings)
        raw_warnings = self.metadata.get("collection_warnings", [])
        if isinstance(raw_warnings, list):
            warnings.extend(str(value) for value in raw_warnings)

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
        performance = analyze_performance_contexts(self.contexts, self.metadata)

        return {
            "metadata": self.metadata,
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
                    "voltage_mv": latest.voltage_mv,
                    "cpu_pct": latest.cpu_pct,
                    "temperature_c": latest.battery_temperature_c,
                    "gpu_frequency_mhz": latest.gpu_frequency_mhz,
                    "gpu_load_pct": latest.gpu_load_pct,
                    "power_source": latest.power_source,
                    "power_sample_age_s": latest.power_sample_age_s,
                    "collector_cpu_pct": latest.collector_cpu_pct,
                }
                if latest is not None
                else None
            ),
            "summary": {
                "average_power_mw": statistics.fmean(powers) if powers else None,
                "p95_power_mw": _percentile(powers, 0.95),
                "average_current_ma": statistics.fmean(currents) if currents else None,
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
                "energy_mwh": _integrate_energy(samples, max_gap),
            },
            "series": [
                {
                    "elapsed_s": sample.elapsed_s,
                    "power_mw": sample.power_mw,
                    "current_ma": sample.current_ma,
                    "voltage_mv": sample.voltage_mv,
                    "cpu_pct": sample.cpu_pct,
                    "temperature_c": sample.battery_temperature_c,
                    "gpu_frequency_mhz": sample.gpu_frequency_mhz,
                    "gpu_load_pct": sample.gpu_load_pct,
                    "power_sample_age_s": sample.power_sample_age_s,
                }
                for sample in displayed
            ],
            "clusters": clusters,
            "context": asdict(latest_context) if latest_context is not None else None,
            "performance": performance,
            "battery": battery if isinstance(battery, dict) else {},
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

        title = str(metadata.get("title") or self.config.get("title") or "Power session")
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
            "device_serial": str(
                metadata.get("device_id")
                or metadata.get("adb_serial")
                or self.config.get("device")
                or ""
            ),
            "checkpoint": checkpoint,
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
            "mobile_power_profiler",
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
                and (resolved / "src" / "mobile_power_profiler").is_dir()
                and (resolved / "src" / "mobile_power_profiler").resolve() == package_dir
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
        refresh_ios: Optional[bool] = None,
        refresh_harmony: Optional[bool] = None,
    ) -> tuple[List[Dict[str, str]], Optional[str]]:
        now = time.time()
        with self._lock:
            refresh_android_now = force or now - self._android_device_cache_at >= 3.0
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

    def pair_ios(self, payload: Dict[str, object]) -> Dict[str, object]:
        device = str(payload.get("device") or "").strip() or None
        with self._lock:
            active = self.active
        if active is not None and active.running:
            raise RuntimeError("Stop the active recording before changing iOS RemotePairing")
        result = pair_ios_device(device, self.ios_python, 12.0)
        self.devices(force=True, refresh_ios=True)
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

        duration = _bounded_int(
            payload.get("duration"),
            "duration",
            2,
            604800,
            DEFAULT_UI_DURATION_S,
        )
        interval = _bounded_float(payload.get("interval"), "interval", 0.2, 60.0, 1.0)
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
        thermal_interval = _bounded_float(
            payload.get("thermal_interval"),
            "thermal interval",
            2.0,
            600.0,
            10.0,
        )
        scheduler_interval = _bounded_float(
            payload.get("scheduler_interval"),
            "scheduler interval",
            5.0,
            1800.0,
            30.0,
        )
        current_unit = str(payload.get("current_unit") or "auto")
        if current_unit not in {"auto", "ma", "ua"}:
            raise ValueError("current unit must be auto, ma, or ua")

        run_name = sanitize_run_name(payload.get("run_name"))
        output_dir = (self.output_root / run_name).resolve()
        try:
            output_dir.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError("Output directory must stay inside the configured run root") from exc
        if output_dir.exists() and any(output_dir.iterdir()):
            raise RuntimeError(f"Output directory is not empty: {output_dir}")

        title = str(payload.get("title") or "").strip()[:200]
        package = str(payload.get("package") or "").strip()[:200]
        start_context = str(payload.get("start_context") or "desktop").strip()
        if start_context not in {"desktop", "app", "other", "unknown"}:
            raise ValueError("start context must be desktop, app, other, or unknown")
        start_note = str(payload.get("start_note") or "").strip()[:500]
        gpu_path = str(payload.get("gpu_frequency_path") or "").strip()[:500]
        session_mode = bool(payload.get("session_mode", True))
        require_unplugged = bool(payload.get("require_unplugged", True))
        no_reset = bool(payload.get("no_reset", False))
        full_history = bool(payload.get("full_history", False))
        system_monitor = bool(payload.get("system_monitor", True))

        config: Dict[str, object] = {
            "device": device,
            "platform": platform,
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
        }
        command = self._base_command() + [
            "record",
            "--platform",
            platform,
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
        if no_reset:
            command.append("--no-reset")
        if full_history:
            command.append("--full-history")
        if not system_monitor:
            command.append("--no-system-monitor")

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
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
            else (self.source_root / (raw_output or "dist/mobile-power-profiler-portable")).resolve()
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
            self.source_root / "dist" / "mobile-power-profiler-portable"
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
            temperature = 31.2 + index * 0.007 + 0.15 * math.sin(index / 24.0)
            voltage = 3898.0 - index * 0.12
            series.append(
                {
                    "elapsed_s": float(index),
                    "power_mw": power,
                    "current_ma": power / max(3.6, voltage / 1000.0),
                    "voltage_mv": voltage,
                    "cpu_pct": min(100.0, cpu),
                    "temperature_c": temperature,
                    "gpu_frequency_mhz": 420.0 + 220.0 * max(0.0, math.sin(index / 21.0)),
                }
            )
        latest = series[-1]
        powers = [float(item["power_mw"]) for item in series]
        return {
            "status": "demo",
            "running": False,
            "is_demo": True,
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
                "sampled_compositor_fps": 117.8,
                "minimum_sampled_compositor_fps": 92.4,
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
                    "cpusets": {"foreground": "0-7", "background": "0-3", "system-background": "0-3", "restricted": "0-7"},
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
    return files("mobile_power_profiler").joinpath("web", name).read_bytes()


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
        self.wfile.write(payload)

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
            elif path == "/api/recover":
                result = self.server.manager.recover_run(payload)
            elif path == "/api/compare":
                result = self.server.manager.compare_history_runs(payload)
            elif path == "/api/build-portable":
                result = self.server.manager.build_portable_bundle(payload)
            elif path == "/api/devices/refresh":
                devices, error = self.server.manager.devices(force=True)
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
    print("Mobile Power Profiler UI")
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
