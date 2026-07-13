# Mobile Power Profiler

中文文档：[Mobile Power Profiler 使用指南（含 BTR2 联动）](docs/usage-zh.md)

A standalone mobile battery and resource profiler for long, multi-application
test sessions across Android, HarmonyOS, and iOS. Android and ADB-compatible
HarmonyOS devices use the standard-library core collector. iOS support is
implemented through an optional, separately installed `pymobiledevice3`
sidecar. The profiler has no Python import, API, or runtime dependency on BTR2.

## Why this exists

PerfDog is useful for live app performance. This tool focuses on evidence that
is easier to audit after a one-hour robotic workflow:

- Fuel-gauge current and battery voltage on one device uptime clock.
- Per-core CPU utilization, cluster frequency, and Android Power Profile
  frequency-impact estimates.
- GPU frequency/load when an OEM node is readable, including Qualcomm
  KGSL/Adreno detection and `gpubusy` busy/total conversion. Protected
  production builds fall back to `dumpsys gpu` UID work duration and per-PID
  GPU memory snapshots without confusing Qualcomm `gpubw` bus frequency with
  the GPU core clock.
- Foreground package, activity, screen state, brightness, and refresh context.
- Whole-system process and hot-thread snapshots spanning apps, Android services,
  native Linux services, and kernel tasks.
- Activity-type aggregation for ART/GC, `kworker` workqueues, RCU,
  IRQ/softirq, storage, memory reclaim, display composition, and power/thermal
  workers, with sampled CPU/power/temperature association.
- ThermalService history (CPU/GPU/NPU/SKIN/BATTERY sensors, severity, cooling
  devices, and exposed thresholds) plus cpuset, ActivityManager, ADPF, CPU
  governor, and Qualcomm-style `core_ctl` state.
- Dedicated detection for update/compilation activity such as `dex2oat`,
  `dexopt`, `artd`, `installd`, `profman`, `odrefresh`, `otapreopt`,
  `update_engine`, and `apexd`, correlated with battery-side power in time.
- Append-only raw data, checkpoints, ADB reconnects, and partial-run recovery.
- A local runtime dashboard for device Probe, recording control, live telemetry,
  collector logs, report history, log import, recovery, evidence packaging,
  two-run comparison, and source-project portable builds.
- Offline alignment of arbitrary timestamped logs through JSON regex rules.
- Measured energy by foreground app, imported phase/state, per-test item, and
  five-minute window. Each test item includes average/P95/peak power, CPU/GPU,
  temperature, top processes, GC/kworker evidence, DEX/update overlap, and a
  system-interference confidence label.
- A standalone Overview/Timeline/Flow/Test Items/Applications/CPU/System/
  Thermal & Scheduler/GPU/Attribution/Data HTML report.
- Optional iOS 17+ wireless RemoteXPC collection after one trusted-USB
  RemotePairing step: physical battery power/current/voltage/temperature,
  DVT process CPU/memory/disk and relative power scores, GPU utilization, and
  event-driven foreground-app transitions.

Modeled CPU, component, and UID values are kept separate from measured total
battery output. They are diagnostic estimates, not physical power rails.

## Install

