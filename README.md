# Mobile Profiler

中文文档：[Mobile Profiler 使用指南（含 BTR2 联动）](docs/usage-zh.md)

A standalone mobile power and performance profiler for long, multi-application
test sessions across Android, HarmonyOS, and iOS. Android uses ADB, native
HarmonyOS uses DevEco Studio's HDC, and both run inside the standard-library
core collector. iOS support is implemented through an optional, separately
installed `pymobiledevice3` sidecar. The profiler has no Python import or
runtime dependency on the BTR2 repository. Its optional ADB vision agent has a
provider-neutral model layer for OpenAI-compatible endpoints, Anthropic Claude,
and Google Gemini. The temporary default remains BTR2's separately deployed
LAN Qwen endpoint.

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
- Foreground package/activity/window, screen state, brightness, and refresh-rate
  residency. Android adds SurfaceFlinger mode-duration deltas plus foreground-window
  SurfaceView/BLAST presented-frame timestamps for native games, with `gfxinfo` UI
  frame-submission, frame-duration, and deadline counters as the normal-View fallback;
  HarmonyOS adds sampled compositor pacing and delivered touch interactions.
- Native HarmonyOS HDC support for BatteryService, `/proc/stat`,
  `hidumper --cpufreq`, AbilityManager, PowerManagerService, `top`/`ps`, and
  ThermalService, plus RenderService display/FPS counters, WindowManager and
  MultimodalInput touch-device evidence. Android-only capabilities remain explicit.
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
- Append-only raw data, checkpoints, ADB/HDC reconnects, and partial-run recovery.
- A local runtime dashboard for device Probe, recording control, live telemetry,
  collector logs, report history, log import, recovery, evidence packaging,
  two-run comparison, and source-project portable builds.
- An Android ADB vision-agent block with a versioned/editable system prompt and
  ordered task-card orchestration. It sends each fresh device screenshot to the
  selected multimodal endpoint, translates its native tool/function response
  into one `phone_action`, executes a validated ADB action, and advances tasks
  under per-task step, timeout, and failure policies.
- An optional **Open Source Automation** dashboard view for deterministic visual
  verification. It reports the image-runtime/disk cost, renders a typed screen
  graph, runs an OpenCV synthetic benchmark, and exposes frame/template/overlay
  evidence without loading a model or accepting executable workflow strings.
- Offline alignment of arbitrary timestamped logs through JSON regex rules.
- Measured energy by foreground app, imported phase/state, per-test item, and
  five-minute window. Each test item includes average/P95/peak power, CPU/GPU,
  temperature, top processes, GC/kworker evidence, DEX/update overlap, and a
  system-interference confidence label.
- A standalone Overview/Timeline/Flow/Test Items/Applications/CPU/System/
  Thermal & Scheduler/GPU/Attribution/Data HTML report.
- Optional iOS 17+ wireless RemoteXPC collection after one trusted-USB
  RemotePairing step: battery current/voltage/temperature, the whole-device
  `PowerTelemetryData.SystemLoad` channel, DVT process CPU/memory/disk and
  relative power scores, GPU utilization, and event-driven foreground-app
  transitions.

Modeled CPU, component, and UID values are kept separate from measured total
battery output. They are diagnostic estimates, not physical power rails.

## Install

Python 3.10+ is required. Android collection needs `adb`. HarmonyOS collection
needs `hdc`, normally installed with DevEco Studio under the OpenHarmony SDK
toolchains directory. The executable can also be supplied with global
`--hdc PATH` or the `HDC` environment variable.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
mobile-profiler --help
```

`mobile-profiler` is the project command.

The Android/HarmonyOS core profiler has no Python runtime dependency outside
the standard library. iOS uses a separate Python environment described below.

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
files. HDC is not redistributed; install DevEco Studio or pass an existing
`hdc.exe` path. On the target computer, extract the ZIP and run `start-ui.bat`.
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
mobile-profiler ui
```

