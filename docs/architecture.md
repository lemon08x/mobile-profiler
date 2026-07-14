# Architecture

## Repository structure

```text
mobile-profiler/
|-- pyproject.toml
|-- README.md
|-- build-portable.bat
|-- start-ui.bat
|-- docs/
|   |-- architecture.md
|   `-- usage-zh.md
|-- examples/
|   `-- btr2-log-rules.json
|-- tools/
|   `-- build-portable.ps1
|-- tests/
|   |-- test_profiler.py
|   `-- test_ui.py
`-- src/mobile_power_profiler/
    |-- __init__.py
    |-- __main__.py
    |-- models.py
    |-- collector.py
    |-- ios.py
    |-- ios_bridge.py
    |-- parsers.py
    |-- log_import.py
    |-- analysis.py
    |-- storage.py
    |-- report.py
    |-- comparison.py
    |-- evidence.py
    |-- ui.py
    |-- web/
    |   |-- index.html
    |   |-- app.css
    |   `-- app.js
    `-- cli.py
```

## External workflow boundary

The profiler and BTR2 are independent processes and repositories.

```text
Android device  <-- ADB -->+
HarmonyOS phone <-- HDC -->+--> Mobile Profiler --> run directory
             ^
             |
robot/camera
             |
            BTR2 ------------------------> timestamped text log
                                                |
                                                `-- optional offline import
```

There is no shared Python module, callback, socket, database, or lifecycle API.
The only optional correlation surface is a timestamped text file interpreted by
a user-selected JSON regex rule file.

## Platform sidecar boundary

The core package stays standard-library-only. Android runs through
`collector.py`, native HarmonyOS runs through `harmony.py`, and iOS runs in a
separate Python interpreter containing `pymobiledevice3`.

```text
Android <-- ADB -------- S| / CTX| -------------+
HarmonyOS <-- HDC ------ N| + JSONL ------------+
                                                  |
                                                  v
                                      normalized Sample / Context /
                                      System / Thermal contracts
                                                  ^
                                                  |
iPhone <-- RemotePairing / RSD --> ios_bridge.py -+
            separate Python         JSON / JSONL
```

`ios.py` is the standard-library parent adapter. It launches `ios_bridge.py`,
merges discovered devices with cached endpoints, and journals sidecar events.
The sidecar supports `list`, `pair`, `probe`, and `record`. Streaming events are
`ready`, `sample`, `context`, `clock`, `system`, `thermal`, `warning`, and
`end`. A normalized sample is persisted as `N|{json}` in the same append-only
sampler stream used by the ADB collector.

The normalized boundary deliberately contains no ADB, HDC, or Apple transport
types. `harmony.py` computes CPU deltas on the host and persists normalized
`N|{json}` samples, so finalization, recovery, live UI, analysis, and reports
share the same contracts as iOS and future adapters.

## Collection lifecycle

### Android ADB lifecycle

1. Probe BatteryService current, cpufreq policies, optional GPU nodes, display modes,
   SurfaceFlinger refresh residency/renderer identity, and touchscreen capabilities.
2. Persist initial metadata and battery/GPU snapshots before recording.
3. Start one persistent `adb shell` sampler with `S|...` framed numeric telemetry.
   `CTX|...` remains a recovery-compatible fallback for callers that explicitly
   disable the Android performance-context worker.
4. Start an independent performance-context worker. Every 10 seconds it captures
   foreground/window state, display modes, brightness, and cumulative foreground
   `gfxinfo` frame counters. When the foreground app exposes a SurfaceView/BLAST layer,
   its SurfaceFlinger present-timestamp ring is sampled every 0.5 seconds and summarized
   into FPS, 1% Low, and frame-interval windows. SurfaceFlinger refresh-duration counters
   and GLES renderer identity are sampled every 30 seconds and once more during finalization.
