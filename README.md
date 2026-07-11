# Android Power Profiler

A standalone, standard-library-only Android battery and resource profiler for
long, multi-application test sessions. It runs beside the system under test and
has no Python import, API, or runtime dependency on BTR2.

## Why this exists

PerfDog is useful for live app performance. This tool focuses on evidence that
is easier to audit after a one-hour robotic workflow:

- Fuel-gauge current and battery voltage on one device uptime clock.
- Per-core CPU utilization, cluster frequency, and Android Power Profile
  frequency-impact estimates.
- GPU frequency/load when an OEM node is readable, with `dumpsys gpu` UID work
  duration as a fallback.
- Foreground package, activity, screen state, brightness, and refresh context.
- Append-only raw data, checkpoints, ADB reconnects, and partial-run recovery.
- Offline alignment of arbitrary timestamped logs through JSON regex rules.
- Measured energy by foreground app, imported phase/state, and five-minute
  window.
- A standalone Overview/Timeline/Flow/Applications/CPU/GPU/Attribution/Data
  HTML report.

Modeled CPU, component, and UID values are kept separate from measured total
battery output. They are diagnostic estimates, not physical power rails.

## Install

Python 3.10+ and an `adb` executable on `PATH` are required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
android-power-profiler --help
```

The profiler has no runtime dependency outside the Python standard library.

## One-hour multi-app workflow

Use Wi-Fi ADB so the USB charging cable can remain disconnected. Confirm the
phone is not externally powered before trusting current and energy values.

```powershell
android-power-profiler probe --device 192.168.21.179:5555

android-power-profiler record `
  --device 192.168.21.179:5555 `
  --duration 3600 `
  --session-mode `
  --require-unplugged `
  --output power-runs\btr2-round-001 `
  --title "BTR2 one-hour workflow"
```

Run BTR2 normally in its own process and repository while this command is
recording. The profiler tracks foreground application transitions; it does not
assume the app visible at session start remains the target.

After the run, optionally align BTR2's timestamped text log:

```powershell
android-power-profiler import-log `
  power-runs\btr2-round-001 `
  C:\path\to\btr2.log `
  --rules examples\btr2-log-rules.json
```

The included BTR2 rule file is only a text-pattern example. It does not import
or call BTR2. Adjust its timestamp format and regexes to match the actual log.

## Recovery

Every complete sampler line is flushed to disk immediately. A checkpoint and
clock synchronization point are written every 30 seconds by default and after
reconnects.

```powershell
android-power-profiler recover power-runs\btr2-round-001
```

Recovery needs only the run directory. It can finalize data after Ctrl+C,
terminal loss, or an ADB failure without reconnecting to the phone. Missing ADB
intervals are reported as coverage gaps and are excluded from energy
integration rather than interpolated as if they were measured.

## Other commands

```powershell
android-power-profiler probe --device SERIAL
android-power-profiler report RUN_DIR
android-power-profiler demo --output power-runs\demo
```

Useful recording options:

- `--package PACKAGE`: retain a named target app for BatteryStats/UID analysis.
- `--session-mode`: do not default to the starting foreground package.
- `--interval 1`: current, CPU, and frequency sampling interval.
- `--checkpoint-interval 30`: journal and clock-sync cadence.
- `--reconnect-timeout 120`: maximum ADB outage before finalizing partial data.
- `--gpu-frequency-path PATH`: override an OEM-readable GPU frequency node.
- `--no-reset`: keep existing BatteryStats history.
- `--full-history`: enable detailed BatteryStats history where supported.

Global `--adb PATH` must appear before the subcommand.

## Sampling schedule

The default long-session schedule limits expensive Android services:

| Data | Cadence | Source |
|---|---:|---|
| Current, `/proc/stat`, CPU/GPU frequency | 1 s | Persistent ADB shell |
| Battery voltage | 5 s | BatteryService dump, held between reads |
| Temperature and foreground context | 10 s | BatteryService/ActivityManager |
| Active refresh rate | 30 s | Display service |
| Clock synchronization | 30 s plus lifecycle events | `/proc/uptime` + host midpoint |

## Run directory

```text
run-directory/
|-- metadata.json
|-- checkpoint.json
|-- samples.csv
|-- contexts.jsonl
|-- clock-sync.jsonl
|-- events.jsonl                 # after import-log
|-- analysis.json
|-- report.html
|-- report-fragment.html
`-- raw/
    |-- sampler-stream.txt       # S| and CTX| frames, append-only during capture
    |-- sampler-stderr.txt
    |-- battery_start.txt
    |-- battery_end.txt
    |-- power_profile.txt
    |-- batterystats.txt
    |-- gpu_start.txt
    `-- gpu_end.txt
```

`samples.csv` and `analysis.json` retain all samples. The HTML payload uses
largest-triangle-three-buckets downsampling at about 1200 points so a one-hour
report remains responsive while preserving the original data files.

## Accuracy boundaries

| Metric | Interpretation |
|---|---|
| Fuel-gauge current | Measured net battery flow; external power can reduce it to zero or charging current |
| Total battery power | Positive discharge-current magnitude multiplied by battery voltage |
| CPU load/frequency | Kernel counters sampled per core and cpufreq policy |
| CPU frequency impact | Android Power Profile estimate for same-device comparison |
| GPU frequency/load | OEM kernel counter only when readable to the ADB shell |
| GPU UID work | Driver activity duration, not electrical energy |
| App/phase energy | Measured total energy allocated by foreground/log time intervals |
| BatteryStats contributors | Android model attribution; values can overlap and need not sum to measured total |

`current_ma` is always a positive magnitude for drain charts and integration.
`signed_current_ma` preserves direction: negative is discharge and positive is
charge after BatteryService status normalization.

Foreground transitions are sampled, so app boundaries have roughly one context
interval of uncertainty. Imported phases shorter than three sample intervals
are marked low confidence.

## Tests

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
```