On Windows, you can also double-click `start-ui.bat` in the repository root.
The script prefers `.venv`, falls back to the system Python, and runs the local
source tree without requiring an editable install first. The launcher selects
an available local port automatically, avoiding stale UI processes or another
local service occupying port 8765. Pass `--port PORT` to override it.

The runtime UI provides device discovery and Probe, recording configuration,
start/stop controls, live power/current/CPU/temperature charts, foreground
context, whole-system process/thread activity, ThermalService and scheduler
state, collector logs, and links to completed reports. Its **Tools & Delivery**
view also exposes BTR2 log import, report regeneration, interrupted-run
recovery, evidence ZIP creation with optional attachments, two-phone
comparison, and source-only Windows portable packaging. These operations call
the same existing CLI or evidence workflows, so UI and command-line artifacts
stay identical.

### Provider-neutral ADB vision agent

The **AI Automation** view is an independent MVP workflow for Android. Select
an authorized ADB device, arrange one or more natural-language task cards,
select a multimodal protocol and endpoint/model, and start the workflow. Each
task has its own objective, attention prompt, step/time limit, and
stop-or-continue failure policy. Reusable templates cover returning home,
opening Settings, brightness initialization, and read-only smoke browsing.

The model panel exposes three operation engines for the same task workflow:

- `vision` sends the previous/current screenshots and uses normalized coordinates.
- `uiautomator2` sends a compact, revision-bound semantic control tree without
  images; exact elements are operated through `tap_element`.
- `hybrid` sends both screenshots and the matching control tree. It prefers exact
  semantic elements, while Canvas/OpenGL game content can fall back to visual
  coordinates.

The semantic and hybrid engines use an optional dependency and remain absent from
the standard-library core installation:

```powershell
python -m pip install -e ".[uiautomator2]"
```

All three modes still persist step screenshots as run evidence. See
[`docs/android-automation-engine-benchmark.md`](docs/android-automation-engine-benchmark.md)
for the current Qwen comparison and the two-task validation protocol.

The adapter layer currently supports:

- OpenAI-compatible Chat Completions, including OpenAI, a complete Azure
  deployment URL, local vLLM/Ollama, and compatible model gateways.
- Anthropic Messages API with native image blocks and `tool_use`.
- Google Gemini `generateContent` with native inline images and function calls.

OpenAI-compatible requests have a bounded 1,000-token output budget. The model
panel also exposes an internal-thinking mode: `auto` leaves the server default
unchanged, while `disabled`/`enabled` forwards vLLM-style
`chat_template_kwargs.enable_thinking`. The bundled LAN Qwen default uses
`disabled` so screenshot decisions return one tool call promptly instead of
spending the request timeout on hidden reasoning; other providers remain on
`auto`.

If an OpenAI-compatible response omits the required `phone_action`, the adapter
performs one bounded protocol-repair request against the same screenshot and
task. If that repair also omits the tool call, the workflow now stops safely as
`take_over` instead of crashing with a generic model-protocol error. A
syntactically valid tool call whose action-specific fields are missing (for
example, a swipe without `start`) is recorded as an unexecuted step and fed
back to the next model turn for correction; it is never forwarded to ADB.

The temporary defaults are still the LAN Qwen deployment at
`http://192.168.31.237:8000` and `qwen3.6-27b`; both fields remain editable and
can be supplied through `MOBILE_PROFILER_MODEL_PROVIDER`,
`MOBILE_PROFILER_MODEL_ENDPOINT`, `MOBILE_PROFILER_MODEL_NAME`, and
`MOBILE_PROFILER_MODEL_API_KEY`. The original `BTR2_LLM_*` environment names
remain backward-compatible aliases.

Each step performs this closed loop:

```text
ADB screenshot --------------------\
                                      -> selected engine evidence
uiautomator2 hierarchy (optional) --/   -> editable system/task prompts
                                          -> model adapter
                                          -> one phone_action
                                          -> validated ADB/UIA action
                                          -> next observation
```