5. Start an independent low-frequency system-monitor worker. It captures
   processes every 10 seconds, hot threads every 30 seconds, ThermalService
   every 10 seconds, and cpuset/ActivityManager/ADPF state every 30 seconds.
   Expensive `top -H` and `dumpsys` calls therefore never block the one-second
   battery sampler.
6. Read stdout and stderr on dedicated threads and flush every line to the run
   journal before parsing it in memory.
7. Record host/device clock midpoint pairs at start, checkpoints, reconnects,
   and end.
8. Restart the sampler after a recoverable ADB disconnect while keeping the
   original wall-clock session deadline.
9. Capture post-run BatteryStats, GPU, CPU, thermal, display, and Power Profile
   evidence.
10. Convert raw counters, correlate system/thermal/scheduler snapshots on device
   uptime, analyze valid intervals, and generate artifacts.

If collection stops early, `recover` rebuilds samples from
`raw/sampler-stream.txt` and uses existing snapshots. It does not require the
device.

### HarmonyOS HDC lifecycle

1. Discover USB/TCP targets with `hdc list targets -v`; public identifiers use
   the `harmony:` prefix so automatic platform selection cannot confuse HDC and
   ADB addresses.
2. Probe `BatteryService`, `hidumper -c base`, `/proc/stat`,
    `hidumper --cpufreq`, `ThermalService`, `PowerManagerService`, AbilityManager,
    RenderService, WindowManagerService, MultimodalInput, and `top`/`ps`.
3. Start one persistent HDC shell that frames device realtime, BatteryService,
   and `/proc/stat` rows. The host computes global/per-core deltas and appends
   normalized `N|{json}` samples.
4. At lower cadence, append foreground/power contexts with display modes,
   `fpsCount` counters, recent compositor timestamps, focused window, delivered
   touch events, and window hitch counters. Process/thermal/scheduler snapshots
   remain separate. Full CPU-frequency dumps are limited to at least 10 seconds
   because they are comparatively expensive on 12-core devices.
5. If a TCP sampler exits, issue `hdc tconn`, relaunch against the same target,
   deduplicate by device timestamp, and retain the original session deadline.
6. Final BatteryService state is saved in metadata. Android-only BatteryStats,
   ActivityManager, ADPF, and dumpsys GPU sources remain explicitly unavailable.
7. Finalization consumes the same normalized journal and artifact pipeline used
   by iOS; no Harmony-specific report fork is required.

### iOS lifecycle

1. A trusted USB connection creates a persistent RemotePairing record.
2. The parent caches the validated Wi-Fi host/port under
   `~/.mobile-profiler/ios-devices.json`, while reading the former
   `.mobile-power-profiler` and `.android-power-profiler` locations as migration
   fallbacks.
3. Probe opens a userspace RSD tunnel, reads DiagnosticsService battery data,
   inspects DVT sysmon capabilities, and samples DVT Graphics availability.
4. Record starts concurrent DVT sysmontap, Graphics, and application-state
   notification streams plus low-frequency battery diagnostics polling.
5. Physical battery power/current/voltage/temperature and high-frequency DVT
   counters are normalized into `Sample`; DVT `powerScore` remains a relative
   field in process snapshots and is never converted into mW.
6. `sysmond`, `DTServiceHub`, and `remotepairingdeviced` are tagged as collector
   overhead. Normalized collector CPU is also retained per sample.
7. If the sidecar exits, the parent restarts it against the cached endpoint
   until the original deadline or reconnect timeout. Device uptime deduplicates
   overlapping rows across sidecar sessions.
8. Finalization consumes the same journal, analysis, CSV, and report pipeline
   as Android.

Apple's physical power fields commonly refresh about every 20 seconds while
DVT counters refresh at 0.5-1 second cadence. `power_sample_age_s` records that
staleness explicitly instead of presenting repeated rows as fresh physical
measurements.

## Runtime UI boundary

