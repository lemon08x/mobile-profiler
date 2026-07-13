(() => {
  "use strict";

  const $ = selector => document.querySelector(selector);
  const $$ = selector => Array.from(document.querySelectorAll(selector));
  const svgNs = "http://www.w3.org/2000/svg";

  const app = {
    state: null,
    metric: "power_mw",
    polling: false,
    activeView: location.hash.replace("#", "") || "live",
    consoleClearedAt: 0,
    currentRunName: null,
    chartGeometry: null,
    notifiedWarnings: new Set(),
  };

  const metricDefinitions = {
    power_mw: {
      title: "实时功率",
      legend: "Battery power",
      color: "#ffb45c",
      value: point => finite(point.power_mw) ? Number(point.power_mw) / 1000 : null,
      unit: "W",
      digits: 3,
    },
    current_ma: {
      title: "放电电流",
      legend: "Current magnitude",
      color: "#4bc6e8",
      value: point => finite(point.current_ma) ? Number(point.current_ma) : null,
      unit: "mA",
      digits: 0,
    },
    cpu_pct: {
      title: "CPU 总负载",
      legend: "CPU utilization",
      color: "#35d49a",
      value: point => finite(point.cpu_pct) ? Number(point.cpu_pct) : null,
      unit: "%",
      digits: 1,
    },
    temperature_c: {
      title: "电池温度",
      legend: "Battery temperature",
      color: "#ff657d",
      value: point => finite(point.temperature_c) ? Number(point.temperature_c) : null,
      unit: "°C",
      digits: 1,
    },
    voltage_mv: {
      title: "电池电压",
      legend: "Battery voltage",
      color: "#9e8cff",
      value: point => finite(point.voltage_mv) ? Number(point.voltage_mv) / 1000 : null,
      unit: "V",
      digits: 3,
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

  function activePlatform(active) {
    return String(active?.platform || active?.metadata?.platform || "android").toLowerCase();
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
    const target = ["live", "system", "thermal", "device", "history", "tools"].includes(view) ? view : "live";
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
      system: "系统活动",
      thermal: "热控与调度",
      device: "设备能力",
      history: "历史报告",
      tools: "工具与交付",
    }[target];
    if (target === "live") requestAnimationFrame(renderChart);
  }

  function selectedDevice() {
    return $("#device-select").value || "";
  }

  function devicePlatform(device) {
    return String(device?.platform || "android").toLowerCase();
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
    const previous = select.value || localStorage.getItem("android-power-device") || "";
    const devices = Array.isArray(state.devices) ? state.devices : [];
    select.innerHTML = "";
    const ready = devices.filter(device => device.state === "device");
    if (!devices.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = state.device_error ? "设备运行时不可用" : "未发现设备";
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
          const platform = devicePlatform(device) === "ios" ? "iOS" : "Android";
          option.textContent = `${platform} · ${name || "Device"} · ${device.serial} · ${device.state}`;
          option.disabled = device.state !== "device";
          group.appendChild(option);
        });
        select.appendChild(group);
      });
    }
    const activeSerial = state.active?.running ? state.active.device_serial : "";
    const preferred = activeSerial || previous;
    const preferredDevice = devices.find(device => device.serial === preferred);
    if (preferredDevice && (preferredDevice.state === "device" || deviceConnectionType(preferredDevice) === "wireless")) {
      select.value = preferred;
    } else if (ready.length) {
      select.value = ready[0].serial;
    }
    select.disabled = Boolean(state.active?.running);
    const chosen = devices.find(device => device.serial === select.value);
    const chosenType = deviceConnectionType(chosen);
    const chosenPlatform = devicePlatform(chosen);
    const deviceState = $("#device-state");
    deviceState.classList.toggle("online", Boolean(chosen?.state === "device"));
    deviceState.classList.toggle("error", Boolean(state.device_error || (chosen && chosen.state !== "device")));
    deviceState.querySelector("span").textContent = chosen?.state === "device" ? `${chosenPlatform === "ios" ? "iOS" : "Android"} · ${deviceConnectionLabel(chosen)}在线` : state.device_error ? "设备运行时异常" : chosen ? `${deviceConnectionLabel(chosen)} · ${chosen.state}` : "等待设备";
    $("#start-record").disabled = !chosen || chosen.state !== "device" || Boolean(state.active?.running);
    $("#enable-tcpip").disabled = !chosen || chosenPlatform !== "android" || chosen.state !== "device" || chosenType !== "usb" || Boolean(state.active?.running);
    $("#pair-ios").disabled = !chosen || chosenPlatform !== "ios" || Boolean(state.active?.running);
    $("#disconnect-wireless").disabled = !chosen || chosenPlatform !== "android" || chosenType !== "wireless" || Boolean(state.active?.running);
  }

  function renderMetrics(active) {
    const latest = active?.latest || {};
    const summary = active?.summary || {};
    const isIos = activePlatform(active) === "ios";
    $("#metric-power").textContent = finite(latest.power_mw) ? `${(Number(latest.power_mw) / 1000).toFixed(3)} W` : "--";
    $("#metric-current").textContent = formatMetric(latest.current_ma, "mA", 0);
    $("#metric-cpu").textContent = formatMetric(latest.cpu_pct, "%", 1);
    $("#metric-temp").textContent = formatMetric(latest.temperature_c, "°C", 1);
    $("#metric-energy").textContent = formatMetric(summary.energy_mwh, "mWh", 2);
    const averagePower = finite(summary.average_power_mw) ? `AVG ${(Number(summary.average_power_mw) / 1000).toFixed(3)} W` : "整机电池侧";
    $("#metric-power-sub").textContent = isIos && finite(latest.power_sample_age_s)
      ? `${averagePower} · age ${Number(latest.power_sample_age_s).toFixed(1)} s`
      : averagePower;
    $("#metric-current-sub").textContent = finite(summary.average_current_ma) ? `AVG ${Number(summary.average_current_ma).toFixed(0)} mA` : "正幅值";
    const averageCpu = finite(summary.average_cpu_pct) ? `AVG ${Number(summary.average_cpu_pct).toFixed(1)}%` : "全核心";
    $("#metric-cpu-sub").textContent = isIos && finite(summary.average_collector_cpu_pct)
      ? `${averageCpu} · collector ${Number(summary.average_collector_cpu_pct).toFixed(1)}%`
      : averageCpu;
    $("#metric-temp-sub").textContent = finite(latest.voltage_mv) ? `${(Number(latest.voltage_mv) / 1000).toFixed(3)} V` : isIos ? "DiagnosticsService" : "BatteryService";
    $("#metric-energy-sub").textContent = active?.sample_count ? `${active.sample_count} samples` : "有效区间积分";
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
    $("#session-title").textContent = active ? `${label} · ${active.title || active.run_name || "Power session"}` : "等待开始新的采集";
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
    $("#start-record").querySelector("span:last-child").textContent = running ? "采集中" : "开始采集";
  }

  function renderClusters(active) {
    const container = $("#cluster-list");
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
    const battery = active?.battery || {};
    $("#context-package").textContent = context.foreground_package || "--";
    $("#context-activity").textContent = context.foreground_activity || "--";
    $("#context-screen").textContent = context.screen_state || "--";
    const brightness = finite(context.brightness_raw) ? Number(context.brightness_raw).toFixed(0) : "--";
    const refresh = finite(context.refresh_rate_hz) ? `${Number(context.refresh_rate_hz).toFixed(0)} Hz` : "--";
    $("#context-display").textContent = `${brightness} / ${refresh}`;
    const voltage = finite(active?.latest?.voltage_mv) ? `${(Number(active.latest.voltage_mv) / 1000).toFixed(3)} V` : "--";
    const level = finite(battery.level_pct) ? `${Number(battery.level_pct).toFixed(0)}%` : "--";
    $("#context-battery").textContent = `${voltage} / ${level}`;
    $("#context-reconnect").textContent = String(active?.checkpoint?.reconnect_count ?? active?.metadata?.reconnect_count ?? 0);
    $("#context-source").textContent = context.source || "sampler";
  }

  function renderSystem(active) {
    const isIos = activePlatform(active) === "ios";
    const monitor = active?.system_monitor || {};
    const processes = Array.isArray(monitor.processes) ? monitor.processes : [];
    const threads = Array.isArray(monitor.threads) ? monitor.threads : [];
    const watched = Array.isArray(monitor.watched_processes) ? monitor.watched_processes : [];
    const priority = Array.isArray(monitor.active_priority) ? monitor.active_priority : [];
    const snapshotCount = Number(monitor.system_snapshot_count || 0);
    $("#system-snapshot-status").textContent = snapshotCount ? `${snapshotCount} 个快照 · uptime ${formatNumber(monitor.system_uptime_s, 0)} s` : monitor.enabled ? "监控已启用，等待首个快照" : "本次采集未启用";
    $("#system-process-count").textContent = finite(monitor.process_count) ? Number(monitor.process_count).toLocaleString("zh-CN") : "--";
    $("#system-thread-count").textContent = finite(monitor.thread_count) ? Number(monitor.thread_count).toLocaleString("zh-CN") : "--";
    $("#system-snapshot-count").textContent = String(snapshotCount);
    $("#system-priority-count").textContent = String(priority.length);

    const banner = $("#system-priority-banner");
    banner.classList.toggle("active", priority.length > 0);
    banner.classList.toggle("idle", priority.length === 0);
    if (priority.length) {
      const leading = [...priority].sort((a, b) => Number(b.cpu_pct || 0) - Number(a.cpu_pct || 0))[0];
      const names = priority.map(item => item.watch_label || item.watch_name || item.name).filter(Boolean);
      $("#system-priority-title").textContent = `检测到 ${names.join(" / ")}`;
      $("#system-priority-detail").textContent = `${leading.watch_impact || "后台系统活动可能同时影响性能、存储 I/O、温度与功耗。"} 当前最高 CPU ${formatNumber(leading.cpu_pct, 1)}%。`;
      $("#system-priority-badge").textContent = "ACTIVE";
    } else {
      $("#system-priority-title").textContent = isIos ? "未检测到重点系统 / 采集器活动" : "未检测到 DEX / 系统更新高影响活动";
      $("#system-priority-detail").textContent = isIos
        ? (watched.length ? `已识别 ${watched.length} 个 iOS 采集器进程；相对 powerScore 不等于物理功率。` : "等待 DVT sysmontap 进程快照。")
        : (watched.length ? `已识别 ${watched.length} 个受监控服务；常驻 daemon 只有在 CPU 可见时才标记为活动。` : "持续监控 dex2oat、dexopt、artd、installd、profman、odrefresh、update_engine 与 apexd。");
      $("#system-priority-badge").textContent = "STANDBY";
    }

    $("#watched-process-body").innerHTML = watched.length ? watched.map(item => `<tr class="${item.activity_active ? "priority-row" : ""}">
      <td><span class="process-identity"><strong>${escapeHtml(item.watch_label || item.watch_name || item.name || "unknown")}</strong><small title="${escapeHtml(item.command || "")}">${escapeHtml(item.command || item.name || "--")}</small></span></td>
      <td>${escapeHtml(item.pid ?? "--")}</td>
      <td>${finite(item.cpu_pct) ? `${Number(item.cpu_pct).toFixed(1)}%` : "not in top"}</td>
      <td>${escapeHtml(item.policy || "--")} / ${escapeHtml(item.state || "--")}</td>
      <td><span class="activity-pill ${item.activity_active ? "active" : "monitored"}">${item.activity_active ? "ACTIVE" : "MONITORED"}</span></td>
    </tr>`).join("") : `<tr><td colspan="5" class="table-empty">${isIos ? "尚未发现受监控的 iOS 采集器进程。" : "尚未发现受监控的 DEX、安装或更新服务。"}</td></tr>`;

    $("#system-process-body").innerHTML = processes.length ? processes.slice(0, 16).map(item => `<tr>
      <td><span class="process-identity"><strong>${escapeHtml(item.name || item.command || "unknown")}</strong><small>PID ${escapeHtml(item.pid ?? "--")} · ${escapeHtml(item.user || "--")} · ${escapeHtml(item.category || "other")}</small></span></td>
      <td><strong class="cpu-value">${formatNumber(item.cpu_pct, 1)}%</strong></td>
      <td>${finite(item.mem_pct) ? `${formatNumber(item.mem_pct, 1)}%` : formatBytes(item.resident_bytes)}</td>
      <td>${escapeHtml(item.policy || "--")} / ${escapeHtml(item.state || "--")}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="table-empty">暂无进程快照；采集开始后默认每 10 秒更新。</td></tr>';

    $("#system-thread-body").innerHTML = threads.length ? threads.slice(0, 16).map(item => `<tr>
      <td><span class="process-identity"><strong>${escapeHtml(item.activity_label || item.name || "unknown")}</strong><small>${escapeHtml(item.name || "--")} · ${escapeHtml(item.process || "--")} · PID ${escapeHtml(item.pid ?? "--")}</small></span></td>
      <td>${escapeHtml(item.tid ?? "--")}</td>
      <td><strong class="cpu-value">${formatNumber(item.cpu_pct, 1)}%</strong></td>
      <td>${escapeHtml(item.policy || "--")} / ${escapeHtml(item.state || "--")}</td>
    </tr>`).join("") : `<tr><td colspan="4" class="table-empty">${isIos ? "当前 iOS DVT 适配未提供线程明细。" : "热点线程默认每 30 秒采集一次。"}</td></tr>`;
  }

  function firstThermalThreshold(thermal, sensorName) {
    const thresholds = Array.isArray(thermal?.thresholds) ? thermal.thresholds : [];
    const row = thresholds.find(item => item.name === sensorName);
    const hot = Array.isArray(row?.hot_c) ? row.hot_c : [];
    const value = hot.find(finite);
    return finite(value) ? Number(value) : null;
  }

  function renderThermalScheduler(active) {
    const isIos = activePlatform(active) === "ios";
    const monitor = active?.system_monitor || {};
    const thermal = monitor.thermal || {};
    const scheduler = monitor.scheduler || {};
    const temperatures = Array.isArray(thermal.temperatures) ? thermal.temperatures : [];
    const cooling = Array.isArray(thermal.cooling_devices) ? thermal.cooling_devices : [];
    const sessions = Array.isArray(scheduler.hint_sessions) ? scheduler.hint_sessions : [];
    const status = finite(thermal.status) ? Number(thermal.status) : null;
    const thermalTemperatures = temperatures.filter(isThermalTemperature);
    const hottest = thermalTemperatures.length ? [...thermalTemperatures].sort((a, b) => Number(b.value_c || 0) - Number(a.value_c || 0))[0] : null;
    $("#thermal-snapshot-status").textContent = Number(monitor.thermal_snapshot_count || 0) ? `${monitor.thermal_snapshot_count} 个热控快照 · ${monitor.scheduler_snapshot_count || 0} 个调度快照` : monitor.enabled ? "等待首个热控快照" : "本次采集未启用";
    $("#thermal-status-value").textContent = status === null ? "--" : String(status);
    $("#thermal-status-label").textContent = status === null ? (isIos ? "iOS 未公开严重度" : "ThermalService") : thermalStatusDefinitions[status] || "未知级别";
    $("#thermal-hottest-value").textContent = hottest && finite(hottest.value_c) ? `${Number(hottest.value_c).toFixed(1)} °C` : "--";
    $("#thermal-hottest-label").textContent = hottest?.name || "--";
    $("#thermal-sensor-count").textContent = String(temperatures.length);
    $("#adpf-session-count").textContent = String(sessions.length);

    $("#thermal-sensor-body").innerHTML = temperatures.length ? temperatures.map(item => {
      const threshold = firstThermalThreshold(thermal, item.name);
      const thermalTemperature = isThermalTemperature(item);
      const unit = thermalSensorUnit(item);
      const status = Number(item.status || 0);
      const statusText = thermalTemperature
        ? `${status} · ${thermalStatusDefinitions[status] || "正常"}`
        : "BCL · 不参与热级别";
      return `<tr>
        <td><span class="process-identity"><strong>${escapeHtml(item.name || "unknown")}</strong><small>type ${escapeHtml(item.type ?? "--")}</small></span></td>
        <td><strong>${formatNumber(item.value_c, unit === "°C" ? 1 : 3)} ${escapeHtml(unit)}</strong></td>
        <td><span class="thermal-pill level-${thermalTemperature ? escapeHtml(status) : "0"}">${escapeHtml(statusText)}</span></td>
        <td>${threshold === null ? "--" : `${threshold.toFixed(1)} ${escapeHtml(unit)}`}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="table-empty">${isIos ? "iOS 电池温度数据尚不可用。" : "ThermalService 温度数据尚不可用。"}</td></tr>`;

    $("#cooling-device-body").innerHTML = cooling.length ? cooling.map(item => `<tr>
      <td><strong>${escapeHtml(item.name || "unknown")}</strong></td>
      <td>${formatNumber(item.value, 0)}</td>
      <td><span class="activity-pill ${Number(item.value || 0) > 0 ? "active" : "monitored"}">${Number(item.value || 0) > 0 ? "ENGAGED" : "IDLE"}</span></td>
    </tr>`).join("") : '<tr><td colspan="3" class="table-empty">未暴露冷却设备。</td></tr>';

    const cpusets = scheduler.cpusets && typeof scheduler.cpusets === "object" ? Object.entries(scheduler.cpusets) : [];
    const policies = Array.isArray(scheduler.cpu_policies) ? scheduler.cpu_policies : [];
    const schedulingRows = [
      ...cpusets.map(([name, cpus]) => `<tr><td><strong>cpuset/${escapeHtml(name)}</strong></td><td>CPU ${escapeHtml(cpus)}</td><td><span class="activity-pill monitored">VISIBLE</span></td></tr>`),
      ...policies.map(item => {
        const minimum = finite(item.scaling_min_khz) ? item.scaling_min_khz : item.cpuinfo_min_khz;
        const maximum = finite(item.scaling_max_khz) ? item.scaling_max_khz : item.cpuinfo_max_khz;
        const range = finite(minimum) || finite(maximum) ? `${finite(minimum) ? (Number(minimum) / 1000).toFixed(0) : "--"}–${finite(maximum) ? (Number(maximum) / 1000).toFixed(0) : "--"} MHz` : "--";
        const cpuRange = item.related_cpus ? `CPU ${item.related_cpus} · ${range}` : range;
        const coreCtl = finite(item.core_ctl_min_cpus) || finite(item.core_ctl_max_cpus)
          ? ` · core_ctl ${item.core_ctl_enabled === false ? "off" : `${item.core_ctl_min_cpus ?? "--"}–${item.core_ctl_max_cpus ?? "--"}`}`
          : "";
        return `<tr><td><strong>${escapeHtml(item.name || "policy")}</strong></td><td>${escapeHtml(cpuRange)}</td><td>${item.governor ? escapeHtml(item.governor) : '<span class="activity-pill restricted">LIMITS ONLY</span>'}${escapeHtml(coreCtl)}</td></tr>`;
      }),
    ];
    $("#scheduler-policy-body").innerHTML = schedulingRows.length ? schedulingRows.join("") : '<tr><td colspan="3" class="table-empty">cpuset / cpufreq 数据不可用。</td></tr>';

    $("#adpf-session-body").innerHTML = sessions.length ? sessions.map(item => {
      const flags = [item.graphics_pipeline ? "graphics" : "", item.power_efficient ? "efficient" : "", item.force_paused ? "paused" : ""].filter(Boolean).join(", ") || "standard";
      return `<tr>
        <td><span class="process-identity"><strong>PID ${escapeHtml(item.pid ?? "--")}</strong><small>UID ${escapeHtml(item.uid ?? "--")}</small></span></td>
        <td>${escapeHtml((item.tids || []).join(", ") || "--")}</td>
        <td>${finite(item.target_duration_ns) ? `${(Number(item.target_duration_ns) / 1e6).toFixed(2)} ms` : "--"}</td>
        <td>${escapeHtml(flags)}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="4" class="table-empty">当前没有可见的 ADPF HintSession。</td></tr>';

    const processStates = Array.isArray(scheduler.watched_processes) ? scheduler.watched_processes : [];
    $("#scheduler-process-body").innerHTML = processStates.length ? processStates.slice(0, 24).map(item => `<tr>
      <td><strong>${escapeHtml(item.name || "unknown")}</strong></td>
      <td>${escapeHtml(item.pid ?? "--")} / ${escapeHtml(item.uid ?? "--")}</td>
      <td>${escapeHtml(item.current_proc_state ?? "--")} · ${escapeHtml(item.adj_type || "--")}</td>
      <td>${escapeHtml(item.current_sched_group ?? "--")}</td>
      <td><span class="activity-pill ${item.frozen ? "restricted" : "monitored"}">${item.frozen ? "FROZEN" : "RUNNABLE"}</span></td>
    </tr>`).join("") : '<tr><td colspan="5" class="table-empty">ActivityManager 进程状态尚不可用。</td></tr>';
    const powerState = Array.isArray(scheduler.power_hal) ? scheduler.power_hal : [];
    $("#scheduler-power-state").innerHTML = powerState.length ? powerState.map(item => `<span>${escapeHtml(item)}</span>`).join("") : "<span>PowerManager 状态尚不可用</span>";
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
    const series = Array.isArray(active?.series) ? active.series : [];
    const definition = metricDefinitions[app.metric];
    $("#chart-title").textContent = definition.title;
    $("#chart-legend").textContent = definition.legend;
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
      app.chartGeometry = null;
      return;
    }
    empty.classList.add("hidden");
    const values = points.map(point => Number(point.value));
    const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    const p95 = percentile(values, .95);
    $("#chart-average").textContent = `${average.toFixed(definition.digits)} ${definition.unit}`;
    $("#chart-p95").textContent = p95 === null ? "--" : `${p95.toFixed(definition.digits)} ${definition.unit}`;

    const width = 1000;
    const height = 320;
    const margins = { left: 58, right: 22, top: 18, bottom: 36 };
    const plotWidth = width - margins.left - margins.right;
    const plotHeight = height - margins.top - margins.bottom;
    const minTime = Math.min(...points.map(point => point.elapsed));
    const maxTime = Math.max(...points.map(point => point.elapsed));
    let minValue = Math.min(...values);
    let maxValue = Math.max(...values);
    if (minValue === maxValue) {
      minValue -= Math.max(1, Math.abs(minValue) * .05);
      maxValue += Math.max(1, Math.abs(maxValue) * .05);
    } else {
      const padding = (maxValue - minValue) * .12;
      minValue -= padding;
      maxValue += padding;
    }
    if (app.metric === "cpu_pct") {
      minValue = Math.max(0, minValue);
      maxValue = Math.min(100, Math.max(10, maxValue));
    }
    const xFor = elapsed => margins.left + ((elapsed - minTime) / Math.max(.001, maxTime - minTime)) * plotWidth;
    const yFor = value => margins.top + (1 - (value - minValue) / Math.max(.001, maxValue - minValue)) * plotHeight;

    const grid = svgNode("g", { class: "chart-grid" });
    for (let index = 0; index <= 4; index += 1) {
      const ratio = index / 4;
      const y = margins.top + ratio * plotHeight;
      const value = maxValue - ratio * (maxValue - minValue);
      grid.appendChild(svgNode("line", { x1: margins.left, y1: y, x2: width - margins.right, y2: y, class: "grid-line" }));
      grid.appendChild(svgNode("text", { x: margins.left - 9, y: y + 3, "text-anchor": "end", class: "axis-label" }, `${value.toFixed(definition.digits)} ${definition.unit}`));
    }
    for (let index = 0; index <= 5; index += 1) {
      const ratio = index / 5;
      const x = margins.left + ratio * plotWidth;
      const elapsed = minTime + ratio * (maxTime - minTime);
      grid.appendChild(svgNode("line", { x1: x, y1: margins.top, x2: x, y2: height - margins.bottom, class: "grid-line" }));
      grid.appendChild(svgNode("text", { x, y: height - 13, "text-anchor": index === 0 ? "start" : index === 5 ? "end" : "middle", class: "axis-label" }, formatDuration(elapsed)));
    }
    svg.appendChild(grid);

    const coordinates = points.map(point => ({ ...point, x: xFor(point.elapsed), y: yFor(Number(point.value)) }));
    const linePath = coordinates.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
    const areaPath = `${linePath} L${coordinates.at(-1).x.toFixed(2)},${height - margins.bottom} L${coordinates[0].x.toFixed(2)},${height - margins.bottom} Z`;
    svg.appendChild(svgNode("path", { d: areaPath, class: "series-area" }));
    svg.appendChild(svgNode("path", { d: linePath, class: "series-line" }));
    const last = coordinates.at(-1);
    svg.appendChild(svgNode("circle", { cx: last.x, cy: last.y, r: 4, class: "latest-marker" }));
    const hoverLine = svgNode("line", { x1: last.x, y1: margins.top, x2: last.x, y2: height - margins.bottom, class: "hover-line", opacity: 0 });
    svg.appendChild(hoverLine);
    app.chartGeometry = { coordinates, width, height, margins, hoverLine, definition };
  }

  function renderActive(active) {
    if (active?.run_name !== app.currentRunName) {
      app.currentRunName = active?.run_name || null;
      app.consoleClearedAt = 0;
      app.notifiedWarnings.clear();
    }
    renderSession(active);
    renderMetrics(active);
    renderClusters(active);
    renderContext(active);
    renderSystem(active);
    renderThermalScheduler(active);
    renderConsole(active);
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
    const gpuModel = data.gpu_probe?.model || data.gpu_source?.name || "GPU";
    $("#probe-gpu-state").textContent = gpuFrequencyAvailable ? `${gpuModel} 频率可读` : gpuLoadAvailable ? `${gpuModel} 负载可读` : data.gpu_work_duration_available ? `${gpuModel} 驱动证据` : "GPU 遥测不可用";
    $("#probe-gpu-detail").textContent = gpuFrequencyAvailable ? data.gpu_source.frequency_path : gpuLoadAvailable ? data.gpu_source.load_path : data.gpu_probe?.reason || "No readable OEM node";
    const gpuBadge = $("#probe-gpu-badge");
    gpuBadge.textContent = gpuFrequencyAvailable ? "DIRECT" : gpuLoadAvailable ? "LOAD" : data.gpu_work_duration_available ? "FALLBACK" : "UNAVAILABLE";
    gpuBadge.className = `probe-badge ${gpuAvailable ? "" : "neutral"}`;
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
    ] : [
      ["Fuel-gauge current", "cmd battery current_now", Boolean(data.current_command_ok)],
      ["GPU frequency", `${gpuModel} · ${data.gpu_probe?.provider || "OEM sysfs"}`, gpuFrequencyAvailable],
      ["GPU load", data.gpu_source?.load_format || "OEM load node", gpuLoadAvailable],
      ["GPU UID work", "dumpsys gpu active duration", Boolean(data.gpu_work_duration_available)],
      ["GPU memory", `${finite(data.gpu_memory_total_bytes) ? `${(Number(data.gpu_memory_total_bytes) / 1048576).toFixed(1)} MiB` : "dumpsys gpu snapshot"}`, Boolean(data.gpu_memory_snapshot_available)],
      ["Perfetto android.power", "registered data source", Boolean(data.perfetto_android_power)],
      ["Perfetto sysfs power", "linux.sysfs_power", Boolean(data.perfetto_sysfs_power)],
      ["PowerStats dump", "dumpsys powerstats", Boolean(data.powerstats_dump_available)],
      ["System process monitor", "top + ps for apps/services/kernel", Boolean(data.system_monitor?.process_top_available)],
      ["ThermalService history", `${data.system_monitor?.thermal_sensor_count || 0} sensors / ${data.system_monitor?.thermal_threshold_count || 0} thresholds`, Boolean(data.system_monitor?.thermalservice_available)],
      ["cpuset / ADPF", `${Object.keys(data.system_monitor?.cpusets || {}).length} cpusets / ${data.system_monitor?.adpf_active_session_count || 0} active sessions`, Boolean(data.system_monitor?.adpf_available)],
      ["WALT / core_ctl", `${(data.system_monitor?.cpu_policies || []).map(item => item.governor).filter(Boolean).join(", ") || "governor n/a"} / ${(data.system_monitor?.cpu_policies || []).filter(item => item.core_ctl_min_cpus != null).length} policies`, (data.system_monitor?.cpu_policies || []).some(item => item.governor === "walt" || item.core_ctl_min_cpus != null)],
    ];
    $("#probe-feature-list").innerHTML = features.map(([name, detail, available]) => `<div class="feature-row"><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></span><i class="feature-state ${available ? "good" : "warn"}"></i></div>`).join("");
    $("#probe-json").textContent = JSON.stringify(data, null, 2);
  }

  function renderHistory(state) {
    $("#history-root").textContent = `采集根目录：${state?.output_root || "power-runs"}`;
    $("#output-root-hint").textContent = `保存在 ${state?.output_root || "power-runs"}`;
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
    return {
      device: selectedDevice(),
      title: $("#title-input").value.trim(),
      run_name: $("#run-name-input").value.trim(),
      duration: Number($("#duration-input").value),
      interval: Number($("#interval-input").value),
      package: $("#package-input").value.trim(),
      start_context: $("#start-context-input").value,
      start_note: $("#start-note-input").value.trim(),
      session_mode: $("#session-mode-input").checked,
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
    };
  }

  function bindEvents() {
    $$(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
    window.addEventListener("hashchange", () => switchView(location.hash.replace("#", "")));
    window.addEventListener("resize", () => requestAnimationFrame(renderChart));
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });

    $("#device-select").addEventListener("change", event => {
      localStorage.setItem("android-power-device", event.target.value);
      renderDevices(app.state);
      renderProbe(app.state);
    });

    const savedAdbAddress = localStorage.getItem("android-power-adb-address");
    if (savedAdbAddress) $("#adb-address-input").value = savedAdbAddress;
    $("#adb-connect-form").addEventListener("submit", async event => {
      event.preventDefault();
      const address = $("#adb-address-input").value.trim();
      if (!address) {
        notify("请输入 ADB 地址", "例如 192.168.1.20:5555。", "error");
        return;
      }
      setBusy(true, `正在连接 ${address}...`);
      try {
        const result = await api("/api/connect", {
          method: "POST",
          body: JSON.stringify({ address }),
        });
        localStorage.setItem("android-power-adb-address", address);
        localStorage.setItem("android-power-device", address);
        notify(result.connected ? "ADB 设备已连接" : "ADB 命令已执行", result.output || address, result.connected ? "success" : "error", 7000);
        await refreshState();
      } catch (error) {
        notify("ADB 连接失败", error.message, "error", 8000);
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
          localStorage.setItem("android-power-adb-address", result.suggested_address);
          if (result.connected) {
            localStorage.setItem("android-power-device", result.suggested_address);
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

    $("#pair-ios").addEventListener("click", async () => {
      const device = selectedDevice();
      const selected = (app.state?.devices || []).find(item => item.serial === device);
      if (!device || devicePlatform(selected) !== "ios") {
        notify("请选择 iPhone", "需要一台已通过 USB 信任的 iPhone。", "error");
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
        notify(
          "iOS 无线配对完成",
          endpoint.host && endpoint.port ? `${endpoint.host}:${endpoint.port}，现在可以拔掉 USB。` : "配对已写入，请保持解锁后刷新设备。",
          "success",
          10000,
        );
        await refreshState();
      } catch (error) {
        notify("iOS 无线配对失败", error.message, "error", 10000);
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
        if (localStorage.getItem("android-power-device") === address) {
          localStorage.removeItem("android-power-device");
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
      button.classList.add("loading");
      try {
        await api("/api/devices/refresh", { method: "POST", body: "{}" });
        await refreshState();
      } catch (error) {
        notify("设备刷新失败", error.message, "error");
      } finally {
        button.classList.remove("loading");
      }
    });

    $$("[data-duration]").forEach(button => button.addEventListener("click", () => {
      $("#duration-input").value = button.dataset.duration;
      $$("[data-duration]").forEach(item => item.classList.toggle("active", item === button));
    }));

    $("#duration-input").addEventListener("input", event => {
      $$("[data-duration]").forEach(item => item.classList.toggle("active", Number(item.dataset.duration) === Number(event.target.value)));
    });

    $$("[data-metric]").forEach(button => button.addEventListener("click", () => {
      app.metric = button.dataset.metric;
      $$("[data-metric]").forEach(item => item.classList.toggle("active", item === button));
      renderChart();
    }));

    $("#record-form").addEventListener("submit", async event => {
      event.preventDefault();
      const payload = recordPayload();
      if (!payload.device) {
        notify("请选择设备", "需要一台处于 device 状态的 ADB 设备。", "error");
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
          body: JSON.stringify({ device, gpu_frequency_path: $("#gpu-path-input").value.trim() }),
        });
        app.state.probes = { ...(app.state.probes || {}), [device]: entry };
        renderProbe(app.state);
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
      notify("历史记录已刷新", app.state?.output_root || "power-runs", "success", 2500);
    });

    const savedToolFields = {
      "import-log-path": "android-power-import-log-path",
      "import-rules-path": "android-power-import-rules-path",
      "import-match-input": "android-power-import-match",
      "archive-attachments-input": "android-power-archive-attachments",
      "archive-output-path": "android-power-archive-output",
      "portable-output-directory": "android-power-portable-output",
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
      localStorage.setItem("android-power-import-log-path", payload.log_path);
      localStorage.setItem("android-power-import-rules-path", payload.rules_path);
      localStorage.setItem("android-power-import-match", payload.match);
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
      localStorage.setItem("android-power-archive-attachments", payload.attachments);
      localStorage.setItem("android-power-archive-output", payload.output_path);
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
      if (!confirm(`确认重新构建便携包？\n\n输出目录：${outputDirectory || "dist\\mobile-power-profiler-portable"}\n已有同名目录和 ZIP 会被替换。`)) return;
      localStorage.setItem("android-power-portable-output", outputDirectory);
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
  switchView(app.activeView);
  pollLoop();
})();