Tasks run sequentially. `finish` completes only the current task and advances
the orchestrator; `take_over` stops the whole workflow. A timeout or exhausted
step budget either stops with `task_failed` or is recorded and skipped,
according to the task's failure policy. The editable system prompt defines the
ADB screenshot/coordinate protocol, one-action-per-frame behavior, popup and
input handling, loop prevention, and takeover boundaries. The prompt requires a
full-screen scan before scrolling, visual-anchor verification after
every input event, explicit evidence for intermediate states, and post-action
proof before position/value-change tasks may finish. The current v10 prompt adds
engine-specific evidence rules, revision-bound semantic element IDs, Canvas
fallback constraints, and task-local `finish`. Its built-in version is exposed by
`/api/state` so the UI can restore the default safely.

The model cannot provide arbitrary shell commands. The server only accepts
revision-bound element taps, coordinate taps, double-tap, long-press, swipe, key
events, bounded text input, package launch, wait, finish, and takeover. Vision
text input remains printable ASCII; uiautomator2 and hybrid can use ordinary UTF-8.
Account authorization, CAPTCHA, payment, destructive actions, and other ambiguous
irreversible steps are explicit takeover boundaries. Every run writes
`config.json`, step screenshots, optional UI XML, and `events.jsonl` below
`profiler-runs/agent-runs/`; API keys are neither returned by `/api/state` nor
persisted in those artifacts.

This first PR keeps the agent block separate from power recording. A power run
and an agent may already coexist at the ADB level, but automatic shared
start/stop, timeline markers, power-aware orchestration, and external-API tools
are intentionally reserved for the next integration stage. The task-card and
prompt contracts are designed to host additional phone-initialization recipes
without changing the allowlisted ADB executor.

The automation layer under `mobile_profiler.automation` keeps its contracts and
ports standard-library-only. The dashboard agent now consumes the optional
`Uiautomator2Provider` for semantic observation/action, while policy/approval,
deterministic verifiers, watchers, skills, and the scenario graph remain separate
future integration points. See [`docs/automation-kernel.md`](docs/automation-kernel.md)
for the mechanism mapping and safety boundary.

### Open source automation hub

The **Open Source Automation** view (`#opensource`) is a data-driven catalog for
selecting multiple open-source projects and the automation features provided by
each project. The simulated-universe feature uses the existing external Star Rail
adapter. MaaEnd is also available through an official, user-installed Windows
release and an MXU instance that has already been configured for the selected ADB
device. MaaAssistantArknights remains the next planned integration.

For MaaEnd, extract the official release, create a uniquely named ADB instance in
MaaEnd, enable only tasks whose Project Interface declares ADB support, then enter
the release directory and instance name in the runtime panel. The adapter validates
the AGPL release, MaaFramework/agent files, controller, resource, device binding,
and enabled tasks. Profiles with enabled external pre-actions are rejected. A
successful preflight permits only this fixed one-shot launch:

```text
MaaEnd.exe --autostart --instance "<instance name>" --quit-after-run
```

The values can alternatively default from `MOBILE_PROFILER_MAAEND_ROOT` and
`MOBILE_PROFILER_MAAEND_INSTANCE`. MaaEnd and its agents remain separate upstream
processes; Mobile Profiler does not load their native DLLs or redistribute their
Pipeline/image resources.

The earlier deterministic visual spike is retained under the collapsed adapter
diagnostics section. Install the optional image runtime only when using that
diagnostic:

```powershell
python -m pip install -e ".[image]"
mobile-profiler ui
```

The optional OpenCV/NumPy runtime adds a measured ~159.6 MiB; selecting and saving
a project plan itself adds no heavyweight runtime. The diagnostic still exposes
the typed graph, exact match coordinates, latency, PNG evidence, and adapter
capability alignment. See
[`docs/deterministic-visual-spike.md`](docs/deterministic-visual-spike.md).

### Two-stage Android endurance campaign

The product-level `campaign` runner composes the existing recorder and ADB vision
agent without changing the provider-neutral automation kernel. One JSON defines two
independently executable stages:

```powershell
mobile-profiler campaign validate examples\android-two-stage-campaign.json
mobile-profiler campaign prepare examples\android-two-stage-campaign.json --device IP:PORT
mobile-profiler campaign test examples\android-two-stage-campaign.json --device IP:PORT
```