The local dashboard is a standard-library `ThreadingHTTPServer` bound to
`127.0.0.1` by default. It does not duplicate the collector. Starting a session
from the UI launches the installed module's existing `record` command in a
child process and tails the append-only sampler journal for live presentation.

```text
Browser <-- local JSON/HTML --> ui.py --> python -m mobile_power_profiler record
                                      |--> report / recover / import-log
                                      |--> compare
                                      |--> evidence archive
                                      |--> source-only portable build script
                                      `--> run journals and snapshot tails
```

This process boundary preserves CLI behavior and makes UI failure independent
from the raw collection journal. The UI can request a graceful interrupt, after
which the normal CLI partial-run finalization path generates recoverable output.
Static HTML, CSS, and JavaScript are packaged with the Python module and use no
CDN or external runtime dependency.

The Tools & Delivery page reuses the CLI for report regeneration, recovery,
BTR2 import, and comparison; evidence archives reuse `evidence.py` directly.
Maintenance work is serialized, and an active run directory cannot be rebuilt,
recovered, imported, or archived. Portable builds are additionally disabled
while any real recording is active.

The `/api/tcpip` workflow is also guarded against active recording. It reads
global IPv4 interfaces before restarting adbd, prioritizes `wlan*`/`wifi*`,
rejects mobile-data `rmnet` addresses for automatic connection, executes
`adb -s SERIAL tcpip 5555`, and then reuses the normal `/api/connect` logic.

Source-project detection requires all of `pyproject.toml`,
`tools/build-portable.ps1`, and `src/mobile_power_profiler`. Therefore a
portable installation can perform capture and data workflows but cannot
present itself as a build checkout. Build output from the UI is constrained to
the source checkout's `dist/` tree because the PowerShell builder intentionally
replaces an existing output directory.

## Time domains

The device clock is canonical within a run. Android reads `/proc/uptime`;
HarmonyOS uses `date +%s.%N` device realtime because production HDC shells can
deny `/proc/uptime`; iOS converts Mach absolute time with the device-reported
timebase. Each
synchronization point is:

```text
host midpoint epoch
host midpoint monotonic
device clock value
transport round-trip time
```

External host timestamps are converted by interpolating the host-minus-device
offset between adjacent synchronization points. Midpoint timing reduces, but
does not eliminate, uncertainty from ADB/HDC/RemoteXPC latency. The original host timestamp,
line number, source log, and regex are retained in event metadata.

## Long-session persistence

| Artifact | Write policy | Purpose |
|---|---|---|
| `raw/sampler-stream.txt` | Flush every line | Source of truth for Android `S|`/`CTX|` and HarmonyOS/iOS normalized `N|` recovery |
| `contexts.jsonl` | Flush every context frame | App/display/window timeline plus optional refresh/FPS/frame/touch performance payload |
| `clock-sync.jsonl` | Flush every sync point | Offline log alignment |
| `system-snapshots.jsonl` | Flush every process/thread snapshot | Whole-system CPU, GC/kworker/kernel groups, and watched DEX/update activity |
| `thermal-snapshots.jsonl` | Flush every ThermalService snapshot | Sensor, severity, cooling, and threshold history |
| `scheduler-snapshots.jsonl` | Flush every scheduler snapshot | cpuset, CPU-policy visibility, proc state, and ADPF |
| `checkpoint.json` | Atomic replace | Run state and last complete counts |
| `samples.csv` | Finalization/recovery | Full converted telemetry |
| `analysis.json` | Finalization/recovery | Full derived metrics |
| `report.html` | Finalization/recovery | Portable interactive report |

A one-hour run is small enough to keep converted samples in memory, but raw
durability never depends on the process reaching finalization.

## Gap handling

Transport or sidecar outages create missing observations. Intervals greater than three sample
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

On iOS, `PowerTelemetryData.SystemLoad` is preferred for whole-device physical
power. Current × voltage remains the fallback. The analysis also reports the
observed power-source names, physical-sample age, and collector CPU overhead.

### Kernel and driver evidence

- Global and per-core `/proc/stat` utilization.
- cpufreq policy frequency and load-weighted residency.
- GPU devfreq/load when readable.
- GPU UID active-duration deltas and per-PID memory snapshots otherwise.
- Android SurfaceFlinger refresh-rate duration deltas and GLES renderer identity.
- Foreground SurfaceView/BLAST present timestamps from SurfaceFlinger `--latency`,
  deduplicated across the short ring buffer for native-game FPS and frame pacing.
- Android foreground-window `gfxinfo` rendered-frame, deadline, jank, missed-vsync,
  and frame-duration histogram deltas without clearing global graphics statistics.
- Periodic whole-system `top`/`ps` snapshots for apps, Android services, native
  services, kernel tasks, and hot threads.
- Name-based activity grouping for ART/GC, kworker workqueues (including
  storage/display/network/memory/power hints when exposed), RCU, IRQ/softirq,
  memory reclaim, storage workers, scheduler workers, and display composition.

### Thermal and scheduler evidence

- ThermalService/thermal HAL temperatures, severity, cooling-device values,
  static thresholds, and headroom thresholds.
- cpuset CPU masks and readable cpufreq policy limits/governors.
- ActivityManager process state, scheduler group, adjustment reason, and frozen
  state for CPU-visible or specially watched processes.
- ADPF HintSession PID/TID, target duration, proc-state permission,
  power-efficient flag, and graphics-pipeline flag.
- Qualcomm WALT governor and per-policy `core_ctl` online-core bounds when the
  production build exposes them.

Production permissions often hide `sched_debug`, top-app cpuset content,
runtime governors/uclamp controls, and the complete OEM thermal algorithm. The
analysis reports this as an observability boundary rather than inferring hidden
policy.

### Qualcomm KGSL adapter

`detect_gpu_source` identifies Qualcomm through SoC properties or a readable
KGSL `gpu_model`. It checks the core-clock nodes under
`/sys/class/kgsl/kgsl-3d0`, supports both direct percentage nodes and KGSL's
two-column `gpubusy` busy/total format, and tags the source as
`qualcomm_kgsl`. Devfreq identities containing `gpubw`, busmon, or memlat are
explicitly rejected because they represent bandwidth/latency devices rather
than the Adreno core clock.

On locked user builds, `gpu_model` may remain readable while clock/load nodes
return `EACCES`. This is represented as a detected-but-restricted provider.
`dumpsys gpu` still supplies cumulative per-UID active durations and an
instantaneous global/per-PID GPU memory snapshot. The analysis keeps these
driver counters separate from battery-side power and does not synthesize a GPU
load percentage.

### Priority background activity

`parsers.py` classifies `dex2oat`, `dexopt`, `artd`, `installd`, `profman`,
`odrefresh`, `otapreopt`, `update_engine`, `update_verifier`, and `apexd`.
Transient compiler processes are active on presence. Resident daemons require
CPU visibility or runnable/uninterruptible state to avoid false positives.

Consecutive detections are merged into estimated windows. `analysis.py`
integrates measured battery power over those windows and compares it with the
session baseline. The output is explicitly a temporal association, not causal
per-process power attribution.

### Runtime and kernel activity grouping

`parsers.py` attaches `activity_kind`, `activity_family`, `subsystem`, label,
and impact hints to recognized process/thread rows. `analysis.py` aggregates
CPU by activity per snapshot, uses the larger process/thread value when both
views contain the same activity to avoid obvious double counting, and merges
consecutive detections into cadence-aware windows.

For each group it retains sampled CPU, whole-device power, session-baseline
delta, correlation, temperature, PID/TID evidence, and confidence. Thread-only
groups use the hot-thread cadence as their observation domain; missing
non-thread snapshots are not treated as proof of zero activity.

### Android models

- CPU frequency impact from per-cluster Power Profile tables.
- BatteryStats component and UID estimates.
- Screen estimate fallback from Power Profile and brightness.

Model outputs are not forced to add up to measured total power.

### iOS diagnostic evidence

- DiagnosticsService physical battery fields and battery temperature.
- DVT sysmontap whole-device/process CPU, memory, disk, and relative power
  scores.
- DVT Graphics relative Device/Renderer/Tiler utilization.
- DVT application Running/Suspended notifications on Mach uptime.

Physical battery output and DVT relative scores are separate evidence layers.
The report does not synthesize an iOS CPU Power Profile, BatteryStats model,
per-process rail, or unavailable thermal/scheduler state.

### Workflow allocation

- Measured energy by sampled foreground package.
- Measured energy by imported duration event/state.
- Per-test aggregation of energy, mWh/min, average/P95/peak power, CPU/GPU,
  temperature, top processes, classified activity, DEX/update overlap,
  thermal-throttling overlap, and visible-system-CPU share.
- A long-session view aligning power, foreground app, test spans and available
  platform-interference evidence, plus a compact performance-context view for
  refresh residency, sampled frame pacing and touch interaction.
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
| `collector.py` | ADB execution, probing, framing, reconnects, clock capture, isolated system-monitor worker |
| `harmony.py` | HDC discovery/TCP setup, HarmonyOS probing/parsing, framed normalized sampling, reconnects, RenderService/WindowManager/MultimodalInput performance context, platform snapshots |
| `ios.py` | Optional sidecar process orchestration, endpoint cache, normalized event journaling, wireless reconnect |
| `ios_bridge.py` | `pymobiledevice3` USB trust/RemotePairing, RSD, DiagnosticsService and DVT collection |
| `parsers.py` | Android text-to-structure conversion |
| `log_import.py` | Generic host-log parsing and clock alignment |
| `analysis.py` | Measurement conversion, gaps, CPU/GPU/app/phase conclusions |
| `storage.py` | CSV/JSONL/raw journal/checkpoint persistence |
| `report.py` | Downsampled interactive presentation |
| `comparison.py` | Paired run/test-item comparison JSON and standalone Chinese HTML |
| `evidence.py` | Attachment preservation and SHA-256 evidence ZIP generation |
| `ui.py` | Local HTTP API, child-process lifecycle, live journal reader, run history |
| `web/` | Responsive runtime dashboard assets and SVG telemetry charts |
| `cli.py` | User workflows and finalization orchestration |

## Portable distribution

`tools/build-portable.ps1` downloads the official Windows Embedded Python that
matches the build host's Python patch version, copies the pure-Python package
into a bundle-local `site-packages`, updates the embedded `python*._pth`, and
optionally copies the ADB executable and Windows DLLs. The generated launchers
prepend bundled Platform Tools to `PATH`, so the destination machine needs no
Python installation or relocatable virtual environment.

The source UI calls this same script and passes the running interpreter's full
patch version explicitly. This avoids relying on an unrelated `python` command
on `PATH`. The default output is
`dist/mobile-profiler-portable(.zip)`. A portable bundle intentionally
contains documentation and examples but omits the source/build structure, so
future software changes must be rebuilt from the source computer.

The portable builder intentionally does not install or redistribute the
optional GPL-3.0-or-later `pymobiledevice3` runtime. iOS users supply a separate
Python interpreter through global `--ios-python`; Android remains fully
self-contained in the standard portable bundle.

## Evidence and comparison

`import-log` preserves the original BTR2 log and rule file under
`attachments/btr2/`. The `archive` command packages the complete run plus
optional external attachments and writes SHA-256 hashes to an in-archive
manifest. The `compare` command regenerates both source runs, pairs test-item
rows by their phase and stable `comparison_key`, and emits machine-readable
`comparison.json` plus a
standalone Chinese HTML report. Cross-device results retain condition warnings
and never reinterpret process/activity overlap as exclusive rail power.
