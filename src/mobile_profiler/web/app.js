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
  const liveTimelineLayoutStorageKey = "mobile-profiler-live-timeline-layout-v1";
  const agentConfigTabStorageKey = "mobile-profiler-agent-config-tab";
  const agentPromptTabStorageKey = "mobile-profiler-agent-prompt-tab";
  const agentTemplateDraftStorageKey = "mobile-profiler-agent-template-drafts-v1";
  const agentCustomTemplateStorageKey = "mobile-profiler-agent-custom-templates-v2";
  const agentSelectedTemplateStorageKey = "mobile-profiler-agent-selected-template-v1";
  const agentSystemPromptStorageKey = "mobile-profiler-agent-system-prompt-v1";
  const agentTemporarySessionStorageKey = "mobile-profiler-agent-temporary-session-v1";
  const agentConfigTabs = ["workflow", "campaign", "software", "model", "prompt"];
  const agentPromptTabs = ["workflow", "task", "system"];
  const maxUiLogLines = 500;
  const uiErrorDedupWindowS = 2;
  const liveTimelineLayoutDefinitions = [
    { key: "cpu_pct", label: "CPU 总负载", hint: "整体 CPU 使用率" },
    { key: "cpu_frequency", label: "CPU 核心组频率", hint: "按平台可验证的核心/频率分组展示" },
    { key: "frame_rate_fps", label: "渲染帧率与 1% Low", hint: "前台应用呈现帧率" },
    { key: "frame_flow", label: "渲染链路各节点", hint: "完整链路节点随时间的帧率" },
    { key: "refresh_rate_hz", label: "显示刷新率", hint: "随时间变化的屏幕档位" },
    { key: "frame_time_ms", label: "帧耗时 P95 / P99", hint: "逐帧呈现间隔；详细 framestats 开启时补充阶段数据" },
    { key: "frame_issue_pct", label: "异常帧占比", hint: "截止时间 / VSync 统计" },
    { key: "gpu_load_pct", label: "GPU 负载", hint: "图形处理器使用率" },
    { key: "gpu_frequency_mhz", label: "GPU 频率", hint: "GPU 当前工作频率" },
    { key: "memory_frequency_mhz", label: "内存 / DMC 频率", hint: "DRAM / DMC / MIF" },
    { key: "power_mw", label: "功率通道", hint: "iOS 分开显示 SystemLoad 与电池 I×V" },
    { key: "current_ma", label: "电池电流", hint: "充放电电流幅值" },
    { key: "voltage_mv", label: "电池电压", hint: "电池端实时电压" },
    { key: "temperature_c", label: "电池温度", hint: "BatteryService / thermal" },
  ];
  const defaultLiveTimelineOrder = liveTimelineLayoutDefinitions.map(item => item.key);
  const liveTimelineLayoutKeys = new Set(defaultLiveTimelineOrder);
  const liveTimelineLayoutDefinitionMap = new Map(
    liveTimelineLayoutDefinitions.map(item => [item.key, item]),
  );

  function normalizeLiveTimelineOrder(value) {
    const seen = new Set();
    const order = [];
    (Array.isArray(value) ? value : []).forEach(rawKey => {
      const key = String(rawKey || "");
      if (!liveTimelineLayoutKeys.has(key) || seen.has(key)) return;
      seen.add(key);
      order.push(key);
    });
    defaultLiveTimelineOrder.forEach(key => {
      if (!seen.has(key)) order.push(key);
    });
    return order;
  }

  function normalizeLiveTimelineHidden(value) {
    return new Set(
      (Array.isArray(value) ? value : [])
        .map(rawKey => String(rawKey || ""))
        .filter(key => liveTimelineLayoutKeys.has(key)),
    );
  }

  function loadLiveTimelineLayouts() {
    try {
      const stored = JSON.parse(localStorage.getItem(liveTimelineLayoutStorageKey) || "{}");
      const contexts = stored?.contexts && typeof stored.contexts === "object" ? stored.contexts : {};
      return Object.fromEntries(
        Object.entries(contexts).map(([context, layout]) => [
          context,
          {
            order: normalizeLiveTimelineOrder(layout?.order),
            hidden: normalizeLiveTimelineHidden(layout?.hidden),
          },
        ]),
      );
    } catch (_error) {
      return {};
    }
  }

  function loadAgentTemplateDrafts() {
    try {
      const parsed = JSON.parse(localStorage.getItem(agentTemplateDraftStorageKey) || "{}");
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (_error) {
      return {};
    }
  }

  function loadAgentCustomTemplates() {
    try {
      const parsed = JSON.parse(localStorage.getItem(agentCustomTemplateStorageKey) || "[]");
      return Array.isArray(parsed)
        ? parsed.filter(item => item && typeof item === "object" && String(item.id || "").trim())
        : [];
    } catch (_error) {
      return [];
    }
  }

  const app = {
    state: null,
    metric: "power_mw",
    testMode: "power",
    durationUnlimited: false,
    platform: ["android", "ios", "harmony"].includes(storedPlatform)
      ? storedPlatform
      : "android",
    polling: false,
    activeView: location.hash.replace("#", "") || "live",
    consoleClearedAt: 0,
    uiLogs: [],
    currentRunName: null,
    chartGeometry: null,
    notifiedWarnings: new Set(),
    notifiedBrightnessPoints: new Set(),
    scannedApps: [],
    scannedAppsDevice: "",
    scannedAppsSource: "",
    selectedScannedPackage: "",
    brightnessDevice: "",
    brightnessInfo: null,
    brightnessError: "",
    brightnessLoading: false,
    brightnessCalibrating: false,
    brightnessRequestId: 0,
    captureFeaturesOverridden: false,
    liveTimelineLayouts: loadLiveTimelineLayouts(),
    liveTimeRange: null,
    liveTimeRangeDraft: null,
    liveTimeRangePointer: null,
    liveRangeSummary: null,
    liveRangeSummaryRequestId: 0,
    liveRangePresentationFrame: 0,
    agentDefaultsApplied: false,
    agentScreenshotRevision: -1,
    agentNotificationKey: "",
    agentTaskSeed: 0,
    agentTemplateSignature: "",
    agentTemplateDrafts: loadAgentTemplateDrafts(),
    agentCustomTemplates: loadAgentCustomTemplates(),
    agentServerTemplates: [],
    agentSelectedTemplateId: localStorage.getItem(agentSelectedTemplateStorageKey) || "",
    agentTemplateInitialized: false,
    agentTemplateLoading: false,
    agentCampaignStage: "",
    agentCampaignConfigStage: "prepare",
    agentCampaignConfigSection: { prepare: "overview", test: "overview" },
    agentCampaignConfigRenderSignature: "",
    agentCampaignRuntimeOverrides: false,
    agentPromptTaskId: "",
    agentDefaultSystemPrompt: "",
    agentSavedSystemPrompt: localStorage.getItem(agentSystemPromptStorageKey) || "",
    agentPromptDirty: false,
    agentTemporarySessionId: localStorage.getItem(agentTemporarySessionStorageKey) || "",
    agentEditorSessionId: "",
    agentProviderSignature: "",
    agentProviderProfiles: new Map(),
    agentPresentedProvider: "",
    agentRunConsoleSection: "decision",
    agentSoftwareCategory: "overview",
    agentSoftwareAssets: new Map(),
    agentSoftwareAssetsDevice: "",
    agentSoftwareAssetsSignature: "",
    agentSoftwareAssetsLoading: false,
    agentSoftwareInstallingPackage: "",
    agentSoftwareInstallSessionId: "",
    agentConfigTab: agentConfigTabs.includes(localStorage.getItem(agentConfigTabStorageKey))
      ? localStorage.getItem(agentConfigTabStorageKey)
      : "workflow",
    agentPromptTab: agentPromptTabs.includes(localStorage.getItem(agentPromptTabStorageKey))
      ? localStorage.getItem(agentPromptTabStorageKey)
      : "workflow",
    agentDefaultSystemPromptVersion: "",
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
      performanceIntervalHint: "读取前台窗口上下文；检测到 SurfaceView/BLAST 或普通应用 buffer 层后，以 0.5 秒节奏采集呈现时间戳。",
      probeTitle: "Android 设备能力检查",
      probeDescription: "确认电池供电、电流传感器、CPU policy、GPU 节点、gfxinfo 与 SurfaceFlinger 能力。",
      probePlaceholder: "选择在线 Android 设备后运行 Probe。该操作只读，不会开始采集或重置 BatteryStats。",
      powerDescription: "以电流功率、CPU/GPU、前台场景、热状态和测试条件为主；进程、调度与模型归因按需开启。",
      performanceDescription: "以 SurfaceFlinger 前台应用层呈现 FPS、1% Low、帧间隔、渲染阶段、CPU/GPU 与热限制为主；系统诊断按需开启。",
      powerNote: "建议先运行 Probe，确认 powered 为空、BatteryStats 和电流命令可用。",
      performanceNote: "性能模式会读取前台应用渲染层呈现时间戳并提高窗口上下文频率；建议关闭录屏、悬浮窗与其他调试工具。",
    },
    ios: {
      title: "iOS 平台",
      description: "RemoteXPC、DVT sysmond、PowerTelemetry 与事件驱动的前台应用状态",
      deviceKicker: "IOS DEVICE",
      addressLabel: "RemoteXPC",
      addressPlaceholder: "使用 USB 信任与 RemotePairing",
      connectLabel: "创建配对",
      packageLabel: "目标 Bundle ID",
      packagePlaceholder: "com.example.iosapp",
      desktopLabel: "主屏幕 / Home Screen",
      schedulerLabel: "DVT 资源上下文",
      performanceIntervalHint: "iOS 性能数据跟随 DVT 主采样周期，不单独轮询帧窗口。",
      probeTitle: "iOS 设备能力检查",
      probeDescription: "确认 RemoteXPC、PowerTelemetry、DVT CPU/GPU/进程数据，并区分 USB 网络端点与拔线后仍可达的 LAN 端点。",
      probePlaceholder: "选择已信任或已完成 RemotePairing 的 iPhone 后运行 Probe；不会修改设备设置。",
      powerDescription: "以整机原始 SystemLoad、电池流量、电流和温度为主，并记录 DVT 进程资源与观察者开销。",
      performanceDescription: "聚焦 DVT CPU/GPU、系统负载、前台应用状态与观察者相关进程 CPU；当前不提供通用应用 FPS。",
      powerNote: "正式功耗与续航测试需在拔掉 USB 后重新刷新，并确认非链路本地 LAN RemotePairing 端点仍在线；169.254/16 可能只是 USB-NCM。DiagnosticsService 的整机原始 SystemLoad 通道通常约 20 秒更新一次。",
      performanceNote: "iOS 性能模式记录 DVT CPU/GPU/进程资源和整机原始 SystemLoad 通道；powerScore 只作相对诊断，不转换为 mW。",
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
      performanceIntervalHint: "原生 RenderService/前台 Ability 建议 5 秒；SmartPerf 主数据固定使用设备原生约 1 秒节奏。",
      probeTitle: "HarmonyOS 设备能力检查",
      probeDescription: "确认 BatteryService、CPU/GPU/DDR、RenderService、SmartPerf 和 power-shell 能力。",
      probePlaceholder: "选择在线 HarmonyOS HDC 设备后运行 Probe；该操作只读，不会切换高性能模式。",
      powerDescription: "以 BatteryService 电流功率、CPU 频率、前台场景和热状态为主；原生后端无效的 GPU/DDR 与系统诊断默认关闭。",
      performanceDescription: "原生模式只展示可验证的 RenderService 合成节奏、CPU 频率和热状态；SmartPerf 提供应用 FPS、GPU 与原始帧抖动，目标进程扫描默认关闭。",
      powerNote: "正式功耗测试请使用无线 HDC 并拔掉 USB，确认 BatteryService 处于放电状态。",
      performanceNote: "SmartPerf 采集与设备 602 高性能模式相互独立；能力上限测试会明显增加功耗和温度。",
    },
  };

  const platformUnavailableFeatures = {
    android: new Set(["harmony_hitches", "touch_events"]),
    harmony: new Set(["hot_threads", "runtime_settings", "power_attribution"]),
    ios: new Set([
      "cpu_frequency", "memory_frequency", "frame_rate", "frame_details",
      "harmony_hitches", "touch_events", "target_process", "hot_threads", "scheduler",
      "runtime_settings", "power_attribution",
    ]),
  };

  const platformRequiredFeatures = {
    android: new Set(),
    harmony: new Set(),
    ios: new Set(["cpu_usage", "gpu_metrics", "foreground_window", "thermal"]),
  };

  const onePercentLowValue = point => finite(point?.one_percent_low_fps)
    ? Number(point.one_percent_low_fps)
    : null;
  const iosSystemLoadPowerSource = "ios_power_telemetry_system_load";
  const iosSystemLoadStaleAfterS = 30;

  function standardOnePercentLowAvailable(active) {
    const performance = active?.performance || {};
    if (performance.one_percent_low_standard === true) return true;
    const source = String(performance.one_percent_low_source || "").toLowerCase();
    const rows = Array.isArray(active?.performance_series) ? active.performance_series : [];
    const detailedIntervals = rows.some(row => (
      Array.isArray(row?.frame_intervals_ms) && row.frame_intervals_ms.length > 0
    ));
    const detailedSource = ["slowest 1%", "frame-time histogram", "frame-jitter"].some(token => source.includes(token));
    return detailedIntervals || (detailedSource && !source.includes("sampled-window") && !source.includes("counter-window"));
  }

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
      axis: { fixedMin: 0, minSpan: 6, padding: .04, tickDigits: 1 },
    },
    voltage_mv: {
      title: "电池电压",
      legend: "Battery voltage",
      color: "#9e8cff",
      value: point => finite(point.voltage_mv) ? Number(point.voltage_mv) / 1000 : null,
      unit: "V",
      digits: 3,
      axis: { fixedMin: 0, minSpan: .3, padding: .04, tickDigits: 2 },
    },
    frame_rate_fps: {
      title: "实时帧率与 1% Low",
      legend: "Foreground frame rate",
      color: "#4bc6e8",
      value: point => finite(point.frame_rate_fps) ? Number(point.frame_rate_fps) : null,
      unit: "FPS",
      digits: 1,
      series: "performance",
      secondaryLabel: "1% LOW",
      secondaryQuantile: .01,
      secondaryValue: onePercentLowValue,
      overlay: {
        legend: "1% Low",
        color: "#f1d267",
        value: onePercentLowValue,
      },
      axis: { fixedMin: 0, minSpan: 30, padding: .08, tickDigits: 0 },
      reference: active => {
        const observedRates = Array.isArray(active?.performance?.observed_refresh_rates_hz)
          ? active.performance.observed_refresh_rates_hz.filter(finite).map(Number)
          : [];
        if (new Set(observedRates).size > 1) return null;
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
        const observedRates = Array.isArray(active?.performance?.observed_refresh_rates_hz)
          ? active.performance.observed_refresh_rates_hz.filter(finite).map(Number)
          : [];
        if (new Set(observedRates).size > 1) return null;
        const refreshRate = Number(active?.performance?.current_refresh_rate_hz);
        return finite(refreshRate) && refreshRate > 0
          ? {
            value: 1000 / refreshRate,
            label: `帧预算 ${formatAxisNumber(1000 / refreshRate, 2)} ms`,
          }
        : null;
      },
    },
    refresh_rate_hz: {
      title: "显示刷新率",
      legend: "Display refresh rate",
      color: "#f1d267",
      value: point => finite(point.refresh_rate_hz)
        ? Number(point.refresh_rate_hz)
        : finite(point.value) ? Number(point.value) : null,
      unit: "Hz",
      digits: 0,
      series: "performance",
      axis: { fixedMin: 0, minSpan: 60, padding: .05, tickDigits: 0 },
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

  function orderedCpuClusters(active) {
    const clusters = Array.isArray(active?.clusters) ? active.clusters : [];
    return clusters
      .map((cluster, index) => ({ cluster, index }))
      .sort((left, right) => {
        const leftMaximum = Number(left.cluster?.maximum_mhz);
        const rightMaximum = Number(right.cluster?.maximum_mhz);
        if (finite(leftMaximum) && finite(rightMaximum) && leftMaximum !== rightMaximum) {
          return leftMaximum - rightMaximum;
        }
        return left.index - right.index;
      })
      .map(item => item.cluster);
  }

  function cpuClusterDisplayName(cluster, index, clusters) {
    if (clusters.length === 3) return ["小核", "中核", "大核"][index];
    const labels = {
      Little: "小核",
      Middle: "中核",
      Big: "大核",
      Performance: "性能核",
      Prime: "超大核",
      CPU: "CPU",
    };
    return labels[String(cluster?.label || "")]
      || cluster?.label
      || cluster?.name
      || `集群 ${index + 1}`;
  }

  function cpuCoreGroupLabel(cluster) {
    const cores = (Array.isArray(cluster?.cores) ? cluster.cores : [])
      .map(Number)
      .filter(Number.isFinite)
      .sort((left, right) => left - right);
    if (!cores.length) return "CPU 核心";
    const contiguous = cores.every((core, index) => index === 0 || core === cores[index - 1] + 1);
    if (contiguous && cores.length > 1) return `CPU${cores[0]}–${cores.at(-1)}`;
    return cores.map(core => `CPU${core}`).join(" / ");
  }

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
    const observedMax = Math.max(...values);
    let minimum = finite(axis.fixedMin) ? Number(axis.fixedMin) : 0;
    let maximum = finite(axis.fixedMax) ? Number(axis.fixedMax) : Math.max(minimum, observedMax);

    const minimumSpan = Math.max(0, Number(axis.minSpan || 0));
    if (maximum - minimum < minimumSpan && !finite(axis.fixedMax)) {
      maximum = minimum + minimumSpan;
    }

    const padding = Math.max(0, Number(axis.padding ?? .08));
    const span = Math.max(Number.EPSILON, maximum - minimum);
    if (!finite(axis.fixedMax)) maximum += span * padding;

    const targetIntervals = Math.max(3, Number(axis.targetIntervals || 4));
    const tickStep = finite(axis.tickStep)
      ? Number(axis.tickStep)
      : niceChartStep((maximum - minimum) / targetIntervals);
    const scaleMin = minimum;
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

  function recommendedPerformanceInterval(platform = selectedPlatform(), mode = app.testMode) {
    if (mode !== "performance") return 10;
    return platform === "harmony" ? 5 : 2;
  }

  function recommendedPrimaryInterval(platform = selectedPlatform(), mode = app.testMode) {
    return platform === "ios" && mode === "power" ? 5 : 1;
  }

  function targetPackageRequired(platform = selectedPlatform(), mode = app.testMode) {
    if (mode !== "performance") return false;
    if (platform === "android") return true;
    return platform === "harmony" && effectiveCapturePreset() === "harmony-smartperf";
  }

  function deviceBooleanField(device, name, fallback = "") {
    if (!device) return false;
    const value = Object.prototype.hasOwnProperty.call(device, name)
      ? device[name]
      : fallback ? device[fallback] : false;
    return String(value || "").toLowerCase() === "true" || value === true;
  }

  function iosRemoteXpcReady(device) {
    return deviceBooleanField(device, "remote_xpc_ready", "wireless_ready");
  }

  function iosUnplugReady(device) {
    return deviceBooleanField(device, "unplug_ready", "wireless_ready");
  }

  function iosEndpointScope(device) {
    return String(device?.endpoint_scope || "unknown").toLowerCase();
  }

  function iosWirelessTransport(device) {
    const value = String(
      device?.wireless_transport
      || device?.transport
      || device?.connection?.transport
      || device?.metadata?.connection?.transport
      || device?.config?.wireless_transport
      || "unknown"
    ).trim().toLowerCase().replaceAll("_", "-");
    if (["bluetooth", "bluetooth-pan", "pan"].includes(value)) return "bluetooth-pan";
    if (["wifi", "wi-fi", "wlan"].includes(value)) return "wifi";
    if (["usb-ncm", "link-local"].includes(value)) return "usb-ncm";
    return "unknown";
  }

  function iosWirelessTransportLabel(device, compact = false) {
    const transport = iosWirelessTransport(device);
    if (compact) {
      return {
        "bluetooth-pan": "蓝牙 PAN",
        wifi: "Wi-Fi",
        "usb-ncm": "USB-NCM",
        unknown: "无线类型未确认",
      }[transport];
    }
    return {
      "bluetooth-pan": "蓝牙热点（PAN）RemotePairing",
      wifi: "Wi-Fi RemotePairing",
      "usb-ncm": "USB-NCM 链路本地 RemotePairing",
      unknown: "无线 RemotePairing（类型未确认）",
    }[transport];
  }

  function recordingDeviceReadiness(device = selectedDeviceInfo()) {
    if (!device || device.state !== "device") {
      return { ready: false, reason: `需要一台处于 device 状态的 ${platformLabel(selectedPlatform())} 设备。` };
    }
    if (devicePlatform(device) === "ios" && !iosRemoteXpcReady(device)) {
      return {
        ready: false,
        reason: "iPhone 尚无可用的 RemotePairing RemoteXPC 端点。请保持 USB 连接并解锁，先点击“创建 iOS RemotePairing”。",
      };
    }
    if (
      devicePlatform(device) === "ios"
      && $("#unplugged-input")?.checked
      && !iosUnplugReady(device)
    ) {
      const scope = iosEndpointScope(device);
      return {
        ready: false,
        reason: scope === "link-local"
          ? "当前 RemoteXPC 端点是链路本地地址，可能依赖 USB-NCM，不能证明拔线后仍可用。请让电脑和 iPhone 接入同一局域网，拔掉 USB 后刷新确认。"
          : "当前 RemotePairing 只确认了端点可连，尚未在拔掉 USB 后验证。请拔线并刷新；设备仍通过 Wi-Fi、局域网或蓝牙 PAN 在线后才能开始断电测试。",
      };
    }
    return { ready: true, reason: "" };
  }

  function defaultHomeRequirementsLabel() {
    const required = targetPackageRequired(selectedPlatform(), app.testMode);
    if (app.testMode !== "performance") return "功耗模式只需确认测试时间";
    return required
      ? "性能模式必须确认测试时间和目标游戏"
      : "性能模式需确认测试时间；目标应用用于场景标记";
  }

  function recordingStartReadiness(active = app.state?.active) {
    if (active?.running) return { ready: false, reason: "采集正在进行", shortReason: "任务进行中" };
    const durationInput = $("#duration-input");
    const duration = Number(durationInput?.value);
    const minimumDuration = Number(durationInput?.min || 2);
    const maximumDuration = Number(durationInput?.max || 604800);
    if (!app.durationUnlimited && (!finite(duration) || duration < minimumDuration || duration > maximumDuration)) {
      return {
        ready: false,
        reason: `测试时间必须在 ${minimumDuration}–${maximumDuration} 秒之间。`,
        shortReason: "请填写有效测试时间",
      };
    }
    if (
      targetPackageRequired(selectedPlatform(), app.testMode)
      && !$("#package-input")?.value.trim()
    ) {
      return {
        ready: false,
        reason: "当前性能采集模式必须选择或填写目标游戏 / 应用包名。",
        shortReason: "请填写性能测试游戏",
      };
    }
    const deviceReadiness = recordingDeviceReadiness();
    if (!deviceReadiness.ready) {
      return {
        ready: false,
        reason: deviceReadiness.reason,
        shortReason: "请先选择满足条件的在线设备",
      };
    }
    return { ready: true, reason: "", shortReason: defaultHomeRequirementsLabel() };
  }

  function updateStartControlState(active = app.state?.active) {
    const button = $("#start-record");
    const requirements = $("#home-start-requirements");
    if (!button || !requirements) return;
    const readiness = recordingStartReadiness(active);
    button.disabled = !readiness.ready;
    button.title = readiness.reason;
    requirements.textContent = readiness.shortReason;
    requirements.classList.toggle("is-blocked", !readiness.ready && !active?.running);
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
    const ios = platform === "ios";
    const harmony = platform === "harmony";
    const performance = app.testMode === "performance";
    const smartPerf = harmony && effectiveCapturePreset() === "harmony-smartperf";
    const lowOverheadOption = $('#capture-preset-input option[value="low-overhead"]');
    if (lowOverheadOption) {
      lowOverheadOption.disabled = ios;
      lowOverheadOption.title = ios
        ? "当前 iOS sidecar 的基础 DVT 流不能由此预设关闭；功耗模式已通过 5 秒主周期降低干扰"
        : "";
      if (ios && $("#capture-preset-input")?.value === "low-overhead") {
        $("#capture-preset-input").value = "auto";
      }
    }
    document.body.dataset.platform = platform;
    $("#config-app-panel-title").textContent = platform === "android" ? "扫描出的应用" : "目标应用";
    $("#config-app-panel-source").textContent = platform === "android"
      ? performance ? "应用扫描已移至首页" : "选择后回填目标应用"
      : performance ? "请在首页手工填写" : "请在测试配置中手工填写";
    $("#platform-title").textContent = profile.title;
    $("#platform-description").textContent = profile.description;
    $("#device-picker-kicker").textContent = profile.deviceKicker;
    $("#device-select").setAttribute("aria-label", `${platformLabel(platform)} 设备`);
    $("#connection-address-label").textContent = profile.addressLabel;
    $("#adb-address-input").placeholder = profile.addressPlaceholder;
    $("#connect-address").textContent = profile.connectLabel;
    const packageRequired = targetPackageRequired(platform, app.testMode);
    const targetLabel = performance
      ? platform === "ios" ? "参考 Bundle ID" : "目标游戏 / 应用包名"
      : profile.packageLabel;
    $("#package-input-label").innerHTML = `${targetLabel} <span>${packageRequired ? "必填" : "可选"}</span>`;
    $("#package-input").placeholder = profile.packagePlaceholder;
    $("#package-input").setAttribute("aria-required", packageRequired ? "true" : "false");
    $("#package-input").required = packageRequired;
    $("#package-input-hint").textContent = performance
      ? platform === "android"
        ? "可从设备扫描结果选择；扫描失败时也可直接手工输入包名。"
        : platform === "ios"
          ? "当前 iOS 后端不提供目标进程专属时间序列；Bundle ID 只作为报告标签，可留空。"
          : packageRequired
            ? "SmartPerf 应用 FPS 必须绑定目标包名，并会在目标不处于前台时停止采用该帧数据。"
            : "原生 RenderService 数据是系统级合成上下文；包名只作为场景标签，选择 SmartPerf 后才必填。"
      : "功耗测试可留空；填写后会保留目标应用的资源与归因上下文。";
    $("#home-start-requirements").textContent = performance
      ? packageRequired
        ? "性能模式必须确认测试时间和目标游戏"
        : "性能模式需确认测试时间；目标应用用于场景标记"
      : "功耗模式只需确认测试时间";
    placeTargetPackageField();
    $("#start-context-input").querySelector('option[value="desktop"]').textContent = profile.desktopLabel;
    $("#scheduler-interval-label").textContent = profile.schedulerLabel;
    $("#performance-interval-hint").textContent = profile.performanceIntervalHint;
    $("#thermal-interval-label").textContent = ios
      ? "电池诊断周期（固定）"
      : smartPerf ? "温度采样周期（固定）" : "温度快照";
    $("#interval-input-hint").textContent = ios
      ? performance
        ? "性能模式 DVT CPU/GPU 主采样默认 1 秒；DiagnosticsService 的整机原始 SystemLoad 通道通常约 20 秒更新。"
        : "功耗模式默认 5 秒读取 DVT CPU 与电池诊断，以降低观察者干扰；DiagnosticsService 的整机原始 SystemLoad 通道通常约 20 秒更新。"
      : harmony
        ? "原生 HDC 默认约 1 秒读取电流与 CPU 整体负载，CPU 核心组频率约 30 秒刷新；SmartPerf 主数据固定约 1 秒。"
        : "默认 1 秒读取电流、CPU 与频率；前台应用层时间戳仍独立按 0.5 秒采集。";
    $("#cpu-frequency-feature-hint").textContent = ios
      ? "iOS 未公开 CPU 核心频率，当前不采集"
      : harmony
        ? "原生约 30 秒刷新；SmartPerf 约 1 秒。按最大频率分组的核心均值"
        : "按核心所属频率策略展示";
    $("#gpu-feature-hint").textContent = ios
      ? "仅展示实际收到的 DVT GPU 利用率事件，不含频率"
      : harmony
        ? "SmartPerf 返回 GPU 频率/负载后才展示"
        : "频率、负载与渲染器";
    $("#memory-frequency-feature-hint").textContent = ios
      ? "iOS 未公开内存频率，当前不采集"
      : harmony
        ? "仅 SmartPerf 验证返回 DDR 后展示"
        : "默认关闭；需可读 DMC 节点";
    $("#thermal-feature-label").textContent = ios
      ? "电池温度"
      : harmony ? "温度传感器" : "温度 / 热限制";
    $("#thermal-feature-hint").textContent = ios
      ? "仅电池温度；不含热严重度、热限制或热降亮"
      : harmony
        ? `${smartPerf ? "SmartPerf 温度随主流约 1 秒采样" : "ThermalService 温度按设置周期采样"}；不含公开热严重度、限亮档位或热降亮上限`
        : "温度曲线、热状态与降亮";
    $("#foreground-window-feature-label").textContent = ios
      ? "前台应用状态"
      : harmony ? "前台应用与显示" : "前台应用 / 窗口";
    $("#foreground-window-feature-hint").textContent = ios
      ? "DVT Running / Suspended；不含分辨率、亮度或刷新率"
      : harmony
        ? "Ability、窗口、刷新配置与 RenderService 背光原始值；原始值非 nit/热限亮"
        : "场景、刷新率、亮度与渲染层";
    $("#frame-feature-group-hint").textContent = ios
      ? "当前 iOS 后端不提供通用应用帧链路"
      : "性能模式只开启当前平台可验证的帧数据";
    $("#frame-rate-feature-hint").textContent = ios
      ? "当前 iOS 后端不提供通用应用 FPS"
      : harmony
        ? "SmartPerf 应用 FPS / RenderService 合成节奏"
        : "前台应用层 / gfxinfo 帧节奏";
    $("#frame-details-feature-hint").textContent = ios
      ? "当前 iOS 后端不提供逐帧阶段时间戳"
      : harmony
        ? "SmartPerf 原始 jitter、P95/P99；不拆渲染阶段"
        : "framestats 阶段、P99 与慢帧结论";
    $("#probe-view-title").textContent = profile.probeTitle;
    $("#probe-view-description").textContent = profile.probeDescription;
    $("#probe-placeholder-copy").textContent = profile.probePlaceholder;
    $("#power-mode-subtitle").textContent = platform === "ios"
      ? "PowerTelemetry / 电流"
      : platform === "harmony" ? "BatteryService / 电流" : "默认 · 电流 / 功率";
    $("#performance-mode-subtitle").textContent = platform === "ios"
      ? "CPU / GPU / 进程"
      : platform === "harmony" ? "SmartPerf / 1% Low / 602" : "FPS / 1% Low / 渲染链路";
    $("#brightness-setting-subtitle").textContent = ios
      ? "读取 AppleARMBacklight 用户亮度、原始背光与实际毫尼特（只读）"
      : harmony
        ? "扫描系统滑杆的真实离散档位，只允许应用 DPM 可稳定回读的亮度值"
        : "按 Android 数值档位固定测试亮度";
    $("#metric-fps-label").textContent = ios ? "CPU 总负载" : "实时帧率";
    $("#metric-fps-tag").textContent = ios ? "CPU" : "FPS";
    $("#metric-one-low-label").textContent = ios ? "GPU 利用率" : "1% Low";
    $("#metric-one-low-tag").textContent = ios ? "GPU" : "LOW";
    $("#metric-frame-p99-label").textContent = ios ? "观察者相关 CPU" : "P99 帧耗时";
    $("#metric-frame-p99-tag").textContent = ios ? "OVERHEAD" : "FRAME";
    $("#metric-frame-issue-label").textContent = ios ? "当前整机 SystemLoad 功率" : "异常帧";
    $("#metric-frame-issue-tag").textContent = ios ? "POWER" : "JANK";
    $("#metric-render-resolution-label").textContent = ios ? "前台应用" : "渲染分辨率";
    $("#metric-render-resolution-tag").textContent = ios ? "APP" : "RENDER";
    $("#metric-interpolation-label").textContent = ios ? "电池温度" : "插帧状态";
    $("#metric-interpolation-tag").textContent = ios ? "THERMAL" : "MEMC";
    $("#metric-memory-label").textContent = ios ? "观察者相关 CPU" : "内存频率";
    $("#metric-memory-tag").textContent = ios ? "OVERHEAD" : "DMC";

    $("#cluster-panel-title").textContent = "CPU 核心频率";
    $("#cluster-panel-source").textContent = ios ? "平台未提供" : "cpufreq policy";
    $("#resource-panel-kicker").textContent = ios ? "DVT RESOURCE TELEMETRY" : harmony ? "HARMONY RESOURCE STATUS" : "RESOURCE STATUS";
    $("#resource-panel-title").textContent = ios ? "iOS 性能资源" : harmony ? "HarmonyOS 性能资源" : "性能资源状态";
    $("#resource-window-label").textContent = ios ? "前台应用" : "前台窗口";
    $("#performance-evidence-label-1").textContent = ios ? "性能数据源" : "帧率来源";
    $("#performance-evidence-label-2").textContent = ios ? "SystemLoad 功率来源" : "Surface 缓冲区来源";
    $("#performance-evidence-label-3").textContent = ios ? "观察者开销" : "插帧判定";
    $("#power-pressure-kicker").textContent = ios ? "IOS POWER OBSERVABILITY" : harmony ? "HARMONY POWER PRESSURE" : "POWER PRESSURE";
    $("#power-pressure-title").textContent = ios ? "iOS 功耗与观察者开销" : harmony ? "HarmonyOS 功耗压力解释" : "功耗压力解释";
    $("#power-pressure-source").textContent = ios ? "PowerTelemetry / DVT / observer" : harmony ? "CPU / GPU / DDR / 进程 / 系统状态" : "负载 / 频率 / 设置与电池侧功率";
    $("#power-pressure-driver-title").textContent = ios ? "原始功率通道" : "资源驱动";
    $("#power-pressure-task-title").textContent = ios ? "进程资源" : "任务负载";
    $("#power-pressure-setting-title").textContent = ios ? "采集器影响" : harmony ? "系统状态" : "设置参数";
    $("#power-pressure-note").innerHTML = ios
      ? "<strong>边界：</strong>PowerTelemetry SystemLoad 是 DiagnosticsService 的整机原始通道；外供时可能接近 SystemPowerIn，但不是电池 I×V，也不是独立电源轨实测。DVT powerScore 仅为相对诊断分数。collector_cpu_pct 是 sysmond、DTServiceHub 与配对服务同期 CPU 的观察者相关上界，不等于工具造成的净增量。"
      : harmony
        ? "<strong>边界：</strong>BatteryService 电流是整机电池侧数据；CPU/GPU/DDR 与进程相关性用于解释趋势，不等于独立电源轨或 Android BatteryStats 归因。"
        : "<strong>边界：</strong>相关系数用于解释电流 / 功率和资源压力是否同步变化，不代表独立硬件电源轨，也不能把多个压力项直接相加。";
    applyPlatformVisibility();
    updateStartControlState();
  }

  function setPlatform(platform, { fromRun = false, initial = false } = {}) {
    const nextPlatform = ["android", "ios", "harmony"].includes(platform) ? platform : "android";
    const active = app.state?.active;
    if (!fromRun && active?.running && nextPlatform !== activePlatform(active)) {
      notify("测试进行中无法切换平台", "停止当前测试后再选择其他平台。", "error");
      return;
    }
    const previousPlatform = app.platform;
    const performanceInterval = $("#performance-interval-input");
    const primaryInterval = $("#interval-input");
    const previousRecommendedInterval = recommendedPerformanceInterval(previousPlatform, app.testMode);
    const previousRecommendedPrimary = recommendedPrimaryInterval(previousPlatform, app.testMode);
    const syncRecommendedInterval = Boolean(
      performanceInterval
      && (
        initial
        || previousPlatform !== nextPlatform
          && Number(performanceInterval.value) === Number(previousRecommendedInterval)
      )
    );
    const syncRecommendedPrimary = Boolean(
      primaryInterval
      && (
        initial
        || previousPlatform !== nextPlatform
          && Number(primaryInterval.value) === Number(previousRecommendedPrimary)
      )
    );
    app.platform = nextPlatform;
    if (syncRecommendedInterval) {
      performanceInterval.value = String(recommendedPerformanceInterval(nextPlatform, app.testMode));
    }
    if (syncRecommendedPrimary) {
      primaryInterval.value = String(recommendedPrimaryInterval(nextPlatform, app.testMode));
    }
    if (previousPlatform !== nextPlatform) {
      app.brightnessRequestId += 1;
      app.brightnessDevice = "";
      app.brightnessInfo = null;
      app.brightnessError = "";
      app.brightnessLoading = false;
      app.brightnessCalibrating = false;
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

  function defaultMarkerName(now = new Date()) {
    const hours = String(now.getHours()).padStart(2, "0");
    const minutes = String(now.getMinutes()).padStart(2, "0");
    const seconds = String(now.getSeconds()).padStart(2, "0");
    return `时间标记 ${hours}:${minutes}:${seconds}`;
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

  function activeDurationUnlimited(active) {
    return Boolean(
      active?.duration_unlimited
      || active?.config?.duration_unlimited
      || active?.metadata?.duration_unlimited
    );
  }

  function syncDurationPreset() {
    const durationInput = $("#duration-input");
    if (durationInput) {
      durationInput.dataset.minimumDuration ||= durationInput.min || "2";
      durationInput.dataset.maximumDuration ||= durationInput.max || "604800";
      durationInput.min = app.durationUnlimited ? "" : durationInput.dataset.minimumDuration;
      durationInput.max = app.durationUnlimited ? "" : durationInput.dataset.maximumDuration;
    }
    const duration = Number(durationInput?.value);
    $$('[data-duration]').forEach(button => {
      const selected = button.dataset.duration === "unlimited"
        ? app.durationUnlimited
        : !app.durationUnlimited && Number(button.dataset.duration) === duration;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    const durationField = $("#duration-field");
    const durationHint = $("#duration-mode-hint");
    durationField?.classList.toggle("duration-unlimited", app.durationUnlimited);
    durationHint?.classList.toggle("is-unlimited", app.durationUnlimited);
    if (durationHint) {
      durationHint.textContent = app.durationUnlimited
        ? "无上限模式已开启；上方秒数会保留为有限模式草稿，采集需手动停止。"
        : "达到设置时间后自动收尾；也可选择无上限并手动停止。";
    }
  }

  const modeDefaults = {
    power: {
      "duration-input": 3720,
      "interval-input": 1,
      "process-interval-input": 10,
      "thread-interval-input": 30,
      "thermal-interval-input": 10,
      "scheduler-interval-input": 30,
      "performance-interval-input": 10,
    },
    performance: {
      "duration-input": 1920,
      "interval-input": 1,
      "process-interval-input": 2,
      "thread-interval-input": 5,
      "thermal-interval-input": 5,
      "scheduler-interval-input": 5,
      "performance-interval-input": 2,
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
      "foreground_window", "thermal", "runtime_settings",
    ]),
    "performance-standard": new Set([
      "cpu_usage", "cpu_frequency", "gpu_metrics",
      "foreground_window", "frame_rate", "thermal",
    ]),
    "harmony-smartperf": new Set([
      "cpu_usage", "cpu_frequency", "gpu_metrics",
      "foreground_window", "frame_rate", "frame_details", "thermal",
    ]),
  };

  function selectedDeviceInfo() {
    return (app.state?.devices || []).find(device => device.serial === selectedDevice()) || null;
  }

  function brightnessCapableDevice(device = selectedDeviceInfo()) {
    const platform = selectedPlatform();
    return ["android", "ios", "harmony"].includes(platform)
      && devicePlatform(device) === platform
      && device?.state === "device"
      ? device
      : null;
  }

  function renderBrightnessControl(device = selectedDeviceInfo()) {
    const input = $("#brightness-input");
    const select = $("#brightness-select");
    const refreshButton = $("#brightness-refresh");
    const calibrateButton = $("#brightness-calibrate");
    const applyButton = $("#brightness-apply");
    const badge = $("#brightness-current-badge");
    const hint = $("#brightness-hint");
    const subtitle = $("#brightness-setting-subtitle");
    if (!input || !select || !refreshButton || !calibrateButton || !applyButton || !badge || !hint) return;
    const readyDevice = brightnessCapableDevice(device);
    const platform = selectedPlatform();
    const harmony = platform === "harmony";
    const ios = platform === "ios";
    refreshButton.textContent = ios ? "读取亮度" : "读取范围";
    applyButton.textContent = ios ? "请在手机端调整" : "应用亮度";
    if (subtitle) {
      subtitle.textContent = ios
        ? "读取 iPhone 用户亮度、原始背光和实际毫尼特"
        : harmony
          ? "按 HarmonyOS 已验证档位固定测试亮度"
          : "按 Android 数值档位固定测试亮度";
    }
    const recording = Boolean(app.state?.active?.running);
    const disabled = !readyDevice || recording || app.brightnessLoading;
    input.classList.toggle("hidden", harmony);
    select.classList.toggle("hidden", !harmony);
    calibrateButton.classList.toggle("hidden", !harmony);
    input.disabled = disabled || ios;
    select.disabled = disabled;
    refreshButton.disabled = disabled;
    calibrateButton.disabled = disabled;
    applyButton.disabled = disabled || ios;
    if (!readyDevice) {
      badge.textContent = "等待设备";
      badge.classList.remove("ready");
      input.value = "";
      select.innerHTML = '<option value="">等待设备</option>';
      delete select.dataset.values;
      hint.textContent = platform === "ios"
        ? "选择在线 iPhone 后读取用户亮度、原始背光和实际毫尼特；当前通道不提供外部写入。"
        : platform === "harmony"
          ? "选择在线 HarmonyOS 设备后读取亮度能力；使用系统滑杆回退时可扫描并选择稳定可回读的离散档位。"
          : "选择在线 Android 设备后读取亮度范围与写入能力。";
      return;
    }
    if (recording) {
      badge.textContent = "采集中锁定";
      badge.classList.remove("ready");
      hint.textContent = "为避免改变本轮测试条件，采集期间不能修改亮度。";
      return;
    }
    if (app.brightnessLoading) {
      badge.textContent = app.brightnessCalibrating ? "扫描中" : "读取中";
      badge.classList.remove("ready");
      hint.textContent = app.brightnessCalibrating
        ? "正在逐级扫描 HarmonyOS 系统滑杆并用 DisplayPowerManagerService 稳定回读，通常需要 3–6 分钟；完成后会恢复前台应用和原屏幕状态。"
        : `正在读取 ${platform === "harmony" ? "HarmonyOS" : platform === "ios" ? "iPhone" : "Android"} 亮度当前值和设备范围…`;
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
    const selectableValues = Array.isArray(info.selectable_values)
      ? info.selectable_values.map(Number).filter(Number.isInteger)
      : [];
    if (harmony) {
      const signature = `${info.current}|${selectableValues.join(",")}`;
      if (select.dataset.values !== signature) {
        select.innerHTML = "";
        const current = Number(info.current);
        if (Number.isInteger(current) && !selectableValues.includes(current)) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = `当前 ${current}（滑杆不可达）`;
          option.disabled = true;
          option.selected = true;
          select.appendChild(option);
        }
        if (!selectableValues.length) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "请先扫描可选值";
          option.selected = true;
          select.appendChild(option);
        } else {
          selectableValues.forEach(value => {
            const option = document.createElement("option");
            option.value = String(value);
            option.textContent = String(value);
            select.appendChild(option);
          });
          if (selectableValues.includes(current)) select.value = String(current);
        }
        select.dataset.values = signature;
      }
      const needsCalibration = info.setter_mode === "settings_fallback" && !selectableValues.length;
      calibrateButton.classList.toggle("hidden", info.setter_mode !== "settings_fallback");
      calibrateButton.textContent = selectableValues.length ? "重新扫描" : "扫描可选值";
      calibrateButton.disabled = disabled || info.setter_mode !== "settings_fallback";
      applyButton.disabled = disabled || info.writable === false || needsCalibration || !select.value;
    } else {
      applyButton.disabled = disabled || info.writable === false || ios;
      if (document.activeElement !== input || !input.value) input.value = String(info.current);
    }
    badge.textContent = ios && finite(info.current_precise)
      ? `${Number(info.current_precise).toFixed(1)}%${finite(info.luminance_nits) ? ` · ${Number(info.luminance_nits).toFixed(1)} nits` : ""}`
      : finite(info.effective_current) && Number(info.effective_current) !== Number(info.current)
      ? `设定 ${info.current} · 有效 ${info.effective_current}`
      : `当前 ${info.current}`;
    badge.classList.add("ready");
    const normalizedStep = finite(info.normalized_step)
      ? Number(info.normalized_step).toFixed(5).replace(/0+$/, "").replace(/\.$/, "")
      : "--";
    const limitDetail = platform === "harmony" && finite(info.brightness_discount)
      ? ` · 显示折扣 ${Number(info.brightness_discount).toFixed(3)}×`
      : "";
    const setterDetail = ios
      ? " · iOS 通道：AppleARMBacklight 只读"
      : platform !== "harmony"
        ? ""
      : info.setter_mode === "direct"
        ? " · 设置通道：系统直写"
        : info.setter_mode === "settings_fallback"
          ? " · 设置通道：设置页兼容（DPM 写接口要求系统应用身份）"
          : " · 当前仅可读取，未发现可用设置通道";
    if (ios) {
      const rawDetail = finite(info.raw_backlight_raw) && finite(info.raw_backlight_maximum)
        ? ` · rawBrightness ${Number(info.raw_backlight_raw).toFixed(0)}/${Number(info.raw_backlight_maximum).toFixed(0)}`
        : "";
      const maxNits = finite(info.maximum_luminance_nits)
        ? ` · 面板公开上限 ${Number(info.maximum_luminance_nits).toFixed(1)} nits`
        : "";
      hint.textContent = `用户亮度 ${finite(info.current_precise) ? Number(info.current_precise).toFixed(1) : "--"}%${rawDetail}${maxNits}。iOS 未向该 sidecar 提供可信的通用亮度 setter，请在手机端调整后点击“读取亮度”复核；采集期间会持续记录实际毫尼特并标记疑似降亮点。`;
    } else if (harmony && info.setter_mode === "settings_fallback") {
      const unavailableCount = Array.isArray(info.unavailable_values) ? info.unavailable_values.length : 0;
      hint.textContent = selectableValues.length
        ? `已校准 ${selectableValues.length} 个稳定档位 · 可选范围 ${selectableValues[0]}–${selectableValues.at(-1)} · 理论范围内另有 ${unavailableCount} 个值不可达（只在下拉框列出可应用值） · ${info.automatic ? "当前为自动亮度，应用时会切换手动" : "当前为手动亮度"}${limitDetail}${setterDetail}`
        : `DisplayPowerManagerService 的理论范围 ${info.minimum}–${info.maximum} 不等于滑杆可达档位。点击“扫描可选值”进行一次约 3–6 分钟的真机校准；校准会临时打开显示设置，并恢复前台应用与原屏幕状态。${setterDetail}`;
    } else {
      hint.textContent = `最小值 ${info.minimum} · 最大值 ${info.maximum} · 最小调整间隔 ${info.step || 1} 级（归一化约 ${normalizedStep}） · ${info.automatic ? "当前为自动亮度，应用时会切换手动" : "当前为手动亮度"}${limitDetail}${setterDetail}`;
    }
  }

  async function refreshBrightnessCapability({ force = false, notifyFailure = false } = {}) {
    const device = brightnessCapableDevice();
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
    app.brightnessCalibrating = false;
    renderBrightnessControl(device);
    try {
      const info = await api("/api/brightness", {
        method: "POST",
        body: JSON.stringify({ device: device.serial, platform: selectedPlatform(), action: "read" }),
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

  async function calibrateHarmonyBrightness() {
    const device = brightnessCapableDevice();
    if (!device || selectedPlatform() !== "harmony") {
      notify("无法扫描亮度档位", "请先选择在线 HarmonyOS 设备。", "error");
      return;
    }
    const requestId = ++app.brightnessRequestId;
    app.brightnessLoading = true;
    app.brightnessCalibrating = true;
    app.brightnessError = "";
    renderBrightnessControl(device);
    try {
      const result = await api("/api/brightness", {
        method: "POST",
        body: JSON.stringify({
          device: device.serial,
          platform: "harmony",
          action: "calibrate",
        }),
      });
      if (requestId !== app.brightnessRequestId) return;
      app.brightnessInfo = result;
      const values = Array.isArray(result.selectable_values) ? result.selectable_values : [];
      const restoreDetail = finite(result.original_value) && finite(result.restored_value)
        && Number(result.original_value) !== Number(result.restored_value)
        ? `；原值 ${result.original_value} 不可达，已恢复到最近的 ${result.restored_value}`
        : "";
      notify(
        "亮度档位扫描完成",
        `识别到 ${values.length} 个稳定档位，范围 ${values[0] ?? "--"}–${values.at(-1) ?? "--"}${restoreDetail}`,
        "success",
        9000,
      );
      const warnings = Array.isArray(result.calibration_warnings)
        ? result.calibration_warnings
        : [];
      if (warnings.length) notify("亮度校准提示", warnings.join("；"), "error", 10000);
    } catch (error) {
      if (requestId !== app.brightnessRequestId) return;
      app.brightnessError = error.message || "无法扫描 HarmonyOS 亮度档位";
      notify("亮度档位扫描失败", app.brightnessError, "error", 10000);
    } finally {
      if (requestId === app.brightnessRequestId) {
        app.brightnessLoading = false;
        app.brightnessCalibrating = false;
        renderBrightnessControl();
      }
    }
  }

  async function applyBrightnessValue() {
    const device = brightnessCapableDevice();
    if (selectedPlatform() === "ios") {
      notify(
        "iPhone 亮度为只读监控",
        "请在 iPhone 上调整亮度，然后点击“读取亮度”复核。iOS 未向当前 sidecar 提供可信的通用外部亮度 setter。",
        "error",
        8000,
      );
      return;
    }
    const input = selectedPlatform() === "harmony"
      ? $("#brightness-select")
      : $("#brightness-input");
    const info = app.brightnessInfo;
    if (!device || !input || !info) {
      notify("无法设置亮度", "请先选择在线 Android 或 HarmonyOS 设备并读取亮度范围。", "error");
      return;
    }
    const value = Number(input.value);
    const selectableValues = Array.isArray(info.selectable_values)
      ? info.selectable_values.map(Number).filter(Number.isInteger)
      : [];
    if (selectedPlatform() === "harmony" && !selectableValues.includes(value)) {
      notify("亮度档位不可用", "请选择扫描后下拉框中列出的 HarmonyOS 亮度值。", "error");
      return;
    }
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
    app.brightnessCalibrating = false;
    app.brightnessError = "";
    renderBrightnessControl(device);
    try {
      const result = await api("/api/brightness", {
        method: "POST",
        body: JSON.stringify({
          device: device.serial,
          platform: selectedPlatform(),
          action: "set",
          value,
        }),
      });
      if (requestId !== app.brightnessRequestId) return;
      app.brightnessInfo = result;
      const setterLabel = selectedPlatform() !== "harmony"
        ? ""
        : result.setter_used === "direct"
          ? " · 系统直写"
          : result.setter_used === "settings_fallback"
            ? " · 设置页兼容通道"
            : result.setter_used === "none"
              ? " · 已是目标值，未打开设置页"
              : "";
      const detail = `当前 ${result.current}${finite(result.effective_current) && Number(result.effective_current) !== Number(result.current) ? ` · 有效 ${result.effective_current}` : ""} · 最小 ${result.minimum} · 最大 ${result.maximum} · 间隔 ${result.step}${setterLabel}`;
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
      ? ["cpu_usage", "foreground_window", "frame_rate", "thermal"]
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
    const smartPerf = platform === "harmony" && effectiveCapturePreset() === "harmony-smartperf";
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
    const forcedOn = new Set(platformRequiredFeatures[platform] || []);
    if (performance) forcedOff.add("power_attribution");
    if (platform === "harmony" && !smartPerf) {
      ["gpu_metrics", "memory_frequency", "frame_details", "target_process"].forEach(name => forcedOff.add(name));
    }
    if (smartPerf) forcedOn.add("frame_details");
    const probeData = currentProbe()?.data || {};
    const memoryUnavailable = platform === "android"
      && probeData.capabilities?.memory_frequency === false;
    const gpuUnavailable = platform === "android"
      && Boolean(Object.keys(probeData).length)
      && !probeData.gpu_source?.frequency_path
      && !probeData.gpu_source?.load_path;
    const smartPerfGpuUnavailable = platform === "harmony"
      && smartPerf
      && probeData.capabilities?.smartperf_gpu_metrics === false;
    const iosGpuUnavailable = platform === "ios"
      && probeData.capabilities?.gpu_utilization === false;
    const iosForegroundUnavailable = platform === "ios"
      && probeData.capabilities?.application_state_notifications === false;
    const iosThermalUnavailable = platform === "ios"
      && Boolean(Object.keys(probeData).length)
      && Number(probeData.system_monitor?.thermal_sensor_count || 0) === 0
      && !finite(probeData.battery?.temperature_c);
    if (memoryUnavailable) forcedOff.add("memory_frequency");
    if (gpuUnavailable || smartPerfGpuUnavailable || iosGpuUnavailable) forcedOff.add("gpu_metrics");
    if (iosForegroundUnavailable) forcedOff.add("foreground_window");
    if (iosThermalUnavailable) forcedOff.add("thermal");
    $$('[data-capture-feature]').forEach(input => {
      const name = input.dataset.captureFeature;
      const required = forcedOn.has(name);
      const disabled = forcedOff.has(name) || required || Boolean(app.state?.active?.running);
      if (forcedOff.has(name)) input.checked = false;
      else if (required) input.checked = true;
      input.disabled = disabled;
      const row = input.closest("label");
      row?.classList.toggle("feature-unavailable", forcedOff.has(name));
      row?.classList.toggle("feature-required", required);
      if (row) {
        row.title = forcedOff.has(name)
          ? name === "memory_frequency" && memoryUnavailable
            ? probeData.memory_probe?.limitations || "设备未向 ADB shell 公开可读的 DMC/DRAM 实时频率"
            : name === "gpu_metrics" && (gpuUnavailable || smartPerfGpuUnavailable || iosGpuUnavailable)
              ? probeData.gpu_probe?.reason || "当前设备没有可用的会话内 GPU 频率或负载数据源"
            : name === "foreground_window" && iosForegroundUnavailable
              ? "iOS Probe 明确未提供 application-state 通知；不会展示伪造的前台应用状态"
            : name === "thermal" && iosThermalUnavailable
              ? "iOS 电池诊断没有返回可用温度，本次不会展示温度曲线"
            : `${platformLabel(platform)} 当前采集后端不提供此项目`
          : required
            ? `${platformLabel(platform)} 基础数据流固定包含此项目，不会启动额外诊断动作`
            : "";
        const impact = row.querySelector(".feature-impact");
        if (impact) {
          impact.dataset.defaultLabel ||= impact.textContent;
          impact.textContent = forcedOff.has(name) ? "不可用" : required ? "固定" : impact.dataset.defaultLabel;
          impact.classList.toggle("fixed", required && !forcedOff.has(name));
        }
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
    const finalValues = Object.fromEntries(inputs.map(input => [
      input.dataset.captureFeature,
      input.checked,
    ]));
    const running = Boolean(app.state?.active?.running);
    [
      ["process-interval-input", "process_snapshots"],
      ["thread-interval-input", "hot_threads"],
      ["thermal-interval-input", "thermal"],
      ["scheduler-interval-input", "scheduler"],
    ].forEach(([id, feature]) => {
      const interval = $("#" + id);
      if (!interval) return;
      const enabled = Boolean(finalValues[feature]);
      const fixedIosThermal = platform === "ios" && feature === "thermal";
      const fixedSmartPerfThermal = smartPerf && feature === "thermal";
      if (feature === "thermal") interval.min = fixedSmartPerfThermal ? "1" : "2";
      if (fixedIosThermal) interval.value = "5";
      else if (fixedSmartPerfThermal) interval.value = "1";
      else if (feature === "thermal" && interval.value === "1") {
        interval.value = String(modeDefaults[app.testMode]?.[id] || 5);
      }
      interval.disabled = running || !enabled || fixedIosThermal || fixedSmartPerfThermal;
      interval.title = fixedIosThermal
        ? "iOS 电池诊断由 sidecar 固定约每 5 秒读取一次"
        : fixedSmartPerfThermal
          ? "SmartPerf 温度来自同一 SP_daemon 主流，固定约每 1 秒一条"
          : "";
      interval.closest(".form-field")?.classList.toggle(
        "capture-interval-disabled",
        !enabled || fixedIosThermal || fixedSmartPerfThermal,
      );
    });
    const primaryInterval = $("#interval-input");
    if (primaryInterval) {
      if (smartPerf) primaryInterval.value = "1";
      primaryInterval.disabled = running || smartPerf;
      primaryInterval.title = smartPerf ? "SmartPerf 主采样由 SP_daemon 固定为约 1 秒" : "";
      primaryInterval.closest(".form-field")?.classList.toggle("capture-interval-disabled", smartPerf);
    }
    const durationInput = $("#duration-input");
    if (durationInput) {
      const minimumDuration = platform === "harmony" ? (smartPerf ? 4 : 8) : 2;
      durationInput.dataset.minimumDuration = String(minimumDuration);
      durationInput.dataset.maximumDuration ||= durationInput.max || "604800";
      durationInput.min = app.durationUnlimited ? "" : String(minimumDuration);
      durationInput.max = app.durationUnlimited ? "" : durationInput.dataset.maximumDuration;
      if (!app.durationUnlimited && Number(durationInput.value) < minimumDuration) {
        durationInput.value = String(minimumDuration);
        syncDurationPreset();
      }
      durationInput.title = platform === "harmony"
        ? smartPerf
          ? "SmartPerf 至少需要 4 秒以形成稳定的多样本时间跨度"
          : "HarmonyOS 原生采集至少需要 8 秒以稳定获得两个以上样本"
        : "";
    }
    const performanceInterval = $("#performance-interval-input");
    if (performanceInterval) {
      const enabled = Boolean(finalValues.foreground_window || finalValues.frame_rate);
      performanceInterval.min = platform === "harmony" ? "5" : "1";
      if (platform === "harmony" && Number(performanceInterval.value) < 5) {
        performanceInterval.value = String(recommendedPerformanceInterval(platform, app.testMode));
      }
      performanceInterval.disabled = running || !enabled;
      performanceInterval.closest(".form-field")?.classList.toggle("capture-interval-disabled", !enabled);
    }
    ["no-reset-input", "full-history-input"].forEach(id => {
      const input = $("#" + id);
      if (!input) return;
      const enabled = Boolean(finalValues.power_attribution);
      input.disabled = running || !enabled;
      input.closest(".compact-check")?.classList.toggle("capture-option-disabled", !enabled);
    });
    const count = inputs.filter(input => input.checked).length;
    $("#capture-feature-count").textContent = `${count} / ${inputs.length} 已启用`;
    $("#system-monitor-input").checked = [
      "process_snapshots", "hot_threads", "thermal", "scheduler",
    ].some(name => Boolean(finalValues[name]));
    const presetLabel = $("#capture-preset-input")?.selectedOptions?.[0]?.textContent || "采集预设";
    const advancedBadge = $("#advanced-setting-count");
    if (advancedBadge) {
      advancedBadge.textContent = customized
        ? "已自定义"
        : $("#capture-preset-input")?.value === "auto" ? "默认已配置" : "预设已应用";
    }
    $("#capture-preset-hint").textContent = customized
      ? `${presetLabel} · 已逐项覆盖；基础电流、电压和时间戳仍保留。`
      : `${presetLabel} · 默认只开启能形成原始曲线或有效结论的数据。`;
    $("#capture-feature-note").textContent = showHighPerformance
      ? "SmartPerf 采集与设备高性能模式相互独立；高性能模式会改变设备功耗和热状态。"
      : platform === "ios"
        ? `iOS 的 CPU、GPU、前台状态与电池温度属于固定基础流；专项诊断动作默认关闭，${app.testMode === "power" ? "主周期默认 5 秒" : "性能主周期默认 1 秒"}。`
        : "基础电流、电压和设备时间戳始终保留；无稳定结论的专项诊断默认关闭。";
  }

  function applyModeDefaults(previousMode, nextMode) {
    if (previousMode === nextMode) return;
    Object.entries(modeDefaults[nextMode]).forEach(([id, nextValue]) => {
      const input = document.getElementById(id);
      if (!input) return;
      if (id === "duration-input" && app.durationUnlimited) return;
      const previousDefault = id === "performance-interval-input"
        ? recommendedPerformanceInterval(selectedPlatform(), previousMode)
        : id === "interval-input"
          ? recommendedPrimaryInterval(selectedPlatform(), previousMode)
        : modeDefaults[previousMode]?.[id];
      const effectiveNextValue = id === "performance-interval-input"
        ? recommendedPerformanceInterval(selectedPlatform(), nextMode)
        : id === "interval-input"
          ? recommendedPrimaryInterval(selectedPlatform(), nextMode)
        : nextValue;
      if (previousDefault === undefined || Number(input.value) === Number(previousDefault)) {
        input.value = String(effectiveNextValue);
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

    $$(".test-mode-switch [data-test-mode]").forEach(button => {
      const selected = button.dataset.testMode === nextMode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.disabled = Boolean(active?.running);
    });

    const performance = nextMode === "performance";
    const profile = platformProfiles[selectedPlatform()];
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
    else if (!validMetrics.has(app.metric)) {
      app.metric = performance ? iosPerformance ? "cpu_pct" : "frame_rate_fps" : "power_mw";
    }
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
    const metadataFeatures = active?.metadata?.capture_configuration?.features;
    if (metadataFeatures && Object.prototype.hasOwnProperty.call(metadataFeatures, name)) {
      return Boolean(metadataFeatures[name]);
    }
    const liveFeatures = active?.config?.capture_features;
    if (liveFeatures && Object.prototype.hasOwnProperty.call(liveFeatures, name)) {
      return Boolean(liveFeatures[name]);
    }
    const input = $(`[data-capture-feature="${name}"]`);
    return input ? Boolean(input.checked) : true;
  }

  function powerFlowPresentation(active) {
    const latest = active?.latest || {};
    const rawDirection = String(
      latest.direction
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
    const direction = charging ? "charging" : discharging ? "discharging" : "unknown";
    const externalPower = rawDirection === "external_power"
      || latest.external_power === true
      || active?.summary?.external_power_observed === true;
    const series = Array.isArray(active?.series) ? active.series : [];
    const observedDirections = new Set(
      series.map(point => String(point?.direction || "").toLowerCase())
        .filter(value => value === "charging" || value === "discharging"),
    );
    const summaryDirection = String(
      active?.summary?.power_flow_direction
      || active?.summary?.direction
      || ""
    ).toLowerCase();
    const sessionDirection = externalPower
      ? "external_power"
      : ["charging", "discharging", "mixed", "idle"].includes(summaryDirection)
      ? summaryDirection
      : observedDirections.size > 1
        ? "mixed"
        : observedDirections.size === 1 ? [...observedDirections][0] : direction;
    const powerSources = Array.from(new Set([
      latest.power_source,
      active?.summary?.observed_power_primary_source,
      ...(Array.isArray(active?.summary?.power_sources) ? active.summary.power_sources : []),
      ...series.map(point => point?.power_source),
    ].map(value => String(value || "")).filter(Boolean)));
    const systemLoad = active?.summary?.system_load_available === true
      || powerSources.includes(iosSystemLoadPowerSource);
    const iosBatteryFlowOnly = activePlatform(active) === "ios" && !systemLoad;
    const currentLabel = externalPower
      ? "外供状态下电池电流"
      : direction === "charging"
      ? "充电电流"
      : direction === "discharging" ? "放电电流" : "电池电流";
    const currentTag = externalPower ? "EXTERNAL" : direction === "charging" ? "INPUT" : "CURRENT";
    const chartCurrentTitle = sessionDirection === "external_power"
      ? "外部供电下电池电流幅值"
      : sessionDirection === "charging"
      ? "充电电流"
      : sessionDirection === "discharging"
        ? "放电电流"
        : sessionDirection === "mixed" ? "电池电流幅值（充放电混合）" : "电池电流幅值";
    const energyAvailable = finite(
      systemLoad
        ? (active?.summary?.system_load_consumption_energy_mwh ?? active?.summary?.energy_mwh)
        : active?.summary?.energy_mwh,
    );
    if (systemLoad) {
      return {
        direction,
        sessionDirection,
        systemLoad: true,
        externalPower,
        powerLabel: "当前整机 SystemLoad 功率",
        powerTag: "SYSTEM",
        currentLabel,
        currentTag,
        energyLabel: energyAvailable ? "累计有效放电能量" : "有效耗电能量",
        chartPowerTitle: "iOS 整机原始 SystemLoad 通道",
        chartPowerLegend: "DiagnosticsService SystemLoad",
        chartCurrentTitle,
        chartCurrentLegend: "Battery current magnitude",
      };
    }
    const currentPowerLabel = iosBatteryFlowOnly
      ? "当前电池流量功率（I×V）"
      : externalPower
      ? "外部供电下电池侧原始流量"
      : direction === "charging"
      ? "当前电池充入功率"
      : direction === "discharging" ? "当前电池放电功率" : "当前电池侧功率";
    const chartPowerTitle = iosBatteryFlowOnly
      ? "iOS 电池流量功率（I×V）"
      : sessionDirection === "external_power"
      ? "外部供电下电池侧原始流量"
      : sessionDirection === "charging"
      ? "电池充入功率"
      : sessionDirection === "discharging"
        ? "电池放电功率"
        : sessionDirection === "mixed" ? "电池侧原始功率（充放电混合）" : "电池侧原始功率";
    return {
      direction,
      sessionDirection,
      systemLoad: false,
      externalPower,
      powerLabel: currentPowerLabel,
      powerTag: iosBatteryFlowOnly ? "BATTERY FLOW" : externalPower ? "EXTERNAL" : direction === "charging" ? "CHARGE" : "BATTERY",
      currentLabel,
      currentTag,
      energyLabel: energyAvailable ? "累计有效放电能量" : "有效耗电能量",
      chartPowerTitle,
      chartPowerLegend: iosBatteryFlowOnly ? "Battery current × voltage" : "Battery-side raw power",
      chartCurrentTitle,
      chartCurrentLegend: "Battery current magnitude",
    };
  }

  function systemLoadPowerSnapshot(active) {
    const summary = active?.summary || {};
    const rows = Array.isArray(active?.series) ? active.series : [];
    const systemLoadRows = rows.filter(row => row?.power_source === iosSystemLoadPowerSource);
    const point = systemLoadRows.at(-1) || null;
    const summaryUsesSystemLoad = String(summary.observed_power_primary_source || "") === iosSystemLoadPowerSource
      || systemLoadRows.length > 0;
    const latestMw = finite(point?.power_mw)
      ? Number(point.power_mw)
      : finite(summary.system_load_latest_power_mw) ? Number(summary.system_load_latest_power_mw) : null;
    const latestElapsed = finite(point?.elapsed_s)
      ? Number(point.elapsed_s)
      : finite(summary.system_load_latest_elapsed_s) ? Number(summary.system_load_latest_elapsed_s) : null;
    const sampleAgeS = point && finite(point.power_sample_age_s)
        ? Number(point.power_sample_age_s) + Math.max(0, Number(active?.elapsed_s || 0) - Number(point.elapsed_s || 0))
        : finite(summary.system_load_latest_sample_age_s) ? Number(summary.system_load_latest_sample_age_s) : null;
    const stale = point?.power_sample_stale === true
      || (finite(sampleAgeS) && Number(sampleAgeS) > iosSystemLoadStaleAfterS);
    return {
      point,
      latestMw,
      latestElapsed,
      sampleAgeS,
      stale,
      observedAverageMw: finite(summary.system_load_observed_average_power_mw)
        ? Number(summary.system_load_observed_average_power_mw)
        : summaryUsesSystemLoad && finite(summary.observed_power_average_mw)
          ? Number(summary.observed_power_average_mw)
          : null,
      consumptionAverageMw: finite(summary.system_load_consumption_average_power_mw)
        ? Number(summary.system_load_consumption_average_power_mw)
        : summaryUsesSystemLoad && finite(summary.average_power_mw)
          ? Number(summary.average_power_mw)
          : null,
      consumptionEnergyMwh: finite(summary.system_load_consumption_energy_mwh)
        ? Number(summary.system_load_consumption_energy_mwh)
        : summaryUsesSystemLoad && finite(summary.energy_mwh) ? Number(summary.energy_mwh) : null,
    };
  }

  function primaryPowerSnapshot(active) {
    const flow = powerFlowPresentation(active);
    const latest = active?.latest || {};
    const summary = active?.summary || {};
    if (flow.systemLoad) return { ...systemLoadPowerSnapshot(active), systemLoad: true };
    return {
      point: latest,
      latestMw: finite(latest.power_mw) ? Number(latest.power_mw) : null,
      latestElapsed: finite(latest.elapsed_s) ? Number(latest.elapsed_s) : null,
      sampleAgeS: finite(latest.power_sample_age_s) ? Number(latest.power_sample_age_s) : null,
      observedAverageMw: finite(summary.observed_power_average_mw)
        ? Number(summary.observed_power_average_mw)
        : null,
      consumptionAverageMw: finite(summary.average_power_mw)
        ? Number(summary.average_power_mw)
        : null,
      consumptionEnergyMwh: finite(summary.energy_mwh) ? Number(summary.energy_mwh) : null,
      systemLoad: false,
    };
  }

  function renderResolutionPresentation(active, performance = {}) {
    const rawSource = String(performance.render_resolution_source || "").trim();
    const evidence = String(performance.render_resolution_evidence || "").trim();
    let source = rawSource;
    if (active?.is_demo) {
      source = rawSource.startsWith("演示数据")
        ? rawSource
        : `演示数据 · 模拟 ${rawSource || "Surface 缓冲区尺寸"}`;
    } else if (rawSource === "SurfaceFlinger GraphicBuffer") {
      source = "SurfaceFlinger · 前台应用渲染层 GraphicBuffer（启动时快照）";
    }
    const boundary = performance.render_resolution_estimated
      ? "这是窗口或显示模式估算值，不等同于应用实际渲染缓冲区。"
      : "这是应用提交给系统合成器的 Surface 缓冲区尺寸；动态分辨率、升采样、裁剪和多 Surface 都可能使它不同于游戏引擎内部渲染分辨率。";
    return {
      source: source || "当前采集链路未提供可验证的应用缓冲区尺寸",
      evidence: evidence ? `命中层：${evidence}` : "",
      boundary,
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
    const iosPerformance = (active?.test_mode || app.testMode) === "performance"
      && activePlatform(active) === "ios";
    $("#metric-one-low-card")?.classList.toggle("hidden", iosPerformance && !available);
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
    const minimum = 0;
    let maximum = Math.max(0, ...selected);
    if (maximum <= minimum) maximum = minimum + 1;
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
        ? ["gpu_load_pct", "power_mw", "temperature_c"]
        : ["frame_rate_fps", "frame_time_ms", "gpu_load_pct", "gpu_frequency_mhz", "memory_frequency_mhz", "temperature_c", "power_mw"]
      : ["power_mw", "current_ma", "voltage_mv", "gpu_load_pct", "gpu_frequency_mhz", "memory_frequency_mhz", "temperature_c"];
    const cards = [];
    keys.forEach(key => {
      if (key === "memory_frequency_mhz" && !captureFeatureEnabled(active, "memory_frequency")) return;
      const definition = metricDefinitions[key];
      if (!definition) return;
      const source = definition.series === "performance" ? active?.performance_series : active?.series;
      const rows = Array.isArray(source) ? source : [];
      const values = rows
        .map(row => key === "power_mw" && powerFlowPresentation(active).systemLoad
          ? row?.power_source === iosSystemLoadPowerSource ? definition.value(row) : null
          : definition.value(row))
        .filter(finite)
        .map(Number);
      if (!values.length) return;
      const current = values.at(-1);
      const average = values.reduce((sum, value) => sum + value, 0) / values.length;
      const secondaryAllowed = key !== "frame_rate_fps" || standardOnePercentLowAvailable(active);
      const secondaryValues = secondaryAllowed && typeof definition.secondaryValue === "function"
        ? (Array.isArray(source) ? source : [])
          .map(definition.secondaryValue)
          .filter(finite)
          .map(Number)
        : [];
      const secondary = secondaryAllowed && typeof definition.secondaryValue === "function"
        ? (secondaryValues.length ? secondaryValues.at(-1) : null)
        : percentile(values, definition.secondaryQuantile ?? .95);
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
      if (key === "power_mw" && powerFlow.systemLoad) {
        const batteryFlowValues = rows
          .map(row => finite(row?.current_ma) && finite(row?.voltage_mv)
            ? Number(row.current_ma) * Number(row.voltage_mv) / 1_000_000
            : null)
          .filter(finite)
          .map(Number);
        if (batteryFlowValues.length) {
          const batteryFlowAverage = batteryFlowValues.reduce((sum, value) => sum + value, 0) / batteryFlowValues.length;
          const batteryFlowP95 = percentile(batteryFlowValues, .95);
          cards.push(`<button type="button" class="live-detail-card" data-live-metric="power_mw" style="--detail-color:#9e8cff">
            <span class="live-detail-copy"><small>电池流量功率（I×V）</small><strong>${batteryFlowValues.at(-1).toFixed(3)} W</strong><span>AVG ${batteryFlowAverage.toFixed(3)} · P95 ${batteryFlowP95 === null ? "--" : batteryFlowP95.toFixed(3)}</span></span>
            ${sparklineMarkup(batteryFlowValues)}
          </button>`);
        }
      }
    });
    container.innerHTML = cards.length
      ? cards.join("")
      : '<div class="empty-row">当前尚无可展开的实时数据；未启用或不可读的项目不会显示。</div>';
    $("#live-detail-count").textContent = cards.length
      ? `${cards.length} 项有效数据`
      : active?.running ? "等待数据" : "本次无有效数据";
  }

  function renderBrightnessThrottling(active) {
    const panel = $("#brightness-dim-panel");
    if (!panel) return;
    const analysis = active?.brightness_throttling || {};
    const platform = activePlatform(active);
    const available = ["android", "ios", "harmony"].includes(platform) && Boolean(analysis.available);
    panel.classList.toggle("hidden", !available);
    if (!available) return;
    const points = Array.isArray(analysis.points) ? analysis.points : [];
    const current = analysis.current_state || {};
    const rawStatus = String(current.status || "none");
    const vendorStateKnown = current.vendor_thermal_active === true || current.vendor_thermal_active === false;
    const vendorActive = current.vendor_thermal_active === true;
    const vendorLastKnown = current.vendor_thermal_last_known_active === true
      || current.vendor_thermal_last_known_active === false;
    const vendorLastKnownActive = current.vendor_thermal_last_known_active === true;
    const vendorLimitDescribesActive = vendorActive
      || (!vendorStateKnown && vendorLastKnownActive);
    const status = vendorStateKnown
      ? vendorActive ? "confirmed" : "none"
      : rawStatus;
    const currentActive = status === "confirmed" || status === "suspected";
    panel.classList.toggle("active", status === "confirmed");
    panel.classList.toggle("suspected", status === "suspected");
    const title = $("#brightness-dim-title");
    const detail = $("#brightness-dim-detail");
    const badge = $("#brightness-dim-badge");
    const source = $("#brightness-dim-source");
    const pointList = $("#brightness-dim-points");
    const parts = [];
    if (finite(current.setting_raw)) {
      parts.push(platform === "ios"
        ? `用户亮度 ${Number(current.setting_raw).toFixed(1)}%`
        : `系统设定 ${Number(current.setting_raw).toFixed(0)}`);
    }
    if (finite(current.effective_raw_estimate)) {
      parts.push(`有效档位约 ${Number(current.effective_raw_estimate).toFixed(0)}`);
    } else if (finite(current.effective_brightness)) {
      parts.push(`有效亮度 ${Number(current.effective_brightness * 100).toFixed(1)}%`);
    }
    if (finite(current.thermal_cap)) parts.push(`热上限 ${Number(current.thermal_cap * 100).toFixed(1)}%`);
    if (vendorLimitDescribesActive && finite(current.vendor_thermal_level)) {
      parts.push(`厂商限亮档 ${Number(current.vendor_thermal_level).toFixed(0)}`);
    }
    if (vendorLimitDescribesActive && finite(current.vendor_thermal_limit_nits)) {
      parts.push(`系统标称上限 ${Number(current.vendor_thermal_limit_nits).toFixed(0)} nit（非亮度计实测）`);
    }
    if (finite(current.vendor_thermal_temperature_c)) parts.push(`厂商温控温度 ${Number(current.vendor_thermal_temperature_c).toFixed(1)} °C`);
    if (platform === "harmony" && finite(current.brightness_discount)) {
      parts.push(`显示折扣 ${Number(current.brightness_discount).toFixed(3)}×`);
    }
    if (finite(current.lcd_backlight_cooling) && Number(current.lcd_backlight_cooling) > 0) {
      parts.push(`LCD 冷却档 ${Number(current.lcd_backlight_cooling).toFixed(0)}`);
    }
    if (platform === "harmony" && finite(current.render_backlight_raw)) {
      parts.push(`RS 背光 ${Number(current.render_backlight_raw).toFixed(0)}`);
    }
    if (platform === "ios" && finite(current.luminance_nits)) {
      parts.push(`实际 ${Number(current.luminance_nits).toFixed(1)} nits`);
    }
    if (platform === "ios" && finite(current.luminance_drop_pct)) {
      parts.push(`较同档基线下降 ${Number(current.luminance_drop_pct).toFixed(1)}%`);
    }
    if (platform === "ios" && finite(current.raw_backlight_raw)) {
      parts.push(`rawBrightness ${Number(current.raw_backlight_raw).toFixed(0)}`);
    }
    if (platform === "ios" && current.thermal_notification_active && current.thermal_notification) {
      parts.push(`热压力通知 ${current.thermal_notification}`);
    }
    if (finite(current.skin_temperature_c)) parts.push(`SKIN ${Number(current.skin_temperature_c).toFixed(1)} °C`);
    if (platform === "ios" && finite(current.battery_temperature_c)) {
      parts.push(`电池 ${Number(current.battery_temperature_c).toFixed(1)} °C`);
    }
    if (currentActive) {
      title.textContent = status === "confirmed" ? "确认发生屏幕热降亮" : "疑似发生屏幕热降亮";
      detail.textContent = `${parts.join(" · ") || "显示侧热限制已触发"}；${current.reason || "请查看时间点证据"}`;
      badge.textContent = status === "confirmed" ? "DIM" : "SUSPECT";
      if (active?.running) {
        const key = `${current.uptime_s || current.elapsed_s}:${status}`;
        if (!app.notifiedBrightnessPoints.has(key)) {
          app.notifiedBrightnessPoints.add(key);
          notify(
            status === "confirmed" ? "检测到屏幕热降亮" : "检测到疑似屏幕热降亮",
            `${formatDuration(current.elapsed_s)} · ${parts.join(" · ") || current.reason || "显示侧热限制"}`,
            "error",
            9000,
          );
        }
      }
    } else if (vendorLastKnown) {
      const age = finite(current.vendor_thermal_observed_age_s)
        ? `${Number(current.vendor_thermal_observed_age_s).toFixed(1)} 秒前`
        : "此前";
      title.textContent = vendorLastKnownActive
        ? "厂商限亮最近一次为生效，当前尚未刷新"
        : "厂商限亮最近一次未生效";
      detail.textContent = `${parts.join(" · ") || "厂商运行时状态"}；状态读取于 ${age}${current.vendor_thermal_state_stale ? "，已超过刷新有效期，不能代表当前状态" : "，等待下一次低频刷新"}。`;
      badge.textContent = current.vendor_thermal_state_stale ? "STALE" : "LAST";
    } else if (vendorStateKnown && !vendorActive) {
      const candidateSource = analysis.vendor_thermal_candidate_caps_nits
        || current.vendor_thermal_candidate_caps_nits;
      const candidateCount = Array.isArray(candidateSource)
        ? candidateSource.length
        : candidateSource && typeof candidateSource === "object"
          ? Object.keys(candidateSource).length
          : 0;
      title.textContent = candidateCount
        ? "已识别厂商限亮候选表，当前未生效"
        : "厂商温控限亮当前未生效";
      const candidateText = candidateCount
        ? `已读取 ${candidateCount} 个固件候选上限，但候选表不能证明哪些档位会在实际温控中触发。`
        : "未读取到可确认的生效档位。";
      detail.textContent = `${parts.length ? `${parts.join(" · ")}；` : ""}运行时 active=false，当前没有生效档位；${candidateText}只有 active=true 时才把运行时档位和标称 nit 作为限亮证据，物理亮度仍需亮度计验证。`;
      badge.textContent = "INACTIVE";
    } else if (points.length) {
      title.textContent = `当前已恢复，历史标记 ${points.length} 个降亮点`;
      detail.textContent = parts.length
        ? parts.join(" · ")
        : platform === "ios"
          ? "当前 AppleARMBacklight 未继续显示同档位实际毫尼特下降。"
          : "当前 DisplayManager 和 lcd-backlight 未继续限制亮度。";
      badge.textContent = "HISTORY";
    } else {
      title.textContent = "未检测到屏幕热降亮";
      detail.textContent = parts.length ? parts.join(" · ") : "系统亮度与显示侧热限制状态正常。";
      badge.textContent = "CLEAR";
    }
    source.textContent = platform === "harmony"
      ? `${points.length} points · DisplayPowerManager / ThermalService`
      : platform === "ios"
        ? `${points.length} points · AppleARMBacklight / iOS thermal pressure`
        : `${points.length} 个确认/疑似点 · DisplayManager / Thermal HAL${vendorStateKnown || vendorLastKnown ? " / Oplus runtime" : ""}`;
    pointList.innerHTML = points.length
      ? points.slice(-40).map(point => {
        const pointVendorKnown = point.vendor_thermal_active === true || point.vendor_thermal_active === false;
        const pointConfirmed = pointVendorKnown ? point.vendor_thermal_active === true : point.status === "confirmed";
        return `<span class="${escapeHtml(pointConfirmed ? "confirmed" : "suspected")}" title="${escapeHtml(point.reason || "显示侧热限制")}">${formatDuration(point.elapsed_s)} · ${pointConfirmed ? "确认" : "疑似"}</span>`;
      }).join("")
      : "<span>尚无疑似降亮点</span>";
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

  function recordUiError(title, detail = "") {
    const now = Date.now() / 1000;
    const normalizedTitle = String(title || "未知错误").trim();
    const normalizedDetail = String(detail || "").trim();
    const signature = `${normalizedTitle}\u0000${normalizedDetail}`;
    const previous = app.uiLogs.at(-1);
    if (
      previous?.signature === signature
      && now - Number(previous.time || 0) < uiErrorDedupWindowS
    ) return;
    app.uiLogs.push({
      time: now,
      source: "ui",
      type: "error",
      line: normalizedDetail ? `${normalizedTitle}：${normalizedDetail}` : normalizedTitle,
      signature,
    });
    if (app.uiLogs.length > maxUiLogLines) {
      app.uiLogs.splice(0, app.uiLogs.length - maxUiLogLines);
    }
    renderConsole(app.state?.active);
  }

  function notify(title, detail = "", type = "success", timeout = 5000) {
    if (type === "error") recordUiError(title, detail);
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

  function placeTargetPackageField() {
    const field = $(".target-app-field");
    const performance = app.testMode === "performance";
    const scannerOnHome = performance && selectedPlatform() === "android";
    const destination = performance
      ? $("#home-package-slot")
      : $("#config-package-slot");
    if (field && destination && field.parentElement !== destination) {
      destination.appendChild(field);
    }
    const appPicker = $(".app-picker");
    const appPickerDestination = scannerOnHome
      ? field
      : $("#config-app-picker-content");
    if (appPicker && appPickerDestination && appPicker.parentElement !== appPickerDestination) {
      if (scannerOnHome) {
        appPickerDestination.insertBefore(appPicker, $("#package-input-hint"));
      } else {
        appPickerDestination.appendChild(appPicker);
      }
    }
    $("#config-app-column")?.classList.toggle("scanner-on-home", scannerOnHome);
    $("#config-view-columns")?.classList.toggle("scanner-on-home", scannerOnHome);
  }

  function mountConfigurationView() {
    const target = $("#config-view-content");
    const formTarget = $("#config-form-column");
    const controlPanel = $(".control-panel");
    const runtimeLayout = $(".runtime-layout");
    const durationField = $("#duration-field");
    const durationPresets = $("#duration-presets");
    const durationSlot = $("#home-duration-slot");
    const startButton = $("#start-record");
    const startSlot = $("#home-start-slot");
    if (
      !target || !formTarget || !controlPanel || !runtimeLayout
      || !durationField || !durationPresets || !durationSlot
      || !startButton || !startSlot
    ) return;
    durationSlot.append(durationField, durationPresets);
    startButton.setAttribute("form", "record-form");
    startSlot.append(startButton);
    formTarget.append(controlPanel);
    placeTargetPackageField();
    runtimeLayout.classList.add("monitoring-only");
  }

  function switchView(view) {
    const legacyTools = view === "tools";
    const legacySystem = view === "system" || view === "thermal";
    const requested = legacySystem
      ? "live"
      : legacyTools ? "history" : view;
    const target = ["live", "config", "agent", "device", "history"].includes(requested)
      ? requested
      : "live";
    app.activeView = target;
    document.body.dataset.view = target;
    if (legacyTools || legacySystem) {
      window.history.replaceState(null, "", `#${target}`);
    } else if (location.hash !== `#${target}`) {
      location.hash = target;
    }
    if (legacyTools) {
      const historyTools = $("#history-tools-details");
      if (historyTools) historyTools.open = true;
    }
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
      config: "测试配置",
      agent: "AI 自动化",
      device: "设备能力",
      history: "历史报告与交付",
    }[target];
    if (target === "live") {
      requestAnimationFrame(() => {
        renderChart();
      });
    }
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
    const resultDetails = $("#app-result-details");
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
    }
    if (resultDetails) {
      resultDetails.classList.add("hidden");
      resultDetails.open = false;
    }
    if ($("#app-result-summary")) $("#app-result-summary").textContent = "展开查看应用列表";
    if ($("#app-picker-status")) $("#app-picker-status").textContent = "尚未扫描";
  }

  function renderAppOptions() {
    const select = $("#app-select");
    const search = $("#app-search-input");
    const resultDetails = $("#app-result-details");
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
    placeholder.textContent = filtered.length ? "请选择设备应用" : query ? "没有匹配的应用" : "未发现应用";
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
    resultDetails?.classList.toggle("hidden", !app.scannedAppsDevice);
    resultList.innerHTML = shown.length ? shown.map(item => {
      const packageName = String(item.package || "");
      const activity = String(item.activity || item.component || "").split(".").at(-1) || "应用";
      const initials = packageName.split(".").filter(Boolean).at(-1)?.slice(0, 2).toUpperCase() || "APP";
      const icon = typeof item.icon_data_uri === "string" && item.icon_data_uri.startsWith("data:image/")
        ? `<img src="${escapeHtml(item.icon_data_uri)}" alt="" loading="lazy">`
        : escapeHtml(initials);
      return `<button type="button" class="app-option ${packageName === packageValue ? "selected" : ""}" data-app-package="${escapeHtml(packageName)}" role="option" aria-selected="${packageName === packageValue ? "true" : "false"}" title="${escapeHtml(packageName)}">
        <span class="app-thumb">${icon}</span>
        <span class="app-option-copy"><strong>${escapeHtml(activity)}</strong><small title="${escapeHtml(packageName)}">${escapeHtml(packageName)}</small></span>
        <span class="app-option-badge">${item.user_app ? "USER" : "SYSTEM"}</span>
      </button>`;
    }).join("") : '<div class="empty-row">没有匹配的应用</div>';
    const noun = app.scannedAppsSource === "third-party-packages-fallback" ? "第三方包" : "可启动应用";
    $("#app-picker-status").textContent = query
      ? `匹配 ${filtered.length} / ${apps.length} 个${noun}`
      : `已扫描 ${apps.length} 个${noun}`;
    if ($("#app-result-summary")) {
      $("#app-result-summary").textContent = `${filtered.length} / ${apps.length} 个应用`;
    }
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
    if (devicePlatform(device) === "ios") {
      if (deviceConnectionType(device) === "usb" && iosRemoteXpcReady(device)) {
        return iosEndpointScope(device) === "link-local"
          ? "USB 有线（RemoteXPC 经链路本地网络）"
          : "USB 有线（RemotePairing 已建立，待拔线验证）";
      }
      if (deviceConnectionType(device) === "wireless") {
        return iosEndpointScope(device) === "link-local"
          ? "USB-NCM 链路本地 RemotePairing（不可作为断电证明）"
          : iosWirelessTransportLabel(device);
      }
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
        ["wireless", platform === "ios" ? "无线设备（Wi-Fi / 蓝牙 PAN）" : "无线设备（Wi-Fi）"],
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
          const connection = platform === "ios" ? ` · ${deviceConnectionLabel(device)}` : "";
          option.textContent = `${name || platformLabel(platform) + " Device"}${connection} · ${device.serial} · ${device.state}`;
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
    const recordingReadiness = recordingDeviceReadiness(chosen);
    const iosPairingNeeded = chosenPlatform === "ios"
      && chosen?.state === "device"
      && !iosRemoteXpcReady(chosen);
    const iosRemoteReady = chosenPlatform === "ios" && iosRemoteXpcReady(chosen);
    const iosUnpluggedReady = chosenPlatform === "ios" && iosUnplugReady(chosen);
    const deviceState = $("#device-state");
    deviceState.classList.toggle("online", Boolean(chosen?.state === "device"));
    deviceState.classList.toggle("error", Boolean(platformError || (chosen && chosen.state !== "device")));
    deviceState.title = platformError || (!recordingReadiness.ready ? recordingReadiness.reason : "");
    deviceState.querySelector("span").textContent = iosPairingNeeded
      ? "iOS · USB 在线（待 RemotePairing）"
      : iosRemoteReady && !iosUnpluggedReady && deviceConnectionType(chosen) === "usb"
        ? iosEndpointScope(chosen) === "link-local"
          ? "iOS · RemoteXPC 经 USB 网络在线（拔线不可保证）"
          : "iOS · RemotePairing 在线（请拔线刷新验证）"
        : chosen?.state === "device" ? `${platformLabel(chosenPlatform)} · ${deviceConnectionLabel(chosen)}在线` : platformError ? `${platformLabel(platform)} 连接异常` : chosen ? `${deviceConnectionLabel(chosen)} · ${chosen.state}` : "等待设备";
    updateStartControlState(state.active);
    $("#enable-tcpip").disabled = !chosen || chosenPlatform !== "android" || chosen.state !== "device" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#enable-harmony-tcpip").disabled = !chosen || chosenPlatform !== "harmony" || chosen.state !== "device" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#pair-ios").disabled = !chosen || chosenPlatform !== "ios" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#connect-ios-bluetooth").disabled = chosenPlatform !== "ios" || Boolean(state.active?.running);
    $("#disconnect-wireless").disabled = !chosen || chosenPlatform !== "android" || chosenType !== "wireless" || Boolean(state.active?.running);
    updateAppScannerAvailability(chosen);
    updateCaptureFeatureControls();
    renderBrightnessControl(chosen);
    if (
      brightnessCapableDevice(chosen)
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
    const primaryPower = primaryPowerSnapshot(active);
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
    $("#metric-energy-tag").textContent = finite(primaryPower.consumptionEnergyMwh) ? "ENERGY" : "N/A";
    const powerTab = $('[data-metric="power_mw"][data-mode-only="power"]');
    const currentTab = $('[data-metric="current_ma"][data-mode-only="power"]');
    if (powerTab) powerTab.textContent = powerFlow.systemLoad
      ? "SystemLoad 功率"
      : isIos ? "电池流量 I×V"
      : powerFlow.sessionDirection === "charging"
        ? "充入功率"
        : powerFlow.sessionDirection === "discharging" ? "放电功率" : "原始功率";
    if (currentTab) currentTab.textContent = powerFlow.sessionDirection === "charging"
      ? "充电电流"
      : powerFlow.sessionDirection === "discharging" ? "放电电流" : "电池电流";
    $("#metric-power").textContent = finite(primaryPower.latestMw) ? `${(Number(primaryPower.latestMw) / 1000).toFixed(3)} W` : "--";
    $("#metric-current").textContent = formatMetric(latest.current_ma, "mA", 0);
    $("#metric-cpu").textContent = formatMetric(latest.cpu_pct, "%", 1);
    $("#metric-memory").textContent = isIos
      ? formatMetric(latest.collector_cpu_pct, "%", 2)
      : formatMetric(latest.memory_frequency_mhz, "MHz", 0);
    $("#metric-temp").textContent = formatMetric(latest.temperature_c, "°C", 1);
    $("#metric-energy").textContent = formatMetric(primaryPower.consumptionEnergyMwh, "mWh", 2);
    const averagePower = finite(primaryPower.consumptionAverageMw)
      ? `有效放电均值 ${(Number(primaryPower.consumptionAverageMw) / 1000).toFixed(3)} W`
      : finite(primaryPower.observedAverageMw)
        ? `${powerFlow.systemLoad ? "SystemLoad 原始均值" : "电池流量均值"} ${(Number(primaryPower.observedAverageMw) / 1000).toFixed(3)} W · 不作耗电结论`
        : "尚无有效功率均值";
    $("#metric-power-sub").textContent = powerFlow.systemLoad
      ? `${averagePower}${finite(primaryPower.sampleAgeS) ? ` · age ${Number(primaryPower.sampleAgeS).toFixed(1)} s` : ""}`
      : powerFlow.externalPower
        ? `${averagePower} · external_power=true，仅原始流量`
      : powerFlow.direction === "charging"
        ? `${averagePower} · 当前样本为电池流入方向`
        : isIos && finite(primaryPower.sampleAgeS)
          ? `${averagePower} · age ${Number(primaryPower.sampleAgeS).toFixed(1)} s`
          : averagePower;
    $("#metric-current-sub").textContent = finite(summary.average_current_ma)
      ? `AVG ${Number(summary.average_current_ma).toFixed(0)} mA${powerFlow.direction === "charging" ? " · 流入电池" : ""}`
      : powerFlow.direction === "charging" ? "流入电池的正幅值" : "正幅值";
    const averageCpu = finite(summary.average_cpu_pct) ? `AVG ${Number(summary.average_cpu_pct).toFixed(1)}%` : "全核心";
    $("#metric-cpu-sub").textContent = isIos && finite(summary.average_collector_cpu_pct)
      ? `${averageCpu} · 观察者相关上界 ${Number(summary.average_collector_cpu_pct).toFixed(1)}%`
      : averageCpu;
    const memory = active?.memory || {};
    $("#metric-memory-sub").textContent = isIos
      ? finite(summary.average_collector_cpu_pct)
        ? `AVG ${Number(summary.average_collector_cpu_pct).toFixed(2)}%`
        : "观察者相关进程 CPU 上界"
      : finite(memory.p95_frequency_mhz)
        ? `P95 ${Number(memory.p95_frequency_mhz).toFixed(0)} MHz · 高频 ${formatNumber(memory.high_frequency_share_pct, 1)}%`
        : memory.limitations || "DRAM / DMC / MIF";
    $("#metric-temp-sub").textContent = finite(latest.voltage_mv) ? `${(Number(latest.voltage_mv) / 1000).toFixed(3)} V` : isIos ? "DiagnosticsService" : "BatteryService";
    $("#metric-energy-sub").textContent = finite(primaryPower.consumptionEnergyMwh)
      ? `${active?.sample_count || 0} samples · 仅有效放电区间积分`
      : "未形成有效放电区间，不显示 0 W / 0 mWh 结论";
    const chargingNotice = $("#charging-power-notice");
    const showChargingNotice = (powerFlow.direction === "charging" || powerFlow.externalPower) && finite(primaryPower.latestMw);
    chargingNotice?.classList.toggle("hidden", !showChargingNotice);
    if (showChargingNotice) {
      const watts = Number(primaryPower.latestMw) / 1000;
      const amps = finite(latest.current_ma) ? Number(latest.current_ma) / 1000 : null;
      const volts = finite(latest.voltage_mv) ? Number(latest.voltage_mv) / 1000 : null;
      if (powerFlow.systemLoad) {
        $("#charging-power-title").textContent = `当前有外部供电：SystemLoad ${watts.toFixed(2)} W`;
        $("#charging-power-detail").textContent = amps !== null && volts !== null
          ? `SystemLoad 是 DiagnosticsService 的整机原始通道，外供时可能接近 SystemPowerIn；电池流量另为 ${amps.toFixed(2)} A × ${volts.toFixed(2)} V。两者不能互相改名，也都不是独立电源轨实测。`
          : "SystemLoad 是 DiagnosticsService 的整机原始通道，不是“电池充入功率”或独立电源轨实测；当前外部供电区间不进入耗电、能量或续航结论。";
      } else if (powerFlow.externalPower) {
        $("#charging-power-title").textContent = `当前为外部供电：电池侧原始流量 ${watts.toFixed(2)} W`;
        $("#charging-power-detail").textContent = amps !== null && volts !== null
          ? `${amps.toFixed(2)} A × ${volts.toFixed(2)} V。即使电流符号为负，external_power=true 时也不能把该区间当作有效电池放电或设备耗电。`
          : "external_power=true；这里只保留原始流量，不生成平均耗电、能量或续航结论。";
      } else {
        $("#charging-power-title").textContent = `当前为充电状态：电池流量 ${watts.toFixed(2)} W`;
        $("#charging-power-detail").textContent = amps !== null && volts !== null
          ? `${amps.toFixed(2)} A × ${volts.toFixed(2)} V。该区间只保留原始流量，不进入设备耗电结论；续航测试请断开外部供电。`
          : "该数值是电池侧原始流量，不等于设备耗电；续航测试请断开外部供电。";
      }
    }
  }

  function renderPerformanceMetrics(active) {
    const performance = active?.performance || {};
    if (activePlatform(active) === "ios") {
      $("#live-resolution-note")?.classList.add("hidden");
      const latest = active?.latest || {};
      const summary = active?.summary || {};
      const primaryPower = primaryPowerSnapshot(active);
      const powerFlow = powerFlowPresentation(active);
      const context = active?.context || {};
      $("#metric-fps").textContent = formatMetric(latest.cpu_pct, "%", 1);
      $("#metric-fps-sub").textContent = "DVT 进程 CPU / 逻辑核心归一化";
      $("#metric-one-low").textContent = formatMetric(latest.gpu_load_pct, "%", 1);
      $("#metric-one-low-sub").textContent = "DVT GPU utilization";
      const overhead = finite(latest.collector_cpu_pct)
        ? latest.collector_cpu_pct
        : summary.average_collector_cpu_pct;
      $("#metric-frame-p99").textContent = formatMetric(overhead, "%", 2);
      $("#metric-frame-p99-sub").textContent = "sysmond / DTServiceHub / pairing daemon 同期 CPU 上界";
      $("#metric-frame-issue").textContent = finite(primaryPower.latestMw)
        ? `${(Number(primaryPower.latestMw) / 1000).toFixed(3)} W`
        : "--";
      $("#metric-frame-issue-label").textContent = powerFlow.systemLoad
        ? "当前整机原始 SystemLoad"
        : "当前电池流量 I×V";
      $("#metric-frame-issue-tag").textContent = powerFlow.systemLoad ? "SYSTEM" : "BATTERY";
      $("#metric-frame-issue-sub").textContent = powerFlow.systemLoad
        ? primaryPower.stale
          ? `DiagnosticsService SystemLoad 已超过 ${iosSystemLoadStaleAfterS} 秒未刷新，仅保留原始展示`
          : finite(primaryPower.sampleAgeS)
            ? `DiagnosticsService SystemLoad · age ${Number(primaryPower.sampleAgeS).toFixed(1)} s`
          : "DiagnosticsService SystemLoad"
        : "电池电流幅值 × 电池电压";
      $("#metric-render-resolution").textContent = context.foreground_package
        || "未确认";
      $("#metric-render-resolution-sub").textContent = context.foreground_activity
        || performance.foreground_state_reason
        || "仅使用测试期间实际收到的 DVT application-state 变化";
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
    const fps = finite(performance.sampled_frame_rate_fps)
      ? Number(performance.sampled_frame_rate_fps)
      : finite(performance.sampled_compositor_fps)
        ? Number(performance.sampled_compositor_fps)
        : null;
    const oneLow = standardOnePercentLowAvailable(active) && finite(performance.one_percent_low_fps)
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
    $("#metric-fps-sub").textContent = fps === null
      ? performance.frame_unavailable_reason || (active?.running ? "等待前台窗口帧计数" : "本次没有有效帧数据")
      : finite(performance.current_refresh_rate_hz)
        ? "显示 " + Number(performance.current_refresh_rate_hz).toFixed(0) + " Hz · " + Number(performance.frame_sample_count || 0).toLocaleString("zh-CN") + " 帧"
        : `${Number(performance.frame_sample_count || 0).toLocaleString("zh-CN")} 帧`;
    $("#metric-one-low").textContent = oneLow === null ? "--" : oneLow.toFixed(1) + " FPS";
    $("#metric-one-low-sub").textContent = oneLow === null
      ? "只有逐帧间隔或详细帧直方图时才生成标准 1% Low"
      : performance.one_percent_low_source || "最慢 1% 帧换算";
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
    const renderPresentation = renderResolutionPresentation(active, performance);
    $("#metric-render-resolution-card")?.classList.toggle("hidden", !renderAvailable);
    $("#performance-render-evidence")?.classList.toggle("hidden", !renderAvailable);
    $("#live-resolution-note")?.classList.toggle("hidden", !renderAvailable);
    $("#metric-render-resolution").textContent = finite(renderWidth) && finite(renderHeight)
      ? Number(renderWidth).toFixed(0) + " × " + Number(renderHeight).toFixed(0)
      : "--";
    $("#metric-render-resolution-sub").textContent = renderAvailable
      ? `来源：${renderPresentation.source}`
      : "当前采集链路未提供可验证的应用缓冲区尺寸";
    $("#live-resolution-value").textContent = renderAvailable
      ? `${Number(renderWidth).toFixed(0)} × ${Number(renderHeight).toFixed(0)}`
      : "--";
    $("#live-resolution-source").textContent = `数据来源：${renderPresentation.source}${renderPresentation.evidence ? ` · ${renderPresentation.evidence}` : ""}`;
    $("#live-resolution-boundary").textContent = renderPresentation.boundary;

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
      const primaryPower = primaryPowerSnapshot(active);
      const powerFlow = powerFlowPresentation(active);
      const processSnapshotsEnabled = captureFeatureEnabled(active, "process_snapshots");
      const context = active?.context || {};
      const monitor = active?.system_monitor || {};
      const gpuObserved = finite(latest.gpu_load_pct)
        || (Array.isArray(active?.series) && active.series.some(row => finite(row?.gpu_load_pct)));
      $("#resource-gpu").textContent = finite(latest.gpu_load_pct)
        ? `${Number(latest.gpu_load_pct).toFixed(1)}%`
        : "--";
      $("#resource-thermal").textContent = finite(latest.temperature_c)
        ? `${Number(latest.temperature_c).toFixed(1)} °C`
        : "--";
      $("#resource-window").textContent = context.foreground_package
        || "--";
      $("#resource-ios-overhead").textContent = finite(latest.collector_cpu_pct)
        ? `${Number(latest.collector_cpu_pct).toFixed(2)}%`
        : finite(active?.summary?.average_collector_cpu_pct)
          ? `${Number(active.summary.average_collector_cpu_pct).toFixed(2)}% AVG`
          : "--";
      $("#resource-ios-power-age")?.closest("div")?.classList.toggle("hidden", !powerFlow.systemLoad);
      $("#resource-ios-power-age").textContent = powerFlow.systemLoad && finite(primaryPower.sampleAgeS)
        ? `${Number(primaryPower.sampleAgeS).toFixed(1)} s`
        : "--";
      $("#resource-whole-power").textContent = finite(primaryPower.latestMw)
        ? `${(Number(primaryPower.latestMw) / 1000).toFixed(3)} W · ${powerFlow.systemLoad ? "SystemLoad 当前样本" : "电池流量 I×V"}`
        : "--";
      $("#resource-snapshot-source").textContent = Number(monitor.system_snapshot_count || 0)
        ? `${Number(monitor.system_snapshot_count)} DVT snapshots`
        : "DVT sysmond";
      $("#performance-frame-source").textContent = gpuObserved
        ? "iOS DVT sysmond / GPU counters"
        : "iOS DVT sysmond";
      $("#performance-frame-confidence").textContent = gpuObserved
        ? "CPU、GPU 和进程资源为诊断遥测，不包含通用应用 FPS"
        : "本次只收到 CPU / 进程诊断遥测；未收到 GPU 数据，也不包含通用应用 FPS";
      $("#performance-evidence-label-2").textContent = powerFlow.systemLoad
        ? "SystemLoad 通道来源"
        : "电池流量来源";
      $("#performance-render-source").textContent = powerFlow.systemLoad
        ? "DiagnosticsService PowerTelemetryData.SystemLoad"
        : "电池电流幅值 × 电池电压";
      $("#performance-render-scale").textContent = powerFlow.systemLoad
        ? primaryPower.stale
          ? `SystemLoad 已超过 ${iosSystemLoadStaleAfterS} 秒未刷新；曲线断开且不进入耗电积分`
          : finite(primaryPower.sampleAgeS)
            ? `SystemLoad 样本年龄 ${Number(primaryPower.sampleAgeS).toFixed(1)} s`
          : "DiagnosticsService 的整机原始 SystemLoad 通道通常约 20 秒更新一次"
        : "当前未收到 SystemLoad；只展示电池端流量，不能冒充整机原始 SystemLoad 通道。";
      $("#performance-interpolation-source").textContent = "观察者相关进程 CPU（上界）";
      $("#performance-interpolation-evidence").textContent = finite(latest.collector_cpu_pct)
        ? `当前 sysmond、DTServiceHub 与配对服务同期归一化 CPU 合计 ${Number(latest.collector_cpu_pct).toFixed(2)}%；包含本底活动，不能当作工具净增量`
        : "报告保留观察者相关进程 CPU 作为干扰上界，不推断净增量";
      return;
    }
    const platform = activePlatform(active);
    const performance = active?.performance || {};
    const context = active?.context || {};
    const monitor = active?.system_monitor || {};

    const latest = active?.latest || {};
    const gpuParts = [];
    if (finite(latest.gpu_load_pct)) gpuParts.push(Number(latest.gpu_load_pct).toFixed(1) + "%");
    if (finite(latest.gpu_frequency_mhz)) gpuParts.push(Number(latest.gpu_frequency_mhz).toFixed(0) + " MHz");
    $("#resource-gpu").textContent = gpuParts.join(" / ") || "--";
    $("#resource-memory").textContent = finite(latest.memory_frequency_mhz)
      ? Number(latest.memory_frequency_mhz).toFixed(0) + " MHz"
      : "--";
    const thermal = monitor.thermal || {};
    const brightnessDim = active?.brightness_throttling || {};
    const brightnessState = brightnessDim.current_state || {};
    const brightnessVendorKnown = brightnessState.vendor_thermal_active === true || brightnessState.vendor_thermal_active === false;
    const brightnessStatus = brightnessVendorKnown
      ? brightnessState.vendor_thermal_active === true ? "confirmed" : "none"
      : String(brightnessState.status || "none");
    $("#resource-thermal").textContent = brightnessStatus === "confirmed"
      ? platform === "harmony" && finite(brightnessState.brightness_discount)
        ? `热降亮 · 折扣 ${Number(brightnessState.brightness_discount).toFixed(3)}×`
        : finite(brightnessState.vendor_thermal_limit_nits)
          ? `热降亮 · 系统标称上限 ${Number(brightnessState.vendor_thermal_limit_nits).toFixed(0)} nit（非实测）`
          : `热降亮 · 上限 ${finite(brightnessState.thermal_cap) ? Number(brightnessState.thermal_cap * 100).toFixed(1) + "%" : "已限制"}`
      : brightnessStatus === "suspected"
        ? "疑似热降亮"
        : finite(thermal.status)
          ? (thermalStatusDefinitions[Number(thermal.status)] || "状态 " + String(thermal.status))
          : "--";
    $("#resource-window").textContent = performance.foreground_window_name || context.foreground_activity || "--";

    const cadenceParts = [];
    const resourceScreenState = String(context.screen_state || "").trim().toLowerCase();
    const harmonyDisplayInactive = platform === "harmony" && resourceScreenState
      && !["awake", "on"].includes(resourceScreenState);
    if (finite(performance.current_refresh_rate_hz)) {
      cadenceParts.push(harmonyDisplayInactive
        ? `${Number(performance.current_refresh_rate_hz).toFixed(0)} Hz（熄屏保留配置，非当前输出）`
        : Number(performance.current_refresh_rate_hz).toFixed(0) + " Hz");
    }
    if (finite(performance.sampled_frame_rate_fps)) cadenceParts.push(Number(performance.sampled_frame_rate_fps).toFixed(1) + " FPS");
    if (finite(performance.cadence_multiplier)) cadenceParts.push("约 " + Number(performance.cadence_multiplier).toFixed(0) + "×");
    $("#resource-cadence").textContent = cadenceParts.join(" / ") || "--";
    $("#resource-whole-power").textContent = finite(latest.power_mw)
      ? (Number(latest.power_mw) / 1000).toFixed(3) + " W · 当前样本"
      : "--";
    const captureBackend = String(
      active?.capture_configuration?.backend
      || active?.metadata?.capture_configuration?.backend
      || active?.config?.capture_configuration?.backend
      || ""
    );
    $("#resource-snapshot-source").textContent = platform === "harmony"
      ? captureBackend === "harmony_smartperf"
        ? "SmartPerf / ThermalService"
        : "HDC / ThermalService"
      : "sampler / thermal";

    $("#performance-frame-source").textContent = performance.frame_source || "平台窗口 / 合成器计数";
    $("#performance-frame-confidence").textContent = performance.one_percent_low_confidence
      ? "1% Low 置信度：" + performance.one_percent_low_confidence
      : performance.frame_unavailable_reason || (active?.running ? "等待有效帧计数" : "本次没有有效帧计数");
    const renderPresentation = renderResolutionPresentation(active, performance);
    $("#performance-render-source").textContent = `数据来源：${renderPresentation.source}`;
    $("#performance-render-scale").textContent = finite(performance.render_scale_pct)
      ? `缓冲区 / 当前显示模式 ${Number(performance.render_scale_pct).toFixed(1)}% · ${renderPresentation.evidence || renderPresentation.boundary}`
      : renderPresentation.evidence || renderPresentation.boundary;
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
      const primaryPower = primaryPowerSnapshot(active);
      const powerFlow = powerFlowPresentation(active);
      const processes = Array.isArray(active?.system_monitor?.processes)
        ? active.system_monitor.processes
        : [];
      const processSnapshotsEnabled = captureFeatureEnabled(active, "process_snapshots");
      const physicalRows = powerFlow.systemLoad
        ? [
            ["当前整机原始 SystemLoad", finite(primaryPower.latestMw) ? `${(Number(primaryPower.latestMw) / 1000).toFixed(3)} W` : "--", "DiagnosticsService SystemLoad"],
            ["有效放电区间 SystemLoad 均值", finite(primaryPower.consumptionAverageMw) ? `${(Number(primaryPower.consumptionAverageMw) / 1000).toFixed(3)} W` : "--", finite(primaryPower.consumptionAverageMw) ? "只使用 SystemLoad 样本" : "外部供电/充电时不生成 0 W 结论"],
            ["原始 SystemLoad 均值", finite(primaryPower.observedAverageMw) ? `${(Number(primaryPower.observedAverageMw) / 1000).toFixed(3)} W` : "--", "与电池 I×V 分域，不是独立电源轨实测"],
            ["电池流量 I×V 均值", finite(summary.battery_flow_average_power_mw) ? `${(Number(summary.battery_flow_average_power_mw) / 1000).toFixed(3)} W` : "--", "电池电流幅值 × 电池电压"],
            ["SystemLoad 样本年龄", finite(primaryPower.sampleAgeS) ? `${Number(primaryPower.sampleAgeS).toFixed(1)} s` : "--", primaryPower.stale ? "已过期，仅原始展示，不进入耗电积分" : "通常约 20 秒刷新"],
          ]
        : [
            ["当前电池流量 I×V", finite(primaryPower.latestMw) ? `${(Number(primaryPower.latestMw) / 1000).toFixed(3)} W` : "--", "ios_battery_current_voltage"],
            ["有效放电区间电池流量均值", finite(primaryPower.consumptionAverageMw) ? `${(Number(primaryPower.consumptionAverageMw) / 1000).toFixed(3)} W` : "--", "只表示电池端流量"],
            ["原始电池流量 I×V 均值", finite(summary.battery_flow_average_power_mw) ? `${(Number(summary.battery_flow_average_power_mw) / 1000).toFixed(3)} W` : "--", "电池电流幅值 × 电池电压"],
          ];
      $("#power-pressure-note").innerHTML = powerFlow.systemLoad
        ? "<strong>通道边界：</strong>SystemLoad 与电池 I×V 分域展示；外供时 SystemLoad 可能接近 SystemPowerIn，但两者都不是独立电源轨实测。"
        : "<strong>当前能力：</strong>尚未收到 DiagnosticsService SystemLoad；这里只展示电池 I×V 流量，不把它改名为整机 SystemLoad。";
      $("#power-pressure-driver-list").innerHTML = physicalRows.map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item[0])}</strong><small>${escapeHtml(item[2])}</small></div><b>${escapeHtml(item[1])}</b></div>`).join("");
      $("#power-pressure-task-list").innerHTML = processes.length
        ? processes.slice(0, 7).map(item => `<div class="pressure-row compact"><div><strong>${escapeHtml(item.name || item.command || "process")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${formatBytes(item.resident_bytes)}</small></div><b>${formatNumber(item.cpu_pct, 1)}%</b></div>`).join("")
        : `<div class="empty-row">${processSnapshotsEnabled ? active?.running ? "等待 DVT 进程快照" : "本次没有 DVT 进程快照" : "进程快照未启用"}</div>`;
      const overhead = finite(latest.collector_cpu_pct)
        ? Number(latest.collector_cpu_pct)
        : finite(summary.average_collector_cpu_pct) ? Number(summary.average_collector_cpu_pct) : null;
      $("#power-pressure-setting-list").innerHTML = [
        ["collector_cpu_pct", overhead === null ? "--" : `${overhead.toFixed(2)}%`, "sysmond / DTServiceHub / pairing daemon"],
        ["GPU utilization", finite(latest.gpu_load_pct) ? `${Number(latest.gpu_load_pct).toFixed(1)}%` : "--", "DVT 诊断遥测"],
        ["相对 powerScore", "仅旁证", "不转换为 mW 或应用功耗"],
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
    $("#power-pressure-note").innerHTML = powerFlow.externalPower
      ? powerFlow.systemLoad
        ? "<strong>当前为外部供电：</strong>iOS SystemLoad 是 DiagnosticsService 的整机原始通道，外供时可能接近 SystemPowerIn；电池 I×V 是另一条电池流量通道。两者都不是独立电源轨实测，也不用于本次续航归因。"
        : "<strong>当前为外部供电：</strong>即使电流符号为负，该区间也只保留电池侧原始流量，不进入平均耗电、能量、续航或任务归因。"
      : powerFlow.direction === "charging"
      ? powerFlow.systemLoad
        ? "<strong>当前为外部供电：</strong>iOS SystemLoad 是 DiagnosticsService 的整机原始通道，电池 I×V 是另一条电池流量通道；两者不因方向而互相改名，也都不是独立电源轨实测。"
        : "<strong>当前为充电状态：</strong>这里只保留电池侧原始流量，不等于续航功耗，也不应用于组件或任务耗电归因；请断开外部供电后进行功耗测试。"
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

  function renderFrameFlowHistory(active, frameFlow) {
    const svg = $("#frame-flow-history-chart");
    const empty = $("#frame-flow-history-empty");
    const source = $("#frame-flow-history-source");
    if (!svg || !empty || !source) return;
    const stages = Array.isArray(frameFlow?.stages) ? frameFlow.stages : [];
    const latest = active?.latest || {};
    const sessionStartUptime = finite(latest.uptime_s) && finite(latest.elapsed_s)
      ? Number(latest.uptime_s) - Number(latest.elapsed_s)
      : null;
    const colors = ["#4bc6e8", "#35d49a", "#f1d267", "#9e8cff", "#ff657d"];
    const laneColors = {
      app_submission: colors[0],
      render_queue: colors[1],
      surface_present: colors[2],
      display_scanout: colors[3],
    };
    const lanes = stages.map((stage, index) => {
      const points = (Array.isArray(stage?.timeline) ? stage.timeline : [])
        .map(point => {
          const elapsed = finite(point?.elapsed_s)
            ? Number(point.elapsed_s)
            : sessionStartUptime !== null && finite(point?.uptime_s)
              ? Math.max(0, Number(point.uptime_s) - sessionStartUptime)
              : null;
          const value = finite(point?.value)
            ? Number(point.value)
            : finite(point?.frame_rate_fps)
              ? Number(point.frame_rate_fps)
              : finite(point?.refresh_rate_hz)
                ? Number(point.refresh_rate_hz)
                : null;
          return elapsed === null || value === null ? null : { elapsed, value };
        })
        .filter(Boolean)
        .sort((left, right) => left.elapsed - right.elapsed);
      return {
        key: String(stage?.key || `stage-${index}`),
        phase: String(stage?.phase || "STAGE"),
        label: String(stage?.label || stage?.key || `节点 ${index + 1}`),
        valueLabel: String(stage?.timeline_value_label || "帧率"),
        unit: String(stage?.timeline_unit || "FPS"),
        status: String(stage?.status || "unavailable"),
        color: laneColors[stage?.key] || colors[index % colors.length],
        points,
      };
    });
    const allPoints = lanes.flatMap(lane => lane.points);
    svg.replaceChildren();
    if (!lanes.length) {
      empty.classList.remove("hidden");
      source.textContent = active?.running ? "等待节点时间序列" : "没有可用节点时间序列";
      return;
    }
    empty.classList.add("hidden");
    const populatedLaneCount = lanes.filter(lane => lane.points.length).length;
    source.textContent = `${populatedLaneCount} / ${lanes.length} 个节点 · ${allPoints.length} 个时间点`;

    const width = Math.max(520, Math.round(svg.clientWidth || 1000));
    const compact = width < 720;
    const left = compact ? 128 : 174;
    const right = compact ? 50 : 70;
    const top = 22;
    const bottom = 36;
    const laneHeight = compact ? 76 : 82;
    const height = top + bottom + laneHeight * lanes.length;
    const maximumTime = Math.max(
      1,
      Number(latest.elapsed_s || active?.elapsed_s || 0),
      ...allPoints.map(point => point.elapsed),
    );
    const observedMaximum = Math.max(
      1,
      Number(active?.performance?.peak_refresh_rate_hz || 0),
      ...allPoints.map(point => point.value),
    );
    const maximumValue = Math.max(30, Math.ceil(observedMaximum / 30) * 30);
    const x = value => left + Number(value) / maximumTime * (width - left - right);

    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.height = `${height}px`;
    svg.setAttribute(
      "aria-label",
      `完整渲染链路节点时间趋势，共 ${populatedLaneCount} 个可用节点，纵轴统一从 0 开始`,
    );
    const ticks = buildTimeTicks(0, maximumTime, compact ? 3 : 5);
    ticks.forEach((value, index) => {
      const position = x(value);
      svg.appendChild(svgNode("line", {
        x1: position,
        x2: position,
        y1: top,
        y2: height - bottom,
        class: "flow-history-grid",
      }));
      svg.appendChild(svgNode("text", {
        x: position,
        y: height - 12,
        "text-anchor": index === 0 ? "start" : index === ticks.length - 1 ? "end" : "middle",
        class: "flow-history-axis",
      }, formatDuration(value)));
    });

    lanes.forEach((lane, laneIndex) => {
      const laneTop = top + laneIndex * laneHeight;
      const laneBottom = laneTop + laneHeight - 15;
      const y = value => laneBottom - clamp(Number(value), 0, maximumValue)
        / maximumValue * (laneBottom - laneTop - 12);
      const latestPoint = lane.points.at(-1);
      svg.appendChild(svgNode("text", {
        x: 12,
        y: laneTop + 24,
        class: "flow-history-label",
      }, `${lane.phase} · ${lane.label}`));
      svg.appendChild(svgNode("text", {
        x: 12,
        y: laneTop + 43,
        class: "flow-history-value",
      }, latestPoint
        ? `${lane.valueLabel} ${latestPoint.value.toFixed(lane.unit === "Hz" ? 0 : 1)} ${lane.unit}`
        : lane.valueLabel));
      svg.appendChild(svgNode("text", {
        x: width - 8,
        y: laneTop + 14,
        "text-anchor": "end",
        class: "flow-history-axis",
      }, `${formatAxisNumber(maximumValue, 0)} ${lane.unit}`));
      svg.appendChild(svgNode("text", {
        x: width - 8,
        y: laneBottom,
        "text-anchor": "end",
        class: "flow-history-axis",
      }, `0 ${lane.unit}`));
      svg.appendChild(svgNode("line", {
        x1: left,
        x2: width - right,
        y1: laneBottom + 5,
        y2: laneBottom + 5,
        class: "flow-history-separator",
      }));
      if (!lane.points.length) {
        const emptyLabel = lane.key === "display_scanout"
          ? "暂无有效刷新率时间序列"
          : lane.key === "render_queue"
            ? "仅有阶段耗时，无独立 FPS 计数"
            : "平台未公开该节点的独立帧率时间序列";
        svg.appendChild(svgNode("text", {
          x: left + 10,
          y: laneTop + 35,
          class: "flow-history-empty-label",
        }, emptyLabel));
        return;
      }
      const coordinates = lane.points.map(point => ({
        ...point,
        x: x(point.elapsed),
        y: y(point.value),
      }));
      let path = `M${coordinates[0].x.toFixed(2)},${coordinates[0].y.toFixed(2)}`;
      for (let index = 1; index < coordinates.length; index += 1) {
        const previous = coordinates[index - 1];
        const current = coordinates[index];
        if (lane.key === "display_scanout") {
          path += ` L${current.x.toFixed(2)},${previous.y.toFixed(2)} L${current.x.toFixed(2)},${current.y.toFixed(2)}`;
        } else {
          path += ` L${current.x.toFixed(2)},${current.y.toFixed(2)}`;
        }
      }
      if (lane.key === "display_scanout" && coordinates.at(-1).elapsed < maximumTime) {
        path += ` L${x(maximumTime).toFixed(2)},${coordinates.at(-1).y.toFixed(2)}`;
      }
      svg.appendChild(svgNode("path", {
        d: path,
        class: `flow-history-line ${lane.status}`,
        style: `--lane-color:${lane.color}`,
      }));
      const last = coordinates.at(-1);
      svg.appendChild(svgNode("circle", {
        cx: last.x,
        cy: last.y,
        r: 3.4,
        class: "flow-history-dot",
        style: `--lane-color:${lane.color}`,
      }));
    });
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
    renderFrameFlowHistory(active, frameFlow);
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
    }).join("") : `<div class="empty-row">${active?.running ? "等待应用、渲染、合成与显示阶段帧数据" : "本次没有形成可验证的渲染链路节点"}</div>`;
    $("#render-pipeline-dominant").textContent = dominant.label || "--";
    $("#render-pipeline-dominant-detail").textContent = finite(dominant.p95_ms)
      ? `P95 ${Number(dominant.p95_ms).toFixed(2)} ms · P99 ${formatNumber(dominant.p99_ms, 2)} ms`
      : pipeline.limitations || (platform === "harmony"
        ? "HarmonyOS 量产接口提供帧抖动与合成节奏，不提供 Android framestats 阶段时间戳"
        : active?.running ? "等待详细帧时间戳" : "本次没有可解析的详细帧时间戳");
    const averageWholePower = platform === "ios"
      ? primaryPowerSnapshot(active).consumptionAverageMw
      : finite(power.average_power_mw) ? power.average_power_mw : active?.summary?.average_power_mw;
    $("#performance-whole-power").textContent = finite(averageWholePower)
      ? `${(Number(averageWholePower) / 1000).toFixed(3)} W`
      : "--";
    $("#render-pipeline-stage-list").innerHTML = stages.length ? stages.slice(0, 10).map(item => `<div class="pipeline-row"><div><strong>${escapeHtml(item.label || item.key)}</strong><small>${Number(item.sample_count || 0)} frames</small></div><div class="pipeline-track"><span style="width:${clamp(Number(item.p95_ms || 0) / Math.max(16.67, ...stages.map(stage => Number(stage.p95_ms || 0))) * 100, 0, 100).toFixed(1)}%"></span></div><b>${formatNumber(item.p95_ms, 2)} ms</b></div>`).join("") : `<div class="empty-row">${platform === "harmony" ? "当前仅提供帧抖动与合成节奏，不拆分 Android 渲染阶段" : active?.running ? "等待详细 framestats" : "本次没有可解析的 framestats 阶段数据"}</div>`;
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
    const sessionTransport = active && activePlatform(active) === "ios"
      ? iosWirelessTransportLabel(active, true)
      : "";
    $("#session-kicker").textContent = sessionTransport
      ? `${kicker} · ${sessionTransport}`
      : kicker;
    const defaultSessionTitle = (active?.test_mode || active?.metadata?.test_mode) === "performance"
      ? "Performance session"
      : "Power session";
    $("#session-title").textContent = active ? `${label} · ${active.title || active.run_name || defaultSessionTitle}` : "等待开始新的采集";
    const elapsed = Number(active?.elapsed_s || 0);
    const requested = Number(active?.requested_duration_s || 0);
    const durationUnlimited = activeDurationUnlimited(active);
    $("#session-time").textContent = durationUnlimited
      ? `已运行 ${formatDuration(elapsed)} / 无上限`
      : `${formatDuration(elapsed)} / ${formatDuration(requested)}`;
    $("#session-samples").textContent = `${active?.sample_count || 0} samples`;
    const sessionProgress = $("#session-progress");
    const indeterminateProgress = running && durationUnlimited;
    sessionProgress.closest(".progress-track")?.classList.toggle("indeterminate", indeterminateProgress);
    sessionProgress.style.width = indeterminateProgress
      ? "34%"
      : `${clamp(Number(active?.progress || 0), 0, 1) * 100}%`;
    stopButton.classList.toggle("hidden", !running);
    markerButton.classList.toggle("hidden", !running);
    reportLink.classList.toggle("hidden", !active?.report_ready);
    $("#run-probe").disabled = running;
    if (active?.report_url) reportLink.href = active.report_url;
    $$("#record-form input, #record-form select, #record-form button, #home-start-panel input, #home-start-panel select, #home-start-panel button").forEach(control => {
      if (control.id === "start-record") return;
      control.disabled = running;
    });
    updateStartControlState(active);
    $("#start-record").querySelector("span:last-child").textContent = running
      ? "采集中"
      : app.testMode === "performance" ? "开始性能测试" : "开始功耗测试";
    $$(".test-mode-switch [data-test-mode]").forEach(button => { button.disabled = running; });
    $$(".platform-switch [data-platform]").forEach(button => { button.disabled = running; });
    $("#system-monitor-input").disabled = running;
    updateAppScannerAvailability();
    updateCaptureFeatureControls();
  }

  function renderClusters(active) {
    const container = $("#cluster-list");
    if (activePlatform(active) === "ios") {
      container.innerHTML = '<div class="empty-row">iOS 当前不提供可验证的逐核心频率</div>';
      return;
    }
    const clusters = orderedCpuClusters(active);
    if (!clusters.length) {
      container.innerHTML = `<div class="empty-row">${active?.running ? "等待 CPU 频率数据" : "本次没有 CPU 频率数据"}</div>`;
      return;
    }
    container.innerHTML = clusters.map((cluster, index) => {
      const frequency = finite(cluster.frequency_mhz) ? Number(cluster.frequency_mhz) : null;
      const maximum = finite(cluster.maximum_mhz) ? Number(cluster.maximum_mhz) : null;
      const displayName = cpuClusterDisplayName(cluster, index, clusters);
      return `<div class="cluster-row">
        <div class="cluster-name"><strong>${escapeHtml(cpuCoreGroupLabel(cluster))}</strong><small>${escapeHtml(displayName)} · ${activePlatform(active) === "harmony" ? `${escapeHtml(cluster.name || "group")} 按最大频率分组均值` : `${escapeHtml(cluster.name || "policy")} 共享频率`}</small></div>
        <div class="cluster-frequency"><span>${frequency === null ? "--" : `${frequency.toFixed(0)} MHz`}</span><small>${maximum === null ? "等待频率上限" : `上限 ${maximum.toFixed(0)} MHz`}</small></div>
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
      const processSnapshotsEnabled = captureFeatureEnabled(active, "process_snapshots");
      if (!context.foreground_package) {
        $("#context-package").textContent = "当前前台未知";
        $("#context-activity").textContent = performance.foreground_state_reason
          || "DVT 未提供采集开始时的前台应用快照";
      }
      const iosBrightness = active?.system_monitor?.thermal?.display_brightness || {};
      $("#context-display-settings").textContent = finite(iosBrightness.user_brightness_pct)
        ? `${Number(iosBrightness.user_brightness_pct).toFixed(1)}% user · ${finite(iosBrightness.luminance_nits) ? Number(iosBrightness.luminance_nits).toFixed(1) + " nits" : "nits --"} · raw ${finite(iosBrightness.raw_backlight_raw) ? Number(iosBrightness.raw_backlight_raw).toFixed(0) : "--"}`
        : "等待 AppleARMBacklight 亮度样本";
      $("#context-pressure-driver").textContent = finite(latest.gpu_load_pct)
        ? `GPU ${Number(latest.gpu_load_pct).toFixed(1)}%`
        : finite(latest.cpu_pct) ? `CPU ${Number(latest.cpu_pct).toFixed(1)}%` : "等待 DVT 样本";
      const processes = Array.isArray(monitor.processes) ? monitor.processes : [];
      const leading = processes.length ? [...processes].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0] : null;
      $("#context-pressure-task").textContent = leading
        ? `${leading.name || "process"} · ${formatNumber(leading.cpu_pct, 1)}% CPU`
        : processSnapshotsEnabled ? "等待 DVT 进程快照" : "进程快照未启用";
      $("#context-power-scheduler").textContent = "iOS 不公开 Android cpuset / Governor / ADPF";
      const voltage = finite(latest.voltage_mv) ? `${(Number(latest.voltage_mv) / 1000).toFixed(3)} V` : "--";
      const level = finite(battery.level_pct) ? `${Number(battery.level_pct).toFixed(0)}%` : "--";
      $("#context-battery").textContent = `${voltage} / ${level}`;
      $("#context-reconnect").textContent = String(active?.checkpoint?.reconnect_count ?? active?.metadata?.reconnect_count ?? 0);
      $("#context-source").textContent = context.source
        || (performance.foreground_state_status === "observed"
          ? "iOS DVT application state"
          : "iOS DVT 状态变化流 · 当前前台未确认");
      return;
    }
    const startSettings = settings.start || {};
    const contextPlatform = activePlatform(active);
    const contextScreenState = String(context.screen_state || "").trim().toLowerCase();
    const contextDisplayInactive = contextPlatform === "harmony" && contextScreenState
      && !["awake", "on"].includes(contextScreenState);
    const refreshValue = finite(performance.current_refresh_rate_hz)
      ? Number(performance.current_refresh_rate_hz)
      : finite(context.refresh_rate_hz) ? Number(context.refresh_rate_hz) : null;
    const refresh = refreshValue === null
      ? "--"
      : contextDisplayInactive
        ? `${refreshValue.toFixed(0)} Hz（熄屏保留配置，非当前输出）`
        : `${refreshValue.toFixed(0)} Hz`;
    const brightness = startSettings["system.screen_brightness"] ?? performance.brightness_raw ?? "--";
    const brightnessContext = contextPlatform === "harmony"
      ? `RenderService 背光原始值 ${brightness}（非 nit、非热限亮${contextDisplayInactive ? "，熄屏保留" : ""}）`
      : `${brightness} brightness`;
    const lowPower = String(startSettings["global.low_power"] ?? "--") === "1" ? "省电开" : "省电关";
    $("#context-display-settings").textContent = `${brightnessContext} / ${refresh} / ${lowPower}`;
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

  /* Legacy performance-context and scheduler panels were removed from the UI.
    const latest = active?.latest || {};
    const primaryPower = primaryPowerSnapshot(active);
    const powerFlow = powerFlowPresentation(active);
    const context = active?.context || {};
    const brightnessState = active?.brightness_throttling?.current_state || {};
    const brightnessStatus = String(brightnessState.status || "none");
    const brightnessIssue = brightnessStatus === "confirmed" || brightnessStatus === "suspected";
    const brightness = monitor?.thermal?.display_brightness || {};
    const systemCount = Number(monitor.system_snapshot_count || 0);
    const processSnapshotsEnabled = captureFeatureEnabled(active, "process_snapshots");
    $("#performance-snapshot-status").textContent = systemCount
      ? `${systemCount} 个 DVT 进程快照`
      : processSnapshotsEnabled
        ? active?.running ? "等待首个 DVT 进程快照" : "尚无 DVT 进程快照"
        : "进程快照未启用 · 基础 DVT CPU/GPU 仍独立采集";
    $("#performance-refresh-value").textContent = formatMetric(latest.cpu_pct, "%", 1);
    $("#performance-refresh-label").textContent = "DVT 进程 CPU 归一化";
    $("#performance-fps-value").textContent = formatMetric(latest.gpu_load_pct, "%", 1);
    $("#performance-fps-label").textContent = "DVT GPU utilization";
    $("#performance-frame-value").textContent = finite(primaryPower.latestMw)
      ? `${(Number(primaryPower.latestMw) / 1000).toFixed(3)} W`
      : "--";
    $("#performance-frame-label").textContent = powerFlow.systemLoad
      ? finite(primaryPower.sampleAgeS)
        ? `DiagnosticsService SystemLoad · age ${Number(primaryPower.sampleAgeS).toFixed(1)} s`
        : "DiagnosticsService SystemLoad"
      : "电池流量 I×V · 当前无 SystemLoad";
    $("#performance-touch-value").textContent = formatMetric(latest.collector_cpu_pct, "%", 2);
    $("#performance-touch-label").textContent = "观察者相关进程 CPU（上界）";

    const resourceRows = [
      { label: "CPU", value: latest.cpu_pct, unit: "%", scale: 100 },
      { label: "GPU", value: latest.gpu_load_pct, unit: "%", scale: 100 },
      { label: "Observer", value: latest.collector_cpu_pct, unit: "%", scale: 25 },
    ].filter(item => item.label !== "GPU" || finite(item.value));
    $("#performance-residency-source").textContent = powerFlow.systemLoad
      ? "DVT / DiagnosticsService SystemLoad"
      : "DVT / battery current × voltage";
    $("#performance-residency-list").innerHTML = resourceRows.map(item => {
      const value = finite(item.value) ? Number(item.value) : null;
      const share = value === null ? 0 : clamp(value / item.scale * 100, 0, 100);
      return `<div class="performance-residency-row"><div><strong>${escapeHtml(item.label)}</strong><small>${value === null ? "--" : `${value.toFixed(item.label === "Observer" ? 2 : 1)} ${item.unit}`}</small></div><div class="performance-residency-track"><span style="width:${share.toFixed(1)}%"></span></div><b>${value === null ? "--" : value.toFixed(1)}</b></div>`;
    }).join("");

    $("#performance-window-value").textContent = context.foreground_package || active?.metadata?.target_package || "--";
    $("#performance-display-value").textContent = finite(brightness.user_brightness_pct)
      ? `${Number(brightness.user_brightness_pct).toFixed(1)}% user / ${finite(brightness.luminance_nits) ? Number(brightness.luminance_nits).toFixed(1) + " nits" : "nits --"} / raw ${finite(brightness.raw_backlight_raw) ? Number(brightness.raw_backlight_raw).toFixed(0) : "--"}`
      : "等待 AppleARMBacklight 亮度样本";
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
    const hasIssue = brightnessIssue || priority.length > 0 || thermalStatus > 0 || overhead >= 10;
    banner.classList.toggle("active", hasIssue);
    banner.classList.toggle("idle", !hasIssue);
    if (brightnessIssue) {
      $("#system-priority-title").textContent = brightnessStatus === "confirmed"
        ? "检测到 iPhone 屏幕热降亮"
        : "检测到 iPhone 疑似屏幕降亮";
      $("#system-priority-detail").textContent = brightnessState.reason || "AppleARMBacklight 实际毫尼特低于同一用户亮度基线。";
      $("#system-priority-badge").textContent = brightnessStatus === "confirmed" ? "DIM" : "SUSPECT";
    } else if (priority.length) {
      const leading = [...priority].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0];
      $("#system-priority-title").textContent = "检测到 iOS 后台资源竞争";
      $("#system-priority-detail").textContent = `${leading.name || leading.watch_name || "后台进程"} 当前 CPU ${formatNumber(leading.cpu_pct, 1)}%。`;
      $("#system-priority-badge").textContent = "ACTIVE";
    } else if (overhead >= 10) {
      $("#system-priority-title").textContent = "观察者相关进程 CPU 较高";
      $("#system-priority-detail").textContent = `sysmond、DTServiceHub 与配对服务同期 CPU 合计为 ${overhead.toFixed(2)}%；这只是干扰上界。功耗测试优先提高 iOS 主采样周期后复测；进程快照开关不会关闭基础 DVT/sysmond。`;
      $("#system-priority-badge").textContent = "OBSERVER";
    } else if (thermalStatus > 0) {
      $("#system-priority-title").textContent = `检测到热状态 ${thermalStatus}`;
      $("#system-priority-detail").textContent = "请结合 DVT CPU/GPU 与电池温度判断持续性能下降。";
      $("#system-priority-badge").textContent = "THERMAL";
    } else if (!processSnapshotsEnabled) {
      $("#system-priority-title").textContent = "后台进程分析未启用";
      $("#system-priority-detail").textContent = "当前只依据基础 DVT CPU/GPU、温度和观察者相关 CPU 展示原始状态；未采集周期进程快照，不能判断后台进程是否异常。";
      $("#system-priority-badge").textContent = "OFF";
    } else {
      $("#system-priority-title").textContent = "未检测到明显 iOS 性能干扰";
      $("#system-priority-detail").textContent = "DVT 进程、GPU、温度和观察者相关进程 CPU 均未触发当前提示阈值。";
      $("#system-priority-badge").textContent = "CLEAR";
    }

    $("#watched-process-body").innerHTML = priority.length ? priority.map(item => `<tr class="priority-row"><td><span class="process-identity"><strong>${escapeHtml(item.watch_label || item.watch_name || item.name || "unknown")}</strong><small>${escapeHtml(item.command || item.name || "--")}</small></span></td><td>${escapeHtml(item.pid ?? "--")}</td><td>${finite(item.cpu_pct) ? `${Number(item.cpu_pct).toFixed(1)}%` : "--"}</td><td><span class="activity-pill active">ACTIVE</span></td></tr>`).join("") : `<tr><td colspan="4" class="table-empty">${processSnapshotsEnabled ? "未检测到明显 iOS 后台异常。" : "未采集后台进程快照，不能判断是否异常。"}</td></tr>`;
    $("#system-process-body").innerHTML = processes.length ? processes.slice(0, 5).map(item => `<tr><td><span class="process-identity"><strong>${escapeHtml(item.name || item.command || "unknown")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${escapeHtml(item.user || "--")}</small></span></td><td><strong class="cpu-value">${formatNumber(item.cpu_pct, 1)}%</strong></td><td>${finite(item.mem_pct) ? `${formatNumber(item.mem_pct, 1)}%` : formatBytes(item.resident_bytes)}</td><td>${escapeHtml(item.state || "--")}</td></tr>`).join("") : `<tr><td colspan="4" class="table-empty">${processSnapshotsEnabled ? "暂无 DVT 进程快照。" : "进程快照未启用。"}</td></tr>`;
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
    const brightnessState = active?.brightness_throttling?.current_state || {};
    const brightnessVendorKnown = brightnessState.vendor_thermal_active === true || brightnessState.vendor_thermal_active === false;
    const brightnessStatus = brightnessVendorKnown
      ? brightnessState.vendor_thermal_active === true ? "confirmed" : "none"
      : String(brightnessState.status || "none");
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
    const currentScreenState = String(active?.context?.screen_state || "").trim().toLowerCase();
    const displayInactive = isHarmony && currentScreenState
      && !["awake", "on"].includes(currentScreenState);
    $("#performance-refresh-value").textContent = currentRefresh === null ? "--" : `${currentRefresh.toFixed(0)} Hz`;
    $("#performance-refresh-label").textContent = displayInactive && currentRefresh !== null
      ? "熄屏保留配置，非当前输出"
      : peakRefresh === null ? "当前显示档位" : `设备最高 ${peakRefresh.toFixed(0)} Hz`;
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
    const effectiveBrightness = finite(brightnessState.effective_raw_estimate)
      ? ` → 有效约 ${Number(brightnessState.effective_raw_estimate).toFixed(0)}`
      : "";
    const brightnessDisplay = isHarmony
      ? `背光原始值 ${brightness}（非 nit、非热限亮${displayInactive ? "，熄屏保留" : ""}）`
      : `${brightness}${effectiveBrightness}`;
    const displaySource = isHarmony
      ? "HarmonyOS RenderService screen"
      : "Android dumpsys display 活动模式";
    $("#performance-window-value").textContent = performance.foreground_window_name ? `${performance.foreground_window_name}${finite(performance.foreground_window_id) ? ` · #${performance.foreground_window_id}` : ""}` : "--";
    $("#performance-display-value").textContent = `${resolution} / ${brightnessDisplay} · ${displaySource}`;
    $("#performance-gpu-value").textContent = performance.gpu_renderer || "--";
    $("#performance-temperature-value").textContent = hottest && finite(hottest.value_c) ? `${Number(hottest.value_c).toFixed(1)} °C · ${hottest.name || "sensor"}` : finite(active?.latest?.temperature_c) ? `${Number(active.latest.temperature_c).toFixed(1)} °C · battery` : "--";
    $("#performance-switch-value").textContent = `${Number(performance.refresh_switch_count || 0)} 次`;
    $("#performance-frame-count").textContent = Number(performance.frame_sample_count || 0).toLocaleString("zh-CN");

    const banner = $("#system-priority-banner");
    const missedPct = finite(performance.frame_issue_pct) ? Number(performance.frame_issue_pct) : finite(performance.missed_vsync_interval_pct) ? Number(performance.missed_vsync_interval_pct) : 0;
    const thermalStatus = finite(thermal.status) ? Number(thermal.status) : 0;
    const hasFrameIssue = missedPct >= 2 || Number(performance.severe_frame_interval_count || 0) > 0;
    const brightnessIssue = brightnessStatus === "confirmed" || brightnessStatus === "suspected";
    const hasIssue = brightnessIssue || priority.length > 0 || hasFrameIssue || thermalStatus > 0;
    banner.classList.toggle("active", hasIssue);
    banner.classList.toggle("idle", !hasIssue);
    if (brightnessIssue) {
      $("#system-priority-title").textContent = brightnessStatus === "confirmed" ? "检测到屏幕热降亮" : "检测到疑似屏幕热降亮";
      $("#system-priority-detail").textContent = `${brightnessState.reason || "显示侧热限制已触发"}；${finite(brightnessState.vendor_thermal_limit_nits) ? `系统标称上限 ${Number(brightnessState.vendor_thermal_limit_nits).toFixed(0)} nit（非实测）` : `系统设定亮度 ${finite(brightnessState.setting_raw) ? Number(brightnessState.setting_raw).toFixed(0) : "未知"}`}。`;
      $("#system-priority-badge").textContent = brightnessStatus === "confirmed" ? "DIM" : "SUSPECT";
    } else if (priority.length) {
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

  */
  function renderConsole(active) {
    const container = $("#console-output");
    let logs = [
      ...(Array.isArray(active?.logs) ? active.logs : []),
      ...app.uiLogs,
    ];
    if (app.consoleClearedAt) logs = logs.filter(item => Number(item.time || 0) >= app.consoleClearedAt);
    logs.sort((left, right) => Number(left.time || 0) - Number(right.time || 0));
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 30;
    if (!logs.length) {
      container.innerHTML = '<div class="console-line ui"><time>--:--:--</time><span>UI</span><p>Dashboard ready. Select a device and configure a session.</p></div>';
      return;
    }
    container.innerHTML = logs.map(item => {
      const date = finite(item.time) ? new Date(Number(item.time) * 1000) : null;
      const clock = date ? date.toLocaleTimeString("zh-CN", { hour12: false }) : "--:--:--";
      const source = String(item.source || "ui");
      const type = item.type === "error" || /error|failed/i.test(String(item.line || "")) ? "error" : source;
      return `<div class="console-line ${escapeHtml(type)}"><time>${escapeHtml(clock)}</time><span>${escapeHtml(source)}</span><p>${escapeHtml(item.line || "")}</p></div>`;
    }).join("");
    if (nearBottom) container.scrollTop = container.scrollHeight;
  }

  const liveTimelinePalette = ["#4bc6e8", "#9e8cff", "#ffb45c", "#35d49a", "#f1d267", "#ff657d"];

  function liveTimelineLayoutContext(active = app.state?.active) {
    const platform = activePlatform(active);
    const requestedMode = String(active?.test_mode || active?.metadata?.test_mode || app.testMode || "power").toLowerCase();
    const mode = requestedMode === "performance" ? "performance" : "power";
    return `${platform}:${mode}`;
  }

  function currentLiveTimelineLayout(active = app.state?.active) {
    const context = liveTimelineLayoutContext(active);
    if (!app.liveTimelineLayouts[context]) {
      app.liveTimelineLayouts[context] = {
        order: [...defaultLiveTimelineOrder],
        hidden: new Set(),
      };
    }
    return app.liveTimelineLayouts[context];
  }

  function saveLiveTimelineLayouts() {
    try {
      const contexts = Object.fromEntries(
        Object.entries(app.liveTimelineLayouts).map(([context, layout]) => [
          context,
          {
            order: normalizeLiveTimelineOrder(layout?.order),
            hidden: [...normalizeLiveTimelineHidden([...(layout?.hidden || [])])],
          },
        ]),
      );
      localStorage.setItem(
        liveTimelineLayoutStorageKey,
        JSON.stringify({ version: 1, contexts }),
      );
    } catch (_error) {
      // Display preferences are optional; telemetry rendering must continue if storage is unavailable.
    }
  }

  function liveTimelineGroupKey(lane) {
    const key = String(lane?.key || "");
    return key.startsWith("frame-flow-") ? "frame_flow" : key;
  }

  function liveTimelineDefinitionSupported(active, key) {
    const platform = activePlatform(active);
    switch (String(key || "")) {
      case "cpu_pct":
        return captureFeatureEnabled(active, "cpu_usage");
      case "cpu_frequency":
        return platform !== "ios" && captureFeatureEnabled(active, "cpu_frequency");
      case "frame_rate_fps":
      case "frame_flow":
        return platform !== "ios" && captureFeatureEnabled(active, "frame_rate");
      case "refresh_rate_hz":
        return platform !== "ios" && (
          captureFeatureEnabled(active, "foreground_window")
          || captureFeatureEnabled(active, "frame_rate")
        );
      case "frame_time_ms":
      case "frame_issue_pct":
        return platform !== "ios" && captureFeatureEnabled(active, "frame_rate");
      case "gpu_load_pct":
        return captureFeatureEnabled(active, "gpu_metrics");
      case "gpu_frequency_mhz":
        return platform !== "ios" && captureFeatureEnabled(active, "gpu_metrics");
      case "memory_frequency_mhz":
        return platform !== "ios" && captureFeatureEnabled(active, "memory_frequency");
      case "temperature_c":
        return captureFeatureEnabled(active, "thermal");
      case "power_mw":
      case "current_ma":
      case "voltage_mv":
        return true;
      default:
        return false;
    }
  }

  function applyLiveTimelineLayout(active, lanes) {
    const layout = currentLiveTimelineLayout(active);
    const rank = new Map(layout.order.map((key, index) => [key, index]));
    return (Array.isArray(lanes) ? lanes : [])
      .map((lane, index) => ({ lane, index, group: liveTimelineGroupKey(lane) }))
      .filter(item => liveTimelineLayoutKeys.has(item.group) && !layout.hidden.has(item.group))
      .sort((left, right) => (
        (rank.get(left.group) ?? defaultLiveTimelineOrder.length)
        - (rank.get(right.group) ?? defaultLiveTimelineOrder.length)
        || left.index - right.index
      ))
      .map(item => item.lane);
  }

  function renderLiveTimelineConfiguration(active, availableLanes) {
    const container = $("#live-timeline-config-list");
    const count = $("#live-timeline-config-count");
    if (!container || !count) return;
    const context = liveTimelineLayoutContext(active);
    const layout = currentLiveTimelineLayout(active);
    const definitions = layout.order
      .map(key => liveTimelineLayoutDefinitionMap.get(key))
      .filter(definition => definition && liveTimelineDefinitionSupported(active, definition.key));
    const lanesByGroup = new Map();
    (Array.isArray(availableLanes) ? availableLanes : []).forEach(lane => {
      const group = liveTimelineGroupKey(lane);
      if (!lanesByGroup.has(group)) lanesByGroup.set(group, []);
      lanesByGroup.get(group).push(lane);
    });
    const enabledCount = definitions.filter(item => !layout.hidden.has(item.key)).length;
    count.textContent = `${enabledCount} / ${definitions.length} 类开启`;

    const signature = JSON.stringify({
      context,
      order: layout.order,
      hidden: [...layout.hidden].sort(),
      available: definitions.map(item => [
        item.key,
        ...(lanesByGroup.get(item.key) || []).map(lane => `${lane.key}:${lane.source}`),
      ]),
    });
    if (container.dataset.signature === signature) return;
    container.dataset.signature = signature;
    container.innerHTML = definitions.map((definition, index) => {
      const groupedLanes = lanesByGroup.get(definition.key) || [];
      const hidden = layout.hidden.has(definition.key);
      const availability = groupedLanes.length
        ? definition.key === "frame_flow"
          ? `${groupedLanes.length} 个链路节点有数据`
          : `有数据 · ${groupedLanes[0].source}`
        : active
          ? active.running ? "等待有效数据" : "本次没有有效数据"
          : "尚未开始采集";
      return `<div class="live-timeline-config-row${hidden ? " is-hidden" : ""}" data-live-timeline-row="${escapeHtml(definition.key)}">
        <label class="live-timeline-config-toggle">
          <input type="checkbox" data-live-timeline-toggle="${escapeHtml(definition.key)}"${hidden ? "" : " checked"} aria-label="显示 ${escapeHtml(definition.label)}">
          <span class="live-timeline-config-copy"><strong>${escapeHtml(definition.label)}</strong><small>${escapeHtml(definition.hint)} · ${escapeHtml(availability)}</small></span>
        </label>
        <span class="live-timeline-config-actions">
          <button class="live-timeline-order-button" type="button" data-live-timeline-move="-1" data-live-timeline-key="${escapeHtml(definition.key)}" aria-label="上移 ${escapeHtml(definition.label)}"${index === 0 ? " disabled" : ""}>↑</button>
          <button class="live-timeline-order-button" type="button" data-live-timeline-move="1" data-live-timeline-key="${escapeHtml(definition.key)}" aria-label="下移 ${escapeHtml(definition.label)}"${index === definitions.length - 1 ? " disabled" : ""}>↓</button>
        </span>
      </div>`;
    }).join("");
  }

  function setLiveTimelineVisibility(active, key, visible) {
    if (!liveTimelineLayoutKeys.has(key) || !liveTimelineDefinitionSupported(active, key)) return false;
    const layout = currentLiveTimelineLayout(active);
    if (visible) layout.hidden.delete(key);
    else layout.hidden.add(key);
    saveLiveTimelineLayouts();
    return true;
  }

  function moveLiveTimelineLayoutItem(active, key, direction) {
    if (!liveTimelineLayoutKeys.has(key) || !liveTimelineDefinitionSupported(active, key)) return false;
    const layout = currentLiveTimelineLayout(active);
    const supportedOrder = layout.order.filter(item => liveTimelineDefinitionSupported(active, item));
    const supportedIndex = supportedOrder.indexOf(key);
    const targetSupportedIndex = supportedIndex + Number(direction);
    if (supportedIndex < 0 || targetSupportedIndex < 0 || targetSupportedIndex >= supportedOrder.length) return false;
    const targetKey = supportedOrder[targetSupportedIndex];
    const index = layout.order.indexOf(key);
    const target = layout.order.indexOf(targetKey);
    if (index < 0 || target < 0) return false;
    [layout.order[index], layout.order[target]] = [layout.order[target], layout.order[index]];
    saveLiveTimelineLayouts();
    return true;
  }

  function distributedTimelinePositions(size) {
    if (size <= 0) return [];
    const positions = [];
    const pending = [[0, size]];
    let cursor = 0;
    while (cursor < pending.length) {
      const [start, end] = pending[cursor];
      cursor += 1;
      if (start >= end) continue;
      const middle = Math.floor((start + end) / 2);
      positions.push(middle);
      pending.push([start, middle], [middle + 1, end]);
    }
    return positions;
  }

  function addTimelineBoundaryGroups(selected, groups, limit, pointCount) {
    if (!groups.length || selected.size >= limit) return;
    const allIndexes = new Set(groups.flatMap(group => (
      group.filter(index => index >= 0 && index < pointCount)
    )));
    if (new Set([...selected, ...allIndexes]).size <= limit) {
      allIndexes.forEach(index => selected.add(index));
      return;
    }
    distributedTimelinePositions(groups.length).forEach(position => {
      const group = new Set(groups[position].filter(index => index >= 0 && index < pointCount));
      const cost = [...group].filter(index => !selected.has(index)).length;
      if (cost <= limit - selected.size) group.forEach(index => selected.add(index));
    });
  }

  function timelineValuesDiffer(previous, current) {
    if (!finite(previous) || !finite(current)) return false;
    const tolerance = Math.max(.01, Math.abs(Number(previous)) * .0001);
    return Math.abs(Number(current) - Number(previous)) > tolerance;
  }

  function downsampleTimelinePoints(points, limit = 900, { preserveSteps = false } = {}) {
    const boundedLimit = Math.max(0, Math.min(points.length, Math.floor(Number(limit) || 0)));
    if (!boundedLimit) return [];
    if (points.length <= boundedLimit) return points;
    if (boundedLimit === 1) return [points.at(-1)];
    if (boundedLimit === 2) return [points[0], points.at(-1)];

    const selected = new Set([0, points.length - 1]);
    const breakBoundaryGroups = [];
    const stepBoundaryGroups = [];
    points.forEach((point, index) => {
      if (index > 0 && point.breakBefore) breakBoundaryGroups.push([index - 1, index]);
      if (
        preserveSteps
        && index > 0
        && timelineValuesDiffer(points[index - 1]?.value, point?.value)
      ) {
        stepBoundaryGroups.push([index - 1, index]);
      }
    });
    addTimelineBoundaryGroups(selected, breakBoundaryGroups, boundedLimit, points.length);
    addTimelineBoundaryGroups(selected, stepBoundaryGroups, boundedLimit, points.length);

    const remaining = boundedLimit - selected.size;
    if (remaining > 0) {
      const bucketSlots = 2;
      const bucketCount = Math.max(1, Math.floor(remaining / bucketSlots));
      const baseQuota = Math.floor(remaining / bucketCount);
      const extraQuota = remaining % bucketCount;
      for (let bucket = 0; bucket < bucketCount; bucket += 1) {
        const quota = baseQuota + (bucket < extraQuota ? 1 : 0);
        const start = Math.floor(bucket * points.length / bucketCount);
        const end = Math.floor((bucket + 1) * points.length / bucketCount);
        const bucketIndexes = [];
        for (let index = start; index < end; index += 1) {
          if (
            index > 0
            && index < points.length - 1
            && !selected.has(index)
            && finite(points[index]?.value)
          ) bucketIndexes.push(index);
        }
        if (!bucketIndexes.length || quota <= 0) continue;

        const minimumIndex = bucketIndexes.reduce((best, index) => (
          Number(points[index].value) < Number(points[best].value) ? index : best
        ));
        const maximumIndex = bucketIndexes.reduce((best, index) => (
          Number(points[index].value) > Number(points[best].value) ? index : best
        ));
        const mean = bucketIndexes.reduce((sum, index) => sum + Number(points[index].value), 0)
          / bucketIndexes.length;
        const bucketExtrema = [...new Set([minimumIndex, maximumIndex])].sort((left, right) => (
          Math.abs(Number(points[right].value) - mean)
          - Math.abs(Number(points[left].value) - mean)
        ));
        let added = 0;
        bucketExtrema.forEach(index => {
          if (added >= quota || selected.size >= boundedLimit) return;
          selected.add(index);
          added += 1;
        });

        if (added < quota && selected.size < boundedLimit) {
          const fill = bucketIndexes.filter(index => !selected.has(index));
          const wanted = Math.min(quota - added, boundedLimit - selected.size, fill.length);
          for (let offset = 0; offset < wanted; offset += 1) {
            const position = Math.min(
              fill.length - 1,
              Math.floor((offset + .5) * fill.length / wanted),
            );
            selected.add(fill[position]);
          }
        }
      }
    }

    if (selected.size < boundedLimit) {
      const fill = points.map((_, index) => index).filter(index => !selected.has(index));
      const wanted = Math.min(boundedLimit - selected.size, fill.length);
      for (let offset = 0; offset < wanted; offset += 1) {
        const position = Math.min(
          fill.length - 1,
          Math.floor((offset + .5) * fill.length / wanted),
        );
        selected.add(fill[position]);
      }
    }

    const indexes = [...selected].sort((left, right) => left - right);
    return indexes.map((sourceIndex, outputIndex) => {
      if (!outputIndex) return points[sourceIndex];
      const previousSourceIndex = indexes[outputIndex - 1];
      const skippedBreak = points.slice(previousSourceIndex + 1, sourceIndex + 1)
        .find(point => point.breakBefore);
      if (!skippedBreak || points[sourceIndex].breakBefore) return points[sourceIndex];
      return {
        ...points[sourceIndex],
        breakBefore: true,
        breakElapsed: finite(skippedBreak.breakElapsed)
          ? Number(skippedBreak.breakElapsed)
          : Number(skippedBreak.elapsed),
      };
    });
  }

  function timelinePoints(
    source,
    value,
    {
      positive = false,
      preserveSteps = false,
      elapsed = null,
      collapseSteps = false,
    } = {},
  ) {
    const points = [];
    let breakPending = false;
    let breakElapsed = null;
    (Array.isArray(source) ? source : []).forEach((raw, index) => {
      const observedElapsed = finite(raw?.elapsed_s) ? Number(raw.elapsed_s) : Number(index);
      const pointElapsed = typeof elapsed === "function"
        ? elapsed(raw, observedElapsed)
        : observedElapsed;
      const next = value(raw);
      const explicitBreak = Boolean(raw?.report_break_before || raw?._report_break_before || raw?.break_before);
      const stale = raw?.stale === true || raw?.gpu_sample_stale === true || raw?.power_sample_stale === true;
      const validValue = finite(next) && (!positive || Number(next) > 0);
      if (explicitBreak || stale || !validValue) {
        breakPending = true;
        if (breakElapsed == null && finite(observedElapsed)) breakElapsed = observedElapsed;
        if (!validValue || stale) return;
      }
      const previousPoint = points.at(-1);
      if (
        collapseSteps
        && previousPoint
        && !breakPending
        && !explicitBreak
        && !timelineValuesDiffer(previousPoint.value, next)
      ) return;
      points.push({
        elapsed: finite(pointElapsed) ? Math.max(0, Number(pointElapsed)) : observedElapsed,
        value: Number(next),
        raw,
        breakBefore: breakPending || explicitBreak,
        breakElapsed: breakPending ? breakElapsed : null,
      });
      breakPending = false;
      breakElapsed = null;
    });
    if (breakPending && points.length && breakElapsed != null) {
      points.at(-1).breakAfterElapsed = breakElapsed;
    }
    return downsampleTimelinePoints(points, 900, { preserveSteps });
  }

  function liveMetricSource(active, key) {
    const platform = activePlatform(active);
    const performance = active?.performance || {};
    const powerFlow = powerFlowPresentation(active);
    const labels = {
      power_mw: powerFlow.systemLoad
        ? "DiagnosticsService 的整机原始 SystemLoad 通道；外供时可能接近 SystemPowerIn，非电池 I×V / 非独立电源轨实测"
        : active?.latest?.power_source || (platform === "harmony" ? "HarmonyOS BatteryService" : "Android fuel gauge / BatteryService"),
      current_ma: platform === "harmony" ? "HarmonyOS BatteryService" : platform === "ios" ? "PowerTelemetry / battery diagnostics" : "Android fuel gauge current_now",
      voltage_mv: platform === "harmony" ? "HarmonyOS BatteryService" : "Battery voltage sensor",
      cpu_pct: platform === "ios" ? "DVT sysmond" : "累计 /proc/stat",
      frame_rate_fps: performance.frame_source || (platform === "harmony" ? "SmartPerf / RenderService" : "前台窗口 / SurfaceFlinger"),
      refresh_rate_hz: performance.refresh_rate_timeline_source
        || (platform === "harmony" ? "RenderService screen activeMode" : "DisplayManager active display mode"),
      frame_time_ms: performance.render_pipeline?.frame_metric_source || performance.frame_source || "逐帧呈现间隔 / framestats",
      gpu_load_pct: platform === "ios" ? "DVT graphics" : platform === "harmony" ? "SmartPerf GPU" : "GPU sysfs / driver",
      gpu_frequency_mhz: platform === "harmony" ? "SmartPerf GPU" : "GPU devfreq / sysfs",
      memory_frequency_mhz: platform === "harmony" ? "SmartPerf DDR" : "DMC / MIF devfreq",
      temperature_c: platform === "ios" ? "battery diagnostics" : "BatteryService / thermal",
    };
    return labels[key] || "runtime sampler";
  }

  function metricTimelineLane(active, key, {
    feature = null,
    enabled = null,
    source = null,
    title = null,
    seriesLabel = null,
    sourceLabel = null,
    positive = false,
    step = false,
  } = {}) {
    if (feature && !captureFeatureEnabled(active, feature)) return null;
    if (enabled !== null && !enabled) return null;
    const definition = metricDefinitions[key];
    if (!definition) return null;
    const rows = source || (definition.series === "performance" ? active?.performance_series : active?.series);
    const systemLoadPower = key === "power_mw" && powerFlowPresentation(active).systemLoad;
    const metricValue = systemLoadPower
      ? row => row?.power_source === iosSystemLoadPowerSource
        && (!finite(row?.power_sample_age_s) || Number(row.power_sample_age_s) <= iosSystemLoadStaleAfterS)
        ? definition.value(row)
        : null
      : definition.value;
    const points = timelinePoints(rows, metricValue, {
      positive,
      preserveSteps: step,
      collapseSteps: systemLoadPower,
      elapsed: systemLoadPower
        ? (row, observedElapsed) => finite(row?.power_sample_age_s)
          ? observedElapsed - Math.max(0, Number(row.power_sample_age_s))
          : observedElapsed
        : null,
    });
    if (!points.length) return null;
    const series = [{
      key,
      label: seriesLabel || definition.legend,
      color: definition.color,
      points,
      step,
    }];
    const overlay = definition.overlay
      && typeof definition.overlay.value === "function"
      && (key !== "frame_rate_fps" || standardOnePercentLowAvailable(active))
      ? definition.overlay
      : null;
    if (overlay) {
      const overlayPoints = timelinePoints(rows, overlay.value, {
        positive,
        preserveSteps: step,
      });
      if (overlayPoints.length) {
        series.push({
          key: `${key}-overlay`,
          label: key === "frame_rate_fps"
            ? performance.one_percent_low_timeline_label || overlay.legend || "Secondary"
            : overlay.legend || "Secondary",
          color: overlay.color || "#f1d267",
          points: overlayPoints,
          dashed: true,
          step,
        });
      }
    }
    if (key === "power_mw" && powerFlowPresentation(active).systemLoad) {
      const batteryFlowPoints = timelinePoints(
        rows,
        row => (
          finite(row?.current_ma) && finite(row?.voltage_mv)
            ? Number(row.current_ma) * Number(row.voltage_mv) / 1_000
            : null
        ),
        { preserveSteps: step },
      );
      if (batteryFlowPoints.length) {
        series.push({
          key: "battery-flow-power",
          label: "电池流量 I×V",
          color: "#9e8cff",
          points: batteryFlowPoints,
          dashed: true,
          step,
        });
      }
    }
    return {
      key,
      title: title || definition.title,
      source: sourceLabel || liveMetricSource(active, key),
      unit: definition.unit,
      digits: definition.digits,
      axis: definition.axis,
      reference: typeof definition.reference === "function" ? definition.reference(active) : null,
      series,
    };
  }

  function cpuFrequencyTimelineLane(active) {
    if (!captureFeatureEnabled(active, "cpu_frequency") || activePlatform(active) === "ios") return null;
    const rows = Array.isArray(active?.series) ? active.series : [];
    const clusters = orderedCpuClusters(active);
    const clusterMap = new Map(clusters.map(cluster => [String(cluster.name || ""), cluster]));
    const keys = [];
    clusters.forEach(cluster => {
      const name = String(cluster.name || "");
      if (name && !keys.includes(name)) keys.push(name);
    });
    rows.forEach(row => {
      Object.keys(row?.frequencies_mhz || {}).forEach(name => {
        if (!keys.includes(name)) keys.push(name);
      });
    });
    const series = keys.map((name, index) => {
      const cluster = clusterMap.get(name);
      const clusterIndex = cluster ? clusters.indexOf(cluster) : index;
      const displayName = cluster
        ? cpuClusterDisplayName(cluster, clusterIndex, clusters)
        : `频率组 ${index + 1}`;
      const coreLabel = cluster ? cpuCoreGroupLabel(cluster) : name;
      return {
        key: `cpu-frequency-${name}`,
        label: `${displayName} ${coreLabel}`,
        color: liveTimelinePalette[index % liveTimelinePalette.length],
        points: timelinePoints(rows, row => row?.frequencies_mhz?.[name], { positive: true }),
      };
    }).filter(item => item.points.length);
    if (!series.length) return null;
    const maximum = Math.max(0, ...clusters.map(cluster => Number(cluster.maximum_mhz || 0)));
    const frequencyCadence = Number(
      active?.metadata?.sampling_schedule_s?.cpu_frequency
      || active?.sampling_schedule_s?.cpu_frequency
      || 0
    );
    return {
      key: "cpu_frequency",
      title: "CPU 核心组频率",
      source: activePlatform(active) === "harmony"
        ? `hidumper --cpufreq · 按最大频率分组均值${frequencyCadence > 1.5 ? ` · 约 ${frequencyCadence.toFixed(0)} 秒刷新，中间点保持最近值` : ""}`
        : "cpufreq policy 共享频率",
      unit: "MHz",
      digits: 0,
      axis: maximum > 0
        ? { fixedMin: 0, fixedMax: maximum, tickDigits: 0 }
        : { fixedMin: 0, minSpan: 1000, padding: .08, tickDigits: 0 },
      reference: null,
      series,
    };
  }

  function frameTimingTimelineLane(active) {
    if (!captureFeatureEnabled(active, "frame_rate")) return null;
    const rows = Array.isArray(active?.performance_series) ? active.performance_series : [];
    const series = [
      {
        key: "frame-time-p95",
        label: "P95",
        color: "#9e8cff",
        points: timelinePoints(rows, row => finite(row?.frame_time_p95_ms) ? Number(row.frame_time_p95_ms) : null, { positive: true }),
      },
      {
        key: "frame-time-p99",
        label: "P99",
        color: "#ff657d",
        points: timelinePoints(rows, row => finite(row?.frame_time_p99_ms) ? Number(row.frame_time_p99_ms) : null, { positive: true }),
        dashed: true,
      },
    ].filter(item => item.points.length);
    if (!series.length) return null;
    return {
      key: "frame_time_ms",
      title: "帧耗时",
      source: liveMetricSource(active, "frame_time_ms"),
      unit: "ms",
      digits: 2,
      axis: metricDefinitions.frame_time_ms.axis,
      reference: metricDefinitions.frame_time_ms.reference(active),
      series,
    };
  }

  function frameIssueTimelineLane(active) {
    if (!captureFeatureEnabled(active, "frame_rate")) return null;
    const rows = Array.isArray(active?.performance_series) ? active.performance_series : [];
    const points = timelinePoints(rows, row => finite(row?.frame_issue_pct) ? Number(row.frame_issue_pct) : null);
    if (!points.length) return null;
    return {
      key: "frame_issue_pct",
      title: "异常帧占比",
      source: active?.performance?.frame_issue_label || "截止时间 / VSync 统计",
      unit: "%",
      digits: 2,
      axis: { fixedMin: 0, minSpan: 2, padding: .1, tickDigits: 1 },
      reference: null,
      series: [{ key: "frame-issue", label: "异常帧", color: "#ff657d", points }],
    };
  }

  function frameFlowTimelineLanes(active) {
    if (!captureFeatureEnabled(active, "frame_rate")) return [];
    const flow = active?.performance?.frame_flow || {};
    const primaryKey = String(flow.primary_key || "");
    const stages = Array.isArray(flow.stages) ? flow.stages : [];
    return stages.flatMap((stage, index) => {
      const key = String(stage?.key || `stage-${index}`);
      if (key === primaryKey || key === "display_scanout") return [];
      const unit = String(stage?.timeline_unit || stage?.unit || "FPS");
      const points = timelinePoints(
        stage?.timeline,
        row => finite(row?.value)
          ? Number(row.value)
          : finite(row?.frame_rate_fps) ? Number(row.frame_rate_fps) : null,
      );
      if (!points.length || !points.some(point => point.value > 0)) return [];
      return [{
        key: `frame-flow-${key}`,
        title: `渲染链路 · ${stage.label || key}`,
        source: stage.source || "平台帧链路计数",
        unit,
        digits: unit === "ms" ? 2 : 1,
        axis: { fixedMin: 0, minSpan: unit === "ms" ? 10 : 30, padding: .08, tickDigits: unit === "ms" ? 1 : 0 },
        reference: null,
        series: [{
          key: `frame-flow-${key}-series`,
          label: stage.timeline_value_label || stage.value_label || stage.label || key,
          color: liveTimelinePalette[(index + 2) % liveTimelinePalette.length],
          points,
          dashed: ["reference", "invalid"].includes(String(stage.status || "")),
          step: key === "display_scanout",
        }],
      }];
    });
  }

  function liveTimelineLanes(active) {
    if (!active) return [];
    const powerFlow = powerFlowPresentation(active);
    const refreshRows = Array.isArray(active?.performance?.refresh_rate_timeline)
      && active.performance.refresh_rate_timeline.length
      ? active.performance.refresh_rate_timeline
      : active?.performance_series;
    const frameFlowLanes = frameFlowTimelineLanes(active);
    return [
      metricTimelineLane(active, "cpu_pct", { feature: "cpu_usage", seriesLabel: "CPU 总负载" }),
      cpuFrequencyTimelineLane(active),
      metricTimelineLane(active, "frame_rate_fps", { feature: "frame_rate", seriesLabel: "渲染帧率" }),
      ...frameFlowLanes,
      metricTimelineLane(active, "refresh_rate_hz", {
        enabled: captureFeatureEnabled(active, "foreground_window") || captureFeatureEnabled(active, "frame_rate"),
        source: refreshRows,
        seriesLabel: "显示刷新率",
        step: true,
      }),
      frameTimingTimelineLane(active),
      frameIssueTimelineLane(active),
      metricTimelineLane(active, "gpu_load_pct", { feature: "gpu_metrics", seriesLabel: "GPU 负载" }),
      metricTimelineLane(active, "gpu_frequency_mhz", { feature: "gpu_metrics", seriesLabel: "GPU 频率", positive: true }),
      metricTimelineLane(active, "memory_frequency_mhz", { feature: "memory_frequency", seriesLabel: "内存频率", positive: true }),
      metricTimelineLane(active, "power_mw", {
        title: powerFlow.chartPowerTitle,
        seriesLabel: powerFlow.chartPowerTitle,
        sourceLabel: liveMetricSource(active, "power_mw"),
      }),
      metricTimelineLane(active, "current_ma", {
        title: powerFlow.chartCurrentTitle,
        seriesLabel: powerFlow.chartCurrentTitle,
        sourceLabel: liveMetricSource(active, "current_ma"),
      }),
      metricTimelineLane(active, "voltage_mv", { seriesLabel: "电池电压", positive: true }),
      metricTimelineLane(active, "temperature_c", { feature: "thermal", seriesLabel: "电池温度" }),
    ].filter(Boolean);
  }

  function timelineGapLimit(coordinates) {
    const intervals = coordinates.slice(1)
      .map((point, index) => point.elapsed - coordinates[index].elapsed)
      .filter(value => value > 0)
      .sort((left, right) => left - right);
    const medianInterval = intervals.length ? intervals[Math.floor(intervals.length / 2)] : 0;
    return Math.max(3, medianInterval * 5);
  }

  function timelinePath(coordinates, step = false) {
    if (!coordinates.length) return "";
    const gapLimit = timelineGapLimit(coordinates);
    let path = `M${coordinates[0].x.toFixed(2)},${coordinates[0].y.toFixed(2)}`;
    for (let index = 1; index < coordinates.length; index += 1) {
      const point = coordinates[index];
      const previous = coordinates[index - 1];
      if (point.breakBefore || point.elapsed - previous.elapsed > gapLimit) {
        path += ` M${point.x.toFixed(2)},${point.y.toFixed(2)}`;
      } else if (step) {
        path += ` H${point.x.toFixed(2)} V${point.y.toFixed(2)}`;
      } else {
        path += ` L${point.x.toFixed(2)},${point.y.toFixed(2)}`;
      }
    }
    return path;
  }

  function compactLaneTicks(scale) {
    if (scale.ticks.length <= 3) return scale.ticks;
    return [...new Set([
      scale.ticks[0],
      scale.ticks[Math.floor((scale.ticks.length - 1) / 2)],
      scale.ticks.at(-1),
    ])];
  }

  function truncateTimelineLabel(value, length) {
    const text = String(value || "");
    return text.length > length ? `${text.slice(0, Math.max(1, length - 1))}…` : text;
  }

  function normalizeLiveTimeRange(value, maximumTime = app.chartGeometry?.maxTime) {
    if (!Array.isArray(value) || value.length < 2 || !finite(maximumTime)) return null;
    const limit = Math.max(0, Number(maximumTime));
    const start = clamp(Math.min(Number(value[0]), Number(value[1])), 0, limit);
    const end = clamp(Math.max(Number(value[0]), Number(value[1])), 0, limit);
    return finite(start) && finite(end) && end > start ? [start, end] : null;
  }

  function activeLiveTimeRange() {
    return normalizeLiveTimeRange(
      app.liveTimeRangeDraft || app.liveTimeRange,
      app.chartGeometry?.maxTime,
    );
  }

  function liveSeriesRangeStatistics(series, start, end) {
    const points = (Array.isArray(series?.points) ? series.points : [])
      .filter(point => finite(point.elapsed) && finite(point.value))
      .sort((left, right) => Number(left.elapsed) - Number(right.elapsed));
    const values = points
      .filter(point => point.elapsed >= start && point.elapsed <= end && finite(point.value))
      .map(point => Number(point.value));
    let weightedTotal = 0;
    let coveredDuration = 0;
    const boundaryValues = [];
    const gapLimit = timelineGapLimit(points);
    for (let index = 0; index < points.length - 1; index += 1) {
      const previous = points[index];
      const current = points[index + 1];
      const delta = current.elapsed - previous.elapsed;
      if (
        delta <= 0
        || delta > gapLimit
        || current.breakBefore
        || (finite(previous.breakAfterElapsed) && Number(previous.breakAfterElapsed) <= current.elapsed)
      ) continue;
      const overlapStart = Math.max(start, previous.elapsed);
      const overlapEnd = Math.min(end, current.elapsed);
      if (overlapEnd <= overlapStart) continue;
      const duration = overlapEnd - overlapStart;
      let leftValue = Number(previous.value);
      let rightValue = Number(previous.value);
      if (!series.step) {
        const leftRatio = (overlapStart - previous.elapsed) / delta;
        const rightRatio = (overlapEnd - previous.elapsed) / delta;
        leftValue += (Number(current.value) - Number(previous.value)) * leftRatio;
        rightValue += (Number(current.value) - Number(previous.value)) * rightRatio;
      }
      weightedTotal += (leftValue + rightValue) * .5 * duration;
      coveredDuration += duration;
      boundaryValues.push(leftValue, rightValue);
    }
    const latest = points.at(-1);
    if (
      series.step
      && latest
      && latest.elapsed < end
      && !finite(latest.breakAfterElapsed)
    ) {
      const overlapStart = Math.max(start, latest.elapsed);
      if (end > overlapStart) {
        const duration = end - overlapStart;
        weightedTotal += Number(latest.value) * duration;
        coveredDuration += duration;
        boundaryValues.push(Number(latest.value));
      }
    }
    const observedValues = [...values, ...boundaryValues];
    if (!observedValues.length) return null;
    return {
      count: values.length,
      average: coveredDuration > 0
        ? weightedTotal / coveredDuration
        : observedValues.reduce((sum, value) => sum + value, 0) / observedValues.length,
      minimum: Math.min(...observedValues),
      maximum: Math.max(...observedValues),
      coveredDuration,
      calculation: coveredDuration > 0 ? "preview_time_weighted" : "preview_sample_average",
    };
  }

  function liveRangeCalculationLabel(summary) {
    const labels = {
      time_weighted_full_resolution: "完整数据时间加权均值",
      sample_average_full_resolution: "完整数据样本均值",
      frame_rate_recomputed: "选区帧率重算",
      one_percent_low_recomputed: "选区 1% Low 重算",
      frame_quantile_recomputed: "选区分位值重算",
      frame_issue_ratio_recomputed: "选区异常比例重算",
      refresh_residency_weighted: "选区驻留时间加权",
      frame_stage_full_resolution: "完整链路样本均值",
      preview_time_weighted: "拖动预览 · 时间加权",
      preview_sample_average: "拖动预览 · 样本均值",
    };
    return labels[String(summary?.calculation || "")] || "区间均值";
  }

  function exactLiveRangeSummary(series, range) {
    const summary = app.liveRangeSummary;
    if (!summary?.full_resolution || !Array.isArray(range)) return null;
    const tolerance = Math.max(.02, (range[1] - range[0]) * .0001);
    if (
      Math.abs(Number(summary.start_elapsed_s) - range[0]) > tolerance
      || Math.abs(Number(summary.end_elapsed_s) - range[1]) > tolerance
    ) return null;
    return summary.metrics?.[series.key] || null;
  }

  function scheduleLiveTimeRangePresentation() {
    if (app.liveRangePresentationFrame) return;
    app.liveRangePresentationFrame = window.requestAnimationFrame(() => {
      app.liveRangePresentationFrame = 0;
      renderLiveTimeRangePresentation();
    });
  }

  async function loadLiveRangeSummary(range) {
    const normalized = normalizeLiveTimeRange(range, app.chartGeometry?.maxTime);
    if (!normalized) return;
    const requestId = ++app.liveRangeSummaryRequestId;
    app.liveRangeSummary = null;
    renderLiveTimeRangePresentation();
    try {
      const result = await api("/api/range-summary", {
        method: "POST",
        body: JSON.stringify({
          start_elapsed_s: normalized[0],
          end_elapsed_s: normalized[1],
        }),
      });
      if (requestId !== app.liveRangeSummaryRequestId) return;
      app.liveRangeSummary = result;
    } catch (_error) {
      if (requestId !== app.liveRangeSummaryRequestId) return;
      app.liveRangeSummary = null;
    }
    renderLiveTimeRangePresentation();
  }

  function renderLiveTimeRangePresentation() {
    const geometry = app.chartGeometry;
    const range = activeLiveTimeRange();
    const status = $("#live-range-status");
    const statistics = $("#live-range-statistics");
    const clearButton = $("#live-range-clear");
    if (!status || !statistics || !clearButton) return;

    const selectionNodes = geometry?.selectionNodes;
    if (!geometry || !range) {
      selectionNodes?.window.setAttribute("opacity", "0");
      selectionNodes?.start.setAttribute("opacity", "0");
      selectionNodes?.end.setAttribute("opacity", "0");
      clearButton.disabled = true;
      statistics.classList.add("hidden");
      statistics.replaceChildren();
      status.textContent = geometry
        ? "在任意图表中按住并横向拖动，查看该区间的样本均值、最小值与最大值。"
        : "等待有效时间序列后即可框选分析。";
      return;
    }

    const [start, end] = range;
    const startX = geometry.xFor(start);
    const endX = geometry.xFor(end);
    selectionNodes.window.setAttribute("x", String(startX));
    selectionNodes.window.setAttribute("width", String(Math.max(1, endX - startX)));
    selectionNodes.window.setAttribute("opacity", "1");
    selectionNodes.start.setAttribute("x1", String(startX));
    selectionNodes.start.setAttribute("x2", String(startX));
    selectionNodes.start.setAttribute("opacity", "1");
    selectionNodes.end.setAttribute("x1", String(endX));
    selectionNodes.end.setAttribute("x2", String(endX));
    selectionNodes.end.setAttribute("opacity", "1");
    clearButton.disabled = false;
    const exactAvailable = geometry.lanes.some(lane => (
      lane.series.some(series => exactLiveRangeSummary(series, range))
    ));
    status.textContent = `${formatDuration(start)} – ${formatDuration(end)} · ${formatDuration(end - start)} · ${exactAvailable ? "完整采集数据统计" : app.liveTimeRangeDraft ? "拖动预览" : "正在计算完整数据，当前显示图表预览"}`;

    const cards = geometry.lanes.map(lane => {
      const rows = lane.series.map(series => ({
        series,
        summary: exactLiveRangeSummary(series, range)
          || liveSeriesRangeStatistics(series, start, end),
      })).filter(item => item.summary);
      if (!rows.length) return "";
      return `<article><strong>${escapeHtml(lane.title)}</strong>${rows.map(({ series, summary }) => (
        `<span><em>${escapeHtml(series.label)} · ${escapeHtml(liveRangeCalculationLabel(summary))}</em><b>${Number(summary.average).toFixed(lane.digits)} ${escapeHtml(lane.unit)}</b></span>`
        + `<small>${Number(summary.sample_count ?? summary.count ?? 0).toLocaleString("zh-CN")} 点${finite(summary.covered_duration_s ?? summary.coveredDuration) ? ` · 覆盖 ${formatDuration(Number(summary.covered_duration_s ?? summary.coveredDuration))}` : ""} · ${Number(summary.minimum).toFixed(lane.digits)}–${Number(summary.maximum).toFixed(lane.digits)} ${escapeHtml(lane.unit)}</small>`
      )).join("")}</article>`;
    }).filter(Boolean);
    statistics.innerHTML = cards.length
      ? cards.join("")
      : '<article><strong>选中区间暂无有效样本</strong><small>尝试扩大框选范围，或确认对应采集项已经产生数据。</small></article>';
    statistics.classList.remove("hidden");
  }

  function liveElapsedForPointer(event, geometry = app.chartGeometry, allowOutside = true) {
    const wrap = $("#chart-wrap");
    if (!wrap || !geometry) return null;
    const rect = wrap.getBoundingClientRect();
    if (!rect.width) return null;
    const viewX = ((event.clientX - rect.left) / rect.width) * geometry.width;
    if (
      !allowOutside
      && (viewX < geometry.margins.left || viewX > geometry.width - geometry.margins.right)
    ) return null;
    const plotX = clamp(viewX, geometry.margins.left, geometry.width - geometry.margins.right);
    return clamp(
      (plotX - geometry.margins.left) / Math.max(1, geometry.plotWidth) * geometry.maxTime,
      geometry.minTime,
      geometry.maxTime,
    );
  }

  function renderChart() {
    const svg = $("#live-chart");
    const empty = $("#chart-empty");
    const active = app.state?.active;
    const availableLanes = liveTimelineLanes(active);
    renderLiveTimelineConfiguration(active, availableLanes);
    const lanes = applyLiveTimelineLayout(active, availableLanes);
    svg.replaceChildren();
    if (!lanes.length) {
      svg.style.height = "260px";
      svg.setAttribute("viewBox", "0 0 1000 260");
      empty.classList.remove("hidden");
      const allHidden = availableLanes.length > 0;
      empty.querySelector("strong").textContent = allHidden
        ? "已隐藏所有图表"
        : active ? active.running ? "等待遥测数据" : "本次没有有效时间序列" : "等待遥测数据";
      empty.querySelector("small").textContent = allHidden
        ? "在“显示内容与顺序”中重新打开需要的数据"
        : active && !active.running
          ? "只展示本次实际采到的数据；空曲线不会占位"
          : "开始采集后，已开启且有效的曲线将在这里实时更新";
      $("#live-timeline-source").textContent = allHidden
        ? `0 / ${availableLanes.length} 条数据泳道正在显示`
        : active
          ? active.running ? "已开启项目尚无有效时间序列" : "本次没有有效时间序列"
          : "等待采集配置与时间序列";
      app.chartGeometry = null;
      renderLiveTimeRangePresentation();
      return;
    }

    empty.classList.add("hidden");
    const width = Math.max(320, Math.round(svg.clientWidth || 1000));
    const compact = width < 620;
    const laneHeight = compact ? 112 : 106;
    const margins = {
      left: compact ? 104 : 142,
      right: compact ? 14 : 22,
      top: 8,
      bottom: compact ? 40 : 42,
    };
    const height = margins.top + lanes.length * laneHeight + margins.bottom;
    const plotWidth = width - margins.left - margins.right;
    const maxPointElapsed = Math.max(0, ...lanes.flatMap(lane => lane.series.flatMap(item => item.points.map(point => point.elapsed))));
    const minTime = 0;
    const maxTime = Math.max(1, Number(active?.elapsed_s || 0), maxPointElapsed);
    const xFor = elapsed => margins.left + (Number(elapsed) / maxTime) * plotWidth;
    svg.style.height = `${height}px`;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute(
      "aria-label",
      `${lanes.length} 条实时数据泳道，共用 ${formatDuration(minTime)} 到 ${formatDuration(maxTime)} 的时间横轴；${lanes.map(lane => lane.title).join("、")}`,
    );
    $("#live-timeline-source").textContent = `${lanes.length} / ${availableLanes.length} 条数据泳道 · ${formatDuration(minTime)}–${formatDuration(maxTime)} · 自定义显示顺序`;

    const backgrounds = svgNode("g", { class: "timeline-backgrounds" });
    lanes.forEach((lane, index) => {
      const y = margins.top + index * laneHeight;
      backgrounds.appendChild(svgNode("rect", {
        x: 0,
        y,
        width,
        height: laneHeight,
        class: `timeline-lane-bg${index % 2 ? " alternate" : ""}`,
      }));
      backgrounds.appendChild(svgNode("line", {
        x1: 0,
        y1: y + laneHeight,
        x2: width,
        y2: y + laneHeight,
        class: "timeline-lane-separator",
      }));
    });
    svg.appendChild(backgrounds);

    const timeGrid = svgNode("g", { class: "timeline-time-grid" });
    const timeTicks = buildTimeTicks(minTime, maxTime, compact ? 3 : width < 900 ? 5 : 7);
    timeTicks.forEach((elapsed, index) => {
      const x = xFor(elapsed);
      timeGrid.appendChild(svgNode("line", {
        x1: x,
        y1: margins.top,
        x2: x,
        y2: height - margins.bottom,
        class: "timeline-time-line",
      }));
      timeGrid.appendChild(svgNode("text", {
        x,
        y: height - 14,
        "text-anchor": index === 0 ? "start" : index === timeTicks.length - 1 ? "end" : "middle",
        class: "timeline-time-label",
      }, formatDuration(elapsed)));
    });
    svg.appendChild(timeGrid);

    const geometryLanes = [];
    lanes.forEach((lane, laneIndex) => {
      const laneTop = margins.top + laneIndex * laneHeight;
      const plotTop = laneTop + 35;
      const plotBottom = laneTop + laneHeight - 15;
      const plotHeight = plotBottom - plotTop;
      const values = lane.series.flatMap(item => item.points.map(point => Number(point.value)));
      const scale = buildChartScale(values, { axis: lane.axis, digits: lane.digits });
      const yFor = value => plotTop + (1 - (Number(value) - scale.minimum) / Math.max(.001, scale.maximum - scale.minimum)) * plotHeight;
      const laneGroup = svgNode("g", { class: "timeline-lane" });
      laneGroup.appendChild(svgNode("title", {}, `${lane.title} · 数据来源：${lane.source}`));
      laneGroup.appendChild(svgNode("text", {
        x: 10,
        y: laneTop + 17,
        class: "timeline-lane-title",
      }, lane.title));
      laneGroup.appendChild(svgNode("text", {
        x: 10,
        y: laneTop + 31,
        class: "timeline-lane-source",
      }, truncateTimelineLabel(lane.source, compact ? 14 : 22)));

      compactLaneTicks(scale).forEach(value => {
        const y = yFor(value);
        laneGroup.appendChild(svgNode("line", {
          x1: margins.left,
          y1: y,
          x2: width - margins.right,
          y2: y,
          class: "timeline-value-grid",
        }));
        laneGroup.appendChild(svgNode("text", {
          x: margins.left - 7,
          y,
          "text-anchor": "end",
          "dominant-baseline": "middle",
          class: "timeline-value-label",
        }, formatAxisNumber(value, scale.tickDigits)));
      });

      let legendX = margins.left + 5;
      lane.series.forEach((item, index) => {
        const latest = item.points.at(-1);
        const configuredFreshLimit = Math.max(
          3,
          Number(active?.metadata?.sample_interval_s || active?.config?.interval_s || 1) * 4,
        );
        const current = latest && (
          !(finite(latest.breakAfterElapsed) && maxTime >= Number(latest.breakAfterElapsed))
          && (
            item.step
            || maxTime - latest.elapsed <= Math.min(timelineGapLimit(item.points), configuredFreshLimit)
          )
        )
          ? latest
          : null;
        const legend = current
          ? `${item.label} ${Number(current.value).toFixed(lane.digits)} ${lane.unit}`
          : `${item.label} 当前无新鲜样本`;
        const estimatedWidth = 24 + legend.length * (compact ? 5.2 : 5.8);
        if (legendX + estimatedWidth > width - margins.right && index > 0) return;
        laneGroup.appendChild(svgNode("line", {
          x1: legendX,
          y1: laneTop + 17,
          x2: legendX + 13,
          y2: laneTop + 17,
          class: `timeline-legend-line${item.dashed ? " secondary" : ""}`,
          style: `--lane-color:${item.color}`,
        }));
        laneGroup.appendChild(svgNode("text", {
          x: legendX + 18,
          y: laneTop + 20,
          class: "timeline-legend-label",
        }, legend));
        legendX += estimatedWidth;
      });

      if (lane.reference && finite(lane.reference.value)
        && Number(lane.reference.value) >= scale.minimum
        && Number(lane.reference.value) <= scale.maximum) {
        const referenceY = yFor(lane.reference.value);
        laneGroup.appendChild(svgNode("line", {
          x1: margins.left,
          y1: referenceY,
          x2: width - margins.right,
          y2: referenceY,
          class: "timeline-reference-line",
        }));
        laneGroup.appendChild(svgNode("text", {
          x: width - margins.right - 4,
          y: referenceY - 4,
          "text-anchor": "end",
          class: "timeline-reference-label",
        }, lane.reference.label));
      }

      const geometrySeries = [];
      lane.series.forEach(item => {
        const coordinates = item.points.map(point => ({
          ...point,
          x: xFor(point.elapsed),
          y: yFor(point.value),
        }));
        const path = timelinePath(coordinates, item.step);
        if (coordinates.length >= 2) {
          laneGroup.appendChild(svgNode("path", {
            d: path,
            class: `timeline-series-line${item.dashed ? " secondary" : ""}`,
            style: `--lane-color:${item.color}`,
          }));
        }
        const latest = coordinates.at(-1);
        laneGroup.appendChild(svgNode("circle", {
          cx: latest.x,
          cy: latest.y,
          r: item.dashed ? 2.8 : 3.4,
          class: "timeline-series-dot",
          style: `--lane-color:${item.color}`,
        }));
        geometrySeries.push({ ...item, coordinates });
      });
      geometryLanes.push({ ...lane, series: geometrySeries, plotTop, plotBottom, scale });
      svg.appendChild(laneGroup);
    });

    const dimPoints = Array.isArray(active?.brightness_throttling?.points)
      ? active.brightness_throttling.points
      : [];
    dimPoints.forEach(point => {
      const elapsed = Number(point.elapsed_s || 0);
      if (!finite(elapsed) || elapsed < minTime || elapsed > maxTime) return;
      const vendorKnown = point.vendor_thermal_active === true || point.vendor_thermal_active === false;
      const status = vendorKnown
        ? point.vendor_thermal_active === true ? "confirmed" : "suspected"
        : String(point.status || "suspected");
      const x = xFor(elapsed);
      const marker = svgNode("line", {
        x1: x,
        y1: margins.top,
        x2: x,
        y2: height - margins.bottom,
        class: `brightness-dim-marker ${status}`,
      });
      marker.appendChild(svgNode("title", {}, `${formatDuration(elapsed)} · ${status === "confirmed" ? "确认热降亮" : "疑似热降亮"} · ${point.reason || "显示侧热限制"}`));
      svg.appendChild(marker);
    });

    const selectionWindow = svgNode("rect", {
      x: margins.left,
      y: margins.top,
      width: 0,
      height: height - margins.top - margins.bottom,
      class: "timeline-range-window",
      opacity: 0,
    });
    const selectionStart = svgNode("line", {
      x1: margins.left,
      y1: margins.top,
      x2: margins.left,
      y2: height - margins.bottom,
      class: "timeline-range-boundary",
      opacity: 0,
    });
    const selectionEnd = svgNode("line", {
      x1: margins.left,
      y1: margins.top,
      x2: margins.left,
      y2: height - margins.bottom,
      class: "timeline-range-boundary",
      opacity: 0,
    });
    svg.append(selectionWindow, selectionStart, selectionEnd);

    const hoverLine = svgNode("line", {
      x1: margins.left,
      y1: margins.top,
      x2: margins.left,
      y2: height - margins.bottom,
      class: "timeline-hover-line",
      opacity: 0,
    });
    svg.appendChild(hoverLine);
    app.chartGeometry = {
      lanes: geometryLanes,
      width,
      height,
      margins,
      minTime,
      maxTime,
      plotWidth,
      xFor,
      hoverLine,
      selectionNodes: {
        window: selectionWindow,
        start: selectionStart,
        end: selectionEnd,
      },
    };
    renderLiveTimeRangePresentation();
  }

  function renderActive(active) {
    const isNewRun = active?.run_name !== app.currentRunName;
    if (isNewRun) {
      app.currentRunName = active?.run_name || null;
      app.consoleClearedAt = 0;
      app.notifiedWarnings.clear();
      app.notifiedBrightnessPoints.clear();
      app.liveTimeRange = null;
      app.liveTimeRangeDraft = null;
      app.liveTimeRangePointer = null;
      app.liveRangeSummary = null;
      app.liveRangeSummaryRequestId += 1;
    }
    if (isNewRun && active) {
      app.durationUnlimited = activeDurationUnlimited(active);
      syncDurationPreset();
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
        if (!app.durationUnlimited && finite(config.duration)) {
          $("#duration-input").value = String(config.duration);
        }
        [
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
    renderBrightnessThrottling(active);
    renderPerformanceMetrics(active);
    renderClusters(active);
    renderContext(active);
    renderPerformanceResources(active);
    renderPowerPressure(active);
    renderRenderPipeline(active);
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
    const pluggedStateAvailable = battery.plugged_state_available !== false
      && (platform !== "ios" || battery.external_power_state_available === true);
    const isUnplugged = pluggedStateAvailable && powered.length === 0;
    $("#probe-device-name").textContent = [device.brand, device.model].filter(Boolean).join(" ") || "Unknown device";
    $("#probe-device-detail").textContent = platform === "ios"
      ? `${device.product_type || device.device || "iPhone"} · ${device.hardware || "--"} · iOS ${device.ios || "--"} · ${device.cpu_count || "--"} CPU`
      : platform === "harmony"
        ? `${device.soc_model || device.hardware || "Unknown SoC"} · ${device.hardware || "--"} · HarmonyOS ${device.harmony || device.openharmony || "--"}`.trim()
        : `${device.soc_manufacturer || ""} ${device.soc_model || device.hardware || "Unknown SoC"} · ${device.hardware || "--"}/${device.board_platform || "--"} · Android ${device.android || "--"}`.trim();
    $("#probe-serial").textContent = data.device?.serial || entry.device || "--";
    $("#probe-power-state").textContent = !pluggedStateAvailable
      ? "外部供电状态无法确认"
      : isUnplugged ? "电池供电" : `外部供电：${powered.join(", ")}`;
    $("#probe-current-state").textContent = `Current command: ${data.current_command || "unavailable"} · ${battery.voltage_mv ? `${battery.voltage_mv} mV` : "voltage n/a"}`;
    const powerBadge = $("#probe-power-badge");
    powerBadge.textContent = !pluggedStateAvailable
      ? "STATE UNKNOWN"
      : isUnplugged && data.current_command_ok ? "READY" : "CHECK POWER";
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
    const probeScreenState = String(
      data.screen_state || data.performance?.screen_state || ""
    ).toLowerCase();
    const probeScreenInactive = probeScreenState === "asleep"
      || probeScreenState === "off"
      || probeScreenState.startsWith("doze");
    const lastKnownForeground = data.last_known_foreground_package
      || data.performance?.last_known_foreground_package;
    $("#probe-foreground").textContent = data.foreground_package
      || (probeScreenInactive && lastKnownForeground
        ? `无当前前台 · 最近 ${lastKnownForeground}`
        : probeScreenInactive ? "无当前前台 · 屏幕未亮" : "Unknown");

    const policies = Array.isArray(data.cpu_policies) ? data.cpu_policies : [];
    $("#probe-cpu-table").innerHTML = policies.length ? policies.map(policy => `<div class="capability-row">
      <strong>${escapeHtml(policy.label || policy.name)}</strong>
      <span>cores ${(policy.cores || []).join(", ") || "--"}${policy.governor ? ` · ${escapeHtml(policy.governor)}` : ""}${policy.core_control?.min_cpus != null ? ` · core_ctl ${escapeHtml(policy.core_control.min_cpus)}–${escapeHtml(policy.core_control.max_cpus)}` : ""}</span>
      <span>${finite(policy.min_khz) ? `${(Number(policy.min_khz) / 1000).toFixed(0)} MHz min` : "min n/a"}</span>
      <span>${finite(policy.max_khz) ? `${(Number(policy.max_khz) / 1000).toFixed(0)} MHz max` : "max n/a"}</span>
    </div>`).join("") : '<div class="empty-row">未识别到 cpufreq policy</div>';

    const iosAppStateStatus = String(data.capabilities?.application_state_notifications_status || "");
    const iosAppStateAvailable = data.capabilities?.application_state_notifications === true
      ? true
      : data.capabilities?.application_state_notifications === false
        ? false
        : null;
    const iosProbeDevice = data.selected_device || data.device || {};
    const iosRemoteReady = iosRemoteXpcReady(iosProbeDevice)
      && Boolean(data.connection?.host)
      && finite(data.connection?.port);
    const iosUnpluggedReady = iosUnplugReady(iosProbeDevice) && iosRemoteReady;
    const iosProbeEndpointScope = String(
      data.connection?.endpoint_scope
      || iosProbeDevice.endpoint_scope
      || "unknown"
    );
    const iosProbeTransportLabel = iosWirelessTransportLabel(
      data.connection || iosProbeDevice
    );
    const harmonyScreenState = String(
      data.power_state?.screen_state
      || data.display?.screen_state
      || data.display?.display_power_state
      || ""
    ).toLowerCase();
    const harmonyDisplayActive = harmonyScreenState === "on" || harmonyScreenState === "awake";
    const harmonyCompositorAvailable = harmonyDisplayActive && finite(data.performance?.compositor_fps);
    const features = platform === "ios" ? [
      ["整机原始 SystemLoad", "DiagnosticsService 原始通道；外供时可能接近 SystemPowerIn，非电池 I×V", Boolean(data.power_telemetry_available)],
      ["进程 CPU / 相对功耗分数", "DVT sysmontap；powerScore 不换算为 mW", Boolean(data.capabilities?.process_cpu)],
      ["GPU 利用率", "仅 DVT Graphics 利用率事件，不含 GPU 频率", Boolean(data.capabilities?.gpu_utilization)],
      ["前台应用状态", iosAppStateStatus === "not_probed" ? "采集期间确认 DVT Running / Suspended；不含显示参数" : "DVT Running / Suspended；不含分辨率、亮度和刷新率", iosAppStateAvailable],
      ["RemoteXPC telemetry", iosRemoteReady
        ? `${iosProbeTransportLabel} · ${data.connection.host}:${data.connection.port}`
        : "当前仅 USB Probe；正式录制需可达的 RemotePairing 端点", Boolean(data.capabilities?.remote_xpc && iosRemoteReady)],
      ["Unplugged power-test link", iosUnpluggedReady
        ? `${iosProbeTransportLabel} · ${data.connection.host}:${data.connection.port} · 拔线后在线`
        : iosRemoteReady
          ? `${iosProbeEndpointScope} · 当前端点不能证明拔掉 USB 后仍可达`
          : "未验证", iosUnpluggedReady],
      ["电池温度", `${data.system_monitor?.thermal_sensor_count || 0} 个温度源；不含热严重度、热限制或热降亮`, Boolean(data.system_monitor?.thermalservice_available)],
    ] : platform === "harmony" ? [
      ["Battery current / voltage", "HarmonyOS BatteryService", Boolean(data.capabilities?.battery_service && data.current_command_ok)],
      ["CPU utilization", "persistent HDC /proc/stat", Boolean(data.capabilities?.proc_stat)],
      ["CPU frequency", "hidumper --cpufreq", Boolean(data.capabilities?.cpufreq)],
      ["SmartPerf native capture", data.smartperf?.command || "SP_daemon", Boolean(data.capabilities?.smartperf_daemon)],
      ["Device high-performance mode", `current ${data.power_mode?.current_mode || "--"} · power-shell 602`, Boolean(data.capabilities?.performance_power_mode)],
      ["Foreground Ability", data.foreground_package || "AbilityManager", Boolean(data.capabilities?.ability_manager)],
      ["Display power state", harmonyScreenState || "unknown", harmonyDisplayActive],
      ["Display refresh modes", `${data.display?.refresh_rate_hz || "--"} Hz · ${(data.display?.supported_refresh_rates_hz || []).join("/") || "modes n/a"}（亮屏时才作为当前扫描节奏）`, Boolean(data.capabilities?.display_modes)],
      ["Brightness read / control", `${data.brightness?.setting_raw ?? "--"} → device ${data.brightness?.effective_raw ?? "--"} · discount ${finite(data.brightness?.brightness_discount) ? Number(data.brightness.brightness_discount).toFixed(3) : "--"}×`, Boolean(data.capabilities?.brightness_control)],
      ["Compositor frame pacing", harmonyDisplayActive ? `${formatNumber(data.performance?.compositor_fps, 1)} FPS sampled` : "屏幕未亮；不采用历史 RenderService 计数作为当前 FPS", harmonyCompositorAvailable],
      ["Foreground window", data.performance?.foreground_window_name || "WindowManagerService", Boolean(data.capabilities?.window_manager)],
      ["Touch devices", `${data.performance?.touch_device_count || 0} devices · axes/events`, Boolean(data.capabilities?.touch_devices)],
      ["Touch hardware sampling rate", data.touch?.sampling_rate_reason || "not exposed", false],
      ["GPU renderer", data.performance?.gpu_renderer || data.gpu_probe?.model || "renderer n/a", Boolean(data.capabilities?.gpu_renderer)],
      ["Temperature sensors", `${data.system_monitor?.thermal_sensor_count || 0} sensors`, Boolean(data.capabilities?.thermal_service)],
      ["Background interference", "HarmonyOS top + ps", Boolean(data.capabilities?.process_top)],
      ["GPU frequency / load", data.gpu_probe?.reason || "HDC permission restricted", Boolean(data.capabilities?.smartperf_gpu_metrics)],
      ["DDR frequency", data.capabilities?.smartperf_ddr_frequency_reason
        || "SmartPerf -d；只有实际返回 ddrFrequency 后才确认并进入曲线", Boolean(data.capabilities?.smartperf_ddr_frequency)],
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
      ["Foreground frame rate interface", probeScreenInactive
        ? "屏幕未亮；仅确认接口能力，不采用历史累计计数作为当前 FPS"
        : data.performance?.surface_layer_name
          ? "SurfaceFlinger foreground application-layer present timestamps"
          : "gfxinfo foreground-window counters", Boolean(data.capabilities?.frame_rate)],
      ["System process monitor", "top + ps for apps/services/kernel", Boolean(data.system_monitor?.process_top_available)],
      ["ThermalService history", `${data.system_monitor?.thermal_sensor_count || 0} sensors / ${data.system_monitor?.thermal_threshold_count || 0} thresholds`, Boolean(data.system_monitor?.thermalservice_available)],
      ["cpuset / ADPF", `${Object.keys(data.system_monitor?.cpusets || {}).length} cpusets / ${data.system_monitor?.adpf_active_session_count || 0} active sessions`, Boolean(data.system_monitor?.adpf_available)],
      ["WALT / core_ctl", `${(data.system_monitor?.cpu_policies || []).map(item => item.governor).filter(Boolean).join(", ") || "governor n/a"} / ${(data.system_monitor?.cpu_policies || []).filter(item => item.core_ctl_min_cpus != null).length} policies`, (data.system_monitor?.cpu_policies || []).some(item => item.governor === "walt" || item.core_ctl_min_cpus != null)],
    ];
    $("#probe-feature-list").innerHTML = features.map(([name, detail, available]) => `<div class="feature-row"><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></span><i class="feature-state ${available === true ? "good" : available === false ? "warn" : ""}"></i></div>`).join("");
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

  function defaultAgentTask(overrides = {}) {
    const defaults = app.state?.adb_agent?.defaults || {};
    return {
      id: "",
      name: "",
      prompt: "",
      attention_prompt: "",
      max_steps: Number(defaults.max_steps || 30),
      timeout_s: Number(defaults.task_timeout_s || 300),
      on_failure: "stop",
      action_limits: [],
      ...overrides,
    };
  }

  function normalizeAgentActionLimits(value) {
    const parsed = typeof value === "string"
      ? (() => { try { return JSON.parse(value); } catch (_error) { return []; } })()
      : value;
    return (Array.isArray(parsed) ? parsed : []).map(item => ({
      actions: [...new Set((Array.isArray(item?.actions) ? item.actions : [])
        .map(action => String(action || "").trim().toLowerCase())
        .filter(Boolean))],
      maximum: Math.max(0, Math.min(200, Number(item?.maximum ?? 1))),
      ...(item?.maximum_per_signature == null ? {} : {
        maximum_per_signature: Math.max(
          1,
          Math.min(200, Number(item.maximum_per_signature || 1)),
        ),
      }),
      label: String(item?.label || "").trim(),
    })).filter(item => item.actions.length);
  }

  function setAgentTemporaryTaskMenu(open, { focusInput = false, returnFocus = false } = {}) {
    const menu = $("#agent-temporary-task-menu");
    const trigger = $("#agent-temporary-task-menu-button");
    if (!menu || !trigger) return;
    const nextOpen = Boolean(open);
    menu.hidden = !nextOpen;
    trigger.classList.toggle("active", nextOpen);
    trigger.setAttribute("aria-expanded", String(nextOpen));
    trigger.setAttribute("aria-label", nextOpen ? "关闭手机临时任务" : "打开手机临时任务");
    if (nextOpen && focusInput) $("#agent-temporary-task-input")?.focus();
    if (!nextOpen && returnFocus) trigger.focus();
  }

  function setAgentConfigTab(tab, { persist = true } = {}) {
    const nextTab = agentConfigTabs.includes(tab) ? tab : "workflow";
    const presentation = {
      workflow: { title: "任务启动", source: "TASK LAUNCH" },
      campaign: { title: "阶段配置", source: "CAMPAIGN JSON" },
      software: { title: "应用与游戏", source: "SUPPORTED SOFTWARE" },
      model: { title: "模型调度", source: "MODEL ROUTING" },
      prompt: {
        title: "Prompt 编辑",
        source: app.agentPromptTab === "system"
          ? "SYSTEM PROMPT"
          : app.agentPromptTab === "task"
            ? "TASK PROMPT"
            : "WORKFLOW DESIGN",
      },
    }[nextTab];
    app.agentConfigTab = nextTab;
    $$('[data-agent-config-tab]').forEach(button => {
      const active = button.dataset.agentConfigTab === nextTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $$('[data-agent-config-view]').forEach(view => {
      const active = view.dataset.agentConfigView === nextTab;
      view.hidden = !active;
      view.classList.toggle("active", active);
    });
    $("#agent-config-panel-title").textContent = presentation.title;
    $("#agent-config-panel-source").textContent = presentation.source;
    if (nextTab === "campaign") {
      renderAgentCampaignConfig(app.state?.campaign?.stage_config || {});
    }
    if (nextTab === "prompt") setAgentPromptTab(app.agentPromptTab, { persist: false });
    if (persist) localStorage.setItem(agentConfigTabStorageKey, nextTab);
  }

  function setAgentRunConsoleSection(section, { focus = false } = {}) {
    const sections = ["decision", "results", "logs"];
    const nextSection = sections.includes(section) ? section : "decision";
    app.agentRunConsoleSection = nextSection;
    $$('[data-agent-run-tab]').forEach(button => {
      const active = button.dataset.agentRunTab === nextSection;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $$('[data-agent-run-view]').forEach(view => {
      const active = view.dataset.agentRunView === nextSection;
      view.hidden = !active;
      view.classList.toggle("active", active);
    });
    if (focus) $(`[data-agent-run-tab="${nextSection}"]`)?.focus();
  }

  function setAgentPromptTab(tab, { persist = true } = {}) {
    const nextTab = agentPromptTabs.includes(tab) ? tab : "workflow";
    app.agentPromptTab = nextTab;
    $$('[data-agent-prompt-tab]').forEach(button => {
      const active = button.dataset.agentPromptTab === nextTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $$('[data-agent-prompt-view]').forEach(view => {
      const active = view.dataset.agentPromptView === nextTab;
      view.hidden = !active;
      view.classList.toggle("active", active);
    });
    if (app.agentConfigTab === "prompt") {
      $("#agent-config-panel-source").textContent = nextTab === "system"
        ? "SYSTEM PROMPT"
        : nextTab === "task"
          ? "TASK PROMPT"
          : "WORKFLOW DESIGN";
    }
    if (nextTab === "workflow") renderAgentWorkflowTaskEditor();
    if (nextTab === "task") renderAgentTaskPromptEditor();
    if (persist) localStorage.setItem(agentPromptTabStorageKey, nextTab);
  }

  function refreshAgentModelSummary() {
    const engineInput = $("#agent-automation-engine-input");
    const engineProfile = app.agentEngineProfiles?.get(engineInput.value) || {};
    const engineLabel = String(
      engineProfile.label
      || engineInput.selectedOptions[0]?.textContent
      || engineInput.value
      || "视觉截图"
    );
    const providerInput = $("#agent-model-provider-input");
    const profile = app.agentProviderProfiles.get(providerInput.value) || {};
    const providerLabel = String(
      profile.label
      || providerInput.selectedOptions[0]?.textContent
      || providerInput.value
      || "未配置协议"
    );
    $("#agent-config-tab-model-meta").textContent = `${engineLabel} · ${providerLabel}`;
  }

  function normalizeAgentSystemPrompt(value) {
    return String(value || "").replace(/\r\n?/g, "\n").trim();
  }

  function currentAgentSystemPromptVersion() {
    const prompt = normalizeAgentSystemPrompt($("#agent-system-prompt-input").value);
    const defaultPrompt = normalizeAgentSystemPrompt(app.agentDefaultSystemPrompt);
    return prompt && defaultPrompt && prompt === defaultPrompt
      ? app.agentDefaultSystemPromptVersion
      : "custom";
  }

  function refreshAgentSystemPromptVersion() {
    const version = currentAgentSystemPromptVersion();
    $("#agent-system-prompt-version").textContent = `${version} · 坐标协议、动作边界与安全规则`;
    refreshAgentSystemPromptSummary(version === "custom" ? "" : version);
  }

  function refreshAgentSystemPromptSummary(label = "") {
    const prompt = $("#agent-system-prompt-input").value.trim();
    $("#agent-prompt-tab-system-meta").textContent = prompt
      ? (label || "自定义规则")
      : "未配置";
  }

  function setAgentPromptDirty(dirty, message = "") {
    app.agentPromptDirty = Boolean(dirty);
    const bar = $(".agent-prompt-save-bar");
    const state = $("#agent-prompt-save-state");
    if (bar) bar.classList.toggle("dirty", app.agentPromptDirty);
    if (state) {
      state.textContent = message || (
        app.agentPromptDirty
          ? "有未保存修改；保存后才会用于下次启动"
          : "当前配置已保存并会用于下次启动"
      );
    }
  }

  function savedAgentSystemPrompt() {
    return normalizeAgentSystemPrompt(
      app.agentSavedSystemPrompt
      || localStorage.getItem(agentSystemPromptStorageKey)
      || app.agentDefaultSystemPrompt
    );
  }

  function agentTaskCardMarkup(rawTask, index) {
    const task = defaultAgentTask(rawTask || {});
    const taskId = String(task.id || `task-${Date.now()}-${index + 1}`);
    const failure = task.on_failure === "continue" ? "continue" : "stop";
    const actionLimits = normalizeAgentActionLimits(task.action_limits);
    const constraintNote = actionLimits.length ? ` · ${actionLimits.length} 组宿主动作上限` : "";
    return `
      <article class="agent-task-card" data-agent-task-id="${escapeHtml(taskId)}">
        <header>
          <button class="agent-task-card-select" type="button" data-agent-task-select aria-pressed="false">
            <span class="agent-task-index" data-agent-task-number>${index + 1}</span>
            <span class="agent-task-card-copy"><strong data-agent-task-name>${escapeHtml(task.name || `任务 ${index + 1}`)}</strong><small data-agent-task-prompt-summary>${task.prompt ? "Prompt 已配置" : "Prompt 未填写"}${constraintNote}</small></span>
          </button>
          <nav aria-label="调整任务顺序">
            <button type="button" title="上移" data-agent-task-action="up">↑</button>
            <button type="button" title="下移" data-agent-task-action="down">↓</button>
            <button type="button" title="删除" data-agent-task-action="remove">×</button>
          </nav>
        </header>
        <div class="agent-task-card-storage agent-task-prompt-storage" aria-hidden="true" hidden>
          <input tabindex="-1" type="text" maxlength="120" data-agent-task-field="name" value="${escapeHtml(task.name || "")}">
          <textarea tabindex="-1" data-agent-task-field="prompt">${escapeHtml(task.prompt || "")}</textarea>
          <textarea tabindex="-1" data-agent-task-field="attention_prompt">${escapeHtml(task.attention_prompt || "")}</textarea>
          <textarea tabindex="-1" data-agent-task-field="action_limits">${escapeHtml(JSON.stringify(actionLimits))}</textarea>
          <input tabindex="-1" type="number" min="1" max="200" step="1" data-agent-task-field="max_steps" value="${escapeHtml(task.max_steps)}">
          <input tabindex="-1" type="number" min="5" max="7200" step="1" data-agent-task-field="timeout_s" value="${escapeHtml(task.timeout_s)}">
          <select tabindex="-1" data-agent-task-field="on_failure"><option value="stop"${failure === "stop" ? " selected" : ""}>停止流程</option><option value="continue"${failure === "continue" ? " selected" : ""}>记录并继续</option></select>
        </div>
      </article>`;
  }

  function agentTaskCardById(taskId) {
    return $$("#agent-task-list .agent-task-card")
      .find(card => card.dataset.agentTaskId === String(taskId || "")) || null;
  }

  function filterAgentTaskSequence() {
    const cards = $$("#agent-task-list .agent-task-card");
    const query = String($("#agent-task-filter-input")?.value || "").trim().toLowerCase();
    let visible = 0;
    cards.forEach((card, index) => {
      const name = card.querySelector('[data-agent-task-field="name"]')?.value || "";
      const prompt = card.querySelector('[data-agent-task-field="prompt"]')?.value || "";
      const haystack = [
        index + 1,
        card.dataset.agentTaskId,
        name,
        prompt.trim() ? "prompt 已配置" : "prompt 未填写",
      ].join(" ").toLowerCase();
      const matches = !query || haystack.includes(query);
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    const summary = $("#agent-task-filter-summary");
    if (summary) summary.textContent = query ? `${visible} / ${cards.length} 项` : `${cards.length} 项`;
  }

  function renderAgentWorkflowTaskEditor({ selectedId = "" } = {}) {
    const cards = $$("#agent-task-list .agent-task-card");
    if (!cards.length) return;
    const card = agentTaskCardById(selectedId || app.agentPromptTaskId) || cards[0];
    const taskId = String(card.dataset.agentTaskId || "");
    const index = cards.indexOf(card);
    const name = card.querySelector('[data-agent-task-field="name"]')?.value || "";
    const prompt = card.querySelector('[data-agent-task-field="prompt"]')?.value || "";
    const maxSteps = card.querySelector('[data-agent-task-field="max_steps"]')?.value || "";
    const timeout = card.querySelector('[data-agent-task-field="timeout_s"]')?.value || "";
    const failure = card.querySelector('[data-agent-task-field="on_failure"]')?.value || "stop";
    const actionLimits = normalizeAgentActionLimits(
      card.querySelector('[data-agent-task-field="action_limits"]')?.value || "[]"
    );
    app.agentPromptTaskId = taskId;
    cards.forEach(item => {
      const active = item === card;
      item.classList.toggle("active", active);
      const selectButton = item.querySelector("[data-agent-task-select]");
      selectButton?.setAttribute("aria-pressed", String(active));
    });
    $("#agent-task-detail-index").textContent = `TASK ${String(index + 1).padStart(2, "0")}`;
    $("#agent-task-detail-title").textContent = name.trim() || `任务 ${index + 1}`;
    $("#agent-task-detail-position").textContent = `${index + 1} / ${cards.length}`;
    const editorValues = {
      name,
      max_steps: maxSteps,
      timeout_s: timeout,
      on_failure: failure,
    };
    $$('[data-agent-task-editor-field]').forEach(control => {
      const value = String(editorValues[control.dataset.agentTaskEditorField] ?? "");
      if (control.value !== value) control.value = value;
      control.disabled = false;
    });
    const configured = Boolean(prompt.trim());
    const promptState = $("#agent-task-detail-prompt-state");
    promptState.textContent = configured ? "已配置" : "待填写";
    promptState.classList.toggle("ready", configured);
    $("#agent-task-detail-action-limits").textContent = actionLimits.length
      ? `${actionLimits.length} 组宿主上限`
      : "未配置";
    $("#agent-open-task-prompt-button").disabled = false;
  }

  function selectAgentWorkflowTask(taskId, { focus = false } = {}) {
    const card = agentTaskCardById(taskId);
    if (!card) return;
    app.agentPromptTaskId = String(card.dataset.agentTaskId || "");
    renderAgentWorkflowTaskEditor({ selectedId: app.agentPromptTaskId });
    if ($("#agent-prompt-task-select")) {
      $("#agent-prompt-task-select").value = app.agentPromptTaskId;
      renderAgentTaskPromptEditor();
    }
    card.scrollIntoView({ block: "nearest" });
    if (focus) $("#agent-task-name-editor-input")?.focus();
  }

  function syncAgentWorkflowTaskEditor(event) {
    const control = event.target.closest("[data-agent-task-editor-field]");
    const card = agentTaskCardById(app.agentPromptTaskId);
    if (!control || !card) return;
    const field = String(control.dataset.agentTaskEditorField || "");
    const storage = card.querySelector(`[data-agent-task-field="${field}"]`);
    if (!storage) return;
    storage.value = control.value;
    if (field === "name") refreshAgentTaskOrder();
    setAgentPromptDirty(true);
  }

  function focusInvalidAgentTaskControl(control) {
    const card = control?.closest(".agent-task-card");
    if (!card) return;
    setAgentPromptTab("workflow");
    selectAgentWorkflowTask(card.dataset.agentTaskId);
    const editor = $(`[data-agent-task-editor-field="${control.dataset.agentTaskField}"]`);
    editor?.focus();
    editor?.reportValidity();
  }

  function refreshAgentTaskPromptSummary() {
    const cards = $$("#agent-task-list .agent-task-card");
    const missing = cards.filter(card => (
      !card.querySelector('[data-agent-task-field="prompt"]')?.value.trim()
    )).length;
    const summary = missing
      ? `${cards.length} 个任务 · ${missing} 待填写`
      : `${cards.length} 个任务`;
    $("#agent-config-tab-prompt-meta").textContent = summary;
    $("#agent-prompt-tab-workflow-meta").textContent = summary;
  }

  function renderAgentTaskPromptEditor() {
    const cards = $$("#agent-task-list .agent-task-card");
    const promptInput = $("#agent-task-prompt-input");
    const attentionInput = $("#agent-task-attention-input");
    if (!cards.length || !promptInput || !attentionInput) return;
    const card = agentTaskCardById(app.agentPromptTaskId) || cards[0];
    const taskId = card.dataset.agentTaskId;
    const index = cards.indexOf(card);
    const name = card.querySelector('[data-agent-task-field="name"]')?.value.trim()
      || `任务 ${index + 1}`;
    const prompt = card.querySelector('[data-agent-task-field="prompt"]')?.value || "";
    const attention = card.querySelector('[data-agent-task-field="attention_prompt"]')?.value || "";
    app.agentPromptTaskId = taskId;
    if ($("#agent-prompt-task-select").value !== taskId) {
      $("#agent-prompt-task-select").value = taskId;
    }
    $("#agent-task-prompt-index").textContent = `TASK ${String(index + 1).padStart(2, "0")}`;
    $("#agent-task-prompt-name").textContent = name;
    if (promptInput.value !== prompt) promptInput.value = prompt;
    if (attentionInput.value !== attention) attentionInput.value = attention;
    const configured = Boolean(prompt.trim());
    const state = $("#agent-task-prompt-state");
    state.textContent = configured ? "Prompt 已配置" : "Prompt 未填写";
    state.classList.toggle("ready", configured);
    $("#agent-prompt-tab-task-meta").textContent = `${index + 1} / ${cards.length} · ${configured ? "已配置" : "待填写"}`;
  }

  function refreshAgentTaskPromptSelector({ selectedId = "" } = {}) {
    const cards = $$("#agent-task-list .agent-task-card");
    const select = $("#agent-prompt-task-select");
    if (!select || !cards.length) return;
    const preferredId = String(selectedId || select.value || app.agentPromptTaskId || "");
    const options = cards.map((card, index) => {
      const name = card.querySelector('[data-agent-task-field="name"]')?.value.trim()
        || `任务 ${index + 1}`;
      return `<option value="${escapeHtml(card.dataset.agentTaskId)}">${index + 1}. ${escapeHtml(name)}</option>`;
    }).join("");
    if (select.innerHTML !== options) select.innerHTML = options;
    const selectedCard = agentTaskCardById(preferredId) || cards[0];
    app.agentPromptTaskId = selectedCard.dataset.agentTaskId;
    select.value = app.agentPromptTaskId;
    renderAgentTaskPromptEditor();
    refreshAgentTaskPromptSummary();
  }

  function syncAgentTaskPromptEditor() {
    const card = agentTaskCardById(app.agentPromptTaskId);
    if (!card) return;
    card.querySelector('[data-agent-task-field="prompt"]').value = $("#agent-task-prompt-input").value;
    card.querySelector('[data-agent-task-field="attention_prompt"]').value = $("#agent-task-attention-input").value;
    const configured = Boolean($("#agent-task-prompt-input").value.trim());
    const actionLimits = normalizeAgentActionLimits(
      card.querySelector('[data-agent-task-field="action_limits"]')?.value || "[]"
    );
    card.classList.toggle("prompt-missing", !configured);
    card.querySelector("[data-agent-task-prompt-summary]").textContent = `${configured ? "Prompt 已配置" : "Prompt 未填写"}${actionLimits.length ? ` · ${actionLimits.length} 组宿主动作上限` : ""}`;
    const state = $("#agent-task-prompt-state");
    state.textContent = configured ? "Prompt 已配置" : "Prompt 未填写";
    state.classList.toggle("ready", configured);
    refreshAgentTaskPromptSummary();
    renderAgentWorkflowTaskEditor();
    setAgentPromptDirty(true);
  }

  function selectAgentTaskPrompt(taskId, { focus = false } = {}) {
    const card = agentTaskCardById(taskId);
    if (!card) return;
    app.agentPromptTaskId = card.dataset.agentTaskId;
    $("#agent-prompt-task-select").value = app.agentPromptTaskId;
    renderAgentTaskPromptEditor();
    renderAgentWorkflowTaskEditor({ selectedId: app.agentPromptTaskId });
    if (focus) $("#agent-task-prompt-input").focus();
  }

  function refreshAgentTaskOrder() {
    const cards = $$("#agent-task-list .agent-task-card");
    cards.forEach((card, index) => {
      card.querySelector("[data-agent-task-number]").textContent = String(index + 1);
      const title = card.querySelector("[data-agent-task-name]");
      const name = card.querySelector('[data-agent-task-field="name"]')?.value.trim();
      const prompt = card.querySelector('[data-agent-task-field="prompt"]')?.value.trim();
      const actionLimits = normalizeAgentActionLimits(
        card.querySelector('[data-agent-task-field="action_limits"]')?.value || "[]"
      );
      title.textContent = name || `任务 ${index + 1}`;
      card.querySelector("[data-agent-task-select]")?.setAttribute(
        "aria-label",
        `选择第 ${index + 1} 个任务：${name || `任务 ${index + 1}`}`,
      );
      card.classList.toggle("prompt-missing", !prompt);
      card.querySelector("[data-agent-task-prompt-summary]").textContent = `${prompt ? "Prompt 已配置" : "Prompt 未填写"}${actionLimits.length ? ` · ${actionLimits.length} 组宿主动作上限` : ""}`;
      card.querySelector('[data-agent-task-action="up"]').disabled = index === 0;
      card.querySelector('[data-agent-task-action="down"]').disabled = index === cards.length - 1;
      card.querySelector('[data-agent-task-action="remove"]').disabled = (
        cards.length <= 1 || Boolean(app.agentCampaignStage)
      );
    });
    const label = $("#agent-task-count-label");
    if (label) label.textContent = `${cards.length} 个子任务 · 按顺序执行；finish 只完成当前子任务`;
    $("#agent-config-tab-task-meta").textContent = `${cards.length} 个任务`;
    filterAgentTaskSequence();
    refreshAgentTaskPromptSelector();
    renderAgentWorkflowTaskEditor();
  }

  function renderAgentTaskEditor(tasks) {
    const normalized = Array.isArray(tasks) && tasks.length ? tasks : [defaultAgentTask()];
    if ($("#agent-task-filter-input")) $("#agent-task-filter-input").value = "";
    $("#agent-task-list").innerHTML = normalized
      .map((task, index) => agentTaskCardMarkup(task, index))
      .join("");
    refreshAgentTaskOrder();
  }

  function appendAgentTask(task = {}) {
    app.agentTaskSeed += 1;
    const item = defaultAgentTask(task);
    const baseId = String(item.id || "task");
    item.id = `${baseId}-${Date.now()}-${app.agentTaskSeed}`;
    const list = $("#agent-task-list");
    const initialCard = list.children.length === 1 ? list.firstElementChild : null;
    const initialCardIsBlank = Boolean(
      initialCard
      && !initialCard.querySelector('[data-agent-task-field="name"]').value.trim()
      && !initialCard.querySelector('[data-agent-task-field="prompt"]').value.trim()
      && !initialCard.querySelector('[data-agent-task-field="attention_prompt"]').value.trim()
    );
    if (item.prompt && initialCardIsBlank) {
      list.innerHTML = agentTaskCardMarkup(item, 0);
    } else {
      list.insertAdjacentHTML("beforeend", agentTaskCardMarkup(item, list.children.length));
    }
    app.agentPromptTaskId = item.id;
    refreshAgentTaskOrder();
    selectAgentWorkflowTask(item.id, { focus: true });
  }

  function readAgentTasks() {
    return $$("#agent-task-list .agent-task-card").map((card, index) => ({
      id: card.dataset.agentTaskId || `task-${index + 1}`,
      name: card.querySelector('[data-agent-task-field="name"]').value.trim() || `任务 ${index + 1}`,
      prompt: card.querySelector('[data-agent-task-field="prompt"]').value.trim(),
      attention_prompt: card.querySelector('[data-agent-task-field="attention_prompt"]').value.trim(),
      max_steps: Number(card.querySelector('[data-agent-task-field="max_steps"]').value),
      timeout_s: Number(card.querySelector('[data-agent-task-field="timeout_s"]').value),
      on_failure: card.querySelector('[data-agent-task-field="on_failure"]').value,
      action_limits: normalizeAgentActionLimits(
        card.querySelector('[data-agent-task-field="action_limits"]')?.value || "[]"
      ),
    }));
  }

  function campaignAwareAgentTemplates(templates, stageConfig) {
    const stages = stageConfig?.stages && typeof stageConfig.stages === "object"
      ? stageConfig.stages
      : {};
    const revision = String(stageConfig?.revision || "campaign-json");
    return (Array.isArray(templates) ? templates : []).map(template => {
      if (template?.kind !== "campaign") return template;
      const stage = stages[String(template.campaign_stage || "")];
      if (!stage || !Array.isArray(stage.tasks) || !stage.tasks.length) return template;
      return {
        ...template,
        revision: `${revision}-${template.campaign_stage}`,
        tasks: stage.tasks.map(task => ({ ...task })),
      };
    });
  }

  function populateAgentTaskTemplates(templates, stageConfig = null) {
    app.agentServerTemplates = campaignAwareAgentTemplates(templates, stageConfig);
    const items = [...app.agentServerTemplates, ...app.agentCustomTemplates];
    const signature = JSON.stringify(items.map(item => [
      item.id,
      item.label,
      item.kind,
      item.campaign_stage,
      item.revision,
      Array.isArray(item.tasks) ? item.tasks.length : 0,
    ]));
    if (signature === app.agentTemplateSignature) return;
    app.agentTemplateSignature = signature;
    const select = $("#agent-task-template-select");
    const selectedId = app.agentSelectedTemplateId || select.value;
    select.innerHTML = items
      .map(item => `<option value="${escapeHtml(item.id || "")}">${escapeHtml(item.label || item.name || item.id || "未命名模板")}</option>`)
      .join("");
    const nextId = items.some(item => String(item.id) === selectedId)
      ? selectedId
      : String(items[0]?.id || "");
    if (nextId) select.value = nextId;
  }

  function agentTemplateById(templateId) {
    const templates = [...app.agentServerTemplates, ...app.agentCustomTemplates];
    return templates.find(item => String(item.id) === String(templateId || "")) || null;
  }

  function persistAgentCustomTemplates() {
    localStorage.setItem(
      agentCustomTemplateStorageKey,
      JSON.stringify(app.agentCustomTemplates || []),
    );
  }

  function persistAgentTemplateDrafts() {
    localStorage.setItem(
      agentTemplateDraftStorageKey,
      JSON.stringify(app.agentTemplateDrafts || {}),
    );
  }

  function saveCurrentAgentTemplateDraft() {
    const templateId = app.agentSelectedTemplateId;
    const template = agentTemplateById(templateId);
    if (!templateId || app.agentTemplateLoading || !template) return;
    const workflowName = $("#agent-workflow-name-input").value.trim() || "未命名任务";
    const tasks = readAgentTasks();
    app.agentTemplateDrafts[templateId] = {
      workflow_name: workflowName,
      loop_enabled: Boolean($("#agent-loop-workflow-input").checked),
      template_revision: String(template.revision || ""),
      tasks,
    };
    persistAgentTemplateDrafts();
    const customTemplate = app.agentCustomTemplates.find(item => String(item.id) === templateId);
    if (customTemplate) {
      customTemplate.label = workflowName;
      customTemplate.name = workflowName;
      customTemplate.workflow_name = workflowName;
      customTemplate.tasks = tasks;
      persistAgentCustomTemplates();
      app.agentTemplateSignature = "";
      populateAgentTaskTemplates(app.agentServerTemplates);
      $("#agent-task-template-select").value = templateId;
    }
  }

  function saveAgentLoopPreference() {
    const templateId = app.agentSelectedTemplateId;
    if (!templateId || !agentTemplateById(templateId)) return;
    const existing = app.agentTemplateDrafts[templateId];
    app.agentTemplateDrafts[templateId] = {
      ...(existing && typeof existing === "object" ? existing : {}),
      loop_enabled: Boolean($("#agent-loop-workflow-input").checked),
    };
    persistAgentTemplateDrafts();
  }

  function saveAgentPromptConfiguration() {
    const invalidTaskControl = $$(
      '#agent-task-list input[type="number"], #agent-task-list select'
    ).find(control => !control.checkValidity());
    if (invalidTaskControl) {
      setAgentPromptTab("workflow");
      notify("任务参数无效", "请检查子任务的步骤上限、超时时间和失败策略。", "error");
      focusInvalidAgentTaskControl(invalidTaskControl);
      return false;
    }
    const systemPrompt = normalizeAgentSystemPrompt($("#agent-system-prompt-input").value);
    if (!systemPrompt) {
      setAgentPromptTab("system");
      notify("System Prompt 不能为空", "请恢复默认 prompt 或填写自定义 ADB Agent 规则。", "error");
      $("#agent-system-prompt-input").focus();
      return false;
    }
    if (app.agentCampaignStage) {
      app.agentCampaignRuntimeOverrides = true;
      const editButton = $("#agent-edit-template-button");
      if (editButton) editButton.textContent = "正在运行时覆盖";
    }
    saveCurrentAgentTemplateDraft();
    app.agentSavedSystemPrompt = systemPrompt;
    localStorage.setItem(agentSystemPromptStorageKey, systemPrompt);
    const missing = readAgentTasks().filter(task => !task.prompt).length;
    setAgentPromptDirty(false, missing
      ? `配置已保存；仍有 ${missing} 个任务 Prompt 待填写`
      : "配置已保存并会用于下次启动");
    notify(
      "Prompt 配置已保存",
      missing ? `已保存为草稿，仍有 ${missing} 个任务目标未填写。` : "任务配置与 System Prompt 已生效。",
      missing ? "warning" : "success",
      4500,
    );
    return true;
  }

  function createAgentTemplate() {
    if (app.agentPromptDirty) {
      notify("请先保存当前修改", "保存当前 Prompt 配置后再新建任务。", "warning", 5000);
      setAgentConfigTab("prompt");
      return;
    }
    const now = Date.now();
    const ordinal = app.agentCustomTemplates.length + 1;
    const label = `新建任务 ${ordinal}`;
    const template = {
      id: `local-task-${now}`,
      kind: "custom",
      label,
      name: label,
      workflow_name: label,
      loop_enabled: false,
      tasks: [defaultAgentTask({ id: `task-${now}`, name: "任务 1" })],
    };
    app.agentCustomTemplates.push(template);
    app.agentTemplateSignature = "";
    populateAgentTaskTemplates(app.agentServerTemplates);
    applyAgentTemplate(template, { useDraft: false });
    setAgentPromptDirty(true, "新任务尚未保存；填写完成后点击保存");
    setAgentConfigTab("prompt");
    setAgentPromptTab("workflow");
    $("#agent-workflow-name-input").focus();
  }

  function applyAgentTemplate(template, { useDraft = true, notifyUser = false } = {}) {
    if (!template) return;
    const templateId = String(template.id || "");
    const storedDraft = app.agentTemplateDrafts[templateId]
      && typeof app.agentTemplateDrafts[templateId] === "object"
      ? app.agentTemplateDrafts[templateId]
      : null;
    const templateRevision = String(template.revision || "");
    const campaignTemplate = template.kind === "campaign";
    const draft = useDraft
      && (!campaignTemplate || app.agentCampaignRuntimeOverrides)
      && storedDraft
      && (!templateRevision || String(storedDraft.template_revision || "") === templateRevision)
      ? storedDraft
      : null;
    const templateTasks = Array.isArray(template.tasks) && template.tasks.length
      ? template.tasks
      : [{ ...template, name: template.label || template.name || "模板任务" }];
    const tasks = Array.isArray(draft?.tasks) && draft.tasks.length
      ? draft.tasks
      : templateTasks;
    app.agentTemplateLoading = true;
    renderAgentTaskEditor(tasks);
    $("#agent-workflow-name-input").value = String(
      draft?.workflow_name
      || template.workflow_name
      || template.label
      || "ADB 测试流程"
    );
    if (campaignTemplate && !draft) app.agentCampaignRuntimeOverrides = false;
    setAgentTemplateMode(template);
    if (app.agentCampaignStage === "test") {
      $("#agent-loop-workflow-input").checked = draft?.loop_enabled === undefined
        ? template.loop_enabled !== false
        : Boolean(draft.loop_enabled);
    }
    $("#agent-task-template-select").value = templateId;
    app.agentTemplateLoading = false;
    refreshAgentTaskOrder();
    setAgentPromptDirty(false);
    if (notifyUser) {
      notify(
        "已切换当前任务",
        `${template.label || template.name || templateId} · 修改后请点击保存`,
        "success",
        4000,
      );
    }
  }

  function editCurrentAgentTemplate() {
    const template = agentTemplateById(app.agentSelectedTemplateId);
    if (!template) return;
    if (template.kind === "campaign" && !app.agentCampaignRuntimeOverrides) {
      app.agentCampaignRuntimeOverrides = true;
      applyAgentTemplate(template, { useDraft: true });
      app.agentCampaignRuntimeOverrides = true;
      setAgentTemplateMode(template);
      notify(
        "已启用本次运行覆盖",
        "保存后，当前任务与 System Prompt 才会覆盖 JSON/内置基线；重新选择阶段可恢复基线。",
        "warning",
        7000,
      );
    }
    setAgentConfigTab("prompt");
    setAgentPromptTab("task");
    refreshAgentTaskPromptSelector();
    $("#agent-task-prompt-input").focus();
  }

  function agentCampaignStartLabel() {
    if (app.agentCampaignStage === "prepare") return "启动预备阶段";
    if (app.agentCampaignStage === "test") return "启动实际测试阶段";
    if (agentTemplateById(app.agentSelectedTemplateId)?.kind === "phone_configuration") {
      return "启动手机配置检查";
    }
    return "启动测试流程";
  }

  function setAgentTemplateMode(template = null) {
    const campaignStage = template?.kind === "campaign"
      ? String(template.campaign_stage || "")
      : "";
    app.agentCampaignStage = ["prepare", "test"].includes(campaignStage) ? campaignStage : "";
    if (app.agentCampaignStage) {
      app.agentCampaignConfigStage = app.agentCampaignStage;
      renderAgentCampaignConfig(app.state?.campaign?.stage_config || {});
    }
    app.agentSelectedTemplateId = String(template?.id || "");
    if (app.agentSelectedTemplateId) {
      localStorage.setItem(agentSelectedTemplateStorageKey, app.agentSelectedTemplateId);
    } else {
      localStorage.removeItem(agentSelectedTemplateStorageKey);
    }
    const loopControl = $("#agent-loop-control");
    const loopInput = $("#agent-loop-workflow-input");
    const loopHint = $("#agent-loop-workflow-hint");
    if (loopControl && loopInput && loopHint) {
      loopControl.hidden = false;
      loopInput.checked = app.agentCampaignStage === "test"
        ? template?.loop_enabled !== false
        : false;
      loopInput.disabled = app.agentCampaignStage !== "test";
      loopHint.textContent = app.agentCampaignStage === "prepare"
        ? "预备阶段固定执行一次；系统设置、本地安装和权限步骤由宿主按配置完成。"
        : app.agentCampaignStage === "test"
          ? "开启后在本次 7200 秒轮次内持续循环可重试 workflow；本次固定只跑 1 轮。"
          : "当前任务固定执行一次。";
    }
    const hint = $("#agent-template-selection-hint");
    if (hint) {
      hint.textContent = app.agentCampaignStage
        ? `${template?.description || "当前 Campaign 阶段已载入。"} 阶段基线来自 JSON；只有点击“运行时覆盖”并保存后，浏览器草稿才覆盖本次运行。`
        : template
          ? `${template.description || "当前模板已载入。"} 顺序、步骤、超时和任务指令统一在 Prompt 编辑中修改。`
          : "选择任务后即可启动；顺序、步骤、超时和任务指令统一在 Prompt 编辑中修改。";
      hint.classList.toggle("campaign", Boolean(app.agentCampaignStage));
    }
    $("#agent-task-list")?.classList.toggle("campaign-preview", Boolean(app.agentCampaignStage));
    const startButton = $("#agent-start-button");
    if (startButton && !startButton.disabled) startButton.textContent = agentCampaignStartLabel();
    const configurationButton = $("#agent-select-phone-configuration-button");
    if (configurationButton) {
      const selected = template?.kind === "phone_configuration";
      configurationButton.classList.toggle("active", selected);
      configurationButton.textContent = selected ? "已选择配置检查" : "选择配置检查";
      configurationButton.disabled = selected || agentSoftwareAutomationRunning();
    }
    const editButton = $("#agent-edit-template-button");
    if (editButton) {
      editButton.disabled = !app.agentSelectedTemplateId;
      editButton.textContent = app.agentCampaignStage
        ? (app.agentCampaignRuntimeOverrides ? "正在运行时覆盖" : "运行时覆盖")
        : "编辑模板";
    }
    refreshAgentTaskOrder();
  }

  function populateAgentModelProviders(providers) {
    const items = Array.isArray(providers) ? providers : [];
    const signature = JSON.stringify(items);
    if (signature === app.agentProviderSignature) return;
    app.agentProviderSignature = signature;
    app.agentProviderProfiles = new Map(items.map(item => [String(item.id), item]));
    if (items.length) {
      $("#agent-model-provider-input").innerHTML = items
        .map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.id)}</option>`)
        .join("");
    }
  }

  function populateAgentAutomationEngines(engines) {
    const items = Array.isArray(engines) ? engines : [];
    const signature = JSON.stringify(items);
    if (signature === app.agentEngineSignature) return;
    app.agentEngineSignature = signature;
    app.agentEngineProfiles = new Map(items.map(item => [String(item.id), item]));
    if (items.length) {
      $("#agent-automation-engine-input").innerHTML = items
        .map(item => `<option value="${escapeHtml(item.id)}"${item.available === false ? " disabled" : ""}>${escapeHtml(item.label || item.id)}${item.available === false ? "（不可用）" : ""}</option>`)
        .join("");
    }
  }

  function applyAgentAutomationEnginePresentation() {
    const input = $("#agent-automation-engine-input");
    const profile = app.agentEngineProfiles?.get(input.value) || {};
    const semanticOnly = input.value === "uiautomator2";
    const hybrid = input.value === "hybrid";
    $("#agent-automation-engine-hint").textContent = String(
      profile.available === false
        ? (profile.detail || "uiautomator2 依赖不可用")
        : (profile.description || profile.detail || (
          semanticOnly
            ? "模型读取语义控件树并按 revision 绑定的元素编号操作；截图仅用于运行证据。"
            : hybrid
              ? "模型同时读取前后截图与控件树；语义元素缺失时可使用视觉坐标。"
              : "模型比较前后 ADB 截图并使用归一化坐标操作。"
        ))
    );
    $("#agent-model-input").placeholder = semanticOnly
      ? "支持文本与工具调用的模型名称"
      : "支持图像与工具调用的模型名称";
    refreshAgentModelSummary();
  }

  function applyAgentProviderPresentation({ replaceDefaults = false } = {}) {
    const provider = $("#agent-model-provider-input").value;
    const profile = app.agentProviderProfiles.get(provider) || {};
    const previous = app.agentProviderProfiles.get(app.agentPresentedProvider) || {};
    const apiInput = $("#agent-api-base-input");
    const modelInput = $("#agent-model-input");
    const keyModeInput = $("#agent-api-key-mode-input");
    const thinkingModeInput = $("#agent-thinking-mode-input");
    if (replaceDefaults) {
      const endpointIsPreviousDefault = !apiInput.value.trim()
        || apiInput.value.trim() === String(previous.default_api_base_url || "");
      const modelIsPreviousDefault = !modelInput.value.trim()
        || modelInput.value.trim() === String(previous.default_model || "");
      if (endpointIsPreviousDefault && profile.default_api_base_url) {
        apiInput.value = String(profile.default_api_base_url);
      }
      if (modelIsPreviousDefault) modelInput.value = String(profile.default_model || "");
      keyModeInput.value = String(profile.default_api_key_mode || "bearer");
      thinkingModeInput.value = String(profile.default_thinking_mode || "auto");
    }
    thinkingModeInput.disabled = provider !== "openai_compatible";
    apiInput.placeholder = String(profile.api_placeholder || "填写模型 API 地址或完整端点");
    modelInput.placeholder = String(
      profile.model_placeholder
      || ($("#agent-automation-engine-input").value === "uiautomator2"
        ? "支持文本与工具调用的模型名称"
        : "支持图像与工具调用的模型名称")
    );
    $("#agent-model-provider-hint").textContent = String(
      profile.description || "选择模型供应商使用的多模态工具调用协议。"
    );
    $("#agent-api-key-hint").textContent = provider === "openai_compatible"
      ? "本地模型可留空"
      : "仅保留在当前进程内存";
    app.agentPresentedProvider = provider;
    applyAgentAutomationEnginePresentation();
  }

  function renderAgentWorkflowReport(agent) {
    const report = $("#agent-workflow-report");
    if (!report) return;
    const tasks = Array.isArray(agent.tasks) ? agent.tasks : [];
    const results = Array.isArray(agent.task_results) ? agent.task_results : [];
    if (agent.running || !results.length) {
      report.hidden = true;
      return;
    }
    const completed = results.filter(item => item.status === "completed").length;
    const skipped = results.filter(item => item.status === "skipped").length;
    const failedResults = results.filter(item => !["completed", "skipped"].includes(String(item.status || "")));
    const pending = Math.max(0, tasks.length - results.length);
    const configurationWorkflow = String(agent.workflow_name || "").includes("手机配置检查")
      || tasks.some(task => /^(?:A[0-3]|B)\./.test(String(task.name || "")));
    const statusLabels = {
      skipped: "已跳过",
      timeout: "已超时",
      max_steps: "步骤耗尽",
      take_over: "人工接管",
      stopped: "已停止",
      error: "运行失败",
    };
    report.hidden = false;
    report.className = `agent-workflow-report ${failedResults.length ? "failed" : skipped ? "warning" : "completed"}`;
    $("#agent-workflow-report-title").textContent = configurationWorkflow ? "手机配置检查统一汇总" : "任务统一汇总";
    $("#agent-workflow-report-state").textContent = failedResults.length
      ? "NEEDS ATTENTION"
      : skipped ? "COMPLETED WITH SKIPS" : "COMPLETED";
    $("#agent-workflow-report-completed").textContent = String(completed);
    $("#agent-workflow-report-skipped").textContent = String(skipped);
    $("#agent-workflow-report-failed").textContent = String(failedResults.length);
    $("#agent-workflow-report-pending").textContent = String(pending);
    const issues = results.filter(item => item.status !== "completed");
    $("#agent-workflow-report-reason-list").innerHTML = issues.length
      ? issues.map(item => `<li class="${escapeHtml(String(item.status || "unknown"))}"><strong>${escapeHtml(item.name || `检查项 ${item.index || ""}`)}</strong><em>${escapeHtml(statusLabels[item.status] || String(item.status || "未完成"))}</em><span>${escapeHtml(item.message || "未提供原因")}</span></li>`).join("")
      : "<li class=\"completed\"><strong>全部检查项</strong><em>已完成</em><span>没有跳过或未完成项目。</span></li>";
  }

  function renderAgentTaskResults(agent) {
    const tasks = Array.isArray(agent.tasks) ? agent.tasks : [];
    const results = Array.isArray(agent.task_results) ? agent.task_results : [];
    const resultByIndex = new Map(results.map(item => [Number(item.index), item]));
    const container = $("#agent-task-results");
    const summary = $("#agent-task-progress-summary");
    if (!tasks.length) {
      container.innerHTML = '<div class="agent-log-empty">配置任务后启动流程</div>';
      summary.textContent = "尚未启动流程";
      renderAgentWorkflowReport(agent);
      return;
    }
    const currentIndex = Number(agent.task_index || 0);
    const statusLabels = {
      pending: "等待执行",
      running: "执行中",
      completed: "已完成",
      skipped: "已跳过",
      timeout: "已超时",
      max_steps: "步骤耗尽",
      take_over: "人工接管",
      stopped: "已停止",
      error: "运行失败",
    };
    container.innerHTML = tasks.map((task, offset) => {
      const index = offset + 1;
      const result = resultByIndex.get(index);
      let taskStatus = String(result?.status || "pending");
      if (!result && currentIndex === index && agent.running) taskStatus = "running";
      if (!result && currentIndex === index && ["stopped", "error"].includes(agent.status)) {
        taskStatus = agent.status;
      }
      const message = result?.message
        || (taskStatus === "running" ? agent.message : "等待前序任务完成");
      const details = result
        ? `${result.steps || 0} 步 · ${formatDuration(result.duration_s || 0)} · ${task.on_failure === "continue" ? "失败后继续" : "失败即停止"}`
        : `${task.max_steps || "--"} 步上限 · ${task.timeout_s || "--"} 秒超时`;
      return `<article class="agent-task-result ${escapeHtml(taskStatus)}"><i>${index}</i><div><strong>${escapeHtml(task.name || `任务 ${index}`)}</strong><p>${escapeHtml(message || "")}</p><small>${escapeHtml(details)}</small></div><em>${escapeHtml(statusLabels[taskStatus] || taskStatus)}</em></article>`;
    }).join("");
    const completed = results.filter(item => item.status === "completed").length;
    const skipped = results.filter(item => item.status === "skipped").length;
    const failed = results.filter(item => !["completed", "skipped"].includes(String(item.status || ""))).length;
    summary.textContent = agent.running
      ? `执行第 ${currentIndex || 1} / ${tasks.length} 项 · 已完成 ${completed} 项`
      : `已结束 ${results.length} / ${tasks.length} 项 · 完成 ${completed} · 跳过 ${skipped} · 未完成 ${failed}`;
    renderAgentWorkflowReport(agent);
  }

  function agentSoftwareStatusPresentation(status) {
    return {
      verified: ["已通过", "verified"],
      installed: ["已安装", "verified"],
      running: ["验证中", "running"],
      pending_validation: ["待正式检验", "pending"],
      needs_attention: ["需人工", "attention"],
      missing: ["未安装", "missing"],
      failed: ["未通过", "failed"],
      stopped: ["已停止", "stopped"],
      not_checked: ["未检验", "not-checked"],
    }[String(status || "not_checked")] || [String(status || "--"), "not-checked"];
  }

  const agentSoftwareCategoryProfiles = {
    overview: {
      kicker: "CATALOG OVERVIEW",
      title: "支持范围与最近验证",
      description: "查看软件覆盖规模与最近一次预备验证状态。",
      matches: () => true,
      overview: true,
    },
    all: {
      kicker: "ALL SOFTWARE",
      title: "全部应用与游戏",
      description: "查看所有已适配软件，以及与各自来源匹配的安装操作。",
      matches: () => true,
    },
    app: {
      kicker: "APPLICATIONS",
      title: "通用应用",
      description: "浏览器、办公、资讯、地图与工具类应用。",
      matches: item => item.software_type !== "game",
    },
    game: {
      kicker: "GAMES",
      title: "游戏",
      description: "已经配置安全主玩法验证流程的游戏。",
      matches: item => item.software_type === "game",
    },
    project: {
      kicker: "PROJECT PACKAGES",
      title: "项目安装包",
      description: "从项目归档的 APK / APKS 安装固定测试版本。",
      matches: item => item.install_mode === "project",
    },
    app_store: {
      kicker: "APP STORE",
      title: "应用商店安装",
      description: "由 Agent 打开设备应用商店，搜索并安装官方应用。",
      matches: item => ["app_store", "app_store_or_official"].includes(item.install_channel),
    },
    official: {
      kicker: "OFFICIAL WEBSITE",
      title: "官网安装",
      description: "由 Agent 仅访问配置中的官方网站并完成下载安装。",
      matches: item => ["official_website", "app_store_or_official"].includes(item.install_channel) && Boolean(item.official_url),
    },
    pending: {
      kicker: "PENDING VALIDATION",
      title: "待正式检验",
      description: "已完成手机烟测，等待预备阶段再次验证安装、初始化和正常流程。",
      matches: item => item.catalog_status === "pending_validation",
    },
  };

  function agentSoftwareAutomationRunning() {
    const adbAgent = app.state?.adb_agent || {};
    const campaign = app.state?.campaign || {};
    return Boolean(
      adbAgent.running
      || campaign.running
      || ["starting", "stopping"].includes(String(adbAgent.status || ""))
      || ["starting", "stopping"].includes(String(campaign.status || ""))
    );
  }

  function agentSoftwareAsset(item) {
    if (app.agentSoftwareAssetsDevice !== selectedDevice()) return null;
    return app.agentSoftwareAssets.get(String(item.package || "")) || null;
  }

  function agentSoftwareInstallationStatus(item) {
    const asset = agentSoftwareAsset(item);
    if (asset && typeof asset.installed === "boolean") {
      return asset.installed ? "installed" : "missing";
    }
    return String(item.installation_status || "not_checked");
  }

  function agentSoftwareIconMarkup(item) {
    const asset = agentSoftwareAsset(item);
    const icon = String(asset?.icon_data_uri || item.icon_data_uri || "");
    const name = String(item.name || item.package || "?");
    if (icon.startsWith("data:image/")) {
      return `<span class="agent-software-icon"><img src="${escapeHtml(icon)}" alt="${escapeHtml(`${name} 图标`)}" loading="lazy"></span>`;
    }
    return `<span class="agent-software-icon fallback" aria-label="${escapeHtml(`${name} 图标占位`)}">${escapeHtml(name.trim().slice(0, 1).toUpperCase() || "?")}</span>`;
  }

  function agentSoftwareInstallActionsMarkup(item) {
    const packageName = String(item.package || "");
    const installationStatus = agentSoftwareInstallationStatus(item);
    const installed = installationStatus === "installed";
    const automationRunning = agentSoftwareAutomationRunning();
    const packageBusy = app.agentSoftwareInstallingPackage === packageName;
    const actions = Array.isArray(item.install_actions) && item.install_actions.length
      ? item.install_actions.map(String)
      : item.install_mode === "project"
        ? ["project"]
        : [String(item.install_channel || "app_store")];
    const buttons = actions.map(action => {
      const missingProject = action === "project" && item.source_available === false;
      const disabled = automationRunning || packageBusy || missingProject || (action === "project" && installed);
      let label = "安装";
      if (packageBusy) label = "正在启动安装…";
      else if (action === "project") {
        label = missingProject ? "项目安装包缺失" : installed ? "项目包已安装" : "从项目安装包安装";
      } else if (action === "official_website") {
        label = installed ? "Agent 从官网检查 / 更新" : "Agent 从官网安装";
      } else {
        label = installed ? "Agent 从应用商店检查 / 更新" : "Agent 从应用商店安装";
      }
      return `<button class="button ghost compact" type="button" data-agent-software-install="${escapeHtml(action)}" data-agent-software-package="${escapeHtml(packageName)}"${disabled ? " disabled" : ""}>${escapeHtml(label)}</button>`;
    }).join("");
    return `<div class="agent-software-install-actions"><span>安装操作</span>${buttons || "<em>当前没有可用安装方式</em>"}</div>`;
  }

  function agentSoftwareCardMarkup(item) {
    const validation = agentSoftwareStatusPresentation(item.validation_status);
    const installation = agentSoftwareStatusPresentation(agentSoftwareInstallationStatus(item));
    const setup = agentSoftwareStatusPresentation(item.setup_status);
    const normalFlow = agentSoftwareStatusPresentation(item.normal_flow_status);
    const engineLabels = (Array.isArray(item.supported_engines) ? item.supported_engines : [])
      .map(engine => app.agentEngineProfiles?.get(String(engine))?.label || String(engine))
      .join(" / ") || "未声明";
    const typeLabel = item.software_type === "game" ? "游戏" : "应用";
    const modeLabel = item.install_mode === "project"
      ? "项目安装包"
      : item.install_channel === "app_store"
        ? "应用商店"
        : item.install_channel === "official_website"
          ? "官方网站"
          : "应用商店 / 官网";
    const source = item.install_source || item.source_path || modeLabel;
    const sourceState = item.install_mode === "project" && item.source_available === false
      ? " · 项目文件缺失"
      : "";
    const catalogPending = item.catalog_status === "pending_validation";
    const headline = catalogPending
      ? agentSoftwareStatusPresentation("pending_validation")
      : validation;
    const official = item.official_url
      ? `<div><dt>官方网站</dt><dd>${escapeHtml(item.official_url)}</dd></div>`
      : "";
    return `<article class="agent-software-card ${escapeHtml(headline[1])}" data-agent-software-package-card="${escapeHtml(item.package || "")}">
      <header><div class="agent-software-identity">${agentSoftwareIconMarkup(item)}<div><small>${escapeHtml(typeLabel)} · ${escapeHtml(modeLabel)}</small><strong>${escapeHtml(item.name || item.package || "未命名软件")}</strong></div></div><em class="${escapeHtml(headline[1])}">${escapeHtml(headline[0])}</em></header>
      <p>${escapeHtml(item.description || "已配置安装、初始化和正常测试流程。")}</p>
      <div class="agent-software-stage-grid">
        <span class="${escapeHtml(installation[1])}"><small>安装</small><strong>${escapeHtml(installation[0])}</strong></span>
        <span class="${escapeHtml(setup[1])}"><small>初始化</small><strong>${escapeHtml(setup[0])}</strong></span>
        <span class="${escapeHtml(normalFlow[1])}"><small>正常流程</small><strong>${escapeHtml(normalFlow[0])}</strong></span>
      </div>
      <dl><div><dt>安装来源</dt><dd>${escapeHtml(`${source}${sourceState}`)}</dd></div>${official}<div><dt>支持引擎</dt><dd>${escapeHtml(engineLabels)}</dd></div></dl>
      ${agentSoftwareInstallActionsMarkup(item)}
      <footer><code>${escapeHtml(item.package || "--")}</code><span>${item.required ? "必检" : "可选"}</span></footer>
      <small class="agent-software-message">${escapeHtml(item.validation_message || "尚未运行预备验证")}</small>
    </article>`;
  }

  function readyAgentSoftwareDevice() {
    const device = selectedDevice();
    const selected = (app.state?.devices || []).find(item => item.serial === device);
    return selected && devicePlatform(selected) === "android" && selected.state === "device"
      ? device
      : "";
  }

  async function loadAgentSoftwareAssets(catalog, { force = false, notifyFailure = false } = {}) {
    const items = Array.isArray(catalog?.items) ? catalog.items : [];
    const device = readyAgentSoftwareDevice();
    if (!device || !items.length || app.agentSoftwareAssetsLoading) return null;
    const packages = items.map(item => String(item.package || "")).filter(Boolean).sort();
    const signature = `${device}|${packages.join("|")}`;
    if (!force && app.agentSoftwareAssetsSignature === signature) return null;
    if (app.agentSoftwareAssetsDevice !== device) {
      app.agentSoftwareAssets = new Map();
      app.agentSoftwareAssetsDevice = device;
    }
    app.agentSoftwareAssetsLoading = true;
    app.agentSoftwareAssetsSignature = signature;
    const refreshButton = $("#agent-software-refresh-button");
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.textContent = "正在读取图标与安装状态…";
    }
    try {
      const result = await api("/api/campaign/software/assets", {
        method: "POST",
        body: JSON.stringify({ device, packages }),
      });
      (Array.isArray(result.assets) ? result.assets : []).forEach(asset => {
        const packageName = String(asset.package || "");
        if (packageName) app.agentSoftwareAssets.set(packageName, asset);
      });
      return result;
    } catch (error) {
      if (notifyFailure) notify("无法刷新软件图标", error.message, "error", 8000);
      return null;
    } finally {
      app.agentSoftwareAssetsLoading = false;
      renderAgentSoftwareCatalog(catalog);
    }
  }

  function agentSoftwareInstallTask(item, action) {
    const name = String(item.name || item.package || "目标应用");
    const packageName = String(item.package || "");
    const catalog = app.state?.campaign?.software_catalog || {};
    const storePackage = String(catalog.store_package || "");
    const prompt = action === "official_website"
      ? `启动浏览器并只打开配置中的官方页面 ${item.official_url}。确认页面属于 ${name} 官方站点后，下载官方 Android 安装包并通过系统安装器安装；安装后确认包 ${packageName} 已可打开再 finish。`
      : `启动当前设备自带应用商店${storePackage ? `（包 ${storePackage}）` : ""}，搜索“${name}”并核对官方应用名称和开发者。完成安装或官方更新后，确认包 ${packageName} 已可打开再 finish。`;
    return defaultAgentTask({
      id: `software-install-${Date.now()}`,
      name: `${name} 安装`,
      prompt,
      attention_prompt: `只安装目标包 ${packageName}。不得登录、输入手机号或验证码、实名、支付、安装推荐应用或从第三方下载站获取 APK；遇到 Google Play、Play Games 或 GMS 页面直接 take_over。`,
      max_steps: 60,
      timeout_s: 1800,
      on_failure: "stop",
    });
  }

  async function installAgentSoftware(item, action) {
    const device = readyAndroidAgentDevice();
    if (!device) return;
    const packageName = String(item.package || "");
    app.agentSoftwareInstallingPackage = packageName;
    renderAgentSoftwareCatalog(app.state?.campaign?.software_catalog || {});
    if (action === "project") {
      try {
        const result = await api("/api/campaign/software/install", {
          method: "POST",
          body: JSON.stringify({ device, package: packageName }),
        });
        if (result.succeeded !== true) {
          throw new Error(result.error || result.output || "ADB 安装未成功");
        }
        const currentAsset = app.agentSoftwareAssets.get(packageName) || { package: packageName };
        app.agentSoftwareAssets.set(packageName, { ...currentAsset, installed: true });
        notify(
          result.already_installed ? "项目软件已经安装" : "项目安装包安装完成",
          `${item.name || packageName} · ${packageName}`,
          "success",
          6000,
        );
        app.agentSoftwareAssetsSignature = "";
      } catch (error) {
        notify("项目安装包安装失败", error.message, "error", 9000);
      } finally {
        app.agentSoftwareInstallingPackage = "";
        renderAgentSoftwareCatalog(app.state?.campaign?.software_catalog || {});
        void loadAgentSoftwareAssets(app.state?.campaign?.software_catalog || {}, { force: true });
      }
      return;
    }

    const result = await startAgentExecution({
      device,
      tasks: [agentSoftwareInstallTask(item, action)],
      workflowName: `安装 ${item.name || packageName}`,
      temporary: true,
    });
    if (result) {
      app.agentSoftwareInstallSessionId = String(result.session_id || "");
    } else {
      app.agentSoftwareInstallingPackage = "";
      renderAgentSoftwareCatalog(app.state?.campaign?.software_catalog || {});
    }
  }

  function renderAgentSoftwareCatalog(catalog) {
    const items = Array.isArray(catalog?.items) ? catalog.items : [];
    const category = agentSoftwareCategoryProfiles[app.agentSoftwareCategory]
      ? app.agentSoftwareCategory
      : "overview";
    app.agentSoftwareCategory = category;
    const profile = agentSoftwareCategoryProfiles[category];
    const showOverview = profile.overview === true;
    const search = String($("#agent-software-search-input")?.value || "").trim().toLowerCase();
    const categoryItems = items.filter(profile.matches);
    const visible = categoryItems.filter(item => !search || [
      item.name,
      item.package,
      item.install_source,
      item.install_channel,
      item.official_url,
      item.description,
      ...(Array.isArray(item.supported_engines) ? item.supported_engines : []),
    ].some(value => String(value || "").toLowerCase().includes(search)));
    const list = $("#agent-software-catalog-list");
    if (list) {
      list.innerHTML = visible.length
        ? visible.map(agentSoftwareCardMarkup).join("")
        : `<div class="agent-software-list-empty">${escapeHtml(search ? "当前分类没有匹配的软件" : "当前分类没有软件")}</div>`;
    }
    $$("[data-agent-software-category]").forEach(button => {
      const key = String(button.dataset.agentSoftwareCategory || "overview");
      const active = key === category;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $$("[data-agent-software-count]").forEach(element => {
      const key = String(element.dataset.agentSoftwareCount || "all");
      const definition = agentSoftwareCategoryProfiles[key] || agentSoftwareCategoryProfiles.all;
      element.textContent = String(items.filter(definition.matches).length);
    });
    const overviewPanel = $("#agent-software-overview");
    const catalogPanel = $("#agent-software-catalog");
    if (overviewPanel) overviewPanel.hidden = !showOverview;
    if (catalogPanel) catalogPanel.hidden = showOverview;
    $("#agent-software-browser-kicker").textContent = profile.kicker;
    $("#agent-software-browser-title").textContent = profile.title;
    $("#agent-software-browser-description").textContent = profile.description;
    $("#agent-software-browser-count").textContent = search
      ? `${visible.length} / ${categoryItems.length} 项`
      : `${visible.length} 项`;
    $("#agent-software-total").textContent = String(catalog?.total ?? items.length);
    $("#agent-software-project-count").textContent = String(catalog?.project_count ?? 0);
    $("#agent-software-external-count").textContent = String(catalog?.external_count ?? 0);
    $("#agent-software-verified-count").textContent = String(catalog?.verified_count ?? 0);
    $("#agent-software-failed-count").textContent = `${catalog?.failed_count ?? 0} 项待处理`;
    $("#agent-software-pending-count").textContent = String(catalog?.pending_count ?? 0);
    $("#agent-config-tab-software-meta").textContent = catalog?.available === false
      ? "目录不可用"
      : `${catalog?.total ?? items.length} 项软件`;
    const refreshButton = $("#agent-software-refresh-button");
    if (refreshButton) {
      refreshButton.disabled = (
        app.agentSoftwareAssetsLoading
        || !readyAgentSoftwareDevice()
        || agentSoftwareAutomationRunning()
      );
      refreshButton.textContent = app.agentSoftwareAssetsLoading
        ? "正在读取图标与安装状态…"
        : "刷新图标与安装状态";
    }

    const run = catalog?.validation_run || {};
    const runStatus = String(run.status || "not_checked");
    const runPresentation = {
      running: "预备验证运行中",
      completed: "最近预备验证已完成",
      completed_with_warnings: "最近预备验证有待处理项",
      failed: "最近预备验证未通过",
      operator_stopped: "最近预备验证已停止",
      device_unavailable: "最近验证时设备不可用",
      device_locked: "最近验证时设备被锁定",
      not_checked: "尚未运行预备验证",
    }[runStatus] || `最近预备验证：${runStatus}`;
    $("#agent-software-validation-title").textContent = runPresentation;
    const timestamp = Number(run.finished_at || run.started_at || 0);
    const timeText = timestamp
      ? new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false })
      : "暂无运行时间";
    $("#agent-software-validation-detail").textContent = runStatus === "running"
      ? `${timeText} 开始 · 当前按安装、初始化、Qwen 正常 workflow 三层验证`
      : `${timeText} · 只有 Qwen 正常测试流程完成才标记为已通过`;
    $("#agent-software-validation").className = `agent-software-validation ${escapeHtml(runStatus)}`;
    void loadAgentSoftwareAssets(catalog);
  }

  function agentCampaignConfigBadges(items = []) {
    return items
      .filter(item => item && item.label)
      .map(item => `<span class="${escapeHtml(item.tone || "")}">${escapeHtml(item.label)}</span>`)
      .join("");
  }

  function agentCampaignTaskSummary(tasks = []) {
    const items = Array.isArray(tasks) ? tasks : [];
    if (!items.length) return "无独立 Agent 子任务";
    return items.map(task => String(task.name || task.id || "未命名任务")).join("、");
  }

  function agentCampaignSectionId(prefix, value, index) {
    const slug = String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 36);
    return `${prefix}-${index + 1}${slug ? `-${slug}` : ""}`;
  }

  function agentPreparationConfigMarkup(stage) {
    const settingGroups = (stage.setting_groups || []).map((group, index) => ({
      id: agentCampaignSectionId("settings", group.id || group.label, index),
      directoryGroup: "设备配置",
      kicker: "ANDROID SETTINGS",
      title: String(group.label || `设置组 ${index + 1}`),
      description: String(group.purpose || "阶段开始前需要确认的 Android 设置。"),
      count: `${group.count ?? (group.items || []).length} 项`,
      content: `
        <div class="agent-campaign-setting-list">
          ${(group.items || []).map(item => `
            <article>
              <div><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.id)}</code></div>
              <span>${escapeHtml(item.value)}</span>
              <em class="${item.required ? "required" : "optional"}">${item.required ? "必需" : "可选"}</em>
            </article>`).join("")}
        </div>`,
    }));

    const installSets = Array.isArray(stage.install_sets) ? stage.install_sets : [];
    const installSection = {
      id: "project-packages",
      directoryGroup: "软件准备",
      kicker: "PROJECT PACKAGES",
      title: "固定版本安装包",
      description: "已安装则跳过；缺失时由宿主使用本地 APK / APKS。",
      count: `${installSets.length} 项`,
      content: `
        <div class="agent-campaign-config-items compact">
          ${installSets.map(item => `
            <article class="agent-campaign-config-item">
              <header><div><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.package)}</code></div>${agentCampaignConfigBadges([{ label: item.required ? "required" : "optional", tone: item.required ? "required" : "" }])}</header>
              <p>${escapeHtml(item.source_name || item.source)}</p>
            </article>`).join("")}
        </div>`,
    };

    const appGroups = (stage.app_groups || []).map((group, index) => ({
      id: agentCampaignSectionId("software", group.id || group.label, index),
      directoryGroup: "软件准备",
      kicker: "SOFTWARE BASELINE",
      title: String(group.label || `软件组 ${index + 1}`),
      description: String(group.purpose || "安装、初始化并复验软件正常流程。"),
      count: `${group.count ?? (group.items || []).length} 项`,
      content: `
        <div class="agent-campaign-config-items">
          ${(group.items || []).map(item => {
            const setupTasks = agentCampaignTaskSummary(item.setup_tasks);
            const workflowIds = (item.workflow_ids || []).join("、");
            const permissions = Array.isArray(item.permissions) ? item.permissions.length : 0;
            return `
              <article class="agent-campaign-config-item${item.workflow_mapped ? "" : " warning"}">
                <header>
                  <div><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.package)}</code></div>
                  <div class="agent-campaign-config-badges">${agentCampaignConfigBadges([
                    { label: item.required ? "required" : "optional", tone: item.required ? "required" : "" },
                    { label: item.workflow_mapped ? `${item.workflow_ids.length} workflow` : "未映射", tone: item.workflow_mapped ? "verified" : "warning" },
                  ])}</div>
                </header>
                <p>${escapeHtml(item.install_source || item.install_channel || "未声明安装来源")}</p>
                <dl>
                  <div><dt>首启目的</dt><dd>${escapeHtml(setupTasks)}</dd></div>
                  <div><dt>功能复验</dt><dd>${escapeHtml(workflowIds || "没有对应阶段 2 workflow")}</dd></div>
                  <div><dt>授权边界</dt><dd>${item.allow_terms_acceptance ? "允许接受应用协议" : "不允许代为接受协议"} · ${permissions ? `${permissions} 项显式权限` : "不授予运行时权限"}</dd></div>
                </dl>
              </article>`;
          }).join("")}
        </div>`,
    }));

    return [...settingGroups, installSection, ...appGroups];
  }

  function agentTestConfigMarkup(stage) {
    return (stage.workflow_groups || []).map((group, index) => ({
      id: agentCampaignSectionId("workflows", group.id || group.label, index),
      directoryGroup: "测试流程",
      kicker: "WORKFLOW GROUP",
      title: String(group.label || `Workflow 组 ${index + 1}`),
      description: String(group.purpose || "正式测试期间循环调度的业务流程。"),
      count: `${group.count ?? (group.items || []).length} 项`,
      content: `
        <div class="agent-campaign-config-items">
          ${(group.items || []).map(item => {
            const contract = item.contract || {};
            const requiredActions = (contract.required_actions || [])
              .map(action => `${action.label || action.actions?.join("/") || "动作"} ≥ ${action.minimum || 1}`)
              .join("；");
            const actionLimits = [
              ...(item.initialization_tasks || []).flatMap(task =>
                (task.action_limits || []).map(limit => ({ ...limit, phase: "初始化" }))
              ),
              ...(item.tasks || []).flatMap(task =>
                (task.action_limits || []).map(limit => ({ ...limit, phase: "主任务" }))
              ),
            ].map(limit => {
              const label = limit.label || limit.actions?.join("/") || "动作";
              const maximum = Number.isFinite(Number(limit.maximum)) ? Number(limit.maximum) : 0;
              const signatureMaximum = Number(limit.maximum_per_signature || 0);
              const signatureNote = signatureMaximum > 0
                ? ` · 同参数 ≤ ${signatureMaximum}`
                : "";
              return `${limit.phase}：${label} ≤ ${maximum}${signatureNote}`;
            }).join("；");
            return `
              <article class="agent-campaign-config-item workflow">
                <header>
                  <div><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.id)} · ${escapeHtml(item.package)}</code></div>
                  <div class="agent-campaign-config-badges">${agentCampaignConfigBadges([
                    { label: item.required ? "required" : "optional", tone: item.required ? "required" : "" },
                    { label: item.automation_engine || "默认引擎", tone: "engine" },
                    { label: item.repeat_after_success ? "持续循环" : "成功一次", tone: item.repeat_after_success ? "repeat" : "" },
                  ])}</div>
                </header>
                <dl>
                  <div><dt>入口目的</dt><dd>${escapeHtml(contract.entry_state || agentCampaignTaskSummary(item.initialization_tasks))}</dd></div>
                  <div><dt>成功证据</dt><dd>${escapeHtml(contract.success_evidence || "以任务 Prompt 和最新截图为准")}</dd></div>
                  <div><dt>动作下限</dt><dd>${escapeHtml(requiredActions || "未声明成功动作下限")}</dd></div>
                  <div><dt>动作上限</dt><dd>${escapeHtml(actionLimits || "未声明任务动作上限")}</dd></div>
                  <div><dt>失败编排</dt><dd>${escapeHtml(`冷却 ${item.retry_cooldown_s}s · 连续 ${item.quarantine_after_failures} 次后隔离 · 登录策略 ${contract.login_policy || "forbidden"}`)}</dd></div>
                </dl>
              </article>`;
          }).join("")}
        </div>`,
    }));
  }

  function agentCampaignWarningsMarkup(warnings = []) {
    const items = Array.isArray(warnings) ? warnings : [];
    if (!items.length) {
      return `
        <div class="agent-campaign-audit-state verified">
          <strong>未发现阶段级配置冲突</strong>
          <p>静态 JSON 审计已通过；运行结果仍以实时宿主验收为准。</p>
        </div>`;
    }
    return `
      <div class="agent-campaign-warning-list">
        ${items.map(item => `
          <article class="${escapeHtml(item.severity || "warning")}">
            <strong>${escapeHtml(item.title)}</strong>
            <p>${escapeHtml(item.detail)}</p>
            <span>${escapeHtml((item.items || []).join("、"))}</span>
          </article>`).join("")}
      </div>`;
  }

  function agentCampaignListMarkup(items = []) {
    const values = Array.isArray(items) ? items : [];
    return `<ol class="agent-campaign-policy-list">${values.length
      ? values.map(item => `<li>${escapeHtml(item)}</li>`).join("")
      : "<li>当前阶段没有额外声明。</li>"}</ol>`;
  }

  function agentCampaignDirectoryMarkup(sections, activeSection, stageId) {
    const groups = new Map();
    sections.forEach(section => {
      const group = String(section.directoryGroup || "配置内容");
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(section);
    });
    return [...groups.entries()].map(([group, items]) => `
      <section class="agent-module-directory-group">
        <small>${escapeHtml(group)}</small>
        ${items.map(section => {
          const active = section.id === activeSection;
          return `
            <button class="${active ? "active" : ""}" type="button" data-agent-campaign-section="${escapeHtml(section.id)}" aria-pressed="${active}" aria-controls="agent-campaign-section-${escapeHtml(stageId)}-${escapeHtml(section.id)}" tabindex="${active ? 0 : -1}">
              <span><small>${escapeHtml(section.kicker)}</small><strong>${escapeHtml(section.title)}</strong></span><em>${escapeHtml(section.count || "")}</em>
            </button>`;
        }).join("")}
      </section>`).join("");
  }

  function agentCampaignSectionsMarkup(sections, activeSection, stageId) {
    return sections.map(section => {
      const active = section.id === activeSection;
      return `
        <section class="agent-module-section agent-campaign-module-section ${escapeHtml(section.tone || "")}" id="agent-campaign-section-${escapeHtml(stageId)}-${escapeHtml(section.id)}" data-agent-campaign-section-panel="${escapeHtml(section.id)}"${active ? "" : " hidden"}>
          <header class="agent-module-section-heading">
            <div><small>${escapeHtml(section.kicker)}</small><strong>${escapeHtml(section.title)}</strong><p>${escapeHtml(section.description)}</p></div>
            ${section.count ? `<span>${escapeHtml(section.count)}</span>` : ""}
          </header>
          ${section.content}
        </section>`;
    }).join("");
  }

  function renderAgentCampaignConfig(overview = {}) {
    const sourceName = $("#agent-campaign-config-source-name");
    const sourcePath = $("#agent-campaign-config-source-path");
    const sourceState = $("#agent-campaign-config-source-state");
    const panel = $("#agent-campaign-stage-panel");
    if (!sourceName || !sourcePath || !sourceState || !panel) return;
    sourceName.textContent = overview.available
      ? `${overview.source_name || "Campaign JSON"} · v${overview.version || "?"}`
      : "Campaign JSON 不可用";
    sourcePath.textContent = String(overview.source_path || "--");
    sourceState.textContent = overview.available ? "JSON BASELINE" : "CONFIG ERROR";
    sourceState.classList.toggle("error", !overview.available);
    const stages = overview.stages || {};
    const fallbackStage = stages.prepare ? "prepare" : (stages.test ? "test" : "");
    const stageId = stages[app.agentCampaignConfigStage]
      ? app.agentCampaignConfigStage
      : fallbackStage;
    app.agentCampaignConfigStage = stageId || "prepare";
    $$('[data-agent-campaign-stage]').forEach(button => {
      const active = button.dataset.agentCampaignStage === stageId;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    const prepareStage = stages.prepare || {};
    const testStage = stages.test || {};
    const prepareAppCount = Number(
      (prepareStage.metrics || []).find(item => item.label === "预备应用")?.value || 0
    );
    const testWorkflowCount = Number(
      (testStage.metrics || []).find(item => item.label === "workflow")?.value || 0
    );
    $("#agent-campaign-stage-prepare-meta").textContent = `${prepareAppCount} 应用 · 设置 / 安装 / 复验`;
    $("#agent-campaign-stage-test-meta").textContent = `${testWorkflowCount} workflow · 循环 / 录制 / 验收`;
    $("#agent-config-tab-campaign-meta").textContent = overview.available
      ? `${prepareAppCount} → ${testWorkflowCount}`
      : "配置不可用";
    if (!overview.available || !stageId || !stages[stageId]) {
      const errorMessage = String(overview.error || "无法读取两阶段 Campaign JSON。");
      const errorSignature = `error:${stageId}:${errorMessage}`;
      if (app.agentCampaignConfigRenderSignature !== errorSignature) {
        panel.innerHTML = `<p class="agent-campaign-config-empty">${escapeHtml(errorMessage)}</p>`;
        app.agentCampaignConfigRenderSignature = errorSignature;
      }
      return;
    }

    const stage = stages[stageId];
    const configSections = stageId === "prepare"
      ? agentPreparationConfigMarkup(stage)
      : agentTestConfigMarkup(stage);
    const stageWarnings = Array.isArray(stage.warnings) ? stage.warnings : [];
    const stagePolicies = Array.isArray(stage.policies) ? stage.policies : [];
    const stageAcceptance = Array.isArray(stage.acceptance) ? stage.acceptance : [];
    const stageSections = [
      {
        id: "overview",
        directoryGroup: "阶段理解",
        kicker: stage.kicker || "CAMPAIGN STAGE",
        title: "阶段概览",
        description: "先确认阶段目的、规模和只读 JSON 基线。",
        count: `${(stage.metrics || []).length} 指标`,
        content: `
          <section class="agent-campaign-stage-purpose">
            <div><small>${escapeHtml(stage.kicker || "CAMPAIGN STAGE")}</small><strong>${escapeHtml(stage.label)}</strong><p>${escapeHtml(stage.purpose)}</p></div>
            <span>JSON 只读基线</span>
          </section>
          <section class="agent-campaign-metrics" aria-label="${escapeHtml(stage.label)}配置摘要">
            ${(stage.metrics || []).map(metric => `
              <article><small>${escapeHtml(metric.label)}</small><strong>${escapeHtml(metric.value)}</strong><span>${escapeHtml(metric.detail)}</span></article>`).join("")}
          </section>`,
      },
      {
        id: "flow",
        directoryGroup: "阶段理解",
        kicker: "EXECUTION FLOW",
        title: "执行链路与每步目的",
        description: "按真实执行顺序理解阶段编排和每一步的职责。",
        count: `${(stage.flow || []).length} 步`,
        content: `
          <section class="agent-campaign-flow" aria-label="执行链路与每步目的">
            <ol>
              ${(stage.flow || []).map((item, index) => `
                <li><i>${index + 1}</i><div><strong>${escapeHtml(item.label)}</strong><p>${escapeHtml(item.purpose)}</p></div></li>`).join("")}
            </ol>
          </section>`,
      },
      ...configSections,
      {
        id: "audit",
        directoryGroup: "运行约束",
        kicker: "CONFIG AUDIT",
        title: "配置审计",
        description: "集中查看静态配置冲突、缺失映射和需要处理的风险。",
        count: stageWarnings.length ? `${stageWarnings.length} 项` : "已通过",
        content: agentCampaignWarningsMarkup(stageWarnings),
      },
      {
        id: "policies",
        directoryGroup: "运行约束",
        kicker: "OPERATING BOUNDARY",
        title: "执行边界",
        description: "Agent 和宿主在本阶段必须共同遵守的安全边界。",
        count: `${stagePolicies.length} 条`,
        content: agentCampaignListMarkup(stagePolicies),
      },
      {
        id: "acceptance",
        directoryGroup: "运行约束",
        kicker: "STRICT ACCEPTANCE",
        title: "验收条件",
        description: "阶段结束时用于判断是否完整通过的统一标准。",
        count: `${stageAcceptance.length} 条`,
        tone: "acceptance",
        content: agentCampaignListMarkup(stageAcceptance),
      },
    ];
    const sectionState = app.agentCampaignConfigSection && typeof app.agentCampaignConfigSection === "object"
      ? app.agentCampaignConfigSection
      : { prepare: "overview", test: "overview" };
    app.agentCampaignConfigSection = sectionState;
    const requestedSection = String(sectionState[stageId] || "overview");
    const activeSection = stageSections.some(section => section.id === requestedSection)
      ? requestedSection
      : "overview";
    sectionState[stageId] = activeSection;
    const renderSignature = JSON.stringify({
      source: overview.source_path || overview.source_name || "",
      version: overview.version || overview.revision || "",
      stageId,
      activeSection,
      stage,
    });
    if (
      app.agentCampaignConfigRenderSignature === renderSignature
      && panel.dataset.agentCampaignStage === stageId
      && panel.querySelector(".agent-campaign-config-layout")
    ) return;
    const previousDirectory = panel.querySelector(".agent-campaign-config-directory");
    const preservedScrollTop = panel.dataset.agentCampaignStage === stageId
      ? Number(previousDirectory?.scrollTop || 0)
      : 0;
    panel.setAttribute(
      "aria-labelledby",
      stageId === "prepare" ? "agent-campaign-stage-tab-prepare" : "agent-campaign-stage-tab-test",
    );
    panel.innerHTML = `
      <div class="agent-module-layout agent-campaign-config-layout">
        <aside class="agent-module-directory agent-campaign-config-directory">
          <div class="agent-module-directory-heading"><small>${escapeHtml(stageId === "prepare" ? "预备阶段" : "正式测试")}</small><strong>配置目录</strong><p>选择一类内容，右侧只显示当前配置。</p></div>
          <nav aria-label="${escapeHtml(stage.label)}配置分类">${agentCampaignDirectoryMarkup(stageSections, activeSection, stageId)}</nav>
        </aside>
        <div class="agent-module-content agent-campaign-config-groups" aria-label="${escapeHtml(stage.label)}配置内容">
          ${agentCampaignSectionsMarkup(stageSections, activeSection, stageId)}
        </div>
      </div>`;
    panel.dataset.agentCampaignStage = stageId;
    app.agentCampaignConfigRenderSignature = renderSignature;
    const nextDirectory = panel.querySelector(".agent-campaign-config-directory");
    if (nextDirectory && preservedScrollTop > 0) {
      nextDirectory.scrollTop = Math.min(
        preservedScrollTop,
        Math.max(0, nextDirectory.scrollHeight - nextDirectory.clientHeight),
      );
    }
  }

  function renderAdbAgent(state) {
    const adbAgent = state?.adb_agent || {};
    const campaign = state?.campaign || {};
    const automationSurface = state?.automation_surface === "campaign" ? "campaign" : "agent";
    const agent = automationSurface === "campaign" ? campaign : adbAgent;
    const defaults = adbAgent.defaults || {};
    const sessionSource = agent.session_id ? agent : defaults;
    app.agentDefaultSystemPrompt = String(defaults.system_prompt || app.agentDefaultSystemPrompt || "");
    app.agentDefaultSystemPromptVersion = String(
      defaults.system_prompt_version || app.agentDefaultSystemPromptVersion || "内置规则"
    );
    populateAgentTaskTemplates(defaults.task_templates, campaign.stage_config || {});
    populateAgentAutomationEngines(defaults.automation_engines);
    populateAgentModelProviders(defaults.model_providers);
    const installAgentStatus = String(adbAgent.status || "idle");
    if (
      app.agentSoftwareInstallSessionId
      && String(adbAgent.session_id || "") === app.agentSoftwareInstallSessionId
      && ["completed", "completed_with_warnings", "stopped", "take_over", "task_failed", "max_steps", "error"].includes(installAgentStatus)
    ) {
      app.agentSoftwareInstallSessionId = "";
      app.agentSoftwareInstallingPackage = "";
      app.agentSoftwareAssetsSignature = "";
    }
    renderAgentCampaignConfig(campaign.stage_config || {});
    renderAgentSoftwareCatalog(campaign.software_catalog || {});
    const editorSessionId = agent.session_id
      ? `${automationSurface}:${String(agent.session_id)}`
      : "";
    const agentSessionChanged = Boolean(
      editorSessionId && editorSessionId !== app.agentEditorSessionId
    );
    const temporarySession = Boolean(
      automationSurface === "agent"
      && editorSessionId
      && (agent.temporary_task === true || editorSessionId === app.agentTemporarySessionId)
    );
    if (agentSessionChanged && temporarySession) {
      app.agentEditorSessionId = editorSessionId;
    }
    if (!app.agentDefaultsApplied || (agentSessionChanged && !temporarySession)) {
      const useStoredDefaults = !agent.session_id;
      const storedValue = (key, fallback) => (
        useStoredDefaults
          ? (localStorage.getItem(key) || String(fallback ?? ""))
          : String(fallback ?? "")
      );
      const requestedProvider = storedValue(
        "mobile-profiler-agent-model-provider",
        sessionSource.model_provider || defaults.model_provider || "openai_compatible",
      );
      $("#agent-model-provider-input").value = app.agentProviderProfiles.has(requestedProvider)
        ? requestedProvider
        : String(defaults.model_provider || "openai_compatible");
      const requestedEngine = storedValue(
        "mobile-profiler-agent-automation-engine",
        sessionSource.automation_engine || defaults.automation_engine || "vision",
      );
      const requestedEngineProfile = app.agentEngineProfiles?.get(requestedEngine);
      $("#agent-automation-engine-input").value = requestedEngineProfile?.available !== false
        ? requestedEngine
        : String(defaults.automation_engine || "vision");
      $("#agent-api-base-input").value = storedValue(
        "mobile-profiler-agent-api-base",
        sessionSource.api_base_url,
      );
      $("#agent-model-input").value = storedValue(
        "mobile-profiler-agent-model",
        sessionSource.model,
      );
      $("#agent-step-delay-input").value = storedValue(
        "mobile-profiler-agent-step-delay",
        sessionSource.step_delay_s || 1.2,
      );
      $("#agent-timeout-input").value = storedValue(
        "mobile-profiler-agent-timeout",
        sessionSource.request_timeout_s || 90,
      );
      $("#agent-api-key-mode-input").value = storedValue(
        "mobile-profiler-agent-api-key-mode",
        sessionSource.api_key_mode || defaults.api_key_mode || "bearer",
      );
      $("#agent-thinking-mode-input").value = storedValue(
        "mobile-profiler-agent-thinking-mode",
        sessionSource.model_thinking_mode || defaults.model_thinking_mode || "auto",
      );
      applyAgentProviderPresentation();
      $("#agent-workflow-name-input").value = String(sessionSource.workflow_name || "ADB 测试流程");
      const storedSystemPrompt = useStoredDefaults
        ? localStorage.getItem(agentSystemPromptStorageKey)
        : null;
      const editorSystemPrompt = String(
        storedSystemPrompt !== null
          ? storedSystemPrompt
          : (sessionSource.system_prompt || defaults.system_prompt || "")
      );
      $("#agent-system-prompt-input").value = editorSystemPrompt;
      app.agentSavedSystemPrompt = normalizeAgentSystemPrompt(editorSystemPrompt);
      refreshAgentSystemPromptVersion();
      const sessionTasks = Array.isArray(agent.tasks) && agent.tasks.length
        ? agent.tasks
        : (agent.task ? [defaultAgentTask({ name: "任务 1", prompt: agent.task, max_steps: agent.max_steps })] : []);
      renderAgentTaskEditor(sessionTasks);
      if (agentSessionChanged && automationSurface === "campaign") {
        const campaignTemplate = app.agentServerTemplates.find(item => (
          item.kind === "campaign" && String(item.campaign_stage) === String(agent.campaign_stage)
        ));
        app.agentCampaignRuntimeOverrides = Boolean(
          agent.runtime_task_overrides || agent.runtime_system_prompt_override
        );
        setAgentTemplateMode(campaignTemplate || {
          kind: "campaign",
          campaign_stage: agent.campaign_stage,
          id: `campaign-${agent.campaign_stage || "stage"}`,
        });
        $("#agent-loop-workflow-input").checked = Boolean(agent.loop_enabled);
        refreshAgentTaskOrder();
        $("#agent-task-template-select").value = String(campaignTemplate?.id || "");
      } else if (agentSessionChanged) {
        setAgentTemplateMode(agentTemplateById(app.agentSelectedTemplateId));
      } else if (!agent.session_id && !app.agentTemplateInitialized) {
        const templates = [...app.agentServerTemplates, ...app.agentCustomTemplates];
        const initialTemplate = agentTemplateById(app.agentSelectedTemplateId)
          || templates.find(item => item.kind === "campaign")
          || templates[0];
        if (initialTemplate) applyAgentTemplate(initialTemplate);
        app.agentTemplateInitialized = true;
      }
      app.agentDefaultsApplied = true;
      app.agentEditorSessionId = editorSessionId;
      setAgentPromptDirty(false);
    }

    const status = String(agent.status || "idle");
    const statusPresentation = {
      idle: ["等待任务", "选择 Android 设备后开始"],
      starting: ["正在启动", agent.message || "准备截图与模型客户端"],
      running: ["任务运行中", agent.message || "截图、决策与 ADB 动作闭环"],
      stopping: ["正在停止", "当前模型请求返回后安全退出"],
      completed: ["流程已完成", agent.message || "全部子任务已完成"],
      completed_with_warnings: ["流程完成，有跳过", agent.message || "部分子任务按失败策略继续"],
      stopped: ["流程已停止", agent.message || "用户已停止流程"],
      take_over: ["需要人工接管", agent.message || "模型无法安全继续"],
      task_failed: ["子任务失败", agent.message || "流程已按失败策略停止"],
      max_steps: ["达到步骤上限", agent.message || "任务未确认完成"],
      error: ["运行失败", agent.error || agent.message || "请查看闭环日志"],
    }[status] || [status.toUpperCase(), agent.message || "--"];
    const badge = $("#agent-status-badge");
    badge.className = `agent-status-badge ${escapeHtml(status)}`;
    badge.querySelector("strong").textContent = statusPresentation[0];
    $("#agent-status-detail").textContent = statusPresentation[1];

    const revision = Number(agent.screenshot_revision || 0);
    const image = $("#agent-screen-image");
    const placeholder = $("#agent-screen-placeholder");
    if (agent.screenshot_available && agent.screenshot_url) {
      if (revision !== app.agentScreenshotRevision) {
        app.agentScreenshotRevision = revision;
        image.src = `${agent.screenshot_url}?revision=${encodeURIComponent(revision)}`;
      }
      image.hidden = false;
      placeholder.hidden = true;
      $("#agent-screen-meta").textContent = `${agent.screenshot_width || "--"} × ${agent.screenshot_height || "--"} · revision ${revision}`;
    } else {
      app.agentScreenshotRevision = -1;
      image.hidden = true;
      image.removeAttribute("src");
      placeholder.hidden = false;
      $("#agent-screen-meta").textContent = "尚未截图";
    }

    $("#agent-task-progress-value").textContent = `${agent.task_index || 0} / ${agent.task_count || 0}`;
    $("#agent-current-task-value").textContent = agent.current_task?.name || "尚未开始";
    $("#agent-step-value").textContent = `${agent.step || 0} / ${agent.max_steps || defaults.max_steps || 0}`;
    $("#agent-phase-value").textContent = String(agent.phase || "idle").toUpperCase();
    $("#agent-launch-current-task").textContent = agent.current_task?.name || "尚未开始";
    $("#agent-launch-task-position").textContent = `${agent.task_index || 0} / ${agent.task_count || 0}`;
    $("#agent-launch-current-step").textContent = `${agent.step || 0} / ${agent.max_steps || defaults.max_steps || 0}`;
    $("#agent-launch-step-state").textContent = String(agent.phase || "idle").toUpperCase();
    $("#agent-launch-state").textContent = statusPresentation[0];
    $("#agent-launch-state").className = status;
    $("#agent-launch-progress-detail").textContent = status === "idle"
      ? "选择 Android 设备和任务后即可开始。"
      : statusPresentation[1];
    const activeProvider = String(agent.model_provider || defaults.model_provider || "openai_compatible");
    const activeProviderLabel = app.agentProviderProfiles.get(activeProvider)?.label || activeProvider;
    const activeEngine = String(agent.automation_engine || defaults.automation_engine || "vision");
    const activeEngineLabel = app.agentEngineProfiles?.get(activeEngine)?.label || activeEngine;
    $("#agent-model-value").textContent = agent.model || defaults.model || "--";
    $("#agent-model-value").title = `${activeEngineLabel} · ${activeProviderLabel}`;
    $("#agent-latency-value").textContent = finite(agent.latest_request_s)
      ? `最近 ${Number(agent.latest_request_s).toFixed(1)} 秒`
      : `${activeEngineLabel} · ${activeProviderLabel}`;
    $("#agent-token-value").textContent = `${agent.prompt_tokens || 0} / ${agent.completion_tokens || 0}`;
    $("#agent-elapsed-value").textContent = formatDuration(agent.elapsed_s || 0);
    $("#agent-device-value").textContent = agent.device || selectedDevice() || "未选择设备";
    $("#agent-reasoning-output").textContent = agent.latest_reasoning || "任务启动后显示模型的简短判断。";
    $("#agent-action-output").textContent = agent.latest_action
      ? JSON.stringify(agent.latest_action, null, 2)
      : "--";
    $("#agent-action-result").textContent = agent.latest_action_result || "尚未执行动作";
    $("#agent-action-state").textContent = status === "idle"
      ? "等待模型"
      : `${status.toUpperCase()} · ${String(agent.phase || "--").toUpperCase()}`;
    renderAgentTaskResults(agent);

    const terminal = ["completed", "completed_with_warnings", "stopped", "take_over", "task_failed", "max_steps", "error"].includes(status);
    const completion = $("#agent-completion-message");
    completion.hidden = !terminal;
    completion.classList.toggle("error", ["take_over", "task_failed", "max_steps", "error"].includes(status));
    completion.classList.toggle("warning", status === "completed_with_warnings");
    completion.textContent = status === "error"
      ? `${agent.message || "ADB Agent 运行失败"}${agent.error ? `：${agent.error}` : ""}`
      : agent.message || statusPresentation[0];

    const logs = Array.isArray(agent.logs) ? agent.logs : [];
    $("#agent-log-output").innerHTML = logs.length
      ? logs.map(entry => {
        const level = String(entry.level || "info");
        const timestamp = finite(entry.time)
          ? new Date(Number(entry.time) * 1000).toLocaleTimeString("zh-CN", { hour12: false })
          : "--:--:--";
        return `<div class="agent-log-line ${escapeHtml(level)}"><time>${escapeHtml(timestamp)}</time><em>${escapeHtml(level.toUpperCase())}</em><p>${escapeHtml(entry.message || "")}</p></div>`;
      }).join("")
      : '<div class="agent-log-empty">等待任务启动</div>';
    const outputDirectory = $("#agent-output-directory");
    outputDirectory.textContent = agent.output_dir
      ? `证据目录 · ${agent.output_dir}`
      : "运行后保存截图与 events.jsonl";
    outputDirectory.title = agent.output_dir || "";

    const selected = (state?.devices || []).find(item => item.serial === selectedDevice());
    const androidReady = Boolean(
      selected
      && devicePlatform(selected) === "android"
      && selected.state === "device"
    );
    const running = Boolean(agent.running || ["starting", "stopping"].includes(status));
    const campaignPreview = Boolean(app.agentCampaignStage);
    $("#agent-start-button").disabled = running || !androidReady;
    if (!running) $("#agent-start-button").textContent = agentCampaignStartLabel();
    $("#agent-start-button").title = androidReady
      ? ""
      : "请先在右上角选择已授权的 Android ADB 设备";
    $("#agent-stop-button").disabled = !running || status === "stopping";
    $$("#adb-agent-form input, #adb-agent-form textarea, #adb-agent-form select, #adb-agent-form details").forEach(control => {
      if (control.tagName === "DETAILS") {
        control.classList.toggle("disabled", running);
      } else {
        const fixedSinglePassLoop = (
          control.id === "agent-loop-workflow-input"
          && app.agentCampaignStage !== "test"
        );
        control.disabled = running || fixedSinglePassLoop;
      }
    });
    $("#agent-add-task-button").disabled = running || campaignPreview;
    $("#agent-edit-template-button").disabled = running || !app.agentSelectedTemplateId;
    $("#agent-new-template-button").disabled = running;
    $("#agent-select-phone-configuration-button").disabled = running
      || agentTemplateById(app.agentSelectedTemplateId)?.kind === "phone_configuration";
    $("#agent-save-prompt-button").disabled = running;
    $("#agent-run-temporary-task-button").disabled = running || !androidReady;
    $("#agent-temporary-task-input").disabled = running;
    $("#agent-reset-system-prompt-button").disabled = running;
    $("#agent-open-preparation-button").disabled = running;
    if (running) {
      $$('[data-agent-task-action]').forEach(control => { control.disabled = true; });
    } else {
      refreshAgentTaskOrder();
    }

    if (terminal && agent.session_id) {
      const notificationKey = `${agent.session_id}:${status}`;
      if (notificationKey !== app.agentNotificationKey) {
        app.agentNotificationKey = notificationKey;
        notify(
          statusPresentation[0],
          status === "error" ? (agent.error || agent.message || "运行失败") : (agent.message || "任务结束"),
          status === "completed" ? "success" : (status === "completed_with_warnings" || status === "stopped" ? "warning" : "error"),
          9000,
        );
      }
    }
  }

  function render(state) {
    app.state = state;
    const version = String(state?.version || "").trim().replace(/^v/i, "");
    const versionBadge = $("#app-version-badge");
    if (version && versionBadge) {
      versionBadge.textContent = `v${version}`;
      versionBadge.title = `Mobile Profiler ${version}`;
    }
    setServerState(true, state.demo_mode ? "Demo preview enabled" : "Local dashboard");
    const activeIsNewRun = Boolean(
      state.active && state.active.run_name !== app.currentRunName
    );
    if (state.active && (state.active.running || activeIsNewRun)) {
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
    renderAdbAgent(state);
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
      capture_features: app.captureFeaturesOverridden ? captureFeatures : {},
      harmony_high_performance: $("#harmony-high-performance-input").checked,
      title: $("#title-input").value.trim(),
      run_name: $("#run-name-input").value.trim(),
      duration: Number($("#duration-input").value),
      duration_unlimited: app.durationUnlimited,
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

  function readyAndroidAgentDevice() {
    const device = selectedDevice();
    const selected = (app.state?.devices || []).find(item => item.serial === device);
    if (!device || !selected || devicePlatform(selected) !== "android" || selected.state !== "device") {
      notify("请选择 Android ADB 设备", "AI 自动化基础闭环当前只支持已授权的 Android 设备。", "error");
      return "";
    }
    return device;
  }

  function readAgentExecutionModelConfig() {
    const apiInput = $("#agent-api-base-input");
    const modelInput = $("#agent-model-input");
    const invalidModelControl = [
      apiInput,
      $("#agent-step-delay-input"),
      $("#agent-timeout-input"),
    ].find(control => !control.checkValidity());
    if (!apiInput.value.trim() || invalidModelControl) {
      setAgentConfigTab("model");
      notify(
        "模型 API 配置无效",
        apiInput.value.trim() ? "请检查 API 地址、动作等待和请求超时。" : "请填写多模态模型 API 地址。",
        "error",
      );
      (invalidModelControl || apiInput).focus();
      return null;
    }
    if (!modelInput.value.trim()) {
      setAgentConfigTab("model");
      notify("模型不能为空", "请填写支持截图理解与工具调用的多模态模型。", "error");
      modelInput.focus();
      return null;
    }
    const systemPrompt = savedAgentSystemPrompt();
    if (!systemPrompt) {
      setAgentConfigTab("prompt");
      setAgentPromptTab("system");
      notify("System Prompt 不能为空", "请恢复默认 prompt 并点击保存。", "error");
      $("#agent-system-prompt-input").focus();
      return null;
    }
    return {
      system_prompt: systemPrompt,
      automation_engine: $("#agent-automation-engine-input").value,
      model_provider: $("#agent-model-provider-input").value,
      api_base_url: apiInput.value.trim(),
      model: modelInput.value.trim(),
      model_thinking_mode: $("#agent-thinking-mode-input").value,
      api_key: $("#agent-api-key-input").value,
      api_key_mode: $("#agent-api-key-mode-input").value,
      step_delay_s: Number($("#agent-step-delay-input").value),
      request_timeout_s: Number($("#agent-timeout-input").value),
    };
  }

  function rememberAgentExecutionModelConfig(payload) {
    localStorage.setItem("mobile-profiler-agent-automation-engine", payload.automation_engine);
    localStorage.setItem("mobile-profiler-agent-model-provider", payload.model_provider);
    localStorage.setItem("mobile-profiler-agent-api-base", payload.api_base_url);
    localStorage.setItem("mobile-profiler-agent-model", payload.model);
    localStorage.setItem("mobile-profiler-agent-thinking-mode", payload.model_thinking_mode);
    localStorage.setItem("mobile-profiler-agent-api-key-mode", payload.api_key_mode);
    localStorage.setItem("mobile-profiler-agent-step-delay", String(payload.step_delay_s));
    localStorage.setItem("mobile-profiler-agent-timeout", String(payload.request_timeout_s));
  }

  async function startAgentExecution({
    device,
    tasks,
    workflowName,
    campaignStage = "",
    loopEnabled = false,
    runtimeOverrides = false,
    temporary = false,
  }) {
    const modelConfig = readAgentExecutionModelConfig();
    if (!modelConfig) return null;
    const payload = {
      device,
      workflow_name: workflowName,
      ...modelConfig,
      loop_enabled: Boolean(loopEnabled),
    };
    if (campaignStage) {
      payload.stage = campaignStage;
      payload.repeat_workflows = campaignStage === "test" && Boolean(loopEnabled);
      payload.max_rounds = 1;
      payload.runtime_task_overrides = Boolean(runtimeOverrides);
      payload.runtime_system_prompt_override = Boolean(runtimeOverrides);
      if (runtimeOverrides) payload.tasks = tasks;
      else delete payload.system_prompt;
    } else {
      payload.tasks = tasks;
    }
    if (temporary) payload.temporary_task = true;
    const startButton = $("#agent-start-button");
    const temporaryButton = $("#agent-run-temporary-task-button");
    startButton.disabled = true;
    temporaryButton.disabled = true;
    if (temporary) temporaryButton.textContent = "正在启动...";
    else startButton.textContent = "正在启动...";
    try {
      const endpoint = campaignStage ? "/api/campaign/start" : "/api/ai-agent/start";
      const result = await api(endpoint, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      rememberAgentExecutionModelConfig(payload);
      $("#agent-api-key-input").value = "";
      app.agentNotificationKey = "";
      app.agentTemporarySessionId = temporary ? String(result.session_id || "") : "";
      if (app.agentTemporarySessionId) {
        localStorage.setItem(agentTemporarySessionStorageKey, app.agentTemporarySessionId);
      } else {
        localStorage.removeItem(agentTemporarySessionStorageKey);
      }
      app.state = campaignStage
        ? { ...(app.state || {}), campaign: result, automation_surface: "campaign" }
        : { ...(app.state || {}), adb_agent: result, automation_surface: "agent" };
      renderAdbAgent(app.state);
      const providerLabel = app.agentProviderProfiles.get(payload.model_provider)?.label
        || payload.model_provider;
      const engineLabel = app.agentEngineProfiles?.get(payload.automation_engine)?.label
        || payload.automation_engine;
      const phoneConfiguration = !temporary
        && !campaignStage
        && agentTemplateById(app.agentSelectedTemplateId)?.kind === "phone_configuration";
      notify(
        temporary
          ? "临时任务已启动"
          : campaignStage
            ? (campaignStage === "prepare" ? "预备阶段已启动" : "实际测试阶段已启动")
            : phoneConfiguration ? "手机配置检查已启动" : "ADB 测试流程已启动",
        campaignStage === "test"
          ? `${payload.repeat_workflows ? "单轮内循环 workflow" : "单轮内整套任务执行一遍"} · 1 × 7200 秒 · ${engineLabel} · ${providerLabel} · ${payload.model}`
          : `${temporary ? "固定执行一次" : `${tasks.length} 个子任务`} · ${engineLabel} · ${providerLabel} · ${payload.model}`,
        "success",
        6000,
      );
      return result;
    } catch (error) {
      notify(
        temporary
          ? "无法启动临时任务"
          : campaignStage ? "无法启动 Campaign 阶段" : "无法启动 ADB Agent",
        error.message,
        "error",
        9000,
      );
      return null;
    } finally {
      startButton.textContent = agentCampaignStartLabel();
      temporaryButton.textContent = "运行临时任务";
      if (app.state) renderAdbAgent(app.state);
    }
  }

  function bindEvents() {
    $$(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
    $$(".platform-switch [data-platform]").forEach(button => button.addEventListener("click", () => {
      setPlatform(button.dataset.platform);
    }));
    $$(".test-mode-switch [data-test-mode]").forEach(button => button.addEventListener("click", () => {
      setTestMode(button.dataset.testMode);
    }));
    window.addEventListener("hashchange", () => switchView(location.hash.replace("#", "")));
    window.addEventListener("resize", () => requestAnimationFrame(() => {
      renderChart();
    }));
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });

    $("#device-select").addEventListener("change", event => {
      localStorage.setItem(`mobile-profiler-device-${selectedPlatform()}`, event.target.value);
      app.brightnessRequestId += 1;
      app.brightnessDevice = "";
      app.brightnessInfo = null;
      app.brightnessError = "";
      app.brightnessLoading = false;
      app.agentSoftwareAssets = new Map();
      app.agentSoftwareAssetsDevice = "";
      app.agentSoftwareAssetsSignature = "";
      app.brightnessCalibrating = false;
      if (app.scannedAppsDevice && app.scannedAppsDevice !== event.target.value) {
        resetAppScanner({ clearPackage: true });
      }
      renderDevices(app.state);
      renderProbe(app.state);
      renderAdbAgent(app.state);
      updateCaptureFeatureControls();
    });
    $("#unplugged-input").addEventListener("change", () => {
      if (app.state) renderDevices(app.state);
    });

    $("#brightness-refresh").addEventListener("click", () => {
      void refreshBrightnessCapability({ force: true, notifyFailure: true });
    });
    $("#brightness-calibrate").addEventListener("click", () => {
      void calibrateHarmonyBrightness();
    });
    $("#brightness-apply").addEventListener("click", () => {
      void applyBrightnessValue();
    });
    $("#brightness-select").addEventListener("change", () => {
      renderBrightnessControl();
    });
    $("#brightness-input").addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      void applyBrightnessValue();
    });

    $("#agent-automation-engine-input").addEventListener("change", () => {
      applyAgentAutomationEnginePresentation();
    });
    $("#agent-model-provider-input").addEventListener("change", () => {
      applyAgentProviderPresentation({ replaceDefaults: true });
    });
    $("#agent-model-input").addEventListener("input", refreshAgentModelSummary);
    $("#agent-temporary-task-menu-button").addEventListener("click", () => {
      const menu = $("#agent-temporary-task-menu");
      setAgentTemporaryTaskMenu(menu.hidden, { focusInput: menu.hidden });
    });
    $("#agent-close-temporary-task-button").addEventListener("click", () => {
      setAgentTemporaryTaskMenu(false, { returnFocus: true });
    });
    document.addEventListener("click", event => {
      const menu = $("#agent-temporary-task-menu");
      if (menu.hidden || event.target.closest(".agent-panel-heading-tools")) return;
      setAgentTemporaryTaskMenu(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape" || $("#agent-temporary-task-menu").hidden) return;
      event.preventDefault();
      setAgentTemporaryTaskMenu(false, { returnFocus: true });
    });
    $$("[data-agent-config-tab]").forEach(button => {
      button.addEventListener("click", () => setAgentConfigTab(button.dataset.agentConfigTab));
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const currentIndex = agentConfigTabs.indexOf(button.dataset.agentConfigTab);
        const nextIndex = event.key === "Home"
          ? 0
          : (event.key === "End"
            ? agentConfigTabs.length - 1
            : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + agentConfigTabs.length) % agentConfigTabs.length);
        setAgentConfigTab(agentConfigTabs[nextIndex]);
        $(`[data-agent-config-tab="${agentConfigTabs[nextIndex]}"]`).focus();
      });
    });
    $$('[data-agent-run-tab]').forEach(button => {
      button.addEventListener("click", () => {
        setAgentRunConsoleSection(String(button.dataset.agentRunTab || "decision"));
      });
      button.addEventListener("keydown", event => {
        if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const sections = ["decision", "results", "logs"];
        const currentIndex = sections.indexOf(String(button.dataset.agentRunTab || "decision"));
        const forward = ["ArrowDown", "ArrowRight"].includes(event.key);
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? sections.length - 1
            : (currentIndex + (forward ? 1 : -1) + sections.length) % sections.length;
        setAgentRunConsoleSection(sections[nextIndex], { focus: true });
      });
    });
    $$('[data-agent-campaign-stage]').forEach(button => {
      button.addEventListener("click", () => {
        app.agentCampaignConfigStage = String(button.dataset.agentCampaignStage || "prepare");
        renderAgentCampaignConfig(app.state?.campaign?.stage_config || {});
      });
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const stages = ["prepare", "test"];
        const currentIndex = stages.indexOf(String(button.dataset.agentCampaignStage || "prepare"));
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? stages.length - 1
            : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + stages.length) % stages.length;
        app.agentCampaignConfigStage = stages[nextIndex];
        renderAgentCampaignConfig(app.state?.campaign?.stage_config || {});
        $(`[data-agent-campaign-stage="${stages[nextIndex]}"]`)?.focus();
      });
    });
    const campaignStagePanel = $("#agent-campaign-stage-panel");
    campaignStagePanel.addEventListener("click", event => {
      const button = event.target.closest("[data-agent-campaign-section]");
      if (!button) return;
      const section = String(button.dataset.agentCampaignSection || "overview");
      app.agentCampaignConfigSection[app.agentCampaignConfigStage] = section;
      renderAgentCampaignConfig(app.state?.campaign?.stage_config || {});
    });
    campaignStagePanel.addEventListener("keydown", event => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      const button = event.target.closest("[data-agent-campaign-section]");
      if (!button) return;
      event.preventDefault();
      const buttons = [...campaignStagePanel.querySelectorAll("[data-agent-campaign-section]")];
      const currentIndex = buttons.indexOf(button);
      const nextIndex = event.key === "Home"
        ? 0
        : event.key === "End"
          ? buttons.length - 1
          : (currentIndex + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      const section = String(buttons[nextIndex]?.dataset.agentCampaignSection || "overview");
      app.agentCampaignConfigSection[app.agentCampaignConfigStage] = section;
      renderAgentCampaignConfig(app.state?.campaign?.stage_config || {});
      campaignStagePanel.querySelector(`[data-agent-campaign-section="${section}"]`)?.focus();
    });
    $("#agent-software-search-input").addEventListener("input", () => {
      renderAgentSoftwareCatalog(app.state?.campaign?.software_catalog || {});
    });
    $$("[data-agent-software-category]").forEach(button => {
      button.addEventListener("click", () => {
        app.agentSoftwareCategory = String(button.dataset.agentSoftwareCategory || "overview");
        renderAgentSoftwareCatalog(app.state?.campaign?.software_catalog || {});
      });
      button.addEventListener("keydown", event => {
        if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const buttons = $$("[data-agent-software-category]");
        const currentIndex = buttons.indexOf(button);
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? buttons.length - 1
            : (currentIndex + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        app.agentSoftwareCategory = String(buttons[nextIndex]?.dataset.agentSoftwareCategory || "overview");
        renderAgentSoftwareCatalog(app.state?.campaign?.software_catalog || {});
        buttons[nextIndex]?.focus();
      });
    });
    $("#agent-software-refresh-button").addEventListener("click", () => {
      app.agentSoftwareAssetsSignature = "";
      void loadAgentSoftwareAssets(
        app.state?.campaign?.software_catalog || {},
        { force: true, notifyFailure: true },
      );
    });
    $("#agent-software-catalog-list").addEventListener("click", event => {
      const button = event.target.closest("[data-agent-software-install]");
      if (!button || button.disabled) return;
      const packageName = String(button.dataset.agentSoftwarePackage || "");
      const item = (app.state?.campaign?.software_catalog?.items || [])
        .find(candidate => String(candidate.package || "") === packageName);
      if (!item) {
        notify("软件目录已变化", "请刷新页面后重新选择安装操作。", "error");
        return;
      }
      void installAgentSoftware(item, String(button.dataset.agentSoftwareInstall || ""));
    });
    $("#agent-open-preparation-button").addEventListener("click", () => {
      const template = [...app.agentServerTemplates, ...app.agentCustomTemplates]
        .find(item => item.kind === "campaign" && item.campaign_stage === "prepare");
      if (!template) {
        notify("预备模板不可用", "当前服务没有提供预备阶段任务。", "error");
        return;
      }
      if (app.agentPromptDirty && !confirm("当前 Prompt 有未保存修改，确认切换到预备验证任务？")) return;
      app.agentCampaignRuntimeOverrides = false;
      applyAgentTemplate(template);
      $("#agent-task-template-select").value = String(template.id || "");
      setAgentConfigTab("workflow");
      notify("已选择预备验证", "启动后将逐项执行安装、初始化和 Qwen 正常流程验证。", "success", 6000);
    });
    $("#agent-select-phone-configuration-button").addEventListener("click", () => {
      const template = agentTemplateById("phone-configuration-endurance-5");
      if (!template) {
        notify("配置检查不可用", "当前服务没有提供续航测试 5.0 手机配置检查任务。", "error");
        return;
      }
      if (app.agentPromptDirty && !confirm("当前 Prompt 有未保存修改，确认切换到手机配置检查？")) return;
      applyAgentTemplate(template);
      $("#agent-task-template-select").value = String(template.id || "");
      setAgentConfigTab("workflow");
      notify(
        "已选择手机配置检查",
        `${Array.isArray(template.tasks) ? template.tasks.length : 0} 个检查项；无法完成的项目会跳过并统一汇总原因。`,
        "success",
        6000,
      );
    });
    $$("[data-agent-prompt-tab]").forEach(button => {
      button.addEventListener("click", () => setAgentPromptTab(button.dataset.agentPromptTab));
      button.addEventListener("keydown", event => {
        if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const currentIndex = agentPromptTabs.indexOf(button.dataset.agentPromptTab);
        const forward = ["ArrowDown", "ArrowRight"].includes(event.key);
        const nextIndex = event.key === "Home"
          ? 0
          : (event.key === "End"
            ? agentPromptTabs.length - 1
            : (currentIndex + (forward ? 1 : -1) + agentPromptTabs.length) % agentPromptTabs.length);
        setAgentPromptTab(agentPromptTabs[nextIndex]);
        $(`[data-agent-prompt-tab="${agentPromptTabs[nextIndex]}"]`).focus();
      });
    });

    $("#agent-add-task-button").addEventListener("click", () => {
      appendAgentTask();
      setAgentPromptDirty(true);
    });
    $("#agent-loop-workflow-input").addEventListener("change", () => {
      refreshAgentTaskOrder();
      saveAgentLoopPreference();
    });
    $("#agent-workflow-name-input").addEventListener("input", () => setAgentPromptDirty(true));
    $("#agent-task-template-select").addEventListener("change", () => {
      const templateId = $("#agent-task-template-select").value;
      if (app.agentPromptDirty) {
        $("#agent-task-template-select").value = app.agentSelectedTemplateId;
        notify("请先保存当前修改", "保存 Prompt 配置后再切换任务。", "warning", 5000);
        setAgentConfigTab("prompt");
        return;
      }
      const template = agentTemplateById(templateId);
      if (!template) return;
      if (template.kind === "campaign") app.agentCampaignRuntimeOverrides = false;
      applyAgentTemplate(template, { notifyUser: true });
      renderAdbAgent(app.state);
    });
    $("#agent-new-template-button").addEventListener("click", createAgentTemplate);
    $("#agent-edit-template-button").addEventListener("click", editCurrentAgentTemplate);
    $("#agent-task-filter-input").addEventListener("input", filterAgentTaskSequence);
    $$('[data-agent-task-editor-field]').forEach(control => {
      control.addEventListener("input", syncAgentWorkflowTaskEditor);
      control.addEventListener("change", syncAgentWorkflowTaskEditor);
    });
    $("#agent-task-list").addEventListener("click", event => {
      const selectButton = event.target.closest("[data-agent-task-select]");
      if (selectButton) {
        selectAgentWorkflowTask(selectButton.closest(".agent-task-card")?.dataset.agentTaskId || "");
        return;
      }
      const button = event.target.closest("[data-agent-task-action]");
      if (!button || button.disabled) return;
      const card = button.closest(".agent-task-card");
      const action = button.dataset.agentTaskAction;
      if (action === "up" && card.previousElementSibling) {
        card.parentElement.insertBefore(card, card.previousElementSibling);
      } else if (action === "down" && card.nextElementSibling) {
        card.parentElement.insertBefore(card.nextElementSibling, card);
      } else if (action === "remove" && card.parentElement.children.length > 1) {
        card.remove();
      }
      refreshAgentTaskOrder();
      setAgentPromptDirty(true);
    });
    $("#agent-open-task-prompt-button").addEventListener("click", () => {
      setAgentPromptTab("task");
      selectAgentTaskPrompt(app.agentPromptTaskId, { focus: true });
    });
    $("#agent-prompt-task-select").addEventListener("change", event => {
      selectAgentTaskPrompt(event.target.value);
    });
    $("#agent-task-prompt-input").addEventListener("input", syncAgentTaskPromptEditor);
    $("#agent-task-attention-input").addEventListener("input", syncAgentTaskPromptEditor);
    $("#agent-save-prompt-button").addEventListener("click", saveAgentPromptConfiguration);
    $("#agent-reset-system-prompt-button").addEventListener("click", () => {
      $("#agent-system-prompt-input").value = app.agentDefaultSystemPrompt;
      refreshAgentSystemPromptVersion();
      setAgentPromptDirty(true, "已恢复默认内容；点击保存后生效");
      notify("已恢复默认 System Prompt", "点击保存后，模板任务和临时任务将使用内置规则。", "success", 4000);
    });
    $("#agent-system-prompt-input").addEventListener("input", () => {
      refreshAgentSystemPromptVersion();
      setAgentPromptDirty(true);
    });

    $("#adb-agent-form").addEventListener("submit", async event => {
      event.preventDefault();
      if (app.agentPromptDirty) {
        setAgentConfigTab("prompt");
        notify("Prompt 配置尚未保存", "点击保存后，修改才会用于任务启动。", "warning", 6000);
        return;
      }
      const device = readyAndroidAgentDevice();
      if (!device) return;
      const tasks = readAgentTasks();
      const invalidTaskControl = $$(
        '#agent-task-list input[type="number"], #agent-task-list select'
      ).find(control => !control.checkValidity());
      if (invalidTaskControl) {
        setAgentConfigTab("prompt");
        setAgentPromptTab("workflow");
        notify("任务参数无效", "请检查子任务的步骤上限、超时时间和失败策略。", "error");
        focusInvalidAgentTaskControl(invalidTaskControl);
        return;
      }
      const missingTaskIndex = tasks.findIndex(task => !task.prompt);
      if (missingTaskIndex >= 0) {
        setAgentConfigTab("prompt");
        setAgentPromptTab("task");
        selectAgentTaskPrompt(tasks[missingTaskIndex].id);
        notify("任务目标不能为空", `请填写第 ${missingTaskIndex + 1} 个子任务的目标。`, "error");
        $("#agent-task-prompt-input").focus();
        return;
      }
      await startAgentExecution({
        device,
        tasks,
        workflowName: $("#agent-workflow-name-input").value.trim() || "ADB 测试流程",
        campaignStage: app.agentCampaignStage,
        loopEnabled: Boolean($("#agent-loop-workflow-input").checked),
        runtimeOverrides: Boolean(app.agentCampaignRuntimeOverrides),
      });
    });

    $("#agent-run-temporary-task-button").addEventListener("click", async () => {
      const device = readyAndroidAgentDevice();
      if (!device) return;
      const prompt = $("#agent-temporary-task-input").value.trim();
      if (!prompt) {
        notify("临时任务不能为空", "请直接描述这一次要在手机上完成并验证的操作。", "error");
        $("#agent-temporary-task-input").focus();
        return;
      }
      const defaults = app.state?.adb_agent?.defaults || {};
      const firstLine = prompt.split(/\r?\n/, 1)[0].trim();
      const task = defaultAgentTask({
        id: `temporary-${Date.now()}`,
        name: firstLine.slice(0, 60) || "手动临时任务",
        prompt,
        max_steps: Number(defaults.max_steps || 30),
        timeout_s: Number(defaults.task_timeout_s || 300),
        on_failure: "stop",
      });
      const result = await startAgentExecution({
        device,
        tasks: [task],
        workflowName: "手动临时任务",
        temporary: true,
      });
      if (result) setAgentTemporaryTaskMenu(false);
    });

    $("#agent-stop-button").addEventListener("click", async () => {
      $("#agent-stop-button").disabled = true;
      try {
        const campaignActive = app.state?.automation_surface === "campaign";
        const result = await api(campaignActive ? "/api/campaign/stop" : "/api/ai-agent/stop", {
          method: "POST",
          body: "{}",
        });
        app.state = campaignActive
          ? { ...(app.state || {}), campaign: result }
          : { ...(app.state || {}), adb_agent: result };
        renderAdbAgent(app.state);
      } catch (error) {
        notify("停止自动化流程失败", error.message, "error", 8000);
      }
    });

    $("#capture-preset-input").addEventListener("change", () => {
      applyCapturePreset();
      updatePlatformPresentation();
    });
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
        "设备高性能模式会在测试期间切换到 HarmonyOS power-shell 602，显著增加功耗和温度。\n\n程序会在正常结束及可捕获异常时尽力恢复；若进程被强杀或电脑断电，请手工核验 current mode。是否启用？"
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
      if (!confirm("将为当前 iPhone 创建 RemotePairing 并缓存 RemoteXPC 端点。完成后仍需拔掉 USB 并刷新，确认 LAN 端点继续在线，是否继续？")) return;
      setBusy(true, "正在创建 iOS RemotePairing...");
      try {
        const result = await api("/api/ios/pair", {
          method: "POST",
          body: JSON.stringify({ device }),
        });
        const endpoint = result.endpoint || {};
        if (!result.connected || !endpoint.host || !endpoint.port) {
          throw new Error(result.connection_error || "RemotePairing 未返回可用的 RemoteXPC 端点");
        }
        const pairedDevice = result.device || {};
        const linkLocal = String(endpoint.scope || pairedDevice.endpoint_scope || "") === "link-local";
        const lanCandidate = Boolean(endpoint.wireless_lan_candidate)
          || deviceBooleanField(pairedDevice, "wireless_lan_candidate", "wireless_ready");
        notify(
          "iOS RemotePairing 已建立",
          linkLocal
            ? `${endpoint.host}:${endpoint.port} 当前可连，但它是链路本地端点，可能依赖 USB-NCM；不能作为拔线功耗测试的无线证明。`
            : lanCandidate
              ? `${endpoint.host}:${endpoint.port} 是 LAN 候选。请拔掉 USB 后刷新；只有设备仍在线时才算断电采集就绪。`
              : `${endpoint.host}:${endpoint.port} 当前可连，但链路类型无法确认；请勿直接视为拔线就绪。`,
          linkLocal ? "error" : "success",
          12000,
        );
        await refreshState();
      } catch (error) {
        const reason = String(error?.message || "未知连接错误").replace(/^ERROR:\s*/i, "");
        notify("iOS RemotePairing 失败", reason, "error", 0);
      } finally {
        setBusy(false);
      }
    });

    $("#connect-ios-bluetooth").addEventListener("click", async () => {
      const device = selectedDevice();
      const selected = (app.state?.devices || []).find(item => item.serial === device);
      if (selected && devicePlatform(selected) !== "ios") {
        notify("请选择 iPhone", "蓝牙热点连接只适用于 iOS 设备。", "error");
        return;
      }
      if (!confirm(
        "将连接已配对 iPhone 的蓝牙个人热点，并把 RemotePairing 切换到蓝牙 PAN 地址。\n\n"
        + "请先完成“1. 创建 RemotePairing”，并确认 iPhone 已开启蓝牙和“个人热点”。"
        + "首次使用时还需要在 Windows 与 iPhone 两端完成蓝牙配对。是否继续？",
      )) return;
      setBusy(true, "正在连接 iPhone 蓝牙热点...");
      try {
        const result = await api("/api/ios/bluetooth", {
          method: "POST",
          body: JSON.stringify({ device: device || null }),
        });
        const endpoint = result.endpoint || {};
        const connectedDevice = result.device || {};
        if (!result.connected || !endpoint.host || !endpoint.port) {
          throw new Error("蓝牙 PAN 已连接，但没有返回可用的 RemotePairing 端点");
        }
        const unplugReady = deviceBooleanField(
          connectedDevice,
          "unplug_ready",
          "wireless_ready",
        );
        notify(
          result.already_connected ? "iPhone 蓝牙已在线" : "iPhone 蓝牙连接成功",
          `${result.address} → ${endpoint.host}:${endpoint.port} · ${result.link_speed || "Bluetooth PAN"}`
            + (unplugReady ? " · 已通过拔线验证" : " · 可拔掉 USB 后刷新验证"),
          "success",
          12000,
        );
        await refreshState();
      } catch (error) {
        const reason = String(error?.message || "未知蓝牙连接错误").replace(/^ERROR:\s*/i, "");
        notify("iPhone 蓝牙连接失败", reason, "error", 0);
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
        $("#app-result-details").open = true;
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

    $("#app-search-input").addEventListener("input", () => {
      const details = $("#app-result-details");
      if (details && !details.classList.contains("hidden")) details.open = true;
      renderAppOptions();
    });
    $("#app-select").addEventListener("change", event => {
      const packageName = event.target.value;
      if (!packageName) return;
      $("#package-input").value = packageName;
      app.selectedScannedPackage = packageName;
      renderAppOptions();
      updateStartControlState();
    });
    $("#app-result-list").addEventListener("click", event => {
      const option = event.target.closest("[data-app-package]");
      if (!option || option.disabled) return;
      const packageName = option.dataset.appPackage || "";
      if (!packageName) return;
      $("#package-input").value = packageName;
      app.selectedScannedPackage = packageName;
      renderAppOptions();
      updateStartControlState();
    });
    $("#package-input").addEventListener("input", event => {
      const packageName = event.target.value.trim();
      app.selectedScannedPackage = app.scannedApps.some(item => item.package === packageName)
        ? packageName
        : "";
      renderAppOptions();
      updateStartControlState();
    });

    $$("[data-duration]").forEach(button => button.addEventListener("click", () => {
      if (button.dataset.duration === "unlimited") {
        app.durationUnlimited = true;
      } else {
        app.durationUnlimited = false;
        $("#duration-input").value = button.dataset.duration;
      }
      syncDurationPreset();
      updateStartControlState();
    }));

    $("#duration-input").addEventListener("input", () => {
      app.durationUnlimited = false;
      syncDurationPreset();
      updateStartControlState();
    });
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
      const recordingReadiness = recordingDeviceReadiness();
      if (!recordingReadiness.ready) {
        notify("设备尚未满足采集条件", recordingReadiness.reason, "error", 9000);
        return;
      }
      if (targetPackageRequired(payload.platform, payload.test_mode) && !payload.package) {
        const detail = payload.platform === "harmony"
          ? "Harmony SmartPerf 应用 FPS 必须绑定目标包名。"
          : "Android 性能测试必须绑定具体包名；可先扫描手机应用后选择。";
        notify("请填写目标游戏 / 应用包名", detail, "error", 8000);
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
      const defaultName = defaultMarkerName();
      const name = prompt("请输入时间标记名称，例如：BTR2 开始、进入视频测试、发现异常", defaultName);
      if (name === null) return;
      const resolvedName = name.trim() || defaultName;
      try {
        const marker = await api("/api/marker", {
          method: "POST",
          body: JSON.stringify({ name: resolvedName }),
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
        const platform = String(entry.data?.platform || (entry.data?.device?.ios ? "ios" : selectedPlatform()));
        const powered = entry.data?.battery?.powered;
        const pluggedStateAvailable = entry.data?.battery?.plugged_state_available !== false
          && (platform !== "ios" || entry.data?.battery?.external_power_state_available === true);
        const unplugged = pluggedStateAvailable && (!powered || (Array.isArray(powered) && powered.length === 0));
        if (!pluggedStateAvailable) {
          notify("Probe 完成，外供状态无法确认", "BatteryService 未返回可靠的 plugged 状态；不能把 powered=[] 当作已拔电，请在正式功耗测试前人工确认外部供电已断开。", "error", 9000);
        } else {
          notify(unplugged ? "Probe 完成，设备可测试" : "Probe 完成，检测到外部供电", unplugged ? "电流与 CPU 数据源已检查。" : `powered: ${Array.isArray(powered) ? powered.join(", ") : powered}`, unplugged ? "success" : "error", 7000);
        }
      } catch (error) {
        notify("Probe 失败", error.message, "error", 8000);
      } finally {
        setBusy(false);
      }
    });

    $("#clear-console").addEventListener("click", () => {
      app.consoleClearedAt = Date.now() / 1000;
      app.uiLogs = [];
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
      const defaultPortable = `dist\\mobile-profiler-v${String(app.state?.version || "X.Y.Z").replace(/^v/i, "")}-portable`;
      if (!confirm(`确认重新构建便携包？\n\n输出目录：${outputDirectory || defaultPortable}\n已有同名目录和 ZIP 会被替换。`)) return;
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

    const liveTimelineConfigList = $("#live-timeline-config-list");
    liveTimelineConfigList.addEventListener("change", event => {
      const input = event.target.closest("[data-live-timeline-toggle]");
      if (!input) return;
      const key = String(input.dataset.liveTimelineToggle || "");
      if (setLiveTimelineVisibility(app.state?.active, key, input.checked)) renderChart();
    });
    liveTimelineConfigList.addEventListener("click", event => {
      const button = event.target.closest("[data-live-timeline-move]");
      if (!button || button.disabled) return;
      const key = String(button.dataset.liveTimelineKey || "");
      const direction = Number(button.dataset.liveTimelineMove || 0);
      if (moveLiveTimelineLayoutItem(app.state?.active, key, direction)) renderChart();
    });

    $("#live-range-clear").addEventListener("click", () => {
      app.liveTimeRange = null;
      app.liveTimeRangeDraft = null;
      app.liveTimeRangePointer = null;
      app.liveRangeSummary = null;
      app.liveRangeSummaryRequestId += 1;
      renderLiveTimeRangePresentation();
    });

    const chartWrap = $("#chart-wrap");
    chartWrap.addEventListener("pointerdown", event => {
      const geometry = app.chartGeometry;
      if (!geometry?.lanes?.length || event.button !== 0) return;
      const elapsed = liveElapsedForPointer(event, geometry, false);
      if (!finite(elapsed)) return;
      app.liveTimeRangePointer = {
        pointerId: event.pointerId,
        startElapsed: Number(elapsed),
        startClientX: event.clientX,
        moved: false,
        previousRange: app.liveTimeRange,
      };
      app.liveTimeRangeDraft = [Number(elapsed), Number(elapsed)];
      chartWrap.classList.add("is-selecting");
      chartWrap.setPointerCapture?.(event.pointerId);
      $("#chart-tooltip").classList.add("hidden");
      geometry.hoverLine.setAttribute("opacity", "0");
      event.preventDefault();
    });
    chartWrap.addEventListener("pointermove", event => {
      const geometry = app.chartGeometry;
      if (!geometry?.lanes?.length) return;
      const pointer = app.liveTimeRangePointer;
      if (pointer && pointer.pointerId === event.pointerId) {
        const elapsed = liveElapsedForPointer(event, geometry);
        if (!finite(elapsed)) return;
        pointer.moved = pointer.moved || Math.abs(event.clientX - pointer.startClientX) >= 4;
        app.liveTimeRangeDraft = [pointer.startElapsed, Number(elapsed)];
        scheduleLiveTimeRangePresentation();
        event.preventDefault();
        return;
      }
      const rect = chartWrap.getBoundingClientRect();
      const viewX = ((event.clientX - rect.left) / rect.width) * geometry.width;
      const plotX = clamp(
        viewX,
        geometry.margins.left,
        geometry.width - geometry.margins.right,
      );
      const elapsed = (plotX - geometry.margins.left) / Math.max(1, geometry.plotWidth)
        * geometry.maxTime;
      geometry.hoverLine.setAttribute("x1", plotX);
      geometry.hoverLine.setAttribute("x2", plotX);
      geometry.hoverLine.setAttribute("opacity", "1");
      const tooltip = $("#chart-tooltip");
      tooltip.classList.remove("hidden");
      const rows = geometry.lanes.map(lane => {
        const values = lane.series.map(item => {
          let nearest = item.coordinates[0];
          let distance = Math.abs(nearest.elapsed - elapsed);
          item.coordinates.forEach(point => {
            const nextDistance = Math.abs(point.elapsed - elapsed);
            if (nextDistance < distance) {
              nearest = point;
              distance = nextDistance;
            }
          });
          const label = lane.series.length > 1 ? `${item.label} ` : "";
          return `${escapeHtml(label)}${Number(nearest.value).toFixed(lane.digits)} ${escapeHtml(lane.unit)}`;
        }).join(" / ");
        return `<span class="chart-tooltip-row"><em>${escapeHtml(lane.title)}</em><b>${values}</b></span>`;
      }).join("");
      tooltip.innerHTML = `<time>${escapeHtml(formatDuration(elapsed))}</time>${rows}`;
      const tooltipWidth = Math.max(210, tooltip.offsetWidth || 0);
      const tooltipHeight = Math.max(54, tooltip.offsetHeight || 0);
      const tooltipX = clamp(event.clientX - rect.left + 12, 8, rect.width - tooltipWidth - 8);
      const tooltipY = clamp(event.clientY - rect.top - 20, 8, rect.height - tooltipHeight - 8);
      tooltip.style.left = `${tooltipX}px`;
      tooltip.style.top = `${tooltipY}px`;
    });
    const finishLiveTimeRange = (event, commit) => {
      const pointer = app.liveTimeRangePointer;
      if (!pointer || (event && pointer.pointerId !== event.pointerId)) return;
      const selected = pointer.moved && commit
        ? normalizeLiveTimeRange(app.liveTimeRangeDraft, app.chartGeometry?.maxTime)
        : null;
      app.liveTimeRange = selected || pointer.previousRange || null;
      app.liveTimeRangeDraft = null;
      app.liveTimeRangePointer = null;
      if (selected) {
        app.liveRangeSummary = null;
        app.liveRangeSummaryRequestId += 1;
      }
      chartWrap.classList.remove("is-selecting");
      if (event && chartWrap.hasPointerCapture?.(event.pointerId)) {
        chartWrap.releasePointerCapture(event.pointerId);
      }
      renderLiveTimeRangePresentation();
      if (selected) loadLiveRangeSummary(selected);
    };
    chartWrap.addEventListener("pointerup", event => finishLiveTimeRange(event, true));
    chartWrap.addEventListener("pointercancel", event => finishLiveTimeRange(event, false));
    chartWrap.addEventListener("mouseleave", () => {
      if (app.liveTimeRangePointer) return;
      $("#chart-tooltip").classList.add("hidden");
      if (app.chartGeometry?.hoverLine) app.chartGeometry.hoverLine.setAttribute("opacity", "0");
    });
  }

  mountConfigurationView();
  bindEvents();
  setAgentConfigTab(app.agentConfigTab, { persist: false });
  setAgentRunConsoleSection(app.agentRunConsoleSection);
  setPlatform(app.platform, { initial: true });
  setTestMode("power", { initial: true });
  switchView(app.activeView);
  renderChart();
  pollLoop();
})();