`prepare` applies allowlisted system settings, installs complete APK/split sets,
grants only explicitly declared permissions, runs per-app first-launch prompts,
and then executes the matching real test workflow with Qwen. A candidate is not
treated as supported until that normal workflow passes; the runner also verifies
that the resumed package is still the target app, converting an off-app false
`finish` into `wrong_foreground`.
`test` records a two-hour session-mode round while cycling configured app/game
workflows, then starts the next round until the Android serial remains unavailable
for the configured grace period. This is the observable ADB shutdown boundary; a
Wi-Fi outage can look identical, so a stable test WLAN is required. See the
[two-stage campaign guide](docs/two-stage-android-campaign.md) for the config schema,
permission/terms boundaries, dry runs, recovery, and output layout.

The same two stages are available in **AI automation → Task launch**. That page is
limited to template selection, repeat mode, live task/step progress, and start/stop.
Workflow name, task order, prompt, attention, step/timeout limits, and failure policy
live under **Prompt editing → Task configuration & prompt**. The endurance repeat
toggle keeps cycling until shutdown/manual stop, or runs the configured scene list
once when disabled. Browser-local template edits are auto-saved, and the edited task
order is forwarded to the Campaign runner. Start/stop is routed to the background
Campaign controller.

The adjacent **Apps & games** tab separates project APK/APKS installs from
store/official-site installs and from `pending_validation` candidates. The current
pending project candidates are 2048, Tetris, Super Snake, and AstroSmash; each has
completed a real-device “main operation + evidence + Home” smoke run. A later
preparation pass dynamically promotes a pending item to supported in the catalog
snapshot. Asteroid is intentionally excluded because its launch path stopped in
Google Play Services instead of reaching gameplay.

The UI has an explicit **Android / iOS / HarmonyOS** platform selector above the
test profile. It filters the device picker and changes connection controls,
field labels, supported capture switches, capability guidance, live metrics,
and performance context before a device is started. Android uses ADB,
BatteryStats, gfxinfo and SurfaceFlinger; iOS uses RemoteXPC, DVT sysmond and
PowerTelemetry; HarmonyOS uses HDC, BatteryService, RenderService, SmartPerf and
the optional power-shell 602 capability-ceiling mode. The server validates that
the manually selected platform matches the selected device, so a visual switch
cannot accidentally launch the wrong collector.

The live page has two capture profiles. **Power** is the default low-overhead
endurance view. **Performance** switches the primary UI to foreground FPS,
frame-time-derived 1% Low, P99 frame time, jank/deadline misses, foreground
window render bounds, explicit MEMC/frame-interpolation evidence, CPU/GPU
allocation, cpusets, governors, ADPF sessions, and thermal state. Cadence ratios
alone are never treated as proof of interpolation because Android cannot
distinguish hardware MEMC from ordinary repeated frames through generic public
interfaces.

Performance tests require a concrete target game or application identifier.
On Android, **Scan phone applications** queries launcher activities on the
selected device, prioritizes third-party apps, supports package/activity search,
and writes the selected package into the existing target field. A manual package
entry remains available for devices that restrict launcher queries. Power mode
keeps the target package optional. Its default duration remains 3720 seconds;
Performance mode defaults to 1920 seconds, with both 30-minute and 32-minute
quick choices for the usual game-test window.

The profiles also change the analysis contract, not only the visible cards.
**Power** explains battery-current and power movement through task load,
scheduler state, display/wireless settings, CPU/GPU clocks, readable
DRAM/DMC/MIF frequency, background activity, BatteryStats, UID, wakelock, and
component evidence. It does not expand the frame-latency path. **Performance**
explains slow frames through VSync start delay, UI traversal/draw,
RenderThread submission, GPU completion, BufferQueue/SurfaceFlinger/HWC
back-pressure, scheduling, and thermal limits. In that mode whole-device power
is retained only as a curve and summary; component, UID, wakelock, foreground
energy, and third-party task power attribution are deliberately disabled.