Python 3.10+ is required. Android and HarmonyOS collection through the shared
ADB path also needs an `adb` executable on `PATH`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
mobile-power-profiler --help
```

The ADB/core profiler has no runtime dependency outside the Python standard
library. iOS uses a separate Python environment described below.

### Portable Windows bundle

Do not copy `.venv` to another computer; Windows virtual environments are not
reliably relocatable. Build a self-contained Embedded Python bundle instead:

```powershell
.\build-portable.bat
# or
powershell -ExecutionPolicy Bypass -File .\tools\build-portable.ps1
```

The output under `dist/` contains Python, the installed profiler package,
launchers, documentation, and (when found) the required ADB Platform Tools
files. On the target computer, extract the ZIP and run `start-ui.bat`.
The current portable builder does not bundle `pymobiledevice3`; point
`--ios-python` at a separately prepared iOS runtime when iOS collection is
needed.

After future source changes, run the full tests and rebuild the same bundle:

```powershell
python -m unittest discover -s tests -v
.\build-portable.bat
```

The **Tools & Delivery** UI view can invoke the same build script when the UI
is running from a complete source checkout. Portable installations deliberately
disable software rebuilding; use them for collection, import, recovery,
archiving, and comparison, then return to the source computer for a new ZIP.

## Runtime UI

Launch the local dashboard and let it open in the default browser:

```powershell
mobile-power-profiler ui
```

On Windows, you can also double-click `start-ui.bat` in the repository root.
The script prefers `.venv`, falls back to the system Python, and runs the local
source tree without requiring an editable install first.

The runtime UI provides device discovery and Probe, recording configuration,
start/stop controls, live power/current/CPU/temperature charts, foreground
context, whole-system process/thread activity, ThermalService and scheduler
state, collector logs, and links to completed reports. Its **Tools & Delivery**
view also exposes BTR2 log import, report regeneration, interrupted-run
recovery, evidence ZIP creation with optional attachments, two-phone
comparison, and source-only Windows portable packaging. These operations call
the same existing CLI or evidence workflows, so UI and command-line artifacts
stay identical.

The top bar also accepts `IP:PORT` and runs `adb connect` directly. For a
USB-authorized device, **Wireless ADB** reads the phone's Wi-Fi IPv4, runs
`adb -s SERIAL tcpip 5555`, fills `IP:5555`, and attempts the network
connection automatically. The button is disabled during recording because
restarting adbd would interrupt sampling. `adb pair` still belongs in a
terminal when Android Wireless Debugging requires pairing. Devices already
connected from CMD appear after refresh, so all workflows remain supported.
The device picker groups USB, Wi-Fi and emulator transports, and **Disconnect
Wireless** runs `adb disconnect` for the selected network device. Wireless
disconnect is disabled while recording.

The same picker also shows iPhones discovered by the optional sidecar. The
**iOS Wireless** action creates RemotePairing while the trusted USB cable is
connected; after the Wi-Fi endpoint is cached, unplug USB before recording.

## iOS wireless workflow

Create a dedicated runtime so the GPL-licensed optional dependency remains
outside the standard-library core and the Android portable bundle:

```powershell
python -m venv .venv-ios
.\.venv-ios\Scripts\python.exe -m pip install "pymobiledevice3==9.34.0"
$iosPython = (Resolve-Path .\.venv-ios\Scripts\python.exe)
```

When the environment is named `.venv-ios`, `start-ui.bat` detects it
automatically. Alternatively set the `IOS_PYTHON` environment variable before
launching the UI.

On the iPhone, enable Developer Mode, unlock it, connect USB, and trust the
computer. Create RemotePairing once:

```powershell
mobile-power-profiler --ios-python $iosPython ios-pair --json
```

When the command reports a Wi-Fi endpoint, remove USB. Probe and record using
the cached `ios:UDID` device identifier:

```powershell
mobile-power-profiler --ios-python $iosPython probe `
  --platform ios --device ios:00008150-EXAMPLE --json

mobile-power-profiler --ios-python $iosPython record `
  --platform ios `
  --device ios:00008150-EXAMPLE `
  --duration 120 `
  --interval 1 `
  --session-mode `
  --require-unplugged `
  --output power-runs\ios-smoke
