(() => {
  "use strict";

  const $ = selector => document.querySelector(selector);
  const $$ = selector => Array.from(document.querySelectorAll(selector));
  const svgNs = "http://www.w3.org/2000/svg";

  const storageMigrations = [
    ["mobile-profiler-platform", "mobile-power-platform"],
    ["mobile-profiler-import-log-path", "android-power-import-log-path"],
    ["mobile-profiler-import-rules-path", "android-power-import-rules-path"],
    ["mobile-profiler-import-match", "android-power-import-match"],
    ["mobile-profiler-archive-attachments", "android-power-archive-attachments"],
    ["mobile-profiler-archive-output", "android-power-archive-output"],
    ["mobile-profiler-portable-output", "android-power-portable-output"],
  ];
  ["android", "ios", "harmony"].forEach(platform => {
    storageMigrations.push([
      `mobile-profiler-address-${platform}`,
      `mobile-power-address-${platform}`,
      ...(platform === "android" ? ["android-power-adb-address"] : []),
    ]);
    storageMigrations.push([
      `mobile-profiler-device-${platform}`,
      `mobile-power-device-${platform}`,
      ...(platform === "android" ? ["android-power-device"] : []),
    ]);
  });
  storageMigrations.forEach(([currentKey, ...legacyKeys]) => {
    if (localStorage.getItem(currentKey) !== null) return;
    const legacyValue = legacyKeys.map(key => localStorage.getItem(key)).find(value => value !== null);
    if (legacyValue !== undefined) localStorage.setItem(currentKey, legacyValue);
  });
  const storedPlatform = localStorage.getItem("mobile-profiler-platform");

  const app = {
    state: null,
    metric: "power_mw",
    testMode: "power",
    platform: ["android", "ios", "harmony"].includes(storedPlatform)
      ? storedPlatform
      : "android",
    polling: false,
    activeView: location.hash.replace("#", "") || "live",
    consoleClearedAt: 0,
    currentRunName: null,
    chartGeometry: null,
    notifiedWarnings: new Set(),
    scannedApps: [],
    scannedAppsDevice: "",
    scannedAppsSource: "",
    selectedScannedPackage: "",
    brightnessDevice: "",
    brightnessInfo: null,
    brightnessError: "",
    brightnessLoading: false,
    brightnessRequestId: 0,
    captureFeaturesOverridden: false,
  };

  const platformProfiles = {
    android: {
      title: "Android 平台",
      description: "ADB、BatteryStats、SurfaceFlinger、gfxinfo 与 Android 调度接口",
      deviceKicker: "ANDROID DEVICE",
      addressLabel: "ADB IP",
      addressPlaceholder: "192.168.1.20:5555",
      connectLabel: "连接 ADB",
      packageLabel: "重点应用包名",
      packagePlaceholder: "com.example.app",
      desktopLabel: "桌面 / Launcher",
      schedulerLabel: "调度上下文",
      performanceIntervalHint: "读取前台窗口上下文；检测到游戏 BLAST 层后以 0.5 秒节奏采集呈现时间戳。",
      probeTitle: "Android 设备能力检查",
      probeDescription: "确认电池供电、电流传感器、CPU policy、GPU 节点、gfxinfo 与 SurfaceFlinger 能力。",
      probePlaceholder: "选择在线 Android 设备后运行 Probe。该操作只读，不会开始采集或重置 BatteryStats。",
      powerDescription: "以电流功率为主，解释任务负载、调度、频率与 Android 设置为何影响续航。",
      performanceDescription: "以 SurfaceFlinger 呈现 FPS、1% Low、帧间隔、gfxinfo 渲染阶段、调度与热限制为主。",
      powerNote: "建议先运行 Probe，确认 powered 为空、BatteryStats 和电流命令可用。",
      performanceNote: "性能模式会读取 BLAST 呈现时间戳并提高窗口、进程和调度快照频率；建议关闭录屏、悬浮窗与其他调试工具。",
    },
    ios: {
      title: "iOS 平台",
      description: "RemoteXPC、DVT sysmond、PowerTelemetry 与事件驱动的前台应用状态",
      deviceKicker: "IOS DEVICE",
      addressLabel: "RemoteXPC",
      addressPlaceholder: "使用 USB 信任与无线配对",
      connectLabel: "创建配对",
      packageLabel: "目标 Bundle ID",
      packagePlaceholder: "com.example.iosapp",
      desktopLabel: "主屏幕 / Home Screen",
      schedulerLabel: "DVT 资源上下文",
      performanceIntervalHint: "iOS 性能数据跟随 DVT 主采样周期，不单独轮询帧窗口。",
      probeTitle: "iOS 设备能力检查",
      probeDescription: "确认 RemoteXPC、PowerTelemetry、DVT CPU/GPU/进程数据和无线端点状态。",
      probePlaceholder: "选择已信任或已完成 RemotePairing 的 iPhone 后运行 Probe；不会修改设备设置。",
      powerDescription: "以物理整机功耗、电流和温度为主，并记录 DVT 进程资源与观察者开销。",
      performanceDescription: "聚焦 DVT CPU/GPU、目标进程、系统负载与观察者开销；当前不提供通用应用 FPS。",
      powerNote: "正式物理功耗测试需完成 RemotePairing 后拔掉 USB；PowerTelemetry 通常约 20 秒更新一次。",
      performanceNote: "iOS 性能模式记录 DVT CPU/GPU/进程资源和整机功耗，不把 powerScore 当作物理功耗。",
    },
    harmony: {
      title: "HarmonyOS 平台",
      description: "HDC、BatteryService、RenderService、SmartPerf 与 power-shell 设备策略",
      deviceKicker: "HARMONYOS DEVICE",
      addressLabel: "HDC IP",
      addressPlaceholder: "192.168.1.20:8710",
      connectLabel: "连接 HDC",
      packageLabel: "重点应用包名",
      packagePlaceholder: "com.example.harmonyapp",
      desktopLabel: "桌面 / Launcher",
      schedulerLabel: "CPU / 调度上下文",
      performanceIntervalHint: "读取 RenderService/前台窗口；SmartPerf 使用设备原生约 1 秒节奏。",
      probeTitle: "HarmonyOS 设备能力检查",
      probeDescription: "确认 BatteryService、CPU/GPU/DDR、RenderService、SmartPerf 和 power-shell 能力。",
      probePlaceholder: "选择在线 HarmonyOS HDC 设备后运行 Probe；该操作只读，不会切换高性能模式。",
      powerDescription: "以 BatteryService 电流功率为主，解释 CPU/GPU/DDR、任务负载与系统状态。",
      performanceDescription: "以 SmartPerf/RenderService 帧表现、CPU/GPU/DDR、调度、热限制和 602 能力上限为主。",
      powerNote: "正式功耗测试请使用无线 HDC 并拔掉 USB，确认 BatteryService 处于放电状态。",
      performanceNote: "SmartPerf 采集与设备 602 高性能模式相互独立；能力上限测试会明显增加功耗和温度。",
    },
  };

  const platformUnavailableFeatures = {
    android: new Set(["harmony_hitches"]),
    harmony: new Set(["hot_threads", "runtime_settings", "power_attribution"]),
    ios: new Set([
      "cpu_frequency", "memory_frequency", "frame_rate", "frame_details",
      "harmony_hitches", "touch_events", "hot_threads", "scheduler",
      "runtime_settings", "power_attribution",
    ]),
  };

  const metricDefinitions = {
    power_mw: {
      title: "实时功率",
      legend: "Battery power",
      color: "#ffb45c",
      value: point => finite(point.power_mw) ? Number(point.power_mw) / 1000 : null,
      unit: "W",
      digits: 3,
      axis: { fixedMin: 0, minSpan: .5, padding: .08, tickDigits: 2 },
    },
    current_ma: {
      title: "放电电流",
      legend: "Current magnitude",
      color: "#4bc6e8",
      value: point => finite(point.current_ma) ? Number(point.current_ma) : null,
      unit: "mA",
      digits: 0,
      axis: { fixedMin: 0, minSpan: 200, padding: .08, tickDigits: 0 },
    },
    cpu_pct: {
      title: "CPU 总负载",
      legend: "CPU utilization",
      color: "#35d49a",
      value: point => finite(point.cpu_pct) ? Number(point.cpu_pct) : null,
      unit: "%",
      digits: 1,
      axis: { fixedMin: 0, fixedMax: 100, tickStep: 25, tickDigits: 0 },
    },
    memory_frequency_mhz: {
      title: "内存 / DMC 频率",
      legend: "Memory frequency",
      color: "#f1d267",
      value: point => finite(point.memory_frequency_mhz) ? Number(point.memory_frequency_mhz) : null,
      unit: "MHz",
      digits: 0,
      axis: { fixedMin: 0, minSpan: 400, padding: .08, tickDigits: 0 },
    },
    temperature_c: {
      title: "电池温度",
      legend: "Battery temperature",
      color: "#ff657d",
      value: point => finite(point.temperature_c) ? Number(point.temperature_c) : null,
      unit: "°C",
      digits: 1,
      axis: { minSpan: 6, padding: .04, tickDigits: 1 },
    },
    voltage_mv: {
      title: "电池电压",
      legend: "Battery voltage",
      color: "#9e8cff",
      value: point => finite(point.voltage_mv) ? Number(point.voltage_mv) / 1000 : null,
      unit: "V",
      digits: 3,
      axis: { minSpan: .3, padding: .04, tickDigits: 2 },
    },
    frame_rate_fps: {
      title: "实时帧率",
      legend: "Foreground frame rate",
      color: "#4bc6e8",
      value: point => finite(point.frame_rate_fps) ? Number(point.frame_rate_fps) : null,
      unit: "FPS",
      digits: 1,
      series: "performance",
      secondaryLabel: "1% LOW",
      secondaryQuantile: .01,
      axis: { fixedMin: 0, minSpan: 30, padding: .08, tickDigits: 0 },
      reference: active => {
        const refreshRate = Number(active?.performance?.current_refresh_rate_hz);
        return finite(refreshRate)
          ? { value: refreshRate, label: `刷新率 ${formatAxisNumber(refreshRate, 0)} Hz` }
          : null;
      },
    },
    frame_time_ms: {
      title: "帧耗时 P95",
      legend: "Frame time",
      color: "#9e8cff",
      value: point => finite(point.frame_time_p95_ms) ? Number(point.frame_time_p95_ms) : finite(point.frame_time_average_ms) ? Number(point.frame_time_average_ms) : null,
      unit: "ms",
      digits: 2,
      series: "performance",
      secondaryLabel: "P99",
      secondaryQuantile: .99,
      axis: { fixedMin: 0, minSpan: 10, padding: .08, tickDigits: 1 },
      reference: active => {
        const refreshRate = Number(active?.performance?.current_refresh_rate_hz);
        return finite(refreshRate) && refreshRate > 0
          ? {
            value: 1000 / refreshRate,
            label: `帧预算 ${formatAxisNumber(1000 / refreshRate, 2)} ms`,
          }
          : null;
      },
    },
    gpu_load_pct: {
      title: "GPU 负载",
      legend: "GPU utilization",
      color: "#35d49a",
      value: point => finite(point.gpu_load_pct) ? Number(point.gpu_load_pct) : null,
      unit: "%",
      digits: 1,
      axis: { fixedMin: 0, fixedMax: 100, tickStep: 25, tickDigits: 0 },
    },
    gpu_frequency_mhz: {
      title: "GPU 频率",
      legend: "GPU frequency",
      color: "#ffb45c",
      value: point => finite(point.gpu_frequency_mhz) ? Number(point.gpu_frequency_mhz) : null,
      unit: "MHz",
      digits: 0,
      axis: { fixedMin: 0, minSpan: 400, padding: .08, tickDigits: 0 },
    },
  };

  const statusDefinitions = {
    starting: ["正在准备", "SESSION STARTING"],
    collecting: ["正在采集", "LIVE CAPTURE"],
    recording: ["正在采集", "LIVE CAPTURE"],
    stopping: ["正在收尾", "FINALIZING"],
    complete: ["采集完成", "SESSION COMPLETE"],
    collected: ["采集完成", "SESSION COMPLETE"],
    partial: ["部分完成", "PARTIAL SESSION"],
    interrupted: ["已中断", "INTERRUPTED"],
    recovered: ["已恢复", "RECOVERED"],
    failed: ["采集失败", "SESSION FAILED"],
    demo: ["演示数据", "DEMO TELEMETRY"],
  };

  const thermalStatusDefinitions = {
    0: "正常",
    1: "轻度",
    2: "中度",
    3: "严重",
    4: "危急",
    5: "紧急",
    6: "关机",
  };

  const bclSensorUnits = {
    6: "V",
    7: "A",
    8: "%",
  };

  function isThermalTemperature(item) {
    return !Object.prototype.hasOwnProperty.call(bclSensorUnits, Number(item?.type));
  }

  function thermalSensorUnit(item) {
    return bclSensorUnits[Number(item?.type)] || "°C";
  }

  function finite(value) {
    return value !== null && value !== undefined && Number.isFinite(Number(value));
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function formatAxisNumber(value, maximumDigits = 0) {
    const threshold = 10 ** -(maximumDigits + 1);
    const normalized = Math.abs(Number(value)) < threshold ? 0 : Number(value);
    return normalized.toLocaleString("zh-CN", {
      minimumFractionDigits: 0,
      maximumFractionDigits: maximumDigits,
      useGrouping: Math.abs(normalized) >= 1000,
    });
  }

  function niceChartStep(rawStep) {
    if (!finite(rawStep) || Number(rawStep) <= 0) return 1;
    const exponent = Math.floor(Math.log10(Number(rawStep)));
    const magnitude = 10 ** exponent;
    const normalized = Number(rawStep) / magnitude;
    const factor = normalized <= 1.5
      ? 1
      : normalized <= 2.25
        ? 2
        : normalized <= 3.5
          ? 2.5
          : normalized <= 7.5
            ? 5
            : 10;
    return factor * magnitude;
  }

  function buildChartScale(values, definition) {
    const axis = definition.axis || {};
    const observedMin = Math.min(...values);
    const observedMax = Math.max(...values);
    let minimum = finite(axis.fixedMin) ? Number(axis.fixedMin) : observedMin;
    let maximum = finite(axis.fixedMax) ? Number(axis.fixedMax) : observedMax;

    const minimumSpan = Math.max(0, Number(axis.minSpan || 0));
    if (maximum - minimum < minimumSpan) {
      if (finite(axis.fixedMin) && !finite(axis.fixedMax)) {
        maximum = minimum + minimumSpan;
      } else if (finite(axis.fixedMax) && !finite(axis.fixedMin)) {
        minimum = maximum - minimumSpan;
      } else {
        const midpoint = (minimum + maximum) / 2;
        minimum = midpoint - minimumSpan / 2;
        maximum = midpoint + minimumSpan / 2;
      }
    }

    const padding = Math.max(0, Number(axis.padding ?? .08));
    const span = Math.max(Number.EPSILON, maximum - minimum);
    if (!finite(axis.fixedMin)) minimum -= span * padding;
    if (!finite(axis.fixedMax)) maximum += span * padding;

    const targetIntervals = Math.max(3, Number(axis.targetIntervals || 4));
    const tickStep = finite(axis.tickStep)
      ? Number(axis.tickStep)
      : niceChartStep((maximum - minimum) / targetIntervals);
    const scaleMin = finite(axis.fixedMin)
      ? Number(axis.fixedMin)
      : Math.floor((minimum + tickStep * 1e-9) / tickStep) * tickStep;
    let scaleMax = finite(axis.fixedMax)
      ? Number(axis.fixedMax)
      : Math.ceil((maximum - tickStep * 1e-9) / tickStep) * tickStep;
    if (scaleMax <= scaleMin) scaleMax = scaleMin + tickStep;

    const ticks = [];
    const tickCount = Math.round((scaleMax - scaleMin) / tickStep);
    for (let index = 0; index <= tickCount; index += 1) {
      ticks.push(scaleMin + index * tickStep);
    }
    return {
      minimum: scaleMin,
      maximum: scaleMax,
      tickDigits: Math.max(0, Number(axis.tickDigits ?? definition.digits ?? 0)),
      ticks,
    };
  }

  function buildTimeTicks(minimum, maximum, targetIntervals) {
    const span = Math.max(.001, maximum - minimum);
    const candidates = [
      1, 2, 5, 10, 15, 30,
      60, 120, 300, 600, 900, 1800,
      3600, 7200, 14400, 28800, 43200,
    ];
    const rawStep = span / Math.max(2, targetIntervals);
    const step = candidates.find(candidate => candidate >= rawStep)
      || niceChartStep(rawStep);
    const ticks = [minimum];
    const firstInner = Math.ceil((minimum + step * .2) / step) * step;
    for (let value = firstInner; value < maximum - step * .2; value += step) {
      ticks.push(value);
    }
    if (maximum - ticks.at(-1) > .001) ticks.push(maximum);
    return ticks;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatNumber(value, digits = 1) {
    return finite(value) ? Number(value).toFixed(digits) : "--";
  }

  function formatMetric(value, unit, digits = 1) {
    return finite(value) ? `${Number(value).toFixed(digits)} ${unit}` : "--";
  }

  function formatBytes(value) {
    if (!finite(value)) return "--";
    const bytes = Number(value);
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${bytes.toFixed(0)} B`;
  }

  function selectedPlatform() {
    return app.platform || "android";
  }

  function applyPlatformVisibility() {
    $$('[data-platforms]').forEach(element => {
      const platforms = String(element.dataset.platforms || "").split(/\s+/).filter(Boolean);
      element.classList.toggle("hidden", !platforms.includes(selectedPlatform()));
    });
  }

  function updatePlatformCounts(devices = app.state?.devices || []) {
    ["android", "ios", "harmony"].forEach(platform => {
      const count = Array.isArray(devices)
        ? devices.filter(device => devicePlatform(device) === platform && device.state === "device").length
        : 0;
      const target = $(`#platform-count-${platform}`);
      if (target) target.textContent = String(count);
    });
  }

  function updatePlatformPresentation() {
    const platform = selectedPlatform();
    const profile = platformProfiles[platform];
    document.body.dataset.platform = platform;
    $("#platform-title").textContent = profile.title;
    $("#platform-description").textContent = profile.description;
    $("#device-picker-kicker").textContent = profile.deviceKicker;
    $("#device-select").setAttribute("aria-label", `${platformLabel(platform)} 设备`);
    $("#connection-address-label").textContent = profile.addressLabel;
    $("#adb-address-input").placeholder = profile.addressPlaceholder;
    $("#connect-address").textContent = profile.connectLabel;
    const performance = app.testMode === "performance";
    const targetLabel = performance
      ? platform === "ios" ? "目标游戏 Bundle ID" : "目标游戏 / 应用包名"
      : profile.packageLabel;
    $("#package-input-label").innerHTML = `${targetLabel} <span>${performance ? "必填" : "可选"}</span>`;
    $("#package-input").placeholder = profile.packagePlaceholder;
    $("#package-input").setAttribute("aria-required", performance ? "true" : "false");
    $("#package-input-hint").textContent = performance
      ? platform === "android"
        ? "从上方扫描结果选择后会自动填写；扫描失败时也可手工输入包名。"
        : `性能测试必须填写目标${platform === "ios" ? "游戏 Bundle ID" : "游戏 / 应用包名"}。`
      : "功耗测试可留空；填写后会保留目标应用的资源与归因上下文。";
    $("#start-context-input").querySelector('option[value="desktop"]').textContent = profile.desktopLabel;
    $("#scheduler-interval-label").textContent = profile.schedulerLabel;
    $("#performance-interval-hint").textContent = profile.performanceIntervalHint;
    $("#probe-view-title").textContent = profile.probeTitle;
    $("#probe-view-description").textContent = profile.probeDescription;
    $("#probe-placeholder-copy").textContent = profile.probePlaceholder;
    $("#power-mode-subtitle").textContent = platform === "ios"
      ? "PowerTelemetry / 电流"
      : platform === "harmony" ? "BatteryService / 电流" : "默认 · 电流 / 功率";
    $("#performance-mode-subtitle").textContent = platform === "ios"
      ? "CPU / GPU / 进程"
      : platform === "harmony" ? "SmartPerf / 1% Low / 602" : "FPS / 1% Low / 调度";
    const ios = platform === "ios";
    const harmony = platform === "harmony";
    $("#metric-fps-label").textContent = ios ? "CPU 总负载" : "实时帧率";
    $("#metric-fps-tag").textContent = ios ? "CPU" : "FPS";
    $("#metric-one-low-label").textContent = ios ? "GPU 利用率" : "1% Low";
    $("#metric-one-low-tag").textContent = ios ? "GPU" : "LOW";
    $("#metric-frame-p99-label").textContent = ios ? "采集器开销" : "P99 帧耗时";
    $("#metric-frame-p99-tag").textContent = ios ? "OVERHEAD" : "FRAME";
    $("#metric-frame-issue-label").textContent = ios ? "整机功耗" : "异常帧";
    $("#metric-frame-issue-tag").textContent = ios ? "POWER" : "JANK";
    $("#metric-render-resolution-label").textContent = ios ? "前台应用" : "渲染分辨率";
    $("#metric-render-resolution-tag").textContent = ios ? "APP" : "RENDER";
    $("#metric-interpolation-label").textContent = ios ? "电池温度" : "插帧状态";
    $("#metric-interpolation-tag").textContent = ios ? "THERMAL" : "MEMC";
    $("#metric-memory-label").textContent = ios ? "采集器开销" : "内存频率";
    $("#metric-memory-tag").textContent = ios ? "OVERHEAD" : "DMC";

    $("#cluster-panel-title").textContent = ios
      ? "DVT 进程 CPU"
      : app.testMode === "performance" ? "CPU 调度分配" : "集群状态";
    $("#cluster-panel-source").textContent = ios
      ? "DVT sysmond"
      : app.testMode === "performance" ? "load / frequency / topology" : "kernel counters";
    $("#resource-panel-kicker").textContent = ios ? "DVT RESOURCE TELEMETRY" : harmony ? "HARMONY RESOURCE TELEMETRY" : "RESOURCE SCHEDULING";
    $("#resource-panel-title").textContent = ios ? "iOS 性能资源" : harmony ? "HarmonyOS 资源调度" : "资源调度分配";
    $("#resource-window-label").textContent = ios ? "前台应用" : "前台窗口";
    $("#performance-evidence-label-1").textContent = ios ? "性能数据源" : "帧率来源";
    $("#performance-evidence-label-2").textContent = ios ? "整机功耗来源" : "渲染尺寸来源";
    $("#performance-evidence-label-3").textContent = ios ? "观察者开销" : "插帧判定";

    $("#performance-view-kicker").textContent = ios ? "IOS DVT PERFORMANCE CONTEXT" : harmony ? "HARMONY FRAME PERFORMANCE" : "FRAME PERFORMANCE CONTEXT";
    $("#performance-view-title").textContent = ios ? "iOS 资源与系统负载" : harmony ? "HarmonyOS 帧表现与资源调度" : "帧表现与资源调度";
    $("#performance-view-description").textContent = ios
      ? "围绕 DVT CPU/GPU、目标进程、物理整机功耗、温度和采集器开销分析性能。"
      : harmony
        ? "围绕 SmartPerf/RenderService 帧节奏、1% Low、CPU/GPU/DDR、调度、热限制和 602 能力上限分析性能。"
        : "围绕刷新率、1% Low、帧延迟、渲染链路、CPU/GPU/内存资源、调度和热限制分析游戏性能。";
    const statLabels = ios
      ? ["CPU LOAD", "GPU LOAD", "SYSTEM LOAD", "OBSERVER CPU"]
      : ["REFRESH RATE", "FRAME RATE", "FRAME P95", "TOUCHES"];
    statLabels.forEach((label, index) => { $(`#performance-stat-label-${index + 1}`).textContent = label; });
    $("#performance-list-kicker").textContent = ios ? "DVT RESOURCE SUMMARY" : "REFRESH RESIDENCY";
    $("#performance-list-title").textContent = ios ? "资源采样摘要" : "刷新档位驻留";
    $("#performance-switch-label").textContent = ios ? "前台状态" : "刷新切换";
    $("#performance-frame-count-label").textContent = ios ? "进程快照" : "采样帧数";
    $("#performance-window-label").textContent = ios ? "前台应用" : "前台窗口";
    $("#performance-display-label").textContent = ios ? "显示参数" : "分辨率 / 亮度";
    $("#interference-table-kicker").textContent = ios ? "BACKGROUND RESOURCE WATCH" : harmony ? "HARMONY RESOURCE INTERFERENCE" : "SCHEDULING INTERFERENCE";
    $("#interference-table-title").textContent = ios ? "后台资源异常" : harmony ? "资源竞争异常" : "调度竞争异常";
    $("#performance-observability-note").innerHTML = ios
      ? "<strong>边界：</strong>iOS 当前页面展示 DVT 诊断遥测和 PowerTelemetry 整机物理功耗，不提供通用应用 FPS、Core Animation 详细帧时间戳或 Android 调度接口。"
      : harmony
        ? "<strong>边界：</strong>HarmonyOS 使用 SmartPerf/RenderService 的应用帧抖动与合成节奏；量产 HDC 不提供 Android gfxinfo 的逐阶段 framestats，整机功耗只记录趋势和汇总。"
        : "<strong>边界：</strong>原生游戏优先使用前台 SurfaceView/BLAST 的 SurfaceFlinger 呈现时间戳，普通 View 应用回退 gfxinfo；详细 framestats 用于分析 UI / RenderThread 到 BufferQueue 的延迟。整机功耗只记录趋势和汇总，不拆分第三方任务、UID 或组件来源。";
    $("#power-pressure-kicker").textContent = ios ? "IOS POWER OBSERVABILITY" : harmony ? "HARMONY POWER PRESSURE" : "POWER PRESSURE";
    $("#power-pressure-title").textContent = ios ? "iOS 功耗与观察者开销" : harmony ? "HarmonyOS 功耗压力解释" : "功耗压力解释";
    $("#power-pressure-source").textContent = ios ? "PowerTelemetry / DVT / observer" : harmony ? "CPU / GPU / DDR / 进程 / 系统状态" : "负载 / 频率 / 设置与整机功率";
    $("#power-pressure-driver-title").textContent = ios ? "物理功耗" : "资源驱动";
    $("#power-pressure-task-title").textContent = ios ? "进程资源" : "任务负载";
    $("#power-pressure-setting-title").textContent = ios ? "采集器影响" : harmony ? "系统状态" : "设置参数";
    $("#power-pressure-note").innerHTML = ios
      ? "<strong>边界：</strong>PowerTelemetry SystemLoad 是整机物理功耗；DVT powerScore 仅为相对诊断分数，collector_cpu_pct 用于记录工具自身开销。"
      : harmony
        ? "<strong>边界：</strong>BatteryService 电流是整机电池侧数据；CPU/GPU/DDR 与进程相关性用于解释趋势，不等于独立电源轨或 Android BatteryStats 归因。"
        : "<strong>边界：</strong>相关系数用于解释电流 / 功率和资源压力是否同步变化，不代表独立硬件电源轨，也不能把多个压力项直接相加。";
    applyPlatformVisibility();
  }

  function setPlatform(platform, { fromRun = false, initial = false } = {}) {
    const nextPlatform = ["android", "ios", "harmony"].includes(platform) ? platform : "android";
    const active = app.state?.active;
    if (!fromRun && active?.running && nextPlatform !== activePlatform(active)) {
      notify("测试进行中无法切换平台", "停止当前测试后再选择其他平台。", "error");
      return;
    }
    const previousPlatform = app.platform;
    app.platform = nextPlatform;
    if (previousPlatform !== nextPlatform) {
      app.brightnessRequestId += 1;
      app.brightnessDevice = "";
      app.brightnessInfo = null;
      app.brightnessError = "";
      app.brightnessLoading = false;
    }
    if (!fromRun) localStorage.setItem("mobile-profiler-platform", nextPlatform);
    $$(".platform-switch [data-platform]").forEach(button => {
      const selected = button.dataset.platform === nextPlatform;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-checked", selected ? "true" : "false");
      button.disabled = Boolean(active?.running);
    });
    updatePlatformPresentation();
    if (initial || previousPlatform !== nextPlatform) {
      const savedAddress = localStorage.getItem(`mobile-profiler-address-${nextPlatform}`);
      $("#adb-address-input").value = savedAddress || "";
    }
    if (!fromRun && previousPlatform !== nextPlatform) {
      resetAppScanner({ clearPackage: true });
      app.metric = app.testMode === "performance"
        ? nextPlatform === "ios" ? "cpu_pct" : "frame_rate_fps"
        : "power_mw";
    }
    if (nextPlatform !== "harmony" && $("#capture-preset-input")?.value === "harmony-smartperf") {
      $("#capture-preset-input").value = "auto";
    }
    if (!fromRun && previousPlatform !== nextPlatform) applyCapturePreset();
    else updateCaptureFeatureControls();
    if (!initial && app.state) {
      renderDevices(app.state);
      renderProbe(app.state);
      setTestMode(app.testMode, { initial: true });
      renderPerformanceMetrics(app.state.active);
      renderPerformanceResources(app.state.active);
      renderSystem(app.state.active);
      renderSchedulerHistory(app.state.active);
      renderChart();
    }
  }

  function activePlatform(active) {
    const selected = Array.isArray(app.state?.devices)
      ? app.state.devices.find(device => device.serial === selectedDevice())
      : null;
    return String(active?.platform || active?.metadata?.platform || selected?.platform || selectedPlatform()).toLowerCase();
  }

  function formatDuration(value) {
    const total = Math.max(0, Math.round(Number(value) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    if (hours) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function formatShortDuration(value) {
    const seconds = Math.max(0, Number(value) || 0);
    if (seconds >= 3600) return `${(seconds / 3600).toFixed(seconds >= 36000 ? 0 : 1)} h`;
    if (seconds >= 60) return `${(seconds / 60).toFixed(seconds >= 600 ? 0 : 1)} min`;
    return `${seconds.toFixed(0)} s`;
  }

  function formatDate(epochSeconds) {
    if (!finite(epochSeconds)) return "--";
    return new Date(Number(epochSeconds) * 1000).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function syncDurationPreset() {
    const duration = Number($("#duration-input")?.value);
    $$('[data-duration]').forEach(button => {
      button.classList.toggle("active", Number(button.dataset.duration) === duration);
    });
  }

  const modeDefaults = {
    power: {
      "duration-input": 3720,
      "interval-input": 1,
      "process-interval-input": 10,
      "thread-interval-input": 30,
      "thermal-interval-input": 10,
      "scheduler-interval-input": 30,
    },
    performance: {
      "duration-input": 1920,
      "interval-input": .5,
      "process-interval-input": 2,
      "thread-interval-input": 5,
      "thermal-interval-input": 5,
      "scheduler-interval-input": 5,
    },
  };

  const captureFeatureNames = [
    "cpu_usage", "cpu_frequency", "gpu_metrics", "memory_frequency",
    "foreground_window", "frame_rate", "frame_details", "harmony_hitches",
    "touch_events", "target_process", "process_snapshots", "hot_threads",
    "thermal", "scheduler", "runtime_settings", "power_attribution",
  ];

  const capturePresetFeatures = {
    "power-standard": new Set([
      "cpu_usage", "cpu_frequency", "gpu_metrics",
      "foreground_window", "target_process", "process_snapshots", "hot_threads",
      "thermal", "scheduler", "runtime_settings", "power_attribution",
    ]),
    "performance-standard": new Set([
      "cpu_usage", "cpu_frequency", "gpu_metrics",
      "foreground_window", "frame_rate", "frame_details", "harmony_hitches",
      "touch_events", "target_process", "process_snapshots", "hot_threads",
      "thermal", "scheduler",
    ]),
    "harmony-smartperf": new Set([
      "cpu_usage", "cpu_frequency", "gpu_metrics",
      "foreground_window", "frame_rate", "frame_details", "target_process", "thermal",
    ]),
  };

  function selectedDeviceInfo() {
    return (app.state?.devices || []).find(device => device.serial === selectedDevice()) || null;
  }

  function androidBrightnessDevice(device = selectedDeviceInfo()) {
    return selectedPlatform() === "android"
      && devicePlatform(device) === "android"
      && device?.state === "device"
      ? device
      : null;
  }

  function renderBrightnessControl(device = selectedDeviceInfo()) {
    const input = $("#brightness-input");
    const refreshButton = $("#brightness-refresh");
    const applyButton = $("#brightness-apply");
    const badge = $("#brightness-current-badge");
    const hint = $("#brightness-hint");
    if (!input || !refreshButton || !applyButton || !badge || !hint) return;
    const readyDevice = androidBrightnessDevice(device);
    const recording = Boolean(app.state?.active?.running);
    const disabled = !readyDevice || recording || app.brightnessLoading;
    input.disabled = disabled;
    refreshButton.disabled = disabled;
    applyButton.disabled = disabled;
    if (!readyDevice) {
      badge.textContent = "等待设备";
      badge.classList.remove("ready");
      input.value = "";
      hint.textContent = "选择在线 Android 设备后读取最小值、最大值和最小调整间隔；应用时会切换为手动亮度。";
      return;
    }
    if (recording) {
      badge.textContent = "采集中锁定";
      badge.classList.remove("ready");
      hint.textContent = "为避免改变本轮测试条件，采集期间不能修改亮度。";
      return;
    }
    if (app.brightnessLoading) {
      badge.textContent = "读取中";
      badge.classList.remove("ready");
      hint.textContent = "正在读取 Android 亮度当前值和设备范围…";
      return;
    }
    const info = app.brightnessDevice === readyDevice.serial ? app.brightnessInfo : null;
    if (!info) {
      badge.textContent = app.brightnessError ? "读取失败" : "等待读取";
      badge.classList.remove("ready");
      hint.textContent = app.brightnessError
        ? `${app.brightnessError}；可点击“读取范围”重试。`
        : "点击“读取范围”获取最小值、最大值和最小调整间隔。";
      return;
    }
    input.min = String(info.minimum);
    input.max = String(info.maximum);
    input.step = String(info.step || 1);
    if (document.activeElement !== input || !input.value) input.value = String(info.current);
    badge.textContent = `当前 ${info.current}`;
    badge.classList.add("ready");
    const normalizedStep = finite(info.normalized_step)
      ? Number(info.normalized_step).toFixed(5).replace(/0+$/, "").replace(/\.$/, "")
      : "--";
    hint.textContent = `最小值 ${info.minimum} · 最大值 ${info.maximum} · 最小调整间隔 ${info.step || 1} 级（归一化约 ${normalizedStep}） · ${info.automatic ? "当前为自动亮度，应用时会切换手动" : "当前为手动亮度"}`;
  }

  async function refreshBrightnessCapability({ force = false, notifyFailure = false } = {}) {
    const device = androidBrightnessDevice();
    if (!device) {
      renderBrightnessControl();
      return null;
    }
    if (!force && app.brightnessDevice === device.serial && (app.brightnessInfo || app.brightnessError)) {
      renderBrightnessControl(device);
      return app.brightnessInfo;
    }
    const requestId = ++app.brightnessRequestId;
    app.brightnessDevice = device.serial;
    app.brightnessInfo = null;
    app.brightnessError = "";
    app.brightnessLoading = true;
    renderBrightnessControl(device);
    try {
      const info = await api("/api/brightness", {
        method: "POST",
        body: JSON.stringify({ device: device.serial, platform: "android", action: "read" }),
      });
      if (requestId !== app.brightnessRequestId || selectedDevice() !== device.serial) return null;
      app.brightnessInfo = info;
      return info;
    } catch (error) {
      if (requestId !== app.brightnessRequestId) return null;
      app.brightnessError = error.message || "无法读取设备亮度";
      if (notifyFailure) notify("亮度范围读取失败", app.brightnessError, "error", 7000);
      return null;
    } finally {
      if (requestId === app.brightnessRequestId) {
        app.brightnessLoading = false;
        renderBrightnessControl();
      }
    }
  }

  async function applyBrightnessValue() {
    const device = androidBrightnessDevice();
    const input = $("#brightness-input");
    const info = app.brightnessInfo;
    if (!device || !input || !info) {
      notify("无法设置亮度", "请先选择在线 Android 设备并读取亮度范围。", "error");
      return;
    }
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < Number(info.minimum) || value > Number(info.maximum)) {
      notify(
        "亮度数值无效",
        `请输入 ${info.minimum}–${info.maximum} 之间的整数，最小调整间隔为 ${info.step || 1}。`,
        "error",
      );
      return;
    }
    const requestId = ++app.brightnessRequestId;
    app.brightnessLoading = true;
    app.brightnessError = "";
    renderBrightnessControl(device);
    try {
      const result = await api("/api/brightness", {
        method: "POST",
        body: JSON.stringify({
          device: device.serial,
          platform: "android",
          action: "set",
          value,
        }),
      });
      if (requestId !== app.brightnessRequestId) return;
      app.brightnessInfo = result;
      const detail = `当前 ${result.current} · 最小 ${result.minimum} · 最大 ${result.maximum} · 间隔 ${result.step}`;
      notify(
        result.applied ? "亮度已应用" : "亮度值已写入",
        `${detail}${result.manual_mode_changed ? " · 已切换为手动亮度" : ""}`,
        result.applied ? "success" : "error",
        6000,
      );
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      if (warnings.length) notify("亮度应用提示", warnings.join("；"), "error", 8000);
    } catch (error) {
      if (requestId !== app.brightnessRequestId) return;
      app.brightnessError = error.message || "无法修改设备亮度";
      notify("亮度修改失败", app.brightnessError, "error", 8000);
    } finally {
      if (requestId === app.brightnessRequestId) {
        app.brightnessLoading = false;
        renderBrightnessControl();
      }
    }
  }

  function effectiveCapturePreset() {
    const requested = $("#capture-preset-input")?.value || "auto";
    if (requested !== "auto") return requested;
    return app.testMode === "performance" ? "performance-standard" : "power-standard";
  }

  function presetFeatureSet(preset) {
    if (preset !== "low-overhead") return capturePresetFeatures[preset] || new Set();
    return new Set(app.testMode === "performance"
      ? ["cpu_usage", "foreground_window", "frame_rate", "target_process", "thermal"]
      : ["cpu_usage", "foreground_window"]);
  }

  function applyCapturePreset() {
    const preset = effectiveCapturePreset();
    const enabled = presetFeatureSet(preset);
    $$('[data-capture-feature]').forEach(input => {
      input.checked = enabled.has(input.dataset.captureFeature);
    });
    updateCaptureFeatureControls({ overridden: false });
  }

  function updateCaptureFeatureControls({ overridden } = {}) {
    if (typeof overridden === "boolean") app.captureFeaturesOverridden = overridden;
    const customized = app.captureFeaturesOverridden;
    const platform = selectedPlatform();
    const performance = app.testMode === "performance";
    const smartPerfOption = $("#capture-preset-input")?.querySelector('option[value="harmony-smartperf"]');
    if (smartPerfOption) smartPerfOption.disabled = platform !== "harmony" || !performance;
    if ((!performance || platform !== "harmony") && $("#capture-preset-input")?.value === "harmony-smartperf") {
      $("#capture-preset-input").value = "auto";
      applyCapturePreset();
      return;
    }

    const highPerformanceRow = $("#harmony-high-performance-row");
    const highPerformanceInput = $("#harmony-high-performance-input");
    const showHighPerformance = platform === "harmony" && performance;
    highPerformanceRow?.classList.toggle("hidden", !showHighPerformance);
    if (!showHighPerformance && highPerformanceInput) highPerformanceInput.checked = false;

    const forcedOff = new Set(platformUnavailableFeatures[platform] || []);
    if (performance) forcedOff.add("power_attribution");
    const probeData = currentProbe()?.data || {};
    const memoryUnavailable = platform === "android"
      && probeData.capabilities?.memory_frequency === false;
    if (memoryUnavailable) forcedOff.add("memory_frequency");
    $$('[data-capture-feature]').forEach(input => {
      const name = input.dataset.captureFeature;
      const disabled = forcedOff.has(name) || Boolean(app.state?.active?.running);
      if (forcedOff.has(name)) input.checked = false;
      input.disabled = disabled;
      const row = input.closest("label");
      row?.classList.toggle("feature-unavailable", forcedOff.has(name));
      if (row) {
        row.title = forcedOff.has(name)
          ? name === "memory_frequency" && memoryUnavailable
            ? probeData.memory_probe?.limitations || "设备未向 ADB shell 公开可读的 DMC/DRAM 实时频率"
            : `${platformLabel(platform)} 当前采集后端不提供此项目`
          : "";
      }
    });

    const values = Object.fromEntries($$('[data-capture-feature]').map(input => [
      input.dataset.captureFeature,
      input.checked,
    ]));
    if (values.frame_details && !values.frame_rate) {
      const frameRate = $('[data-capture-feature="frame_rate"]');
      if (frameRate) frameRate.checked = true;
    }
    if (values.harmony_hitches && !values.foreground_window) {
      const foreground = $('[data-capture-feature="foreground_window"]');
      if (foreground) foreground.checked = true;
    }
    if (values.hot_threads && !values.process_snapshots) {
      const processes = $('[data-capture-feature="process_snapshots"]');
      if (processes) processes.checked = true;
    }

    const inputs = $$('[data-capture-feature]');
    const count = inputs.filter(input => input.checked).length;
    $("#capture-feature-count").textContent = `${count} / ${inputs.length} 已启用`;
    $("#system-monitor-input").checked = [
      "process_snapshots", "hot_threads", "thermal", "scheduler",
    ].some(name => Boolean($(`[data-capture-feature="${name}"]`)?.checked));
    const presetLabel = $("#capture-preset-input")?.selectedOptions?.[0]?.textContent || "采集预设";
    const advancedBadge = $("#advanced-setting-count");
    if (advancedBadge) {
      advancedBadge.textContent = customized
        ? "已自定义"
        : $("#capture-preset-input")?.value === "auto" ? "默认已配置" : "预设已应用";
    }
    $("#capture-preset-hint").textContent = customized
      ? `${presetLabel} · 已逐项覆盖；基础电流、电压和时间戳仍保留。`
      : `${presetLabel} · 可继续逐项关闭以降低测试干扰。`;
    $("#capture-feature-note").textContent = showHighPerformance
      ? "SmartPerf 采集与设备高性能模式相互独立；高性能模式会改变设备功耗和热状态。"
      : platform === "ios"
        ? "iOS 仅启用 DVT/PowerTelemetry 实际可提供的项目；基础电流、电压和时间戳保留。"
        : "基础电流、电压和设备时间戳始终保留。";
  }

  function applyModeDefaults(previousMode, nextMode) {
    if (previousMode === nextMode) return;
    Object.entries(modeDefaults[nextMode]).forEach(([id, nextValue]) => {
      const input = document.getElementById(id);
      if (!input) return;
      const previousDefault = modeDefaults[previousMode]?.[id];
      if (previousDefault === undefined || Number(input.value) === Number(previousDefault)) {
        input.value = String(nextValue);
      }
    });
    syncDurationPreset();
  }

  function setTestMode(mode, { fromRun = false, initial = false } = {}) {
    const nextMode = mode === "performance" ? "performance" : "power";
    const active = app.state?.active;
    if (!fromRun && active?.running && nextMode !== (active.test_mode || "power")) {
      notify("测试进行中无法切换采集档位", "停止当前测试后再选择另一种模式。", "error");
      return;
    }
    const previousMode = app.testMode;
    app.testMode = nextMode;
    document.body.dataset.testMode = nextMode;
    if (
      (!fromRun && !initial)
      || (fromRun && initial && !active?.running)
    ) {
      applyModeDefaults(previousMode, nextMode);
    }
    if (!fromRun && !initial && $("#capture-preset-input")?.value === "auto") {
      applyCapturePreset();
    }

    $$("[data-test-mode]").forEach(button => {
      const selected = button.dataset.testMode === nextMode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.disabled = Boolean(active?.running);
    });

    const performance = nextMode === "performance";
    const profile = platformProfiles[selectedPlatform()];
    if (!performance && app.activeView === "system") switchView("live");
    $("#test-mode-title").textContent = performance ? "性能测试模式" : "功耗测试模式";
    $("#test-mode-description").textContent = performance ? profile.performanceDescription : profile.powerDescription;
    $("#capture-config-title").textContent = performance ? "性能采集配置" : "功耗采集配置";
    $("#capture-mode-badge").lastChild.textContent = performance ? " PERFORMANCE" : " POWER";
    $("#advanced-setting-count").textContent = "默认已配置";
    $("#control-note").textContent = performance ? profile.performanceNote : profile.powerNote;
    updatePlatformPresentation();
    syncDurationPreset();
    updateAppScannerAvailability();

    const monitorInput = $("#system-monitor-input");
    monitorInput.disabled = Boolean(active?.running);
    updateCaptureFeatureControls();

    const iosPerformance = performance && selectedPlatform() === "ios";
    const validMetrics = performance
      ? iosPerformance
        ? new Set(["cpu_pct", "gpu_load_pct", "power_mw", "temperature_c"])
        : new Set(["frame_rate_fps", "frame_time_ms", "cpu_pct", "gpu_load_pct", "gpu_frequency_mhz"])
      : new Set(["power_mw", "current_ma", "cpu_pct", "memory_frequency_mhz", "temperature_c", "voltage_mv"]);
    if (previousMode !== nextMode && iosPerformance) app.metric = "cpu_pct";
    else if (!validMetrics.has(app.metric)) app.metric = performance ? iosPerformance ? "cpu_pct" : "frame_rate_fps" : "power_mw";
    $$("[data-metric]").forEach(button => {
      button.classList.toggle("active", button.dataset.metric === app.metric);
    });
    const startLabel = $("#start-record")?.querySelector("span:last-child");
    if (startLabel && !active?.running) {
      startLabel.textContent = performance ? "开始性能测试" : "开始功耗测试";
    }
    if (!initial) {
      renderPerformanceMetrics(active);
      renderPerformanceResources(active);
      renderPowerPressure(active);
      renderRenderPipeline(active);
      renderChart();
    }
  }

  function percentile(values, fraction) {
    const ordered = values.filter(finite).map(Number).sort((a, b) => a - b);
    if (!ordered.length) return null;
    if (ordered.length === 1) return ordered[0];
    const position = clamp(fraction, 0, 1) * (ordered.length - 1);
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    if (lower === upper) return ordered[lower];
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower);
  }

  function captureFeatureEnabled(active, name) {
    const liveFeatures = active?.config?.capture_features;
    if (liveFeatures && Object.prototype.hasOwnProperty.call(liveFeatures, name)) {
      return Boolean(liveFeatures[name]);
    }
    const metadataFeatures = active?.metadata?.capture_configuration?.features;
    if (metadataFeatures && Object.prototype.hasOwnProperty.call(metadataFeatures, name)) {
      return Boolean(metadataFeatures[name]);
    }
    const input = $(`[data-capture-feature="${name}"]`);
    return input ? Boolean(input.checked) : true;
  }

  function powerFlowPresentation(active) {
    const latest = active?.latest || {};
    const rawDirection = String(
      latest.direction
      || active?.summary?.direction
      || active?.battery?.status
      || active?.metadata?.battery_start?.status
      || "unknown"
    ).toLowerCase();
    const charging = rawDirection === "charging" || (
      rawDirection === "unknown"
      && finite(latest.signed_current_ma)
      && Number(latest.signed_current_ma) > 0
    );
    const discharging = rawDirection === "discharging" || (
      rawDirection === "unknown"
      && finite(latest.signed_current_ma)
      && Number(latest.signed_current_ma) < 0
    );
    if (charging) {
      return {
        direction: "charging",
        powerLabel: "电池充入功率",
        powerTag: "CHARGE",
        currentLabel: "充电电流",
        currentTag: "INPUT",
        energyLabel: "累计充入能量",
        chartPowerTitle: "电池充入功率",
        chartPowerLegend: "Battery charge power",
        chartCurrentTitle: "充电电流",
        chartCurrentLegend: "Battery charge current",
      };
    }
    if (discharging) {
      return {
        direction: "discharging",
        powerLabel: "电池放电功率",
        powerTag: "BATTERY",
        currentLabel: "放电电流",
        currentTag: "CURRENT",
        energyLabel: "累计放电能量",
        chartPowerTitle: "电池放电功率",
        chartPowerLegend: "Battery discharge power",
        chartCurrentTitle: "放电电流",
        chartCurrentLegend: "Battery discharge current",
      };
    }
    return {
      direction: "unknown",
      powerLabel: "电池侧功率",
      powerTag: "BATTERY",
      currentLabel: "电池电流",
      currentTag: "CURRENT",
      energyLabel: "累计电池侧能量",
      chartPowerTitle: "电池侧功率",
      chartPowerLegend: "Battery-side power",
      chartCurrentTitle: "电池电流幅值",
      chartCurrentLegend: "Battery current magnitude",
    };
  }

  function gpuTelemetryAvailable(active) {
    if (!active) return true;
    if (!captureFeatureEnabled(active, "gpu_metrics")) return false;
    const latest = active.latest || {};
    if (finite(latest.gpu_load_pct) || finite(latest.gpu_frequency_mhz)) return true;
    const series = Array.isArray(active.series) ? active.series : [];
    if (series.some(item => finite(item.gpu_load_pct) || finite(item.gpu_frequency_mhz))) return true;
    // A detected sysfs/DVT candidate is only a capability hint. Keep the controls
    // while the first sample is pending, then hide them unless telemetry actually
    // arrived; this avoids displaying permanently empty GPU rows on restricted OEMs.
    return Boolean(active.running) && Number(active.sample_count || 0) === 0;
  }

  function updateGpuVisibility(active) {
    const available = gpuTelemetryAvailable(active);
    $$('[data-metric="gpu_load_pct"], [data-metric="gpu_frequency_mhz"]').forEach(button => {
      button.classList.toggle("hidden", !available);
    });
    $("#resource-gpu-row")?.classList.toggle("hidden", !available);
    $("#performance-gpu-row")?.classList.toggle("hidden", !available);
    const iosPerformance = (active?.test_mode || app.testMode) === "performance"
      && activePlatform(active) === "ios";
    $("#metric-one-low-card")?.classList.toggle("hidden", iosPerformance && !available);
    $("#performance-fps-stat")?.classList.toggle("hidden", iosPerformance && !available);
    if (!available && ["gpu_load_pct", "gpu_frequency_mhz"].includes(app.metric)) {
      app.metric = app.testMode === "performance" && activePlatform(active) !== "ios"
        ? "frame_rate_fps"
        : "cpu_pct";
      $$('[data-metric]').forEach(button => {
        button.classList.toggle("active", button.dataset.metric === app.metric);
      });
    }
    return available;
  }

  function sparklineMarkup(values) {
    const selected = values.slice(-60).filter(finite).map(Number);
    if (selected.length < 2) return '<svg class="live-sparkline" viewBox="0 0 86 42" aria-hidden="true"><line x1="2" y1="21" x2="84" y2="21"></line></svg>';
    let minimum = Math.min(...selected);
    let maximum = Math.max(...selected);
    if (minimum === maximum) {
      minimum -= 1;
      maximum += 1;
    }
    const points = selected.map((value, index) => {
      const x = 2 + index / (selected.length - 1) * 82;
      const y = 38 - (value - minimum) / Math.max(.0001, maximum - minimum) * 34;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg class="live-sparkline" viewBox="0 0 86 42" aria-hidden="true"><line x1="2" y1="38" x2="84" y2="38"></line><path d="M${points.replaceAll(" ", " L")}"></path></svg>`;
  }

  function renderLiveDetails(active) {
    const container = $("#live-detail-grid");
    if (!container) return;
    const platform = activePlatform(active);
    const performanceMode = (active?.test_mode || app.testMode) === "performance";
    const keys = performanceMode
      ? platform === "ios"
        ? ["cpu_pct", "gpu_load_pct", "power_mw", "temperature_c"]
        : ["frame_rate_fps", "frame_time_ms", "cpu_pct", "gpu_load_pct", "gpu_frequency_mhz", "memory_frequency_mhz", "temperature_c", "power_mw"]
      : ["power_mw", "current_ma", "voltage_mv", "cpu_pct", "gpu_load_pct", "gpu_frequency_mhz", "memory_frequency_mhz", "temperature_c"];
    const cards = [];
    keys.forEach(key => {
      if (key === "memory_frequency_mhz" && !captureFeatureEnabled(active, "memory_frequency")) return;
      const definition = metricDefinitions[key];
      const source = definition.series === "performance" ? active?.performance_series : active?.series;
      const values = (Array.isArray(source) ? source : []).map(definition.value).filter(finite).map(Number);
      if (!values.length) return;
      const current = values.at(-1);
      const average = values.reduce((sum, value) => sum + value, 0) / values.length;
      const secondary = percentile(values, definition.secondaryQuantile ?? .95);
      const powerFlow = powerFlowPresentation(active);
      const title = key === "power_mw"
        ? powerFlow.chartPowerTitle
        : key === "current_ma"
          ? powerFlow.chartCurrentTitle
          : definition.title;
      cards.push(`<button type="button" class="live-detail-card" data-live-metric="${escapeHtml(key)}" style="--detail-color:${escapeHtml(definition.color)}">
        <span class="live-detail-copy"><small>${escapeHtml(title)}</small><strong>${current.toFixed(definition.digits)} ${escapeHtml(definition.unit)}</strong><span>AVG ${average.toFixed(definition.digits)} · ${escapeHtml(definition.secondaryLabel || "P95")} ${secondary === null ? "--" : secondary.toFixed(definition.digits)}</span></span>
        ${sparklineMarkup(values)}
      </button>`);
    });
    container.innerHTML = cards.length
      ? cards.join("")
      : '<div class="empty-row">当前尚无可展开的实时数据；未启用或不可读的项目不会显示。</div>';
    $("#live-detail-count").textContent = cards.length ? `${cards.length} 项有效数据` : "等待数据";
  }

  function svgNode(name, attributes = {}, text = "") {
    const node = document.createElementNS(svgNs, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text) node.textContent = text;
    return node;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }
    if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
    return data;
  }

  function notify(title, detail = "", type = "success", timeout = 5000) {
    const container = $("#toast-stack");
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.innerHTML = `<i></i><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div><button type="button" aria-label="关闭">×</button>`;
    const close = () => toast.remove();
    toast.querySelector("button").addEventListener("click", close);
    container.appendChild(toast);
    if (timeout) setTimeout(close, timeout);
  }

  function setBusy(active, label = "正在处理...") {
    $("#busy-label").textContent = label;
    $("#busy-overlay").classList.toggle("hidden", !active);
    $("#busy-overlay").setAttribute("aria-hidden", active ? "false" : "true");
  }

  function setServerState(online, detail = "Local dashboard") {
    const dot = $("#server-dot");
    dot.classList.toggle("online", online);
    dot.classList.toggle("offline", !online);
    $("#server-label").textContent = online ? "服务在线" : "连接断开";
    $("#server-detail").textContent = detail;
  }

  function switchView(view) {
    const requested = view === "thermal" ? "system" : view;
    const target = ["live", "system", "device", "history", "tools"].includes(requested) ? requested : "live";
    app.activeView = target;
    location.hash = target;
    $$(".nav-item").forEach(button => {
      const active = button.dataset.view === target;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    $$(".view").forEach(panel => {
      const active = panel.dataset.panel === target;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    });
    $("#page-heading").textContent = {
      live: "实时监控",
      system: "性能上下文",
      device: "设备能力",
      history: "历史报告",
      tools: "工具与交付",
    }[target];
    if (target === "live") requestAnimationFrame(renderChart);
  }

  function selectedDevice() {
    return $("#device-select").value || "";
  }

  function resetAppScanner({ clearPackage = false } = {}) {
    const packageInput = $("#package-input");
    if (
      clearPackage
      && app.selectedScannedPackage
      && packageInput?.value.trim() === app.selectedScannedPackage
    ) {
      packageInput.value = "";
    }
    app.scannedApps = [];
    app.scannedAppsDevice = "";
    app.scannedAppsSource = "";
    app.selectedScannedPackage = "";
    const search = $("#app-search-input");
    const select = $("#app-select");
    const resultList = $("#app-result-list");
    if (search) {
      search.value = "";
      search.disabled = true;
    }
    if (select) {
      select.innerHTML = '<option value="">请先扫描手机应用</option>';
      select.disabled = true;
    }
    if (resultList) {
      resultList.innerHTML = "";
      resultList.classList.add("hidden");
    }
    if ($("#app-picker-status")) $("#app-picker-status").textContent = "尚未扫描";
    if ($("#app-picker-selection")) {
      $("#app-picker-selection").textContent = packageInput?.value.trim()
        ? `${packageInput.value.trim()} · 手工输入`
        : "未选择目标应用";
    }
  }

  function renderAppOptions() {
    const select = $("#app-select");
    const search = $("#app-search-input");
    const resultList = $("#app-result-list");
    if (!select || !search || !resultList) return;
    const query = search.value.trim().toLowerCase();
    const apps = Array.isArray(app.scannedApps) ? app.scannedApps : [];
    const filtered = apps.filter(item => {
      const searchable = [
        item.package,
        item.label,
        item.activity,
        item.component,
        ...(Array.isArray(item.activities) ? item.activities : []),
      ].filter(Boolean).join(" ").toLowerCase();
      return !query || searchable.includes(query);
    });
    select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = filtered.length ? "请选择测试游戏 / 应用" : query ? "没有匹配的应用" : "未发现应用";
    select.appendChild(placeholder);
    [
      [true, "第三方应用"],
      [false, "系统应用"],
    ].forEach(([userApp, label]) => {
      const matching = filtered.filter(item => Boolean(item.user_app) === userApp);
      if (!matching.length) return;
      const group = document.createElement("optgroup");
      group.label = label;
      matching.forEach(item => {
        const option = document.createElement("option");
        option.value = item.package || "";
        option.textContent = item.package || "Unknown package";
        option.title = item.component || item.package || "";
        group.appendChild(option);
      });
      select.appendChild(group);
    });
    const packageValue = $("#package-input").value.trim();
    if (filtered.some(item => item.package === packageValue)) select.value = packageValue;
    const shown = filtered.slice(0, 80);
    resultList.classList.toggle("hidden", !app.scannedAppsDevice);
    resultList.innerHTML = shown.length ? shown.map(item => {
      const packageName = String(item.package || "");
      const activity = String(item.activity || item.component || "").split(".").at(-1) || "应用";
      const initials = packageName.split(".").filter(Boolean).at(-1)?.slice(0, 2).toUpperCase() || "APP";
      const icon = typeof item.icon_data_uri === "string" && item.icon_data_uri.startsWith("data:image/")
        ? `<img src="${escapeHtml(item.icon_data_uri)}" alt="" loading="lazy">`
        : escapeHtml(initials);
      return `<button type="button" class="app-option ${packageName === packageValue ? "selected" : ""}" data-app-package="${escapeHtml(packageName)}" role="option" aria-selected="${packageName === packageValue ? "true" : "false"}">
        <span class="app-thumb">${icon}</span>
        <span class="app-option-copy"><strong>${escapeHtml(activity)}</strong><small>${escapeHtml(packageName)}</small></span>
        <span class="app-option-badge">${item.user_app ? "USER" : "SYSTEM"}</span>
      </button>`;
    }).join("") : '<div class="empty-row">没有匹配的应用</div>';
    const noun = app.scannedAppsSource === "third-party-packages-fallback" ? "第三方包" : "可启动应用";
    $("#app-picker-status").textContent = query
      ? `匹配 ${filtered.length} / ${apps.length} 个${noun}`
      : `已扫描 ${apps.length} 个${noun}`;
    const scannedMatch = apps.some(item => item.package === packageValue);
    $("#app-picker-selection").textContent = packageValue
      ? `${packageValue} · ${scannedMatch ? "已选择" : "手工输入"}`
      : "未选择目标应用";
    const running = Boolean(app.state?.active?.running);
    const currentScan = Boolean(app.scannedAppsDevice && app.scannedAppsDevice === selectedDevice());
    search.disabled = !currentScan || running;
    select.disabled = !currentScan || running || !filtered.length;
    resultList.querySelectorAll("button").forEach(button => { button.disabled = running; });
  }

  function updateAppScannerAvailability(device = selectedDeviceInfo()) {
    const running = Boolean(app.state?.active?.running);
    const androidReady = Boolean(
      device
      && devicePlatform(device) === "android"
      && device.state === "device"
    );
    if (app.scannedAppsDevice && device?.serial && app.scannedAppsDevice !== device.serial) {
      resetAppScanner({ clearPackage: true });
    }
    const currentScan = Boolean(
      androidReady
      && app.scannedAppsDevice
      && app.scannedAppsDevice === device.serial
    );
    const scanButton = $("#scan-apps");
    if (scanButton) scanButton.disabled = !androidReady || running;
    const search = $("#app-search-input");
    const select = $("#app-select");
    if (search) search.disabled = !currentScan || running;
    if (select) {
      const hasOptions = Boolean(select.querySelector('option[value]:not([value=""])'));
      select.disabled = !currentScan || running || !hasOptions;
    }
    $("#app-result-list")?.querySelectorAll("button").forEach(button => {
      button.disabled = !currentScan || running;
    });
  }

  function devicePlatform(device) {
    return String(device?.platform || "android").toLowerCase();
  }

  function platformLabel(platform) {
    return { android: "Android", harmony: "HarmonyOS", ios: "iOS" }[String(platform || "").toLowerCase()] || "Mobile";
  }

  function deviceConnectionType(device) {
    const declared = String(device?.connection_type || "").toLowerCase();
    if (["usb", "wireless", "emulator"].includes(declared)) return declared;
    const serial = String(device?.serial || "");
    if (/^emulator-\d+$/i.test(serial)) return "emulator";
    if (/\._adb-tls-connect\._tcp/i.test(serial) || /:\d+$/.test(serial)) return "wireless";
    return serial ? "usb" : "unknown";
  }

  function deviceConnectionLabel(device) {
    if (
      devicePlatform(device) === "ios"
      && deviceConnectionType(device) === "usb"
      && String(device?.wireless_ready || "").toLowerCase() === "true"
    ) {
      return "USB 有线（无线已配对）";
    }
    return {
      usb: "USB 有线",
      wireless: "Wi-Fi 无线",
      emulator: "模拟器",
      unknown: "其他",
    }[deviceConnectionType(device)] || "其他";
  }

  function renderDevices(state) {
    const select = $("#device-select");
    const platform = selectedPlatform();
    const platformError = platform === "ios"
      ? state.ios_error
      : platform === "harmony"
        ? state.harmony_error
        : state.device_error;
    const allDevices = Array.isArray(state.devices) ? state.devices : [];
    const currentValue = select.value || "";
    const currentMatchesPlatform = allDevices.some(
      device => device.serial === currentValue && devicePlatform(device) === platform
    );
    const previous = (currentMatchesPlatform ? currentValue : "")
      || localStorage.getItem(`mobile-profiler-device-${platform}`)
      || "";
    const devices = allDevices.filter(device => devicePlatform(device) === platform);
    updatePlatformCounts(allDevices);
    select.innerHTML = "";
    const ready = devices.filter(device => device.state === "device");
    if (!devices.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = platformError ? `${platformLabel(platform)} 连接不可用` : `未发现 ${platformLabel(platform)} 设备`;
      select.appendChild(option);
    } else {
      [
        ["usb", "有线设备（USB）"],
        ["wireless", "无线设备（Wi-Fi）"],
        ["emulator", "模拟器"],
        ["unknown", "其他设备"],
      ].forEach(([type, label]) => {
        const matching = devices.filter(device => deviceConnectionType(device) === type);
        if (!matching.length) return;
        const group = document.createElement("optgroup");
        group.label = label;
        matching.forEach(device => {
          const option = document.createElement("option");
          option.value = device.serial || "";
          option.dataset.connectionType = type;
          option.dataset.platform = devicePlatform(device);
          const name = [device.model, device.product].filter(Boolean).join(" / ");
          option.textContent = `${name || platformLabel(platform) + " Device"} · ${device.serial} · ${device.state}`;
          option.disabled = device.state !== "device";
          group.appendChild(option);
        });
        select.appendChild(group);
      });
    }
    const activeSerial = state.active?.running && activePlatform(state.active) === platform
      ? state.active.device_serial
      : "";
    const preferred = activeSerial || previous;
    const preferredDevice = devices.find(device => device.serial === preferred);
    if (preferredDevice?.state === "device") {
      select.value = preferred;
    } else if (ready.length) {
      select.value = ready[0].serial;
    }
    select.disabled = Boolean(state.active?.running);
    const chosen = devices.find(device => device.serial === select.value);
    const chosenType = deviceConnectionType(chosen);
    const chosenPlatform = chosen ? devicePlatform(chosen) : platform;
    const deviceState = $("#device-state");
    deviceState.classList.toggle("online", Boolean(chosen?.state === "device"));
    deviceState.classList.toggle("error", Boolean(platformError || (chosen && chosen.state !== "device")));
    deviceState.title = platformError || "";
    deviceState.querySelector("span").textContent = chosen?.state === "device" ? `${platformLabel(chosenPlatform)} · ${deviceConnectionLabel(chosen)}在线` : platformError ? `${platformLabel(platform)} 连接异常` : chosen ? `${deviceConnectionLabel(chosen)} · ${chosen.state}` : "等待设备";
    $("#start-record").disabled = !chosen || chosen.state !== "device" || Boolean(state.active?.running);
    $("#enable-tcpip").disabled = !chosen || chosenPlatform !== "android" || chosen.state !== "device" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#enable-harmony-tcpip").disabled = !chosen || chosenPlatform !== "harmony" || chosen.state !== "device" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#pair-ios").disabled = !chosen || chosenPlatform !== "ios" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#disconnect-wireless").disabled = !chosen || chosenPlatform !== "android" || chosenType !== "wireless" || Boolean(state.active?.running);
    updateAppScannerAvailability(chosen);
    updateCaptureFeatureControls();
    renderBrightnessControl(chosen);
    if (
      androidBrightnessDevice(chosen)
      && !state.active?.running
      && app.brightnessDevice !== chosen.serial
    ) {
      void refreshBrightnessCapability();
    }
  }

  function renderMetrics(active) {
    const latest = active?.latest || {};
    const summary = active?.summary || {};
    const isIos = activePlatform(active) === "ios";
    const powerFlow = powerFlowPresentation(active);
    updateGpuVisibility(active);
    const memoryEnabled = captureFeatureEnabled(active, "memory_frequency");
    const memoryAvailable = isIos || (
      memoryEnabled
      && (finite(latest.memory_frequency_mhz) || Boolean(active?.memory?.available))
    );
    $("#metric-memory-card")?.classList.toggle("hidden", !memoryAvailable);
    $("#resource-memory-row")?.classList.toggle("hidden", !memoryAvailable || isIos);
    const memoryTab = $('[data-metric="memory_frequency_mhz"]');
    memoryTab?.classList.toggle("hidden", !memoryAvailable || isIos);
    if (!memoryAvailable && app.metric === "memory_frequency_mhz") {
      app.metric = app.testMode === "performance" ? "frame_rate_fps" : "power_mw";
      $$('[data-metric]').forEach(item => item.classList.toggle("active", item.dataset.metric === app.metric));
    }
    $("#metric-power-label").textContent = powerFlow.powerLabel;
    $("#metric-power-tag").textContent = powerFlow.powerTag;
    $("#metric-current-label").textContent = powerFlow.currentLabel;
    $("#metric-current-tag").textContent = powerFlow.currentTag;
    $("#metric-energy-label").textContent = powerFlow.energyLabel;
    $("#metric-energy-tag").textContent = powerFlow.direction === "charging" ? "CHARGE" : "ENERGY";
    const powerTab = $('[data-metric="power_mw"][data-mode-only="power"]');
    const currentTab = $('[data-metric="current_ma"][data-mode-only="power"]');
    if (powerTab) powerTab.textContent = powerFlow.direction === "charging" ? "充入功率" : "放电功率";
    if (currentTab) currentTab.textContent = powerFlow.direction === "charging" ? "充电电流" : "放电电流";
    $("#metric-power").textContent = finite(latest.power_mw) ? `${(Number(latest.power_mw) / 1000).toFixed(3)} W` : "--";
    $("#metric-current").textContent = formatMetric(latest.current_ma, "mA", 0);
    $("#metric-cpu").textContent = formatMetric(latest.cpu_pct, "%", 1);
    $("#metric-memory").textContent = isIos
      ? formatMetric(latest.collector_cpu_pct, "%", 2)
      : formatMetric(latest.memory_frequency_mhz, "MHz", 0);
    $("#metric-temp").textContent = formatMetric(latest.temperature_c, "°C", 1);
    $("#metric-energy").textContent = formatMetric(summary.energy_mwh, "mWh", 2);
    const averagePower = finite(summary.average_power_mw) ? `AVG ${(Number(summary.average_power_mw) / 1000).toFixed(3)} W` : "整机电池侧";
    $("#metric-power-sub").textContent = powerFlow.direction === "charging"
      ? `${averagePower} · 电池吸收功率，不代表设备自身功耗`
      : isIos && finite(latest.power_sample_age_s)
        ? `${averagePower} · age ${Number(latest.power_sample_age_s).toFixed(1)} s`
        : averagePower;
    $("#metric-current-sub").textContent = finite(summary.average_current_ma)
      ? `AVG ${Number(summary.average_current_ma).toFixed(0)} mA${powerFlow.direction === "charging" ? " · 流入电池" : ""}`
      : powerFlow.direction === "charging" ? "流入电池的正幅值" : "正幅值";
    const averageCpu = finite(summary.average_cpu_pct) ? `AVG ${Number(summary.average_cpu_pct).toFixed(1)}%` : "全核心";
    $("#metric-cpu-sub").textContent = isIos && finite(summary.average_collector_cpu_pct)
      ? `${averageCpu} · collector ${Number(summary.average_collector_cpu_pct).toFixed(1)}%`
      : averageCpu;
    const memory = active?.memory || {};
    $("#metric-memory-sub").textContent = isIos
      ? finite(summary.average_collector_cpu_pct)
        ? `AVG ${Number(summary.average_collector_cpu_pct).toFixed(2)}%`
        : "observer CPU"
      : finite(memory.p95_frequency_mhz)
        ? `P95 ${Number(memory.p95_frequency_mhz).toFixed(0)} MHz · 高频 ${formatNumber(memory.high_frequency_share_pct, 1)}%`
        : memory.limitations || "DRAM / DMC / MIF";
    $("#metric-temp-sub").textContent = finite(latest.voltage_mv) ? `${(Number(latest.voltage_mv) / 1000).toFixed(3)} V` : isIos ? "DiagnosticsService" : "BatteryService";
    $("#metric-energy-sub").textContent = active?.sample_count
      ? `${active.sample_count} samples · ${powerFlow.direction === "charging" ? "充入能量积分" : "有效区间积分"}`
      : powerFlow.direction === "charging" ? "充入能量积分" : "有效区间积分";
    const chargingNotice = $("#charging-power-notice");
    const showChargingNotice = powerFlow.direction === "charging" && finite(latest.power_mw);
    chargingNotice?.classList.toggle("hidden", !showChargingNotice);
    if (showChargingNotice) {
      const watts = Number(latest.power_mw) / 1000;
      const amps = finite(latest.current_ma) ? Number(latest.current_ma) / 1000 : null;
      const volts = finite(latest.voltage_mv) ? Number(latest.voltage_mv) / 1000 : null;
      $("#charging-power-title").textContent = `当前为充电状态：电池充入 ${watts.toFixed(2)} W`;
      $("#charging-power-detail").textContent = amps !== null && volts !== null
        ? `${amps.toFixed(2)} A × ${volts.toFixed(2)} V。黑屏只降低设备负载，不会让快充输入归零；续航测试请断开外部供电。`
        : "该数值是电池吸收的充电功率，不等于黑屏待机功耗；续航测试请断开外部供电。";
    }
  }

  function renderPerformanceMetrics(active) {
    if (activePlatform(active) === "ios") {
      const latest = active?.latest || {};
      const summary = active?.summary || {};
      const context = active?.context || {};
      $("#metric-fps").textContent = formatMetric(latest.cpu_pct, "%", 1);
      $("#metric-fps-sub").textContent = "DVT 进程 CPU / 逻辑核心归一化";
      $("#metric-one-low").textContent = formatMetric(latest.gpu_load_pct, "%", 1);
      $("#metric-one-low-sub").textContent = "DVT GPU utilization";
      const overhead = finite(latest.collector_cpu_pct)
        ? latest.collector_cpu_pct
        : summary.average_collector_cpu_pct;
      $("#metric-frame-p99").textContent = formatMetric(overhead, "%", 2);
      $("#metric-frame-p99-sub").textContent = "sysmond / DTServiceHub / pairing daemon";
      $("#metric-frame-issue").textContent = finite(latest.power_mw)
        ? `${(Number(latest.power_mw) / 1000).toFixed(3)} W`
        : "--";
      $("#metric-frame-issue-sub").textContent = finite(latest.power_sample_age_s)
        ? `PowerTelemetry · age ${Number(latest.power_sample_age_s).toFixed(1)} s`
        : "PowerTelemetry SystemLoad";
      $("#metric-render-resolution").textContent = context.foreground_package
        || active?.metadata?.target_package
        || "--";
      $("#metric-render-resolution-sub").textContent = context.foreground_activity
        || "事件驱动的 application state";
      $("#metric-render-resolution-card")?.classList.remove("hidden");
      $("#metric-interpolation-card")?.classList.remove("hidden");
      $("#performance-render-evidence")?.classList.remove("hidden");
      $("#performance-interpolation-evidence-card")?.classList.remove("hidden");
      $(".performance-evidence-grid")?.style.setProperty("grid-template-columns", "repeat(3, minmax(0, 1fr))");
      const interpolationCard = $("#metric-interpolation-card");
      interpolationCard.classList.remove("detected", "disabled", "indeterminate", "unavailable");
      $("#metric-interpolation").textContent = formatMetric(latest.temperature_c, "°C", 1);
      $("#metric-interpolation-sub").textContent = "iOS battery diagnostics";
      return;
    }
    const performance = active?.performance || {};
    const fps = finite(performance.sampled_frame_rate_fps)
      ? Number(performance.sampled_frame_rate_fps)
      : finite(performance.sampled_compositor_fps)
        ? Number(performance.sampled_compositor_fps)
        : null;
    const oneLow = finite(performance.one_percent_low_fps)
      ? Number(performance.one_percent_low_fps)
      : null;
    const p99 = finite(performance.frame_metric_p99_ms)
      ? Number(performance.frame_metric_p99_ms)
      : null;
    const issuePct = finite(performance.frame_issue_pct)
      ? Number(performance.frame_issue_pct)
      : finite(performance.missed_vsync_interval_pct)
        ? Number(performance.missed_vsync_interval_pct)
        : null;

    $("#metric-fps").textContent = fps === null ? "--" : fps.toFixed(1) + " FPS";
    $("#metric-fps-sub").textContent = finite(performance.current_refresh_rate_hz)
      ? "显示 " + Number(performance.current_refresh_rate_hz).toFixed(0) + " Hz · " + Number(performance.frame_sample_count || 0).toLocaleString("zh-CN") + " 帧"
      : performance.frame_unavailable_reason || "等待前台窗口帧计数";
    $("#metric-one-low").textContent = oneLow === null ? "--" : oneLow.toFixed(1) + " FPS";
    $("#metric-one-low-sub").textContent = performance.one_percent_low_source || "最慢 1% 帧换算";
    $("#metric-frame-p99").textContent = p99 === null ? "--" : p99.toFixed(2) + " ms";
    $("#metric-frame-p99-sub").textContent = finite(performance.frame_metric_p95_ms)
      ? "P95 " + Number(performance.frame_metric_p95_ms).toFixed(2) + " ms"
      : performance.frame_metric_label || "应用帧耗时";
    $("#metric-frame-issue").textContent = issuePct === null ? "--" : issuePct.toFixed(2) + "%";
    $("#metric-frame-issue-sub").textContent = performance.frame_issue_label || "截止时间 / VSync";

    const renderWidth = performance.render_width_px;
    const renderHeight = performance.render_height_px;
    const renderAvailable = Boolean(performance.render_resolution_available)
      && finite(renderWidth)
      && finite(renderHeight);
    $("#metric-render-resolution-card")?.classList.toggle("hidden", !renderAvailable);
    $("#performance-render-evidence")?.classList.toggle("hidden", !renderAvailable);
    $("#metric-render-resolution").textContent = finite(renderWidth) && finite(renderHeight)
      ? Number(renderWidth).toFixed(0) + " × " + Number(renderHeight).toFixed(0)
      : "--";
    $("#metric-render-resolution-sub").textContent = performance.render_resolution_estimated
      ? "显示分辨率回退值"
      : performance.render_resolution_source || "等待前台窗口边界";

    const interpolationLabels = {
      detected: "已开启",
      disabled: "已关闭",
      indeterminate: "待确认",
      unavailable: "不可读",
    };
    const interpolationStatus = String(performance.frame_interpolation_status || "unavailable");
    const interpolationAvailable = Boolean(performance.frame_interpolation_available);
    $("#metric-interpolation-card")?.classList.toggle("hidden", !interpolationAvailable);
    $("#performance-interpolation-evidence-card")?.classList.toggle("hidden", !interpolationAvailable);
    const evidenceCount = 1 + (renderAvailable ? 1 : 0) + (interpolationAvailable ? 1 : 0);
    $(".performance-evidence-grid")?.style.setProperty(
      "grid-template-columns",
      `repeat(${evidenceCount}, minmax(0, 1fr))`,
    );
    const interpolationCard = $("#metric-interpolation-card");
    interpolationCard.classList.remove("detected", "disabled", "indeterminate", "unavailable");
    interpolationCard.classList.add(interpolationStatus);
    $("#metric-interpolation").textContent = interpolationLabels[interpolationStatus] || "待确认";
    $("#metric-interpolation-sub").textContent = performance.frame_interpolation_label || "等待显式系统证据";
  }

  function renderPerformanceResources(active) {
    if (activePlatform(active) === "ios") {
      const latest = active?.latest || {};
      const context = active?.context || {};
      const monitor = active?.system_monitor || {};
      $("#resource-gpu").textContent = finite(latest.gpu_load_pct)
        ? `${Number(latest.gpu_load_pct).toFixed(1)}%`
        : "--";
      $("#resource-thermal").textContent = finite(latest.temperature_c)
        ? `${Number(latest.temperature_c).toFixed(1)} °C`
        : "--";
      $("#resource-window").textContent = context.foreground_package
        || active?.metadata?.target_package
        || "--";
      $("#resource-ios-overhead").textContent = finite(latest.collector_cpu_pct)
        ? `${Number(latest.collector_cpu_pct).toFixed(2)}%`
        : finite(active?.summary?.average_collector_cpu_pct)
          ? `${Number(active.summary.average_collector_cpu_pct).toFixed(2)}% AVG`
          : "--";
      $("#resource-ios-power-age").textContent = finite(latest.power_sample_age_s)
        ? `${Number(latest.power_sample_age_s).toFixed(1)} s`
        : "--";
      $("#resource-whole-power").textContent = finite(latest.power_mw)
        ? `${(Number(latest.power_mw) / 1000).toFixed(3)} W · 物理整机`
        : "--";
      $("#resource-snapshot-source").textContent = Number(monitor.system_snapshot_count || 0)
        ? `${Number(monitor.system_snapshot_count)} DVT snapshots`
        : "DVT sysmond";
      $("#performance-frame-source").textContent = "iOS DVT sysmond / GPU counters";
      $("#performance-frame-confidence").textContent = "CPU、GPU 和进程资源为诊断遥测，不包含通用应用 FPS";
      $("#performance-render-source").textContent = "PowerTelemetryData.SystemLoad";
      $("#performance-render-scale").textContent = finite(latest.power_sample_age_s)
        ? `物理功耗样本年龄 ${Number(latest.power_sample_age_s).toFixed(1)} s`
        : "物理功耗通常约 20 秒更新一次";
      $("#performance-interpolation-source").textContent = "collector_cpu_pct";
      $("#performance-interpolation-evidence").textContent = finite(latest.collector_cpu_pct)
        ? `当前归一化采集器 CPU ${Number(latest.collector_cpu_pct).toFixed(2)}%`
        : "报告保留 sysmond、DTServiceHub 与配对服务的观察者开销";
      return;
    }
    const platform = activePlatform(active);
    const performance = active?.performance || {};
    const context = active?.context || {};
    const monitor = active?.system_monitor || {};
    const scheduler = monitor.scheduler || {};
    const cpusets = scheduler.cpusets || {};
    const policies = Array.isArray(scheduler.cpu_policies) ? scheduler.cpu_policies : [];
    const processStates = Array.isArray(scheduler.watched_processes) ? scheduler.watched_processes : [];
    const hintSessions = Array.isArray(scheduler.hint_sessions) ? scheduler.hint_sessions : [];
    const foregroundPackage = context.foreground_package || "";
    const foregroundProcess = processStates.find(item => item.name === foregroundPackage)
      || processStates.find(item => foregroundPackage && String(item.name || "").startsWith(foregroundPackage + ":"))
      || null;

    $("#resource-top-app-cpuset").textContent = cpusets["top-app"] || "--";
    $("#resource-foreground-sched").textContent = foregroundProcess
      ? "group " + String(foregroundProcess.current_sched_group ?? "--")
        + " / procState " + String(foregroundProcess.current_proc_state ?? "--")
        + " / adj " + String(foregroundProcess.oom_adj ?? "--")
      : foregroundPackage ? foregroundPackage + " · 等待调度快照" : "--";
    const governors = [...new Set(policies.map(item => item.governor).filter(Boolean))];
    $("#resource-governors").textContent = governors.length
      ? governors.join(" / ")
      : policies.length ? "仅公开频率上下限" : "--";
    const graphicsHints = hintSessions.filter(item => item.graphics_pipeline).length;
    $("#resource-adpf").textContent = hintSessions.length
      ? String(hintSessions.length) + " 个活动会话" + (graphicsHints ? " · " + String(graphicsHints) + " 个图形管线" : "")
      : scheduler.availability?.hint_session_supported === false ? "设备不支持" : "0 / 等待快照";

    const latest = active?.latest || {};
    const gpuParts = [];
    if (finite(latest.gpu_load_pct)) gpuParts.push(Number(latest.gpu_load_pct).toFixed(1) + "%");
    if (finite(latest.gpu_frequency_mhz)) gpuParts.push(Number(latest.gpu_frequency_mhz).toFixed(0) + " MHz");
    $("#resource-gpu").textContent = gpuParts.join(" / ") || "--";
    $("#resource-memory").textContent = finite(latest.memory_frequency_mhz)
      ? Number(latest.memory_frequency_mhz).toFixed(0) + " MHz"
      : "--";
    const thermal = monitor.thermal || {};
    $("#resource-thermal").textContent = finite(thermal.status)
      ? (thermalStatusDefinitions[Number(thermal.status)] || "状态 " + String(thermal.status))
      : "--";
    $("#resource-window").textContent = performance.foreground_window_name || context.foreground_activity || "--";

    const cadenceParts = [];
    if (finite(performance.current_refresh_rate_hz)) cadenceParts.push(Number(performance.current_refresh_rate_hz).toFixed(0) + " Hz");
    if (finite(performance.sampled_frame_rate_fps)) cadenceParts.push(Number(performance.sampled_frame_rate_fps).toFixed(1) + " FPS");
    if (finite(performance.cadence_multiplier)) cadenceParts.push("约 " + Number(performance.cadence_multiplier).toFixed(0) + "×");
    $("#resource-cadence").textContent = cadenceParts.join(" / ") || "--";
    $("#resource-whole-power").textContent = finite(latest.power_mw)
      ? (Number(latest.power_mw) / 1000).toFixed(3) + " W · 只记录整机"
      : "--";
    $("#resource-snapshot-source").textContent = Number(monitor.scheduler_snapshot_count || 0)
      ? String(monitor.scheduler_snapshot_count) + " snapshots"
      : platform === "harmony" ? "PowerManager / SmartPerf" : "cpuset / ADPF";

    $("#performance-frame-source").textContent = performance.frame_source || "平台窗口 / 合成器计数";
    $("#performance-frame-confidence").textContent = performance.one_percent_low_confidence
      ? "1% Low 置信度：" + performance.one_percent_low_confidence
      : performance.frame_unavailable_reason || "等待有效帧计数";
    $("#performance-render-source").textContent = performance.render_resolution_source || "--";
    $("#performance-render-scale").textContent = finite(performance.render_scale_pct)
      ? "相对显示模式 " + Number(performance.render_scale_pct).toFixed(1) + "%"
      : "等待前台窗口边界";
    $("#performance-interpolation-source").textContent = performance.frame_interpolation_label || "--";
    const interpolationEvidence = Array.isArray(performance.frame_interpolation_evidence)
      ? performance.frame_interpolation_evidence
      : [];
    $("#performance-interpolation-evidence").textContent = interpolationEvidence.length
      ? interpolationEvidence[0]
      : "无显式开关时，不会仅凭刷新倍率推断插帧。";
  }

  function renderPowerPressure(active) {
    if (activePlatform(active) === "ios") {
      const latest = active?.latest || {};
      const summary = active?.summary || {};
      const processes = Array.isArray(active?.system_monitor?.processes)
        ? active.system_monitor.processes
        : [];
      const physicalRows = [
        ["当前整机功耗", finite(latest.power_mw) ? `${(Number(latest.power_mw) / 1000).toFixed(3)} W` : "--", "PowerTelemetry SystemLoad"],
        ["平均整机功耗", finite(summary.average_power_mw) ? `${(Number(summary.average_power_mw) / 1000).toFixed(3)} W` : "--", "有效物理样本"],
        ["物理样本年龄", finite(latest.power_sample_age_s) ? `${Number(latest.power_sample_age_s).toFixed(1)} s` : "--", "通常约 20 秒刷新"],
      ];
      $("#power-pressure-driver-list").innerHTML = physicalRows.map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item[0])}</strong><small>${escapeHtml(item[2])}</small></div><b>${escapeHtml(item[1])}</b></div>`).join("");
      $("#power-pressure-task-list").innerHTML = processes.length ? processes.slice(0, 7).map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item.name || item.command || "process")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${formatBytes(item.resident_bytes)}</small></div><b>${formatNumber(item.cpu_pct, 1)}%</b></div>`).join("") : '<div class="empty-row">等待 DVT 进程快照</div>';
      const overhead = finite(latest.collector_cpu_pct)
        ? Number(latest.collector_cpu_pct)
        : finite(summary.average_collector_cpu_pct) ? Number(summary.average_collector_cpu_pct) : null;
      $("#power-pressure-setting-list").innerHTML = [
        ["collector_cpu_pct", overhead === null ? "--" : `${overhead.toFixed(2)}%`, "sysmond / DTServiceHub / pairing daemon"],
        ["GPU utilization", finite(latest.gpu_load_pct) ? `${Number(latest.gpu_load_pct).toFixed(1)}%` : "--", "DVT 诊断遥测"],
        ["相对 powerScore", "仅旁证", "不转换为 mW 或应用物理功耗"],
      ].map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item[0])}</strong><small>${escapeHtml(item[2])}</small></div><b>${escapeHtml(item[1])}</b></div>`).join("");
      return;
    }
    const pressure = active?.power_pressure || {};
    const drivers = Array.isArray(pressure.drivers) ? pressure.drivers : [];
    const tasks = Array.isArray(pressure.tasks) ? pressure.tasks : [];
    const settings = pressure.settings || active?.runtime_settings || {};
    const settingRows = Array.isArray(settings.rows) ? settings.rows : [];
    const platform = activePlatform(active);
    const powerFlow = powerFlowPresentation(active);
    $("#power-pressure-note").innerHTML = powerFlow.direction === "charging"
      ? "<strong>当前为充电状态：</strong>资源变化与这里显示的电池充入功率相关，不等于续航功耗，也不应用于组件或任务耗电归因；请断开外部供电后进行功耗测试。"
      : platform === "harmony"
        ? "<strong>边界：</strong>BatteryService 电流是整机电池侧数据；CPU/GPU/DDR 与进程相关性用于解释趋势，不等于独立电源轨或 Android BatteryStats 归因。"
        : "<strong>边界：</strong>相关系数用于解释电流 / 功率和资源压力是否同步变化，不代表独立硬件电源轨，也不能把多个压力项直接相加。";
    $("#power-pressure-driver-list").innerHTML = drivers.length ? drivers.slice(0, 7).map(item => {
      const correlation = finite(item.correlation) ? Number(item.correlation) : null;
      const width = correlation === null ? 0 : clamp(Math.abs(correlation) * 100, 0, 100);
      return `<div class="pressure-row"><div><strong>${escapeHtml(item.label || item.key || "资源")}</strong><small>${Number(item.sample_count || 0)} samples</small></div><div class="pressure-track"><span style="width:${width.toFixed(1)}%"></span></div><b>${correlation === null ? "--" : `r=${correlation.toFixed(2)}`}</b></div>`;
    }).join("") : '<div class="empty-row">等待至少 3 个有效样本</div>';
    $("#power-pressure-task-list").innerHTML = tasks.length ? tasks.slice(0, 7).map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item.name || "unknown")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${escapeHtml(item.state || "--")}</small></div><b>${formatNumber(item.cpu_pct, 1)}%</b></div>`).join("") : '<div class="empty-row">等待系统进程快照</div>';
    if (settingRows.length) {
      $("#power-pressure-setting-list").innerHTML = settingRows.slice(0, 8).map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item.label || item.key)}</strong><small>${escapeHtml(item.impact || "")}</small></div><b>${escapeHtml(item.end ?? item.start ?? "--")}${item.changed ? "*" : ""}</b></div>`).join("");
    } else if (platform === "harmony") {
      const context = active?.context || {};
      const performance = active?.performance || {};
      const mode = active?.metadata?.device_performance_mode || {};
      const harmonyRows = [
        ["屏幕状态", context.screen_state || "--", "PowerManagerService"],
        ["刷新率", finite(performance.current_refresh_rate_hz) ? `${Number(performance.current_refresh_rate_hz).toFixed(0)} Hz` : "--", "RenderService"],
        ["设备性能模式", mode.applied ? `602${mode.restored ? " → 已恢复" : ""}` : mode.requested ? "请求失败或待确认" : "正常模式", "power-shell"],
      ];
      $("#power-pressure-setting-list").innerHTML = harmonyRows.map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item[0])}</strong><small>${escapeHtml(item[2])}</small></div><b>${escapeHtml(item[1])}</b></div>`).join("");
    } else {
      $("#power-pressure-setting-list").innerHTML = '<div class="empty-row">等待设置快照</div>';
    }
  }

  function renderRenderPipeline(active) {
    const platform = activePlatform(active);
    const performance = active?.performance || {};
    const frameFlow = performance.frame_flow || {};
    const flowStages = Array.isArray(frameFlow.stages) ? frameFlow.stages : [];
    const render = active?.render_performance || {};
    const pipeline = render.pipeline || performance.render_pipeline || {};
    const dominant = render.dominant_stage || pipeline.dominant_stage || {};
    const stages = Array.isArray(pipeline.stages) ? pipeline.stages : [];
    const monitorThreads = Array.isArray(active?.system_monitor?.threads) ? active.system_monitor.threads : [];
    const threads = Array.isArray(render.render_threads) && render.render_threads.length
      ? render.render_threads
      : monitorThreads.filter(item => /renderthread|surfaceflinger|renderengine|composer|hwc|gpu|main/i.test(`${item.name || ""} ${item.process || ""}`)).slice(0, 10);
    const power = render.power_recording || {};
    const statusLabels = {
      primary: "主数据",
      valid: "有效",
      reference: "仅参考",
      invalid: "无效",
      unavailable: "无数据",
    };
    const allowedStatuses = new Set(Object.keys(statusLabels));
    const primaryStage = flowStages.find(item => item?.key === frameFlow.primary_key)
      || flowStages.find(item => item?.status === "primary");
    $("#render-pipeline-source").textContent = primaryStage
      ? `主数据 · ${primaryStage.phase || primaryStage.label || "FPS"}`
      : pipeline.available
        ? `${Number(pipeline.frame_count || 0)} detailed frames`
        : "未找到有效主帧率";
    const summaryParts = [];
    if (primaryStage) summaryParts.push(`主数据：${primaryStage.label || primaryStage.phase}`);
    if (Number(frameFlow.valid_count || 0)) summaryParts.push(`有效阶段 ${Number(frameFlow.valid_count)} 个`);
    if (Number(frameFlow.reference_count || 0)) summaryParts.push(`参考 ${Number(frameFlow.reference_count)} 个`);
    if (Number(frameFlow.invalid_count || 0)) summaryParts.push(`无效 ${Number(frameFlow.invalid_count)} 个`);
    $("#frame-flow-summary").textContent = summaryParts.length
      ? summaryParts.join(" · ")
      : "当前会话尚未形成可判定的帧率数据流。";
    $("#frame-flow-note").innerHTML = `<strong>判定边界：</strong>${escapeHtml(frameFlow.note || "应用提交速率、合成器呈现 FPS 与屏幕刷新率不能直接互换；无效来源保留展示但不会参与主 FPS 和 1% Low。")}`;
    $("#frame-flow-list").innerHTML = flowStages.length ? flowStages.map((item, index) => {
      const status = allowedStatuses.has(String(item.status)) ? String(item.status) : "unavailable";
      const valueDigits = item.unit === "Hz" ? 0 : item.unit === "ms" ? 2 : 1;
      const value = finite(item.value) ? Number(item.value).toFixed(valueDigits) : "--";
      const metrics = Array.isArray(item.metrics) ? item.metrics.filter(metric => finite(metric?.value)) : [];
      const metricHtml = metrics.length ? `<div class="frame-flow-metrics">${metrics.slice(0, 3).map(metric => `<span><small>${escapeHtml(metric.label || "指标")}</small><b>${Number(metric.value).toFixed(Number(metric.digits ?? 1))}${metric.unit ? ` ${escapeHtml(metric.unit)}` : ""}</b></span>`).join("")}</div>` : "";
      const sampleText = finite(item.sample_count) && Number(item.sample_count) > 0
        ? `${Number(item.sample_count)} samples`
        : item.confidence ? `${escapeHtml(item.confidence)} confidence` : "";
      return `<article class="frame-flow-stage ${status}">
        <header><span>${String(index + 1).padStart(2, "0")} · ${escapeHtml(item.phase || "STAGE")}</span><i>${statusLabels[status]}</i></header>
        <h4>${escapeHtml(item.label || item.key || "帧阶段")}</h4>
        <div class="frame-flow-value"><strong>${value}</strong><small>${escapeHtml(item.unit || "")}</small></div>
        <span class="frame-flow-value-label">${escapeHtml(item.value_label || "当前阶段")}</span>
        ${metricHtml}
        <p class="frame-flow-source" title="${escapeHtml(item.source || "")}">${escapeHtml(item.source || "未记录来源")}</p>
        <p class="frame-flow-detail">${escapeHtml(item.detail || "暂无判定说明")}</p>
        ${sampleText ? `<footer>${sampleText}</footer>` : ""}
      </article>`;
    }).join("") : '<div class="empty-row">等待应用、渲染、合成与显示阶段帧数据</div>';
    $("#render-pipeline-dominant").textContent = dominant.label || "--";
    $("#render-pipeline-dominant-detail").textContent = finite(dominant.p95_ms)
      ? `P95 ${Number(dominant.p95_ms).toFixed(2)} ms · P99 ${formatNumber(dominant.p99_ms, 2)} ms`
      : pipeline.limitations || (platform === "harmony"
        ? "HarmonyOS 量产接口提供帧抖动与合成节奏，不提供 Android framestats 阶段时间戳"
        : "等待详细帧时间戳");
    const averageWholePower = finite(power.average_power_mw) ? power.average_power_mw : active?.summary?.average_power_mw;
    $("#performance-whole-power").textContent = finite(averageWholePower)
      ? `${(Number(averageWholePower) / 1000).toFixed(3)} W`
      : "--";
    $("#render-pipeline-stage-list").innerHTML = stages.length ? stages.slice(0, 10).map(item => `<div class="pipeline-row"><div><strong>${escapeHtml(item.label || item.key)}</strong><small>${Number(item.sample_count || 0)} frames</small></div><div class="pipeline-track"><span style="width:${clamp(Number(item.p95_ms || 0) / Math.max(16.67, ...stages.map(stage => Number(stage.p95_ms || 0))) * 100, 0, 100).toFixed(1)}%"></span></div><b>${formatNumber(item.p95_ms, 2)} ms</b></div>`).join("") : `<div class="empty-row">${platform === "harmony" ? "当前仅提供帧抖动与合成节奏，不拆分 Android 渲染阶段" : "等待详细 framestats"}</div>`;
    $("#render-thread-list").innerHTML = threads.length ? threads.slice(0, 10).map(item => {
      const cpu = finite(item.cpu_pct) ? item.cpu_pct : finite(item.average_when_visible_cpu_pct) ? item.average_when_visible_cpu_pct : item.maximum_cpu_pct;
      return `<div class="pipeline-row compact"><div><strong>${escapeHtml(item.name || "thread")}</strong><small>${escapeHtml(item.process || "--")} · ${escapeHtml(item.pid ?? "--")}/${escapeHtml(item.tid ?? "--")}</small></div><b>${formatNumber(cpu, 1)}%</b></div>`;
    }).join("") : `<div class="empty-row">${platform === "harmony" ? "HarmonyOS 当前不启用全系统热点线程扫描" : "等待线程快照"}</div>`;
  }

  function renderSession(active) {
    const strip = $("#session-strip");
    const orbit = $("#record-orbit");
    const stopButton = $("#stop-record");
    const markerButton = $("#add-marker");
    const reportLink = $("#open-report");
    const running = Boolean(active?.running);
    const status = active?.status || "idle";
    const [label, kicker] = statusDefinitions[status] || ["等待开始", "SESSION READY"];
    strip.className = "session-strip";
    if (running) strip.classList.add("recording");
    if (status === "failed") strip.classList.add("failed");
    orbit.className = "record-orbit";
    if (running) orbit.classList.add("active");
    if (["complete", "collected", "recovered"].includes(status)) orbit.classList.add("complete");
    $("#session-kicker").textContent = kicker;
    const defaultSessionTitle = (active?.test_mode || active?.metadata?.test_mode) === "performance"
      ? "Performance session"
      : "Power session";
    $("#session-title").textContent = active ? `${label} · ${active.title || active.run_name || defaultSessionTitle}` : "等待开始新的采集";
    const elapsed = Number(active?.elapsed_s || 0);
    const requested = Number(active?.requested_duration_s || 0);
    $("#session-time").textContent = `${formatDuration(elapsed)} / ${formatDuration(requested)}`;
    $("#session-samples").textContent = `${active?.sample_count || 0} samples`;
    $("#session-progress").style.width = `${clamp(Number(active?.progress || 0), 0, 1) * 100}%`;
    stopButton.classList.toggle("hidden", !running);
    markerButton.classList.toggle("hidden", !running);
    reportLink.classList.toggle("hidden", !active?.report_ready);
    $("#run-probe").disabled = running;
    if (active?.report_url) reportLink.href = active.report_url;
    $$("#record-form input, #record-form select, #record-form button").forEach(control => {
      if (control.id === "start-record") return;
      control.disabled = running;
    });
    $("#start-record").disabled = running || !selectedDevice();
    $("#start-record").querySelector("span:last-child").textContent = running
      ? "采集中"
      : app.testMode === "performance" ? "开始性能测试" : "开始功耗测试";
    $$("[data-test-mode]").forEach(button => { button.disabled = running; });
    $$(".platform-switch [data-platform]").forEach(button => { button.disabled = running; });
    $("#system-monitor-input").disabled = running;
    updateCaptureFeatureControls();
  }

  function renderClusters(active) {
    const container = $("#cluster-list");
    if (activePlatform(active) === "ios") {
      const processes = Array.isArray(active?.system_monitor?.processes)
        ? active.system_monitor.processes.slice(0, 6)
        : [];
      if (!processes.length) {
        container.innerHTML = '<div class="empty-row">等待 DVT 进程 CPU 数据</div>';
        return;
      }
      container.innerHTML = processes.map(process => {
        const load = finite(process.cpu_pct) ? clamp(Number(process.cpu_pct), 0, 100) : 0;
        return `<div class="cluster-row"><div class="cluster-name"><strong>${escapeHtml(process.name || process.command || "process")}</strong><small>PID ${escapeHtml(process.pid ?? "--")}</small></div><div class="cluster-bar"><span style="width:${load.toFixed(1)}%"></span></div><div class="cluster-load">${finite(process.cpu_pct) ? `${Number(process.cpu_pct).toFixed(1)}%` : "--"}</div><div class="cluster-frequency"><span>${formatBytes(process.resident_bytes)}</span><small> memory</small></div></div>`;
      }).join("");
      return;
    }
    const clusters = Array.isArray(active?.clusters) ? active.clusters : [];
    if (!clusters.length) {
      container.innerHTML = '<div class="empty-row">等待 CPU 数据</div>';
      return;
    }
    container.innerHTML = clusters.map(cluster => {
      const load = finite(cluster.load_pct) ? clamp(Number(cluster.load_pct), 0, 100) : 0;
      const frequency = finite(cluster.frequency_mhz) ? Number(cluster.frequency_mhz) : null;
      const maximum = finite(cluster.maximum_mhz) ? Number(cluster.maximum_mhz) : null;
      return `<div class="cluster-row">
        <div class="cluster-name"><strong>${escapeHtml(cluster.label || cluster.name)}</strong><small>cores ${(cluster.cores || []).join(", ") || "--"}</small></div>
        <div class="cluster-bar"><span style="width:${load.toFixed(1)}%"></span></div>
        <div class="cluster-load">${finite(cluster.load_pct) ? `${Number(cluster.load_pct).toFixed(1)}%` : "--"}</div>
        <div class="cluster-frequency"><span>${frequency === null ? "--" : `${frequency.toFixed(0)} MHz`}</span><small>${maximum === null ? "" : `/ ${maximum.toFixed(0)}`}</small></div>
      </div>`;
    }).join("");
  }

  function renderContext(active) {
    const context = active?.context || {};
    const performance = active?.performance || context.performance || {};
    const pressure = active?.power_pressure || {};
    const settings = active?.runtime_settings || {};
    const monitor = active?.system_monitor || {};
    const scheduler = monitor.scheduler || {};
    const battery = active?.battery || {};
    $("#context-package").textContent = context.foreground_package || "--";
    $("#context-activity").textContent = [context.foreground_activity, performance.foreground_window_name].filter(Boolean).join(" / ") || "--";
    $("#context-screen").textContent = context.screen_state || "--";
    if (activePlatform(active) === "ios") {
      const latest = active?.latest || {};
      $("#context-display-settings").textContent = "当前 sidecar 不采集亮度 / 刷新率设置";
      $("#context-pressure-driver").textContent = finite(latest.gpu_load_pct)
        ? `GPU ${Number(latest.gpu_load_pct).toFixed(1)}%`
        : finite(latest.cpu_pct) ? `CPU ${Number(latest.cpu_pct).toFixed(1)}%` : "等待 DVT 样本";
      const processes = Array.isArray(monitor.processes) ? monitor.processes : [];
      const leading = processes.length ? [...processes].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0] : null;
      $("#context-pressure-task").textContent = leading
        ? `${leading.name || "process"} · ${formatNumber(leading.cpu_pct, 1)}% CPU`
        : "等待 DVT 进程快照";
      $("#context-power-scheduler").textContent = "iOS 不公开 Android cpuset / Governor / ADPF";
      const voltage = finite(latest.voltage_mv) ? `${(Number(latest.voltage_mv) / 1000).toFixed(3)} V` : "--";
      const level = finite(battery.level_pct) ? `${Number(battery.level_pct).toFixed(0)}%` : "--";
      $("#context-battery").textContent = `${voltage} / ${level}`;
      $("#context-reconnect").textContent = String(active?.checkpoint?.reconnect_count ?? active?.metadata?.reconnect_count ?? 0);
      $("#context-source").textContent = context.source || "iOS DVT application state";
      return;
    }
    const startSettings = settings.start || {};
    const refresh = finite(performance.current_refresh_rate_hz)
      ? `${Number(performance.current_refresh_rate_hz).toFixed(0)} Hz`
      : finite(context.refresh_rate_hz) ? `${Number(context.refresh_rate_hz).toFixed(0)} Hz` : "--";
    const brightness = startSettings["system.screen_brightness"] ?? performance.brightness_raw ?? "--";
    const lowPower = String(startSettings["global.low_power"] ?? "--") === "1" ? "省电开" : "省电关";
    $("#context-display-settings").textContent = `${brightness} brightness / ${refresh} / ${lowPower}`;
    const leadingDriver = pressure.leading_driver || {};
    $("#context-pressure-driver").textContent = leadingDriver.label
      ? `${leadingDriver.label} · r=${formatNumber(leadingDriver.correlation, 2)}`
      : "等待相关性样本";
    const tasks = Array.isArray(pressure.tasks) ? pressure.tasks : [];
    const leadingTask = tasks.length ? [...tasks].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0] : null;
    $("#context-pressure-task").textContent = leadingTask
      ? `${leadingTask.name || "unknown"} · ${formatNumber(leadingTask.cpu_pct, 1)}% CPU`
      : "等待进程快照";
    const policies = Array.isArray(scheduler.cpu_policies) ? scheduler.cpu_policies : [];
    const governors = [...new Set(policies.map(item => item.governor).filter(Boolean))];
    const topApp = scheduler.cpusets?.["top-app"] || "--";
    $("#context-power-scheduler").textContent = `${topApp} / ${governors.join(" / ") || "governor --"}`;
    const voltage = finite(active?.latest?.voltage_mv) ? `${(Number(active.latest.voltage_mv) / 1000).toFixed(3)} V` : "--";
    const level = finite(battery.level_pct) ? `${Number(battery.level_pct).toFixed(0)}%` : "--";
    $("#context-battery").textContent = `${voltage} / ${level}`;
    $("#context-reconnect").textContent = String(active?.checkpoint?.reconnect_count ?? active?.metadata?.reconnect_count ?? 0);
    $("#context-source").textContent = context.source || "sampler";
  }

  function renderIosPerformanceSystem(active, monitor, processes, priority, hottest) {
    const latest = active?.latest || {};
    const context = active?.context || {};
    const systemCount = Number(monitor.system_snapshot_count || 0);
    $("#performance-snapshot-status").textContent = systemCount
      ? `${systemCount} 个 DVT 进程快照`
      : active?.running ? "等待首个 DVT 性能样本" : "尚无 DVT 性能样本";
    $("#performance-refresh-value").textContent = formatMetric(latest.cpu_pct, "%", 1);
    $("#performance-refresh-label").textContent = "DVT 进程 CPU 归一化";
    $("#performance-fps-value").textContent = formatMetric(latest.gpu_load_pct, "%", 1);
    $("#performance-fps-label").textContent = "DVT GPU utilization";
    $("#performance-frame-value").textContent = finite(latest.power_mw)
      ? `${(Number(latest.power_mw) / 1000).toFixed(3)} W`
      : "--";
    $("#performance-frame-label").textContent = finite(latest.power_sample_age_s)
      ? `PowerTelemetry · age ${Number(latest.power_sample_age_s).toFixed(1)} s`
      : "PowerTelemetry SystemLoad";
    $("#performance-touch-value").textContent = formatMetric(latest.collector_cpu_pct, "%", 2);
    $("#performance-touch-label").textContent = "归一化采集器 CPU 开销";

    const resourceRows = [
      { label: "CPU", value: latest.cpu_pct, unit: "%", scale: 100 },
      { label: "GPU", value: latest.gpu_load_pct, unit: "%", scale: 100 },
      { label: "Observer", value: latest.collector_cpu_pct, unit: "%", scale: 25 },
    ].filter(item => item.label !== "GPU" || finite(item.value));
    $("#performance-residency-source").textContent = "DVT / PowerTelemetry";
    $("#performance-residency-list").innerHTML = resourceRows.map(item => {
      const value = finite(item.value) ? Number(item.value) : null;
      const share = value === null ? 0 : clamp(value / item.scale * 100, 0, 100);
      return `<div class="performance-residency-row"><div><strong>${escapeHtml(item.label)}</strong><small>${value === null ? "--" : `${value.toFixed(item.label === "Observer" ? 2 : 1)} ${item.unit}`}</small></div><div class="performance-residency-track"><span style="width:${share.toFixed(1)}%"></span></div><b>${value === null ? "--" : value.toFixed(1)}</b></div>`;
    }).join("");

    $("#performance-window-value").textContent = context.foreground_package || active?.metadata?.target_package || "--";
    $("#performance-display-value").textContent = "iOS 显示参数未由当前 sidecar 采集";
    $("#performance-gpu-value").textContent = finite(latest.gpu_load_pct)
      ? `${Number(latest.gpu_load_pct).toFixed(1)}% utilization`
      : "--";
    $("#performance-temperature-value").textContent = hottest && finite(hottest.value_c)
      ? `${Number(hottest.value_c).toFixed(1)} °C · ${hottest.name || "sensor"}`
      : finite(latest.temperature_c) ? `${Number(latest.temperature_c).toFixed(1)} °C · battery` : "--";
    $("#performance-switch-value").textContent = context.foreground_package ? "事件驱动" : "--";
    $("#performance-frame-count").textContent = String(systemCount);

    const thermal = monitor.thermal || {};
    const thermalStatus = finite(thermal.status) ? Number(thermal.status) : 0;
    const overhead = finite(latest.collector_cpu_pct) ? Number(latest.collector_cpu_pct) : 0;
    const banner = $("#system-priority-banner");
    const hasIssue = priority.length > 0 || thermalStatus > 0 || overhead >= 10;
    banner.classList.toggle("active", hasIssue);
    banner.classList.toggle("idle", !hasIssue);
    if (priority.length) {
      const leading = [...priority].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0];
      $("#system-priority-title").textContent = "检测到 iOS 后台资源竞争";
      $("#system-priority-detail").textContent = `${leading.name || leading.watch_name || "后台进程"} 当前 CPU ${formatNumber(leading.cpu_pct, 1)}%。`;
      $("#system-priority-badge").textContent = "ACTIVE";
    } else if (overhead >= 10) {
      $("#system-priority-title").textContent = "采集器开销偏高";
      $("#system-priority-detail").textContent = `collector_cpu_pct 为 ${overhead.toFixed(2)}%，建议关闭进程快照或提高采样周期。`;
      $("#system-priority-badge").textContent = "OBSERVER";
    } else if (thermalStatus > 0) {
      $("#system-priority-title").textContent = `检测到热状态 ${thermalStatus}`;
      $("#system-priority-detail").textContent = "请结合 DVT CPU/GPU 与电池温度判断持续性能下降。";
      $("#system-priority-badge").textContent = "THERMAL";
    } else {
      $("#system-priority-title").textContent = "未检测到明显 iOS 性能干扰";
      $("#system-priority-detail").textContent = "DVT 进程、GPU、温度和观察者开销均未触发异常阈值。";
      $("#system-priority-badge").textContent = "CLEAR";
    }

    $("#watched-process-body").innerHTML = priority.length ? priority.map(item => `<tr class="priority-row"><td><span class="process-identity"><strong>${escapeHtml(item.watch_label || item.watch_name || item.name || "unknown")}</strong><small>${escapeHtml(item.command || item.name || "--")}</small></span></td><td>${escapeHtml(item.pid ?? "--")}</td><td>${finite(item.cpu_pct) ? `${Number(item.cpu_pct).toFixed(1)}%` : "--"}</td><td><span class="activity-pill active">ACTIVE</span></td></tr>`).join("") : '<tr><td colspan="4" class="table-empty">未检测到明显 iOS 后台异常。</td></tr>';
    $("#system-process-body").innerHTML = processes.length ? processes.slice(0, 5).map(item => `<tr><td><span class="process-identity"><strong>${escapeHtml(item.name || item.command || "unknown")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${escapeHtml(item.user || "--")}</small></span></td><td><strong class="cpu-value">${formatNumber(item.cpu_pct, 1)}%</strong></td><td>${finite(item.mem_pct) ? `${formatNumber(item.mem_pct, 1)}%` : formatBytes(item.resident_bytes)}</td><td>${escapeHtml(item.state || "--")}</td></tr>`).join("") : '<tr><td colspan="4" class="table-empty">暂无 DVT 进程快照。</td></tr>';
  }

  function renderSystem(active) {
    const platform = activePlatform(active);
    const isIos = platform === "ios";
    const isHarmony = platform === "harmony";
    const performance = active?.performance || {};
    const monitor = active?.system_monitor || {};
    const processes = Array.isArray(monitor.processes) ? monitor.processes : [];
    const priority = Array.isArray(monitor.active_priority) ? monitor.active_priority : [];
    const thermal = monitor.thermal || {};
    const temperatures = Array.isArray(thermal.temperatures) ? thermal.temperatures : [];
    const thermalTemperatures = temperatures.filter(isThermalTemperature);
    const hottest = thermalTemperatures.length ? [...thermalTemperatures].sort((a, b) => Number(b.value_c || 0) - Number(a.value_c || 0))[0] : null;
    if (isIos) {
      renderIosPerformanceSystem(active, monitor, processes, priority, hottest);
      return;
    }
    const contextCount = Number(performance.context_sample_count || 0);
    const systemCount = Number(monitor.system_snapshot_count || 0);
    $("#performance-snapshot-status").textContent = contextCount
      ? `${contextCount} 个性能上下文 · ${systemCount} 个进程快照`
      : active?.running ? "等待首个性能上下文" : "尚无性能上下文";

    const currentRefresh = finite(performance.current_refresh_rate_hz) ? Number(performance.current_refresh_rate_hz) : null;
    const peakRefresh = finite(performance.peak_refresh_rate_hz) ? Number(performance.peak_refresh_rate_hz) : null;
    $("#performance-refresh-value").textContent = currentRefresh === null ? "--" : `${currentRefresh.toFixed(0)} Hz`;
    $("#performance-refresh-label").textContent = peakRefresh === null ? "当前显示档位" : `设备最高 ${peakRefresh.toFixed(0)} Hz`;
    const sampledFrameRate = finite(performance.sampled_frame_rate_fps) ? performance.sampled_frame_rate_fps : performance.sampled_compositor_fps;
    const minimumFrameRate = finite(performance.minimum_sampled_frame_rate_fps) ? performance.minimum_sampled_frame_rate_fps : performance.minimum_sampled_compositor_fps;
    const frameRateUnit = performance.frame_rate_unit || "FPS";
    const frameMetricP95 = finite(performance.frame_metric_p95_ms) ? performance.frame_metric_p95_ms : performance.frame_interval_p95_ms;
    const frameIssuePct = finite(performance.frame_issue_pct) ? performance.frame_issue_pct : performance.missed_vsync_interval_pct;
    $("#performance-fps-value").textContent = finite(sampledFrameRate) ? `${Number(sampledFrameRate).toFixed(1)}` : "--";
    $("#performance-fps-label").textContent = finite(minimumFrameRate) ? `${performance.frame_rate_label || "帧率"} · 最低 ${Number(minimumFrameRate).toFixed(1)} ${frameRateUnit}` : (performance.frame_unavailable_reason || performance.frame_rate_label || "帧率抽样");
    $("#performance-frame-value").textContent = finite(frameMetricP95) ? `${Number(frameMetricP95).toFixed(2)} ms` : "--";
    $("#performance-frame-label").textContent = finite(frameIssuePct) ? `${Number(frameIssuePct).toFixed(2)}% ${performance.frame_issue_label || "异常"}` : (performance.frame_metric_label || "帧指标 P95");
    $("#performance-touch-value").textContent = finite(performance.touch_interaction_count) ? String(Number(performance.touch_interaction_count).toFixed(0)) : "--";
    $("#performance-touch-label").textContent = finite(performance.touch_interactions_per_minute) ? `${Number(performance.touch_interactions_per_minute).toFixed(1)} 次/分钟 · 硬件采样率未公开` : "硬件采样率未公开";

    const residency = Array.isArray(performance.refresh_residency) ? performance.refresh_residency : [];
    $("#performance-residency-source").textContent = performance.refresh_residency_source || "平台累计计数";
    $("#performance-residency-list").innerHTML = residency.length ? residency.map(item => {
      const share = clamp(Number(item.share_pct || 0), 0, 100);
      const duration = finite(item.estimated_duration_s) ? `${Number(item.estimated_duration_s).toFixed(1)} s` : "--";
      return `<div class="performance-residency-row">
        <div><strong>${formatNumber(item.refresh_rate_hz, 0)} Hz</strong><small>${duration}</small></div>
        <div class="performance-residency-track"><span style="width:${share.toFixed(2)}%"></span></div>
        <b>${share.toFixed(1)}%</b>
      </div>`;
    }).join("") : '<div class="empty-row">当前平台尚未提供会话内刷新档位计数。</div>';

    const resolution = finite(performance.display_width_px) && finite(performance.display_height_px)
      ? `${Number(performance.display_width_px).toFixed(0)} × ${Number(performance.display_height_px).toFixed(0)}`
      : "--";
    const brightness = finite(performance.brightness_raw) ? Number(performance.brightness_raw).toFixed(0) : "--";
    $("#performance-window-value").textContent = performance.foreground_window_name ? `${performance.foreground_window_name}${finite(performance.foreground_window_id) ? ` · #${performance.foreground_window_id}` : ""}` : "--";
    $("#performance-display-value").textContent = `${resolution} / ${brightness}`;
    $("#performance-gpu-value").textContent = performance.gpu_renderer || "--";
    $("#performance-temperature-value").textContent = hottest && finite(hottest.value_c) ? `${Number(hottest.value_c).toFixed(1)} °C · ${hottest.name || "sensor"}` : finite(active?.latest?.temperature_c) ? `${Number(active.latest.temperature_c).toFixed(1)} °C · battery` : "--";
    $("#performance-switch-value").textContent = `${Number(performance.refresh_switch_count || 0)} 次`;
    $("#performance-frame-count").textContent = Number(performance.frame_sample_count || 0).toLocaleString("zh-CN");

    const banner = $("#system-priority-banner");
    const missedPct = finite(performance.frame_issue_pct) ? Number(performance.frame_issue_pct) : finite(performance.missed_vsync_interval_pct) ? Number(performance.missed_vsync_interval_pct) : 0;
    const thermalStatus = finite(thermal.status) ? Number(thermal.status) : 0;
    const hasFrameIssue = missedPct >= 2 || Number(performance.severe_frame_interval_count || 0) > 0;
    const hasIssue = priority.length > 0 || hasFrameIssue || thermalStatus > 0;
    banner.classList.toggle("active", hasIssue);
    banner.classList.toggle("idle", !hasIssue);
    if (priority.length) {
      const leading = [...priority].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0];
      const names = priority.map(item => item.watch_label || item.watch_name || item.name).filter(Boolean);
      $("#system-priority-title").textContent = `检测到 ${names.join(" / ")}`;
      $("#system-priority-detail").textContent = `后台系统活动可能造成 CPU 调度竞争、I/O 阻塞或缓存压力。当前最高 CPU ${formatNumber(leading.cpu_pct, 1)}%。`;
      $("#system-priority-badge").textContent = "ACTIVE";
    } else if (hasFrameIssue) {
      $("#system-priority-title").textContent = "检测到帧节奏异常";
      $("#system-priority-detail").textContent = `${performance.frame_issue_label || "帧异常"}占 ${missedPct.toFixed(2)}%，累计异常帧 ${Number(performance.frame_issue_count || performance.severe_frame_interval_count || 0)} 个。`;
      $("#system-priority-badge").textContent = "FRAME";
    } else if (thermalStatus > 0) {
      $("#system-priority-title").textContent = `检测到热状态 ${thermalStatus}`;
      $("#system-priority-detail").textContent = `平台热严重度为 ${thermalStatusDefinitions[thermalStatus] || "升高"}；请结合 CPU/GPU 频率上限判断是否发生热降频。`;
      $("#system-priority-badge").textContent = "THERMAL";
    } else {
      $("#system-priority-title").textContent = "未检测到明显性能干扰";
      $("#system-priority-detail").textContent = `${isHarmony ? "鸿蒙" : isIos ? "iOS" : "Android"} 后台活动、热状态与抽样帧节奏均未触发异常阈值。`;
      $("#system-priority-badge").textContent = "CLEAR";
    }

    $("#watched-process-body").innerHTML = priority.length ? priority.map(item => `<tr class="priority-row">
      <td><span class="process-identity"><strong>${escapeHtml(item.watch_label || item.watch_name || item.name || "unknown")}</strong><small title="${escapeHtml(item.command || "")}">${escapeHtml(item.command || item.name || "--")}</small></span></td>
      <td>${escapeHtml(item.pid ?? "--")}</td>
      <td>${finite(item.cpu_pct) ? `${Number(item.cpu_pct).toFixed(1)}%` : "--"}</td>
      <td><span class="activity-pill active">ACTIVE</span></td>
    </tr>`).join("") : '<tr><td colspan="4" class="table-empty">未检测到后台更新、安装或编译异常。</td></tr>';

    $("#system-process-body").innerHTML = processes.length ? processes.slice(0, 5).map(item => `<tr>
      <td><span class="process-identity"><strong>${escapeHtml(item.name || item.command || "unknown")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${escapeHtml(item.user || "--")} · ${escapeHtml(item.category || "other")}</small></span></td>
      <td><strong class="cpu-value">${formatNumber(item.cpu_pct, 1)}%</strong></td>
      <td>${finite(item.mem_pct) ? `${formatNumber(item.mem_pct, 1)}%` : formatBytes(item.resident_bytes)}</td>
      <td>${escapeHtml(item.policy || "--")} / ${escapeHtml(item.state || "--")}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="table-empty">暂无进程快照；只在这里显示当前前 5 项。</td></tr>';
  }

  function renderSchedulerHistory(active) {
    const panel = $("#scheduler-history-panel");
    const svg = $("#scheduler-history-chart");
    const empty = $("#scheduler-history-empty");
    if (!panel || !svg || !empty) return;
    const platform = activePlatform(active);
    const enabled = platform !== "ios" && captureFeatureEnabled(active, "scheduler");
    panel.classList.toggle("hidden", !enabled);
    if (!enabled) return;
    const series = Array.isArray(active?.scheduler_series)
      ? active.scheduler_series.filter(item => finite(item.elapsed_s))
      : [];
    const latest = series.at(-1) || {};
    $("#scheduler-history-source").textContent = series.length
      ? `${series.length} 个调度快照`
      : active?.running ? "等待调度快照" : "没有调度历史";
    $("#scheduler-cpuset-value").textContent = finite(latest.cpuset_cpu_count)
      ? `${Number(latest.cpuset_cpu_count).toFixed(0)} 核`
      : "--";
    $("#scheduler-cpuset-label").textContent = [latest.cpuset_name, latest.cpuset_cpus].filter(Boolean).join(" · ") || "top-app / foreground";
    const groupLabels = { 0: "默认 / 后台", 1: "受限", 2: "前台", 3: "Top-app" };
    $("#scheduler-group-value").textContent = finite(latest.foreground_sched_group)
      ? `组 ${Number(latest.foreground_sched_group).toFixed(0)}`
      : "--";
    $("#scheduler-group-label").textContent = finite(latest.foreground_sched_group)
      ? `${groupLabels[Number(latest.foreground_sched_group)] || "调度组"}${latest.foreground_process ? ` · ${latest.foreground_process}` : ""}`
      : latest.foreground_process || "目标进程";
    $("#scheduler-hint-value").textContent = finite(latest.hint_session_count)
      ? `${Number(latest.hint_session_count).toFixed(0)} 个`
      : "--";
    $("#scheduler-hint-label").textContent = finite(latest.graphics_session_count)
      ? `${Number(latest.graphics_session_count).toFixed(0)} 个图形管线`
      : "活动 / 图形管线";

    svg.replaceChildren();
    const laneDefinitions = [
      { key: "cpuset_cpu_count", label: "可调度 CPU", unit: "核", className: "cpuset", minimum: 0 },
      { key: "foreground_sched_group", label: "前台调度组", unit: "组", className: "group", minimum: 0, maximum: 3 },
      { key: "hint_session_count", label: "ADPF 会话", unit: "个", className: "hint", minimum: 0 },
    ].filter(lane => series.some(item => finite(item[lane.key])));
    if (series.length < 2 || !laneDefinitions.length) {
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    const width = Math.max(420, Math.round(svg.clientWidth || 1000));
    const compact = width < 650;
    const left = compact ? 112 : 148;
    const right = compact ? 22 : 42;
    const top = 18;
    const bottom = 34;
    const laneHeight = compact ? 72 : 78;
    const height = top + bottom + laneDefinitions.length * laneHeight;
    const minimumTime = Math.min(...series.map(item => Number(item.elapsed_s)));
    const maximumTime = Math.max(...series.map(item => Number(item.elapsed_s)));
    const safeMaximumTime = Math.max(minimumTime + 1, maximumTime);
    const x = value => left + (Number(value) - minimumTime) / (safeMaximumTime - minimumTime) * (width - left - right);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("aria-label", `CPU 调度历史趋势，共 ${series.length} 个快照`);
    const ticks = buildTimeTicks(minimumTime, safeMaximumTime, compact ? 3 : 5);
    ticks.forEach((value, index) => {
      const position = x(value);
      svg.appendChild(svgNode("line", { x1: position, x2: position, y1: top, y2: height - bottom, class: "scheduler-grid" }));
      svg.appendChild(svgNode("text", {
        x: position,
        y: height - 12,
        "text-anchor": index === 0 ? "start" : index === ticks.length - 1 ? "end" : "middle",
        class: "scheduler-axis",
      }, formatDuration(value)));
    });
    laneDefinitions.forEach((lane, laneIndex) => {
      const laneTop = top + laneIndex * laneHeight;
      const laneBottom = laneTop + laneHeight - 12;
      const values = series.map(item => finite(item[lane.key]) ? Number(item[lane.key]) : null);
      const valid = values.filter(finite).map(Number);
      const minimum = Number(lane.minimum ?? Math.min(...valid));
      const maximum = Math.max(Number(lane.maximum ?? 0), ...valid, minimum + 1);
      const y = value => laneTop + 10 + (maximum - Number(value)) / Math.max(1, maximum - minimum) * (laneBottom - laneTop - 20);
      const latestValue = [...values].reverse().find(finite);
      svg.appendChild(svgNode("text", { x: 12, y: laneTop + 25, class: "scheduler-label" }, lane.label));
      svg.appendChild(svgNode("text", { x: 12, y: laneTop + 43, class: "scheduler-value" }, finite(latestValue) ? `${Number(latestValue).toFixed(0)} ${lane.unit}` : "--"));
      svg.appendChild(svgNode("text", { x: width - 8, y: laneTop + 15, "text-anchor": "end", class: "scheduler-axis" }, `${maximum.toFixed(0)} ${lane.unit}`));
      svg.appendChild(svgNode("text", { x: width - 8, y: laneBottom, "text-anchor": "end", class: "scheduler-axis" }, `${minimum.toFixed(0)} ${lane.unit}`));
      svg.appendChild(svgNode("line", { x1: left, x2: width - right, y1: laneBottom + 5, y2: laneBottom + 5, class: "scheduler-separator" }));
      const coordinates = series
        .map((item, index) => finite(values[index]) ? { x: x(item.elapsed_s), y: y(values[index]) } : null)
        .filter(Boolean);
      if (!coordinates.length) return;
      let path = `M${coordinates[0].x.toFixed(2)},${coordinates[0].y.toFixed(2)}`;
      for (let index = 1; index < coordinates.length; index += 1) {
        const previous = coordinates[index - 1];
        const current = coordinates[index];
        path += ` L${current.x.toFixed(2)},${previous.y.toFixed(2)} L${current.x.toFixed(2)},${current.y.toFixed(2)}`;
      }
      svg.appendChild(svgNode("path", { d: path, class: `scheduler-line ${lane.className}` }));
      const last = coordinates.at(-1);
      svg.appendChild(svgNode("circle", { cx: last.x, cy: last.y, r: 3.5, class: `scheduler-dot ${lane.className}` }));
    });
  }

  function renderConsole(active) {
    const container = $("#console-output");
    let logs = Array.isArray(active?.logs) ? active.logs : [];
    if (app.consoleClearedAt) logs = logs.filter(item => Number(item.time || 0) >= app.consoleClearedAt);
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 30;
    if (!logs.length) {
      container.innerHTML = '<div class="console-line ui"><time>--:--:--</time><span>UI</span><p>Dashboard ready. Select a device and configure a session.</p></div>';
      return;
    }
    container.innerHTML = logs.map(item => {
      const date = finite(item.time) ? new Date(Number(item.time) * 1000) : null;
      const clock = date ? date.toLocaleTimeString("zh-CN", { hour12: false }) : "--:--:--";
      const source = String(item.source || "ui");
      const type = /error|failed/i.test(String(item.line || "")) ? "error" : source;
      return `<div class="console-line ${escapeHtml(type)}"><time>${escapeHtml(clock)}</time><span>${escapeHtml(source)}</span><p>${escapeHtml(item.line || "")}</p></div>`;
    }).join("");
    if (nearBottom) container.scrollTop = container.scrollHeight;
  }

  function renderChart() {
    const svg = $("#live-chart");
    const empty = $("#chart-empty");
    const active = app.state?.active;
    const baseDefinition = metricDefinitions[app.metric];
    const powerFlow = powerFlowPresentation(active);
    const definition = app.metric === "power_mw"
      ? { ...baseDefinition, title: powerFlow.chartPowerTitle, legend: powerFlow.chartPowerLegend }
      : app.metric === "current_ma"
        ? { ...baseDefinition, title: powerFlow.chartCurrentTitle, legend: powerFlow.chartCurrentLegend }
        : baseDefinition;
    const sourceSeries = definition.series === "performance"
      ? active?.performance_series
      : active?.series;
    const series = Array.isArray(sourceSeries) ? sourceSeries : [];
    $("#chart-title").textContent = definition.title;
    $("#chart-legend").textContent = definition.legend;
    $("#chart-secondary-label").textContent = definition.secondaryLabel || "P95";
    $(".legend-line").style.background = definition.color;
    svg.style.setProperty("--chart-color", definition.color);
    const points = series
      .map(point => ({ elapsed: Number(point.elapsed_s || 0), value: definition.value(point), raw: point }))
      .filter(point => finite(point.value));
    svg.replaceChildren();
    if (points.length < 2) {
      empty.classList.remove("hidden");
      $("#chart-average").textContent = "--";
      $("#chart-p95").textContent = "--";
      $("#chart-range").textContent = "--";
      app.chartGeometry = null;
      return;
    }
    empty.classList.add("hidden");
    const values = points.map(point => Number(point.value));
    const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    const p95 = percentile(values, definition.secondaryQuantile ?? .95);
    $("#chart-average").textContent = `${average.toFixed(definition.digits)} ${definition.unit}`;
    $("#chart-p95").textContent = p95 === null ? "--" : `${p95.toFixed(definition.digits)} ${definition.unit}`;

    const width = Math.max(320, Math.round(svg.clientWidth || 1000));
    const height = Math.max(240, Math.round(svg.clientHeight || 320));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const compact = width < 520;
    const margins = {
      left: compact ? 52 : 64,
      right: compact ? 12 : 20,
      top: 28,
      bottom: compact ? 38 : 40,
    };
    const plotWidth = width - margins.left - margins.right;
    const plotHeight = height - margins.top - margins.bottom;
    const minTime = Math.min(...points.map(point => point.elapsed));
    const maxTime = Math.max(...points.map(point => point.elapsed));
    const scale = buildChartScale(values, definition);
    const minValue = scale.minimum;
    const maxValue = scale.maximum;
    const rangeMinimum = formatAxisNumber(minValue, scale.tickDigits);
    const rangeMaximum = formatAxisNumber(maxValue, scale.tickDigits);
    $("#chart-range").textContent = `${rangeMinimum}–${rangeMaximum} ${definition.unit}`;
    svg.setAttribute(
      "aria-label",
      `${definition.title}，坐标范围 ${rangeMinimum} 到 ${rangeMaximum} ${definition.unit}`,
    );
    const xFor = elapsed => margins.left + ((elapsed - minTime) / Math.max(.001, maxTime - minTime)) * plotWidth;
    const yFor = value => margins.top + (1 - (value - minValue) / Math.max(.001, maxValue - minValue)) * plotHeight;

    const grid = svgNode("g", { class: "chart-grid" });
    grid.appendChild(svgNode("text", {
      x: margins.left,
      y: 15,
      class: "axis-unit",
    }, definition.unit));
    scale.ticks.forEach(value => {
      const y = yFor(value);
      const isBoundary = Math.abs(value - minValue) < 1e-9 || Math.abs(value - maxValue) < 1e-9;
      grid.appendChild(svgNode("line", {
        x1: margins.left,
        y1: y,
        x2: width - margins.right,
        y2: y,
        class: `grid-line grid-line-y${isBoundary ? " grid-line-boundary" : ""}`,
      }));
      grid.appendChild(svgNode("line", {
        x1: margins.left - 4,
        y1: y,
        x2: margins.left,
        y2: y,
        class: "axis-tick",
      }));
      grid.appendChild(svgNode("text", {
        x: margins.left - 9,
        y,
        "text-anchor": "end",
        "dominant-baseline": "middle",
        class: "axis-label axis-label-y",
      }, formatAxisNumber(value, scale.tickDigits)));
    });
    const timeTicks = buildTimeTicks(minTime, maxTime, compact ? 3 : width < 760 ? 4 : 5);
    timeTicks.forEach((elapsed, index) => {
      const x = xFor(elapsed);
      grid.appendChild(svgNode("line", {
        x1: x,
        y1: margins.top,
        x2: x,
        y2: height - margins.bottom,
        class: "grid-line grid-line-x",
      }));
      grid.appendChild(svgNode("line", {
        x1: x,
        y1: height - margins.bottom,
        x2: x,
        y2: height - margins.bottom + 4,
        class: "axis-tick",
      }));
      grid.appendChild(svgNode("text", {
        x,
        y: height - 14,
        "text-anchor": index === 0 ? "start" : index === timeTicks.length - 1 ? "end" : "middle",
        class: "axis-label axis-label-x",
      }, formatDuration(elapsed)));
    });
    grid.appendChild(svgNode("line", {
      x1: margins.left,
      y1: margins.top,
      x2: margins.left,
      y2: height - margins.bottom,
      class: "axis-line",
    }));
    grid.appendChild(svgNode("line", {
      x1: margins.left,
      y1: height - margins.bottom,
      x2: width - margins.right,
      y2: height - margins.bottom,
      class: "axis-line",
    }));
    svg.appendChild(grid);

    const reference = typeof definition.reference === "function"
      ? definition.reference(active)
      : null;
    let referenceLabel = null;
    if (reference && finite(reference.value) && reference.value >= minValue && reference.value <= maxValue) {
      const referenceY = yFor(Number(reference.value));
      const referenceLabelY = referenceY <= margins.top + 14 ? referenceY + 15 : referenceY - 6;
      svg.appendChild(svgNode("line", {
        x1: margins.left,
        y1: referenceY,
        x2: width - margins.right,
        y2: referenceY,
        class: "reference-line",
      }));
      const referenceLabelWidth = Math.min(
        compact ? 116 : 150,
        Math.max(76, String(reference.label).length * (compact ? 5.8 : 6.4) + 12),
      );
      referenceLabel = svgNode("g", { class: "reference-label-group" });
      referenceLabel.appendChild(svgNode("rect", {
        x: width - margins.right - referenceLabelWidth,
        y: referenceLabelY - 11,
        width: referenceLabelWidth,
        height: 16,
        rx: 4,
        class: "reference-label-bg",
      }));
      referenceLabel.appendChild(svgNode("text", {
        x: width - margins.right - 5,
        y: referenceLabelY,
        "text-anchor": "end",
        class: "reference-label",
      }, reference.label));
    }

    const coordinates = points.map(point => ({ ...point, x: xFor(point.elapsed), y: yFor(Number(point.value)) }));
    const linePath = coordinates.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
    const areaPath = `${linePath} L${coordinates.at(-1).x.toFixed(2)},${height - margins.bottom} L${coordinates[0].x.toFixed(2)},${height - margins.bottom} Z`;
    svg.appendChild(svgNode("path", { d: areaPath, class: "series-area" }));
    svg.appendChild(svgNode("path", { d: linePath, class: "series-line" }));
    const last = coordinates.at(-1);
    svg.appendChild(svgNode("circle", { cx: last.x, cy: last.y, r: 4, class: "latest-marker" }));
    const hoverLine = svgNode("line", { x1: last.x, y1: margins.top, x2: last.x, y2: height - margins.bottom, class: "hover-line", opacity: 0 });
    svg.appendChild(hoverLine);
    if (referenceLabel) svg.appendChild(referenceLabel);
    app.chartGeometry = { coordinates, width, height, margins, hoverLine, definition };
  }

  function renderActive(active) {
    const isNewRun = active?.run_name !== app.currentRunName;
    if (isNewRun) {
      app.currentRunName = active?.run_name || null;
      app.consoleClearedAt = 0;
      app.notifiedWarnings.clear();
    }
    if (active && (isNewRun || active.running)) {
      if (active.running) {
        setPlatform(active.platform || active.metadata?.platform || selectedPlatform(), {
          fromRun: true,
          initial: true,
        });
      }
      setTestMode(active.test_mode || active.metadata?.test_mode || "power", {
        fromRun: true,
        initial: true,
      });
      if (isNewRun && active.running && active.config) {
        const config = active.config;
        [
          ["duration-input", "duration"],
          ["interval-input", "interval"],
          ["checkpoint-input", "checkpoint_interval"],
          ["reconnect-input", "reconnect_timeout"],
          ["process-interval-input", "process_interval"],
          ["thread-interval-input", "thread_interval"],
          ["thermal-interval-input", "thermal_interval"],
          ["scheduler-interval-input", "scheduler_interval"],
          ["performance-interval-input", "performance_interval"],
        ].forEach(([id, key]) => {
          if (finite(config[key])) document.getElementById(id).value = String(config[key]);
        });
        [
          ["title-input", config.title],
          ["run-name-input", active.run_name || config.run_name],
          ["package-input", config.package],
          ["start-note-input", config.start_note],
          ["gpu-path-input", config.gpu_frequency_path],
        ].forEach(([id, value]) => {
          if (value !== null && value !== undefined) document.getElementById(id).value = String(value);
        });
        [
          ["start-context-input", config.start_context],
          ["current-unit-input", config.current_unit],
        ].forEach(([id, value]) => {
          if (value) document.getElementById(id).value = String(value);
        });
        [
          ["session-mode-input", "session_mode"],
          ["unplugged-input", "require_unplugged"],
          ["no-reset-input", "no_reset"],
          ["full-history-input", "full_history"],
          ["system-monitor-input", "system_monitor"],
          ["harmony-high-performance-input", "harmony_high_performance"],
        ].forEach(([id, key]) => {
          document.getElementById(id).checked = Boolean(config[key]);
        });
        syncDurationPreset();
      }
      if (isNewRun && active.config?.capture_features) {
        $("#capture-preset-input").value = active.config.capture_preset || "auto";
        $$('[data-capture-feature]').forEach(input => {
          if (Object.prototype.hasOwnProperty.call(active.config.capture_features, input.dataset.captureFeature)) {
            input.checked = Boolean(active.config.capture_features[input.dataset.captureFeature]);
          }
        });
        $("#harmony-high-performance-input").checked = Boolean(active.config.harmony_high_performance);
        updateCaptureFeatureControls({ overridden: false });
      }
    }
    renderSession(active);
    renderMetrics(active);
    renderPerformanceMetrics(active);
    renderClusters(active);
    renderContext(active);
    renderPerformanceResources(active);
    renderPowerPressure(active);
    renderRenderPipeline(active);
    renderSystem(active);
    renderSchedulerHistory(active);
    renderConsole(active);
    renderLiveDetails(active);
    renderChart();
    const warnings = Array.isArray(active?.warnings) ? active.warnings : [];
    warnings.forEach(warning => {
      if (app.notifiedWarnings.has(warning)) return;
      app.notifiedWarnings.add(warning);
      notify("采集警告", warning, "error", 8000);
    });
  }

  function currentProbe(state = app.state) {
    const serial = selectedDevice();
    return serial && state?.probes ? state.probes[serial] : null;
  }

  function renderProbe(state) {
    const entry = currentProbe(state);
    const placeholder = $("#probe-placeholder");
    const results = $("#probe-results");
    if (!entry?.data) {
      placeholder.classList.remove("hidden");
      results.classList.add("hidden");
      return;
    }
    placeholder.classList.add("hidden");
    results.classList.remove("hidden");
    const data = entry.data;
    const device = data.device || {};
    const platform = String(data.platform || (device.ios ? "ios" : "android"));
    const battery = data.battery || {};
    const powered = Array.isArray(battery.powered) ? battery.powered : battery.powered ? [battery.powered] : [];
    const isUnplugged = powered.length === 0;
    $("#probe-device-name").textContent = [device.brand, device.model].filter(Boolean).join(" ") || "Unknown device";
    $("#probe-device-detail").textContent = platform === "ios"
      ? `${device.product_type || device.device || "iPhone"} · ${device.hardware || "--"} · iOS ${device.ios || "--"} · ${device.cpu_count || "--"} CPU`
      : platform === "harmony"
        ? `${device.soc_model || device.hardware || "Unknown SoC"} · ${device.hardware || "--"} · HarmonyOS ${device.harmony || device.openharmony || "--"}`.trim()
        : `${device.soc_manufacturer || ""} ${device.soc_model || device.hardware || "Unknown SoC"} · ${device.hardware || "--"}/${device.board_platform || "--"} · Android ${device.android || "--"}`.trim();
    $("#probe-serial").textContent = data.device?.serial || entry.device || "--";
    $("#probe-power-state").textContent = isUnplugged ? "电池供电" : `外部供电：${powered.join(", ")}`;
    $("#probe-current-state").textContent = `Current command: ${data.current_command || "unavailable"} · ${battery.voltage_mv ? `${battery.voltage_mv} mV` : "voltage n/a"}`;
    const powerBadge = $("#probe-power-badge");
    powerBadge.textContent = isUnplugged && data.current_command_ok ? "READY" : "CHECK POWER";
    powerBadge.className = `probe-badge ${isUnplugged && data.current_command_ok ? "" : "bad"}`;
    const gpuFrequencyAvailable = Boolean(data.gpu_source?.frequency_path);
    const gpuLoadAvailable = Boolean(data.gpu_source?.load_path);
    const gpuAvailable = gpuFrequencyAvailable || gpuLoadAvailable;
    const gpuRendererAvailable = Boolean(data.capabilities?.gpu_renderer || data.performance?.gpu_renderer);
    const gpuModel = data.gpu_probe?.model || data.gpu_source?.name || "GPU";
    $("#probe-gpu-state").textContent = gpuFrequencyAvailable ? `${gpuModel} 频率可读` : gpuLoadAvailable ? `${gpuModel} 负载可读` : data.gpu_work_duration_available ? `${gpuModel} 驱动证据` : gpuRendererAvailable ? `${gpuModel} 已识别` : "GPU 遥测不可用";
    $("#probe-gpu-detail").textContent = gpuFrequencyAvailable ? data.gpu_source.frequency_path : gpuLoadAvailable ? data.gpu_source.load_path : data.gpu_probe?.reason || "No readable OEM node";
    const gpuBadge = $("#probe-gpu-badge");
    gpuBadge.textContent = gpuFrequencyAvailable ? "DIRECT" : gpuLoadAvailable ? "LOAD" : data.gpu_work_duration_available ? "FALLBACK" : gpuRendererAvailable ? "RENDERER" : "UNAVAILABLE";
    gpuBadge.className = `probe-badge ${gpuAvailable || gpuRendererAvailable ? "" : "neutral"}`;
    $("#probe-foreground").textContent = data.foreground_package || "Unknown";

    const policies = Array.isArray(data.cpu_policies) ? data.cpu_policies : [];
    $("#probe-cpu-table").innerHTML = policies.length ? policies.map(policy => `<div class="capability-row">
      <strong>${escapeHtml(policy.label || policy.name)}</strong>
      <span>cores ${(policy.cores || []).join(", ") || "--"}${policy.governor ? ` · ${escapeHtml(policy.governor)}` : ""}${policy.core_control?.min_cpus != null ? ` · core_ctl ${escapeHtml(policy.core_control.min_cpus)}–${escapeHtml(policy.core_control.max_cpus)}` : ""}</span>
      <span>${finite(policy.min_khz) ? `${(Number(policy.min_khz) / 1000).toFixed(0)} MHz min` : "min n/a"}</span>
      <span>${finite(policy.max_khz) ? `${(Number(policy.max_khz) / 1000).toFixed(0)} MHz max` : "max n/a"}</span>
    </div>`).join("") : '<div class="empty-row">未识别到 cpufreq policy</div>';

    const features = platform === "ios" ? [
      ["Battery power", "IORegistry PowerTelemetryData.SystemLoad", Boolean(data.power_telemetry_available)],
      ["Process CPU / power score", "DVT sysmontap", Boolean(data.capabilities?.process_cpu)],
      ["GPU utilization", "DVT graphics", Boolean(data.capabilities?.gpu_utilization)],
      ["App state timeline", "DVT Running / Suspended notifications", Boolean(data.capabilities?.application_state_notifications)],
      ["Wireless RemoteXPC", `${data.connection?.host || "USB"}:${data.connection?.port || "--"}`, Boolean(data.capabilities?.remote_xpc)],
      ["Battery temperature", `${data.system_monitor?.thermal_sensor_count || 0} sensor`, Boolean(data.system_monitor?.thermalservice_available)],
    ] : platform === "harmony" ? [
      ["Battery current / voltage", "HarmonyOS BatteryService", Boolean(data.capabilities?.battery_service && data.current_command_ok)],
      ["CPU utilization", "persistent HDC /proc/stat", Boolean(data.capabilities?.proc_stat)],
      ["CPU frequency", "hidumper --cpufreq", Boolean(data.capabilities?.cpufreq)],
      ["SmartPerf native capture", data.smartperf?.command || "SP_daemon", Boolean(data.capabilities?.smartperf_daemon)],
      ["Device high-performance mode", `current ${data.power_mode?.current_mode || "--"} · power-shell 602`, Boolean(data.capabilities?.performance_power_mode)],
      ["Foreground Ability", data.foreground_package || "AbilityManager", Boolean(data.capabilities?.ability_manager)],
      ["Display refresh modes", `${data.display?.refresh_rate_hz || "--"} Hz · ${(data.display?.supported_refresh_rates_hz || []).join("/") || "modes n/a"}`, Boolean(data.capabilities?.display_modes)],
      ["Compositor frame pacing", `${formatNumber(data.performance?.compositor_fps, 1)} FPS sampled`, Boolean(data.capabilities?.render_service_fps)],
      ["Foreground window", data.performance?.foreground_window_name || "WindowManagerService", Boolean(data.capabilities?.window_manager)],
      ["Touch devices", `${data.performance?.touch_device_count || 0} devices · axes/events`, Boolean(data.capabilities?.touch_devices)],
      ["Touch hardware sampling rate", data.touch?.sampling_rate_reason || "not exposed", false],
      ["GPU renderer", data.performance?.gpu_renderer || data.gpu_probe?.model || "renderer n/a", Boolean(data.capabilities?.gpu_renderer)],
      ["Temperature sensors", `${data.system_monitor?.thermal_sensor_count || 0} sensors`, Boolean(data.capabilities?.thermal_service)],
      ["Background interference", "HarmonyOS top + ps", Boolean(data.capabilities?.process_top)],
      ["GPU frequency / load", data.gpu_probe?.reason || "HDC permission restricted", Boolean(data.capabilities?.smartperf_gpu_metrics)],
    ] : [
      ["Fuel-gauge current", "cmd battery current_now", Boolean(data.current_command_ok)],
      ["GPU frequency", `${gpuModel} · ${data.gpu_probe?.provider || "OEM sysfs"}`, gpuFrequencyAvailable],
      ["GPU load", data.gpu_source?.load_format || "OEM load node", gpuLoadAvailable],
      ["Memory / DMC frequency", data.memory_source?.frequency_path || data.memory_probe?.limitations || "DRAM/DMC node not exposed", Boolean(data.capabilities?.memory_frequency)],
      ["GPU UID work", "dumpsys gpu active duration", Boolean(data.gpu_work_duration_available)],
      ["GPU memory", `${finite(data.gpu_memory_total_bytes) ? `${(Number(data.gpu_memory_total_bytes) / 1048576).toFixed(1)} MiB` : "dumpsys gpu snapshot"}`, Boolean(data.gpu_memory_snapshot_available)],
      ["Perfetto android.power", "registered data source", Boolean(data.perfetto_android_power)],
      ["Perfetto sysfs power", "linux.sysfs_power", Boolean(data.perfetto_sysfs_power)],
      ["PowerStats dump", "dumpsys powerstats", Boolean(data.powerstats_dump_available)],
      ["Foreground frame rate", data.performance?.surface_layer_name ? "SurfaceFlinger BLAST present timestamps" : "gfxinfo foreground-window counters", Boolean(data.capabilities?.frame_rate)],
      ["System process monitor", "top + ps for apps/services/kernel", Boolean(data.system_monitor?.process_top_available)],
      ["ThermalService history", `${data.system_monitor?.thermal_sensor_count || 0} sensors / ${data.system_monitor?.thermal_threshold_count || 0} thresholds`, Boolean(data.system_monitor?.thermalservice_available)],
      ["cpuset / ADPF", `${Object.keys(data.system_monitor?.cpusets || {}).length} cpusets / ${data.system_monitor?.adpf_active_session_count || 0} active sessions`, Boolean(data.system_monitor?.adpf_available)],
      ["WALT / core_ctl", `${(data.system_monitor?.cpu_policies || []).map(item => item.governor).filter(Boolean).join(", ") || "governor n/a"} / ${(data.system_monitor?.cpu_policies || []).filter(item => item.core_ctl_min_cpus != null).length} policies`, (data.system_monitor?.cpu_policies || []).some(item => item.governor === "walt" || item.core_ctl_min_cpus != null)],
    ];
    $("#probe-feature-list").innerHTML = features.map(([name, detail, available]) => `<div class="feature-row"><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></span><i class="feature-state ${available ? "good" : "warn"}"></i></div>`).join("");
    $("#probe-json").textContent = JSON.stringify(data, null, 2);
  }

  function renderHistory(state) {
    $("#history-root").textContent = `采集根目录：${state?.output_root || "profiler-runs"}`;
    $("#output-root-hint").textContent = `保存在 ${state?.output_root || "profiler-runs"}`;
    const history = Array.isArray(state?.history) ? state.history : [];
    const body = $("#history-body");
    if (!history.length) {
      body.innerHTML = '<tr><td colspan="8" class="table-empty">暂无采集记录。完成一次 record 后会显示在这里。</td></tr>';
      return;
    }
    body.innerHTML = history.map(run => `<tr>
      <td><span class="history-title"><strong>${escapeHtml(run.title || run.run_name)}</strong><small>${escapeHtml(run.run_name)} · ${escapeHtml(formatDate(run.modified_at))}</small></span></td>
      <td>${escapeHtml(run.device || run.serial || "--")}</td>
      <td><span class="status-pill ${escapeHtml(run.status || "unknown")}">${escapeHtml(run.status || "unknown")}</span></td>
      <td>${escapeHtml(formatShortDuration(run.duration_s))}</td>
      <td>${finite(run.average_power_mw) ? `${(Number(run.average_power_mw) / 1000).toFixed(3)} W` : "--"}</td>
      <td>${finite(run.energy_mwh) ? `${Number(run.energy_mwh).toFixed(2)} mWh` : "--"}</td>
      <td>${finite(run.coverage_pct) ? `${Number(run.coverage_pct).toFixed(1)}%` : "--"}</td>
      <td><span class="history-actions">${run.report_url ? `<a class="history-link" href="${escapeHtml(run.report_url)}" target="_blank" rel="noreferrer">打开报告</a>` : ""}<button class="history-link archive-run" type="button" data-archive-run="${escapeHtml(run.run_name)}">打包原始数据</button></span></td>
    </tr>`).join("");
    body.querySelectorAll("[data-archive-run]").forEach(button => button.addEventListener("click", async () => {
      const runName = button.dataset.archiveRun;
      button.disabled = true;
      try {
        const result = await api("/api/archive", {
          method: "POST",
          body: JSON.stringify({ run_name: runName }),
        });
        notify("证据包已生成", result.archive_path, "success", 10000);
      } catch (error) {
        notify("打包失败", error.message, "error", 8000);
      } finally {
        button.disabled = false;
      }
    }));
  }

  function renderRunSelect(select, history, defaultIndex = 0) {
    const signature = history.map(run => `${run.run_name}:${run.status}:${run.title}`).join("|");
    if (select.dataset.signature === signature) return;
    const previous = select.value;
    select.innerHTML = "";
    if (!history.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无运行记录";
      select.appendChild(option);
      select.dataset.signature = signature;
      return;
    }
    history.forEach(run => {
      const option = document.createElement("option");
      option.value = run.run_name;
      option.textContent = `${run.title || run.run_name} · ${run.status || "unknown"}`;
      select.appendChild(option);
    });
    select.value = history.some(run => run.run_name === previous)
      ? previous
      : history[Math.min(defaultIndex, history.length - 1)].run_name;
    select.dataset.signature = signature;
  }

  function renderTools(state) {
    const history = Array.isArray(state?.history) ? state.history : [];
    renderRunSelect($("#maintenance-run-select"), history, 0);
    renderRunSelect($("#import-run-select"), history, 0);
    renderRunSelect($("#archive-run-select"), history, 0);
    renderRunSelect($("#compare-run-a"), history, 0);
    renderRunSelect($("#compare-run-b"), history, 1);
    if (history.length > 1 && $("#compare-run-a").value === $("#compare-run-b").value) {
      const alternative = history.find(run => run.run_name !== $("#compare-run-a").value);
      if (alternative) $("#compare-run-b").value = alternative.run_name;
    }

    const tooling = state?.tooling || {};
    const mode = $("#tooling-mode");
    mode.className = `tooling-mode ${tooling.busy ? "busy" : tooling.source_mode ? "source" : "portable"}`;
    mode.querySelector("strong").textContent = tooling.busy
      ? "维护任务正在运行"
      : tooling.source_mode
        ? "源码工程模式"
        : "便携运行模式";
    mode.querySelector("small").textContent = tooling.busy
      ? (tooling.operation || "Please wait")
      : tooling.source_mode
        ? (tooling.source_root || "Source project")
        : "采集、导入、归档和比较可用";

    const rulesInput = $("#import-rules-path");
    if (!rulesInput.dataset.defaultApplied && tooling.default_rules_path) {
      rulesInput.value = rulesInput.value || tooling.default_rules_path;
      rulesInput.dataset.defaultApplied = "true";
    }
    const outputInput = $("#portable-output-directory");
    if (!outputInput.dataset.defaultApplied && tooling.portable_output_default) {
      outputInput.value = outputInput.value || tooling.portable_output_default;
      outputInput.dataset.defaultApplied = "true";
    }

    const busy = Boolean(tooling.busy);
    const hasRuns = history.length > 0;
    $("#regenerate-report-button").disabled = busy || !hasRuns;
    $("#recover-run-button").disabled = busy || !hasRuns;
    $("#import-log-form button[type='submit']").disabled = busy || !hasRuns;
    $("#archive-form button[type='submit']").disabled = busy || !hasRuns;
    $("#compare-form button[type='submit']").disabled = busy || history.length < 2;

    const buildAvailable = Boolean(tooling.portable_build_available);
    const activeRecording = Boolean(state?.active?.running && !state?.active?.is_demo);
    $("#portable-build-button").disabled = busy || !buildAvailable || activeRecording;
    $(".portable-card").classList.toggle("unavailable", !buildAvailable);
    $("#portable-build-hint").textContent = buildAvailable
      ? activeRecording
        ? "请先停止手机采集，再构建便携包，避免交付构建与采集同时运行。"
        : `将调用 ${tooling.source_root || "源码工程"} 中的 tools\\build-portable.ps1。`
      : "当前是便携包环境：可以采集、分析、归档和比较，但重新打包必须回到包含 src、tools 和 pyproject.toml 的源码电脑。";

    const comparisons = Array.isArray(tooling.comparisons) ? tooling.comparisons : [];
    const recent = $("#recent-comparisons");
    recent.innerHTML = comparisons.length
      ? `<span>最近报告</span>${comparisons.slice(0, 5).map(item => `<a href="${escapeHtml(item.comparison_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title || item.name)} · ${escapeHtml(formatDate(item.modified_at))}</a>`).join("")}`
      : "<span>尚未生成对比报告</span>";
  }

  function showToolResult(title, result) {
    const lines = [title, new Date().toLocaleString("zh-CN")];
    [
      ["运行记录", result.run_name],
      ["报告", result.report_path],
      ["证据 ZIP", result.archive_path],
      ["对比报告", result.comparison_path],
      ["对比目录", result.comparison_dir],
      ["便携目录", result.bundle_dir],
      ["便携 ZIP", result.zip_path],
    ].forEach(([label, value]) => { if (value) lines.push(`${label}: ${value}`); });
    if (result.import && finite(result.import.event_count)) {
      lines.push(`导入事件: ${result.import.event_count}，匹配日志行: ${result.import.matched_line_count ?? "--"}`);
    }
    if (finite(result.entry_count)) {
      lines.push(`归档条目: ${result.entry_count}，额外附件: ${result.attachment_count || 0}`);
    }
    if (result.output) lines.push("", String(result.output));
    $("#tool-output").textContent = lines.join("\n");
    const link = $("#tool-output-link");
    const url = result.comparison_url || result.report_url || "";
    link.classList.toggle("hidden", !url);
    link.href = url || "#";
    link.textContent = result.comparison_url ? "打开对比报告" : "打开报告";
  }

  async function runToolOperation(endpoint, payload, busyLabel, successTitle) {
    setBusy(true, busyLabel);
    try {
      const result = await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
      showToolResult(successTitle, result);
      notify(successTitle, result.archive_path || result.zip_path || result.comparison_path || result.report_path || "操作完成", "success", 10000);
      await refreshState();
      return result;
    } catch (error) {
      $("#tool-output").textContent = `${successTitle}失败\n${new Date().toLocaleString("zh-CN")}\n\n${error.message}`;
      $("#tool-output-link").classList.add("hidden");
      notify(`${successTitle}失败`, error.message, "error", 10000);
      return null;
    } finally {
      setBusy(false);
    }
  }

  function render(state) {
    app.state = state;
    setServerState(true, state.demo_mode ? "Demo preview enabled" : "Local dashboard");
    if (state.active?.running) {
      setPlatform(state.active.platform || state.active.metadata?.platform || selectedPlatform(), {
        fromRun: true,
        initial: true,
      });
    } else {
      updatePlatformPresentation();
    }
    renderDevices(state);
    renderActive(state.active);
    renderProbe(state);
    renderHistory(state);
    renderTools(state);
  }

  async function refreshState() {
    if (app.polling) return;
    app.polling = true;
    try {
      const state = await api("/api/state");
      render(state);
    } catch (error) {
      setServerState(false, error.message || "Connection failed");
    } finally {
      app.polling = false;
    }
  }

  async function pollLoop() {
    await refreshState();
    setTimeout(pollLoop, document.hidden ? 2500 : 1000);
  }

  function recordPayload() {
    const captureFeatures = Object.fromEntries($$('[data-capture-feature]').map(input => [
      input.dataset.captureFeature,
      input.checked,
    ]));
    return {
      device: selectedDevice(),
      platform: selectedPlatform(),
      test_mode: app.testMode,
      capture_preset: $("#capture-preset-input").value,
      capture_features: captureFeatures,
      harmony_high_performance: $("#harmony-high-performance-input").checked,
      title: $("#title-input").value.trim(),
      run_name: $("#run-name-input").value.trim(),
      duration: Number($("#duration-input").value),
      interval: Number($("#interval-input").value),
      package: $("#package-input").value.trim(),
      start_context: $("#start-context-input").value,
      start_note: $("#start-note-input").value.trim(),
      session_mode: app.testMode === "power" && $("#session-mode-input").checked,
      require_unplugged: $("#unplugged-input").checked,
      checkpoint_interval: Number($("#checkpoint-input").value),
      reconnect_timeout: Number($("#reconnect-input").value),
      current_unit: $("#current-unit-input").value,
      gpu_frequency_path: $("#gpu-path-input").value.trim(),
      no_reset: $("#no-reset-input").checked,
      full_history: $("#full-history-input").checked,
      system_monitor: $("#system-monitor-input").checked,
      process_interval: Number($("#process-interval-input").value),
      thread_interval: Number($("#thread-interval-input").value),
      thermal_interval: Number($("#thermal-interval-input").value),
      scheduler_interval: Number($("#scheduler-interval-input").value),
      performance_interval: Number($("#performance-interval-input").value),
    };
  }

  function bindEvents() {
    $$(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
    $$(".platform-switch [data-platform]").forEach(button => button.addEventListener("click", () => {
      setPlatform(button.dataset.platform);
    }));
    $$("[data-test-mode]").forEach(button => button.addEventListener("click", () => {
      setTestMode(button.dataset.testMode);
    }));
    window.addEventListener("hashchange", () => switchView(location.hash.replace("#", "")));
    window.addEventListener("resize", () => requestAnimationFrame(() => {
      renderChart();
      renderSchedulerHistory(app.state?.active);
    }));
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });

    $("#device-select").addEventListener("change", event => {
      localStorage.setItem(`mobile-profiler-device-${selectedPlatform()}`, event.target.value);
      app.brightnessRequestId += 1;
      app.brightnessDevice = "";
      app.brightnessInfo = null;
      app.brightnessError = "";
      app.brightnessLoading = false;
      if (app.scannedAppsDevice && app.scannedAppsDevice !== event.target.value) {
        resetAppScanner({ clearPackage: true });
      }
      renderDevices(app.state);
      renderProbe(app.state);
      updateCaptureFeatureControls();
    });

    $("#brightness-refresh").addEventListener("click", () => {
      void refreshBrightnessCapability({ force: true, notifyFailure: true });
    });
    $("#brightness-apply").addEventListener("click", () => {
      void applyBrightnessValue();
    });
    $("#brightness-input").addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      void applyBrightnessValue();
    });

    $("#capture-preset-input").addEventListener("change", () => applyCapturePreset());
    $$('[data-capture-feature]').forEach(input => {
      const card = input.closest(".capture-feature-card");
      let pointerChecked = null;
      card?.addEventListener("pointerdown", event => {
        if (event.button !== 0 || input.disabled) return;
        pointerChecked = input.checked;
      });
      card?.addEventListener("pointercancel", () => { pointerChecked = null; });
      card?.addEventListener("click", event => {
        if (pointerChecked === null) return;
        const requested = !pointerChecked;
        pointerChecked = null;
        event.preventDefault();
        setTimeout(() => {
          if (input.disabled) return;
          input.checked = requested;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }, 0);
      });
      input.addEventListener("change", () => {
        if (input.dataset.captureFeature === "frame_rate" && !input.checked) {
          $('[data-capture-feature="frame_details"]').checked = false;
        }
        if (input.dataset.captureFeature === "frame_details" && input.checked) {
          $('[data-capture-feature="frame_rate"]').checked = true;
        }
        if (input.dataset.captureFeature === "foreground_window" && !input.checked) {
          $('[data-capture-feature="harmony_hitches"]').checked = false;
        }
        if (input.dataset.captureFeature === "harmony_hitches" && input.checked) {
          $('[data-capture-feature="foreground_window"]').checked = true;
        }
        if (input.dataset.captureFeature === "hot_threads" && input.checked) {
          $('[data-capture-feature="process_snapshots"]').checked = true;
        }
        if (input.dataset.captureFeature === "process_snapshots" && !input.checked) {
          $('[data-capture-feature="hot_threads"]').checked = false;
        }
        updateCaptureFeatureControls({ overridden: true });
      });
    });
    $("#system-monitor-input").addEventListener("change", event => {
      ["process_snapshots", "hot_threads", "thermal", "scheduler"].forEach(name => {
        const input = $(`[data-capture-feature="${name}"]`);
        if (input && !input.disabled) input.checked = event.target.checked;
      });
      updateCaptureFeatureControls({ overridden: true });
    });
    $("#harmony-high-performance-input").addEventListener("change", event => {
      if (!event.target.checked) return;
      const confirmed = confirm(
        "设备高性能模式会在测试期间切换到 HarmonyOS power-shell 602，显著增加功耗和温度。\n\n程序会在正常结束、停止或异常退出时恢复原模式。是否启用？"
      );
      if (!confirmed) event.target.checked = false;
    });

    $("#adb-connect-form").addEventListener("submit", async event => {
      event.preventDefault();
      const platform = selectedPlatform();
      if (platform === "ios") return;
      const address = $("#adb-address-input").value.trim();
      if (!address) {
        notify(`请输入 ${platform === "harmony" ? "HDC" : "ADB"} 地址`, platformProfiles[platform].addressPlaceholder, "error");
        return;
      }
      const isHarmony = platform === "harmony";
      setBusy(true, `正在连接 ${address}...`);
      try {
        const result = await api(isHarmony ? "/api/harmony/connect" : "/api/connect", {
          method: "POST",
          body: JSON.stringify({ address }),
        });
        localStorage.setItem(`mobile-profiler-address-${platform}`, address);
        localStorage.setItem(`mobile-profiler-device-${platform}`, result.serial || address);
        const transport = isHarmony ? "HDC" : "ADB";
        notify(result.connected ? `${transport} 设备已连接` : `${transport} 命令已执行`, result.output || address, result.connected ? "success" : "error", 7000);
        await refreshState();
      } catch (error) {
        notify(`${isHarmony ? "HDC" : "ADB"} 连接失败`, error.message, "error", 8000);
      } finally {
        setBusy(false);
      }
    });

    $("#enable-tcpip").addEventListener("click", async () => {
      const device = selectedDevice();
      const selected = (app.state?.devices || []).find(item => item.serial === device);
      if (!device || deviceConnectionType(selected) !== "usb") {
        notify("请选择 USB ADB 设备", "手机需要先通过 USB 完成调试授权。", "error");
        return;
      }
      if (!confirm(`将对 ${device} 执行 adb tcpip 5555，并重启手机上的 adbd。\n\n确认当前没有正在进行的采集。`)) return;
      setBusy(true, "正在开启无线 ADB 并读取 Wi-Fi 地址...");
      try {
        const result = await api("/api/tcpip", {
          method: "POST",
          body: JSON.stringify({ device, port: 5555, auto_connect: true }),
        });
        if (result.suggested_address) {
          $("#adb-address-input").value = result.suggested_address;
          localStorage.setItem("mobile-profiler-address-android", result.suggested_address);
          if (result.connected) {
            localStorage.setItem("mobile-profiler-device-android", result.suggested_address);
          }
        }
        const detail = result.connected
          ? `${result.suggested_address} 已连接，可拔掉 USB。`
          : result.suggested_address
            ? `${result.suggested_address} 已填入；${result.connect_error || "请点击连接重试。"}`
            : result.connect_error || result.tcpip_output;
        notify(result.connected ? "无线 ADB 已开启并连接" : "无线 ADB 已开启", detail, "success", 10000);
        await refreshState();
      } catch (error) {
        notify("无法开启无线 ADB", error.message, "error", 10000);
      } finally {
        setBusy(false);
      }
    });

    $("#enable-harmony-tcpip").addEventListener("click", async () => {
      const device = selectedDevice();
      const selected = (app.state?.devices || []).find(item => item.serial === device);
      if (!device || devicePlatform(selected) !== "harmony" || deviceConnectionType(selected) !== "usb") {
        notify("请选择 USB 鸿蒙设备", "手机需要先通过 USB 完成 HDC 调试授权。", "error");
        return;
      }
      if (!confirm(`将对 ${device} 执行 hdc tmode port 8710，并自动连接手机的 Wi-Fi 地址。\n\n确认当前没有正在进行的采集。`)) return;
      setBusy(true, "正在开启鸿蒙无线 HDC 并读取 Wi-Fi 地址...");
      try {
        const result = await api("/api/harmony/tcpip", {
          method: "POST",
          body: JSON.stringify({ device, port: 8710, auto_connect: true }),
        });
        if (result.suggested_address) {
          const prefixedAddress = `harmony:${result.suggested_address}`;
          $("#adb-address-input").value = result.suggested_address;
          localStorage.setItem("mobile-profiler-address-harmony", result.suggested_address);
          if (result.connected) {
            localStorage.setItem("mobile-profiler-device-harmony", result.suggested_device || prefixedAddress);
          }
        }
        const detail = result.connected
          ? `${result.suggested_address} 已连接，可拔掉 USB。`
          : result.suggested_address
            ? `${result.suggested_address} 已填入；${result.connect_error || "请点击连接重试。"}`
            : result.connect_error || result.tcpip_output;
        notify(result.connected ? "鸿蒙无线 HDC 已开启并连接" : "鸿蒙无线 HDC 已开启", detail, "success", 10000);
        await refreshState();
      } catch (error) {
        notify("无法开启鸿蒙无线 HDC", error.message, "error", 10000);
      } finally {
        setBusy(false);
      }
    });

    $("#pair-ios").addEventListener("click", async () => {
      const device = selectedDevice();
      const selected = (app.state?.devices || []).find(item => item.serial === device);
      if (!device || devicePlatform(selected) !== "ios" || deviceConnectionType(selected) !== "usb") {
        notify("请选择 USB iPhone", "创建或修复无线连接时，iPhone 必须通过 USB 连接并保持解锁。", "error");
        return;
      }
      if (!confirm("将为当前 iPhone 创建 RemotePairing 并缓存 Wi-Fi 端点。配对完成后可拔掉 USB，是否继续？")) return;
      setBusy(true, "正在创建 iOS RemotePairing...");
      try {
        const result = await api("/api/ios/pair", {
          method: "POST",
          body: JSON.stringify({ device }),
        });
        const endpoint = result.endpoint || {};
        if (!result.connected || !endpoint.host || !endpoint.port) {
          throw new Error(result.connection_error || "RemotePairing 未返回可用的 Wi-Fi 端点");
        }
        notify(
          "iOS 无线配对完成",
          `${endpoint.host}:${endpoint.port} 已验证，现在可以拔掉 USB。`,
          "success",
          10000,
        );
        await refreshState();
      } catch (error) {
        const reason = String(error?.message || "未知连接错误").replace(/^ERROR:\s*/i, "");
        notify("iOS 无线配对失败", reason, "error", 0);
      } finally {
        setBusy(false);
      }
    });

    $("#disconnect-wireless").addEventListener("click", async () => {
      const address = selectedDevice();
      const selected = (app.state?.devices || []).find(item => item.serial === address);
      if (!address || deviceConnectionType(selected) !== "wireless") {
        notify("请选择无线 ADB 设备", "只有 IP:PORT 或无线调试设备可以断开。", "error");
        return;
      }
      if (!confirm(`确认断开无线 ADB 连接 ${address}？`)) return;
      setBusy(true, `正在断开 ${address}...`);
      try {
        const result = await api("/api/disconnect", {
          method: "POST",
          body: JSON.stringify({ address }),
        });
        if (localStorage.getItem("mobile-profiler-device-android") === address) {
          localStorage.removeItem("mobile-profiler-device-android");
        }
        notify("无线 ADB 已断开", result.output || address, "success", 7000);
        await refreshState();
      } catch (error) {
        notify("无线 ADB 断开失败", error.message, "error", 8000);
      } finally {
        setBusy(false);
      }
    });

    $("#refresh-devices").addEventListener("click", async () => {
      const button = $("#refresh-devices");
      const platform = selectedPlatform();
      button.classList.add("loading");
      try {
        const result = await api("/api/devices/refresh", {
          method: "POST",
          body: JSON.stringify({ platform }),
        });
        await refreshState();
        const platformError = platform === "ios"
          ? app.state?.ios_error
          : platform === "harmony"
            ? app.state?.harmony_error
            : result.error || app.state?.device_error;
        if (platformError) {
          notify(`${platformLabel(platform)} 连接检查失败`, platformError, "error", 0);
        }
      } catch (error) {
        notify("设备刷新失败", error.message, "error", 0);
      } finally {
        button.classList.remove("loading");
      }
    });

    $("#scan-apps").addEventListener("click", async () => {
      const device = selectedDevice();
      const selected = selectedDeviceInfo();
      if (!device || devicePlatform(selected) !== "android" || selected?.state !== "device") {
        notify("请选择在线 Android 设备", "应用扫描需要一台已授权且处于 device 状态的手机。", "error");
        return;
      }
      setBusy(true, "正在扫描手机上的可启动应用...");
      try {
        const result = await api("/api/apps", {
          method: "POST",
          body: JSON.stringify({ device, platform: "android" }),
        });
        app.scannedApps = Array.isArray(result.apps) ? result.apps : [];
        app.scannedAppsDevice = result.device || device;
        app.scannedAppsSource = result.source || "launcher-activities";
        $("#app-search-input").value = "";
        const currentPackage = $("#package-input").value.trim();
        app.selectedScannedPackage = app.scannedApps.some(item => item.package === currentPackage)
          ? currentPackage
          : "";
        renderAppOptions();
        updateAppScannerAvailability(selected);
        const warnings = Array.isArray(result.warnings) ? result.warnings.filter(Boolean) : [];
        notify(
          "手机应用扫描完成",
          `${app.scannedApps.length} 个${result.source === "third-party-packages-fallback" ? "第三方包" : "可启动应用"} · ${Number(result.icon_count || 0)} 个图标${warnings.length ? ` · ${warnings.join("；")}` : ""}`,
          warnings.length ? "error" : "success",
          8000,
        );
      } catch (error) {
        resetAppScanner();
        updateAppScannerAvailability(selected);
        notify("应用扫描失败", `${error.message}；仍可手工输入游戏包名。`, "error", 9000);
      } finally {
        setBusy(false);
      }
    });

    $("#app-search-input").addEventListener("input", renderAppOptions);
    $("#app-select").addEventListener("change", event => {
      const packageName = event.target.value;
      if (!packageName) return;
      $("#package-input").value = packageName;
      app.selectedScannedPackage = packageName;
      renderAppOptions();
    });
    $("#app-result-list").addEventListener("click", event => {
      const option = event.target.closest("[data-app-package]");
      if (!option || option.disabled) return;
      const packageName = option.dataset.appPackage || "";
      if (!packageName) return;
      $("#package-input").value = packageName;
      app.selectedScannedPackage = packageName;
      renderAppOptions();
    });
    $("#package-input").addEventListener("input", event => {
      const packageName = event.target.value.trim();
      app.selectedScannedPackage = app.scannedApps.some(item => item.package === packageName)
        ? packageName
        : "";
      renderAppOptions();
    });

    $$("[data-duration]").forEach(button => button.addEventListener("click", () => {
      $("#duration-input").value = button.dataset.duration;
      syncDurationPreset();
    }));

    $("#duration-input").addEventListener("input", syncDurationPreset);

    $$("[data-metric]").forEach(button => button.addEventListener("click", () => {
      app.metric = button.dataset.metric;
      $$("[data-metric]").forEach(item => item.classList.toggle("active", item === button));
      renderChart();
    }));
    $("#live-detail-grid").addEventListener("click", event => {
      const card = event.target.closest("[data-live-metric]");
      if (!card) return;
      app.metric = card.dataset.liveMetric;
      $$("[data-metric]").forEach(item => item.classList.toggle("active", item.dataset.metric === app.metric));
      renderChart();
    });

    $("#record-form").addEventListener("submit", async event => {
      event.preventDefault();
      const payload = recordPayload();
      if (!payload.device) {
        notify("请选择设备", `需要一台处于 device 状态的 ${platformLabel(selectedPlatform())} 设备。`, "error");
        return;
      }
      if (payload.test_mode === "performance" && !payload.package) {
        notify("请选择测试游戏 / 应用", "性能测试必须绑定具体包名；Android 可先扫描手机应用后选择。", "error", 8000);
        $("#package-input").focus();
        return;
      }
      setBusy(true, "正在启动采集...");
      try {
        const active = await api("/api/record", { method: "POST", body: JSON.stringify(payload) });
        app.state = { ...(app.state || {}), active };
        renderActive(active);
        notify("采集已启动", active.output_dir || "Collector is starting");
        switchView("live");
      } catch (error) {
        notify("无法开始采集", error.message, "error", 8000);
      } finally {
        setBusy(false);
        refreshState();
      }
    });

    $("#stop-record").addEventListener("click", async () => {
      if (!confirm("停止当前采集并生成可恢复的部分报告？")) return;
      setBusy(true, "正在通知采集器收尾...");
      try {
        const active = await api("/api/stop", { method: "POST", body: "{}" });
        app.state = { ...(app.state || {}), active };
        renderActive(active);
        notify("已请求停止", "采集器正在保存数据并生成报告。", "success");
      } catch (error) {
        notify("停止失败", error.message, "error");
      } finally {
        setBusy(false);
      }
    });

    $("#add-marker").addEventListener("click", async () => {
      const name = prompt("请输入时间标记名称，例如：BTR2 开始、进入视频测试、发现异常");
      if (!name || !name.trim()) return;
      try {
        const marker = await api("/api/marker", {
          method: "POST",
          body: JSON.stringify({ name: name.trim() }),
        });
        notify("时间标记已保存", `${marker.name} · uptime ${Number(marker.device_uptime_s).toFixed(3)} s`, "success");
      } catch (error) {
        notify("无法添加标记", error.message, "error");
      }
    });

    $("#run-probe").addEventListener("click", async () => {
      const device = selectedDevice();
      if (!device) {
        notify("请选择设备", "Probe 需要一台在线设备。", "error");
        return;
      }
      setBusy(true, "正在读取设备能力...");
      try {
        const entry = await api("/api/probe", {
          method: "POST",
          body: JSON.stringify({
            device,
            platform: selectedPlatform(),
            gpu_frequency_path: $("#gpu-path-input").value.trim(),
          }),
        });
        app.state.probes = { ...(app.state.probes || {}), [device]: entry };
        renderProbe(app.state);
        updateCaptureFeatureControls();
        const powered = entry.data?.battery?.powered;
        const unplugged = !powered || (Array.isArray(powered) && powered.length === 0);
        notify(unplugged ? "Probe 完成，设备可测试" : "Probe 完成，检测到外部供电", unplugged ? "电流与 CPU 数据源已检查。" : `powered: ${Array.isArray(powered) ? powered.join(", ") : powered}`, unplugged ? "success" : "error", 7000);
      } catch (error) {
        notify("Probe 失败", error.message, "error", 8000);
      } finally {
        setBusy(false);
      }
    });

    $("#clear-console").addEventListener("click", () => {
      app.consoleClearedAt = Date.now() / 1000;
      renderConsole(app.state?.active);
    });

    $("#refresh-history").addEventListener("click", async () => {
      await refreshState();
      notify("历史记录已刷新", app.state?.output_root || "profiler-runs", "success", 2500);
    });

    const savedToolFields = {
      "import-log-path": "mobile-profiler-import-log-path",
      "import-rules-path": "mobile-profiler-import-rules-path",
      "import-match-input": "mobile-profiler-import-match",
      "archive-attachments-input": "mobile-profiler-archive-attachments",
      "archive-output-path": "mobile-profiler-archive-output",
      "portable-output-directory": "mobile-profiler-portable-output",
    };
    Object.entries(savedToolFields).forEach(([id, key]) => {
      const saved = localStorage.getItem(key);
      if (saved) $("#" + id).value = saved;
    });

    $("#regenerate-report-button").addEventListener("click", async () => {
      const runName = $("#maintenance-run-select").value;
      if (!runName) return notify("请选择运行记录", "没有可重建的采集目录。", "error");
      await runToolOperation(
        "/api/report",
        { run_name: runName },
        `正在重新分析 ${runName}...`,
        "报告已重建",
      );
    });

    $("#recover-run-button").addEventListener("click", async () => {
      const runName = $("#maintenance-run-select").value;
      if (!runName) return notify("请选择运行记录", "没有可恢复的采集目录。", "error");
      if (!confirm(`确认从落盘日志恢复 ${runName}？该运行会标记为 recovered。`)) return;
      await runToolOperation(
        "/api/recover",
        { run_name: runName },
        `正在恢复 ${runName}...`,
        "中断运行已恢复",
      );
    });

    $("#import-log-form").addEventListener("submit", async event => {
      event.preventDefault();
      const payload = {
        run_name: $("#import-run-select").value,
        log_path: $("#import-log-path").value.trim(),
        rules_path: $("#import-rules-path").value.trim(),
        match: $("#import-match-input").value,
        replace: $("#import-replace-input").checked,
      };
      if (!payload.run_name || !payload.log_path) {
        notify("信息不完整", "请选择运行记录并填写 BTR2 日志路径。", "error");
        return;
      }
      localStorage.setItem("mobile-profiler-import-log-path", payload.log_path);
      localStorage.setItem("mobile-profiler-import-rules-path", payload.rules_path);
      localStorage.setItem("mobile-profiler-import-match", payload.match);
      await runToolOperation(
        "/api/import-log",
        payload,
        "正在对齐日志并重新分析...",
        "BTR2 日志已导入",
      );
    });

    $("#archive-form").addEventListener("submit", async event => {
      event.preventDefault();
      const payload = {
        run_name: $("#archive-run-select").value,
        attachments: $("#archive-attachments-input").value,
        output_path: $("#archive-output-path").value.trim(),
      };
      if (!payload.run_name) return notify("请选择运行记录", "没有可归档的采集目录。", "error");
      localStorage.setItem("mobile-profiler-archive-attachments", payload.attachments);
      localStorage.setItem("mobile-profiler-archive-output", payload.output_path);
      await runToolOperation(
        "/api/archive",
        payload,
        `正在归档 ${payload.run_name}...`,
        "证据包已生成",
      );
    });

    $("#compare-form").addEventListener("submit", async event => {
      event.preventDefault();
      const payload = {
        run_a: $("#compare-run-a").value,
        run_b: $("#compare-run-b").value,
        label_a: $("#compare-label-a").value.trim(),
        label_b: $("#compare-label-b").value.trim(),
        title: $("#compare-title").value.trim(),
        output_name: $("#compare-output-name").value.trim(),
      };
      if (!payload.run_a || !payload.run_b || payload.run_a === payload.run_b) {
        notify("请选择两条不同记录", "Run A 与 Run B 不能相同。", "error");
        return;
      }
      const result = await runToolOperation(
        "/api/compare",
        payload,
        "正在重新分析两条记录并生成对比...",
        "双机对比报告已生成",
      );
      if (result) $("#compare-output-name").value = "";
    });

    $("#portable-build-form").addEventListener("submit", async event => {
      event.preventDefault();
      const outputDirectory = $("#portable-output-directory").value.trim();
      if (!confirm(`确认重新构建便携包？\n\n输出目录：${outputDirectory || "dist\\mobile-profiler-portable"}\n已有同名目录和 ZIP 会被替换。`)) return;
      localStorage.setItem("mobile-profiler-portable-output", outputDirectory);
      await runToolOperation(
        "/api/build-portable",
        {
          output_directory: outputDirectory,
          include_adb: $("#portable-include-adb").checked,
        },
        "正在构建便携包，首次运行可能需要下载 Embedded Python...",
        "新版便携包已生成",
      );
    });

    $("#clear-tool-output").addEventListener("click", () => {
      $("#tool-output").textContent = "等待操作。所有路径都指向运行 UI 的这台电脑。";
      $("#tool-output-link").classList.add("hidden");
    });

    const chartWrap = $("#chart-wrap");
    chartWrap.addEventListener("mousemove", event => {
      const geometry = app.chartGeometry;
      if (!geometry?.coordinates?.length) return;
      const rect = chartWrap.getBoundingClientRect();
      const viewX = ((event.clientX - rect.left) / rect.width) * geometry.width;
      let nearest = geometry.coordinates[0];
      let distance = Math.abs(nearest.x - viewX);
      geometry.coordinates.forEach(point => {
        const nextDistance = Math.abs(point.x - viewX);
        if (nextDistance < distance) {
          nearest = point;
          distance = nextDistance;
        }
      });
      geometry.hoverLine.setAttribute("x1", nearest.x);
      geometry.hoverLine.setAttribute("x2", nearest.x);
      geometry.hoverLine.setAttribute("opacity", "1");
      const tooltip = $("#chart-tooltip");
      tooltip.classList.remove("hidden");
      tooltip.innerHTML = `${escapeHtml(formatDuration(nearest.elapsed))}<strong>${Number(nearest.value).toFixed(geometry.definition.digits)} ${escapeHtml(geometry.definition.unit)}</strong>`;
      const tooltipX = clamp(event.clientX - rect.left + 12, 8, rect.width - 145);
      const tooltipY = clamp(event.clientY - rect.top - 20, 8, rect.height - 54);
      tooltip.style.left = `${tooltipX}px`;
      tooltip.style.top = `${tooltipY}px`;
    });
    chartWrap.addEventListener("mouseleave", () => {
      $("#chart-tooltip").classList.add("hidden");
      if (app.chartGeometry?.hoverLine) app.chartGeometry.hoverLine.setAttribute("opacity", "0");
    });
  }

  bindEvents();
  setPlatform(app.platform, { initial: true });
  setTestMode("power", { initial: true });
  switchView(app.activeView);
  pollLoop();
})();