Capture work can be reduced independently from the selected analysis mode.
The UI provides Power standard, Performance standard, Low overhead, and
Harmony SmartPerf presets, followed by per-feature switches for CPU/GPU/DDR,
frame data, touch, process/thread snapshots, thermal, scheduler, settings, and
power attribution. Disabling a feature skips its collector where the platform
allows it, and the report records the effective configuration and expected
observer overhead. Current, voltage, and device timestamps remain the common
base channel.

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
**iOS RemotePairing** action creates the pairing record while trusted USB is
connected. A reachable RemoteXPC endpoint only proves that telemetry works on
the current route: `169.254/16` and IPv6 link-local addresses can be USB-NCM.
For an unplugged power test, remove USB, refresh discovery, and require the
iPhone to remain reachable through a non-link-local LAN endpoint.

On Windows, **Connect iPhone Bluetooth** can join an already paired iPhone
Personal Hotspot through Bluetooth PAN. Create RemotePairing over USB first,
enable Bluetooth and Personal Hotspot on the iPhone, then click the button. It
uses the PAN gateway as the iPhone address, validates the cached RemotePairing
port, and updates the endpoint cache. If Windows has not paired the phone yet,
the Bluetooth pairing wizard is opened; finish pairing and click the button
again. Bluetooth PAN is sufficient for telemetry but is slower than Wi-Fi.

HarmonyOS targets appear with a `harmony:` prefix. With a USB-authorized phone,
**Harmony Wireless** reads `wlan0`, runs `hdc -t TARGET tmode port 8710`, and
connects `IP:8710`. The address field also accepts
`harmony:192.168.1.20:8710` and dispatches it to `hdc tconn`.

## HarmonyOS HDC workflow

Confirm HDC discovery and create a wireless endpoint while USB is connected:

```powershell
$hdc = "C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"
& $hdc list targets -v
& $hdc -t USB_SERIAL tmode port 8710
& $hdc tconn PHONE_IP:8710
```

After `PHONE_IP:8710` is `Connected`, remove USB and verify BatteryService is
discharging. Probe and record with the prefixed identifier:

```powershell
mobile-profiler --hdc $hdc probe `
  --platform harmony --device harmony:PHONE_IP:8710 --json

mobile-profiler --hdc $hdc record `
  --platform harmony `
  --device harmony:PHONE_IP:8710 `
  --duration 120 `
  --interval 1 `
  --session-mode `
  --require-unplugged `
  --output profiler-runs\harmony-smoke
```

HarmonyOS uses BatteryService current/voltage/temperature, persistent HDC
`/proc/stat`, low-frequency `hidumper --cpufreq`, AbilityManager,
PowerManagerService, `top`/`ps`, and ThermalService. RenderService adds current
and supported refresh modes, session `fpsCount` residency deltas, sampled
compositor FPS/frame intervals and the GPU renderer; WindowManager adds the
focused window; MultimodalInput adds touch-device axes and delivered interaction
counts. The production interface does **not** expose the panel hardware touch
sampling rate, so the report leaves it unavailable instead of inferring 240/300 Hz.
Android BatteryStats, ActivityManager, ADPF, and `dumpsys gpu` are likewise not
relabeled as HarmonyOS evidence.

In Performance mode, the optional **Harmony SmartPerf capture** preset uses the
device `SP_daemon` at its native approximately one-second cadence. When the
device exposes it, this adds target-app FPS/frame jitter, target PID CPU/PSS,
per-core CPU utilization/frequency, GPU utilization/frequency, DDR frequency,
temperature, current, and voltage. It requires a foreground or explicit package
name and is independent of the **Device high-performance mode** switch.

The high-performance switch changes the device policy with
`power-shell setmode 602`; it does not enable SmartPerf collection. It is off by
default, is intended for measuring the device capability ceiling, and can
materially increase battery drain and temperature. The recorder reads the
original mode, applies 602, and restores the original mode after normal
completion, manual stop, or an exception. The report records both application
and restoration status. USB-connected runs are suitable for functional and
frame-performance checks, but USB power invalidates formal whole-device current,
power, and endurance conclusions; use wireless HDC and an unplugged device for
those results.