```

The RemotePairing record is stored by `pymobiledevice3`; the last working
host/port is cached under `~/.mobile-power-profiler/ios-devices.json`. iOS
physical power commonly refreshes only about every 20 seconds even though DVT
CPU/GPU/process counters update at 0.5-1 second cadence. The report retains
`power_sample_age_s` and `collector_cpu_pct`, and never converts DVT
`powerScore` into mW or mixes it with physical whole-device power.

For long-lived test evidence, keep `power-runs` outside the versioned program
directory and pass it explicitly when starting either source or portable UI:

```powershell
.\start-ui.bat --output-root D:\MobilePowerData\power-runs
```

Preview the complete interface with synthetic live telemetry and no phone:

```powershell
mobile-power-profiler ui --demo
```

Useful UI options:

```powershell
mobile-power-profiler ui --port 8765 --output-root power-runs
mobile-power-profiler ui --no-browser
```

The default bind address is `127.0.0.1`; the dashboard is local-only unless
you explicitly change `--host`.

## One-hour multi-app workflow

Use Wi-Fi ADB so the USB charging cable can remain disconnected. Confirm the
phone is not externally powered before trusting current and energy values.

```powershell
mobile-power-profiler probe --device 192.168.21.179:5555

mobile-power-profiler record `
  --device 192.168.21.179:5555 `
  --duration 3720 `
  --session-mode `
  --require-unplugged `
  --output power-runs\btr2-round-001 `
  --title "BTR2 one-hour workflow"
```

Start the profiler first and wait until the live view shows its first telemetry
sample, then start BTR2. The recommended 3720-second window adds two minutes of
headroom around the one-hour workflow. The profiler tracks foreground
application transitions; it does not assume the app visible at session start
remains the target.

After the run, optionally align BTR2's timestamped text log:

```powershell
mobile-power-profiler import-log `
  power-runs\btr2-round-001 `
  C:\path\to\btr2.log `
  --rules examples\btr2-log-rules.json
```

The included BTR2 rule file is only a text-pattern example. It does not import
or call BTR2. Current BTR2 DCL logs use relative elapsed timestamps and cannot
be aligned directly; capture BTR2's absolute-timestamp console output instead.
See the [Chinese usage guide](docs/usage-zh.md) for the verified workflow, and
adjust the timestamp format and regexes if BTR2's log format changes.

The **Test Items** report view is designed for the full one-hour workflow. It
shows a summary matrix plus aligned lanes for whole-device power, foreground
app, BTR2 test span, classified system activity, Thermal status, and ADPF state.
Clicking a matrix/detail row zooms the chart to that item. If no duration events
were imported, the analyzer falls back to foreground Activity intervals.

## Recovery

Every complete sampler line is flushed to disk immediately. A checkpoint and
clock synchronization point are written every 30 seconds by default and after
reconnects.

```powershell
mobile-power-profiler recover power-runs\btr2-round-001
```

Recovery needs only the run directory. It can finalize data after Ctrl+C,
terminal loss, or an ADB failure without reconnecting to the phone. Missing ADB
intervals are reported as coverage gaps and are excluded from energy
integration rather than interpolated as if they were measured.

## Other commands

```powershell
mobile-power-profiler probe --device SERIAL
mobile-power-profiler report RUN_DIR
mobile-power-profiler demo --output power-runs\demo
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
- `--no-system-monitor`: disable process/thread/thermal/scheduler snapshots.
- `--process-interval 10`: whole-system process cadence.
- `--thread-interval 30`: hot-thread cadence.
- `--thermal-interval 10`: ThermalService cadence.
- `--scheduler-interval 30`: cpuset, ActivityManager, and ADPF cadence.
- `--start-context desktop --start-note "BTR2 starts later"`: preserve the
  expected capture start and pre-roll note in metadata.

Global `--adb PATH` must appear before the subcommand.

## Evidence archive and two-phone comparison

`import-log` copies the original BTR2 log and rule file into the run's
`attachments/btr2/` directory. Package the complete auditable run with hashes:

```powershell
mobile-power-profiler archive RUN_DIR --attach EXTRA_LOG --output RUN-evidence.zip
```

Compare two completed runs after importing their respective BTR2 logs:

```powershell
mobile-power-profiler compare RUN_A RUN_B `
  --label-a "Phone A" --label-b "Phone B" `
  --output power-runs\compare-a-vs-b
