from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


SCHEMA_VERSION = 8
APP_NAME = "Mobile Profiler"
DEFAULT_ADB = os.environ.get("ADB", "adb")
DEFAULT_DURATION_S = 60
DEFAULT_INTERVAL_S = 1.0


@dataclass
class CommandResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class CpuPolicy:
    name: str
    path: str
    cluster_index: int
    label: str
    cores: List[int] = field(default_factory=list)
    min_khz: Optional[float] = None
    max_khz: Optional[float] = None
    available_frequencies_khz: List[float] = field(default_factory=list)
    governor: Optional[str] = None
    core_control: Dict[str, object] = field(default_factory=dict)


@dataclass
class GpuSource:
    name: str
    frequency_path: Optional[str] = None
    load_path: Optional[str] = None
    load_format: str = "percentage"
    minimum_mhz: Optional[float] = None
    maximum_mhz: Optional[float] = None
    available_frequencies_mhz: List[float] = field(default_factory=list)
    source_type: str = "sysfs"


@dataclass
class MemorySource:
    name: str
    frequency_path: str
    minimum_mhz: Optional[float] = None
    maximum_mhz: Optional[float] = None
    available_frequencies_mhz: List[float] = field(default_factory=list)
    source_type: str = "devfreq"


@dataclass
class CpuTimes:
    user: float = 0.0
    nice: float = 0.0
    system: float = 0.0
    idle: float = 0.0
    iowait: float = 0.0
    irq: float = 0.0
    softirq: float = 0.0
    steal: float = 0.0

    @classmethod
    def from_values(cls, values: List[float]) -> "CpuTimes":
        padded = list(values[:8]) + [0.0] * max(0, 8 - len(values))
        return cls(*padded[:8])

    def total_and_idle(self) -> tuple[float, float]:
        idle_total = self.idle + self.iowait
        total = (
            self.user
            + self.nice
            + self.system
            + self.idle
            + self.iowait
            + self.irq
            + self.softirq
            + self.steal
        )
        return total, idle_total


@dataclass
class RawSample:
    index: int
    uptime_s: float
    current_raw: float
    voltage_mv: Optional[float]
    temperature_tenths_c: Optional[float]
    cpu: CpuTimes
    core_cpu: Dict[int, CpuTimes] = field(default_factory=dict)
    frequencies_khz: Dict[str, float] = field(default_factory=dict)
    gpu_frequency_raw: Optional[float] = None
    gpu_load_raw: Optional[float] = None
    memory_frequency_raw: Optional[float] = None


@dataclass
class Sample:
    index: int
    elapsed_s: float
    uptime_s: float
    current_ma: float
    signed_current_ma: float
    voltage_mv: float
    power_mw: float
    direction: str
    cpu_pct: Optional[float]
    core_cpu_pct: Dict[str, float] = field(default_factory=dict)
    cluster_cpu_pct: Dict[str, float] = field(default_factory=dict)
    frequencies_mhz: Dict[str, float] = field(default_factory=dict)
    gpu_frequency_mhz: Optional[float] = None
    gpu_load_pct: Optional[float] = None
    memory_frequency_mhz: Optional[float] = None
    battery_temperature_c: Optional[float] = None
    power_source: str = "battery_current_voltage"
    power_sample_age_s: Optional[float] = None
    collector_cpu_pct: Optional[float] = None


@dataclass
class ContextSample:
    uptime_s: float
    foreground_package: Optional[str] = None
    foreground_activity: Optional[str] = None
    screen_state: Optional[str] = None
    brightness_raw: Optional[float] = None
    refresh_rate_hz: Optional[float] = None
    source: str = "sampler"
    performance: Dict[str, object] = field(default_factory=dict)


@dataclass
class ClockSyncPoint:
    host_epoch_s: float
    host_monotonic_s: float
    device_uptime_s: float
    round_trip_ms: float


@dataclass
class ExternalEvent:
    device_uptime_s: float
    name: str
    phase: str
    kind: str = "instant"
    host_epoch_s: Optional[float] = None
    duration_s: Optional[float] = None
    source: str = "external_log"
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class SystemSnapshot:
    uptime_s: float
    host_epoch_s: float
    processes: List[Dict[str, object]] = field(default_factory=list)
    threads: List[Dict[str, object]] = field(default_factory=list)
    watched_processes: List[Dict[str, object]] = field(default_factory=list)
    process_count: Optional[int] = None
    thread_count: Optional[int] = None
    collection_ms: Optional[float] = None


@dataclass
class ThermalSnapshot:
    uptime_s: float
    host_epoch_s: float
    status: Optional[int] = None
    hal_ready: Optional[bool] = None
    temperatures: List[Dict[str, object]] = field(default_factory=list)
    cooling_devices: List[Dict[str, object]] = field(default_factory=list)
    thresholds: List[Dict[str, object]] = field(default_factory=list)
    headroom_thresholds: List[Optional[float]] = field(default_factory=list)
    collection_ms: Optional[float] = None


@dataclass
class SchedulerSnapshot:
    uptime_s: float
    host_epoch_s: float
    cpusets: Dict[str, str] = field(default_factory=dict)
    cpu_policies: List[Dict[str, object]] = field(default_factory=list)
    hint_sessions: List[Dict[str, object]] = field(default_factory=list)
    watched_processes: List[Dict[str, object]] = field(default_factory=list)
    power_hal: List[str] = field(default_factory=list)
    availability: Dict[str, object] = field(default_factory=dict)
    collection_ms: Optional[float] = None