## iOS wireless workflow

Create a dedicated runtime so the GPL-licensed optional dependency remains
outside the standard-library core and the Android portable bundle:

```powershell
py -3.13 -m venv .venv-ios
.\.venv-ios\Scripts\python.exe -m pip install `
  "pymobiledevice3==9.34.0" "pmd-pytcp==0.0.6"
$iosPython = (Resolve-Path .\.venv-ios\Scripts\python.exe)
```

Use the official CPython 3.13 or newer Windows build. iOS 18.2 and newer removed
the older QUIC tunnel, so pymobiledevice3 must use Python's native TLS-PSK
callback. `pymobiledevice3 9.34.0` also requires the synchronous
`pmd-pytcp 0.0.6` userspace-tunnel API; pip's newer 0.1.x API is incompatible.

When the environment is named `.venv-ios`, `start-ui.bat` validates and detects
it automatically. It also checks `.venv-ios313` and the user-local
`%LOCALAPPDATA%\mobile-profiler\ios-python313` runtime. An incompatible old
sidecar is ignored instead of being passed to the UI. Alternatively set the
`IOS_PYTHON` environment variable before launching the UI; it is validated by
the same check.

On the iPhone, enable Developer Mode, unlock it, connect USB, and trust the
computer. Create RemotePairing once:

```powershell
mobile-profiler --ios-python $iosPython ios-pair --json
```

When the command reports a RemoteXPC endpoint, inspect its scope. A link-local
endpoint is usable for externally powered performance collection but is not
proof that the USB cable can be removed. For an unplugged test, remove USB,
refresh discovery, and continue only if a non-link-local endpoint remains
reachable. Probe and record using the cached `ios:UDID` device identifier:

```powershell
mobile-profiler --ios-python $iosPython probe `
  --platform ios --device ios:00008150-EXAMPLE --json

mobile-profiler --ios-python $iosPython record `
  --platform ios `
  --device ios:00008150-EXAMPLE `
  --duration 120 `
  --session-mode `
  --require-unplugged `
  --output profiler-runs\ios-smoke
```

The RemotePairing record is stored by `pymobiledevice3`; the last working
host/port is cached under `~/.mobile-profiler/ios-devices.json`. The UI keeps
`remote_xpc_ready` separate from `unplug_ready`, so a USB-NCM endpoint is not
presented as a verified Wi-Fi power-test path. The iOS
`PowerTelemetryData.SystemLoad` channel commonly refreshes only about every
20 seconds even though DVT CPU/GPU/process counters update at 0.5-1 second
cadence. The report retains `power_sample_age_s` and `collector_cpu_pct`, and
never converts DVT `powerScore` into mW or mixes it with SystemLoad or the
separate battery current × voltage flow channel.

For long-lived test evidence, keep `profiler-runs` outside the versioned program
directory and pass it explicitly when starting either source or portable UI:

```powershell
.\start-ui.bat --output-root D:\MobileProfilerData\profiler-runs
```

Preview the complete interface with synthetic live telemetry and no phone:

```powershell
mobile-profiler ui --demo
```

Useful UI options:

```powershell
mobile-profiler ui --port 8765 --output-root profiler-runs
mobile-profiler ui --no-browser
```

The default bind address is `127.0.0.1`; the dashboard is local-only unless
you explicitly change `--host`.

## One-hour multi-app workflow

Use Wi-Fi ADB so the USB charging cable can remain disconnected. Confirm the
phone is not externally powered before trusting current and energy values.

```powershell
mobile-profiler probe --device 192.168.21.179:5555

mobile-profiler record `
  --device 192.168.21.179:5555 `
  --duration 3720 `
  --session-mode `
  --require-unplugged `
  --output profiler-runs\btr2-round-001 `
  --title "BTR2 one-hour workflow"
```

Start the profiler first and wait until the live view shows its first telemetry
sample, then start BTR2. The recommended 3720-second window adds two minutes of
headroom around the one-hour workflow. The profiler tracks foreground
application transitions; it does not assume the app visible at session start
remains the target.