```

The comparison emits `comparison.json` and a Chinese `comparison.html`, pairing
test items by phase/name and showing B-minus-A power, energy rate, CPU,
temperature, GC/kworker, DEX/update, and system-interference evidence.
For a combined multi-phone BTR2 log, import each run with a metadata filter such
as `--match phone_key=phone1` / `--match phone_key=phone2`.

## Sampling schedule

The default Android long-session schedule limits expensive services:

| Data | Cadence | Source |
|---|---:|---|
| Current, `/proc/stat`, CPU/GPU frequency | 1 s | Persistent ADB shell |
| Battery voltage | 5 s | BatteryService dump, held between reads |
| Temperature and foreground context | 10 s | BatteryService/ActivityManager |
| Whole-system processes + watched update/DEX services | 10 s | `top` + `ps -A` |
| Hot threads + GC/kworker/kernel classification | 30 s | `top -H` |
| Thermal sensors, severity, cooling, thresholds | 10 s | ThermalService / thermal HAL |
| cpuset, process state, ADPF sessions | 30 s | cgroup + ActivityManager + `performance_hint` |
| Active refresh rate | 30 s | Display service |
| Clock synchronization | 30 s plus lifecycle events | `/proc/uptime` + host midpoint |

For iOS, DVT CPU/GPU/process rows use the selected sample interval, process
snapshots default to 10 seconds, battery diagnostics are polled every 5
seconds, and the underlying physical-power value typically changes about every
20 seconds.

## Run directory

```text
run-directory/
|-- metadata.json
|-- checkpoint.json
|-- samples.csv
|-- contexts.jsonl
|-- clock-sync.jsonl
|-- events.jsonl                 # after import-log
|-- system-snapshots.jsonl       # processes, GC/kworker hot threads, watched activities
|-- thermal-snapshots.jsonl      # sensors, severity, cooling and thresholds
|-- scheduler-snapshots.jsonl    # cpuset, proc state, CPU policy and ADPF
|-- analysis.json
|-- report.html
|-- report-fragment.html
|-- attachments/btr2/           # preserved source log and import rules
`-- raw/
    |-- sampler-stream.txt       # Android S|/CTX| or normalized sidecar N| frames
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
| Process/thread CPU | Periodic whole-system snapshots, not a continuous scheduler trace |
| GC/kworker/RCU/IRQ activity | Name-based aggregation of periodic hot-thread/process snapshots; short activity can be missed |
| DEX/update power delta | Battery-side power during detected windows versus the session baseline; temporal association, not per-process attribution |
| Thermal policy | Exposed status, sensors, cooling devices, and static thresholds; the complete OEM decision algorithm may remain private |
| Scheduler state | Exposed cpuset, ActivityManager proc state, and ADPF sessions; production permissions may hide governor/uclamp/sched_debug details |
| App/phase energy | Measured total energy allocated by foreground/log time intervals |
| Per-test system interference | Sampled overlap with classified system/thermal activity and visible system CPU; not exclusive process energy |
| BatteryStats contributors | Android model attribution; values can overlap and need not sum to measured total |
| iOS `SystemLoad` | Physical whole-device battery-side power with a typically ~20 s refresh cadence; repeated 1 s rows retain sample age |
| iOS DVT `powerScore` | Relative diagnostic score only; not mW, joules, or per-process rail energy |
| iOS collector CPU | Normalized CPU used by `sysmond`, `DTServiceHub`, and `remotepairingdeviced`; retained as observer-overhead evidence |

`current_ma` is always a positive magnitude for drain charts and integration.
`signed_current_ma` preserves direction: negative is discharge and positive is
charge after BatteryService status normalization.

Foreground transitions are sampled, so app boundaries have roughly one context
interval of uncertainty. Imported phases shorter than three sample intervals
are marked low confidence.

System monitoring has observer overhead: `top`, especially `top -H`, and
service dumps briefly use device CPU. Keep the same monitor cadences across
comparison runs; the collector records its command duration in each snapshot.

## Tests

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
```
