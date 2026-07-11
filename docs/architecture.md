# Architecture

## Repository structure

```text
android-power-profiler/
|-- pyproject.toml
|-- README.md
|-- docs/
|   `-- architecture.md
|-- examples/
|   `-- btr2-log-rules.json
|-- tests/
|   `-- test_profiler.py
`-- src/android_power_profiler/
    |-- __init__.py
    |-- __main__.py
    |-- models.py
    |-- collector.py
    |-- parsers.py
    |-- log_import.py
    |-- analysis.py
    |-- storage.py
    |-- report.py
    `-- cli.py
```

## Sidecar boundary

The profiler and BTR2 are independent processes and repositories.

```text
Android phone <-- ADB --> Android Power Profiler --> run directory
      ^
      |
robot/camera
      |
     BTR2 -------------------------------> timestamped text log
                                                |
                                                `-- optional offline import
```

There is no shared Python module, callback, socket, database, or lifecycle API.
The only optional correlation surface is a timestamped text file interpreted by
a user-selected JSON regex rule file.

## Collection lifecycle

1. Probe BatteryService current, cpufreq policies, and optional GPU nodes.
2. Persist initial metadata and battery/GPU snapshots before recording.
3. Start one persistent `adb shell` sampler with framed output:
   - `S|...` for numeric telemetry.
   - `CTX|...` for foreground/display context.
4. Read stdout and stderr on dedicated threads and flush every line to the run
   journal before parsing it in memory.
5. Record host/device clock midpoint pairs at start, checkpoints, reconnects,
   and end.
6. Restart the sampler after a recoverable ADB disconnect while keeping the
   original wall-clock session deadline.
7. Capture post-run BatteryStats, GPU, CPU, thermal, display, and Power Profile
   evidence.
8. Convert raw counters, analyze valid intervals, and generate artifacts.

If collection stops early, `recover` rebuilds samples from
`raw/sampler-stream.txt` and uses existing snapshots. It does not require the
device.

## Time domains

Device uptime is the canonical telemetry clock. Each synchronization point is:

```text
host midpoint epoch
host midpoint monotonic
device /proc/uptime
ADB round-trip time
```

External host timestamps are converted by interpolating the host-minus-device
offset between adjacent synchronization points. Midpoint timing reduces, but
does not eliminate, uncertainty from ADB latency. The original host timestamp,
line number, source log, and regex are retained in event metadata.

## Long-session persistence

| Artifact | Write policy | Purpose |
|---|---|---|
| `raw/sampler-stream.txt` | Flush every line | Source of truth for recovery |
| `contexts.jsonl` | Flush every context frame | App/display timeline |
| `clock-sync.jsonl` | Flush every sync point | Offline log alignment |
| `checkpoint.json` | Atomic replace | Run state and last complete counts |
| `samples.csv` | Finalization/recovery | Full converted telemetry |
| `analysis.json` | Finalization/recovery | Full derived metrics |
| `report.html` | Finalization/recovery | Portable interactive report |

A one-hour run is small enough to keep converted samples in memory, but raw
durability never depends on the process reaching finalization.

## Gap handling

ADB outages create missing observations. Intervals greater than three sample
periods are excluded from energy, CPU residency, app allocation, phase
allocation, and five-minute windows. The report exposes covered duration,
missing duration, and coverage percentage.

This avoids the more dangerous behavior of drawing a straight line across a
long disconnect and integrating it as measured energy.

## Analysis layers

### Measured battery side

- Positive current magnitude and signed direction.
- Voltage, total power, mWh, and discharge mAh.
- Time-weighted averages over valid intervals.
- Spike detection and five-minute windows.

### Kernel and driver evidence

- Global and per-core `/proc/stat` utilization.
- cpufreq policy frequency and load-weighted residency.
- GPU devfreq/load when readable.
- GPU UID active-duration deltas otherwise.

### Android models

- CPU frequency impact from per-cluster Power Profile tables.
- BatteryStats component and UID estimates.
- Screen estimate fallback from Power Profile and brightness.

Model outputs are not forced to add up to measured total power.

### Workflow allocation

- Measured energy by sampled foreground package.
- Measured energy by imported duration event/state.
- Transition counts and context coverage.
- Low-confidence marking when a phase is shorter than three sample periods or
  has less than 75 percent telemetry coverage.

## Log rule model

`import-log` accepts:

- A global timestamp regex, formats, and timezone.
- Event rules with regex, name template, phase template, key template, and kind.
- `start`/`end` pairing by phase and key.
- `state` closure on the next state or session end.
- `instant` markers for taps, swipes, retries, and milestones.

The importer is generic. `examples/btr2-log-rules.json` is configuration data,
not a code dependency.

## Report payload

Full-resolution data remains in CSV/JSON. When more than 1200 samples are
present, report generation selects display indices with
largest-triangle-three-buckets using measured power. The CPU timeline uses the
same indices, preserving cross-chart alignment.

## Ownership

| Module | Owns |
|---|---|
| `models.py` | Versioned data contracts |
| `collector.py` | ADB execution, probing, framing, reconnects, clock capture |
| `parsers.py` | Android text-to-structure conversion |
| `log_import.py` | Generic host-log parsing and clock alignment |
| `analysis.py` | Measurement conversion, gaps, CPU/GPU/app/phase conclusions |
| `storage.py` | CSV/JSONL/raw journal/checkpoint persistence |
| `report.py` | Downsampled interactive presentation |
| `cli.py` | User workflows and finalization orchestration |