After the run, optionally align BTR2's timestamped text log:

```powershell
mobile-profiler import-log `
  profiler-runs\btr2-round-001 `
  C:\path\to\btr2.log `
  --rules examples\btr2-log-rules.json
```

The included BTR2 rule file is only a text-pattern example. It does not import
or call BTR2. Current BTR2 DCL logs use relative elapsed timestamps and cannot
be aligned directly; capture BTR2's absolute-timestamp console output instead.
See the [Chinese usage guide](docs/usage-zh.md) for the verified workflow, and
adjust the timestamp format and regexes if BTR2's log format changes.

The **Performance Context** report view prioritizes refresh-rate residency,
sampled compositor FPS/frame intervals, touch interactions, display/window/GPU
context, and only the most relevant background or thermal anomalies. The
**Test Items** view retains aligned lanes for whole-device power, foreground app,
BTR2 spans and available platform interference evidence.
Clicking a matrix/detail row zooms the chart to that item. If no duration events
were imported, the analyzer falls back to foreground Activity intervals.

## Recovery

Every complete sampler line is flushed to disk immediately. A checkpoint and
clock synchronization point are written every 30 seconds by default and after
reconnects.

```powershell
mobile-profiler recover profiler-runs\btr2-round-001
```

Recovery needs only the run directory. It can finalize data after Ctrl+C,
terminal loss, or an ADB/HDC failure without reconnecting to the phone. Missing
transport intervals are reported as coverage gaps and are excluded from energy
integration rather than interpolated as if they were measured.

## Other commands

```powershell
mobile-profiler probe --device SERIAL
mobile-profiler report RUN_DIR
mobile-profiler demo --output profiler-runs\demo
```

Useful recording options:

- `--test-mode power|performance`: select the capture profile; `power` is the default.
- `--performance-interval 2`: foreground display/window/gfxinfo context cadence used by
  performance mode; detected BLAST game layers are sampled every 0.5 seconds to avoid
  overflowing SurfaceFlinger's short timestamp ring.
- `--capture-preset auto|power-standard|performance-standard|low-overhead|harmony-smartperf`:
  select a collector preset; SmartPerf is available only for HarmonyOS Performance mode.
  The standard presets keep time-series data with direct report consumers enabled. Whole-system
  process/thread scans, scheduler snapshots, delivered-touch/hitch counters, target-process
  snapshots, and BatteryStats model attribution are opt-in diagnostics by default.
  Power Standard keeps CPU/GPU, foreground/display, thermal, and before/after settings evidence;
  Performance Standard adds FPS while leaving detailed `gfxinfo framestats` and system diagnostics
  off by default; enable detailed frame timing only for a targeted investigation.
- `--enable-feature NAME` / `--disable-feature NAME`: repeatable per-feature overrides.
- `--harmony-high-performance`: temporarily apply HarmonyOS
  `power-shell setmode 602`; valid only in HarmonyOS Performance mode.
- `--package PACKAGE`: retain a named target app for BatteryStats/UID analysis.
- `--session-mode`: power mode only; do not default to the starting foreground package.
- `--interval SECONDS`: defaults to 5 seconds for iOS power mode and 1 second for
  iOS performance mode and other platforms; Android BLAST frame timestamps remain
  on their separate 0.5-second sampler.
- `--require-unplugged`: reject connected or unverifiable external-power state; this
  is the default for formal CLI recording.
- `--allow-external-power`: explicitly allow a connected diagnostic run. Supply-powered
  intervals retain raw channels but do not produce consumption, energy, endurance, or
  attribution conclusions.
- `--checkpoint-interval 30`: journal and clock-sync cadence.
- `--reconnect-timeout 120`: maximum device-transport outage before finalizing partial data.
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

Global `--adb PATH`, `--hdc PATH`, and `--ios-python PATH` must appear before
the subcommand.

## Evidence archive and two-phone comparison

`import-log` copies the original BTR2 log and rule file into the run's
`attachments/btr2/` directory. Package the complete auditable run with hashes:

```powershell
mobile-profiler archive RUN_DIR --attach EXTRA_LOG --output RUN-evidence.zip
```

Compare two completed runs after importing their respective BTR2 logs:

```powershell
mobile-profiler compare RUN_A RUN_B `
  --label-a "Phone A" --label-b "Phone B" `
  --output profiler-runs\compare-a-vs-b
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
| Temperature | 10 s | BatteryService |
| Foreground, display modes, Android `gfxinfo` frame counters | 10 s | ActivityManager + Display + gfxinfo |
| Native-game presented frames when a foreground BLAST layer exists | 0.5 s | SurfaceFlinger `--latency` ring |
| Whole-system processes + watched update/DEX services | 10 s | `top` + `ps -A` |
| Hot threads + GC/kworker/kernel classification | 30 s | `top -H` |
| Thermal sensors, severity, cooling, thresholds | 10 s | ThermalService / thermal HAL |
| cpuset, process state, ADPF sessions | 30 s | cgroup + ActivityManager + `performance_hint` |
| Refresh-rate residency + renderer identity | 30 s plus final snapshot | SurfaceFlinger |
| Clock synchronization | 30 s plus lifecycle events | `/proc/uptime` + host midpoint |

For iOS, DVT CPU/GPU/process rows use the selected sample interval, process
snapshots default to 10 seconds, battery diagnostics are polled every 5
seconds, and the underlying `PowerTelemetryData.SystemLoad` value typically
changes about every 20 seconds.

For HarmonyOS, BatteryService and `/proc/stat` use the selected sample interval;
native `hidumper --cpufreq` defaults to 30 seconds because one full dump is
comparatively expensive, while process and
ThermalService snapshots use their configured monitor intervals. The foreground
context cadence (5–10 seconds) also samples RenderService screen/fpsCount/recent
composer timestamps, WindowManager focus, and MultimodalInput delivered events.
The Harmony SmartPerf preset instead follows `SP_daemon`'s fixed approximately
one-second output cadence for its enabled native metrics.
All HarmonyOS rows use the device realtime epoch because production HDC shells
may deny `/proc/uptime`.

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
| GPU frequency/load | Android OEM kernel counter when readable; HarmonyOS SmartPerf can expose SP_daemon GPU frequency/load, while the platform-native Harmony path may expose only renderer identity |
| GPU UID work | Driver activity duration, not electrical energy |
| Harmony SmartPerf FPS/frame jitter | Target-app `SP_daemon` samples at an approximately one-second cadence; not a display-controller hardware counter |
| Harmony high-performance mode | A device policy change to mode 602 for capability-ceiling testing, not a measurement source; compare it with a separate normal-mode run |
| Process/thread CPU | Periodic whole-system snapshots, not a continuous scheduler trace |
| Refresh-rate residency | RenderService `fpsCount` delta, converted to approximate time by count/rate |
| Sampled compositor FPS/frame pacing | Periodic recent RenderService submissions; not app-internal render-loop FPS |
| Touch interactions | Delivered MultimodalInput events; not panel hardware sampling frequency |
| GC/kworker/RCU/IRQ activity | Name-based aggregation of periodic hot-thread/process snapshots; short activity can be missed |
| DEX/update power delta | Battery-side power during detected windows versus the session baseline; temporal association, not per-process attribution |
| Thermal policy | Exposed status, sensors, cooling devices, and static thresholds; the complete OEM decision algorithm may remain private |
| Scheduler state | Exposed cpuset, ActivityManager proc state, and ADPF sessions; production permissions may hide governor/uclamp/sched_debug details |
| App/phase energy | Measured total energy allocated by foreground/log time intervals |
| Per-test system interference | Sampled overlap with classified system/thermal activity and visible system CPU; not exclusive process energy |
| BatteryStats contributors | Android model attribution; values can overlap and need not sum to measured total |
| iOS `SystemLoad` | Whole-device PowerTelemetry channel with a typically ~20 s refresh cadence; under external power it can track `SystemPowerIn` and must not be renamed as battery flow. Repeated rows retain sample age |
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
